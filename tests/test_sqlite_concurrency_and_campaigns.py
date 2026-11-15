from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

from fastapi.testclient import TestClient

import app.cloud_persistence as cp
from app.db import as_dict, connect, init_db, utc_now
from app.folders import list_folders, pull_folders, sync_folder
from app.main import create_app
import app.outreach as outreach
from app.outreach import (
    _dialog_has_client_reply,
    create_campaign,
    get_campaign,
    is_within_outreach_schedule,
    list_campaigns,
)
from app.telegram_gateway import FakeGateway


def _seed_accounts_and_data(conn) -> tuple[list[int], int]:
    now = utc_now()
    acc_ids = [11, 22, 31]
    for idx, aid in enumerate(acc_ids):
        conn.execute(
            """
            INSERT INTO accounts (
                id, phone, session_path, status, telegram_user_id,
                telegram_username, telegram_name, created_at,
                health, updated_at, daily_limit, sent_today, provider
            ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, 'active', ?, 20, 0, 'telegram')
            """,
            (
                aid,
                f"+799900000{aid}",
                f"data/sessions/acc_{aid}.session",
                70000 + aid,
                f"sales_{aid}",
                f"Аккаунт {aid}",
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO folders (
                account_id, telegram_folder_id, title, sync_enabled,
                blocked_reason, created_at, updated_at
            ) VALUES (?, ?, ?, 1, NULL, ?, ?)
            """,
            (aid, 100 + idx, f"Папка {aid}", now, now),
        )
        conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, username, name, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'new', ?, ?)
            """,
            (aid, 80000 + aid, f"lead_{aid}", f"Лид {aid}", now, now),
        )
    conn.commit()

    camp = create_campaign(
        conn,
        name="Concurrent Test Campaign",
        account_id=31,
        daily_limit=20,
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}!"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Возвращаюсь к письму"},
        ],
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name,
            current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 1, 80011, 'lead_11', 'Лид 11', 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], now, now, now),
    )
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title,
            is_group, unread_count, last_from_peer, muted, lead_id,
            created_at, updated_at
        ) VALUES (31, 'user', 80011, 'lead_11', 'Лид 11', 0, 0, 0, 0, 1, ?, ?)
        """,
        (now, now),
    )
    conn.commit()
    return acc_ids, int(camp["id"])


def test_connect_overlapping_cursors_and_concurrent_threads_are_serialized(tmp_path):
    """Deterministic check that connect() serializes concurrent access and buffers cursors."""
    db_file = tmp_path / "conc_unit.db"
    conn = connect(db_file)
    init_db(conn)
    conn.execute("INSERT INTO kv_settings (key, value, updated_at) VALUES ('k1', 'v1', '2026-01-01')")
    conn.execute("INSERT INTO kv_settings (key, value, updated_at) VALUES ('k2', 'v2', '2026-01-01')")
    conn.commit()

    # 1) Overlapping cursor isolation: mutating DB after execute() must not alter already-executed cursor rows
    cur = conn.execute(
        "SELECT key, value FROM kv_settings WHERE key IN ('k1', 'k2') ORDER BY key"
    )
    conn.execute("DELETE FROM kv_settings WHERE key = 'k1'")
    conn.execute("UPDATE kv_settings SET value = 'mutated' WHERE key = 'k2'")
    conn.commit()
    rows_snapshot = [as_dict(r) for r in cur.fetchall()]
    assert rows_snapshot == [
        {"key": "k1", "value": "v1"},
        {"key": "k2", "value": "v2"},
    ]

    # 2) Transaction context manager (`with conn:`) must serialize other threads so dirty writes aren't observed
    entered_tx = threading.Event()
    reader_saw: list[object] = []
    reader_errs: list[Exception] = []

    def _writer_in_tx():
        with conn:
            conn.execute(
                "INSERT INTO kv_settings (key, value, updated_at) VALUES ('tx_only', 'dirty', '2026-01-01')"
            )
            entered_tx.set()
            time.sleep(0.05)
            conn.execute("DELETE FROM kv_settings WHERE key = 'tx_only'")

    def _reader_during_tx():
        entered_tx.wait(timeout=2.0)
        try:
            row = conn.execute(
                "SELECT value FROM kv_settings WHERE key = 'tx_only'"
            ).fetchone()
            reader_saw.append(row["value"] if row is not None else None)
        except Exception as exc:
            reader_errs.append(exc)

    t_w = threading.Thread(target=_writer_in_tx)
    t_r = threading.Thread(target=_reader_during_tx)
    t_w.start()
    t_r.start()
    t_w.join(timeout=5.0)
    t_r.join(timeout=5.0)
    assert not reader_errs, f"Concurrent transaction reader error: {reader_errs}"
    assert reader_saw == [None], f"Reader saw uncommitted/interleaved state: {reader_saw}"

    # 3) Concurrent threads running execute + executescript + PRAGMA database_list + commit must never crash
    for i in range(10):
        conn.execute(
            "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES (?, ?, '2026-01-01')",
            (f"item_{i}", f"val_{i}"),
        )
    conn.commit()

    thread_errors: list[tuple[int, str]] = []

    def _hammer(worker_id: int):
        target_key = f"item_{worker_id % 10}"
        expected_val = f"val_{worker_id % 10}"
        for step in range(80):
            try:
                if step % 15 == 0:
                    conn.executescript("SELECT 1;")
                elif step % 7 == 0:
                    db_row = conn.execute("PRAGMA database_list").fetchone()
                    if db_row is None or str(db_row[2]) != str(db_file):
                        thread_errors.append((worker_id, f"bad database_list: {db_row}"))
                        return
                elif step % 11 == 0:
                    c2 = conn.cursor()
                    r2 = c2.execute(
                        "SELECT value FROM kv_settings WHERE key = ?", (target_key,)
                    ).fetchone()
                    if r2 is None or r2["value"] != expected_val:
                        thread_errors.append((worker_id, f"cursor mismatch: {r2}"))
                        return
                else:
                    row = conn.execute(
                        "SELECT key, value FROM kv_settings WHERE key = ?", (target_key,)
                    ).fetchone()
                    if row is None or row["value"] != expected_val:
                        thread_errors.append((worker_id, f"row mismatch: {row}"))
                        return
            except Exception as exc:
                thread_errors.append((worker_id, f"{type(exc).__name__}: {exc}"))
                return

    workers = [threading.Thread(target=_hammer, args=(i,)) for i in range(10)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=15.0)
    assert not thread_errors, f"Concurrent SQLite errors: {thread_errors}"
    conn.close()


def test_concurrent_http_folders_campaigns_leads_endpoints(tmp_path, monkeypatch):
    """Simulate boot-time Promise.all on /api/folders + /api/campaigns + /api/leads across worker threads."""
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "StrongTimPass#2026")
    monkeypatch.setenv("CRM_VLAD_PASSWORD", "StrongVladPass#2026")
    monkeypatch.setenv("CRM_OPERATOR_PASSWORD", "StrongOpPass#2026")

    conn = connect(tmp_path / "http_conc.db")
    init_db(conn)
    acc_ids, camp_id = _seed_accounts_and_data(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        login_resp = client.post(
            "/api/auth/login",
            json={"username": "tim", "password": "StrongTimPass#2026"},
            headers={"User-Agent": "Mozilla/5.0 Chrome/125.0"},
        )
        assert login_resp.status_code == 200, login_resp.text
        token = login_resp.json()["token"]
        hdrs = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 Chrome/125.0",
        }

        def _hit_endpoint(idx: int):
            kind = idx % 4
            if kind == 0:
                aid = acc_ids[idx % len(acc_ids)]
                resp = client.get(f"/api/folders?account_id={aid}", headers=hdrs)
                return ("folders", aid, resp.status_code, resp.json())
            if kind == 1:
                resp = client.get("/api/campaigns", headers=hdrs)
                return ("campaigns", None, resp.status_code, resp.json())
            if kind == 2:
                resp = client.get(f"/api/campaigns/{camp_id}", headers=hdrs)
                return ("campaign_detail", camp_id, resp.status_code, resp.json())
            resp = client.get("/api/leads", headers=hdrs)
            return ("leads", None, resp.status_code, resp.json())

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(_hit_endpoint, range(72)))

        for kind, aid, status, body in results:
            assert status == 200, f"{kind} (aid={aid}) returned {status}: {body}"
            if kind == "folders":
                assert isinstance(body, list) and len(body) == 1
                assert body[0]["account_id"] == aid
            elif kind == "campaigns":
                assert isinstance(body, list) and len(body) == 1
                assert body[0]["id"] == camp_id
            elif kind == "campaign_detail":
                assert body["id"] == camp_id
            elif kind == "leads":
                assert isinstance(body, list) and len(body) == 3

        missing = client.get("/api/folders?account_id=999999", headers=hdrs)
        assert missing.status_code == 404


def test_outreach_null_and_malformed_dialog_fields_do_not_crash(tmp_path, monkeypatch):
    conn = connect(tmp_path / "null_guards.db")
    init_db(conn)
    _, camp_id = _seed_accounts_and_data(conn)

    rec = {"id": 1, "current_step": 1, "last_sent_at": "2026-01-01T00:00:00"}
    assert _dialog_has_client_reply(conn, {"id": None, "last_from_peer": 0}, rec) is False
    assert (
        _dialog_has_client_reply(
            conn,
            {"id": None, "last_from_peer": 1, "last_message_at": None},
            rec,
        )
        is False
    )
    assert _dialog_has_client_reply(conn, {"id": "bad", "last_from_peer": None}, rec) is False
    assert _dialog_has_client_reply(conn, None, rec) is False
    assert _dialog_has_client_reply(conn, {"id": 1, "last_from_peer": 0}, None) is False

    # Simulate a corrupted/partial dialog row returned during campaign reply sync
    monkeypatch.setattr(
        outreach,
        "_find_recipient_dialogs",
        lambda *_args, **_kwargs: [
            {"id": None, "last_from_peer": 0, "account_id": 31},
            {"id": "invalid", "last_from_peer": None},
        ],
    )
    camp = get_campaign(conn, camp_id)
    assert camp["id"] == camp_id
    all_camps = list_campaigns(conn)
    assert len(all_camps) == 1
    assert all_camps[0]["id"] == camp_id
    assert isinstance(is_within_outreach_schedule(conn), bool)


def test_folders_malformed_gateway_payloads_do_not_crash(tmp_path):
    import asyncio

    conn = connect(tmp_path / "folders_guard.db")
    init_db(conn)
    _seed_accounts_and_data(conn)

    class _MalformedFoldersGateway(FakeGateway):
        async def list_folders(self, session_path: str) -> list:
            return [
                {"telegram_folder_id": None, "title": "Skip None"},
                {"telegram_folder_id": "not-an-int", "title": "Skip Bad"},
                {"telegram_folder_id": 201, "title": "Valid Folder"},
            ]

        async def folder_peers(self, session_path: str, folder_id: int) -> list:
            return [
                {"peer_type": "user", "telegram_user_id": "bad-id", "username": "skip"},
                {"peer_type": "user", "telegram_user_id": True, "username": "skip_bool"},
                {"peer_type": "user", "telegram_user_id": 99123, "username": "good_peer", "name": "Good"},
            ]

    gw = _MalformedFoldersGateway()
    pulled = asyncio.run(pull_folders(conn, 31, gw))
    assert [f["telegram_folder_id"] for f in pulled] == [201]
    res = asyncio.run(sync_folder(conn, pulled[0]["id"], gw))
    assert res["total"] == 1
    assert len(list_folders(conn, 31)) == 1


def test_cloud_persistence_backup_and_restore_with_wrapped_connection(tmp_path, monkeypatch):
    """Ensure backup_to_cloud(conn) and restore_from_cloud(live_conn=conn) unwrap _ThreadSafeConnection."""
    db_path = tmp_path / "cloud_src.db"
    conn = connect(db_path)
    init_db(conn)
    conn.execute("INSERT INTO kv_settings (key, value, updated_at) VALUES ('cloud_key', 'from_src', '2026-01-01')")
    conn.commit()

    stored: dict[str, bytes] = {}
    monkeypatch.setattr(
        cp,
        "_resolve_r2_config",
        lambda _conn=None: {"account_id": "acc", "access_key": "k", "secret_key": "s", "bucket": "b"},
    )

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def put(self, url, content=b"", headers=None):
            stored["payload"] = content
            return type("R", (), {"status_code": 200, "text": ""})()

        def get(self, url, headers=None):
            data = stored.get("payload", b"")
            return type("R", (), {"status_code": 200 if data else 404, "content": data})()

    monkeypatch.setattr(cp.httpx, "Client", _FakeClient)
    b_res = cp.backup_to_cloud(conn, db_path)
    assert b_res["ok"] is True

    # Restore directly into a separate wrapped live_conn
    live_db = tmp_path / "cloud_live.db"
    live_conn = connect(live_db)
    init_db(live_conn)
    r_res = cp.restore_from_cloud(live_db, force=True, live_conn=live_conn)
    assert r_res["ok"] is True and r_res["restored"] is True
    row = live_conn.execute("SELECT value FROM kv_settings WHERE key = 'cloud_key'").fetchone()
    assert row is not None and row["value"] == "from_src"
    conn.close()
    live_conn.close()
