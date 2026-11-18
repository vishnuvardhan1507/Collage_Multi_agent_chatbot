"""Research alerts use isolated databases and mocked publications/LLM/SMTP."""
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from routes.alert_routes import alert_bp
from db.init_db import init_db
from db.migrate import migrate
from tools import alerts, research_updates as research
from tools.db_tool import get_connection, schema_metadata
from tools.operations import OperationError

NOW = datetime(2026, 11, 9, 7, 0, tzinfo=ZoneInfo('Asia/Kolkata'))


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.base.name) / 'fixture.db'
        init_db(cls.fixture)

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.db'
        shutil.copyfile(self.fixture, self.path)
        self.app = Flask(__name__)
        self.app.config.update(DATABASE_PATH=str(self.path), COLLEGE_TIMEZONE='Asia/Kolkata',
                               CHAT_MEMORY_DIR=str(Path(self.temp.name)/'memory'),
                               JWT_SECRET_KEY='isolated-research-test-key-with-at-least-thirty-two-characters')
        JWTManager(self.app)
        self.app.register_blueprint(alert_bp)
        context = self.app.app_context(); context.push(); self.addCleanup(context.pop)
        with closing(get_connection()) as conn, conn:
            conn.execute('DELETE FROM alert_rules')
            conn.execute("UPDATE research_sources SET enabled=1 WHERE id='nature-mi'")
        clock = patch('tools.alerts.college_now', return_value=NOW)
        clock.start(); self.addCleanup(clock.stop)

    def read(self, sql, args=()):
        with closing(get_connection()) as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def write(self, sql, args=()):
        with closing(get_connection()) as conn, conn:
            conn.execute(sql, args)

    def paper(self, number=1, courses=None):
        evidence = {'course_ids': courses or ['CS301'], 'findings': 'Authors report improved measured performance.',
                    'recognition': 'Official research highlight.', 'limitations': 'Limited to this experiment.',
                    'article_url': 'https://www.nature.com/articles/example',
                    'recognition_url': 'https://www.nature.com/highlight/example'}
        self.write('INSERT INTO research_publications VALUES(?,?,?,?,?,?,?)',
                   (f'10.1234/{number}', 'nature-mi', f'Paper {number}', '2026-11-08',
                    'qualified', json.dumps(evidence), NOW.isoformat()))

    def evaluate(self, now=NOW):
        with closing(get_connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            alerts.evaluate_due(conn, now)

    def digests(self, user='STU001'):
        return self.read('SELECT * FROM subject_update_digests WHERE user_id=?', (user,))

    def test_automatic_without_subscription_and_repeatable_migration(self):
        self.paper(); self.evaluate()
        self.assertTrue(self.digests())
        self.assertEqual({r['user_id'] for r in self.read('SELECT * FROM subject_update_digests')},{'STU001','STU002'})
        self.assertIsNone(migrate(self.path))
        self.assertFalse(self.read('PRAGMA foreign_key_check'))
        for name in ('research_publications','subject_update_digests','academic_notices','academic_notice_candidates'):
            self.assertNotIn(name,str(schema_metadata()))

    def test_curriculum_includes_completed_and_pending(self):
        courses=self.read("SELECT course_id FROM course_registrations WHERE student_id='STU001' AND status IN ('pending','completed')")
        self.paper(courses=[courses[0]['course_id']]);self.evaluate()
        self.assertTrue(self.digests())

    def test_three_daily_items_and_no_repeat(self):
        for n in range(5):self.paper(n)
        self.evaluate();self.evaluate()
        self.assertEqual(len(self.digests()),1)
        self.assertEqual(len(json.loads(self.digests()[0]['items_json'])),3)
        self.write("UPDATE notification_outbox SET status='sent'")
        later=NOW+timedelta(days=1)
        self.write('UPDATE research_publications SET checked_at=?',(later.isoformat(),))
        self.evaluate(later)
        items=[i['id'] for d in self.digests() for i in json.loads(d['items_json'])]
        self.assertEqual(len(items),5);self.assertEqual(len(set(items)),5)

    def test_daily_time_empty_day_and_recovery(self):
        self.evaluate();self.assertFalse(self.digests())
        self.paper()
        self.evaluate(NOW.replace(hour=5,minute=59));self.assertFalse(self.digests())
        self.evaluate(NOW+timedelta(hours=10));self.assertTrue(self.digests())
        self.assertEqual(self.digests()[0]['day'],NOW.date().isoformat())

    def test_ist_boundary_with_utc_input(self):
        self.paper()
        with closing(get_connection()) as conn,conn:
            research.queue_digests(conn,NOW.replace(hour=5,minute=59).astimezone(ZoneInfo('UTC')),{'STU001'})
            self.assertIsNone(conn.execute('SELECT * FROM subject_update_digests').fetchone())
            research.queue_digests(conn,NOW.astimezone(ZoneInfo('UTC')),{'STU001'})
            self.assertIsNotNone(conn.execute('SELECT * FROM subject_update_digests').fetchone())

    def refresh(self, now=NOW):
        message = self.read("SELECT * FROM notification_outbox WHERE user_id='STU001' AND event_key LIKE 'subject-update:%'")[0]
        with closing(get_connection()) as conn, conn:
            return research.refresh_email(conn,message,now)

    def test_curriculum_removal_suppresses_but_completion_does_not(self):
        self.paper();self.evaluate()
        self.write("UPDATE course_registrations SET status='completed' WHERE student_id='STU001' AND course_id='CS301'")
        self.assertTrue(self.refresh())
        self.write("DELETE FROM course_registrations WHERE student_id='STU001' AND course_id='CS301'")
        self.assertFalse(self.refresh())

    def test_personal_alert_deletion_does_not_affect_system_email(self):
        rule=alerts.create_alert('STU001','student','class_reminder',{'course_id':'CS301'})
        self.paper();self.evaluate()
        alerts.delete_alert('STU001','student',rule['id'])
        self.assertTrue(self.refresh())
        self.assertTrue(self.digests())

    def test_retracted_stale_and_disabled_sources_not_delivered(self):
        self.paper();self.evaluate()
        self.write("UPDATE research_publications SET status='rejected'")
        self.assertFalse(self.refresh())
        self.write("UPDATE research_publications SET status='qualified'")
        self.assertFalse(self.refresh(NOW+timedelta(days=2)))
        self.write('UPDATE research_sources SET enabled=0')
        self.assertFalse(self.refresh())

    def test_account_isolation_and_no_course_relevance(self):
        self.paper(courses=['CE301']);self.evaluate()
        self.assertFalse(self.digests())
        self.assertFalse(alerts.list_alerts('STU001','student')['recent'])
        self.assertFalse(self.digests('FAC001'))

    def test_day_expiry(self):
        self.paper();self.evaluate()
        self.assertFalse(self.refresh(NOW+timedelta(days=1)))

    def test_recent_history_latest_five_and_owner(self):
        self.paper()
        for i in range(7):
            day=(NOW+timedelta(days=i)).date().isoformat()
            key=f'subject-update:STU001:{day}'
            self.write('INSERT INTO subject_update_digests(event_key,user_id,day,items_json,created_at) VALUES(?,?,?,?,?)',
                       (key,'STU001',day,'[]',day+' 01:00:00'))
            self.write('INSERT INTO notification_outbox(event_key,user_id,subject,body) VALUES(?,?,?,?)',
                       (key,'STU001',f'Update {i}','fixture'))
        recent=alerts.list_alerts('STU001','student')['recent']
        self.assertEqual(len(recent),5)
        self.assertEqual(recent[0]['subject'],'Update 6')
        self.assertTrue(all(r['system_generated'] for r in recent))
        self.assertFalse(alerts.list_alerts('STU002','student')['recent'])

    def test_migration_archives_legacy_preserves_history(self):
        self.write("INSERT INTO alert_rules(id,user_id,kind,config_json) VALUES(99,'STU001','research_updates','{}')")
        self.write("INSERT INTO research_digests VALUES('research:STU001:old','STU001','2026-W45','[]','[99]')")
        self.write("INSERT INTO notification_outbox(event_key,user_id,subject,body,status) VALUES('research:STU001:old','STU001','Old','Old','sent')")
        self.write("INSERT INTO alert_occurrences(rule_id,user_id,event_key,subject,body) VALUES(99,'STU001','research:STU001:old','Old','Old')")
        for table in ('subject_update_digests','academic_notice_candidates','academic_notices','academic_notice_sources'):
            self.write('DROP TABLE '+table)
        self.write('DELETE FROM schema_migrations WHERE version=19')
        saved=migrate(self.path)
        self.assertTrue(Path(saved).exists())
        self.assertIsNone(migrate(self.path))
        self.assertEqual(self.read('SELECT active FROM alert_rules WHERE id=99')[0]['active'],0)
        self.assertEqual(self.read('SELECT status FROM notification_outbox')[0]['status'],'sent')
        self.assertEqual(len(alerts.list_alerts('STU001','student')['recent']),1)
        self.assertFalse(alerts.list_alerts('STU001','student')['alerts'])
        with self.assertRaises(OperationError):alerts.delete_alert('STU001','student',99)
        self.assertFalse(self.read('PRAGMA foreign_key_check'))

    def test_notice_evidence_relevance_and_no_adoption_claim(self):
        source={'id':'aicte-curriculum','title':'AICTE','domains':['aicte.gov.in']}
        url='https://www.aicte.gov.in/notice'
        text='Updated machine learning curriculum published 8 November 2026. The recommendation introduces substantial new material on model evaluation and responsible AI.'
        decision={'qualified':True,'title':'Updated machine learning curriculum','published':'2026-11-08',
                  'publication_quote':'Updated machine learning curriculum published 8 November 2026.',
                  'evidence_quote':'The recommendation introduces substantial new material on model evaluation and responsible AI.',
                  'course_ids':['CS301'],'findings':'New model evaluation content recommended.',
                  'significance':'Substantive curriculum recommendation.','limitations':'Institutional adoption unknown.'}
        with patch.object(research,'fetch',return_value=text),patch.object(research,'candidates_for',return_value=[{'course_id':'CS301'}]),patch('agents.llm.invoke_json',return_value=decision):
            evidence,reason=research.assess_notice(url,source,[],NOW)
            self.assertEqual(reason,'qualified')
            self.write('INSERT INTO academic_notices VALUES(?,?,?,?,?,?,?)',
                       ('notice1',source['id'],decision['title'],decision['published'],'qualified',json.dumps(evidence),NOW.isoformat()))
            self.evaluate()
            self.assertIn('not confirmation', self.read("SELECT body FROM notification_outbox WHERE user_id='STU001'")[0]['body'])
            self.assertTrue(self.refresh())
            decision['publication_quote']='Invented announcement text that is not on the source page.'
            with self.assertRaises(ValueError):research.assess_notice(url,source,[],NOW)

    def test_notice_discovery_rejects_arbitrary_links_and_records_failure(self):
        with patch.object(research,'fetch',return_value='<a href="https://www.aicte.gov.in/notice">New curriculum</a>'),patch('agents.llm.invoke_json',return_value={'urls':['https://evil.test/notice']}):
            research.collect_notices_due(NOW)
        self.assertEqual(self.read("SELECT status FROM research_checkpoints WHERE name='notices'")[0]['status'],'complete_with_errors')
        self.assertFalse(self.read('SELECT * FROM academic_notice_candidates'))
        self.assertFalse(self.read('SELECT * FROM notification_outbox'))

    def test_legacy_sent_papers_are_not_repeated(self):
        self.paper()
        evidence=json.loads(self.read('SELECT evidence_json FROM research_publications')[0]['evidence_json'])
        old=[{'doi':'10.1234/1','evidence':evidence}]
        self.write('INSERT INTO research_digests VALUES(?,?,?,?,?)',('research:STU001:old','STU001','2026-W45',json.dumps(old),'[]'))
        self.write("INSERT INTO notification_outbox(event_key,user_id,subject,body,status) VALUES('research:STU001:old','STU001','Old','Old','sent')")
        self.evaluate();self.assertFalse(self.digests())

    def test_source_url_restrictions(self):
        for url in ('http://nature.com/a','https://nature.com.attacker.test/a','https://127.0.0.1/a','https://user@nature.com/a','https://nature.com:444/a'):
            self.assertFalse(research.allowed_url(url, ['nature.com']))
        self.assertTrue(research.allowed_url('https://www.nature.com/a',['nature.com']))

    def test_authenticated_api_boundaries(self):
        client=self.app.test_client()
        body={'kind':'research_updates','config':{'course_ids':None}}
        self.assertEqual(client.post('/api/alerts',json=body).status_code,401)
        for user in ('STU001','FAC001'):
            headers={'Authorization':'Bearer '+create_access_token(identity=user)}
            self.assertEqual(client.post('/api/alerts',json=body,headers=headers).status_code,400)
        self.assertFalse(self.read('SELECT * FROM alert_rules'))

    def test_canonical_identifiers_deduplicate_different_dois(self):
        self.paper(1);self.paper(2)
        for row in self.read('SELECT doi,evidence_json FROM research_publications'):
            evidence=json.loads(row['evidence_json']);evidence['canonical_id']='publisher:article-123'
            self.write('UPDATE research_publications SET evidence_json=? WHERE doi=?',(json.dumps(evidence),row['doi']))
        self.evaluate()
        self.assertEqual(len(json.loads(self.digests()[0]['items_json'])),1)

    def assessment_fixture(self):
        source={'issns':['1234-5678'], 'domains':['nature.com'], 'policy_url':'https://nature.com/policy',
                'policy_evidence':'Original research receives external peer review.',
                'recognition_urls':['https://nature.com/highlights']}
        record={'DOI':'10.1234/example','ISSN':['1234-5678'],'type':'journal-article',
                'title':['Important Study'], 'published':{'date-parts':[[2026,11,8]]},
                'resource':{'primary':{'URL':'https://nature.com/article'}}}
        publication='Important Study is an original research article published 8 November 2026.'
        finding='The study measured a 40 percent reduction against the stated baseline.'
        recognition='Research highlight: Important Study reports substantial experimental progress.'
        pages={'https://nature.com/article':publication+' '+finding,
               'https://nature.com/highlights':recognition}
        decision={'qualified':True,'reviewed_original':True,'substantial':True,
                  'publication_date':'2026-11-08',
                  'publication_quote':publication,'evidence_quote':finding,'recognition_quote':recognition,
                  'recognition_url':'https://nature.com/highlights','course_ids':['CS301'],
                  'findings':'Reported baseline improvement.','recognition':'Research highlight.',
                  'limitations':'One benchmark only.'}
        return source,record,pages,decision

    def test_grounded_screening_and_fabricated_quotes(self):
        source,record,pages,decision=self.assessment_fixture()
        with patch.object(research,'fetch',side_effect=lambda url,*a,**kw:pages[url]), \
             patch.object(research,'candidates_for',return_value=[{'course_id':'CS301','units_json':'learning'}]), \
             patch('agents.llm.invoke_json',return_value=decision):
            evidence,reason=research.assess(record,source,[],NOW)
            self.assertEqual(reason,'qualified')
            decision['evidence_quote']='An invented result that was never on the page.'
            with self.assertRaises(ValueError):research.assess(record,source,[],NOW)

    def test_rejects_weak_unreviewed_and_unsupported(self):
        source,record,pages,decision=self.assessment_fixture()
        for kind in ('posted-content','editorial','book'):
            record['type']=kind
            self.assertIsNone(research.assess(record,source,[],NOW)[0])
        record['type']='journal-article'
        record['published']['date-parts']=[[2025,1,1]]
        self.assertIsNone(research.assess(record,source,[],NOW)[0])
        record['published']['date-parts']=[[2026,11,8]]
        with patch.object(research,'fetch',side_effect=lambda url,*a,**kw:pages[url]), \
             patch.object(research,'candidates_for',return_value=[{'course_id':'CS301','units_json':'learning'}]), \
             patch('agents.llm.invoke_json',return_value={**decision,'substantial':False}):
            self.assertIsNone(research.assess(record,source,[],NOW)[0])

    def test_collection_checkpoint_and_network_outside_transaction(self):
        def fail(*args,**kwargs):
            # A second writer can acquire a write transaction while fetching.
            with closing(get_connection()) as conn,conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute("UPDATE research_sources SET enabled=0 WHERE id='acm-tosem'")
            raise ValueError('fixture offline')
        with patch.object(research,'fetch',side_effect=fail) as network:
            research.collect_due(NOW)
            count=network.call_count
            research.collect_due(NOW)
            self.assertEqual(network.call_count,count)
        self.assertEqual(self.read('SELECT cursor FROM research_checkpoints')[0]['cursor'],1)

    def test_inaccessible_paper_does_not_block_journal(self):
        source=self.read("SELECT * FROM research_sources WHERE id='acm-tosem'")[0]
        self.write("UPDATE research_sources SET enabled=1,policy_evidence='peer reviewed' WHERE id='acm-tosem'")
        records=[{'DOI':'10.1234/blocked','title':['Blocked'],'published':{'date-parts':[[2026,11,8]]}},
                 {'DOI':'10.1234/accessible','title':['Accessible'],'published':{'date-parts':[[2026,11,8]]}}]
        with patch.object(research,'fetch',return_value=json.dumps({'message':{'items':records}})),patch.object(research,'assess',side_effect=[ValueError('inaccessible'),(None,'weak evidence')]) as assess:
            research.collect_due(NOW)
            self.write("UPDATE research_checkpoints SET lease_until=0 WHERE name='collection'")
            research.collect_due(NOW)
            self.assertEqual(assess.call_count,2)
        rows=self.read("SELECT doi,status FROM research_publications ORDER BY doi")
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['status'],'rejected')
        self.assertEqual(rows[1]['status'],'unverified')

    def test_notice_collection_uses_persisted_candidates_and_single_daily_check(self):
        url='https://www.aicte.gov.in/notice'
        evidence={'title':'Fixture curriculum update','published':'2026-11-08','course_ids':['CS301'],
                  'findings':'New topic','recognition':'Substantive change','limitations':'Unconfirmed adoption','article_url':url}
        with patch.object(research,'fetch',return_value=f'<a href="{url}">Curriculum update</a>') as network,patch('agents.llm.invoke_json',return_value={'urls':[url]}),patch.object(research,'assess_notice',return_value=(evidence,'qualified')) as assess:
            research.collect_notices_due(NOW)
            self.write("UPDATE research_checkpoints SET lease_until=0 WHERE name='notices'")
            research.collect_notices_due(NOW)
            research.collect_notices_due(NOW)
            self.assertEqual(network.call_count,1)
            self.assertEqual(assess.call_count,1)
        self.assertEqual(self.read("SELECT status FROM research_checkpoints WHERE name='notices'")[0]['status'],'complete')
        self.evaluate();self.assertTrue(self.digests())

    def test_chat_cannot_create_subscription_and_prompt_explains_automatic(self):
        from agents.prompts import GUARDRAIL_AGENT_PROMPT, SUPERVISOR_AGENT_PROMPT
        with self.assertRaises(OperationError):
            alerts.chat_update('STU001','student',{'action':'create','kind':'research_updates','config':{}})
        self.assertFalse(self.read('SELECT * FROM alert_rules'))
        self.assertIn('automatic', GUARDRAIL_AGENT_PROMPT.lower())
        self.assertIn('do not return alert_update', SUPERVISOR_AGENT_PROMPT)

    def test_concurrent_evaluation_and_smtp_retry(self):
        self.paper()
        def run():
            with self.app.app_context():
                self.evaluate()
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _:run(),range(2)))
        self.assertEqual(len(self.digests()),1)
        self.write("DELETE FROM course_registrations WHERE student_id='STU002'")
        self.write("UPDATE notification_outbox SET status='suppressed' WHERE user_id!='STU001'")
        from tools.notifications import deliver_one
        with patch('auth.mail.send_email',side_effect=[OSError('fixture failure'),None]) as smtp:
            deliver_one()
            self.assertEqual(self.read('SELECT status FROM notification_outbox')[0]['status'],'pending')
            self.write('UPDATE notification_outbox SET next_attempt=0')
            deliver_one()
            self.assertEqual(self.read('SELECT status FROM notification_outbox')[0]['status'],'sent')
            self.assertEqual(smtp.call_args_list[0].args[3],smtp.call_args_list[1].args[3])
            self.assertIn('Limitations:',smtp.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
