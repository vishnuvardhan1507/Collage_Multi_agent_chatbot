"""Class-day reminders: isolated databases and mocked interpretation/email."""
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_faculty_alerts as fixtures
from agents import llm
from agents.prompts import GUARDRAIL_AGENT_PROMPT
from db.init_db import init_db
from db.migrate import migrate
from tools import alerts
from tools.db_tool import get_connection, fetch_all
from tools.notifications import deliver_one
from tools.operations import OperationError


class ClassReminderTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.FacultyAlertTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.FacultyAlertTests.tearDownClass.__func__)
    setUp = fixtures.FacultyAlertTests.setUp
    write = fixtures.FacultyAlertTests.write
    headers = fixtures.FacultyAlertTests.headers

    def moment(self, day='2026-10-26', time='07:00'):
        return datetime.fromisoformat(day+'T'+time).replace(tzinfo=ZoneInfo('Asia/Kolkata'))

    def create(self, user='STU001', course='Machine Learning', when=None):
        with patch.object(alerts, 'college_now', return_value=when or self.moment(time='06:59')):
            return alerts.create_alert(user, 'student' if user.startswith('STU') else 'faculty',
                                       'class_reminder', {'course_id':course})

    def evaluate(self, when):
        with closing(get_connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            alerts.evaluate_due(conn, when)

    def occurrences(self):
        return fetch_all('SELECT * FROM alert_occurrences ORDER BY id')

    def test_timing_recurrence_edits_and_dedup(self):
        rule = self.create()
        self.assertEqual(rule['config'], {'course_id':'CS301'})
        self.evaluate(self.moment(time='06:59'))
        self.assertEqual(self.occurrences(), [])
        self.evaluate(self.moment())
        self.evaluate(self.moment(time='08:00'))
        self.assertEqual(len(self.occurrences()), 1)
        self.assertIn('CS301', self.occurrences()[0]['body'])
        self.assertIn('09:00–10:00', self.occurrences()[0]['body'])
        with patch.object(alerts, 'college_now', return_value=self.moment()):
            alerts.update_alert('STU001','student',rule['id'],'class_reminder',{'course_id':None})
        self.assertEqual(len(self.occurrences()), 1)
        self.evaluate(self.moment('2026-10-29'))
        self.assertEqual(len(self.occurrences()), 2)

    def test_late_creation_missing_expired_weekends(self):
        self.create(course=None, when=self.moment(time='10:30'))
        body=self.occurrences()[0]['body']
        self.assertNotIn('09:00', body)
        self.assertIn('11:00', body)
        self.create(when=self.moment(time='09:00'))
        self.assertEqual(len(self.occurrences()), 1)
        self.write("UPDATE course_timetable SET weekday=5 WHERE course_id='CS301' AND weekday=0")
        self.evaluate(self.moment('2026-10-31'))
        self.assertEqual(len(self.occurrences()), 1)
        self.evaluate(self.moment('2026-11-01'))
        self.write("UPDATE course_timetable SET effective_to='2026-10-25'")
        self.evaluate(self.moment('2026-11-02'))
        self.assertEqual(len(self.occurrences()), 1)
        self.write('DELETE FROM course_timetable')
        self.create(when=self.moment())
        self.assertEqual(len(self.occurrences()), 1)

    def test_permissions_changes_and_validation(self):
        for user in ('STU001','FAC001'):
            with self.assertRaises(OperationError): self.create(user,'EC301')
        for value in (123, True, {}, "CS301' OR 1=1"):
            with self.assertRaises(OperationError): self.create(course=value)
        with self.assertRaises(OperationError):
            alerts.create_alert('STU001','student','class_reminder',{'course_id':None,'sql':'SELECT 1'})
        self.create(); self.create('FAC001')
        self.write("UPDATE course_registrations SET status='completed' WHERE student_id='STU001' AND course_id='CS301'")
        self.write("UPDATE courses SET faculty_id='FAC002' WHERE course_id='CS301'")
        self.evaluate(self.moment())
        self.assertEqual(self.occurrences(), [])
        self.assertNotIn('CS301',[c['course_id'] for c in alerts.list_alerts('STU001','student')['reminder_courses']])
        self.assertNotIn('CS301',[c['course_id'] for c in alerts.list_alerts('FAC001','faculty')['reminder_courses']])
        self.write("UPDATE courses SET course_name='Operating Systems' WHERE course_id='CS302'")
        with self.assertRaises(OperationError): self.create('FAC001','Operating Systems')

    def test_all_courses_multiple_sessions_and_worker_failure(self):
        self.write("INSERT INTO course_timetable(course_id,weekday,start_time,end_time,effective_from,effective_to) VALUES('CS301',0,'14:00','15:00','2026-10-26','2026-10-26')")
        self.create(course=None)
        self.create('FAC001', None)
        with patch.object(alerts,'college_now',return_value=self.moment()), patch('auth.mail.send_email',side_effect=RuntimeError('simulated')):
            self.assertTrue(deliver_one())
        self.assertEqual(len(self.occurrences()), 2)
        body=self.occurrences()[0]['body']
        self.assertLess(body.index('09:00'),body.index('11:00'))
        self.assertLess(body.index('11:00'),body.index('14:00'))
        self.assertEqual(len(fetch_all('SELECT * FROM alert_rules')), 2)
        self.write('UPDATE notification_outbox SET next_attempt=0')
        with patch.object(alerts,'college_now',return_value=self.moment()), patch('auth.mail.send_email') as mail:
            self.assertTrue(deliver_one())
            self.assertEqual(mail.call_count,1)
        self.assertEqual(len(self.occurrences()), 2)

    def test_selected_courses_support_any_and_all(self):
        with patch.object(alerts,'college_now',return_value=self.moment(time='06:59')):
            any_rule=alerts.create_alert('STU001','student','class_reminder',{
                'course_ids':['CS301','CS303'],'match_mode':'any'})
        self.assertIn('CS301 or CS303',any_rule['summary'])
        self.evaluate(self.moment())
        self.assertEqual(len([row for row in self.occurrences() if row['rule_id']==any_rule['id']]),1)
        with patch.object(alerts,'college_now',return_value=self.moment(time='06:59')):
            all_rule=alerts.create_alert('STU001','student','class_reminder',{
                'course_ids':['CS301','CS303'],'match_mode':'all'})
        self.evaluate(self.moment())
        self.assertEqual(len([row for row in self.occurrences() if row['rule_id']==all_rule['id']]),1)
        self.write("UPDATE course_timetable SET weekday=1 WHERE course_id='CS303' AND weekday=0")
        with patch.object(alerts,'college_now',return_value=self.moment('2026-11-02',time='06:59')):
            strict=alerts.create_alert('STU001','student','class_reminder',{
                'course_ids':['CS301','CS303'],'match_mode':'all'})
        self.evaluate(self.moment('2026-11-02'))
        self.assertEqual([row for row in self.occurrences() if row['rule_id']==strict['id']],[])

    def test_chat_paraphrases_and_contextual_edit(self):
        for user in ('STU001','FAC001'):
            for query in ('Notify me on days I have Machine Learning','Remind me whenever ML is scheduled','Not attendance; notify me when I have that class'):
                def model(prompt,payload):
                    if prompt==GUARDRAIL_AGENT_PROMPT:
                        return {'verdict':'allow','topic_scope':'college','modules':['timetable']}
                    self.assertIn('class_reminder',prompt)
                    self.assertTrue(any(c['course_id']=='CS301' for c in payload['alert_context']['reminder_courses']))
                    return {'route':'direct','alert_update':{'action':'create','kind':'class_reminder','config':{'course_id':'CS301'}}}
                with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=AssertionError('No SQL')):
                    response=self.client.post('/api/chat',headers=self.headers(user),json={'session_id':'classes','message':query})
                self.assertEqual(response.status_code,200,response.json)
                self.assertEqual(response.json['actions'],['alert_saved'])
            role='student' if user.startswith('STU') else 'faculty'
            rule=alerts.list_alerts(user,role)['alerts'][0]
            def edit_model(prompt,payload):
                if prompt==GUARDRAIL_AGENT_PROMPT:
                    return {'verdict':'allow','topic_scope':'college','modules':['timetable']}
                self.assertTrue(payload['chat_history'])
                return {'route':'direct','alert_update':{'action':'update','id':rule['id'],'kind':'class_reminder','config':{'course_id':None}}}
            with patch.object(llm,'invoke_json',side_effect=edit_model):
                response=self.client.post('/api/chat',headers=self.headers(user),json={'session_id':'classes','message':'Change the latest reminder to all my courses'})
            self.assertEqual(response.json['actions'],['alert_saved'])
            self.assertIsNone(alerts.list_alerts(user,role)['alerts'][0]['config']['course_id'])

    def test_migration_preserves_history_and_deleted_defaults(self):
        legacy=Path(self.temp.name)/'legacy.db'
        with patch('db.migrate.migrate_class_reminders'):
            init_db(legacy)
        with closing(sqlite3.connect(legacy)) as conn,conn:
            conn.execute("DELETE FROM alert_rules WHERE user_id='STU002'")
            rule=conn.execute('SELECT id FROM alert_rules LIMIT 1').fetchone()[0]
            conn.execute("INSERT INTO alert_condition_state VALUES(?,'test',1,'2026-10-26 01:00:00')",(rule,))
            conn.execute("INSERT INTO alert_occurrences(rule_id,user_id,event_key,subject,body) VALUES(?,'STU001','preserve-event','subject','body')",(rule,))
            tables=('alert_rules','alert_condition_state','alert_occurrences','notification_outbox')
            before={t:conn.execute('SELECT * FROM '+t).fetchall() for t in tables}
        backup=migrate(legacy)
        self.assertTrue(Path(backup).exists())
        self.assertIsNone(migrate(legacy))
        with closing(sqlite3.connect(legacy)) as conn:
            for t in tables:self.assertEqual(before[t],conn.execute('SELECT * FROM '+t).fetchall())
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            conn.execute("INSERT INTO alert_rules(user_id,kind,config_json) VALUES('STU001','class_reminder','{\"course_id\":null}')")


if __name__=='__main__': unittest.main()
