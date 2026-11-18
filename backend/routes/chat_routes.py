import re
from uuid import uuid4

from flask import Blueprint, current_app, g, jsonify, request
from groq import RateLimitError

from agents.graph import build_graph
from auth.access import authenticated
from tools.memory_tool import append_turn, clear_history, delete_session, load_history, load_session, read_memory, sync_profile
from datetime import datetime, timezone
from tools.operations import college_today
from tools.operations import OperationError
from tools.confirmations import decide, hydrate
from tools import attendance_chat
from tools import topic_research
from tools.timetable import timetable_context


chat_bp = Blueprint("chat", __name__, url_prefix="/api/chat")
_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def valid_session(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) is not None


@chat_bp.post("")
@authenticated
def chat():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), str) or not payload["message"].strip():
        return jsonify({"error": "message is required"}), 400
    message = payload["message"].strip()
    if len(message) > 12000:
        return jsonify({"error": "Please keep messages under 12000 characters"}), 400
    session_id = payload.get("session_id") or f"session_{uuid4().hex[:12]}"
    if not valid_session(session_id):
        return jsonify({"error": "Invalid session ID"}), 400
    user = g.current_user
    user_id, role = user["user_id"], user["role"]
    sync_profile(user_id, user["username"], role)
    attendance_chat.reconcile(user_id,session_id)
    topic_research.reconcile(user_id,session_id)
    history = load_history(user_id, session_id)
    request_id = uuid4().hex
    try:
        append_turn(user_id, session_id, "user", message, request_id=request_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    state = {
        "user_id": user_id, "role": role, "session_id": session_id, "query": message,
        "request_id": request_id,
        "chat_history": history, "current_date": college_today().isoformat(),
        "timezone": current_app.config.get("COLLEGE_TIMEZONE", "Asia/Kolkata"),
        "retry_count": 0, "terminal": False, "agent_trace": [], "sources": [], "actions": [],
    }
    error, status = None, 200
    try:
        result = _get_graph().invoke(state, config={"recursion_limit": 40})
    except RateLimitError:
        error, status = "Groq rate limit reached. Please wait a few minutes and try again.", 429
    except Exception as exc:
        if isinstance(exc, RuntimeError) and "GROQ_API_KEY" in str(exc):
            error, status = "GROQ_API_KEY is not configured on the backend", 503
        else:
            current_app.logger.warning("Chat processing failed (%s)", type(exc).__name__)
            error, status = "The assistant could not complete this request. Please try again.", 503
    if error:
        committed=attendance_chat.receipt(user_id,request_id) or topic_research.receipt(user_id,request_id)
        if committed:
            result={'final_response':committed['response'],'actions':committed['actions'],'agent_trace':['attendance_receipt'],'sources':[]}
            error,status=None,200
        else:
            result = {"final_response": error, "agent_trace": [], "sources": [], "actions": []}
    trace = result.get("agent_trace", []) + ["memory_write"]
    response = result.get("final_response") or "No answer was available. Please rephrase your question."
    append_turn(user_id, session_id, "assistant", response, request_id=request_id,
                trace=trace, sources=result.get("sources", []), actions=result.get("actions", []), failed=bool(error), confirmation=result.get('confirmation'),
                **({'timetable_context': timetable_context(result['sql_result'])}
                   if not error and isinstance(result.get('sql_result'), dict) and result['sql_result'].get('kind') == 'timetable' else {}))
    body = {"response": response, "session_id": session_id, "timestamp": datetime.now(timezone.utc).isoformat(), "agent_trace": trace,
            "sources": result.get("sources", []), "actions": result.get("actions", [])}
    if result.get('confirmation'):
        body['confirmation']=result['confirmation']
    if error:
        body["error"] = error
    return jsonify(body), status


@chat_bp.get("/history")
@authenticated
def history():
    session_id = request.args.get("session_id")
    if session_id and not valid_session(session_id):
        return jsonify({"error": "Invalid session ID"}), 400
    user_id = g.current_user["user_id"]
    attendance_chat.reconcile(user_id,session_id)
    topic_research.reconcile(user_id,session_id)
    return jsonify(hydrate(load_session(user_id, session_id) if session_id else read_memory(user_id),user_id))


@chat_bp.post('/confirmations/<identifier>')
@authenticated
def confirm_action(identifier):
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict) or set(payload)!={'session_id','decision'} or not valid_session(payload.get('session_id')) or not isinstance(payload.get('decision'),str) or payload['decision'] not in {'confirm','cancel'}:
        return jsonify(error='Provide a session ID and confirm or cancel decision only.'),400
    try:
        result=decide(identifier,g.current_user['user_id'],g.current_user['role'],payload['session_id'],payload['decision'])
        return jsonify(result)
    except (OperationError,ValueError) as exc:
        return jsonify(error=str(exc)),409
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    except Exception:
        current_app.logger.warning('Confirmation result could not be delivered')
        return jsonify(error='Could not retrieve the result. Retry this confirmation; it will not submit twice.'),503


@chat_bp.delete("/history/<session_id>")
@authenticated
def remove_history(session_id):
    if not valid_session(session_id):
        return jsonify({"error": "Invalid session ID"}), 400
    delete_session(g.current_user["user_id"], session_id)
    return jsonify({"message": "Conversation deleted", "session_id": session_id})


@chat_bp.delete('/history')
@authenticated
def clear_all_history():
    count = clear_history(g.current_user['user_id'])
    return jsonify(message='Chat history cleared', deleted_count=count)
