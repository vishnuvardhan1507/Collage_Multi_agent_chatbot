from flask import Flask, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager

from auth.routes import auth_bp
from config import Config
from routes.chat_routes import chat_bp
from routes.leave_routes import leave_bp
from tools.rag_tool import ensure_vector_store


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    CORS(app, origins=[origin.strip() for origin in app.config["CORS_ORIGINS"].split(",")], supports_credentials=True)
    JWTManager(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(leave_bp)

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
    create_app().run(debug=True)
