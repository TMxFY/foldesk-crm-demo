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


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


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
            created_at TEXT NOT NULL,
            health TEXT CHECK (
                health IS NULL
                OR health IN ('active', 'offline', 'unauthorized', 'banned', 'frozen')
            ),
            last_error TEXT,
            updated_at TEXT
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
            description TEXT,
            email TEXT,
            url TEXT,
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

        CREATE TABLE IF NOT EXISTS dialogs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            peer_type TEXT NOT NULL DEFAULT 'user' CHECK (
                peer_type IN ('user', 'chat', 'channel')
            ),
            peer_id INTEGER NOT NULL,
            username TEXT,
            title TEXT,
            is_group INTEGER NOT NULL DEFAULT 0 CHECK (is_group IN (0, 1)),
            unread_count INTEGER NOT NULL DEFAULT 0 CHECK (unread_count >= 0),
            last_message_at TEXT,
            last_message_preview TEXT,
            last_from_peer INTEGER NOT NULL DEFAULT 0 CHECK (last_from_peer IN (0, 1)),
            muted INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
            lead_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (account_id, peer_type, peer_id),
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dialog_id INTEGER NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
            text TEXT,
            sent_at TEXT NOT NULL,
            reply_to_telegram_message_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (dialog_id, telegram_message_id),
            FOREIGN KEY (dialog_id) REFERENCES dialogs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            body TEXT NOT NULL CHECK (length(trim(body)) > 0),
            source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'forwarded')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS notes_lead_created ON notes(lead_id, created_at);

        -- Checkbox only. Dated follow-ups stay in reminders; tasks has no due_at.
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            done_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS tasks_lead_done ON tasks(lead_id, done_at);

        CREATE TABLE IF NOT EXISTS property_defs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            object_type TEXT NOT NULL CHECK (object_type IN ('lead', 'account')),
            key TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK (
                type IN (
                    'text', 'textarea', 'single_select', 'multi_select',
                    'url', 'email', 'tel', 'amount'
                )
            ),
            options_json TEXT NOT NULL DEFAULT '[]',
            required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (object_type, key)
        );

        CREATE INDEX IF NOT EXISTS property_defs_object ON property_defs(object_type, position);

        CREATE TABLE IF NOT EXISTS property_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id INTEGER NOT NULL REFERENCES property_defs(id) ON DELETE CASCADE,
            lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            value_text TEXT,
            value_int INTEGER,
            currency TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (lead_id IS NOT NULL AND account_id IS NULL)
                OR (lead_id IS NULL AND account_id IS NOT NULL)
            )
        );

        CREATE INDEX IF NOT EXISTS property_values_property ON property_values(property_id);
        CREATE INDEX IF NOT EXISTS property_values_lead ON property_values(lead_id);
        CREATE INDEX IF NOT EXISTS property_values_account ON property_values(account_id);

        CREATE UNIQUE INDEX IF NOT EXISTS property_values_one_lead
            ON property_values(property_id, lead_id)
            WHERE lead_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS property_values_one_account
            ON property_values(property_id, account_id)
            WHERE account_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS operator_session (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            logged_in INTEGER NOT NULL DEFAULT 0 CHECK (logged_in IN (0, 1)),
            chat_id TEXT,
            logged_in_at TEXT
        );
        """
    )
    _ensure_column(conn, "leads", "description", "TEXT")
    _ensure_column(conn, "leads", "email", "TEXT")
    _ensure_column(conn, "leads", "url", "TEXT")
    _ensure_column(conn, "accounts", "health", "TEXT")
    _ensure_column(conn, "accounts", "last_error", "TEXT")
    _ensure_column(conn, "accounts", "updated_at", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS accounts_health ON accounts(health)")
    conn.commit()


def as_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
