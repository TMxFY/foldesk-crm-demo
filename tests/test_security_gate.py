import os
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app


def _make_secured_client(tmp_path, monkeypatch, gateway=None, wa_gateway=None):
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MCP_API_KEY", "test-mcp-secret-999")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "StrongTimPass#2026")
    monkeypatch.setenv("CRM_VLAD_PASSWORD", "StrongVladPass#2026")
    monkeypatch.setenv("CRM_OPERATOR_PASSWORD", "StrongOpPass#2026")
    conn = connect(tmp_path / "crm_sec.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=gateway, wa_gateway=wa_gateway)
    return TestClient(app, follow_redirects=False), conn


def test_unauthenticated_page_and_api_are_blocked_when_auth_required(tmp_path, monkeypatch):
    client, _ = _make_secured_client(tmp_path, monkeypatch)

    # 1. Root page / redirects to /login (302/307) when unauthenticated
    r_root = client.get("/")
    assert r_root.status_code in (302, 307)
    assert r_root.headers["location"].startswith("/login")

    # 2. Protected API endpoints return 401 when unauthenticated
    for path in ("/api/leads", "/api/accounts", "/api/inbox", "/api/analytics/summary", "/api/auth/users", "/api/mcp/info"):
        r_api = client.get(path)
        assert r_api.status_code == 401, f"Expected 401 for {path}, got {r_api.status_code}"

    # 3. Local loopback healthcheck (127.0.0.1) is allowed for GET /api/accounts only when client is 127.0.0.1 and X-Forwarded-For is absent,
    #    OR we have a dedicated /api/health endpoint that returns {"ok": true} without leaking accounts!
    r_health = client.get("/api/health")
    assert r_health.status_code == 200
    assert r_health.json().get("ok") is True

    # 4. Login page /login is accessible (200) and MUST NOT contain plaintext passwords in HTML
    r_login = client.get("/login")
    assert r_login.status_code == 200
    html = r_login.text
    assert "StrongTimPass#2026" not in html
    assert "TimFoldesk2026!" not in html
    assert "VladSales2026!" not in html
    assert "Operator2026!" not in html
    assert "data-preset-pass=" not in html


def test_public_registration_is_blocked_without_existing_admin_session(tmp_path, monkeypatch):
    client, _ = _make_secured_client(tmp_path, monkeypatch)

    # Unauthenticated attacker cannot self-register an admin account
    r_reg = client.post(
        "/api/auth/register",
        json={
            "username": "hacker_admin",
            "password": "HackerPassword123!",
            "display_name": "Attacker",
            "role": "admin",
        },
    )
    assert r_reg.status_code in (401, 403)


def test_anti_scraping_blocks_bot_user_agents_and_scanner_paths(tmp_path, monkeypatch):
    client, _ = _make_secured_client(tmp_path, monkeypatch)

    # 1. Scanner paths (.env, wp-config.php, .git/config, backup.sql) get immediate 403
    for bad_path in ("/.env", "/api/.env", "/wp-config.php", "/.git/config", "/backup.sql", "/dump.sql"):
        r_scan = client.get(bad_path)
        assert r_scan.status_code == 403, f"Expected 403 on scanner path {bad_path}, got {r_scan.status_code}"

    # 2. Scraper User-Agents get 403 on page/API endpoints
    for bot_ua in ("python-requests/2.31.0", "Scrapy/2.11.0", "AhrefsBot/7.0", "SemrushBot", "sqlmap/1.8"):
        r_bot = client.get("/login", headers={"User-Agent": bot_ua})
        assert r_bot.status_code == 403, f"Expected 403 for scraper UA {bot_ua}, got {r_bot.status_code}"

    # 3. robots.txt disallows all indexing and scraping
    r_robots = client.get("/robots.txt", headers={"User-Agent": "Googlebot/2.1"})
    assert r_robots.status_code == 200
    assert "Disallow: /" in r_robots.text


def test_login_brute_force_rate_limit_and_authenticated_access(tmp_path, monkeypatch):
    client, _ = _make_secured_client(tmp_path, monkeypatch)

    # Valid login with StrongTimPass#2026 succeeds and sets HttpOnly cookie
    r_ok = client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "StrongTimPass#2026"},
        headers={"User-Agent": "Mozilla/5.0 Chrome/125.0"},
    )
    assert r_ok.status_code == 200
    token = r_ok.json()["token"]
    assert "HttpOnly" in r_ok.headers.get("set-cookie", "") or "httponly" in r_ok.headers.get("set-cookie", "").lower()

    # With valid token, / and /api/leads return 200 and X-Robots-Tag header is set
    r_leads = client.get("/api/leads", headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"})
    assert r_leads.status_code == 200
    assert "noindex" in r_leads.headers.get("x-robots-tag", "").lower()

    # Brute-force: 8 failed login attempts from same IP trigger 429 Too Many Requests
    for _ in range(8):
        client.post(
            "/api/auth/login",
            json={"username": "tim", "password": "wrong-password"},
            headers={"X-Forwarded-For": "203.0.113.77", "User-Agent": "Mozilla/5.0"},
        )
    r_locked = client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "wrong-password"},
        headers={"X-Forwarded-For": "203.0.113.77", "User-Agent": "Mozilla/5.0"},
    )
    assert r_locked.status_code == 429


def test_sales_role_rbac_prevents_login_loop_and_blocks_infra_endpoints(tmp_path, monkeypatch):
    client, conn = _make_secured_client(tmp_path, monkeypatch)
    conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, proxy_url)
        VALUES ('+15550001111', 'data/sessions/test_acc.session', 'connected', '2026-01-01T00:00:00', 'socks5://nmuser:supersecretpass@gate.nodemaven.com:1080')
        """
    )
    conn.commit()

    r_vlad = client.post(
        "/api/auth/login",
        json={"username": "vlad", "password": "StrongVladPass#2026"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    assert r_vlad.status_code == 200
    vlad_headers = {"Authorization": f"Bearer {r_vlad.json()['token']}", "User-Agent": "Mozilla/5.0"}

    # 1. GET /api/mcp/info returns 403 (NOT 401, preventing app.js boot redirect loop)
    r_mcp_info = client.get("/api/mcp/info", headers=vlad_headers)
    assert r_mcp_info.status_code == 403

    # 2. Normal CRM read endpoints work (200), and GET /api/accounts masks proxy password for non-admin
    assert client.get("/api/leads", headers=vlad_headers).status_code == 200
    r_accs = client.get("/api/accounts", headers=vlad_headers)
    assert r_accs.status_code == 200
    accs = r_accs.json()
    assert len(accs) == 1
    assert "supersecretpass" not in str(accs[0].get("proxy_url") or "")
    assert "***" in str(accs[0].get("proxy_url") or "")

    # 3. Infrastructure / credentials / destructive endpoints return 403 for non-admin
    r_cloud = client.post(
        "/api/cloud/credentials",
        json={"endpoint_url": "https://evil.r2.cloudflarestorage.com", "access_key_id": "x", "secret_access_key": "y", "bucket": "b"},
        headers=vlad_headers,
    )
    assert r_cloud.status_code == 403
    assert client.post("/api/settings/tg-credentials", json={"api_id": 123, "api_hash": "abc"}, headers=vlad_headers).status_code == 403
    assert client.post("/api/leadgen/config", json={"enabled": True}, headers=vlad_headers).status_code == 403
    assert client.post("/api/proxy/nodemaven", json={}, headers=vlad_headers).status_code == 403
    assert client.post("/api/outreach/autopilot", json={"enabled": False}, headers=vlad_headers).status_code == 403
    assert client.post("/api/campaigns/1/run", headers=vlad_headers).status_code == 403


def test_leaked_default_passwords_rejected_even_when_env_passwords_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CRM_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("CRM_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("CRM_VLAD_PASSWORD", raising=False)
    monkeypatch.delenv("CRM_OPERATOR_PASSWORD", raising=False)

    # Simulate legacy DB seeded before auth enforcement had TimFoldesk2026!
    conn = connect(tmp_path / "legacy_defaults.db")
    init_db(conn)

    # Now enable CRM_REQUIRE_AUTH=1 WITHOUT setting CRM_ADMIN_PASSWORD
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MCP_API_KEY", "test-mcp-secret-999")
    app = create_app(conn=conn)
    client = TestClient(app, follow_redirects=False)

    for uname, leaked_pwd in (
        ("tim", "TimFoldesk2026!"),
        ("vlad", "VladSales2026!"),
        ("operator", "Operator2026!"),
    ):
        r = client.post(
            "/api/auth/login",
            json={"username": uname, "password": leaked_pwd},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        assert r.status_code == 401, f"Leaked default password must be rejected for {uname}"


def test_xff_spoofing_rightmost_ip_rate_limit(tmp_path, monkeypatch):
    client, _ = _make_secured_client(tmp_path, monkeypatch)

    # Attacker rotates spoofed leftmost XFF hop while proxy appends real peer 203.0.113.88
    for i in range(8):
        r_fail = client.post(
            "/api/auth/login",
            json={"username": "tim", "password": "wrong-password"},
            headers={
                "X-Forwarded-For": f"1.1.1.{i + 1}, 203.0.113.88",
                "User-Agent": "Mozilla/5.0",
            },
        )
        assert r_fail.status_code == 401

    r_locked = client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "wrong-password"},
        headers={
            "X-Forwarded-For": "1.1.1.1, 203.0.113.88",
            "User-Agent": "Mozilla/5.0",
        },
    )
    assert r_locked.status_code == 429


def test_logged_out_and_expired_stateless_tokens_cannot_resurrect(tmp_path, monkeypatch):
    import time
    from app import crm_auth

    client, conn = _make_secured_client(tmp_path, monkeypatch)
    r_login = client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "StrongTimPass#2026"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    assert r_login.status_code == 200
    token = r_login.json()["token"]
    assert len(token.split(".")) == 4

    # Logout and clear in-memory _REVOKED_TOKENS (simulating systemctl restart)
    r_logout = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
    )
    assert r_logout.status_code == 200
    crm_auth._REVOKED_TOKENS.clear()

    r_after_restart = client.get(
        "/api/leads",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
    )
    assert r_after_restart.status_code == 401

    # Expired stateless token cannot resurrect even if signed with current DB secret
    prow = conn.execute("SELECT id, password_hash FROM crm_users WHERE username = 'tim'").fetchone()
    expired_tok = crm_auth._sign_stateless_token(
        int(prow["id"]),
        int(time.time()) - 10,
        "expirednonce123",
        conn=conn,
        pwd_hash=str(prow["password_hash"]),
    )
    assert (
        client.get(
            "/api/leads",
            headers={"Authorization": f"Bearer {expired_tok}", "User-Agent": "Mozilla/5.0"},
        ).status_code
        == 401
    )


def test_admin_register_preserves_admin_cookie_and_csrf_origin_check(tmp_path, monkeypatch):
    from app.crm_auth import AUTH_COOKIE_NAME

    client, _ = _make_secured_client(tmp_path, monkeypatch)
    r_admin = client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "StrongTimPass#2026"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    assert r_admin.status_code == 200
    admin_cookie_before = client.cookies.get(AUTH_COOKIE_NAME)
    assert admin_cookie_before

    # Cross-origin mutation attempt is blocked with 403
    r_csrf = client.post(
        "/api/auth/register",
        json={"username": "csrf_op", "password": "OpPassword123!", "role": "operator"},
        headers={"User-Agent": "Mozilla/5.0", "Origin": "https://evil.attacker.example"},
    )
    assert r_csrf.status_code == 403

    # Same-origin admin registration succeeds WITHOUT overwriting admin's cookie
    r_reg = client.post(
        "/api/auth/register",
        json={"username": "new_sales_rep", "password": "RepPassword123!", "role": "sales"},
        headers={"User-Agent": "Mozilla/5.0", "Origin": "http://testserver"},
    )
    assert r_reg.status_code == 200
    assert "set-cookie" not in r_reg.headers
    assert client.cookies.get(AUTH_COOKIE_NAME) == admin_cookie_before

    r_me = client.get("/api/auth/me", headers={"User-Agent": "Mozilla/5.0"})
    assert r_me.status_code == 200
    assert r_me.json()["user"]["username"] == "tim"


def test_non_admin_roles_can_add_manage_and_delete_accounts_without_proxy_leak(tmp_path, monkeypatch):
    from app.telegram_gateway import FakeGateway

    class _MultiAccountFakeGateway(FakeGateway):
        async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
            res = await super().confirm_code(phone, code, phone_code_hash, session_path)
            if res.get("status") == "connected":
                uid = int("".join(ch for ch in str(phone) if ch.isdigit()) or "1001")
                res["telegram_user_id"] = uid
                res["telegram_username"] = f"user_{uid}"
            return res

    class _FakeWaGateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__(code="11111")
            self.cleared_paths: list[str] = []

        async def request_code(
            self,
            phone: str,
            session_path: str,
            proxy_url: str | None = None,
            auth_mode: str = "code",
            **_kw,
        ) -> str:
            return await super().request_code(phone, session_path)

        def clear_session(self, session_path: str) -> None:
            self.cleared_paths.append(str(session_path))

    gateway = _MultiAccountFakeGateway(code="11111")
    wa_gateway = _FakeWaGateway()
    client, conn = _make_secured_client(tmp_path, monkeypatch, gateway=gateway, wa_gateway=wa_gateway)
    proxy_secret_url = "socks5://nmuser:supersecretpass@gate.nodemaven.com:1080"

    assert (
        client.post(
            "/api/accounts/start",
            json={"phone": "+15551110000"},
            headers={"User-Agent": "Mozilla/5.0"},
        ).status_code
        == 401
    )
    assert client.delete("/api/accounts/1", headers={"User-Agent": "Mozilla/5.0"}).status_code == 401
    assert client.post("/api/accounts/purge-dead", headers={"User-Agent": "Mozilla/5.0"}).status_code == 401

    created_ids: list[int] = []
    for username, password, phone in (
        ("vlad", "StrongVladPass#2026", "+15551110001"),
        ("operator", "StrongOpPass#2026", "+15551110002"),
    ):
        r_login = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        assert r_login.status_code == 200
        hdrs = {
            "Authorization": f"Bearer {r_login.json()['token']}",
            "User-Agent": "Mozilla/5.0",
        }

        r_start = client.post(
            "/api/accounts/start",
            json={"phone": phone, "proxy_url": proxy_secret_url},
            headers=hdrs,
        )
        assert r_start.status_code == 200, f"{username} start failed: {r_start.text}"
        start_data = r_start.json()
        acc_id = int(start_data["id"])
        created_ids.append(acc_id)
        if "proxy_url" in start_data and start_data["proxy_url"] is not None:
            assert "supersecretpass" not in str(start_data["proxy_url"])
            assert "***" in str(start_data["proxy_url"])

        r_code = client.post(
            f"/api/accounts/{acc_id}/code",
            json={"code": "11111"},
            headers=hdrs,
        )
        assert r_code.status_code == 200, f"{username} code failed: {r_code.text}"
        code_data = r_code.json()
        assert code_data["status"] == "connected"
        assert "supersecretpass" not in str(code_data.get("proxy_url") or "")

        r_limit = client.patch(
            f"/api/accounts/{acc_id}/limit",
            json={"daily_limit": 25},
            headers=hdrs,
        )
        assert r_limit.status_code == 200, f"{username} limit failed: {r_limit.text}"
        limit_data = r_limit.json()
        assert "supersecretpass" not in str(limit_data.get("proxy_url") or "")
        assert "***" in str(limit_data.get("proxy_url") or "")

        r_settings = client.patch(
            f"/api/accounts/{acc_id}/settings",
            json={"daily_limit": 30},
            headers=hdrs,
        )
        assert r_settings.status_code == 200, f"{username} settings failed: {r_settings.text}"
        settings_data = r_settings.json()
        assert "supersecretpass" not in str(settings_data.get("proxy_url") or "")
        assert "***" in str(settings_data.get("proxy_url") or "")

        r_reconnect = client.post(
            f"/api/accounts/{acc_id}/reconnect",
            headers=hdrs,
        )
        assert r_reconnect.status_code == 200, f"{username} reconnect failed: {r_reconnect.text}"
        assert "supersecretpass" not in str(r_reconnect.json().get("proxy_url") or "")

        r_check = client.post("/api/accounts/check-all", headers=hdrs)
        assert r_check.status_code == 200, f"{username} check-all failed: {r_check.text}"
        for acc_row in r_check.json().get("accounts") or []:
            assert "supersecretpass" not in str(acc_row.get("proxy_url") or "")

    # Non-admin (vlad) can delete a mistyped pending_code WhatsApp account and purge dead accounts safely without proxy leaks
    vlad_login = client.post(
        "/api/auth/login",
        json={"username": "vlad", "password": "StrongVladPass#2026"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    assert vlad_login.status_code == 200
    vlad_hdrs = {
        "Authorization": f"Bearer {vlad_login.json()['token']}",
        "User-Agent": "Mozilla/5.0",
    }

    wa_start_1 = client.post(
        "/api/accounts/start",
        json={"phone": "+7908819595", "provider": "whatsapp", "proxy_url": proxy_secret_url},
        headers=vlad_hdrs,
    )
    assert wa_start_1.status_code == 200, wa_start_1.text
    wa_id_1 = int(wa_start_1.json()["id"])
    r_del_wa = client.delete(f"/api/accounts/{wa_id_1}", headers=vlad_hdrs)
    assert r_del_wa.status_code == 200, r_del_wa.text
    assert r_del_wa.json()["deleted"] is True
    assert r_del_wa.json()["id"] == wa_id_1
    assert any(f"wa_{wa_id_1}.session" in p for p in wa_gateway.cleared_paths)

    wa_start_2 = client.post(
        "/api/accounts/start",
        json={"phone": "+7908819596", "provider": "whatsapp", "proxy_url": proxy_secret_url},
        headers=vlad_hdrs,
    )
    assert wa_start_2.status_code == 200, wa_start_2.text
    wa_id_2 = int(wa_start_2.json()["id"])
    r_purge = client.post("/api/accounts/purge-dead", headers=vlad_hdrs)
    assert r_purge.status_code == 200, r_purge.text
    purge_data = r_purge.json()
    assert wa_id_2 in purge_data.get("deleted_ids", [])
    assert any(f"wa_{wa_id_2}.session" in p for p in wa_gateway.cleared_paths)
    for acc_row in purge_data.get("accounts") or []:
        assert "supersecretpass" not in str(acc_row.get("proxy_url") or "")
        assert "***" in str(acc_row.get("proxy_url") or "")

    # Operator can purge dead accounts (including raw colon proxy masking + cookie auth) and delete TG accounts safely
    op_login = client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "StrongOpPass#2026"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    assert op_login.status_code == 200
    op_hdrs = {
        "Authorization": f"Bearer {op_login.json()['token']}",
        "User-Agent": "Mozilla/5.0",
    }
    from app.telegram_gateway import mask_proxy_url

    assert mask_proxy_url("gate.nodemaven.com:1080:nmuser:supersecretpass") == "socks5://nmuser:***@gate.nodemaven.com:1080"
    wa_start_op = client.post(
        "/api/accounts/start",
        json={"phone": "+7908819597", "provider": "telegram", "proxy_url": "gate.nodemaven.com:1080:nmuser:supersecretpass"},
        headers=op_hdrs,
    )
    assert wa_start_op.status_code == 200, wa_start_op.text
    op_wa_id = int(wa_start_op.json()["id"])
    r_op_purge = client.post("/api/accounts/purge-dead", headers=op_hdrs)
    assert r_op_purge.status_code == 200, r_op_purge.text
    assert op_wa_id in r_op_purge.json().get("deleted_ids", [])
    for acc_row in r_op_purge.json().get("accounts") or []:
        assert "supersecretpass" not in str(acc_row.get("proxy_url") or "")
        assert "***" in str(acc_row.get("proxy_url") or "")

    # Cookie auth (as browser UI sends) works for DELETE too, and app.state.wa_gateway=None degrades safely without crash
    wa_start_3 = client.post(
        "/api/accounts/start",
        json={"phone": "+7908819598", "provider": "whatsapp"},
        headers=op_hdrs,
    )
    assert wa_start_3.status_code == 200, wa_start_3.text
    wa_id_3 = int(wa_start_3.json()["id"])
    app_no_wa = create_app(conn=conn, gateway=gateway, wa_gateway=None)
    app_no_wa.state.wa_gateway = None
    with TestClient(app_no_wa, follow_redirects=False) as client_no_wa:
        r_cookie_login = client_no_wa.post(
            "/api/auth/login",
            json={"username": "operator", "password": "StrongOpPass#2026"},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        assert r_cookie_login.status_code == 200
        assert client_no_wa.delete(f"/api/accounts/{wa_id_3}", headers={"User-Agent": "Mozilla/5.0"}).status_code == 200

    assert client.delete(f"/api/accounts/{created_ids[1]}", headers=op_hdrs).status_code == 200

    r_admin = client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "StrongTimPass#2026"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    assert r_admin.status_code == 200
    admin_hdrs = {
        "Authorization": f"Bearer {r_admin.json()['token']}",
        "User-Agent": "Mozilla/5.0",
    }
    admin_accs = client.get("/api/accounts", headers=admin_hdrs)
    assert admin_accs.status_code == 200
    by_id = {int(item["id"]): item for item in admin_accs.json()}
    assert by_id[created_ids[0]]["proxy_url"] == proxy_secret_url
    assert client.delete(f"/api/accounts/{created_ids[0]}", headers=admin_hdrs).status_code == 200

