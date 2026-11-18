import base64
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['RAG_USE_CHROMA'] = '0'
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from PIL import Image
from auth.routes import auth_bp
from routes.chat_routes import chat_bp
from db.init_db import init_db
from db.migrate import migrate
from tools.memory_tool import append_turn, delete_session, load_history
from tools.db_tool import schema_metadata


class ChatFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.db'
        init_db(self.db)
        migrate(self.db)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, JWT_SECRET_KEY='a-test-secret-that-is-long-enough-for-jwt', DATABASE_PATH=str(self.db), CHAT_MEMORY_DIR=str(Path(self.temp.name) / 'memory'))
        JWTManager(self.app)
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(chat_bp)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.client = self.app.test_client()
        self.headers = self.token('STU001')

    def token(self, user):
        return {'Authorization': 'Bearer ' + create_access_token(identity=user, additional_claims={'ver': 0})}

    def test_clear_all_history_is_owned_and_blocks_delayed_replies(self):
        from tools.memory_tool import read_memory, update_memory
        from tools.confirmations import propose, decide
        from tools.operations import OperationError
        from tools.db_tool import fetch_all
        for user in ['STU001','FAC001']:
            for session in ['one','two']:
                append_turn(user,session,'user','Saved question')
            def draft(data):
                data['sessions'][0]['leave_draft']={'reason':'unfinished'}
                data['sessions'][0]['attendance_draft']={'marks':{}}
            update_memory(user,draft)
        pending=propose({'type':'submit_leave','from_date':'2099-01-10','to_date':'2099-01-11','reason':'Test'},'STU001','student','one')
        other=read_memory('FAC001')
        with sqlite3.connect(self.db) as conn:
            before={table:conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ['alert_rules','leave_requests','classroom_bookings','attendance']}
        self.assertEqual(self.client.delete('/api/chat/history').status_code,401)
        response=self.client.delete('/api/chat/history',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['deleted_count'],2)
        self.assertEqual(read_memory('STU001')['sessions'],[])
        self.assertEqual(read_memory('FAC001'),other)
        self.assertEqual(set(read_memory('STU001')['deleted_sessions']),{'one','two'})
        self.assertEqual(fetch_all('SELECT status FROM chat_confirmations')[0]['status'],'cancelled')
        with self.assertRaises(OperationError): decide(pending['id'],'STU001','student','one','confirm')
        append_turn('STU001','one','assistant','Late response')
        self.assertEqual(read_memory('STU001')['sessions'],[])
        with self.assertRaises(ValueError): append_turn('STU001','one','user','Stale tab')
        self.assertEqual(self.client.delete('/api/chat/history',headers=self.headers).json['deleted_count'],0)
        append_turn('STU001','new','user','New chat')
        self.assertEqual(len(read_memory('STU001')['sessions']),1)
        self.assertEqual(self.client.delete('/api/chat/history',headers=self.token('FAC001')).json['deleted_count'],2)
        self.assertEqual(len(read_memory('STU001')['sessions']),1)
        with sqlite3.connect(self.db) as conn:
            after={table:conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in before}
        self.assertEqual(before,after)

    def image(self, color='red', size=(60, 40)):
        data = io.BytesIO()
        Image.new('RGB', size, color).save(data, 'PNG')
        data.seek(0)
        return data

    def upload(self, image=None, crop=None):
        return self.client.put('/api/auth/me/avatar', headers=self.headers, data={'image': (image or self.image(), 'avatar.png'), 'crop': json.dumps(crop or {'x': .2, 'y': .6, 'zoom': 2})})

    def test_avatar_lifecycle_is_private_and_normalized(self):
        self.assertEqual(self.client.get('/api/auth/me/avatar').status_code, 401)
        result = self.upload()
        self.assertEqual(result.status_code, 200)
        avatar = result.json['avatar']
        self.assertEqual(avatar['crop']['zoom'], 2)
        decoded = Image.open(io.BytesIO(base64.b64decode(avatar['src'].split(',')[1])))
        self.assertEqual(decoded.format, 'JPEG')
        self.assertEqual(self.client.get('/api/auth/me/avatar', headers=self.headers).json['avatar'], avatar)
        self.assertIsNone(self.client.get('/api/auth/me/avatar', headers=self.token('FAC001')).json['avatar'])
        self.assertNotEqual(self.upload(self.image('blue')).json['avatar']['src'], avatar['src'])
        self.client.delete('/api/auth/me/avatar', headers=self.headers)
        self.assertIsNone(self.client.get('/api/auth/me/avatar', headers=self.headers).json['avatar'])
        self.assertNotIn('user_avatars', schema_metadata())

    def test_invalid_uploads_and_crops_leave_existing_photo(self):
        original = self.upload().json
        for data in [io.BytesIO(b'not an image'), io.BytesIO(b'x' * (5 * 1024 * 1024 + 1)), self.image(size=(4001, 4000))]:
            self.assertIn(self.upload(data).status_code, (400, 413))
        for crop in [{'x': -1}, {'zoom': 99}, {'y': float('nan')}, []]:
            self.assertEqual(self.client.put('/api/auth/me/avatar', headers=self.headers, data={'image': (self.image(), 'x.png'), 'crop': json.dumps(crop)}).status_code, 400)
        self.assertEqual(self.client.get('/api/auth/me/avatar', headers=self.headers).json, original)

    def test_history_read_does_not_create_session_and_delete_is_scoped(self):
        self.assertEqual(self.client.get('/api/chat/history?session_id=new', headers=self.headers).json['sessions'], [])
        self.assertFalse((Path(self.temp.name) / 'memory' / 'STU001.json').exists())
        for user in ['STU001', 'FAC001']:
            append_turn(user, 'same', 'user', 'Private question')
        self.assertIn('timestamp', load_history('STU001', 'same')[0])
        self.assertEqual(self.client.delete('/api/chat/history/same').status_code, 401)
        self.assertEqual(self.client.delete('/api/chat/history/same', headers=self.headers).status_code, 200)
        self.assertEqual(load_history('STU001', 'same'), [])
        self.assertEqual(len(load_history('FAC001', 'same')), 1)
        self.assertEqual(self.client.post('/api/chat', headers=self.headers, json={'session_id': 'same', 'message': 'Revive'}).status_code, 409)

    def test_delete_during_pending_reply_cannot_resurrect_session(self):
        entered, release = threading.Event(), threading.Event()
        def invoke(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'final_response': 'Late reply'}
        result = []
        def send():
            with self.app.test_client() as client:
                result.append(client.post('/api/chat', headers=self.headers, json={'session_id': 'pending', 'message': 'Question'}))
        with patch('routes.chat_routes._get_graph') as graph:
            graph.return_value.invoke.side_effect = invoke
            thread = threading.Thread(target=send)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.client.delete('/api/chat/history/pending', headers=self.headers)
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(result[0].status_code, 200)
        self.assertEqual(load_history('STU001', 'pending'), [])
        self.assertIn('pending', self.client.get('/api/chat/history', headers=self.headers).json['deleted_sessions'])

    def test_migration_repeatable_and_preserves_photo(self):
        original = self.upload().json
        self.assertIsNone(migrate(self.db))
        self.assertEqual(self.client.get('/api/auth/me/avatar', headers=self.headers).json, original)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=3').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
