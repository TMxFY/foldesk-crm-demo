import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "crm.db"

STATUSES = ("new", "talking", "waiting", "won", "lost")
ACCOUNT_STATUSES = ("pending_code", "pending_password", "connected", "error")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def resolve_session(session_path: str) -> Path:
    path = Path(session_path)
    if not path.is_absolute():
        path = ROOT / path
    return path


def connect(db_path=None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            session_path TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending_code', 'pending_password', 'connected', 'error')
            ),
            phone_code_hash TEXT,
            telegram_user_id INTEGER,
            telegram_username TEXT,
            telegram_name TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            telegram_user_id INTEGER,
            username TEXT,
            name TEXT,
            phone TEXT,
            status TEXT NOT NULL DEFAULT 'new' CHECK (
                status IN ('new', 'talking', 'waiting', 'won', 'lost')
            ),
            note TEXT,
            next_step TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS leads_account_tg_unique
            ON leads(account_id, telegram_user_id)
            WHERE telegram_user_id IS NOT NULL;

        -- NULL account_id в составном индексе не совпадают между собой.
        CREATE UNIQUE INDEX IF NOT EXISTS leads_null_account_tg_unique
            ON leads(telegram_user_id)
            WHERE account_id IS NULL AND telegram_user_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            due_at TEXT NOT NULL,
            text TEXT NOT NULL,
            done_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );
        """
    )
    conn.commit()


def as_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
