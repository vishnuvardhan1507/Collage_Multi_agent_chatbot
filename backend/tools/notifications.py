"""Durable event outbox; SMTP is never called inside business transactions."""
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone

from tools.db_tool import get_connection


def enqueue(conn,key,user,subject,body,student=None,course=None):
    conn.execute('''INSERT OR IGNORE INTO notification_outbox(event_key,user_id,subject,body,student_id,course_id)
        VALUES(?,?,?,?,?,?)''',(key,user,subject,body,student,course))


def operation_notification(conn,kind,record_id):
    if kind=='book_classroom':
        row=conn.execute('SELECT * FROM classroom_bookings WHERE booking_id=?',(record_id,)).fetchone()
        enqueue(conn,f'booking:{record_id}',row['faculty_id'],'Classroom booking confirmed',
            f'Booking #{record_id}\nRoom: {row["classroom_id"]}\nDate: {row["booking_date"]}\nTime: {row["start_time"]}â€“{row["end_time"]}\nPurpose: {row["purpose"]}')
    elif kind=='review_leave':
        from tools.alerts import leave_event
        row=conn.execute('SELECT status FROM leave_requests WHERE leave_id=?',(record_id,)).fetchone()
        if row: leave_event(conn,record_id,row['status'])


def deliver_one():
    from auth.mail import send_email
    now=time.time()
    heartbeat('notification-worker')
    with closing(get_connection()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        from tools.alerts import evaluate_due
        evaluate_due(conn)
        conn.execute("UPDATE notification_outbox SET status='failed',last_error='Delivery lease expired' WHERE status='sending' AND lease_until<? AND attempts>=5",(now,))
        row=conn.execute('''SELECT * FROM notification_outbox WHERE attempts<5 AND
            ((status='pending' AND next_attempt<=?) OR (status='sending' AND lease_until<?)) ORDER BY id LIMIT 1''',(now,now)).fetchone()
        if not row:return False
        row=dict(row)
        owner=conn.execute('SELECT is_active FROM users WHERE user_id=?',(row['user_id'],)).fetchone()
        if not owner or not owner['is_active']:
            conn.execute("UPDATE notification_outbox SET status='suppressed' WHERE id=?",(row['id'],))
            return True
        from tools.alerts import refresh_scheduled_email
        if not refresh_scheduled_email(conn, row):
            return True
        if row['student_id']:
            warning=conn.execute('SELECT below,generation FROM attendance_warnings WHERE student_id=? AND course_id=?',(row['student_id'],row['course_id'])).fetchone()
            if not warning or not warning['below'] or row['event_key'].rsplit(':',1)[-1]!=str(warning['generation']):
                conn.execute("UPDATE notification_outbox SET status='suppressed' WHERE id=?",(row['id'],));return True
        email=conn.execute('SELECT email FROM users WHERE user_id=?',(row['user_id'],)).fetchone()
        lease=now+300
        conn.execute("UPDATE notification_outbox SET status='sending',attempts=attempts+1,lease_until=? WHERE id=?",(lease,row['id']))
    error=None
    try:
        if not email or not email['email']:raise ValueError('Registered email missing')
        message_id='<'+str(uuid.uuid5(uuid.NAMESPACE_URL,'nexusiq:'+row['event_key']))+'@nexusiq.local>'
        if row.get('html_body'):
            send_email(email['email'],row['subject'],row['body'],message_id,html_body=row['html_body'])
        else:
            send_email(email['email'],row['subject'],row['body'],message_id)
    except Exception as exc:
        # Exception messages can contain SMTP addresses or credentials; retain type only.
        error=type(exc).__name__
    with closing(get_connection()) as conn,conn:
        if error:
            conn.execute('''UPDATE notification_outbox SET status=?,last_error=?,next_attempt=?,lease_until=NULL
                WHERE id=? AND status='sending' AND lease_until=?''',
                ('failed' if row['attempts']+1>=5 else 'pending',error,time.time()+min(3600,60*2**row['attempts']),row['id'],lease))
        else:
            conn.execute("UPDATE notification_outbox SET status='sent',sent_at=?,last_error=NULL,lease_until=NULL WHERE id=? AND status='sending' AND lease_until=?",(datetime.now(timezone.utc).isoformat(),row['id'],lease))
    return True


def register_worker(app):
    import click
    @app.cli.command('notification-worker')
    @click.option('--once',is_flag=True,help='Process currently due events, then exit.')
    def worker(once):
        """Deliver queued mail. Run one worker alongside the backend."""
        from threading import Event, Thread
        from tools.research_updates import collect_due, collect_notices_due
        stop = Event()
        def topic_loop():
            from tools.topic_research import run_one
            with app.app_context():
                while not stop.is_set():
                    try:
                        heartbeat('topic-research')
                        run_one()
                    except Exception as exc:
                        app.logger.warning('Topic research worker: %s', type(exc).__name__)
                    stop.wait(5)
        def research_loop():
            with app.app_context():
                while not stop.is_set():
                    heartbeat('research-collector')
                    for collect in (collect_due, collect_notices_due):
                        try:
                            collect()
                        except Exception as exc:
                            app.logger.warning('Subject update collector %s: %s', collect.__name__, type(exc).__name__)
                    stop.wait(60)
        if not once:
            Thread(target=research_loop, daemon=True, name='research-collector').start()
            Thread(target=topic_loop, daemon=True, name='topic-research').start()
        try:
            while True:
                worked=deliver_one()
                if not worked:
                    if once:break
                    time.sleep(5)
        finally:
            stop.set()


def heartbeat(name, status='running'):
    with closing(get_connection()) as conn, conn:
        conn.execute("INSERT INTO worker_heartbeats VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen,status=excluded.status",
                     (name,datetime.now(timezone.utc).isoformat(),status))
