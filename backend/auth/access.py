from functools import wraps

from flask import g, jsonify
from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request

from tools.db_tool import fetch_one


def authenticated(function):
    """Tokens identify users; current database records determine authorization."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        verify_jwt_in_request()
        user = fetch_one("SELECT user_id, username, role, token_version FROM users WHERE user_id = ?", (get_jwt_identity(),))
        if not user or user["role"] not in {"student", "faculty"}:
            return jsonify({"error": "Account is no longer available"}), 401
        if get_jwt().get("ver", 0) != user["token_version"]:
            return jsonify({"error": "Your password has changed. Please log in again."}), 401
        g.current_user = user
        return function(*args, **kwargs)
    return wrapped
