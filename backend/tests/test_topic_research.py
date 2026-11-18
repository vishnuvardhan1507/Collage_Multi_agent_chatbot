"""On-demand reference emails: isolated SQLite, fake pages/model and mocked SMTP."""
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flask import Flask
from db.init_db import init_db
from db.migrate import migrate
from tools import topic_research as topic, alerts
from tools.db_tool import get_connection, schema_metadata
from tools.operations import OperationError


class TopicResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=tempfile.TemporaryDirectory()
        cls.fixture=Path(cls.base.name)/'base.db'
        init_db(cls.fixture)

    @classmethod
    def tearDownClass(cls):cls.base.cleanup()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'test.db';shutil.copyfile(self.fixture,self.path)
        self.app=Flask(__name__);self.app.config.update(DATABASE_PATH=str(self.path),CHAT_MEMORY_DIR=str(Path(self.tmp.name)/'memory'),COLLEGE_TIMEZONE='Asia/Kolkata')
        ctx=self.app.app_context();ctx.push();self.addCleanup(ctx.pop)
        self.write('DELETE FROM alert_rules')

    def read(self,sql,args=()):
        with closing(get_connection()) as c:return [dict(r) for r in c.execute(sql,args)]

    def write(self,sql,args=()):
        with closing(get_connection()) as c,c:c.execute(sql,args)

    def queue(self,user='STU001',request_id='request1',request=None):
        with patch('agents.llm.invoke_json',side_effect=lambda p,v: {'related':True,'parent_refs':[v['parents'][0]['ref']]}):
            return topic.enqueue_request(user,'student','session1',request or {'course':'CS302','topic':'SQL joins','goal':'Worked examples'},request_id,'research joins and email links')

    def result(self):
        return {'outcome':'found','references':[{'url':'https://www.postgresql.org/docs/current/tutorial-join.html',
            'title':'Joins Between Tables','source':'PostgreSQL','usefulness':'Explains joins with examples.',
            'quote':'Queries can access multiple tables at once.'}],'failures':[]}

    def test_validation_and_student_scope(self):
        for req in ({'topic':[]},{'course':'CS302','topic':'x','url':'https://evil.test'}, {'topic':'x'*301}):
            with self.assertRaises(ValueError):topic.validate(req)
        with self.assertRaises(PermissionError):topic.enqueue_request('FAC001','faculty','s',{},'r')
        with self.assertRaises(OperationError):self.queue(request={'course':'EC301','topic':'circuits'})
        with self.assertRaises(OperationError):self.queue(request={'topic':'joins'})
        self.assertFalse(self.read('SELECT * FROM research_jobs'))

    def test_grounding_rejects_unrelated_or_invalid_parent_refs(self):
        for result in ({'related':False,'parent_refs':[]},{'related':True,'parent_refs':['invented']}):
            with patch('agents.llm.invoke_json',return_value=result),self.assertRaises(OperationError):
                topic.enqueue_request('STU001','student','s',{'course':'CS302','topic':'unrelated'},'r')
        self.assertFalse(self.read('SELECT * FROM research_jobs'))

    def test_queue_is_immediate_without_email_and_idempotent_receipts(self):
        first=self.queue();second=self.queue(request_id='request2')
        self.assertEqual(first['actions'],['research_queued'])
        self.assertIn('queued',second['response'])
        self.assertEqual(len(self.read('SELECT * FROM research_jobs')),1)
        self.assertEqual(len(self.read('SELECT * FROM research_job_receipts')),2)
        self.assertFalse(self.read('SELECT * FROM notification_outbox'))
        with patch('agents.llm.invoke_json') as model:
            self.queue(request_id='request1')
            model.assert_not_called()
        self.assertIsNone(topic.receipt('STU002','request1'))

    def test_delivery_html_plaintext_and_retry(self):
        self.queue()
        with patch.object(topic,'research',return_value=self.result()):self.assertTrue(topic.run_one())
        from tools.notifications import deliver_one
        with patch('auth.mail.send_email',side_effect=[OSError('offline'),None]) as smtp:
            deliver_one();self.assertEqual(self.read('SELECT status FROM notification_outbox')[0]['status'],'pending')
            self.write('UPDATE notification_outbox SET next_attempt=0')
            deliver_one();self.assertEqual(self.read('SELECT status FROM notification_outbox')[0]['status'],'sent')
            self.assertEqual(smtp.call_args_list[0].args[3],smtp.call_args_list[1].args[3])
            self.assertIn('<a href="https://',smtp.call_args.kwargs['html_body'])
            self.assertIn('https://',smtp.call_args.args[2])
        self.assertEqual(len(self.read('SELECT * FROM notification_outbox')),1)

    def test_actual_email_mime_contains_clickable_html_and_text_fallback(self):
        from auth.mail import send_email
        self.app.config.update(SMTP_HOST='localhost',SMTP_SECURITY='none',SMTP_FROM_EMAIL='fixture@example.test')
        with patch('auth.mail._send') as transport:
            send_email('student@example.test','References','https://www.postgresql.org/', '<fixture@local>',
                       html_body='<a href="https://www.postgresql.org/">PostgreSQL</a>')
        message=transport.call_args.args[0]
        self.assertIn('https://www.postgresql.org/',message.get_body(preferencelist=('plain',)).get_content())
        self.assertIn('<a href=',message.get_body(preferencelist=('html',)).get_content())

    def test_worker_lease_concurrency_and_network_outside_transaction(self):
        self.queue()
        def research(job):
            self.write("UPDATE research_jobs SET error='test writer acquired lock' WHERE id=?",(job['id'],))
            return self.result()
        def run():
            with self.app.app_context():return topic.run_one()
        with patch.object(topic,'research',side_effect=research) as model,ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(lambda _:run(),range(2)))
            self.assertEqual(model.call_count,1)
        self.assertEqual(len(self.read('SELECT * FROM notification_outbox')),1)

    def test_recovery_and_exhausted_interpretation_retry(self):
        self.queue()
        self.write("UPDATE research_jobs SET status='running',lease_until=0")
        with patch.object(topic,'research',side_effect=ValueError('bad model')):
            for _ in range(3):
                self.write('UPDATE research_jobs SET next_attempt=0')
                topic.run_one()
        self.assertEqual(self.read('SELECT status FROM research_jobs')[0]['status'],'failed')
        self.assertEqual(len(self.read('SELECT * FROM notification_outbox')),1)
        self.assertIn('could not be completed',self.read('SELECT body FROM notification_outbox')[0]['body'])

    def test_curriculum_recheck_and_account_recent_isolation(self):
        self.queue()
        with patch.object(topic,'research',return_value=self.result()):topic.run_one()
        self.assertEqual(len(alerts.list_alerts('STU001','student')['recent']),1)
        self.assertFalse(alerts.list_alerts('STU002','student')['recent'])
        self.write("DELETE FROM course_registrations WHERE student_id='STU001' AND course_id='CS302'")
        from tools.notifications import deliver_one
        with patch('auth.mail.send_email') as smtp:
            deliver_one();smtp.assert_not_called()
        self.assertEqual(self.read('SELECT status FROM notification_outbox')[0]['status'],'suppressed')

    def test_deleted_history_is_not_recreated_and_job_is_not_replayed(self):
        self.queue()
        topic.reconcile('STU001');topic.reconcile('STU001')
        from tools.memory_tool import read_memory,delete_session
        self.assertEqual(len(read_memory('STU001')['sessions'][0]['messages']),1)
        delete_session('STU001','session1');topic.reconcile('STU001')
        self.assertFalse(read_memory('STU001')['sessions'])
        self.assertEqual(len(self.read('SELECT * FROM research_jobs')),1)

    def test_discovery_grounded_references_and_no_generated_urls(self):
        self.queue();job=self.read('SELECT * FROM research_jobs')[0]
        index='https://www.postgresql.org/docs/current/tutorial.html'
        target='https://www.postgresql.org/docs/current/tutorial-join.html'
        content='Joins Between Tables. Queries can access multiple tables at once. ' * 4
        def fetch(url,*a,**kw):
            if 'api.crossref.org' in url:return json.dumps({'message':{'items':[]}})
            return f'<a href="{target}">Joins Between Tables</a>' if url==index else content
        answers=[{'source_ids':['postgresql']},{'urls':[target]}, {'references':[{'url':target,'title':'Joins Between Tables','usefulness':'Explains joins with examples.','quote':'Queries can access multiple tables at once.'}]}]
        with patch.object(topic,'fetch',side_effect=fetch),patch('agents.llm.invoke_json',side_effect=answers):
            result=topic.research(job)
        self.assertEqual(len(result['references']),1)
        answers[-1]['references'][0]['url']='https://evil.test'
        with patch.object(topic,'fetch',side_effect=fetch),patch('agents.llm.invoke_json',side_effect=answers),self.assertRaises(ValueError):
            topic.research(job)
        answers[-1]['references'][0]['url']=target
        answers[-1]['references'][0]['quote']='This fabricated quotation does not exist in the retrieved page.'
        with patch.object(topic,'fetch',side_effect=fetch),patch('agents.llm.invoke_json',side_effect=answers),self.assertRaises(ValueError):
            topic.research(job)

    def test_unavailable_sources_are_not_reported_as_no_match(self):
        self.queue();job=self.read('SELECT * FROM research_jobs')[0]
        with patch('agents.llm.invoke_json',return_value={'source_ids':['postgresql']}),patch.object(topic,'fetch',side_effect=OSError('offline')):
            result=topic.research(job)
        self.assertEqual(result['outcome'],'source_unavailable');self.assertFalse(result['references'])
        self.assertIn('could not be accessed',topic.render(job,result))

    def test_fabricated_quotes_and_html_escape(self):
        result=self.result();result['references'][0]['title']='<script>bad</script>'
        body=topic.html_body({'topic':'<b>topic</b>'},result)
        self.assertNotIn('<script>',body);self.assertIn('&lt;script&gt;',body)

    def test_migration_repeatability_and_private_tables(self):
        self.queue();self.assertIsNone(migrate(self.path))
        self.assertEqual(len(self.read('SELECT * FROM research_jobs')),1)
        self.assertFalse(self.read('PRAGMA foreign_key_check'))
        self.assertNotIn('research_jobs',str(schema_metadata()))

    def test_agent_paraphrases_and_context_route_without_sql(self):
        from agents.graph import build_graph
        from agents.prompts import GUARDRAIL_AGENT_PROMPT
        for question in ('Find learning links about SQL joins and email me','Focus on LEFT JOIN examples'):
            def model(prompt,payload,**kwargs):
                if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college','modules':['research']}
                if 'parents' in payload:return {'related':True,'parent_refs':[payload['parents'][0]['ref']]}
                self.assertIn('research_request',prompt)
                return {'route':'direct','research_request':{'course':'CS302','topic':'SQL joins','goal':'examples'}}
            state=dict(user_id='STU001',role='student',session_id='s',request_id=question,query=question,
                       chat_history=[{'role':'user','content':'Research database joins and email me'}],
                       current_date='2026-11-18',timezone='Asia/Kolkata',agent_trace=[],actions=[],sources=[],retry_count=0)
            with patch('agents.llm.invoke_json',side_effect=model),patch('agents.graph.execute_query') as sql:
                result=build_graph().invoke(state)
            sql.assert_not_called();self.assertIn('research_queued',result['actions'])


if __name__=='__main__':unittest.main()
