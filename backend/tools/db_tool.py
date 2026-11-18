"""Trusted database access and isolated, role-scoped SQL reads."""
import sqlite3
import re
from contextlib import closing
from pathlib import Path

from flask import current_app, has_app_context


SCHEMAS = {
    'transport_services': 'service_id TEXT, campus TEXT, timezone TEXT, operating_days TEXT, campus_arrival TEXT, campus_departure TEXT, planning_radius_km REAL, radius_note TEXT, arrive_before_minutes INTEGER, currency TEXT, fare_basis TEXT',
    'bus_routes': 'bus_id TEXT, service_id TEXT, route_name TEXT, monthly_round_trip_fare INTEGER',
    'bus_stops': 'bus_id TEXT, stop_sequence INTEGER, locality TEXT, stop_name TEXT, approximate_radius_km REAL, morning_pickup TEXT, afternoon_dropoff TEXT',
    'bus_locality_aliases': 'bus_id TEXT, alias TEXT, stop_sequence INTEGER',
    "hostel_rooms": "room_id TEXT, sharing_capacity INTEGER, has_ac INTEGER, occupied_beds INTEGER, monthly_room_fee INTEGER",
    "mess_plans": "plan_id TEXT, name TEXT, monthly_mess_fee INTEGER",
    "mess_menus": "plan_id TEXT, weekday INTEGER, breakfast TEXT, lunch TEXT, evening_snacks TEXT, dinner TEXT",
    "students": "student_id TEXT, name TEXT, department TEXT, semester INTEGER, advisor_faculty_id TEXT",
    "faculty": "faculty_id TEXT, name TEXT, department TEXT, designation TEXT, email TEXT, office_hours TEXT",
    "courses": "course_id TEXT, course_name TEXT, department TEXT, credits INTEGER, faculty_id TEXT",
    "prerequisites": "course_id TEXT, prerequisite_course_id TEXT",
    "course_registrations": "registration_id INTEGER, student_id TEXT, course_id TEXT, status TEXT, registered_on TEXT",
    "attendance": "attendance_id INTEGER, student_id TEXT, course_id TEXT, total_classes INTEGER, attended_classes INTEGER",
    "results": "result_id INTEGER, student_id TEXT, course_id TEXT, grade TEXT, marks REAL",
    "leave_requests": "leave_id INTEGER, student_id TEXT, from_date TEXT, to_date TEXT, reason TEXT, status TEXT, reviewed_by TEXT, created_at TEXT, rejection_reason TEXT",
    "classrooms": "classroom_id TEXT, building TEXT, capacity INTEGER, has_ac INTEGER, projector_count INTEGER, computer_count INTEGER",
    # Occupancy deliberately excludes faculty, purpose and booking IDs.
    "classroom_bookings": "classroom_id TEXT, booking_date TEXT, start_time TEXT, end_time TEXT",
    "course_timetable": "timetable_id INTEGER, course_id TEXT, weekday INTEGER, start_time TEXT, end_time TEXT, effective_from TEXT, effective_to TEXT, is_demo INTEGER",
}

FACULTY_SCOPE = """SELECT s.student_id FROM students s
WHERE s.department = (SELECT department FROM faculty WHERE faculty_id = ?)"""


def _db_path(db_path=None):
    if db_path:
        return str(db_path)
    if has_app_context():
        return current_app.config["DATABASE_PATH"]
    return str(Path(__file__).resolve().parents[1] / "db" / "college.db")


def get_connection(db_path=None):
    conn = sqlite3.connect(_db_path(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def fetch_one(sql, params=(), db_path=None):
    with closing(get_connection(db_path)) as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None


def fetch_all(sql, params=(), db_path=None):
    with closing(get_connection(db_path)) as conn:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def verify_identity(conn, user_id, role):
    row = conn.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row or role not in {"student", "faculty"} or row["role"] != role:
        raise PermissionError("Account or role is no longer valid.")


def scoped_connection(user_id, role, allow_menus=True):
    """Copy authorized data into a private database with no connection to live files."""
    isolated = sqlite3.connect(":memory:")
    isolated.row_factory = sqlite3.Row
    try:
        with closing(get_connection()) as source:
            source.execute("BEGIN")
            verify_identity(source, user_id, role)
            for table, schema in SCHEMAS.items():
                # Human course names should match regardless of the question's casing.
                # This affects only the isolated read database, never stored records.
                if table == 'courses':
                    schema = schema.replace('course_name TEXT', 'course_name TEXT COLLATE NOCASE')
                isolated.execute(f"CREATE TABLE {table} ({schema})")
                columns = ", ".join(col.strip().split()[0] for col in schema.split(","))
                where, params = "", ()
                if table in {"students", "course_registrations", "attendance", "results", "leave_requests"}:
                    if role == "student":
                        where, params = " WHERE student_id = ?", (user_id,)
                    else:
                        where, params = f" WHERE student_id IN ({FACULTY_SCOPE})", (user_id,)
                elif role == "faculty" and table in {"courses", "faculty"}:
                    where, params = " WHERE department = (SELECT department FROM faculty WHERE faculty_id = ?)", (user_id,)
                elif role == "faculty" and table in {"course_timetable", "prerequisites"}:
                    where, params = " WHERE course_id IN (SELECT course_id FROM courses WHERE department = (SELECT department FROM faculty WHERE faculty_id = ?))", (user_id,)
                rows = source.execute(f"SELECT {columns} FROM {table}{where}", params).fetchall()
                if rows:
                    marks = ",".join("?" for _ in schema.split(","))
                    isolated.executemany(f"INSERT INTO {table} VALUES ({marks})", [tuple(row) for row in rows])
        isolated.commit()
        isolated.execute("PRAGMA query_only = ON")
        allowed_functions = {
            "count", "sum", "avg", "min", "max", "round", "coalesce", "ifnull", "nullif",
            "abs", "lower", "upper", "length", "substr", "substring", "trim", "ltrim", "rtrim",
            "replace", "like", "glob", "date", "time", "datetime", "strftime", "julianday",
            "total", "group_concat", "row_number", "rank", "dense_rank", "printf",
        }

        def authorize(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and database == "main" and arg1 in SCHEMAS:
                if arg1 == 'mess_menus' and not allow_menus:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in allowed_functions:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        isolated.set_authorizer(authorize)
        budget = [0]

        def progress():
            budget[0] += 1
            return int(budget[0] > 1000)

        isolated.set_progress_handler(progress, 1000)
        return isolated
    except Exception:
        isolated.close()
        raise


def execute_query(sql, user_id, role, allow_menus=True):
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("No SQL was generated.")
    if sql.lstrip().split(None, 1)[0].lower() not in {"select", "with"}:
        raise PermissionError("Generated SQL must be a read-only SELECT.")
    with closing(scoped_connection(user_id, role, allow_menus=allow_menus)) as conn:
        try:
            rows = conn.execute(sql).fetchmany(501)
            if len(rows) > 500:
                raise ValueError("Too many results; narrow the query to at most 500 records.")
            return [dict(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            if any(marker in str(exc).lower() for marker in ("authorized", "readonly", "prohibited", "no such table: users")):
                raise PermissionError("Query attempts an unauthorized database operation.") from exc
            raise
        except sqlite3.Warning as exc:
            raise PermissionError("Only one read-only SQL statement is permitted.") from exc


def schema_metadata():
    return "\n".join(f"{table}({', '.join(c.strip().split()[0] for c in schema.split(','))})" for table, schema in SCHEMAS.items())


MENU_MEALS = ('breakfast', 'lunch', 'evening_snacks', 'dinner')


class UnsupportedMenuChoice(ValueError):
    pass


def transport_catalog(user_id, role):
    """Current service identity and bus spellings, not inferred stops or routes."""
    with closing(get_connection()) as conn:
        verify_identity(conn, user_id, role)
        services = [dict(row) for row in conn.execute(
            'SELECT service_id,campus FROM transport_services ORDER BY service_id')]
        routes = [dict(row) for row in conn.execute(
            'SELECT bus_id,service_id FROM bus_routes ORDER BY bus_id')]
    aliases = {}
    for route in routes:
        identifier = route['bus_id']
        aliases[identifier.casefold()] = identifier
        match = re.fullmatch(r'B(\d+)', identifier, re.I)
        if match:
            numbers = {match.group(1), str(int(match.group(1)))}
            for number in numbers:
                for separator in ('', '-', ' '):
                    alias = 'b' + separator + number
                    # Never select between colliding canonical IDs.
                    aliases.setdefault(alias, identifier)
                    if aliases[alias] != identifier:
                        aliases[alias] = None
    return {'services': services, 'routes': routes,
            'bus_id_aliases': {key: value for key, value in aliases.items() if value is not None}}


def menu_catalog(user_id, role):
    with closing(get_connection()) as conn:
        verify_identity(conn, user_id, role)
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(mess_menus)')}
        return {'plans': [dict(row) for row in conn.execute('SELECT plan_id,name FROM mess_plans ORDER BY plan_id')],
                'weekdays': list(range(7)), 'meals': [meal for meal in MENU_MEALS if meal in columns]}


def validate_menu_request(request, catalog, relative_weekdays=None):
    if not isinstance(request, dict) or set(request) - {'plans', 'weekdays', 'meals', 'dish'}:
        raise ValueError('Menu request accepts only plans, weekdays, meals and dish; no SQL or other fields.')
    available = {'plans': [row['plan_id'] for row in catalog['plans']],
                 'weekdays': catalog['weekdays'], 'meals': catalog['meals']}
    resolved = {}
    for key, choices in available.items():
        values = request.get(key)
        if values is None:
            resolved[key] = choices
            continue
        if not isinstance(values, list) or not values or len(values) > 7:
            raise ValueError(f'{key} must be a nonempty list or null for all available choices.')
        if key == 'weekdays':
            resolved_days = []
            for value in values:
                if isinstance(value, dict):
                    if set(value) != {'relative'} or not isinstance(value['relative'], str) or value['relative'] not in (relative_weekdays or {}):
                        raise ValueError('Relative weekday must reference today, tomorrow or yesterday.')
                    value = relative_weekdays[value['relative']]
                resolved_days.append(value)
            values = resolved_days
        expected = int if key == 'weekdays' else str
        if any(type(value) is not expected for value in values):
            raise ValueError(f'Invalid value type in {key}.')
        if any(value not in choices for value in values):
            raise UnsupportedMenuChoice(f'Unsupported menu {key}. Available choices: ' + ', '.join(map(str, choices)))
        resolved[key] = [value for value in choices if value in values]
    dish = request.get('dish')
    if dish is not None:
        if not isinstance(dish, str) or not dish.strip() or len(dish) > 100 or any(not c.isprintable() for c in dish):
            raise ValueError('Dish must be nonempty plain text of at most 100 characters, or null.')
        resolved['dish'] = ' '.join(dish.split())
    return resolved


def read_menu(request, user_id, role):
    # Revalidate against current catalog; only application-owned identifiers enter SQL.
    request = validate_menu_request(request, menu_catalog(user_id, role))
    rows = []
    if request['plans'] and request['meals']:
        columns = ','.join(request['meals'])
        plans = ','.join('?' for _ in request['plans'])
        days = ','.join('?' for _ in request['weekdays'])
        with closing(scoped_connection(user_id, role)) as conn:
            rows = [dict(row) for row in conn.execute(
                f'SELECT plan_id,weekday,{columns} FROM mess_menus WHERE plan_id IN ({plans}) AND weekday IN ({days}) ORDER BY plan_id,weekday',
                (*request['plans'], *request['weekdays']))]
    missing = []
    for plan in request['plans']:
        for day in request['weekdays']:
            row = next((r for r in rows if r['plan_id'] == plan and r['weekday'] == day), None)
            for meal in request['meals']:
                if row is None or row[meal] is None:
                    missing.append({'plan_id': plan, 'weekday': day, 'meal': meal,
                                    'reason': 'missing_record' if row is None else 'missing_meal'})
    if request.get('dish'):
        # Literal whole-word/phrase matching, never model-generated SQL or regex.
        pattern = re.compile(r'(?<!\w)' + r'\s+'.join(re.escape(word) for word in request['dish'].split()) + r'(?!\w)', re.I)
        matches = []
        for row in rows:
            meals = {meal: row[meal] for meal in request['meals']
                     if row[meal] is not None and pattern.search(row[meal])}
            if meals:
                matches.append({'plan_id': row['plan_id'], 'weekday': row['weekday'], **meals})
        rows = matches
    return {'request': request, 'rows': rows, 'missing': missing}
