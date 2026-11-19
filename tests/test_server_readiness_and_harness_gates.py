from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

from fastapi.testclient import TestClient
import pytest

from app.db import connect, init_db, utc_now
from app.hygiene import (
    append_direct_x_lead,
    audit_crm_hygiene,
    audit_server_leadgen_readiness,
    heal_crm_hygiene,
    read_direct_x_leads,
)
from app.leads import create_lead
from app.main import autopilot_loop, create_app
from app.mcp_server import handle_jsonrpc_message
from app.outreach import create_campaign
from leadgen.vps_autonomous_replenish import split_tg_and_direct_x_leads
import scripts.proactive_audit as proactive_audit

WORKTREE_ROOT = Path(__file__).resolve().parents[1]


def _create_isolated_mtproto_session(path: Path, *, seed_byte: int = 0x5A) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sconn = sqlite3.connect(str(path))
    try:
        sconn.execute(
            "CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER, auth_key BLOB)"
        )
        sconn.execute(
            "INSERT INTO sessions VALUES (2, '149.154.167.50', 443, ?)",
            (bytes([seed_byte]) * 256,),
        )
        sconn.commit()
    finally:
        sconn.close()
    return path


def _create_fake_treg_env(base_dir: Path) -> tuple[Path, Path]:
    bin_dir = base_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    treg_exe = bin_dir / "treg"
    treg_exe.write_text("#!/bin/sh\necho '{\"ok\": true}'\n", encoding="utf-8")
    treg_exe.chmod(0o755)
    cfg_path = base_dir / "treg_config.json"
    cfg_path.write_text(json.dumps({"token": "team-multo-valid-token"}), encoding="utf-8")
    return bin_dir, cfg_path


def test_deep_health_and_startup_and_autopilot_heal_real_crm_hygiene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    monkeypatch.setenv("RELAYFAST_API_KEY", "rf-live-key-123")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path / "leadgen_state"))
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(tmp_path / "missing_scraper.session"))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(tmp_path / "empty_sessions"))
    monkeypatch.setenv("TREG_ENRICH", "0")
    monkeypatch.delenv("TREG_API_KEY", raising=False)

    # Enable tg_ad_spy + meta_ads so both scraper_session and treg are strictly required on VPS
    (tmp_path / "leadgen_config.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "b2b": {
                    "enabled": True,
                    "sources": {
                        "tg_ad_spy": True,
                        "meta_ads": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "readiness.db"
    conn = connect(db_path)
    init_db(conn)
    now = utc_now()
    acc_id = int(
        conn.execute(
            """
            INSERT INTO accounts (
                phone, telegram_user_id, telegram_username, telegram_name,
                session_path, status, health, created_at, updated_at
            ) VALUES ('+79991112233', 990011, 'b2bsender', 'Sender', 'data/sessions/s1.session', 'connected', 'active', ?, ?)
            """,
            (now, now),
        ).lastrowid
    )
    camp = create_campaign(
        conn,
        name="Leadgen Auto (TG)",
        account_id=acc_id,
        steps=[{"template_text": "Hey,\n\nSaw Acme. We run SoftUGC."}],
    )
    conn.execute("UPDATE campaigns SET status = 'running' WHERE id = ?", (camp["id"],))
    junk_lead = create_lead(
        conn,
        name="FINPR Marketing Agency",
        company="FINPR Agency",
        niche="Агентство",
        lead_source="Cold outreach",
        username="smm_agency_owner",
        telegram_user_id=445566,
        status="new",
        account_id=acc_id,
    )
    create_lead(
        conn,
        name="Contactless Ghost",
        lead_source="Cold outreach",
        username=None,
        telegram_user_id=None,
        phone=None,
        status="new",
    )
    # Spot-Check #2: Manual / 'Other' / NULL-source talking lead without campaign_recipients MUST stay untouched!
    manual_partner = create_lead(
        conn,
        name="Partner Discussion",
        lead_source="Other",
        status="talking",
        amount=0,
        username=None,
        note="partner marketing agency intro",
    )
    null_talking = create_lead(
        conn,
        name="Direct Warm Chat",
        lead_source=None,
        status="talking",
        amount=0,
        username=None,
        note="open-source marketing agency friend",
    )

    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name, category,
            status, current_step, created_at, updated_at
        ) VALUES (?, ?, 445566, 'smm_agency_owner', 'FINPR Marketing Agency', 'Агентство', 'pending', 0, ?, ?)
        """,
        (camp["id"], junk_lead["id"], now, now),
    )
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('leadgen_stage_errors', ?, ?)
        """,
        (
            json.dumps(
                [
                    {
                        "stage": "tg_enrich_leads",
                        "returncode": -1,
                        "timeout": 180,
                        "error": "timeout_after_180s",
                        "at": now,
                    }
                ]
            ),
            now,
        ),
    )
    conn.commit()

    sent_messages: list[tuple[int, str]] = []

    class _SpyGateway:
        async def send_message(self, session_path, user_id, text, username=None):
            sent_messages.append((int(user_id), text))
            return {"telegram_message_id": 701, "sent_at": utc_now()}

    # 1) create_app() runs real heal_crm_hygiene(conn) on startup while preserving manual/Other talking leads!
    app = create_app(conn=conn, gateway=_SpyGateway())
    rec_status = conn.execute(
        "SELECT status FROM campaign_recipients WHERE lead_id = ?", (junk_lead["id"],)
    ).fetchone()["status"]
    assert rec_status == "completed"

    for preserved_id in (manual_partner["id"], null_talking["id"]):
        p_row = conn.execute("SELECT status FROM leads WHERE id = ?", (preserved_id,)).fetchone()
        assert p_row["status"] == "talking"

    # 2) Inject new runtime junk after startup and verify /api/health?deep=1 reports broken state
    late_junk = create_lead(
        conn,
        name="Traffic Marketing Agency",
        company="Traffic Marketing Agency",
        niche="Агентство",
        lead_source="Cold outreach",
        username="traffic_agency_pro",
        telegram_user_id=778899,
        status="new",
        account_id=acc_id,
    )
    create_lead(
        conn,
        name="Empty Lead After Boot",
        lead_source="Cold outreach",
        username=None,
        telegram_user_id=None,
        phone=None,
        status="new",
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name, category,
            status, current_step, created_at, updated_at
        ) VALUES (?, ?, 778899, 'traffic_agency_pro', 'Traffic Marketing Agency', 'Агентство', 'pending', 0, ?, ?)
        """,
        (camp["id"], late_junk["id"], now, now),
    )
    conn.commit()

    class _LoopbackApp:
        def __init__(self, inner):
            self._inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                scope = dict(scope, client=("127.0.0.1", 50000))
            await self._inner(scope, receive, send)

    with TestClient(_LoopbackApp(app)) as client:
        broken_resp = client.get("/api/health?deep=1")
        assert broken_resp.status_code == 200
        broken = broken_resp.json()
        assert broken["icp_hygiene_ok"] is False
        assert broken["active_junk_recipients"] == 1
        assert broken["active_junk_leads"] == 1
        assert broken["contactless_leads"] == 1
        assert broken["leadgen_ready"] is False
        assert set(broken["leadgen_readiness"]["missing_components"]) == {
            "isolated_scraper_tg_session",
            "treg_cli",
            "stage_errors",
        }

        # 3) autopilot_loop heals BEFORE run_autopilot_tick so 0 messages go out to junk recipient,
        # while keeping fresh stage_errors visible on /api/health?deep=1!
        async def _run_one_autopilot_tick():
            task = asyncio.create_task(autopilot_loop(app, interval_seconds=0.005))
            for _ in range(40):
                await asyncio.sleep(0.01)
                if getattr(app.state, "autopilot_last_tick_at", None) is not None:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run_one_autopilot_tick())
        assert sent_messages == []
        late_rec_status = conn.execute(
            "SELECT status FROM campaign_recipients WHERE lead_id = ?", (late_junk["id"],)
        ).fetchone()["status"]
        assert late_rec_status == "completed"

        # 4) Provision isolated MTProto session + treg CLI + run explicit --heal to clear stale stage_errors -> green!
        iso_sess = _create_isolated_mtproto_session(tmp_path / "scraper" / "signals_ro.session")
        bin_dir, treg_cfg = _create_fake_treg_env(tmp_path / "treg_env")
        monkeypatch.setenv("SCRAPER_TG_SESSION", str(iso_sess))
        monkeypatch.setenv("TREG_ENRICH", "1")
        monkeypatch.setenv("TREG_CONFIG_PATH", str(treg_cfg))
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        heal_crm_hygiene(conn, clear_stale_stage_errors=True)

        green_resp = client.get("/api/health?deep=1")
        assert green_resp.status_code == 200
        green = green_resp.json()
        assert green["icp_hygiene_ok"] is True
        assert green["active_junk_recipients"] == 0
        assert green["active_junk_leads"] == 0
        assert green["contactless_leads"] == 0
        assert green["leadgen_ready"] is True
        assert green["leadgen_readiness"]["missing_components"] == []
    conn.close()


def test_direct_x_contract_roundtrip_between_replenish_hygiene_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MCP_API_KEY", "test-mcp-secret-key-999")

    # 1) Write row via split_tg_and_direct_x_leads (uses work_email / founder_x / project / project_url)
    final_jsonl = state_dir / "raw_batch.final.jsonl"
    dx_jsonl = state_dir / "direct_x_leads.jsonl"
    final_jsonl.write_text(
        json.dumps(
            {
                "name": "Jane Founder",
                "project": "OrbitClip",
                "project_url": "https://orbitclip.ai",
                "work_email": "jane@orbitclip.ai",
                "founder_x": "https://x.com/janefounder",
                "category": "SaaS / AI app",
                "what_they_do": "Subscription AI video clipper ($29/mo)",
                "icp_score": 91,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    split_res = split_tg_and_direct_x_leads(final_jsonl, dx_jsonl)
    assert split_res == {"tg_wa_kept": 0, "direct_x_added": 1, "dropped": 0}

    # 2) Duplicate via append_direct_x_lead (uses email / x_url / company / url) MUST deduplicate!
    dup_added = append_direct_x_lead(
        state_dir,
        {
            "name": "Jane Founder",
            "company": "OrbitClip",
            "email": "jane@orbitclip.ai",
            "x_url": "https://x.com/janefounder",
            "url": "https://orbitclip.ai",
        },
    )
    assert dup_added is False

    # 3) Searchable via MCP list_direct_x_leads by work_email / project / founder_x
    conn = connect(tmp_path / "auth_direct.db")
    init_db(conn)
    mcp_resp = handle_jsonrpc_message(
        conn,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "list_direct_x_leads",
                "arguments": {"query": "orbitclip", "limit": 10},
            },
        },
    )
    sc = mcp_resp["result"]["structuredContent"]
    assert sc["total"] == 1
    assert sc["leads"][0]["email"] == "jane@orbitclip.ai"
    assert sc["leads"][0]["company"] == "OrbitClip"
    assert sc["leads"][0]["x_url"] == "https://x.com/janefounder"

    # 4) Cap & auth check on /api/leadgen/direct-leads
    with dx_jsonl.open("a", encoding="utf-8") as fh:
        for i in range(205):
            fh.write(json.dumps({"idx": i, "x_url": f"https://x.com/founder_{i}"}) + "\n")
        fh.write("{corrupted-json-line\n")

    app = create_app(conn=conn)
    class _LoopbackProxyApp:
        def __init__(self, inner):
            self._inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                scope = dict(scope, client=("127.0.0.1", 50000))
            await self._inner(scope, receive, send)

    with TestClient(_LoopbackProxyApp(app)) as client:
        unauth = client.get("/api/leadgen/direct-leads")
        assert unauth.status_code == 401

        authed = client.get(
            "/api/leadgen/direct-leads",
            headers={"X-MCP-API-Key": "test-mcp-secret-key-999"},
        )
        assert authed.status_code == 200
        body = authed.json()
        assert body["ok"] is True
        assert body["count"] == 206
        assert len(body["items"]) == 200
    conn.close()


def test_proactive_audit_db_heal_clears_stale_stage_errors_and_enforces_real_vps_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)
    monkeypatch.setenv("RELAYFAST_API_KEY", "rf-live-key-123")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(tmp_path / "missing.session"))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(tmp_path / "empty_sessions"))
    monkeypatch.setenv("TREG_ENRICH", "0")

    (tmp_path / "leadgen_config.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "b2b": {"enabled": True, "sources": {"tg_ad_spy": True, "meta_ads": True}},
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "audit_hygiene.db"
    conn = connect(db_path)
    init_db(conn)
    now = utc_now()
    create_lead(
        conn,
        name="Dirty Marketing Agency",
        company="Dirty Marketing Agency",
        niche="Агентство",
        lead_source="Cold outreach",
        username="dirty_agency",
        telegram_user_id=12345,
    )
    create_lead(conn, name="Bot Lead", username="helper_notify_bot", status="new")
    # Pre-deploy historical stage error in kv_settings (Spot-Check #3)
    conn.execute(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES ('leadgen_stage_errors', ?, ?)",
        (json.dumps([{"stage": "tg_enrich_leads", "returncode": -1, "timeout": 180, "at": now}]), now),
    )
    conn.commit()
    conn.close()

    # 1) Dirty DB fails with exit code 1
    rc_dirty = proactive_audit.main(["--db", str(db_path)])
    captured_dirty = capsys.readouterr()
    assert rc_dirty == 1
    assert "PROACTIVE AUDIT FAILED" in captured_dirty.err

    # 2) --heal repairs reply notifications + crm_hygiene AND clears pre-deploy leadgen_stage_errors
    rc_heal = proactive_audit.main(["--db", str(db_path), "--heal"])
    captured_heal = capsys.readouterr()
    assert rc_heal == 0, captured_heal.out + captured_heal.err

    # 3) --enforce-vps-readiness fails with exit 1 when isolated_scraper_tg_session / treg_cli missing
    rc_vps_fail = proactive_audit.main(["--db", str(db_path), "--enforce-vps-readiness"])
    captured_vps_fail = capsys.readouterr()
    assert rc_vps_fail == 1
    assert "isolated_scraper_tg_session" in captured_vps_fail.err
    assert "treg_cli" in captured_vps_fail.err

    # 4) CRMTG_VPS_RUNTIME=1 also enforces readiness automatically without CLI flag
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    rc_env_fail = proactive_audit.main(["--db", str(db_path)])
    captured_env_fail = capsys.readouterr()
    assert rc_env_fail == 1
    assert "isolated_scraper_tg_session" in captured_env_fail.err

    # 5) Once isolated session + treg CLI are provisioned -> exit 0 (stale stage_errors already cleared by --heal!)
    iso_sess = _create_isolated_mtproto_session(tmp_path / "scraper" / "signals_ro.session")
    bin_dir, treg_cfg = _create_fake_treg_env(tmp_path / "treg_env")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(iso_sess))
    monkeypatch.setenv("TREG_ENRICH", "1")
    monkeypatch.setenv("TREG_CONFIG_PATH", str(treg_cfg))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    rc_ok = proactive_audit.main(["--db", str(db_path), "--enforce-vps-readiness"])
    captured_ok = capsys.readouterr()
    assert rc_ok == 0, captured_ok.out + captured_ok.err


def test_proactive_audit_health_url_cli_exits_1_on_hollow_pipe_and_0_when_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    monkeypatch.setenv("RELAYFAST_API_KEY", "rf-live-key-123")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(tmp_path / "missing.session"))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(tmp_path / "empty_sessions"))
    monkeypatch.setenv("TREG_ENRICH", "0")

    (tmp_path / "leadgen_config.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "b2b": {"enabled": True, "sources": {"tg_ad_spy": True, "meta_ads": True}},
            }
        ),
        encoding="utf-8",
    )

    conn = connect(tmp_path / "live_http.db")
    init_db(conn)
    app = create_app(conn=conn)

    class _LoopbackProxyApp:
        def __init__(self, inner):
            self._inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                scope = dict(scope, client=("127.0.0.1", 50000))
            await self._inner(scope, receive, send)

    with TestClient(_LoopbackProxyApp(app)) as client:

        class _ProxyToAppHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                resp = client.get(self.path)
                raw = resp.content
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):  # noqa: A002
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyToAppHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{port}/api/health?deep=1"
        try:
            # 1) Subprocess proactive_audit.py --health-url MUST exit 1 when leadgen_ready=False
            fail_proc = subprocess.run(
                [sys.executable, str(WORKTREE_ROOT / "scripts" / "proactive_audit.py"), "--health-url", url],
                capture_output=True,
                text=True,
                env={"PATH": "/usr/bin:/bin", "CRMTG_SKIP_DOTENV": "1"},
            )
            assert fail_proc.returncode == 1, fail_proc.stdout + fail_proc.stderr
            assert "isolated_scraper_tg_session" in fail_proc.stderr
            assert "treg_cli" in fail_proc.stderr

            # 2) Fix leadgen readiness, inject unhealed agency -> still exits 1
            iso_sess = _create_isolated_mtproto_session(tmp_path / "scraper" / "signals_ro.session")
            bin_dir, treg_cfg = _create_fake_treg_env(tmp_path / "treg_env")
            monkeypatch.setenv("SCRAPER_TG_SESSION", str(iso_sess))
            monkeypatch.setenv("TREG_ENRICH", "1")
            monkeypatch.setenv("TREG_CONFIG_PATH", str(treg_cfg))
            monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

            create_lead(
                conn,
                name="Unhealed Marketing Agency",
                company="Unhealed Marketing Agency",
                niche="Агентство",
                lead_source="Cold outreach",
                username="unhealed_agency",
                telegram_user_id=998877,
            )
            hygiene_fail_proc = subprocess.run(
                [sys.executable, str(WORKTREE_ROOT / "scripts" / "proactive_audit.py"), "--health-url", url],
                capture_output=True,
                text=True,
                env={"PATH": "/usr/bin:/bin", "CRMTG_SKIP_DOTENV": "1"},
            )
            assert hygiene_fail_proc.returncode == 1, hygiene_fail_proc.stdout + hygiene_fail_proc.stderr
            assert "icp_hygiene_ok" in hygiene_fail_proc.stderr

            # 3) Heal real hygiene -> exits 0!
            heal_crm_hygiene(conn)
            assert audit_crm_hygiene(conn)["ok"] is True
            assert audit_server_leadgen_readiness(conn)["ok"] is True
            ok_proc = subprocess.run(
                [sys.executable, str(WORKTREE_ROOT / "scripts" / "proactive_audit.py"), "--health-url", url],
                capture_output=True,
                text=True,
                env={"PATH": "/usr/bin:/bin", "CRMTG_SKIP_DOTENV": "1"},
            )
            assert ok_proc.returncode == 0, ok_proc.stdout + ok_proc.stderr
        finally:
            server.shutdown()
            server.server_close()
    conn.close()


def test_systemd_deploy_workflow_and_verify_sh_gate_e(tmp_path: Path):
    for svc_name in ("crmtg.service", "crmtg-leadgen.service"):
        svc_text = (WORKTREE_ROOT / "deploy" / "systemd" / svc_name).read_text(encoding="utf-8")
        assert 'Environment="HOME=/root"' in svc_text
        assert 'Environment="PATH=/opt/crmtg/.venv/bin:/usr/local/bin:/usr/bin:/bin"' in svc_text
        assert 'Environment="CRMTG_VPS_RUNTIME=1"' in svc_text

    wf_text = (WORKTREE_ROOT / ".github" / "workflows" / "deploy-vps.yml").read_text(encoding="utf-8")
    assert 'proactive_audit.py" --db "$APP_DIR/data/crm.db" --heal || true' in wf_text
    assert 'd.get("icp_hygiene_ok") is True and d.get("leadgen_ready") is True' in wf_text

    sandbox = tmp_path / "gate_e_sandbox"
    for sub in ("scripts", "app/static", "leadgen", "tests", "deploy/systemd", ".github/workflows"):
        (sandbox / sub).mkdir(parents=True, exist_ok=True)
    for link_dir in (".claude", "replica"):
        (sandbox / link_dir).symlink_to(WORKTREE_ROOT / link_dir)
    if (WORKTREE_ROOT / ".venv").exists():
        (sandbox / ".venv").symlink_to((WORKTREE_ROOT / ".venv").resolve())

    verify_src = (WORKTREE_ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
    sb_verify = sandbox / "scripts" / "verify.sh"
    sb_verify.write_text(verify_src, encoding="utf-8")
    sb_verify.chmod(0o755)
    (sandbox / "app" / "static" / "app.js").write_text("const ok = 1;\n", encoding="utf-8")
    (sandbox / "leadgen" / "__init__.py").write_text("", encoding="utf-8")
    (sandbox / "leadgen" / "vps_autonomous_replenish.py").write_text(
        "import argparse\nif __name__ == '__main__': argparse.ArgumentParser().parse_args()\n",
        encoding="utf-8",
    )
    (sandbox / "tests" / "test_ok.py").write_text("def test_x(): assert True\n", encoding="utf-8")

    (sandbox / "app" / "main.py").write_text("def api_health():\n    return {'ok': True}\n", encoding="utf-8")
    (sandbox / "scripts" / "proactive_audit.py").write_text(
        "import argparse\nif __name__ == '__main__': argparse.ArgumentParser().parse_args()\n",
        encoding="utf-8",
    )
    (sandbox / ".github" / "workflows" / "deploy-vps.yml").write_text("name: deploy-vps\n", encoding="utf-8")
    (sandbox / "deploy" / "systemd" / "crmtg.service").write_text(
        '[Service]\nEnvironment="CRMTG_VPS_RUNTIME=1"\nExecStart=/opt/crmtg/.venv/bin/python -m leadgen.vps_autonomous_replenish\n',
        encoding="utf-8",
    )
    (sandbox / "deploy" / "systemd" / "crmtg-leadgen.service").write_text(
        '[Service]\nEnvironment="CRMTG_VPS_RUNTIME=1"\nExecStart=/opt/crmtg/.venv/bin/python -m leadgen.vps_autonomous_replenish\n',
        encoding="utf-8",
    )
    (sandbox / "app" / "hygiene.py").write_text(
        "def audit_crm_hygiene(conn): return {'ok': True}\n",
        encoding="utf-8",
    )

    bad = subprocess.run([str(sb_verify), "--fast-checks-only"], cwd=str(sandbox), capture_output=True, text=True)
    assert bad.returncode == 1
    assert "Gate E" in bad.stderr

    (sandbox / "app" / "main.py").write_text(
        "def api_health():\n    return {'icp_hygiene_ok': True, 'leadgen_ready': True}\n",
        encoding="utf-8",
    )
    (sandbox / "scripts" / "proactive_audit.py").write_text(
        "import argparse\nCHECKS = ('icp_hygiene_ok', 'leadgen_ready', 'missing_components')\n"
        "if __name__ == '__main__': argparse.ArgumentParser().parse_args()\n",
        encoding="utf-8",
    )
    (sandbox / ".github" / "workflows" / "deploy-vps.yml").write_text(
        'run: d.get("icp_hygiene_ok") is True and d.get("leadgen_ready") is True\n',
        encoding="utf-8",
    )
    for svc_name in ("crmtg.service", "crmtg-leadgen.service"):
        (sandbox / "deploy" / "systemd" / svc_name).write_text(
            '[Service]\nEnvironment="HOME=/root"\n'
            'Environment="PATH=/opt/crmtg/.venv/bin:/usr/local/bin:/usr/bin:/bin"\n'
            'Environment="CRMTG_VPS_RUNTIME=1"\n'
            "ExecStart=/opt/crmtg/.venv/bin/python -m leadgen.vps_autonomous_replenish\n",
            encoding="utf-8",
        )
    (sandbox / "app" / "hygiene.py").write_text(
        "def audit_crm_hygiene(conn): return {'ok': True}\n"
        "def heal_crm_hygiene(conn): return {}\n"
        "def audit_server_leadgen_readiness(conn, *, enforce_vps_checks=None): return {'ok': True}\n",
        encoding="utf-8",
    )

    good = subprocess.run([str(sb_verify), "--fast-checks-only"], cwd=str(sandbox), capture_output=True, text=True)
    assert good.returncode == 0, good.stdout + good.stderr
