import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['RAG_USE_CHROMA']='0'
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from werkzeug.security import generate_password_hash
from auth.routes import auth_bp
from routes.admin_routes import admin_bp
from routes.chat_routes import chat_bp
from db.init_db import init_db
from db.migrate import migrate
from tools.db_tool import get_connection, verify_identity, SCHEMAS
from tools import admin_service as admin
from tools.admin_documents import read_document, publish_document, recover_publications


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=tempfile.TemporaryDirectory()
        cls.fixture=Path(cls.base.name)/'fixture.db'
        init_db(cls.fixture)

    @classmethod
    def tearDownClass(cls): cls.base.cleanup()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.db=root/'test.db';shutil.copyfile(self.fixture,self.db)
        self.kb=root/'kb';self.kb.mkdir();(self.kb/'handbook.md').write_text('# Existing policy\n')
        self.app=Flask(__name__);self.app.config.update(TESTING=True,DATABASE_PATH=str(self.db),JWT_SECRET_KEY='isolated-administrator-test-secret-key',KNOWLEDGE_BASE_DIR=str(self.kb),CHROMA_PERSIST_DIR=str(root/'chroma'),CHAT_MEMORY_DIR=str(root/'memory'))
        JWTManager(self.app);self.app.register_blueprint(auth_bp);self.app.register_blueprint(admin_bp);self.app.register_blueprint(chat_bp)
        ctx=self.app.app_context();ctx.push();self.addCleanup(ctx.pop)
        self.client=self.app.test_client()
        with closing(get_connection()) as c,c:
            c.execute("INSERT INTO users(user_id,username,password_hash,role,email) VALUES('ADMTEST','Test Admin',?,'admin','admin@example.test')",(generate_password_hash('safe-admin-password'),))
        self.headers={'Authorization':'Bearer '+create_access_token(identity='ADMTEST',additional_claims={'ver':0})}

    def rows(self,resource): return admin.list_resource('ADMTEST',resource)
    def save(self,resource,row,**changes):
        return admin.save_resource('ADMTEST',resource,{**row,**changes,'reason':'Isolated test'})

    def test_admin_login_and_student_route_denial(self):
        r=self.client.post('/api/auth/login',json={'user_id':'ADMTEST','password':'safe-admin-password'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['role'],'admin')
        r=self.client.get('/api/chat/history',headers=self.headers)
        self.assertEqual(r.status_code,403)
        for uid in ('STU001','FAC001'):
            token=create_access_token(identity=uid,additional_claims={'ver':0})
            self.assertEqual(self.client.get('/api/admin/overview',headers={'Authorization':'Bearer '+token}).status_code,403)

    def test_no_public_admin_signup_or_recovery(self):
        response=self.client.post('/api/auth/register',json={'user_id':'ADMNEW','role':'admin','department':'Computer Science','full_name':'New Admin','email':'new@example.test','password':'password'})
        self.assertEqual(response.status_code,400)
        from auth.account_service import issue_reset
        with self.assertRaises(ValueError): issue_reset('ADMTEST')

    def test_account_deactivation_and_revision(self):
        row=next(r for r in self.rows('accounts') if r['user_id']=='STU001')
        with self.assertRaises(ValueError): admin.save_account('ADMTEST',{**row,'is_active':0,'reason':'test'})
        admin.save_account('ADMTEST',{**row,'is_active':0,'reason':'test','confirmed':True})
        token=create_access_token(identity='STU001',additional_claims={'ver':0})
        self.assertEqual(self.client.get('/api/auth/me',headers={'Authorization':'Bearer '+token}).status_code,401)
        with closing(get_connection()) as c:
            with self.assertRaises(PermissionError): verify_identity(c,'STU001','student')
        with self.assertRaises(admin.Conflict): admin.save_account('ADMTEST',{**row,'reason':'stale'})
        updated=next(r for r in self.rows('accounts') if r['user_id']=='STU001')
        admin.save_account('ADMTEST',{**updated,'is_active':1,'reason':'reactivate','confirmed':True})
        self.assertEqual(self.client.get('/api/auth/me',headers={'Authorization':'Bearer '+token}).status_code,401)

    def test_account_create_setup_failure_keeps_account(self):
        with patch('auth.mail.send_reset_email',side_effect=RuntimeError('SMTP failure')):
            r=self.client.post('/api/admin/records/accounts',headers=self.headers,json={'user_id':'STUNEW','username':'New Student','role':'student','department':'Computer Science','semester':1,'email':'new@example.test','is_active':1,'_revision':0,'reason':'Provision account'})
        self.assertEqual(r.status_code,200,r.json)
        self.assertIn('failed',r.json['setup_email'])
        self.assertTrue(any(r['user_id']=='STUNEW' for r in self.rows('accounts')))
        self.assertNotIn('password',json.dumps(self.rows('accounts')))

    def test_prerequisite_cycle_atomic(self):
        rows=self.rows('prerequisites');a=next(r for r in rows if r['course_id']=='CS301');b=next(r for r in rows if r['course_id']=='CS302')
        self.save('prerequisites',a,prerequisite_ids=['CS302'])
        with self.assertRaises(ValueError): self.save('prerequisites',b,prerequisite_ids=['CS301'])
        self.assertEqual(next(r for r in self.rows('prerequisites') if r['course_id']=='CS302'),b)

    def test_occupancy_equipment_and_stale_edits(self):
        room=self.rows('hostel_rooms')[0]
        with self.assertRaises(ValueError): self.save('hostel_rooms',room,occupied_beds=99)
        self.save('hostel_rooms',room,occupied_beds=0)
        with self.assertRaises(admin.Conflict): self.save('hostel_rooms',room,occupied_beds=1)
        room=next(r for r in self.rows('classrooms') if r['classroom_id']=='A-101')
        with closing(get_connection()) as c,c:
            c.execute("INSERT INTO classroom_bookings(classroom_id,faculty_id,booking_date,start_time,end_time,purpose) VALUES('A-101','FAC001','2099-01-05','10:00','11:00','test')")
        with self.assertRaises(ValueError): self.save('classrooms',room,capacity=10)
        self.save('classrooms',room,projector_count=3)

    def test_syllabus_uses_existing_shape(self):
        row=self.rows('course_syllabi')[0]
        self.save('course_syllabi',row)
        with self.assertRaises(ValueError): self.save('course_syllabi',self.rows('course_syllabi')[0],units_json='[]')

    def test_timetable_future_replacement_and_conflict(self):
        row=next(r for r in self.rows('course_timetable') if r['course_id']=='CS301' and r['weekday']==0)
        with patch('tools.admin_service.today',return_value=date(2026,11,20)):
            with self.assertRaises(ValueError): self.save('course_timetable',row,effective_from='2026-11-20',confirmed=True)
            with self.assertRaises(ValueError): self.save('course_timetable',row,effective_from='2026-11-23',weekday=5,confirmed=True)
            self.save('course_timetable',row,effective_from='2026-11-23',start_time='08:00',end_time='09:00',confirmed=True)
            current=self.rows('course_timetable')
            self.assertEqual(next(r for r in current if r['timetable_id']==row['timetable_id'])['effective_to'],'2026-11-22')
            self.assertTrue(any(r['effective_from']=='2026-11-23' and r['course_id']=='CS301' for r in current))
            with self.assertRaises(ValueError): self.save('course_timetable',{'_revision':0,'timetable_id':None,'course_id':'CS302','weekday':0,'start_time':'08:00','end_time':'09:00','effective_from':'2026-11-23','effective_to':'2026-12-31'})

    def test_cross_department_assignment_rejected(self):
        course=next(r for r in self.rows('courses') if r['course_id']=='CS301')
        other=next(r for r in self.rows('accounts') if r['role']=='faculty' and r['department']!=course['department'])
        with self.assertRaises(ValueError): self.save('courses',course,faculty_id=other['user_id'],confirmed=True)

    def test_document_publish_revision_and_path_security(self):
        doc=read_document('ADMTEST','handbook.md')
        with self.assertRaises(ValueError): read_document('ADMTEST','../config.py')
        result=publish_document('ADMTEST','handbook.md',{'content':'# New policy\n<script>unsafe</script>','revision':doc['revision'],'reason':'Test','confirmed':True})
        self.assertEqual(result['message'],'Document published.')
        with self.assertRaises(admin.Conflict): publish_document('ADMTEST','handbook.md',{'content':'# Other','revision':doc['revision'],'reason':'Stale','confirmed':True})
        self.assertEqual(len(read_document('ADMTEST','handbook.md')['history']),2)
        from tools.rag_tool import retrieve
        self.assertIn('New policy',retrieve('New policy')['context'])
        recover_publications()

    def test_monitoring_and_private_schema(self):
        from tools.notifications import heartbeat
        heartbeat('notification-worker')
        result=admin.monitoring('ADMTEST')
        self.assertEqual(result['workers'][0]['name'],'notification-worker')
        self.assertNotIn('body',json.dumps(result))
        self.assertNotIn('admin_audit',SCHEMAS)
        with self.assertRaises(PermissionError): admin.monitoring('STU001')

    def test_local_admin_bootstrap_and_recovery(self):
        from auth.admin_cli import register_admin_cli
        register_admin_cli(self.app)
        runner=self.app.test_cli_runner()
        inputs='ADMLOCAL\nLocal Admin\nlocal@example.test\na-secure-test-password\na-secure-test-password\n'
        result=runner.invoke(args=['admin-account'],input=inputs)
        self.assertEqual(result.exit_code,0,result.output)
        self.assertNotIn('a-secure-test-password',result.output)
        result=runner.invoke(args=['admin-account','--recover'],input=inputs)
        self.assertEqual(result.exit_code,0,result.output)
        with closing(get_connection()) as c:
            self.assertEqual(c.execute("SELECT token_version FROM users WHERE user_id='ADMLOCAL'").fetchone()[0],1)

    def test_inactive_outbox_suppression(self):
        from tools.notifications import deliver_one
        with closing(get_connection()) as c,c:
            c.execute('UPDATE users SET is_active=0 WHERE user_id=?',('FAC001',))
            c.execute("INSERT INTO notification_outbox(event_key,user_id,subject,body) VALUES('test','FAC001','Private','Private')")
        with patch('tools.alerts.evaluate_due'),patch('auth.mail.send_email') as send:
            deliver_one();send.assert_not_called()
        with closing(get_connection()) as c:
            self.assertEqual(c.execute("SELECT status FROM notification_outbox WHERE event_key='test'").fetchone()[0],'suppressed')

    def test_repeatable_migration_preserves_records(self):
        with closing(get_connection()) as c:
            before=c.execute('SELECT user_id,password_hash,token_version FROM users ORDER BY user_id').fetchall()
        migrate(self.db);migrate(self.db)
        with closing(get_connection()) as c:
            self.assertEqual([tuple(r) for r in before],[tuple(r) for r in c.execute('SELECT user_id,password_hash,token_version FROM users ORDER BY user_id')])
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())

    def test_real_version20_upgrade_preserves_foreign_keys(self):
        # Reconstruct the pre-upgrade users schema without touching the live database.
        with closing(get_connection()) as c:
            c.execute('PRAGMA foreign_keys=OFF')
            with c:
                c.execute("DELETE FROM users WHERE role='admin'")
                c.execute('DELETE FROM admin_audit')
                c.execute('''CREATE TABLE old_users(user_id TEXT PRIMARY KEY,username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('student','faculty')),
                    created_at TEXT,email TEXT COLLATE NOCASE,token_version INTEGER NOT NULL DEFAULT 0)''')
                c.execute('INSERT INTO old_users SELECT user_id,username,password_hash,role,created_at,email,token_version FROM users')
                c.execute('DROP TABLE users');c.execute('ALTER TABLE old_users RENAME TO users')
                c.execute('CREATE UNIQUE INDEX users_email_unique ON users(email) WHERE email IS NOT NULL')
                for table in ('admin_audit','admin_revisions','admin_document_revisions','worker_heartbeats'):
                    c.execute('DROP TABLE '+table)
                c.execute('DELETE FROM schema_migrations WHERE version=21')
            before=[tuple(r) for r in c.execute('SELECT * FROM users ORDER BY user_id')]
        saved=migrate(self.db)
        self.assertTrue(Path(saved).is_file())
        with closing(get_connection()) as c:
            after=[tuple(r)[:-1] for r in c.execute('SELECT * FROM users ORDER BY user_id')]
            self.assertEqual(before,after)
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(c.execute('SELECT MIN(is_active) FROM users').fetchone()[0],1)

    def test_deactivation_cancels_pending_actions_and_mail(self):
        with closing(get_connection()) as c,c:
            c.execute("INSERT INTO chat_confirmations(confirmation_id,user_id,session_id,operation,status,expires_at,created_at) VALUES('pending','STU001','session','{}','pending',9999999999,1)")
            c.execute("INSERT INTO notification_outbox(event_key,user_id,subject,body) VALUES('pending','STU001','Private','Private')")
        row=next(r for r in self.rows('accounts') if r['user_id']=='STU001')
        admin.save_account('ADMTEST',{**row,'is_active':0,'reason':'test','confirmed':True})
        with closing(get_connection()) as c:
            self.assertEqual(c.execute("SELECT status FROM chat_confirmations WHERE confirmation_id='pending'").fetchone()[0],'cancelled')
            self.assertEqual(c.execute("SELECT status FROM notification_outbox WHERE event_key='pending'").fetchone()[0],'suppressed')

    def test_concurrent_edits_commit_once(self):
        from concurrent.futures import ThreadPoolExecutor
        row=self.rows('hostel_rooms')[0]
        def save():
            with self.app.app_context():
                try:self.save('hostel_rooms',row,occupied_beds=0);return 'saved'
                except admin.Conflict:return 'stale'
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(lambda _:save(),range(2)))
        self.assertEqual(sorted(result),['saved','stale'])
        with closing(get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM admin_audit WHERE action='record.save'").fetchone()[0],1)

    def test_recovery_after_file_replacement(self):
        from tools.admin_documents import digest
        old=read_document('ADMTEST','handbook.md');content='# Interrupted publication\n'
        with closing(get_connection()) as c,c:
            c.execute('INSERT INTO admin_document_revisions(name,content,previous_hash,content_hash,actor,status) VALUES(?,?,?,?,?,?)',('handbook.md',content,old['revision'],digest(content),'ADMTEST','prepared'))
        (self.kb/'handbook.md').write_text(content)
        recover_publications()
        self.assertEqual(read_document('ADMTEST','handbook.md')['history'][0]['status'],'published_index_pending')

    def delete_payload(self,row,resource):
        return {**{k:row[k] for k in admin.RESOURCES[resource][0]},'_revision':row['_revision'],
                'reason':'Isolated deletion test','confirmed':True}

    def test_delete_room_confirmation_revision_and_audit(self):
        self.save('classrooms',{'classroom_id':'TEST-1','building':'Test','capacity':20,'has_ac':None,
                  'projector_count':None,'computer_count':None,'_revision':0})
        row=next(r for r in self.rows('classrooms') if r['classroom_id']=='TEST-1')
        payload=self.delete_payload(row,'classrooms')
        for changes in ({'confirmed':False},{'reason':''},{'_revision':0},{'unexpected':'sql'}):
            with self.assertRaises(ValueError):admin.delete_resource('ADMTEST','classrooms',{**payload,**changes})
        with self.assertRaises(PermissionError):admin.delete_resource('FAC001','classrooms',payload)
        admin.delete_resource('ADMTEST','classrooms',payload)
        self.assertFalse(any(r['classroom_id']=='TEST-1' for r in self.rows('classrooms')))
        with self.assertRaises(admin.Conflict):admin.delete_resource('ADMTEST','classrooms',payload)
        with closing(get_connection()) as c:
            audit=c.execute("SELECT before_json,after_json FROM admin_audit WHERE action='record.delete'").fetchall()
            self.assertEqual(len(audit),1)
            self.assertEqual(json.loads(audit[0]['before_json'])['classroom_id'],'TEST-1')
            self.assertIsNone(json.loads(audit[0]['after_json']))

    def test_delete_protects_bookings_pending_confirmations_and_alerts(self):
        row=next(r for r in self.rows('classrooms') if r['classroom_id']=='A-101')
        payload=self.delete_payload(row,'classrooms')
        with closing(get_connection()) as c,c:
            c.execute("DELETE FROM classroom_bookings WHERE classroom_id='A-101'")
            c.execute("INSERT INTO classroom_bookings(classroom_id,faculty_id,booking_date,start_time,end_time,purpose) VALUES('A-101','FAC001','2025-01-01','10:00','11:00','historical')")
        with self.assertRaisesRegex(admin.Conflict,'booking records'):admin.delete_resource('ADMTEST','classrooms',payload)
        with closing(get_connection()) as c,c:
            c.execute("DELETE FROM classroom_bookings WHERE classroom_id='A-101'")
            c.execute("INSERT INTO chat_confirmations(confirmation_id,user_id,session_id,operation,status,expires_at,created_at) VALUES('test','FAC001','test',?,'pending',9999999999,0)",(json.dumps({'type':'book_classroom','classroom_id':'A-101'}),))
        with self.assertRaisesRegex(admin.Conflict,'pending booking'):admin.delete_resource('ADMTEST','classrooms',payload)
        with closing(get_connection()) as c,c:
            c.execute("DELETE FROM chat_confirmations WHERE confirmation_id='test'")
            c.execute("INSERT INTO alert_rules(user_id,kind,config_json) VALUES('FAC001','classroom_free',?)",(json.dumps({'classroom_ids':['A-101']}),))
        with self.assertRaisesRegex(admin.Conflict,'availability alert'):admin.delete_resource('ADMTEST','classrooms',payload)
        self.assertTrue(any(r['classroom_id']=='A-101' for r in self.rows('classrooms')))

    def test_delete_future_schedule_preserves_started_periods(self):
        row=next(r for r in self.rows('course_timetable') if r['course_id']=='CS301')
        with patch('tools.admin_service.today',return_value=date(2026,11,23)):
            with self.assertRaisesRegex(admin.Conflict,'historical'):admin.delete_resource('ADMTEST','course_timetable',self.delete_payload(row,'course_timetable'))
            self.save('course_timetable',{'timetable_id':None,'course_id':'CS301','weekday':0,'start_time':'07:00','end_time':'08:00','effective_from':'2027-01-01','effective_to':'2027-12-31','_revision':0})
            future=next(r for r in self.rows('course_timetable') if r['effective_from']=='2027-01-01')
            admin.delete_resource('ADMTEST','course_timetable',self.delete_payload(future,'course_timetable'))
        self.assertTrue(any(r['timetable_id']==row['timetable_id'] for r in self.rows('course_timetable')))
        with self.assertRaises(ValueError):admin.delete_resource('ADMTEST','accounts',{'user_id':'STU001'})

    def test_delete_endpoint_is_admin_only(self):
        row=next(r for r in self.rows('classrooms') if r['classroom_id']=='A-103')
        payload=self.delete_payload(row,'classrooms')
        for user in ('STU001','FAC001'):
            token=create_access_token(identity=user,additional_claims={'ver':0})
            r=self.client.delete('/api/admin/records/classrooms',json=payload,headers={'Authorization':'Bearer '+token})
            self.assertEqual(r.status_code,403)
        with closing(get_connection()) as c,c:c.execute("DELETE FROM classroom_bookings WHERE classroom_id='A-103'")
        response=self.client.delete('/api/admin/records/classrooms',json=payload,headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.client.delete('/api/admin/records/classrooms',json=payload,headers=self.headers).status_code,409)

    def test_overview_uses_real_sessions_and_distinct_active_bookings(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,11,23,9,30,tzinfo=ZoneInfo('Asia/Kolkata'))
        with closing(get_connection()) as c,c:
            c.execute("DELETE FROM classroom_bookings WHERE booking_date='2026-11-23'")
            for room,start,end in [('A-101','09:00','10:00'),('A-101','09:00','10:00'),('B-201','10:00','11:00')]:
                c.execute('INSERT INTO classroom_bookings(classroom_id,faculty_id,booking_date,start_time,end_time,purpose) VALUES(?,?,?,?,?,?)',(room,'FAC001','2026-11-23',start,end,'test'))
        with patch('tools.admin_service.datetime') as clock:
            clock.now.return_value=now
            result=admin.overview('ADMTEST')
        self.assertEqual(result['utilization'],{'booked':1,'unbooked':9,'percentage':10})
        self.assertTrue(any(r['course_id']=='CS301' for r in result['schedule']))
        self.assertTrue(all('classroom_id' not in r for r in result['schedule']))

    def test_admin_search_literal_matching_and_private_accounts(self):
        self.assertTrue(any(r['id']=='CS301' for r in admin.search_records('ADMTEST','Machine Learning')))
        self.assertEqual(admin.search_records('ADMTEST','ADMTEST'),[])
        self.assertEqual(admin.search_records('ADMTEST','%'),[])
        self.assertEqual(admin.search_records('ADMTEST',"' OR 1=1 --"),[])
        with self.assertRaises(PermissionError):admin.search_records('STU001','Vishnu')

    def test_delete_other_resources_without_cascading(self):
        alias=self.rows('bus_locality_aliases')[0]
        admin.delete_resource('ADMTEST','bus_locality_aliases',self.delete_payload(alias,'bus_locality_aliases'))
        self.assertFalse(any(r['bus_id']==alias['bus_id'] and r['alias']==alias['alias'] for r in self.rows('bus_locality_aliases')))
        for resource in ('bus_routes','transport_services','mess_plans','courses'):
            row=self.rows(resource)[0]
            with self.assertRaises(admin.Conflict):admin.delete_resource('ADMTEST',resource,self.delete_payload(row,resource))
        occupied=next(r for r in self.rows('hostel_rooms') if r['occupied_beds']>0)
        with self.assertRaisesRegex(admin.Conflict,'occupied'):admin.delete_resource('ADMTEST','hostel_rooms',self.delete_payload(occupied,'hostel_rooms'))
        empty=next(r for r in self.rows('hostel_rooms') if r['occupied_beds']==0)
        admin.delete_resource('ADMTEST','hostel_rooms',self.delete_payload(empty,'hostel_rooms'))
        menu=self.rows('mess_menus')[0]
        admin.delete_resource('ADMTEST','mess_menus',self.delete_payload(menu,'mess_menus'))
        self.assertEqual(len(self.rows('mess_menus')),13)
        syllabus=self.rows('course_syllabi')[0]
        admin.delete_resource('ADMTEST','course_syllabi',self.delete_payload(syllabus,'course_syllabi'))
        self.assertEqual(len(self.rows('course_syllabi')),69)
        with closing(get_connection()) as c:self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())

    def test_prerequisite_delete_clears_links_not_course(self):
        row=next(r for r in self.rows('prerequisites') if r['prerequisite_ids'])
        admin.delete_resource('ADMTEST','prerequisites',self.delete_payload(row,'prerequisites'))
        updated=next(r for r in self.rows('prerequisites') if r['course_id']==row['course_id'])
        self.assertEqual(updated['prerequisite_ids'],[])
        self.assertEqual(len(self.rows('courses')),70)

    def test_delete_account_means_deactivate_with_preserved_history(self):
        row=next(r for r in self.rows('accounts') if r['user_id']=='STU001')
        with closing(get_connection()) as c:before=c.execute("SELECT COUNT(*) FROM course_registrations WHERE student_id='STU001'").fetchone()[0]
        result=admin.delete_resource('ADMTEST','accounts',{'user_id':'STU001','_revision':row['_revision'],'confirmed':True,'reason':'Retire account'})
        self.assertIn('deactivated',result['message'])
        self.assertEqual(next(r for r in self.rows('accounts') if r['user_id']=='STU001')['is_active'],0)
        with closing(get_connection()) as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM course_registrations WHERE student_id='STU001'").fetchone()[0],before)

    def test_policy_delete_preserves_revision_and_clears_retrieval(self):
        from tools.admin_documents import delete_document
        from tools.rag_tool import retrieve
        self.assertIn('Existing policy',retrieve('Existing policy')['context'])
        doc=read_document('ADMTEST','handbook.md')
        payload={'revision':doc['revision'],'reason':'Replace outdated policy','confirmed':True}
        with self.assertRaises(ValueError):delete_document('ADMTEST','handbook.md',{**payload,'confirmed':False})
        with self.assertRaises(PermissionError):delete_document('STU001','handbook.md',payload)
        delete_document('ADMTEST','handbook.md',payload)
        self.assertFalse((self.kb/'handbook.md').exists())
        self.assertEqual(retrieve('Existing policy')['context'],'')
        state=read_document('ADMTEST','handbook.md')
        self.assertFalse(state['exists']);self.assertEqual(state['revision'],'missing')
        self.assertEqual(state['history'][0]['status'],'deleted_indexed')
        with closing(get_connection()) as c:self.assertIn('Existing policy',c.execute('SELECT content FROM admin_document_revisions').fetchone()[0])
        publish_document('ADMTEST','handbook.md',{'content':'# Replacement','revision':'missing','reason':'Restore','confirmed':True})
        self.assertTrue(read_document('ADMTEST','handbook.md')['exists'])


if __name__=='__main__':unittest.main()
