from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))

from app.db import connect, init_db  # noqa: E402
from app.hygiene import audit_server_leadgen_readiness  # noqa: E402
from app.leadgen_settings import DEFAULT_B2B_STEPS, save_leadgen_config  # noqa: E402
from app.mcp_server import _handle_ingest_discovered_leads  # noqa: E402
from app.outreach import create_campaign  # noqa: E402
from leadgen import tg_channel_graph, vps_autonomous_replenish  # noqa: E402
from leadgen.crm_mcp_push import build_lead_payload_from_dict  # noqa: E402


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = tmp_path / "leadgen_config.json"
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    scraper_dir = tmp_path / "scraper_sessions"
    scraper_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(scraper_dir))
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.delenv("SCRAPER_TG_SESSION", raising=False)
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LEADGEN_WRITE_CASES_BANK", raising=False)

    db_path = tmp_path / "crm.db"
    conn = connect(db_path)
    init_db(conn)
    yield {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "conn": conn,
        "cfg_file": cfg_file,
        "state_dir": state_dir,
        "scraper_dir": scraper_dir,
    }
    conn.close()


def _write_valid_tg_session(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as sconn:
        sconn.execute(
            """
            CREATE TABLE sessions (
                dc_id INTEGER PRIMARY KEY,
                server_address TEXT,
                port INTEGER,
                auth_key BLOB,
                takeout_id INTEGER
            )
            """
        )
        sconn.execute(
            "INSERT INTO sessions (dc_id, server_address, port, auth_key, takeout_id) "
            "VALUES (2, '149.154.167.50', 443, ?, NULL)",
            (os.urandom(256),),
        )
        sconn.commit()
    return path


def test_run_autonomous_replenish_with_existing_ofm_review_file(isolated_env):
    conn = isolated_env["conn"]
    state_dir: Path = isolated_env["state_dir"]

    ofm_path = state_dir / "ofm_review_leads.jsonl"
    ofm_path.write_text(
        json.dumps({"username": "ofm_buyer_1"}) + "\n" + json.dumps({"username": "ofm_buyer_2"}) + "\n",
        encoding="utf-8",
    )

    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "sources": {
                    "buyer_catalogs": True,
                    "tg_groups": False,
                    "ofm_groups": False,
                    "facebook_groups": False,
                    "meta_ads": False,
                    "social_x_reddit": False,
                    "yc_ph": False,
                },
            },
            "vacancies": {"enabled": False},
        },
        sync_cases_file=False,
    )

    def noop_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=noop_runner)
    assert res["ok"] is True
    assert res["ofm_review_added"] == 0


def test_replenish_cli_unhandled_crash_writes_stage_errors_and_flips_readiness(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    conn = isolated_env["conn"]
    db_path: Path = isolated_env["db_path"]
    scraper_sess = _write_valid_tg_session(isolated_env["scraper_dir"] / "signals_ro.session")

    monkeypatch.setenv("SCRAPER_TG_SESSION", str(scraper_sess))
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    monkeypatch.setattr("app.hygiene._check_treg_ready", lambda: True)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(vps_autonomous_replenish, "run_autonomous_replenish", _boom)

    rc = vps_autonomous_replenish.main(["--db", str(db_path)])
    assert rc == 1

    err_row = conn.execute(
        "SELECT value FROM kv_settings WHERE key = 'leadgen_stage_errors'"
    ).fetchone()
    assert err_row is not None
    parsed = json.loads(err_row["value"])
    assert isinstance(parsed, list) and parsed
    assert parsed[0]["stage"] == "vps_autonomous_replenish_crash"
    assert parsed[0]["returncode"] == 1
    assert "simulated crash" in parsed[0]["error"]

    readiness = audit_server_leadgen_readiness(conn, enforce_vps_checks=True)
    assert readiness["ok"] is False
    assert "stage_errors" in readiness["missing_components"]


def test_master_ids_committed_only_after_qualify_succeeds(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    conn = isolated_env["conn"]
    state_dir: Path = isolated_env["state_dir"]
    master_jsonl = state_dir / "master_ids.jsonl"

    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    create_campaign(
        conn,
        name="SoftUGC — База Все2 (2 045 лидов)",
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "target_campaign_tg": "SoftUGC — База Все2 (2 045 лидов)",
                "sources": {
                    "buyer_catalogs": True,
                    "tg_groups": False,
                    "ofm_groups": False,
                    "facebook_groups": False,
                    "meta_ads": False,
                    "social_x_reddit": False,
                    "yc_ph": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "whop_content_rewards": False,
                    "tiktok_ads": False,
                    "tiktok_shop": False,
                    "app_stores": False,
                    "ats_hiring": False,
                    "ph_detailed": False,
                    "d2c_whatsapp": False,
                    "tg_identity_bridge": False,
                },
                "limits": {"external_limit": 0},
            },
            "vacancies": {"enabled": False},
        },
        sync_cases_file=False,
    )

    raw_lead = {
        "id": 90001,
        "username": "alice_founder",
        "name": "Alice Cooper",
        "project": "VoiceNoteAI",
        "company": "VoiceNoteAI",
        "project_url": "https://voicenote.ai",
        "bio": "Founder of VoiceNoteAI — paid AI meeting assistant ($19/mo subscription)",
        "what_they_do": "Paid AI voice note subscription app ($19/mo)",
        "category": "SaaS / AI app",
        "role": "Founder",
        "roles": ["Founder"],
        "source": "catalog_test",
    }

    fail_qualify = {"flag": True}

    def runner(cmd, **kwargs):
        cmd_str = " ".join(str(x) for x in cmd)
        if "buyer_catalogs.py" in cmd_str:
            out_p = Path(cmd[cmd.index("--output") + 1])
            out_p.write_text(json.dumps(raw_lead, ensure_ascii=False) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_enrich_leads.py" in cmd_str:
            inp_p = Path(cmd[cmd.index("--input") + 1])
            out_p = Path(cmd[cmd.index("--output") + 1])
            out_p.write_text(inp_p.read_text(encoding="utf-8"), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_llm_qualify.py" in cmd_str:
            if fail_qualify["flag"]:
                raise subprocess.TimeoutExpired(cmd, 180)
            inp_p = Path(cmd[cmd.index("--input") + 1])
            out_p = Path(cmd[cmd.index("--output") + 1])
            rows = [json.loads(ln) for ln in inp_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for r in rows:
                r["is_lead"] = True
                r["qualified"] = True
                r["icp_score"] = 90
                r["monetization"] = "subscription"
                r["short_form_fit"] = True
            out_p.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_curate.py" in cmd_str:
            inp_p = Path(cmd[cmd.index("--input") + 1])
            out_p = Path(cmd[cmd.index("--output") + 1])
            rows = [json.loads(ln) for ln in inp_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for r in rows:
                r["first_touch"] = (
                    "Hey Alice,\n\nSaw VoiceNoteAI. We run SoftUGC, bringing 183.5M views "
                    "in 90 days via AI short-form video on revshare. Want to see 3 sample reels?"
                )
            out_p.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # Run 1: tg_llm_qualify times out -> master_ids.jsonl MUST NOT record alice_founder
    res1 = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=runner)
    assert res1["ok"] is True
    assert res1["ingested_total"] == 0
    assert not master_jsonl.exists() or "alice_founder" not in master_jsonl.read_text(encoding="utf-8")

    # Run 2: tg_llm_qualify succeeds -> alice_founder re-enters, gets qualified, enters CRM AND master_ids.jsonl
    fail_qualify["flag"] = False
    res2 = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=runner)
    assert res2["ok"] is True
    assert res2["ingested_total"] == 1
    assert master_jsonl.is_file() and "alice_founder" in master_jsonl.read_text(encoding="utf-8")
    rec_rows = conn.execute(
        "SELECT username, status FROM campaign_recipients WHERE username = 'alice_founder'"
    ).fetchall()
    assert len(rec_rows) == 1
    assert rec_rows[0]["status"] == "pending"


def test_platform_hosts_do_not_false_dedup_and_project_url_wins_over_shortener(isolated_env):
    conn = isolated_env["conn"]
    create_campaign(
        conn,
        name="SoftUGC — База Все2 (2 045 лидов)",
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )

    batch = [
        {
            "username": "meannareid",
            "name": "Anna Reid",
            "company": "Empacer",
            "website": "https://www.producthunt.com/posts/empacer",
            "category": "SaaS / AI app",
            "their_angle": "Voice-first AI assistant for small business owners",
            "step1_text": (
                "Hey Anna,\n\nSaw Empacer on Product Hunt. We run SoftUGC (183.5M views in 90 days) "
                "using AI short-form video on revshare. Want 3 sample reels?"
            ),
        },
        {
            "username": "Yoikk12",
            "name": "Bruce",
            "company": "Sunlight Diary",
            "website": "https://www.producthunt.com/products/sunlight-diary-by-looove",
            "category": "SaaS / AI app",
            "their_angle": "Proactive personal health AI agent",
            "step1_text": (
                "Hey Bruce,\n\nSaw Sunlight Diary on Product Hunt. We run SoftUGC (183.5M views in 90 days) "
                "using AI short-form video on revshare. Want 3 sample reels?"
            ),
        },
        {
            "username": "whop_founder_a",
            "name": "Sam Fox",
            "company": "FundedFox",
            "website": "https://whop.com/discover/fundedfox/",
            "category": "Проп трейдинг",
            "their_angle": "Prop trading challenges with $50k funded accounts",
            "step1_text": (
                "Hey Sam,\n\nSaw FundedFox on Whop. We run SoftUGC (183.5M views in 90 days) "
                "using AI short-form video on CPA. Want 3 sample reels?"
            ),
        },
        {
            "username": "whop_founder_b",
            "name": "Max Lane",
            "company": "PeakTrades",
            "website": "https://whop.com/discover/peaktrades/",
            "category": "Проп трейдинг",
            "their_angle": "Funded trading platform for futures traders",
            "step1_text": (
                "Hey Max,\n\nSaw PeakTrades on Whop. We run SoftUGC (183.5M views in 90 days) "
                "using AI short-form video on CPA. Want 3 sample reels?"
            ),
        },
    ]

    for item in batch:
        res = _handle_ingest_discovered_leads(
            conn,
            {
                "leads": [item],
                "enroll_in_campaign": True,
                "campaign_name": "SoftUGC — База Все2 (2 045 лидов)",
            },
        )
        assert res["enrolled_in_campaign"] == 1, f"Dropped {item['username']}: {res}"
        assert res["skipped_duplicates"] == 0

    built = build_lead_payload_from_dict(
        {
            "username": "gornal",
            "name": "Alexander Gorny",
            "company": "Momeditation",
            "website": "https://clck.ru/3FPm3j",
            "project_url": "https://momeditation.app",
            "category": "SaaS / AI app",
            "bio": "Co-founder of momeditation.app — subscription meditation app",
        },
        touch_mode="direct",
    )
    assert built["website"] == "https://momeditation.app"


def test_channel_graph_frontier_advances_to_depth2_on_second_run(tmp_path: Path):
    class _FakeChan:
        def __init__(self, cid: int, uname: str, title: str, subs: int = 1200):
            self.id = cid
            self.username = uname
            self.title = title
            self.participants_count = subs

    class _FakeUser:
        def __init__(self, uid: int, uname: str, first: str):
            self.id = uid
            self.username = uname
            self.first_name = first
            self.last_name = "Founder"
            self.bot = False

    graph = {
        "seed_chan": {
            "entity": _FakeChan(1, "seed_chan", "Seed SaaS"),
            "about": "Building https://seedflow.io — DM @seed_founder",
            "recs": ["rec_one"],
        },
        "rec_one": {
            "entity": _FakeChan(2, "rec_one", "Voice Craft"),
            "about": "Building https://voicecraft.dev — DM @voice_ceo",
            "recs": ["rec_two"],
        },
        "rec_two": {
            "entity": _FakeChan(3, "rec_two", "Indie Desk"),
            "about": "Building https://indiedesk.app — DM @indie_maker",
            "recs": [],
        },
    }

    class _FakeClient:
        async def get_entity(self, target):
            key = str(target).lstrip("@").lower()
            return graph[key]["entity"]

        async def get_messages(self, entity, limit=3):
            return []

        async def __call__(self, req):
            name = req.__class__.__name__
            if name == "GetFullChannelRequest":
                key = getattr(req.channel, "username", "").lower()
                info = graph[key]
                return SimpleNamespace(
                    full_chat=SimpleNamespace(
                        about=info["about"],
                        participants_count=1200,
                        linked_chat_id=None,
                    ),
                    chats=[info["entity"]],
                )
            if name == "GetChannelRecommendationsRequest":
                key = getattr(req.channel, "username", "").lower()
                return SimpleNamespace(
                    chats=[graph[r]["entity"] for r in graph[key]["recs"]]
                )
            if name == "GetFullUserRequest":
                uname = str(req.id).lstrip("@").lower()
                return SimpleNamespace(
                    users=[_FakeUser(10, uname, uname.split("_")[0].title())],
                    full_user=SimpleNamespace(about="Founder"),
                )
            raise NotImplementedError(name)

    client = _FakeClient()
    state_file = tmp_path / "cg_seen.json"
    out1 = tmp_path / "cg1.jsonl"
    out2 = tmp_path / "cg2.jsonl"

    rows1 = asyncio.run(
        tg_channel_graph.crawl_channel_graph(
            client=client,
            seeds=["seed_chan"],
            depth=2,
            limit=2,
            state_path=str(state_file),
            output_path=str(out1),
            delay=0.0,
        )
    )
    assert [r["username"] for r in rows1] == ["seed_founder", "voice_ceo"]

    rows2 = asyncio.run(
        tg_channel_graph.crawl_channel_graph(
            client=client,
            seeds=["seed_chan"],
            depth=2,
            limit=2,
            state_path=str(state_file),
            output_path=str(out2),
            delay=0.0,
        )
    )
    assert [r["username"] for r in rows2] == ["indie_maker"]


def test_static_ast_local_use_before_def_detector(tmp_path: Path):
    verify_sh = _WORKTREE_ROOT / "scripts" / "verify.sh"
    sb = tmp_path / "sandbox"
    for d in (
        "scripts",
        "app",
        "leadgen",
        "tests",
        "deploy/systemd",
        ".claude/skills/replica-design",
        ".claude/skills/replica-launch",
        "replica/design",
        "replica/launch",
    ):
        (sb / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(verify_sh, sb / "scripts" / "verify.sh")
    shutil.copy2(_WORKTREE_ROOT / ".claude/skills/replica-design/contrast.py", sb / ".claude/skills/replica-design/contrast.py")
    shutil.copy2(_WORKTREE_ROOT / ".claude/skills/replica-launch/listing.py", sb / ".claude/skills/replica-launch/listing.py")
    shutil.copy2(_WORKTREE_ROOT / "replica/design/tokens.json", sb / "replica/design/tokens.json")
    shutil.copy2(_WORKTREE_ROOT / "replica/launch/listing.json", sb / "replica/launch/listing.json")
    (sb / "leadgen" / "__init__.py").write_text("", encoding="utf-8")
    (sb / "leadgen" / "vps_autonomous_replenish.py").write_text(
        "import sys\nif '--help' in sys.argv: sys.exit(0)\n", encoding="utf-8"
    )
    (sb / "leadgen" / "tg_ad_spy.py").write_text("DEFAULT_AD_SPY_CHANNELS = ('a',)\n", encoding="utf-8")
    (sb / "leadgen" / "tg_channel_graph.py").write_text("DEFAULT_GRAPH_SEED_CHANNELS = ('a',)\n", encoding="utf-8")
    (sb / "leadgen" / "bad_order.py").write_text(
        "def run():\n"
        "    x = _helper()\n"
        "    def _helper():\n"
        "        return 1\n"
        "    return x\n",
        encoding="utf-8",
    )

    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "PYTHON_BIN": sys.executable}
    res = subprocess.run(
        ["bash", str(sb / "scripts" / "verify.sh"), "--fast-checks-only"],
        cwd=str(sb),
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert "_helper" in res.stderr
    assert "bad_order.py" in res.stderr


def test_apply_master_merge_ignores_none_zero_ids_and_platform_project_urls(tmp_path: Path):
    from leadgen.tg_to_gsheet import apply_master_merge

    master = tmp_path / "master_ids.jsonl"
    raw1 = tmp_path / "raw1.jsonl"
    raw2 = tmp_path / "raw2.jsonl"

    rows1 = [
        {"id": None, "username": "u_none_1", "project_url": "https://www.producthunt.com/posts/app1"},
        {"id": None, "username": "u_none_2", "project_url": "https://www.producthunt.com/posts/app2"},
        {"id": 0, "username": "u_zero_3", "project_url": "https://whop.com/discover/brand1/"},
    ]
    raw1.write_text("\n".join(json.dumps(r) for r in rows1) + "\n", encoding="utf-8")
    cnt1 = apply_master_merge(str(raw1), str(master), commit_master=True)
    assert cnt1 == 3

    rows2 = [
        {"id": None, "username": "u_none_4", "project_url": "https://www.producthunt.com/posts/app3"},
        {"id": 0, "username": "u_none_1", "project_url": "https://www.producthunt.com/posts/app1"},
    ]
    raw2.write_text("\n".join(json.dumps(r) for r in rows2) + "\n", encoding="utf-8")
    cnt2 = apply_master_merge(str(raw2), str(master), commit_master=True)
    assert cnt2 == 1
    written_new = [json.loads(ln) for ln in Path(f"{raw2}.new").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [r["username"] for r in written_new] == ["u_none_4"]


def test_pending_qualify_queue_recovers_on_enrich_fail_curate_fail_and_partial_llm_batch(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    conn = isolated_env["conn"]
    state_dir: Path = isolated_env["state_dir"]
    master_jsonl = state_dir / "master_ids.jsonl"
    pending_queue = state_dir / "pending_qualify_queue.jsonl"
    llm_failed_file = state_dir / "llm_failed_batches.jsonl"

    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    create_campaign(
        conn,
        name="SoftUGC — База Все2 (2 045 лидов)",
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "target_campaign_tg": "SoftUGC — База Все2 (2 045 лидов)",
                "sources": {
                    "buyer_catalogs": True,
                    "tg_groups": False,
                    "ofm_groups": False,
                    "facebook_groups": False,
                    "meta_ads": False,
                    "social_x_reddit": False,
                    "yc_ph": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "whop_content_rewards": False,
                    "tiktok_ads": False,
                    "tiktok_shop": False,
                    "app_stores": False,
                    "ats_hiring": False,
                    "ph_detailed": False,
                    "d2c_whatsapp": False,
                    "tg_identity_bridge": False,
                },
                "limits": {"external_limit": 0},
            },
            "vacancies": {"enabled": False},
        },
        sync_cases_file=False,
    )

    alice = {
        "username": "alice_queue",
        "name": "Alice Queue",
        "project": "AliceApp",
        "company": "AliceApp",
        "project_url": "https://aliceapp.io",
        "bio": "Founder of AliceApp ($19/mo subscription)",
        "what_they_do": "AI subscription app ($19/mo)",
        "category": "SaaS / AI app",
        "role": "Founder",
        "roles": ["Founder"],
        "source": "catalog_test",
    }
    bob = {
        "username": "bob_queue",
        "name": "Bob Queue",
        "project": "BobStore",
        "company": "BobStore",
        "project_url": "https://bobstore.shop",
        "bio": "Founder of BobStore skincare brand",
        "what_they_do": "D2C skincare e-commerce store",
        "category": "E-com / D2C",
        "role": "Founder",
        "roles": ["Founder"],
        "source": "catalog_test",
    }

    mode = {"phase": "enrich_fail"}

    def runner(cmd, **kwargs):
        cmd_str = " ".join(str(x) for x in cmd)
        if "buyer_catalogs.py" in cmd_str:
            out_p = Path(cmd[cmd.index("--output") + 1])
            if mode["phase"] == "enrich_fail":
                out_p.write_text(json.dumps(alice) + "\n" + json.dumps(bob) + "\n", encoding="utf-8")
            else:
                out_p.write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_enrich_leads.py" in cmd_str:
            if mode["phase"] == "enrich_fail":
                return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="enrich error")
            inp_p = Path(cmd[cmd.index("--input") + 1])
            out_p = Path(cmd[cmd.index("--output") + 1])
            out_p.write_text(inp_p.read_text(encoding="utf-8"), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_llm_qualify.py" in cmd_str:
            assert mode["phase"] != "enrich_fail", "Must not run qualify when enrich failed"
            inp_p = Path(cmd[cmd.index("--input") + 1])
            out_p = Path(cmd[cmd.index("--output") + 1])
            in_rows = [json.loads(ln) for ln in inp_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            kept = []
            failed = []
            for r in in_rows:
                if mode["phase"] == "partial_batch" and r.get("username") == "bob_queue":
                    failed.append(r)
                    continue
                r["is_lead"] = True
                r["qualified"] = True
                r["icp_score"] = 90
                r["monetization"] = "subscription"
                kept.append(r)
            out_p.write_text("\n".join(json.dumps(r) for r in kept) + ("\n" if kept else ""), encoding="utf-8")
            if failed:
                llm_failed_file.write_text("\n".join(json.dumps(r) for r in failed) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_curate.py" in cmd_str:
            if mode["phase"] == "curate_fail":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="curate failed")
            inp_p = Path(cmd[cmd.index("--input") + 1])
            out_p = Path(cmd[cmd.index("--output") + 1])
            rows = [json.loads(ln) for ln in inp_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for r in rows:
                r["first_touch"] = (
                    f"Hey {r.get('name')},\n\nSaw {r.get('project')}. We run SoftUGC, bringing 183.5M views "
                    "in 90 days via AI short-form video on revshare. Want to see 3 sample reels?"
                )
            out_p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # 1. Enrich fails -> queue saved, master_ids untouched
    res1 = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=runner)
    assert res1["ok"] is True
    assert res1["ingested_total"] == 0
    assert not master_jsonl.exists()
    assert pending_queue.is_file() and "alice_queue" in pending_queue.read_text(encoding="utf-8")

    # 2. Curate fails -> queue still preserved, master_ids still untouched
    mode["phase"] = "curate_fail"
    res2 = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=runner)
    assert res2["ingested_total"] == 0
    assert not master_jsonl.exists()
    assert pending_queue.is_file() and "bob_queue" in pending_queue.read_text(encoding="utf-8")

    # 3. Partial LLM batch failure -> alice commits & enters CRM; bob stays in queue, NOT in master_ids
    mode["phase"] = "partial_batch"
    res3 = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=runner)
    assert res3["ingested_total"] == 1
    master_txt_3 = master_jsonl.read_text(encoding="utf-8")
    assert "alice_queue" in master_txt_3 and "bob_queue" not in master_txt_3
    assert pending_queue.is_file() and "bob_queue" in pending_queue.read_text(encoding="utf-8")

    # 4. Final recovery -> bob commits, enters CRM, queue is cleaned up
    mode["phase"] = "all_ok"
    res4 = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=runner)
    assert res4["ingested_total"] == 1
    assert "bob_queue" in master_jsonl.read_text(encoding="utf-8")
    assert not pending_queue.exists()


def test_tg_llm_qualify_null_content_parallel_batches_and_relay_route_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from leadgen import relay_route, tg_llm_qualify

    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_ENABLED", "0")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tg_llm_qualify, "API_KEY", "test-key")
    relay_route.reset({"claude-sonnet-5", "claude-haiku-5-5"}, loaded=True)

    calls: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=90):
        model = (json or {}).get("model")
        calls.append(model)
        if model == "claude-sonnet-5":
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"choices": [{"message": {"content": None}}]},
            )
        rows_in = __import__("json").loads((json or {})["messages"][1]["content"])
        reply = [
            {
                "is_lead": True,
                "icp_score": 90,
                "monetization": "subscription",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Founder",
                "project": item["project"],
                "project_url": item["project_url"],
                "what_they_do": "Subscription AI app ($19/mo)",
            }
            for item in rows_in
        ]
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": __import__("json").dumps(reply)}}]},
        )

    monkeypatch.setattr(tg_llm_qualify.requests, "post", fake_post)
    rows = [
        {"name": "A", "username": "u_a", "project": "AppOne", "project_url": "https://appone.io", "bio": "Founder AppOne", "has_monetization_signal": True},
        {"name": "B", "username": "u_b", "project": "AppTwo", "project_url": "https://apptwo.io", "bio": "Founder AppTwo", "has_monetization_signal": True},
    ]
    kept, dropped = tg_llm_qualify.qualify_loaded(rows, batch_size=1)
    assert dropped == 0
    assert [r["username"] for r in kept] == ["u_a", "u_b"]
    assert "claude-haiku-5-5" in calls


def test_treg_social_leads_parallel_pool_tolerates_single_query_exception(monkeypatch: pytest.MonkeyPatch):
    from datetime import date
    from leadgen import treg_social_leads

    call_idx = {"n": 0}

    def fake_search_x(q: str):
        call_idx["n"] += 1
        if "looking for clippers" in q:
            raise RuntimeError("simulated worker crash")
        if "need AI UGC" in q:
            return {
                "posts": [
                    {
                        "authorUsername": "cool_founder",
                        "authorName": "Cool Founder",
                        "text": "We need AI UGC videos for our SaaS https://coolsaas.io",
                        "url": "https://x.com/cool_founder/status/123",
                        "isReply": False,
                    }
                ]
            }
        return {"posts": []}

    monkeypatch.setattr(treg_social_leads, "search_x", fake_search_x)
    rows, _, stats = treg_social_leads.discover(
        day=date(2026, 11, 18),
        seen={},
        cap=5,
        search_x_fn=fake_search_x,
        search_reddit_fn=lambda s, t: {"posts": []},
        search_comments_fn=lambda u: {"posts": []},
    )
    assert stats["admitted"] >= 1
    assert any(r.get("founder_x") == "https://x.com/cool_founder" for r in rows)


def test_pick_best_product_url_and_tg_enrich_leads_is_social_sync():
    from leadgen import tg_enrich_leads
    from leadgen.supply_side import pick_best_product_url

    assert (
        pick_best_product_url(
            {
                "project_url": "N/A",
                "website": "mailto:founder@realapp.io",
                "site": "https://clck.ru/3FPm3j",
                "url": "https://realapp.io",
            }
        )
        == "https://realapp.io"
    )
    assert tg_enrich_leads.is_social("https://clck.ru/3FPm3j") is True
    assert tg_enrich_leads.is_social("https://gumroad.com/l/course") is True
    assert (
        tg_enrich_leads.pick_project_url(["https://clck.ru/3FPm3j", "https://momeditation.app"])
        == "https://momeditation.app"
    )


def test_missing_llm_key_does_not_commit_master_or_drop_pending_queue(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    from leadgen import vps_autonomous_replenish

    conn = isolated_env["conn"]
    state_dir: Path = isolated_env["state_dir"]
    master_jsonl = state_dir / "master_ids.jsonl"
    pending_queue = state_dir / "pending_qualify_queue.jsonl"

    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    create_campaign(
        conn,
        name="SoftUGC — База Все2 (2 045 лидов)",
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "target_campaign_tg": "SoftUGC — База Все2 (2 045 лидов)",
                "sources": {
                    "buyer_catalogs": True,
                    "tg_groups": False,
                    "ofm_groups": False,
                    "facebook_groups": False,
                    "meta_ads": False,
                    "social_x_reddit": False,
                    "yc_ph": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "whop_content_rewards": False,
                    "tiktok_ads": False,
                    "tiktok_shop": False,
                    "app_stores": False,
                    "ats_hiring": False,
                    "ph_detailed": False,
                    "d2c_whatsapp": False,
                    "tg_identity_bridge": False,
                },
                "limits": {"external_limit": 0},
            },
            "vacancies": {"enabled": False},
        },
        sync_cases_file=False,
    )

    raw_row = {
        "id": 9911,
        "username": "no_key_founder",
        "name": "No Key Founder",
        "bio": "Founder of WaitApp https://waitapp.io",
        "project_url": "https://waitapp.io",
        "source": "catalog_test",
        "has_monetization_signal": True,
    }

    def runner(cmd, **kwargs):
        cmd_str = " ".join(str(x) for x in cmd)
        if "buyer_catalogs.py" in cmd_str:
            out_p = Path(cmd[cmd.index("--output") + 1])
            out_p.write_text(json.dumps(raw_row) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_enrich_leads.py" in cmd_str:
            inp_p = Path(cmd[cmd.index("--input") + 1])
            out_p = Path(cmd[cmd.index("--output") + 1])
            out_p.write_text(inp_p.read_text(encoding="utf-8"), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=runner)
    assert res["ingested_total"] == 0
    assert not master_jsonl.exists() or master_jsonl.read_text(encoding="utf-8").strip() == ""
    assert pending_queue.is_file()
    q_rows = [json.loads(line) for line in pending_queue.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(r.get("username") == "no_key_founder" for r in q_rows)


def test_treg_social_leads_enforces_budget_inside_x_queries(monkeypatch: pytest.MonkeyPatch):
    import time as _time
    from datetime import date
    from leadgen import treg_social_leads

    calls = {"n": 0}

    def slow_search_x(q: str):
        calls["n"] += 1
        _time.sleep(0.12)
        return {"posts": []}

    monkeypatch.setenv("TREG_SOCIAL_BUDGET_SEC", "0.08")
    started = _time.monotonic()
    rows, _, stats = treg_social_leads.discover(
        day=date(2026, 11, 18),
        seen={},
        cap=5,
        search_x_fn=slow_search_x,
        search_reddit_fn=lambda s, t: {"posts": []},
        search_comments_fn=lambda u: {"posts": []},
    )
    elapsed = _time.monotonic() - started
    assert elapsed < 0.8
    assert calls["n"] < len(treg_social_leads.buyer_queries(date(2026, 11, 18))) + 5


def test_gate_i_catches_local_import_before_definition(tmp_path: Path):
    import shutil
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    verify_copy = tmp_path / "scripts" / "verify.sh"
    verify_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_root / "scripts" / "verify.sh", verify_copy)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "leadgen").mkdir(parents=True, exist_ok=True)
    (tmp_path / "leadgen" / "bad_import.py").write_text(
        "def broken():\n    x = json.loads('{}')\n    import json\n    return x\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["bash", str(verify_copy), "--fast-checks-only"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    combined = proc.stdout + "\n" + proc.stderr
    assert "local 'json' used before def/import" in combined


def test_tg_llm_qualify_handles_non_dict_json_gracefully(monkeypatch: pytest.MonkeyPatch):
    from leadgen import tg_llm_qualify

    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    monkeypatch.setattr(tg_llm_qualify, "chain", lambda: ["claude-haiku-5-5"])

    class DummyResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    monkeypatch.setattr(tg_llm_qualify.requests, "post", lambda *a, **k: DummyResp(["not-a-dict"]))
    with pytest.raises(ValueError, match="non-dict response"):
        tg_llm_qualify.call_llm([{"username": "a", "name": "A", "bio": "b"}], "claude-haiku-5-5")


