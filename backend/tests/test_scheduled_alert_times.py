"""Editable IST schedules, using isolated databases and no external mail."""
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_faculty_alerts as fixtures
from tools import alerts
from tools.db_tool import get_connection, fetch_all
from tools.operations import OperationError
from tools.notifications import deliver_one


class ScheduledTimeTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.FacultyAlertTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.FacultyAlertTests.tearDownClass.__func__)
    setUp = fixtures.FacultyAlertTests.setUp
    write = fixtures.FacultyAlertTests.write

    def now(self, clock='06:00', day='2026-10-26'):
        return datetime.fromisoformat(day+'T'+clock).replace(tzinfo=ZoneInfo('Asia/Kolkata'))

    def create(self, kind='class_reminder', config=None):
        with patch.object(alerts, 'college_now', return_value=self.now()):
            return alerts.create_alert('FAC001', 'faculty', kind, config or {'course_id':'CS301'})

    def evaluate(self, now):
        with closing(get_connection()) as conn, conn:
            alerts.evaluate_due(conn, now)

    def queued(self):
        return fetch_all('SELECT * FROM notification_outbox ORDER BY id')

    def refresh(self, now):
        message = dict(self.queued()[0])
        with closing(get_connection()) as conn, conn:
            return alerts.refresh_scheduled_email(conn, message, now)

    def update(self, rule, config, now=None):
        with patch.object(alerts, 'college_now', return_value=now or self.now()):
            return alerts.update_alert('FAC001','faculty',rule['id'],rule['kind'],config)

    def test_defaults_and_validation(self):
        self.assertIn('7:00 AM IST', self.create()['summary'])
        self.assertIn('6:00 PM IST', self.create('attendance_missing')['summary'])
        for value in ('7:00','24:00','12:60','',None,7,True,'07:00:00'):
            with self.subTest(value=value), self.assertRaises(OperationError):
                self.create(config={'course_id':None,'reminder_time':value})
        with self.assertRaises(PermissionError):
            self.create('leave_faculty', {'reminder_time':'07:00'})

    def test_minute_precision_timezone_and_context_edit(self):
        rule=self.create(config={'course_id':'CS301','reminder_time':'08:15'})
        rule=self.update(rule, {'course_id':None})
        self.assertEqual(rule['config']['reminder_time'],'08:15')
        self.evaluate(self.now('08:14').astimezone(timezone.utc))
        self.assertEqual(self.queued(),[])
        self.evaluate(self.now('08:15').astimezone(timezone.utc))
        self.assertEqual(len(self.queued()),1)
        self.update(rule, {'course_id':None,'reminder_time':'08:30'}, self.now('08:20'))
        self.assertFalse(self.refresh(self.now('08:20')))
        self.assertEqual(self.queued()[0]['next_attempt'],self.now('08:30').timestamp())
        self.assertTrue(self.refresh(self.now('08:30')))
        self.evaluate(self.now('08:31'))
        self.assertEqual(len(self.queued()),1)

    def test_pending_class_refresh_and_expiry(self):
        rule=self.create(config={'course_id':None})
        self.evaluate(self.now('07:00'))
        self.assertIn('09:00',self.queued()[0]['body'])
        self.update(rule, {'course_id':None,'reminder_time':'10:30'})
        self.assertTrue(self.refresh(self.now('10:30')))
        self.assertNotIn('09:00',self.queued()[0]['body'])
        self.assertIn('11:00',self.queued()[0]['body'])
        self.assertFalse(self.refresh(self.now('00:01', '2026-10-27')))
        self.assertEqual(self.queued()[0]['status'],'suppressed')

    def test_menu_refresh_time_edit_and_dedup(self):
        config={'plans':['veg'], 'meals':['breakfast'], 'dishes':['idli'], 'reminder_time':'08:00'}
        rule=self.create('menu_match', config)
        self.evaluate(self.now('07:59'))
        self.assertEqual(self.queued(),[])
        self.evaluate(self.now('08:00'))
        self.update(rule, {**config,'reminder_time':'09:00'})
        self.assertFalse(self.refresh(self.now('08:30')))
        # Moving the time earlier wakes a never-attempted deferred message.
        self.update(rule, {**config,'reminder_time':'08:10'})
        self.assertEqual(self.queued()[0]['next_attempt'],0)
        self.assertTrue(self.refresh(self.now('08:30')))
        self.write("UPDATE notification_outbox SET status='sent'")
        self.update(rule, {**config,'reminder_time':'08:20'},self.now('08:30'))
        self.evaluate(self.now('10:00'))
        self.assertEqual(len(self.queued()),1)

    def test_missing_custom_gate_and_permission_recheck(self):
        self.create('attendance_missing', {'course_id':'CS301','reminder_time':'10:15'})
        self.evaluate(self.now('10:14'))
        self.assertEqual(self.queued(),[])
        self.evaluate(self.now('10:15'))
        self.assertEqual(len(self.queued()),1)
        self.write("UPDATE courses SET faculty_id='FAC002' WHERE course_id='CS301'")
        self.assertFalse(self.refresh(self.now('10:16')))
        self.assertEqual(self.queued()[0]['status'],'suppressed')

    def test_midnight_and_weekend(self):
        self.create(config={'course_id':None,'reminder_time':'00:00'})
        self.evaluate(self.now('00:00').astimezone(timezone.utc))
        self.assertIn('2026-10-26',self.queued()[0]['event_key'])
        self.evaluate(self.now('00:00','2026-10-31'))
        self.assertEqual(len(self.queued()),1)

    def test_worker_defers_and_retries_without_duplicate_mail(self):
        rule=self.create(config={'course_id':None})
        self.evaluate(self.now('07:00'))
        self.update(rule, {'course_id':None,'reminder_time':'08:15'})
        with patch.object(alerts,'college_now',return_value=self.now('08:00')), patch('tools.notifications.time.time',return_value=self.now('08:00').timestamp()), patch('auth.mail.send_email') as send:
            self.assertTrue(deliver_one())
            send.assert_not_called()
        with patch.object(alerts,'college_now',return_value=self.now('08:15')), patch('tools.notifications.time.time',return_value=self.now('08:15').timestamp()), patch('auth.mail.send_email',side_effect=OSError('mock SMTP failure')):
            self.assertTrue(deliver_one())
        self.assertEqual(self.queued()[0]['status'],'pending')
        with patch.object(alerts,'college_now',return_value=self.now('08:17')), patch('tools.notifications.time.time',return_value=self.now('08:17').timestamp()), patch('auth.mail.send_email') as send:
            self.assertTrue(deliver_one())
            self.assertFalse(deliver_one())
            send.assert_called_once()
        self.assertEqual(self.queued()[0]['status'],'sent')
        self.assertEqual(len(self.queued()),1)


if __name__ == '__main__':
    unittest.main()
