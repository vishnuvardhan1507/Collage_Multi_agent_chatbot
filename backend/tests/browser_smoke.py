"""Optional real-browser smoke test. Uses a temporary database and mocked language calls."""
import os
import shutil
import sqlite3
from contextlib import closing
from werkzeug.security import generate_password_hash
import sys
import tempfile
import threading
import io
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RAG_USE_CHROMA"] = "0"
os.environ["ALLOW_MODEL_DOWNLOAD"] = "0"

from flask import send_from_directory
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from PIL import Image

from agents import llm
from agents.prompts import GUARDRAIL_AGENT_PROMPT, SUPERVISOR_AGENT_PROMPT, SQL_QUERY_AGENT_PROMPT, VALIDATOR_AGENT_PROMPT
from app import create_app
from config import Config
from db.migrate import migrate
from tools.operations import college_today


def model(prompt, payload):
    if prompt == GUARDRAIL_AGENT_PROMPT:
        return {"verdict": "allow", "topic_scope": "college"}
    if prompt == SUPERVISOR_AGENT_PROMPT:
        return {"route": "sql", "scoped_request": payload["query"]}
    if prompt == SQL_QUERY_AGENT_PROMPT:
        if 'book classroom browser' in payload['query'].lower():
            return {'operation':{'type':'book_classroom','classroom_id':'B-202','booking_date':'2038-01-10','start_time':'10:00','end_time':'11:00','purpose':'Browser confirmation'}}
        if "delete browser smoke" in payload["query"].lower():
            return {"operation": {"type": "delete_leave", "description": "Browser smoke demo"}}
        if "submit leave" in payload["query"].lower():
            day = college_today().isoformat()
            return {"operation": {"type": "submit_leave", "from_date": day, "to_date": day, "reason": "Browser smoke demo"}}
        if payload["role"] == "faculty":
            return {"sql": "SELECT course_id, course_name FROM courses"}
        return {"sql": "SELECT c.course_name, cr.status FROM course_registrations cr JOIN courses c ON c.course_id=cr.course_id"}
    if prompt == VALIDATOR_AGENT_PROMPT:
        return {"verdict": "valid"}
    raise AssertionError("Unexpected prompt")


def main():
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        database = directory / "browser.db"
        shutil.copyfile(Config.DATABASE_PATH, database)
        migrate(database)
        # Keep smoke credentials independent of real account password changes.
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE users SET password_hash=? WHERE user_id IN ('STU001','FAC001')", (generate_password_hash('password123'),))
            connection.execute("DELETE FROM leave_requests")
        connection.close()
        app = create_app({"DATABASE_PATH": str(database), "CHAT_MEMORY_DIR": str(directory / "memory"), "TESTING": True})
        dist = root / "frontend" / "dist"

        @app.get("/")
        def index():
            return send_from_directory(dist, "index.html")

        @app.get("/<path:filename>")
        def assets(filename):
            return send_from_directory(dist, filename)

        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with patch.object(llm, "invoke_json", side_effect=model), patch.object(llm, "invoke_text", return_value="Your authorized courses include Machine Learning."), sync_playwright() as playwright:
                cached = list((Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").glob("chromium-*/chrome-win*/chrome.exe"))
                browser = playwright.chromium.launch(headless=True, **({"executable_path": str(cached[-1])} if cached else {}))
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.add_init_script("""
                  window.voiceCalls = [];
                  window.SpeechRecognition = class {
                    start() { window.recognitionMock = this; }
                    abort() { if (this.onend) this.onend(); }
                  };
                  Object.defineProperty(window, 'speechSynthesis', {value: {
                    speak(u) { window.voiceCalls.push(u.text); window.utteranceMock = u; }, cancel() {}
                  }});
                """)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}")

                def login(user):
                    page.get_by_label("User ID", exact=True).fill(user)
                    page.locator('input[type="password"]').fill("password123")
                    page.get_by_role("button", name="Login", exact=True).click()
                    page.get_by_role("heading", name="NexusIQ Assistant", exact=True).wait_for()
                    page.wait_for_function("!document.querySelector('textarea').disabled")

                def chat(text):
                    page.get_by_label("Message", exact=True).fill(text)
                    page.get_by_role("button", name="Send message", exact=True).click()
                    page.wait_for_function("!document.querySelector('button[title=\"New session\"]').disabled")

                def toggle_leaves():
                    if page.locator('.leave-dialog').count():
                        page.get_by_role('button', name='Close leave tools', exact=True).click()
                    else:
                        page.locator('.composer').get_by_role('button', name='Leave tools', exact=True).click()

                def check_leave_close():
                    draft = page.get_by_label('Message', exact=True)
                    draft.fill('Keep this draft')
                    session = page.locator('.session-label strong').text_content()
                    hand = page.get_by_role('button', name='Leave tools', exact=True)
                    for mobile in (False, True):
                        page.set_viewport_size({'width': 390, 'height': 844} if mobile else {'width': 1440, 'height': 1000})
                        hand.click()
                        close = page.get_by_role('button', name='Close leave tools', exact=True)
                        expect(close).to_be_visible()
                        expect(close).to_have_text('')
                        page.locator('.leave-dialog-content').evaluate('(el) => { el.style.maxHeight = "100px"; el.scrollTop = el.scrollHeight; }')
                        expect(close).to_be_in_viewport()
                        close.focus()
                        page.keyboard.press('Enter')
                        expect(page.locator('.leave-dialog')).to_have_count(0)
                        expect(hand).to_be_focused()
                        expect(draft).to_have_value('Keep this draft')
                        hand.click()
                        page.keyboard.press('Escape')
                        expect(page.locator('.leave-dialog')).to_have_count(0)
                        expect(hand).to_be_focused()
                        draft.fill('Keep this draft')
                        assert page.locator('.session-label strong').text_content() == session
                    page.set_viewport_size({'width': 1440, 'height': 1000})
                    draft.fill('')

                def assert_blank_chat():
                    expect(page.locator('.messages')).to_be_empty()
                    expect(page.locator('.chat-header')).to_be_visible()
                    expect(page.locator('.prompt-list[open]')).to_be_visible()
                    expect(page.get_by_label('Message', exact=True)).to_be_visible()

                login("STU001")
                expect(page.locator('.session-label strong')).to_have_text('New conversation')
                original_prompts = page.locator('.prompt-list > button').all_text_contents()
                refresh = page.get_by_role('button', name='Refresh demo prompts', exact=True)
                refresh.focus()
                page.keyboard.press('Enter')
                updated_prompts = page.locator('.prompt-list > button').all_text_contents()
                assert len(updated_prompts) == 4 and not set(original_prompts) & set(updated_prompts)
                expect(page.locator('.prompt-list[open]')).to_be_visible()
                # Check long text without modifying an account or saved conversation.
                name = page.locator('.profile h2')
                original_name = name.text_content()
                name.evaluate("el => el.textContent = 'Alexandria Vishnuvardhan Long Profile Name'")
                assert name.evaluate('el => el.scrollWidth > el.clientWidth && getComputedStyle(el).textOverflow === "ellipsis"')
                assert page.locator('.sidebar').evaluate('el => el.scrollWidth <= el.clientWidth')
                name.evaluate('(el, text) => el.textContent = text', original_name)
                check_leave_close()
                assert page.get_by_text("Authenticated as student").is_visible()
                toggle_leaves()
                page.get_by_title('From date', exact=True).fill('2035-01-10')
                page.get_by_title('To date', exact=True).fill('2035-01-12')
                page.get_by_placeholder('Reason', exact=True).fill('Deletion browser test')
                page.get_by_title('Submit leave', exact=True).click()
                row = page.locator('.leave-row').filter(has_text='Deletion browser test')
                row.wait_for()
                page.get_by_title('From date', exact=True).fill('2035-01-12')
                page.get_by_title('To date', exact=True).fill('2035-01-13')
                page.get_by_placeholder('Reason', exact=True).fill('Overlap browser test')
                page.get_by_title('Submit leave', exact=True).click()
                page.locator('.leave-panel').get_by_role('alert').filter(has_text='overlap').wait_for()
                row.get_by_role('button', name='Delete leave #', exact=False).click()
                page.get_by_role('button', name='No', exact=True).click()
                expect(row).to_have_count(1)
                row.get_by_role('button', name='Delete leave #', exact=False).click()
                page.get_by_role('button', name='Yes', exact=True).click()
                expect(row).to_have_count(0)
                toggle_leaves()

                expect(page.locator('.sidebar').get_by_role('button', name='Leave tools', exact=True)).to_have_count(0)
                assert_blank_chat()
                chat("Show my enrolled courses")
                page.get_by_text("Your authorized courses include Machine Learning.", exact=True).wait_for()
                original = page.locator('.session-label strong').inner_text()
                page.get_by_title("New session", exact=True).click()
                page.wait_for_function("!document.querySelector('textarea').disabled")
                assert_blank_chat()
                chat("Submit leave today for Browser smoke demo")
                page.get_by_role('button',name='Confirm',exact=True).wait_for()
                with closing(sqlite3.connect(database)) as conn:
                    assert conn.execute("SELECT COUNT(*) FROM leave_requests WHERE reason='Browser smoke demo'").fetchone()[0]==0
                page.reload()
                page.get_by_role('button',name='Confirm',exact=True).wait_for()
                page.get_by_role('button',name='Cancel',exact=True).click()
                page.locator('.chat-confirmation').get_by_text('Cancelled',exact=True).wait_for()
                chat("Submit leave today for Browser smoke demo")
                confirm=page.get_by_role('button',name='Confirm',exact=True)
                confirm.focus()
                page.keyboard.press('Enter')
                page.locator('.chat-confirmation').get_by_text('Confirmed',exact=True).wait_for()
                toggle_leaves()
                page.locator(".leave-list").get_by_text("Browser smoke demo", exact=True).wait_for()
                student_session = page.locator('.session-label strong').text_content()
                toggle_leaves()
                page.locator('.history-dropdown > summary').click()
                page.locator('.history-option > button:first-child').filter(has_text='Show my enrolled courses').click()
                page.get_by_text("Your authorized courses include Machine Learning.", exact=True).wait_for()
                page.reload()
                page.get_by_text("Your authorized courses include Machine Learning.", exact=True).wait_for()
                page.get_by_title("Log out", exact=True).click()

                login("FAC001")
                check_leave_close()
                assert page.get_by_text("Authenticated as faculty").is_visible()
                assert_blank_chat()
                assert page.locator('.session-label strong').inner_text() != student_session
                chat('Book classroom browser test')
                page.get_by_role('button',name='Confirm',exact=True).wait_for()
                page.set_viewport_size({'width':390,'height':844})
                expect(page.get_by_role('button',name='Confirm',exact=True)).to_be_in_viewport()
                page.get_by_role('button',name='Confirm',exact=True).click()
                page.locator('.chat-confirmation').get_by_text('Confirmed',exact=True).wait_for()
                page.set_viewport_size({'width':1440,'height':1000})
                with closing(sqlite3.connect(database)) as conn:
                    assert conn.execute("SELECT COUNT(*) FROM classroom_bookings WHERE purpose='Browser confirmation'").fetchone()[0]==1
                toggle_leaves()
                page.get_by_role("heading", name="Leave review", exact=True).wait_for()
                card = page.locator(".leave-row").filter(has_text="Browser smoke demo")
                card.get_by_role("button", name="Reject leave", exact=True).click()
                expect(page.get_by_role('button', name='Reject request', exact=True)).to_be_disabled()
                page.get_by_label('Rejection reason', exact=True).fill('Required practical assessment')
                page.get_by_role('button', name='Cancel', exact=True).click()
                expect(card).to_be_visible()
                card.get_by_role('button', name='Reject leave', exact=True).click()
                page.get_by_label('Rejection reason', exact=True).fill('Required practical assessment')
                page.get_by_role('button', name='Reject request', exact=True).click()
                page.locator(".leave-panel").get_by_text("Leave request rejected.", exact=True).wait_for()
                toggle_leaves()
                chat("Show my assigned classes")
                page.get_by_text("Your authorized courses include Machine Learning.", exact=True).wait_for()
                page.get_by_title("Log out", exact=True).click()
                login("STU001")
                toggle_leaves()
                page.locator(".leave-row").filter(has_text="Browser smoke demo").get_by_text("rejected", exact=True).wait_for()
                page.locator(".leave-row").filter(has_text="Browser smoke demo").get_by_text("Required practical assessment", exact=False).wait_for()
                toggle_leaves()
                chat('Delete Browser smoke demo leave request')
                page.get_by_text('deleted.', exact=False).last.wait_for()
                toggle_leaves()
                expect(page.locator('.leave-row').filter(has_text='Browser smoke demo')).to_have_count(0)

                toggle_leaves()
                assert page.locator('.prompt-list[open]').count() == 1
                assert page.locator('.leave-drawer').count() == 0
                page.get_by_title('New session', exact=True).click()
                page.wait_for_function("!document.querySelector('textarea').disabled")
                assert_blank_chat()

                # Account-owned photo with adjustment, reload persistence, replacement and deletion.
                image = io.BytesIO()
                Image.new('RGB', (120, 80), '#23aad8').save(image, 'PNG')
                page.get_by_label('Edit profile photo', exact=True).click()
                # Portrait and square sources previously could not move horizontally at 1x.
                for dimensions in [(80, 120), (100, 100), (120, 80)]:
                    probe = io.BytesIO()
                    pattern = Image.new('RGB', dimensions, '#218cdc')
                    pattern.paste('#ffb14a', (0, 0, dimensions[0] // 2, dimensions[1]))
                    pattern.save(probe, 'PNG')
                    page.get_by_label('Upload profile photo').set_input_files({'name': 'pan.png', 'mimeType': 'image/png', 'buffer': probe.getvalue()})
                    page.wait_for_function("document.querySelector('.crop-preview img')?.naturalWidth > 0")
                    page.get_by_label('Zoom', exact=True).fill('1')
                    page.get_by_label('Horizontal position', exact=True).fill('0.1')
                    left = page.locator('.crop-preview img').bounding_box()['x']
                    page.get_by_label('Horizontal position', exact=True).fill('0.9')
                    right = page.locator('.crop-preview img').bounding_box()['x']
                    assert right < left - 10, (dimensions, left, right)
                    page.get_by_role('button', name='Reset adjustments').click()
                    frame = page.locator('.crop-preview').bounding_box()
                    start = page.locator('.crop-preview img').bounding_box()['x']
                    page.mouse.move(frame['x'] + frame['width']/2, frame['y'] + frame['height']/2)
                    page.mouse.down()
                    page.mouse.move(frame['x'] + frame['width']/2 + 10, frame['y'] + frame['height']/2 + 6, steps=4)
                    page.mouse.up()
                    finish = page.locator('.crop-preview img').bounding_box()['x']
                    assert abs((finish - start) - 10) < 2
                assert page.locator('.avatar-dialog').bounding_box()['width'] <= 400
                page.screenshot(path=str(root / 'frontend' / 'avatar-editor-desktop.png'))
                page.set_viewport_size({'width':375,'height':667})
                box = page.locator('.avatar-dialog').bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= 375
                assert box['height'] <= 635
                page.screenshot(path=str(root / 'frontend' / 'avatar-editor-mobile.png'))
                page.set_viewport_size({'width':1440,'height':1000})
                page.get_by_label('Upload profile photo').set_input_files({'name': 'profile.png', 'mimeType': 'image/png', 'buffer': image.getvalue()})
                page.wait_for_function("document.querySelector('.crop-preview img')?.naturalWidth === 120")
                page.get_by_label('Zoom', exact=True).fill('1.5')
                page.get_by_label('Horizontal position', exact=True).fill('0.3')
                page.get_by_role('button', name='Save', exact=True).click()
                page.locator('.profile-avatar img').wait_for()
                page.reload()
                page.locator('.profile-avatar img').wait_for()
                page.get_by_label('Edit profile photo', exact=True).click()
                assert page.get_by_label('Zoom', exact=True).input_value() == '1.5'
                assert page.get_by_label('Horizontal position', exact=True).input_value() == '0.3'
                page.get_by_label('Vertical position', exact=True).fill('0.7')
                page.get_by_role('button', name='Save', exact=True).click()
                page.locator('.avatar-dialog').wait_for(state='detached')

                saved_photo = page.locator('.profile-avatar img').get_attribute('src')
                replacement = io.BytesIO()
                Image.new('RGB', (100, 120), '#3366bb').save(replacement, 'PNG')
                page.get_by_label('Edit profile photo', exact=True).click()
                page.get_by_label('Upload profile photo').set_input_files({'name': 'new.png', 'mimeType': 'image/png', 'buffer': replacement.getvalue()})
                page.get_by_role('button', name='Cancel', exact=True).click()
                assert page.locator('.profile-avatar img').get_attribute('src') == saved_photo
                page.get_by_label('Edit profile photo', exact=True).click()
                page.get_by_label('Upload profile photo').set_input_files({'name': 'new.png', 'mimeType': 'image/png', 'buffer': replacement.getvalue()})
                page.get_by_role('button', name='Save', exact=True).click()
                page.locator('.avatar-dialog').wait_for(state='detached')
                assert page.locator('.profile-avatar img').get_attribute('src') != saved_photo

                # Dictation never sends automatically; speech and stop controls use browser APIs.
                page.get_by_label('Message', exact=True).fill('My draft')
                page.get_by_label('Start voice typing', exact=True).click()
                page.evaluate("window.recognitionMock.onresult({results: [[{transcript: 'with dictated words'}]]})")
                expect(page.get_by_label('Message', exact=True)).to_have_value('My draft with dictated words')
                page.get_by_label('Stop dictation', exact=True).click()
                page.get_by_label('Start voice typing', exact=True).click()
                page.evaluate("window.recognitionMock.onerror({error: 'not-allowed'})")
                page.get_by_text('Microphone permission was denied.', exact=False).wait_for()
                page.get_by_label('Stop dictation', exact=True).click()
                expect(page.locator('.voice-status')).to_have_text('Off')
                voice_toggle = page.get_by_role('button', name='Toggle voice playback', exact=True)
                expect(voice_toggle).to_have_text('')
                voice_toggle.click()
                expect(page.locator('.voice-status')).to_have_text('On')
                expect(voice_toggle).to_have_attribute('aria-pressed', 'true')
                with patch.object(llm, 'invoke_text', return_value='Course schedule\n\n| Course | Day | Time |\n| --- | --- | --- |\n| Machine Learning | Friday | 09:00–10:00 |'):
                    chat('Show my course schedule')
                expect(page.locator('.session-label strong')).to_have_text('Show my course schedule')
                page.get_by_role('table').wait_for()
                page.wait_for_function('window.voiceCalls.length > 0')
                page.get_by_label('Stop reading', exact=True).click()
                page.get_by_label('Read answer aloud', exact=True).last.click()
                expect(page.locator('.voice-status')).to_have_text('On')
                expect(voice_toggle).to_have_attribute('aria-pressed', 'true')
                page.evaluate('window.utteranceMock.onend()')
                expect(page.locator('.voice-status')).to_have_text('Off')
                page.get_by_label('Read answer aloud', exact=True).last.click()
                page.get_by_label('Stop reading', exact=True).click()
                expect(page.locator('.voice-status')).to_have_text('Off')
                page.get_by_label('Read answer aloud', exact=True).last.click()
                voice_toggle.click()
                expect(page.locator('.voice-status')).to_have_text('Off')
                assert page.evaluate('window.scrollY === 0'), 'Chat must not scroll the document'
                assert page.locator('.chat-header').bounding_box()['y'] == 0
                artifacts = root / 'artifacts'
                artifacts.mkdir(exist_ok=True)
                page.screenshot(path=str(artifacts / 'nexusiq-chat-desktop.png'), full_page=True)

                # Native disclosure keyboard support, cancellation, and deleting the active session.
                history = page.locator('.history-dropdown > summary')
                history.focus()
                page.keyboard.press('Enter')
                assert page.locator('.history-options').is_visible()
                page.keyboard.press('Escape')
                assert not page.locator('.history-options').is_visible()
                history.click()
                page.locator('.history-option.selected').get_by_role('button', name='Delete conversation:', exact=False).click()
                page.get_by_role('button', name='Cancel', exact=True).click()
                page.locator('.history-option.selected').get_by_role('button', name='Delete conversation:', exact=False).click()
                page.get_by_role('button', name='Delete conversation', exact=True).click()
                assert_blank_chat()
                assert page.locator('.history-option.selected').count() == 0
                page.get_by_label('Edit profile photo', exact=True).click()
                page.get_by_role('button', name='Remove photo', exact=True).click()
                page.locator('.avatar-dialog').wait_for(state='detached')
                assert page.locator('.profile-avatar img').count() == 0
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.get_by_label("Message", exact=True).is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile layout overflows horizontally"
                page.get_by_label('Open sidebar', exact=True).click()
                assert page.locator('.sidebar-open').is_visible()
                assert page.locator('.sidebar').evaluate('el => el.scrollWidth <= el.clientWidth')
                page.screenshot(path=str(artifacts / 'nexusiq-sidebar-mobile.png'), full_page=True)
                page.get_by_label('Close sidebar', exact=True).click()
                page.wait_for_function("document.querySelector('.sidebar').getBoundingClientRect().right <= 1")
                page.screenshot(path=str(artifacts / 'nexusiq-chat-mobile.png'), full_page=True)
                browser.close()
            assert not errors, errors
            print("PASS: both roles, chat/history/reload, leave refresh/review, avatar upload/edit/remove, mocked dictation/audio, keyboard dropdown/deletion, desktop/mobile screenshots; no browser JS errors.")
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
