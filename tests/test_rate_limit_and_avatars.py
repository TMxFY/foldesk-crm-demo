import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import inbox as inbox_mod
from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import FakeGateway, TelethonGateway


def _make_secured_crm(tmp_path, monkeypatch, *, gateway=None):
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MCP_API_KEY", "test-mcp-secret-999")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "StrongTimPass#2026")
    monkeypatch.setenv("CRM_VLAD_PASSWORD", "StrongVladPass#2026")
    monkeypatch.setenv("CRM_OPERATOR_PASSWORD", "StrongOpPass#2026")
    monkeypatch.setattr(inbox_mod, "INBOX_MEDIA_DIR", tmp_path / "inbox_media")

    conn = connect(tmp_path / "crm_rate.db")
    init_db(conn)
    acc_id = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, provider)
        VALUES ('+15551234567', 'data/sessions/demo_acc.session', 'connected', '2026-01-01T00:00:00', 'telegram')
        """
    ).lastrowid
    for i in range(1, 305):
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer, muted,
                created_at, updated_at
            ) VALUES (?, 'user', ?, ?, ?, 0, 0, '2026-01-01T00:00:00', 'hi', 1, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
            """,
            (acc_id, 910000 + i, f"lead_{i}", f"Lead {i}"),
        )
    conn.commit()
    app = create_app(conn=conn, gateway=gateway)
    return app, conn


def test_authenticated_300_avatars_plus_crm_api_never_429_while_unauth_flood_rate_limited(
    tmp_path, monkeypatch
):
    app, _conn = _make_secured_crm(tmp_path, monkeypatch)
    proxy_ip = "198.51.100.42"
    base_headers = {
        "X-Forwarded-For": proxy_ip,
        "User-Agent": "Mozilla/5.0 Chrome/125.0",
    }

    authed_client = TestClient(app, follow_redirects=False)
    r_login = authed_client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "StrongTimPass#2026"},
        headers=base_headers,
    )
    assert r_login.status_code == 200
    assert authed_client.cookies.get("foldesk_auth")

    # 1. Fetch 300 dialog avatars via proxied session cookie (simulating Inbox/Sheet render)
    for did in range(1, 301):
        r_av = authed_client.get(f"/api/dialogs/{did}/avatar", headers=base_headers)
        assert r_av.status_code == 200, f"Avatar #{did} failed with {r_av.status_code}: {r_av.text}"

    # 2. Immediately call core CRM APIs from the same proxied IP -> MUST NOT hit 429
    for method, path in (
        ("GET", "/api/leads"),
        ("GET", "/api/dialogs/1/messages"),
        ("POST", "/api/inbox/dialogs/1/read"),
        ("GET", "/api/analytics/summary"),
        ("GET", "/api/snippets"),
        ("GET", "/api/folders?account_id=1"),
    ):
        resp = authed_client.request(method, path, headers=base_headers)
        assert resp.status_code == 200, f"{method} {path} returned {resp.status_code}: {resp.text}"

    # Verify the 300 avatar loads did NOT consume the authenticated API rate-limit bucket
    auth_buckets = getattr(app.state, "_auth_api_rate_buckets", {})
    assert sum(len(v) for v in auth_buckets.values()) == 6

    # 3. Unauthenticated flood without valid session cookie MUST STILL be blocked & rate-limited with Retry-After
    unauth_client = TestClient(app, follow_redirects=False)
    flood_headers = {
        "X-Forwarded-For": "203.0.113.99",
        "User-Agent": "Mozilla/5.0 Chrome/125.0",
    }
    statuses = []
    retry_after_vals = []
    for i in range(300):
        r_unauth = unauth_client.get(
            "/api/leads" if i % 2 == 0 else "/api/dialogs/1/avatar",
            headers=flood_headers,
        )
        statuses.append(r_unauth.status_code)
        if r_unauth.status_code == 429:
            retry_after_vals.append(r_unauth.headers.get("retry-after"))

    assert 401 in statuses
    assert 429 in statuses, f"Expected 429 on unauthenticated flood, got {set(statuses)}"
    assert all(val is not None and val.isdigit() and int(val) >= 1 for val in retry_after_vals), (
        f"Expected numeric Retry-After >= 1 on 429 responses, got {retry_after_vals[:5]}"
    )


def test_avatar_preserves_cache_control_while_json_api_stays_no_store(tmp_path, monkeypatch):
    app, _conn = _make_secured_crm(tmp_path, monkeypatch)
    proxy_headers = {
        "X-Forwarded-For": "198.51.100.77",
        "User-Agent": "Mozilla/5.0 Chrome/125.0",
    }
    client = TestClient(app, follow_redirects=False)
    r_login = client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "StrongTimPass#2026"},
        headers=proxy_headers,
    )
    assert r_login.status_code == 200

    r_avatar = client.get("/api/dialogs/1/avatar", headers=proxy_headers)
    assert r_avatar.status_code == 200
    cc_avatar = (r_avatar.headers.get("cache-control") or "").lower()
    assert "no-store" not in cc_avatar, f"Avatar Cache-Control must not be no-store, got {cc_avatar!r}"
    assert "max-age=" in cc_avatar, f"Avatar Cache-Control must preserve max-age, got {cc_avatar!r}"

    for api_path in ("/api/leads", "/api/analytics/summary"):
        r_json = client.get(api_path, headers=proxy_headers)
        assert r_json.status_code == 200
        assert (r_json.headers.get("cache-control") or "").lower() == "no-store"

    # Unauthenticated 401 on avatar must still remain non-cacheable
    unauth = TestClient(app, follow_redirects=False)
    r_unauth_av = unauth.get("/api/dialogs/1/avatar", headers=proxy_headers)
    assert r_unauth_av.status_code == 401
    assert (r_unauth_av.headers.get("cache-control") or "").lower() == "no-store"


class _SlowAvatarGateway(FakeGateway):
    def __init__(self, delay: float = 0.6, *, succeed: bool = True, fail_exc: Exception | None = None):
        super().__init__()
        self.delay = delay
        self.succeed = succeed
        self.fail_exc = fail_exc
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def download_profile_photo(
        self, session_path: str, peer_id: int | str, dest_path: str
    ) -> str | None:
        self.calls += 1
        self.active += 1
        if self.active > self.max_active:
            self.max_active = self.active
        try:
            await asyncio.sleep(self.delay)
            if self.fail_exc is not None:
                raise self.fail_exc
            if not self.succeed:
                return None
            p = Path(dest_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\xff\xd8\xff\xe0" + b"J" * 128)
            return str(p)
        finally:
            self.active -= 1


def test_50_concurrent_uncached_avatars_bound_rpc_concurrency_and_fail_open_fast(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(inbox_mod, "INBOX_MEDIA_DIR", tmp_path / "inbox_media")
    conn = connect(tmp_path / "avatars_burst.db")
    init_db(conn)
    acc_id = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, provider)
        VALUES ('+15559990000', 'data/sessions/real_burst.session', 'connected', '2026-01-01T00:00:00', 'telegram')
        """
    ).lastrowid
    dialog_ids = []
    for i in range(1, 51):
        did = conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer, muted,
                created_at, updated_at
            ) VALUES (?, 'user', ?, ?, ?, 0, 0, '2026-01-01T00:00:00', 'hi', 1, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
            """,
            (acc_id, 5000000 + i, f"burst_{i}", f"Burst User {i}"),
        ).lastrowid
        dialog_ids.append(did)
    conn.commit()

    slow_gw = _SlowAvatarGateway(delay=0.6, succeed=True)

    async def _run():
        started = time.monotonic()
        results = await asyncio.gather(
            *(inbox_mod.get_dialog_avatar(conn, did, gateway=slow_gw) for did in dialog_ids)
        )
        elapsed = time.monotonic() - started
        return results, elapsed

    results, elapsed = asyncio.run(_run())
    assert len(results) == 50
    for body, mime in results:
        assert mime in ("image/jpeg", "image/svg+xml")
        assert len(body) > 20
    # Must NOT launch 50 concurrent Telethon RPCs and must NOT serialize 50 * 0.6s = 30s
    assert slow_gw.max_active <= 3, f"Expected bounded concurrent TG RPCs <= 3, got {slow_gw.max_active}"
    assert elapsed < 3.0, f"Burst of 50 avatars took too long ({elapsed:.2f}s); expected fast fail-open"


def test_telethon_gateway_avatar_does_not_starve_send_message_or_evict_healthy_client(
    tmp_path,
):
    class _MockClient:
        def __init__(self):
            self.connected = True
            self.active_dl = 0
            self.max_active_dl = 0
            self.dl_calls = 0

        def is_connected(self) -> bool:
            return self.connected

        async def disconnect(self) -> None:
            self.connected = False

        async def get_entity(self, target):
            return {"id": target}

        async def download_profile_photo(self, entity, file=None, **_kwargs):
            self.dl_calls += 1
            self.active_dl += 1
            self.max_active_dl = max(self.max_active_dl, self.active_dl)
            try:
                await asyncio.sleep(0.5)
                return None
            finally:
                self.active_dl -= 1

        async def send_message(self, target, body, **_kwargs):
            return type("Msg", (), {"id": 777, "message": body, "date": None})()

    async def _run():
        gw = TelethonGateway()
        gw._rpc_timeout = 1.0
        gw._avatar_timeout = 0.2
        gw._avatar_lock_timeout = 0.05
        sess_path = "data/sessions/acc_avatar_test.session"
        client = _MockClient()
        gw._clients[sess_path] = client

        started = time.monotonic()
        avatar_tasks = [
            asyncio.create_task(
                gw.download_profile_photo(sess_path, 3000000 + i, str(tmp_path / f"av_{i}.jpg"))
            )
            for i in range(50)
        ]
        await asyncio.sleep(0.01)
        send_started = time.monotonic()
        sent = await asyncio.wait_for(
            gw.send_message(sess_path, 999111, "Urgent reply"),
            timeout=0.6,
        )
        send_elapsed = time.monotonic() - send_started
        avatar_res = await asyncio.gather(*avatar_tasks)
        total_elapsed = time.monotonic() - started
        return client, sent, send_elapsed, avatar_res, total_elapsed, gw

    client, sent, send_elapsed, avatar_res, total_elapsed, gw = asyncio.run(_run())
    assert sent["telegram_message_id"] == 777
    assert send_elapsed < 0.5
    assert total_elapsed < 1.5
    assert all(r is None for r in avatar_res)
    assert client.max_active_dl <= 2
    assert client.dl_calls <= 6
    # Healthy connected client must remain cached and connected even when slow avatar timed out
    assert gw._clients.get("data/sessions/acc_avatar_test.session") is client
    assert client.is_connected()


def test_failed_or_timed_out_avatar_uses_short_cooldown_without_permanent_miss_marker(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(inbox_mod, "INBOX_MEDIA_DIR", tmp_path / "inbox_media")
    conn = connect(tmp_path / "avatars_cooldown.db")
    init_db(conn)
    acc_id = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, provider)
        VALUES ('+15558880000', 'data/sessions/real_cd.session', 'connected', '2026-01-01T00:00:00', 'telegram')
        """
    ).lastrowid
    did_err = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer, muted,
            created_at, updated_at
        ) VALUES (?, 'user', 6000001, 'err_user', 'Error User', 0, 0, '2026-01-01T00:00:00', 'hi', 1, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (acc_id,),
    ).lastrowid
    did_empty = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer, muted,
            created_at, updated_at
        ) VALUES (?, 'user', 6000002, 'empty_user', 'Empty User', 0, 0, '2026-01-01T00:00:00', 'hi', 1, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (acc_id,),
    ).lastrowid
    conn.commit()

    failing_gw = _SlowAvatarGateway(delay=0.0, fail_exc=RuntimeError("FloodWait or network glitch"))
    empty_gw = _SlowAvatarGateway(delay=0.0, succeed=False)

    async def _run():
        # 1. Transient failure -> SVG fallback, NO permanent .none marker, cooldown blocks repeat RPC
        b1, m1 = await inbox_mod.get_dialog_avatar(conn, did_err, gateway=failing_gw)
        assert m1 == "image/svg+xml"
        assert b"<svg" in b1
        assert failing_gw.calls == 1
        assert not (tmp_path / "inbox_media" / "avatars" / f"dialog_{did_err}.none").exists()

        # Immediate retry within cooldown does NOT hammer TG again
        b2, m2 = await inbox_mod.get_dialog_avatar(conn, did_err, gateway=failing_gw)
        assert m2 == "image/svg+xml"
        assert failing_gw.calls == 1

        # Once cooldown expires and TG recovers, genuine photo loads progressively
        now_base = time.monotonic()
        monkeypatch.setattr(inbox_mod.time, "monotonic", lambda: now_base + 120.0)
        failing_gw.fail_exc = None
        failing_gw.succeed = True
        b_ok, m_ok = await inbox_mod.get_dialog_avatar(conn, did_err, gateway=failing_gw)
        assert m_ok == "image/jpeg"
        assert b_ok.startswith(b"\xff\xd8\xff\xe0")
        assert failing_gw.calls == 2

        # 2. Genuine empty photo (clean None return) writes .none marker and stops repeat RPCs
        b3, m3 = await inbox_mod.get_dialog_avatar(conn, did_empty, gateway=empty_gw)
        assert m3 == "image/svg+xml"
        assert empty_gw.calls == 1
        assert (tmp_path / "inbox_media" / "avatars" / f"dialog_{did_empty}.none").exists()
        await inbox_mod.get_dialog_avatar(conn, did_empty, gateway=empty_gw)
        assert empty_gw.calls == 1

    asyncio.run(_run())


def test_same_dialog_concurrent_requests_coalesce_to_single_rpc(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_mod, "INBOX_MEDIA_DIR", tmp_path / "inbox_media")
    conn = connect(tmp_path / "avatars_dedup.db")
    init_db(conn)
    acc_id = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, provider)
        VALUES ('+15557770000', 'data/sessions/real_dedup.session', 'connected', '2026-01-01T00:00:00', 'telegram')
        """
    ).lastrowid
    did = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer, muted,
            created_at, updated_at
        ) VALUES (?, 'user', 7000001, 'dedup_user', 'Dedup User', 0, 0, '2026-01-01T00:00:00', 'hi', 1, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (acc_id,),
    ).lastrowid
    conn.commit()

    fast_gw = _SlowAvatarGateway(delay=0.05, succeed=True)

    async def _run():
        return await asyncio.gather(
            *(inbox_mod.get_dialog_avatar(conn, did, gateway=fast_gw) for _ in range(10))
        )

    results = asyncio.run(_run())
    assert fast_gw.calls == 1
    assert all(m == "image/jpeg" for _, m in results)


def test_telethon_gateway_warms_dialogs_once_on_missing_input_entity_and_reuses_client(
    tmp_path,
):
    class _ColdSessionClient:
        def __init__(self):
            self.connected = True
            self.warmed = False
            self.get_dialogs_calls = 0

        def is_connected(self) -> bool:
            return self.connected

        async def get_entity(self, target):
            if isinstance(target, int) and not self.warmed:
                raise ValueError("Could not find the input entity for PeerUser")
            return {"id": target}

        async def get_dialogs(self, limit=150):
            self.get_dialogs_calls += 1
            self.warmed = True
            return []

        async def download_profile_photo(self, entity, file=None, download_big=False):
            assert download_big is False
            p = Path(str(file))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\xff\xd8\xff\xe0" + b"K" * 128)
            return str(p)

    async def _run():
        gw = TelethonGateway()
        sess_path = "data/sessions/acc_cold_entity.session"
        client = _ColdSessionClient()
        gw._clients[sess_path] = client

        dest = tmp_path / "cold_av.jpg"
        saved = await gw.download_profile_photo(sess_path, 8880001, str(dest))
        return gw, client, saved

    gw, client, saved = asyncio.run(_run())
    assert saved is not None
    assert client.get_dialogs_calls == 1
    assert "data/sessions/acc_cold_entity.session" in gw._warmed_dialogs
    assert gw._clients.get("data/sessions/acc_cold_entity.session") is client


def test_bogus_token_flood_and_sse_exemption(tmp_path, monkeypatch):
    from app.security import _is_rate_limit_exempt_for_authed

    assert _is_rate_limit_exempt_for_authed("/api/inbox/events", "GET") is True
    assert _is_rate_limit_exempt_for_authed("/api/dialogs/42/avatar", "GET") is True
    assert _is_rate_limit_exempt_for_authed("/api/leads", "GET") is False

    app, _conn = _make_secured_crm(tmp_path, monkeypatch)
    unauth = TestClient(app, follow_redirects=False)
    headers = {
        "X-Forwarded-For": "198.51.100.200",
        "User-Agent": "Mozilla/5.0 Chrome/125.0",
        "Cookie": "foldesk_auth=bogus.invalid.token.sig",
    }
    statuses = []
    retry_afters = []
    for _ in range(260):
        r = unauth.get("/api/leads", headers=headers)
        statuses.append(r.status_code)
        if r.status_code == 429:
            retry_afters.append(r.headers.get("retry-after"))
    assert 401 in statuses
    assert 429 in statuses
    assert retry_afters and all(v is not None and v.isdigit() and int(v) >= 1 for v in retry_afters)


def test_importing_app_main_has_no_eager_create_app_side_effects(tmp_path):
    worktree_root = str(Path(__file__).resolve().parents[1])
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "PYTEST_CURRENT_TEST"
    }
    env["PYTHONPATH"] = worktree_root
    env["DISABLE_TG_WATCHES"] = "1"
    tmp_db = tmp_path / "eager_check.db"
    code = (
        f"from pathlib import Path\n"
        f"import app.db as _db\n"
        f"_db.DEFAULT_DB = Path({repr(str(tmp_db))})\n"
        "import app.main as m\n"
        "assert 'app' not in vars(m), 'app.main created app eagerly on import'\n"
        "assert callable(m.create_app)\n"
        "sentinel = object()\n"
        "calls = []\n"
        "m.load_env_file = lambda *a, **kw: calls.append('env')\n"
        "m.create_app = lambda *a, **kw: (calls.append('create'), sentinel)[1]\n"
        "assert m.app is sentinel\n"
        "assert m.app is sentinel\n"
        "assert calls == ['env', 'create']\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert not tmp_db.exists()


def test_avatar_atomic_temp_write_and_cache_control_tiers(tmp_path, monkeypatch):
    class _FlakyAtomicGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.mode = "corrupt_fail"
            self.seen_targets: list[int | str] = []

        async def download_profile_photo(self, session_path: str, target: int | str, dest_path: str):
            self.seen_targets.append(target)
            if self.mode == "corrupt_fail":
                # Simulate >64B partial write before network failure
                Path(dest_path).write_bytes(b"CORRUPT_PARTIAL_" * 20)
                raise RuntimeError("network reset mid-download")
            if self.mode == "empty":
                return None
            Path(dest_path).write_bytes(b"\xff\xd8\xff\xe0VALID_JPEG_" * 12)
            return dest_path

    gw = _FlakyAtomicGateway()
    app, conn = _make_secured_crm(tmp_path, monkeypatch, gateway=gw)
    acc_real = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, provider)
        VALUES ('+15559991111', 'data/sessions/real_atomic.session', 'connected', '2026-01-01T00:00:00', 'telegram')
        """
    ).lastrowid
    did_ok = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer, muted,
            created_at, updated_at
        ) VALUES (?, 'user', 801001, 'atomic_ok', 'Atomic OK', 0, 0, '2026-01-01T00:00:00', 'hi', 1, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (acc_real,),
    ).lastrowid
    did_empty = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer, muted,
            created_at, updated_at
        ) VALUES (?, 'user', 801002, 'atomic_empty', 'Atomic Empty', 0, 0, '2026-01-01T00:00:00', 'hi', 1, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (acc_real,),
    ).lastrowid
    did_handle = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer, muted,
            created_at, updated_at
        ) VALUES (?, 'user', 'alice_handle', 'alice_handle', 'Alice Handle', 0, 0, '2026-01-01T00:00:00', 'hi', 1, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (acc_real,),
    ).lastrowid
    conn.commit()

    proxy_headers = {"X-Forwarded-For": "198.51.100.88", "User-Agent": "Mozilla/5.0 Chrome/125.0"}
    client = TestClient(app, follow_redirects=False)
    assert client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "StrongTimPass#2026"},
        headers=proxy_headers,
    ).status_code == 200

    avatars_dir = tmp_path / "inbox_media" / "avatars"

    # 1. Transient failure with >64B partial write must not poison cached jpg or leave temp files
    r_fail = client.get(f"/api/dialogs/{did_ok}/avatar", headers=proxy_headers)
    assert r_fail.status_code == 200
    assert "image/svg+xml" in r_fail.headers.get("content-type", "")
    assert r_fail.headers.get("cache-control", "").lower() == "private, no-store"
    assert not (avatars_dir / f"dialog_{did_ok}.jpg").exists()
    assert not list(avatars_dir.glob(".tmp_*"))

    # 2. Next successful fetch atomically replaces jpg & caches privately for 1h
    inbox_mod._AVATAR_ERROR_COOLDOWN_UNTIL.clear()
    gw.mode = "ok"
    r_ok = client.get(f"/api/dialogs/{did_ok}/avatar", headers=proxy_headers)
    assert r_ok.status_code == 200
    assert "image/jpeg" in r_ok.headers.get("content-type", "")
    assert r_ok.headers.get("cache-control", "").lower() == "private, max-age=3600"
    assert r_ok.content == b"\xff\xd8\xff\xe0VALID_JPEG_" * 12
    assert not list(avatars_dir.glob(".tmp_*"))

    # 3. Confirmed empty photo & demo fallback are cached privately for 300s (never public)
    gw.mode = "empty"
    r_empty = client.get(f"/api/dialogs/{did_empty}/avatar", headers=proxy_headers)
    assert r_empty.status_code == 200
    assert "image/svg+xml" in r_empty.headers.get("content-type", "")
    assert r_empty.headers.get("cache-control", "").lower() == "private, max-age=300"

    r_demo = client.get("/api/dialogs/1/avatar", headers=proxy_headers)
    assert r_demo.status_code == 200
    assert r_demo.headers.get("cache-control", "").lower() == "private, max-age=300"

    # 4. Non-numeric peer_id must not crash with ValueError
    gw.mode = "empty"
    r_handle = client.get(f"/api/dialogs/{did_handle}/avatar", headers=proxy_headers)
    assert r_handle.status_code == 200
    assert "alice_handle" in gw.seen_targets


def test_telethon_gateway_open_cancellation_disconnects_and_drops_zombie_client(
    monkeypatch,
):
    created_clients: list = []
    client_created_hook: list = []

    class _SessionStub:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _HangingClient:
        def __init__(self, *_args, **_kwargs):
            self.connected = False
            self.connect_started = asyncio.Event()
            self.disconnect_calls = 0
            self.session = _SessionStub()
            created_clients.append(self)
            if client_created_hook:
                client_created_hook[0].set()

        def is_connected(self) -> bool:
            return self.connected

        async def connect(self) -> None:
            self.connected = True
            self.connect_started.set()
            await asyncio.Event().wait()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            self.connected = False

    import telethon

    monkeypatch.setattr(telethon, "TelegramClient", _HangingClient)

    async def _run():
        gw = TelethonGateway()
        monkeypatch.setattr(gw, "_credentials", lambda _s: (12345, "0123456789abcdef"))

        # Case 1: New client creation cancelled mid-connect
        created_evt = asyncio.Event()
        client_created_hook.append(created_evt)
        t1 = asyncio.create_task(gw._open("data/sessions/cancel_new.session"))
        await asyncio.wait_for(created_evt.wait(), timeout=1.0)
        c1 = created_clients[-1]
        await asyncio.wait_for(c1.connect_started.wait(), timeout=1.0)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        assert c1.disconnect_calls == 1
        assert c1.session.closed is True
        assert not c1.is_connected()
        assert "data/sessions/cancel_new.session" not in gw._clients

        # Case 2: Disconnected cached client reconnect cancelled mid-connect
        cached_client = _HangingClient()
        gw._clients["data/sessions/cancel_cached.session"] = cached_client
        t2 = asyncio.create_task(gw._open("data/sessions/cancel_cached.session"))
        await asyncio.wait_for(cached_client.connect_started.wait(), timeout=1.0)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        assert cached_client.disconnect_calls == 1
        assert cached_client.session.closed is True
        assert not cached_client.is_connected()
        assert "data/sessions/cancel_cached.session" not in gw._clients

    asyncio.run(_run())


def test_file_db_wal_cloud_backup_toggle_and_auth_conn_shutdown(tmp_path, monkeypatch):
    from app import cloud_persistence

    conn = connect(tmp_path / "wal_verify.db")
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        assert mode == "wal"
    finally:
        conn.close()

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "live-token")
    monkeypatch.setenv("R2_ACCOUNT_ID", "acc")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sec")
    monkeypatch.setenv("R2_BUCKET", "bkt")
    monkeypatch.setenv("CLOUD_BACKUP_ENABLED", "0")
    assert cloud_persistence._resolve_r2_config(None) is None

    app, _ = _make_secured_crm(tmp_path, monkeypatch)
    proxy_headers = {"X-Forwarded-For": "198.51.100.99", "User-Agent": "Mozilla/5.0 Chrome/125.0"}
    with TestClient(app, follow_redirects=False) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "tim", "password": "StrongTimPass#2026"},
            headers=proxy_headers,
        ).status_code == 200
        assert client.get("/api/leads", headers=proxy_headers).status_code == 200
        assert getattr(app.state, "_auth_conn", None) is not None
    assert getattr(app.state, "_auth_conn", None) is None


