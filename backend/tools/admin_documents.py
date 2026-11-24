"""Allowlisted document publishing with durable, recoverable publication intents."""
import hashlib
import os
import tempfile
from contextlib import closing
from pathlib import Path
from flask import current_app
from tools.db_tool import get_connection
from tools.admin_service import admin_identity, transaction, audit, text, Conflict

DOCUMENTS=('handbook.md','academic_regulations.md','examination_rules.md','leave_policy.md','onboarding.md')


def path_for(name):
    if name not in DOCUMENTS: raise ValueError('Unsupported knowledge-base document.')
    root=Path(current_app.config['KNOWLEDGE_BASE_DIR']).resolve()
    path=root/name
    if path.is_symlink() or path.resolve().parent!=root: raise ValueError('Document path is not permitted.')
    return path


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def snapshot(path):
    if not path.exists():return '', 'missing'
    content=path.read_text(encoding='utf-8')
    return content,digest(content)


def read_document(actor,name):
    with closing(get_connection()) as conn:
        admin_identity(conn,actor)
        content,revision=snapshot(path_for(name))
        history=[dict(r) for r in conn.execute('SELECT id,actor,status,content_hash,created_at FROM admin_document_revisions WHERE name=? ORDER BY id DESC LIMIT 20',(name,))]
        return {'name':name,'content':content,'revision':revision,'exists':revision!='missing','history':history}


def recover_publications():
    with closing(get_connection()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        for row in conn.execute("SELECT * FROM admin_document_revisions WHERE status IN ('prepared','delete_prepared')").fetchall():
            path=path_for(row['name'])
            _,current=snapshot(path)
            status=('deleted_index_pending' if current=='missing' else 'failed') if row['status']=='delete_prepared' else ('published_index_pending' if current==row['content_hash'] else 'failed')
            conn.execute('UPDATE admin_document_revisions SET status=? WHERE id=?',(status,row['id']))
            audit(conn,row['actor'],'document.recovered',row['name'],'Publication recovery',None,{'status':status,'hash':current})


def publish_document(actor,name,payload):
    if not isinstance(payload,dict) or set(payload)-{'content','revision','reason','confirmed'}:
        raise ValueError('Unsupported document fields.')
    if payload.get('confirmed') is not True: raise ValueError('Confirm publication.')
    content=text(payload.get('content'),'Document',150000)+'\n'
    reason=text(payload.get('reason'),'Publication reason',500)
    path=path_for(name); new_hash=digest(content)
    # Commit the intent first. An interrupted replacement remains recoverable.
    with transaction(actor) as conn:
        old,old_hash=snapshot(path)
        if payload.get('revision')!=old_hash: raise Conflict('Document changed. Reload before publishing.')
        if conn.execute("SELECT 1 FROM admin_document_revisions WHERE name=? AND status IN ('prepared','delete_prepared')",(name,)).fetchone():
            raise Conflict('Another publication is pending. Restart backend to recover it.')
        if not conn.execute('SELECT 1 FROM admin_document_revisions WHERE name=?',(name,)).fetchone():
            conn.execute('INSERT INTO admin_document_revisions(name,content,previous_hash,content_hash,actor,status) VALUES(?,?,?,?,?,?)',(name,old,old_hash,old_hash,actor,'previous'))
        rid=conn.execute('INSERT INTO admin_document_revisions(name,content,previous_hash,content_hash,actor,status) VALUES(?,?,?,?,?,?)',
                         (name,content,old_hash,new_hash,actor,'prepared')).lastrowid
        audit(conn,actor,'document.prepare',name,reason,{'hash':old_hash},{'hash':new_hash,'revision_id':rid})
    temporary=None
    try:
        with transaction(actor) as conn:
            if snapshot(path)[1]!=old_hash: raise Conflict('Document changed before publication.')
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,suffix='.tmp',delete=False,newline='\n') as handle:
                temporary=handle.name;handle.write(content);handle.flush();os.fsync(handle.fileno())
            os.replace(temporary,path)
            conn.execute("UPDATE admin_document_revisions SET status='published_index_pending' WHERE id=?",(rid,))
            audit(conn,actor,'document.publish',name,reason,{'hash':old_hash},{'hash':new_hash})
    except Exception:
        recover_publications()
        raise
    finally:
        if temporary and os.path.exists(temporary): os.unlink(temporary)
    status='published_index_pending'
    try:
        from tools.rag_tool import ensure_vector_store
        ensure_vector_store()
        status='published_indexed'
        with transaction(actor) as conn:
            conn.execute('UPDATE admin_document_revisions SET status=? WHERE id=?',(status,rid))
    except Exception:
        current_app.logger.warning('Admin document published; search indexing is pending.')
    return {'message':'Document published.', 'index_status':status,'revision':new_hash}


def delete_document(actor,name,payload):
    if not isinstance(payload,dict) or set(payload)!={'revision','reason','confirmed'} or payload['confirmed'] is not True:
        raise ValueError('Provide the document revision, reason and explicit confirmation.')
    path=path_for(name);reason=text(payload['reason'],'Deletion reason',500)
    with transaction(actor) as conn:
        content,old_hash=snapshot(path)
        if old_hash=='missing' or old_hash!=payload['revision']:raise Conflict('Document changed or was deleted. Reload the page.')
        if conn.execute("SELECT 1 FROM admin_document_revisions WHERE name=? AND status IN ('prepared','delete_prepared')",(name,)).fetchone():
            raise Conflict('Another document change is pending.')
        rid=conn.execute('INSERT INTO admin_document_revisions(name,content,previous_hash,content_hash,actor,status) VALUES(?,?,?,?,?,?)',
                         (name,content,old_hash,old_hash,actor,'delete_prepared')).lastrowid
        audit(conn,actor,'document.delete_prepare',name,reason,{'hash':old_hash},None)
    try:
        with transaction(actor) as conn:
            if snapshot(path)[1]!=old_hash:raise Conflict('Document changed before deletion.')
            path.unlink()
            conn.execute("UPDATE admin_document_revisions SET status='deleted_index_pending' WHERE id=?",(rid,))
            audit(conn,actor,'document.delete',name,reason,{'hash':old_hash},None)
    except Exception:
        recover_publications();raise
    status='deleted_index_pending'
    try:
        from tools.rag_tool import ensure_vector_store
        ensure_vector_store();status='deleted_indexed'
        with transaction(actor) as conn:conn.execute('UPDATE admin_document_revisions SET status=? WHERE id=?',(status,rid))
    except Exception:
        current_app.logger.warning('Admin document deleted; search indexing is pending.')
    return {'message':'Document deleted. Its revision history is preserved.','index_status':status}
