"""Registration and single-use, expiring password reset tokens."""
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import closing

from flask import current_app
from werkzeug.security import generate_password_hash

from tools.db_tool import get_connection

DEPARTMENTS = ("Computer Science", "Electronics", "Mathematics", "Management", "Information Technology", "Mechanical Engineering", "Civil Engineering")


def validate_user_id(user_id, role=None):
    if not isinstance(user_id, str):
        raise ValueError('Enter your account ID.')
    prefix = {'student': 'STU', 'faculty': 'FAC', 'admin': 'ADM'}.get(role)
    if prefix and not user_id.startswith(prefix):
        raise ValueError(f"Username should start with {prefix} for {role}.")
    if not prefix and not user_id.startswith(('STU', 'FAC', 'ADM')):
        raise ValueError("Username should start with STU for students or FAC for faculty.")
    if not re.fullmatch(r'(STU|FAC|ADM)[A-Za-z0-9_-]{1,37}', user_id):
        raise ValueError("Username must have STU or FAC followed by letters, numbers, underscores or hyphens (4–40 characters).")


def normalize_email(value):
    if not isinstance(value, str):
        raise ValueError("Enter a valid email address.")
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}", email):
        raise ValueError("Enter a valid email address.")
    return email


def validate_password(password):
    if not isinstance(password, str) or len(password) < 6 or len(password) > 128 or not password.strip():
        raise ValueError("Use a password between 6 and 128 characters.")


def consume_limit(bucket, key, limit, window=900):
    fingerprint = hmac.new(current_app.config["JWT_SECRET_KEY"].encode(), f"{bucket}:{key}".encode(), hashlib.sha256).hexdigest()
    now = int(time.time())
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM auth_rate_limits WHERE expires_at <= ?", (now,))
        row = conn.execute("SELECT attempts FROM auth_rate_limits WHERE bucket_key=?", (fingerprint,)).fetchone()
        if row and row["attempts"] >= limit:
            return False
        conn.execute("""INSERT INTO auth_rate_limits VALUES (?,1,?)
            ON CONFLICT(bucket_key) DO UPDATE SET attempts=attempts+1""", (fingerprint, now + window))
    return True


def register_account(data):
    user_id = data.get("user_id", "")
    name = data.get("full_name", "")
    role, department = data.get("role"), data.get("department")
    if not isinstance(user_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", user_id.strip()):
        raise ValueError("ID must contain 3–40 letters, numbers, underscores or hyphens.")
    user_id = user_id.strip()
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 100:
        raise ValueError("Enter your full name (2–100 characters).")
    if role not in {"student", "faculty"} or department not in DEPARTMENTS:
        raise ValueError("Choose a valid account type and department.")
    validate_user_id(user_id, role)
    email = normalize_email(data.get("email"))
    validate_password(data.get("password"))
    password_hash = generate_password_hash(data["password"])
    try:
        with closing(get_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO users(user_id,username,password_hash,role,email) VALUES (?,?,?,?,?)", (user_id, name.strip(), password_hash, role, email))
            if role == "student":
                conn.execute("INSERT INTO students(student_id,name,department,semester) VALUES (?,?,?,1)", (user_id, name.strip(), department))
            else:
                conn.execute("INSERT INTO faculty(faculty_id,name,department,email) VALUES (?,?,?,?)", (user_id, name.strip(), department, email))
    except sqlite3.IntegrityError as exc:
        raise ValueError("An account with this ID or email already exists. Log in or reset your password.") from exc
    return user_id


def issue_reset(user_id):
    with closing(get_connection()) as conn:
        user = conn.execute("SELECT role,is_active FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user or not user["is_active"] or user["role"] == "admin":
            raise ValueError("Account recovery is unavailable for this account.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    digest = _otp_digest(user_id, code)
    now = int(time.time())
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM password_reset_otps WHERE user_id=? OR expires_at<=?", (user_id, now))
        conn.execute("INSERT INTO password_reset_otps(user_id,code_hash,expires_at,created_at) VALUES (?,?,?,?)",
                     (user_id, digest, now + current_app.config.get("PASSWORD_RESET_TTL_SECONDS", 600), now))
    return code


def _otp_digest(user_id, code):
    return hmac.new(current_app.config['JWT_SECRET_KEY'].encode(), f'reset-otp:{user_id}:{code}'.encode(), hashlib.sha256).hexdigest()


def revoke_reset(user_id, code):
    with closing(get_connection()) as conn, conn:
        conn.execute("DELETE FROM password_reset_otps WHERE user_id=? AND code_hash=?", (user_id, _otp_digest(user_id, code)))


def reset_password(email, code, password, user_id):
    validate_user_id(user_id)
    validate_password(password)
    email = normalize_email(email)
    error = "This OTP is invalid or expired. Request a new code if needed."
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("Enter the six-digit OTP from your email.")
    password_hash = generate_password_hash(password)
    now = int(time.time())
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM users WHERE user_id=? AND email=? COLLATE NOCASE AND is_active=1 AND role!='admin'", (user_id, email)).fetchone():
            raise ValueError("This email is not registered for this account ID.")
        row = conn.execute("""SELECT o.* FROM password_reset_otps o JOIN users u ON u.user_id=o.user_id
            WHERE u.user_id=? AND u.email=? COLLATE NOCASE AND o.expires_at>? AND o.attempts<5""", (user_id, email, now)).fetchone()
        valid = row is not None and hmac.compare_digest(row['code_hash'], _otp_digest(row['user_id'], code))
        if valid:
            conn.execute("UPDATE users SET password_hash=?, token_version=token_version+1 WHERE user_id=?", (password_hash, row["user_id"]))
            conn.execute("DELETE FROM password_reset_otps WHERE user_id=?", (row['user_id'],))
            conn.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (row['user_id'],))
        elif row:
            conn.execute("UPDATE password_reset_otps SET attempts=attempts+1 WHERE user_id=?", (row['user_id'],))
    # Raise after committing so incorrect attempts cannot roll back their counter.
    if not valid:
        raise ValueError(error)
    return row['user_id']
