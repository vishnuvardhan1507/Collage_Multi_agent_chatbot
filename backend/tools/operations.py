"""The only agent-accessible writes; ownership comes from authenticated identity."""
from contextlib import closing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

from tools.db_tool import FACULTY_SCOPE, get_connection, verify_identity


class OperationError(ValueError):
    pass


def eligible_courses(user_id, role):
    """Return unregistered courses in the authenticated student's department
    whose recorded prerequisites are all completed.

    This is a read-only inquiry. Registration writes continue through the
    existing transactional register_course operation.
    """
    if role != "student":
        raise PermissionError("Course registration eligibility is available to students only.")
    with closing(get_connection()) as conn:
        conn.execute("BEGIN")
        verify_identity(conn, user_id, role)
        student = conn.execute(
            "SELECT department FROM students WHERE student_id = ?", (user_id,)
        ).fetchone()
        if not student:
            raise PermissionError("Student account could not be verified.")
        courses = conn.execute("""
            SELECT c.course_id, c.course_name
            FROM courses c
            WHERE c.department = ?
              AND NOT EXISTS (
                  SELECT 1 FROM course_registrations cr
                  WHERE cr.student_id = ? AND cr.course_id = c.course_id
              )
            ORDER BY c.course_id
        """, (student["department"], user_id)).fetchall()
        eligible = []
        blocked = 0
        for course in courses:
            missing = conn.execute("""
                SELECT p.prerequisite_course_id
                FROM prerequisites p
                WHERE p.course_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM course_registrations cr
                      WHERE cr.student_id = ?
                        AND cr.course_id = p.prerequisite_course_id
                        AND cr.status = 'completed'
                  )
                ORDER BY p.prerequisite_course_id
            """, (course["course_id"], user_id)).fetchall()
            if missing:
                blocked += 1
            else:
                eligible.append({"course_id": course["course_id"], "course_name": course["course_name"]})
        return {
            "department": student["department"],
            "courses": eligible,
            "unregistered_count": len(courses),
            "blocked_by_prerequisites": blocked,
        }


def college_today():
    zone = current_app.config.get("COLLEGE_TIMEZONE", "Asia/Kolkata") if has_app_context() else "Asia/Kolkata"
    return datetime.now(ZoneInfo(zone)).date()


def parse_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise OperationError("Provide a valid date in YYYY-MM-DD format.")


def required(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OperationError(f"Please provide {key.replace('_', ' ')}.")
    return value.strip()


def validate_room_requirements(requirements):
    allowed = {'min_capacity', 'has_ac', 'min_projectors', 'min_computers'}
    if not isinstance(requirements, dict) or set(requirements) - allowed:
        raise OperationError('Invalid classroom requirements.')
    for key, value in requirements.items():
        if key == 'has_ac':
            if type(value) is not bool:
                raise OperationError('AC requirement must be true or false.')
        elif type(value) is not int or value < 1:
            raise OperationError('Required seats, projectors and computers must be positive whole numbers.')
    return requirements


def check_room_requirements(room, requirements):
    columns = {'min_capacity':'capacity', 'has_ac':'has_ac', 'min_projectors':'projector_count', 'min_computers':'computer_count'}
    for key, value in validate_room_requirements(requirements).items():
        actual = room[columns[key]]
        label = columns[key].replace('_', ' ')
        if actual is None:
            raise OperationError(f'This room has unknown {label}; the requirement cannot be confirmed.')
        if (key == 'has_ac' and actual != int(value)) or (key != 'has_ac' and actual < value):
            raise OperationError(f'This room does not meet the requested {label} requirement.')


def compile_operation(operation, user_id, role):
    if not isinstance(operation, dict):
        raise OperationError("Provide a supported operation and its required fields.")
    kind = operation.get("type")
    capabilities = {"submit_leave": "student", "delete_leave": "student", "register_course": "student", "review_leave": "faculty", "book_classroom": "faculty"}
    if kind not in capabilities or capabilities[kind] != role:
        raise PermissionError("Your role cannot perform this operation.")
    fields = {
        "submit_leave": {"from_date", "to_date", "reason"},
        "delete_leave": {"leave_id"},
        "register_course": {"course_id"},
        "review_leave": {"leave_id", "status", "rejection_reason"},
        "book_classroom": {"classroom_id", "booking_date", "start_time", "end_time", "purpose", "requirements"},
    }
    if set(operation) - fields[kind] - {"type"}:
        raise OperationError("Operation contains unsupported fields; identity is supplied by the server.")
    values = {key: required(operation, key) for key in fields[kind] if key not in {"leave_id", "rejection_reason", "requirements"}}
    if kind == "submit_leave":
        start, end = parse_date(values["from_date"]), parse_date(values["to_date"])
        if end < start or start < college_today():
            raise OperationError("Leave dates must be ordered and cannot start in the past.")
        return "INSERT INTO leave_requests(student_id, from_date, to_date, reason, status) VALUES (?, ?, ?, ?, 'pending')", (user_id, start.isoformat(), end.isoformat(), values["reason"])
    if kind == "delete_leave":
        leave_id = operation.get("leave_id")
        if type(leave_id) is not int or leave_id < 1:
            raise OperationError("Please identify one leave request by its numeric leave ID.")
        return "DELETE FROM leave_requests WHERE leave_id=? AND student_id=? AND to_date>=?", (leave_id, user_id, college_today().isoformat())
    if kind == "register_course":
        return "INSERT INTO course_registrations(student_id, course_id, status) VALUES (?, ?, 'enrolled')", (user_id, values["course_id"])
    if kind == "review_leave":
        leave_id = operation.get("leave_id")
        if type(leave_id) is not int or leave_id < 1:
            raise OperationError("Please identify one leave request by its numeric leave ID.")
        if values["status"] not in {"approved", "rejected"}:
            raise OperationError("Leave status must be approved or rejected.")
        rejection_reason = required(operation, 'rejection_reason') if values['status'] == 'rejected' else None
        if rejection_reason and len(rejection_reason) > 1000:
            raise OperationError('Rejection reason must be 1000 characters or fewer.')
        sql = f"UPDATE leave_requests SET status = ?, reviewed_by = ?, rejection_reason = ? WHERE leave_id = ? AND status = 'pending' AND student_id IN ({FACULTY_SCOPE})"
        return sql, (values["status"], user_id, rejection_reason, leave_id, user_id)
    validate_room_requirements(operation.get("requirements", {}))
    booking_date = parse_date(values["booking_date"])
    if booking_date < college_today():
        raise OperationError("Classroom bookings cannot be in the past.")
    for key in ("start_time", "end_time"):
        try:
            if datetime.strptime(values[key], "%H:%M").strftime("%H:%M") != values[key]:
                raise ValueError()
        except ValueError:
            raise OperationError("Provide start and end times in HH:MM format.")
    if values["start_time"] >= values["end_time"]:
        raise OperationError("End time must be later than start time on the same day.")
    return "INSERT INTO classroom_bookings(classroom_id, faculty_id, booking_date, start_time, end_time, purpose) VALUES (?, ?, ?, ?, ?, ?)", (values["classroom_id"], user_id, booking_date.isoformat(), values["start_time"], values["end_time"], values["purpose"])


def resolve_leave_deletion(operation, user_id, role):
    """Resolve a model-supplied selector using only authenticated, current records."""
    if role != 'student':
        raise PermissionError('Your role cannot perform this operation.')
    if set(operation) - {'type', 'leave_id', 'description'}:
        raise OperationError('Deletion contains unsupported fields.')
    leave_id, description = operation.get('leave_id'), operation.get('description')
    if leave_id is not None:
        if type(leave_id) is not int or leave_id < 1:
            raise OperationError('Please provide a numeric leave request ID.')
        predicate, selector = 'leave_id=?', leave_id
    else:
        description = required(operation, 'description')
        predicate, selector = 'instr(lower(reason), lower(?)) > 0', description
    with closing(get_connection()) as conn:
        verify_identity(conn, user_id, role)
        rows = conn.execute(f"SELECT leave_id, student_id, from_date, to_date, reason, status FROM leave_requests WHERE student_id=? AND {predicate} ORDER BY leave_id", (user_id, selector)).fetchall()
    if not rows:
        raise OperationError('No matching leave request was found in your account.')
    if len(rows) > 1:
        choices = '\n'.join(f"- #{row['leave_id']}: {row['from_date']} to {row['to_date']} ? {row['reason']}" for row in rows)
        raise OperationError('More than one leave request matches. Which leave ID do you want to delete?\n' + choices)
    evidence = dict(rows[0])
    evidence['can_delete'] = parse_date(evidence['to_date']) >= college_today()
    if not evidence['can_delete']:
        raise OperationError(f"Leave request #{evidence['leave_id']} has ended. Past leave requests cannot be deleted.")
    return {'type': 'delete_leave', 'leave_id': evidence['leave_id']}, evidence


def execute_operation(operation, user_id, role):
    with closing(get_connection()) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return execute_operation_in_transaction(conn, operation, user_id, role)


def execute_operation_in_transaction(conn, operation, user_id, role):
    sql, params = compile_operation(operation, user_id, role)
    kind = operation["type"]
    verify_identity(conn, user_id, role)
    if kind == "submit_leave":
        if conn.execute("SELECT 1 FROM leave_requests WHERE student_id=? AND status IN ('pending','approved') AND from_date<=? AND to_date>=?", (user_id, params[2], params[1])).fetchone():
            raise OperationError("These dates overlap an existing pending or approved leave request.")
    if kind == "delete_leave":
        row = conn.execute("SELECT to_date FROM leave_requests WHERE leave_id=? AND student_id=?", (operation['leave_id'], user_id)).fetchone()
        if not row:
            raise OperationError("Leave request not found in your account.")
        if parse_date(row['to_date']) < college_today():
            raise OperationError("Past leave requests cannot be deleted.")
    if kind == "register_course":
        course = params[1]
        if not conn.execute("SELECT 1 FROM courses WHERE course_id = ?", (course,)).fetchone():
            raise OperationError("Course not found.")
        if conn.execute("SELECT 1 FROM course_registrations WHERE student_id = ? AND course_id = ?", (user_id, course)).fetchone():
            raise OperationError("You already have a registration for this course.")
        missing = conn.execute("""SELECT prerequisite_course_id FROM prerequisites p WHERE course_id = ?
            AND NOT EXISTS (SELECT 1 FROM course_registrations cr WHERE cr.student_id = ?
            AND cr.course_id = p.prerequisite_course_id AND cr.status = 'completed')""", (course, user_id)).fetchall()
        if missing:
            raise OperationError("Complete these prerequisites first: " + ", ".join(row[0] for row in missing))
    if kind == "book_classroom":
        room, _, day, start, end, _ = params
        room_record = conn.execute("SELECT * FROM classrooms WHERE classroom_id = ?", (room,)).fetchone()
        if not room_record:
            raise OperationError("Classroom not found.")
        check_room_requirements(room_record, operation.get("requirements", {}))
        if conn.execute("SELECT 1 FROM classroom_bookings WHERE classroom_id = ? AND booking_date = ? AND start_time < ? AND end_time > ?", (room, day, end, start)).fetchone():
            raise OperationError("That classroom is already booked during the requested time.")
    cursor = conn.execute(sql, params)
    if kind == "review_leave" and cursor.rowcount != 1:
        raise OperationError("No pending leave request was found within your review scope.")
    record_id = operation.get("leave_id") if kind in {"review_leave", "delete_leave"} else cursor.lastrowid
    if kind == 'submit_leave':
        from tools.alerts import leave_event
        leave_event(conn, record_id, 'pending')
    if kind in {'book_classroom','review_leave'}:
        from tools.notifications import operation_notification
        operation_notification(conn,kind,record_id)
    return {"rows_affected": cursor.rowcount, "record_id": record_id, "action": kind}


def validate_projection_request(request):
    if isinstance(request, dict) and request.get('mode') == 'class_count':
        if set(request) - {'mode', 'course', 'missed_classes'}:
            raise OperationError('Specify the course and number of classes you would miss.')
        if not isinstance(request.get('course'), str) or not 1 <= len(request['course'].strip()) <= 200:
            raise OperationError('Which course would you like to project attendance for?')
        if type(request.get('missed_classes')) is not int or not 1 <= request['missed_classes'] <= 366:
            raise OperationError('How many classes would you miss? Choose a whole number from 1 to 366.')
        return None, None
    if not isinstance(request, dict) or set(request) - {'start_date', 'end_date', 'include_courses'}:
        raise OperationError('Provide only a projection start date, end date, and optional course details flag.')
    start, end = parse_date(request.get('start_date')), parse_date(request.get('end_date'))
    if start < college_today() or end < start:
        raise OperationError('Choose an ordered attendance projection range starting today or later.')
    if (end - start).days >= 366:
        raise OperationError('Choose an attendance projection range of at most 366 days.')
    if type(request.get('include_courses', False)) is not bool:
        raise OperationError('The course details flag must be true or false.')
    return start, end


def project_attendance(user_id, role, day):
    if role != 'student':
        raise PermissionError('Attendance projections are available for your own student account.')
    # Preserve the original single-date tool interface.
    request = {'start_date': day, 'end_date': day, 'include_courses': True} if isinstance(day, str) else day
    start, end = validate_projection_request(request)
    if request.get('mode') == 'class_count':
        with closing(get_connection()) as conn:
            conn.execute('BEGIN')
            verify_identity(conn, user_id, role)
            courses = conn.execute('''SELECT DISTINCT c.course_id,c.course_name FROM course_registrations cr
                JOIN courses c ON c.course_id=cr.course_id
                WHERE cr.student_id=? AND cr.status='enrolled'
                AND (lower(c.course_id)=lower(?) OR lower(trim(c.course_name))=lower(?))''',
                (user_id, request['course'].strip(), request['course'].strip())).fetchall()
            if len(courses) != 1:
                raise OperationError('Please specify the ID of one of your currently enrolled courses for this projection.')
            course = dict(courses[0])
            rows = conn.execute('SELECT attended_classes,total_classes FROM attendance WHERE student_id=? AND course_id=?',
                                (user_id, course['course_id'])).fetchall()
            attended, total = tuple(rows[0]) if len(rows) == 1 else (None, None)
            valid = type(attended) is int and type(total) is int and total > 0 and 0 <= attended <= total
            missed = request['missed_classes']
            return {'mode': 'class_count', **course, 'attended_classes': attended, 'total_classes': total,
                    'missed_classes': missed,
                    'current_percentage': round(attended*100.0/total, 2) if valid else None,
                    'projected_percentage': round(attended*100.0/(total+missed), 2) if valid else None,
                    'assumption': 'This assumes only these additional classes are missed; it does not change your attendance records or predict class dates.'}
    days = [start + timedelta(days=i) for i in range((end-start).days+1)]
    with closing(get_connection()) as conn:
        conn.execute('BEGIN')
        verify_identity(conn, user_id, role)
        courses = conn.execute("""SELECT DISTINCT c.course_id,c.course_name FROM course_registrations cr
            JOIN courses c ON c.course_id=cr.course_id
            WHERE cr.student_id=? AND cr.status='enrolled' ORDER BY c.course_id""", (user_id,)).fetchall()
        result = []
        for course in courses:
            item = dict(course)
            attendance = conn.execute('SELECT attended_classes,total_classes FROM attendance WHERE student_id=? AND course_id=?', (user_id,item['course_id'])).fetchall()
            attended, total = tuple(attendance[0]) if len(attendance)==1 else (None,None)
            valid = type(attended) is int and type(total) is int and total > 0 and 0 <= attended <= total
            schedule = conn.execute('SELECT weekday,start_time,end_time,effective_from,effective_to,is_demo FROM course_timetable WHERE course_id=? AND effective_from<=? AND effective_to>=?',
                                    (item['course_id'],end.isoformat(),start.isoformat())).fetchall()
            missing_days = [d.isoformat() for d in days if d.weekday()<5 and not any(t['effective_from'] <= d.isoformat() <= t['effective_to'] for t in schedule)]
            missed = sum(1 for d in days if d.weekday()<5 for t in schedule if t['weekday']==d.weekday() and t['effective_from']<=d.isoformat()<=t['effective_to'])
            known = not missing_days
            reasons = []
            if not valid:
                reasons.append('missing, duplicate, or invalid attendance counts')
            if not known:
                reasons.append('unknown timetable coverage for ' + ', '.join(missing_days))
            item.update(attended_classes=attended, total_classes=total,
                        missed_classes=missed if known else None, schedule_known=known,
                        demo_schedule=any(t['is_demo'] for t in schedule), missing_schedule_dates=missing_days,
                        current_percentage=round(attended*100.0/total,2) if valid else None,
                        projected_percentage=round(attended*100.0/(total+missed),2) if valid and known else None,
                        unavailable_reason='; '.join(reasons) or None)
            result.append(item)
    counts_known = bool(result) and all(r['current_percentage'] is not None for r in result)
    projection_known = bool(result) and all(r['projected_percentage'] is not None for r in result)
    attended = sum(r['attended_classes'] for r in result) if counts_known else None
    total = sum(r['total_classes'] for r in result) if counts_known else None
    missed = sum(r['missed_classes'] for r in result) if result and all(r['schedule_known'] for r in result) else None
    overall = {'attended_classes':attended, 'total_classes':total, 'missed_classes':missed,
               'current_percentage':round(attended*100.0/total,2) if counts_known else None,
               'projected_percentage':round(attended*100.0/(total+missed),2) if projection_known else None}
    return {'projection_date':start.isoformat() if start==end else None,
            'start_date':start.isoformat(), 'end_date':end.isoformat(),
            'timezone':current_app.config.get('COLLEGE_TIMEZONE','Asia/Kolkata') if has_app_context() else 'Asia/Kolkata',
            'include_courses':request.get('include_courses',False), 'courses':result, 'overall':overall,
            'assumption':'Only scheduled classes missed on these dates are added to current totals. Other future attendance and unrecorded holidays or cancellations are not forecast.'}
