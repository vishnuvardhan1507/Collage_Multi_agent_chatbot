"""Non-destructive, repeatable schema migration and opt-in demonstration data."""
import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def backup(conn, path):
    target = Path(path).with_name(Path(path).name + "." + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak")
    with closing(sqlite3.connect(target)) as destination:
        conn.backup(destination)
    return str(target)


def migrate_transport(conn):
    from db.init_db import TRANSPORT_SERVICES, BUS_ROUTES, BUS_STOPS, BUS_LOCALITY_ALIASES
    conn.execute('''CREATE TABLE IF NOT EXISTS transport_services(
        service_id TEXT PRIMARY KEY, campus TEXT NOT NULL, timezone TEXT NOT NULL,
        operating_days TEXT NOT NULL, campus_arrival TEXT NOT NULL, campus_departure TEXT NOT NULL,
        planning_radius_km REAL NOT NULL CHECK(planning_radius_km>=0), radius_note TEXT NOT NULL,
        arrive_before_minutes INTEGER NOT NULL CHECK(arrive_before_minutes>=0), currency TEXT NOT NULL,
        fare_basis TEXT NOT NULL, provenance TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS bus_routes(
        bus_id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES transport_services(service_id),
        route_name TEXT NOT NULL, monthly_round_trip_fare INTEGER NOT NULL CHECK(monthly_round_trip_fare>=0))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS bus_stops(
        bus_id TEXT NOT NULL REFERENCES bus_routes(bus_id), stop_sequence INTEGER NOT NULL CHECK(stop_sequence>0),
        locality TEXT NOT NULL, stop_name TEXT NOT NULL, approximate_radius_km REAL NOT NULL CHECK(approximate_radius_km>=0),
        morning_pickup TEXT NOT NULL CHECK(morning_pickup GLOB '[0-2][0-9]:[0-5][0-9]' AND morning_pickup<'24:00'),
        afternoon_dropoff TEXT NOT NULL CHECK(afternoon_dropoff GLOB '[0-2][0-9]:[0-5][0-9]' AND afternoon_dropoff<'24:00'),
        PRIMARY KEY(bus_id,stop_sequence))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS bus_locality_aliases(
        bus_id TEXT NOT NULL REFERENCES bus_routes(bus_id), alias TEXT NOT NULL COLLATE NOCASE,
        stop_sequence INTEGER, PRIMARY KEY(bus_id,alias),
        FOREIGN KEY(bus_id,stop_sequence) REFERENCES bus_stops(bus_id,stop_sequence))''')
    conn.execute('CREATE INDEX IF NOT EXISTS bus_alias_lookup ON bus_locality_aliases(alias)')
    for table, rows in [('transport_services',TRANSPORT_SERVICES),('bus_routes',BUS_ROUTES),
                        ('bus_stops',BUS_STOPS),('bus_locality_aliases',BUS_LOCALITY_ALIASES)]:
        for row in rows:
            conn.execute(f'INSERT INTO {table} VALUES({",".join("?" for _ in row)}) ON CONFLICT DO NOTHING',row)
    conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES (11)')


def migrate_alerts(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS alert_rules(
        id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
        kind TEXT NOT NULL CHECK(kind IN ('attendance_student','attendance_faculty','classroom_free','menu_match','leave_student','leave_faculty')),
        config_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','completed','expired')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('CREATE INDEX IF NOT EXISTS alert_rule_owner ON alert_rules(user_id,active,id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS alert_condition_state(
        rule_id INTEGER NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE, subject_key TEXT NOT NULL,
        matching INTEGER NOT NULL CHECK(matching IN (0,1)), updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(rule_id,subject_key))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS alert_occurrences(
        id INTEGER PRIMARY KEY, rule_id INTEGER NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL REFERENCES users(user_id), event_key TEXT NOT NULL UNIQUE,
        subject TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('CREATE INDEX IF NOT EXISTS alert_occurrence_owner ON alert_occurrences(user_id,id DESC)')
    # Personal all-course 80% rules replace future fixed-threshold warnings.
    conn.execute('''INSERT INTO alert_rules(user_id,kind,config_json)
        SELECT u.user_id,'attendance_student','{"threshold":80,"course_id":null}' FROM users u
        WHERE u.role='student' AND NOT EXISTS(SELECT 1 FROM alert_rules r WHERE r.user_id=u.user_id AND r.kind='attendance_student' AND json_extract(r.config_json,'$.course_id') IS NULL)''')
    conn.execute('''INSERT INTO alert_rules(user_id,kind,config_json)
        SELECT u.user_id,'leave_student','{"statuses":["approved","rejected"]}' FROM users u
        WHERE u.role='student' AND NOT EXISTS(SELECT 1 FROM alert_rules r WHERE r.user_id=u.user_id AND r.kind='leave_student')''')
    # Prevent queued legacy fixed-80% mail from contradicting the new personal settings.
    conn.execute("UPDATE notification_outbox SET status='suppressed' WHERE event_key LIKE 'attendance:%' AND status IN ('pending','failed')")
    conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES (14)')
    migrate_class_reminders(conn)
    migrate_missing_attendance(conn)


def migrate_class_reminders(conn):
    _extend_alert_kind(conn, 15, 'class_reminder')


def migrate_missing_attendance(conn):
    _extend_alert_kind(conn, 16, 'attendance_missing')


def _extend_alert_kind(conn, version, kind):
    """Extend the parent constraint without cascading away its private history."""
    if conn.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
        return
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='alert_rules'").fetchone()[0]
    children = []
    for table in ('alert_condition_state', 'alert_occurrences'):
        definitions = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL ORDER BY type DESC", (table,)).fetchall()
        conn.execute(f'CREATE TEMP TABLE saved_{table} AS SELECT * FROM {table}')
        children.append((table, definitions))
        conn.execute(f'DROP TABLE {table}')
    indexes = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='alert_rules' AND type='index' AND sql IS NOT NULL").fetchall()
    replacement = re.sub(r'CREATE TABLE\s+"?alert_rules"?\s*\(', 'CREATE TABLE alert_rules_v15(', schema, count=1)
    conn.execute(replacement.replace("'leave_faculty'", f"'leave_faculty','{kind}'"))
    conn.execute('INSERT INTO alert_rules_v15 SELECT * FROM alert_rules')
    conn.execute('DROP TABLE alert_rules')
    conn.execute('ALTER TABLE alert_rules_v15 RENAME TO alert_rules')
    for (sql,) in indexes:
        conn.execute(sql)
    for table, definitions in children:
        for (sql,) in definitions:
            conn.execute(sql)
        conn.execute(f'INSERT INTO {table} SELECT * FROM saved_{table}')
        conn.execute(f'DROP TABLE saved_{table}')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise sqlite3.IntegrityError('Class reminder migration failed foreign-key validation.')
    conn.execute('INSERT INTO schema_migrations(version) VALUES (?)', (version,))


def _migrate_base(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError("Initialize a NEW database first; migration requires an existing database.")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
        if exists and conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version BETWEEN 1 AND 16").fetchone()[0] == 16:
            return None
        saved = backup(conn, path)
        if exists and conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version BETWEEN 1 AND 14').fetchone()[0] == 14:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                migrate_class_reminders(conn)
                migrate_missing_attendance(conn)
            return saved
        from tools.attendance import migrate_attendance
        from tools.attendance_chat import migrate_receipts
        if exists and conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version BETWEEN 1 AND 12').fetchone()[0] == 12:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                migrate_receipts(conn)
                migrate_alerts(conn)
            return saved
        if exists and conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version BETWEEN 1 AND 11').fetchone()[0] == 11:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                migrate_attendance(conn)
                migrate_receipts(conn)
                migrate_alerts(conn)
            return saved
        if exists and conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version BETWEEN 1 AND 10').fetchone()[0] == 10:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                migrate_transport(conn)
                migrate_attendance(conn)
                migrate_receipts(conn)
                migrate_alerts(conn)
            return saved
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("""CREATE TABLE IF NOT EXISTS course_timetable(
                timetable_id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id TEXT NOT NULL REFERENCES courses(course_id),
                weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
                start_time TEXT NOT NULL, end_time TEXT NOT NULL,
                effective_from TEXT NOT NULL, effective_to TEXT NOT NULL,
                is_demo INTEGER NOT NULL DEFAULT 0 CHECK(is_demo IN (0,1)),
                CHECK(start_time < end_time), CHECK(effective_from <= effective_to),
                UNIQUE(course_id, weekday, start_time, effective_from))""")
            conn.execute("CREATE TABLE IF NOT EXISTS demo_records(table_name TEXT, record_key TEXT, PRIMARY KEY(table_name, record_key))")
            conn.execute("CREATE INDEX IF NOT EXISTS booking_overlap_lookup ON classroom_bookings(classroom_id, booking_date, start_time, end_time)")
            conn.execute("CREATE INDEX IF NOT EXISTS registration_scope_lookup ON course_registrations(student_id, course_id)")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            if "email" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN email TEXT COLLATE NOCASE")
            if "token_version" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users(email) WHERE email IS NOT NULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS password_reset_tokens(
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
                expires_at INTEGER NOT NULL, used_at INTEGER, created_at INTEGER NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS reset_user_lookup ON password_reset_tokens(user_id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS auth_rate_limits(
                bucket_key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires_at INTEGER NOT NULL)""")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (2)")
            conn.execute("""CREATE TABLE IF NOT EXISTS user_avatars(
                user_id TEXT PRIMARY KEY REFERENCES users(user_id),
                image BLOB NOT NULL, crop TEXT NOT NULL)""")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)")
            conn.execute("""CREATE TABLE IF NOT EXISTS password_reset_otps(
                user_id TEXT PRIMARY KEY REFERENCES users(user_id),
                code_hash TEXT NOT NULL, expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)""")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")
            leave_columns = {row[1] for row in conn.execute("PRAGMA table_info(leave_requests)")}
            if "rejection_reason" not in leave_columns:
                conn.execute("ALTER TABLE leave_requests ADD COLUMN rejection_reason TEXT")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)")
            from db.init_db import CLASSROOMS
            room_columns = {row[1] for row in conn.execute('PRAGMA table_info(classrooms)')}
            for name, constraint in [('has_ac', 'CHECK(has_ac IN (0,1))'), ('projector_count', 'CHECK(projector_count >= 0)'), ('computer_count', 'CHECK(computer_count >= 0)')]:
                if name not in room_columns:
                    conn.execute(f'ALTER TABLE classrooms ADD COLUMN {name} INTEGER {constraint}')
            for room, building, seats, ac, projectors, computers in CLASSROOMS:
                conn.execute('INSERT OR IGNORE INTO classrooms(classroom_id,building,capacity,has_ac,projector_count,computer_count) VALUES(?,?,?,?,?,?)', (room,building,seats,ac,projectors,computers))
                conn.execute('UPDATE classrooms SET has_ac=COALESCE(has_ac,?), projector_count=COALESCE(projector_count,?), computer_count=COALESCE(computer_count,?) WHERE classroom_id=?', (ac,projectors,computers,room))
            conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES (6)')
            conn.execute('''CREATE TABLE IF NOT EXISTS hostel_rooms(
                room_id TEXT PRIMARY KEY,
                sharing_capacity INTEGER NOT NULL CHECK(sharing_capacity IN (2,3,4)),
                has_ac INTEGER NOT NULL CHECK(has_ac IN (0,1)),
                occupied_beds INTEGER NOT NULL CHECK(occupied_beds BETWEEN 0 AND sharing_capacity),
                monthly_room_fee INTEGER NOT NULL CHECK(monthly_room_fee >= 0))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS mess_plans(
                plan_id TEXT PRIMARY KEY CHECK(plan_id IN ('veg','non_veg')),
                name TEXT NOT NULL,
                monthly_mess_fee INTEGER NOT NULL CHECK(monthly_mess_fee >= 0))''')
            had_menus = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mess_menus'").fetchone() is not None
            menu_schema = '''CREATE TABLE IF NOT EXISTS mess_menus(
                plan_id TEXT NOT NULL REFERENCES mess_plans(plan_id),
                weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
                breakfast TEXT CHECK(breakfast IS NULL OR length(trim(breakfast)) > 0),
                lunch TEXT CHECK(lunch IS NULL OR length(trim(lunch)) > 0),
                evening_snacks TEXT CHECK(evening_snacks IS NULL OR length(trim(evening_snacks)) > 0),
                dinner TEXT CHECK(dinner IS NULL OR length(trim(dinner)) > 0),
                PRIMARY KEY(plan_id,weekday))'''
            conn.execute(menu_schema)
            menu_columns = {row[1] for row in conn.execute('PRAGMA table_info(mess_menus)')}
            if 'meal' in menu_columns:
                conn.execute(menu_schema.replace('mess_menus(', 'mess_menus_v9('))
                conn.execute('''INSERT INTO mess_menus_v9
                    SELECT plan_id,weekday,
                    MAX(CASE WHEN meal='breakfast' THEN items END),
                    MAX(CASE WHEN meal='lunch' THEN items END),
                    MAX(CASE WHEN meal='evening_snacks' THEN items END),
                    MAX(CASE WHEN meal='dinner' THEN items END)
                    FROM mess_menus GROUP BY plan_id,weekday''')
                conn.execute('DROP TABLE mess_menus')
                conn.execute('ALTER TABLE mess_menus_v9 RENAME TO mess_menus')
            from db.init_db import HOSTEL_ROOMS, MESS_PLANS, MESS_MENUS
            for table, columns, rows in [
                ('hostel_rooms', 'room_id,sharing_capacity,has_ac,occupied_beds,monthly_room_fee', HOSTEL_ROOMS),
                ('mess_plans', 'plan_id,name,monthly_mess_fee', MESS_PLANS),
                ('mess_menus', 'plan_id,weekday,breakfast,lunch,evening_snacks,dinner',
                 [] if had_menus else [tuple(row[key] for key in ('plan_id','weekday','breakfast','lunch','evening_snacks','dinner')) for row in MESS_MENUS]),
            ]:
                for row in rows:
                    cursor = conn.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in row)}) ON CONFLICT DO NOTHING', row)
                    if cursor.rowcount:
                        key = str(row[0]) if table != 'mess_menus' else f'{row[0]}:{row[1]}'
                        conn.execute('INSERT OR IGNORE INTO demo_records VALUES(?,?)', (table, key))
            conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES (7)')
            conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES (8)')
            # Missing legacy days are explicitly unknown, never filled with seed dishes.
            for (plan,) in conn.execute('SELECT plan_id FROM mess_plans').fetchall():
                conn.executemany('INSERT INTO mess_menus(plan_id,weekday) VALUES(?,?) ON CONFLICT DO NOTHING', [(plan, day) for day in range(7)])
            conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES (9)')
            conn.execute('''CREATE TABLE IF NOT EXISTS chat_confirmations(
                confirmation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
                session_id TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL
                CHECK(status IN ('pending','confirmed','cancelled','expired','failed','superseded')),
                expires_at INTEGER NOT NULL, result TEXT, response TEXT, created_at INTEGER NOT NULL)''')
            conn.execute('CREATE INDEX IF NOT EXISTS confirmation_owner ON chat_confirmations(user_id,session_id,status)')
            conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES (10)')
            migrate_transport(conn)
            migrate_attendance(conn)
            migrate_receipts(conn)
            migrate_alerts(conn)
        return saved


def _migrate_academic(path):
    saved = _migrate_base(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=17').fetchone():
            return saved
        saved = saved or backup(conn, path)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE IF NOT EXISTS course_syllabi(
                course_id TEXT PRIMARY KEY REFERENCES courses(course_id) ON DELETE CASCADE,
                aliases_json TEXT NOT NULL, units_json TEXT NOT NULL, provenance TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS career_roles(
                role_id TEXT PRIMARY KEY, aliases_json TEXT NOT NULL, provenance TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS career_course_relevance(
                role_id TEXT NOT NULL REFERENCES career_roles(role_id),
                course_id TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
                priority INTEGER NOT NULL CHECK(priority>0), reason TEXT NOT NULL,
                PRIMARY KEY(role_id,course_id))''')
            from db.init_db import seed_academic_guidance
            seed_academic_guidance(conn)
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Academic guidance foreign-key validation failed.')
            conn.execute('INSERT INTO schema_migrations(version) VALUES (17)')
    return saved


def _migrate_research(path):
    saved = _migrate_academic(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=18').fetchone():
            return saved
        saved = saved or backup(conn, path)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            _extend_alert_kind(conn, 18, 'research_updates')
            from tools.research_updates import create_schema
            create_schema(conn)
    return saved


def _migrate_automatic_updates(path):
    saved = _migrate_research(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=19').fetchone():
            return saved
        saved = saved or backup(conn, path)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE subject_update_digests(
                event_key TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
                day TEXT NOT NULL, items_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id,day))''')
            # System delivery history must not depend on a deletable personal rule.
            conn.execute('''CREATE TABLE academic_notice_sources(
                id TEXT PRIMARY KEY, config_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)''')
            conn.execute('''CREATE TABLE academic_notices(
                notice_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES academic_notice_sources(id),
                title TEXT NOT NULL, published TEXT NOT NULL, status TEXT NOT NULL,
                evidence_json TEXT NOT NULL, checked_at TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE academic_notice_candidates(
                source_id TEXT NOT NULL REFERENCES academic_notice_sources(id), day TEXT NOT NULL,
                url TEXT NOT NULL, checked INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(source_id,day,url))''')
            from db.init_db import ACADEMIC_NOTICE_SOURCES
            for source in ACADEMIC_NOTICE_SOURCES:
                conn.execute('INSERT OR IGNORE INTO academic_notice_sources(id,config_json) VALUES(?,?)',
                             (source['id'], json.dumps(source)))
            # Preserve legacy rules/occurrences for audit; APIs hide and reject them.
            conn.execute("UPDATE alert_rules SET active=0,state='completed' WHERE kind='research_updates'")
            conn.execute("UPDATE notification_outbox SET status='suppressed' WHERE event_key LIKE 'research:%' AND status IN ('pending','failed')")
            conn.execute('INSERT INTO schema_migrations(version) VALUES(19)')
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Automatic subject updates foreign-key validation failed')
    return saved


def migrate(path):
    saved = _migrate_automatic_updates(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=20').fetchone():
            return saved
        saved = saved or backup(conn, path)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE research_jobs(
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id), session_id TEXT NOT NULL,
                request_key TEXT NOT NULL, course_id TEXT NOT NULL REFERENCES courses(course_id),
                topic TEXT NOT NULL, goal TEXT NOT NULL, parent_topics TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','running','ready','failed','suppressed')),
                attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, result_json TEXT, error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id,request_key))''')
            conn.execute('CREATE INDEX research_job_queue ON research_jobs(status,next_attempt,lease_until)')
            conn.execute('''CREATE TABLE research_job_receipts(
                user_id TEXT NOT NULL REFERENCES users(user_id),request_id TEXT NOT NULL,
                session_id TEXT NOT NULL,job_id TEXT NOT NULL REFERENCES research_jobs(id),
                result TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id,request_id))''')
            conn.execute('INSERT INTO schema_migrations(version) VALUES(20)')
    return saved


def seed_demo(path, year=None):
    """Insert missing current academic samples only; preserve accounts and existing rows."""
    import tempfile
    from db.init_db import init_db
    migrate(path)
    messages = []
    identities = {
        'courses': ('course_id',), 'prerequisites': ('course_id','prerequisite_course_id'),
        'course_registrations': ('student_id','course_id'), 'results': ('student_id','course_id'),
        'attendance': ('student_id','course_id'),
        'course_timetable': ('course_id','weekday','start_time','effective_from'),
    }
    with tempfile.TemporaryDirectory() as temporary:
        reference = Path(temporary) / 'samples.db'
        init_db(reference)
        with closing(sqlite3.connect(reference)) as source, closing(sqlite3.connect(path)) as conn:
            source.row_factory = conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            messages.append('Demo backup: ' + backup(conn, path))
            with conn:
                for table, keys in identities.items():
                    for row in source.execute(f'SELECT * FROM {table}'):
                        values = dict(row)
                        for key in ('registration_id','result_id','attendance_id','timetable_id','registered_on'):
                            values.pop(key, None)
                        if table == 'course_timetable' and year:
                            values['effective_from'], values['effective_to'] = f'{year}-01-01', f'{year}-12-31'
                        where = ' AND '.join(f'{key}=?' for key in keys)
                        existing = conn.execute(f'SELECT * FROM {table} WHERE {where}', tuple(values[key] for key in keys)).fetchall()
                        if existing:
                            if len(existing) != 1 or any(existing[0][key] != value for key,value in values.items()):
                                messages.append(f'Preserved conflicting existing {table}: ' + str(tuple(values[key] for key in keys)))
                            continue
                        try:
                            conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
                            messages.append(f'Added sample {table}: ' + str(tuple(values[key] for key in keys)))
                        except sqlite3.IntegrityError:
                            messages.append(f'Skipped {table} with missing related records or conflicting key.')
    return messages


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(Path(__file__).with_name("college.db")))
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--year", type=int)
    args = parser.parse_args()
    saved = migrate(args.database)
    print("Migration applied; backup: " + saved if saved else "Schema is current.")
    if args.demo:
        print("\n".join(seed_demo(args.database, args.year)))
