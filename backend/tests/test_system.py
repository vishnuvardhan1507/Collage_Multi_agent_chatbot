"""Run with: python -m unittest discover -s backend/tests -v"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RAG_USE_CHROMA"] = "0"
os.environ["ALLOW_MODEL_DOWNLOAD"] = "0"

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from agents import llm
from agents.graph import build_graph
from agents.prompts import GUARDRAIL_AGENT_PROMPT, SQL_QUERY_AGENT_PROMPT, SUPERVISOR_AGENT_PROMPT, VALIDATOR_AGENT_PROMPT
from auth.routes import auth_bp
from db.init_db import init_db
from db.migrate import migrate, seed_demo
from routes.chat_routes import chat_bp
from routes.leave_routes import leave_bp
from tools.db_tool import execute_query, fetch_all, fetch_one, get_connection
from tools.memory_tool import append_turn, load_history, read_memory, sync_profile
from tools.operations import OperationError, eligible_courses, execute_operation, project_attendance
from tools.rag_tool import retrieve


TODAY = date(2026, 9, 30)


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.base.name) / "fixture.db"
        init_db(cls.fixture)
        migrate(cls.fixture)
        with sqlite3.connect(cls.fixture) as conn:
            conn.execute("UPDATE leave_requests SET from_date='2026-08-20', to_date='2026-08-22'")
            conn.execute("INSERT INTO classroom_bookings(classroom_id,faculty_id,booking_date,start_time,end_time,purpose) VALUES('Lab-302','FAC001','2026-08-21','10:00','12:00','Sample lab')")

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.database = root / "test.db"
        shutil.copyfile(self.fixture, self.database)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, JWT_SECRET_KEY="test-secret-key-at-least-thirty-two-characters", DATABASE_PATH=str(self.database),
            CHAT_MEMORY_DIR=str(root / "memory"), KNOWLEDGE_BASE_DIR=str(Path(__file__).resolve().parents[1] / "knowledge_base"),
            CHROMA_PERSIST_DIR=str(root / "vectors"), COLLEGE_TIMEZONE="Asia/Kolkata")
        JWTManager(self.app)
        for blueprint in (auth_bp, chat_bp, leave_bp):
            self.app.register_blueprint(blueprint)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.clock = patch("tools.operations.college_today", return_value=TODAY)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.client = self.app.test_client()

    def test_leave_overlap_and_deletion_rules(self):
        headers = self.token()
        payload = {'from_date': '2030-01-10', 'to_date': '2030-01-12', 'reason': 'Appointment'}
        created = self.client.post('/api/leaves', json=payload, headers=headers)
        self.assertEqual(created.status_code, 201)
        leave_id = created.json['leave']['leave_id']
        for start, end in [('2030-01-09','2030-01-10'), ('2030-01-12','2030-01-13'), ('2030-01-11','2030-01-11'), ('2030-01-09','2030-01-13')]:
            response = self.client.post('/api/leaves', json={**payload,'from_date': start,'to_date': end}, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertIn('overlap', response.json['error'])
        self.assertEqual(self.client.post('/api/leaves', json={**payload,'from_date':'2030-01-13','to_date':'2030-01-13'}, headers=headers).status_code, 201)
        self.assertEqual(self.client.delete(f'/api/leaves/{leave_id}', headers=self.token('FAC001','faculty')).status_code, 403)
        self.assertEqual(self.client.delete(f'/api/leaves/{leave_id}', headers=self.token('STU002')).status_code, 400)
        with patch('tools.operations.college_today', return_value=date(2030,1,13)):
            self.assertEqual(self.client.delete(f'/api/leaves/{leave_id}', headers=headers).status_code, 400)
        with patch('tools.operations.college_today', return_value=date(2030,1,12)):
            self.assertEqual(self.client.delete(f'/api/leaves/{leave_id}', headers=headers).status_code, 200)
        self.assertIsNone(fetch_one('SELECT * FROM leave_requests WHERE leave_id=?', (leave_id,)))

    def test_concurrent_leave_overlap(self):
        operation = {'type':'submit_leave','from_date':'2031-01-10','to_date':'2031-01-12','reason':'Appointment'}
        def submit(_):
            with self.app.app_context():
                try:
                    execute_operation(operation, 'STU001', 'student')
                    return True
                except OperationError:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(submit, range(2))), [False, True])

    def token(self, user="STU001", role="student"):
        token = create_access_token(identity=user, additional_claims={"role": role})
        return {"Authorization": f"Bearer {token}"}

    def state(self, role="student", query="Show my attendance"):
        return {"user_id": "FAC001" if role == "faculty" else "STU001", "role": role,
                "session_id": "session_test", "query": query, "chat_history": [],
                "current_date": TODAY.isoformat(), "timezone": "Asia/Kolkata", "retry_count": 0,
                "terminal": False, "agent_trace": [], "sources": [], "actions": []}

    def test_module_prompt_selection_and_fallback(self):
        from agents import prompts as p
        original = {'database_schema': p.DATABASE_SCHEMA, 'chat_history': [{'content':'unchanged'}],
                    'transport_catalog': {'routes': []}, 'menu_catalog': {'plans': []},
                    'active_leave_draft': {'reason':'private draft'}, 'attendance_courses': ['CS301']}
        for invalid in (None, [], 'transport', ['unknown'], ['transport', 1], ['transport', 'unknown']):
            state = {'modules': invalid}
            self.assertEqual(p.scoped_prompt(p.SUPERVISOR_AGENT_PROMPT, state),p.SUPERVISOR_AGENT_PROMPT)
            self.assertEqual(p.scoped_payload(original,state),original)
        state = {'modules':['transport']}
        payload = p.scoped_payload(original,state)
        self.assertIs(payload['chat_history'], original['chat_history'])
        self.assertNotIn('menu_catalog',payload)
        self.assertNotIn('active_leave_draft',payload)
        self.assertNotIn('attendance_courses',payload)
        self.assertEqual({line.split('(')[0] for line in payload['database_schema'].splitlines()},p.MODULE_TABLES['transport'])
        for prompt in p.PROMPT_SECTIONS:
            scoped=p.scoped_prompt(prompt,state)
            self.assertLess(len(scoped),len(prompt))
            self.assertNotIn('sharing_capacity',scoped)
            self.assertNotIn('min_projectors',scoped)
        for module in p.MODULE_TABLES:
            result=p.scoped_payload(original,{'modules':[module]})
            self.assertEqual({line.split('(')[0] for line in result['database_schema'].splitlines()},p.MODULE_TABLES[module])
        mixed=p.scoped_payload(original,{'modules':['transport','attendance','policy']})
        self.assertIn('bus_routes(',mixed['database_schema'])
        self.assertIn('attendance(',mixed['database_schema'])
        self.assertNotIn('classrooms(',mixed['database_schema'])

    def test_scoped_transport_graph_preserves_history_and_call_count(self):
        from agents import prompts as p
        for role in ('student','faculty'):
            state=self.state(role=role,query='its fare?')
            state['chat_history']=[{'role':'user','content':'Which bus serves Manikonda?'}]
            calls=[]
            def model(prompt,payload):
                calls.append((prompt,payload))
                self.assertEqual(payload['chat_history'],state['chat_history'])
                if prompt==p.GUARDRAIL_AGENT_PROMPT:
                    return {'verdict':'allow','topic_scope':'college','modules':['transport']}
                self.assertNotIn('menu_catalog',payload)
                self.assertNotIn('active_leave_draft',payload)
                if 'database_schema' in payload:
                    self.assertNotIn('attendance(',payload['database_schema'])
                    self.assertIn('bus_routes(',payload['database_schema'])
                if prompt==p.scoped_prompt(p.SUPERVISOR_AGENT_PROMPT,{'modules':['transport']}):
                    return {'route':'sql','scoped_request':'Monthly fare for Manikonda to campus'}
                if prompt==p.scoped_prompt(p.SQL_QUERY_AGENT_PROMPT,{'modules':['transport']}):
                    return {'sql':"SELECT bus_id,monthly_round_trip_fare FROM bus_routes WHERE bus_id='B05'"}
                return {'verdict':'valid'}
            def answer(prompt,payload,**kwargs):
                calls.append((prompt,payload))
                self.assertEqual(prompt,p.scoped_prompt(p.ANSWER_PROMPT,{'modules':['transport']}))
                self.assertEqual(payload['sql_result'][0]['monthly_round_trip_fare'],2200)
                return 'B05 costs INR 2,200 per month, round trip.'
            with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=answer):
                result=build_graph().invoke(state)
            self.assertEqual(len(calls),5)
            self.assertIn('2,200',result['final_response'])

    def test_module_specific_workflows(self):
        from agents import prompts as p
        scenarios=[
            ('attendance','student','next two classes',{'route':'sql'},
             {'projection_request':{'mode':'class_count','course':'Machine Learning','missed_classes':2}},'33'),
            ('classrooms','faculty','purpose: quiz',{'route':'sql'},
             {'operation':{'type':'book_classroom','classroom_id':'B-201','booking_date':'2030-01-10','start_time':'14:00','end_time':'15:00','purpose':'quiz'}},'confirm'),
            ('leave','student','family function',{'route':'sql','leave_update':{'action':'start','fields':{'reason':'family function'}}},None,'date'),
            ('timetable','student','And Monday?',{'route':'sql','timetable_request':{'mode':'weekday','weekday':0}},None,None),
            ('mess','student','Non-veg only',{'route':'menu','menu_request':{'plans':['non_veg'],'meals':['breakfast'],'dish':'upma'}},None,None),
        ]
        for module,role,query,plan,candidate,expected in scenarios:
            with self.subTest(module=module):
                state=self.state(role=role,query=query)
                state['session_id']='module_'+module
                state['chat_history']=[{'role':'user','content':'Show my Machine Learning attendance'}]
                def model(prompt,payload):
                    if prompt==p.GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college','modules':[module]}
                    if prompt==p.scoped_prompt(p.SUPERVISOR_AGENT_PROMPT,{'modules':[module]}):return plan
                    if prompt==p.scoped_prompt(p.SQL_QUERY_AGENT_PROMPT,{'modules':[module]}):return candidate
                    return {'verdict':'valid'}
                with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=RuntimeError('mock fallback')):
                    result=build_graph().invoke(state)
                self.assertTrue(result['final_response'])
                if module=='attendance':self.assertIn('next 2 classes',result['final_response'])
                if module=='classrooms':
                    self.assertEqual(result['confirmation']['status'],'pending')
                    self.assertIsNone(fetch_one("SELECT * FROM classroom_bookings WHERE booking_date='2030-01-10'"))
                if module=='leave':self.assertIn('family function',read_memory('STU001')['sessions'][-1]['leave_draft']['reason'])
                if module=='mess':self.assertIn('Thursday',result['final_response'])

    def model(self, candidates, route="sql", invalid_first=False):
        iterator = iter(candidates)
        calls = {"candidates": 0, "validators": 0, "feedback": [], "supervisor": []}

        def invoke(prompt, payload):
            if prompt == GUARDRAIL_AGENT_PROMPT:
                return {"verdict": "allow", "topic_scope": "college"}
            if prompt == SUPERVISOR_AGENT_PROMPT:
                calls["supervisor"].append(payload)
                return {"route": route, "scoped_request": payload["query"], "retrieval_query": "minimum attendance policy", "direct_answer": "Hello!"}
            if prompt == SQL_QUERY_AGENT_PROMPT:
                calls["candidates"] += 1
                calls["feedback"].append(payload.get("validation_feedback"))
                candidate = next(iterator)
                if isinstance(candidate, Exception):
                    raise candidate
                return candidate
            if prompt == VALIDATOR_AGENT_PROMPT:
                calls["validators"] += 1
                if invalid_first and calls["validators"] == 1:
                    return {"verdict": "invalid", "feedback": "Include course_name."}
                return {"verdict": "valid"}
            raise AssertionError("Unexpected prompt")
        return invoke, calls

    def test_chat_deletion_lookup_evidence_and_followup(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute("DELETE FROM leave_requests WHERE student_id='STU001'")
        def create(day, reason):
            return execute_operation({'type':'submit_leave','from_date':day,'to_date':day,'reason':reason}, 'STU001','student')['record_id']
        first = create('2030-03-01','Sample personal leave request')
        def run(selector, query, history=None):
            model, _ = self.model([{'operation': {'type':'delete_leave', **selector}}])
            def checked(prompt, payload):
                if prompt == VALIDATOR_AGENT_PROMPT:
                    self.assertEqual(payload['operation_evidence']['student_id'], 'STU001')
                    self.assertTrue(payload['operation_evidence']['can_delete'])
                    self.assertEqual(payload['operation']['leave_id'], payload['operation_evidence']['leave_id'])
                    self.assertEqual(payload['current_date'], TODAY.isoformat())
                    self.assertEqual(payload['timezone'], 'Asia/Kolkata')
                return model(prompt, payload)
            state = self.state(query=query)
            state['chat_history'] = history or []
            with patch.object(llm,'invoke_json',side_effect=checked):
                return build_graph().invoke(state)
        result = run({'description':'sample personal'}, 'Delete sample personal leave request')
        self.assertEqual(result['final_response'], f'Leave request #{first} deleted.')
        self.assertEqual(result['actions'], ['delete_leave'])
        self.assertIsNone(fetch_one('SELECT 1 FROM leave_requests WHERE leave_id=?',(first,)))
        first = create('2030-03-02','Sample personal leave request')
        second = create('2030-03-03','Sample personal leave request')
        result = run({'description':'sample personal'}, 'Delete sample personal leave request')
        self.assertIn('Which leave ID', result['final_response'])
        self.assertIn(f'#{first}', result['final_response'])
        self.assertIn(f'#{second}', result['final_response'])
        history = [{'role':'user','content':'Delete sample personal leave request'}, {'role':'assistant','content':result['final_response']}]
        result = run({'leave_id':second}, f'#{second}', history)
        self.assertEqual(result['actions'], ['delete_leave'])
        self.assertIsNotNone(fetch_one('SELECT 1 FROM leave_requests WHERE leave_id=?',(first,)))
        self.assertIn('No matching', run({'description':'no such reason'}, 'Delete no such reason leave')['final_response'])
        with get_connection() as conn:
            conn.execute('UPDATE leave_requests SET to_date=? WHERE leave_id=?', ('2020-01-01',first))
        self.assertIn('Past leave', run({'leave_id':first}, f'Delete leave #{first}')['final_response'])
        with get_connection() as conn:
            conn.execute('UPDATE leave_requests SET to_date=? WHERE leave_id=?', (TODAY.isoformat(),first))
        self.assertEqual(run({'leave_id':first}, f'Delete leave #{first}')['actions'], ['delete_leave'])

    def test_chat_deletion_cannot_resolve_other_students_leave(self):
        from tools.operations import resolve_leave_deletion
        row = fetch_one("SELECT leave_id FROM leave_requests WHERE student_id != 'STU001' LIMIT 1")
        self.assertIsNotNone(row)
        with self.assertRaisesRegex(OperationError, 'No matching'):
            resolve_leave_deletion({'type':'delete_leave','leave_id':row['leave_id']},'STU001','student')
        with self.assertRaisesRegex(OperationError, 'No matching'):
            resolve_leave_deletion({'type':'delete_leave','description':"%' OR 1=1 --"},'STU001','student')

    def test_new_answers_omit_source_commentary_but_keep_metadata(self):
        from agents.supervisor_agent import supervisor_aggregate_node
        for route, sources, response in [
            ('sql', [], 'Your attendance is 90%. These details are taken directly from the database query result.'),
            ('rag', ['leave_policy.md'], 'The sample allowance is five days [leave_policy.md].\nSources: leave_policy.md'),
        ]:
            state = self.state()
            state.update(route=route, sources=sources, sql_result=[], rag_context='Sample policies', scoped_request='Show details')
            with patch.object(llm, 'invoke_text', return_value=response):
                result = supervisor_aggregate_node(state)
            self.assertEqual(result['sources'], sources)
            self.assertNotIn('database query result', result['final_response'])
            self.assertNotIn('Sources:', result['final_response'])
            self.assertNotIn('leave_policy.md', result['final_response'])
            self.assertNotIn('Fictional demo.', result['final_response'])
        state = self.state()
        state.update(route='rag', sources=['leave_policy.md'], scoped_request='Show details')
        with patch.object(llm,'invoke_text',return_value='The allowance is unavailable. These are fictional sample policies.'):
            result = supervisor_aggregate_node(state)
        self.assertIn('unavailable', result['final_response'])
        self.assertIn('fictional', result['final_response'].lower())

    def test_bus_database_seed_and_migration(self):
        from db.init_db import BUS_ROUTES, BUS_STOPS, BUS_LOCALITY_ALIASES, TRANSPORT_SERVICES
        for table, expected in [('bus_routes',BUS_ROUTES),('bus_stops',BUS_STOPS),('bus_locality_aliases',BUS_LOCALITY_ALIASES),('transport_services',TRANSPORT_SERVICES)]:
            with sqlite3.connect(self.database) as conn:
                self.assertEqual(set(conn.execute('SELECT * FROM '+table)),set(expected))
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE bus_routes SET monthly_round_trip_fare=999 WHERE bus_id='B01'")
            conn.execute('DELETE FROM schema_migrations WHERE version=11')
        before=fetch_all('SELECT * FROM classroom_bookings')
        self.assertTrue(migrate(self.database))
        self.assertEqual(fetch_one("SELECT monthly_round_trip_fare FROM bus_routes WHERE bus_id='B01'")['monthly_round_trip_fare'],999)
        self.assertIsNone(migrate(self.database))
        self.assertEqual(before,fetch_all('SELECT * FROM classroom_bookings'))
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        for bus in BUS_ROUTES:
            rows=fetch_all('SELECT * FROM bus_stops WHERE bus_id=? ORDER BY stop_sequence',(bus[0],))
            self.assertEqual([r['stop_sequence'] for r in rows],list(range(1,len(rows)+1)))
            self.assertEqual([r['morning_pickup'] for r in rows],sorted(r['morning_pickup'] for r in rows))
            self.assertEqual([r['afternoon_dropoff'] for r in rows],sorted((r['afternoon_dropoff'] for r in rows),reverse=True))

    def test_bus_aliases_and_permissions(self):
        for user,role in [('STU001','student'),('FAC001','faculty')]:
            for locality,bus in [('bachupally','B01'),('chanda nagar','B02'),('tellapur','B03'),('financial district','B04'),('toli chowki','B05'),('banjara hills','B06'),('begumpet','B07'),('kukatpally','B08'),('miyapur','B09'),('madhapur','B10')]:
                rows=execute_query("SELECT r.bus_id,s.stop_name,s.morning_pickup,s.afternoon_dropoff FROM bus_locality_aliases a JOIN bus_routes r ON r.bus_id=a.bus_id LEFT JOIN bus_stops s ON s.bus_id=a.bus_id AND s.stop_sequence=a.stop_sequence WHERE lower(a.alias)='"+locality+"'",user,role)
                self.assertEqual(rows[0]['bus_id'],bus)
                self.assertIsNotNone(rows[0]['stop_name'])
            self.assertEqual(execute_query("SELECT * FROM bus_locality_aliases WHERE alias='uppal'",user,role),[])
            self.assertEqual(len(execute_query('SELECT * FROM bus_routes',user,role)),10)
            for sql in ["DELETE FROM bus_routes", "SELECT provenance FROM transport_services", "SELECT * FROM users"]:
                with self.assertRaises((PermissionError,sqlite3.DatabaseError,ValueError)):
                    execute_query(sql,user,role)
        self.assertIsNone(fetch_one("SELECT stop_sequence FROM bus_locality_aliases WHERE alias='serilingampally'")['stop_sequence'])
        with sqlite3.connect(self.database) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            for sql in ["UPDATE bus_routes SET monthly_round_trip_fare=-1", "UPDATE bus_stops SET morning_pickup='25:00'", "INSERT INTO bus_locality_aliases VALUES('B01','bad',999)"]:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)

    def test_transport_catalog_all_bus_formats_and_live_identity(self):
        from tools.db_tool import transport_catalog
        for user,role in [('STU001','student'),('FAC001','faculty')]:
            catalog=transport_catalog(user,role)
            self.assertEqual(len(catalog['routes']),10)
            self.assertIn('HITEC City',catalog['services'][0]['campus'])
            for n in range(1,11):
                for text in [f'B{n:02}',f'B-{n:02}',f'b {n:02}',f'B{n}',f'B-{n}']:
                    canonical=catalog['bus_id_aliases'][text.casefold()]
                    self.assertEqual(canonical,f'B{n:02}')
                    self.assertTrue(fetch_all('SELECT stop_name FROM bus_stops WHERE bus_id=?',(canonical,)))
            for unknown in ['b-99','b99','b-201','a-101']:
                self.assertNotIn(unknown,catalog['bus_id_aliases'])
        with self.assertRaises(PermissionError):
            transport_catalog('STU001','faculty')
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE transport_services SET campus='Updated campus'")
        self.assertEqual(transport_catalog('STU001','student')['services'][0]['campus'],'Updated campus')

    def test_campus_transport_context_reaches_every_planning_stage(self):
        for role in ['student','faculty']:
            for locality,bus in [('banjara hills','B06'),('manikonda','B05')]:
                for campus in ['institution','institute','college','campus']:
                    for reverse in [False,True]:
                        query=f'What is the bus fare from {campus} to {locality}?' if reverse else f'What is the bus fare from {locality} to {campus}?'
                        sql=("SELECT r.bus_id,r.monthly_round_trip_fare,t.currency,t.campus FROM bus_locality_aliases a "
                             "JOIN bus_routes r ON r.bus_id=a.bus_id JOIN transport_services t ON t.service_id=r.service_id "
                             f"WHERE lower(a.alias)='{locality}'")
                        model,_=self.model([{'sql':sql}])
                        with patch.object(llm,'invoke_json',side_effect=model) as calls, patch.object(llm,'invoke_text',return_value='Fare answer') as answer:
                            build_graph().invoke(self.state(role,query))
                        evidence=answer.call_args.args[1]['sql_result']
                        self.assertEqual(evidence[0]['bus_id'],bus)
                        self.assertEqual(evidence[0]['monthly_round_trip_fare'],2200)
                        for prompt in [SUPERVISOR_AGENT_PROMPT,SQL_QUERY_AGENT_PROMPT,VALIDATOR_AGENT_PROMPT]:
                            payload=next(c.args[1] for c in calls.call_args_list if c.args[0]==prompt)
                            self.assertEqual(payload['transport_catalog']['bus_id_aliases']['b-03'],'B03')
                            self.assertEqual(payload['query'],query)

    def test_transport_catalog_does_not_rewrite_classroom_query(self):
        query='Show classroom B-201 capacity'
        model,_=self.model([{'sql':"SELECT classroom_id,capacity FROM classrooms WHERE classroom_id='B-201'"}])
        with patch.object(llm,'invoke_json',side_effect=model) as calls, patch.object(llm,'invoke_text',return_value='B-201 has 80 seats.') as answer:
            build_graph().invoke(self.state('faculty',query))
        self.assertEqual(answer.call_args.args[1]['sql_result'][0]['capacity'],80)
        for call in calls.call_args_list:
            if 'query' in call.args[1]: self.assertEqual(call.args[1]['query'],query)

    def test_bus_followup_retrieval_preserves_locality(self):
        sql="SELECT r.bus_id,r.monthly_round_trip_fare,s.stop_name,s.afternoon_dropoff FROM bus_routes r JOIN bus_stops s ON r.bus_id=s.bus_id WHERE r.bus_id='B09' AND s.locality='Miyapur'"
        model,_=self.model([{'sql':sql}])
        state=self.state(query='What is its fare and return time?')
        state['chat_history']=[{'role':'user','content':'Which bus from Miyapur?'},{'role':'assistant','content':'B09 serves Miyapur.'}]
        with patch.object(llm,'invoke_json',side_effect=model) as calls, patch.object(llm,'invoke_text',return_value='B09: INR 1800 monthly; Miyapur drop-off 17:25.') as answer:
            result=build_graph().invoke(state)
        evidence=answer.call_args.args[1]['sql_result']
        self.assertEqual(evidence[0]['monthly_round_trip_fare'],1800)
        self.assertEqual(evidence[0]['afternoon_dropoff'],'17:25')
        self.assertEqual(result['sources'],[])
        self.assertIn('db_tool',result['agent_trace'])
        self.assertNotIn('rag_tool',result['agent_trace'])
        payload=next(c.args[1] for c in calls.call_args_list if c.args[0]==SQL_QUERY_AGENT_PROMPT)
        self.assertEqual(payload['chat_history'],state['chat_history'])

    def test_bus_filename_disclaimer_removed_without_losing_fare(self):
        from agents.supervisor_agent import supervisor_aggregate_node
        state = self.state(query='What is the Manikonda bus fare?')
        state.update(route='rag', sources=['bus_routes.md'], scoped_request=state['query'])
        response = 'B05 serves Manikonda. Monthly round-trip fare: INR 2,200.\n\n*This figure is from the fictional demo schedule in the bus_routes.md file and is not a real fare.*'
        with patch.object(llm,'invoke_text',return_value=response):
            result = supervisor_aggregate_node(state)
        self.assertIn('INR 2,200', result['final_response'])
        self.assertNotIn('bus_routes.md', result['final_response'])
        self.assertNotIn('This figure', result['final_response'])
        self.assertNotIn('(Fictional demo.)', result['final_response'])
        self.assertEqual(result['sources'], ['bus_routes.md'])

    def test_bus_answer_has_no_automatic_demo_footer(self):
        from agents.supervisor_agent import supervisor_aggregate_node
        state = self.state(query='Give me the bus fee from Kokapet')
        state.update(route='rag', sources=['bus_routes.md'], scoped_request=state['query'])
        with patch.object(llm, 'invoke_text', return_value='B04: INR 2,500 per month, round trip.\n\n(Fictional demo.)'):
            result = supervisor_aggregate_node(state)
        self.assertEqual(result['final_response'], 'B04: INR 2,500 per month, round trip.')
        self.assertEqual(result['sources'], ['bus_routes.md'])

    def test_room_equipment_filters_and_unknown_values(self):
        self.assertEqual(len(execute_query('SELECT * FROM classrooms','FAC001','faculty')), 10)
        filters = [('has_ac=1',6), ('projector_count>=2',2), ('computer_count>=30',3), ('capacity>=100',2)]
        for where, count in filters:
            self.assertEqual(len(execute_query('SELECT * FROM classrooms WHERE '+where,'FAC001','faculty')), count)
        rows = execute_query('SELECT * FROM classrooms WHERE has_ac=1 AND projector_count>=1 AND computer_count>=30 ORDER BY capacity,classroom_id','FAC001','faculty')
        self.assertEqual([r['classroom_id'] for r in rows], ['Lab-302','Lab-303'])
        self.assertEqual(execute_query('SELECT * FROM classrooms WHERE computer_count>=100','FAC001','faculty'), [])
        execute_operation(self.booking('10:00','11:00'),'FAC001','faculty')
        rows = execute_query("SELECT c.* FROM classrooms c WHERE has_ac=1 AND computer_count>=30 AND NOT EXISTS (SELECT 1 FROM classroom_bookings b WHERE b.classroom_id=c.classroom_id AND booking_date='2026-10-01' AND start_time<'11:00' AND end_time>'10:00') ORDER BY capacity,classroom_id",'FAC001','faculty')
        self.assertEqual([r['classroom_id'] for r in rows], ['Lab-303'])
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE classrooms SET has_ac=NULL WHERE classroom_id='Lab-303'")
        self.assertEqual(execute_query("SELECT * FROM classrooms WHERE classroom_id='Lab-303' AND has_ac=1",'FAC001','faculty'), [])

    def test_classroom_time_clarification_and_followup(self):
        from agents import prompts as p
        initial='Show me the available classrooms for booking for 45 students today'
        question='What start and end time do you need the room today?'
        history=[{'role':'user','content':initial},{'role':'assistant','content':question}]
        scopes={'modules':['classrooms']}
        captured=[]
        def model(prompt,payload):
            if prompt==p.GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college','modules':['classrooms']}
            if prompt==p.scoped_prompt(p.SUPERVISOR_AGENT_PROMPT,scopes):
                self.assertIn('Only filter building when the user explicitly requests',prompt)
                if not payload['chat_history']:
                    return {'route':'direct','direct_answer':question,'scoped_request':'45 seats today, times missing'}
                self.assertEqual(payload['chat_history'],history)
                return {'route':'sql','scoped_request':'Rooms for 45 students on 2026-09-30 from 15:00 to 16:00'}
            if prompt==p.scoped_prompt(p.SQL_QUERY_AGENT_PROMPT,scopes):
                captured.append(payload)
                return {'sql':"SELECT c.* FROM classrooms c WHERE capacity>=45 AND NOT EXISTS (SELECT 1 FROM classroom_bookings b WHERE b.classroom_id=c.classroom_id AND booking_date='2026-09-30' AND start_time<'16:00' AND end_time>'15:00') ORDER BY capacity,classroom_id"}
            self.assertIn('Classrooms are not department-scoped',prompt)
            return {'verdict':'valid'}
        before=fetch_all('SELECT * FROM classroom_bookings')
        with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=RuntimeError('factual fallback')):
            result=build_graph().invoke(self.state(role='faculty',query=initial))
            self.assertEqual(result['final_response'],question)
            state=self.state(role='faculty',query='3pm to 4pm');state['chat_history']=history
            result=build_graph().invoke(state)
        self.assertEqual(len(captured),1)
        self.assertIn('45 students',captured[0]['scoped_request'])
        self.assertTrue(result['sql_result'])
        self.assertTrue(all(r['capacity']>=45 for r in result['sql_result']))
        self.assertEqual(before,fetch_all('SELECT * FROM classroom_bookings'))
        # An explicit building preference is supported without tying it to faculty department.
        rooms=execute_query("SELECT * FROM classrooms WHERE building='Main Block' AND capacity>=45 AND has_ac=1 AND projector_count>=1",'FAC001','faculty')
        self.assertEqual([r['classroom_id'] for r in rooms],['A-102'])

    def test_booking_requirements_rechecked_and_validated(self):
        requirements = {'min_capacity':30,'has_ac':True,'min_projectors':1,'min_computers':30}
        op = {**self.booking(), 'requirements':requirements}
        with self.assertRaises(PermissionError):
            execute_operation(op,'STU001','student')
        for invalid in [{'min_computers':-1},{'min_capacity':True},{'has_ac':1},{'extra':1},None]:
            with self.assertRaises(OperationError):
                execute_operation({**op,'requirements':invalid},'FAC001','faculty')
        for column, value in [('has_ac',0),('computer_count',20),('projector_count',None),('capacity',10)]:
            with sqlite3.connect(self.database) as conn:
                original = conn.execute(f"SELECT {column} FROM classrooms WHERE classroom_id='Lab-302'").fetchone()[0]
                conn.execute(f"UPDATE classrooms SET {column}=? WHERE classroom_id='Lab-302'",(value,))
            with self.assertRaises(OperationError):
                execute_operation(op,'FAC001','faculty')
            with sqlite3.connect(self.database) as conn:
                conn.execute(f"UPDATE classrooms SET {column}=? WHERE classroom_id='Lab-302'",(original,))
        self.assertEqual(execute_operation(op,'FAC001','faculty')['rows_affected'],1)
        with self.assertRaisesRegex(OperationError,'already booked'):
            execute_operation(op,'FAC002','faculty')

    def test_equipment_migration_preserves_existing_rooms_and_bookings(self):
        before = fetch_all('SELECT * FROM classroom_bookings')
        with sqlite3.connect(self.database) as conn:
            conn.execute('DELETE FROM schema_migrations WHERE version=6')
            conn.execute("UPDATE classrooms SET capacity=99,building='Custom',has_ac=0,projector_count=NULL WHERE classroom_id='Lab-302'")
            conn.execute("INSERT INTO classrooms(classroom_id,building,capacity) VALUES('Custom-1','Custom',20)")
            conn.execute("DELETE FROM classrooms WHERE classroom_id='Seminar-2'")
        self.assertTrue(migrate(self.database))
        room = fetch_one("SELECT * FROM classrooms WHERE classroom_id='Lab-302'")
        self.assertEqual((room['capacity'],room['building'],room['has_ac'],room['projector_count']), (99,'Custom',0,1))
        self.assertIsNone(fetch_one("SELECT has_ac FROM classrooms WHERE classroom_id='Custom-1'")['has_ac'])
        self.assertEqual(before,fetch_all('SELECT * FROM classroom_bookings'))
        self.assertIsNone(migrate(self.database))
        self.assertIsNotNone(fetch_one("SELECT * FROM classrooms WHERE classroom_id='Seminar-2'"))

    def test_contextual_room_booking_retains_requirements(self):
        op = {**self.booking(),'requirements':{'has_ac':True,'min_computers':30}}
        model, _ = self.model([{'operation':op}])
        state = self.state(role='faculty',query='Book Lab-302 for that session')
        state['chat_history'] = [{'role':'user','content':'Find an AC room with 30 computers on October 1 from 14:00 to 16:00 for Demo lab'}, {'role':'assistant','content':'Lab-302 or Lab-303. Which room should I book?'}]
        with patch.object(llm,'invoke_json',side_effect=model) as calls:
            result = build_graph().invoke(state)
        payload = next(call.args[1] for call in calls.call_args_list if call.args[0] == VALIDATOR_AGENT_PROMPT)
        self.assertEqual(payload['operation']['requirements'],op['requirements'])
        self.assertEqual(result['actions'], [])
        self.assertEqual(result['confirmation']['status'], 'pending')

    def test_booking_purpose_followup_does_not_repeat_question(self):
        op = {**self.booking('09:00','11:00'), 'classroom_id':'B-202', 'purpose':'conducting exam'}
        model, _ = self.model([{'operation':op}])
        def repeated_supervisor(prompt,payload):
            if prompt == SUPERVISOR_AGENT_PROMPT:
                self.assertIn('conducting exam', payload['booking_continuation'])
                return {'route':'direct','direct_answer':'Please specify the purpose?'}
            if prompt == SQL_QUERY_AGENT_PROMPT:
                self.assertIn('purpose is no longer missing', payload['scoped_request'])
            return model(prompt,payload)
        state = self.state(role='faculty',query='purpose:conducting exam')
        state['chat_history'] = [{'role':'user','content':'Book b-202 on 2026-10-01 from 09:00 to 11:00'}, {'role':'assistant','content':'Could you specify the purpose of the booking for classroom B-202 on 2026-10-01 from 09:00 to 11:00?'}]
        with patch.object(llm,'invoke_json',side_effect=repeated_supervisor):
            result=build_graph().invoke(state)
        self.assertEqual(result['actions'],[])
        self.assertIn('conducting exam',result['confirmation']['summary'])
        row=fetch_one("SELECT purpose,classroom_id FROM classroom_bookings WHERE booking_date='2026-10-01'")
        self.assertIsNone(row)

    def test_class_count_attendance_projection(self):
        from contextlib import closing
        from agents.supervisor_agent import _projection_text
        with closing(get_connection()) as conn, conn:
            conn.execute("UPDATE attendance SET attended_classes=33,total_classes=41 WHERE student_id='STU001' AND course_id='CS301'")
        request = {'mode':'class_count','course':'machine learning','missed_classes':2}
        result = project_attendance('STU001','student',request)
        self.assertEqual(result['current_percentage'],80.49)
        self.assertEqual(result['projected_percentage'],76.74)
        self.assertIn('33/43',_projection_text(result))
        self.assertEqual(fetch_one("SELECT total_classes FROM attendance WHERE student_id='STU001' AND course_id='CS301'")['total_classes'],41)
        for count in (0,-1,True,2.5,'2',367):
            with self.assertRaises(OperationError):
                project_attendance('STU001','student',{**request,'missed_classes':count})
        with self.assertRaises(PermissionError):
            project_attendance('FAC001','faculty',request)
        with self.assertRaises(OperationError):
            project_attendance('STU001','student',{**request,'course':'Unknown course'})
        with closing(get_connection()) as conn, conn:
            conn.execute("DELETE FROM attendance WHERE student_id='STU001' AND course_id='CS301'")
        self.assertIsNone(project_attendance('STU001','student',request)['projected_percentage'])

    def test_attendance_course_name_case_insensitive(self):
        expected = fetch_one("SELECT attended_classes,total_classes FROM attendance WHERE student_id='STU001' AND course_id='CS301'")
        for title in ('Machine Learning', 'machine learning', 'MACHINE LEARNING'):
            sql = "SELECT a.student_id,a.course_id,a.attended_classes,a.total_classes FROM attendance a JOIN courses c ON c.course_id=a.course_id WHERE c.course_name='" + title + "'"
            rows = execute_query(sql, 'STU001', 'student')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['student_id'], 'STU001')
            self.assertEqual(rows[0]['course_id'], 'CS301')
            self.assertEqual(rows[0]['attended_classes'], expected['attended_classes'])
            self.assertEqual(rows[0]['total_classes'], expected['total_classes'])
            self.assertEqual(execute_query(sql + " AND a.student_id='STU002'", 'STU001', 'student'), [])
        self.assertEqual(execute_query("SELECT * FROM courses WHERE course_name='Unknown Course'", 'STU001', 'student'), [])

    def test_student_scope_cannot_be_bypassed_by_predicates_or_union(self):
        for sql in ["SELECT student_id FROM students", "SELECT student_id FROM students WHERE 'STU001'='STU001' OR 1=1", "SELECT student_id FROM attendance UNION SELECT student_id FROM results"]:
            rows = execute_query(sql, "STU001", "student")
            self.assertTrue(rows)
            self.assertEqual({r["student_id"] for r in rows}, {"STU001"})
        self.assertEqual(execute_query("SELECT * FROM students WHERE student_id='STU005'", "STU001", "student"), [])

    def test_faculty_scope_and_public_information(self):
        students = execute_query("SELECT student_id FROM students", "FAC001", "faculty")
        self.assertIn("STU001", {r["student_id"] for r in students})
        self.assertNotIn("STU005", {r["student_id"] for r in students})
        self.assertEqual({r["department"] for r in execute_query("SELECT * FROM courses", "FAC001", "faculty")}, {"Computer Science"})
        self.assertEqual(len(execute_query("SELECT * FROM faculty", "STU001", "student")), 7)
        self.assertTrue(execute_query("SELECT * FROM prerequisites", "STU001", "student"))
        occupancy = execute_query("SELECT * FROM classroom_bookings", "FAC001", "faculty")
        self.assertEqual(set(occupancy[0]), {"classroom_id", "booking_date", "start_time", "end_time"})

    def test_credentials_admin_and_all_generated_writes_are_blocked(self):
        candidates = ["SELECT * FROM users", "SELECT * FROM sqlite_master", "SELECT readfile('college.db')",
                      "ATTACH DATABASE 'college.db' AS other", "UPDATE attendance SET attended_classes=99",
                      "DELETE FROM attendance", "INSERT INTO leave_requests(student_id) VALUES ('STU001')",
                      "WITH x AS (SELECT 1) DELETE FROM attendance", "SELECT 1; SELECT 2", "PRAGMA database_list"]
        for sql in candidates:
            with self.subTest(sql=sql), self.assertRaises((PermissionError, sqlite3.Error)):
                execute_query(sql, "STU001", "student")

    def test_identity_rechecked_in_tools(self):
        with self.assertRaises(PermissionError):
            execute_query("SELECT * FROM students", "STU001", "faculty")

    def test_leave_submit_and_scoped_review(self):
        result = execute_operation({"type": "submit_leave", "from_date": "2026-10-01", "to_date": "2026-10-02", "reason": "Medical appointment"}, "STU001", "student")
        row = fetch_one("SELECT * FROM leave_requests WHERE leave_id=?", (result["record_id"],))
        self.assertEqual((row["student_id"], row["status"]), ("STU001", "pending"))
        op = {"type": "review_leave", "leave_id": result["record_id"], "status": "approved"}
        with self.assertRaises(OperationError):
            execute_operation(op, "FAC003", "faculty")
        execute_operation(op, "FAC001", "faculty")
        with self.assertRaises(OperationError):
            execute_operation(op, "FAC001", "faculty")
        self.assertEqual(fetch_one("SELECT status FROM leave_requests WHERE leave_id=?", (result["record_id"],))["status"], "approved")

    def test_rejection_reason_required_and_visible_to_owner(self):
        created = execute_operation({'type':'submit_leave','from_date':'2032-01-10','to_date':'2032-01-10','reason':'Personal'}, 'STU001','student')
        leave_id = created['record_id']
        url = f'/api/leaves/{leave_id}/review'
        for reason in [None, '', '   ', 'x' * 1001]:
            response = self.client.patch(url, json={'status':'rejected','rejection_reason':reason}, headers=self.token('FAC001','faculty'))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(fetch_one('SELECT status FROM leave_requests WHERE leave_id=?',(leave_id,))['status'], 'pending')
        self.assertEqual(self.client.patch(url, json={'status':'rejected','rejection_reason':'Required assessment'}, headers=self.token('FAC003','faculty')).status_code, 400)
        response = self.client.patch(url, json={'status':'rejected','rejection_reason':'  Required assessment  '}, headers=self.token('FAC001','faculty'))
        self.assertEqual(response.status_code, 200)
        rows = self.client.get('/api/leaves', headers=self.token()).json['leaves']
        self.assertEqual(next(row for row in rows if row['leave_id'] == leave_id)['rejection_reason'], 'Required assessment')
        self.assertEqual(execute_query(f'SELECT rejection_reason FROM leave_requests WHERE leave_id={leave_id}', 'STU001','student')[0]['rejection_reason'], 'Required assessment')
        self.assertEqual(self.client.patch(url, json={'status':'rejected','rejection_reason':'Changed'}, headers=self.token('FAC001','faculty')).status_code, 400)

    def test_leave_rejection_and_dates(self):
        execute_operation({"type": "review_leave", "leave_id": 1, "status": "rejected", "rejection_reason": "Required assessment"}, "FAC001", "faculty")
        for start, end in [("2026-02-30", "2026-10-01"), ("2026-10-02", "2026-10-01"), ("2026-09-01", "2026-10-01")]:
            with self.assertRaises(OperationError):
                execute_operation({"type": "submit_leave", "from_date": start, "to_date": end, "reason": "Leave"}, "STU001", "student")
        with self.assertRaises(OperationError):
            execute_operation({"type": "submit_leave", "from_date": "2026-10-01", "to_date": "2026-10-01", "reason": " "}, "STU001", "student")

    def test_operations_reject_role_and_identity_injection(self):
        with self.assertRaises(PermissionError):
            execute_operation({"type": "review_leave", "leave_id": 1, "status": "approved"}, "STU001", "student")
        with self.assertRaises(OperationError):
            execute_operation({"type": "register_course", "course_id": "MA201", "student_id": "STU002"}, "STU001", "student")

    def test_registration_prerequisites_and_duplicates(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute("DELETE FROM course_registrations WHERE student_id='STU001' AND course_id='CS401'")
        with self.assertRaisesRegex(OperationError, "prerequisites"):
            execute_operation({"type": "register_course", "course_id": "CS401"}, "STU001", "student")
        with self.assertRaisesRegex(OperationError, "already"):
            execute_operation({"type": "register_course", "course_id": "CS301"}, "STU001", "student")
        with sqlite3.connect(self.database) as conn:
            conn.execute("INSERT INTO courses(course_id,course_name,faculty_id) VALUES ('CS390','Advanced demo','FAC001')")
            conn.execute("INSERT INTO prerequisites VALUES ('CS390','CS201')")
        execute_operation({"type": "register_course", "course_id": "CS390"}, "STU001", "student")
        self.assertIsNotNone(fetch_one("SELECT 1 FROM course_registrations WHERE student_id='STU001' AND course_id='CS390'"))

    def test_course_eligibility_uses_department_registrations_and_completed_prerequisites(self):
        # Seeded STU001 has a registration in every CS offering, including pending ones.
        result = eligible_courses('STU001', 'student')
        self.assertEqual(result['department'], 'Computer Science')
        self.assertEqual(result['courses'], [])
        self.assertEqual(result['unregistered_count'], 0)

        with sqlite3.connect(self.database) as conn:
            conn.execute("INSERT INTO courses(course_id,course_name,department,credits,faculty_id) VALUES('CS990','Eligible elective','Computer Science',3,'FAC001')")
            conn.execute("INSERT INTO courses(course_id,course_name,department,credits,faculty_id) VALUES('CS991','Blocked elective','Computer Science',3,'FAC001')")
            conn.execute("INSERT INTO courses(course_id,course_name,department,credits,faculty_id) VALUES('IT990','Other department elective','Information Technology',3,'FAC007')")
            conn.execute("INSERT INTO prerequisites VALUES('CS990','CS201')")
            conn.execute("INSERT INTO prerequisites VALUES('CS991','CS301')")
        result = eligible_courses('STU001', 'student')
        self.assertEqual(result['courses'], [{'course_id':'CS990','course_name':'Eligible elective'}])
        self.assertEqual(result['unregistered_count'], 2)
        self.assertEqual(result['blocked_by_prerequisites'], 1)
        with self.assertRaises(PermissionError):
            eligible_courses('FAC001', 'faculty')
        with self.assertRaises(PermissionError):
            eligible_courses('STU002', 'faculty')

    def test_faculty_teaching_load_is_active_scoped_and_bypasses_sql_generation(self):
        from agents import prompts as p
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tools.timetable import read_teaching_load
        now=datetime(2026,10,9,12,0,tzinfo=ZoneInfo('Asia/Kolkata'))
        load=read_teaching_load({'date':'today'},'FAC001','faculty',now)
        self.assertEqual([course['course_id'] for course in load['courses']],['CS301','CS302','CS303','CS304'])
        with self.assertRaises(PermissionError): read_teaching_load({'date':'today'},'STU001','student',now)
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE course_timetable SET effective_to='2026-10-08' WHERE course_id='CS304'")
        self.assertNotIn('CS304',[course['course_id'] for course in read_teaching_load({'date':'today'},'FAC001','faculty',now)['courses']])
        state=self.state(role='faculty',query='What subjects am I handling this term?')
        def model(prompt,payload):
            if prompt==p.GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college','modules':['courses']}
            if prompt==p.scoped_prompt(p.SUPERVISOR_AGENT_PROMPT,{'modules':['courses']}):return {'route':'sql','teaching_load_request':{'date':'today'}}
            self.fail('Teaching load must not call the SQL generator.')
        with patch('agents.supervisor_agent.college_now',return_value=now),patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text') as text:
            result=build_graph().invoke(state)
        self.assertIn('CS301',result['final_response']);self.assertIn('teaching_load_tool',result['agent_trace']);text.assert_not_called()

    def test_chat_course_eligibility_uses_trusted_read_and_factual_fallback(self):
        from agents import prompts as p
        state = self.state(query='What courses can I register for now?')
        calls = []
        def planner(prompt, payload):
            calls.append(prompt)
            if prompt == GUARDRAIL_AGENT_PROMPT:
                return {'verdict':'allow','topic_scope':'college','modules':['courses']}
            if prompt == p.scoped_prompt(p.SUPERVISOR_AGENT_PROMPT, {'modules':['courses']}):
                return {'route':'sql','course_eligibility_request':{'action':'list_eligible'}}
            self.fail('Eligibility lookup should not use generated SQL or answer generation.')
        with patch.object(llm, 'invoke_json', side_effect=planner), patch.object(llm, 'invoke_text') as answer:
            result = build_graph().invoke(state)
        self.assertEqual(result['course_eligibility_result']['department'], 'Computer Science')
        self.assertIn('no unregistered courses listed', result['final_response'])
        self.assertEqual(len(calls), 2)
        answer.assert_not_called()
        self.assertIn('course_eligibility_tool', result['agent_trace'])

    def booking(self, start="14:00", end="16:00"):
        return {"type": "book_classroom", "classroom_id": "Lab-302", "booking_date": "2026-10-01", "start_time": start, "end_time": end, "purpose": "Demo lab"}

    def test_booking_overlap_and_adjacent_slots(self):
        execute_operation(self.booking(), "FAC001", "faculty")
        with self.assertRaisesRegex(OperationError, "already booked"):
            execute_operation(self.booking("15:00", "17:00"), "FAC002", "faculty")
        execute_operation(self.booking("16:00", "17:00"), "FAC002", "faculty")
        for start, end in [("18:00", "17:00"), ("9:00", "10:00"), ("25:00", "26:00")]:
            with self.assertRaises(OperationError):
                execute_operation(self.booking(start, end), "FAC001", "faculty")

    def test_concurrent_booking_only_one_commits(self):
        def book(user):
            with self.app.app_context():
                try:
                    execute_operation(self.booking(), user, "faculty")
                    return True
                except OperationError:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(book, ["FAC001", "FAC002"]))
        self.assertEqual(sorted(outcomes), [False, True])

    def leave_planner(self, update, observed=None):
        def model(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college'}
            if prompt==SUPERVISOR_AGENT_PROMPT:
                if observed is not None:observed.append(payload.get('active_leave_draft'))
                return {'route':'sql','leave_update':update}
            raise AssertionError('Leave draft must not regenerate fields in another model call')
        return model

    def test_leave_draft_reason_then_dates_reload_and_confirm(self):
        from tools.leave_drafts import load_draft
        headers=self.token()
        before=fetch_all('SELECT * FROM leave_requests')
        with patch.object(llm,'invoke_json',side_effect=self.leave_planner({'action':'start','fields':{'reason':'family function'}})):
            first=self.client.post('/api/chat',headers=headers,json={'session_id':'leave-context','message':'I have a family function next week, so I need a few days off.'})
        self.assertEqual(first.status_code,200)
        self.assertIn('start date',first.json['response'])
        self.assertNotIn('reason',first.json['response'])
        self.assertEqual(load_draft('STU001','leave-context')[0],{'reason':'family function'})
        self.assertEqual(self.client.get('/api/chat/history',headers=headers).status_code,200)
        seen=[]
        with patch.object(llm,'invoke_json',side_effect=self.leave_planner({'action':'update','fields':{'from_date':'2026-10-14','to_date':'2026-10-16'}},seen)):
            second=self.client.post('/api/chat',headers=headers,json={'session_id':'leave-context','message':'14th-16th october'})
        self.assertEqual(second.status_code,200)
        self.assertEqual(seen,[{'reason':'family function'}])
        self.assertIn('family function',second.json['confirmation']['summary'])
        self.assertEqual(second.json['confirmation']['status'],'pending')
        self.assertIsNone(load_draft('STU001','leave-context')[0])
        self.assertEqual(before,fetch_all('SELECT * FROM leave_requests'))
        identifier=second.json['confirmation']['id']
        result=self.client.post('/api/chat/confirmations/'+identifier,headers=headers,json={'session_id':'leave-context','decision':'confirm'})
        self.assertEqual(result.json['confirmation']['status'],'confirmed')
        self.assertEqual(fetch_one("SELECT reason FROM leave_requests WHERE student_id='STU001' AND from_date='2026-10-14'")['reason'],'family function')
        self.client.post('/api/chat/confirmations/'+identifier,headers=headers,json={'session_id':'leave-context','decision':'confirm'})
        self.assertEqual(len(fetch_all('SELECT * FROM leave_requests')),len(before)+1)

    def test_leave_draft_dates_first_corrections_new_request_and_isolation(self):
        from tools.leave_drafts import merge_draft,load_draft,missing_question
        initial=merge_draft('STU001','student','draft',{'action':'start','fields':{'from_date':'2026-10-14','to_date':'2026-10-16'}},0)
        self.assertIn('reason',missing_question(initial['draft']))
        updated=merge_draft('STU001','student','draft',{'action':'update','fields':{'reason':'graduation ceremony','to_date':'2026-10-17'}},initial['revision'])
        self.assertIsNone(missing_question(updated['draft']))
        self.assertEqual(updated['draft']['from_date'],'2026-10-14')
        clear=merge_draft('STU001','student','draft',{'action':'update','fields':{'from_date':None}},updated['revision'])
        self.assertIn('start date',missing_question(clear['draft']))
        self.assertIsNone(load_draft('STU002','draft')[0])
        self.assertIsNone(load_draft('STU001','different')[0])
        replaced=merge_draft('STU001','student','draft',{'action':'start','fields':{'reason':'appointment'}},clear['revision'])
        self.assertEqual(replaced['draft'],{'reason':'appointment'})
        merge_draft('STU001','student','draft',{'action':'cancel'},replaced['revision'])
        self.assertIsNone(load_draft('STU001','draft')[0])

    def test_leave_draft_stale_update_deleted_session_and_invalid_fields(self):
        from tools.leave_drafts import merge_draft,validate_update,load_draft
        from tools.memory_tool import delete_session
        from tools.confirmations import propose
        initial=merge_draft('STU001','student','stale',{'action':'start','fields':{'reason':'family event'}},0)
        with self.assertRaises(OperationError):merge_draft('STU001','student','stale',{'action':'update','fields':{'reason':'old answer'}},0)
        with self.assertRaises(OperationError):propose({'type':'submit_leave','from_date':'2026-10-14','to_date':'2026-10-16','reason':'old answer'},'STU001','student','stale',expected_leave_revision=0)
        delete_session('STU001','stale')
        with self.assertRaises(ValueError):merge_draft('STU001','student','stale',{'action':'update','fields':{}},initial['revision'])
        with self.assertRaises(PermissionError):merge_draft('FAC001','faculty','x',{'action':'start'},0)
        for update in [{'action':'update','fields':{'user_id':'STU002'}},{'action':'start','fields':{'from_date':'14th October'}},{'action':'cancel','fields':{'reason':'x'}}]:
            with self.assertRaises(ValueError):validate_update(update)

    def test_leave_draft_unrelated_task_discards_incomplete_details(self):
        from tools.leave_drafts import merge_draft,load_draft
        merge_draft('STU001','student','session_test',{'action':'start','fields':{'reason':'family event'}},0)
        model,_=self.model([],route='direct')
        with patch.object(llm,'invoke_json',side_effect=model):build_graph().invoke(self.state(query='Hello, tell me what you can do'))
        self.assertIsNone(load_draft('STU001','session_test')[0])

    def test_timetable_weekday_resolution_and_boundaries(self):
        from tools.timetable import read_timetable,validate_timetable_request,format_timetable
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,10,9,12,0,tzinfo=ZoneInfo('Asia/Kolkata'))
        for user,role in [('STU001','student'),('FAC001','faculty')]:
            result=read_timetable({'mode':'weekday','weekday':0},user,role,now)
            self.assertEqual(result['start_date'],'2026-10-12')
            self.assertEqual(result['days'][0]['count'],2)
            self.assertEqual([(r['course_name'],r['start_time'],r['end_time']) for r in result['days'][0]['sessions']], [('Machine Learning','09:00','10:00'),('Operating Systems','11:00','12:00')])
            self.assertIn('2026-10-12',format_timetable(result))
        self.assertEqual(read_timetable({'mode':'weekday','weekday':4},'STU001','student',now)['start_date'],'2026-10-09')
        self.assertEqual(read_timetable({'mode':'weekday','weekday':5},'STU001','student',now)['days'][0]['count'],0)
        for value in [-1,7,True,'0',None,[],0.0]:
            with self.assertRaises(OperationError):validate_timetable_request({'mode':'weekday','weekday':value},now)
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE course_timetable SET effective_to='2026-10-11' WHERE course_id='CS301'")
        result=read_timetable({'mode':'weekday','weekday':0},'STU001','student',now)
        self.assertIsNone(result['days'][0]['count'])
        self.assertEqual(result['days'][0]['missing_courses'][0]['course_id'],'CS301')

    def test_weekday_planner_examples_and_context(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,10,9,12,0,tzinfo=ZoneInfo('Asia/Kolkata'))
        history=[]
        cases=[('How many classes do I have on Mondays?',{'mode':'weekday','weekday':0},'2026-10-12'),('Monday classes',{'mode':'weekday','weekday':0},'2026-10-12'),('And Tuesday?',{'mode':'weekday','weekday':1},'2026-10-13'),('Monday this week instead',{'mode':'day','date':'2026-10-05'},'2026-10-05'),('And Tuesday of that week?',{'mode':'day','date':'2026-10-06'},'2026-10-06')]
        for question,request,expected in cases:
            def planner(prompt,payload):
                if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college'}
                self.assertEqual(prompt,SUPERVISOR_AGENT_PROMPT)
                self.assertEqual(payload['chat_history'],history)
                self.assertIn('on Mondays',prompt)
                return {'route':'sql','timetable_request':request}
            state=self.state(query=question);state['chat_history']=history.copy()
            with patch('agents.supervisor_agent.college_now',return_value=now),patch.object(llm,'invoke_json',side_effect=planner),patch.object(llm,'invoke_text',side_effect=RuntimeError('offline')):
                result=build_graph().invoke(state)
            self.assertEqual(result['sql_result']['start_date'],expected)
            self.assertIn(expected,result['final_response'])
            self.assertNotIn('confirmation',result)
            history += [{'role':'user','content':question},{'role':'assistant','content':result['final_response']}]

    def test_timetable_daily_weekly_scopes_and_counts(self):
        from tools.timetable import read_timetable
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,10,9,12,0,tzinfo=ZoneInfo('Asia/Kolkata'))
        before=fetch_all('SELECT * FROM course_timetable')
        for user,role in [('STU001','student'),('FAC001','faculty')]:
            day=read_timetable({'mode':'day','date':'today'},user,role,now)
            self.assertEqual(day['days'][0]['count'],1)
            self.assertEqual(day['days'][0]['sessions'][0]['course_id'],'CS302')
            self.assertTrue(day['days'][0]['sessions'][0]['faculty_name'])
            week=read_timetable({'mode':'week','date':'today'},user,role,now)
            self.assertEqual(week['start_date'],'2026-10-05')
            self.assertEqual(week['end_date'],'2026-10-11')
            self.assertEqual(sum(d['count'] for d in week['days']),8)
            self.assertEqual({r['course_id'] for d in week['days'] for r in d['sessions']},{'CS301','CS302','CS303','CS304'})
            weekend=read_timetable({'mode':'day','date':'tomorrow'},user,role,now)
            self.assertEqual(weekend['days'][0]['count'],0)
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE courses SET faculty_id='FAC002' WHERE course_id='CS302'")
            conn.execute("INSERT INTO course_timetable(course_id,weekday,start_time,end_time,effective_from,effective_to,is_demo) VALUES('CS301',0,'14:00','15:00','2026-01-01','2026-12-31',1)")
        day=read_timetable({'mode':'day','date':'today'},'FAC001','faculty',now)
        self.assertEqual(day['days'][0]['count'],0)
        monday=read_timetable({'mode':'day','date':'2026-10-12'},'STU001','student',now)
        self.assertEqual(monday['days'][0]['count'],3)
        self.assertEqual([r['start_time'] for r in monday['days'][0]['sessions']],['09:00','11:00','14:00'])
        self.assertEqual(len(before),56)

    def test_timetable_next_ongoing_and_timezone_boundaries(self):
        from tools.timetable import read_timetable
        from datetime import datetime,timezone
        from zoneinfo import ZoneInfo
        zone=ZoneInfo('Asia/Kolkata')
        result=read_timetable({'mode':'next'},'STU001','student',datetime(2026,10,12,9,30,tzinfo=zone))
        self.assertEqual(result['ongoing'][0]['course_id'],'CS301')
        self.assertEqual(result['next_sessions'][0]['course_id'],'CS303')
        boundary=read_timetable({'mode':'next'},'STU001','student',datetime(2026,10,12,10,0,tzinfo=zone))
        self.assertEqual(boundary['ongoing'],[])
        self.assertEqual(boundary['end_date'],'2026-10-18')
        midnight=read_timetable({'mode':'day','date':'today'},'STU001','student',datetime(2026,10,9,18,31,tzinfo=timezone.utc))
        self.assertEqual(midnight['start_date'],'2026-10-10')
        self.assertEqual(midnight['days'][0]['count'],0)

    def test_timetable_unknown_coverage_validation_and_isolation(self):
        from tools.timetable import read_timetable,format_timetable,validate_timetable_request
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,10,9,tzinfo=ZoneInfo('Asia/Kolkata'))
        with sqlite3.connect(self.database) as conn:
            conn.execute("DELETE FROM course_timetable WHERE course_id='CS301'")
        result=read_timetable({'mode':'day','date':'2026-10-12'},'STU001','student',now)
        self.assertIsNone(result['days'][0]['count'])
        self.assertNotIn('No classes scheduled',format_timetable(result))
        self.assertIn('CS301',format_timetable(result))
        expired=read_timetable({'mode':'range','start_date':'2026-12-31','end_date':'2027-01-01'},'STU001','student',now)
        self.assertEqual(len(expired['days'][1]['missing_courses']),4)
        unknown=read_timetable({'mode':'next'},'STU001','student',datetime(2027,1,1,tzinfo=ZoneInfo('Asia/Kolkata')))
        self.assertIn('seven-calendar-day window',format_timetable(unknown))
        self.assertIn('unavailable',format_timetable(unknown))
        for req in [{'mode':'day','date':'today','user_id':'STU002'}, {'mode':'next','sql':'SELECT 1'}, {'mode':'range','start_date':'2026-10-11','end_date':'2026-10-09'}, {'mode':'day','date':'not a date'}, {'mode':[]}]:
            with self.assertRaises(OperationError):validate_timetable_request(req,now)
        with self.assertRaises(PermissionError):read_timetable({'mode':'next'},'FAC001','student',now)

    def test_supervisor_timetable_bypasses_generated_sql(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,10,9,12,0,tzinfo=ZoneInfo('Asia/Kolkata'))
        def planner(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college'}
            if prompt==SUPERVISOR_AGENT_PROMPT:return {'route':'sql','timetable_request':{'mode':'day','date':'today'}}
            raise AssertionError('No SQL generation or LLM validation needed for a trusted timetable read')
        with patch('agents.supervisor_agent.college_now',return_value=now),patch.object(llm,'invoke_json',side_effect=planner),patch.object(llm,'invoke_text',side_effect=RuntimeError('offline')):
            result=build_graph().invoke(self.state(query='How many classes today?'))
        self.assertEqual(result['sql_result']['days'][0]['count'],1)
        self.assertNotIn('sql_query_agent',result['agent_trace'])
        self.assertIn('CS302',result['final_response'])
        # An otherwise plausible answer missing dates or faculty is rendered factually.
        from agents.supervisor_agent import supervisor_aggregate_node
        result.pop('final_response')
        with patch.object(llm,'invoke_text',return_value='You have 1 class today: Database Systems from 10:00 to 11:00.'):
            answer=supervisor_aggregate_node(result)['final_response']
        self.assertIn('2026-10-09',answer)
        self.assertIn('Faculty',answer)

    def test_timetable_graph_followups_grounding_and_fallback(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,10,9,12,0,tzinfo=ZoneInfo('Asia/Kolkata'))
        history=[]
        before=fetch_all('SELECT * FROM course_timetable')
        for question,req in [('How many classes do I have today?',{'mode':'day','date':'today'}),('What are the timings?',{'mode':'day','date':'today'}),('What about tomorrow?',{'mode':'day','date':'tomorrow'}),('Show this week',{'mode':'week','date':'today'}),('Which class is next?',{'mode':'next'})]:
            state=self.state(query=question);state['chat_history']=history.copy();state['current_date']='2026-10-09'
            model,_=self.model([{'timetable_request':req}])
            with patch('agents.supervisor_agent.college_now',return_value=now),patch.object(llm,'invoke_json',side_effect=model) as calls,patch.object(llm,'invoke_text',side_effect=RuntimeError('offline')) as answer:
                result=build_graph().invoke(state)
            self.assertIn('Timetable:',result['final_response'])
            self.assertEqual(result['actions'],[])
            self.assertNotIn('confirmation',result)
            payload=next(c.args[1] for c in calls.call_args_list if c.args[0]==SQL_QUERY_AGENT_PROMPT)
            self.assertEqual(payload['chat_history'],history)
            self.assertNotIn('chat_history',answer.call_args.args[1])
            self.assertEqual(answer.call_args.args[1]['timetable']['kind'],'timetable')
            history += [{'role':'user','content':question},{'role':'assistant','content':result['final_response']}]
        self.assertEqual(before,fetch_all('SELECT * FROM course_timetable'))

    def projection_fixture(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute("DELETE FROM course_timetable WHERE course_id LIKE 'CS%'")
            for course,attended,total in [('CS301',10,20),('CS302',90,100),('CS303',0,10),('CS304',40,50)]:
                conn.execute('UPDATE attendance SET attended_classes=?,total_classes=? WHERE student_id=? AND course_id=?',(attended,total,'STU001',course))
                conn.execute("INSERT INTO course_timetable(course_id,weekday,start_time,end_time,effective_from,effective_to,is_demo) VALUES(?,0,'09:00','10:00','2026-01-01','2026-12-31',1)",(course,))

    def test_range_projection_weighted_overall_and_weekend(self):
        self.projection_fixture()
        before=fetch_all('SELECT * FROM attendance')
        leaves=fetch_all('SELECT * FROM leave_requests')
        result=project_attendance('STU001','student',{'start_date':'2026-10-10','end_date':'2026-10-12'})
        self.assertEqual(len(result['courses']),4)
        self.assertEqual(result['overall']['attended_classes'],140)
        self.assertEqual(result['overall']['total_classes'],180)
        self.assertEqual(result['overall']['missed_classes'],4)
        self.assertEqual(result['overall']['current_percentage'],round(140/180*100,2))
        self.assertEqual(result['overall']['projected_percentage'],round(140/184*100,2))
        self.assertEqual(next(r for r in result['courses'] if r['course_id']=='CS303')['projected_percentage'],0)
        weekend=project_attendance('STU001','student',{'start_date':'2026-10-10','end_date':'2026-10-11'})
        self.assertEqual(weekend['overall']['missed_classes'],0)
        self.assertEqual(weekend['overall']['current_percentage'],weekend['overall']['projected_percentage'])
        self.assertEqual(before,fetch_all('SELECT * FROM attendance'))
        self.assertEqual(leaves,fetch_all('SELECT * FROM leave_requests'))

    def test_projection_missing_and_invalid_counts(self):
        self.projection_fixture()
        with sqlite3.connect(self.database) as conn:
            conn.execute("DELETE FROM attendance WHERE student_id='STU001' AND course_id='CS301'")
        result=project_attendance('STU001','student','2026-10-12')
        self.assertEqual(len(result['courses']),4)
        self.assertIsNone(result['overall']['projected_percentage'])
        with sqlite3.connect(self.database) as conn:
            conn.execute("INSERT INTO attendance(student_id,course_id,attended_classes,total_classes) VALUES('STU001','CS301',10,20)")
        for attended,total in [(None,20),(10,None),(-1,20),(21,20),(0,0)]:
            with sqlite3.connect(self.database) as conn:
                conn.execute("UPDATE attendance SET attended_classes=?,total_classes=? WHERE student_id='STU001' AND course_id='CS301'",(attended,total))
            result=project_attendance('STU001','student','2026-10-12')
            self.assertIsNone(result['overall']['current_percentage'])
            self.assertIsNone(result['overall']['projected_percentage'])

    def test_projection_timetable_coverage_and_multiple_sessions(self):
        self.projection_fixture()
        with sqlite3.connect(self.database) as conn:
            conn.execute("INSERT INTO course_timetable(course_id,weekday,start_time,end_time,effective_from,effective_to,is_demo) VALUES('CS301',0,'11:00','12:00','2026-10-12','2026-10-12',1)")
        result=project_attendance('STU001','student',{'start_date':'2026-10-10','end_date':'2026-10-12'})
        self.assertEqual(result['overall']['missed_classes'],5)
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE course_timetable SET effective_to='2026-10-11' WHERE course_id='CS302'")
        result=project_attendance('STU001','student',{'start_date':'2026-10-10','end_date':'2026-10-12'})
        self.assertIsNotNone(result['overall']['current_percentage'])
        self.assertIsNone(result['overall']['projected_percentage'])
        self.assertIsNone(result['overall']['missed_classes'])
        row=next(r for r in result['courses'] if r['course_id']=='CS302')
        self.assertEqual(row['missing_schedule_dates'],['2026-10-12'])

    def test_projection_request_validation_and_permissions(self):
        from tools.operations import validate_projection_request
        for request in [{'start_date':'2026-10-12','end_date':'2026-10-10'}, {'start_date':'2026-09-29','end_date':'2026-10-01'}, {'start_date':'2026-10-01','end_date':'2028-01-01'}, {'start_date':'2026-10-01','end_date':'2026-10-03','user_id':'STU002'}, {'start_date':'2026-10-01','end_date':'2026-10-03','include_courses':'yes'}]:
            with self.assertRaises(OperationError):validate_projection_request(request)
        with self.assertRaises(PermissionError):project_attendance('FAC001','faculty','2026-10-12')
        with self.assertRaises(PermissionError):project_attendance('FAC001','student','2026-10-12')

    def test_projection_unavailable_wording_and_policy_preserves_calculation(self):
        from agents.supervisor_agent import supervisor_aggregate_node, _projection_text
        self.projection_fixture()
        request={'start_date':'2026-10-10','end_date':'2026-10-12'}
        state=self.state(); state.update(route='both',projection_request=request,sql_result=project_attendance('STU001','student',request),rag_context='Attendance policy')
        with patch.object(llm,'invoke_text',return_value='Approved leave does not automatically change attendance.'):
            answer=supervisor_aggregate_node(state)['final_response']
        self.assertIn('76.09%',answer)
        self.assertIn('Approved leave',answer)
        with sqlite3.connect(self.database) as conn:
            conn.execute("DELETE FROM attendance WHERE student_id='STU001' AND course_id='CS301'")
        answer=_projection_text(project_attendance('STU001','student',request))
        self.assertIn('Projected overall attendance: unavailable.',answer)
        self.assertNotIn('0.00%',answer)

    def test_multiday_graph_grounded_response_and_followup(self):
        self.projection_fixture()
        history=[]
        for question,request in [('What will my overall attendance be if absent for the next 3 consecutive dates?', {'start_date':'2026-10-10','end_date':'2026-10-12','include_courses':False}), ('Show each subject too, but only for Monday.', {'start_date':'2026-10-12','end_date':'2026-10-12','include_courses':True})]:
            model,_=self.model([{'projection_request':request}])
            state=self.state(query=question);state['current_date']='2026-10-09';state['chat_history']=history.copy()
            with patch.object(llm,'invoke_json',side_effect=model) as calls, patch.object(llm,'invoke_text',side_effect=AssertionError('Projection arithmetic must not use answer LLM')):
                result=build_graph().invoke(state)
            self.assertIn('76.09%',result['final_response'])
            self.assertNotIn('query result',result['final_response'])
            self.assertNotIn('confirmation',result)
            self.assertEqual(result['actions'],[])
            payload=next(c.args[1] for c in calls.call_args_list if c.args[0]==SQL_QUERY_AGENT_PROMPT)
            self.assertEqual(payload['chat_history'],history)
            history += [{'role':'user','content':question},{'role':'assistant','content':result['final_response']}]

    def test_attendance_projection_and_missing_schedule(self):
        before = fetch_all("SELECT * FROM attendance")
        result = project_attendance("STU001", "student", "2026-10-01")
        database = next(row for row in result["courses"] if row["course_id"] == "CS301")
        self.assertEqual(database["missed_classes"], 1)
        self.assertEqual(database["projected_percentage"], round(37 / 41 * 100, 2))
        self.assertEqual(database["demo_schedule"], 1)
        self.assertEqual(before, fetch_all("SELECT * FROM attendance"))
        missing = project_attendance("STU001", "student", "2027-01-01")
        self.assertTrue(all(row["projected_percentage"] is None for row in missing["courses"]))

    def test_migration_repeatable_preserves_existing_records_and_conflicts(self):
        self.assertIsNone(migrate(self.database))
        before = fetch_all("SELECT * FROM students")
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE results SET marks=55 WHERE student_id='STU001' AND course_id='CS201'")
        counts = [fetch_one(f"SELECT COUNT(*) n FROM {table}")["n"] for table in ("results", "course_timetable", "course_registrations")]
        messages = seed_demo(self.database, 2026)
        self.assertTrue(any("conflicting" in message for message in messages))
        self.assertEqual(before, fetch_all("SELECT * FROM students"))
        self.assertEqual(counts, [fetch_one(f"SELECT COUNT(*) n FROM {table}")["n"] for table in ("results", "course_timetable", "course_registrations")])
        self.assertEqual(fetch_one("SELECT marks FROM results WHERE student_id='STU001' AND course_id='CS201'")["marks"], 55)
        self.assertTrue(list(self.database.parent.glob("*.bak")))

    def test_pdf_example_data_available(self):
        self.assertTrue(execute_query("SELECT * FROM course_registrations WHERE status='pending'", "STU001", "student"))
        self.assertTrue(execute_query("SELECT s.name FROM students s JOIN course_registrations cr ON cr.student_id=s.student_id JOIN courses c ON c.course_id=cr.course_id WHERE c.course_name='Artificial Intelligence' AND cr.status='pending'", "FAC001", "faculty"))
        self.assertTrue(execute_query("SELECT r.* FROM results r JOIN courses c ON c.course_id=r.course_id WHERE c.course_name='Data Structures'", "FAC001", "faculty"))

    def test_hostel_inventory_filters_and_prices(self):
        for user, role in [('STU001', 'student'), ('FAC001', 'faculty')]:
            self.assertEqual(len(execute_query('SELECT * FROM hostel_rooms', user, role)), 18)
            for sharing in (2, 3, 4):
                for ac in (0, 1):
                    rows = execute_query(f'SELECT *, sharing_capacity-occupied_beds AS available_beds FROM hostel_rooms WHERE sharing_capacity={sharing} AND has_ac={ac} AND occupied_beds < sharing_capacity ORDER BY room_id', user, role)
                    self.assertEqual(len(rows), 2)
                    self.assertEqual([r['available_beds'] for r in rows], [1, sharing])
                    self.assertEqual({r['monthly_room_fee'] for r in rows}, {8000 - sharing * 1000 + ac * 2000})
        fees = execute_query('SELECT * FROM mess_plans ORDER BY monthly_mess_fee', 'STU001', 'student')
        self.assertEqual([r['monthly_mess_fee'] for r in fees], [3000, 4000])
        total = execute_query("SELECT monthly_room_fee,monthly_mess_fee,monthly_room_fee+monthly_mess_fee AS total FROM hostel_rooms CROSS JOIN mess_plans WHERE room_id='H2A-2' AND plan_id='veg'", 'STU001', 'student')[0]
        self.assertEqual(total, {'monthly_room_fee':8000,'monthly_mess_fee':3000,'total':11000})
        with sqlite3.connect(self.database) as conn:
            conn.execute('UPDATE hostel_rooms SET occupied_beds=sharing_capacity WHERE sharing_capacity=2 AND has_ac=1')
        self.assertEqual(execute_query('SELECT * FROM hostel_rooms WHERE sharing_capacity=2 AND has_ac=1 AND occupied_beds<sharing_capacity', 'STU001', 'student'), [])
        with self.assertRaises(PermissionError):
            execute_query('DELETE FROM hostel_rooms', 'STU001', 'student')

    def test_all_weekly_menus_and_constraints(self):
        rows = execute_query('SELECT * FROM mess_menus', 'STU001', 'student')
        self.assertEqual(len(rows), 14)
        for plan in ('veg', 'non_veg'):
            for day in range(7):
                row = next(r for r in rows if r['plan_id']==plan and r['weekday']==day)
                for meal in ('breakfast','lunch','evening_snacks','dinner'):
                    self.assertTrue(row[meal])
                    if plan == 'veg' or meal == 'evening_snacks':
                        self.assertNotRegex(row[meal].lower(), r'\b(egg|chicken|fish|meat|omelette)\b')
                    else:
                        self.assertRegex(row[meal].lower(), r'\b(egg|chicken|fish|omelette)\b')
        self.assertEqual(next(r['dinner'] for r in rows if r['plan_id']=='veg' and r['weekday']==1), 'Chapati, dal tadka, mixed vegetable curry, rice, and curd')
        for sql in ["UPDATE hostel_rooms SET occupied_beds=5", "UPDATE hostel_rooms SET monthly_room_fee=-1", "UPDATE hostel_rooms SET sharing_capacity=1", "UPDATE hostel_rooms SET has_ac=2", "UPDATE mess_menus SET weekday=7", "UPDATE mess_menus SET breakfast=' '", "UPDATE mess_menus SET plan_id='missing'", "UPDATE mess_plans SET monthly_mess_fee=-1"]:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError), sqlite3.connect(self.database) as conn:
                conn.execute('PRAGMA foreign_keys=ON')
                conn.execute(sql)

    def test_hostel_migration_preserves_data_and_backups(self):
        accounts = fetch_all('SELECT * FROM users')
        bookings = fetch_all('SELECT * FROM classroom_bookings')
        with sqlite3.connect(self.database) as conn:
            conn.execute('DELETE FROM schema_migrations WHERE version=7')
            conn.execute("UPDATE hostel_rooms SET monthly_room_fee=9000,occupied_beds=1 WHERE room_id='H2A-1'")
            conn.execute("UPDATE mess_menus SET dinner='Custom meal' WHERE plan_id='veg' AND weekday=1")
            conn.execute("DELETE FROM hostel_rooms WHERE room_id='H4N-3'")
        saved = migrate(self.database)
        self.assertTrue(Path(saved).exists())
        with sqlite3.connect(saved) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM hostel_rooms').fetchone()[0], 17)
        self.assertEqual(fetch_one("SELECT monthly_room_fee,occupied_beds FROM hostel_rooms WHERE room_id='H2A-1'"), {'monthly_room_fee':9000,'occupied_beds':1})
        self.assertEqual(fetch_one("SELECT dinner FROM mess_menus WHERE plan_id='veg' AND weekday=1")['dinner'], 'Custom meal')
        self.assertEqual(fetch_one('SELECT COUNT(*) n FROM hostel_rooms')['n'], 18)
        self.assertIsNone(migrate(self.database))
        self.assertEqual(accounts, fetch_all('SELECT * FROM users'))
        self.assertEqual(bookings, fetch_all('SELECT * FROM classroom_bookings'))
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_daily_menu_migration_preserves_missing_and_custom_dishes(self):
        original = fetch_all('SELECT * FROM mess_menus ORDER BY plan_id,weekday')
        with sqlite3.connect(self.database) as conn:
            conn.execute('DELETE FROM schema_migrations WHERE version=9')
            conn.execute('DROP TABLE mess_menus')
            conn.execute("""CREATE TABLE mess_menus(
                plan_id TEXT NOT NULL REFERENCES mess_plans(plan_id),
                weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
                meal TEXT NOT NULL CHECK(meal IN ('breakfast','lunch','evening_snacks','dinner')),
                items TEXT NOT NULL, PRIMARY KEY(plan_id,weekday,meal))""")
            for row in original:
                for meal in ('breakfast','lunch','evening_snacks','dinner'):
                    conn.execute('INSERT INTO mess_menus VALUES(?,?,?,?)', (row['plan_id'],row['weekday'],meal,row[meal]))
            conn.execute("UPDATE mess_menus SET items='Custom dinner' WHERE plan_id='veg' AND weekday=1 AND meal='dinner'")
            conn.execute("DELETE FROM mess_menus WHERE plan_id='veg' AND weekday=2 AND meal='lunch'")
            conn.execute("DELETE FROM mess_menus WHERE plan_id='non_veg' AND weekday=6")
        backup = migrate(self.database)
        self.assertTrue(Path(backup).exists())
        with sqlite3.connect(backup) as conn:
            self.assertIn('meal', [r[1] for r in conn.execute('PRAGMA table_info(mess_menus)')])
        for row in original:
            if row['plan_id']=='veg' and row['weekday']==1:
                row['dinner']='Custom dinner'
            if row['plan_id']=='veg' and row['weekday']==2:
                row['lunch']=None
            if row['plan_id']=='non_veg' and row['weekday']==6:
                row.update({key:None for key in ('breakfast','lunch','evening_snacks','dinner')})
        self.assertEqual(fetch_all('SELECT * FROM mess_menus ORDER BY plan_id,weekday'), original)
        self.assertIsNone(migrate(self.database))
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def menu_model(self, request, route='menu'):
        def invoke(prompt, payload):
            if prompt == GUARDRAIL_AGENT_PROMPT:
                return {'verdict':'allow','topic_scope':'college'}
            if prompt == SUPERVISOR_AGENT_PROMPT:
                return {'route':route,'menu_request':request, 'scoped_request':'Show my attendance'}
            if prompt == SQL_QUERY_AGENT_PROMPT:
                return {'sql':'SELECT * FROM attendance'}
            if prompt == VALIDATOR_AGENT_PROMPT:
                return {'verdict':'valid'}
            raise AssertionError('Unexpected model call')
        return invoke

    def test_llm_menu_request_receives_context_and_returns_only_requested_data(self):
        from agents.prompts import MENU_ANSWER_PROMPT
        for query in ['What are they serving Saturdays?', 'non veeg dinner please', 'Non-veg instead', 'Only dinner', 'What is dinner tomorrow?']:
            state=self.state(query=query)
            state['chat_history']=[{'role':'user','content':'Show veg dinner on Saturday'}, {'role':'assistant','content':'Old hallucinated lobster menu'}]
            request={'plans':['non_veg'],'weekdays':[5],'meals':['dinner']}
            with patch.object(llm,'invoke_json',side_effect=self.menu_model(request)) as interpret, patch.object(llm,'invoke_text',return_value='Saturday non-veg dinner: Chapati, egg curry, rice, salad.') as answer:
                result=build_graph().invoke(state)
            supervisor=[c.args[1] for c in interpret.call_args_list if c.args[0]==SUPERVISOR_AGENT_PROMPT][0]
            self.assertEqual(supervisor['chat_history'],state['chat_history'])
            self.assertEqual(supervisor['current_date'],state['current_date'])
            self.assertEqual(supervisor['timezone'],'Asia/Kolkata')
            self.assertIn('menu_catalog',supervisor)
            self.assertEqual(answer.call_args.args[0],MENU_ANSWER_PROMPT)
            evidence=answer.call_args.args[1]
            self.assertNotIn('chat_history',evidence)
            self.assertEqual(evidence['rows'],[{'plan_id':'non_veg','weekday':5,'dinner':'Chapati, egg curry, rice, salad'}])
            self.assertIn('menu_tool',result['agent_trace'])
            self.assertNotIn('sql_query_agent',result['agent_trace'])
            self.assertFalse(result['actions'])

    def test_dish_filter_matches_only_requested_meals_and_days(self):
        from tools.db_tool import read_menu
        from agents.response_formatter import format_menu_response
        request={'dish':'UPMA','meals':['breakfast']}
        for user,role in [('STU001','student'),('FAC001','faculty')]:
            evidence=read_menu(request,user,role)
            self.assertEqual(len(evidence['rows']),2)
            self.assertTrue(all(r['weekday']==3 and set(r)=={'plan_id','weekday','breakfast'} for r in evidence['rows']))
            answer=format_menu_response(evidence['rows'],evidence['request'],evidence['missing'])
            self.assertIn('Thursday',answer)
            self.assertIn('Non-veg and Veg',answer)
            self.assertNotIn('Monday',answer)
            self.assertNotIn('|',answer)
        self.assertFalse(read_menu({**request,'weekdays':[0]},'STU001','student')['rows'])
        self.assertFalse(read_menu({'dish':'up','meals':['breakfast']},'STU001','student')['rows'])
        self.assertFalse(read_menu({'dish':'upma','meals':['dinner']},'STU001','student')['rows'])
        evidence=read_menu({'dish':'egg','plans':['non_veg'],'meals':['breakfast']},'STU001','student')
        self.assertEqual([r['weekday'] for r in evidence['rows']],[0,1,3,4,6])
        self.assertFalse(read_menu({'dish':'egg','plans':['veg']},'STU001','student')['rows'])
        self.assertEqual(len(read_menu({'dish':None},'STU001','student')['rows']),14)

    def test_dish_filter_validation_literal_matching_and_unknown_coverage(self):
        from tools.db_tool import read_menu
        from agents.response_formatter import format_menu_response
        for dish in [42,True,[],{},'', ' ', 'x'*101,'upma\n','upma\x00']:
            with self.subTest(dish=dish),self.assertRaises(ValueError):
                read_menu({'dish':dish},'STU001','student')
        for dish in ["upma' OR 1=1 --",'.*','%','unlisted dish']:
            evidence=read_menu({'dish':dish},'STU001','student')
            self.assertEqual(evidence['rows'],[])
            self.assertEqual(evidence['missing'],[])
            self.assertIn('No recorded match',format_menu_response([],evidence['request']))
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE mess_menus SET breakfast=NULL WHERE plan_id='veg' AND weekday=3")
            conn.execute("DELETE FROM mess_menus WHERE plan_id='veg' AND weekday=4")
        evidence=read_menu({'dish':'upma','meals':['breakfast']},'STU001','student')
        self.assertEqual(len(evidence['rows']),1)
        self.assertEqual([m['reason'] for m in evidence['missing']],['missing_meal','missing_record'])
        answer=format_menu_response(evidence['rows'],evidence['request'],evidence['missing'])
        self.assertIn('Thursday',answer)
        self.assertIn('additional matches cannot be ruled out',answer)
        self.assertEqual(len(read_menu({},'STU001','student')['rows']),13)

    def test_dish_graph_evidence_followups_and_fallback(self):
        history=[]
        cases=[('Which day will I get upma in breakfast?',{'dish':'upma','meals':['breakfast']},2),
               ('What about dosa?',{'dish':'dosa','meals':['breakfast']},2),
               ('Non-veg only',{'dish':'dosa','meals':['breakfast'],'plans':['non_veg']},1),
               ('Show the full menu',{'dish':None},14)]
        for query,request,count in cases:
            state=self.state(query=query);state['chat_history']=list(history)
            with patch.object(llm,'invoke_json',side_effect=self.menu_model(request)) as interpret, patch.object(llm,'invoke_text',side_effect=RuntimeError('offline')) as answer:
                result=build_graph().invoke(state)
            payload=answer.call_args.args[1]
            self.assertEqual(len(payload['rows']),count)
            self.assertNotIn('chat_history',payload)
            planner=[c.args[1] for c in interpret.call_args_list if c.args[0]==SUPERVISOR_AGENT_PROMPT][0]
            self.assertEqual(planner['chat_history'],history)
            if request.get('dish'):
                self.assertNotIn('|',result['final_response'])
                self.assertNotIn('Monday',result['final_response'])
            else:
                self.assertIn('| Day |',result['final_response'])
            history += [{'role':'user','content':query},{'role':'assistant','content':result['final_response']}]

    def test_menu_validation_and_sql_bypass(self):
        from tools.db_tool import menu_catalog,validate_menu_request,read_menu,UnsupportedMenuChoice
        catalog=menu_catalog('STU001','student')
        relative=validate_menu_request({'plans':['veg'],'weekdays':[{'relative':'tomorrow'}],'meals':['dinner']},catalog,{'today':0,'tomorrow':1,'yesterday':6})
        self.assertEqual(relative,{'plans':['veg'],'weekdays':[1],'meals':['dinner']})
        with self.assertRaises(ValueError):
            validate_menu_request({'weekdays':[{'relative':'next millennium'}]},catalog,{'tomorrow':1})
        resolved=validate_menu_request({},catalog)
        self.assertEqual(len(read_menu(resolved,'STU001','student')['rows']),14)
        for request in [{'sql':'DROP TABLE users'}, {'plans':'veg'}, {'weekdays':[True]}, {'meals':[]}, {'meals':['dinner; DROP TABLE users']}, {'plans':['special']}, {'weekdays':[9]}]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_menu_request(request,catalog)
        with self.assertRaises(PermissionError):
            read_menu({},'STU001','faculty')
        with self.assertRaises(PermissionError):
            execute_query('SELECT * FROM mess_menus','STU001','student',allow_menus=False)
        with patch.object(llm,'invoke_json',side_effect=self.menu_model({'plans':['special']})), patch.object(llm,'invoke_text') as answer:
            result=build_graph().invoke(self.state(query='Special mess menu'))
        self.assertIn('Available choices',result['final_response'])
        answer.assert_not_called()

    def test_menu_interpretation_retry_and_exhaustion(self):
        for fix in (False,True):
            attempts=[]
            def model(prompt,payload):
                if prompt==GUARDRAIL_AGENT_PROMPT: return {'verdict':'allow','topic_scope':'college'}
                self.assertEqual(prompt,SUPERVISOR_AGENT_PROMPT)
                attempts.append(payload)
                if fix and len(attempts)==2: return {'route':'menu','menu_request':{'plans':['veg'],'weekdays':[1],'meals':['dinner']}}
                return {'route':'menu','menu_request':{'sql':'SELECT * FROM users'}}
            with patch.object(llm,'invoke_json',side_effect=model), patch.object(llm,'invoke_text',side_effect=RuntimeError('offline')):
                result=build_graph().invoke(self.state(query='Veg dinner Tuesday'))
            self.assertEqual(len(attempts),2 if fix else 3)
            self.assertIn('no SQL',attempts[1]['validation_feedback'])
            self.assertIn('dal tadka' if fix else "couldn't interpret",result['final_response'])

    def test_menu_missing_entries_and_query_failure(self):
        from tools.db_tool import read_menu
        request={'plans':['veg'],'weekdays':[1,2],'meals':['dinner']}
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE mess_menus SET dinner=NULL WHERE plan_id='veg' AND weekday=1")
            conn.execute("DELETE FROM mess_menus WHERE plan_id='veg' AND weekday=2")
        evidence=read_menu(request,'STU001','student')
        self.assertEqual([m['reason'] for m in evidence['missing']],['missing_meal','missing_record'])
        with patch.object(llm,'invoke_json',side_effect=self.menu_model(request)), patch.object(llm,'invoke_text',side_effect=RuntimeError('offline')):
            result=build_graph().invoke(self.state())
        self.assertEqual(result['final_response'].count('Unavailable'),2)
        with patch.object(llm,'invoke_json',side_effect=self.menu_model(request)), patch('agents.graph.read_menu',side_effect=sqlite3.OperationalError('offline')), patch.object(llm,'invoke_text') as answer:
            result=build_graph().invoke(self.state())
        self.assertIn('temporarily unavailable',result['final_response'])
        answer.assert_not_called()

    def test_mixed_menu_and_academic_request_preserves_both(self):
        from agents.prompts import MENU_ANSWER_PROMPT
        request={'plans':['veg'],'weekdays':[1],'meals':['dinner']}
        def answer(prompt,payload,**kwargs):
            if prompt==MENU_ANSWER_PROMPT:
                self.assertIn('dal tadka',payload['rows'][0]['dinner'])
                return 'Tuesday dinner: Chapati and dal tadka.'
            self.assertTrue(payload['sql_result'])
            return 'Your attendance information.'
        with patch.object(llm,'invoke_json',side_effect=self.menu_model(request,'sql')), patch.object(llm,'invoke_text',side_effect=answer):
            result=build_graph().invoke(self.state(query='Show attendance and Tuesday veg dinner'))
        self.assertIn('attendance information',result['final_response'])
        self.assertIn('Tuesday dinner',result['final_response'])

    def test_daily_menu_queries_for_both_plans_and_individual_meals(self):
        both = execute_query('SELECT * FROM mess_menus ORDER BY plan_id,weekday', 'STU001', 'student')
        self.assertEqual(len(both), 14)
        self.assertEqual([row['weekday'] for row in both[:7]], list(range(7)))
        self.assertEqual({row['plan_id'] for row in both}, {'veg','non_veg'})
        for day in range(7):
            for meal in ('breakfast','lunch','evening_snacks','dinner'):
                rows = execute_query(f'SELECT plan_id,{meal} FROM mess_menus WHERE weekday={day} ORDER BY plan_id', 'STU002', 'student')
                self.assertEqual(len(rows), 2)
                if meal != 'evening_snacks':
                    self.assertNotEqual(rows[0][meal], rows[1][meal])
        with sqlite3.connect(self.database) as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO mess_menus(plan_id,weekday) VALUES('veg',0)")

    def test_sql_execution_error_refined(self):
        model, calls = self.model([{"sql": "SELECT nonexistent FROM attendance"}, {"sql": "SELECT * FROM attendance"}])
        with patch.object(llm, "invoke_json", side_effect=model), patch.object(llm, "invoke_text", return_value="Your attendance details"):
            result = build_graph().invoke(self.state())
        self.assertEqual(calls["candidates"], 2)
        self.assertIn("nonexistent", calls["feedback"][1])
        self.assertTrue(result["sql_result"])
        self.assertEqual(result["agent_trace"][:2], ["supervisor_intake", "guardrail"])

    def test_validator_feedback_refined(self):
        model, calls = self.model([{"sql": "SELECT * FROM attendance"}] * 2, invalid_first=True)
        with patch.object(llm, "invoke_json", side_effect=model), patch.object(llm, "invoke_text", return_value="Answer"):
            build_graph().invoke(self.state())
        self.assertEqual(calls["feedback"][1], "Include course_name.")

    def test_missing_and_malformed_candidates_stop_after_three(self):
        for candidates in [[{}, {}, {}], [ValueError("malformed"), ValueError("malformed"), ValueError("malformed")]]:
            model, calls = self.model(candidates)
            with patch.object(llm, "invoke_json", side_effect=model):
                result = build_graph().invoke(self.state())
            self.assertEqual(calls["candidates"], 3)
            self.assertIn("three attempts", result["final_response"])

    def test_sql_authorization_failure_not_retried(self):
        model, calls = self.model([{"sql": "SELECT * FROM sqlite_master"}])
        with patch.object(llm, "invoke_json", side_effect=model):
            result = build_graph().invoke(self.state())
        self.assertEqual(calls["candidates"], 1)
        self.assertTrue(result["terminal"])

    def test_combined_rag_sql_and_followup_context(self):
        model, calls = self.model([{"sql": "SELECT * FROM attendance"}], route="both")
        state = self.state(query="What about my eligibility?")
        state["chat_history"] = [{"role": "user", "content": "My Machine Learning attendance"}]
        with patch.object(llm, "invoke_json", side_effect=model), patch.object(llm, "invoke_text", return_value="Attendance compared to sample policy") as answer:
            result = build_graph().invoke(state)
        evidence = answer.call_args.args[1]
        self.assertTrue(evidence["sql_result"])
        self.assertIn("75", evidence["context"])
        self.assertTrue(result["sources"])
        self.assertEqual(calls["supervisor"][0]["chat_history"], state["chat_history"])
        self.assertNotIn("fictional", result["final_response"].lower())

    def test_projection_graph_deterministic_answer(self):
        model, _ = self.model([{"projection_date": "2026-10-01"}])
        with patch.object(llm, "invoke_json", side_effect=model):
            result = build_graph().invoke(self.state(query="Attendance if I take leave tomorrow?"))
        self.assertIn("37 / (40 + 1)", result["final_response"])
        self.assertNotIn("fictional", result["final_response"].lower())

    def test_missing_operation_fields_clarify_without_write(self):
        model, calls = self.model([{"operation": {"type": "submit_leave", "from_date": "2026-10-01", "to_date": "2026-10-01"}}])
        before = fetch_one("SELECT COUNT(*) n FROM leave_requests")["n"]
        with patch.object(llm, "invoke_json", side_effect=model):
            result = build_graph().invoke(self.state())
        self.assertIn("reason", result["final_response"])
        self.assertEqual(calls["candidates"], 1)
        self.assertEqual(before, fetch_one("SELECT COUNT(*) n FROM leave_requests")["n"])

    def test_committed_write_not_retried_if_model_answer_unavailable(self):
        model, calls = self.model([{"operation": self.booking()}])
        with patch.object(llm, "invoke_json", side_effect=model), patch.object(llm, "invoke_text", side_effect=RuntimeError("offline")):
            result = build_graph().invoke(self.state("faculty"))
        self.assertEqual(calls["candidates"], 1)
        self.assertEqual(result["actions"], [])
        self.assertIn("confirm or cancel", result["final_response"])

    def test_auth_requires_token_and_uses_database_role(self):
        self.assertEqual(self.client.get("/api/leaves").status_code, 401)
        me = self.client.get("/api/auth/me", headers=self.token(role="faculty"))
        self.assertEqual(me.json["role"], "student")
        denied = self.client.patch("/api/leaves/1/review", headers=self.token(role="faculty"), json={"status": "approved"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.token("missing-user")).status_code, 401)

    def confirmation(self, role='student', session='confirm-test', operation=None):
        from tools.confirmations import propose
        user='FAC001' if role=='faculty' else 'STU001'
        append_turn(user,session,'user','Please submit this action')
        operation=operation or (self.booking() if role=='faculty' else {'type':'submit_leave','from_date':'2030-01-10','to_date':'2030-01-11','reason':'Appointment'})
        card=propose(operation,user,role,session)
        append_turn(user,session,'assistant',card['summary'],confirmation=card)
        return card

    def test_confirmation_endpoint_no_write_before_confirm_and_retries(self):
        before=fetch_one('SELECT COUNT(*) n FROM leave_requests')['n']
        card=self.confirmation()
        self.assertEqual(fetch_one('SELECT COUNT(*) n FROM leave_requests')['n'],before)
        url='/api/chat/confirmations/'+card['id']
        payload={'session_id':'confirm-test','decision':'confirm'}
        self.assertEqual(self.client.post(url,json=payload,headers=self.token('STU002')).status_code,409)
        self.assertEqual(self.client.post(url,json={**payload,'operation':{}},headers=self.token()).status_code,400)
        first=self.client.post(url,json=payload,headers=self.token())
        self.assertEqual(first.json['confirmation']['status'],'confirmed')
        again=self.client.post(url,json=payload,headers=self.token())
        self.assertEqual(first.json['response'],again.json['response'])
        self.assertEqual(fetch_one('SELECT COUNT(*) n FROM leave_requests')['n'],before+1)
        history=self.client.get('/api/chat/history',headers=self.token()).json
        messages=history['sessions'][0]['messages']
        self.assertEqual(messages[1]['confirmation']['status'],'confirmed')
        self.assertEqual(sum(m.get('confirmation_result_id')==card['id'] for m in messages),1)
        with self.assertRaises(Exception):
            execute_query('SELECT * FROM chat_confirmations','STU001','student')

    def test_confirmation_cancel_expire_replace_delete(self):
        from tools.confirmations import decide
        from tools.memory_tool import delete_session
        card=self.confirmation()
        newer=self.confirmation()
        self.assertEqual(decide(card['id'],'STU001','student','confirm-test','confirm')['confirmation']['status'],'superseded')
        self.assertEqual(decide(newer['id'],'STU001','student','confirm-test','cancel')['confirmation']['status'],'cancelled')
        self.assertEqual(decide(newer['id'],'STU001','student','confirm-test','confirm')['confirmation']['status'],'cancelled')
        expired=self.confirmation(session='expired')
        with sqlite3.connect(self.database) as conn:
            conn.execute('UPDATE chat_confirmations SET expires_at=0 WHERE confirmation_id=?',(expired['id'],))
        self.assertEqual(decide(expired['id'],'STU001','student','expired','confirm')['confirmation']['status'],'expired')
        deleted=self.confirmation(session='deleted')
        delete_session('STU001','deleted')
        with self.assertRaises(OperationError):
            decide(deleted['id'],'STU001','student','deleted','confirm')
        self.assertEqual(fetch_one('SELECT status FROM chat_confirmations WHERE confirmation_id=?',(deleted['id'],))['status'],'cancelled')

    def test_confirmation_concurrent_and_memory_retry(self):
        from tools.confirmations import decide
        card=self.confirmation(role='faculty')
        def confirm(_):
            with self.app.app_context():
                return decide(card['id'],'FAC001','faculty','confirm-test','confirm')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(confirm,range(2)))
        self.assertEqual(results[0]['response'],results[1]['response'])
        self.assertEqual(fetch_one("SELECT COUNT(*) n FROM classroom_bookings WHERE booking_date='2026-10-01'")['n'],1)
        retry=self.confirmation(session='lost-response')
        with patch('tools.memory_tool._write',side_effect=OSError('disk unavailable')), self.assertRaises(OSError):
            decide(retry['id'],'STU001','student','lost-response','confirm')
        result=decide(retry['id'],'STU001','student','lost-response','confirm')
        self.assertEqual(result['confirmation']['status'],'confirmed')
        self.assertEqual(fetch_one("SELECT COUNT(*) n FROM leave_requests WHERE from_date='2030-01-10'")['n'],1)

    def test_confirmation_revalidates_conflicts_equipment_dates(self):
        from tools.confirmations import decide
        card=self.confirmation(role='faculty')
        execute_operation(self.booking(),'FAC002','faculty')
        result=decide(card['id'],'FAC001','faculty','confirm-test','confirm')
        self.assertEqual(result['confirmation']['status'],'failed')
        self.assertIn('already booked',result['response'])
        card=self.confirmation(role='faculty',session='equipment',operation={**self.booking('16:00','17:00'),'requirements':{'has_ac':True}})
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE classrooms SET has_ac=0 WHERE classroom_id='Lab-302'")
        self.assertEqual(decide(card['id'],'FAC001','faculty','equipment','confirm')['confirmation']['status'],'failed')
        card=self.confirmation(session='past')
        with patch('tools.operations.college_today',return_value=date(2031,1,1)):
            self.assertEqual(decide(card['id'],'STU001','student','past','confirm')['confirmation']['status'],'failed')

    def test_confirmation_delete_race_and_migration_repeatability(self):
        from tools.confirmations import decide
        from tools.memory_tool import delete_session
        card=self.confirmation()
        def run(action):
            with self.app.app_context():
                try:
                    return decide(card['id'],'STU001','student','confirm-test','confirm') if action=='confirm' else delete_session('STU001','confirm-test')
                except OperationError:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run,['delete','confirm']))
        self.assertIn('confirm-test',read_memory('STU001')['deleted_sessions'])
        with self.assertRaises(OperationError):
            decide(card['id'],'STU001','student','confirm-test','confirm')
        count=fetch_one("SELECT COUNT(*) n FROM leave_requests WHERE from_date='2030-01-10'")['n']
        self.assertIn(count,[0,1])
        status=fetch_one('SELECT status FROM chat_confirmations WHERE confirmation_id=?',(card['id'],))['status']
        self.assertEqual(status,'confirmed' if count else 'cancelled')
        before=fetch_all('SELECT * FROM chat_confirmations')
        with sqlite3.connect(self.database) as conn:
            conn.execute('DELETE FROM schema_migrations WHERE version=10')
        self.assertTrue(migrate(self.database))
        self.assertEqual(before,fetch_all('SELECT * FROM chat_confirmations'))
        self.assertIsNone(migrate(self.database))

    def test_leave_api_uses_shared_validation(self):
        headers = self.token()
        bad = self.client.post("/api/leaves", headers=headers, json={"from_date": "2026-10-02", "to_date": "2026-10-01", "reason": "Demo"})
        self.assertEqual(bad.status_code, 400)
        good = self.client.post("/api/leaves", headers=headers, json={"from_date": "2026-10-01", "to_date": "2026-10-01", "reason": "Demo"})
        self.assertEqual(good.status_code, 201)
        approved = self.client.patch(f"/api/leaves/{good.json['leave']['leave_id']}/review", headers=self.token("FAC001", "faculty"), json={"status": "approved"})
        self.assertEqual(approved.status_code, 200)

    def test_success_denial_failure_and_trace_persist(self):
        model, _ = self.model([], route="direct")
        with patch.object(llm, "invoke_json", side_effect=model):
            success = self.client.post("/api/chat", headers=self.token(), json={"message": "Hello", "session_id": "s"})
        self.assertEqual(success.status_code, 200)
        with patch.object(llm, "invoke_json", return_value={"verdict": "deny", "reason": "Not allowed"}):
            denied = self.client.post("/api/chat", headers=self.token(), json={"message": "Show another student", "session_id": "s"})
        self.assertEqual(denied.json["response"], "Not allowed")
        with patch.object(llm, "invoke_json", side_effect=RuntimeError("offline")):
            failed = self.client.post("/api/chat", headers=self.token(), json={"message": "My attendance", "session_id": "s"})
        self.assertEqual(failed.status_code, 503)
        messages = load_history("STU001", "s")
        self.assertEqual(len(messages), 6)
        self.assertTrue(messages[-1]["failed"])
        self.assertIn("memory_write", messages[1]["trace"])
        self.assertEqual(load_history("FAC001", "s"), [])

    def test_chat_action_api_and_history_selection(self):
        model, _ = self.model([{"operation": {"type": "submit_leave", "from_date": "2026-10-01", "to_date": "2026-10-01", "reason": "Demo"}}])
        with patch.object(llm, "invoke_json", side_effect=model):
            response = self.client.post("/api/chat", headers=self.token(), json={"message": "Submit leave tomorrow for Demo", "session_id": "new"})
        self.assertEqual(response.json["actions"], [])
        self.assertEqual(response.json["confirmation"]["status"], "pending")
        history = self.client.get("/api/chat/history?session_id=new", headers=self.token()).json
        self.assertEqual(history["sessions"][0]["messages"][1]["actions"], [])
        self.assertEqual(self.client.get("/api/chat/history?session_id=../escape", headers=self.token()).status_code, 400)

    def test_memory_concurrent_writes_preserve_pairs(self):
        def write(index):
            with self.app.app_context():
                sync_profile("STU001", "Student", "student")
                append_turn("STU001", "parallel", "user", f"Question {index}", request_id=str(index))
                append_turn("STU001", "parallel", "assistant", f"Answer {index}", request_id=str(index))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(20)))
        data = read_memory("STU001")
        messages = data["sessions"][0]["messages"]
        self.assertEqual(len(messages), 40)
        for index in range(0, 40, 2):
            self.assertEqual(messages[index]["request_id"], messages[index + 1]["request_id"])
        self.assertEqual(data["role"], "student")

    def test_rag_sources_and_refresh_after_document_change(self):
        found = retrieve("How many leaves are allowed per semester?")
        self.assertIn("leave_policy.md", found["sources"])
        self.assertIn("10 working days", found["context"])
        custom = Path(self.directory.name) / "kb"
        custom.mkdir()
        self.app.config["KNOWLEDGE_BASE_DIR"] = str(custom)
        document = custom / "sample.md"
        document.write_text("Policy zirconium alpha", encoding="utf-8")
        self.assertIn("alpha", retrieve("zirconium")["context"])
        document.write_text("Policy zirconium beta", encoding="utf-8")
        self.assertIn("beta", retrieve("zirconium")["context"])

    def test_json_agent_rejects_non_object(self):
        with patch.object(llm, "invoke_text", return_value="[]"), self.assertRaises(ValueError):
            llm.invoke_json("test", {})

    def test_rag_vector_index_refreshes_and_removes_stale_chunks(self):
        custom = Path(self.directory.name) / "vector_kb"
        custom.mkdir()
        first, second = custom / "first.md", custom / "second.md"
        first.write_text("demo policy alpha", encoding="utf-8")
        second.write_text("demo policy old", encoding="utf-8")
        self.app.config["KNOWLEDGE_BASE_DIR"] = str(custom)
        records, updates = {}, []

        class Collection:
            metadata = None
            def get(self): return {"ids": list(records)}
            def count(self): return len(records)
            def delete(self, ids):
                for key in ids: records.pop(key)
            def upsert(self, ids, documents, metadatas, embeddings):
                updates.append(list(documents))
                records.update({key: (doc, meta) for key, doc, meta in zip(ids, documents, metadatas)})
            def modify(self, metadata): self.metadata = metadata
            def query(self, **kwargs):
                return {"documents": [[r[0] for r in records.values()]], "metadatas": [[r[1] for r in records.values()]]}

        collection = Collection()
        model = SimpleNamespace(encode=lambda docs, **kwargs: SimpleNamespace(tolist=lambda: [[1.0] for _ in docs]))
        fake_chroma = SimpleNamespace(PersistentClient=lambda **kwargs: SimpleNamespace(get_or_create_collection=lambda *args, **kwargs: collection))
        fake_transformers = SimpleNamespace(SentenceTransformer=lambda *args, **kwargs: model)
        with patch.dict(os.environ, {"RAG_USE_CHROMA": "1", "ALLOW_MODEL_DOWNLOAD": "0"}), patch.dict(sys.modules, {"chromadb": fake_chroma, "sentence_transformers": fake_transformers}):
            self.assertIn("alpha", retrieve("demo policy")["context"])
            first.write_text("demo policy beta", encoding="utf-8")
            second.unlink()
            result = retrieve("demo policy")
        self.assertIn("beta", result["context"])
        self.assertNotIn("old", result["context"])
        self.assertEqual(result["sources"], ["first.md"])
        self.assertEqual(len(updates), 2)

    def test_rate_limit_is_stored_with_compatible_error_response(self):
        import httpx
        from groq import RateLimitError
        exception = RateLimitError("quota", response=httpx.Response(429, request=httpx.Request("POST", "https://example.test")), body=None)
        with patch.object(llm, "invoke_json", side_effect=exception):
            result = self.client.post("/api/chat", headers=self.token(), json={"message": "My attendance", "session_id": "rate"})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.json["response"], result.json["error"])
        self.assertEqual(len(load_history("STU001", "rate")), 2)

    def test_app_refuses_placeholder_signing_key(self):
        from app import create_app
        with self.assertRaisesRegex(RuntimeError, "JWT_SECRET_KEY"):
            create_app({"JWT_SECRET_KEY": "replace_with_a_random_secret_at_least_32_bytes_long"})

    def test_concurrent_registration_only_one_commits(self):
        def register(_):
            with self.app.app_context():
                try:
                    execute_operation({"type": "register_course", "course_id": "MA001"}, "STU001", "student")
                    return True
                except OperationError:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_malformed_validator_output_retries(self):
        model, calls = self.model([{"sql": "SELECT * FROM attendance"}] * 2)
        first = [True]
        def invalid_validator(prompt, payload):
            if prompt == VALIDATOR_AGENT_PROMPT and first[0]:
                first[0] = False
                raise ValueError("Invalid JSON")
            return model(prompt, payload)
        with patch.object(llm, "invoke_json", side_effect=invalid_validator), patch.object(llm, "invoke_text", return_value="Attendance"):
            result = build_graph().invoke(self.state())
        self.assertEqual(calls["candidates"], 2)
        self.assertEqual(result["validation_verdict"], "valid")

    def test_formatter_fallback_preserves_generic_details(self):
        from agents.response_formatter import format_sql_response
        answer = format_sql_response([{"name": "Faculty A", "email": "a@example.test"}, {"name": "Faculty B", "email": "b@example.test"}])
        self.assertIn("a@example.test", answer)
        self.assertIn("b@example.test", answer)


if __name__ == "__main__":
    unittest.main()
