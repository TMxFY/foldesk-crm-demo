from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.airtable_sync import (
    is_lead_airtable_excluded,
    pull_from_airtable,
    push_calendar_date_to_airtable,
    push_lead_to_airtable,
    sync_airtable,
)
from app.db import connect, init_db, utc_now
from app.leads import create_lead, get_lead, update_lead


class _TrackingAirtableClient:
    """Offline fake Airtable client recording every read/write API call."""

    def __init__(
        self,
        *,
        leads: list[dict[str, Any]] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        projects: list[dict[str, Any]] | None = None,
    ) -> None:
        self.table_name = "Leads"
        self.tables: dict[str, list[dict[str, Any]]] = {
            "Leads": [dict(r, fields=dict(r.get("fields") or {})) for r in (leads or [])],
            "Tasks": [dict(r, fields=dict(r.get("fields") or {})) for r in (tasks or [])],
            "Projects": [dict(r, fields=dict(r.get("fields") or {})) for r in (projects or [])],
        }
        self.calls: list[dict[str, Any]] = []
        self._seq = 1000

    def list_records(self, table: str | None = None, **_kw: Any) -> list[dict[str, Any]]:
        t = table or self.table_name
        self.calls.append({"op": "list", "table": t})
        return [dict(r, fields=dict(r.get("fields") or {})) for r in self.tables.get(t, [])]

    get_records = list_records

    def create_record(
        self,
        table: str,
        fields: dict[str, Any],
        *,
        typecast: bool = True,
    ) -> dict[str, Any]:
        self._seq += 1
        rec_id = f"recIso{self._seq}"
        rec = {"id": rec_id, "fields": dict(fields)}
        self.tables.setdefault(table, []).append(rec)
        self.calls.append(
            {"op": "create", "table": table, "id": rec_id, "fields": dict(fields)}
        )
        return dict(rec, fields=dict(rec["fields"]))

    def update_record(
        self,
        table: str,
        record_id: str,
        fields: dict[str, Any],
        *,
        typecast: bool = True,
    ) -> dict[str, Any]:
        self.calls.append(
            {"op": "update", "table": table, "id": record_id, "fields": dict(fields)}
        )
        for row in self.tables.setdefault(table, []):
            if row["id"] == record_id:
                row["fields"].update(fields)
                return dict(row, fields=dict(row["fields"]))
        rec = {"id": record_id, "fields": dict(fields)}
        self.tables[table].append(rec)
        return dict(rec, fields=dict(rec["fields"]))


def _ensure_isolation_columns(conn: sqlite3.Connection) -> None:
    """Ensure v2 isolation columns exist in test DB regardless of migration order."""
    lead_cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if "segment" not in lead_cols:
        conn.execute("ALTER TABLE leads ADD COLUMN segment TEXT DEFAULT 'b2b'")
    if "exclude_from_airtable" not in lead_cols:
        conn.execute("ALTER TABLE leads ADD COLUMN exclude_from_airtable INTEGER DEFAULT 0")

    camp_cols = {r[1] for r in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "segment" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN segment TEXT DEFAULT 'b2b'")
    if "exclude_from_airtable" not in camp_cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN exclude_from_airtable INTEGER DEFAULT 0")
    conn.commit()


@pytest.fixture
def conn(tmp_path: Path):
    db = connect(tmp_path / "isolation.db")
    init_db(db)
    _ensure_isolation_columns(db)
    yield db
    db.close()


def _insert_campaign(
    conn: sqlite3.Connection,
    *,
    name: str,
    segment: str = "b2b",
    exclude_from_airtable: int = 0,
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO campaigns (name, segment, exclude_from_airtable, status, created_at, updated_at)
        VALUES (?, ?, ?, 'draft', ?, ?)
        """,
        (name, segment, exclude_from_airtable, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _link_recipient(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
    lead_id: int | None = None,
    telegram_user_id: int | None = None,
    username: str | None = None,
    phone: str | None = None,
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, phone, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (campaign_id, lead_id, telegram_user_id, username, phone, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_is_lead_airtable_excluded_helper_variants(tmp_path: Path) -> None:
    assert is_lead_airtable_excluded(None) is False
    assert is_lead_airtable_excluded({"segment": "whale", "exclude_from_airtable": 0}) is False
    assert is_lead_airtable_excluded({"segment": "b2b", "exclude_from_airtable": "0"}) is False
    assert is_lead_airtable_excluded({"segment": "ofm", "exclude_from_airtable": False}) is False
    assert is_lead_airtable_excluded({"segment": "vacancy", "exclude_from_airtable": None}) is False

    assert is_lead_airtable_excluded({"exclude_from_airtable": 1}) is True
    assert is_lead_airtable_excluded({"exclude_from_airtable": True}) is True
    assert is_lead_airtable_excluded({"exclude_from_airtable": "1"}) is True
    assert is_lead_airtable_excluded({"exclude_from_airtable": "true"}) is True
    assert is_lead_airtable_excluded({"segment": "ru_expert"}) is True
    assert is_lead_airtable_excluded({"segment": "RU_EXPERT"}) is True
    assert is_lead_airtable_excluded({"segment": " ru_expert "}) is True

    # Campaign-level isolation overrides non-excluded lead
    normal_lead = {"segment": "b2b", "exclude_from_airtable": 0}
    assert is_lead_airtable_excluded(normal_lead, {"exclude_from_airtable": 1}) is True
    assert is_lead_airtable_excluded(normal_lead, {"segment": "ru_expert"}) is True
    assert (
        is_lead_airtable_excluded(
            normal_lead, {"segment": "whale", "exclude_from_airtable": 0}
        )
        is False
    )

    # Works safely on sqlite3.Row even when isolation columns do not exist
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, name TEXT)")
    mem.execute("INSERT INTO leads (name) VALUES ('Legacy Lead')")
    legacy_row = mem.execute("SELECT * FROM leads").fetchone()
    assert is_lead_airtable_excluded(legacy_row) is False
    assert is_lead_airtable_excluded(legacy_row, {"exclude_from_airtable": 1}) is True
    mem.close()


def test_sync_and_push_exclude_ru_expert_flag_and_campaign_leads(
    conn: sqlite3.Connection,
) -> None:
    whale_camp_id = _insert_campaign(
        conn, name="Whales EN — TG", segment="whale", exclude_from_airtable=0
    )
    excl_camp_id = _insert_campaign(
        conn, name="Private RU Outreach", segment="b2b", exclude_from_airtable=1
    )
    ru_camp_id = _insert_campaign(
        conn, name="РФ-Эксперты — TG", segment="ru_expert", exclude_from_airtable=0
    )

    whale_lead = create_lead(
        conn,
        name="WhaleBrand",
        company="WhaleBrand Inc",
        username="whale_ceo",
        status="talking",
        amount=4000,
    )
    conn.execute(
        "UPDATE leads SET segment = 'whale', exclude_from_airtable = 0 WHERE id = ?",
        (whale_lead["id"],),
    )
    _link_recipient(conn, campaign_id=whale_camp_id, lead_id=whale_lead["id"], username="whale_ceo")

    b2b_lead = create_lead(
        conn,
        name="NormalB2B",
        company="NormalB2B LLC",
        username="normal_b2b",
        status="new",
        amount=2500,
    )
    conn.execute(
        "UPDATE leads SET segment = 'b2b', exclude_from_airtable = 0 WHERE id = ?",
        (b2b_lead["id"],),
    )

    ru_lead = create_lead(
        conn,
        name="Эксперт Иван",
        username="ivan_ru_expert",
        status="talking",
        amount=3000,
    )
    conn.execute(
        "UPDATE leads SET segment = 'ru_expert', exclude_from_airtable = 0 WHERE id = ?",
        (ru_lead["id"],),
    )

    flagged_lead = create_lead(
        conn,
        name="Flagged Lead",
        username="flagged_private",
        status="talking",
        amount=2000,
    )
    conn.execute(
        "UPDATE leads SET segment = 'b2b', exclude_from_airtable = 1 WHERE id = ?",
        (flagged_lead["id"],),
    )

    camp_linked_lead = create_lead(
        conn,
        name="Campaign Excluded By ID",
        username="camp_excl_id",
        status="talking",
    )
    _link_recipient(
        conn,
        campaign_id=excl_camp_id,
        lead_id=camp_linked_lead["id"],
        username="camp_excl_id",
    )

    camp_uname_lead = create_lead(
        conn,
        name="Campaign Excluded By Username",
        username="camp_excl_uname",
        phone="+44 7700 900123",
        status="new",
    )
    _link_recipient(
        conn,
        campaign_id=excl_camp_id,
        lead_id=None,
        username="@Camp_Excl_Uname",
    )

    camp_ru_seg_lead = create_lead(
        conn,
        name="Campaign RU Segment",
        username="camp_ru_seg",
        status="new",
    )
    _link_recipient(
        conn,
        campaign_id=ru_camp_id,
        lead_id=camp_ru_seg_lead["id"],
        username="camp_ru_seg",
    )
    conn.commit()

    client = _TrackingAirtableClient()
    res = sync_airtable(conn, direction="both", client=client)

    assert res["ok"] is True
    assert res["pushed_to_airtable"] == 2

    pushed_names = {r["fields"]["Lead Name"] for r in client.tables["Leads"]}
    assert pushed_names == {"WhaleBrand", "NormalB2B"}

    for excluded_id in (
        ru_lead["id"],
        flagged_lead["id"],
        camp_linked_lead["id"],
        camp_uname_lead["id"],
        camp_ru_seg_lead["id"],
    ):
        stored = get_lead(conn, excluded_id)
        assert stored["airtable_record_id"] is None
        assert stored["airtable_synced_at"] is None
        ev_count = conn.execute(
            "SELECT COUNT(*) FROM lead_events WHERE lead_id = ? AND event_type = 'airtable_synced'",
            (excluded_id,),
        ).fetchone()[0]
        assert ev_count == 0

        # Direct push_lead_to_airtable must also refuse without calling Airtable or requiring credentials
        direct = push_lead_to_airtable(conn, excluded_id, client=None)
        assert direct["ok"] is True
        assert direct["excluded"] is True
        assert direct["skipped"] is True
        assert direct["action"] == "skipped_excluded"
        assert direct["airtable_record_id"] is None

    # Updating a normal whale lead still syncs cleanly without leaking excluded leads
    update_lead(conn, whale_lead["id"], status="won", amount=9500)
    res2 = sync_airtable(conn, direction="both", client=client)
    assert res2["pushed_to_airtable"] == 1
    assert len(client.tables["Leads"]) == 2
    whale_rec = next(r for r in client.tables["Leads"] if r["fields"]["Lead Name"] == "WhaleBrand")
    assert whale_rec["fields"]["Status"] == "Signed"
    assert whale_rec["fields"]["Value"] == 9500


def test_previously_synced_lead_blocks_updates_calendar_push_and_stale_delete(
    conn: sqlite3.Connection,
) -> None:
    lead = create_lead(
        conn,
        name="Previously Synced Brand",
        username="prev_synced",
        status="talking",
        amount=5000,
        next_follow_up="2026-11-20",
    )
    client = _TrackingAirtableClient()
    first = push_lead_to_airtable(conn, lead["id"], client=client)
    assert first["action"] == "created"
    rec_id = first["airtable_record_id"]
    assert rec_id

    calls_before = len(client.calls)

    # Mark lead excluded and dirty it
    conn.execute(
        """
        UPDATE leads
        SET exclude_from_airtable = 1,
            note = 'Private note must stay out of Airtable',
            updated_at = '2099-01-01T00:00:00'
        WHERE id = ?
        """,
        (lead["id"],),
    )
    conn.commit()

    sync_res = sync_airtable(conn, direction="push", client=client)
    assert sync_res["pushed_to_airtable"] == 0
    assert len(client.calls) == calls_before

    direct_res = push_lead_to_airtable(conn, lead["id"], client=client)
    assert direct_res["excluded"] is True
    assert len(client.calls) == calls_before

    cal_res = push_calendar_date_to_airtable(
        conn,
        item_kind="lead_followup",
        item_id=lead["id"],
        new_date="2026-12-05",
        client=client,
    )
    assert cal_res["ok"] is True
    assert cal_res["pushed"] is False
    assert get_lead(conn, lead["id"])["next_follow_up"] == "2026-12-05"
    assert len(client.calls) == calls_before

    # Even if remote Airtable no longer has rec_id, pull_from_airtable must not delete an isolated lead
    client.tables["Leads"] = [
        {"id": "recOtherLead", "fields": {"Lead Name": "Other Brand", "Contact": "@other_brand"}}
    ]
    pull_from_airtable(conn, client=client, preserve_dirty=False)
    assert get_lead(conn, lead["id"]) is not None


def test_pull_from_airtable_skips_excluded_lead_without_clobber_or_duplicate(
    conn: sqlite3.Connection,
) -> None:
    ru_lead = create_lead(
        conn,
        name="Локальный Эксперт",
        username="coach_ivan",
        status="new",
        note="Только локальная заметка",
    )
    conn.execute(
        "UPDATE leads SET segment = 'ru_expert', exclude_from_airtable = 1 WHERE id = ?",
        (ru_lead["id"],),
    )
    conn.commit()

    client = _TrackingAirtableClient(
        leads=[
            {
                "id": "recCoachRemote",
                "fields": {
                    "Lead Name": "Remote Overwrite",
                    "Status": "Signed",
                    "Contact": "@coach_ivan",
                    "Value": 9999,
                    "Notes": "Should not overwrite excluded local lead",
                },
            }
        ],
        tasks=[
            {
                "id": "recTaskCoach",
                "fields": {
                    "Task Name": "Task for remote",
                    "Due Date": "2026-12-01",
                    "Related Lead": ["recCoachRemote"],
                },
            }
        ],
    )

    pull_res = pull_from_airtable(conn, client=client)
    assert pull_res["created_in_crm"] == 0
    assert pull_res["updated_in_crm"] == 0

    stored = get_lead(conn, ru_lead["id"])
    assert stored["name"] == "Локальный Эксперт"
    assert stored["status"] == "new"
    assert stored["note"] == "Только локальная заметка"
    assert stored["airtable_record_id"] is None

    total_coach_rows = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE lower(username) = 'coach_ivan'"
    ).fetchone()[0]
    assert total_coach_rows == 1

    ev = conn.execute(
        "SELECT lead_id FROM calendar_events WHERE external_id = 'recTaskCoach'"
    ).fetchone()
    assert ev is not None
    assert ev["lead_id"] is None


def test_sync_push_only_skips_all_api_calls_when_only_excluded_leads_exist(
    conn: sqlite3.Connection,
) -> None:
    ru_lead = create_lead(conn, name="RU Expert Only", username="only_ru")
    conn.execute("UPDATE leads SET segment = 'ru_expert' WHERE id = ?", (ru_lead["id"],))
    conn.commit()

    client = _TrackingAirtableClient()
    res = sync_airtable(conn, direction="push", client=client)
    assert res["pushed_to_airtable"] == 0
    assert client.calls == []


def test_legacy_schemas_without_new_columns_work_gracefully(tmp_path: Path) -> None:
    legacy_path = tmp_path / "explicit_legacy.db"
    lconn = sqlite3.connect(legacy_path)
    lconn.row_factory = sqlite3.Row
    lconn.executescript(
        """
        CREATE TABLE kv_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE pipeline_stages (
            key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            position INTEGER NOT NULL,
            color TEXT
        );
        CREATE TABLE lead_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            meta_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'call',
            start_at TEXT NOT NULL,
            end_at TEXT,
            all_day INTEGER NOT NULL DEFAULT 0,
            lead_id INTEGER,
            meet_link TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled',
            source TEXT NOT NULL DEFAULT 'crm',
            external_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            telegram_name TEXT,
            telegram_username TEXT
        );
        CREATE TABLE dialogs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            peer_type TEXT NOT NULL DEFAULT 'user',
            peer_id INTEGER,
            username TEXT,
            phone TEXT,
            lead_id INTEGER,
            last_message_at TEXT,
            last_message_preview TEXT,
            last_from_peer INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE campaign_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            step_order INTEGER NOT NULL,
            delay_hours INTEGER NOT NULL DEFAULT 0,
            template_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE campaign_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            lead_id INTEGER,
            assigned_account_id INTEGER,
            telegram_user_id INTEGER,
            username TEXT,
            phone TEXT,
            current_step INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            last_sent_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            telegram_user_id INTEGER,
            username TEXT,
            name TEXT,
            phone TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            note TEXT,
            next_step TEXT,
            folder_id INTEGER,
            url TEXT,
            company TEXT,
            niche TEXT,
            offer_type TEXT,
            offer_details TEXT,
            amount INTEGER NOT NULL DEFAULT 0,
            monthly_value INTEGER NOT NULL DEFAULT 0,
            lead_source TEXT,
            next_follow_up TEXT,
            last_contacted TEXT,
            airtable_record_id TEXT,
            airtable_status TEXT,
            airtable_synced_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    now = utc_now()
    lconn.execute(
        "INSERT INTO campaigns (id, name, created_at, updated_at) VALUES (1, 'Isolated', ?, ?)",
        (now, now),
    )
    lconn.execute(
        "INSERT INTO campaigns (id, name, created_at, updated_at) VALUES (2, 'Open', ?, ?)",
        (now, now),
    )
    lconn.execute(
        "INSERT INTO leads (id, username, name, status, created_at, updated_at) VALUES (10, 'hidden_user', 'Hidden By Campaign', 'new', ?, ?)",
        (now, now),
    )
    lconn.execute(
        "INSERT INTO leads (id, username, name, status, created_at, updated_at) VALUES (11, 'open_user', 'Open Lead', 'new', ?, ?)",
        (now, now),
    )
    lconn.execute(
        "INSERT INTO campaign_recipients (campaign_id, lead_id, username) VALUES (1, 10, 'hidden_user')"
    )
    lconn.execute(
        "INSERT INTO campaign_recipients (campaign_id, lead_id, username) VALUES (2, 11, 'open_user')"
    )
    lconn.commit()

    # 1. Zero isolation columns on leads or campaigns -> sync runs without OperationalError
    client_none = _TrackingAirtableClient()
    res_none = sync_airtable(lconn, direction="both", client=client_none)
    assert res_none["pushed_to_airtable"] == 2

    # Reset sync markers and test partial schema: campaigns.exclude_from_airtable only
    lconn.execute(
        "UPDATE leads SET airtable_record_id = NULL, airtable_status = NULL, airtable_synced_at = NULL"
    )
    lconn.execute("ALTER TABLE campaigns ADD COLUMN exclude_from_airtable INTEGER DEFAULT 0")
    lconn.execute("UPDATE campaigns SET exclude_from_airtable = 1 WHERE id = 1")
    lconn.commit()

    client_camp_only = _TrackingAirtableClient()
    res_camp_only = sync_airtable(lconn, direction="both", client=client_camp_only)
    assert res_camp_only["pushed_to_airtable"] == 1
    assert [r["fields"]["Lead Name"] for r in client_camp_only.tables["Leads"]] == ["Open Lead"]

    # 3. leads.segment only (without exclude_from_airtable anywhere)
    lconn.execute("ALTER TABLE leads ADD COLUMN segment TEXT")
    lconn.execute("UPDATE leads SET segment = 'ru_expert' WHERE id = 10")
    lconn.execute("CREATE TABLE campaigns_min (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)")
    lconn.execute("INSERT INTO campaigns_min (id, name) VALUES (1, 'Legacy')")
    lconn.execute("DROP TABLE campaigns")
    lconn.execute("ALTER TABLE campaigns_min RENAME TO campaigns")
    lconn.commit()

    assert push_lead_to_airtable(lconn, 10, client=client_camp_only)["excluded"] is True
    lconn.close()
