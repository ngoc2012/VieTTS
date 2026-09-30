"""Low-level utility functions (no business logic)."""
import logging
import os
import smtplib
from email.message import EmailMessage


def _build_code_email(email: str, code: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Your VieNeu-TTS verification code"
    msg["From"] = os.environ.get("SMTP_FROM", "noreply@vieneu-tts.local")
    msg["To"] = email
    msg.set_content(f"Your verification code is {code}. It expires in 1 hour.")
    return msg


def send_code_email(email: str, code: str):
    # ponytail: no SMTP creds configured -> log the code instead of erroring.
    # set SMTP_HOST/PORT/USER/PASS/FROM env vars to actually send mail.
    host = os.environ.get("SMTP_HOST")
    if not host:
        logging.info(f"[signup] verification code for {email}: {code} (SMTP not configured)")
        return
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls()
        user = os.environ.get("SMTP_USER")
        if user:
            s.login(user, os.environ.get("SMTP_PASS", ""))
        s.send_message(_build_code_email(email, code))
