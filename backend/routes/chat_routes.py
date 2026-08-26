from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required
from groq import RateLimitError

from tools.db_tool import fetch_one
from tools.memory_tool import load_history, load_session, read_memory, write_memory
from workflow.graph import build_graph


chat_bp = Blueprint("chat", __name__, url_prefix="/api/chat")
_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _sync_memory_profile(user_id: str, role: str) -> None:
    user = fetch_one(
        "SELECT username, role FROM users WHERE user_id = ?",
        (user_id,),
        db_path=current_app.config["DATABASE_PATH"],
    )
    data = read_memory(user_id)
    data["username"] = user["username"] if user else user_id
    data["role"] = role
    write_memory(user_id, data)


@chat_bp.post("")
@jwt_required()
def chat():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    user_id = get_jwt_identity()
    role = get_jwt().get("role")
    session_id = payload.get("session_id") or f"session_{uuid4().hex[:8]}"
    _sync_memory_profile(user_id, role)

    state = {
        "user_id": user_id,
        "role": role,
        "session_id": session_id,
        "query": message,
        "chat_history": load_history(user_id, session_id),
        "guardrail_verdict": None,
        "guardrail_reason": None,
        "database_schema": None,
        "route": None,
        "scoped_request": None,
        "direct_answer": None,
        "sql_query": None,
        "sql_result": None,
        "validation_verdict": None,
        "validation_feedback": None,
        "retry_count": 0,
        "rag_context": None,
        "final_response": None,
        "agent_trace": [],
    }

    try:
        result = _get_graph().invoke(state)
    except RuntimeError as exc:
        if "GROQ_API_KEY" in str(exc):
            return jsonify({"error": "GROQ_API_KEY is not configured on the backend"}), 503
        raise
    except RateLimitError:
        return jsonify({"error": "Groq rate limit reached. Please wait a few minutes and try again."}), 429
    return jsonify(
        {
            "response": result.get("final_response"),
            "session_id": session_id,
            "agent_trace": result.get("agent_trace", []),
        }
    )


@chat_bp.get("/history")
@jwt_required()
def history():
    user_id = get_jwt_identity()
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify(read_memory(user_id))
    return jsonify(load_session(user_id, session_id))
