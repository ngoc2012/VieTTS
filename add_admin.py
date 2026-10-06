"""One-shot script: create/promote the admin account.

Run: uv run python add_admin.py
"""
import billing

USERNAME = ""
PASSWORD = ""

billing.init_db()
acc = billing.get_account_by_username(USERNAME)
if not acc:
    acc = billing.create_account(USERNAME, PASSWORD)
billing.set_admin(acc["id"], True)
print(f"{USERNAME} is now admin (balance {billing.eur(acc['balance_cents'])})")
