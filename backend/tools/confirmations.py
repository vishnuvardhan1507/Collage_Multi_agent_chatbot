"""Account-owned chat proposals. Lock order: user memory, then SQLite transaction."""
import json
import time
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from tools.db_tool import get_connection, verify_identity
from tools.memory_tool import update_memory
from tools.operations import compile_operation, execute_operation_in_transaction, OperationError


def summary(operation):
    if operation['type']=='book_hostel':
        room=operation['room_snapshot']
        return (f"Book one hostel bed in {room['room_id']}?\n"
                f"Sharing: {room['sharing_capacity']}; AC: {'Yes' if room['has_ac'] else 'No'}\n"
                f"Room fee: INR {room['monthly_room_fee']:,} per student per month.\n"
                'Immediate allocation; mess charges are separate. This proposal does not hold a bed.')
    if operation['type']=='cancel_hostel':
        return f"Cancel hostel booking #{operation['booking_id']} for {operation['room_id']}?\nYour allocated bed will be released."
    if operation['type']=='delete_alerts':
        return 'Delete these alerts?\n' + '\n'.join(f"#{item['id']}: {item['summary']}" for item in operation['alerts'])
    if operation['type'] == 'submit_leave':
        return f"Submit leave from {operation['from_date']} to {operation['to_date']}?\nReason: {operation['reason']}"
    text = (f"Book classroom {operation['classroom_id']} on {operation['booking_date']} "
            f"from {operation['start_time']} to {operation['end_time']}?\nPurpose: {operation['purpose']}")
    labels = {'min_capacity':'Minimum seats', 'has_ac':'AC required', 'min_projectors':'Minimum projectors', 'min_computers':'Minimum computers'}
    for key, value in operation.get('requirements', {}).items():
        text += f"\n{labels[key]}: {('Yes' if value else 'No') if isinstance(value,bool) else value}"
    return text


def public(row):
    status = row['status']
    if status == 'pending' and row['expires_at'] <= int(time.time()):
        status = 'expired'
    return {'id':row['confirmation_id'], 'status':status,
            'expires_at':datetime.fromtimestamp(row['expires_at'], timezone.utc).isoformat(),
            'summary':summary(json.loads(row['operation'])), 'result_message':row['response']}


def propose(operation, user_id, role, session_id, expected_leave_revision=None):
    if operation.get('type') not in {'submit_leave','book_classroom','delete_alerts','book_hostel','cancel_hostel'}:
        raise OperationError('Unsupported confirmation action.')
    if operation['type'] not in {'delete_alerts','book_hostel','cancel_hostel'}:
        compile_operation(operation,user_id,role)
    result = {}
    def create(data):
        nonlocal operation
        if session_id in data.get('deleted_sessions',[]):
            raise OperationError('This conversation has been deleted. Start a new session.')
        session = next((s for s in data['sessions'] if s['session_id']==session_id),None)
        if expected_leave_revision is not None and (session is None or session.get('leave_draft_revision')!=expected_leave_revision):
            raise OperationError('Your leave details changed before confirmation was prepared. Please review the latest request.')
        now=int(time.time()); identifier=uuid4().hex
        with closing(get_connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            verify_identity(conn,user_id,role)
            if operation['type'] in {'book_hostel','cancel_hostel'}:
                from tools.hostel import prepare
                operation=prepare(conn,operation,user_id,role)
            if operation['type']=='delete_alerts':
                from tools.alerts import deletion_selection, _summary
                rows=deletion_selection(conn,user_id,role,operation.get('ids'))
                operation['ids']=[row['id'] for row in rows]
                operation['alerts']=[{'id':row['id'],'kind':row['kind'],'config':json.loads(row['config_json']),
                    'created_at':row['created_at'],'updated_at':row['updated_at'],
                    'summary':_summary(row['kind'],json.loads(row['config_json']))} for row in rows]
            conn.execute("UPDATE chat_confirmations SET status='superseded' WHERE user_id=? AND session_id=? AND status='pending'",(user_id,session_id))
            conn.execute('INSERT INTO chat_confirmations(confirmation_id,user_id,session_id,operation,status,expires_at,created_at) VALUES(?,?,?,?,?,?,?)',
                         (identifier,user_id,session_id,json.dumps(operation),'pending',now+900,now))
            result.update(public(conn.execute('SELECT * FROM chat_confirmations WHERE confirmation_id=?',(identifier,)).fetchone()))
        if operation['type']=='submit_leave' and session is not None:
            session.pop('leave_draft',None)
            session['leave_draft_revision'] = session.get('leave_draft_revision',0)+1
    update_memory(user_id,create)
    return result


def decide(identifier, user_id, role, session_id, decision):
    result = {}
    def apply(data):
        if session_id in data.get('deleted_sessions',[]):
            raise OperationError('This conversation has been deleted.')
        session=next((s for s in data['sessions'] if s['session_id']==session_id),None)
        if session is None:
            raise OperationError('Conversation not found.')
        with closing(get_connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            verify_identity(conn,user_id,role)
            row=conn.execute('SELECT * FROM chat_confirmations WHERE confirmation_id=? AND user_id=? AND session_id=?',(identifier,user_id,session_id)).fetchone()
            if row is None:
                raise OperationError('Confirmation not found.')
            if row['status']=='pending':
                outcome=None
                if row['expires_at']<=int(time.time()):
                    status, response='expired','This request has expired. Please send a new request.'
                elif decision=='cancel':
                    status, response='cancelled',('Deletion cancelled. No alerts were deleted.' if json.loads(row['operation'])['type']=='delete_alerts' else 'Request cancelled. Nothing was submitted.')
                else:
                    conn.execute('SAVEPOINT confirmed_action')
                    try:
                        operation=json.loads(row['operation'])
                        if operation['type']=='delete_alerts':
                            from tools.alerts import deletion_selection, delete_alerts_in_transaction
                            selected=deletion_selection(conn,user_id,role,operation['ids'])
                            for current, expected in zip(selected,operation['alerts']):
                                if any(current[k]!=expected[k] for k in ('id','kind','created_at','updated_at')) or json.loads(current['config_json'])!=expected['config']:
                                    raise OperationError('An alert changed after this confirmation was shown. Request deletion again to review the latest details.')
                            deleted=delete_alerts_in_transaction(conn,user_id,role,operation['ids'])['deleted']
                            outcome={'action':'alert_deleted','deleted':deleted}
                        elif operation['type'] in {'book_hostel','cancel_hostel'}:
                            from tools.hostel import execute_confirmed
                            outcome=execute_confirmed(conn,operation,user_id,role)
                        else:
                            outcome=execute_operation_in_transaction(conn,operation,user_id,role)
                        status='confirmed'
                        response=(outcome['message'] if outcome['action'] in {'book_hostel','cancel_hostel'} else
                                  ('Alert ' if len(outcome['deleted'])==1 else 'Alerts ') + ', '.join('#'+str(i) for i in outcome['deleted']) + ' deleted.' if outcome['action']=='alert_deleted' else
                                  f"Leave request #{outcome['record_id']} submitted." if outcome['action']=='submit_leave'
                                  else f"Classroom {operation['classroom_id']} booked on {operation['booking_date']} from {operation['start_time']} to {operation['end_time']} (booking #{outcome['record_id']}).")
                    except (OperationError,PermissionError,ValueError) as exc:
                        conn.execute('ROLLBACK TO confirmed_action')
                        status,response='failed',str(exc)
                    conn.execute('RELEASE confirmed_action')
                conn.execute('UPDATE chat_confirmations SET status=?,result=?,response=? WHERE confirmation_id=?',(status,json.dumps(outcome),response,identifier))
                row=conn.execute('SELECT * FROM chat_confirmations WHERE confirmation_id=?',(identifier,)).fetchone()
            result.update(confirmation=public(row), response=row['response'] or 'This request was replaced by a newer proposal.',
                          actions=[json.loads(row['result'])['action']] if row['status']=='confirmed' else [],
                          session_id=session_id, timestamp=datetime.now(timezone.utc).isoformat())
        # Commit above precedes memory write. A retry uses the recorded outcome, never reexecutes.
        messages=session.setdefault('messages',[])
        if not any(m.get('confirmation_result_id')==identifier for m in messages):
            messages.append({'role':'assistant','content':result['response'],'timestamp':result['timestamp'],
                             'confirmation_result_id':identifier,'actions':result['actions']})
    update_memory(user_id,apply)
    return result


def hydrate(data,user_id):
    with closing(get_connection()) as conn:
        rows={row['confirmation_id']:row for row in conn.execute('SELECT * FROM chat_confirmations WHERE user_id=?',(user_id,))}
    for session in data.get('sessions',[]):
        for message in session.get('messages',[]):
            identifier=(message.get('confirmation') or {}).get('id')
            if identifier and identifier in rows and rows[identifier]['session_id']==session['session_id']:
                message['confirmation']=public(rows[identifier])
    return data
