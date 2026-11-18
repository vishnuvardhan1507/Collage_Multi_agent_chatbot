"""Opt-in Groq read-only checks; conversation state and database are temporary."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RAG_USE_CHROMA"] = "0"
os.environ["ALLOW_MODEL_DOWNLOAD"] = "0"

from flask_jwt_extended import create_access_token
from app import create_app
from config import Config


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not Config.GROQ_API_KEY or Config.GROQ_API_KEY.startswith("your_"):
        print("SKIP: Groq key is not configured.")
        return
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        database = directory / "live.db"
        shutil.copyfile(Config.DATABASE_PATH, database)
        app = create_app({"DATABASE_PATH": str(database), "CHAT_MEMORY_DIR": str(directory / "memory")})
        client = app.test_client()
        checks = [
            ("STU001", "student", "Show my enrolled and pending courses."),
            ("STU001", "student", "What will my attendance be if I take leave tomorrow, and will I meet the minimum attendance policy?"),
            ("FAC001", "faculty", "Display results of all students in Machine Learning."),
        ]
        for user, role, question in checks:
            with app.app_context():
                token = create_access_token(identity=user, additional_claims={"role": role})
            result = client.post("/api/chat", json={"message": question, "session_id": "live_check"}, headers={"Authorization": f"Bearer {token}"})
            body = result.get_json()
            print(role, result.status_code, body.get("agent_trace"), flush=True)
            print(body.get("response", body.get("error", "")), flush=True)
            if result.status_code != 200:
                print("STOP: external service check unavailable.", flush=True)
                break


if __name__ == "__main__":
    main()
