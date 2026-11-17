from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType

from fastapi.testclient import TestClient
import pytest

from app.db import connect, init_db, utc_now
from app.leads import create_lead
from app.main import autopilot_loop, create_app
from app.outreach import create_campaign
import scripts.proactive_audit as proactive_audit

WORKTREE_ROOT = Path(__file__).resolve().parents[1]


def _build_contract_hygiene_module(state: dict) -> ModuleType:
    """Real SQLite-backed implementation of the app.hygiene contract for harness tests."""
    mod = ModuleType("app.hygiene")

    def audit_crm_hygiene(conn) -> dict:
        state["audit_hygiene_calls"] = state.get("audit_hygiene_calls", 0) + 1
        junk_rec = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM campaign_recipients
            WHERE status = 'pending' AND lower(coalesce(name, '')) LIKE '%agency%'
            """
        ).fetchone()["cnt"]
        junk_leads = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM leads
            WHERE status NOT IN ('lost', 'quarantined')
              AND lower(coalesce(name, '')) LIKE '%agency%'
            """
        ).fetchone()["cnt"]
        contactless = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM leads
            WHERE status NOT IN ('lost', 'quarantined', 'won')
              AND telegram_user_id IS NULL
              AND coalesce(trim(username), '') = ''
              AND coalesce(trim(phone), '') = ''
            """
        ).fetchone()["cnt"]
        dirty_titles = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM leads
            WHERE coalesce(name, '') LIKE '%|||%'
            """
        ).fetchone()["cnt"]
        total_bad = int(junk_rec) + int(junk_leads) + int(contactless) + int(dirty_titles)
        return {
            "ok": total_bad == 0,
            "active_junk_recipient_count": int(junk_rec),
            "active_junk_lead_count": int(junk_leads),
            "contactless_lead_count": int(contactless),
            "dirty_title_lead_count": int(dirty_titles),
        }

    def heal_crm_hygiene(conn) -> dict:
        state["heal_calls"] = state.get("heal_calls", 0) + 1
        cur_rec = conn.execute(
            """
            UPDATE campaign_recipients
            SET status = 'failed', error_text = 'quarantined_non_icp'
            WHERE status = 'pending' AND lower(coalesce(name, '')) LIKE '%agency%'
            """
        )
        cur_leads = conn.execute(
            """
            UPDATE leads
            SET status = 'lost'
            WHERE status NOT IN ('lost', 'quarantined', 'won')
              AND (
                  lower(coalesce(name, '')) LIKE '%agency%'
                  OR (
                      telegram_user_id IS NULL
                      AND coalesce(trim(username), '') = ''
                      AND coalesce(trim(phone), '') = ''
                  )
              )
            """
        )
        cur_titles = conn.execute(
            """
            UPDATE leads
            SET name = replace(name, '|||', ' ')
            WHERE coalesce(name, '') LIKE '%|||%'
            """
        )
        conn.commit()
        return {
            "quarantined_recipients": int(cur_rec.rowcount or 0),
            "quarantined_leads": int(cur_leads.rowcount or 0),
            "sanitized_leads": int(cur_titles.rowcount or 0),
        }

    def audit_server_leadgen_readiness(
        conn, *, enforce_vps_checks: bool | None = None
    ) -> dict:
        state["audit_readiness_calls"] = state.get("audit_readiness_calls", 0) + 1
        enforced = (
            bool(enforce_vps_checks)
            if enforce_vps_checks is not None
            else bool(state.get("default_enforced", False))
        )
        missing: list[str] = []
        scraper_ready = bool(state.get("scraper_session_ready", True))
        treg_ready = bool(state.get("treg_ready", True))
        llm_ready = bool(state.get("llm_key_ready", True))
        stage_timeouts = 0
        row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'leadgen_stage_errors'"
        ).fetchone()
        if row and row["value"] and "timeout" in str(row["value"]).lower():
            stage_timeouts = 1
        if enforced:
            if not llm_ready:
                missing.append("LLM_API_KEY")
            if not scraper_ready:
                missing.append("SCRAPER_TG_SESSION")
            if not treg_ready:
                missing.append("treg")
            if stage_timeouts > 0:
                missing.append("stage_timeout:tg_enrich_leads")
        return {
            "ok": len(missing) == 0,
            "enforced": enforced,
            "leadgen_enabled": True,
            "llm_key_ready": llm_ready,
            "scraper_session_required": enforced,
            "scraper_session_ready": scraper_ready,
            "treg_required": enforced,
            "treg_ready": treg_ready,
            "typesafe_ready": True,
            "stage_timeout_count": stage_timeouts,
            "missing_components": missing,
        }

    setattr(mod, "audit_crm_hygiene", audit_crm_hygiene)
    setattr(mod, "heal_crm_hygiene", heal_crm_hygiene)
    setattr(mod, "audit_server_leadgen_readiness", audit_server_leadgen_readiness)
    return mod


def test_deep_health_and_startup_and_autopilot_heal_crm_hygiene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = {
        "default_enforced": True,
        "scraper_session_ready": False,
        "treg_ready": False,
        "llm_key_ready": True,
    }
    monkeypatch.setitem(sys.modules, "app.hygiene", _build_contract_hygiene_module(state))

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
        steps=[{"template_text": "Привет! Делаем системный SoftUGC для аппок."}],
    )
    conn.execute("UPDATE campaigns SET status = 'running' WHERE id = ?", (camp["id"],))
    junk_lead = create_lead(
        conn,
        name="SMM Agency Studio",
        username="smm_agency_owner",
        telegram_user_id=445566,
        status="new",
        account_id=acc_id,
    )
    create_lead(
        conn,
        name="Contactless Ghost",
        username=None,
        telegram_user_id=None,
        phone=None,
        status="new",
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name,
            status, current_step, created_at, updated_at
        ) VALUES (?, ?, 445566, 'smm_agency_owner', 'SMM Agency Studio', 'pending', 0, ?, ?)
        """,
        (camp["id"], junk_lead["id"], now, now),
    )
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('leadgen_stage_errors', '{"tg_enrich_leads": "timeout_after_180s"}', ?)
        """,
        (now,),
    )
    conn.commit()

    sent_messages: list[tuple[int, str]] = []

    class _SpyGateway:
        async def send_message(self, session_path, user_id, text, username=None):
            sent_messages.append((int(user_id), text))
            return {"telegram_message_id": 701, "sent_at": utc_now()}

    # 1) create_app() runs heal_crm_hygiene(conn) on startup!
    app = create_app(conn=conn, gateway=_SpyGateway())
    assert state.get("heal_calls", 0) >= 1
    rec_status = conn.execute(
        "SELECT status FROM campaign_recipients WHERE lead_id = ?", (junk_lead["id"],)
    ).fetchone()["status"]
    assert rec_status == "failed"

    # 2) Inject new runtime junk after startup and verify /api/health?deep=1 reports broken state
    late_junk = create_lead(
        conn,
        name="Traffic Agency Pro",
        username="traffic_agency_pro",
        telegram_user_id=778899,
        status="new",
        account_id=acc_id,
    )
    create_lead(
        conn,
        name="Empty Lead After Boot",
        username=None,
        telegram_user_id=None,
        phone=None,
        status="new",
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name,
            status, current_step, created_at, updated_at
        ) VALUES (?, ?, 778899, 'traffic_agency_pro', 'Traffic Agency Pro', 'pending', 0, ?, ?)
        """,
        (camp["id"], late_junk["id"], now, now),
    )
    conn.commit()

    with TestClient(app) as client:
        broken_resp = client.get("/api/health?deep=1")
        assert broken_resp.status_code == 200
        broken = broken_resp.json()
        assert broken["icp_hygiene_ok"] is False
        assert broken["active_junk_recipients"] == 1
        assert broken["active_junk_leads"] == 1
        assert broken["contactless_leads"] == 1
        assert broken["leadgen_ready"] is False
        assert set(broken["leadgen_readiness"]["missing_components"]) == {
            "SCRAPER_TG_SESSION",
            "treg",
            "stage_timeout:tg_enrich_leads",
        }

        # 3) autopilot_loop heals BEFORE run_autopilot_tick so 0 messages are sent to junk recipient
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
        assert late_rec_status == "failed"

        # 4) Fix server readiness components -> /api/health?deep=1 flips to green
        state["scraper_session_ready"] = True
        state["treg_ready"] = True
        conn.execute("DELETE FROM kv_settings WHERE key = 'leadgen_stage_errors'")
        conn.commit()

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


def test_direct_leads_endpoint_requires_auth_and_caps_at_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True)
    jsonl_file = state_dir / "direct_x_leads.jsonl"
    raw_lines = [json.dumps({"idx": i, "x_handle": f"@founder_{i}"}) for i in range(205)]
    raw_lines.insert(10, "")
    raw_lines.insert(20, "{corrupted-json-line")
    jsonl_file.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MCP_API_KEY", "test-mcp-secret-key-999")

    conn = connect(tmp_path / "auth_direct.db")
    init_db(conn)
    app = create_app(conn=conn)
    with TestClient(app) as client:
        unauth = client.get("/api/leadgen/direct-leads")
        assert unauth.status_code == 401

        authed = client.get(
            "/api/leadgen/direct-leads",
            headers={"X-MCP-API-Key": "test-mcp-secret-key-999"},
        )
        assert authed.status_code == 200
        body = authed.json()
        assert body["ok"] is True
        assert body["count"] == 205
        assert len(body["items"]) == 200
        assert body["items"][0]["idx"] == 5
        assert body["items"][-1]["idx"] == 204
    conn.close()


def test_proactive_audit_db_heal_and_vps_readiness_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    state = {
        "default_enforced": False,
        "scraper_session_ready": False,
        "treg_ready": False,
        "llm_key_ready": True,
    }
    monkeypatch.setitem(sys.modules, "app.hygiene", _build_contract_hygiene_module(state))
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)

    db_path = tmp_path / "audit_hygiene.db"
    conn = connect(db_path)
    init_db(conn)
    create_lead(conn, name="Dirty Agency Lead", username="dirty_agency", telegram_user_id=12345)
    create_lead(conn, name="Bot Lead", username="helper_notify_bot", status="new")
    conn.close()

    # 1) Dirty DB fails with exit code 1
    rc_dirty = proactive_audit.main(["--db", str(db_path)])
    captured_dirty = capsys.readouterr()
    assert rc_dirty == 1
    assert "PROACTIVE AUDIT FAILED" in captured_dirty.err

    # 2) --heal repairs both reply notifications / bot leads AND crm_hygiene
    rc_heal = proactive_audit.main(["--db", str(db_path), "--heal"])
    captured_heal = capsys.readouterr()
    assert rc_heal == 0, captured_heal.out + captured_heal.err

    # 3) --enforce-vps-readiness fails with exit 1 when SCRAPER_TG_SESSION / treg missing
    rc_vps_fail = proactive_audit.main(["--db", str(db_path), "--enforce-vps-readiness"])
    captured_vps_fail = capsys.readouterr()
    assert rc_vps_fail == 1
    assert "SCRAPER_TG_SESSION" in captured_vps_fail.err
    assert "treg" in captured_vps_fail.err

    # 4) CRMTG_VPS_RUNTIME=1 also enforces readiness automatically without CLI flag
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    rc_env_fail = proactive_audit.main(["--db", str(db_path)])
    captured_env_fail = capsys.readouterr()
    assert rc_env_fail == 1
    assert "SCRAPER_TG_SESSION" in captured_env_fail.err

    # 5) Once server readiness components are restored -> exit 0
    state["scraper_session_ready"] = True
    state["treg_ready"] = True
    rc_ok = proactive_audit.main(["--db", str(db_path), "--enforce-vps-readiness"])
    assert rc_ok == 0


def test_proactive_audit_health_url_cli_exits_1_on_hollow_pipe_and_0_when_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = {
        "default_enforced": True,
        "scraper_session_ready": False,
        "treg_ready": False,
        "llm_key_ready": True,
    }
    monkeypatch.setitem(sys.modules, "app.hygiene", _build_contract_hygiene_module(state))

    conn = connect(tmp_path / "live_http.db")
    init_db(conn)
    app = create_app(conn=conn)

    with TestClient(app) as client:

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
            assert "SCRAPER_TG_SESSION" in fail_proc.stderr
            assert "treg" in fail_proc.stderr

            # 2) Fix leadgen readiness, break ICP hygiene -> still exits 1
            state["scraper_session_ready"] = True
            state["treg_ready"] = True
            create_lead(conn, name="Unhealed Agency", username="unhealed_agency", telegram_user_id=998877)
            hygiene_fail_proc = subprocess.run(
                [sys.executable, str(WORKTREE_ROOT / "scripts" / "proactive_audit.py"), "--health-url", url],
                capture_output=True,
                text=True,
                env={"PATH": "/usr/bin:/bin", "CRMTG_SKIP_DOTENV": "1"},
            )
            assert hygiene_fail_proc.returncode == 1, hygiene_fail_proc.stdout + hygiene_fail_proc.stderr
            assert "icp_hygiene_ok" in hygiene_fail_proc.stderr

            # 3) Heal hygiene -> exits 0!
            sys.modules["app.hygiene"].heal_crm_hygiene(conn)
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

    # Sandbox verification for Gate E in scripts/verify.sh
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

    # Broken Gate E files: missing icp_hygiene_ok / leadgen_ready / PATH=/usr/local/bin / hygiene defs
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
    assert "app/main.py" in bad.stderr
    assert "scripts/proactive_audit.py" in bad.stderr
    assert "deploy-vps.yml" in bad.stderr
    assert "crmtg-leadgen.service" in bad.stderr
    assert "audit_server_leadgen_readiness" in bad.stderr

    # Fix all Gate E invariants (including full app/hygiene.py contract) -> MUST exit 0
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
