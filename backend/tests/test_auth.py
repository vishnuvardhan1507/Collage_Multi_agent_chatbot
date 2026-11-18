import hashlib
import hmac
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RAG_USE_CHROMA"] = "0"
from flask import Flask
from flask_jwt_extended import JWTManager
from auth.routes import auth_bp
from auth.account_service import issue_reset, reset_password
from auth.mail import mail_configured, send_reset_email
from db.init_db import init_db
from db.migrate import migrate
from tools.db_tool import fetch_one


class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.base.name) / "fixture.db"
        init_db(cls.fixture)
        migrate(cls.fixture)

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "accounts.db"
        shutil.copyfile(self.fixture, self.database)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, DATABASE_PATH=str(self.database), JWT_SECRET_KEY="private-test-key-with-at-least-32-characters",
            SMTP_HOST="smtp.gmail.com", SMTP_PORT=587, SMTP_SECURITY="starttls", SMTP_USERNAME="sender@example.test",
            SMTP_PASSWORD="test-app-password", SMTP_FROM_EMAIL="", PUBLIC_APP_URL="http://localhost:5173", PASSWORD_RESET_TTL_SECONDS=600)
        JWTManager(self.app)
        self.app.register_blueprint(auth_bp)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.client = self.app.test_client()

    def registration(self, **changes):
        return {"full_name": "Test Student", "user_id": "STUTEST01", "email": "student@example.test", "password": "original-password", "role": "student", "department": "Computer Science", **changes}

    def register(self, **changes):
        return self.client.post("/api/auth/register", json=self.registration(**changes))

    def login(self, user_id="STUTEST01", password="original-password"):
        return self.client.post("/api/auth/login", json={"user_id": user_id, "password": password})

    def test_registration_creates_student_and_login(self):
        self.assertEqual(self.register().status_code, 201)
        user = fetch_one("SELECT * FROM users WHERE user_id='STUTEST01'")
        self.assertEqual(user["email"], "student@example.test")
        self.assertNotEqual(user["password_hash"], "original-password")
        profile = fetch_one("SELECT * FROM students WHERE student_id='STUTEST01'")
        self.assertEqual(profile["department"], "Computer Science")
        result = self.login()
        self.assertEqual(result.status_code, 200)
        me = self.client.get("/api/auth/me", headers={"Authorization": "Bearer " + result.json["access_token"]})
        self.assertEqual(me.json["role"], "student")

    def test_registration_creates_faculty_profile_without_assignments(self):
        self.assertEqual(self.register(user_id="FACNEW", role="faculty").status_code, 201)
        self.assertIsNotNone(fetch_one("SELECT 1 FROM faculty WHERE faculty_id='FACNEW'"))
        self.assertEqual(fetch_one("SELECT COUNT(*) n FROM courses WHERE faculty_id='FACNEW'")["n"], 0)

    def test_cannot_claim_existing_id_or_duplicate_email(self):
        before = fetch_one("SELECT * FROM users WHERE user_id='STU001'")
        self.assertEqual(self.register(user_id="STU001").status_code, 400)
        self.assertEqual(before, fetch_one("SELECT * FROM users WHERE user_id='STU001'"))
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.register(user_id="STUTEST02", email="STUDENT@EXAMPLE.TEST").status_code, 400)
        self.assertIsNone(fetch_one("SELECT * FROM students WHERE student_id='STUTEST02'"))

    def test_registration_validation(self):
        for changes in [{"email": "bad"}, {"email": "a@example.test\r\nBcc: another@example.test"}, {"user_id": "../escape"}, {"password": "123"}, {"role": "admin"}, {"department": "Unknown"}, {"full_name": " "}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.register(**changes).status_code, 400)

    def test_forgot_email_contains_otp_but_api_and_storage_do_not(self):
        self.register()
        with patch("auth.routes.send_reset_email") as send:
            result = self.client.post("/api/auth/forgot-password", json={"user_id": "STUTEST01", "email": "Student@Example.Test"})
            token = send.call_args.args[1]
        self.assertEqual(result.status_code, 202)
        self.assertRegex(token, r'^[0-9]{6}$')
        self.assertNotIn(token, result.get_data(as_text=True))
        record = fetch_one("SELECT * FROM password_reset_otps WHERE user_id='STUTEST01'")
        self.assertEqual(record["code_hash"], hmac.new(self.app.config["JWT_SECRET_KEY"].encode(), f"reset-otp:STUTEST01:{token}".encode(), hashlib.sha256).hexdigest())
        self.assertGreater(record["expires_at"], time.time() + 590)
        unknown = self.client.post("/api/auth/forgot-password", json={"user_id": "STUTEST01", "email": "unknown@example.test"})
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("not registered", unknown.json["error"])

    def test_reset_changes_password_once_and_revokes_sessions(self):
        self.register()
        old_token = self.login().json["access_token"]
        token = issue_reset("STUTEST01")
        result = self.client.post("/api/auth/reset-password", json={"user_id": "STUTEST01", "email": "student@example.test", "otp": token, "password": "new-password"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['user_id'], 'STUTEST01')
        self.assertIn('STUTEST01', result.json['message'])
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(password="new-password").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": "Bearer " + old_token}).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/reset-password", json={"user_id": "STUTEST01", "email": "student@example.test", "otp": token, "password": "another-password"}).status_code, 400)

    def test_expired_and_superseded_reset_codes(self):
        self.register()
        old = issue_reset("STUTEST01")
        new = issue_reset("STUTEST01")
        with self.assertRaises(ValueError):
            reset_password("student@example.test", old, "new-password", "STUTEST01")
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE password_reset_otps SET expires_at=0")
        with self.assertRaises(ValueError):
            reset_password("student@example.test", new, "new-password", "STUTEST01")
        self.assertEqual(self.login().status_code, 200)

    def test_concurrent_reset_code_can_only_be_used_once(self):
        self.register()
        token = issue_reset("STUTEST01")
        def reset(_):
            with self.app.app_context():
                try:
                    reset_password("student@example.test", token, "new-password", "STUTEST01")
                    return True
                except ValueError:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(reset, range(2))), [False, True])

    def test_missing_smtp_is_unavailable_and_failure_revokes_code(self):
        self.register()
        self.app.config["SMTP_PASSWORD"] = ""
        for email in ("student@example.test",):
            self.assertEqual(self.client.post("/api/auth/forgot-password", json={"user_id": "STUTEST01", "email": email}).status_code, 503)
        self.app.config["SMTP_PASSWORD"] = "test-password"
        with patch("auth.routes.send_reset_email", side_effect=OSError("offline")):
            self.assertEqual(self.client.post("/api/auth/forgot-password", json={"user_id": "STUTEST01", "email": "student@example.test"}).status_code, 202)
        self.assertEqual(fetch_one("SELECT COUNT(*) n FROM password_reset_otps")["n"], 0)

    def test_recovery_rate_limit_and_request_validation(self):
        start = time.time()
        for index in range(5):
            with patch("auth.account_service.time.time", return_value=start + index * 61):
                self.assertEqual(self.client.post("/api/auth/forgot-password", json={"user_id": "STUTEST01", "email": "unknown@example.test"}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/forgot-password", json={"user_id": "STUTEST01", "email": "unknown@example.test"}).status_code, 429)
        for endpoint in ("register", "forgot-password", "reset-password", "login"):
            self.assertEqual(self.client.post(f"/api/auth/{endpoint}", json=[]).status_code, 400)

    def test_gmail_transport_uses_starttls_and_app_password(self):
        self.app.config["SMTP_PASSWORD"] = "abcd efgh ijkl mnop"
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with patch("auth.mail.smtplib.SMTP", return_value=smtp) as constructor:
            send_reset_email("student@example.test", "012345")
        constructor.assert_called_once_with(host="smtp.gmail.com", port=587, timeout=15)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("sender@example.test", "abcdefghijklmnop")
        message = smtp.send_message.call_args.args[0]
        self.assertIn("012345", message.get_content())
        self.assertNotIn("http", message.get_content())
        self.assertEqual(message["To"], "student@example.test")

    def test_five_wrong_attempts_lock_the_code(self):
        self.register()
        code = issue_reset('STUTEST01')
        wrong = '000000' if code != '000000' else '000001'
        for _ in range(5):
            with self.assertRaises(ValueError):
                reset_password('student@example.test', wrong, 'new-password', 'STUTEST01')
        self.assertEqual(fetch_one("SELECT attempts FROM password_reset_otps WHERE user_id='STUTEST01'")['attempts'], 5)
        with self.assertRaises(ValueError):
            reset_password('student@example.test', code, 'new-password', 'STUTEST01')
        self.assertEqual(self.login().status_code, 200)

    def test_resend_cooldown_and_account_binding(self):
        self.register()
        self.register(user_id='STUTEST02', email='other@example.test')
        with patch('auth.routes.send_reset_email') as send:
            self.assertEqual(self.client.post('/api/auth/forgot-password', json={'user_id': 'STUTEST01', 'email': 'student@example.test'}).status_code, 202)
            code = send.call_args.args[1]
            self.assertEqual(self.client.post('/api/auth/forgot-password', json={'user_id': 'STUTEST01', 'email': 'student@example.test'}).status_code, 429)
            self.assertEqual(send.call_count, 1)
        with self.assertRaises(ValueError):
            reset_password('other@example.test', code, 'new-password', 'STUTEST01')
        self.assertEqual(fetch_one("SELECT attempts FROM password_reset_otps WHERE user_id='STUTEST01'")['attempts'], 0)
        reset_password('student@example.test', code, 'new-password', 'STUTEST01')

    def test_recovery_requires_matching_id_and_email(self):
        self.register()
        self.register(user_id='FACNEW', role='faculty', email='faculty@example.test')
        code = issue_reset('FACNEW')
        with patch('auth.routes.send_reset_email') as send:
            for user_id, email in [('STUTEST01', 'faculty@example.test'), ('FACNEW', 'student@example.test'), (None, 'student@example.test')]:
                payload = {'user_id': user_id, 'email': email}
                self.assertEqual(self.client.post('/api/auth/forgot-password', json=payload).status_code, 400)
                self.assertEqual(self.client.post('/api/auth/reset-password', json={**payload, 'otp': code, 'password': 'new-password'}).status_code, 400)
            send.assert_not_called()
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.login(user_id='FACNEW').status_code, 200)
        self.assertEqual(fetch_one("SELECT attempts FROM password_reset_otps WHERE user_id='FACNEW'")['attempts'], 0)
        self.assertEqual(self.client.post('/api/auth/reset-password', json={'user_id': 'FACNEW', 'email': 'faculty@example.test', 'otp': code, 'password': 'new-password'}).status_code, 200)
        self.assertEqual(self.login(user_id='FACNEW').status_code, 401)
        self.assertEqual(self.login(user_id='FACNEW', password='new-password').status_code, 200)

    def test_old_link_payload_is_not_accepted(self):
        self.assertEqual(self.client.post('/api/auth/reset-password', json={'token': 'a' * 43, 'password': 'new-password'}).status_code, 400)

    def test_insecure_remote_smtp_and_origin_independence(self):
        self.app.config["SMTP_SECURITY"] = "none"
        self.assertFalse(mail_configured())
        self.app.config["SMTP_SECURITY"] = "starttls"
        self.app.config["PUBLIC_APP_URL"] = ""
        self.assertTrue(mail_configured())


if __name__ == "__main__":
    unittest.main()
