import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app


def mail_configured():
    config = current_app.config
    host = config.get("SMTP_HOST", "")
    security = config.get("SMTP_SECURITY", "starttls")
    local = host in {"localhost", "127.0.0.1", "::1"}
    if not host or not (config.get("SMTP_FROM_EMAIL") or config.get("SMTP_USERNAME")):
        return False
    if security not in {"starttls", "ssl", "none"} or (security == "none" and not local):
        return False
    return local or bool(config.get("SMTP_USERNAME") and config.get("SMTP_PASSWORD"))


def send_reset_email(email, code):
    config = current_app.config
    if not mail_configured():
        raise RuntimeError("Password reset email is not configured.")
    minutes = max(1, config.get("PASSWORD_RESET_TTL_SECONDS", 600) // 60)
    message = EmailMessage()
    message["Subject"] = "Your NexusIQ password reset OTP"
    message["From"] = config.get("SMTP_FROM_EMAIL") or config["SMTP_USERNAME"]
    message["To"] = email
    message.set_content(f"Hello,\n\nYour NexusIQ password reset OTP is:\n\n{code}\n\nEnter this code on the password reset screen along with your new password.\nThe code expires in {minutes} minutes, allows five attempts, and can only be used once.\nNever share this code. If you did not request it, ignore this email.\n\nNexusIQ Institution\n")
    _send(message)


def send_email(email, subject, body, message_id, html_body=None):
    if not mail_configured():
        raise RuntimeError('Email delivery is not configured.')
    message = EmailMessage()
    message['Subject']=subject
    message['From']=current_app.config.get('SMTP_FROM_EMAIL') or current_app.config['SMTP_USERNAME']
    message['To']=email
    message['Message-ID']=message_id
    message.set_content(body+'\n\nNexusIQ Institution')
    if html_body:
        message.add_alternative(html_body, subtype='html')
    _send(message)


def _send(message):
    config=current_app.config
    security = config.get("SMTP_SECURITY", "starttls")
    options = {"host": config["SMTP_HOST"], "port": config.get("SMTP_PORT", 587), "timeout": config.get("SMTP_TIMEOUT", 15)}
    context = ssl.create_default_context()
    transport = smtplib.SMTP_SSL(context=context, **options) if security == "ssl" else smtplib.SMTP(**options)
    with transport as smtp:
        smtp.ehlo()
        if security == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        if config.get("SMTP_USERNAME"):
            password = config.get("SMTP_PASSWORD", "")
            if config["SMTP_HOST"] == "smtp.gmail.com":
                password = password.replace(" ", "")
            smtp.login(config["SMTP_USERNAME"], password)
        smtp.send_message(message)
