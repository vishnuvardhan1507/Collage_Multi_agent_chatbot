from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import create_access_token, get_jwt, get_jwt_identity, jwt_required

from auth.utils import verify_password
from tools.db_tool import fetch_one


auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@auth_bp.post("/login")
def login():
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("user_id", "")).strip()
    password = payload.get("password", "")
    if not user_id or not password:
        return jsonify({"error": "user_id and password are required"}), 400

    db_path = current_app.config["DATABASE_PATH"]
    user = fetch_one(
        "SELECT user_id, username, password_hash, role FROM users WHERE user_id = ?",
        (user_id,),
        db_path=db_path,
    )
    if not user or not verify_password(user["password_hash"], password):
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_access_token(identity=user["user_id"], additional_claims={"role": user["role"]})
    return jsonify({"access_token": token, "role": user["role"], "name": user["username"]})


@auth_bp.get("/me")
@jwt_required()
def me():
    user_id = get_jwt_identity()
    claims = get_jwt()
    db_path = current_app.config["DATABASE_PATH"]
    user = fetch_one("SELECT user_id, username, role FROM users WHERE user_id = ?", (user_id,), db_path=db_path)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"user_id": user["user_id"], "name": user["username"], "role": claims.get("role", user["role"])})
