import asyncio
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import pytest

from app.db import connect, init_db, locked_conn, utc_now
from app.main import (
    autopilot_loop,
    create_app,
    start_account_watch,
    stop_inbox_watches,
    sync_live_tg_read_receipts_once,
)
from app.telegram_gateway import FakeGateway


ROOT = Path(__file__).resolve().parent.parent


def _seed_account_and_crm_lead(conn, *, session_path: str = "sessions/live_1") -> tuple[int, int]:
    now = utc_now()
    acc_id = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, health, provider, daily_limit, created_at)
        VALUES ('+79990001122', ?, 'connected', 'active', 'telegram', 20, ?)
        """,
        (session_path, now),
    ).lastrowid
    lead_id = conn.execute(
        """
        INSERT INTO leads (account_id, telegram_user_id, username, name, status, created_at, updated_at)
        VALUES (?, 70011, 'crm_buyer', 'CRM Buyer', 'talking', ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    dlg_id = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            lead_id, provider, created_at, updated_at
        ) VALUES (?, 'user', 70011, 'crm_buyer', 'CRM Buyer', 0, 1, ?, 'Hello', 1, ?, 'telegram', ?, ?)
        """,
        (acc_id, now, lead_id, now, now),
    ).lastrowid
    conn.commit()
    return int(acc_id), int(dlg_id)


def test_leased_boot_refuses_to_start_when_another_host_owns_r2_lease(tmp_path, monkeypatch):
    """Reproduces AUTH_KEY_DUPLICATED split-brain: if wait_and_acquire_tg_runtime_lease returns False,
    the app MUST NOT set tg_lease_ready=True and MUST NOT start watches or autopilot."""
    import app.cloud_persistence as cp

    conn = connect(tmp_path / "lease_guard.db")
    init_db(conn)
    _seed_account_and_crm_lead(conn)

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("CRMTG_EPHEMERAL_RUNTIME", raising=False)
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "0")
    monkeypatch.setenv("ALLOW_LOCAL_TG_SESSIONS", "1")
    monkeypatch.setenv("CLOUD_BACKUP_ENABLED", "0")
    monkeypatch.setattr(cp, "_resolve_r2_config", lambda _c=None: {"bucket": "b"})
    monkeypatch.setattr(cp, "wait_and_acquire_tg_runtime_lease", lambda *_a, **_kw: False)

    app = create_app(conn=conn, gateway=None)

    async def _exercise():
        async with app.router.lifespan_context(app):
            boot_t = getattr(app.state, "tg_lease_boot_task", None)
            if boot_t is not None:
                await asyncio.wait_for(boot_t, timeout=2.0)
            assert app.state.tg_lease_ready is False
            assert getattr(app.state, "autopilot_task", None) is None
            assert len(app.state.inbox_tasks) == 0

    try:
        asyncio.run(_exercise())
    finally:
        conn.close()


def test_sync_live_tg_read_receipts_never_imports_personal_non_crm_chats(tmp_path):
    """Reproduces Privacy-by-Default leak: background read-receipt sync pulled top-60 personal chats into dialogs."""
    conn = connect(tmp_path / "privacy_receipts.db")
    init_db(conn)
    acc_id, _dlg_id = _seed_account_and_crm_lead(conn)

    gw = FakeGateway(
        dialogs=[
            {
                "telegram_user_id": 70011,
                "username": "crm_buyer",
                "name": "CRM Buyer",
                "unread_count": 0,
                "last_from_peer": 1,
                "last_message_preview": "Read now",
            },
            {
                "telegram_user_id": 999888777,
                "username": "personal_family_chat",
                "name": "Mom Personal",
                "unread_count": 3,
                "last_from_peer": 1,
                "last_message_preview": "Private message",
            },
        ]
    )
    app = create_app(conn=conn, gateway=gw)
    try:
        res = asyncio.run(sync_live_tg_read_receipts_once(app, account_id=acc_id))
        assert res["ok"] is True
        leaked = conn.execute(
            "SELECT id FROM dialogs WHERE peer_id = 999888777"
        ).fetchone()
        assert leaked is None, "Personal non-CRM chat leaked into dialogs during background read-receipt sync!"
    finally:
        conn.close()


def test_autopilot_loop_records_and_surfaces_tick_exceptions_in_deep_health(tmp_path, monkeypatch):
    """Reproduces silent autopilot death: exceptions inside run_autopilot_tick were swallowed with zero telemetry,
    and /api/health had no deep runtime verification."""
    import app.main as main_mod

    conn = connect(tmp_path / "autopilot_telemetry.db")
    init_db(conn)
    _seed_account_and_crm_lead(conn)

    def _boom(*_a, **_kw):
        raise RuntimeError("Simulated gateway explosion in tick")

    monkeypatch.setattr(main_mod, "run_autopilot_tick", _boom)
    app = create_app(conn=conn, gateway=FakeGateway())

    async def _run_one_tick():
        task = asyncio.create_task(autopilot_loop(app, interval_seconds=0.02))
        try:
            await asyncio.sleep(0.07)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_run_one_tick())
        assert getattr(app.state, "autopilot_error_count", 0) >= 1
        assert "Simulated gateway explosion" in str(getattr(app.state, "autopilot_last_error", ""))

        with TestClient(app) as client:
            resp = client.get("/api/health?deep=1")
            assert resp.status_code == 200
            data = resp.json()
            assert data["db_ok"] is True
            assert data["active_accounts"] >= 1
            assert data["autopilot_error_count"] >= 1
    finally:
        conn.close()


def test_start_account_watch_self_heals_after_transient_disconnect(tmp_path):
    """Reproduces watch_private dying permanently on the first transient MTProto disconnect."""
    conn = connect(tmp_path / "watch_self_heal.db")
    init_db(conn)
    acc_id, _dlg_id = _seed_account_and_crm_lead(conn, session_path="sessions/flaky_1")

    class FlakyWatchGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.attempts = 0
            self.recovered = asyncio.Event()

        async def watch_private(self, session_path, on_message, **kwargs):
            self.attempts += 1
            ev = self._listening.setdefault(session_path, asyncio.Event())
            ev.set()
            if self.attempts == 1:
                await asyncio.sleep(0.02)
                raise ConnectionError("Server closed the connection: 2 bytes read")
            await on_message(
                {
                    "telegram_user_id": 70011,
                    "username": "crm_buyer",
                    "name": "CRM Buyer",
                    "telegram_message_id": 555,
                    "text": "Reply after reconnect!",
                    "sent_at": utc_now(),
                }
            )
            self.recovered.set()
            await asyncio.Future()

    gw = FlakyWatchGateway()
    app = create_app(conn=conn, gateway=gw)
    app.state.watch_retry_delay = 0.03

    async def _exercise():
        await start_account_watch(app, acc_id)
        try:
            await asyncio.wait_for(gw.recovered.wait(), timeout=1.5)
            assert gw.attempts >= 2
            msg = conn.execute(
                "SELECT text FROM messages WHERE telegram_message_id = 555"
            ).fetchone()
            assert msg is not None and msg["text"] == "Reply after reconnect!"
        finally:
            await stop_inbox_watches(app)

    try:
        asyncio.run(_exercise())
    finally:
        conn.close()


def test_systemd_service_enables_persistent_tg_watches():
    """Reproduces dead live-inbox on Contabo VPS: deploy/systemd/crmtg.service omitted ENABLE_TG_PERSISTENT_WATCHES=1."""
    unit_text = (ROOT / "deploy" / "systemd" / "crmtg.service").read_text(encoding="utf-8")
    assert 'Environment="ENABLE_TG_PERSISTENT_WATCHES=1"' in unit_text


def test_airtable_sync_endpoint_does_not_hold_global_db_lock_during_slow_http(tmp_path):
    """Reproduces global CRM freeze: POST /api/airtable/sync held app.state.conn._lock across slow HTTP I/O."""
    db_file = tmp_path / "nonblocking_airtable.db"
    conn = connect(db_file)
    init_db(conn)
    now = utc_now()
    for k, v in (
        ("airtable_api_key", "pat_fake_key"),
        ("airtable_base_id", "appFakeBase"),
        ("airtable_table_name", "Leads"),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (k, v, now),
        )
    conn.commit()

    class SlowAirtableClient:
        def list_records(self, *_a, **_kw):
            time.sleep(0.55)
            return []

        def create_records(self, *_a, **_kw):
            return []

        def update_records(self, *_a, **_kw):
            return []

    app = create_app(conn=conn, gateway=FakeGateway())
    app.state.airtable_client = SlowAirtableClient()

    lock_wait_seconds = []

    def _concurrent_reader():
        time.sleep(0.10)
        t0 = time.monotonic()
        with locked_conn(app.state.conn):
            app.state.conn.execute("SELECT COUNT(*) FROM leads").fetchone()
        lock_wait_seconds.append(time.monotonic() - t0)

    try:
        with TestClient(app) as client:
            reader = threading.Thread(target=_concurrent_reader)
            reader.start()
            resp = client.post("/api/airtable/sync", json={"direction": "pull"})
            reader.join(timeout=2.0)
            assert resp.status_code == 200
            assert lock_wait_seconds, "Reader thread did not finish"
            assert lock_wait_seconds[0] < 0.20, (
                f"Global DB lock was held during slow HTTP sync ({lock_wait_seconds[0]:.3f}s)!"
            )
    finally:
        conn.close()


def test_playable_voice_http_route_falls_back_to_original_ogg_instead_of_empty_200(
    tmp_path, monkeypatch
):
    """P1 seam repro: when ensure_playable_voice_wav returns None (corrupt OGG / no ffmpeg),
    GET /media/inbox/<file>?playable=1 and message media route MUST NOT return 200 OK with 0 bytes."""
    import app.main as main_mod

    media_dir = tmp_path / "inbox_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    raw_ogg = b"OggS-real-client-audio-payload-bytes"
    (media_dir / "voice_broken.ogg").write_bytes(raw_ogg)
    monkeypatch.setattr(main_mod, "INBOX_MEDIA_DIR", media_dir)
    monkeypatch.setattr(main_mod, "ensure_playable_voice_wav", lambda *_a, **_kw: None)

    conn = connect(tmp_path / "voice_http.db")
    init_db(conn)
    acc_id, dlg_id = _seed_account_and_crm_lead(conn)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at,
            media_url, media_type, media_mime, media_filename, created_at, updated_at
        ) VALUES (?, 901, 'in', '', ?, '/media/inbox/voice_broken.ogg', 'voice', 'audio/ogg', 'voice_broken.ogg', ?, ?)
        """,
        (dlg_id, now, now, now),
    )
    conn.commit()
    app = create_app(conn=conn, gateway=FakeGateway())
    try:
        with TestClient(app) as client:
            r1 = client.get("/media/inbox/voice_broken.ogg?playable=1")
            assert r1.status_code == 200
            assert len(r1.content) > 0, "Returned empty 0-byte 200 OK for ?playable=1!"
            assert r1.content == raw_ogg

            r2 = client.get(f"/api/dialogs/{dlg_id}/messages/901/media?playable=1")
            assert r2.status_code == 200
            assert r2.content == raw_ogg
    finally:
        conn.close()


def test_folder_single_sync_and_patch_require_admin_rbac():
    """P1 seam repro: sales/operator could bypass /api/folders/sync-all RBAC via /api/folders/{id}/sync or PATCH."""
    from app.security import _requires_admin_role

    assert _requires_admin_role("/api/folders/sync-all", "POST") is True
    assert _requires_admin_role("/api/folders/7/sync", "POST") is True
    assert _requires_admin_role("/api/folders/7", "PATCH") is True
    assert _requires_admin_role("/api/folders/pull", "POST") is True


def test_cloud_backup_endpoint_backs_up_actual_isolated_db_not_default_db(tmp_path, monkeypatch):
    """P1 seam repro: POST /api/cloud/backup runs in worker thread (in_bg_thread=True) and backed up DEFAULT_DB
    instead of the custom db_file attached to app.state.conn."""
    import base64 as _b64
    import gzip as _gzip
    import json as _json
    import sqlite3 as _sq
    import app.cloud_persistence as cp

    custom_db = tmp_path / "custom_tenant.db"
    conn = connect(custom_db)
    init_db(conn)
    now = utc_now()
    for k, v in (
        ("r2_account_id", "acc_test"),
        ("r2_access_key", "key_test"),
        ("r2_secret_key", "sec_test"),
        ("r2_bucket", "bucket_test"),
        ("unique_marker_in_custom_db", "PRESENT_42"),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (k, v, now),
        )
    conn.commit()

    captured_payloads = []

    class _FakeResp:
        status_code = 200
        text = "OK"

    class _FakeHttpxClient:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def put(self, url, content=b"", headers=None):
            captured_payloads.append(bytes(content))
            return _FakeResp()

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("CRMTG_EPHEMERAL_RUNTIME", raising=False)
    for env_k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY", "R2_SECRET_KEY", "R2_BUCKET", "CLOUD_BACKUP_ENABLED", "BOT_TOKEN"):
        monkeypatch.delenv(env_k, raising=False)
    monkeypatch.setattr(cp, "_read_dotenv", lambda: {})
    monkeypatch.setattr(cp.httpx, "Client", _FakeHttpxClient)
    monkeypatch.setattr(cp, "DEFAULT_DB", tmp_path / "empty_default.db")

    app = create_app(conn=conn, gateway=FakeGateway())
    try:
        with TestClient(app) as client:
            resp = client.post("/api/cloud/backup")
            assert resp.status_code == 200
            assert resp.json().get("ok") is True, f"Backup failed instead of reading custom DB: {resp.json()}"
            assert captured_payloads, "Expected R2 PUT upload"
            bundle = _json.loads(_gzip.decompress(captured_payloads[0]).decode("utf-8"))
            snap_file = tmp_path / "uploaded_snap.db"
            snap_file.write_bytes(_b64.b64decode(bundle["db_b64"]))
            chk = _sq.connect(snap_file)
            try:
                val = chk.execute(
                    "SELECT value FROM kv_settings WHERE key = 'unique_marker_in_custom_db'"
                ).fetchone()
                assert val is not None and val[0] == "PRESENT_42"
            finally:
                chk.close()
    finally:
        conn.close()

