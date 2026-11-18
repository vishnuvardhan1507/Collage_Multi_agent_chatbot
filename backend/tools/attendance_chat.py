"""Structured faculty attendance drafts; no model-generated writes."""
import json
from contextlib import closing
from datetime import timedelta,datetime,timezone
from uuid import uuid4

from tools import attendance
from tools.db_tool import get_connection
from tools.memory_tool import read_memory,update_memory,_session
from tools.operations import OperationError


def migrate_receipts(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS attendance_chat_receipts(
        request_id TEXT NOT NULL,user_id TEXT NOT NULL REFERENCES users(user_id),session_id TEXT NOT NULL,
        result TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(user_id,request_id))''')
    conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES(13)')


def context(user,role,session_id):
    if role!='faculty':return None,None,0
    with closing(get_connection()) as conn:
        attendance.faculty(conn,user,role)
        courses=[dict(r) for r in conn.execute('SELECT course_id,course_name FROM courses WHERE faculty_id=? ORDER BY course_id',(user,))]
    data=read_memory(user)
    if session_id in data.get('deleted_sessions',[]):raise OperationError('This conversation was deleted.')
    session=next((s for s in data['sessions'] if s['session_id']==session_id),{})
    return courses,session.get('attendance_draft'),session.get('attendance_draft_revision',0)


def validate(update):
    if not isinstance(update,dict) or set(update)-{'action','fields'} or update.get('action') not in ('start','update','cancel'):
        raise ValueError('Attendance update requires action start, update or cancel, and fields only.')
    fields=update.get('fields',{})
    if not isinstance(fields,dict) or set(fields)-{'course','date','session_start','marks','all_status','rest_status'}:
        raise ValueError('Attendance fields: course, date, session_start, marks, all_status, rest_status. No SQL.')
    if update['action']=='cancel' and fields:raise ValueError('Cancel cannot contain attendance fields.')
    for key in ('course','date','session_start'):
        if key in fields and fields[key] is not None and (not isinstance(fields[key],str) or not fields[key].strip() or len(fields[key])>150):
            raise ValueError('Attendance selectors must be bounded text or null.')
    for key in ('all_status','rest_status'):
        if key in fields and fields[key] not in ('present','absent',None):raise ValueError('Attendance status must be present or absent.')
    marks=fields.get('marks',[])
    if not isinstance(marks,list) or len(marks)>500:raise ValueError('Marks must be a list of student/status objects.')
    for mark in marks:
        if not isinstance(mark,dict) or set(mark)!={'student','status'} or not isinstance(mark['student'],str) or not mark['student'].strip() or len(mark['student'])>150 or mark['status'] not in ('present','absent'):
            raise ValueError('Each mark needs a student ID/name and present or absent status.')
    return update


def receipt(user,request_id):
    with closing(get_connection()) as conn:
        row=conn.execute('SELECT result FROM attendance_chat_receipts WHERE user_id=? AND request_id=?',(user,request_id)).fetchone()
        return json.loads(row['result']) if row else None


def reconcile(user,session_id=None):
    """Recover committed replies after a history-write failure, without repeating writes."""
    with closing(get_connection()) as conn:
        rows=conn.execute('SELECT * FROM attendance_chat_receipts WHERE user_id=? ORDER BY created_at',(user,)).fetchall()
    if not rows:return
    def change(data):
        for row in rows:
            if session_id and row['session_id']!=session_id:continue
            if row['session_id'] in data.get('deleted_sessions',[]):continue
            session=_session(data,row['session_id'])
            if not any(m.get('request_id')==row['request_id'] and m.get('role')=='assistant' for m in session['messages']):
                result=json.loads(row['result'])
                session['messages'].append({'role':'assistant','content':result['response'],'actions':result['actions'],
                    'request_id':row['request_id'],'timestamp':row['created_at']})
            draft=session.get('attendance_draft')
            if draft and draft.get('id')==json.loads(row['result']).get('draft_id'):
                session.pop('attendance_draft',None)
                session['attendance_draft_revision']=session.get('attendance_draft_revision',0)+1
    update_memory(user,change)


def handle(user,role,session_id,update,expected_revision,request_id=None):
    if role!='faculty':raise PermissionError('Only teaching faculty can mark attendance.')
    validate(update);request_id=request_id or uuid4().hex
    outcome={}
    def change(data):
        session=_session(data,session_id)
        with closing(get_connection()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE');attendance.faculty(conn,user,role)
            existing=conn.execute('SELECT result,session_id FROM attendance_chat_receipts WHERE user_id=? AND request_id=?',(user,request_id)).fetchone()
            if existing:
                if existing['session_id']!=session_id:raise PermissionError('Request belongs to another conversation.')
                outcome.update(json.loads(existing['result']));return
            if session.get('attendance_draft_revision',0)!=expected_revision:
                raise attendance.AttendanceConflict('Your attendance draft changed. Please send your latest instruction again.')
            draft={'id':uuid4().hex,'fields':{},'explicit_marks':{}} if update['action']=='start' else session.get('attendance_draft') or {'id':uuid4().hex,'fields':{},'explicit_marks':{}}
            session['attendance_draft_revision']=expected_revision+1
            if update['action']=='cancel':
                session.pop('attendance_draft',None);outcome.update(response='Attendance draft cancelled. Nothing was saved.',actions=[]);return
            fields=dict(update.get('fields',{}))
            if fields.get('date') in ('today','yesterday'):
                fields['date']=(attendance.college_now().date()+timedelta(days=0 if fields['date']=='today' else -1)).isoformat()
            if fields.get('course'):
                canonical=conn.execute('SELECT course_id FROM courses WHERE faculty_id=? AND (lower(course_id)=lower(?) OR lower(course_name)=lower(?))',
                    (user,fields['course'].strip(),fields['course'].strip())).fetchall()
                if len(canonical)==1:fields['course']=canonical[0]['course_id']
            # Selecting a different class/date must never carry marks to that class.
            target_changed=any(k in fields and draft['fields'].get(k) not in (None,fields[k]) for k in ('course','date','session_start'))
            if target_changed:
                draft={'id':uuid4().hex,'fields':{k:v for k,v in draft['fields'].items() if k in ('course','date','session_start')},'explicit_marks':{}}
                if 'session_start' not in fields:draft['fields'].pop('session_start',None)
            for key,value in fields.items():
                if key!='marks':
                    if value is None:draft['fields'].pop(key,None)
                    else:draft['fields'][key]=value.strip() if isinstance(value,str) else value
            session['attendance_draft']=draft
            def ask(message):outcome.update(response=message,actions=[])
            needed=[k for k in ('course','date') if not draft['fields'].get(k)]
            if needed:
                # Retain unresolved user references until the session's roster is known.
                draft.setdefault('unresolved',[]).extend(fields.get('marks',[]))
                ask('Please specify the '+ ' and '.join(needed)+' for attendance.');return
            f=draft['fields'];day_text=f['date']
            if day_text in ('today','yesterday'):
                day_text=(attendance.college_now().date()+timedelta(days=0 if day_text=='today' else -1)).isoformat();f['date']=day_text
            day=attendance.parse_day(day_text)
            if day.weekday()>=5:raise OperationError('Holiday—attendance not required.')
            courses=conn.execute('SELECT course_id,course_name FROM courses WHERE faculty_id=? AND (lower(course_id)=lower(?) OR lower(course_name)=lower(?))',(user,f['course'],f['course'])).fetchall()
            if len(courses)!=1:
                draft.setdefault('unresolved',[]).extend(fields.get('marks',[]))
                ask('Please give the exact course ID for one of your assigned courses.');return
            course=courses[0]
            choices=conn.execute('SELECT * FROM course_timetable WHERE course_id=? AND weekday=? AND effective_from<=? AND effective_to>=? ORDER BY start_time',
                (course['course_id'],day.weekday(),day_text,day_text)).fetchall()
            if f.get('session_start'):choices=[r for r in choices if r['start_time']==f['session_start']]
            if len(choices)!=1:
                draft.setdefault('unresolved',[]).extend(fields.get('marks',[]))
                ask('Choose the session start time: '+', '.join(r['start_time']+'–'+r['end_time'] for r in choices) if choices else 'No matching scheduled session was found. Please check the date and start time.');return
            tid=choices[0]['timetable_id'];schedule,saved,rows=attendance.roster_in(conn,user,tid,day)
            signature={'timetable_id':tid,'date':day_text,'revision':saved['revision'] if saved else 0,'roster':[r['student_id'] for r in rows],
                'enrolled':[r[0] for r in conn.execute("SELECT DISTINCT student_id FROM course_registrations WHERE course_id=? AND status='enrolled' ORDER BY student_id",(course['course_id'],))]}
            if draft.get('snapshot') and draft['snapshot']!=signature:
                draft['snapshot']=signature;draft['explicit_marks']={};draft['fields']={k:v for k,v in f.items() if k in ('course','date','session_start')};draft.pop('unresolved',None)
                ask('Attendance or enrollment changed. Please review the current roster and restate your marks: '+', '.join(r['student_id'] for r in rows));return
            draft['snapshot']=signature
            if not rows:raise OperationError('No enrolled students are available for this session.')
            pending=draft.pop('unresolved',[])+fields.get('marks',[])
            resolved={}
            for mark in pending:
                name=' '.join(mark['student'].split()).casefold()
                matches=[r for r in rows if name==r['student_id'].casefold() or name==' '.join(r['name'].split()).casefold()]
                if len(matches)!=1:
                    draft['explicit_marks'].update(resolved)
                    draft['unresolved']=[]
                    ask('Please use a unique student ID from this roster: '+', '.join(r['student_id']+' '+r['name'] for r in rows));return
                resolved[matches[0]['student_id']]=1 if mark['status']=='present' else 0
            if 'all_status' in fields:
                draft['explicit_marks']={};draft['fields'].pop('rest_status',None)
            draft['explicit_marks'].update(resolved)
            marks={r['student_id']:r['present'] for r in rows}
            if f.get('all_status'):
                marks={r['student_id']:int(f['all_status']=='present') for r in rows}
            marks.update(draft['explicit_marks'])
            if f.get('rest_status'):
                marks.update({r['student_id']:int(f['rest_status']=='present') for r in rows if r['student_id'] not in draft['explicit_marks']})
            missing=[sid for sid,value in marks.items() if value is None]
            if missing:
                ask('Please mark Present or Absent for: '+', '.join(missing)+'. Nothing has been saved yet.');return
            if not draft['explicit_marks'] and not f.get('all_status') and not f.get('rest_status'):
                ask('Please specify the attendance marks you want to change.');return
            result=attendance.save_in_transaction(conn,user,role,tid,day_text,{'revision':signature['revision'],'marks':marks})
            count=sum(marks.values())
            response=f"{result['message'].split('.')[0]}. {course['course_id']} — {course['course_name']}, {day_text}, {schedule['start_time']}–{schedule['end_time']}: {count} present, {len(marks)-count} absent."
            outcome.update(response=response,actions=['mark_attendance'],draft_id=draft['id'])
            conn.execute('INSERT INTO attendance_chat_receipts VALUES(?,?,?,?,?)',(request_id,user,session_id,json.dumps(outcome),datetime.now(timezone.utc).isoformat()))
            session.pop('attendance_draft',None)
    update_memory(user,change)
    return outcome
