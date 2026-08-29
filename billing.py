"""Credit-based billing on local SQLite.

Every account starts with SIGNUP_CREDIT_CENTS. Billable operations
debit the account atomically; a charge on an account whose balance
is too low raises InsufficientFunds and nothing is written.

CLI:
    python billing.py seed              # create test accounts (alice/bob/carol)
    python billing.py list              # list accounts and balances
    python billing.py topup <user> <eur>
    python billing.py check             # assert-based self-check
"""
import sqlite3
import sys
import time
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = Path(__file__).parent / "billing.db"

SIGNUP_CREDIT_CENTS = 1000  # 10.00 EUR for every new client

# Prices in euro cents per unit.
RATES = {
    "tts_synthesize": 1,    # 0.01 EUR per synthesis request
    "pdf_upload": 5,        # 0.05 EUR per uploaded PDF (conversion included)
    "zone_ocr": 1,          # 0.01 EUR per zone OCR + translation
    "page_translate": 2,    # 0.02 EUR per page (retranslate / translate-all)
    "yt_download": 10,      # 0.10 EUR per YouTube download
}

RATE_LABELS = {
    "tts_synthesize": "TTS synthesis (per request)",
    "pdf_upload": "PDF upload & conversion",
    "zone_ocr": "Zone OCR + translation",
    "page_translate": "Page translation (per page)",
    "yt_download": "YouTube download",
}


class BillingError(Exception):
    pass


class InsufficientFunds(BillingError):
    pass


class DuplicateAccount(BillingError):
    pass


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                balance_cents INTEGER NOT NULL,
                created_at REAL NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                amount_cents INTEGER NOT NULL,
                description TEXT NOT NULL,
                created_at REAL NOT NULL
            )""")


def create_account(username: str, password: str):
    """Create an account with the signup credit. Returns the account row."""
    username = username.strip().lower()
    if not username or not password:
        raise BillingError("Username and password are required")
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO accounts (username, password_hash, balance_cents, created_at) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), SIGNUP_CREDIT_CENTS, time.time()),
            )
            account_id = cur.lastrowid
            c.execute(
                "INSERT INTO transactions (account_id, amount_cents, description, created_at) VALUES (?, ?, ?, ?)",
                (account_id, SIGNUP_CREDIT_CENTS, "Welcome credit", time.time()),
            )
    except sqlite3.IntegrityError:
        raise DuplicateAccount(f"Username '{username}' already exists")
    return get_account(account_id)


def authenticate(username: str, password: str):
    """Return the account row on valid credentials, else None."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM accounts WHERE username = ?", (username.strip().lower(),)
        ).fetchone()
    if row and check_password_hash(row["password_hash"], password):
        return row
    return None


def get_account(account_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()


def get_account_by_username(username: str):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM accounts WHERE username = ?", (username.strip().lower(),)
        ).fetchone()


def charge(account_id: int, cents: int, description: str):
    """Atomically debit `cents`. Raises InsufficientFunds if balance too low."""
    if cents <= 0:
        raise BillingError("Charge amount must be positive")
    with _conn() as c:
        cur = c.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id = ? AND balance_cents >= ?",
            (cents, account_id, cents),
        )
        if cur.rowcount == 0:
            if get_account(account_id) is None:
                raise BillingError("Account not found")
            raise InsufficientFunds("Balance too low")
        c.execute(
            "INSERT INTO transactions (account_id, amount_cents, description, created_at) VALUES (?, ?, ?, ?)",
            (account_id, -cents, description, time.time()),
        )


def credit(account_id: int, cents: int, description: str):
    if cents <= 0:
        raise BillingError("Credit amount must be positive")
    with _conn() as c:
        cur = c.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?",
            (cents, account_id),
        )
        if cur.rowcount == 0:
            raise BillingError("Account not found")
        c.execute(
            "INSERT INTO transactions (account_id, amount_cents, description, created_at) VALUES (?, ?, ?, ?)",
            (account_id, cents, description, time.time()),
        )


def has_transaction(account_id: int, description: str) -> bool:
    """True if this exact transaction was already recorded (idempotency guard)."""
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM transactions WHERE account_id = ? AND description = ?",
            (account_id, description),
        ).fetchone() is not None


def get_transactions(account_id: int, limit: int = 50):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM transactions WHERE account_id = ? ORDER BY id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()


def eur(cents: int) -> str:
    return f"{cents / 100:.2f} €"


# ── CLI ──────────────────────────────────────────────────────────────────────

TEST_ACCOUNTS = [("alice", "alice123"), ("bob", "bob123"), ("carol", "carol123")]


def _cli_seed():
    init_db()
    for user, pw in TEST_ACCOUNTS:
        try:
            acc = create_account(user, pw)
            print(f"created {user} (password: {pw}) balance {eur(acc['balance_cents'])}")
        except DuplicateAccount:
            print(f"exists  {user}")


def _cli_list():
    init_db()
    with _conn() as c:
        for row in c.execute("SELECT * FROM accounts ORDER BY id"):
            print(f"#{row['id']:<3} {row['username']:<20} {eur(row['balance_cents'])}")


def _cli_topup(username: str, euros: str):
    init_db()
    acc = get_account_by_username(username)
    if not acc:
        sys.exit(f"No account '{username}'")
    cents = round(float(euros) * 100)
    credit(acc["id"], cents, "Manual top-up (CLI)")
    print(f"{username}: {eur(get_account(acc['id'])['balance_cents'])}")


def _cli_check():
    """Self-check against a throwaway database."""
    global DB_PATH
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        DB_PATH = Path(tmp) / "check.db"
        init_db()
        acc = create_account("Test.User", "pw")
        assert acc["username"] == "test.user"
        assert acc["balance_cents"] == SIGNUP_CREDIT_CENTS
        assert authenticate("test.user", "pw")["id"] == acc["id"]
        assert authenticate("test.user", "wrong") is None
        try:
            create_account("test.user", "pw2")
            raise AssertionError("duplicate allowed")
        except DuplicateAccount:
            pass
        charge(acc["id"], 300, "test charge")
        assert get_account(acc["id"])["balance_cents"] == 700
        try:
            charge(acc["id"], 701, "too much")
            raise AssertionError("overdraft allowed")
        except InsufficientFunds:
            pass
        assert get_account(acc["id"])["balance_cents"] == 700
        credit(acc["id"], 100, "top-up")
        assert get_account(acc["id"])["balance_cents"] == 800
        txs = get_transactions(acc["id"])
        assert [t["amount_cents"] for t in txs] == [100, -300, SIGNUP_CREDIT_CENTS]
    print("billing self-check OK")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "seed":
        _cli_seed()
    elif cmd == "list":
        _cli_list()
    elif cmd == "topup" and len(sys.argv) == 4:
        _cli_topup(sys.argv[2], sys.argv[3])
    elif cmd == "check":
        _cli_check()
    else:
        sys.exit(__doc__)
