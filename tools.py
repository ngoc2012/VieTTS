"""Low-level utility functions (no business logic)."""
import logging
import os
import smtplib
from email.message import EmailMessage


def _build_code_email(email: str, code: str, user: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Your VieNeu-TTS verification code"
    msg["From"] = user
    msg["To"] = email
    msg.set_content(f"Your verification code is {code}. It expires in 1 hour.")
    return msg


def send_code_email(email: str, code: str):
    # Same SMTP_SERVER/PORT/USER/PASS env vars as tunnel_restart.sh's send_email().
    # ponytail: no creds configured -> log the code instead of erroring.
    server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    passwd = os.environ.get("SMTP_PASS", "").replace(" ", "")

    if not user or not passwd:
        logging.info(f"[signup] verification code for {email}: {code} (SMTP_USER/SMTP_PASS not set)")
        return

    with smtplib.SMTP(server, port) as s:
        s.ehlo()
        s.starttls()
        s.login(user, passwd)
        s.send_message(_build_code_email(email, code, user))
