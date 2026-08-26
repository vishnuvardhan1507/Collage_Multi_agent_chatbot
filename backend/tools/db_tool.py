import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

from flask import current_app, has_app_context


def _db_path(db_path: Optional[str] = None) -> str:
    if db_path:
        return db_path
    if has_app_context():
        return current_app.config["DATABASE_PATH"]
    return str(Path(__file__).resolve().parents[1] / "db" / "college.db")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def fetch_one(sql: str, params: Iterable[Any] = (), db_path: Optional[str] = None):
    with get_connection(db_path) as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None


def fetch_all(sql: str, params: Iterable[Any] = (), db_path: Optional[str] = None):
    with get_connection(db_path) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]


def execute_query(sql: str, user_id: str, role: str):
    if not sql or not isinstance(sql, str):
        raise PermissionError("No executable SQL was provided.")

    normalized = " ".join(sql.lower().split())
    if ";" in normalized.rstrip(";"):
        raise PermissionError("Multiple SQL statements are not allowed.")
    if any(token in normalized for token in ("drop ", "alter ", "pragma ", "attach ", "detach ")):
        raise PermissionError("Administrative SQL is not allowed.")
    if "attendance" in normalized and any(op in normalized for op in ("update attendance", "delete from attendance", "drop", "alter")):
        raise PermissionError("Attendance table is read-only for all agent actions.")
    if role == "student" and user_id not in sql:
        raise PermissionError("Query does not scope to requester.")

    statement = normalized.split(" ", 1)[0]
    if statement not in {"select", "insert", "update"}:
        raise PermissionError("Only SELECT, permitted INSERT, and permitted UPDATE statements may execute.")
    if statement == "insert":
        if role == "student":
            allowed = normalized.startswith("insert into leave_requests") or normalized.startswith("insert into course_registrations")
        elif role == "faculty":
            allowed = normalized.startswith("insert into classroom_bookings")
        else:
            allowed = False
        if not allowed:
            raise PermissionError("This INSERT is not one of the permitted agent write operations.")
        if user_id not in sql:
            raise PermissionError("Write query does not scope to requester.")
    if statement == "update":
        if role != "faculty" or not normalized.startswith("update leave_requests"):
            raise PermissionError("Only scoped faculty updates to leave request status are permitted.")
        set_clause = normalized.split(" set ", 1)[1].split(" where ", 1)[0] if " set " in normalized and " where " in normalized else ""
        if not set_clause or any(column in set_clause for column in ("reason", "from_date", "to_date", "student_id", "created_at")):
            raise PermissionError("Leave request updates may only change status and reviewed_by.")
        if user_id not in sql:
            raise PermissionError("Write query does not scope to requester.")

    with get_connection() as conn:
        cur = conn.execute(sql)
        if statement == "select":
            return [dict(row) for row in cur.fetchall()]
        conn.commit()
        return {"rows_affected": cur.rowcount, "lastrowid": cur.lastrowid}
