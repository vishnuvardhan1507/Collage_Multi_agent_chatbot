"""Trusted private booking reads and confirmed bed allocations; inventory uses the SQL agent."""
import math
from contextlib import closing

from tools.db_tool import get_connection, verify_identity
from tools.operations import OperationError


def preferences(value):
    if not isinstance(value, dict) or set(value) - {'sharing_capacity', 'has_ac', 'max_monthly_fee'}:
        raise OperationError('Provide sharing type, AC preference and/or a maximum monthly room fee.')
    if 'sharing_capacity' in value and (type(value['sharing_capacity']) is not int or value['sharing_capacity'] not in (2,3,4)):
        raise OperationError('Choose 2, 3 or 4 sharing.')
    if 'has_ac' in value and type(value['has_ac']) is not bool:
        raise OperationError('AC preference must be true or false.')
    if 'max_monthly_fee' in value:
        n=value['max_monthly_fee']
        if type(n) not in (int,float) or not math.isfinite(n) or not 0<=n<=1000000:
            raise OperationError('Provide a valid nonnegative monthly room budget.')
    return dict(value)


def validate_request(request):
    if not isinstance(request,dict) or request.get('mode') not in {'book','current','cancel'}:
        raise OperationError('Use hostel_request for selected-room booking, current booking or cancellation only. Inventory searches require route=sql and scoped_request for the SQL Query Agent, without hostel_request.')
    fields={'book':{'room_id','preferences'},'current':set(),'cancel':{'booking_id'}}[request['mode']]
    if set(request)-fields-{'mode'}:
        raise OperationError('Unsupported hostel fields. Identity is supplied by the server.')
    preferences(request.get('preferences',{}))
    if request.get('room_id') is not None and (not isinstance(request['room_id'],str) or not 1<=len(request['room_id'].strip())<=80):
        raise OperationError('Select a room using its listed room ID.')
    if request.get('booking_id') is not None and (type(request['booking_id']) is not int or request['booking_id']<1):
        raise OperationError('Provide a positive booking ID.')
    return request


def student_only(role):
    if role!='student':raise PermissionError('Only students can book hostel beds or view and cancel their own booking.')


def snapshot(room):
    return {k:room[k] for k in ('room_id','sharing_capacity','has_ac','monthly_room_fee')}


def room_check(conn,room_id,prefs):
    row=conn.execute('SELECT * FROM hostel_rooms WHERE room_id=? COLLATE NOCASE',(room_id,)).fetchone()
    if not row:raise OperationError('That hostel room no longer exists. Please request the available rooms again.')
    if row['occupied_beds']>=row['sharing_capacity']:
        raise OperationError('That hostel room has no vacant beds now. Please request the available rooms again.')
    for key,value in preferences(prefs).items():
        if (key=='max_monthly_fee' and row['monthly_room_fee']>value) or (key!='max_monthly_fee' and row[key]!=value):
            raise OperationError('The selected room does not match your preferences. Please select a matching room or explicitly change your preferences.')
    return row


def active_booking(conn,user_id):
    row=conn.execute("SELECT * FROM hostel_bookings WHERE student_id=? AND status='active'",(user_id,)).fetchone()
    return dict(row) if row else None


def prepare(conn,operation,user_id,role):
    student_only(role);verify_identity(conn,user_id,role)
    kind=operation.get('type')
    if kind=='book_hostel':
        if set(operation)-{'type','room_id','preferences'}:raise OperationError('Unsupported hostel booking fields.')
        validate_request({'mode':'book',**{k:v for k,v in operation.items() if k!='type'}})
        if not operation.get('room_id'):raise OperationError('Which available room would you like to select?')
        if active_booking(conn,user_id):raise OperationError('You already have an active hostel booking. Cancel it before booking another room.')
        prefs=preferences(operation.get('preferences',{}))
        room=room_check(conn,operation['room_id'].strip(),prefs)
        return {'type':kind,'room_id':room['room_id'],'preferences':prefs,'room_snapshot':snapshot(room)}
    if kind=='cancel_hostel':
        if set(operation)-{'type','booking_id'}:raise OperationError('Unsupported hostel cancellation fields.')
        validate_request({'mode':'cancel',**{k:v for k,v in operation.items() if k!='type'}})
        booking=active_booking(conn,user_id)
        if not booking or operation.get('booking_id',booking['booking_id'])!=booking['booking_id']:
            raise OperationError('No matching active hostel booking was found in your account.')
        return {'type':kind,'booking_id':booking['booking_id'],'room_id':booking['room_id'],
                'monthly_room_fee':booking['monthly_room_fee']}
    raise OperationError('Unsupported hostel action.')


def execute_confirmed(conn,operation,user_id,role):
    student_only(role);verify_identity(conn,user_id,role)
    kind=operation['type']
    if kind=='book_hostel':
        if active_booking(conn,user_id):raise OperationError('You already have an active hostel booking.')
        room=room_check(conn,operation['room_id'],operation['preferences'])
        if snapshot(room)!=operation['room_snapshot']:
            raise OperationError('The room details or price changed. Please select the room again and review a new confirmation.')
        cursor=conn.execute("INSERT INTO hostel_bookings(student_id,room_id,status,monthly_room_fee) VALUES(?,?,'active',?)",
                            (user_id,room['room_id'],room['monthly_room_fee']))
        record_id=cursor.lastrowid
        conn.execute('UPDATE hostel_rooms SET occupied_beds=occupied_beds+1 WHERE room_id=?',(room['room_id'],))
        message=f"Hostel booking #{record_id} confirmed: one bed in {room['room_id']}, INR {room['monthly_room_fee']:,} per month. Mess is separate."
    elif kind=='cancel_hostel':
        booking=active_booking(conn,user_id)
        if not booking or booking['booking_id']!=operation['booking_id'] or booking['room_id']!=operation['room_id']:
            raise OperationError('That booking is no longer active in your account. No bed was released.')
        record_id=booking['booking_id']
        if conn.execute('UPDATE hostel_rooms SET occupied_beds=occupied_beds-1 WHERE room_id=? AND occupied_beds>0',(booking['room_id'],)).rowcount!=1:
            raise OperationError('The room occupancy needs administrator review. Your booking has not been cancelled.')
        conn.execute("UPDATE hostel_bookings SET status='cancelled',cancelled_at=CURRENT_TIMESTAMP WHERE booking_id=?",(record_id,))
        message=f"Hostel booking #{record_id} cancelled. Your bed in {booking['room_id']} has been released."
    else:raise OperationError('Unsupported hostel action.')
    # Occupancy changes invalidate a previously opened admin editor.
    from tools.admin_service import bump, key_string
    bump(conn,'hostel_rooms',key_string([operation['room_id']]))
    return {'action':kind,'record_id':record_id,'rows_affected':1,'message':message}


def handle(request,user_id,role,session_id):
    validate_request(request)
    mode=request['mode']
    if mode in {'book','cancel'}:
        from tools.confirmations import propose
        operation={'type':'book_hostel' if mode=='book' else 'cancel_hostel',**{k:v for k,v in request.items() if k!='mode'}}
        proposal=propose(operation,user_id,role,session_id)
        return {'response':proposal['summary']+'\nPlease confirm or cancel below.','confirmation':proposal}
    with closing(get_connection()) as conn:
        verify_identity(conn,user_id,role)
        if mode=='current':
            student_only(role)
            booking=active_booking(conn,user_id)
            if not booking:return {'response':'You have no active hostel booking.'}
            return {'response':f"Your active hostel booking is #{booking['booking_id']}: one bed in {booking['room_id']}, INR {booking['monthly_room_fee']:,} per month. Mess is separate."}
