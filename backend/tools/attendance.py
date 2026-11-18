"""Faculty-owned session attendance. All writes use one SQLite transaction."""
import json
from contextlib import closing
from datetime import date

from tools.db_tool import get_connection, verify_identity
from tools.timetable import college_now
from tools.operations import OperationError

CUTOVER = '2026-10-19'


class AttendanceConflict(OperationError):
    pass


def migrate_attendance(conn):
    statements = [
        '''CREATE TABLE attendance_balances(student_id TEXT REFERENCES students(student_id),
           course_id TEXT REFERENCES courses(course_id), attended INTEGER NOT NULL CHECK(attended>=0),
           total INTEGER NOT NULL CHECK(total>=attended), PRIMARY KEY(student_id,course_id))''',
        '''CREATE TABLE attendance_sessions(id INTEGER PRIMARY KEY, timetable_id INTEGER NOT NULL REFERENCES course_timetable(timetable_id),
           day TEXT NOT NULL CHECK(day>='2026-10-19' AND strftime('%w',day) NOT IN ('0','6')),
           course_id TEXT NOT NULL REFERENCES courses(course_id), start_time TEXT NOT NULL,end_time TEXT NOT NULL,
           revision INTEGER NOT NULL DEFAULT 0, UNIQUE(timetable_id,day),UNIQUE(course_id,day,start_time))''',
        '''CREATE TABLE attendance_marks(session_id INTEGER REFERENCES attendance_sessions(id),
           student_id TEXT REFERENCES students(student_id),present INTEGER NOT NULL CHECK(present IN (0,1)),
           PRIMARY KEY(session_id,student_id))''',
        '''CREATE TABLE attendance_audit(id INTEGER PRIMARY KEY,session_id INTEGER NOT NULL REFERENCES attendance_sessions(id),
           faculty_id TEXT NOT NULL REFERENCES faculty(faculty_id),revision INTEGER NOT NULL,
           before_marks TEXT NOT NULL,after_marks TEXT NOT NULL,created_at TEXT NOT NULL, UNIQUE(session_id,revision))''',
        '''CREATE TABLE attendance_warnings(student_id TEXT REFERENCES students(student_id),course_id TEXT REFERENCES courses(course_id),
           below INTEGER NOT NULL CHECK(below IN (0,1)),generation INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(student_id,course_id))''',
        '''CREATE TABLE notification_outbox(id INTEGER PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,
           user_id TEXT NOT NULL REFERENCES users(user_id),subject TEXT NOT NULL,body TEXT NOT NULL,
           status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','failed','suppressed')),
           attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,lease_until REAL,
           student_id TEXT,course_id TEXT,last_error TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,sent_at TEXT)'''
    ]
    for statement in statements:
        conn.execute(statement.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ',1))
    # Invalid/missing historical records remain unresolved, never silently become zero.
    conn.execute('''INSERT OR IGNORE INTO attendance_balances
        SELECT student_id,course_id,attended_classes,total_classes FROM attendance
        GROUP BY student_id,course_id HAVING count(*)=1 AND typeof(attended_classes)='integer'
        AND typeof(total_classes)='integer' AND attended_classes>=0 AND total_classes>=attended_classes''')
    conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES(12)')


def faculty(conn, user, role):
    verify_identity(conn,user,role)
    if role!='faculty':
        raise PermissionError('Only teaching faculty can mark attendance.')


def parse_day(value):
    try:
        day=date.fromisoformat(value)
    except (TypeError,ValueError):
        raise OperationError('Choose a valid date.')
    if day.isoformat()!=value or value<CUTOVER:
        raise OperationError('Daily attendance starts on October 19, 2026.')
    if day>college_now().date():
        raise OperationError('Future attendance cannot be marked.')
    return day


def session_record(conn,user,timetable_id,day):
    row=conn.execute('''SELECT t.*,c.course_name FROM course_timetable t JOIN courses c ON c.course_id=t.course_id
        WHERE t.timetable_id=? AND c.faculty_id=?''',(timetable_id,user)).fetchone()
    if not row:
        raise PermissionError('This session is not assigned to you.')
    if day.weekday()>=5:
        raise OperationError('Holiday—attendance not required.')
    if row['weekday']!=day.weekday() or not row['effective_from']<=day.isoformat()<=row['effective_to']:
        raise OperationError('No effective timetable session on this date.')
    if day==college_now().date() and row['start_time']>college_now().strftime('%H:%M'):
        raise OperationError('This class has not started yet.')
    return row


def sessions(user,role,day_text=None):
    now=college_now();day=parse_day(day_text or now.date().isoformat())
    with closing(get_connection()) as conn:
        faculty(conn,user,role)
        rows=[] if day.weekday()>=5 else [dict(r) for r in conn.execute('''SELECT t.timetable_id,t.course_id,c.course_name,t.start_time,t.end_time,
            COALESCE(s.revision,0) AS revision FROM course_timetable t JOIN courses c ON c.course_id=t.course_id
            LEFT JOIN attendance_sessions s ON s.timetable_id=t.timetable_id AND s.day=?
            WHERE c.faculty_id=? AND t.weekday=? AND t.effective_from<=? AND t.effective_to>=?
            ORDER BY t.start_time,t.course_id''',(day.isoformat(),user,day.weekday(),day.isoformat(),day.isoformat()))]
    for row in rows:
        row['can_mark']=day<now.date() or row['start_time']<=now.strftime('%H:%M')
    return dict(date=day.isoformat(),today=now.date().isoformat(),cutover=CUTOVER,holiday=day.weekday()>=5,sessions=rows)


def roster_in(conn,user,timetable_id,day):
    schedule=session_record(conn,user,timetable_id,day)
    saved=conn.execute('SELECT * FROM attendance_sessions WHERE timetable_id=? AND day=?',(timetable_id,day.isoformat())).fetchone()
    if saved:
        rows=conn.execute('''SELECT m.student_id,s.name,m.present FROM attendance_marks m JOIN students s ON s.student_id=m.student_id
            WHERE m.session_id=? ORDER BY m.student_id''',(saved['id'],)).fetchall()
    else:
        rows=conn.execute('''SELECT DISTINCT s.student_id,s.name,NULL AS present FROM students s JOIN course_registrations cr ON cr.student_id=s.student_id
            WHERE cr.course_id=? AND cr.status='enrolled' ORDER BY s.student_id''',(schedule['course_id'],)).fetchall()
    return schedule,saved,[dict(r) for r in rows]


def roster(user,role,timetable_id,day_text):
    day=parse_day(day_text)
    with closing(get_connection()) as conn:
        conn.execute('BEGIN');faculty(conn,user,role)
        schedule,saved,rows=roster_in(conn,user,timetable_id,day)
        return dict(course_id=schedule['course_id'],revision=saved['revision'] if saved else 0,students=rows)


def save(user,role,timetable_id,day_text,payload):
    with closing(get_connection()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        return save_in_transaction(conn,user,role,timetable_id,day_text,payload)


def save_in_transaction(conn,user,role,timetable_id,day_text,payload):
    from tools.notifications import enqueue
    day=parse_day(day_text)
    if not isinstance(payload,dict) or set(payload)!={'revision','marks'} or type(payload['revision']) is not int or not isinstance(payload['marks'],dict):
        raise OperationError('Provide the roster marks and its revision.')
    marks=payload['marks']
    if any(type(v) is not int or v not in (0,1) for v in marks.values()):
        raise OperationError('Explicitly mark every student Present or Absent.')
    faculty(conn,user,role)
    schedule,saved,rows=roster_in(conn,user,timetable_id,day)
    if not rows or set(marks)!={r['student_id'] for r in rows}:
        raise AttendanceConflict('The enrolled roster changed or is incomplete. Reload attendance.')
    revision=saved['revision'] if saved else 0
    if revision!=payload['revision']:
        raise AttendanceConflict('Attendance changed. Reload before saving again.')
    before={r['student_id']:r['present'] for r in rows}
    if saved and before==marks:
        return dict(revision=revision,message='Attendance unchanged.')
    course=schedule['course_id']
    for student in marks:
        balance=conn.execute('SELECT * FROM attendance_balances WHERE student_id=? AND course_id=?',(student,course)).fetchone()
        current=conn.execute('SELECT * FROM attendance WHERE student_id=? AND course_id=?',(student,course)).fetchall()
        if not balance or len(current)!=1 or type(current[0]['attended_classes']) is not int or type(current[0]['total_classes']) is not int or not 0<=current[0]['attended_classes']<=current[0]['total_classes']:
            raise OperationError(f'Attendance starting balance needs review for {student}, {course}.')
        recorded=conn.execute('''SELECT COALESCE(SUM(m.present),0),COUNT(*) FROM attendance_marks m
            JOIN attendance_sessions s ON s.id=m.session_id WHERE m.student_id=? AND s.course_id=?''',(student,course)).fetchone()
        if (current[0]['attended_classes'],current[0]['total_classes'])!=(balance['attended']+recorded[0],balance['total']+recorded[1]):
            raise AttendanceConflict(f'Attendance totals changed outside daily marking for {student}. Review the starting balance before saving.')
    if not saved:
        sid=conn.execute('INSERT INTO attendance_sessions(timetable_id,day,course_id,start_time,end_time) VALUES(?,?,?,?,?)',
            (timetable_id,day.isoformat(),course,schedule['start_time'],schedule['end_time'])).lastrowid
    else:
        sid=saved['id']
    for student,present in marks.items():
        conn.execute('INSERT INTO attendance_marks VALUES(?,?,?) ON CONFLICT(session_id,student_id) DO UPDATE SET present=excluded.present',(sid,student,present))
        balance=conn.execute('SELECT attended,total FROM attendance_balances WHERE student_id=? AND course_id=?',(student,course)).fetchone()
        sums=conn.execute('''SELECT COALESCE(SUM(m.present),0),COUNT(*) FROM attendance_marks m JOIN attendance_sessions s ON s.id=m.session_id
            WHERE m.student_id=? AND s.course_id=?''',(student,course)).fetchone()
        attended,total=balance['attended']+sums[0],balance['total']+sums[1]
        conn.execute('UPDATE attendance SET attended_classes=?,total_classes=? WHERE student_id=? AND course_id=?',(attended,total,student,course))
        warning=conn.execute('SELECT * FROM attendance_warnings WHERE student_id=? AND course_id=?',(student,course)).fetchone()
        # Legacy fixed-threshold state is retained for compatibility only. Custom
        # alert rules below are the source of future attendance notifications.
        low=False
        generation=warning['generation'] if warning else 0
        if low and (not warning or not warning['below']):
            generation+=1
            enqueue(conn,f'attendance:{student}:{course}:{generation}',student,'Attendance below 80%',
                f'Your attendance in {course} — {schedule["course_name"]} is {attended}/{total} ({attended*100/total:.2f}%), below 80%.\nPlease attend upcoming classes. This reminder is not formal examination clearance.',student,course)
        if not low:
            conn.execute("UPDATE notification_outbox SET status='suppressed' WHERE student_id=? AND course_id=? AND status IN ('pending','failed')",(student,course))
        conn.execute('INSERT INTO attendance_warnings VALUES(?,?,?,?) ON CONFLICT(student_id,course_id) DO UPDATE SET below=excluded.below,generation=excluded.generation',(student,course,int(low),generation))
        from tools.alerts import evaluate_due
        evaluate_due(conn, college_now(), attendance_subject=(student,course))
    revision+=1
    conn.execute('UPDATE attendance_sessions SET revision=? WHERE id=?',(revision,sid))
    conn.execute('INSERT INTO attendance_audit(session_id,faculty_id,revision,before_marks,after_marks,created_at) VALUES(?,?,?,?,?,?)',
        (sid,user,revision,json.dumps(before,sort_keys=True),json.dumps(marks,sort_keys=True),college_now().isoformat()))
    return dict(revision=revision,message='Attendance saved. Applicable alert emails are queued.')
