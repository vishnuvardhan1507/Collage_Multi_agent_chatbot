"""Course-scoped timetable reads; isolated data and mocked interpretation."""
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from groq import RateLimitError
import httpx
from routes.chat_routes import chat_bp
from db.init_db import init_db
from tools.db_tool import get_connection
from tools.timetable import read_timetable, format_timetable, timetable_catalog, validate_timetable_request, timetable_context, valid_timetable_weekdays
from tools.memory_tool import load_history, append_turn
from agents.supervisor_agent import supervisor_intake_node
from tools.operations import OperationError
from agents.graph import build_graph
from agents.prompts import GUARDRAIL_AGENT_PROMPT, scoped_payload

NOW = datetime(2026,11,17,8,0,tzinfo=ZoneInfo('Asia/Kolkata'))


class TimetableCourseFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=tempfile.TemporaryDirectory()
        cls.fixture=Path(cls.base.name)/'fixture.db'
        init_db(cls.fixture)

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.db';shutil.copyfile(self.fixture,self.path)
        app=Flask(__name__)
        app.config.update(DATABASE_PATH=str(self.path),CHAT_MEMORY_DIR=str(Path(self.temp.name)/'memory'),COLLEGE_TIMEZONE='Asia/Kolkata',JWT_SECRET_KEY='isolated-timetable-test-secret-over-thirty-two-characters')
        JWTManager(app);app.register_blueprint(chat_bp);self.app=app
        context=app.app_context();context.push();self.addCleanup(context.pop)

    def read(self, request, user='STU001',role='student',now=NOW):
        return read_timetable(request,user,role,now=now)

    def sessions(self,result):
        return [s for day in result['days'] for s in day['sessions']]

    def write(self,sql,args=()):
        with closing(get_connection()) as conn,conn:conn.execute(sql,args)

    def test_reported_week_and_compact_fallback(self):
        result=self.read({'mode':'week','date':'today','course_ids':['CS301']})
        self.assertEqual([(s['date'],s['start_time'],s['end_time']) for s in self.sessions(result)],
                         [('2026-11-16','09:00','10:00'),('2026-11-19','09:00','10:00')])
        answer=format_timetable(result)
        self.assertEqual(answer.count('| Date |'),1)
        self.assertIn('2 classes scheduled.',answer)
        self.assertNotIn('CS302',answer);self.assertNotIn('Holiday',answer)
        self.assertIn('| Date | Day |',answer)
        self.assertIn('| 2026-11-16 | Monday |',answer)
        self.assertIn('| 2026-11-19 | Thursday |',answer)

    def test_names_aliases_duplicates_and_multiple_courses(self):
        for selection in (['Machine Learning'],['cs301'],['CS301','Machine Learning']):
            result=self.read({'mode':'week','date':'today','course_ids':selection})
            self.assertEqual(result['course_count'],1)
            self.assertEqual({s['course_id'] for s in self.sessions(result)},{'CS301'})
        self.write('UPDATE course_syllabi SET aliases_json=? WHERE course_id=?',(json.dumps(['custom ML alias']),'CS301'))
        self.assertEqual(self.read({'mode':'week','date':'today','course_ids':['custom ML alias']})['course_count'],1)
        result=self.read({'mode':'week','date':'today','course_ids':['CS301','CS302']})
        self.assertEqual(len(self.sessions(result)),4)

    def test_invalid_and_ambiguous_selections_never_broaden(self):
        for selected in ([], 'CS301', [True],[''],['x'*101],['CS301']*21):
            with self.assertRaises(OperationError):validate_timetable_request({'mode':'next','course_ids':selected},NOW)
        for selected in (['FAKE'],['CS301','FAKE'],["CS301' OR 1=1 --"]):
            with self.assertRaises(OperationError):self.read({'mode':'week','date':'today','course_ids':selected})
        self.write("UPDATE course_syllabi SET aliases_json='[\"shared alias\"]' WHERE course_id IN ('CS301','CS302')")
        with self.assertRaisesRegex(OperationError,'Which course'):
            self.read({'mode':'next','course_ids':['shared alias']})

    def test_authorization_and_current_enrollment(self):
        for user,role in [('STU001','student'),('FAC001','faculty')]:
            allowed={c['course_id'] for c in timetable_catalog(user,role)}
            self.assertIn('CS301',allowed)
            self.assertNotIn('CS101',allowed)
            with self.assertRaises(OperationError):self.read({'mode':'next','course_ids':['CE301']},user,role)
        self.write("UPDATE course_registrations SET status='completed' WHERE student_id='STU001' AND course_id='CS301'")
        with self.assertRaises(OperationError):self.read({'mode':'next','course_ids':['CS301']})
        self.write("UPDATE courses SET faculty_id='FAC002' WHERE course_id='CS301'")
        with self.assertRaises(OperationError):self.read({'mode':'next','course_ids':['CS301']},'FAC001','faculty')

    def test_all_modes_next_ongoing_and_weekends(self):
        for request in ({'mode':'day','date':'2026-11-19'}, {'mode':'weekday','weekday':3},
                        {'mode':'range','start_date':'2026-11-19','end_date':'2026-11-19'}):
            result=self.read({**request,'course_ids':['CS301']})
            self.assertEqual(len(self.sessions(result)),1)
        result=self.read({'mode':'next','course_ids':['CS301']})
        self.assertEqual(result['next_sessions'][0]['date'],'2026-11-19')
        now=datetime(2026,11,19,9,30,tzinfo=NOW.tzinfo)
        result=self.read({'mode':'next','course_ids':['CS301']},now=now)
        self.assertEqual(len(result['ongoing']),1)
        self.assertEqual(result['next_sessions'][0]['date'],'2026-11-23')
        self.assertEqual(self.read({'mode':'weekday','weekday':5,'course_ids':['CS301']})['days'][0]['count'],0)

    def test_missing_coverage_scoped_to_requested_courses(self):
        self.write("DELETE FROM course_timetable WHERE course_id='CS302'")
        self.assertTrue(all(d['count'] is not None for d in self.read({'mode':'week','date':'today','course_ids':['CS301']})['days']))
        result=self.read({'mode':'week','date':'today','course_ids':['CS302']})
        self.assertIn('full count unavailable',format_timetable(result))
        self.assertIn('coverage unavailable for CS302',format_timetable(result))
        self.write("UPDATE course_timetable SET effective_to='2026-11-18' WHERE course_id='CS301'")
        result=self.read({'mode':'week','date':'today','course_ids':['CS301']})
        self.assertEqual(len(self.sessions(result)),1)
        self.assertIn('2026-11-19: timetable coverage unavailable',format_timetable(result))

    def test_unfiltered_compatibility_and_prompt_scoping(self):
        first=self.read({'mode':'week','date':'today'})
        second=self.read({'mode':'week','date':'today','course_ids':None})
        self.assertEqual(first,second)
        self.assertEqual(len(self.sessions(first)),8)
        self.assertIn('Holiday',format_timetable(first))
        self.assertNotIn('timetable_catalog',scoped_payload({'timetable_catalog':[]},{'modules':['transport']}))

    def test_interpretation_and_context_never_use_generated_sql(self):
        history=[{'role':'user','content':'When do I have CS301 classes in a week?'}]
        for query, selection, day in [('When do I have CS301 classes in a week?',['CS301'],'today'),
                                     ('Next week?',['CS301'],'2026-11-23'),
                                     ('What about Database Systems?',['CS302'],'today'),
                                     ('Show all my classes',None,'today')]:
            def model(prompt,payload,**kwargs):
                if prompt==GUARDRAIL_AGENT_PROMPT:
                    return {'verdict':'allow','topic_scope':'college','modules':['timetable']}
                self.assertEqual(payload['chat_history'],history)
                self.assertIn('CS301',{c['course_id'] for c in payload['timetable_catalog']})
                return {'route':'sql','timetable_request':{'mode':'week','date':day,'course_ids':selection}}
            state=dict(user_id='STU001',role='student',session_id='timetable-filter-test',query=query,
                       chat_history=history,current_date='2026-11-17',timezone='Asia/Kolkata',
                       agent_trace=[],retry_count=0,actions=[],sources=[],route='direct')
            with patch('agents.llm.invoke_json',side_effect=model),patch('agents.llm.invoke_text',side_effect=RuntimeError('mock fallback')), \
                 patch('agents.supervisor_agent.college_now',return_value=NOW),patch('agents.graph.execute_query') as sql:
                result=build_graph().invoke(state)
            sql.assert_not_called()
            self.assertNotIn('sql_query_agent',result['agent_trace'])
            if selection:
                self.assertIn(selection[0],result['final_response'])
                self.assertNotIn('CS304',result['final_response'])

    def test_weekday_validation_and_incorrect_model_answer(self):
        timetable=self.read({'mode':'week','date':'today','course_ids':['CS301']})
        answer=format_timetable(timetable)
        sessions=self.sessions(timetable)
        self.assertTrue(valid_timetable_weekdays(answer,sessions))
        self.assertFalse(valid_timetable_weekdays(answer.replace('Thursday','Wednesday'),sessions))
        self.assertFalse(valid_timetable_weekdays(answer.replace('Thursday',''),sessions))
        self.assertEqual(timetable_context(timetable)['request'],{'mode':'week','date':'2026-11-16','course_ids':['CS301']})
        history=[{'role':'assistant','content':answer,'timetable_context':timetable_context(timetable)},
                 {'role':'user','content':'Also give me which day it is'},
                 {'role':'assistant','content':'Groq rate limit reached','failed':True}]
        def model(prompt,payload,**kwargs):
            self.assertEqual(payload['last_timetable_context']['request']['course_ids'],['CS301'])
            self.assertFalse(any(m.get('failed') for m in payload['chat_history']))
            if prompt==GUARDRAIL_AGENT_PROMPT:
                return {'verdict':'allow','topic_scope':'college','modules':['timetable']}
            return {'route':'sql','timetable_request':payload['last_timetable_context']['request']}
        state=dict(user_id='STU001',role='student',session_id='followup-test',query='Also give me which day it is whether Monday or Tuesday etc',
                   chat_history=history,current_date='2026-11-17',timezone='Asia/Kolkata',agent_trace=[],retry_count=0,actions=[],sources=[])
        with patch('agents.llm.invoke_json',side_effect=model),patch('agents.llm.invoke_text',return_value=answer.replace('Thursday','Wednesday')), \
             patch('agents.supervisor_agent.college_now',return_value=NOW),patch('agents.graph.execute_query') as sql:
            result=build_graph().invoke(state)
        sql.assert_not_called()
        self.assertEqual(result['final_response'],answer)
        self.assertTrue(history[-1]['failed']) # original history was not changed

    def test_context_persisted_only_on_success_and_isolated_by_session(self):
        timetable=self.read({'mode':'week','date':'today','course_ids':['CS301']})
        client=self.app.test_client()
        headers={'Authorization':'Bearer '+create_access_token(identity='STU001')}
        payload={'session_id':'context-test','message':'When is CS301 this week?'}
        with patch('routes.chat_routes._get_graph') as graph:
            graph.return_value.invoke.return_value={'final_response':format_timetable(timetable),'sql_result':timetable}
            self.assertEqual(client.post('/api/chat',json=payload,headers=headers).status_code,200)
        history=load_history('STU001','context-test')
        self.assertEqual(history[-1]['timetable_context'],timetable_context(timetable))
        error=RateLimitError('quota',response=httpx.Response(429,request=httpx.Request('POST','https://example.test')),body=None)
        with patch('routes.chat_routes._get_graph') as graph:
            graph.return_value.invoke.side_effect=error
            self.assertEqual(client.post('/api/chat',json={**payload,'message':'Add the weekdays'},headers=headers).status_code,429)
        history=load_history('STU001','context-test')
        self.assertTrue(history[-1]['failed'])
        self.assertNotIn('timetable_context',history[-1])
        state=supervisor_intake_node({'chat_history':history,'agent_trace':[]})
        self.assertEqual(state['last_timetable_context'],timetable_context(timetable))
        for user,session in [('STU001','other-session'),('STU002','context-test')]:
            state=supervisor_intake_node({'chat_history':load_history(user,session),'agent_trace':[]})
            self.assertIsNone(state['last_timetable_context'])
        # Clearing the session removes its metadata along with its messages.
        self.assertEqual(client.delete('/api/chat/history/context-test',headers=headers).status_code,200)
        self.assertEqual(load_history('STU001','context-test'),[])

    def test_context_is_intent_not_cached_schedule_or_authorization(self):
        timetable=self.read({'mode':'week','date':'today','course_ids':['CS301']})
        request=timetable_context(timetable)['request']
        self.write("UPDATE course_timetable SET start_time='08:00',end_time='09:00' WHERE course_id='CS301'")
        self.assertEqual(self.sessions(self.read(request))[0]['start_time'],'08:00')
        self.write("UPDATE course_registrations SET status='completed' WHERE student_id='STU001' AND course_id='CS301'")
        with self.assertRaises(OperationError):self.read(request)

    def test_direct_weekday_bypass_retries_lookup_and_allows_clarification(self):
        context=timetable_context(self.read({'mode':'week','date':'today','course_ids':['CS301']}))
        state=dict(user_id='STU001',role='student',session_id='bypass-test',query='Also add weekday names',
                   chat_history=[{'role':'assistant','content':'Prior timetable','timetable_context':context}],
                   current_date='2026-11-17',timezone='Asia/Kolkata',agent_trace=[],retry_count=0,actions=[],sources=[])
        decisions=[{'verdict':'allow','topic_scope':'college','modules':['timetable']},
                   {'route':'direct','direct_answer':'Today is Wednesday.'},
                   {'route':'sql','timetable_request':context['request']}]
        with patch('agents.llm.invoke_json',side_effect=decisions) as model,patch('agents.llm.invoke_text',side_effect=RuntimeError('fallback')):
            result=build_graph().invoke(state)
        self.assertIn('Monday',result['final_response']);self.assertIn('Thursday',result['final_response'])
        self.assertNotIn('Today is Wednesday',result['final_response'])
        self.assertIn('fresh lookup',model.call_args.args[1]['validation_feedback'])
        with patch('agents.llm.invoke_json',side_effect=[decisions[0],{'route':'direct','needs_clarification':True,'direct_answer':'Which course do you mean?'}]):
            result=build_graph().invoke(state)
        self.assertEqual(result['final_response'],'Which course do you mean?')


if __name__=='__main__':unittest.main()
