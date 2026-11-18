"""Faculty alert scope and agent integration; isolated storage, no external mail."""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from db.init_db import init_db
from routes.alert_routes import alert_bp
from routes.chat_routes import chat_bp
from tools import alerts
from tools.db_tool import get_connection, fetch_all, fetch_one
from tools.operations import OperationError
from agents import llm
from agents.prompts import GUARDRAIL_AGENT_PROMPT


class FacultyAlertTests(unittest.TestCase):
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
        root = Path(self.temp.name)
        self.path = root / 'test.db'
        shutil.copyfile(self.fixture, self.path)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, DATABASE_PATH=str(self.path), CHAT_MEMORY_DIR=str(root/'memory'),
            COLLEGE_TIMEZONE='Asia/Kolkata', JWT_SECRET_KEY='test-only-secret-at-least-thirty-two-characters')
        JWTManager(self.app)
        self.app.register_blueprint(alert_bp)
        self.app.register_blueprint(chat_bp)
        ctx = self.app.app_context(); ctx.push(); self.addCleanup(ctx.pop)
        self.write('DELETE FROM alert_rules')
        self.write('UPDATE attendance SET total_classes=100,attended_classes=90')
        self.client = self.app.test_client()

    def write(self, sql, params=()):
        with closing(get_connection()) as conn, conn:
            conn.execute(sql, params)

    def headers(self, user='FAC001'):
        return {'Authorization':'Bearer '+create_access_token(identity=user)}

    def create(self, threshold=75, course=None, student=None):
        return alerts.create_alert('FAC001','faculty','attendance_faculty',
            {'threshold':threshold,'course_id':course,'student_id':student})

    def evaluate(self):
        with closing(get_connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            alerts.evaluate_due(conn)

    def test_dynamic_all_courses_and_notifications(self):
        self.write("UPDATE attendance SET attended_classes=74 WHERE student_id IN ('STU001','STU003')")
        rule = self.create()
        occurrences = fetch_all('SELECT * FROM alert_occurrences')
        self.assertEqual(len(occurrences),4)
        self.assertTrue(all('STU001' in row['body'] and '74/100' in row['body'] and '75%' in row['body'] for row in occurrences))
        self.assertIn('all my assigned courses',rule['summary'])
        self.evaluate(); self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),4)
        context = alerts.interpretation_context('FAC001','faculty')
        self.assertEqual(context['owned_alerts'][0]['id'],rule['id'])
        self.assertTrue(all(c['course_id'].startswith('CS') for c in context['assigned_courses']))
        self.assertNotIn('STU003',json.dumps(context))

    def test_threshold_recovery_and_repeat_correction(self):
        self.write("UPDATE attendance SET attended_classes=75 WHERE student_id='STU001' AND course_id='CS301'")
        self.create(course='CS301')
        self.assertFalse(fetch_all('SELECT * FROM alert_occurrences'))
        for count in [74,75,74]:
            self.write("UPDATE attendance SET attended_classes=? WHERE student_id='STU001' AND course_id='CS301'",(count,))
            self.evaluate()
        self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),2)
        self.assertEqual([r['status'] for r in fetch_all('SELECT status FROM notification_outbox ORDER BY id')],['suppressed','pending'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            def run(_):
                with self.app.app_context(): self.evaluate()
            list(pool.map(run,range(2)))
        self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),2)

    def test_assignments_enrollment_and_invalid_attendance(self):
        self.write("UPDATE attendance SET attended_classes=60 WHERE student_id='STU001'")
        self.create()
        self.write("UPDATE courses SET faculty_id='FAC002' WHERE course_id='CS301'")
        self.write("UPDATE course_registrations SET status='completed' WHERE student_id='STU001' AND course_id='CS302'")
        self.evaluate()
        pending=fetch_all("SELECT body FROM notification_outbox WHERE status='pending'")
        self.assertEqual(len(pending),2)
        self.assertTrue(all('CS301' not in r['body'] and 'CS302' not in r['body'] for r in pending))
        # A missing balance and a zero denominator must never be reported as 0%.
        self.write("DELETE FROM attendance WHERE course_id='CS303'")
        self.write("UPDATE attendance SET total_classes=0,attended_classes=0 WHERE course_id='CS304'")
        self.evaluate()
        self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),4)
        self.write('DELETE FROM alert_rules')
        self.create()
        self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),0)

    def test_selection_validation_api_and_owner_isolation(self):
        rule=self.create(72.5,'machine learning','Vishnu')
        self.assertEqual(rule['config'],{'threshold':72.5,'course_id':'CS301','student_id':'STU001'})
        for course,student in [('EC301',None),(None,'STU001'),('CS301','STU003'),("CS301'; DROP TABLE courses;--",None)]:
            with self.assertRaises((OperationError,PermissionError)): self.create(course=course,student=student)
        for threshold in [True,0,100,'75',float('nan')]:
            with self.assertRaises(OperationError): self.create(threshold)
        self.write("UPDATE students SET name='Vishnu' WHERE student_id='STU002'")
        with self.assertRaisesRegex(OperationError,'Specify an ID'): self.create(course='CS301',student='Vishnu')
        response=self.client.get('/api/alerts',headers=self.headers())
        self.assertTrue(response.json['assigned_courses'])
        self.assertEqual(self.client.get('/api/alerts',headers=self.headers('FAC002')).json['alerts'],[])
        self.assertEqual(self.client.delete(f"/api/alerts/{rule['id']}",headers=self.headers('FAC002')).status_code,404)
        self.assertEqual(self.client.post('/api/alerts',json={'kind':'attendance_faculty','config':{'threshold':75,'course_id':None,'student_id':None}},headers=self.headers('STU001')).status_code,403)
        self.assertEqual(self.client.get('/api/alerts').status_code,401)

    def test_grouped_students_support_or_and_atomically(self):
        any_rule=alerts.create_alert('FAC001','faculty','attendance_faculty',{
            'threshold':75,'course_ids':['CS301'],'student_ids':['STU001','STU002'],'match_mode':'any'})
        self.assertIn('STU001 or STU002',any_rule['summary'])
        self.write("UPDATE attendance SET attended_classes=74 WHERE student_id='STU001' AND course_id='CS301'")
        self.evaluate()
        self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences WHERE rule_id=?',(any_rule['id'],))),1)
        self.write('DELETE FROM alert_rules')
        self.write("UPDATE attendance SET attended_classes=90 WHERE course_id='CS301'")
        all_rule=alerts.create_alert('FAC001','faculty','attendance_faculty',{
            'threshold':75,'course_ids':['CS301'],'student_ids':['STU001','STU002'],'match_mode':'all'})
        self.write("UPDATE attendance SET attended_classes=74 WHERE student_id='STU001' AND course_id='CS301'")
        self.evaluate()
        self.assertEqual(fetch_all('SELECT * FROM alert_occurrences WHERE rule_id=?',(all_rule['id'],)),[])
        self.write("UPDATE attendance SET attended_classes=74 WHERE student_id='STU002' AND course_id='CS301'")
        self.evaluate()
        rows=fetch_all('SELECT * FROM alert_occurrences WHERE rule_id=?',(all_rule['id'],))
        self.assertEqual(len(rows),1)
        self.assertIn('STU001',rows[0]['body']);self.assertIn('STU002',rows[0]['body'])
        before=len(fetch_all('SELECT * FROM alert_rules'))
        with self.assertRaises((OperationError,PermissionError)):
            alerts.create_alert('FAC001','faculty','attendance_faculty',{
                'threshold':75,'course_ids':['CS301'],'student_ids':['STU001','STU003'],'match_mode':'any'})
        self.assertEqual(len(fetch_all('SELECT * FROM alert_rules')),before)

    def test_grouped_classrooms_support_or_and(self):
        for room in ('A-101','B-201'):
            self.write('''INSERT INTO classroom_bookings(classroom_id,faculty_id,booking_date,start_time,end_time,purpose)
                VALUES(?, 'FAC002', '2030-01-10', '10:00', '11:00', 'Busy')''',(room,))
        any_rule=alerts.create_alert('FAC001','faculty','classroom_free',{
            'classroom_ids':['A-101','B-201'],'booking_date':'2030-01-10','start_time':'10:00','end_time':'11:00','match_mode':'any'})
        self.write("DELETE FROM classroom_bookings WHERE classroom_id='A-101' AND booking_date='2030-01-10'")
        self.evaluate()
        self.assertEqual(fetch_one('SELECT state FROM alert_rules WHERE id=?',(any_rule['id'],))['state'],'completed')
        self.write("INSERT INTO classroom_bookings(classroom_id,faculty_id,booking_date,start_time,end_time,purpose) VALUES('A-101','FAC002','2030-01-10','10:00','11:00','Busy')")
        all_rule=alerts.create_alert('FAC001','faculty','classroom_free',{
            'classroom_ids':['A-101','B-201'],'booking_date':'2030-01-10','start_time':'10:00','end_time':'11:00','match_mode':'all'})
        self.write("DELETE FROM classroom_bookings WHERE classroom_id='A-101' AND booking_date='2030-01-10'")
        self.evaluate()
        self.assertEqual(fetch_all('SELECT * FROM alert_occurrences WHERE rule_id=?',(all_rule['id'],)),[])
        self.write("DELETE FROM classroom_bookings WHERE classroom_id='B-201' AND booking_date='2030-01-10'")
        self.evaluate()
        self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences WHERE rule_id=?',(all_rule['id'],))),1)

    def test_chat_can_create_multi_student_or_rule(self):
        def model(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT:
                return {'verdict':'allow','topic_scope':'college','modules':['attendance']}
            self.assertIn('student_ids',prompt);self.assertTrue(payload['alert_context']['assigned_courses'])
            return {'route':'direct','alert_update':{'action':'create','kind':'attendance_faculty','config':{
                'threshold':75,'course_ids':['CS301'],'student_ids':['STU001','STU002'],'match_mode':'any'}}}
        with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=AssertionError('No SQL')):
            response=self.client.post('/api/chat',headers=self.headers(),json={'session_id':'grouped','message':'Notify me if STU001 or STU002 falls below 75% in Machine Learning'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['actions'],['alert_saved'])
        rule=alerts.list_alerts('FAC001','faculty')['alerts'][0]
        self.assertEqual(rule['config']['student_ids'],['STU001','STU002'])
        self.assertEqual(rule['config']['match_mode'],'any')

    def test_llm_interpreted_paraphrases_and_followup_edits(self):
        seen=[]
        requests=[
            ("add an alert if any student's attendance drops 75%. Applicable to all courses that I am teaching",75,None),
            ('Make it 70%',70,None),
            ('Only Machine Learning',70,'Machine Learning'),
            ('Across everything I teach',70,None),
        ]
        def model(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT:
                self.assertIn('ALL courses',payload['capability_matrix'])
                return {'verdict':'allow','topic_scope':'college','modules':['attendance']}
            self.assertIn('alert_context',payload)
            seen.append(payload)
            query,threshold,course=requests[len(seen)-1]
            self.assertEqual(payload['query'],query)
            rules=payload['alert_context']['owned_alerts']
            update={'action':'update' if rules else 'create','kind':'attendance_faculty',
                    'config':{'threshold':threshold,'course_id':course,'student_id':None}}
            if rules: update['id']=rules[0]['id']
            return {'route':'direct','alert_update':update}
        with patch.object(llm,'invoke_json',side_effect=model), patch.object(llm,'invoke_text',side_effect=AssertionError('No answer/SQL model needed')):
            for query,threshold,course in requests:
                response=self.client.post('/api/chat',headers=self.headers(),json={'session_id':'faculty-alert-test','message':query})
                self.assertEqual(response.status_code,200,response.json)
                self.assertIn('alert_tool',response.json['agent_trace'])
                self.assertNotIn('sql_query_agent',response.json['agent_trace'])
                self.assertIn('saved',response.json['response'])
        self.assertTrue(seen[-1]['chat_history'])
        rules=alerts.list_alerts('FAC001','faculty')['alerts']
        self.assertEqual(len(rules),1)
        self.assertEqual(rules[0]['config'],{'threshold':70.0,'course_id':None,'student_id':None})

    def test_batch_delete_validates_every_id_before_writing(self):
        first=self.create()['id']; second=self.create(65,'CS301')['id']
        other=alerts.create_alert('FAC002','faculty','attendance_faculty',{'threshold':75,'course_id':None,'student_id':None})['id']
        for ids in [[],None,'1,2',[True],[0],[-1],['1'],[1.5],[2**64],[first,other],[first,99999]]:
            with self.assertRaises((OperationError,PermissionError)):
                alerts.delete_alerts('FAC001','faculty',ids)
            self.assertEqual(len(alerts.list_alerts('FAC001','faculty')['alerts']),2)
        result=alerts.delete_alerts('FAC001','faculty',[second,first,second])
        self.assertEqual(result['deleted'],[second,first])
        self.assertEqual(len(alerts.list_alerts('FAC002','faculty')['alerts']),1)
        with self.assertRaises(PermissionError): alerts.delete_alerts('FAC001','faculty',[first,second])

    def test_batch_delete_suppression_and_transaction_rollback(self):
        ids=[]
        with closing(get_connection()) as conn,conn:
            for kind in ['attendance_student','leave_student','menu_match','classroom_free','attendance_faculty','leave_faculty']:
                identifier=conn.execute('INSERT INTO alert_rules(user_id,kind,config_json) VALUES(?,?,?)',('FAC001',kind,'{}')).lastrowid
                ids.append(identifier)
                for status in ['pending','failed','sent','sending']:
                    key=f'leave:test:{identifier}:{status}' if kind.startswith('leave') else f'alert:{identifier}:{status}'
                    conn.execute('INSERT INTO alert_occurrences(rule_id,user_id,event_key,subject,body) VALUES(?,?,?,?,?)',(identifier,'FAC001',key,'test','test'))
                    conn.execute('INSERT INTO notification_outbox(event_key,user_id,subject,body,status) VALUES(?,?,?,?,?)',(key,'FAC001','test','test',status))
            conn.execute(f"CREATE TRIGGER reject_test_delete BEFORE DELETE ON alert_rules WHEN OLD.id={ids[-1]} BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError): alerts.delete_alerts('FAC001','faculty',ids)
        self.assertEqual(fetch_one('SELECT COUNT(*) n FROM alert_rules')['n'],6)
        self.assertEqual(fetch_one("SELECT COUNT(*) n FROM notification_outbox WHERE status='suppressed'")['n'],0)
        self.write('DROP TRIGGER reject_test_delete')
        alerts.delete_alerts('FAC001','faculty',ids)
        self.assertFalse(fetch_all('SELECT * FROM alert_occurrences'))
        counts={row['status']:row['n'] for row in fetch_all('SELECT status,COUNT(*) n FROM notification_outbox GROUP BY status')}
        self.assertEqual(counts,{'suppressed':12,'sent':6,'sending':6})

    def test_llm_delete_both_and_all_current_alerts(self):
        for query in ['delete both','delete these two','delete all my current alerts']:
            with self.subTest(query=query):
                first=self.create()['id']; second=self.create(65,'CS301')['id']
                third=self.create(60,'CS302')['id']
                self.write('UPDATE alert_rules SET active=0 WHERE id=?',(third,))
                from tools.memory_tool import append_turn
                append_turn('FAC001','faculty-delete', 'assistant',f'Alerts #{first} and #{second} are current.')
                def model(prompt,payload):
                    if prompt==GUARDRAIL_AGENT_PROMPT: return {'verdict':'allow','topic_scope':'college','modules':['attendance']}
                    self.assertIn('ids (a nonempty list',prompt)
                    self.assertEqual([r['id'] for r in payload['alert_context']['owned_alerts']],[first,second])
                    self.assertTrue(payload['chat_history'])
                    return {'route':'direct','alert_update':{'action':'delete','ids':[first,second]}}
                with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=AssertionError('No SQL generation')):
                    result=self.client.post('/api/chat',headers=self.headers(),json={'session_id':'faculty-delete','message':query})
                self.assertEqual(result.status_code,200,result.json)
                self.assertEqual(result.json['actions'],[])
                self.assertEqual(len(alerts.list_alerts('FAC001','faculty')['alerts']),3)
                identifier=result.json['confirmation']['id']
                decision=self.client.post('/api/chat/confirmations/'+identifier,headers=self.headers(),json={'session_id':'faculty-delete','decision':'confirm'})
                self.assertIn('alert_deleted',decision.json['actions'])
                repeat=self.client.post('/api/chat/confirmations/'+identifier,headers=self.headers(),json={'session_id':'faculty-delete','decision':'confirm'})
                self.assertEqual(repeat.json['response'],decision.json['response'])
                self.assertEqual([r['id'] for r in alerts.list_alerts('FAC001','faculty')['alerts']],[third])
                alerts.delete_alert('FAC001','faculty',third)

    def test_delete_confirmation_cancel_expiry_changes_and_isolation(self):
        from tools.memory_tool import append_turn
        from tools.confirmations import decide, hydrate
        from tools.memory_tool import read_memory
        first=self.create()['id']; second=self.create(65,'CS301')['id']
        append_turn('FAC001','confirm-delete','user','delete both')
        def proposal():
            return alerts.chat_update('FAC001','faculty',{'action':'delete','ids':[first,second]},'confirm-delete')['confirmation']
        pending=proposal()
        append_turn('FAC001','confirm-delete','assistant',pending['summary'],confirmation=pending)
        self.assertEqual(hydrate(read_memory('FAC001'),'FAC001')['sessions'][0]['messages'][-1]['confirmation']['status'],'pending')
        with self.assertRaises(OperationError): decide(pending['id'],'FAC001','faculty','wrong-session','confirm')
        append_turn('FAC002','confirm-delete','user','confirm')
        with self.assertRaises(OperationError): decide(pending['id'],'FAC002','faculty','confirm-delete','confirm')
        self.assertEqual(decide(pending['id'],'FAC001','faculty','confirm-delete','cancel')['confirmation']['status'],'cancelled')
        self.assertEqual(len(alerts.list_alerts('FAC001','faculty')['alerts']),2)
        pending=proposal()
        self.write('UPDATE chat_confirmations SET expires_at=0 WHERE confirmation_id=?',(pending['id'],))
        self.assertEqual(decide(pending['id'],'FAC001','faculty','confirm-delete','confirm')['confirmation']['status'],'expired')
        pending=proposal(); newer=proposal()
        self.assertEqual(decide(pending['id'],'FAC001','faculty','confirm-delete','confirm')['confirmation']['status'],'superseded')
        alerts.update_alert('FAC001','faculty',first,'attendance_faculty',{'threshold':50,'course_id':None,'student_id':None})
        self.assertEqual(decide(newer['id'],'FAC001','faculty','confirm-delete','confirm')['confirmation']['status'],'failed')
        self.assertEqual(len(alerts.list_alerts('FAC001','faculty')['alerts']),2)

    def test_ambiguous_alert_and_malformed_model_output_do_not_write(self):
        self.create(75);self.create(65,'CS301')
        def model(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT: return {'verdict':'allow','topic_scope':'college','modules':['attendance']}
            self.assertEqual(len(payload['alert_context']['owned_alerts']),2)
            return {'route':'direct','direct_answer':'Which alert should I change? Please specify its ID.'}
        with patch.object(llm,'invoke_json',side_effect=model):
            result=self.client.post('/api/chat',headers=self.headers(),json={'session_id':'ambiguous','message':'Make it 70%'})
        self.assertIn('Which alert',result.json['response'])
        for update in [{'action':'create','kind':'attendance_faculty','config':{'threshold':70,'sql':'DELETE FROM attendance'}},
                       {'action':'update','id':True,'kind':'attendance_faculty','config':{}}, {'action':'delete','id':'1 OR 1=1'}]:
            with self.assertRaises((OperationError,PermissionError)): alerts.chat_update('FAC001','faculty',update)
        self.assertEqual(len(alerts.list_alerts('FAC001','faculty')['alerts']),2)

    def test_menu_notification_requests_for_both_roles(self):
        for user,dish,query in [('STU001','chicken','notify me when the menu have chicken.'),('FAC001','paneer','Email me whenever paneer is served')]:
            def model(prompt,payload):
                if prompt==GUARDRAIL_AGENT_PROMPT:
                    self.assertIn('personal mess-menu dish alerts',payload['capability_matrix'])
                    return {'verdict':'allow','topic_scope':'college','modules':['mess']}
                self.assertIn('kind=menu_match, not menu_request',prompt)
                self.assertIsNotNone(payload['menu_catalog'])
                return {'route':'direct','alert_update':{'action':'create','kind':'menu_match','config':{
                    'plans':['veg','non_veg'],'meals':['breakfast','lunch','evening_snacks','dinner'],'dish':dish}}}
            with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=AssertionError('No menu-answer call')):
                response=self.client.post('/api/chat',headers=self.headers(user),json={'session_id':'menu-alert','message':query})
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual(response.json['actions'],['alert_saved'])
            self.assertIn('alert_tool',response.json['agent_trace'])
            self.assertNotIn('menu_tool',response.json['agent_trace'])
            role='student' if user=='STU001' else 'faculty'
            rules=alerts.list_alerts(user,role)['alerts']
            self.assertEqual(rules[0]['config']['dish'],dish)
            self.assertEqual(len(rules[0]['config']['meals']),4)


if __name__=='__main__': unittest.main()
