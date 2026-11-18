import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.init_db import init_db

class FreshInitTests(unittest.TestCase):
    def test_current_accounts_courses_and_existing_database_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'new.db'
            init_db(database)
            with closing(sqlite3.connect(database)) as conn:
                ids = {r[0] for r in conn.execute('SELECT user_id FROM users')}
                self.assertEqual(ids, {f'STU{i:03}' for i in range(1,15)} | {f'FAC{i:03}' for i in range(1,8)})
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM courses').fetchone()[0], 70)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM classrooms').fetchone()[0], 10)
                self.assertEqual(conn.execute("SELECT computer_count FROM classrooms WHERE classroom_id='Lab-303'").fetchone()[0], 40)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM prerequisites').fetchone()[0], 56)
                for sid in sorted(i for i in ids if i.startswith('STU')):
                    self.assertEqual(dict(conn.execute('SELECT status,COUNT(*) FROM course_registrations WHERE student_id=? GROUP BY status',(sid,))), {'completed':4,'enrolled':4,'pending':2})
                self.assertEqual(conn.execute("SELECT email FROM users WHERE user_id='STU001'").fetchone()[0], 'student@example.com')
                self.assertEqual(conn.execute("SELECT email FROM users WHERE user_id='FAC001'").fetchone()[0], 'faculty@example.com')
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertIn('rejection_reason', {r[1] for r in conn.execute('PRAGMA table_info(leave_requests)')})
            original = database.read_bytes()
            with self.assertRaises(FileExistsError):
                init_db(database)
            self.assertEqual(database.read_bytes(), original)
