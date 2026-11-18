"""Exercise the real React account screens and SMTP reset delivery without external email."""
import asyncore
import email
import os
import re
import shutil
import smtpd
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RAG_USE_CHROMA"] = "0"
from flask import send_from_directory
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app import create_app
from config import Config
from db.migrate import migrate


def main():
    root = Path(__file__).resolve().parents[2]
    artifacts = root / "artifacts"
    artifacts.mkdir(exist_ok=True)
    messages = []
    channels = {}

    class Capture(smtpd.SMTPServer):
        def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
            messages.append(email.message_from_bytes(data))

    smtp = Capture(("127.0.0.1", 0), None, map=channels)
    smtp_port = smtp.socket.getsockname()[1]
    stop = threading.Event()
    def mail_loop():
        while not stop.is_set():
            asyncore.loop(timeout=0.05, count=1, map=channels)
    mail_thread = threading.Thread(target=mail_loop, daemon=True)
    mail_thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "auth.db"
            shutil.copyfile(Config.DATABASE_PATH, database)
            migrate(database)
            app = create_app({"DATABASE_PATH": str(database), "CHAT_MEMORY_DIR": str(Path(directory) / "memory"),
                "SMTP_HOST": "127.0.0.1", "SMTP_PORT": smtp_port, "SMTP_SECURITY": "none", "SMTP_USERNAME": "", "SMTP_PASSWORD": "", "SMTP_FROM_EMAIL": "noreply@example.test"})
            dist = root / "frontend" / "dist"
            @app.get("/")
            def index(): return send_from_directory(dist, "index.html")
            @app.get("/<path:filename>")
            def assets_route(filename): return send_from_directory(dist, filename)
            server = make_server("127.0.0.1", 0, app, threaded=True)
            origin = f"http://127.0.0.1:{server.server_port}"
            app.config["PUBLIC_APP_URL"] = origin
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    cached = list((Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").glob("chromium-*/chrome-win*/chrome.exe"))
                    browser = playwright.chromium.launch(headless=True, **({"executable_path": str(cached[-1])} if cached else {}))
                    page = browser.new_page(viewport={"width": 2131, "height": 1230})
                    errors = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    page.goto(origin)
                    page.get_by_role("heading", name="NexusIQ Institution").wait_for()
                    page.screenshot(path=str(artifacts / "login-desktop.png"), full_page=True)
                    page.get_by_role("button", name="Sign up", exact=True).click()
                    page.set_viewport_size({"width": 1756, "height": 1200})
                    page.screenshot(path=str(artifacts / "signup-desktop.png"), full_page=True)
                    page.get_by_label("Full name", exact=True).fill("UI Test Student")
                    page.get_by_label("User ID", exact=True).fill("STU_UI001")
                    page.get_by_label("Registered email", exact=True).fill("student@example.test")
                    page.get_by_label("Password", exact=True).fill("first-password")
                    page.get_by_role("button", name="Create account", exact=True).click()
                    page.get_by_role("status").filter(has_text="Account created").wait_for()
                    page.get_by_label("Password", exact=True).fill("first-password")
                    page.get_by_role("button", name="Login", exact=True).click()
                    page.get_by_role("heading", name="NexusIQ Assistant", exact=True).wait_for()
                    old_token = page.evaluate("localStorage.getItem('college_assistant_token')")
                    page.wait_for_function("!document.querySelector('button[title=\"Log out\"]').disabled")
                    page.get_by_title("Log out", exact=True).click()
                    page.get_by_label("User ID", exact=True).fill("STU_UI001")
                    page.get_by_role("button", name="Forgot password?", exact=True).click()
                    assert page.get_by_label("User ID", exact=True).input_value() == "STU_UI001"
                    page.get_by_label("Registered email", exact=True).fill("wrong@example.test")
                    page.get_by_role("button", name="Send OTP", exact=True).click()
                    page.get_by_role("alert").filter(has_text="not registered for this account ID").wait_for()
                    assert len(messages) == 0
                    page.get_by_label("Registered email", exact=True).fill("student@example.test")
                    page.get_by_role("button", name="Send OTP", exact=True).click()
                    page.get_by_role("status").filter(has_text="six-digit OTP").wait_for()
                    assert len(messages) == 1
                    body = messages[0].get_payload(decode=True).decode()
                    code = re.search(r"\b[0-9]{6}\b", body).group(0)
                    assert 'http' not in body
                    page.get_by_label('OTP', exact=True).fill(code)
                    assert page.get_by_role('button', name=re.compile('Resend OTP in')).is_disabled()
                    page.get_by_label("New password", exact=True).fill("new-password")
                    page.get_by_label("Confirm password", exact=True).fill("does-not-match")
                    page.get_by_role("button", name="Reset password", exact=True).click()
                    page.get_by_role("alert").filter(has_text="do not match").wait_for()
                    page.get_by_label("Confirm password", exact=True).fill("new-password")
                    page.get_by_role("button", name="Reset password", exact=True).click()
                    page.get_by_role("status").filter(has_text="password has been reset").wait_for()
                    assert 'STU_UI001' in page.get_by_role('status').inner_text()
                    assert page.get_by_label('User ID', exact=True).input_value() == 'STU_UI001'
                    assert "token=" not in page.url
                    assert page.request.get(origin + "/api/auth/me", headers={"Authorization": "Bearer " + old_token}).status == 401
                    page.get_by_label("User ID", exact=True).fill("STU_UI001")
                    page.get_by_label("Password", exact=True).fill("new-password")
                    page.get_by_role("button", name="Login", exact=True).click()
                    page.get_by_role("heading", name="NexusIQ Assistant", exact=True).wait_for()
                    # Used OTPs fail, including from the reset screen while logged in.
                    page.goto(origin + '/#reset-password')
                    page.get_by_label('User ID', exact=True).fill('STU_UI001')
                    page.get_by_label('Registered email', exact=True).fill('student@example.test')
                    page.get_by_label('OTP', exact=True).fill(code)
                    page.get_by_label("New password", exact=True).fill("another-password")
                    page.get_by_label("Confirm password", exact=True).fill("another-password")
                    page.get_by_role("button", name="Reset password", exact=True).click()
                    page.get_by_role("alert").filter(has_text="invalid or expired").wait_for()
                    page.evaluate("localStorage.clear()")
                    page.goto(origin)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.get_by_role("button", name="Login", exact=True).wait_for()
                    page.screenshot(path=str(artifacts / "login-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    page.get_by_role("button", name="Sign up", exact=True).click()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    page.get_by_role("button", name="Create account", exact=True).scroll_into_view_if_needed()
                    page.screenshot(path=str(artifacts / "signup-mobile.png"), full_page=True)
                    assert not errors, errors
                    browser.close()
                print("PASS: registration, login, emailed OTP via local SMTP, cooldown, password mismatch, reset, OTP reuse rejection, session revocation, mobile layouts, no JS errors.")
            finally:
                server.shutdown()
                thread.join(timeout=5)
    finally:
        stop.set()
        mail_thread.join(timeout=2)
        smtp.close()


if __name__ == "__main__":
    main()
