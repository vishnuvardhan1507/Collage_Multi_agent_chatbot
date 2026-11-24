import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['RAG_USE_CHROMA']='0'
from flask import Flask
from flask_jwt_extended import JWTManager,create_access_token
from db.init_db import init_db
from db.migrate import migrate
from tools.db_tool import get_connection,execute_query
from tools.hostel import handle,validate_request
from tools.confirmations import propose,decide,hydrate
from tools.memory_tool import append_turn,load_history,delete_session
from tools.operations import OperationError,execute_operation
from routes.chat_routes import chat_bp


class HostelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=tempfile.TemporaryDirectory();cls.fixture=Path(cls.base.name)/'fixture.db';init_db(cls.fixture)
    @classmethod
    def tearDownClass(cls):cls.base.cleanup()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'test.db';shutil.copyfile(self.fixture,self.db)
        self.app=Flask(__name__);self.app.config.update(TESTING=True,DATABASE_PATH=str(self.db),CHAT_MEMORY_DIR=str(Path(self.tmp.name)/'memory'),JWT_SECRET_KEY='isolated-hostel-test-secret-long-enough')
        JWTManager(self.app);self.app.register_blueprint(chat_bp)
        ctx=self.app.app_context();ctx.push();self.addCleanup(ctx.pop)
        self.client=self.app.test_client()
        self.headers={'Authorization':'Bearer '+create_access_token(identity='STU001',additional_claims={'ver':0})}
        for uid in ('STU001','STU002'):append_turn(uid,'s','user','Hostel booking')
        self.room=self.query('SELECT * FROM hostel_rooms WHERE sharing_capacity=2 AND has_ac=1 AND occupied_beds=1')[0]
    def query(self,sql,params=()):
        with closing(get_connection()) as c:return [dict(r) for r in c.execute(sql,params)]
    def write(self,sql,params=()):
        with closing(get_connection()) as c,c:c.execute(sql,params)
    def book(self,uid='STU001',session='s',**fields):
        return propose({'type':'book_hostel','room_id':self.room['room_id'],**fields},uid,'student',session)
    def confirm(self,p,uid='STU001',session='s',decision='confirm'):
        return decide(p['id'],uid,'student',session,decision)
    def occupancy(self):return self.query('SELECT occupied_beds FROM hostel_rooms WHERE room_id=?',(self.room['room_id'],))[0]['occupied_beds']

    def test_inventory_sql_all_categories_and_no_match(self):
        for sharing in (2,3,4):
            for ac in (1,0):
                sql=("SELECT room_id, sharing_capacity, has_ac, sharing_capacity-occupied_beds AS available_beds, "
                     f"monthly_room_fee FROM hostel_rooms WHERE sharing_capacity={sharing} AND has_ac={ac} "
                     "AND occupied_beds < sharing_capacity ORDER BY monthly_room_fee,room_id")
                for uid,role in [('STU001','student'),('FAC001','faculty')]:
                    rows=execute_query(sql,uid,role)
                    self.assertEqual(len(rows),2)
                    self.assertTrue(all(r['sharing_capacity']==sharing and r['has_ac']==ac and r['available_beds']>0 for r in rows))
        self.assertEqual(execute_query('SELECT room_id FROM hostel_rooms WHERE monthly_room_fee<1 AND occupied_beds<sharing_capacity','STU001','student'),[])

    def test_confirmation_no_write_until_clicked_and_repeat(self):
        p=self.book(preferences={'has_ac':True,'sharing_capacity':2,'max_monthly_fee':8000})
        self.assertEqual(self.occupancy(),1);self.assertEqual(self.query('SELECT * FROM hostel_bookings'),[])
        self.assertIn('8,000',p['summary']);self.assertIn('Sharing: 2',p['summary'])
        first=self.confirm(p);second=self.confirm(p)
        self.assertEqual(first['response'],second['response']);self.assertEqual(self.occupancy(),2)
        self.assertEqual(len(self.query('SELECT * FROM hostel_bookings')),1)
        self.assertEqual(first['confirmation']['status'],'confirmed')
        self.assertIn('one bed',first['response'])
        self.assertIn(self.room['room_id'],handle({'mode':'current'},'STU001','student','s')['response'])
        self.assertIn('no active',handle({'mode':'current'},'STU002','student','s')['response'])

    def test_cancel_pending_expiry_supersession_and_deleted_session(self):
        p=self.book();self.confirm(p,decision='cancel');self.assertEqual(self.occupancy(),1)
        p=self.book();self.write('UPDATE chat_confirmations SET expires_at=0 WHERE confirmation_id=?',(p['id'],))
        self.assertEqual(self.confirm(p)['confirmation']['status'],'expired')
        p=self.book();new=self.book()
        self.assertEqual(self.confirm(p)['confirmation']['status'],'superseded')
        delete_session('STU001','s')
        with self.assertRaises(OperationError):self.confirm(new)
        self.assertEqual(self.occupancy(),1)

    def test_price_characteristic_changes_full_room_and_requirements(self):
        for column,value in [('monthly_room_fee',9000),('has_ac',0),('sharing_capacity',3),('occupied_beds',2)]:
            p=self.book();self.write(f'UPDATE hostel_rooms SET {column}=? WHERE room_id=?',(value,self.room['room_id']))
            self.assertEqual(self.confirm(p)['confirmation']['status'],'failed')
            self.write(f'UPDATE hostel_rooms SET {column}=? WHERE room_id=?',(self.room[column],self.room['room_id']))
        with self.assertRaises(OperationError):self.book(preferences={'has_ac':False})
        self.assertEqual(self.query('SELECT * FROM hostel_bookings'),[])

    def test_one_active_and_cancellation_is_owned_idempotent(self):
        self.confirm(self.book())
        other=self.query('SELECT room_id FROM hostel_rooms WHERE occupied_beds=0')[0]['room_id']
        with self.assertRaises(OperationError):propose({'type':'book_hostel','room_id':other},'STU001','student','s')
        with self.assertRaises(OperationError):propose({'type':'cancel_hostel','booking_id':1},'STU002','student','s')
        cancellation=handle({'mode':'cancel'},'STU001','student','s')['confirmation']
        self.assertEqual(self.occupancy(),2)
        self.confirm(cancellation);self.confirm(cancellation)
        self.assertEqual(self.occupancy(),1)
        row=self.query('SELECT * FROM hostel_bookings')[0]
        self.assertEqual(row['status'],'cancelled');self.assertIsNotNone(row['cancelled_at'])
        self.confirm(self.book());self.assertEqual(self.occupancy(),2)

    def test_permissions_private_tables_and_unsupported_data(self):
        for mode in ('book','cancel','current'):
            with self.assertRaises(PermissionError):handle({'mode':mode,'room_id':self.room['room_id']} if mode=='book' else {'mode':mode},'FAC001','faculty','s')
        for request in [{'mode':'book','student_id':'STU002'},{'mode':'book','preferences':{'sharing_capacity':True}},
                        {'mode':'book','preferences':{'max_monthly_fee':float('nan')}},{'mode':'book','preferences':{'has_ac':1}},
                        {'mode':'cancel','booking_id':'1 OR 1=1'},{'mode':'search','sql':'DELETE FROM hostel_rooms'}]:
            with self.assertRaises(OperationError):validate_request(request)
        with self.assertRaises(Exception):execute_query('SELECT * FROM hostel_bookings','STU001','student')
        with self.assertRaises(PermissionError):execute_operation({'type':'book_hostel','room_id':self.room['room_id']},'STU001','student')
        with self.assertRaises(OperationError):propose({'type':'book_hostel','room_id':"' OR 1=1 --"},'STU001','student','s')

    def test_concurrent_last_bed_and_duplicate_confirmations(self):
        p1=self.book();p2=self.book('STU002')
        def run(pair):
            with self.app.app_context():return self.confirm(pair[0],pair[1])
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,[(p1,'STU001'),(p2,'STU002')]))
        self.assertEqual(sorted(r['confirmation']['status'] for r in results),['confirmed','failed'])
        self.assertEqual(self.occupancy(),2);self.assertEqual(len(self.query('SELECT * FROM hostel_bookings')),1)
        winner=next(pair for pair,result in zip([(p1,'STU001'),(p2,'STU002')],results) if result['confirmation']['status']=='confirmed')
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,[winner,winner]))
        self.assertTrue(all(r['confirmation']['status']=='confirmed' for r in results));self.assertEqual(self.occupancy(),2)

    def test_lost_history_write_retry_and_endpoint_isolation(self):
        p=self.book()
        import tools.memory_tool as memory
        original=memory.update_memory
        def lose_write(uid,fn):
            def wrapped(data):fn(data);raise OSError('Simulated history persistence failure')
            return original(uid,wrapped)
        with patch('tools.confirmations.update_memory',side_effect=lose_write):
            with self.assertRaises(OSError):self.confirm(p)
        self.assertEqual(self.occupancy(),2)
        other={'Authorization':'Bearer '+create_access_token(identity='STU002',additional_claims={'ver':0})}
        self.assertNotEqual(self.client.post('/api/chat/confirmations/'+p['id'],headers=other,json={'session_id':'s','decision':'confirm'}).status_code,200)
        r=self.client.post('/api/chat/confirmations/'+p['id'],headers=self.headers,json={'session_id':'s','decision':'confirm'})
        self.assertEqual(r.status_code,200);self.assertEqual(self.occupancy(),2)

    def test_migration_and_admin_stale_edit_occupancy_protection(self):
        from tools import admin_service as admin
        self.write("INSERT INTO users(user_id,username,password_hash,role) VALUES('ADMTEST','Admin','unused','admin')")
        old=next(r for r in admin.list_resource('ADMTEST','hostel_rooms') if r['room_id']==self.room['room_id'])
        self.confirm(self.book())
        with self.assertRaises(admin.Conflict):admin.save_resource('ADMTEST','hostel_rooms',{**old,'reason':'stale'})
        row=next(r for r in admin.list_resource('ADMTEST','hostel_rooms') if r['room_id']==self.room['room_id'])
        with self.assertRaises(ValueError):admin.save_resource('ADMTEST','hostel_rooms',{**row,'occupied_beds':0,'reason':'invalid'})
        with self.assertRaises(admin.Conflict):admin.delete_resource('ADMTEST','hostel_rooms',{'room_id':row['room_id'],'_revision':row['_revision'],'reason':'delete','confirmed':True})
        before=self.query('SELECT * FROM hostel_rooms');bookings=self.query('SELECT * FROM hostel_bookings')
        self.assertIsNone(migrate(self.db));self.assertEqual(before,self.query('SELECT * FROM hostel_rooms'))
        self.assertEqual(bookings,self.query('SELECT * FROM hostel_bookings'));self.assertEqual(self.query('PRAGMA foreign_key_check'),[])

    def test_llm_structured_requests_and_context_without_sql(self):
        from agents.supervisor_agent import supervisor_node
        cases=[('the one you listed, please',{'mode':'book','room_id':self.room['room_id'],'preferences':{'has_ac':True}}),
               ('show my allocation',{'mode':'current'})]
        history=[{'role':'user','content':'I want AC'},{'role':'assistant','content':self.room['room_id']}]
        for question,request in cases:
            state={'user_id':'STU001','role':'student','session_id':'s','query':question,'chat_history':history,
                   'modules':['hostel'],'current_date':'2026-11-23','timezone':'Asia/Kolkata','agent_trace':[]}
            with patch('agents.supervisor_agent.llm.invoke_json',return_value={'route':'direct','hostel_request':request}) as model:
                out=supervisor_node(state)
            self.assertIn('hostel_tool',out['agent_trace']);self.assertNotIn('sql_query_agent',out['agent_trace'])
            self.assertEqual(model.call_args.args[1]['chat_history'],history)
        bad={'route':'direct','hostel_request':{'mode':'search','sql':'arbitrary'}}
        with patch('agents.supervisor_agent.llm.invoke_json',return_value=bad) as model:
            out=supervisor_node(state)
        self.assertEqual(model.call_count,3);self.assertIn("couldn't interpret",out['direct_answer'])

    def test_search_graph_uses_model_sql_and_grounded_answer(self):
        sql=("SELECT room_id,sharing_capacity,has_ac,sharing_capacity-occupied_beds AS available_beds,monthly_room_fee "
             "FROM hostel_rooms WHERE sharing_capacity=2 AND has_ac=1 AND monthly_room_fee<=8000 "
             "AND occupied_beds<sharing_capacity ORDER BY monthly_room_fee,room_id")
        history=[{'role':'user','content':'I need two sharing, at most 8000 per month'},
                 {'role':'assistant','content':'Would you prefer AC?'}]
        for m in history:append_turn('STU001','s',m['role'],m['content'])
        replies=[{'verdict':'allow','topic_scope':'college','modules':['hostel']},
                 {'route':'sql','scoped_request':'Find vacant two sharing AC rooms at most INR 8000 per month'},
                 {'sql':sql},{'verdict':'valid'}]
        with patch('agents.llm.invoke_json',side_effect=replies) as model, \
             patch('agents.llm.invoke_text',return_value='Matching AC rooms; select a room ID.') as answer, \
             patch('agents.graph.execute_query',wraps=execute_query) as db, \
             patch('agents.supervisor_agent.hostel.handle') as private:
            r=self.client.post('/api/chat',headers=self.headers,json={'message':'AC please','session_id':'s'})
        self.assertEqual(r.status_code,200,r.json)
        trace=r.json['agent_trace']
        for stage in ('sql_query_agent','validator_agent','db_tool','supervisor'):self.assertIn(stage,trace)
        self.assertNotIn('hostel_tool',trace);private.assert_not_called()
        db.assert_called_once_with(sql,'STU001','student',allow_menus=False)
        self.assertIn('hostel_rooms',model.call_args_list[2].args[1]['database_schema'])
        self.assertNotIn('classrooms',model.call_args_list[2].args[1]['database_schema'])
        self.assertTrue(any('two sharing' in m.get('content','') for m in model.call_args_list[2].args[1]['chat_history']))
        evidence=answer.call_args.args[1]['sql_result']
        self.assertEqual(len(evidence),2)
        self.assertTrue(all(x['has_ac']==1 and x['sharing_capacity']==2 and x['monthly_room_fee']<=8000 and x['available_beds']>0 for x in evidence))
        self.assertEqual(self.query('SELECT * FROM hostel_bookings'),[])
        self.assertNotIn('confirmation',r.json)

    def test_old_search_and_direct_answer_are_replanned(self):
        from agents.supervisor_agent import supervisor_node
        from agents.graph import planned_next
        for invalid in ({'route':'direct','hostel_request':{'mode':'search'}},
                        {'route':'direct','direct_answer':'There are plenty of rooms.'}):
            state={'user_id':'STU001','role':'student','session_id':'s','query':'Show available rooms',
                   'chat_history':[],'modules':['hostel'],'current_date':'2026-11-24','timezone':'Asia/Kolkata','agent_trace':[]}
            with patch('agents.llm.invoke_json',side_effect=[invalid,{'route':'sql','scoped_request':'List vacant rooms'}]) as model:
                result=supervisor_node(state)
            self.assertEqual(model.call_count,2)
            self.assertTrue(model.call_args.args[1]['validation_feedback'])
            self.assertEqual(planned_next(result),'sql_query_agent')
            self.assertNotIn('hostel_tool',result['agent_trace'])

    def test_hostel_search_cannot_write_or_read_private_records(self):
        from agents.graph import execute_sql_node
        for sql in ('DELETE FROM hostel_rooms',"UPDATE hostel_rooms SET occupied_beds=0",
                    'SELECT * FROM hostel_bookings','SELECT * FROM chat_confirmations'):
            state={'sql_query':sql,'user_id':'STU001','role':'student','agent_trace':[],
                   'validation_verdict':'valid','retry_count':3}
            result=execute_sql_node(state)
            self.assertEqual(result['validation_verdict'],'invalid')
        self.assertEqual(self.occupancy(),1)
        self.assertEqual(self.query('SELECT * FROM hostel_bookings'),[])

    def test_chat_graph_proposes_and_history_restores_card(self):
        from agents.prompts import GUARDRAIL_AGENT_PROMPT
        def model(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college','modules':['hostel']}
            return {'route':'direct','hostel_request':{'mode':'book','room_id':self.room['room_id'],'preferences':{'has_ac':True}}}
        with patch('agents.llm.invoke_json',side_effect=model),patch('agents.graph.execute_query') as sql:
            r=self.client.post('/api/chat',headers=self.headers,json={'message':'Book the selected AC room','session_id':'s'})
        self.assertEqual(r.status_code,200,r.json);sql.assert_not_called()
        p=r.json['confirmation'];self.assertEqual(p['status'],'pending');self.assertEqual(self.occupancy(),1)
        data=self.client.get('/api/chat/history?session_id=s',headers=self.headers).json
        self.assertTrue(any((m.get('confirmation') or {}).get('id')==p['id'] for m in data['sessions'][0]['messages']))
        self.confirm(p)
        data=self.client.get('/api/chat/history?session_id=s',headers=self.headers).json
        self.assertTrue(any((m.get('confirmation') or {}).get('status')=='confirmed' for m in data['sessions'][0]['messages']))

    def test_upgrade_from_previous_schema_preserves_counts(self):
        self.write('DROP TABLE hostel_bookings');self.write('DELETE FROM schema_migrations WHERE version=22')
        before=self.query('SELECT * FROM hostel_rooms')
        backup=migrate(self.db)
        self.assertTrue(Path(backup).exists());self.assertEqual(self.query('SELECT * FROM hostel_rooms'),before)
        self.assertEqual(self.query('SELECT * FROM hostel_bookings'),[])
        self.assertIsNone(migrate(self.db))


if __name__=='__main__':unittest.main()
