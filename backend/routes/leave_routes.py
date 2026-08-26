from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required

from tools.db_tool import fetch_all, fetch_one, get_connection


leave_bp = Blueprint("leaves", __name__, url_prefix="/api/leaves")


def _db_path():
    return current_app.config["DATABASE_PATH"]


def _faculty_scope_clause():
    return """
    (
        s.advisor_faculty_id = ?
        OR EXISTS (
            SELECT 1
            FROM course_registrations cr
            JOIN courses c ON c.course_id = cr.course_id
            WHERE cr.student_id = lr.student_id
              AND c.faculty_id = ?
        )
    )
    """


@leave_bp.get("")
@jwt_required()
def list_leaves():
    user_id = get_jwt_identity()
    role = get_jwt().get("role")
    status = request.args.get("status")

    if role == "student":
        params = [user_id]
        where = "lr.student_id = ?"
    else:
        params = [user_id, user_id]
        where = _faculty_scope_clause()

    if status:
        where += " AND lr.status = ?"
        params.append(status)

    rows = fetch_all(
        f"""
        SELECT
            lr.leave_id,
            lr.student_id,
            s.name AS student_name,
            lr.from_date,
            lr.to_date,
            lr.reason,
            lr.status,
            lr.reviewed_by,
            lr.created_at
        FROM leave_requests lr
        JOIN students s ON s.student_id = lr.student_id
        WHERE {where}
        ORDER BY lr.created_at DESC, lr.leave_id DESC
        """,
        params,
        db_path=_db_path(),
    )
    return jsonify({"leaves": rows})


@leave_bp.post("")
@jwt_required()
def create_leave():
    user_id = get_jwt_identity()
    role = get_jwt().get("role")
    if role != "student":
        return jsonify({"error": "Only students can submit leave requests"}), 403

    payload = request.get_json(silent=True) or {}
    from_date = (payload.get("from_date") or "").strip()
    to_date = (payload.get("to_date") or "").strip()
    reason = (payload.get("reason") or "").strip()
    if not from_date or not to_date or not reason:
        return jsonify({"error": "from_date, to_date, and reason are required"}), 400

    with get_connection(_db_path()) as conn:
        cur = conn.execute(
            """
            INSERT INTO leave_requests(student_id, from_date, to_date, reason, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (user_id, from_date, to_date, reason),
        )
        conn.commit()
        leave_id = cur.lastrowid

    row = fetch_one(
        """
        SELECT leave_id, student_id, from_date, to_date, reason, status, reviewed_by, created_at
        FROM leave_requests
        WHERE leave_id = ?
        """,
        (leave_id,),
        db_path=_db_path(),
    )
    return jsonify({"leave": row}), 201


@leave_bp.patch("/<int:leave_id>/review")
@jwt_required()
def review_leave(leave_id: int):
    user_id = get_jwt_identity()
    role = get_jwt().get("role")
    if role != "faculty":
        return jsonify({"error": "Only faculty can review leave requests"}), 403

    payload = request.get_json(silent=True) or {}
    status = (payload.get("status") or "").strip().lower()
    if status not in {"approved", "rejected"}:
        return jsonify({"error": "status must be approved or rejected"}), 400

    allowed = fetch_one(
        f"""
        SELECT lr.leave_id
        FROM leave_requests lr
        JOIN students s ON s.student_id = lr.student_id
        WHERE lr.leave_id = ?
          AND lr.status = 'pending'
          AND {_faculty_scope_clause()}
        """,
        (leave_id, user_id, user_id),
        db_path=_db_path(),
    )
    if not allowed:
        return jsonify({"error": "No pending leave request found in your review scope"}), 404

    with get_connection(_db_path()) as conn:
        conn.execute(
            "UPDATE leave_requests SET status = ?, reviewed_by = ? WHERE leave_id = ?",
            (status, user_id, leave_id),
        )
        conn.commit()

    row = fetch_one(
        """
        SELECT
            lr.leave_id,
            lr.student_id,
            s.name AS student_name,
            lr.from_date,
            lr.to_date,
            lr.reason,
            lr.status,
            lr.reviewed_by,
            lr.created_at
        FROM leave_requests lr
        JOIN students s ON s.student_id = lr.student_id
        WHERE lr.leave_id = ?
        """,
        (leave_id,),
        db_path=_db_path(),
    )
    return jsonify({"leave": row})
