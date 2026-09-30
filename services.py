"""Business logic layer, on top of billing.py (DB) and tools.py (utilities)."""
import billing
from tools import send_code_email


def request_signup_code(email: str) -> str:
    """Issue a fresh code for `email` and email it. Returns the code, or
    None if rate-limited (caller should still show the "code sent" state
    to avoid revealing the limiter to an attacker)."""
    email = email.strip().lower()
    try:
        code = billing.create_email_code(email)
    except billing.RateLimited:
        return None
    send_code_email(email, code)
    return code


def verify_signup(email: str, code: str):
    """Valid code -> the (possibly new) account. Invalid/expired -> None."""
    if not billing.verify_email_code(email, code):
        return None
    return billing.get_or_create_account_by_email(email)
