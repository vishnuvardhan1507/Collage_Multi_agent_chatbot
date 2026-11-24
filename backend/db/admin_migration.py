"""Version 21: private administration, preserving all existing identities."""
import sqlite3
import re
from contextlib import closing


def migrate_admin(path):
    from db.migrate import backup
    with closing(sqlite3.connect(path)) as conn:
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=21').fetchone():
            return None
        saved = backup(conn, path)
        # SQLite's documented table-rebuild procedure. Do not rename the old parent:
        # child foreign keys must continue to reference users.
        conn.execute('PRAGMA foreign_keys=OFF')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM schema_migrations WHERE version=21').fetchone():
                return saved
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
            indexes = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='users' AND type='index' AND sql IS NOT NULL").fetchall()
            schema = re.sub(r'^CREATE TABLE\s+(?:"users"|`users`|\[users\]|users)',
                            'CREATE TABLE admin_users_new',schema,count=1,flags=re.I)
            schema = schema.replace("'student','faculty'", "'student','faculty','admin'")
            conn.execute(schema)
            conn.execute('INSERT INTO admin_users_new SELECT * FROM users')
            conn.execute('DROP TABLE users')
            conn.execute('ALTER TABLE admin_users_new RENAME TO users')
            for (sql,) in indexes:
                conn.execute(sql)
            conn.execute('ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))')
            conn.execute('''CREATE TABLE admin_audit(id INTEGER PRIMARY KEY, actor TEXT NOT NULL,
                action TEXT NOT NULL,target TEXT NOT NULL,reason TEXT NOT NULL,
                before_json TEXT,after_json TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            conn.execute('''CREATE TABLE admin_revisions(resource TEXT NOT NULL,record_key TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(resource,record_key))''')
            conn.execute('''CREATE TABLE admin_document_revisions(id INTEGER PRIMARY KEY,name TEXT NOT NULL,
                content TEXT NOT NULL,previous_hash TEXT NOT NULL,content_hash TEXT NOT NULL,
                actor TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            conn.execute('''CREATE TABLE worker_heartbeats(name TEXT PRIMARY KEY,last_seen TEXT NOT NULL,status TEXT NOT NULL)''')
            conn.execute('INSERT INTO schema_migrations(version) VALUES(21)')
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Admin migration foreign-key validation failed')
        conn.execute('PRAGMA foreign_keys=ON')
    return saved
