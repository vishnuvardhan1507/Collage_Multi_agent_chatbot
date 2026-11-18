"""Authenticated, read-only timetable retrieval and calendar calculations."""
from contextlib import closing
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import current_app

from tools.db_tool import get_connection, verify_identity
from tools.operations import OperationError, parse_date


def college_now():
    return datetime.now(ZoneInfo(current_app.config.get('COLLEGE_TIMEZONE', 'Asia/Kolkata')))


def validate_timetable_request(request, now=None):
    now = now or college_now()
    if not isinstance(request, dict):
        raise OperationError('Provide a structured timetable request.')
    mode = request.get('mode')
    fields = {'day': {'mode', 'date'}, 'week': {'mode', 'date'},
              'range': {'mode', 'start_date', 'end_date'}, 'next': {'mode'},
              'weekday': {'mode', 'weekday'}}
    if not isinstance(mode, str) or mode not in fields or set(request) - (fields[mode] | {'course_ids'}):
        raise OperationError('Choose a timetable day, week, date range, or next class.')
    selected = request.get('course_ids')
    if selected is not None and (not isinstance(selected, list) or not 1 <= len(selected) <= 20 or
            any(not isinstance(value, str) or not value.strip() or len(value) > 100 for value in selected)):
        raise OperationError('Choose a nonempty list of course IDs or names, or null for all your courses.')
    def resolve(value):
        if isinstance(value, str) and value in {'today', 'tomorrow', 'yesterday'}:
            return now.date() + timedelta(days={'today': 0, 'tomorrow': 1, 'yesterday': -1}[value])
        return parse_date(value)
    if mode == 'weekday':
        weekday = request.get('weekday')
        if type(weekday) is not int or not 0 <= weekday <= 6:
            raise OperationError('Weekday must be an integer from 0 (Monday) through 6 (Sunday).')
        start = end = now.date() + timedelta(days=(weekday-now.weekday()) % 7)
    elif mode == 'next':
        start, end = now.date(), now.date() + timedelta(days=6)
    elif mode == 'range':
        start, end = resolve(request.get('start_date')), resolve(request.get('end_date'))
    else:
        start = end = resolve(request.get('date'))
        if mode == 'week':
            start -= timedelta(days=start.weekday())
            end = start + timedelta(days=6)
    if end < start or (end-start).days >= 366:
        raise OperationError('Choose an ordered timetable range of at most 366 days.')
    return mode, start, end


def validate_teaching_load_request(request, now=None):
    now = now or college_now()
    if not isinstance(request, dict) or set(request) != {'date'}:
        raise OperationError('Provide a teaching-load date only.')
    value = request['date']
    if value == 'today':
        return now.date()
    if value == 'tomorrow':
        return now.date() + timedelta(days=1)
    if value == 'yesterday':
        return now.date() - timedelta(days=1)
    return parse_date(value)


def read_teaching_load(request, user_id, role, now=None):
    if role != 'faculty':
        raise PermissionError('Teaching-load information is available to faculty only.')
    now = now or college_now()
    day = validate_teaching_load_request(request, now)
    with closing(get_connection()) as conn:
        conn.execute('BEGIN')
        verify_identity(conn, user_id, role)
        rows = conn.execute('''SELECT c.course_id,c.course_name,t.weekday,t.start_time,t.end_time
            FROM courses c JOIN course_timetable t ON t.course_id=c.course_id
            WHERE c.faculty_id=? AND t.effective_from<=? AND t.effective_to>=?
            ORDER BY c.course_id,t.weekday,t.start_time''', (user_id, day.isoformat(), day.isoformat())).fetchall()
    courses = {}
    for row in rows:
        course = courses.setdefault(row['course_id'], {'course_id':row['course_id'], 'course_name':row['course_name'], 'weekly_sessions':[]})
        course['weekly_sessions'].append({'weekday':row['weekday'], 'start_time':row['start_time'], 'end_time':row['end_time']})
    return {'kind':'teaching_load', 'date':day.isoformat(), 'courses':list(courses.values())}


def format_teaching_load(result):
    if not result['courses']:
        return f"No current teaching courses with recorded timetable coverage were found for {result['date']}."
    names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
    lines = [f"Your current teaching courses on {result['date']}:"]
    for course in result['courses']:
        times = ', '.join(f"{names[item['weekday']]} {item['start_time']}–{item['end_time']}" for item in course['weekly_sessions'])
        lines.append(f"- {course['course_id']} — {course['course_name']} ({times})")
    return '\n'.join(lines)


def _authorized_courses(conn, user_id, role):
    if role == 'student':
        predicate = "EXISTS(SELECT 1 FROM course_registrations cr WHERE cr.course_id=c.course_id AND cr.student_id=? AND cr.status='enrolled')"
    elif role == 'faculty':
        predicate = "c.faculty_id=? AND EXISTS(SELECT 1 FROM course_registrations cr WHERE cr.course_id=c.course_id AND cr.status='enrolled')"
    else:
        raise PermissionError('Timetable access requires a student or faculty account.')
    rows = conn.execute('''SELECT c.course_id,c.course_name,f.name AS faculty_name,s.aliases_json
        FROM courses c LEFT JOIN faculty f ON f.faculty_id=c.faculty_id
        LEFT JOIN course_syllabi s ON s.course_id=c.course_id WHERE '''+predicate+' ORDER BY c.course_id', (user_id,)).fetchall()
    return [{**dict(row), 'aliases':json.loads(row['aliases_json'] or '[]')} for row in rows]


def timetable_catalog(user_id, role):
    with closing(get_connection()) as conn:
        verify_identity(conn, user_id, role)
        return [{key:row[key] for key in ('course_id','course_name','aliases')}
                for row in _authorized_courses(conn, user_id, role)]


def _select_courses(courses, selected):
    if selected is None:
        return courses
    normalize = lambda value: ' '.join(value.casefold().split())
    resolved = {}
    for value in selected:
        target = normalize(value)
        # An explicit ID is authoritative even if another course uses it as an alias.
        matches = [c for c in courses if normalize(c['course_id']) == target]
        if not matches:
            matches = [c for c in courses if target in
                       [normalize(name) for name in [c['course_name']] + c['aliases']]]
        if len(matches) > 1:
            raise OperationError('Which course do you mean by "'+value+'"? Choose '+
                                 ', '.join(c['course_id']+' — '+c['course_name'] for c in matches)+'.')
        if not matches:
            raise OperationError('No enrolled or assigned course matches "'+value+'" in your timetable. Please choose an authorized course ID or name.')
        resolved[matches[0]['course_id']] = matches[0]
    return list(resolved.values())


def read_timetable(request, user_id, role, now=None):
    zone = ZoneInfo(current_app.config.get('COLLEGE_TIMEZONE', 'Asia/Kolkata'))
    now = (now or college_now()).astimezone(zone)
    mode, start, end = validate_timetable_request(request, now)
    with closing(get_connection()) as conn:
        conn.execute('BEGIN')
        verify_identity(conn, user_id, role)
        courses = _select_courses(_authorized_courses(conn, user_id, role), request.get('course_ids'))
        courses = [{key:c[key] for key in ('course_id','course_name','faculty_name')} for c in courses]
        schedules = {c['course_id']: conn.execute('SELECT * FROM course_timetable WHERE course_id=? AND effective_from<=? AND effective_to>=?', (c['course_id'], end.isoformat(), start.isoformat())).fetchall() for c in courses}
    days = []
    for offset in range((end-start).days+1):
        day = start + timedelta(days=offset)
        sessions, missing = [], []
        if day.weekday()>=5:
            days.append({'date':day.isoformat(),'weekday':day.strftime('%A'),'count':0,'recorded_count':0,'sessions':[], 'missing_courses':[], 'holiday':True})
            continue
        for course in courses:
            active = [t for t in schedules[course['course_id']] if t['effective_from'] <= day.isoformat() <= t['effective_to']]
            if not active:
                missing.append(dict(course))
            for t in active:
                if t['weekday'] != day.weekday():
                    continue
                begins = datetime.fromisoformat(day.isoformat()+'T'+t['start_time']).replace(tzinfo=zone)
                ends = datetime.fromisoformat(day.isoformat()+'T'+t['end_time']).replace(tzinfo=zone)
                sessions.append({**dict(course), 'date': day.isoformat(), 'weekday': day.strftime('%A'), 'start_time': t['start_time'], 'end_time': t['end_time'],
                                 'status': 'ongoing' if begins <= now < ends else 'upcoming' if begins > now else 'ended'})
        sessions.sort(key=lambda r: (r['start_time'], r['end_time'], r['course_id']))
        days.append({'date': day.isoformat(), 'weekday': day.strftime('%A'), 'count': len(sessions) if courses and not missing else None,
                     'recorded_count': len(sessions), 'sessions': sessions, 'missing_courses': missing})
    ongoing = [r for d in days for r in d['sessions'] if r['status']=='ongoing']
    upcoming = [r for d in days for r in d['sessions'] if r['status']=='upcoming']
    first = upcoming[0] if upcoming else None
    next_sessions = [r for r in upcoming if (r['date'],r['start_time']) == (first['date'],first['start_time'])] if first else []
    return {'kind': 'timetable', 'mode': mode, 'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'timezone': str(zone), 'as_of': now.isoformat(), 'days': days, 'course_count': len(courses),
            'selected_courses': courses if request.get('course_ids') is not None else None,
            'ongoing': ongoing, 'next_sessions': next_sessions}


def timetable_context(result):
    """Persist resolved intent only; schedules are always read again under current permissions."""
    if not isinstance(result, dict) or result.get('kind') != 'timetable':
        return None
    mode = result['mode']
    request = {'mode': mode, 'course_ids': [c['course_id'] for c in result['selected_courses']]
               if result.get('selected_courses') is not None else None}
    if mode in {'day', 'week', 'weekday'}:
        request.update(mode='day' if mode == 'weekday' else mode, date=result['start_date'])
    elif mode == 'range':
        request.update(start_date=result['start_date'], end_date=result['end_date'])
    return {'request': request, 'start_date': result['start_date'], 'end_date': result['end_date'],
            'timezone': result['timezone']}


def valid_timetable_weekdays(answer, sessions):
    """Require an unambiguous correct date/day pair for every displayed session."""
    names = {'Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'}
    for session in sessions:
        expected = datetime.fromisoformat(session['date']).strftime('%A')
        found = False
        for line in answer.splitlines():
            if session['date'] not in line:
                continue
            mentioned = {name for name in names if name.casefold() in line.casefold()}
            if mentioned and mentioned != {expected}:
                return False
            found |= mentioned == {expected}
        if not found:
            return False
    return True


def format_timetable(result):
    def class_count(number):
        return f"{number} {'class' if number == 1 else 'classes'}"
    lines = [f"Timetable: {result['start_date']} to {result['end_date']} ({result['timezone']})."]
    if not result['course_count']:
        return '\n'.join(lines + ['No active enrolled or teaching courses were found.'])
    def table(rows):
        return ['| Date | Day | Course | Start | End | Faculty |', '| --- | --- | --- | --- | --- | --- |'] + [
            f"| {r['date']} | {datetime.fromisoformat(r['date']).strftime('%A')} | {r['course_id']} — {r['course_name']} | {r['start_time']} | {r['end_time']} | {r['faculty_name'] or 'Unknown'} |" for r in rows]
    if result.get('selected_courses') and result['mode'] in {'week','range'}:
        sessions = [session for day in result['days'] for session in day['sessions']]
        missing = [day for day in result['days'] if day['missing_courses']]
        lines.append('Courses: ' + ', '.join(c['course_id']+' — '+c['course_name'] for c in result['selected_courses']))
        lines.append(class_count(len(sessions)) + (' recorded; full count unavailable.' if missing else ' scheduled.'))
        if sessions:
            lines.extend(table(sessions))
        elif not missing:
            lines.append('No classes scheduled for the selected courses in this period.')
        for day in missing:
            lines.append(day['date'] + ': timetable coverage unavailable for ' + ', '.join(c['course_id'] for c in day['missing_courses']) + '.')
        return '\n\n'.join(lines[:2]) + '\n' + '\n'.join(lines[2:])
    if result['mode']=='next':
        lines.append('As of ' + result['as_of'])
        if result['ongoing']:
            lines += ['Currently ongoing:', *table(result['ongoing'])]
        else:
            lines.append('No ongoing session is recorded.')
        if result['next_sessions']:
            lines += ['Next recorded class:', *table(result['next_sessions'])]
        else:
            lines.append('No upcoming session is recorded in this seven-calendar-day window.')
    for day in result['days']:
        if result['mode'] != 'next':
            count = class_count(day['count']) + ' scheduled' if day['count'] is not None else class_count(day['recorded_count']) + ' recorded; full count unavailable'
            lines.append(f"{day['date']} ({day['weekday']}): {count}.")
            if day.get('holiday'):
                lines.append('Holiday—attendance not required.')
            if day['sessions']:
                lines += table(day['sessions'])
            elif day['count'] == 0:
                lines.append('No classes scheduled.')
        if day['missing_courses']:
            lines.append(day['date'] + ': timetable coverage unavailable for ' + ', '.join(c['course_id'] for c in day['missing_courses']) + '.')
    lines.append('This is the recorded schedule; unrecorded holidays or cancellations are not reflected.')
    return lines[0] + '\n\n' + '\n'.join(lines[1:])
