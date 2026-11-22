import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import as_dict, connect, init_db  # noqa: E402
from app.leadgen_settings import (  # noqa: E402
    DEFAULT_LEADGEN_CONFIG,
    _normalize_config,
    get_leadgen_config,
    push_discovered_now,
    resolve_target_b2b_tg_campaign_name,
    save_leadgen_config,
    sync_leadgen_campaigns,
)
from app.mcp_server import _handle_ingest_discovered_leads  # noqa: E402
from app.outreach import create_campaign  # noqa: E402
from leadgen.language_router import (  # noqa: E402
    CAMPAIGN_RU_EXPERTS_TG,
    CAMPAIGN_VACANCY_EN_TG,
    CAMPAIGN_VACANCY_RU_TG,
    CAMPAIGN_VACANCY_RU_WA,
    CAMPAIGN_WHALES_EN_TG,
    CAMPAIGN_WHALES_EN_WA,
)


@pytest.fixture
def isolated_crm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "crm_v2.db"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"

    monkeypatch.setenv("CRMTG_DB_PATH", str(db_path))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))

    conn = connect(db_path)
    init_db(conn)
    try:
        yield conn, state_dir, cfg_path
    finally:
        conn.close()


def test_default_leadgen_config_and_disk_json_v2_sections(
    isolated_crm, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _state_dir, cfg_path = isolated_crm
    save_leadgen_config(conn, DEFAULT_LEADGEN_CONFIG)
    assert cfg_path.exists()
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))

    for cfg in (DEFAULT_LEADGEN_CONFIG, on_disk):
        # Enrichment budget & alert guardrails
        enr = cfg["enrichment"]
        assert enr == {
            "daily_budget_cap_usd": 5.0,
            "per_lead_cap_usd": 0.05,
            "super_whale_cap_usd": 0.15,
            "contactout_verified_cap_usd": 0.25,
            "super_whale_icp_min": 92,
            "super_whale_pool_min_usd": 10000,
            "low_balance_warn_usd": 2.0,
            "low_balance_stop_usd": 0.50,
            "alert_chat_id": "",
        }

        # Whales v2 section
        wh = cfg["whales"]
        assert wh["geos"] == ["US", "GB", "AE", "SA", "DE", "FR", "NL", "ES", "IT", "SE", "CA", "AU"]
        assert wh["whop_min_pool_usd"] == 5000
        assert wh["max_dms_per_company"] == 2
        assert wh["secondary_dm_wait_days"] == 6
        assert wh["tg_group_admins_enabled"] is True
        assert wh["phone_tg_checker_daily_limit"] == 120
        assert wh["phone_tg_checker_batch_size"] == 15

        # Vacancies v2 section (Gate D requires default enabled == False)
        vac = cfg["vacancies"]
        assert vac["enabled"] is False
        assert vac["layers"] == {
            "AI-контент": True,
            "трафик": True,
            "автоматизация": False,
            "AI-роль": False,
        }
        for src_key in ("tg_chats", "hh_ru", "profi_ru", "kwork", "linkedin_jobs", "upwork"):
            assert src_key in vac["sources"]
        assert vac["vps_scraper_session"] == "/opt/crmtg/data/scraper_sessions/jobs_reader.session"
        assert vac["editable_prompt_override"] == ""
        assert vac["showcase_url"] == "https://tmxfy.github.io/"

        # RU-Experts section (hidden from Airtable)
        ru_exp = cfg["ru_experts"]
        assert ru_exp["enabled"] is False
        assert ru_exp["target_campaign"] == CAMPAIGN_RU_EXPERTS_TG
        assert ru_exp["exclude_from_airtable"] is True
        assert ru_exp["min_subs"] == 1000
        assert ru_exp["max_subs"] == 50000
        assert ru_exp["ig_to_tg_enabled"] is True
        assert ru_exp["daily_limit"] == 20
        for list_k in ("niches", "stop_words", "seed_channels", "seed_chats"):
            assert isinstance(ru_exp[list_k], list)
        assert isinstance(ru_exp.get("ig_handles", []), list)

        # SPEC Section 7 deletions & new toggles on b2b
        b2b = cfg["b2b"]
        assert b2b["target_campaign_tg"] == CAMPAIGN_WHALES_EN_TG
        assert b2b["limits"]["external_limit"] == 40
        groups_low = {g.lower() for g in b2b["tg_groups_new"]}
        assert not (groups_low & {"solofounderschat", "indiehackers", "buildinpublic"})
        for src_toggle in ("hh_ru", "profi_ru", "kwork", "upwork", "tg_group_admins", "ru_experts"):
            assert src_toggle in b2b["sources"]

    assert DEFAULT_LEADGEN_CONFIG["ru_experts"]["ig_handles"] == []

    # Backward compatibility: legacy on-disk JSON without 'enrichment' and with custom vacancies.layers
    legacy_cfg_path = cfg_path.parent / "legacy_leadgen_config.json"
    legacy_disk_payload = {
        "enabled": True,
        "vacancies": {
            "enabled": False,
            "layers": {
                "AI-контент": True,
                "трафик": True,
                "автоматизация": True,
                "AI-роль": True,
            },
        },
    }
    legacy_cfg_path.write_text(json.dumps(legacy_disk_payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(legacy_cfg_path))
    legacy_conn = connect(cfg_path.parent / "legacy_cfg.db")
    init_db(legacy_conn)
    try:
        loaded_legacy = get_leadgen_config(legacy_conn)
        assert loaded_legacy["enrichment"]["per_lead_cap_usd"] == 0.05
        assert loaded_legacy["vacancies"]["layers"] == legacy_disk_payload["vacancies"]["layers"]
    finally:
        legacy_conn.close()


def test_normalize_and_get_leadgen_config_unlinks_legacy_baza_and_noisy_groups(isolated_crm) -> None:
    conn, _state_dir, _cfg_path = isolated_crm

    legacy_payload = {
        "b2b": {
            "target_campaign_tg": "SoftUGC — База Все2 (2 045 лидов)",
            "tg_groups_new": ["solofounderschat", "@indiehackers", "buildinpublic", "SaaSFounders"],
        },
        "enrichment": {"daily_budget_cap_usd": 12.5, "super_whale_icp_min": 95},
        "whales": {"geos": ["us", "gb", "AE"], "whop_min_pool_usd": 7500},
        "ru_experts": {
            "min_subs": 2000,
            "max_subs": 30000,
            "exclude_from_airtable": False,
            "ig_handles": [
                "@Expert_RU",
                "https://instagram.com/SecondExpert/?hl=en",
                "expert_ru",
            ],
        },
    }
    norm = _normalize_config(legacy_payload)
    assert norm["b2b"]["target_campaign_tg"] == CAMPAIGN_WHALES_EN_TG
    assert norm["b2b"]["tg_groups_new"] == ["SaaSFounders"]
    assert norm["enrichment"]["daily_budget_cap_usd"] == 12.5
    assert norm["enrichment"]["super_whale_icp_min"] == 95
    assert norm["whales"]["geos"] == ["US", "GB", "AE"]
    assert norm["whales"]["whop_min_pool_usd"] == 7500
    assert norm["ru_experts"]["exclude_from_airtable"] is True  # Hard privacy invariant
    assert norm["ru_experts"]["ig_handles"] == ["expert_ru", "secondexpert"]

    # Persist into DB & verify get_leadgen_config / resolve_target_b2b_tg_campaign_name never pick База Все2
    create_campaign(conn, name="SoftUGC — База Все2 (2 045 лидов)", status="paused", daily_limit=30)
    save_leadgen_config(conn, legacy_payload)
    loaded = get_leadgen_config(conn)
    assert loaded["b2b"]["target_campaign_tg"] == CAMPAIGN_WHALES_EN_TG
    assert loaded["ru_experts"]["ig_handles"] == ["expert_ru", "secondexpert"]
    assert resolve_target_b2b_tg_campaign_name(conn, "SoftUGC — База Все2 (2 045 лидов)") == CAMPAIGN_WHALES_EN_TG

    with pytest.raises(ValueError):
        _normalize_config({"ru_experts": {"min_subs": 40000, "max_subs": 5000}})


def test_sync_leadgen_campaigns_idempotent_draft_segments_never_running(isolated_crm) -> None:
    conn, _state_dir, _cfg_path = isolated_crm
    create_campaign(conn, name="SoftUGC — База Все2 (2 045 лидов)", status="paused", daily_limit=30)

    first = sync_leadgen_campaigns(conn)
    second = sync_leadgen_campaigns(conn)

    expected_names = {
        CAMPAIGN_WHALES_EN_TG,
        CAMPAIGN_WHALES_EN_WA,
        CAMPAIGN_VACANCY_RU_TG,
        CAMPAIGN_VACANCY_RU_WA,
        CAMPAIGN_VACANCY_EN_TG,
        CAMPAIGN_RU_EXPERTS_TG,
    }
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["synced_count"] == 6
    assert second["synced_count"] == 6
    assert {c["name"] for c in first["campaigns"]} == expected_names
    assert {c["name"] for c in second["campaigns"]} == expected_names

    rows = [as_dict(r) for r in conn.execute("SELECT * FROM campaigns ORDER BY id ASC").fetchall()]
    by_name = {r["name"]: r for r in rows}
    assert set(expected_names).issubset(by_name.keys())

    for cname in expected_names:
        c = by_name[cname]
        assert c["status"] == "draft", f"{cname} must start in draft, never running"

    assert by_name[CAMPAIGN_WHALES_EN_TG]["segment"] == "whale"
    assert by_name[CAMPAIGN_WHALES_EN_TG]["language"] == "en"
    assert by_name[CAMPAIGN_WHALES_EN_TG]["copy_profile"] == "whale_mass"
    assert int(by_name[CAMPAIGN_WHALES_EN_TG]["exclude_from_airtable"]) == 0

    assert by_name[CAMPAIGN_WHALES_EN_WA]["segment"] == "whale"
    assert by_name[CAMPAIGN_WHALES_EN_WA]["copy_profile"] == "whale_mass"
    wa_steps = conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order ASC",
        (by_name[CAMPAIGN_WHALES_EN_WA]["id"],),
    ).fetchall()
    assert len(wa_steps) == 3
    assert all(not r["image_url"] for r in wa_steps)

    assert by_name[CAMPAIGN_VACANCY_RU_TG]["segment"] == "vacancy"
    assert by_name[CAMPAIGN_VACANCY_RU_TG]["language"] == "ru"
    assert by_name[CAMPAIGN_VACANCY_RU_TG]["copy_profile"] == "vacancy_ru"

    assert by_name[CAMPAIGN_VACANCY_RU_WA]["segment"] == "vacancy"
    assert by_name[CAMPAIGN_VACANCY_RU_WA]["language"] == "ru"
    assert by_name[CAMPAIGN_VACANCY_RU_WA]["copy_profile"] == "vacancy_ru"

    assert by_name[CAMPAIGN_VACANCY_EN_TG]["segment"] == "vacancy"
    assert by_name[CAMPAIGN_VACANCY_EN_TG]["language"] == "en"
    assert by_name[CAMPAIGN_VACANCY_EN_TG]["copy_profile"] == "vacancy_en"

    assert by_name[CAMPAIGN_RU_EXPERTS_TG]["segment"] == "ru_expert"
    assert by_name[CAMPAIGN_RU_EXPERTS_TG]["language"] == "ru"
    assert by_name[CAMPAIGN_RU_EXPERTS_TG]["copy_profile"] == "ru_expert"
    assert int(by_name[CAMPAIGN_RU_EXPERTS_TG]["exclude_from_airtable"]) == 1
    ru_steps = conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order ASC",
        (by_name[CAMPAIGN_RU_EXPERTS_TG]["id"],),
    ).fetchall()
    assert len(ru_steps) == 2

    assert resolve_target_b2b_tg_campaign_name(conn) == CAMPAIGN_WHALES_EN_TG


def test_mcp_ingest_persists_v2_lead_columns_copy_profiles_and_airtable_exclusion(isolated_crm) -> None:
    conn, _state_dir, _cfg_path = isolated_crm

    res = _handle_ingest_discovered_leads(
        conn,
        {
            "enroll_in_campaign": True,
            "leads": [
                {
                    "name": "Sarah Jenkins",
                    "company": "GlowSkin",
                    "username": "sarah_glowskin",
                    "website": "https://www.GlowSkin.io/pages/creator-club",
                    "category": "Beauty D2C",
                    "their_angle": "Running $15K monthly TikTok UGC campaigns",
                    "source": "whop_content_rewards",
                    "segment": "whale",
                    "language": "en",
                    "pool_usd": "$15,000",
                    "secondary_contact": {"name": "Mike", "role": "CMO", "username": "mike_glow"},
                },
                {
                    "name": "Liam Carter",
                    "company": "FitPulse",
                    "provider": "whatsapp",
                    "phone": "+14155550199",
                    "website": "https://fitpulse.app",
                    "category": "Fitness App",
                    "their_angle": "Scaling daily Reels creator payouts",
                    "source": "whop_content_rewards",
                    "segment": "whale",
                    "language": "en",
                    "pool_usd": 8000,
                },
                {
                    "name": "Артём Соколов",
                    "company": "AiFlow Club",
                    "username": "artem_aiflow",
                    "their_angle": "Эксперт по нейросетям в Telegram, 14к подписчиков",
                    "segment": "ru_expert",
                    "language": "ru",
                    "source": "ru_experts",
                },
            ],
        },
    )

    assert res["ok"] is True
    assert res["created_leads"] == 3
    assert res["enrolled_in_campaign"] == 3

    # Check leads v2 columns persisted accurately
    whale_tg_lead = as_dict(
        conn.execute("SELECT * FROM leads WHERE username = 'sarah_glowskin'").fetchone()
    )
    assert whale_tg_lead["segment"] == "whale"
    assert whale_tg_lead["language"] == "en"
    assert int(whale_tg_lead["exclude_from_airtable"]) == 0
    assert whale_tg_lead["company_domain"] == "glowskin.io"
    assert int(whale_tg_lead["pool_usd"]) == 15000
    sec_parsed = json.loads(whale_tg_lead["secondary_contact_json"])
    assert sec_parsed["username"] == "mike_glow"

    ru_lead = as_dict(
        conn.execute("SELECT * FROM leads WHERE username = 'artem_aiflow'").fetchone()
    )
    assert ru_lead["segment"] == "ru_expert"
    assert ru_lead["language"] == "ru"
    assert int(ru_lead["exclude_from_airtable"]) == 1

    # Verify target campaigns remain draft (never auto-started) & copy profiles match
    campaigns_by_name = {
        r["name"]: as_dict(r)
        for r in conn.execute("SELECT * FROM campaigns").fetchall()
    }
    assert CAMPAIGN_WHALES_EN_TG in campaigns_by_name
    assert CAMPAIGN_WHALES_EN_WA in campaigns_by_name
    assert CAMPAIGN_RU_EXPERTS_TG in campaigns_by_name
    for cname in (CAMPAIGN_WHALES_EN_TG, CAMPAIGN_WHALES_EN_WA, CAMPAIGN_RU_EXPERTS_TG):
        assert campaigns_by_name[cname]["status"] == "draft"

    whale_tg_rec = as_dict(
        conn.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND username = 'sarah_glowskin'",
            (campaigns_by_name[CAMPAIGN_WHALES_EN_TG]["id"],),
        ).fetchone()
    )
    assert whale_tg_rec["status"] == "pending"
    assert "revshare" not in whale_tg_rec["step1_text"].lower()
    assert "revenue share" not in whale_tg_rec["step1_text"].lower()
    assert "creatoros" not in whale_tg_rec["step1_text"].lower()
    assert "$15,000" in whale_tg_rec["step1_text"] or "whop" in whale_tg_rec["step1_text"].lower()

    whale_wa_rec = as_dict(
        conn.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ?",
            (campaigns_by_name[CAMPAIGN_WHALES_EN_WA]["id"],),
        ).fetchone()
    )
    assert whale_wa_rec["status"] == "pending"
    assert "revshare" not in whale_wa_rec["step1_text"].lower()
    assert whale_wa_rec["step3_text"] is not None
    assert "http" not in whale_wa_rec["step3_text"].lower()  # WA has no meme image URL

    ru_rec = as_dict(
        conn.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ?",
            (campaigns_by_name[CAMPAIGN_RU_EXPERTS_TG]["id"],),
        ).fetchone()
    )
    assert ru_rec["status"] == "pending"
    assert any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in ru_rec["step1_text"])
    assert ru_rec["step2_text"]
    assert ru_rec["step3_text"] is None  # 2-step RU expert chain

    # Also verify non-ru_expert lead inherits campaign.exclude_from_airtable == 1 when enrolled directly
    priv_camp = create_campaign(conn, name="Private Partner Sync", status="draft", daily_limit=10)
    conn.execute("UPDATE campaigns SET exclude_from_airtable = 1 WHERE id = ?", (priv_camp["id"],))
    conn.commit()
    _handle_ingest_discovered_leads(
        conn,
        {
            "campaign_id": priv_camp["id"],
            "enroll_in_campaign": True,
            "leads": [
                {
                    "name": "Derek Wu",
                    "company": "ClipScale",
                    "username": "derek_clipscale",
                    "website": "https://clipscale.co",
                    "category": "B2B SaaS",
                    "their_angle": "Builds automated video analytics for startups",
                    "source": "Product Hunt",
                }
            ],
        },
    )
    derek_lead = as_dict(conn.execute("SELECT * FROM leads WHERE username = 'derek_clipscale'").fetchone())
    assert int(derek_lead["exclude_from_airtable"]) == 1


def test_mcp_ingest_keeps_manual_and_assisted_leads_out_of_campaign_recipients(isolated_crm) -> None:
    conn, _state_dir, _cfg_path = isolated_crm

    res = _handle_ingest_discovered_leads(
        conn,
        {
            "enroll_in_campaign": True,
            "leads": [
                {
                    "name": "Elena Vance",
                    "company": "VelvetAI",
                    "segment": "social_manual",
                    "language": "en",
                    "linkedin_url": "https://linkedin.com/in/elenavance",
                    "website": "https://velvetai.com",
                    "category": "AI SaaS",
                    "their_angle": "Scaling consumer creator workflows on TikTok",
                    "source": "linkedin_sales",
                },
                {
                    "name": "HR Студия",
                    "company": "TrendLab",
                    "segment": "vacancy_assisted",
                    "language": "ru",
                    "source": "hh_ru",
                    "manual_action_url": "https://hh.ru/vacancy/998877",
                    "their_angle": "Ищем рилсмейкера и UGC продюсера в проект",
                },
            ],
        },
    )

    assert res["ok"] is True
    assert res["created_leads"] == 2
    assert res["enrolled_in_campaign"] == 0
    assert len(res["lead_ids"]) == 2

    rec_cnt = conn.execute("SELECT COUNT(*) FROM campaign_recipients").fetchone()[0]
    assert int(rec_cnt) == 0

    rows = [as_dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY id ASC").fetchall()]
    assert len(rows) == 2

    social_row, vac_row = rows[0], rows[1]
    assert social_row["segment"] == "social_manual"
    assert social_row["manual_action_url"] == "https://linkedin.com/in/elenavance"
    assert social_row["manual_draft_text"]

    assert vac_row["segment"] == "vacancy_assisted"
    assert vac_row["language"] == "ru"
    assert vac_row["manual_action_url"] == "https://hh.ru/vacancy/998877"
    assert "https://tmxfy.github.io/" in vac_row["manual_draft_text"]


def test_mcp_ingest_vacancy_ru_language_guard_and_baza2_unlink(isolated_crm) -> None:
    conn, _state_dir, _cfg_path = isolated_crm

    # 1. Legacy SoftUGC — База Все2 name must unlink onto Whales EN — TG
    res_unlink = _handle_ingest_discovered_leads(
        conn,
        {
            "campaign_name": "SoftUGC — База Все2 (2 045 лидов)",
            "enroll_in_campaign": True,
            "leads": [
                {
                    "name": "Mark Reed",
                    "company": "PulseLab",
                    "username": "mark_pulselab",
                    "website": "https://pulselab.io",
                    "category": "SaaS",
                    "their_angle": "Automated video analytics for mobile teams",
                    "source": "whop_content_rewards",
                    "pool_usd": 12000,
                }
            ],
        },
    )
    assert res_unlink["enrolled_in_campaign"] == 1
    baza_camp = conn.execute(
        "SELECT * FROM campaigns WHERE name LIKE '%База Все2%'"
    ).fetchone()
    assert baza_camp is None
    whales_camp = as_dict(
        conn.execute("SELECT * FROM campaigns WHERE name = ?", (CAMPAIGN_WHALES_EN_TG,)).fetchone()
    )
    assert whales_camp["status"] == "draft"

    # 2. RU vacancy auto-generates RU outreach copy referencing showcase_url
    res_vac = _handle_ingest_discovered_leads(
        conn,
        {
            "enroll_in_campaign": True,
            "leads": [
                {
                    "name": "Илья Ковалёв",
                    "username": "ilya_reels_hr",
                    "segment": "vacancy",
                    "language": "ru",
                    "source": "tg_chats",
                    "their_angle": "Нужен рилсмейкер для ежедневных Reels в Telegram-канал бренда",
                }
            ],
        },
    )
    assert res_vac["enrolled_in_campaign"] == 1
    vac_camp = as_dict(
        conn.execute("SELECT * FROM campaigns WHERE name = ?", (CAMPAIGN_VACANCY_RU_TG,)).fetchone()
    )
    assert vac_camp["status"] == "draft"
    vac_rec = as_dict(
        conn.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ?",
            (vac_camp["id"],),
        ).fetchone()
    )
    assert vac_rec["status"] == "pending"
    assert "https://tmxfy.github.io/" in vac_rec["step1_text"]

    # 3. Wrong-language explicit step1_text into EN campaign must NOT queue as pending
    res_bad_lang = _handle_ingest_discovered_leads(
        conn,
        {
            "campaign_name": CAMPAIGN_WHALES_EN_TG,
            "enroll_in_campaign": True,
            "leads": [
                {
                    "name": "Bad Lang Whale",
                    "company": "TestCo",
                    "username": "badlang_whale",
                    "website": "https://testco.ai",
                    "category": "AI SaaS",
                    "their_angle": "Building AI video workflows",
                    "segment": "whale",
                    "language": "en",
                    "step1_text": "Привет! Мы делаем UGC-ролики для брендов и хотим предложить сотрудничество.",
                }
            ],
        },
    )
    assert res_bad_lang["enrolled_in_campaign"] == 1
    bad_rec = as_dict(
        conn.execute(
            "SELECT * FROM campaign_recipients WHERE username = 'badlang_whale'"
        ).fetchone()
    )
    assert bad_rec["status"] == "failed"
    assert "language_mismatch_en" in (bad_rec["error_text"] or "")


def test_push_discovered_now_routes_via_language_router_and_guards_ru_experts(isolated_crm) -> None:
    conn, state_dir, _cfg_path = isolated_crm
    sync_leadgen_campaigns(conn)

    # Write discovered Whale + Queue JSONL + RU Expert JSONL
    (state_dir / "discovered_leads.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "name": "Dan Miller",
                        "company": "TrackFlo",
                        "username": "dan_trackflo",
                        "website": "https://trackflo.io",
                        "category": "AI SaaS",
                        "their_angle": "Creator analytics SaaS for mobile video apps",
                        "source": "whop_content_rewards",
                        "segment": "whale",
                        "pool_usd": 9000,
                        "icp_score": 92,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "name": "Noah LinkedIn",
                        "company": "ScaleClip",
                        "segment": "social_manual",
                        "linkedin_url": "https://linkedin.com/in/noahscale",
                        "website": "https://scaleclip.io",
                        "category": "SaaS",
                        "their_angle": "B2B video platform scaling paid creator hooks",
                        "source": "linkedin_sales",
                        "icp_score": 90,
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "ru_experts.jsonl").write_text(
        json.dumps(
            {
                "name": "Ольга Белова",
                "username": "olga_edtech",
                "segment": "ru_expert",
                "language": "ru",
                "source": "ru_experts",
                "their_angle": "Продюсирование онлайн-курсов и Telegram-воронок",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    # ru_experts disabled by default -> expert must not be pushed yet, whale -> Whales EN — TG, social_manual -> leads only
    res1 = push_discovered_now(conn)
    assert res1["ok"] is True
    assert res1["ingested"] == 2
    assert conn.execute("SELECT * FROM leads WHERE username = 'olga_edtech'").fetchone() is None

    whale_cnt = conn.execute(
        """
        SELECT COUNT(*) FROM campaign_recipients cr
        JOIN campaigns c ON c.id = cr.campaign_id
        WHERE c.name = ?
        """,
        (CAMPAIGN_WHALES_EN_TG,),
    ).fetchone()[0]
    assert int(whale_cnt) == 1
    whale_rec = as_dict(
        conn.execute("SELECT * FROM campaign_recipients WHERE username = 'dan_trackflo'").fetchone()
    )
    assert "revshare" not in (whale_rec["step1_text"] or "").lower()

    noah_lead = as_dict(
        conn.execute("SELECT * FROM leads WHERE manual_action_url = 'https://linkedin.com/in/noahscale'").fetchone()
    )
    assert noah_lead["segment"] == "social_manual"
    noah_in_rec = conn.execute(
        "SELECT COUNT(*) FROM campaign_recipients WHERE lead_id = ?",
        (noah_lead["id"],),
    ).fetchone()[0]
    assert int(noah_in_rec) == 0

    # Enable ru_experts -> expert routes to РФ-Эксперты with exclude_from_airtable == 1
    save_leadgen_config(conn, {"ru_experts": {"enabled": True}})
    res2 = push_discovered_now(conn)
    assert res2["ok"] is True
    assert res2["ingested"] >= 1

    olga_lead = as_dict(conn.execute("SELECT * FROM leads WHERE username = 'olga_edtech'").fetchone())
    assert int(olga_lead["exclude_from_airtable"]) == 1
    assert olga_lead["segment"] == "ru_expert"
