from flask import Blueprint, g, jsonify, request

from auth.access import authenticated
from tools.db_tool import FACULTY_SCOPE, fetch_all, fetch_one
from tools.operations import OperationError, execute_operation, college_today


leave_bp = Blueprint("leaves", __name__, url_prefix="/api/leaves")


@leave_bp.get("")
@authenticated
def list_leaves():
    user = g.current_user
    if user["role"] == "student":
        where, params = "lr.student_id = ?", [user["user_id"]]
    else:
        where, params = f"lr.student_id IN ({FACULTY_SCOPE})", [user["user_id"]]
    status = request.args.get("status")
    if status:
        if status not in {"pending", "approved", "rejected"}:
            return jsonify({"error": "Invalid leave status"}), 400
        where += " AND lr.status = ?"
        params.append(status)
    rows = fetch_all(f"""SELECT lr.*, s.name AS student_name FROM leave_requests lr
        JOIN students s ON s.student_id = lr.student_id WHERE {where}
        ORDER BY lr.created_at DESC, lr.leave_id DESC""", params)
    today = college_today().isoformat()
    for row in rows:
        row["can_delete"] = user["role"] == "student" and row["to_date"] >= today
    return jsonify({"leaves": rows})


def _perform(operation, status_code):
    user = g.current_user
    try:
        result = execute_operation(operation, user["user_id"], user["role"])
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except OperationError as exc:
        return jsonify({"error": str(exc)}), 400
    row = fetch_one("""SELECT lr.*, s.name AS student_name FROM leave_requests lr
        JOIN students s ON s.student_id = lr.student_id WHERE lr.leave_id = ?""", (result["record_id"],))
    return jsonify({"leave": row}), status_code


@leave_bp.post("")
@authenticated
def create_leave():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400
    return _perform({**payload, "type": "submit_leave"}, 201)


@leave_bp.patch("/<int:leave_id>/review")
@authenticated
def review_leave(leave_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400
    return _perform({**payload, "type": "review_leave", "leave_id": leave_id}, 200)


@leave_bp.delete("/<int:leave_id>")
@authenticated
def delete_leave(leave_id):
    return _perform({"type": "delete_leave", "leave_id": leave_id}, 200)
