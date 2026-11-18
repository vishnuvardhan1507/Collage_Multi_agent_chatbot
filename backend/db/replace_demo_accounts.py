"""Explicitly replace student/faculty accounts with branch-based demonstration data.

Stop the backend before running. Database and conversation backups are made first.
This command is intentionally separate from ordinary migrations and demo insertion.
"""
import argparse
from contextlib import closing
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from filelock import FileLock
from auth.account_service import DEPARTMENTS
from config import Config
from db.migrate import backup, migrate


from db.init_db import seed_accounts, seed_courses


def replace_accounts(database, memory_dir):
    database, memory_dir = Path(database).resolve(), Path(memory_dir).resolve()
    migrate(database)
    archive = memory_dir.parent / 'backups' / (memory_dir.name + '.before-account-replacement-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    if memory_dir.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(memory_dir, archive)
    with closing(sqlite3.connect(database)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        saved = backup(conn, database)
        old_ids = {r[0] for r in conn.execute('SELECT user_id FROM users')}
        version = conn.execute('SELECT COALESCE(MAX(token_version),0)+1 FROM users').fetchone()[0]
        departments = {dept: f'FAC{i+1:03d}' for i, dept in enumerate(DEPARTMENTS)}
        unknown = {r[0] for r in conn.execute('SELECT DISTINCT department FROM courses')} - set(departments)
        if unknown:
            raise ValueError(f'Unrecognized existing course branches; replacement aborted: {unknown}')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            for table in ['alert_occurrences','alert_condition_state','alert_rules','attendance_chat_receipts','notification_outbox','attendance_audit','attendance_marks','attendance_sessions','attendance_warnings','attendance_balances']:
                conn.execute(f'DELETE FROM {table}')
            for table in ['chat_confirmations', 'user_avatars', 'password_reset_tokens', 'password_reset_otps', 'auth_rate_limits', 'classroom_bookings', 'leave_requests', 'results', 'attendance', 'course_registrations']:
                conn.execute(f'DELETE FROM {table}')
            for table in ['course_timetable', 'prerequisites', 'courses', 'students', 'faculty', 'users', 'demo_records']:
                conn.execute(f'DELETE FROM {table}')
            seed_accounts(conn, token_version=version)
            seed_courses(conn)
            from tools.attendance import migrate_attendance
            from db.migrate import migrate_alerts
            migrate_attendance(conn)
            migrate_alerts(conn)
            if conn.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Foreign-key validation failed; replacement rolled back.')
        new_ids = {r[0] for r in conn.execute('SELECT user_id FROM users')}
    for uid in old_ids | new_ids:
        target = (memory_dir / f'{uid}.json').resolve()
        if target.parent != memory_dir:
            raise ValueError('Invalid conversation path; cleanup stopped.')
        if target.exists():
            with FileLock(str(target) + '.lock'):
                target.unlink()
    return {'database_backup': saved, 'memory_backup': str(archive) if archive.exists() else None,
            'branches': len(DEPARTMENTS), 'faculty': len(DEPARTMENTS), 'students': len(DEPARTMENTS)*2}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace-existing', action='store_true', required=True)
    args = parser.parse_args()
    print(replace_accounts(Config.DATABASE_PATH, Config.CHAT_MEMORY_DIR))
