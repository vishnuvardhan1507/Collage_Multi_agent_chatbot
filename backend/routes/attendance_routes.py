from flask import Blueprint,g,jsonify,request
from auth.access import authenticated
from tools import attendance
from tools.operations import OperationError

attendance_bp=Blueprint('attendance',__name__,url_prefix='/api/attendance')


def call(function,*args):
    user=g.current_user
    try:return jsonify(function(user['user_id'],user['role'],*args))
    except attendance.AttendanceConflict as exc:return jsonify(error=str(exc)),409
    except PermissionError as exc:return jsonify(error=str(exc)),403
    except (OperationError,ValueError) as exc:return jsonify(error=str(exc)),400


@attendance_bp.get('/sessions')
@authenticated
def sessions():return call(attendance.sessions,request.args.get('date'))


@attendance_bp.get('/sessions/<int:identifier>/<day>')
@authenticated
def roster(identifier,day):return call(attendance.roster,identifier,day)


@attendance_bp.put('/sessions/<int:identifier>/<day>')
@authenticated
def save(identifier,day):return call(attendance.save,identifier,day,request.get_json(silent=True))
