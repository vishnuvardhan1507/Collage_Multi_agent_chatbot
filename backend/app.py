from flask import Flask, jsonify
import os
from flask_cors import CORS
from flask_jwt_extended import JWTManager

from auth.routes import auth_bp
from config import Config
from routes.chat_routes import chat_bp
from routes.leave_routes import leave_bp
from routes.attendance_routes import attendance_bp
from routes.alert_routes import alert_bp
from tools.notifications import register_worker
from tools.rag_tool import ensure_vector_store


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)
    secret = app.config.get("JWT_SECRET_KEY")
    if not isinstance(secret, str) or len(secret) < 32 or secret in {
        "dev-secret-change-me-please-set-a-real-32-byte-key",
        "replace_with_a_random_secret_at_least_32_bytes_long",
    }:
        raise RuntimeError("Set JWT_SECRET_KEY to a private random value of at least 32 characters.")

    CORS(app, origins=[origin.strip() for origin in app.config["CORS_ORIGINS"].split(",")], supports_credentials=True)
    JWTManager(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(leave_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(alert_bp)
    register_worker(app)

    with app.app_context():
        try:
            ensure_vector_store()
        except Exception as exc:
            app.logger.warning("RAG store initialization skipped: %s", exc)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    return app


if __name__ == "__main__":
    create_app().run(debug=True, port=int(os.getenv("PORT", "5001")))
