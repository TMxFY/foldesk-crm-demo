from __future__ import annotations

import math
import sqlite3
from typing import Any

import pytest

from app.db import (
    _ensure_column,
    connect,
    get_enrichment_spend_for_day,
    init_db,
    list_enrichment_spend_summary,
    record_enrichment_ledger_entry,
)


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple[str, Any]]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1]: (str(row[2] or "").upper(), row[4]) for row in rows}


def test_init_db_adds_outreach_v2_columns_and_ledger_schema(tmp_path):
    db_path = tmp_path / "fresh.db"
    conn = connect(db_path)
    try:
        init_db(conn)
        # Re-running init_db must stay strictly idempotent.
        init_db(conn)

        camp_cols = _table_columns(conn, "campaigns")
        assert camp_cols["segment"][0] == "TEXT"
        assert camp_cols["language"][0] == "TEXT"
        assert camp_cols["copy_profile"][0] == "TEXT"
        assert camp_cols["exclude_from_airtable"][0] == "INTEGER"
        assert camp_cols["custom_step_templates_json"][0] == "TEXT"

        lead_cols = _table_columns(conn, "leads")
        for expected in (
            "segment",
            "language",
            "exclude_from_airtable",
            "company_domain",
            "pool_usd",
            "secondary_contact_json",
            "manual_action_url",
            "manual_draft_text",
        ):
            assert expected in lead_cols

        # Verify defaults when inserting minimal campaign & lead rows.
        conn.execute(
            """
            INSERT INTO campaigns (name, created_at, updated_at)
            VALUES ('Whales EN', '2026-11-22T00:00:00', '2026-11-22T00:00:00')
            """
        )
        camp = conn.execute(
            """
            SELECT segment, language, copy_profile, exclude_from_airtable, custom_step_templates_json
            FROM campaigns WHERE name = 'Whales EN'
            """
        ).fetchone()
        assert tuple(camp) == ("b2b", "en", "default", 0, None)

        conn.execute(
            """
            INSERT INTO leads (name, created_at, updated_at)
            VALUES ('Acme CEO', '2026-11-22T00:00:00', '2026-11-22T00:00:00')
            """
        )
        lead = conn.execute(
            """
            SELECT segment, language, exclude_from_airtable, company_domain, pool_usd,
                   secondary_contact_json, manual_action_url, manual_draft_text
            FROM leads WHERE name = 'Acme CEO'
            """
        ).fetchone()
        assert tuple(lead) == ("b2b", "en", 0, None, None, None, None, None)

        # Verify enrichment_ledger table & composite index.
        ledger_cols = _table_columns(conn, "enrichment_ledger")
        assert list(ledger_cols.keys()) == [
            "id",
            "day",
            "provider",
            "endpoint",
            "lead_key",
            "cost_usd",
            "hit",
            "created_at",
        ]
        idx_row = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_enrichment_ledger_day_provider'
            """
        ).fetchone()
        assert idx_row is not None
        idx_cols = [
            r[2]
            for r in conn.execute("PRAGMA index_info(idx_enrichment_ledger_day_provider)").fetchall()
        ]
        assert idx_cols == ["day", "provider"]

        plan_rows = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT COALESCE(SUM(cost_usd), 0.0)
            FROM enrichment_ledger
            WHERE day = '2026-11-22' AND provider = 'treg'
            """
        ).fetchall()
        assert any("idx_enrichment_ledger_day_provider" in str(r[-1]) for r in plan_rows)
    finally:
        conn.close()


def test_idempotent_migrations_on_preexisting_legacy_db(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = connect(db_path)
    try:
        # Simulate an older schema (including legacy leads status CHECK constraint)
        # before any outreach-v2 columns or enrichment_ledger existed.
        conn.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                session_path TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                telegram_folder_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                telegram_user_id INTEGER,
                username TEXT,
                name TEXT,
                phone TEXT,
                status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'talking', 'waiting', 'won', 'lost')),
                note TEXT,
                next_step TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                account_id INTEGER,
                daily_limit INTEGER NOT NULL DEFAULT 20,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO leads (username, name, status, created_at, updated_at)
            VALUES ('legacy_founder', 'Legacy Founder', 'new', '2026-10-01T00:00:00', '2026-10-01T00:00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO campaigns (name, daily_limit, status, created_at, updated_at)
            VALUES ('Legacy Campaign', 15, 'paused', '2026-10-01T00:00:00', '2026-10-01T00:00:00')
            """
        )
        conn.commit()

        # First migration + repeated idempotent calls must succeed cleanly.
        init_db(conn)
        init_db(conn)
        _ensure_column(conn, "leads", "company_domain", "TEXT")
        _ensure_column(conn, "campaigns", "segment", "TEXT DEFAULT 'b2b'")

        lead_row = conn.execute(
            """
            SELECT username, segment, language, exclude_from_airtable, company_domain, pool_usd
            FROM leads WHERE username = 'legacy_founder'
            """
        ).fetchone()
        assert tuple(lead_row) == ("legacy_founder", "b2b", "en", 0, None, None)

        camp_row = conn.execute(
            """
            SELECT name, segment, language, copy_profile, exclude_from_airtable, custom_step_templates_json
            FROM campaigns WHERE name = 'Legacy Campaign'
            """
        ).fetchone()
        assert tuple(camp_row) == ("Legacy Campaign", "b2b", "en", "default", 0, None)
    finally:
        conn.close()


def test_enrichment_ledger_helpers_and_spend_summary(tmp_path):
    db_path = tmp_path / "ledger.db"
    conn = connect(db_path)
    try:
        init_db(conn)

        empty_summary = list_enrichment_spend_summary(conn, "2026-11-22")
        assert empty_summary == {
            "day": "2026-11-22",
            "total_usd": 0.0,
            "calls": 0,
            "hits": 0,
            "by_provider": {},
        }
        assert get_enrichment_spend_for_day(conn, "2026-11-22") == 0.0
        assert get_enrichment_spend_for_day(conn, "2026-11-22", provider="treg") == 0.0

        id1 = record_enrichment_ledger_entry(
            conn,
            day="2026-11-22",
            provider="Treg",
            endpoint="/people.phone.find",
            lead_key="acme.com",
            cost_usd=0.04,
            hit=True,
            created_at="2026-11-22T10:00:00",
        )
        id2 = record_enrichment_ledger_entry(
            conn,
            day="2026-11-22",
            provider="treg",
            endpoint="/crawl4ai.phone.find",
            lead_key="beta.io",
            cost_usd=0.00095,
            hit=0,
        )
        id3 = record_enrichment_ledger_entry(
            conn,
            day="2026-11-22",
            provider="monid",
            endpoint="/instagram.profile_contact",
            lead_key="ig:brand",
            cost_usd=0.00175,
            hit=1,
        )
        # Different day must remain isolated.
        record_enrichment_ledger_entry(
            conn,
            day="2026-11-21",
            provider="treg",
            endpoint="/people.phone.find",
            lead_key="yesterday.ai",
            cost_usd=1.25,
            hit=1,
        )

        assert id1 < id2 < id3

        assert get_enrichment_spend_for_day(conn, "2026-11-22") == 0.0427
        assert get_enrichment_spend_for_day(conn, "2026-11-22", provider="treg") == 0.04095
        assert get_enrichment_spend_for_day(conn, "2026-11-22", provider="TREG") == 0.04095
        assert get_enrichment_spend_for_day(conn, "2026-11-22", provider="monid") == 0.00175
        assert get_enrichment_spend_for_day(conn, "2026-11-21", provider="treg") == 1.25

        summary = list_enrichment_spend_summary(conn, "2026-11-22")
        assert summary == {
            "day": "2026-11-22",
            "total_usd": 0.0427,
            "calls": 3,
            "hits": 2,
            "by_provider": {
                "monid": {
                    "provider": "monid",
                    "total_usd": 0.00175,
                    "calls": 1,
                    "hits": 1,
                },
                "treg": {
                    "provider": "treg",
                    "total_usd": 0.04095,
                    "calls": 2,
                    "hits": 1,
                },
            },
        }
    finally:
        conn.close()


def test_enrichment_ledger_validation_and_raw_sqlite_transaction_compat():
    raw_conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError, match="provider"):
            record_enrichment_ledger_entry(
                raw_conn,
                day="2026-11-22",
                provider="   ",
                endpoint="/phone.find",
                lead_key="acme.com",
                cost_usd=0.04,
            )

        with pytest.raises(ValueError, match="cost_usd"):
            record_enrichment_ledger_entry(
                raw_conn,
                day="2026-11-22",
                provider="treg",
                endpoint="/phone.find",
                lead_key="acme.com",
                cost_usd=-0.01,
            )

        with pytest.raises(ValueError, match="cost_usd"):
            record_enrichment_ledger_entry(
                raw_conn,
                day="2026-11-22",
                provider="treg",
                endpoint="/phone.find",
                lead_key="acme.com",
                cost_usd=math.nan,
            )

        # Works directly on raw sqlite3.Connection without row_factory.
        row_id = record_enrichment_ledger_entry(
            raw_conn,
            day="2026-11-22",
            provider="treg",
            endpoint="/phone.find",
            lead_key="raw.example",
            cost_usd=0.02,
            hit=True,
        )
        assert row_id == 1
        assert get_enrichment_spend_for_day(raw_conn, "2026-11-22") == 0.02
        summary = list_enrichment_spend_summary(raw_conn, "2026-11-22")
        assert summary["total_usd"] == 0.02
        assert summary["calls"] == 1
        assert summary["hits"] == 1
    finally:
        raw_conn.close()

    # Verify commit=False works inside ThreadSafeConnection.transaction().
    ts_conn = connect(":memory:")
    try:
        init_db(ts_conn)
        with pytest.raises(RuntimeError):
            with ts_conn.transaction():
                record_enrichment_ledger_entry(
                    ts_conn,
                    day="2026-11-22",
                    provider="treg",
                    endpoint="/phone.find",
                    lead_key="rolled_back",
                    cost_usd=0.10,
                    hit=1,
                    commit=False,
                )
                raise RuntimeError("abort savepoint")
        assert get_enrichment_spend_for_day(ts_conn, "2026-11-22") == 0.0
    finally:
        ts_conn.close()
