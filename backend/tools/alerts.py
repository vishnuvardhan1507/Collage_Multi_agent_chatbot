"""Private, validated alert rules and their durable email occurrences."""
import json
import re
import uuid
from contextlib import closing
from datetime import date, datetime
from zoneinfo import ZoneInfo

from tools.db_tool import get_connection, verify_identity
from tools.notifications import enqueue
from tools.operations import OperationError, parse_date
from tools.timetable import college_now

KINDS = {'attendance_student', 'attendance_faculty', 'classroom_free', 'menu_match', 'leave_student', 'leave_faculty', 'class_reminder', 'attendance_missing', 'research_updates'}
MEALS = {'breakfast', 'lunch', 'evening_snacks', 'dinner'}
PLANS = {'veg', 'non_veg'}
SCHEDULE_DEFAULTS = {'class_reminder':'07:00', 'menu_match':'07:00', 'attendance_missing':'18:00'}


def _reminder_time(kind, config):
    return config.get('reminder_time', SCHEDULE_DEFAULTS[kind])


def _local_now(now):
    return now.astimezone(ZoneInfo('Asia/Kolkata'))


def _scheduled_due(kind, config, now):
    return _local_now(now).strftime('%H:%M') >= _reminder_time(kind, config)


def _time_label(kind, config):
    return datetime.strptime(_reminder_time(kind, config), '%H:%M').strftime('%I:%M %p').lstrip('0') + ' IST'



def _config(value):
    if not isinstance(value, dict):
        raise OperationError('Alert settings must be an object.')
    return value


def _menu_dishes(config):
    return config['dishes'] if 'dishes' in config else [config['dish']]


def _menu_match_mode(config):
    """Legacy menu alerts did not store a mode and always used ANY matching."""
    return config.get('match_mode', 'any')


def _match_mode(config):
    """Legacy grouped alerts behaved as ANY and did not store an operator."""
    return config.get('match_mode', 'any')


def _selected(config, singular, plural):
    """Read a new list setting while retaining legacy singular/null settings."""
    if plural in config:
        return config[plural]
    value = config.get(singular)
    return None if value is None else [value]


def _selection_payload(config, singular, plural, values):
    """Preserve a legacy field shape when that is what an older client supplied."""
    if plural in config:
        return {plural: values}
    return {singular: None if values is None else values[0]}


def _raw_selection(config, singular, plural, label, maximum=20):
    if singular in config and plural in config:
        raise OperationError(f'Provide {label} as either one value or a list, not both.')
    if plural in config:
        values = config[plural]
        if values is None:
            return None
        if not isinstance(values, list) or not values or len(values) > maximum:
            raise OperationError(f'Choose between 1 and {maximum} {label}.')
        return values
    return None if config.get(singular) is None else [config.get(singular)]


def _validated_mode(config, count):
    mode = config.get('match_mode', 'any')
    if mode not in {'any', 'all'}:
        raise OperationError('Match mode must be any or all.')
    return mode if count > 1 else 'any'


def _mode_payload(config, count):
    if 'match_mode' not in config and not any(key.endswith('_ids') for key in config):
        return {}
    return {'match_mode':_validated_mode(config, count)}


def _text(value, label, maximum=100):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise OperationError(f'Provide a valid {label}.')
    return value.strip()


def _time(value, label):
    value = _text(value, label, 5)
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise OperationError(f'Provide {label} in HH:MM format.')
    return value


def _rule_row(row):
    item = dict(row)
    item['config'] = json.loads(item.pop('config_json'))
    return item


def _faculty_courses(conn, user):
    return [dict(row) for row in conn.execute(
        'SELECT course_id,course_name FROM courses WHERE faculty_id=? ORDER BY course_id', (user,))]


def _faculty_courses_with_students(conn, user):
    courses = _faculty_courses(conn, user)
    for course in courses:
        course['students'] = [dict(row) for row in conn.execute('''SELECT s.student_id,s.name
            FROM students s JOIN course_registrations cr ON cr.student_id=s.student_id
            WHERE cr.course_id=? AND cr.status='enrolled' ORDER BY s.student_id''', (course['course_id'],))]
    return courses


def _reminder_courses(conn, user, role):
    if role == 'faculty':
        return _faculty_courses(conn, user)
    if role == 'student':
        return [dict(row) for row in conn.execute('''SELECT DISTINCT c.course_id,c.course_name
            FROM courses c JOIN course_registrations cr ON cr.course_id=c.course_id
            WHERE cr.student_id=? AND cr.status='enrolled' ORDER BY c.course_id''', (user,))]
    return []


def interpretation_context(user, role):
    """Private, owner-scoped choices for interpretation, never generated SQL."""
    with closing(get_connection()) as conn:
        verify_identity(conn, user, role)
        rules = []
        for row in conn.execute("SELECT * FROM alert_rules WHERE user_id=? AND active=1 AND kind!='research_updates' ORDER BY id", (user,)):
            rule = _rule_row(row)
            rules.append({key: rule[key] for key in ('id', 'kind', 'config')})
            rules[-1]['summary'] = _summary(rule['kind'], rule['config'])
        courses = _faculty_courses_with_students(conn, user) if role == 'faculty' else []
        return {'owned_alerts': rules, 'assigned_courses': courses,
                'reminder_courses': _reminder_courses(conn, user, role)}


def _resolve_choice(value, rows, id_key, name_key, label):
    value = _text(value, label)
    matches = [row for row in rows if row[id_key].casefold() == value.casefold()]
    if not matches:
        matches = [row for row in rows if row[name_key].casefold() == value.casefold()]
    if not matches:
        raise OperationError(f'No authorized {label} matches that selection. Choose a listed {label}.')
    if len(matches) > 1:
        raise OperationError(f'Which {label} do you mean? Specify an ID: ' + ', '.join(row[id_key] for row in matches))
    return matches[0][id_key]


def _resolve_choices(values, rows, id_key, name_key, label):
    if values is None:
        return None
    resolved = []
    for value in values:
        choice = _resolve_choice(value, rows, id_key, name_key, label)
        if choice not in resolved:
            resolved.append(choice)
    return resolved


def _validate(conn, user, role, kind, raw):
    config = dict(_config(raw))
    reminder_time = None
    if kind in SCHEDULE_DEFAULTS and 'reminder_time' in config:
        reminder_time = _time(config.pop('reminder_time'), 'reminder time')
    valid = _validate_fields(conn, user, role, kind, config)
    if reminder_time is not None:
        valid['reminder_time'] = reminder_time
    return valid


def _validate_fields(conn, user, role, kind, raw):
    if kind not in KINDS:
        raise OperationError('Choose a supported alert type.')
    config = _config(raw)
    if kind == 'research_updates':
        raise OperationError('Subject updates are automatic for every student and cannot be managed as a personal alert.')
    if kind == 'attendance_missing':
        if role != 'faculty':
            raise PermissionError('Only faculty can set unmarked attendance reminders.')
        if set(config) - {'course_id', 'course_ids', 'match_mode'}:
            raise OperationError('Choose one or more courses, or all assigned courses, for the unmarked attendance reminder.')
        courses = _faculty_courses(conn, user)
        if not courses:
            raise OperationError('You have no assigned courses to monitor.')
        selected = _raw_selection(config, 'course_id', 'course_ids', 'courses')
        selected = _resolve_choices(selected, courses, 'course_id', 'course_name', 'course')
        count = len(selected) if selected is not None else len(courses)
        return {**_selection_payload(config, 'course_id', 'course_ids', selected), **_mode_payload(config, count)}
    if kind == 'class_reminder':
        if set(config) - {'course_id', 'course_ids', 'match_mode'}:
            raise OperationError('Select one or more courses, or all your courses, for the class reminder; no percentage is needed.')
        courses = _reminder_courses(conn, user, role)
        if not courses:
            raise OperationError('You have no enrolled or assigned courses for class reminders.')
        selected = _raw_selection(config, 'course_id', 'course_ids', 'courses')
        selected = _resolve_choices(selected, courses, 'course_id', 'course_name', 'course')
        count = len(selected) if selected is not None else len(courses)
        return {**_selection_payload(config, 'course_id', 'course_ids', selected), **_mode_payload(config, count)}
    if kind == 'attendance_student':
        if role != 'student': raise PermissionError('Only students can configure their own attendance alerts.')
        if set(config) - {'threshold', 'course_id', 'course_ids', 'match_mode'}: raise OperationError('Attendance alert contains unsupported settings.')
        threshold = config.get('threshold')
        if type(threshold) not in (int, float) or not 1 <= threshold < 100: raise OperationError('Attendance threshold must be from 1 to 99.')
        courses = _reminder_courses(conn, user, role)
        selected = _raw_selection(config, 'course_id', 'course_ids', 'courses')
        selected = _resolve_choices(selected, courses, 'course_id', 'course_name', 'course')
        count = len(selected) if selected is not None else len(courses)
        return {'threshold': float(threshold), **_selection_payload(config, 'course_id', 'course_ids', selected), **_mode_payload(config, count)}
    if kind == 'attendance_faculty':
        if role != 'faculty': raise PermissionError('Only faculty can configure course attendance alerts.')
        if set(config) - {'threshold', 'course_id', 'course_ids', 'student_id', 'student_ids', 'match_mode'}: raise OperationError('Attendance alert contains unsupported settings.')
        threshold = config.get('threshold')
        if type(threshold) not in (int, float) or not 1 <= threshold < 100: raise OperationError('Attendance threshold must be from 1 to 99.')
        courses = _faculty_courses(conn, user)
        if not courses:
            raise OperationError('You have no assigned courses to monitor.')
        selected_courses = _raw_selection(config, 'course_id', 'course_ids', 'courses')
        selected_courses = _resolve_choices(selected_courses, courses, 'course_id', 'course_name', 'course')
        if config.get('student_id') is not None and selected_courses is None:
            raise OperationError('Which assigned course should I monitor for this student?')
        course_ids = selected_courses or [row['course_id'] for row in courses]
        placeholders = ','.join('?' for _ in course_ids)
        roster = conn.execute(f'''SELECT DISTINCT s.student_id,s.name FROM students s
            JOIN course_registrations cr ON cr.student_id=s.student_id
            WHERE cr.course_id IN ({placeholders}) AND cr.status='enrolled' ORDER BY s.student_id''', course_ids).fetchall()
        selected_students = _raw_selection(config, 'student_id', 'student_ids', 'students')
        selected_students = _resolve_choices(selected_students, roster, 'student_id', 'name', 'student')
        count = (len(selected_students) if selected_students is not None else len(roster)) * len(course_ids)
        return {'threshold': float(threshold),
                **_selection_payload(config, 'course_id', 'course_ids', selected_courses),
                **_selection_payload(config, 'student_id', 'student_ids', selected_students),
                **_mode_payload(config, count)}
    if kind == 'classroom_free':
        if role != 'faculty': raise PermissionError('Only faculty can configure classroom alerts.')
        if set(config) - {'classroom_id', 'classroom_ids', 'booking_date', 'start_time', 'end_time', 'match_mode'} or not {'booking_date','start_time','end_time'}.issubset(config): raise OperationError('Provide one or more classrooms, date, start time and end time.')
        raw_rooms = _raw_selection(config, 'classroom_id', 'classroom_ids', 'classrooms', 10)
        if raw_rooms is None: raise OperationError('Choose at least one classroom.')
        rooms = [dict(row) for row in conn.execute('SELECT classroom_id,classroom_id AS room_name FROM classrooms ORDER BY classroom_id')]
        selected_rooms = _resolve_choices(raw_rooms, rooms, 'classroom_id', 'room_name', 'classroom')
        day = parse_date(config['booking_date']).isoformat(); start = _time(config['start_time'], 'start time'); end = _time(config['end_time'], 'end time')
        if start >= end: raise OperationError('End time must be later than start time.')
        for room in selected_rooms:
            if not conn.execute('SELECT 1 FROM classroom_bookings WHERE classroom_id=? AND booking_date=? AND start_time<? AND end_time>?', (room, day, end, start)).fetchone():
                raise OperationError(f'{room} is already free for that slot; an availability alert is not needed.')
        return {**_selection_payload(config, 'classroom_id', 'classroom_ids', selected_rooms), 'booking_date': day,
                'start_time': start, 'end_time': end, **_mode_payload(config, len(selected_rooms))}
    if kind == 'menu_match':
        keys = set(config)
        allowed = {'plans', 'meals', 'dish', 'dishes', 'match_mode'}
        if keys - allowed or not {'plans', 'meals'}.issubset(keys) or ('dish' in keys) == ('dishes' in keys):
            raise OperationError('Provide plans, meals and dishes to match.')
        plans, meals = config['plans'], config['meals']
        if not isinstance(plans, list) or not plans or any(not isinstance(p,str) or p not in PLANS for p in plans): raise OperationError('Choose Veg, Non-veg, or both mess plans.')
        if not isinstance(meals, list) or not meals or any(not isinstance(m,str) or m not in MEALS for m in meals): raise OperationError('Choose one or more meals.')
        raw_dishes = _menu_dishes(config)
        if not isinstance(raw_dishes,list) or not 1 <= len(raw_dishes) <= 10:
            raise OperationError('Choose between 1 and 10 dishes.')
        dishes = {}
        for raw in raw_dishes:
            dish = _text(raw, 'dish', 80)
            if any(ord(c)<32 or ord(c)==127 for c in dish):
                raise OperationError('Dish names must be plain text on one line.')
            dishes.setdefault(dish.casefold(), dish)
        mode = config.get('match_mode', 'any')
        if not isinstance(mode, str) or mode not in {'any', 'all'}:
            raise OperationError('Dish match mode must be any or all.')
        # Keep old clients and stored single-dish rules compatible.
        selection = {'dish':next(iter(dishes.values()))} if 'dish' in config else {'dishes':list(dishes.values())}
        return {'plans': sorted(set(plans)), 'meals': sorted(set(meals)), **selection, 'match_mode':mode}
    if kind == 'leave_student':
        if role != 'student': raise PermissionError('Only students can configure their leave decision alerts.')
        statuses = config.get('statuses')
        if set(config) != {'statuses'} or not isinstance(statuses, list) or not statuses or set(statuses) - {'approved','rejected'}: raise OperationError('Choose approved, rejected, or both leave decisions.')
        return {'statuses': sorted(set(statuses))}
    if kind == 'leave_faculty':
        if role != 'faculty' or config: raise PermissionError('Faculty leave alerts apply only to new requests in their department.')
        return {}


def _summary(kind, config):
    if kind == 'research_updates':
        return 'Archived research subscription; subject updates are now automatic for the entire curriculum'
    joiner = ' and ' if _match_mode(config) == 'all' else ' or '
    if kind == 'attendance_missing':
        courses = _selected(config, 'course_id', 'course_ids')
        return f"Unmarked attendance for {joiner.join(courses) if courses else 'all my assigned courses'}: {_time_label(kind, config)}"
    if kind == 'class_reminder':
        courses = _selected(config, 'course_id', 'course_ids')
        return f"Class reminders for {joiner.join(courses) if courses else 'all my courses'}: {_time_label(kind, config)} on scheduled class days"
    if kind == 'attendance_student':
        courses = _selected(config, 'course_id', 'course_ids')
        return f"Attendance below {config['threshold']:g}%" + (f" for {joiner.join(courses)}" if courses else ' for all enrolled courses')
    if kind == 'attendance_faculty':
        students = _selected(config, 'student_id', 'student_ids'); courses = _selected(config, 'course_id', 'course_ids')
        return f"Attendance below {config['threshold']:g}%" + (f" for {joiner.join(students)}" if students else ' for any enrolled student') + (f" in {joiner.join(courses)}" if courses else ' across all my assigned courses')
    if kind == 'classroom_free':
        rooms = _selected(config, 'classroom_id', 'classroom_ids') or []
        return f"{joiner.join(rooms)} free on {config['booking_date']} {config['start_time']}–{config['end_time']}"
    if kind == 'classroom_free': return f"{config['classroom_id']} free on {config['booking_date']} {config['start_time']}–{config['end_time']}"
    if kind == 'menu_match':
        separator = ' and ' if _menu_match_mode(config) == 'all' else ' or '
        return f"{separator.join(_menu_dishes(config))} in {', '.join(config['meals']).replace('_',' ')} at {_time_label(kind, config)}"
    if kind == 'leave_student': return 'My leave ' + ' or '.join(config['statuses'])
    return 'New leave requests in my department'


def list_alerts(user, role):
    with closing(get_connection()) as conn:
        verify_identity(conn, user, role)
        rules = [_rule_row(row) for row in conn.execute("SELECT * FROM alert_rules WHERE user_id=? AND kind!='research_updates' ORDER BY active DESC,id DESC", (user,))]
        for rule in rules: rule['summary'] = _summary(rule['kind'], rule['config'])
        recent = [dict(row) for row in conn.execute('''SELECT * FROM (SELECT o.id AS id,o.rule_id,o.subject,o.body,o.created_at,n.status AS email_status,n.sent_at,0 AS system_generated,n.id AS sort_id
            FROM alert_occurrences o LEFT JOIN notification_outbox n ON n.event_key=o.event_key
            WHERE o.user_id=?
            UNION ALL
            SELECT d.event_key AS id,NULL AS rule_id,n.subject,n.body,d.created_at,n.status AS email_status,n.sent_at,1 AS system_generated,n.id AS sort_id
            FROM subject_update_digests d JOIN notification_outbox n ON n.event_key=d.event_key
            WHERE d.user_id=?
            UNION ALL
            SELECT 'topic-research:' || j.id,NULL, 'Research: ' || j.topic,
                COALESCE(n.body,''),j.created_at,COALESCE(n.status,j.status),n.sent_at,0,n.id
            FROM research_jobs j LEFT JOIN notification_outbox n ON n.event_key='topic-research:' || j.id
            WHERE j.user_id=?) ORDER BY created_at DESC,sort_id DESC,id DESC LIMIT 5''', (user,user,user))]
        return {'alerts': rules, 'recent': recent, 'assigned_courses': _faculty_courses_with_students(conn, user) if role == 'faculty' else [],
                'reminder_courses': _reminder_courses(conn, user, role),
                'classrooms':[row['classroom_id'] for row in conn.execute('SELECT classroom_id FROM classrooms ORDER BY classroom_id')] if role == 'faculty' else []}


def create_alert(user, role, kind, config):
    with closing(get_connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE'); verify_identity(conn, user, role)
        valid = _validate(conn, user, role, kind, config)
        cursor = conn.execute('INSERT INTO alert_rules(user_id,kind,config_json,active,state) VALUES(?,?,?,?,?)', (user,kind,json.dumps(valid,sort_keys=True),1,'active'))
        evaluate_due(conn, college_now(), only_rule=cursor.lastrowid)
        row = conn.execute('SELECT * FROM alert_rules WHERE id=?', (cursor.lastrowid,)).fetchone()
        result = _rule_row(row); result['summary'] = _summary(kind, valid)
        return result


def update_alert(user, role, identifier, kind, config):
    with closing(get_connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE'); verify_identity(conn,user,role)
        old=conn.execute('SELECT * FROM alert_rules WHERE id=? AND user_id=?',(identifier,user)).fetchone()
        if not old: raise PermissionError('Alert not found.')
        if old['kind'] == 'research_updates':
            raise OperationError('Subject updates are automatic and cannot be edited as a personal alert.')
        if kind == old['kind'] and kind in SCHEDULE_DEFAULTS and isinstance(config, dict) and 'reminder_time' not in config:
            config = {**config, 'reminder_time':_reminder_time(kind, json.loads(old['config_json']))}
        valid=_validate(conn,user,role,kind,config)
        if old['kind'].startswith('attendance_'):
            conn.execute("UPDATE notification_outbox SET status='suppressed' WHERE event_key LIKE ? AND status IN ('pending','failed')", (f'alert:{identifier}:attendance:%',))
        conn.execute("UPDATE alert_rules SET kind=?,config_json=?,active=1,state='active',updated_at=CURRENT_TIMESTAMP WHERE id=?",(kind,json.dumps(valid,sort_keys=True),identifier))
        if kind in SCHEDULE_DEFAULTS:
            conn.execute("UPDATE notification_outbox SET next_attempt=0 WHERE status='pending' AND attempts=0 AND event_key IN (SELECT event_key FROM alert_occurrences WHERE rule_id=?)", (identifier,))
        conn.execute('DELETE FROM alert_condition_state WHERE rule_id=?',(identifier,))
        evaluate_due(conn,college_now(),only_rule=identifier)
        row=conn.execute('SELECT * FROM alert_rules WHERE id=?',(identifier,)).fetchone(); result=_rule_row(row);result['summary']=_summary(kind,valid);return result


def delete_alert(user, role, identifier):
    delete_alerts(user, role, [identifier])
    return {'deleted': identifier}


def deletion_selection(conn, user, role, identifiers):
    if not isinstance(identifiers, list) or not identifiers or any(type(i) is not int or i <= 0 or i > 9223372036854775807 for i in identifiers):
        raise OperationError('Provide a nonempty list of positive integer alert IDs.')
    identifiers = list(dict.fromkeys(identifiers))
    verify_identity(conn,user,role)
    owned = {row['id']:dict(row) for row in conn.execute('SELECT * FROM alert_rules WHERE user_id=?', (user,))}
    if not set(identifiers).issubset(owned):
        raise PermissionError('One or more selected alerts are no longer available to you. Nothing was deleted. Refresh your alerts and select again.')
    if any(owned[i]['kind'] == 'research_updates' for i in identifiers):
        raise OperationError('Subject updates are automatic and cannot be deleted as a personal alert.')
    return [owned[i] for i in identifiers]


def delete_alerts_in_transaction(conn, user, role, identifiers):
    selected = deletion_selection(conn,user,role,identifiers)
    identifiers = [row['id'] for row in selected]
    for identifier in identifiers:
        # Use occurrence keys: leave events do not use the alert:<id> prefix.
        conn.execute("""UPDATE notification_outbox SET status='suppressed'
            WHERE user_id=? AND status IN ('pending','failed') AND event_key IN
            (SELECT event_key FROM alert_occurrences WHERE rule_id=? AND user_id=?)""", (user,identifier,user))
        conn.execute('DELETE FROM alert_rules WHERE id=? AND user_id=?', (identifier,user))
    return {'deleted': identifiers}


def delete_alerts(user, role, identifiers):
    with closing(get_connection()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        return delete_alerts_in_transaction(conn,user,role,identifiers)


def _emit(conn, rule, key, subject, body):
    # Preserve the established leave event key for compatibility with existing
    # mail history; all other custom occurrences are namespaced by rule.
    event_key=key if key.startswith('leave:') else f'alert:{rule["id"]}:{key}'
    if conn.execute('INSERT OR IGNORE INTO alert_occurrences(rule_id,user_id,event_key,subject,body) VALUES(?,?,?,?,?)',(rule['id'],rule['user_id'],event_key,subject,body)).rowcount:
        enqueue(conn,event_key,rule['user_id'],subject,body)


def _set_match(conn, rule, subject_key, matching, key, subject, body):
    old=conn.execute('SELECT matching FROM alert_condition_state WHERE rule_id=? AND subject_key=?',(rule['id'],subject_key)).fetchone()
    # A new crossing has a new identity even when a correction keeps total_classes
    # unchanged. BEGIN IMMEDIATE serializes evaluations; retries see matching=True.
    if matching and (not old or not old['matching']): _emit(conn,rule,key + ':' + uuid.uuid4().hex,subject,body)
    if not matching:
        conn.execute("UPDATE notification_outbox SET status='suppressed' WHERE event_key LIKE ? AND status IN ('pending','failed')", (f'alert:{rule["id"]}:attendance:{subject_key}:%',))
    conn.execute('INSERT INTO alert_condition_state(rule_id,subject_key,matching) VALUES(?,?,?) ON CONFLICT(rule_id,subject_key) DO UPDATE SET matching=excluded.matching,updated_at=CURRENT_TIMESTAMP',(rule['id'],subject_key,int(matching)))


def _attendance_grouped(conn, rule, config, subject=None):
    """Evaluate list-based attendance rules without allowing model-generated filters."""
    role = 'student' if rule['kind'] == 'attendance_student' else 'faculty'
    courses = _reminder_courses(conn, rule['user_id'], role) if role == 'student' else _faculty_courses(conn, rule['user_id'])
    authorized_courses = [row['course_id'] for row in courses]
    requested_courses = _selected(config, 'course_id', 'course_ids')
    course_ids = [value for value in authorized_courses if requested_courses is None or value in requested_courses]
    if not course_ids:
        return
    params = [rule['user_id'], *course_ids]
    student_clause = ''
    requested_students = _selected(config, 'student_id', 'student_ids') if role == 'faculty' else [rule['user_id']]
    if requested_students:
        student_clause = f" AND a.student_id IN ({','.join('?' for _ in requested_students)})"
        params.extend(requested_students)
    rows = conn.execute(f'''SELECT a.student_id,a.course_id,c.course_name,a.attended_classes,a.total_classes,s.name AS student_name
        FROM attendance a JOIN course_registrations cr ON cr.student_id=a.student_id AND cr.course_id=a.course_id AND cr.status='enrolled'
        JOIN students s ON s.student_id=a.student_id JOIN courses c ON c.course_id=a.course_id
        WHERE {'a.student_id=?' if role == 'student' else 'c.faculty_id=?'}
        AND a.course_id IN ({','.join('?' for _ in course_ids)}){student_clause}
        ORDER BY a.student_id,a.course_id''', params).fetchall()
    valid = []
    for row in rows:
        total, attended = row['total_classes'], row['attended_classes']
        if type(total) is int and type(attended) is int and total > 0 and 0 <= attended <= total:
            valid.append((row, attended * 100 / total))
    mode = _match_mode(config)
    if mode == 'all':
        represented_courses = {row['course_id'] for row, _ in valid}
        represented_students = {row['student_id'] for row, _ in valid}
        complete = set(course_ids).issubset(represented_courses) and (not requested_students or set(requested_students).issubset(represented_students))
        matching = bool(valid) and complete and all(percent < config['threshold'] for _, percent in valid)
        lines = [f'{row["student_name"]} ({row["student_id"]}) · {row["course_id"]} — {row["attended_classes"]}/{row["total_classes"]} ({percent:.2f}%)'
                 for row, percent in valid]
        _set_match(conn, rule, 'group', matching, 'attendance:group', f'All selected attendance records are below {config["threshold"]:g}%',
                   '\n'.join(lines) + f'\nAll are below your {config["threshold"]:g}% alert threshold.' if lines else '')
        return
    authorized = {f'{row["student_id"]}:{row["course_id"]}' for row, _ in valid}
    for old in conn.execute('SELECT subject_key FROM alert_condition_state WHERE rule_id=?', (rule['id'],)).fetchall():
        if old['subject_key'] not in authorized:
            _set_match(conn, rule, old['subject_key'], False, '', '', '')
            conn.execute('DELETE FROM alert_condition_state WHERE rule_id=? AND subject_key=?', (rule['id'],old['subject_key']))
    for row, percentage in valid:
        if subject is not None and (row['student_id'],row['course_id']) != subject:
            continue
        key = f'{row["student_id"]}:{row["course_id"]}'
        identity = f'{row["student_name"]} ({row["student_id"]})\n' if role == 'faculty' else ''
        _set_match(conn,rule,key,percentage < config['threshold'],f'attendance:{key}:{row["total_classes"]}',
          f'Attendance below {config["threshold"]:g}%',identity + f'{row["course_id"]} — {row["course_name"]}: {row["attended_classes"]}/{row["total_classes"]} ({percentage:.2f}%) is below your {config["threshold"]:g}% alert threshold.')


def _attendance(conn, rule, config, subject=None):
    if any(key in config for key in ('course_ids','student_ids','match_mode')):
        return _attendance_grouped(conn, rule, config, subject)
    if rule['kind']=='attendance_student':
        rows=conn.execute('''SELECT a.student_id,a.course_id,c.course_name,a.attended_classes,a.total_classes FROM attendance a
          JOIN course_registrations cr ON cr.student_id=a.student_id AND cr.course_id=a.course_id AND cr.status='enrolled'
          JOIN courses c ON c.course_id=a.course_id WHERE a.student_id=? AND (? IS NULL OR a.course_id=?)''',(rule['user_id'],config['course_id'],config['course_id'])).fetchall()
    else:
        rows=conn.execute('''SELECT a.student_id,a.course_id,c.course_name,a.attended_classes,a.total_classes,s.name AS student_name FROM attendance a
          JOIN course_registrations cr ON cr.student_id=a.student_id AND cr.course_id=a.course_id AND cr.status='enrolled'
          JOIN students s ON s.student_id=a.student_id
          JOIN courses c ON c.course_id=a.course_id WHERE c.faculty_id=? AND (? IS NULL OR a.course_id=?) AND (? IS NULL OR a.student_id=?)''',(rule['user_id'],config['course_id'],config['course_id'],config['student_id'],config['student_id'])).fetchall()
        authorized = {f'{row["student_id"]}:{row["course_id"]}' for row in rows}
        for old in conn.execute('SELECT subject_key FROM alert_condition_state WHERE rule_id=?', (rule['id'],)).fetchall():
            if old['subject_key'] not in authorized:
                _set_match(conn, rule, old['subject_key'], False, '', '', '')
                conn.execute('DELETE FROM alert_condition_state WHERE rule_id=? AND subject_key=?', (rule['id'],old['subject_key']))
    for row in rows:
        if subject is not None and (row['student_id'],row['course_id']) != subject:
            continue
        total, attended = row['total_classes'], row['attended_classes']
        if type(total) is not int or type(attended) is not int or total <= 0 or not 0 <= attended <= total:
            continue
        percentage=attended*100/total
        matched=percentage < config['threshold']
        identity = f'{row["student_name"]} ({row["student_id"]})\n' if rule['kind']=='attendance_faculty' else ''
        _set_match(conn,rule,f'{row["student_id"]}:{row["course_id"]}',matched,f'attendance:{row["student_id"]}:{row["course_id"]}:{row["total_classes"]}',
          f'Attendance below {config["threshold"]:g}%',identity + f'{row["course_id"]} — {row["course_name"]}: {row["attended_classes"]}/{row["total_classes"]} ({percentage:.2f}%) is below your {config["threshold"]:g}% alert threshold.')


def _class_reminder_body(conn, rule, config, now):
    if now.weekday() >= 5 or not _scheduled_due('class_reminder', config, now):
        return
    identity = conn.execute('SELECT role FROM users WHERE user_id=?', (rule['user_id'],)).fetchone()
    if not identity:
        return
    courses = _reminder_courses(conn, rule['user_id'], identity['role'])
    selected = _selected(config, 'course_id', 'course_ids')
    ids = [c['course_id'] for c in courses if selected is None or c['course_id'] in selected]
    if not ids:
        return
    day = now.date().isoformat()
    rows = conn.execute(f'''SELECT t.course_id,c.course_name,t.start_time,t.end_time
        FROM course_timetable t JOIN courses c ON c.course_id=t.course_id
        WHERE t.course_id IN ({','.join('?' for _ in ids)}) AND t.weekday=?
        AND t.effective_from<=? AND t.effective_to>=? AND t.start_time>?
        ORDER BY t.start_time,t.course_id,t.timetable_id''', (*ids, now.weekday(), day, day, now.strftime('%H:%M'))).fetchall()
    if rows and (_match_mode(config) == 'any' or set(ids).issubset({row['course_id'] for row in rows})):
        body = f'Your upcoming classes on {day} ({now.strftime("%A")}), IST:\n' + '\n'.join(
            f'{r["start_time"]}–{r["end_time"]} · {r["course_id"]} — {r["course_name"]}' for r in rows)
        return body


def _missing_attendance_body(conn, rule, config, now):
    from tools.attendance import CUTOVER
    day = now.date().isoformat()
    if not _scheduled_due('attendance_missing', config, now) or now.weekday() >= 5 or day < CUTOVER:
        return None
    selected = _selected(config, 'course_id', 'course_ids')
    courses = _faculty_courses(conn, rule['user_id'])
    ids = [row['course_id'] for row in courses if selected is None or row['course_id'] in selected]
    if not ids:
        return None
    rows = conn.execute(f'''SELECT t.course_id,c.course_name,t.start_time,t.end_time
        FROM course_timetable t JOIN courses c ON c.course_id=t.course_id
        JOIN users u ON u.user_id=c.faculty_id AND u.role='faculty'
        WHERE c.faculty_id=? AND c.course_id IN ({','.join('?' for _ in ids)}) AND t.weekday=?
        AND t.effective_from<=? AND t.effective_to>=? AND t.end_time<=?
        AND EXISTS(SELECT 1 FROM course_registrations cr WHERE cr.course_id=c.course_id AND cr.status='enrolled')
        AND NOT EXISTS(SELECT 1 FROM attendance_sessions s WHERE s.timetable_id=t.timetable_id AND s.day=? AND s.revision>0)
        ORDER BY t.start_time,t.course_id,t.timetable_id''',
        (rule['user_id'],*ids,now.weekday(),day,day,now.strftime('%H:%M'),day)).fetchall()
    if not rows or (_match_mode(config) == 'all' and not set(ids).issubset({row['course_id'] for row in rows})):
        return None
    return f'Attendance has not been saved for these classes on {day} (IST):\n' + '\n'.join(
        f'{r["start_time"]}–{r["end_time"]} · {r["course_id"]} — {r["course_name"]}' for r in rows)


def _menu_body(conn, rule, config, now):
    if not _scheduled_due('menu_match', config, now):
        return None
    matches=[]
    patterns=[(dish,re.compile(r'(?<!\w)'+re.escape(dish)+r'(?!\w)',re.I)) for dish in _menu_dishes(config)]
    mode = _menu_match_mode(config)
    for meal in config['meals']:
        rows=conn.execute(f"SELECT plan_id,{meal} AS items FROM mess_menus WHERE weekday=? AND plan_id IN ({','.join('?'*len(config['plans']))})",(now.weekday(),*config['plans'])).fetchall()
        for row in rows:
            found=[dish for dish,pattern in patterns if row['items'] and pattern.search(row['items'])]
            qualifies = bool(found) if mode == 'any' else len(found) == len(patterns)
            if qualifies: matches.append((row['plan_id'],meal,row['items'],found))
    if matches:
        return '\n'.join(f"{plan.replace('_',' ')} {meal.replace('_',' ')} ({', '.join(found)}): {items}" for plan,meal,items,found in matches)


SCHEDULE_EVENTS = {'class-reminder':('class_reminder',_class_reminder_body),
                   'attendance-missing':('attendance_missing',_missing_attendance_body),
                   'menu':('menu_match',_menu_body)}


def refresh_scheduled_email(conn, message, now=None):
    """Revalidate/defer unsent reminders before acquiring the SMTP lease."""
    if message['event_key'].startswith('topic-research:'):
        from tools.topic_research import prepare_email
        return prepare_email(conn, message)
    if message['event_key'].startswith(('research:', 'subject-update:')):
        from tools.research_updates import refresh_email
        return refresh_email(conn, message, _local_now(now or college_now()))
    match = re.fullmatch(r'alert:(\d+):(class-reminder|attendance-missing|menu):(\d{4}-\d{2}-\d{2})', message['event_key'])
    if not match:
        return True
    now = _local_now(now or college_now())
    kind, render = SCHEDULE_EVENTS[match[2]]
    rule = conn.execute('SELECT * FROM alert_rules WHERE id=? AND user_id=? AND kind=? AND active=1',
                        (int(match[1]),message['user_id'],kind)).fetchone()
    body = None
    if rule and match[3] == now.date().isoformat():
        config = json.loads(rule['config_json'])
        if not _scheduled_due(kind, config, now):
            target = datetime.combine(now.date(), datetime.strptime(_reminder_time(kind, config), '%H:%M').time(), tzinfo=now.tzinfo)
            conn.execute("UPDATE notification_outbox SET status='pending',next_attempt=?,lease_until=NULL WHERE id=?", (target.timestamp(),message['id']))
            return False
        body = render(conn, rule, config, now)
    if not body:
        conn.execute("UPDATE notification_outbox SET status='suppressed',lease_until=NULL WHERE id=?", (message['id'],))
        return False
    message['body'] = body
    conn.execute('UPDATE notification_outbox SET body=? WHERE id=?',(body,message['id']))
    conn.execute('UPDATE alert_occurrences SET body=? WHERE event_key=?',(body,message['event_key']))
    return True


# Compatibility for internal callers/tests of the previous delivery hook.
refresh_missing_attendance_email = refresh_scheduled_email


def evaluate_due(conn, now=None, only_rule=None, attendance_subject=None):
    now=now or college_now(); now=now if isinstance(now,datetime) else datetime.combine(now,datetime.min.time(),tzinfo=college_now().tzinfo)
    now = _local_now(now)
    rows=conn.execute('SELECT * FROM alert_rules WHERE active=1'+(' AND id=?' if only_rule else ''), (() if only_rule is None else (only_rule,))).fetchall()
    if only_rule is None and attendance_subject is None:
        from tools.research_updates import queue_digests
        queue_digests(conn, now)
    for row in rows:
        rule=dict(row); config=json.loads(rule['config_json']); kind=rule['kind']
        if kind == 'class_reminder':
            body = _class_reminder_body(conn, rule, config, now)
            if body:
                _emit(conn,rule,f'class-reminder:{now.date().isoformat()}',f'Your classes on {now.date().isoformat()}',body)
        elif kind == 'attendance_missing':
            # Attendance saves call evaluation before advancing the session revision.
            # Let the worker inspect the committed session, never this partial write.
            if attendance_subject is not None:
                continue
            body = _missing_attendance_body(conn, rule, config, now)
            if body:
                _emit(conn,rule,f'attendance-missing:{now.date().isoformat()}',f'Unmarked attendance for {now.date().isoformat()}',body)
        elif kind.startswith('attendance_'):
            _attendance(conn,rule,config,attendance_subject)
        elif kind=='classroom_free':
            start=datetime.fromisoformat(config['booking_date']+'T'+config['start_time']).replace(tzinfo=now.tzinfo)
            if now >= start:
                conn.execute("UPDATE alert_rules SET active=0,state='expired',updated_at=CURRENT_TIMESTAMP WHERE id=?",(rule['id'],)); continue
            rooms = _selected(config, 'classroom_id', 'classroom_ids') or []
            available = [room for room in rooms if not conn.execute('SELECT 1 FROM classroom_bookings WHERE classroom_id=? AND booking_date=? AND start_time<? AND end_time>?',(room,config['booking_date'],config['end_time'],config['start_time'])).fetchone()]
            matched = bool(available) if _match_mode(config) == 'any' else len(available) == len(rooms)
            if matched:
                names = ', '.join(available)
                _emit(conn,rule,'classroom-free',f'{names} available',f'{names} free on {config["booking_date"]} from {config["start_time"]} to {config["end_time"]}.')
                conn.execute("UPDATE alert_rules SET active=0,state='completed',updated_at=CURRENT_TIMESTAMP WHERE id=?",(rule['id'],))
        elif kind=='menu_match':
            body = _menu_body(conn, rule, config, now)
            if body:
                _emit(conn,rule,f'menu:{now.date().isoformat()}',"Today's matching menu dishes",body)


def leave_event(conn, leave_id, event):
    row=conn.execute('SELECT l.*,s.department FROM leave_requests l JOIN students s ON s.student_id=l.student_id WHERE l.leave_id=?',(leave_id,)).fetchone()
    if not row:return
    for rule in conn.execute("SELECT * FROM alert_rules WHERE active=1 AND kind IN ('leave_student','leave_faculty')").fetchall():
        rule=dict(rule); config=json.loads(rule['config_json'])
        if rule['kind']=='leave_student' and rule['user_id']==row['student_id'] and event in config['statuses']:
            body=f'Leave request #{leave_id} {event}.\nDates: {row["from_date"]} to {row["to_date"]}'
            if event=='approved': body+='\nLeave purpose: '+(row['reason'] or 'Not provided')
            if event=='rejected': body+='\nReason: '+(row['rejection_reason'] or 'Not provided')
            _emit(conn,rule,f'leave:{leave_id}:{event}',f'Leave request {event}',body)
        elif rule['kind']=='leave_faculty' and event=='pending':
            faculty=conn.execute('SELECT department FROM faculty WHERE faculty_id=?',(rule['user_id'],)).fetchone()
            if faculty and faculty['department']==row['department']:
                _emit(conn,rule,f'leave:{leave_id}:pending','New leave request',f'Leave request #{leave_id} is pending review for {row["from_date"]} to {row["to_date"]}.')


def chat_update(user, role, update, session_id=None):
    if not isinstance(update,dict) or update.get('action') not in {'create','update','delete'}: raise OperationError('Alert request is invalid.')
    if update['action']=='delete':
        if set(update)=={'action','id'}:
            identifiers = [update['id']]
        elif set(update)=={'action','ids'}:
            identifiers = update['ids']
        else:
            raise OperationError('Specify an alert ID or a list of alert IDs to delete.')
        if not session_id:
            raise OperationError('Open a conversation to confirm alert deletion.')
        from tools.confirmations import propose
        confirmation = propose({'type':'delete_alerts','ids':identifiers},user,role,session_id)
        return {'response':confirmation['summary']+'\nPlease confirm or cancel below.', 'actions':[], 'confirmation':confirmation}
    allowed={'action','kind','config'} if update['action']=='create' else {'action','id','kind','config'}
    if set(update)!=allowed or not isinstance(update.get('kind'),str) or (update['action']=='update' and type(update.get('id')) is not int): raise OperationError('Alert request needs a valid type and settings.')
    result=create_alert(user,role,update['kind'],update['config']) if update['action']=='create' else update_alert(user,role,update['id'],update['kind'],update['config'])
    return {'response':f"Alert #{result['id']} saved: {result['summary']}.", 'actions':['alert_saved']}
