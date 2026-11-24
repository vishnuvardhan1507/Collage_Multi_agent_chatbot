"""Manual-only administrator API. No credentials or private conversations are exposed."""
import sqlite3
from functools import wraps
from flask import Blueprint, g, jsonify, request
from auth.access import authenticated
from tools import admin_service as service
from tools.admin_documents import DOCUMENTS, read_document, publish_document, delete_document

admin_bp=Blueprint('admin',__name__,url_prefix='/api/admin')


def administrator(fn):
    @wraps(fn)
    @authenticated
    def wrapped(*args,**kwargs):
        if g.current_user['role']!='admin': return jsonify(error='Administrator access required.'),403
        try: return fn(*args,**kwargs)
        except service.Conflict as exc: return jsonify(error=str(exc)),409
        except PermissionError as exc: return jsonify(error=str(exc)),403
        except (ValueError,TypeError) as exc: return jsonify(error=str(exc)),400
        except sqlite3.IntegrityError: return jsonify(error='A related record is missing or this ID/email already exists.'),409
    return wrapped


@admin_bp.get('/overview')
@administrator
def overview(): return jsonify(service.overview(g.current_user['user_id']))


@admin_bp.get('/search')
@administrator
def search(): return jsonify(results=service.search_records(g.current_user['user_id'],request.args.get('q','')))


@admin_bp.get('/catalog')
@administrator
def catalog():
    return jsonify(resources={name:{'keys':keys,'fields':fields,'deletable':name in service.DELETABLE_RESOURCES} for name,(keys,fields) in service.RESOURCES.items()},
                   departments=service.DEPARTMENTS,documents=DOCUMENTS)


@admin_bp.get('/records/<resource>')
@administrator
def records(resource): return jsonify(records=service.list_resource(g.current_user['user_id'],resource))


@admin_bp.delete('/records/<resource>')
@administrator
def delete(resource):
    return jsonify(service.delete_resource(g.current_user['user_id'],resource,request.get_json(silent=True)))


@admin_bp.post('/records/<resource>')
@administrator
def save(resource):
    payload=request.get_json(silent=True)
    if resource=='accounts':
        result=service.save_account(g.current_user['user_id'],payload)
        if result['created']:
            result['setup_email']=service.setup_email(g.current_user['user_id'],result['user_id'])['message']
    else: result=service.save_resource(g.current_user['user_id'],resource,payload)
    return jsonify(result)


@admin_bp.post('/accounts/<uid>/setup-email')
@administrator
def setup_email(uid): return jsonify(service.setup_email(g.current_user['user_id'],uid))


@admin_bp.get('/courses/<cid>/impact')
@administrator
def impact(cid): return jsonify(service.assignment_impact(g.current_user['user_id'],cid))


@admin_bp.get('/documents/<name>')
@administrator
def document(name): return jsonify(read_document(g.current_user['user_id'],name))


@admin_bp.post('/documents/<name>')
@administrator
def publish(name): return jsonify(publish_document(g.current_user['user_id'],name,request.get_json(silent=True)))


@admin_bp.delete('/documents/<name>')
@administrator
def remove_document(name): return jsonify(delete_document(g.current_user['user_id'],name,request.get_json(silent=True)))


@admin_bp.get('/monitoring')
@administrator
def monitoring(): return jsonify(service.monitoring(g.current_user['user_id']))
