import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "crm.db"
OUTREACH_MEDIA_DIR = ROOT / "data" / "outreach_media"

STATUSES = ("new", "talking", "waiting", "won", "lost")
AIRTABLE_STAGE_KEYS = (
    "new",
    "talking",
    "call_scheduled",
    "waiting",
    "on_hold",
    "won",
    "lost",
)
DEFAULT_PIPELINE_STAGES = (
    ("new", "Лид", 1, "#94a3b8"),
    ("talking", "Разговор", 2, "#38bdf8"),
    ("waiting", "Ждём", 3, "#fbbf24"),
    ("won", "Выигран", 4, "#34d399"),
    ("lost", "Проигран", 5, "#f87171"),
)
AIRTABLE_PIPELINE_STAGES = (
    ("new", "Лид", 1, "#94a3b8"),
    ("talking", "Разговор", 2, "#38bdf8"),
    ("call_scheduled", "Созвон назначен", 3, "#a855f7"),
    ("waiting", "Ждём", 4, "#fbbf24"),
    ("on_hold", "На паузе", 5, "#64748b"),
    ("won", "Выигран", 6, "#34d399"),
    ("lost", "Проигран", 7, "#f87171"),
)
AIRTABLE_STATUS_TO_KEY = {
    "new": "new",
    "лид": "new",
    "in conversation": "talking",
    "talking": "talking",
    "разговор": "talking",
    "call scheduled": "call_scheduled",
    "call_scheduled": "call_scheduled",
    "созвон назначен": "call_scheduled",
    "offer sent": "waiting",
    "waiting": "waiting",
    "ждём": "waiting",
    "оффер отправлен": "waiting",
    "on hold": "on_hold",
    "on_hold": "on_hold",
    "на паузе": "on_hold",
    "signed": "won",
    "won": "won",
    "выигран": "won",
    "cancelled": "lost",
    "canceled": "lost",
    "lost": "lost",
    "проигран": "lost",
    "contacted": "talking",
    "qualified": "waiting",
    "converted": "won",
}
KEY_TO_AIRTABLE_STATUS = {
    "new": "New",
    "talking": "In conversation",
    "call_scheduled": "Call scheduled",
    "waiting": "Offer sent",
    "on_hold": "On hold",
    "won": "Signed",
    "lost": "Cancelled",
}
ACCOUNT_STATUSES = ("pending_code", "pending_password", "connected", "error")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def resolve_session(session_path: str) -> Path:
    path = Path(session_path)
    if not path.is_absolute():
        path = ROOT / path
    return path


def resolve_outreach_media(media_ref: str) -> Path | str:
    raw = str(media_ref or "").strip()
    if raw.startswith("/media/outreach/"):
        filename = Path(raw.removeprefix("/media/outreach/")).name
        local_path = OUTREACH_MEDIA_DIR / filename
        if local_path.exists():
            return local_path
    if raw.startswith(("http://", "https://")):
        return raw
    path = Path(raw)
    if path.is_absolute() and path.exists():
        return path
    rel_path = ROOT / raw
    if not path.is_absolute() and rel_path.exists():
        return rel_path
    return raw


def connect(db_path=None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
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
            updated_at TEXT,
            daily_limit INTEGER NOT NULL DEFAULT 20,
            sent_today INTEGER NOT NULL DEFAULT 0,
            sent_date TEXT,
            proxy_url TEXT,
            access_scope TEXT NOT NULL DEFAULT 'workspace',
            provider TEXT NOT NULL DEFAULT 'telegram',
            wa_jid TEXT,
            pairing_code TEXT,
            qr_code TEXT,
            session_blob BLOB
        );

        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            telegram_user_id INTEGER,
            username TEXT,
            name TEXT,
            phone TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            note TEXT,
            next_step TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            description TEXT,
            email TEXT,
            url TEXT,
            dialog_id INTEGER,
            provider TEXT NOT NULL DEFAULT 'telegram',
            amount INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'USD',
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
            provider TEXT NOT NULL DEFAULT 'telegram',
            phone TEXT,
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
            external_message_id TEXT,
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

        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            telegram_folder_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            sync_enabled INTEGER NOT NULL DEFAULT 0 CHECK (sync_enabled IN (0, 1)),
            blocked_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (account_id, telegram_folder_id)
        );

        -- getUpdates без offset отдаёт тот же update_id снова. Одна строка — одна заметка.
        CREATE TABLE IF NOT EXISTS bot_seen_updates (
            update_id INTEGER PRIMARY KEY,
            lead_id INTEGER,
            note_id INTEGER
        );

        -- Один локальный оператор. Повтор того же имени обновляет строку, не плодит вторую.
        CREATE TABLE IF NOT EXISTS saved_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT,
            folder_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS saved_views_name ON saved_views(name);

        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            account_ids_json TEXT,
            csv_headers_json TEXT,
            daily_limit INTEGER NOT NULL DEFAULT 20 CHECK (daily_limit >= 1 AND daily_limit <= 500),
            status TEXT NOT NULL DEFAULT 'draft' CHECK (
                status IN ('draft', 'running', 'paused', 'completed')
            ),
            leadgen_enabled INTEGER NOT NULL DEFAULT 0,
            leadgen_daily_quota INTEGER NOT NULL DEFAULT 20,
            leadgen_sources_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS campaign_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
            step_order INTEGER NOT NULL CHECK (step_order >= 1),
            delay_hours INTEGER NOT NULL DEFAULT 0 CHECK (delay_hours >= 0),
            template_text TEXT NOT NULL,
            image_url TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (campaign_id, step_order)
        );

        CREATE TABLE IF NOT EXISTS campaign_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
            lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
            assigned_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            telegram_user_id INTEGER,
            username TEXT,
            name TEXT,
            phone TEXT,
            company TEXT,
            note TEXT,
            website TEXT,
            category TEXT,
            their_angle TEXT,
            our_angle TEXT,
            hook TEXT,
            step1_text TEXT,
            step2_text TEXT,
            step3_text TEXT,
            extra_json TEXT,
            current_step INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'in_progress', 'completed', 'replied', 'failed')
            ),
            last_sent_at TEXT,
            error_text TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS campaign_recipients_campaign ON campaign_recipients(campaign_id, status);

        CREATE TABLE IF NOT EXISTS outreach_schedule (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            timezone TEXT NOT NULL DEFAULT 'UTC',
            days_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kv_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS snippets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shortcut TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pipeline_stages (
            key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            position INTEGER NOT NULL,
            color TEXT
        );

        CREATE TABLE IF NOT EXISTS lead_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            meta_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS lead_events_lead_created ON lead_events(lead_id, created_at);

        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'call',
            start_at TEXT NOT NULL,
            end_at TEXT,
            all_day INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
            lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
            meet_link TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled' CHECK (
                status IN ('scheduled', 'completed', 'cancelled')
            ),
            source TEXT NOT NULL DEFAULT 'crm',
            external_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS calendar_events_start ON calendar_events(start_at);
        CREATE INDEX IF NOT EXISTS calendar_events_lead ON calendar_events(lead_id);
        CREATE UNIQUE INDEX IF NOT EXISTS calendar_events_ext_unique
            ON calendar_events(source, external_id)
            WHERE external_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS crm_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'operator',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS crm_auth_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES crm_users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        """
    )
    _migrate(conn)


def _migrate_legacy_leads_status_check(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'leads'"
    ).fetchone()
    if not row or not row[0] or "status IN (" not in str(row[0]):
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(leads)").fetchall()]
    col_list = ", ".join(cols)
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(
            """
            CREATE TABLE leads__new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                telegram_user_id INTEGER,
                username TEXT,
                name TEXT,
                phone TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                note TEXT,
                next_step TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                description TEXT,
                email TEXT,
                url TEXT,
                dialog_id INTEGER,
                provider TEXT NOT NULL DEFAULT 'telegram',
                folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'USD',
                company TEXT,
                niche TEXT,
                offer_type TEXT,
                offer_details TEXT,
                monthly_value INTEGER NOT NULL DEFAULT 0,
                lead_source TEXT,
                next_follow_up TEXT,
                last_contacted TEXT,
                airtable_record_id TEXT,
                airtable_status TEXT,
                airtable_synced_at TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )
            """
        )
        conn.execute(f"INSERT INTO leads__new ({col_list}) SELECT {col_list} FROM leads")
        conn.execute("DROP TABLE leads")
        conn.execute("ALTER TABLE leads__new RENAME TO leads")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS leads_account_tg_unique
                ON leads(account_id, telegram_user_id)
                WHERE telegram_user_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS leads_null_account_tg_unique
                ON leads(telegram_user_id)
                WHERE account_id IS NULL AND telegram_user_id IS NOT NULL
            """
        )
        conn.commit()
    finally:
        if conn.in_transaction:
            conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "leads", "description", "TEXT")
    _ensure_column(conn, "leads", "email", "TEXT")
    _ensure_column(conn, "leads", "url", "TEXT")
    _ensure_column(conn, "leads", "folder_id", "INTEGER REFERENCES folders(id) ON DELETE SET NULL")
    _ensure_column(conn, "leads", "dialog_id", "INTEGER")
    _ensure_column(conn, "leads", "provider", "TEXT NOT NULL DEFAULT 'telegram'")
    _ensure_column(conn, "leads", "amount", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "leads", "currency", "TEXT NOT NULL DEFAULT 'USD'")
    _ensure_column(conn, "leads", "company", "TEXT")
    _ensure_column(conn, "leads", "niche", "TEXT")
    _ensure_column(conn, "leads", "offer_type", "TEXT")
    _ensure_column(conn, "leads", "offer_details", "TEXT")
    _ensure_column(conn, "leads", "monthly_value", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "leads", "lead_source", "TEXT")
    _ensure_column(conn, "leads", "next_follow_up", "TEXT")
    _ensure_column(conn, "leads", "last_contacted", "TEXT")
    _ensure_column(conn, "leads", "airtable_record_id", "TEXT")
    _ensure_column(conn, "leads", "airtable_status", "TEXT")
    _ensure_column(conn, "leads", "airtable_synced_at", "TEXT")
    _migrate_legacy_leads_status_check(conn)
    _ensure_column(conn, "accounts", "health", "TEXT")
    _ensure_column(conn, "accounts", "last_error", "TEXT")
    _ensure_column(conn, "accounts", "updated_at", "TEXT")
    _ensure_column(conn, "accounts", "daily_limit", "INTEGER NOT NULL DEFAULT 20")
    _ensure_column(conn, "accounts", "sent_today", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "accounts", "sent_date", "TEXT")
    _ensure_column(conn, "accounts", "proxy_url", "TEXT")
    _ensure_column(conn, "accounts", "access_scope", "TEXT NOT NULL DEFAULT 'workspace'")
    _ensure_column(conn, "accounts", "provider", "TEXT NOT NULL DEFAULT 'telegram'")
    _ensure_column(conn, "accounts", "wa_jid", "TEXT")
    _ensure_column(conn, "accounts", "pairing_code", "TEXT")
    _ensure_column(conn, "accounts", "qr_code", "TEXT")
    _ensure_column(conn, "accounts", "session_blob", "BLOB")
    _ensure_column(conn, "dialogs", "provider", "TEXT NOT NULL DEFAULT 'telegram'")
    _ensure_column(conn, "dialogs", "phone", "TEXT")
    _ensure_column(conn, "messages", "external_message_id", "TEXT")
    _ensure_column(conn, "messages", "media_url", "TEXT")
    _ensure_column(conn, "campaigns", "account_ids_json", "TEXT")
    _ensure_column(conn, "campaigns", "csv_headers_json", "TEXT")
    _ensure_column(conn, "campaigns", "leadgen_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "campaigns", "leadgen_daily_quota", "INTEGER NOT NULL DEFAULT 20")
    _ensure_column(conn, "campaigns", "leadgen_sources_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "campaign_steps", "image_url", "TEXT")
    _ensure_column(
        conn,
        "campaign_recipients",
        "assigned_account_id",
        "INTEGER REFERENCES accounts(id) ON DELETE SET NULL",
    )
    _ensure_column(conn, "campaign_recipients", "website", "TEXT")
    _ensure_column(conn, "campaign_recipients", "category", "TEXT")
    _ensure_column(conn, "campaign_recipients", "their_angle", "TEXT")
    _ensure_column(conn, "campaign_recipients", "our_angle", "TEXT")
    _ensure_column(conn, "campaign_recipients", "hook", "TEXT")
    _ensure_column(conn, "campaign_recipients", "step1_text", "TEXT")
    _ensure_column(conn, "campaign_recipients", "step2_text", "TEXT")
    _ensure_column(conn, "campaign_recipients", "step3_text", "TEXT")
    _ensure_column(conn, "campaign_recipients", "extra_json", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_stages (
            key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            position INTEGER NOT NULL,
            color TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'call',
            start_at TEXT NOT NULL,
            end_at TEXT,
            all_day INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
            lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
            meet_link TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled' CHECK (
                status IN ('scheduled', 'completed', 'cancelled')
            ),
            source TEXT NOT NULL DEFAULT 'crm',
            external_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS calendar_events_start ON calendar_events(start_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS calendar_events_lead ON calendar_events(lead_id)")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS calendar_events_ext_unique
            ON calendar_events(source, external_id)
            WHERE external_id IS NOT NULL
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS leads_airtable_rec ON leads(airtable_record_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crm_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'operator',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crm_auth_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES crm_users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    from app.crm_auth import ensure_default_crm_users

    ensure_default_crm_users(conn)
    _ensure_default_pipeline_stages(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS accounts_health ON accounts(health)")
    conn.execute("CREATE INDEX IF NOT EXISTS leads_folder ON leads(folder_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_peer ON dialogs(peer_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_lead ON dialogs(lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_uname ON dialogs(username COLLATE NOCASE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_user_lead ON dialogs(lead_id) WHERE peer_type = 'user'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_user_peer ON dialogs(peer_id) WHERE peer_type = 'user'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_user_uname ON dialogs(lower(username)) WHERE peer_type = 'user' AND username IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_user_phone ON dialogs(phone) WHERE peer_type = 'user' AND phone IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_dlg_dir ON messages(dialog_id, direction, sent_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_rec_camp ON campaign_recipients(campaign_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_rec_lead ON campaign_recipients(lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_rec_tg ON campaign_recipients(telegram_user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_rec_uname ON campaign_recipients(username COLLATE NOCASE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_rec_lower_uname_nn ON campaign_recipients(lower(username)) WHERE username IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_rec_phone_nn ON campaign_recipients(phone) WHERE phone IS NOT NULL")
    snippets_count = conn.execute("SELECT COUNT(*) FROM snippets").fetchone()[0]
    if snippets_count == 0:
        user_mod = conn.execute(
            "SELECT 1 FROM kv_settings WHERE key = 'snippets_user_modified'"
        ).fetchone()
        if user_mod is None:
            from app.snippets import seed_default_snippets

            seed_default_snippets(conn)
    if (
        "PYTEST_CURRENT_TEST" not in os.environ
        and os.environ.get("BOT_TOKEN") != "e2e-not-a-secret"
    ):
        purged = conn.execute(
            "SELECT 1 FROM kv_settings WHERE key = 'purge_synthetic_amounts_v1'"
        ).fetchone()
        if purged is None:
            now_ts = utc_now()
            conn.execute(
                "UPDATE leads SET amount = 0, monthly_value = 0, updated_at = ? WHERE COALESCE(amount, 0) != 0 OR COALESCE(monthly_value, 0) != 0",
                (now_ts,),
            )
            conn.execute("DELETE FROM lead_events WHERE event_type = 'amount_changed'")
            conn.execute(
                "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('purge_synthetic_amounts_v1', '1', ?)",
                (now_ts,),
            )
        dead_sessions_fixed = conn.execute(
            "SELECT 1 FROM kv_settings WHERE key = 'purge_dead_tg_sessions_v1'"
        ).fetchone()
        if dead_sessions_fixed is None:
            now_ts = utc_now()
            conn.execute(
                """
                UPDATE accounts
                SET health = 'unauthorized',
                    status = 'error',
                    last_error = 'AUTH_KEY_DUPLICATED: переподключите сессию'
                WHERE id IN (11, 12)
                  AND telegram_username IN ('saburowe', 'HeatherLopez478')
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('purge_dead_tg_sessions_v1', '1', ?)",
                (now_ts,),
            )
    conn.commit()


def _ensure_default_pipeline_stages(conn: sqlite3.Connection) -> None:
    is_test_env = (
        "PYTEST_CURRENT_TEST" in os.environ
        or os.environ.get("BOT_TOKEN") == "e2e-not-a-secret"
    )
    stages = DEFAULT_PIPELINE_STAGES if is_test_env else AIRTABLE_PIPELINE_STAGES
    for key, label, position, color in stages:
        conn.execute(
            """
            INSERT OR IGNORE INTO pipeline_stages (key, label, position, color)
            VALUES (?, ?, ?, ?)
            """,
            (key, label, position, color),
        )
    if not is_test_env:
        ensure_airtable_pipeline_stages(conn)


def ensure_airtable_pipeline_stages(conn: sqlite3.Connection) -> list[dict]:
    existing_rows = conn.execute(
        "SELECT key, position FROM pipeline_stages"
    ).fetchall()
    existing_by_key = {str(r[0]): int(r[1]) for r in existing_rows}
    airtable_keys = {k for k, _, _, _ in AIRTABLE_PIPELINE_STAGES}
    all_present = airtable_keys.issubset(existing_by_key.keys())
    airtable_positions = [
        existing_by_key[k] for k in airtable_keys if k in existing_by_key
    ]
    positions_unique = len(airtable_positions) == len(set(airtable_positions))

    for key, label, position, color in AIRTABLE_PIPELINE_STAGES:
        conn.execute(
            """
            INSERT OR IGNORE INTO pipeline_stages (key, label, position, color)
            VALUES (?, ?, ?, ?)
            """,
            (key, label, position, color),
        )
        if not (all_present and positions_unique):
            conn.execute(
                "UPDATE pipeline_stages SET position = ? WHERE key = ?",
                (position, key),
            )
    conn.commit()
    return list_pipeline_stages(conn)


def as_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def valid_statuses(conn: sqlite3.Connection | None = None) -> set[str]:
    allowed = set(STATUSES) | set(AIRTABLE_STAGE_KEYS) | set(KEY_TO_AIRTABLE_STATUS.values())
    if conn is not None:
        try:
            rows = conn.execute("SELECT key FROM pipeline_stages").fetchall()
            allowed.update(str(r[0]) for r in rows if r[0])
        except Exception:
            pass
    return allowed


def normalize_status(raw: str | None, conn: sqlite3.Connection | None = None) -> str:
    if raw is None:
        return "new"
    s = str(raw).strip()
    if not s:
        return "new"
    lower = s.lower()
    if lower in AIRTABLE_STATUS_TO_KEY:
        return AIRTABLE_STATUS_TO_KEY[lower]
    if s in valid_statuses(conn):
        return s
    return s


def status_to_airtable(status: str | None) -> str:
    norm = normalize_status(status)
    return KEY_TO_AIRTABLE_STATUS.get(norm, "New")


def list_pipeline_stages(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT key, label, position, color FROM pipeline_stages ORDER BY position ASC, rowid ASC"
    ).fetchall()
    return [as_dict(row) for row in rows]


def update_pipeline_stage(
    conn: sqlite3.Connection,
    key: str,
    *,
    label: str | None = None,
    position: int | None = None,
    color: str | None = None,
) -> dict:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise LookupError("stage")
    existing = conn.execute(
        "SELECT key, label, position, color FROM pipeline_stages WHERE key = ?",
        (clean_key,),
    ).fetchone()
    if existing is None:
        raise LookupError("stage")
    updates = {}
    if label is not None:
        clean_label = str(label).strip()
        if not clean_label:
            raise ValueError("Укажите название стадии")
        updates["label"] = clean_label
    if position is not None:
        try:
            updates["position"] = int(position)
        except (TypeError, ValueError):
            raise ValueError("Некорректная позиция стадии") from None
    if color is not None:
        updates["color"] = str(color).strip() or None
    if updates:
        assignments = ", ".join(f"{col} = ?" for col in updates)
        conn.execute(
            f"UPDATE pipeline_stages SET {assignments} WHERE key = ?",
            [*updates.values(), clean_key],
        )
        conn.commit()
    row = conn.execute(
        "SELECT key, label, position, color FROM pipeline_stages WHERE key = ?",
        (clean_key,),
    ).fetchone()
    return as_dict(row)


def create_pipeline_stage(
    conn: sqlite3.Connection,
    *,
    key: str | None = None,
    label: str = "",
    position: int | None = None,
    color: str | None = None,
) -> dict:
    clean_label = str(label or "").strip()
    if not clean_label:
        raise ValueError("Укажите название стадии")
    explicit_key = key is not None and str(key).strip() != ""
    if explicit_key:
        clean_key = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(key).strip().lower()).strip("_")
        if not clean_key:
            raise ValueError("Некорректный ключ стадии")
        dup = conn.execute(
            "SELECT 1 FROM pipeline_stages WHERE key = ?", (clean_key,)
        ).fetchone()
        if dup is not None:
            raise ValueError("Стадия с таким ключом уже существует")
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", clean_label.lower()).strip("_")
        base_key = slug or "stage"
        clean_key = base_key
        idx = 1
        while (
            conn.execute(
                "SELECT 1 FROM pipeline_stages WHERE key = ?", (clean_key,)
            ).fetchone()
            is not None
        ):
            idx += 1
            clean_key = f"{base_key}_{idx}"
    if position is None:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM pipeline_stages"
        ).fetchone()[0]
        clean_pos = int(max_pos) + 1
    else:
        try:
            clean_pos = int(position)
        except (TypeError, ValueError):
            raise ValueError("Некорректная позиция стадии") from None
    clean_color = (str(color).strip() if color is not None else "") or "#6366f1"
    conn.execute(
        "INSERT INTO pipeline_stages (key, label, position, color) VALUES (?, ?, ?, ?)",
        (clean_key, clean_label, clean_pos, clean_color),
    )
    conn.commit()
    row = conn.execute(
        "SELECT key, label, position, color FROM pipeline_stages WHERE key = ?",
        (clean_key,),
    ).fetchone()
    return as_dict(row)


def bootstrap_prod_snapshot(conn: sqlite3.Connection) -> bool:
    import json

    snap_path = Path(__file__).resolve().parent / "prod_snapshot.json"
    if not snap_path.exists():
        return False
    lead_cnt = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    folder_cnt = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
    if int(lead_cnt or 0) > 0 or int(folder_cnt or 0) > 0:
        return False
    try:
        data = json.loads(snap_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False

    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        if data.get("pipeline_stages"):
            conn.execute("DELETE FROM pipeline_stages")
        for table_name in (
            "pipeline_stages",
            "accounts",
            "folders",
            "leads",
            "calendar_events",
            "dialogs",
            "lead_events",
            "property_defs",
            "outreach_schedule",
            "snippets",
            "kv_settings",
        ):
            rows = data.get(table_name)
            if not isinstance(rows, list) or not rows:
                continue
            valid_cols = {
                r["name"]
                for r in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            }
            for row in rows:
                if not isinstance(row, dict):
                    continue
                cols = [k for k in row.keys() if k in valid_cols]
                if not cols:
                    continue
                col_list = ", ".join(f'"{c}"' for c in cols)
                placeholders = ", ".join("?" for _ in cols)
                vals = [row[c] for c in cols]
                conn.execute(
                    f'INSERT OR REPLACE INTO "{table_name}" ({col_list}) VALUES ({placeholders})',
                    vals,
                )
        conn.commit()
    finally:
        if conn.in_transaction:
            conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    return True


