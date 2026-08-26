import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from flask import current_app, has_app_context


def _memory_dir() -> Path:
    if has_app_context():
        return Path(current_app.config["CHAT_MEMORY_DIR"])
    return Path(__file__).resolve().parents[1] / "chat_memory"


def _memory_path(user_id: str) -> Path:
    path = _memory_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{user_id}.json"


def _default_doc(user_id: str) -> Dict:
    return {"user_id": user_id, "username": user_id, "role": "", "sessions": []}


def read_memory(user_id: str) -> Dict:
    path = _memory_path(user_id)
    if not path.exists():
        return _default_doc(user_id)
    return json.loads(path.read_text(encoding="utf-8"))


def write_memory(user_id: str, data: Dict) -> None:
    _memory_path(user_id).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_history(user_id: str, session_id: str) -> List[Dict[str, str]]:
    data = read_memory(user_id)
    for session in data.get("sessions", []):
        if session.get("session_id") == session_id:
            return session.get("messages", [])
    return []


def load_session(user_id: str, session_id: str) -> Dict:
    data = read_memory(user_id)
    for session in data.get("sessions", []):
        if session.get("session_id") == session_id:
            return data
    data.setdefault("sessions", []).append(
        {"session_id": session_id, "timestamp": datetime.utcnow().isoformat(), "messages": []}
    )
    write_memory(user_id, data)
    return data


def append_turn(user_id: str, session_id: str, role: str, content: str) -> None:
    data = read_memory(user_id)
    sessions = data.setdefault("sessions", [])
    session = next((item for item in sessions if item.get("session_id") == session_id), None)
    if not session:
        session = {"session_id": session_id, "timestamp": datetime.utcnow().isoformat(), "messages": []}
        sessions.append(session)
    session.setdefault("messages", []).append({"role": role, "content": content})
    write_memory(user_id, data)
