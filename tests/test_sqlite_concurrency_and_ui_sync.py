from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

import app.inbox as inbox_mod
from app.db import ROOT, connect, init_db, utc_now
from app.inbox import apply_incoming, mark_dialog_read
from app.folders import list_folders
from app.leads import create_lead
from app.main import create_app, start_account_watch, stop_inbox_watches, sync_live_tg_read_receipts_once
from app.outreach import create_campaign, is_within_outreach_schedule, list_campaigns
from app.telegram_gateway import FakeGateway, listening_event


def _seed_crm_state(conn, *, acc_phone: str = "+79991112233", sess: str = "sessions/test_acc"):
    now = utc_now()
    acc_id = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, telegram_username, telegram_name, created_at)
        VALUES (?, ?, 'connected', 'sales_bot_acc', 'Sales Acc', ?)
        """,
        (acc_phone, sess, now),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO folders (account_id, telegram_folder_id, title, sync_enabled, created_at, updated_at)
        VALUES (?, 101, 'Clients', 1, ?, ?)
        """,
        (acc_id, now, now),
    )
    conn.commit()
    camp = create_campaign(
        conn,
        name="Stress Campaign",
        account_id=acc_id,
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hi {name}"}],
    )
    lead = create_lead(
        conn,
        name="Client One",
        username="client_one",
        telegram_user_id=70011,
        status="talking",
        account_id=acc_id,
    )
    dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 70011,
            "username": "client_one",
            "name": "Client One",
            "text": "Привет, расскажите про условия",
            "telegram_message_id": 1001,
            "sent_at": now,
            "direction": "in",
            "peer_type": "user",
        },
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name,
            current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, 70011, 'client_one', 'Client One', 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], lead["id"], now, now, now),
    )
    conn.commit()
    return acc_id, camp["id"], lead["id"], dlg["id"]


def test_connect_returns_threadsafe_sqlite_connection_and_locked_conn(tmp_path):
    from app.db import conn_lock, locked_conn

    db_path = tmp_path / "threadsafe.db"
    conn = connect(db_path)
    try:
        init_db(conn)
        assert isinstance(conn, sqlite3.Connection)
        assert is_within_outreach_schedule(conn) in (True, False)
        lock = conn_lock(conn)
        assert lock is not None
        with locked_conn(conn):
            with locked_conn(conn):
                row = conn.execute("SELECT 1 AS ok").fetchone()
                assert row["ok"] == 1
    finally:
        conn.close()

    raw = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        with locked_conn(raw):
            assert raw.execute("SELECT 42").fetchone()[0] == 42
    finally:
        raw.close()


def test_shared_conn_threadpool_concurrency_stress(tmp_path):
    conn = connect(tmp_path / "stress.db")
    init_db(conn)
    acc_id, _camp_id, _lead_id, dlg_id = _seed_crm_state(conn)

    # Direct 16-thread Python DB stress against the shared conn
    direct_errors: list[tuple[str, str]] = []
    barrier = threading.Barrier(16)

    def direct_worker(tid: int):
        try:
            barrier.wait(timeout=5)
            for idx in range(25):
                if tid % 4 == 0:
                    folders = list_folders(conn, acc_id)
                    assert len(folders) == 1
                elif tid % 4 == 1:
                    camps = list_campaigns(conn)
                    assert len(camps) >= 1
                elif tid % 4 == 2:
                    apply_incoming(
                        conn,
                        acc_id,
                        {
                            "telegram_user_id": 70011 + (idx % 3),
                            "username": f"client_{idx % 3}" if idx % 3 else "client_one",
                            "name": "Client",
                            "text": f"msg-{tid}-{idx}",
                            "telegram_message_id": 10000 + tid * 100 + idx,
                            "sent_at": utc_now(),
                            "direction": "in",
                        },
                    )
                else:
                    mark_dialog_read(conn, dlg_id)
        except Exception as exc:
            direct_errors.append((type(exc).__name__, str(exc)))

    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [pool.submit(direct_worker, tid) for tid in range(16)]
        for f in futs:
            f.result()

    assert direct_errors == [], f"Direct shared-conn race errors: {direct_errors}"

    # Concurrent HTTP requests via TestClient + background DB writer on same app.state.conn
    app = create_app(conn=conn, gateway=FakeGateway())
    http_errors: list[str] = []
    stop_writer = threading.Event()

    def bg_writer():
        seq = 50000
        while not stop_writer.is_set():
            seq += 1
            try:
                apply_incoming(
                    conn,
                    acc_id,
                    {
                        "telegram_user_id": 70011,
                        "username": "client_one",
                        "name": "Client One",
                        "text": f"live-{seq}",
                        "telegram_message_id": seq,
                        "sent_at": utc_now(),
                        "direction": "in",
                    },
                )
            except Exception as exc:
                http_errors.append(f"writer:{type(exc).__name__}:{exc}")
                break

    with TestClient(app) as client:
        writer_t = threading.Thread(target=bg_writer, daemon=True)
        writer_t.start()
        try:
            def http_worker(tid: int):
                for idx in range(15):
                    for method, url in (
                        ("GET", f"/api/folders?account_id={acc_id}"),
                        ("GET", "/api/campaigns"),
                        ("GET", f"/api/inbox?account_id={acc_id}&with_counts=1&crm_only=1"),
                        ("POST", f"/api/inbox/dialogs/{dlg_id}/read?crm_only=1"),
                    ):
                        resp = client.request(method, url)
                        if resp.status_code != 200:
                            http_errors.append(f"t{tid}-{idx} {method} {url} -> {resp.status_code}: {resp.text}")

            with ThreadPoolExecutor(max_workers=12) as pool:
                futs = [pool.submit(http_worker, i) for i in range(12)]
                for f in futs:
                    f.result()
        finally:
            stop_writer.set()
            writer_t.join(timeout=5)
            conn.close()

    assert http_errors == [], f"HTTP concurrency errors: {http_errors[:5]}"


def test_start_account_watch_on_outgoing_and_on_read_inbox_sse_and_legacy_gateway(tmp_path, monkeypatch):
    conn = connect(tmp_path / "watch_sync.db")
    init_db(conn)
    acc_id, _camp_id, _lead_id, dlg_id = _seed_crm_state(conn, sess="sessions/watch_1")
    conn.execute("UPDATE dialogs SET unread_count = 2, last_from_peer = 1 WHERE id = ?", (dlg_id,))
    conn.execute(
        "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) VALUES (?, 55, 'out', 'Hi', ?, ?, ?)",
        (dlg_id, utc_now(), utc_now(), utc_now()),
    )
    conn.commit()

    # Ensure inbox doubles exist if Subagent A isn't merged yet
    if not hasattr(inbox_mod, "apply_live_outgoing"):
        def _fake_apply_live_outgoing(db_conn, aid: int, message: dict):
            pid = int(message["telegram_user_id"])
            now = utc_now()
            db_conn.execute(
                """
                UPDATE dialogs
                SET unread_count = 0, last_from_peer = 0, last_message_preview = ?, last_message_at = ?, updated_at = ?
                WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
                """,
                (message.get("text") or "", message.get("sent_at") or now, now, aid, pid),
            )
            db_conn.commit()
            row = db_conn.execute(
                "SELECT * FROM dialogs WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?",
                (aid, pid),
            ).fetchone()
            return {
                "id": row["id"],
                "account_id": row["account_id"],
                "telegram_user_id": row["peer_id"],
                "unread_count": row["unread_count"],
                "last_from_peer": row["last_from_peer"],
                "last_message_preview": row["last_message_preview"],
                "peer_type": row["peer_type"],
                "is_group": row["is_group"],
                "lead_id": row["lead_id"],
            }

        monkeypatch.setattr(inbox_mod, "apply_live_outgoing", _fake_apply_live_outgoing, raising=False)

    if not hasattr(inbox_mod, "apply_inbox_read_update"):
        def _fake_apply_inbox_read_update(db_conn, aid: int, peer_id: int, max_id: int | None = None):
            now = utc_now()
            db_conn.execute(
                "UPDATE dialogs SET unread_count = 0, updated_at = ? WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?",
                (now, aid, int(peer_id)),
            )
            db_conn.commit()
            row = db_conn.execute(
                "SELECT * FROM dialogs WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?",
                (aid, int(peer_id)),
            ).fetchone()
            return {
                "id": row["id"],
                "account_id": row["account_id"],
                "telegram_user_id": row["peer_id"],
                "unread_count": row["unread_count"],
                "last_from_peer": row["last_from_peer"],
                "peer_type": row["peer_type"],
                "is_group": row["is_group"],
                "lead_id": row["lead_id"],
            }

        monkeypatch.setattr(inbox_mod, "apply_inbox_read_update", _fake_apply_inbox_read_update, raising=False)

    class DualWatchGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.on_outgoing_cb = None
            self.on_read_cb = None

        async def watch_private(self, session_path: str, on_message, on_read=None, on_outgoing=None) -> None:
            self.on_read_cb = on_read
            self.on_outgoing_cb = on_outgoing
            await super().watch_private(session_path, on_message, on_read=on_read)

    gw = DualWatchGateway()
    app = create_app(conn=conn, gateway=gw)

    async def _run():
        q = app.state.inbox_hub.subscribe(acc_id)
        try:
            await start_account_watch(app, acc_id)
            assert gw.on_read_cb is not None
            assert gw.on_outgoing_cb is not None

            # 1. Read inbox update from TG Desktop/phone -> clears unread_count & publishes SSE
            await gw.on_read_cb({"telegram_user_id": 70011, "read_inbox_max_id": 1001})
            ev1 = await asyncio.wait_for(q.get(), timeout=2)
            assert ev1["id"] == dlg_id
            assert ev1["unread_count"] == 0

            # 2. Outbox read update -> stamps campaign_recipients.read_at
            await gw.on_read_cb({"telegram_user_id": 70011, "read_outbox_max_id": 55})
            rec_row = conn.execute(
                "SELECT read_at FROM campaign_recipients WHERE telegram_user_id = 70011"
            ).fetchone()
            assert rec_row["read_at"] is not None

            # 3. Outgoing reply from phone/TG Desktop -> updates dialog & publishes SSE
            await gw.on_outgoing_cb(
                {
                    "telegram_user_id": 70011,
                    "telegram_message_id": 2002,
                    "text": "Ответ с телефона",
                    "direction": "out",
                    "sent_at": utc_now(),
                }
            )
            ev2 = await asyncio.wait_for(q.get(), timeout=2)
            assert ev2["id"] == dlg_id
            assert ev2["last_from_peer"] == 0
            assert ev2["unread_count"] == 0
            assert ev2["last_message_preview"] == "Ответ с телефона"
        finally:
            app.state.inbox_hub.unsubscribe(acc_id, q)
            await stop_inbox_watches(app)

    asyncio.run(_run())

    # Also verify minimal custom gateway without on_read / on_outgoing kwargs doesn't break
    class LegacyFakeGateway:
        def __init__(self):
            self.called = False

        async def load_inbox(self, session_path, limit=30):
            return []

        async def watch_private(self, session_path, on_message):
            self.called = True
            listening_event(self, session_path).set()
            await asyncio.Future()

    legacy_gw = LegacyFakeGateway()
    app2 = create_app(conn=conn, gateway=legacy_gw)

    async def _run_legacy():
        await start_account_watch(app2, acc_id)
        try:
            assert legacy_gw.called is True
        finally:
            await stop_inbox_watches(app2)

    asyncio.run(_run_legacy())
    conn.close()


def test_mark_dialog_read_publishes_sse_calls_gateway_and_honors_crm_only(tmp_path):
    conn = connect(tmp_path / "read_ack.db")
    init_db(conn)
    acc_id, _camp_id, _lead_id, crm_dlg_id = _seed_crm_state(conn, sess="sessions/read_ack")
    # Non-CRM stranger dialog with unread=4 (must NOT pollute crm_only=1 counts)
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer, muted,
            created_at, updated_at
        ) VALUES (?, 'user', 99999, 'stranger_chat', 'Stranger', 0, 4, ?, 'spam', 1, 0, ?, ?)
        """,
        (acc_id, utc_now(), utc_now(), utc_now()),
    )
    conn.commit()

    class ReadAckGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.read_acks = []
            self.fail_next = False

        async def mark_dialog_read(self, session_path: str, peer_id: int, max_id: int | None = None) -> bool:
            if self.fail_next:
                raise RuntimeError("telegram offline")
            self.read_acks.append((session_path, int(peer_id), max_id))
            return True

    gw = ReadAckGateway()
    app = create_app(conn=conn, gateway=gw)
    q = app.state.inbox_hub.subscribe(acc_id)
    try:
        with TestClient(app) as client:
            # 1. Mark read with incoming messages present -> passes max_id = max incoming telegram_message_id
            now = utc_now()
            conn.execute(
                """
                INSERT INTO messages (
                    dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
                ) VALUES (?, 1501, 'in', 'Первое', ?, ?, ?), (?, 2050, 'in', 'Второе', ?, ?, ?), (?, 3000, 'out', 'Ответ', ?, ?, ?)
                """,
                (crm_dlg_id, now, now, now, crm_dlg_id, now, now, now, crm_dlg_id, now, now, now),
            )
            conn.commit()
            res = client.post(f"/api/inbox/dialogs/{crm_dlg_id}/read?crm_only=1")
            assert res.status_code == 200
            body = res.json()
            assert body["unread_count"] == 0
            assert body["counts"]["unread"] == 0
            assert body["all_counts"]["unread"] == 0
            assert len(gw.read_acks) == 1
            assert gw.read_acks[0] == ("sessions/read_ack", 70011, 2050)

            published = q.get_nowait()
            assert published["id"] == crm_dlg_id
            assert published["unread_count"] == 0

            # Unfiltered read keeps global counts backward-compatible
            res_unfiltered = client.post(f"/api/inbox/dialogs/{crm_dlg_id}/read")
            assert res_unfiltered.status_code == 200
            assert res_unfiltered.json()["counts"]["unread"] == 1

            # Gateway error must NOT fail HTTP 200 mark-read
            gw.fail_next = True
            res_offline = client.post(f"/api/inbox/dialogs/{crm_dlg_id}/read?crm_only=1")
            assert res_offline.status_code == 200
            assert res_offline.json()["unread_count"] == 0
    finally:
        app.state.inbox_hub.unsubscribe(acc_id, q)
        conn.close()


def test_sync_live_tg_read_receipts_once_publishes_only_changed_dialogs_to_sse(tmp_path):
    conn = connect(tmp_path / "sync_receipts.db")
    init_db(conn)
    acc_id, _camp_id, _lead_id, crm_dlg_id = _seed_crm_state(conn, sess="sessions/sync_1")
    assert conn.execute("SELECT unread_count FROM dialogs WHERE id = ?", (crm_dlg_id,)).fetchone()[0] == 1

    gw = FakeGateway(
        dialogs=[
            {
                "telegram_user_id": 70011,
                "username": "client_one",
                "name": "Client One",
                "unread_count": 0,
                "last_from_peer": 1,
                "last_message_preview": "Привет, расскажите про условия",
            }
        ]
    )
    app = create_app(conn=conn, gateway=gw)

    async def _run():
        q = app.state.inbox_hub.subscribe(acc_id)
        try:
            res1 = await sync_live_tg_read_receipts_once(app, account_id=acc_id)
            assert res1["ok"] is True
            assert res1["synced_accounts"] == 1
            row_after = conn.execute("SELECT unread_count FROM dialogs WHERE id = ?", (crm_dlg_id,)).fetchone()
            assert row_after["unread_count"] == 0

            published = await asyncio.wait_for(q.get(), timeout=2)
            assert published["id"] == crm_dlg_id
            assert published["unread_count"] == 0

            # Second identical sync must not spam duplicate SSE dialog events
            await sync_live_tg_read_receipts_once(app, account_id=acc_id)
            assert q.empty()
        finally:
            app.state.inbox_hub.unsubscribe(acc_id, q)

    asyncio.run(_run())
    conn.close()


def test_app_js_syntax_and_inbox_badge_sync_rules():
    js_path = ROOT / "app" / "static" / "app.js"
    syntax = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
    assert syntax.returncode == 0, f"app.js syntax error:\n{syntax.stderr}"

    source = js_path.read_text(encoding="utf-8")
    # Ensure crm_only=1 is passed when marking read/unread from CRM UI and no < 30 truncation bug blocks count sync
    assert "/read?crm_only=1" in source
    assert "/unread?crm_only=1" in source
    assert "state.allDialogs.length < 30" not in source
    assert "rows.length < 30" not in source
    assert "inboxListLoadToken" in source
    assert "pendingReadDialogIds" in source


def test_db_wal_migration_and_threadsafe_connection_methods(tmp_path):
    db_file = tmp_path / "wal_check.db"
    conn = connect(db_file)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"

        init_db(conn)
        dlg_cols = {r[1] for r in conn.execute("PRAGMA table_info(dialogs)").fetchall()}
        assert {"last_read_at", "read_inbox_max_id", "reply_turn_seq"} <= dlg_cols

        notif_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(bot_reply_notifications)").fetchall()
        }
        assert {"turn_key", "turn_seq", "incoming_message_id", "preview_hash"} <= notif_cols

        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('k1', 'v1', ?)",
                (utc_now(),),
            )
            assert isinstance(conn.in_transaction, bool)

        dump_lines = list(conn.iterdump())
        assert any("kv_settings" in line for line in dump_lines)

        dst_conn = connect(tmp_path / "backup_dst.db")
        try:
            conn.backup(dst_conn)
            copied = dst_conn.execute("SELECT value FROM kv_settings WHERE key = 'k1'").fetchone()
            assert copied is not None and copied[0] == "v1"
        finally:
            dst_conn.close()
    finally:
        conn.close()


def test_ensure_private_inbox_per_account_and_sync_over_30_dialogs(tmp_path, monkeypatch):
    from app.main import _run_db_sync, ensure_private_inbox

    conn = connect(tmp_path / "multi_acc_over30.db")
    init_db(conn)
    now = utc_now()
    acc1 = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, provider, created_at)
        VALUES ('+79991000001', 'sessions/acc1', 'connected', 'telegram', ?)
        """,
        (now,),
    ).lastrowid
    acc2 = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, provider, created_at)
        VALUES ('+79991000002', 'sessions/acc2', 'connected', 'telegram', ?)
        """,
        (now,),
    ).lastrowid
    # Seed 35 dialogs on acc1 where dialog #35 has unread_count=2
    for idx in range(1, 36):
        ts = f"2026-11-15T10:{idx:02d}:00"
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                provider, created_at, updated_at
            ) VALUES (?, 'user', ?, ?, ?, 0, ?, ?, ?, 1, 'telegram', ?, ?)
            """,
            (
                acc1,
                80000 + idx,
                f"user_{idx}",
                f"User {idx}",
                2 if idx == 35 else 0,
                ts,
                f"Msg {idx}",
                now,
                now,
            ),
        )
    conn.commit()

    class CountingMultiGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def load_inbox(self, session_path: str, limit: int = 30) -> list:
            self.calls.append((session_path, limit))
            if session_path == "sessions/acc2":
                return [
                    {
                        "telegram_user_id": 99001,
                        "username": "acc2_peer",
                        "name": "Acc2 Peer",
                        "unread_count": 1,
                        "last_from_peer": 1,
                        "last_message_preview": "Hi acc2",
                    }
                ]
            # acc1: return all 35 with newest first and user_35 now read (unread=0)
            return [
                {
                    "telegram_user_id": 80000 + i,
                    "username": f"user_{i}",
                    "name": f"User {i}",
                    "unread_count": 0,
                    "last_from_peer": 1,
                    "last_message_preview": f"Msg {i}",
                }
                for i in range(35, 0, -1)
            ]

    gw = CountingMultiGateway()
    app = create_app(conn=conn, gateway=gw)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    async def _run():
        # Account 2 has 0 dialogs in SQLite even though acc1 has 35 -> MUST pull acc2 inbox!
        await ensure_private_inbox(app, int(acc2))
        assert ("sessions/acc2", 30) in gw.calls
        row2 = conn.execute(
            "SELECT * FROM dialogs WHERE account_id = ? AND peer_id = 99001", (acc2,)
        ).fetchone()
        assert row2 is not None

        # Sync >30 dialogs on acc1 -> dialog #1 (oldest, beyond cap=30!) OR #35 changes must publish
        q = app.state.inbox_hub.subscribe(int(acc1))
        try:
            conn.execute(
                "UPDATE dialogs SET unread_count = 3 WHERE account_id = ? AND peer_id = 80001",
                (acc1,),
            )
            conn.commit()
            res = await sync_live_tg_read_receipts_once(app, account_id=int(acc1), limit=60)
            assert res["ok"] is True
            published_ids = []
            while not q.empty():
                published_ids.append(q.get_nowait()["telegram_user_id"])
            assert 80001 in published_ids
            assert 80035 in published_ids
        finally:
            app.state.inbox_hub.unsubscribe(int(acc1), q)

        # Verify _run_db_sync handles both ThreadSafeConnection (offloaded) and raw :memory: inline
        raw_mem = sqlite3.connect(":memory:")
        try:
            raw_tid = await _run_db_sync(raw_mem, lambda c: threading.get_ident(), raw_mem)
            ts_tid = await _run_db_sync(conn, lambda c: threading.get_ident(), conn)
            assert raw_tid == threading.get_ident()
            assert ts_tid != threading.get_ident()
        finally:
            raw_mem.close()

    asyncio.run(_run())
    conn.close()


def test_cloud_restore_holds_locked_conn(tmp_path, monkeypatch):
    import app.cloud_persistence as cloud_mod
    from app.db import conn_lock

    conn = connect(tmp_path / "restore_lock.db")
    init_db(conn)
    lock_events = []

    def _fake_restore(*, force=False, live_conn=None):
        lock_events.append(conn_lock(conn)._is_owned())
        return {"restored": False}

    monkeypatch.setattr(cloud_mod, "restore_from_cloud", _fake_restore)
    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        resp = client.post("/api/cloud/restore")
        assert resp.status_code == 200
        assert resp.json()["restored"] is False
    assert lock_events == [True]
    conn.close()
