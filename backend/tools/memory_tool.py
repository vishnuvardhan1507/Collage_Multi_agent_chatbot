import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from flask import current_app, has_app_context


def _memory_dir():
    return Path(current_app.config["CHAT_MEMORY_DIR"]) if has_app_context() else Path(__file__).resolve().parents[1] / "chat_memory"


def _memory_path(user_id):
    if not isinstance(user_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", user_id):
        raise ValueError("Invalid user ID for conversation storage.")
    directory = _memory_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user_id}.json"


def _read(path, user_id):
    if not path.exists():
        return {"user_id": user_id, "username": user_id, "role": "", "sessions": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, data):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_memory(user_id):
    path = _memory_path(user_id)
    with FileLock(str(path) + ".lock", timeout=30):
        return _read(path, user_id)


def update_memory(user_id, change):
    path = _memory_path(user_id)
    with FileLock(str(path) + ".lock", timeout=30):
        data = _read(path, user_id)
        change(data)
        _write(path, data)
        return data


def sync_profile(user_id, username, role):
    return update_memory(user_id, lambda data: data.update(username=username, role=role))


def _session(data, session_id):
    if session_id in data.get("deleted_sessions", []):
        raise ValueError("This conversation has been deleted. Start a new session.")
    sessions = data.setdefault("sessions", [])
    session = next((item for item in sessions if item["session_id"] == session_id), None)
    if session is None:
        session = {"session_id": session_id, "timestamp": datetime.now(timezone.utc).isoformat(), "messages": []}
        sessions.append(session)
    return session


def load_history(user_id, session_id):
    data = read_memory(user_id)
    return next((s.get("messages", []) for s in data["sessions"] if s["session_id"] == session_id), [])


def load_session(user_id, session_id):
    return read_memory(user_id)


def delete_session(user_id, session_id):
    def remove(data):
        from contextlib import closing
        from tools.db_tool import get_connection
        with closing(get_connection()) as conn, conn:
            conn.execute("UPDATE chat_confirmations SET status='cancelled',response='Conversation deleted.' WHERE user_id=? AND session_id=? AND status='pending'", (user_id,session_id))
        data["sessions"] = [s for s in data.get("sessions", []) if s["session_id"] != session_id]
        deleted = data.setdefault("deleted_sessions", [])
        if session_id not in deleted:
            deleted.append(session_id)
    return update_memory(user_id, remove)


def clear_history(user_id):
    """Clear this account under the same memory-first lock as confirmations."""
    removed = []
    def remove(data):
        from contextlib import closing
        from tools.db_tool import get_connection
        sessions = data.get('sessions', [])
        removed.extend(s['session_id'] for s in sessions)
        deleted = set(data.get('deleted_sessions', []))
        deleted.update(removed)
        with closing(get_connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            deleted.update(row['session_id'] for row in conn.execute(
                "SELECT session_id FROM chat_confirmations WHERE user_id=? AND status='pending'", (user_id,)))
            conn.execute("UPDATE chat_confirmations SET status='cancelled',response='Conversation deleted.' WHERE user_id=? AND status='pending'", (user_id,))
        data['sessions'] = []
        data['deleted_sessions'] = sorted(deleted)
    update_memory(user_id, remove)
    return len(removed)


def append_turn(user_id, session_id, role, content, **metadata):
    def append(data):
        if role == "assistant" and session_id in data.get("deleted_sessions", []):
            return
        session = _session(data, session_id)
        message = {"role": role, "content": content, "timestamp": datetime.now(timezone.utc).isoformat(), **metadata}
        # Pair concurrent replies with their question, without losing either turn.
        messages = session.setdefault("messages", [])
        if role == "assistant" and metadata.get("request_id"):
            for index, previous in enumerate(messages):
                if previous.get("request_id") == metadata["request_id"] and previous["role"] == "user":
                    messages.insert(index + 1, message)
                    break
            else:
                messages.append(message)
        else:
            messages.append(message)
    return update_memory(user_id, append)
