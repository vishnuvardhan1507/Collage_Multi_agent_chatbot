"""Explicit, administrator-only management. Never called by the agent or generated SQL."""
import hashlib
import json
import re
import secrets
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import current_app
from werkzeug.security import generate_password_hash
from auth.account_service import DEPARTMENTS, normalize_email, validate_user_id
from tools.db_tool import get_connection


class Conflict(ValueError):
    pass


# Keys and editable fields are trusted constants, never supplied as SQL by a client.
RESOURCES = {
    'courses': (['course_id'], ['course_name','department','credits','faculty_id']),
    'classrooms': (['classroom_id'], ['building','capacity','has_ac','projector_count','computer_count']),
    'hostel_rooms': (['room_id'], ['sharing_capacity','has_ac','occupied_beds','monthly_room_fee']),
    'mess_plans': (['plan_id'], ['name','monthly_mess_fee']),
    'mess_menus': (['plan_id','weekday'], ['breakfast','lunch','evening_snacks','dinner']),
    'transport_services': (['service_id'], ['campus','timezone','operating_days','campus_arrival','campus_departure','planning_radius_km','radius_note','arrive_before_minutes','currency','fare_basis']),
    'bus_routes': (['bus_id'], ['service_id','route_name','monthly_round_trip_fare']),
    'bus_stops': (['bus_id','stop_sequence'], ['locality','stop_name','approximate_radius_km','morning_pickup','afternoon_dropoff']),
    'bus_locality_aliases': (['bus_id','alias'], ['stop_sequence']),
    'course_timetable': (['timetable_id'], ['course_id','weekday','start_time','end_time','effective_from','effective_to']),
    'course_syllabi': (['course_id'], ['aliases_json','units_json']),
    'prerequisites': (['course_id'], ['prerequisite_ids']),
}

DELETABLE_RESOURCES=set(RESOURCES)


def reject_references(conn,resource,old):
    """Check even CASCADE foreign keys before deletion; never silently cascade data."""
    for table_row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        table=table_row[0]
        groups={}
        for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            if fk['table']==resource: groups.setdefault(fk['id'],[]).append(fk)
        for foreign_keys in groups.values():
            where=' AND '.join('"'+f['from']+'"=?' for f in foreign_keys)
            values=[old[f['to']] for f in foreign_keys]
            if conn.execute(f'SELECT 1 FROM "{table}" WHERE {where} LIMIT 1',values).fetchone():
                raise Conflict(f'Related records in {table.replace("_"," ")} prevent deletion. Remove or reassign those dependencies first.')


def delete_resource(actor,resource,payload):
    """Delete an unreferenced record, never cascade through academic history."""
    if resource not in DELETABLE_RESOURCES|{'accounts'} or not isinstance(payload,dict):
        raise ValueError('Deletion is unavailable for this resource.')
    keys=RESOURCES[resource][0] if resource!='accounts' else ['user_id']
    if set(payload)!=set(keys+['_revision','reason','confirmed']) or payload['confirmed'] is not True:
        raise ValueError('Provide the record ID, revision, reason and explicit deletion confirmation.')
    reason=text(payload['reason'],'Deletion reason',500)
    values=[payload[k] for k in keys]
    for k,value in zip(keys,values):
        if k in ('timetable_id','stop_sequence','weekday'):integer(value,k,0 if k=='weekday' else 1)
        else:text(value,k,160)
    if resource=='accounts':
        # Accounts are retired, never physically removed/reused with old JWTs or records.
        with transaction(actor) as conn:
            check_revision(conn,resource,key_string(values),payload['_revision'])
            old=next((r for r in account_rows(conn) if r['user_id']==values[0]),None)
            if not old:raise Conflict('Account is unavailable. Refresh the page.')
        result=save_account(actor,{**old,'is_active':0,'_revision':payload['_revision'],
                                   'reason':reason,'confirmed':True})
        return {**result,'message':f'Account {values[0]} deactivated. Its history and academic records are preserved.'}
    key=key_string(values);where=' AND '.join(k+'=?' for k in keys)
    with transaction(actor) as conn:
        check_revision(conn,resource,key,payload['_revision'])
        if resource=='prerequisites':
            old={'course_id':values[0],'prerequisite_ids':[r[0] for r in conn.execute('SELECT prerequisite_course_id FROM prerequisites WHERE course_id=?',values)]}
            if not old['prerequisite_ids']:raise Conflict('No prerequisite links remain for this course.')
            conn.execute('DELETE FROM prerequisites WHERE course_id=?',values)
            bump(conn,resource,key);audit(conn,actor,'record.delete',resource+':'+key,reason,old,None)
            return {'message':f'Prerequisite links for {values[0]} removed. The course is unchanged.'}
        row=conn.execute(f'SELECT * FROM {resource} WHERE {where}',values).fetchone()
        if not row: raise Conflict('Record no longer exists. Refresh the page.')
        old=dict(row)
        if resource=='classrooms':
            if conn.execute('SELECT 1 FROM classroom_bookings WHERE classroom_id=? LIMIT 1',values).fetchone():
                raise Conflict('This classroom has booking records and cannot be deleted.')
            # Pending confirmations must keep a stable target until completed or expired.
            import time
            for proposal in conn.execute("SELECT operation FROM chat_confirmations WHERE status='pending' AND expires_at>?",(int(time.time()),)):
                operation=json.loads(proposal[0])
                if operation.get('type')=='book_classroom' and operation.get('classroom_id')==values[0]:
                    raise Conflict('This classroom has a pending booking confirmation and cannot be deleted.')
            for rule in conn.execute("SELECT config_json FROM alert_rules WHERE kind='classroom_free' AND active=1"):
                config=json.loads(rule[0])
                if config.get('classroom_id')==values[0] or values[0] in (config.get('classroom_ids') or []):
                    raise Conflict('An active availability alert references this classroom.')
        elif resource=='course_timetable':
            if old['effective_from']<=today().isoformat():
                raise Conflict('Started or historical timetable periods cannot be deleted. Their attendance history must be preserved.')
            if conn.execute('SELECT 1 FROM attendance_sessions WHERE timetable_id=? LIMIT 1',values).fetchone():
                raise Conflict('Attendance records reference this schedule; it cannot be deleted.')
        elif resource=='hostel_rooms' and old['occupied_beds']>0:
            raise Conflict('An occupied hostel room cannot be deleted.')
        if resource in ('courses','mess_plans'):
            for rule in conn.execute('SELECT config_json FROM alert_rules WHERE active=1'):
                config=json.loads(rule[0])
                referenced=(config.get('course_id')==values[0] or values[0] in (config.get('course_ids') or [])) if resource=='courses' else values[0] in (config.get('plans') or [])
                if referenced:raise Conflict('An active alert references this record; edit or remove that alert first.')
        reject_references(conn,resource,old)
        conn.execute(f'DELETE FROM {resource} WHERE {where}',values)
        bump(conn,resource,key)
        audit(conn,actor,'record.delete',resource+':'+key,reason,old,None)
    return {'message':f'{resource.replace("_"," ").capitalize()} record {" / ".join(map(str,values))} deleted.'}


def today():
    return datetime.now(ZoneInfo('Asia/Kolkata')).date()


def admin_identity(conn, actor):
    row = conn.execute('SELECT role,is_active FROM users WHERE user_id=?', (actor,)).fetchone()
    if not row or row['role'] != 'admin' or not row['is_active']:
        raise PermissionError('Administrator access required.')


@contextmanager
def transaction(actor):
    with closing(get_connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        admin_identity(conn, actor)
        yield conn


def audit(conn, actor, action, target, reason, before, after):
    conn.execute('INSERT INTO admin_audit(actor,action,target,reason,before_json,after_json) VALUES(?,?,?,?,?,?)',
                 (actor,action,target,reason,json.dumps(before),json.dumps(after)))


def text(value, label, limit=200, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value,str) or not value.strip() or len(value)>limit or '\x00' in value:
        raise ValueError(f'{label}: enter text between 1 and {limit} characters.')
    return value.strip()


def integer(value, label, minimum=0, maximum=1000000, optional=False):
    if optional and value is None:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label}: enter a whole number from {minimum} to {maximum}.')
    return value


def clock(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value):
        raise ValueError('Use a valid 24-hour HH:MM time.')
    return value


def key_string(values):
    return json.dumps(values, separators=(',',':'))


def revision(conn, resource, key):
    row = conn.execute('SELECT revision FROM admin_revisions WHERE resource=? AND record_key=?',(resource,key)).fetchone()
    return row['revision'] if row else 0


def check_revision(conn, resource, key, expected):
    if type(expected) is not int or expected != revision(conn,resource,key):
        raise Conflict('This record changed. Reload it before saving.')


def bump(conn,resource,key):
    conn.execute('INSERT INTO admin_revisions VALUES(?,?,1) ON CONFLICT(resource,record_key) DO UPDATE SET revision=revision+1',(resource,key))


def account_rows(conn):
    return [dict(r) for r in conn.execute('''SELECT u.user_id,u.username,u.role,u.email,u.is_active,
        COALESCE(s.department,f.department) department,s.semester,s.advisor_faculty_id,
        f.designation,f.office_hours FROM users u LEFT JOIN students s ON s.student_id=u.user_id
        LEFT JOIN faculty f ON f.faculty_id=u.user_id WHERE u.role IN ('student','faculty') ORDER BY u.user_id''')]


def list_resource(actor, resource):
    with closing(get_connection()) as conn:
        admin_identity(conn,actor)
        if resource=='accounts':
            rows=account_rows(conn); keys=['user_id']
        elif resource=='prerequisites':
            keys=['course_id']; rows=[{'course_id':r[0], 'prerequisite_ids':[p[0] for p in conn.execute('SELECT prerequisite_course_id FROM prerequisites WHERE course_id=? ORDER BY prerequisite_course_id',(r[0],))]} for r in conn.execute('SELECT course_id FROM courses ORDER BY course_id')]
        elif resource in RESOURCES:
            keys, fields=RESOURCES[resource]
            rows=[dict(r) for r in conn.execute(f'SELECT {",".join(keys+fields)} FROM {resource} ORDER BY {",".join(keys)}')]
        else:
            raise ValueError('Unsupported management resource.')
        for row in rows:
            row['_revision']=revision(conn,resource,key_string([row[k] for k in keys]))
        return rows


def save_account(actor, payload):
    allowed={'user_id','username','role','department','email','semester','advisor_faculty_id','designation','office_hours','is_active','_revision','reason','confirmed'}
    if not isinstance(payload,dict) or set(payload)-allowed:
        raise ValueError('Unsupported account fields.')
    uid=text(payload.get('user_id'),'Account ID',40)
    reason=text(payload.get('reason'),'Change reason',500)
    with transaction(actor) as conn:
        old=next((r for r in account_rows(conn) if r['user_id']==uid),None)
        key=key_string([uid]); check_revision(conn,'accounts',key,payload.get('_revision'))
        data={**(old or {}),**payload}
        role=data.get('role'); department=data.get('department')
        if role not in ('student','faculty') or department not in DEPARTMENTS:
            raise ValueError('Choose a student or faculty account and supported department.')
        validate_user_id(uid,role)
        if old and (role!=old['role'] or department!=old['department']):
            raise ValueError('Existing roles and departments cannot be changed.')
        name=text(data.get('username'),'Name',100); email=normalize_email(data.get('email'))
        active=integer(data.get('is_active',1),'Active',0,1)
        if old and active!=old['is_active'] and payload.get('confirmed') is not True:
            raise ValueError('Confirm this account status change.')
        if role=='student':
            semester=integer(data.get('semester',1),'Semester',1,12)
            advisor=data.get('advisor_faculty_id') or None
            if advisor and not conn.execute('SELECT 1 FROM faculty f JOIN users u ON u.user_id=f.faculty_id WHERE f.faculty_id=? AND f.department=? AND u.is_active=1',(advisor,department)).fetchone():
                raise ValueError('Choose an active advisor in the student department.')
        if old:
            conn.execute('UPDATE users SET username=?,email=?,is_active=?,token_version=token_version+? WHERE user_id=?',
                         (name,email,active,int(active!=old['is_active']),uid))
        else:
            conn.execute('INSERT INTO users(user_id,username,email,role,password_hash,is_active) VALUES(?,?,?,?,?,?)',
                         (uid,name,email,role,generate_password_hash(secrets.token_urlsafe(64)),active))
        if role=='student':
            conn.execute('''INSERT INTO students(student_id,name,department,semester,advisor_faculty_id) VALUES(?,?,?,?,?)
                ON CONFLICT(student_id) DO UPDATE SET name=excluded.name,semester=excluded.semester,advisor_faculty_id=excluded.advisor_faculty_id''', (uid,name,department,semester,advisor))
        else:
            designation=text(data.get('designation') or None,'Designation',100,optional=True)
            hours=text(data.get('office_hours') or None,'Office hours',200,optional=True)
            conn.execute('''INSERT INTO faculty(faculty_id,name,department,email,designation,office_hours) VALUES(?,?,?,?,?,?)
                ON CONFLICT(faculty_id) DO UPDATE SET name=excluded.name,email=excluded.email,designation=excluded.designation,office_hours=excluded.office_hours''', (uid,name,department,email,designation,hours))
        if not active:
            conn.execute("UPDATE chat_confirmations SET status='cancelled',response='Account deactivated.' WHERE user_id=? AND status='pending'",(uid,))
            conn.execute("UPDATE notification_outbox SET status='suppressed' WHERE user_id=? AND status IN ('pending','failed')",(uid,))
            conn.execute("UPDATE research_jobs SET status='suppressed' WHERE user_id=? AND status IN ('queued','running')",(uid,))
            conn.execute('DELETE FROM password_reset_otps WHERE user_id=?',(uid,))
        bump(conn,'accounts',key)
        new=next(r for r in account_rows(conn) if r['user_id']==uid)
        audit(conn,actor,'account.update' if old else 'account.create',uid,reason,old,new)
    return {'message':'Account saved.', 'user_id':uid, 'created':old is None}


def setup_email(actor,uid):
    from auth.account_service import issue_reset, revoke_reset, consume_limit
    from auth.mail import send_reset_email
    with transaction(actor) as conn:
        row=conn.execute("SELECT email FROM users WHERE user_id=? AND role IN ('student','faculty') AND is_active=1",(uid,)).fetchone()
        if not row: raise ValueError('Active student or faculty account not found.')
        email=row['email']
    if not consume_limit('admin-setup-email',uid,1,window=60):
        raise ValueError('Wait 60 seconds before requesting another setup email.')
    code=issue_reset(uid)
    try:
        send_reset_email(email,code)
        message='Password setup OTP accepted by SMTP. Use Forgot password to enter the OTP.'
    except Exception:
        revoke_reset(uid,code)
        message='Account is saved, but the setup email failed. Retry setup email after checking SMTP.'
    with transaction(actor) as conn:
        audit(conn,actor,'account.setup_email',uid,'Requested password setup',None,{'result':message})
    return {'message':message}


def schedule_conflicts(conn, proposed, exclude=None, faculty_override=None):
    """Check actual shared weekdays in intersecting effective ranges."""
    course=conn.execute('SELECT faculty_id FROM courses WHERE course_id=?',(proposed['course_id'],)).fetchone()
    if not course: raise ValueError('Course does not exist.')
    faculty=faculty_override or course['faculty_id']
    rows=conn.execute('''SELECT t.*,c.faculty_id FROM course_timetable t JOIN courses c USING(course_id)
        WHERE t.weekday=? AND t.start_time<? AND t.end_time>? AND t.effective_from<=? AND t.effective_to>=?''',
        (proposed['weekday'],proposed['end_time'],proposed['start_time'],proposed['effective_to'],proposed['effective_from'])).fetchall()
    for other in rows:
        if other['timetable_id']==exclude: continue
        start=max(date.fromisoformat(proposed['effective_from']),date.fromisoformat(other['effective_from']))
        end=min(date.fromisoformat(proposed['effective_to']),date.fromisoformat(other['effective_to']))
        if start+timedelta(days=(proposed['weekday']-start.weekday())%7)>end: continue
        shared=conn.execute('''SELECT 1 FROM course_registrations a JOIN course_registrations b ON a.student_id=b.student_id
            WHERE a.course_id=? AND b.course_id=? AND a.status='enrolled' AND b.status='enrolled' LIMIT 1''',
            (proposed['course_id'],other['course_id'])).fetchone()
        if other['course_id']==proposed['course_id'] or (faculty and faculty==other['faculty_id']) or shared:
            raise ValueError(f"Schedule conflicts with {other['course_id']} at {other['start_time']}–{other['end_time']}.")


def validate_resource(conn,resource,data,old,payload):
    for field in ('capacity','sharing_capacity','occupied_beds','monthly_room_fee','monthly_mess_fee','monthly_round_trip_fare','credits','projector_count','computer_count','arrive_before_minutes','stop_sequence'):
        if field in data:
            data[field]=integer(data[field],field,1 if field in ('capacity','sharing_capacity','credits') else 0,optional=field in ('projector_count','computer_count') or resource=='bus_locality_aliases')
    if 'has_ac' in data: data['has_ac']=integer(data['has_ac'],'AC',0,1,optional=resource=='classrooms')
    for field in ('planning_radius_km','approximate_radius_km'):
        if field in data and (type(data[field]) not in (int,float) or not 0<=data[field]<=10000):
            raise ValueError('Distance must be a non-negative finite number.')
    for field in ('morning_pickup','afternoon_dropoff','campus_arrival','campus_departure','start_time','end_time'):
        if field in data: clock(data[field])
    for field in ('weekday',):
        if field in data: integer(data[field],field,0,6)
    if resource=='courses':
        if data['department'] not in DEPARTMENTS: raise ValueError('Unsupported department.')
        if old and data['department']!=old['department']: raise ValueError('Existing course department is immutable.')
        if data['faculty_id'] and not conn.execute('''SELECT 1 FROM faculty f JOIN users u ON u.user_id=f.faculty_id
            WHERE f.faculty_id=? AND f.department=? AND u.is_active=1''',(data['faculty_id'],data['department'])).fetchone():
            raise ValueError('Choose an active faculty member from this department.')
        if old and data['faculty_id']!=old['faculty_id']:
            if payload.get('confirmed') is not True: raise ValueError('Review the assignment impact and confirm reassignment.')
            for r in conn.execute('SELECT * FROM course_timetable WHERE course_id=? AND effective_to>=?',(data['course_id'],today().isoformat())):
                schedule_conflicts(conn,dict(r),r['timetable_id'],data['faculty_id'])
    if resource=='hostel_rooms':
        if data['sharing_capacity'] not in (2,3,4) or data['occupied_beds']>data['sharing_capacity']:
            raise ValueError('Use 2, 3 or 4 sharing; occupancy cannot exceed capacity.')
        allocated=conn.execute("SELECT COUNT(*) FROM hostel_bookings WHERE room_id=? AND status='active'",(data['room_id'],)).fetchone()[0]
        if data['occupied_beds']<allocated:
            raise ValueError('Occupancy cannot be lower than the active student hostel allocations.')
    if resource=='classrooms' and old:
        reduction=any(old[k] is not None and (data[k] is None or data[k]<old[k]) for k in ('capacity','has_ac','projector_count','computer_count'))
        if reduction and conn.execute('SELECT 1 FROM classroom_bookings WHERE classroom_id=? AND booking_date>=?',(data['classroom_id'],today().isoformat())).fetchone():
            raise ValueError('Future bookings exist. Capacity/equipment cannot be reduced.')
    if resource=='transport_services':
        if data['timezone']!='Asia/Kolkata' or data['currency']!='INR': raise ValueError('Transport uses Asia/Kolkata and INR.')
    if resource=='course_syllabi':
        try:
            aliases=json.loads(data['aliases_json']); units=json.loads(data['units_json'])
        except (ValueError,TypeError): raise ValueError('Enter valid JSON aliases and units.')
        if not isinstance(aliases,list) or len(aliases)>30 or any(not isinstance(a,str) or not 1<=len(a)<=160 for a in aliases):
            raise ValueError('Aliases must be a list of short names.')
        if not isinstance(units,list) or not 1<=len(units)<=30: raise ValueError('Provide 1–30 syllabus units.')
        for unit in units:
            if not isinstance(unit,dict) or set(unit)!={'unit','title','topics','learning_outcome'}: raise ValueError('Each unit needs unit, title, topics and learning_outcome.')
            integer(unit['unit'],'Unit number',1,100);text(unit['title'],'Unit title');text(unit['learning_outcome'],'Learning outcome',2000)
            if not isinstance(unit['topics'],list) or not unit['topics'] or len(unit['topics'])>60: raise ValueError('Provide topics as a nonempty list.')
            for topic in unit['topics']: text(topic,'Topic',300)
    if resource=='course_timetable':
        if data['weekday']>=5: raise ValueError('Saturday and Sunday are non-teaching days.')
        try: start=date.fromisoformat(data['effective_from']); end=date.fromisoformat(data['effective_to'])
        except (ValueError,TypeError): raise ValueError('Use valid ISO effective dates.')
        if start<=today() or end<start or data['start_time']>=data['end_time']:
            raise ValueError('Schedules must start tomorrow or later, with ordered dates and times.')
        if old and (data['course_id']!=old['course_id'] or start<=date.fromisoformat(old['effective_from'])):
            raise ValueError('A replacement must retain its course and start after the old effective start.')
        if old and payload.get('confirmed') is not True: raise ValueError('Confirm the future schedule replacement.')
        schedule_conflicts(conn,data,old['timetable_id'] if old else None)
    if resource=='prerequisites':
        ids=data['prerequisite_ids']
        if not isinstance(ids,list) or len(ids)>70 or any(not isinstance(x,str) for x in ids): raise ValueError('Select prerequisite course IDs.')
        if len(set(ids))!=len(ids): raise ValueError('Duplicate prerequisites are not allowed.')
        edges={r[0]:[] for r in conn.execute('SELECT course_id FROM courses')}
        if data['course_id'] not in edges or any(x not in edges for x in ids): raise ValueError('Unknown course.')
        for a,b in conn.execute('SELECT course_id,prerequisite_course_id FROM prerequisites'): edges[a].append(b)
        edges[data['course_id']]=ids
        visited=set(); visiting=set()
        def visit(node):
            if node in visiting: raise ValueError('Prerequisites must not contain a cycle or self-reference.')
            if node in visited: return
            visiting.add(node)
            for child in edges[node]: visit(child)
            visiting.remove(node);visited.add(node)
        for node in edges: visit(node)


def save_resource(actor,resource,payload):
    if resource not in RESOURCES or not isinstance(payload,dict): raise ValueError('Unsupported resource.')
    keys,fields=RESOURCES[resource]
    if set(payload)-set(keys+fields+['_revision','reason','confirmed']): raise ValueError('Unsupported fields.')
    reason=text(payload.get('reason'),'Change reason',500)
    data={k:payload.get(k) for k in keys+fields}
    for k in keys:
        if k=='timetable_id' and data[k] is None: continue
        if k in ('weekday','stop_sequence','timetable_id'): integer(data[k],k,0 if k=='weekday' else 1)
        else: text(data[k],k,160)
    key=key_string([data[k] for k in keys])
    where=' AND '.join(k+'=?' for k in keys)
    with transaction(actor) as conn:
        check_revision(conn,resource,key,payload.get('_revision'))
        if resource=='prerequisites':
            old={'course_id':data['course_id'],'prerequisite_ids':[r[0] for r in conn.execute('SELECT prerequisite_course_id FROM prerequisites WHERE course_id=?',(data['course_id'],))]}
        else:
            row=conn.execute(f'SELECT * FROM {resource} WHERE {where}',[data[k] for k in keys]).fetchone()
            old=dict(row) if row else None
        # Nullable fields are intentional; all other text is bounded here.
        nullable={'faculty_id','breakfast','lunch','evening_snacks','dinner'}
        numeric={'capacity','sharing_capacity','occupied_beds','monthly_room_fee','monthly_mess_fee','monthly_round_trip_fare','credits','projector_count','computer_count','has_ac','arrive_before_minutes','stop_sequence','planning_radius_km','approximate_radius_km','weekday'}
        for f in fields:
            if f not in numeric and f!='prerequisite_ids':
                data[f]=text(data[f],f,30000 if f.endswith('_json') else 2000,optional=f in nullable)
        validate_resource(conn,resource,data,old,payload)
        if resource=='prerequisites':
            conn.execute('DELETE FROM prerequisites WHERE course_id=?',(data['course_id'],))
            conn.executemany('INSERT INTO prerequisites VALUES(?,?)',[(data['course_id'],x) for x in data['prerequisite_ids']])
        elif resource=='course_timetable':
            if old:
                if conn.execute('SELECT 1 FROM attendance_sessions WHERE timetable_id=? AND day>=?',(old['timetable_id'],data['effective_from'])).fetchone():
                    raise ValueError('Saved attendance exists in the replacement period.')
                new_end=min(old['effective_to'],(date.fromisoformat(data['effective_from'])-timedelta(days=1)).isoformat())
                conn.execute('UPDATE course_timetable SET effective_to=? WHERE timetable_id=?',(new_end,old['timetable_id']))
            cursor=conn.execute(f'INSERT INTO course_timetable({",".join(fields)}) VALUES({",".join("?" for _ in fields)})',[data[k] for k in fields])
            data['timetable_id']=cursor.lastrowid
        elif old:
            conn.execute(f'UPDATE {resource} SET {",".join(k+"=?" for k in fields)} WHERE {where}',[data[k] for k in fields+keys])
        else:
            insert=dict(data)
            if resource in ('course_syllabi','transport_services'): insert['provenance']='Administrator-maintained record'
            conn.execute(f'INSERT INTO {resource}({",".join(insert)}) VALUES({",".join("?" for _ in insert)})',list(insert.values()))
        bump(conn,resource,key)
        audit(conn,actor,'record.save',resource+':'+key,reason,old,data)
    return {'message':'Saved successfully.', 'record':data}


def overview(actor):
    with closing(get_connection()) as conn:
        admin_identity(conn,actor)
        counts={t:conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('students','faculty','courses','classrooms','hostel_rooms','bus_routes')}
        counts['inactive_accounts']=conn.execute('SELECT COUNT(*) FROM users WHERE is_active=0').fetchone()[0]
        now=datetime.now(ZoneInfo('Asia/Kolkata'))
        day=now.date().isoformat(); clock_time=now.strftime('%H:%M')
        counts['as_of']=now.isoformat()
        counts['schedule']=[] if now.weekday()>=5 else [dict(r) for r in conn.execute('''
            SELECT t.timetable_id,t.course_id,c.course_name,t.start_time,t.end_time,f.name faculty_name
            FROM course_timetable t JOIN courses c USING(course_id)
            LEFT JOIN faculty f ON f.faculty_id=c.faculty_id
            WHERE t.weekday=? AND t.effective_from<=? AND t.effective_to>=?
            ORDER BY t.start_time,t.course_id,t.timetable_id''',(now.weekday(),day,day))]
        booked=conn.execute('''SELECT COUNT(DISTINCT b.classroom_id) FROM classroom_bookings b
            JOIN classrooms c USING(classroom_id)
            WHERE b.booking_date=? AND b.start_time<=? AND b.end_time>?''',(day,clock_time,clock_time)).fetchone()[0]
        counts['utilization']={'booked':booked,'unbooked':counts['classrooms']-booked,
                               'percentage':round(booked/counts['classrooms']*100) if counts['classrooms'] else None}
        counts['attention_count']=conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE status='failed'").fetchone()[0]
        return counts


def search_records(actor,query):
    query=text(query,'Search',100)
    # Literal substring matching: '%' and '_' are not query operators.
    pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    results=[]
    with closing(get_connection()) as conn:
        admin_identity(conn,actor)
        sources=[('accounts',"SELECT user_id id,username title,role detail FROM users WHERE role IN ('student','faculty') AND (user_id LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\') ORDER BY user_id LIMIT 8"),
                 ('courses',"SELECT course_id id,course_name title,department detail FROM courses WHERE course_id LIKE ? ESCAPE '\\' OR course_name LIKE ? ESCAPE '\\' ORDER BY course_id LIMIT 8"),
                 ('classrooms',"SELECT classroom_id id,classroom_id title,building detail FROM classrooms WHERE classroom_id LIKE ? ESCAPE '\\' OR building LIKE ? ESCAPE '\\' ORDER BY classroom_id LIMIT 8")]
        for resource,sql in sources:
            results.extend({'resource':resource,**dict(r)} for r in conn.execute(sql,(pattern,pattern)))
    return results


def monitoring(actor):
    with closing(get_connection()) as conn:
        admin_identity(conn,actor)
        return {'workers':[dict(r) for r in conn.execute('SELECT * FROM worker_heartbeats ORDER BY name')],
            'email_counts':[dict(r) for r in conn.execute('SELECT status,COUNT(*) count FROM notification_outbox GROUP BY status')],
            'research_jobs':[dict(r) for r in conn.execute('SELECT status,COUNT(*) count FROM research_jobs GROUP BY status')],
            'collection_status':[dict(r) for r in conn.execute('SELECT name,day,status,cursor FROM research_checkpoints ORDER BY name')],
            'recent_email_status':[dict(r) for r in conn.execute('SELECT id,status,attempts,created_at,sent_at FROM notification_outbox ORDER BY id DESC LIMIT 30')],
            'audit':[dict(r) for r in conn.execute('SELECT id,actor,action,target,reason,created_at FROM admin_audit ORDER BY id DESC LIMIT 100')]}


def assignment_impact(actor,course_id):
    with closing(get_connection()) as conn:
        admin_identity(conn,actor)
        return {'enrolled_students':conn.execute("SELECT COUNT(*) FROM course_registrations WHERE course_id=? AND status='enrolled'",(course_id,)).fetchone()[0],
            'weekly_sessions':[dict(r) for r in conn.execute('SELECT weekday,start_time,end_time,effective_from,effective_to FROM course_timetable WHERE course_id=? AND effective_to>=?',(course_id,today().isoformat()))],
            'notice':'Reassignment changes teaching access for this course. Historical attendance and audit records remain unchanged.'}
