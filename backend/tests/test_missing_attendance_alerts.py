"""Missing-attendance reminders use committed sessions, never model-generated SQL."""
import sqlite3
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
from tools import alerts, attendance
from tools.db_tool import get_connection, fetch_all, fetch_one
from tools.notifications import deliver_one
from tools.operations import OperationError


class MissingAttendanceTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.FacultyAlertTests.setUpClass.__func__)
    tearDownClass=classmethod(fixtures.FacultyAlertTests.tearDownClass.__func__)
    setUp=fixtures.FacultyAlertTests.setUp
    write=fixtures.FacultyAlertTests.write
    headers=fixtures.FacultyAlertTests.headers

    def now(self,day='2026-10-26',time='18:00'):
        return datetime.fromisoformat(day+'T'+time).replace(tzinfo=ZoneInfo('Asia/Kolkata'))

    def create(self,course=None,when=None):
        with patch.object(alerts,'college_now',return_value=when or self.now(time='17:59')):
            return alerts.create_alert('FAC001','faculty','attendance_missing',{'course_id':course})

    def evaluate(self,when=None):
        with closing(get_connection()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE');alerts.evaluate_due(conn,when or self.now())

    def mark(self,course):
        # Set consistent isolated starting balances before using the real save service.
        self.write('UPDATE attendance_balances SET attended=90,total=100')
        tid=fetch_one('SELECT timetable_id FROM course_timetable WHERE course_id=? AND weekday=0',(course,))['timetable_id']
        with patch.object(attendance,'college_now',return_value=self.now()):
            roster=attendance.roster('FAC001','faculty',tid,'2026-10-26')
            attendance.save('FAC001','faculty',tid,'2026-10-26',{'revision':roster['revision'],'marks':{s['student_id']:1 for s in roster['students']}})

    def test_schedule_saved_sessions_and_dedup(self):
        rule=self.create()
        self.evaluate(self.now(time='17:59'))
        self.assertEqual(fetch_all('SELECT * FROM alert_occurrences'),[])
        self.mark('CS301')
        self.evaluate();self.evaluate()
        rows=fetch_all('SELECT * FROM alert_occurrences')
        self.assertEqual(len(rows),1)
        self.assertNotIn('CS301',rows[0]['body']);self.assertIn('CS303',rows[0]['body'])
        with patch.object(alerts,'college_now',return_value=self.now()):
            alerts.update_alert('FAC001','faculty',rule['id'],'attendance_missing',{'course_id':'CS303'})
        self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),1)
        self.evaluate(self.now('2026-11-02'));self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),2)

    def test_late_creation_filters_and_empty_rosters(self):
        self.create('Machine Learning',self.now(time='20:00'))
        self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),1)
        self.write('DELETE FROM alert_occurrences');self.write('DELETE FROM notification_outbox')
        self.write("UPDATE course_timetable SET start_time='18:00',end_time='19:00' WHERE course_id='CS301'")
        self.evaluate();self.assertEqual(fetch_all('SELECT * FROM alert_occurrences'),[])
        self.evaluate(self.now(time='19:00'));self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),1)
        self.write('DELETE FROM alert_occurrences');self.write('DELETE FROM notification_outbox')
        self.write("UPDATE course_registrations SET status='completed' WHERE course_id='CS301'")
        self.evaluate(self.now(time='20:00'));self.assertEqual(fetch_all('SELECT * FROM alert_occurrences'),[])
        self.write("UPDATE course_timetable SET weekday=5 WHERE course_id='CS301' AND weekday=0")
        for day in ('2026-10-31','2026-11-01','2026-10-12'):
            self.evaluate(self.now(day));self.assertEqual(fetch_all('SELECT * FROM alert_occurrences'),[])

    def test_refresh_partial_completion_suppression_and_retry(self):
        self.create();self.evaluate();self.mark('CS301')
        with patch.object(alerts,'college_now',return_value=self.now()),patch('auth.mail.send_email',side_effect=OSError('mock')) as mail:
            deliver_one()
            body=mail.call_args.args[2]
            self.assertNotIn('CS301',body);self.assertIn('CS303',body)
        self.mark('CS303')
        self.write('UPDATE notification_outbox SET next_attempt=0')
        with patch.object(alerts,'college_now',return_value=self.now()),patch('auth.mail.send_email') as mail:
            self.assertTrue(deliver_one());mail.assert_not_called()
        self.assertEqual(fetch_one('SELECT status FROM notification_outbox')['status'],'suppressed')
        self.evaluate();self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),1)

    def test_reassignment_expiry_and_account_isolation(self):
        rule=self.create('CS301');self.evaluate()
        self.assertEqual(self.client.delete('/api/alerts/'+str(rule['id']),headers=self.headers('FAC002')).status_code,404)
        self.assertEqual(self.client.post('/api/alerts',headers=self.headers('STU001'),json={'kind':'attendance_missing','config':{'course_id':None}}).status_code,403)
        with self.assertRaises(OperationError):self.create('EC301')
        self.write("UPDATE courses SET faculty_id='FAC002' WHERE course_id='CS301'")
        with patch.object(alerts,'college_now',return_value=self.now()),patch('auth.mail.send_email') as mail:
            deliver_one();mail.assert_not_called()
        self.assertEqual(fetch_one('SELECT status FROM notification_outbox')['status'],'suppressed')
        self.create('CS303',self.now())
        with patch.object(alerts,'college_now',return_value=self.now('2026-10-27',time='00:01')),patch('auth.mail.send_email') as mail:
            deliver_one();mail.assert_not_called()
        self.assertTrue(all(r['status']=='suppressed' for r in fetch_all('SELECT status FROM notification_outbox')))

    def test_effective_dates_drafts_and_successful_delivery(self):
        from tools.memory_tool import append_turn
        append_turn('FAC001','draft','user','Mark everyone present for ML')
        self.write("UPDATE course_timetable SET effective_to='2026-10-25' WHERE course_id='CS303'")
        self.create();self.evaluate()
        rows=fetch_all('SELECT * FROM alert_occurrences')
        self.assertIn('CS301',rows[0]['body']);self.assertNotIn('CS303',rows[0]['body'])
        with patch.object(alerts,'college_now',return_value=self.now()),patch('auth.mail.send_email') as mail:
            deliver_one();self.assertEqual(mail.call_count,1)
            deliver_one();self.assertEqual(mail.call_count,1)
        self.assertEqual(fetch_one('SELECT status FROM notification_outbox')['status'],'sent')

    def test_chat_interpretation_and_correction(self):
        queries=['Remind me if I forget to mark attendance','Only Machine Learning','Not upcoming classes, remind me when attendance is not saved']
        with patch.object(alerts,'college_now',return_value=self.now(time='12:00')):
            old=alerts.create_alert('FAC001','faculty','class_reminder',{'course_id':None})
        def model(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college','modules':['attendance']}
            self.assertIn('attendance_missing',prompt)
            self.assertTrue(payload['alert_context']['assigned_courses'])
            return {'route':'direct','alert_update':{'action':'update','id':old['id'],'kind':'attendance_missing','config':{'course_id':'Machine Learning'}}}
        with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=AssertionError('No SQL')):
            for query in queries:
                response=self.client.post('/api/chat',headers=self.headers(),json={'session_id':'missing','message':query})
                self.assertEqual(response.status_code,200,response.json)
                self.assertEqual(response.json['actions'],['alert_saved'])
                self.assertIn('6:00 PM IST',response.json['response'])
        self.assertEqual(len(fetch_all('SELECT * FROM alert_rules')),1)
        self.assertEqual(fetch_one('SELECT kind FROM alert_rules')['kind'],'attendance_missing')

    def test_migration_preserves_existing_history(self):
        path=Path(self.temp.name)/'legacy.db'
        with patch('db.migrate.migrate_missing_attendance'):
            init_db(path)
        with closing(sqlite3.connect(path)) as conn,conn:
            rule=conn.execute('SELECT id FROM alert_rules LIMIT 1').fetchone()[0]
            conn.execute("INSERT INTO alert_occurrences(rule_id,user_id,event_key,subject,body) VALUES(?,'STU001','saved','subject','body')",(rule,))
            conn.execute("INSERT INTO alert_condition_state(rule_id,subject_key,matching) VALUES(?,'saved',1)",(rule,))
            tables=('alert_rules','alert_occurrences','alert_condition_state','notification_outbox')
            before={t:conn.execute('SELECT * FROM '+t).fetchall() for t in tables}
        self.assertTrue(Path(migrate(path)).exists());self.assertIsNone(migrate(path))
        with closing(sqlite3.connect(path)) as conn:
            for t in tables:self.assertEqual(before[t],conn.execute('SELECT * FROM '+t).fetchall())
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            conn.execute("INSERT INTO alert_rules(user_id,kind,config_json) VALUES('FAC001','attendance_missing','{\"course_id\":null}')")


if __name__=='__main__':unittest.main()
