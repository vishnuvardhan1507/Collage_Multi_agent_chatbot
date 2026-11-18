"""Private per-conversation leave drafts; no leave is submitted here."""
from contextlib import closing

from tools.db_tool import get_connection, verify_identity
from tools.memory_tool import read_memory, update_memory, _session
from tools.operations import OperationError, parse_date


def load_draft(user_id, session_id):
    data = read_memory(user_id)
    if session_id in data.get('deleted_sessions', []):
        raise OperationError('This conversation has been deleted.')
    session = next((s for s in data['sessions'] if s['session_id']==session_id), {})
    return session.get('leave_draft'), session.get('leave_draft_revision', 0)


def validate_update(update):
    if not isinstance(update, dict) or set(update)-{'action','fields'}:
        raise ValueError('Leave draft update accepts only action and fields.')
    if update.get('action') not in ('start','update','cancel'):
        raise ValueError('Leave draft action must be start, update, or cancel.')
    fields = update.get('fields', {})
    if not isinstance(fields, dict) or set(fields)-{'from_date','to_date','reason'}:
        raise ValueError('Leave fields are from_date, to_date, and reason only.')
    if update['action']=='cancel' and fields:
        raise ValueError('Cancellation cannot supply leave fields.')
    for key,value in fields.items():
        if value is None:
            continue
        if not isinstance(value,str) or not value.strip() or len(value)>1000:
            raise ValueError('Leave fields must be nonempty text or null when explicitly cleared.')
        if key!='reason':
            parse_date(value)
    return update


def merge_draft(user_id, role, session_id, update, expected_revision):
    if role!='student':
        raise PermissionError('Only students can draft their own leave requests.')
    validate_update(update)
    with closing(get_connection()) as conn:
        verify_identity(conn,user_id,role)
    result = {}
    def change(data):
        session = _session(data,session_id)
        if session.get('leave_draft_revision',0)!=expected_revision:
            raise OperationError('Your leave details changed while this reply was being prepared. Please send your latest correction again.')
        draft = {} if update['action']=='start' else dict(session.get('leave_draft') or {})
        if update['action']=='cancel':
            draft = {}
        else:
            for key,value in update.get('fields',{}).items():
                if value is None:
                    draft.pop(key,None)
                else:
                    draft[key] = value.strip()
        revision = expected_revision+1
        session['leave_draft_revision'] = revision
        if draft or update['action']!='cancel':
            session['leave_draft'] = draft
        else:
            session.pop('leave_draft',None)
        result.update(draft=draft,revision=revision)
    update_memory(user_id,change)
    return result


def missing_question(draft):
    missing = [label for key,label in [('from_date','start date'),('to_date','end date'),('reason','reason')] if not draft.get(key)]
    return 'Please provide the ' + ' and '.join(missing) + ' for your leave request.' if missing else None
