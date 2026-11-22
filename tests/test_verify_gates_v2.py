from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from app.db import connect, init_db, record_enrichment_ledger_entry, utc_now
from app.leads import create_lead
from leadgen import outreach_copy
import scripts.proactive_audit as proactive_audit


def _setup_verify_sandbox(tmp_path: Path, files: dict[str, str]) -> Path:
    sandbox = tmp_path / "verify_v2_sandbox"
    for sub in ("scripts", "app/static", "leadgen", "tests", "deploy/systemd"):
        (sandbox / sub).mkdir(parents=True, exist_ok=True)
    for link_dir in (".claude", "replica"):
        src_link = WORKTREE_ROOT / link_dir
        if src_link.exists():
            (sandbox / link_dir).symlink_to(src_link)
    venv_path = WORKTREE_ROOT / ".venv"
    if venv_path.exists():
        (sandbox / ".venv").symlink_to(venv_path.resolve())

    sb_verify = sandbox / "scripts" / "verify.sh"
    sb_verify.write_text((WORKTREE_ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8"), encoding="utf-8")
    sb_verify.chmod(0o755)
    (sandbox / "app" / "static" / "app.js").write_text("const ok = 1;\n", encoding="utf-8")
    (sandbox / "leadgen" / "__init__.py").write_text("", encoding="utf-8")
    (sandbox / "leadgen" / "vps_autonomous_replenish.py").write_text(
        "import argparse\nif __name__ == '__main__': argparse.ArgumentParser().parse_args()\n",
        encoding="utf-8",
    )
    (sandbox / "deploy" / "systemd" / "clean.service").write_text(
        '[Service]\nEnvironment="CRMTG_VPS_RUNTIME=1"\nExecStart=/opt/crmtg/.venv/bin/python -m leadgen.vps_autonomous_replenish\n',
        encoding="utf-8",
    )
    for rel, content in files.items():
        target = sandbox / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sandbox


def _run_verify(sandbox: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(sandbox / "scripts" / "verify.sh"), "--fast-checks-only"],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        check=False,
    )


def test_gate_v_blocks_gaming_in_meta_ads_and_golden_icp_but_allows_reject(tmp_path: Path):
    # 1) meta_ad_leads with roblox in SEARCH_QUERIES must fail Gate V
    sb_bad_ma = _setup_verify_sandbox(
        tmp_path / "bad_ma",
        {
            "leadgen/meta_ad_leads.py": 'SEARCH_QUERIES = ("AI coaching app", "roblox game")\n',
        },
    )
    res_bad_ma = _run_verify(sb_bad_ma)
    assert res_bad_ma.returncode == 1
    assert "Gate V violation in leadgen/meta_ad_leads.py" in res_bad_ma.stderr
    assert "roblox" in res_bad_ma.stderr

    # 2) tg_llm_qualify with Fortnite in Golden ICP clusters must fail Gate V
    sb_bad_tq = _setup_verify_sandbox(
        tmp_path / "bad_tq",
        {
            "leadgen/tg_llm_qualify.py": (
                'SYSTEM_PROMPT = """\n'
                "Golden ICP clusters:\n"
                "1. Gaming / Fortnite map creators\n\n"
                "REJECT:\n"
                "- Agencies\n"
                '"""\n'
            ),
        },
    )
    res_bad_tq = _run_verify(sb_bad_tq)
    assert res_bad_tq.returncode == 1
    assert "Gate V violation in leadgen/tg_llm_qualify.py" in res_bad_tq.stderr
    assert "fortnite" in res_bad_tq.stderr

    # 3) Clean meta_ad_leads + tg_llm_qualify (with Roblox/Fortnite/FiveM ONLY in REJECT) must pass
    sb_good = _setup_verify_sandbox(
        tmp_path / "good",
        {
            "leadgen/meta_ad_leads.py": 'SEARCH_QUERIES = ("AI coaching app", "crypto wallet app")\n',
            "leadgen/tg_llm_qualify.py": (
                'CATEGORIES = ["SaaS / AI app", "E-com / D2C"]\n'
                'SYSTEM_PROMPT = """\n'
                "Golden ICP clusters:\n"
                "1. High-LTV SaaS & D2C apps\n\n"
                "REJECT:\n"
                "- Gaming / Roblox / Fortnite / FiveM\n"
                '"""\n'
            ),
            "leadgen/monid_client.py": "ok = True\n",
            "leadgen/high_yield_veins.py": "VEIN = 'd2c_whatsapp'\n",
            "leadgen/tg_identity_bridge.py": "ok = True\n",
            "leadgen/contact_waterfall.py": (
                "def enrich_lead_contacts(row, allow_paid_tier2=True, has_tg=False, has_wa=False, budget_guard_fn=None):\n"
                "    if allow_paid_tier2 and not (has_tg or has_wa):\n"
                "        return {}\n"
                "    return row\n"
            ),
            "leadgen/ofm_and_groups.py": "ok = True\n",
            "leadgen/vps_autonomous_replenish.py": (
                "import argparse\n"
                "def run_autonomous_replenish(conn):\n"
                "    return {'stages': ['high_yield_veins.py', 'tg_identity_bridge.py', 'contact_waterfall.py', 'ofm_and_groups.py', 'last_run_summary.json', 'd2c_whatsapp']}\n"
                "if __name__ == '__main__': argparse.ArgumentParser().parse_args()\n"
            ),
            "app/leadgen_settings.py": "CFG = ('ofm_review_leads.jsonl', 'd2c_whatsapp')\n",
        },
    )
    res_good = _run_verify(sb_good)
    assert res_good.returncode == 0, res_good.stdout + res_good.stderr


def test_gate_v_blocks_tier2_email_or_x_skip_regression(tmp_path: Path):
    sb_bad_cw = _setup_verify_sandbox(
        tmp_path / "bad_cw",
        {
            "leadgen/contact_waterfall.py": (
                "def enrich_lead_contacts(row, allow_paid_tier2=True, has_tg=False, has_wa=False, has_x=True, has_personal_email=True):\n"
                "    if allow_paid_tier2 and not (has_tg or has_wa or has_x or has_personal_email):\n"
                "        return {}\n"
                "    return row\n"
            ),
        },
    )
    res_bad_cw = _run_verify(sb_bad_cw)
    assert res_bad_cw.returncode == 1
    assert "Gate V violation in leadgen/contact_waterfall.py" in res_bad_cw.stderr

    sb_good_cw = _setup_verify_sandbox(
        tmp_path / "good_cw",
        {
            "leadgen/contact_waterfall.py": (
                "def enrich_lead_contacts(row, allow_paid_tier2=True, has_tg=False, has_wa=False, has_personal_email=True, budget_guard_fn=None):\n"
                "    if allow_paid_tier2 and not (has_tg or has_wa):\n"
                "        if not has_personal_email:\n"
                "            pass\n"
                "        return {}\n"
                "    return row\n"
            ),
        },
    )
    res_good_cw = _run_verify(sb_good_cw)
    assert res_good_cw.returncode == 0, res_good_cw.stdout + res_good_cw.stderr


def test_gate_v_enforces_airtable_isolation_no_revshare_whale_and_language_router(tmp_path: Path):
    # 1) Missing ru_expert / exclude_from_airtable isolation in app/airtable_sync.py must fail
    sb_bad_at = _setup_verify_sandbox(
        tmp_path / "bad_at",
        {
            "app/airtable_sync.py": (
                "def pull_from_airtable(conn):\n"
                "    _should_preserve_local_lead_source()\n"
                "    _compute_effective_lead_patch()\n"
                "    update_lead()\n"
                "    record_lead_event('airtable_synced')\n"
                "def _select_airtable_push_candidates(conn): return []\n"
                "def push_lead_to_airtable(conn, lead_id): return {}\n"
                "def sync_airtable(conn): return {}\n"
            ),
        },
    )
    res_bad_at = _run_verify(sb_bad_at)
    assert res_bad_at.returncode == 1
    assert "Gate V violation in app/airtable_sync.py" in res_bad_at.stderr

    # 2) REVSHARE_OFFER_TEXT inside _build_whale_mass_steps must fail
    sb_bad_oc = _setup_verify_sandbox(
        tmp_path / "bad_oc",
        {
            "leadgen/outreach_copy.py": (
                'REVSHARE_OFFER_TEXT = "We work on revshare or CPA"\n'
                'WHALE_PITCH_TEXT = "183.5M views"\n'
                'WHALE_OFFER_TEXT = "Want 3 demo reels?"\n'
                "def _build_whale_mass_steps():\n"
                "    return REVSHARE_OFFER_TEXT, 's2', 's3'\n"
            ),
        },
    )
    res_bad_oc = _run_verify(sb_bad_oc)
    assert res_bad_oc.returncode == 1
    assert "Gate V violation in leadgen/outreach_copy.py" in res_bad_oc.stderr

    # 3) Real outreach_copy whale_mass Step 1 never contains revshare/CPA/CreatorOS
    seq = outreach_copy.build_outreach_sequence(
        {
            "company": "AstroPulse",
            "their_angle": "AI astrology subscription app ($14.99/mo)",
            "source": "meta_ads",
            "segment": "whale",
        },
        copy_profile="whale_mass",
        language="en",
        provider="telegram",
        use_llm=False,
    )
    assert outreach_copy.REVSHARE_OFFER_TEXT not in seq.step1_text
    assert "revshare" not in seq.step1_text.lower()
    assert "cpa" not in seq.step1_text.lower()
    assert "creatoros" not in seq.step1_text.lower()

    # 4) Missing assert_copy_matches_language in leadgen/language_router.py must fail
    sb_bad_lr = _setup_verify_sandbox(
        tmp_path / "bad_lr",
        {
            "leadgen/language_router.py": "def detect_lead_language(row): return 'en'\n",
        },
    )
    res_bad_lr = _run_verify(sb_bad_lr)
    assert res_bad_lr.returncode == 1
    assert "Gate V violation in leadgen/language_router.py" in res_bad_lr.stderr

    # 5) Missing frozen-no-spillover / language lock in app/outreach.py must fail Gate V
    sb_bad_outreach = _setup_verify_sandbox(
        tmp_path / "bad_outreach",
        {
            "app/outreach.py": (
                "_VACANCY_JUNK_ROLE_RE = None\n"
                "def find_vacancy_account_pool_violations(): return []\n"
                "# find_vacancy_account_pool_violations(1) find_vacancy_account_pool_violations(2) "
                "find_vacancy_account_pool_violations(3) find_vacancy_account_pool_violations(4)\n"
                "SQL = 'SELECT l.created_at AS lead_created_at FROM leads l'\n"
                "OK_CALL = 'skip_reply_stop=True'\n"
                "WA_ERR = 'сессия whatsapp не авторизована'\n"
            ),
        },
    )
    res_bad_outreach = _run_verify(sb_bad_outreach)
    assert res_bad_outreach.returncode == 1
    assert "Gate V violation in app/outreach.py" in res_bad_outreach.stderr


def test_proactive_audit_selftest_exits_zero():
    proc = subprocess.run(
        [sys.executable, str(WORKTREE_ROOT / "scripts" / "proactive_audit.py"), "--self-test"],
        cwd=str(WORKTREE_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "proactive_audit --self-test: OK" in proc.stdout


def test_proactive_audit_ru_expert_isolation_invariant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)
    cfg_path = tmp_path / "leadgen_config.json"
    cfg_path.write_text(json.dumps({"enabled": True, "enrichment": {"daily_budget_cap_usd": 5.0}}), encoding="utf-8")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path / "leadgen_state"))

    db_path = tmp_path / "ru_iso.db"
    conn = connect(db_path)
    init_db(conn)
    lead = create_lead(
        conn,
        name="Алина Эксперт",
        company="Школа Алины",
        niche="Продюсирование",
        lead_source="ru_experts",
        username="alina_expert_ru",
        telegram_user_id=9201,
        status="new",
    )
    conn.execute(
        "UPDATE leads SET segment = 'ru_expert', language = 'ru', exclude_from_airtable = 0 WHERE id = ?",
        (int(lead["id"]),),
    )
    conn.commit()
    conn.close()

    assert proactive_audit.main(["--db", str(db_path)]) == 1

    # Also test --heal repairs exclude_from_airtable=1 and brings DB back to exit 0
    assert proactive_audit.main(["--db", str(db_path), "--heal"]) == 0
    conn2 = connect(db_path)
    excl_val = conn2.execute("SELECT exclude_from_airtable FROM leads WHERE id = ?", (int(lead["id"]),)).fetchone()[0]
    conn2.close()
    assert excl_val == 1


def test_proactive_audit_ru_language_campaigns_invariant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)
    cfg_path = tmp_path / "leadgen_config.json"
    cfg_path.write_text(json.dumps({"enabled": True, "enrichment": {"daily_budget_cap_usd": 5.0}}), encoding="utf-8")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path / "leadgen_state"))

    db_path = tmp_path / "ru_lang.db"
    conn = connect(db_path)
    init_db(conn)
    now = utc_now()
    camp_id = int(
        conn.execute(
            """
            INSERT INTO campaigns (
                name, daily_limit, status, segment, language, copy_profile, exclude_from_airtable, created_at, updated_at
            ) VALUES ('Вакансии RU Test', 20, 'draft', 'vacancy', 'ru', 'vacancy_ru', 0, ?, ?)
            """,
            (now, now),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at) "
        "VALUES (?, 1, 0, 'Hey! We produce 1500 reels/mo for your brand.', ?)",
        (camp_id, now),
    )
    conn.commit()
    assert proactive_audit.main(["--db", str(db_path)]) == 1

    # Placeholder-only template '{first_touch}' with English custom_step_templates_json must still fail!
    conn.execute(
        "UPDATE campaign_steps SET template_text = '{first_touch}' WHERE campaign_id = ? AND step_order = 1",
        (camp_id,),
    )
    conn.execute(
        "UPDATE campaigns SET custom_step_templates_json = ? WHERE id = ?",
        (json.dumps({"step1": "Want 3 demo reels for your brand?"}), camp_id),
    )
    conn.commit()
    assert proactive_audit.main(["--db", str(db_path)]) == 1

    # Valid Russian Step 1 template + custom Russian override must pass
    conn.execute(
        "UPDATE campaign_steps SET template_text = 'Добрый день! Выстраиваем лидген через короткие ролики SoftUGC.' "
        "WHERE campaign_id = ? AND step_order = 1",
        (camp_id,),
    )
    conn.execute(
        "UPDATE campaigns SET custom_step_templates_json = ? WHERE id = ?",
        (json.dumps({"step1": "Привет! Поможем выстроить поток коротких роликов под заявки."}), camp_id),
    )
    conn.commit()
    conn.close()
    assert proactive_audit.main(["--db", str(db_path)]) == 0


def test_proactive_audit_en_campaign_catches_ru_leak_and_creatoros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)
    cfg_path = tmp_path / "leadgen_config.json"
    cfg_path.write_text(json.dumps({"enabled": True, "enrichment": {"daily_budget_cap_usd": 5.0}}), encoding="utf-8")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path / "leadgen_state"))

    db_path = tmp_path / "en_lang.db"
    conn = connect(db_path)
    init_db(conn)
    now = utc_now()
    camp_id = int(
        conn.execute(
            """
            INSERT INTO campaigns (
                name, daily_limit, status, segment, language, copy_profile, exclude_from_airtable, created_at, updated_at
            ) VALUES ('Whales EN Test', 30, 'draft', 'whale', 'en', 'whale_mass', 0, ?, ?)
            """,
            (now, now),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at) "
        "VALUES (?, 1, 0, 'Привет! Check out SoftUGC reels', ?)",
        (camp_id, now),
    )
    conn.commit()
    assert proactive_audit.main(["--db", str(db_path)]) == 1

    # CreatorOS in EN campaign step 2 fails exit 1
    conn.execute(
        "UPDATE campaign_steps SET template_text = 'Hey, saw your product. Want 3 demo reels for Acme?' "
        "WHERE campaign_id = ? AND step_order = 1",
        (camp_id,),
    )
    conn.execute(
        "INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at) "
        "VALUES (?, 2, 48, 'Checking back from CreatorOS team on your demo reels', ?)",
        (camp_id, now),
    )
    conn.commit()
    assert proactive_audit.main(["--db", str(db_path)]) == 1

    # RU leak in custom_step_templates_json or pending recipient step1_text fails exit 1
    conn.execute("DELETE FROM campaign_steps WHERE campaign_id = ? AND step_order = 2", (camp_id,))
    conn.execute(
        "UPDATE campaigns SET custom_step_templates_json = ? WHERE id = ?",
        (json.dumps({"step2": "Привет! Возвращаюсь по вашему проекту."}), camp_id),
    )
    conn.commit()
    assert proactive_audit.main(["--db", str(db_path)]) == 1

    conn.execute("UPDATE campaigns SET custom_step_templates_json = NULL WHERE id = ?", (camp_id,))
    conn.execute(
        "INSERT INTO campaign_recipients (campaign_id, telegram_user_id, username, name, step1_text, status, created_at, updated_at) "
        "VALUES (?, 9301, 'en_leak_rec', 'Acme', 'Привет! Check out SoftUGC reels', 'pending', ?, ?)",
        (camp_id, now, now),
    )
    conn.commit()
    assert proactive_audit.main(["--db", str(db_path)]) == 1

    # Clean EN copy with capitalized Cyrillic recipient name passes exit 0
    conn.execute(
        "UPDATE campaign_recipients SET step1_text = 'Hey Иван, saw your product. Want 3 demo reels for Acme?' "
        "WHERE campaign_id = ? AND username = 'en_leak_rec'",
        (camp_id,),
    )
    conn.commit()
    conn.close()
    assert proactive_audit.main(["--db", str(db_path)]) == 0


def test_proactive_audit_enrichment_ledger_bounds_invariant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)
    cfg_path = tmp_path / "leadgen_config.json"
    cfg_path.write_text(json.dumps({"enabled": True, "enrichment": {"daily_budget_cap_usd": 5.0}}), encoding="utf-8")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path / "leadgen_state"))

    db_path = tmp_path / "ledger_bounds.db"
    conn = connect(db_path)
    init_db(conn)
    record_enrichment_ledger_entry(
        conn,
        day="2026-11-22",
        provider="treg",
        endpoint="/people.phone.find",
        lead_key="whale1.com",
        cost_usd=3.20,
        hit=1,
    )
    record_enrichment_ledger_entry(
        conn,
        day="2026-11-22",
        provider="monid",
        endpoint="/facebook.page_contact",
        lead_key="whale2.com",
        cost_usd=1.75,
        hit=1,
    )
    conn.commit()
    # Combined $4.95 <= $5.00 cap passes
    assert proactive_audit.main(["--db", str(db_path)]) == 0

    # Adding $0.30 pushes day total to $5.25 > $5.00 -> fails exit 1
    record_enrichment_ledger_entry(
        conn,
        day="2026-11-22",
        provider="treg",
        endpoint="/people.phone.find",
        lead_key="whale3.com",
        cost_usd=0.30,
        hit=1,
    )
    conn.commit()
    assert proactive_audit.main(["--db", str(db_path)]) == 1

    # Single-provider $6.00 on another day also fails exit 1
    conn.execute("DELETE FROM enrichment_ledger WHERE lead_key = 'whale3.com'")
    record_enrichment_ledger_entry(
        conn,
        day="2026-11-21",
        provider="treg",
        endpoint="/people.phone.find",
        lead_key="whale4.com",
        cost_usd=6.00,
        hit=1,
    )
    conn.commit()
    conn.close()
    assert proactive_audit.main(["--db", str(db_path)]) == 1


def test_gate_s_bare_except_pass_count_and_verify_fast_checks_ok():
    bare_except_actual = 0
    for scan_sub in ("app", "leadgen"):
        for py_f in sorted((WORKTREE_ROOT / scan_sub).rglob("*.py")):
            t_ast = ast.parse(py_f.read_text(encoding="utf-8"), filename=str(py_f))
            for nd in ast.walk(t_ast):
                if isinstance(nd, ast.ExceptHandler) and len(nd.body) == 1 and isinstance(nd.body[0], ast.Pass):
                    if nd.type is None or (isinstance(nd.type, ast.Name) and nd.type.id in ("Exception", "BaseException")):
                        bare_except_actual += 1
    assert bare_except_actual <= 196, f"Bare except/pass ceiling exceeded: {bare_except_actual} > 196"

    proc = subprocess.run(
        ["bash", str(WORKTREE_ROOT / "scripts" / "verify.sh"), "--fast-checks-only"],
        cwd=str(WORKTREE_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr


def test_gate_v5_strengthened_sandbox_rejects_broken_fail_closed_defaults(tmp_path: Path):
    # Fail-closed default check in sandbox for resolve_phones_telegram(is_checker_session=True)
    sandbox = tmp_path / "gate_v5_sandbox"
    (sandbox / "scripts").mkdir(parents=True, exist_ok=True)
    (sandbox / "leadgen").mkdir(parents=True, exist_ok=True)
    (sandbox / "app").mkdir(parents=True, exist_ok=True)
    (sandbox / "deploy" / "systemd").mkdir(parents=True, exist_ok=True)
    (sandbox / "scripts" / "verify.sh").write_text(
        (WORKTREE_ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (sandbox / "app" / "static").mkdir(parents=True, exist_ok=True)
    (sandbox / "app" / "static" / "app.js").write_text("console.log('ok');\n", encoding="utf-8")
    (sandbox / "leadgen" / "phone_messenger_resolver.py").write_text(
        "async def resolve_phones_telegram(client, phones, *, is_checker_session=True):\n"
        "    return {}\n",
        encoding="utf-8",
    )
    bad_pmr = subprocess.run(
        ["bash", str(sandbox / "scripts" / "verify.sh"), "--fast-checks-only"],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_pmr.returncode != 0
    assert "is_checker_session=False" in (bad_pmr.stdout + bad_pmr.stderr)

    # Fixing phone_messenger_resolver and testing budget_guard allow_unavailable=True fails Gate V5
    (sandbox / "leadgen" / "phone_messenger_resolver.py").write_text(
        "async def resolve_phones_telegram(client, phones, *, is_checker_session=False):\n"
        "    return {}\n",
        encoding="utf-8",
    )
    (sandbox / "leadgen" / "budget_guard.py").write_text(
        "def check_balances_and_alert(conn, *, allow_unavailable=True):\n"
        "    return None\n",
        encoding="utf-8",
    )
    bad_bg = subprocess.run(
        ["bash", str(sandbox / "scripts" / "verify.sh"), "--fast-checks-only"],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_bg.returncode != 0
    assert "allow_unavailable=False" in (bad_bg.stdout + bad_bg.stderr)
