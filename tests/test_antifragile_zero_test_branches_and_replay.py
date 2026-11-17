import asyncio
import importlib
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import CANONICAL_STEP3_IMAGE_URL, connect, init_db, utc_now
from app.inbox import apply_incoming, open_dialog_by_lead
from app.main import (
    autopilot_loop,
    create_app,
    ensure_private_inbox,
    start_account_watch,
    start_connected_watches,
    stop_inbox_watches,
    sync_live_tg_read_receipts_once,
)
import app.outreach as outreach_mod
from app.outreach import (
    create_campaign,
    run_autopilot_tick,
    stop_outreach_on_reply,
    update_campaign,
)
from leadgen.crm_mcp_push import push_leads_via_mcp
from leadgen.job_reply import build_default_followups, build_vacancy_mcp_lead
from leadgen.outreach_copy import (
    REVSHARE_OFFER_TEXT,
    SOFTUGC_PITCH_TEXT,
    VLAD_STEP2_TEMPLATE,
    VLAD_STEP3_TEXT,
    first_touch,
)

STREAM_C_CORE_APP_FILES = (
    "app/main.py",
    "app/db.py",
    "app/cloud_persistence.py",
    "app/airtable_sync.py",
    "app/crm_auth.py",
    "app/whatsapp_gateway.py",
    "app/mcp_server.py",
)


def test_zero_pytest_current_test_and_sys_modules_in_core_app_modules():
    runtime_cfg_path = ROOT / "app" / "runtime_config.py"
    conftest_path = ROOT / "tests" / "conftest.py"
    assert runtime_cfg_path.exists(), "app/runtime_config.py must exist"
    assert conftest_path.exists(), "tests/conftest.py must exist"

    runtime_cfg = importlib.import_module("app.runtime_config")
    assert callable(getattr(runtime_cfg, "should_skip_dotenv", None))
    assert callable(getattr(runtime_cfg, "is_ephemeral_runtime", None))

    conftest_src = conftest_path.read_text(encoding="utf-8")
    assert "CRMTG_SKIP_DOTENV" in conftest_src
    assert "CRMTG_EPHEMERAL_RUNTIME" in conftest_src

    violations: list[str] = []
    forbidden_markers = (
        "PYTEST_CURRENT_TEST",
        '"pytest" in sys.modules',
        "'pytest' in sys.modules",
        '"pytest" not in sys.modules',
        "'pytest' not in sys.modules",
    )
    for rel_path in STREAM_C_CORE_APP_FILES:
        abs_path = ROOT / rel_path
        src = abs_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            for marker in forbidden_markers:
                if marker in line:
                    violations.append(f"{rel_path}:{lineno}: {line.strip()}")

    assert violations == [], (
        "Split-brain pytest checks found in production modules:\n" + "\n".join(violations)
    )


def test_dead_session_health_gate_blocks_watches_inbox_and_receipts_even_when_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    conn = connect(tmp_path / "health_gate.db")
    init_db(conn)
    now = utc_now()

    # Exact morning bug reproduction: accounts 21 (unauthorized) and 28 (banned) still have status='connected'
    conn.executemany(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, provider, daily_limit, created_at, updated_at)
        VALUES (?, ?, ?, 'connected', ?, 'telegram', 20, ?, ?)
        """,
        [
            (21, "+998901112221", "data/sessions/acc21.session", "unauthorized", now, now),
            (28, "+998901112228", "data/sessions/acc28.session", "banned", now, now),
            (30, "+998901112230", "data/sessions/acc30.session", "active", now, now),
        ],
    )
    lead_id = int(
        conn.execute(
            """
            INSERT INTO leads (account_id, telegram_user_id, username, name, status, created_at, updated_at)
            VALUES (30, 77030, 'healthy_peer', 'Healthy Peer', 'talking', ?, ?)
            """,
            (now, now),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            lead_id, provider, created_at, updated_at
        ) VALUES (30, 'user', 77030, 'healthy_peer', 'Healthy Peer', 0, 1, ?, 'Hi', 1, ?, 'telegram', ?, ?)
        """,
        (now, lead_id, now, now),
    )
    conn.commit()

    dead_sessions = {"data/sessions/acc21.session", "data/sessions/acc28.session"}
    watch_calls: list[str] = []
    inbox_calls: list[str] = []

    class _StrictHealthSpyGateway:
        def __init__(self) -> None:
            self._ready_events: dict[str, asyncio.Event] = {}

        def _listening_event(self, session_path: str) -> asyncio.Event:
            ev = self._ready_events.get(session_path)
            if ev is None:
                ev = asyncio.Event()
                self._ready_events[session_path] = ev
            return ev

        async def load_inbox(self, session_path: str, limit: int = 30) -> list[dict]:
            s_path = str(session_path)
            inbox_calls.append(s_path)
            if s_path in dead_sessions:
                raise AssertionError(f"load_inbox called on dead session {s_path}!")
            return [
                {
                    "telegram_user_id": 77030,
                    "username": "healthy_peer",
                    "name": "Healthy Peer",
                    "unread_count": 0,
                    "last_from_peer": 1,
                    "last_message_preview": "Read",
                }
            ]

        async def watch_private(self, session_path: str, on_message, **_kwargs) -> None:
            s_path = str(session_path)
            watch_calls.append(s_path)
            if s_path in dead_sessions:
                raise AssertionError(f"watch_private called on dead session {s_path}!")
            from app.telegram_gateway import listening_event

            listening_event(self, s_path).set()
            await asyncio.Event().wait()

        async def close_all(self) -> None:
            return None

    gw = _StrictHealthSpyGateway()
    monkeypatch.setenv("ENABLE_TG_PERSISTENT_WATCHES", "1")
    monkeypatch.delenv("DISABLE_TG_WATCHES", raising=False)
    app = create_app(conn=conn, gateway=gw)

    slo_watchdog_calls: list[int] = []

    def _fake_slo_watchdog(db_conn):
        slo_watchdog_calls.append(1)
        return {"ok": True}

    monkeypatch.setattr(outreach_mod, "check_outreach_slo_watchdog", _fake_slo_watchdog, raising=False)
    done_10_ticks = asyncio.Event()
    tick_seen = {"n": 0}

    async def _fast_autopilot_tick(*_a, **_kw):
        tick_seen["n"] += 1
        if tick_seen["n"] >= 10:
            done_10_ticks.set()
        return {"ok": True, "sent": 0}

    import app.main as main_mod

    monkeypatch.setattr(main_mod, "run_autopilot_tick", _fast_autopilot_tick)

    async def _exercise() -> None:
        await ensure_private_inbox(app, 21)
        await ensure_private_inbox(app, 28)
        await start_account_watch(app, 21)
        await start_account_watch(app, 28)
        await start_connected_watches(app)

        assert 21 not in app.state.inbox_tasks
        assert 28 not in app.state.inbox_tasks
        assert 30 in app.state.inbox_tasks
        assert watch_calls == ["data/sessions/acc30.session"]

        r21 = await sync_live_tg_read_receipts_once(app, account_id=21)
        r28 = await sync_live_tg_read_receipts_once(app, account_id=28)
        r_all = await sync_live_tg_read_receipts_once(app)
        assert r21["synced_accounts"] == 0
        assert r28["synced_accounts"] == 0
        assert r_all["synced_accounts"] == 1

        # Simulate production background read-receipts + SLO watchdog in autopilot_loop
        monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "0")
        loop_task = asyncio.create_task(autopilot_loop(app, interval_seconds=0.001))
        try:
            await asyncio.wait_for(done_10_ticks.wait(), timeout=2.0)
            await asyncio.sleep(0.01)
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
            await stop_inbox_watches(app)

    try:
        asyncio.run(_exercise())
    finally:
        conn.close()

    assert not (set(watch_calls) & dead_sessions)
    assert not (set(inbox_calls) & dead_sessions)
    assert len(slo_watchdog_calls) >= 1, "autopilot_loop must invoke check_outreach_slo_watchdog every 10 ticks"


def test_deep_health_endpoint_exposes_queue_telemetry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISABLE_TG_WATCHES", "1")
    conn = connect(tmp_path / "deep_health.db")
    init_db(conn)
    now = utc_now()

    conn.executemany(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, provider, created_at)
        VALUES (?, ?, ?, 'connected', ?, 'telegram', ?)
        """,
        [
            (21, "+998900000021", "data/sessions/21.session", "unauthorized", now),
            (28, "+998900000028", "data/sessions/28.session", "banned", now),
            (31, "+998900000031", "data/sessions/31.session", "active", now),
        ],
    )
    c_run_1 = create_campaign(
        conn,
        name="Running B2B SoftUGC",
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hi"}],
    )["id"]
    c_run_2 = create_campaign(
        conn,
        name="Running TG Вакансии",
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Добрый день"}],
    )["id"]
    c_paused = create_campaign(
        conn,
        name="Paused Archive",
        status="paused",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Paused"}],
    )["id"]

    conn.executemany(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, status, current_step, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # Running campaign 1: 2 untouched pending (current_step=0), 1 in_progress (step=1), 1 pending (step=1)
            (c_run_1, 50101, "lead_r1_a", "pending", 0, now, now),
            (c_run_1, 50102, "lead_r1_b", "pending", 0, now, now),
            (c_run_1, 50103, "lead_r1_c", "in_progress", 1, now, now),
            (c_run_1, 50104, "lead_r1_d", "pending", 1, now, now),
            # Running campaign 2: 1 untouched pending (current_step=0)
            (c_run_2, 50201, "lead_r2_a", "pending", 0, now, now),
            # Paused campaign: 4 untouched pending -> MUST NOT count toward running pending_step1_total
            (c_paused, 50301, "lead_p_a", "pending", 0, now, now),
            (c_paused, 50302, "lead_p_b", "pending", 0, now, now),
            (c_paused, 50303, "lead_p_c", "pending", 0, now, now),
            (c_paused, 50304, "lead_p_d", "pending", 0, now, now),
        ],
    )
    conn.commit()

    app = create_app(conn=conn)
    try:
        with TestClient(app) as client:
            shallow = client.get("/api/health").json()
            assert shallow == {"ok": True, "status": "healthy", "db_ok": True}

            deep_resp = client.get("/api/health?deep=1")
            assert deep_resp.status_code == 200
            body = deep_resp.json()
            assert body["ok"] is True
            assert body["active_accounts"] == 3
            assert body["healthy_accounts"] == 1
            assert body["running_campaigns"] == 2
            assert body["pending_step1_total"] == 3
    finally:
        conn.close()


def test_prod_shape_replay_all_three_archetypes_under_production_runtime_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "0")
    monkeypatch.setenv("CLOUD_BACKUP_ENABLED", "0")
    monkeypatch.setenv("DISABLE_TG_WATCHES", "1")
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    runtime_cfg = importlib.import_module("app.runtime_config")
    assert runtime_cfg.is_ephemeral_runtime() is False

    conn = connect(tmp_path / "prod_replay.db")
    init_db(conn)
    now = utc_now()

    # Mix dead-but-connected accounts (21, 28) with healthy TG (31) and healthy WhatsApp (32)
    conn.executemany(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, provider,
            telegram_user_id, telegram_username, telegram_name,
            daily_limit, sent_today, created_at, updated_at
        ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, ?, 25, 0, ?, ?)
        """,
        [
            (21, "+998901000021", "data/sessions/dead21.session", "unauthorized", "telegram", 1021, "dead_21", "Dead 21", now, now),
            (28, "+998901000028", "data/sessions/dead28.session", "banned", "telegram", 1028, "dead_28", "Dead 28", now, now),
            (31, "+998901000031", "data/sessions/live31.session", "active", "telegram", 1031, "vlad_live", "Vlad SoftUGC", now, now),
            (32, "+491510000032", "data/sessions/wa32.session", "active", "whatsapp", 491510000032, "wa_live", "WA Live", now, now),
            (36, "+998901000036", "data/sessions/vac36.session", "active", "telegram", 1036, "vac_dedicated", "Dedicated Vac", now, now),
        ],
    )
    conn.commit()

    # Archetype 1: B2B SoftUGC (canonical 3-step sequence)
    b2b_step1 = first_touch(
        "PulseNote AI",
        "AI meeting copilot for B2B SaaS teams",
        category="SaaS",
        site="https://pulsenote.ai",
    )
    b2b_camp = create_campaign(
        conn,
        name="SoftUGC — Auto Leadgen B2B",
        account_ids=[21, 28, 31],
        status="running",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
            {"step_order": 3, "delay_hours": 24, "template_text": VLAD_STEP3_TEXT, "image_url": CANONICAL_STEP3_IMAGE_URL},
        ],
    )
    b2b_cid = int(b2b_camp["id"])
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category, their_angle,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, 880001, 'pulsenote_ceo', 'Alex Founder', 'PulseNote AI', 'SaaS',
                  'AI meeting copilot for B2B SaaS teams', ?, 0, 'pending', ?, ?)
        """,
        (b2b_cid, b2b_step1, now, now),
    )
    conn.commit()

    # Archetype 2: TG Вакансии via canonical MCP push
    vacancy = {
        "role": "AI-креатор Reels/Shorts",
        "why": "Производство вертикальных AI UGC роликов под ключ",
        "pay": "от 80 000 руб./мес.",
        "contact": "@vacancy_hr_lead",
        "layer": "AI-контент",
        "mode": "отдать своим",
        "src": "AI Вакансии",
        "url": "https://t.me/ai_jobs_channel/404",
    }
    f1, f2 = build_default_followups(vacancy)
    vac_step1 = (
        "Добрый день! По вакансии AI-креатора Reels/Shorts.\n\n"
        "Собираем AI UGC ролики под ключ (SoftUGC, 183.5 млн просмотров за 90 дней). "
        "Какой объём роликов в месяц планируете?"
    )
    vac_mcp_item = build_vacancy_mcp_lead(
        vacancy,
        {"text": vac_step1, "followup_1": f1, "followup_2": f2},
        touch_mode="direct",
    )
    assert vac_mcp_item is not None
    vac_push = push_leads_via_mcp(
        [vac_mcp_item],
        enroll_in_campaign=True,
        campaign_name="TG Вакансии",
        conn=conn,
    )
    assert vac_push["ok"] is True
    vac_cid = int(vac_push["campaign_id"])
    # Dedicated vacancy account #36 (isolated from B2B account #31; dead #21/#28 still skipped)
    update_campaign(conn, vac_cid, status="running", account_id=36, account_ids=[36])

    # Archetype 3: WhatsApp 14-digit international number (+86 138 0013 8000 1 -> 14 digits)
    wa_camp = create_campaign(
        conn,
        name="WhatsApp Intl B2B",
        account_id=32,
        account_ids=[32],
        status="running",
        steps=[
            {
                "step_order": 1,
                "delay_hours": 0,
                "template_text": "Hey {name}, we run SoftUGC for international e-com brands. Open to 3 demo reels?",
            },
            {
                "step_order": 2,
                "delay_hours": 24,
                "template_text": "Following up on the 3 SoftUGC demo reels for {company} — want me to send them over?",
            },
        ],
    )
    wa_cid = int(wa_camp["id"])
    wa_lead_id = int(
        conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, phone, name, company, provider, status, created_at, updated_at
            ) VALUES (32, 86138001380001, '+86 (138) 0013-8000-1', 'Chen Wei', 'ShenzhenTech', 'whatsapp', 'new', ?, ?)
            """,
            (now, now),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, phone, telegram_user_id, name, company, current_step, status, created_at, updated_at
        ) VALUES (?, ?, '+86 (138) 0013-8000-1', 86138001380001, 'Chen Wei', 'ShenzhenTech', 0, 'pending', ?, ?)
        """,
        (wa_cid, wa_lead_id, now, now),
    )
    conn.commit()

    tg_sent: list[tuple[str, object, str]] = []
    wa_sent: list[tuple[str, object, str]] = []

    class _ProdReplayTgGateway:
        async def search_dialogs(self, session_path: str, query: str, limit: int = 1):
            assert "dead" not in str(session_path), f"Dead session touched in search_dialogs: {session_path}"
            uname = str(query).lstrip("@")
            uid = 880001 if uname == "pulsenote_ceo" else 880002
            return [{"telegram_user_id": uid, "username": uname}]

        async def session_health(self, session_path: str):
            assert "dead" not in str(session_path)
            return {"health": "active", "last_error": None}

        async def send_message(self, session_path: str, peer_target, text: str, reply_to=None, image_path=None):
            assert "dead" not in str(session_path), f"Dead session touched in send_message: {session_path}"
            assert "creatoros" not in text.lower(), f"Sacred Rule 1 violated in outgoing TG text: {text}"
            tg_sent.append((str(session_path), peer_target, text))
            return {"telegram_message_id": 9500 + len(tg_sent), "sent_at": now}

        async def close_all(self):
            return None

    class _ProdReplayWaGateway:
        async def send_message(self, session_path: str, peer_target, text: str, reply_to=None, image_path=None):
            assert "creatoros" not in text.lower(), f"Sacred Rule 1 violated in outgoing WA text: {text}"
            wa_sent.append((str(session_path), peer_target, text))
            return {"telegram_message_id": 9700 + len(wa_sent), "sent_at": now}

    tg_gw = _ProdReplayTgGateway()
    wa_gw = _ProdReplayWaGateway()
    app = create_app(conn=conn, gateway=tg_gw)
    app.state.wa_gateway = wa_gw

    try:
        with TestClient(app) as client:
            pre_health = client.get("/api/health?deep=1").json()
            assert pre_health["running_campaigns"] == 3
            assert pre_health["pending_step1_total"] == 3

            tick_out = asyncio.run(
                run_autopilot_tick(
                    conn,
                    gateway=tg_gw,
                    wa_gateway=wa_gw,
                    force=True,
                    jitter_range=None,
                    max_send_per_campaign=1,
                )
            )
            assert tick_out["sent"] == 3

            post_health = client.get("/api/health?deep=1").json()
            assert post_health["running_campaigns"] == 3
            assert post_health["pending_step1_total"] == 0

        # Verify all 3 archetypes dispatched cleanly through live healthy isolated accounts
        assert len(tg_sent) == 2
        assert {s[0] for s in tg_sent} == {
            "data/sessions/live31.session",
            "data/sessions/vac36.session",
        }
        b2b_texts = [s[2] for s in tg_sent if "PulseNote" in s[2] or SOFTUGC_PITCH_TEXT in s[2]]
        assert len(b2b_texts) == 1
        assert b2b_texts[0].endswith(REVSHARE_OFFER_TEXT)
        vac_texts = [s[2] for s in tg_sent if "AI-креатора Reels/Shorts" in s[2]]
        assert vac_texts == [vac_step1]

        assert len(wa_sent) == 1
        assert wa_sent[0][0] == "data/sessions/wa32.session"
        assert str(wa_sent[0][1]) == "86138001380001"
        assert "SoftUGC" in wa_sent[0][2]

        # Incoming reply on 14-digit WhatsApp number resolves full 14-digit peer_id and stops outreach
        opened_wa = open_dialog_by_lead(conn, wa_lead_id, 32)
        inc_wa = apply_incoming(
            conn,
            32,
            {
                "telegram_user_id": 86138001380001,
                "telegram_message_id": 9801,
                "name": "Chen Wei",
                "phone": "+86138001380001",
                "provider": "whatsapp",
                "text": "Sure, send over the 3 demo reels!",
                "direction": "in",
            },
        )
        assert int(inc_wa["lead_id"]) == wa_lead_id
        assert int(inc_wa["telegram_user_id"]) == 86138001380001
        stopped = stop_outreach_on_reply(conn, account_id=32, phone="+86138001380001", notify=False)
        assert stopped >= 0
        wa_rec_status = conn.execute(
            "SELECT status FROM campaign_recipients WHERE campaign_id = ?", (wa_cid,)
        ).fetchone()["status"]
        assert wa_rec_status == "replied"
    finally:
        conn.close()


def test_verify_sh_static_gates_and_skill_adversarial_skeptic_section(tmp_path: Path):
    import subprocess

    verify_path = ROOT / "scripts" / "verify.sh"
    verify_src = verify_path.read_text(encoding="utf-8")
    assert "PYTEST_CURRENT_TEST" in verify_src, "scripts/verify.sh must enforce Zero PYTEST_CURRENT_TEST Gate"
    assert "*.service" in verify_src, (
        "scripts/verify.sh must enforce Systemd Hollow-Pipe Gate on deploy/systemd/*.service"
    )
    assert "push_discovered_now" in verify_src, (
        "scripts/verify.sh must reject bare push_discovered_now in systemd ExecStart"
    )

    skill_src = (ROOT / ".agents" / "skills" / "crmtg-ops-and-sync" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "## 4. Mandatory Adversarial Skeptic & Self-Hardening Gate" in skill_src
    assert "./scripts/verify.sh" in skill_src
    assert "adversarial-skeptic" in skill_src
    assert "/api/health?deep=1" in skill_src

    # Live execution check of verify.sh static gates inside a minimal isolated repo layout
    sandbox = tmp_path / "sandbox_repo"
    for sub in ("scripts", "app/static", "leadgen", "tests", "deploy/systemd"):
        (sandbox / sub).mkdir(parents=True, exist_ok=True)
    for link_dir in (".claude", "replica"):
        (sandbox / link_dir).symlink_to(ROOT / link_dir)
    if (ROOT / ".venv").exists():
        (sandbox / ".venv").symlink_to((ROOT / ".venv").resolve())
    sb_verify = sandbox / "scripts" / "verify.sh"
    sb_verify.write_text(verify_src, encoding="utf-8")
    sb_verify.chmod(0o755)
    (sandbox / "app" / "static" / "app.js").write_text("const ok = 1;\n", encoding="utf-8")
    (sandbox / "app" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (sandbox / "leadgen" / "__init__.py").write_text("", encoding="utf-8")
    (sandbox / "leadgen" / "vps_autonomous_replenish.py").write_text(
        "import argparse\nif __name__ == '__main__': argparse.ArgumentParser().parse_args()\n",
        encoding="utf-8",
    )
    (sandbox / "tests" / "test_ok.py").write_text("def test_x(): assert True\n", encoding="utf-8")

    # 1) Hollow push_discovered_now + missing -m module + PYTEST_CURRENT_TEST + *_IN_TEST + missing CRMTG_VPS_RUNTIME=1 -> MUST exit 1
    (sandbox / "app" / "bad.py").write_text(
        "import os\nif 'PYTEST_CURRENT_TEST' in os.environ or os.getenv('FORCE_X_IN_TEST'): pass\n",
        encoding="utf-8",
    )
    (sandbox / "deploy" / "systemd" / "hollow.service").write_text(
        "[Service]\nExecStart=/opt/crmtg/.venv/bin/python -c 'push_discovered_now(conn)'\n",
        encoding="utf-8",
    )
    (sandbox / "deploy" / "systemd" / "missing_mod.service").write_text(
        "[Service]\nExecStart=/opt/crmtg/.venv/bin/python -m leadgen.ghost_nonexistent\n",
        encoding="utf-8",
    )
    bad_run = subprocess.run(
        [str(sb_verify), "--fast-checks-only"],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
    )
    assert bad_run.returncode == 1
    assert "Zero PYTEST_CURRENT_TEST Gate violation" in bad_run.stderr
    assert "Zero *_IN_TEST Env Gate violation" in bad_run.stderr
    assert "Systemd Runtime Gate violation" in bad_run.stderr
    assert "bare push_discovered_now without upstream discovery" in bad_run.stderr
    assert "missing repo module '-m leadgen.ghost_nonexistent'" in bad_run.stderr

    # 2) Clean repo without PYTEST_CURRENT_TEST / *_IN_TEST and with CRMTG_VPS_RUNTIME=1 + real -m module -> MUST exit 0
    (sandbox / "app" / "bad.py").unlink()
    (sandbox / "deploy" / "systemd" / "hollow.service").unlink()
    (sandbox / "deploy" / "systemd" / "missing_mod.service").unlink()
    (sandbox / "deploy" / "systemd" / "clean.service").write_text(
        '[Service]\nEnvironment="CRMTG_VPS_RUNTIME=1"\nExecStart=/opt/crmtg/.venv/bin/python -m leadgen.vps_autonomous_replenish\n',
        encoding="utf-8",
    )
    ok_run = subprocess.run(
        [str(sb_verify), "--fast-checks-only"],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
    )
    assert ok_run.returncode == 0, f"Expected clean verify.sh exit 0, stderr={ok_run.stderr}"

