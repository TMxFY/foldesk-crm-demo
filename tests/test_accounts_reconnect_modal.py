from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient

from app.accounts import (
    _apply_result,
    deduplicate_accounts,
    start_login,
    update_account_settings,
)
from app.crm_auth import register_crm_user
from app.db import ROOT, connect, init_db, utc_now, utc_today
from app.main import create_app
from app.telegram_gateway import FakeGateway, GatewayError
from app.whatsapp_gateway import WhatsAppGateway
from real_session_helpers import make_protocol_wa_client_factory, make_real_telethon_string_session


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _resolve_playwright_node_path() -> str:
    candidates = [ROOT / "e2e" / "node_modules"]
    try:
        common = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            text=True,
        ).strip()
        if common:
            main_root = (ROOT / common).resolve().parent
            candidates.append(main_root / "e2e" / "node_modules")
    except Exception:
        pass
    parts = [str(p) for p in candidates if p.is_dir()]
    if os.environ.get("NODE_PATH"):
        parts.append(os.environ["NODE_PATH"])
    return os.pathsep.join(parts)


class _PerPhoneProxyRecordingGateway(FakeGateway):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_proxy_calls: list[tuple[str, str | None]] = []
        self.proxies: dict[str, str | None] = {}

    def set_proxy(self, session_path: str, proxy_url: str | None) -> None:
        self.proxies[session_path] = proxy_url
        self.set_proxy_calls.append((session_path, proxy_url))

    async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
        res = await super().confirm_code(phone, code, phone_code_hash, session_path)
        if res.get("status") == "connected":
            uid_map = {"+79959022911": 8002, "+79003330003": 8003}
            uid = uid_map.get(phone, 8099)
            res = {
                **res,
                "telegram_user_id": uid,
                "telegram_username": f"user_{uid}",
                "telegram_name": f"User {uid}",
            }
        return res


def test_reauth_in_place_updates_proxy_to_isp_and_auto_and_rejects_active_duplicate(tmp_path: Path):
    db_path = tmp_path / "reauth_api.db"
    conn = connect(db_path)
    init_db(conn)
    now = utc_now()
    today = utc_today()

    # 1. Healthy connected account (must reject duplicate /start)
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, telegram_user_id, telegram_username, telegram_name,
            created_at, health, updated_at, daily_limit, sent_today, sent_date, provider, proxy_url
        ) VALUES (1, '+79001110001', 'data/sessions/1.session', 'connected', 8001, 'active_one', 'Active One',
                  ?, 'active', ?, 20, 0, ?, 'telegram', 'auto')
        """,
        (now, now, today),
    )
    # 2. Unauthorized account (must allow in-place reauth keeping id=2 and updating proxy_url)
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, telegram_user_id, telegram_username, telegram_name,
            created_at, health, last_error, updated_at, daily_limit, sent_today, sent_date, provider, proxy_url
        ) VALUES (2, '+79959022911', 'data/sessions/2.session', 'connected', 8002, 'make1tlo', 'Sergey Glebov',
                  ?, 'unauthorized', 'AuthKeyDuplicatedError', ?, 20, 5, ?, 'telegram', 'auto')
        """,
        (now, now, today),
    )
    # 3. Offline account with custom proxy (must allow switching proxy back to 'auto' and call gw.set_proxy)
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, telegram_user_id, telegram_username, telegram_name,
            created_at, health, last_error, updated_at, daily_limit, sent_today, sent_date, provider, proxy_url
        ) VALUES (3, '+79003330003', 'data/sessions/3.session', 'connected', 8003, 'offline_acc', 'Offline Acc',
                  ?, 'offline', 'Нет связи', ?, 20, 0, ?, 'telegram', 'socks5://old:pw@1.1.1.1:1080')
        """,
        (now, now, today),
    )
    conn.commit()

    gw = _PerPhoneProxyRecordingGateway(code="11111")
    app = create_app(conn=conn, gateway=gw)
    with TestClient(app) as client:
        dup = client.post("/api/accounts/start", json={"phone": "+79001110001"})
        assert dup.status_code == 400
        assert "уже добавлен" in dup.json()["detail"].lower()

        st2 = client.post(
            "/api/accounts/start",
            json={
                "phone": "+79959022911",
                "proxy_url": "socks5://isp:pass@1.2.3.4:1080",
                "access_scope": "personal",
            },
        )
        assert st2.status_code == 200, st2.text
        assert st2.json()["id"] == 2
        assert st2.json()["status"] == "pending_code"
        assert st2.json()["proxy_url"] == "socks5://isp:pass@1.2.3.4:1080"
        assert ("data/sessions/2.session", "socks5://isp:pass@1.2.3.4:1080") in gw.set_proxy_calls

        cf2 = client.post("/api/accounts/2/code", json={"code": "11111"})
        assert cf2.status_code == 200, cf2.text
        assert cf2.json()["status"] == "connected"

        row2 = conn.execute(
            "SELECT id, status, health, last_error, proxy_url, access_scope FROM accounts WHERE id = 2"
        ).fetchone()
        assert row2["id"] == 2
        assert row2["status"] == "connected"
        assert row2["health"] == "active"
        assert row2["last_error"] is None
        assert row2["proxy_url"] == "socks5://isp:pass@1.2.3.4:1080"
        assert row2["access_scope"] == "personal"

        # Reset offline account #3 back to "auto"
        st3 = client.post(
            "/api/accounts/start",
            json={"phone": "+79003330003", "proxy_url": "auto"},
        )
        assert st3.status_code == 200, st3.text
        assert st3.json()["id"] == 3
        assert ("data/sessions/3.session", "auto") in gw.set_proxy_calls
        cf3 = client.post("/api/accounts/3/code", json={"code": "11111"})
        assert cf3.status_code == 200
        row3 = conn.execute("SELECT status, health, proxy_url FROM accounts WHERE id = 3").fetchone()
        assert row3["status"] == "connected"
        assert row3["health"] == "active"
        assert row3["proxy_url"] == "auto"


def test_retry_after_pending_and_error_preserves_account_id_and_dialogs(tmp_path: Path):
    conn = connect(tmp_path / "retry_preserve.db")
    init_db(conn)
    now = utc_now()

    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, telegram_user_id, telegram_username, telegram_name,
            created_at, health, last_error, updated_at, provider
        ) VALUES (12, '+79004440012', 'data/sessions/12.session', 'connected', 8012, 'retry_user', 'Retry User',
                  ?, 'unauthorized', 'Session revoked', ?, 'telegram')
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO dialogs (
            id, account_id, peer_type, peer_id, username, title, provider, created_at, updated_at
        ) VALUES (501, 12, 'user', 9901, 'peer_501', 'Peer 501', 'telegram', ?, ?)
        """,
        (now, now),
    )
    conn.commit()

    class _FlakyRequestGw(FakeGateway):
        def __init__(self):
            super().__init__(code="11111")
            self.calls = 0

        async def request_code(self, phone: str, session_path: str) -> str:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("Transient RPC timeout")
            return await super().request_code(phone, session_path)

    gw = _FlakyRequestGw()

    # 1st attempt -> pending_code (keeps id=12)
    r1 = asyncio.run(start_login(conn, "+79004440012", gw, proxy_url="auto"))
    assert r1["id"] == 12
    assert r1["status"] == "pending_code"

    # 2nd attempt while pending_code -> request_code fails -> row id=12 marked error, NOT deleted
    with pytest.raises(GatewayError, match="Transient RPC timeout"):
        asyncio.run(start_login(conn, "+79004440012", gw, proxy_url="eu_residential"))
    row_err = conn.execute("SELECT id, status FROM accounts WHERE id = 12").fetchone()
    assert row_err is not None and row_err["status"] == "error"

    # 3rd attempt after error -> STILL preserves id=12 and dialog ownership!
    r3 = asyncio.run(start_login(conn, "+79004440012", gw, proxy_url="eu_residential"))
    assert r3["id"] == 12
    assert r3["status"] == "pending_code"
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert conn.execute("SELECT account_id FROM dialogs WHERE id = 501").fetchone()["account_id"] == 12


def test_failed_request_code_on_offline_account_preserves_session_and_restores_disk_file(tmp_path: Path, monkeypatch):
    import app.accounts as acc_mod

    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        acc_mod,
        "resolve_session",
        lambda p: sess_dir if str(p).rstrip("/").endswith("data/sessions") else sess_dir / Path(str(p)).name,
    )

    orig_sess = sess_dir / "9.session"
    with sqlite3.connect(orig_sess) as sc:
        sc.execute("CREATE TABLE sessions (dc_id INTEGER, auth_key BLOB)")
        sc.execute("INSERT INTO sessions VALUES (2, ?)", (b"Z" * 256,))
        sc.commit()
    with open(orig_sess, "ab") as f:
        f.write(b"\x00" * 4096)
    orig_bytes = orig_sess.read_bytes()
    real_str = make_real_telethon_string_session()

    conn = connect(tmp_path / "offline_rollback.db")
    init_db(conn)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, telegram_user_id, telegram_username,
            health, last_error, proxy_url, session_blob, session_string, created_at, updated_at, provider
        ) VALUES (9, '+79009990009', 'data/sessions/9.session', 'connected', 8009, 'off_acc',
                  'offline', 'Network unreachable', 'socks5://orig:p@1.1.1.1:1080', ?, ?, ?, ?, 'telegram')
        """,
        (orig_bytes, real_str, now, now),
    )
    conn.commit()

    class _FailingTouchGw(FakeGateway):
        async def request_code(self, phone: str, session_path: str) -> str:
            # Simulate Telethon touching the target file path before failing
            Path(acc_mod.resolve_session(session_path)).write_bytes(b"partial-new")
            raise GatewayError("SOCKS5 handshake timed out")

    gw = _FailingTouchGw()
    with pytest.raises(GatewayError, match="SOCKS5 handshake timed out"):
        asyncio.run(start_login(conn, "+79009990009", gw, proxy_url="eu_residential"))

    assert orig_sess.exists()
    assert orig_sess.read_bytes() == orig_bytes
    assert not (sess_dir / "backup_9.session").exists()
    row = conn.execute(
        "SELECT status, health, proxy_url, session_blob, session_string, last_error FROM accounts WHERE id = 9"
    ).fetchone()
    assert row["status"] == "connected"
    assert row["health"] == "offline"
    assert row["proxy_url"] == "socks5://orig:p@1.1.1.1:1080"
    assert row["session_blob"] == orig_bytes
    assert row["session_string"] == real_str
    assert "SOCKS5" in (row["last_error"] or "")


def test_spambot_frozen_with_live_session_blocks_relogin_without_wiping(tmp_path: Path):
    conn = connect(tmp_path / "spambot_guard.db")
    init_db(conn)
    now = utc_now()
    live_str = make_real_telethon_string_session()
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, telegram_user_id, telegram_username,
            health, last_error, session_string, created_at, updated_at, provider
        ) VALUES (32, '+381677672089', 'data/sessions/32.session', 'connected', 600032, 'tim_korznikov',
                  'frozen', '@SpamBot: your account is now limited until 3 Dec 2026', ?, ?, ?, 'telegram')
        """,
        (live_str, now, now),
    )
    conn.commit()

    gw = FakeGateway(code="11111")
    with pytest.raises(ValueError, match="SpamBot"):
        asyncio.run(start_login(conn, "+381677672089", gw, proxy_url="auto"))

    row = conn.execute("SELECT status, health, session_string FROM accounts WHERE id = 32").fetchone()
    assert row["status"] == "connected"
    assert row["health"] == "frozen"
    assert row["session_string"] == live_str


def test_masked_proxy_rejected_and_wa_proxy_validated_and_cleared(tmp_path: Path):
    conn = connect(tmp_path / "masked_wa.db")
    init_db(conn)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, telegram_user_id, health, proxy_url, created_at, updated_at, provider
        ) VALUES (20, '+79002020020', 'data/sessions/20.session', 'connected', 8020, 'unauthorized',
                  'socks5://u:realpass@5.5.5.5:1080', ?, ?, 'telegram')
        """,
        (now, now),
    )
    conn.commit()

    factory, _records = make_protocol_wa_client_factory(
        registered_phones={"+447700900333"},
        pairing_code="WA11-2233",
        pair_on_connect={"jid": "447700900333:1@s.whatsapp.net", "push_name": "WA Account"},
    )
    wa_gw = WhatsAppGateway(client_factory=factory, db_path=tmp_path / "masked_wa.db")
    app = create_app(conn=conn, gateway=FakeGateway(code="11111"), wa_gateway=wa_gw)

    with TestClient(app) as client:
        bad_start = client.post(
            "/api/accounts/start",
            json={"phone": "+79002020020", "proxy_url": "socks5://u:***@5.5.5.5:1080"},
        )
        assert bad_start.status_code == 400
        assert "***" in bad_start.json()["detail"]

        bad_patch = client.patch(
            "/api/accounts/20/settings",
            json={"proxy_url": "socks5://u:***@5.5.5.5:1080"},
        )
        assert bad_patch.status_code == 400
        row20 = conn.execute("SELECT proxy_url FROM accounts WHERE id = 20").fetchone()
        assert row20["proxy_url"] == "socks5://u:realpass@5.5.5.5:1080"

        # Seed a disconnected/offline WA account with a proxy
        wa_conn_st = client.post(
            "/api/accounts/start",
            json={
                "phone": "+447700900333",
                "provider": "whatsapp",
                "auth_mode": "code",
                "proxy_url": "socks5://wauser:wapass@7.7.7.7:1080",
            },
        )
        assert wa_conn_st.status_code == 200, wa_conn_st.text
        wa_id = wa_conn_st.json()["id"]
        assert client.post(f"/api/accounts/{wa_id}/code", json={"code": "WA11-2233"}).status_code == 200
        conn.execute("UPDATE accounts SET health = 'offline' WHERE id = ?", (wa_id,))
        conn.commit()

        # Passing "auto" to WA start MUST fail before touching DB row!
        wa_bad = client.post(
            "/api/accounts/start",
            json={
                "phone": "+447700900333",
                "provider": "whatsapp",
                "auth_mode": "code",
                "proxy_url": "auto",
            },
        )
        assert wa_bad.status_code == 400
        wa_after_bad = conn.execute("SELECT status, health, proxy_url FROM accounts WHERE id = ?", (wa_id,)).fetchone()
        assert wa_after_bad["status"] == "connected"
        assert wa_after_bad["health"] == "offline"
        assert wa_after_bad["proxy_url"] == "socks5://wauser:wapass@7.7.7.7:1080"

        # Explicit proxy_url: null MUST clear saved WA proxy cleanly
        wa_clear = client.post(
            "/api/accounts/start",
            json={
                "phone": "+447700900333",
                "provider": "whatsapp",
                "auth_mode": "code",
                "proxy_url": None,
            },
        )
        assert wa_clear.status_code == 200, wa_clear.text
        assert wa_clear.json()["proxy_url"] is None
        wa_after_clear = conn.execute("SELECT proxy_url FROM accounts WHERE id = ?", (wa_id,)).fetchone()
        assert wa_after_clear["proxy_url"] is None
        asyncio.run(wa_gw.close_all())


def test_apply_result_allows_offline_duplicate_and_dedup_prefers_active(tmp_path: Path):
    conn = connect(tmp_path / "dedup_offline.db")
    init_db(conn)
    now = utc_now()

    # Duplicate phone: id=1 is unauthorized, id=2 is active -> deduplicate must keep id=2!
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, telegram_user_id, health, created_at)
        VALUES (1, '+79007770001', 'data/sessions/1.session', 'connected', 9001, 'unauthorized', ?)
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, telegram_user_id, health, created_at)
        VALUES (2, '+79007770001', 'data/sessions/2.session', 'connected', 9001, 'active', ?)
        """,
        (now,),
    )
    deduped = deduplicate_accounts(conn)
    assert deduped == 1
    kept = conn.execute("SELECT id, health FROM accounts").fetchall()
    assert len(kept) == 1
    assert kept[0]["id"] == 2 and kept[0]["health"] == "active"

    # _apply_result replaces an 'offline' connected duplicate on new login (e.g., WA QR) without raising ValueError
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, telegram_user_id, health, provider, created_at)
        VALUES (10, '+447700900888', 'data/sessions/wa_10.session', 'connected', 447700900888, 'offline', 'whatsapp', ?)
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, provider, created_at)
        VALUES (11, 'wa-qr-pending-abcd', 'data/sessions/wa_11.session', 'pending_code', 'whatsapp', ?)
        """,
        (now,),
    )
    conn.commit()
    _apply_result(
        conn,
        11,
        {
            "status": "connected",
            "phone": "+447700900888",
            "telegram_user_id": 447700900888,
            "telegram_name": "WA New",
            "wa_jid": "447700900888@s.whatsapp.net",
        },
    )
    row11 = conn.execute("SELECT status, phone FROM accounts WHERE id = 11").fetchone()
    assert row11["status"] == "connected" and row11["phone"] == "+447700900888"


def _seed_modal_e2e_db(db_path: Path, operator_password: str) -> None:
    conn = connect(db_path)
    init_db(conn)
    now = utc_now()
    today = utc_today()
    spambot_str = make_real_telethon_string_session()

    # 2 active accounts + 1 unauthorized (@make1tlo) + 1 live @SpamBot frozen (@tim_korznikov) + 1 offline (@offline_op)
    accounts = [
        (1, "+79088819595", "vladwilhelm", "Vladislav Wilhelm", "active", None, 4, "auto", None),
        (2, "+79951210659", "subscale", "Тимофей Орлёнок", "active", None, 3, "auto", None),
        (
            3,
            "+79959022911",
            "make1tlo",
            "Sergey Glebov",
            "unauthorized",
            "The authorization key (session file) was used under two different IP addresses simultaneously",
            5,
            "auto",
            None,
        ),
        (
            4,
            "+381677672089",
            "tim_korznikov",
            "Tim Korz",
            "frozen",
            "@SpamBot: your account is now limited until 3 Dec 2026",
            1,
            "auto",
            spambot_str,
        ),
        (
            5,
            "+79005550005",
            "offline_op",
            "Offline Account",
            "offline",
            "Proxy connect timeout",
            0,
            "socks5://opuser:realpass@5.5.5.5:1080",
            None,
        ),
    ]
    for acc_id, phone, uname, name, health, err, sent, proxy_url, sess_str in accounts:
        conn.execute(
            """
            INSERT INTO accounts (
                id, phone, session_path, status, telegram_user_id, telegram_username, telegram_name,
                created_at, health, last_error, updated_at, daily_limit, sent_today, sent_date,
                provider, proxy_url, session_string
            ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, ?, ?, ?, 20, ?, ?, 'telegram', ?, ?)
            """,
            (
                acc_id,
                phone,
                f"data/sessions/{acc_id}.session",
                600000 + acc_id,
                uname,
                name,
                now,
                health,
                err,
                now,
                sent,
                today,
                proxy_url,
                sess_str,
            ),
        )

    conn.execute(
        """
        INSERT INTO leads (
            id, account_id, telegram_user_id, username, name, company, status, created_at, updated_at, provider
        ) VALUES (101, 1, 720101, 'lead_alpha', 'Алексей Лид', 'Alpha AI', 'talking', ?, ?, 'telegram')
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO dialogs (
            id, account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (1001, 1, 'user', 720101, 'lead_alpha', 'Алексей Лид', 'telegram',
                  0, 1, '2026-11-22T10:00:00', 'Жду детали', 1, 0, 101, ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (1001, 9001, 'in', 'Жду детали', '2026-11-22T10:00:00', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO campaigns (
            id, name, account_id, account_ids_json, status, daily_limit, created_at, updated_at
        ) VALUES (1, 'SoftUGC — OFM (TG)', 1, '[1,2,3]', 'running', 25, ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name, status, current_step,
            assigned_account_id, sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (1, 101, 720101, 'lead_alpha', 'Алексей Лид', 'replied', 1, 3, 3, ?, ?, ?)
        """,
        (now, now, now),
    )
    conn.commit()
    register_crm_user(conn, "op_user", operator_password, "Operator", role="operator")
    conn.close()


def test_accounts_reconnect_modal_e2e_playwright(tmp_path: Path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "accounts_modal_e2e.db"
    admin_password = "ModalPlaywright!2026"
    operator_password = "OperatorPass!2026"
    _seed_modal_e2e_db(db_path, operator_password)

    shot_dir = Path(os.environ.get("PLAYWRIGHT_SCREENSHOT_DIR") or (tmp_path / "shots"))
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot_desktop = shot_dir / "accounts-modal-desktop.png"
    shot_reauth = shot_dir / "accounts-modal-reauth.png"
    shot_mobile = shot_dir / "accounts-modal-mobile.png"
    shot_inbox = shot_dir / "inbox-v2-connect-pill.png"

    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("BOT_TOKEN", None)
    env["PYTHONPATH"] = str(ROOT)
    env["CRM_REQUIRE_AUTH"] = "1"
    env["CRM_ADMIN_PASSWORD"] = admin_password
    env["CRM_VIEW_MODE"] = "sheet"
    env["CLOUD_BACKUP_ENABLED"] = "0"
    env["DISABLE_TG_WATCHES"] = "1"
    env["DEMO_AUTOSEED"] = "0"
    env["BOOTSTRAP_PROD_SNAPSHOT"] = "0"
    env["AIRTABLE_API_KEY"] = ""
    env["AIRTABLE_BASE_ID"] = ""
    env["AIRTABLE_TOKEN"] = ""

    server_code = f"""
import uvicorn
from pathlib import Path
from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import FakeGateway

conn = connect(Path({str(db_path)!r}))
init_db(conn)
gw = FakeGateway(code="11111", password="secret", needs_password=False)
gw.health = "active"
app = create_app(conn=conn, gateway=gw)
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
"""
    server = subprocess.Popen(
        [sys.executable, "-c", server_code],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                with urlopen(f"{base_url}/api/health", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.15)
        assert ready, "Test server did not start in time"

        pw_script = f"""
const fs = require('fs');
const {{ chromium }} = require('playwright');

(async () => {{
  const chromePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const launchOpts = {{ headless: true }};
  if (fs.existsSync(chromePath)) launchOpts.executablePath = chromePath;
  const browser = await chromium.launch(launchOpts);
  const context = await browser.newContext({{ viewport: {{ width: 1440, height: 900 }} }});
  const page = await context.newPage();

  const consoleErrors = [];
  const pageErrors = [];
  const server5xx = [];
  let expect400CodeError = false;
  const attachErrorMonitors = (p, {{ isOperator = false }} = {{}}) => {{
    p.on('pageerror', (err) => pageErrors.push(err.message));
    p.on('console', (msg) => {{
      if (msg.type() !== 'error') return;
      const txt = msg.text() || '';
      if (expect400CodeError && txt.includes('400 (Bad Request)')) return;
      if (isOperator && txt.includes('403 (Forbidden)')) return;
      consoleErrors.push(txt);
    }});
    p.on('response', (res) => {{
      if (res.status() >= 500) server5xx.push(`${{res.status()}} ${{res.url()}}`);
      if (isOperator && res.status() === 403 && res.url().includes('/api/accounts')) {{
        server5xx.push(`Unexpected 403 on accounts API: ${{res.url()}}`);
      }}
    }});
  }};
  attachErrorMonitors(page);

  const loginRes = await page.request.post('{base_url}/api/auth/login', {{
    data: {{ username: 'tim', password: '{admin_password}' }},
  }});
  if (!loginRes.ok()) throw new Error(`Login failed: ${{loginRes.status()}}`);

  await page.goto('{base_url}/', {{ waitUntil: 'domcontentloaded' }});
  await page.locator('#board .lead-card').first().waitFor({{ state: 'visible', timeout: 15000 }});
  await page.waitForTimeout(300);

  // 1. Topbar button visible on Лиды tab and reflects 2/5 active + 3 warn
  const topBtn = page.locator('#open-accounts-modal-btn');
  await topBtn.waitFor({{ state: 'visible' }});
  const topBtnText = (await topBtn.textContent()).trim();
  if (!topBtnText.includes('Аккаунты 2/5') || !topBtnText.includes('3')) {{
    throw new Error(`Unexpected topbar button text: ${{topBtnText}}`);
  }}

  // 2. Click topbar button -> native <dialog id="accounts-modal"> opens with #account-auth-box-title & #account-list
  await topBtn.click();
  await page.locator('#accounts-modal[open]').waitFor({{ state: 'visible' }});
  await page.locator('#account-auth-box').waitFor({{ state: 'visible' }});
  await page.locator('#account-auth-box-title').waitFor({{ state: 'visible' }});
  await page.locator('#account-list .account').first().waitFor({{ state: 'visible' }});

  const reauthBtn3 = page.locator('button[data-reauth-account-id="3"]');
  await reauthBtn3.waitFor({{ state: 'visible' }});
  const reauthBtn5 = page.locator('button[data-reauth-account-id="5"]');
  await reauthBtn5.waitFor({{ state: 'visible' }});
  // Live @SpamBot frozen account #4 must NOT render "Войти заново" in list
  if ((await page.locator('button[data-reauth-account-id="4"]').count()) !== 0) {{
    throw new Error('Unexpected Войти заново button for @SpamBot-frozen account #4');
  }}

  const modalBox = await page.locator('#accounts-modal').evaluate((d) => {{
    const body = d.querySelector('.accounts-modal-body');
    return {{
      open: d.open,
      theme: document.documentElement.getAttribute('data-theme'),
      scrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
      bodyScrollW: body ? body.scrollWidth : 0,
      bodyClientW: body ? body.clientWidth : 0,
      bodyScrollLeft: body ? body.scrollLeft : 0,
    }};
  }});
  if (
    !modalBox.open ||
    modalBox.theme !== 'dark' ||
    modalBox.scrollW > modalBox.innerW ||
    modalBox.bodyScrollW > modalBox.bodyClientW + 2 ||
    modalBox.bodyScrollLeft !== 0
  ) {{
    throw new Error(`Modal layout invalid: ${{JSON.stringify(modalBox)}}`);
  }}
  await page.screenshot({{ path: {str(shot_desktop)!r} }});

  // 3. Test wrong code shows #accounts-modal-status inside dialog while #toast stays hidden, then "Новый номер" resets cleanly
  await reauthBtn3.click();
  await page.locator('#account-reauth-banner').waitFor({{ state: 'visible' }});
  const prefilledPhone = await page.locator('#phone').inputValue();
  if (prefilledPhone !== '+79959022911') {{
    throw new Error(`Expected prefilled phone +79959022911, got ${{prefilledPhone}}`);
  }}
  await page.locator('#account-proxy-custom').fill('socks5://isp:pass@1.2.3.4:1080');
  await page.screenshot({{ path: {str(shot_reauth)!r} }});

  await page.locator('#login-form button[type="submit"]').click();
  await page.locator('#code-form').waitFor({{ state: 'visible', timeout: 10000 }});
  await page.locator('#code').fill('99999');
  expect400CodeError = true;
  await page.locator('#code-form button[type="submit"]').click();
  await page.locator('#accounts-modal-status').waitFor({{ state: 'visible' }});
  expect400CodeError = false;
  const errModalStatus = await page.evaluate(() => {{
    const dlg = document.querySelector('#accounts-modal');
    const st = document.querySelector('#accounts-modal-status');
    const bgToast = document.querySelector('#toast');
    const rect = st ? st.getBoundingClientRect() : null;
    return {{
      contained: Boolean(dlg && st && dlg.contains(st)),
      height: rect ? rect.height : 0,
      kind: st ? st.dataset.kind : null,
      text: st ? st.textContent : '',
      bgToastHidden: bgToast ? bgToast.hidden : true,
    }};
  }});
  if (!errModalStatus.contained || errModalStatus.height <= 0 || errModalStatus.kind !== 'error' || !errModalStatus.bgToastHidden) {{
    throw new Error(`Expected error status inside modal with hidden background toast: ${{JSON.stringify(errModalStatus)}}`);
  }}

  // Click "Новый номер" -> must reset code-form and phone
  await page.locator('#account-reauth-reset-btn').click();
  await page.locator('#code-form').waitFor({{ state: 'hidden' }});
  if ((await page.locator('#phone').inputValue()) !== '') {{
    throw new Error('Expected #phone cleared after clicking Новый номер');
  }}

  // Click "Код" on pending #3 -> resumes login -> submit valid code 11111 -> #accounts-modal-status shows success inside dialog
  await page.locator('button[data-resume-account-id="3"]').click();
  await page.locator('#code-form').waitFor({{ state: 'visible', timeout: 10000 }});
  await page.locator('#code').fill('11111');
  await page.locator('#code-form button[type="submit"]').click();
  await page.locator('#accounts-modal-status[data-kind="ok"]').waitFor({{ state: 'visible', timeout: 10000 }});
  const okStatusCheck = await page.evaluate(() => ({{
    text: document.querySelector('#accounts-modal-status')?.textContent || '',
    bgToastHidden: document.querySelector('#toast')?.hidden,
  }}));
  if (!okStatusCheck.text.includes('Аккаунт подключён') || !okStatusCheck.bgToastHidden) {{
    throw new Error(`Unexpected modal success status: ${{JSON.stringify(okStatusCheck)}}`);
  }}
  await page.waitForTimeout(250);

  // Verify account #3 remained id=3 and is now active with updated proxy
  const accsAfter = await (await page.request.get('{base_url}/api/accounts')).json();
  const acc3 = accsAfter.find((a) => Number(a.id) === 3);
  if (!acc3 || acc3.status !== 'connected' || acc3.health !== 'active' || acc3.proxy_url !== 'socks5://isp:pass@1.2.3.4:1080') {{
    throw new Error(`Reauth did not update account #3 properly: ${{JSON.stringify(acc3)}}`);
  }}
  const topBtnAfter = (await topBtn.textContent()).trim();
  if (!topBtnAfter.includes('Аккаунты 3/5') || !topBtnAfter.includes('2')) {{
    throw new Error(`Topbar counter did not update after reauth: ${{topBtnAfter}}`);
  }}

  await page.keyboard.press('Escape');
  await page.locator('#accounts-modal').waitFor({{ state: 'hidden' }});

  // 4. Mobile 390x844 screenshot & zero overflow check
  await page.setViewportSize({{ width: 390, height: 844 }});
  await topBtn.click();
  await page.locator('#accounts-modal[open]').waitFor({{ state: 'visible' }});
  await page.waitForTimeout(200);
  const mobileOverflow = await page.evaluate(() => {{
    const dlg = document.querySelector('#accounts-modal');
    const body = dlg ? dlg.querySelector('.accounts-modal-body') : null;
    return {{
      theme: document.documentElement.getAttribute('data-theme'),
      docOverflow: document.documentElement.scrollWidth > window.innerWidth,
      dlgOverflow: dlg ? dlg.scrollWidth > dlg.clientWidth + 2 : false,
      bodyOverflow: body ? body.scrollWidth > body.clientWidth + 2 : false,
      bodyScrollLeft: body ? body.scrollLeft : 0,
    }};
  }});
  if (
    mobileOverflow.theme !== 'dark' ||
    mobileOverflow.docOverflow ||
    mobileOverflow.dlgOverflow ||
    mobileOverflow.bodyOverflow ||
    mobileOverflow.bodyScrollLeft !== 0
  ) {{
    throw new Error(`Mobile overflow/theme error in accounts modal: ${{JSON.stringify(mobileOverflow)}}`);
  }}
  await page.screenshot({{ path: {str(shot_mobile)!r} }});
  await page.keyboard.press('Escape');
  await page.locator('#accounts-modal').waitFor({{ state: 'hidden' }});

  // Back to desktop 1440x900
  await page.setViewportSize({{ width: 1440, height: 900 }});

  // 5. Inbox v2 React: #inbox-connect-account-btn AND Account Picker #inbox-picker-manage-accounts-btn open modal
  await page.locator('a[data-sheet-link="inbox"]').first().click();
  await page.locator('#inbox-v2-host[data-inbox-v2="mounted"]').waitFor({{ state: 'visible', timeout: 15000 }});
  const inboxConnectBtn = page.locator('#inbox-connect-account-btn');
  await inboxConnectBtn.waitFor({{ state: 'visible' }});
  await page.screenshot({{ path: {str(shot_inbox)!r} }});
  await inboxConnectBtn.click();
  await page.locator('#accounts-modal[open]').waitFor({{ state: 'visible' }});
  await page.locator('#accounts-modal-close-btn').click();
  await page.locator('#accounts-modal').waitFor({{ state: 'hidden' }});

  // Select dialog Алексей Лид, open account picker popover, click #inbox-picker-manage-accounts-btn
  await page.locator('#inbox-v2-host').getByText('Алексей Лид').first().click();
  const pickerTrigger = page.locator('#inbox-v2-host button[popovertarget="inbox-account-picker-popover"]');
  await pickerTrigger.waitFor({{ state: 'visible' }});
  await pickerTrigger.click();
  const manageAccountsBtn = page.locator('#inbox-picker-manage-accounts-btn');
  await manageAccountsBtn.waitFor({{ state: 'visible' }});
  await manageAccountsBtn.click();
  await page.locator('#accounts-modal[open]').waitFor({{ state: 'visible' }});
  await page.locator('#accounts-modal-close-btn').click();
  await page.locator('#accounts-modal').waitFor({{ state: 'hidden' }});

  // 6. Статистика -> @SpamBot button shows "↻ Проверить" hint AND closing modal preserves scroll position
  await page.locator('a[data-sheet-link="analytics"]').first().click();
  await page.locator('#stats-accounts-health').waitFor({{ state: 'visible' }});
  const statsReauthBtn = page.locator('button[data-stats-reauth-id="4"]');
  await statsReauthBtn.waitFor({{ state: 'visible' }});
  const scrollMetrics = await page.evaluate(() => {{
    const btn = document.querySelector('button[data-stats-reauth-id="4"]');
    if (btn) btn.scrollIntoView({{ block: 'center' }});
    return {{ beforeY: window.scrollY }};
  }});
  await statsReauthBtn.click();
  await page.locator('#accounts-modal[open]').waitFor({{ state: 'visible' }});
  const phone4 = await page.locator('#phone').inputValue();
  const banner4 = await page.locator('#account-reauth-banner-text').textContent();
  if (phone4 !== '+381677672089' || !(banner4 || '').includes('↻ Проверить')) {{
    throw new Error(`Expected frozen SpamBot warning for #4, got phone=${{phone4}} banner=${{banner4}}`);
  }}
  await page.locator('#accounts-modal-close-btn').click();
  await page.locator('#accounts-modal').waitFor({{ state: 'hidden' }});
  await page.waitForTimeout(100);
  if (scrollMetrics.beforeY > 0) {{
    const afterY = await page.evaluate(() => window.scrollY);
    if (Math.abs(afterY - scrollMetrics.beforeY) > 5) {{
      throw new Error(`Modal close jumped scroll position: before=${{scrollMetrics.beforeY}} after=${{afterY}}`);
    }}
  }}

  // 7. Рассылки -> Аккаунты tab renders #accounts cleanly inside #outreach-accounts-pane
  await page.locator('a[data-sheet-link="outreach"]').first().click();
  await page.locator('#outreach-tab-accounts').click();
  await page.locator('#outreach-accounts-pane #accounts').waitFor({{ state: 'visible' }});
  await page.locator('#outreach-accounts-pane #account-list .account').first().waitFor({{ state: 'visible' }});

  // 8. Real non-admin operator context: masked *** proxy must NOT prefill #account-proxy-custom
  const opCtx = await browser.newContext({{ viewport: {{ width: 1280, height: 800 }} }});
  const opPage = await opCtx.newPage();
  attachErrorMonitors(opPage, {{ isOperator: true }});
  const opLogin = await opPage.request.post('{base_url}/api/auth/login', {{
    data: {{ username: 'op_user', password: '{operator_password}' }},
  }});
  if (!opLogin.ok()) throw new Error(`Operator login failed: ${{opLogin.status()}}`);
  const opAccs = await (await opPage.request.get('{base_url}/api/accounts')).json();
  const opAcc5 = opAccs.find((a) => Number(a.id) === 5);
  if (!opAcc5 || !(opAcc5.proxy_url || '').includes('***')) {{
    throw new Error(`Expected masked *** proxy for operator on #5, got ${{JSON.stringify(opAcc5)}}`);
  }}
  await opPage.goto('{base_url}/', {{ waitUntil: 'domcontentloaded' }});
  await opPage.locator('#open-accounts-modal-btn').waitFor({{ state: 'visible', timeout: 15000 }});
  await opPage.locator('#open-accounts-modal-btn').click();
  await opPage.locator('#accounts-modal[open]').waitFor({{ state: 'visible' }});
  await opPage.locator('button[data-reauth-account-id="5"]').click();
  await opPage.locator('#account-reauth-banner').waitFor({{ state: 'visible' }});
  const customProxyVal = await opPage.locator('#account-proxy-custom').inputValue();
  const customProxyPlaceholder = await opPage.locator('#account-proxy-custom').getAttribute('placeholder');
  if (customProxyVal !== '' || !(customProxyPlaceholder || '').includes('сохранён')) {{
    throw new Error(`Masked proxy leaked or placeholder missing: val=${{JSON.stringify(customProxyVal)}} ph=${{JSON.stringify(customProxyPlaceholder)}}`);
  }}
  await opCtx.close();

  if (pageErrors.length || consoleErrors.length || server5xx.length) {{
    throw new Error(`Errors detected: pageErrors=${{JSON.stringify(pageErrors)}} consoleErrors=${{JSON.stringify(consoleErrors)}} server5xx=${{JSON.stringify(server5xx)}}`);
  }}

  await browser.close();
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = _resolve_playwright_node_path()
        node_bin = shutil.which("node") or "/opt/homebrew/bin/node"
        proc = subprocess.run(
            [node_bin, "-e", pw_script],
            cwd=str(ROOT / "e2e"),
            env=node_env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert proc.returncode == 0, (
            f"Accounts Modal Playwright E2E failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
