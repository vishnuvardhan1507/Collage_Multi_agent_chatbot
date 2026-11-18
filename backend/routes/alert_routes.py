from flask import Blueprint, g, jsonify, request

from auth.access import authenticated
from tools import alerts
from tools.operations import OperationError

alert_bp = Blueprint('alerts', __name__, url_prefix='/api/alerts')


def _user():
    return g.current_user['user_id'], g.current_user['role']


@alert_bp.get('')
@authenticated
def list_rules():
    user, role = _user()
    try: return jsonify(alerts.list_alerts(user, role))
    except PermissionError as exc: return jsonify(error=str(exc)), 403


@alert_bp.post('')
@authenticated
def create_rule():
    data=request.get_json(silent=True) or {}; user,role=_user()
    try:
        if set(data)!={'kind','config'}: raise OperationError('Provide an alert type and settings.')
        return jsonify(alert=alerts.create_alert(user,role,data['kind'],data['config'])),201
    except PermissionError as exc:return jsonify(error=str(exc)),403
    except (OperationError,ValueError) as exc:return jsonify(error=str(exc)),400


@alert_bp.patch('/<int:identifier>')
@authenticated
def update_rule(identifier):
    data=request.get_json(silent=True) or {};user,role=_user()
    try:
        if set(data)!={'kind','config'}: raise OperationError('Provide an alert type and settings.')
        return jsonify(alert=alerts.update_alert(user,role,identifier,data['kind'],data['config']))
    except PermissionError as exc:return jsonify(error=str(exc)),403
    except (OperationError,ValueError) as exc:return jsonify(error=str(exc)),400


@alert_bp.delete('/<int:identifier>')
@authenticated
def delete_rule(identifier):
    user,role=_user()
    try:return jsonify(alerts.delete_alert(user,role,identifier))
    except PermissionError as exc:return jsonify(error=str(exc)),404
    except OperationError as exc:return jsonify(error=str(exc)),400
