import unittest
from unittest.mock import patch
import test_attendance_daily as daily
from tools import attendance_chat as chat,attendance
from tools.db_tool import fetch_all,fetch_one
from tools.memory_tool import read_memory,delete_session,append_turn
from tools.operations import OperationError
from agents import llm
from agents.graph import build_graph
from agents.prompts import GUARDRAIL_AGENT_PROMPT,SUPERVISOR_AGENT_PROMPT
from concurrent.futures import ThreadPoolExecutor


class ChatAttendanceTests(unittest.TestCase):
    setUp=daily.DailyAttendanceTests.setUp
    write=daily.DailyAttendanceTests.write
    marks=daily.DailyAttendanceTests.marks

    def invoke(self,fields,action='update',session='chat',request=None,user='FAC001'):
        self.app.config['CHAT_MEMORY_DIR']=self.temp.name+'/memory'
        _,_,revision=chat.context(user,'faculty',session)
        return chat.handle(user,'faculty',session,{'action':action,'fields':fields},revision,request)

    def test_full_roster_and_correction_without_duplicate_count(self):
        fields={'course':'CS301','date':'2026-10-19','all_status':'present'}
        result=self.invoke(fields,'start',request='first')
        self.assertEqual(result['actions'],['mark_attendance']);self.assertIn('2 present, 0 absent',result['response'])
        replay=chat.handle('FAC001','faculty','chat',{'action':'start','fields':fields},0,'first')
        self.assertEqual(replay,result)
        self.invoke(fields,'start')
        self.assertEqual(len(fetch_all('SELECT * FROM attendance_audit')),1)
        self.invoke({'course':'CS301','date':'2026-10-19','marks':[{'student':'STU001','status':'absent'}]},'start')
        self.assertEqual(len(fetch_all('SELECT * FROM attendance_audit')),2)
        self.assertEqual(fetch_one("SELECT total_classes FROM attendance WHERE student_id='STU001' AND course_id='CS301'")['total_classes'],41)

    def test_partial_marks_reload_and_rest(self):
        result=self.invoke({'course':'Machine Learning','date':'2026-10-19','marks':[{'student':'Vishnu','status':'absent'}]},'start')
        self.assertIn('STU002',result['response']);self.assertFalse(fetch_all('SELECT * FROM attendance_sessions'))
        self.assertIsNotNone(chat.context('FAC001','faculty','chat')[1])
        result=self.invoke({'rest_status':'present'})
        self.assertIn('1 present, 1 absent',result['response'])
        self.assertIsNone(chat.context('FAC001','faculty','chat')[1])

    def test_missing_selectors_and_multiple_sessions(self):
        result=self.invoke({'marks':[{'student':'STU001','status':'absent'}]},'start')
        self.assertIn('course and date',result['response'])
        self.write("INSERT INTO course_timetable(course_id,weekday,start_time,end_time,effective_from,effective_to) VALUES('CS301',0,'14:00','15:00','2026-01-01','2026-12-31')")
        result=self.invoke({'course':'CS301','date':'2026-10-19'})
        self.assertIn('09:00',result['response']);self.assertIn('14:00',result['response'])
        result=self.invoke({'session_start':'14:00','rest_status':'present'})
        self.assertIn('1 present, 1 absent',result['response'])

    def test_bad_names_can_be_corrected_and_other_accounts_rejected(self):
        self.write("UPDATE students SET name='Vishnu' WHERE student_id='STU002'")
        result=self.invoke({'course':'CS301','date':'2026-10-19','marks':[{'student':'Vishnu','status':'present'}]},'start')
        self.assertIn('unique student ID',result['response'])
        result=self.invoke({'marks':[{'student':'STU001','status':'present'}],'rest_status':'absent'})
        self.assertEqual(result['actions'],['mark_attendance'])
        result=self.invoke({'course':'CS301','date':'2026-10-19','marks':[{'student':'STU003','status':'present'}]},'start')
        self.assertFalse(result['actions']);self.assertIn('roster',result['response'])
        with self.assertRaises(PermissionError):chat.handle('STU001','student','chat',{'action':'start','fields':{}},0)
        result=self.invoke({'course':'CS301','date':'2026-10-19','all_status':'present'},'start',user='FAC002')
        self.assertFalse(result['actions'])

    def test_stale_panel_changes_and_enrollment_changes_require_review(self):
        self.invoke({'course':'CS301','date':'2026-10-19','marks':[{'student':'STU001','status':'present'}]},'start')
        attendance.save('FAC001','faculty',self.tid,'2026-10-19',self.marks())
        result=self.invoke({'rest_status':'absent'})
        self.assertIn('changed',result['response']);self.assertFalse(result['actions'])
        result=self.invoke({'marks':[{'student':'STU002','status':'absent'}]})
        self.assertEqual(result['actions'],['mark_attendance'])
        self.invoke({'course':'CS301','date':'2026-10-26','marks':[{'student':'STU001','status':'present'}]},'start')
        self.write("UPDATE course_registrations SET status='completed' WHERE student_id='STU002' AND course_id='CS301'")
        result=self.invoke({'rest_status':'absent'})
        self.assertIn('enrollment changed',result['response']);self.assertFalse(result['actions'])

    def test_weekends_future_cancellation_and_deleted_chat(self):
        for day in ['2026-10-24','2026-10-25','2026-11-02','2026-10-12']:
            with self.assertRaises(OperationError):self.invoke({'course':'CS301','date':day,'all_status':'present'},'start')
        self.invoke({'course':'CS301'},'start')
        self.assertIsNone(chat.context('FAC001','faculty','other')[1])
        self.invoke({},'cancel');self.assertIsNone(chat.context('FAC001','faculty','chat')[1])
        delete_session('FAC001','chat')
        with self.assertRaises(ValueError):chat.handle('FAC001','faculty','chat',{'action':'start','fields':{}},0)

    def test_receipt_recovers_memory_failure_without_repeating_write(self):
        self.app.config['CHAT_MEMORY_DIR']=self.temp.name+'/memory'
        append_turn('FAC001','chat','user','Mark all present',request_id='lost')
        with patch('tools.memory_tool._write',side_effect=OSError('disk temporarily unavailable')):
            with self.assertRaises(OSError):chat.handle('FAC001','faculty','chat',{'action':'start','fields':{'course':'CS301','date':'2026-10-19','all_status':'present'}},0,'lost')
        self.assertIsNotNone(chat.receipt('FAC001','lost'));self.assertEqual(len(fetch_all('SELECT * FROM attendance_audit')),1)
        chat.reconcile('FAC001','chat');chat.reconcile('FAC001','chat')
        messages=read_memory('FAC001')['sessions'][0]['messages']
        self.assertEqual(len([m for m in messages if m['role']=='assistant']),1)
        self.assertEqual(len(fetch_all('SELECT * FROM attendance_audit')),1)

    def test_concurrent_complete_turn_executes_once(self):
        self.app.config['CHAT_MEMORY_DIR']=self.temp.name+'/memory'
        def run(_):
            with self.app.app_context():return chat.handle('FAC001','faculty','chat',{'action':'start','fields':{'course':'CS301','date':'2026-10-19','all_status':'present'}},0,'same')
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(len(fetch_all('SELECT * FROM attendance_audit')),1)

    def test_graph_interpretation_and_task_switch(self):
        self.app.config['CHAT_MEMORY_DIR']=self.temp.name+'/memory'
        update={'action':'start','fields':{'course':'CS301','date':'2026-10-19','all_status':'present'}}
        def model(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college'}
            self.assertEqual(prompt,SUPERVISOR_AGENT_PROMPT)
            self.assertTrue(payload['attendance_courses'])
            return {'route':'direct','attendance_update':update}
        state=dict(user_id='FAC001',role='faculty',session_id='chat',query='mark everyone present',chat_history=[],current_date='2026-10-26',timezone='Asia/Kolkata',agent_trace=[],sources=[],actions=[],retry_count=0)
        with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text') as text:
            result=build_graph().invoke(state)
        self.assertIn('mark_attendance',result['actions']);text.assert_not_called();self.assertNotIn('confirmation',result)
        self.invoke({'course':'CS301'},'start')
        def unrelated(prompt,payload):return {'verdict':'allow','topic_scope':'college'} if prompt==GUARDRAIL_AGENT_PROMPT else {'route':'direct','direct_answer':'Hello'}
        with patch.object(llm,'invoke_json',side_effect=unrelated):build_graph().invoke({**state,'query':'Hello'})
        self.assertIsNone(chat.context('FAC001','faculty','chat')[1])

    def test_malformed_interpretation_never_writes(self):
        import json
        self.app.config['CHAT_MEMORY_DIR']=self.temp.name+'/memory'
        def model(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college'}
            raise json.JSONDecodeError('bad JSON','',0)
        state=dict(user_id='FAC001',role='faculty',session_id='chat',query='Mark everyone present',chat_history=[],current_date='2026-10-26',timezone='Asia/Kolkata',agent_trace=[],sources=[],actions=[],retry_count=0)
        with patch.object(llm,'invoke_json',side_effect=model) as calls:
            result=build_graph().invoke(state)
        self.assertIn("couldn't interpret",result['final_response'])
        self.assertEqual(len(calls.call_args_list),4)
        self.assertFalse(fetch_all('SELECT * FROM attendance_sessions'))

    def test_selector_normalization_preserves_draft_and_warning_events(self):
        self.write("UPDATE attendance_balances SET attended=32,total=40 WHERE course_id='CS301'")
        self.write("UPDATE attendance SET attended_classes=32,total_classes=40 WHERE course_id='CS301'")
        self.invoke({'course':'Machine Learning','date':'today','marks':[{'student':'STU001','status':'absent'}]},'start')
        result=self.invoke({'course':'CS301','date':'2026-10-26','marks':[{'student':'STU002','status':'absent'}]})
        self.assertIn('0 present, 2 absent',result['response'])
        self.assertEqual(len(fetch_all('SELECT * FROM notification_outbox')),2)
        self.invoke({'course':'CS301','date':'today','all_status':'absent'},'start')
        self.assertEqual(len(fetch_all('SELECT * FROM notification_outbox')),2)
        self.assertEqual(len(fetch_all('SELECT * FROM attendance_audit')),1)
