import os
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app


def _make_secured_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MCP_API_KEY", "test-mcp-secret-999")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "StrongTimPass#2026")
    monkeypatch.setenv("CRM_VLAD_PASSWORD", "StrongVladPass#2026")
    monkeypatch.setenv("CRM_OPERATOR_PASSWORD", "StrongOpPass#2026")
    conn = connect(tmp_path / "crm_sec.db")
    init_db(conn)
    app = create_app(conn=conn)
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
    assert client.delete("/api/accounts/1", headers=vlad_headers).status_code == 403
    assert client.post("/api/settings/tg-credentials", json={"api_id": 123, "api_hash": "abc"}, headers=vlad_headers).status_code == 403


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

