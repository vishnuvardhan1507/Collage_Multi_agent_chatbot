import sys
import tempfile
import unittest
import sqlite3
from pathlib import Path
from contextlib import closing
from datetime import datetime,date
from zoneinfo import ZoneInfo
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flask import Flask
from flask_jwt_extended import JWTManager,create_access_token
from db.init_db import init_db
from db.migrate import migrate
from tools import attendance as a
from tools.db_tool import fetch_all,fetch_one,execute_query
from tools.operations import execute_operation,project_attendance,OperationError
from tools.timetable import read_timetable
from tools.notifications import deliver_one,enqueue
from routes.attendance_routes import attendance_bp


class DailyAttendanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.db';init_db(self.path)
        self.app=Flask(__name__);self.app.config.update(DATABASE_PATH=str(self.path),COLLEGE_TIMEZONE='Asia/Kolkata',JWT_SECRET_KEY='a-test-secret-with-at-least-thirty-two-characters')
        JWTManager(self.app);self.app.register_blueprint(attendance_bp)
        ctx=self.app.app_context();ctx.push();self.addCleanup(ctx.pop)
        self.now=datetime(2026,10,26,15,tzinfo=ZoneInfo('Asia/Kolkata'))
        clock=patch('tools.attendance.college_now',return_value=self.now);clock.start();self.addCleanup(clock.stop)
        self.tid=fetch_one("SELECT timetable_id FROM course_timetable WHERE course_id='CS301' AND weekday=0")['timetable_id']

    def marks(self,day='2026-10-19',present=1):
        r=a.roster('FAC001','faculty',self.tid,day)
        return {'revision':r['revision'],'marks':{s['student_id']:present for s in r['students']}}

    def write(self,sql,params=()):
        with closing(sqlite3.connect(self.path)) as c,c:c.execute(sql,params)

    def test_balances_corrections_audit_and_stale_save(self):
        old=fetch_one("SELECT * FROM attendance WHERE student_id='STU001' AND course_id='CS301'")
        payload=self.marks();a.save('FAC001','faculty',self.tid,'2026-10-19',payload)
        row=fetch_one("SELECT * FROM attendance WHERE student_id='STU001' AND course_id='CS301'")
        self.assertEqual((row['attended_classes'],row['total_classes']),(old['attended_classes']+1,old['total_classes']+1))
        with self.assertRaises(a.AttendanceConflict):a.save('FAC001','faculty',self.tid,'2026-10-19',payload)
        payload=self.marks(present=0);a.save('FAC001','faculty',self.tid,'2026-10-19',payload)
        row=fetch_one("SELECT * FROM attendance WHERE student_id='STU001' AND course_id='CS301'")
        self.assertEqual((row['attended_classes'],row['total_classes']),(old['attended_classes'],old['total_classes']+1))
        payload=self.marks(present=0);a.save('FAC001','faculty',self.tid,'2026-10-19',payload)
        self.assertEqual(len(fetch_all('SELECT * FROM attendance_audit')),2)
        self.assertIsNone(migrate(self.path))
        self.assertEqual(fetch_one('SELECT total FROM attendance_balances WHERE student_id=? AND course_id=?',('STU001','CS301'))['total'],40)

    def test_scope_dates_roster_and_invalid_baseline(self):
        for user,role in [('STU001','student'),('FAC002','faculty')]:
            with self.assertRaises(PermissionError):a.roster(user,role,self.tid,'2026-10-19')
        for day in ['2026-10-18','2026-10-24','2026-10-25','2026-10-27','bad']:
            with self.assertRaises(OperationError):a.save('FAC001','faculty',self.tid,day,{'revision':0,'marks':{}})
        with patch('tools.attendance.college_now',return_value=self.now.replace(hour=8)):
            with self.assertRaises(OperationError):a.roster('FAC001','faculty',self.tid,'2026-10-26')
        payload=self.marks();payload['marks']['STU003']=1
        with self.assertRaises(a.AttendanceConflict):a.save('FAC001','faculty',self.tid,'2026-10-19',payload)
        self.write("DELETE FROM attendance_balances WHERE student_id='STU001' AND course_id='CS301'")
        with self.assertRaises(OperationError):a.save('FAC001','faculty',self.tid,'2026-10-19',self.marks())
        self.assertFalse(fetch_all('SELECT * FROM attendance_sessions'))
        with self.assertRaises((PermissionError,sqlite3.OperationalError)):execute_query('SELECT * FROM notification_outbox','STU001','student')

    def test_weekends_projections_and_timezone(self):
        self.write("INSERT INTO course_timetable(course_id,weekday,start_time,end_time,effective_from,effective_to) VALUES('CS301',5,'09:00','10:00','2026-01-01','2026-12-31')")
        self.assertTrue(a.sessions('FAC001','faculty','2026-10-24')['holiday'])
        result=read_timetable({'mode':'range','start_date':'2026-10-24','end_date':'2026-10-25'},'STU001','student',self.now)
        self.assertTrue(all(d['count']==0 and d['holiday'] for d in result['days']))
        with patch('tools.operations.college_today',return_value=date(2026,10,19)):
            result=project_attendance('STU001','student',{'start_date':'2026-10-24','end_date':'2026-10-25'})
            self.assertEqual(result['overall']['current_percentage'],result['overall']['projected_percentage'])
            self.assertEqual(result['overall']['missed_classes'],0)
        # UTC Sunday evening is Monday in the college timezone.
        result=read_timetable({'mode':'day','date':'today'},'STU001','student',datetime(2026,10,25,20,tzinfo=ZoneInfo('UTC')))
        self.assertEqual(result['start_date'],'2026-10-26');self.assertEqual(result['days'][0]['count'],2)

    def test_warning_threshold_recovery_repeat_and_delivery(self):
        self.write("UPDATE users SET email='student2@example.test' WHERE user_id='STU002'")
        self.write("UPDATE attendance_balances SET attended=4,total=5 WHERE course_id='CS301'")
        self.write("UPDATE attendance SET attended_classes=4,total_classes=5 WHERE course_id='CS301'")
        a.save('FAC001','faculty',self.tid,'2026-10-19',self.marks(present=0))
        self.assertEqual(len(fetch_all('SELECT * FROM notification_outbox')),2)
        a.save('FAC001','faculty',self.tid,'2026-10-26',self.marks('2026-10-26',0))
        self.assertEqual(len(fetch_all('SELECT * FROM notification_outbox')),2)
        a.save('FAC001','faculty',self.tid,'2026-10-19',self.marks(present=1))
        a.save('FAC001','faculty',self.tid,'2026-10-26',self.marks('2026-10-26',1))
        self.assertTrue(all(r['status']=='suppressed' for r in fetch_all('SELECT * FROM notification_outbox')))
        a.save('FAC001','faculty',self.tid,'2026-10-26',self.marks('2026-10-26',0))
        self.assertEqual(len(fetch_all('SELECT * FROM notification_outbox')),4)
        with patch('auth.mail.send_email') as send:
            while deliver_one():pass
            self.assertEqual(send.call_count,2)
        self.assertEqual(len(fetch_all("SELECT * FROM notification_outbox WHERE status='sent'")),2)

    def test_initial_low_and_exact_eighty(self):
        self.write("UPDATE attendance_balances SET attended=3,total=4 WHERE course_id='CS301'")
        self.write("UPDATE attendance SET attended_classes=3,total_classes=4 WHERE course_id='CS301'")
        a.save('FAC001','faculty',self.tid,'2026-10-19',self.marks(present=1))
        self.assertFalse(fetch_all('SELECT * FROM notification_outbox')) # 4/5 exactly 80
        a.save('FAC001','faculty',self.tid,'2026-10-19',self.marks(present=0))
        self.assertEqual(len(fetch_all('SELECT * FROM notification_outbox')),2)

    def test_already_low_warns_once_and_two_sessions_count_separately(self):
        self.write("UPDATE attendance_balances SET attended=2,total=5 WHERE course_id='CS301'")
        self.write("UPDATE attendance SET attended_classes=2,total_classes=5 WHERE course_id='CS301'")
        self.write("INSERT INTO course_timetable(course_id,weekday,start_time,end_time,effective_from,effective_to) VALUES('CS301',0,'14:00','15:00','2026-01-01','2026-12-31')")
        a.save('FAC001','faculty',self.tid,'2026-10-19',self.marks())
        other=fetch_one("SELECT timetable_id FROM course_timetable WHERE course_id='CS301' AND start_time='14:00'")['timetable_id']
        a.save('FAC001','faculty',other,'2026-10-19',{'revision':0,'marks':{'STU001':1,'STU002':0}})
        row=fetch_one("SELECT * FROM attendance WHERE student_id='STU001' AND course_id='CS301'")
        self.assertEqual((row['attended_classes'],row['total_classes']),(4,7))
        self.assertEqual(len(fetch_all('SELECT * FROM notification_outbox')),2)

    def test_event_rollback_and_retry_exhaustion(self):
        from tools.db_tool import get_connection
        from tools.operations import execute_operation_in_transaction
        with self.assertRaises(RuntimeError),closing(get_connection()) as conn,conn:
            execute_operation_in_transaction(conn,dict(type='book_classroom',classroom_id='B-201',booking_date='2030-01-10',start_time='10:00',end_time='11:00',purpose='Test'),'FAC001','faculty')
            raise RuntimeError('rollback')
        self.assertFalse(fetch_all('SELECT * FROM notification_outbox'))
        self.assertFalse(fetch_all("SELECT * FROM classroom_bookings WHERE booking_date='2030-01-10'"))
        with closing(get_connection()) as conn,conn:
            enqueue(conn,'test','FAC001','Test','Test')
            enqueue(conn,'test','FAC001','Test','Test')
        self.assertEqual(len(fetch_all('SELECT * FROM notification_outbox')),1)
        with patch('auth.mail.send_email',side_effect=OSError('failure')):
            for _ in range(5):
                self.write('UPDATE notification_outbox SET next_attempt=0')
                self.assertTrue(deliver_one())
        self.assertEqual(fetch_one('SELECT status FROM notification_outbox')['status'],'failed')
        self.assertFalse(deliver_one())

    def test_approved_leave_email_includes_original_purpose(self):
        with patch('tools.operations.college_today',return_value=date(2026,10,19)):
            leave=execute_operation(dict(type='submit_leave',from_date='2026-11-10',to_date='2026-11-11',reason='Family function'),'STU001','student')
            execute_operation(dict(type='review_leave',leave_id=leave['record_id'],status='approved'),'FAC001','faculty')
        row=fetch_one('SELECT * FROM notification_outbox WHERE event_key=?',(f"leave:{leave['record_id']}:approved",))
        self.assertEqual(row['user_id'],'STU001')
        self.assertIn('Dates: 2026-11-10 to 2026-11-11',row['body'])
        self.assertIn('Leave purpose: Family function',row['body'])
        with patch('auth.mail.send_email') as send:
            self.assertTrue(deliver_one())
            self.assertEqual(send.call_args.args[2],row['body'])

    def test_outbox_retry_expired_lease_and_operation_events(self):
        with patch('tools.operations.college_today',return_value=date(2026,10,19)):
            leave=execute_operation(dict(type='submit_leave',from_date='2026-11-10',to_date='2026-11-10',reason='Appointment'),'STU001','student')
            execute_operation(dict(type='review_leave',leave_id=leave['record_id'],status='rejected',rejection_reason='Assessment scheduled'),'FAC001','faculty')
            booking=execute_operation(dict(type='book_classroom',classroom_id='B-201',booking_date='2026-11-10',start_time='10:00',end_time='11:00',purpose='Quiz'),'FAC001','faculty')
        rows=fetch_all('SELECT * FROM notification_outbox');self.assertEqual(len(rows),2)
        self.assertIn('Assessment scheduled',rows[0]['body']);self.assertIn('B-201',rows[1]['body'])
        with patch('auth.mail.send_email',side_effect=OSError('secret')):
            self.assertTrue(deliver_one())
        row=fetch_one('SELECT * FROM notification_outbox WHERE id=?',(rows[0]['id'],))
        self.assertEqual(row['last_error'],'OSError');self.assertEqual(row['attempts'],1)
        self.write("UPDATE notification_outbox SET status='sending',lease_until=0")
        with patch('auth.mail.send_email') as send:
            while deliver_one():pass
            self.assertEqual(send.call_count,2)
        self.assertFalse(deliver_one())
        self.assertTrue(fetch_one('SELECT * FROM classroom_bookings WHERE booking_id=?',(booking['record_id'],)))

    def test_concurrent_save_and_roster_snapshot(self):
        payload=self.marks()
        def attempt(_):
            with self.app.app_context():
                try:a.save('FAC001','faculty',self.tid,'2026-10-19',payload);return True
                except a.AttendanceConflict:return False
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sorted(pool.map(attempt,range(2))),[False,True])
        self.write("UPDATE course_registrations SET status='completed' WHERE student_id='STU001' AND course_id='CS301'")
        self.assertIn('STU001',self.marks()['marks'])
        self.assertEqual(len(fetch_all('SELECT * FROM attendance_audit')),1)

    def test_api_roles_and_weekend(self):
        client=self.app.test_client()
        for user,role,status in [('STU001','student',403),('FAC001','faculty',200)]:
            token=create_access_token(identity=user,additional_claims={'role':role,'token_version':0})
            response=client.get('/api/attendance/sessions?date=2026-10-24',headers={'Authorization':'Bearer '+token})
            self.assertEqual(response.status_code,status,response.json)
            if status==200:self.assertTrue(response.json['holiday'])


if __name__=='__main__':unittest.main()
