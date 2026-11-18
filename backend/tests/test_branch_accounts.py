import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['RAG_USE_CHROMA'] = '0'
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from auth.routes import auth_bp
from auth.account_service import DEPARTMENTS
from db.init_db import init_db
from db.replace_demo_accounts import replace_accounts
from routes.leave_routes import leave_bp
from tools.db_tool import execute_query
from tools.operations import execute_operation, OperationError


class BranchAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp.name) / 'accounts.db'
        init_db(cls.database)
        memory = Path(cls.temp.name) / 'memory'
        memory.mkdir()
        (memory / 'FAC001.json').write_text('{"sessions":[]}')
        cls.report = replace_accounts(cls.database, memory)
        cls.memory = memory

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(DATABASE_PATH=str(self.database), JWT_SECRET_KEY='test-key-at-least-thirty-two-characters', TESTING=True)
        JWTManager(self.app)
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(leave_bp)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.client = self.app.test_client()

    def test_accounts_emails_backups_and_credentials(self):
        self.assertTrue(Path(self.report['database_backup']).is_file())
        self.assertTrue((Path(self.report['memory_backup']) / 'FAC001.json').is_file())
        self.assertFalse((self.memory / 'FAC001.json').exists())
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], 21)
            self.assertEqual(dict(conn.execute("SELECT user_id,email FROM users WHERE email IS NOT NULL")), {'STU001':'student@example.com','FAC001':'faculty@example.com'})
            self.assertEqual(set(conn.execute('SELECT department,COUNT(*) FROM students GROUP BY department')), {(d,2) for d in DEPARTMENTS})
            self.assertEqual(set(conn.execute('SELECT department,COUNT(*) FROM faculty GROUP BY department')), {(d,1) for d in DEPARTMENTS})
        for uid in ['STU001','FAC001']:
            self.assertEqual(self.client.post('/api/auth/login', json={'user_id':uid,'password':'password123'}).status_code, 200)
        old = create_access_token(identity='FAC001', additional_claims={'ver':0})
        self.assertEqual(self.client.get('/api/auth/me', headers={'Authorization':'Bearer '+old}).status_code, 401)

    def test_prefix_validation(self):
        for uid in ['123456789','student001','fac001','FAC']:
            self.assertEqual(self.client.post('/api/auth/login', json={'user_id':uid,'password':'password123'}).status_code, 400)
        for uid, role, prefix in [('FAC999','student','STU'),('STU999','faculty','FAC')]:
            response = self.client.post('/api/auth/register', json={'user_id':uid,'full_name':'Prefix Test','role':role,'email':'unused@example.test','password':'password123','department':'Computer Science'})
            self.assertEqual(response.status_code,400)
            self.assertIn('Username should start with '+prefix,response.json['error'])

    def test_every_faculty_has_only_own_branch(self):
        for i, dept in enumerate(DEPARTMENTS):
            fid = f'FAC{i+1:03d}'
            expected = {f'STU{i*2+1:03d}',f'STU{i*2+2:03d}'}
            for table in ['students','attendance','results','leave_requests','course_registrations']:
                self.assertEqual({r['student_id'] for r in execute_query(f'SELECT DISTINCT student_id FROM {table} WHERE 1=1',fid,'faculty')},expected)
            self.assertEqual({r['department'] for r in execute_query('SELECT * FROM courses',fid,'faculty')},{dept})
        self.assertEqual({r['student_id'] for r in execute_query('SELECT * FROM students','STU001','student')},{'STU001'})

    def test_cross_branch_assignments_cannot_bypass_read_or_leave_review(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE students SET advisor_faculty_id='FAC001' WHERE student_id='STU003'")
            conn.execute("INSERT INTO course_registrations(student_id,course_id,status) VALUES('STU003','CS301','enrolled')")
            leave_id=conn.execute("SELECT leave_id FROM leave_requests WHERE student_id='STU003'").fetchone()[0]
        try:
            rows=execute_query("SELECT student_id FROM students WHERE student_id='STU003' UNION SELECT student_id FROM attendance WHERE student_id='STU003'",'FAC001','faculty')
            self.assertEqual(rows,[])
            with self.assertRaises(OperationError):
                execute_operation({'type':'review_leave','leave_id':leave_id,'status':'approved'},'FAC001','faculty')
            token=create_access_token(identity='FAC001',additional_claims={'ver':1})
            response=self.client.get('/api/leaves',headers={'Authorization':'Bearer '+token})
            self.assertEqual(response.status_code,200)
            self.assertTrue(all(r['student_id'] in {'STU001','STU002'} for r in response.json['leaves']))
        finally:
            with sqlite3.connect(self.database) as conn:
                conn.execute("UPDATE students SET advisor_faculty_id='FAC002' WHERE student_id='STU003'")
                conn.execute("DELETE FROM course_registrations WHERE student_id='STU003' AND course_id='CS301'")
