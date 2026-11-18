"""Replace academic course assignments with ten-course branch curricula.

Accounts, passwords, recovery emails, avatars, leaves, facilities and conversations
are preserved. Run with the backend stopped. A database backup is mandatory.
"""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config
from db.migrate import backup, migrate


from db.init_db import CURRICULA, COMMON, SUFFIXES, STATUSES, seed_courses


def assign_courses(database):
    database = Path(database).resolve()
    migrate(database)
    with closing(sqlite3.connect(database)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        students = conn.execute('SELECT student_id,department,advisor_faculty_id FROM students ORDER BY student_id').fetchall()
        if not students:
            raise ValueError('No students found; course replacement aborted.')
        owners = {}
        for dept in CURRICULA:
            faculty = conn.execute('SELECT faculty_id FROM faculty WHERE department=?', (dept,)).fetchall()
            if len(faculty) != 1:
                raise ValueError(f'{dept} must have exactly one assigned faculty member before replacement.')
            owners[dept] = faculty[0][0]
        for sid, dept, advisor in students:
            if dept not in owners or advisor != owners[dept]:
                raise ValueError(f'{sid} has an unknown branch or mismatched assigned faculty; replacement aborted.')
        saved = backup(conn, database)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM attendance_sessions LIMIT 1').fetchone():
                raise ValueError('Daily attendance exists; course replacement would lose its history. Replacement aborted.')
            conn.execute('DELETE FROM attendance_balances')
            for table in ['results', 'attendance', 'course_registrations', 'course_timetable', 'prerequisites', 'courses']:
                conn.execute(f'DELETE FROM {table}')
            conn.execute("DELETE FROM demo_records WHERE table_name IN ('results','attendance','course_registrations','course_timetable','prerequisites','courses')")
            seed_courses(conn)
            from tools.attendance import migrate_attendance
            migrate_attendance(conn)
        return {'backup': saved, 'students':len(students), 'courses':70, 'registrations':len(students)*10,
                'per_student':{'completed':4,'currently_enrolled':4,'to_be_completed':2}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace-existing', action='store_true', required=True)
    parser.parse_args()
    print(assign_courses(Config.DATABASE_PATH))
