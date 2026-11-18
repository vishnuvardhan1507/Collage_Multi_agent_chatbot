from flask import Blueprint, current_app, g, jsonify, request
from flask_jwt_extended import create_access_token

from auth.utils import verify_password
from auth.access import authenticated
from auth.account_service import consume_limit, normalize_email, register_account, issue_reset, reset_password, revoke_reset, validate_user_id
from auth.mail import mail_configured, send_reset_email
from tools.db_tool import fetch_one


auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")
from auth.avatar import register_avatar_routes
register_avatar_routes(auth_bp)


@auth_bp.post("/login")
def login():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400
    user_id = str(payload.get("user_id", "")).strip()
    password = payload.get("password", "")
    if not user_id or not isinstance(password, str) or not password:
        return jsonify({"error": "user_id and password are required"}), 400
    if len(user_id) > 100 or len(password) > 128:
        return jsonify({"error": "Invalid credentials"}), 401
    try:
        validate_user_id(user_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not consume_limit("login-ip", request.remote_addr or "unknown", 60) or not consume_limit("login-id", user_id, 15):
        return jsonify({"error": "Too many login attempts. Please try again in 15 minutes."}), 429

    db_path = current_app.config["DATABASE_PATH"]
    user = fetch_one(
        "SELECT user_id, username, password_hash, role, token_version FROM users WHERE user_id = ?",
        (user_id,),
        db_path=db_path,
    )
    if user:
        try:
            validate_user_id(user_id, user['role'])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    if not user or not verify_password(user["password_hash"], password):
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_access_token(identity=user["user_id"], additional_claims={"role": user["role"], "ver": user["token_version"]})
    return jsonify({"access_token": token, "role": user["role"], "name": user["username"]})


@auth_bp.get("/me")
@authenticated
def me():
    user = g.current_user
    return jsonify({"user_id": user["user_id"], "name": user["username"], "role": user["role"]})


@auth_bp.post("/register")
def register():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400
    if not consume_limit("register", request.remote_addr or "unknown", 10):
        return jsonify({"error": "Too many attempts. Please try again in 15 minutes."}), 429
    try:
        user_id = register_account(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"message": "Account created. You can now log in.", "user_id": user_id}), 201


@auth_bp.post("/forgot-password")
def forgot_password():
    payload = request.get_json(silent=True)
    try:
        email = normalize_email(payload.get("email") if isinstance(payload, dict) else None)
        user_id = payload.get("user_id", "")
        validate_user_id(user_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not consume_limit("forgot-ip", request.remote_addr or "unknown", 10) or not consume_limit("forgot-email", email, 5):
        return jsonify({"error": "Too many reset requests. Please try again in 15 minutes."}), 429
    user = fetch_one("SELECT user_id FROM users WHERE user_id = ? AND email = ? COLLATE NOCASE", (user_id, email))
    if not user:
        return jsonify({"error": "This email is not registered for this account ID."}), 400
    if not mail_configured():
        return jsonify({"error": "Password reset email is not available yet. Please contact your institution."}), 503
    if not consume_limit("forgot-cooldown", email, 1, window=60):
        return jsonify({"error": "Please wait 60 seconds before requesting another OTP."}), 429
    if user:
        token = issue_reset(user["user_id"])
        try:
            send_reset_email(email, token)
        except Exception as exc:
            revoke_reset(user["user_id"], token)
            current_app.logger.warning("Password reset email delivery failed (%s)", type(exc).__name__)
    # Never return OTPs via the API.
    return jsonify({"message": "A six-digit OTP has been requested for this account. Check your inbox and spam folder.", "resend_after": 60}), 202


@auth_bp.post("/reset-password")
def reset():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400
    if not consume_limit("reset-ip", request.remote_addr or "unknown", 20):
        return jsonify({"error": "Too many attempts. Please try again in 15 minutes."}), 429
    try:
        reset_user_id = reset_password(payload.get("email"), payload.get("otp"), payload.get("password"), payload.get("user_id"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"message": f"Your password has been reset for {reset_user_id}. Log in with your new password.", "user_id": reset_user_id})
