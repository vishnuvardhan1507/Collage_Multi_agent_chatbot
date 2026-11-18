"""Administrator-only local command for linking an existing account's recovery email."""
import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from auth.account_service import normalize_email
from config import Config
from db.migrate import backup, migrate
from tools.db_tool import get_connection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    email = normalize_email(args.email)
    migrate(Config.DATABASE_PATH)
    app = Flask(__name__)
    app.config.from_object(Config)
    with app.app_context(), closing(get_connection()) as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (args.user_id,)).fetchone():
            parser.error("User ID does not exist.")
        saved = backup(conn, Config.DATABASE_PATH)
        try:
            with conn:
                conn.execute("UPDATE users SET email=? WHERE user_id=?", (email, args.user_id))
                conn.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (args.user_id,))
        except sqlite3.IntegrityError:
            parser.error("That email is already linked to another account.")
        print("Recovery email updated. Database backup:", saved)


if __name__ == "__main__":
    main()
