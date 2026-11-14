from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent


def test_systemd_unit_is_valid_and_binds_18084():
    unit_path = ROOT / "deploy" / "systemd" / "crmtg.service"
    assert unit_path.exists(), "deploy/systemd/crmtg.service must exist"
    text = unit_path.read_text(encoding="utf-8")
    assert "WorkingDirectory=/opt/crmtg" in text
    assert "EnvironmentFile=-/opt/crmtg/.env" in text
    assert 'Environment="DEMO_AUTOSEED=0"' in text
    assert 'Environment="CRMTG_VPS_RUNTIME=1"' in text
    assert 'Environment="CRM_REQUIRE_AUTH=1"' in text
    assert "--host 127.0.0.1 --port 18084" in text
    assert "Restart=always" in text


def test_should_enable_live_tg_runtime_prefers_vps_over_render(monkeypatch):
    from app.main import should_enable_live_tg_runtime

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ALLOW_LOCAL_TG_SESSIONS", raising=False)
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)
    monkeypatch.delenv("RENDER", raising=False)

    # Contabo VPS with CRMTG_VPS_RUNTIME=1 must enable live TG runtime
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    assert should_enable_live_tg_runtime(conn=None, gateway=None) is True



def test_caddy_snippet_routes_crm_domains_to_18084():
    caddy_path = ROOT / "deploy" / "caddy" / "crmtg.caddy"
    assert caddy_path.exists(), "deploy/caddy/crmtg.caddy must exist"
    text = caddy_path.read_text(encoding="utf-8")
    assert "crm.softugc.com" in text
    assert "crm-169-58-145-85.sslip.io" in text
    assert "max_size 200MB" in text
    assert "reverse_proxy 127.0.0.1:18084" in text


def test_caddy_snippet_enforces_edge_waf_and_security_headers():
    caddy_path = ROOT / "deploy" / "caddy" / "crmtg.caddy"
    text = caddy_path.read_text(encoding="utf-8")

    # Balanced braces and quotes sanity check
    assert text.count("{") == text.count("}"), "Unbalanced braces in crmtg.caddy"
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert stripped.count('"') % 2 == 0, f"Unbalanced quotes on line {lineno} of crmtg.caddy"

    # Security & anti-indexing headers (deferred after upstream reverse_proxy)
    assert re.search(r"header\s*\{\s*defer\b", text), "header block must start with defer"
    assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains"' in text
    assert 'X-Content-Type-Options "nosniff"' in text
    assert 'X-Frame-Options "SAMEORIGIN"' in text
    assert 'Referrer-Policy "no-referrer"' in text
    assert 'X-Robots-Tag "noindex, nofollow, noarchive, nosnippet"' in text
    assert "-Server" in text

    # Edge exploit/scanner path blocker
    assert "@scanners" in text
    for pat in ("/.env*", "*/.env*", "/.git*", "*.php", "*.sql", "/wp-admin*", "/wp-config*", "/phpmyadmin*"):
        assert pat in text, f"Missing scanner path pattern {pat} in crmtg.caddy"
    assert 'respond @scanners "Forbidden" 403' in text

    # Edge scraper & attack bot UA blocker
    assert "@bad_bots" in text
    for bot in ("scrapy", "sqlmap", "nikto", "nmap", "ahrefsbot", "semrushbot", "gptbot", "ccbot", "claude-web", "perplexitybot"):
        assert bot in text.lower(), f"Missing bot token {bot} in crmtg.caddy"
    assert 'respond @bad_bots "Forbidden" 403' in text

    # Script scrapers blocked on anonymous web routes while preserving /mcp, /api/health, and authenticated API automation
    assert "@script_scrapers" in text
    assert "not path /mcp* /api/health /robots.txt" in text
    assert "not header Authorization *" in text
    assert "not header X-MCP-API-Key *" in text
    assert "not header X-MCP-Key *" in text
    assert "not header X-Foldesk-Token *" in text
    assert 'respond @script_scrapers "Forbidden" 403' in text

    # Anti-spoofing client IP headers in reverse_proxy
    assert "header_up X-Real-IP {remote_host}" in text
    assert "header_up X-Forwarded-For {remote_host}" in text


def test_github_actions_workflow_runs_pytest_then_deploys_to_vps():
    wf_path = ROOT / ".github" / "workflows" / "deploy-vps.yml"
    assert wf_path.exists(), ".github/workflows/deploy-vps.yml must exist"
    text = wf_path.read_text(encoding="utf-8")
    assert "branches: [main]" in text
    assert "workflow_dispatch:" in text
    assert "pytest -q" in text
    assert "needs: test" in text
    assert "appleboy/ssh-action@v1.2.0" in text
    assert 'mkdir -p -m 700 "$APP_DIR/backups"' in text
    assert "umask 077" in text
    assert 'chmod 600 "$APP_DIR/backups/crm.db.pre-${TARGET_SHA:0:8}"' in text
    assert "systemctl restart crmtg.service" in text
    assert "http://127.0.0.1:18084/api/health" in text
    assert "http://127.0.0.1:18084/api/accounts" not in text


def test_render_autodeploy_is_disabled():
    render_path = ROOT / "render.yaml"
    assert render_path.exists()
    text = render_path.read_text(encoding="utf-8")
    assert re.search(r"autoDeploy:\s*false\b", text), "render.yaml must have autoDeploy: false after VPS migration"
