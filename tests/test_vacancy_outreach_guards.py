from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))

from app.db import connect, init_db, utc_now
from app.leadgen_settings import (
    DEFAULT_B2B_STEPS,
    DEFAULT_LEADGEN_CONFIG,
    DEFAULT_VACANCY_STEPS,
    get_leadgen_config,
    save_leadgen_config,
    sync_leadgen_campaigns,
)
from app.outreach import (
    create_campaign,
    find_vacancy_account_pool_violations,
    get_campaign,
    preview_outreach_batch,
    run_campaign_batch,
    set_autopilot_enabled,
)
from app.telegram_gateway import FakeGateway
from leadgen.job_monitor import prefilter as monitor_prefilter


def _seed_tg_account(
    conn,
    acc_id: int,
    username: str,
    name: str = "Test Acc",
    *,
    daily_limit: int = 20,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, provider,
            telegram_user_id, telegram_username, telegram_name,
            daily_limit, sent_today, created_at, updated_at
        ) VALUES (?, ?, ?, 'connected', 'active', 'telegram', ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            acc_id,
            f"+1555000{acc_id:04d}",
            f"data/sessions/{acc_id}.session",
            8000000 + acc_id,
            username,
            name,
            daily_limit,
            now,
            now,
        ),
    )
    conn.commit()


def test_vacancies_disabled_by_default_across_backend_json_and_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg_file = tmp_path / "leadgen_config.json"
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))
    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "0")

    conn = connect(tmp_path / "vac_default_off.db")
    init_db(conn)
    try:
        assert DEFAULT_LEADGEN_CONFIG["vacancies"]["enabled"] is False
        cfg = get_leadgen_config(conn)
        assert cfg["vacancies"]["enabled"] is False

        # Sync campaigns must NOT create 'TG Вакансии' when vacancies.enabled is False
        sync_res = sync_leadgen_campaigns(conn, cfg)
        synced_names = [c["name"] for c in sync_res["campaigns"]]
        assert "TG Вакансии" not in synced_names
        assert "Whales EN — TG" in synced_names
        assert "Whales EN — WA" in synced_names

        # Frontend JS state and HTML checkbox must also default to disabled
        app_js = (_WORKTREE_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        assert "vacancies: {\n        enabled: false," in app_js

        index_html = (_WORKTREE_ROOT / "app" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert 'id="leadgen-vacancies-enabled" checked' not in index_html
        assert 'id="leadgen-vacancies-enabled"' in index_html
    finally:
        conn.close()


def test_migration_v6_pauses_vacancy_campaign_and_closes_pending_and_in_progress_preserving_b2b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg_file = tmp_path / "leadgen_config.json"
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))
    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "0")

    db_path = tmp_path / "prod_shape_v6.db"
    conn = connect(db_path)
    init_db(conn)
    _seed_tg_account(conn, 27, "vladwilhelm", "Vladislav Wilhelm")
    _seed_tg_account(conn, 30, "subscale", "Тимофей Орлёнок")

    b2b_camp = create_campaign(
        conn,
        name="SoftUGC — База Все2 (2 045 лидов)",
        account_ids=[27, 30],
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )
    vac_camp = create_campaign(
        conn,
        name="TG Вакансии",
        account_ids=[27, 30],
        daily_limit=20,
        status="draft",
        steps=DEFAULT_VACANCY_STEPS,
        leadgen_enabled=True,
        leadgen_daily_quota=20,
    )
    # Simulate pre-existing legacy running state created before the account-pool guard existed
    conn.execute("UPDATE campaigns SET status = 'running' WHERE id = ?", (vac_camp["id"],))
    save_leadgen_config(conn, {"vacancies": {"enabled": True, "daily_limit": 20}})

    now = "2026-11-17T08:35:00"
    conn.executemany(
        """
        INSERT INTO campaign_recipients (
            campaign_id, username, name, company, step1_text,
            current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # B2B rows -> must remain 100% untouched
            (b2b_camp["id"], "b2b_pending_1", "Alex", "SaaSCo", "Hey Alex", 0, "pending", None, now, now),
            (b2b_camp["id"], "b2b_inprog_1", "Ben", "AppCo", "Hey Ben", 1, "in_progress", now, now, now),
            # Vacancy rows -> pending & in_progress must close to completed; replied stays replied
            (vac_camp["id"], "vac_stale_pending", "Монтажёр (@vac_stale_pending)", "Монтажёр", "Добрый день!", 0, "pending", None, "2026-11-06T13:58:01", now),
            (vac_camp["id"], "vac_sent_inprog", "AI-креатор (@vac_sent_inprog)", "AI-креатор", "Добрый день!", 1, "in_progress", now, "2026-11-06T13:58:01", now),
            (vac_camp["id"], "CalcarDaekonDNote", "Сторисмейкер (@CalcarDaekonDNote)", "Сторисмейкер", "Добрый день!", 1, "replied", now, "2026-11-06T13:58:01", now),
        ],
    )
    # Remove v6 migration marker so re-running init_db simulates upgrading an existing prod DB
    conn.execute("DELETE FROM kv_settings WHERE key = 'disable_vacancy_outreach_v6'")
    conn.commit()

    init_db(conn)
    try:
        after_vac = get_campaign(conn, vac_camp["id"])
        assert after_vac["status"] == "paused"
        assert after_vac["account_ids"] == []
        assert bool(after_vac["leadgen_enabled"]) is False
        assert after_vac["leadgen_daily_quota"] == 0

        vac_rows = {
            r["username"]: dict(r)
            for r in conn.execute(
                "SELECT username, status, current_step, error_text FROM campaign_recipients WHERE campaign_id = ?",
                (vac_camp["id"],),
            ).fetchall()
        }
        assert vac_rows["vac_stale_pending"]["status"] == "completed"
        assert vac_rows["vac_sent_inprog"]["status"] == "completed"
        assert vac_rows["CalcarDaekonDNote"]["status"] == "replied"

        after_b2b = get_campaign(conn, b2b_camp["id"])
        assert after_b2b["status"] == "running"
        b2b_rows = {
            r["username"]: r["status"]
            for r in conn.execute(
                "SELECT username, status FROM campaign_recipients WHERE campaign_id = ?",
                (b2b_camp["id"],),
            ).fetchall()
        }
        assert b2b_rows == {"b2b_pending_1": "pending", "b2b_inprog_1": "in_progress"}

        assert get_leadgen_config(conn)["vacancies"]["enabled"] is False
    finally:
        conn.close()


def test_hard_account_isolation_blocks_vladwilhelm_and_b2b_pool_for_vacancies(
    tmp_path: Path,
):
    conn = connect(tmp_path / "vac_isolation.db")
    init_db(conn)
    _seed_tg_account(conn, 27, "vladwilhelm", "Vladislav Wilhelm")
    _seed_tg_account(conn, 31, "make1tlo", "Sergey Glebov")
    _seed_tg_account(conn, 99, "dedicated_vac_sender", "SoftUGC Jobs")

    create_campaign(
        conn,
        name="SoftUGC — База Все2",
        account_ids=[31],
        daily_limit=40,
        status="running",
        steps=DEFAULT_B2B_STEPS,
    )

    # 1) Vacancy campaign assigned to @vladwilhelm (#27) -> blocked in preview & run_campaign_batch
    vac_vlad = create_campaign(
        conn,
        name="TG Вакансии (Vlad Pool)",
        account_ids=[27],
        daily_limit=10,
        status="paused",
        steps=DEFAULT_VACANCY_STEPS,
    )
    now = utc_now()
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, step1_text,
            current_step, status, created_at, updated_at
        ) VALUES (?, 700101, 'ai_studio_founder', 'AI Producer (@ai_studio_founder)', 'AI Producer',
                  'Добрый день! Готовы взять продакшн 90 AI-роликов в месяц под ключ через SoftUGC. Какой объём нужен?',
                  0, 'pending', ?, ?)
        """,
        (vac_vlad["id"], now, now),
    )
    conn.commit()

    viol_vlad = find_vacancy_account_pool_violations(conn, vac_vlad["id"])
    assert any(v["code"] == "vacancy_on_protected_account" for v in viol_vlad)

    prev_vlad = preview_outreach_batch(conn, vac_vlad["id"], strict=False)
    assert prev_vlad["ok"] is False
    assert any(v["code"] == "vacancy_on_protected_account" for v in prev_vlad["violations"])

    gw = FakeGateway()
    with pytest.raises(ValueError, match="vladwilhelm|ваканс"):
        asyncio.run(run_campaign_batch(conn, vac_vlad["id"], gateway=gw, max_send=1))

    # 2) Vacancy campaign sharing account #31 with B2B campaign -> blocked
    vac_shared = create_campaign(
        conn,
        name="TG Вакансии (Shared B2B)",
        account_ids=[31],
        daily_limit=10,
        status="paused",
        steps=DEFAULT_VACANCY_STEPS,
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, step1_text,
            current_step, status, created_at, updated_at
        ) VALUES (?, 700102, 'ai_agency_owner', 'AI Video Lead (@ai_agency_owner)', 'AI Video Lead',
                  'Добрый день! Закроем поток AI UGC роликов командой SoftUGC. Какой хронометраж?',
                  0, 'pending', ?, ?)
        """,
        (vac_shared["id"], now, now),
    )
    conn.commit()

    viol_shared = find_vacancy_account_pool_violations(conn, vac_shared["id"])
    assert any(v["code"] == "vacancy_shares_b2b_account_pool" for v in viol_shared)

    with pytest.raises(ValueError, match="B2B|пул|ваканс"):
        asyncio.run(run_campaign_batch(conn, vac_shared["id"], gateway=gw, max_send=1))

    # 3) set_autopilot_enabled(activate_drafts=True) must NEVER flip paused/draft vacancy campaigns to running
    set_autopilot_enabled(conn, True, activate_drafts=True)
    assert get_campaign(conn, vac_vlad["id"])["status"] == "paused"
    assert get_campaign(conn, vac_shared["id"])["status"] == "paused"

    # 4) Dedicated non-B2B account #99 has zero account-pool violations
    vac_dedicated = create_campaign(
        conn,
        name="TG Вакансии (Dedicated)",
        account_ids=[99],
        daily_limit=10,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, step1_text,
            current_step, status, created_at, updated_at
        ) VALUES (?, 700103, 'clean_ai_client', 'AI UGC Продюсер (@clean_ai_client)', 'AI UGC Продюсер',
                  'Добрый день! Команда SoftUGC закроет поток AI UGC креативов под ваш оффер. Какой объём роликов в месяц?',
                  0, 'pending', ?, ?)
        """,
        (vac_dedicated["id"], now, now),
    )
    conn.commit()
    assert find_vacancy_account_pool_violations(conn, vac_dedicated["id"]) == []
    sent_res = asyncio.run(run_campaign_batch(conn, vac_dedicated["id"], gateway=gw, max_send=1))
    assert sent_res["sent"] == 1
    conn.close()


def test_storymaker_junk_and_stale_vacancies_blocked_in_monitor_and_last_mile(
    tmp_path: Path,
):
    # 1) job_monitor prefilter drops storymaker, kids-book illustration, and meme montage posts immediately
    raw_posts = [
        {
            "text": "Ищу сторисмейкера для личного бренда! Простой монтаж (склейка кадров, титры) и оформление сторис 1-2 сторис 3-4 раза в неделю, оплата 10-15 тысяч в месяц. Писать @CalcarDaekonDNote"
        },
        {
            "text": "Нужен иллюстратор детской книги нейросетью — 15 разворотов с одним узнаваемым персонажем, бюджет 40 000 руб за полтора месяца. Писать в личку @asel_pest_hr"
        },
        {
            "text": "Ищем рилсмейкера на монтаж мемов и развлекательных роликов! Рассматриваем новичков, только склейка без сценариев, график 10-18 мск. Отклик: @miiiilka_vooo"
        },
        {
            "text": "Ищем подрядчика / AI UGC продюсера на производство 60 вертикальных рекламных креативов в месяц для мобильного приложения (США), бюджет от $2500/мес, удалённо. Контакт @us_app_growth"
        },
    ]
    kept = monitor_prefilter(raw_posts, min_len=80)
    assert len(kept) == 1
    assert "@us_app_growth" in kept[0]["text"]

    # 2) Last-mile outreach guard blocks storymakers AND stale >72h Step 1 vacancies even on a dedicated account
    conn = connect(tmp_path / "vac_last_mile.db")
    init_db(conn)
    _seed_tg_account(conn, 99, "dedicated_vac_sender", "SoftUGC Jobs")

    vac_camp = create_campaign(
        conn,
        name="TG Вакансии Dedicated",
        account_ids=[99],
        daily_limit=20,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    conn.executemany(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, their_angle,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?)
        """,
        [
            (
                vac_camp["id"],
                701001,
                "CalcarDaekonDNote",
                "Сторисмейкер для личного бренда (@CalcarDaekonDNote)",
                "Сторисмейкер для личного бренда",
                "Простой монтаж и оформление сторис за 10-15 тыс. в месяц",
                "Добрый день! По сторисмейкеру для личного бренда. Какой стиль аккаунта?",
                "2026-11-17T10:00:00",
                "2026-11-17T10:00:00",
            ),
            (
                vac_camp["id"],
                701002,
                "stale_nov6_hr",
                "AI-креатор для Reels (@stale_nov6_hr)",
                "AI-креатор для Reels",
                "Создание коротких видео с AI",
                "Добрый день! По AI-креатору для Reels. Какой объём роликов в месяц?",
                "2026-11-06T13:58:01",
                "2026-11-06T13:58:01",
            ),
            (
                vac_camp["id"],
                701003,
                "fresh_ai_ugc_hr",
                "AI UGC продюсер (@fresh_ai_ugc_hr)",
                "AI UGC продюсер",
                "Производство 60 AI UGC роликов в месяц, $2500/мес",
                "Добрый день! Команда SoftUGC готова закрыть 60 AI UGC роликов в месяц. Какой продукт продвигаете?",
                "2026-11-17T09:00:00",
                "2026-11-17T09:00:00",
            ),
        ],
    )
    conn.commit()

    gw = FakeGateway()
    now_dt = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
    res = asyncio.run(
        run_campaign_batch(
            conn,
            vac_camp["id"],
            gateway=gw,
            max_send=10,
            respect_delays=True,
            now_dt=now_dt,
        )
    )
    assert res["sent"] == 1

    statuses = {
        r["username"]: (r["status"], r["current_step"], r["error_text"] or "")
        for r in conn.execute(
            "SELECT username, status, current_step, error_text FROM campaign_recipients WHERE campaign_id = ?",
            (vac_camp["id"],),
        ).fetchall()
    }
    assert statuses["CalcarDaekonDNote"][0] == "completed"
    assert statuses["CalcarDaekonDNote"][1] == 0
    assert "ICP" in statuses["CalcarDaekonDNote"][2] or "сторис" in statuses["CalcarDaekonDNote"][2].lower()

    assert statuses["stale_nov6_hr"][0] == "completed"
    assert statuses["stale_nov6_hr"][1] == 0
    assert "устарев" in statuses["stale_nov6_hr"][2].lower() or "ICP" in statuses["stale_nov6_hr"][2]

    assert statuses["fresh_ai_ugc_hr"][0] == "completed"
    assert statuses["fresh_ai_ugc_hr"][1] == 1
    conn.close()


def test_skeptic_closures_no_auto_wakeup_no_qa_bypass_linkedin_jobs_safe_and_limit_zero_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.mcp_server import call_mcp_tool_handler
    from app.outreach import (
        enroll_leads_from_crm,
        heal_transient_failed_recipients,
        import_campaign_csv,
        is_vacancy_campaign,
        update_campaign,
    )
    from leadgen import job_jev, job_reply

    cfg_file = tmp_path / "leadgen_config.json"
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))

    conn = connect(tmp_path / "skeptic_vac.db")
    init_db(conn)
    _seed_tg_account(conn, 27, "vladwilhelm", "Vladislav Wilhelm")
    _seed_tg_account(conn, 31, "make1tlo", "Sergey Glebov")
    _seed_tg_account(conn, 99, "dedicated_vac_99", "Dedicated Vac")

    # 1) Legitimate B2B campaign named "SoftUGC — LinkedIn Jobs" MUST NOT be classified as vacancy
    b2b_li = create_campaign(
        conn,
        name="SoftUGC — LinkedIn Jobs",
        account_ids=[27, 31],
        daily_limit=30,
        status="running",
        steps=DEFAULT_B2B_STEPS,
    )
    assert is_vacancy_campaign(b2b_li) is False
    assert find_vacancy_account_pool_violations(conn, b2b_li["id"]) == []

    # 2) Renamed vacancy campaign ("TG Заказы") with leadgen_sources=["vacancies:AI-контент"] IS caught,
    # and [QA] prefix does NOT bypass @vladwilhelm protection!
    with pytest.raises(ValueError, match="vladwilhelm"):
        create_campaign(
            conn,
            name="[QA] TG Вакансии",
            account_ids=[27],
            status="running",
            steps=DEFAULT_VACANCY_STEPS,
        )
    renamed_vac = create_campaign(
        conn,
        name="TG Заказы Подряд",
        account_ids=[31],
        status="paused",
        leadgen_sources=["vacancies:AI-контент"],
        steps=DEFAULT_VACANCY_STEPS,
    )
    assert is_vacancy_campaign(renamed_vac) is True
    with pytest.raises(ValueError, match="B2B-пул"):
        update_campaign(conn, renamed_vac["id"], status="running")

    # 3) CSV import, CRM enroll, MCP ingest, and heal_transient_failed_recipients NEVER auto-wake vacancy campaigns
    vac_comp = create_campaign(
        conn,
        name="TG Вакансии Completed",
        account_ids=[99],
        status="completed",
        steps=DEFAULT_VACANCY_STEPS,
    )
    import_campaign_csv(
        conn,
        vac_comp["id"],
        "username,name,company,step1_text\n@vac_csv_1,AI Lead,AI Lead,\"Добрый день! Готовы взять AI UGC на потоке. Какой объём?\"\n",
    )
    assert get_campaign(conn, vac_comp["id"])["status"] == "completed"

    now = utc_now()
    cur_l = conn.execute(
        "INSERT INTO leads (username, name, company, status, created_at, updated_at) VALUES ('vac_crm_2', 'AI Role', 'AI Role', 'new', ?, ?)",
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, username, name, company, step1_text, current_step, status, error_text, created_at, updated_at
        ) VALUES (?, 'vac_failed_spambot', 'AI Role', 'AI Role', 'Добрый день! Какой объём?', 0, 'failed', '@SpamBot: account is now limited', ?, ?)
        """,
        (vac_comp["id"], now, now),
    )
    conn.commit()
    enroll_leads_from_crm(conn, vac_comp["id"], lead_ids=[int(cur_l.lastrowid)])
    assert get_campaign(conn, vac_comp["id"])["status"] == "completed"

    healed = heal_transient_failed_recipients(conn)
    assert healed == 0
    failed_row = conn.execute(
        "SELECT status FROM campaign_recipients WHERE username = 'vac_failed_spambot'"
    ).fetchone()
    assert failed_row["status"] == "failed"

    # 4) Stale lead_created_at (>72h) blocked even when cr.created_at is fresh AND respect_delays=False!
    vac_run = create_campaign(
        conn,
        name="TG Вакансии Freshness",
        account_ids=[99],
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    old_lead_id = int(
        conn.execute(
            "INSERT INTO leads (username, name, company, status, created_at, updated_at) VALUES ('old_lead_nov1', 'AI UGC', 'AI UGC', 'new', '2026-11-01T10:00:00', '2026-11-01T10:00:00')"
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name, company, step1_text,
            current_step, status, created_at, updated_at
        ) VALUES (?, ?, 799001, 'old_lead_nov1', 'AI UGC', 'AI UGC',
                  'Добрый день! Команда SoftUGC закроет поток AI UGC. Какой объём?',
                  0, 'pending', '2026-11-17T11:55:00', '2026-11-17T11:55:00')
        """,
        (vac_run["id"], old_lead_id),
    )
    conn.commit()
    gw = FakeGateway()
    batch_res = asyncio.run(
        run_campaign_batch(
            conn,
            vac_run["id"],
            gateway=gw,
            max_send=5,
            respect_delays=False,
            now_dt=datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc),
        )
    )
    assert batch_res["sent"] == 0
    st_row = conn.execute(
        "SELECT status, error_text FROM campaign_recipients WHERE username = 'old_lead_nov1'"
    ).fetchone()
    assert st_row["status"] == "completed" and "72ч" in (st_row["error_text"] or "")

    # 5) Expanded junk variants rejected in job_jev (zero HTTP call) and MCP ingest
    j_verdict = job_jev.verdict("Ищем сторис-мейкера на ведение сторис личного блога @hr_story")
    assert j_verdict["fit"] is False
    mcp_junk = call_mcp_tool_handler(
        conn,
        "ingest_discovered_leads",
        {
            "campaign_id": vac_comp["id"],
            "enroll_in_campaign": True,
            "leads": [
                {
                    "username": "@meme_cutter_hr",
                    "name": "Рилсмейкер на нарезку мемов",
                    "company": "Рилсмейкер на мемы",
                    "lead_source": "TG Вакансии",
                    "step1_text": "Добрый день! По нарезке мемов. Какой объём?",
                }
            ],
        },
    )
    assert mcp_junk["enrolled_in_campaign"] == 0

    # 6) MCP posted_at (>72h) is NOT shadowed by fresh leads.created_at = now in preview or run_campaign_batch
    mcp_stale_posted = call_mcp_tool_handler(
        conn,
        "ingest_discovered_leads",
        {
            "campaign_id": vac_run["id"],
            "enroll_in_campaign": True,
            "leads": [
                {
                    "username": "@mcp_posted_nov1",
                    "name": "AI Video Producer (@mcp_posted_nov1)",
                    "company": "AI Video Producer",
                    "lead_source": "TG Вакансии (AI Chat)",
                    "posted_at": "2026-11-01T09:00:00+00:00",
                    "step1_text": "Добрый день! Команда SoftUGC готова взять поток AI UGC. Какой объём роликов в месяц?",
                }
            ],
        },
    )
    assert mcp_stale_posted["enrolled_in_campaign"] == 1
    prev_stale = preview_outreach_batch(conn, vac_run["id"], strict=False)
    assert any(v["code"] == "stale_vacancy_lead" for v in prev_stale["violations"])
    batch_res_2 = asyncio.run(
        run_campaign_batch(
            conn,
            vac_run["id"],
            gateway=gw,
            max_send=5,
            respect_delays=False,
            now_dt=datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc),
        )
    )
    assert batch_res_2["sent"] == 0
    st_row_2 = conn.execute(
        "SELECT status, error_text FROM campaign_recipients WHERE username = 'mcp_posted_nov1'"
    ).fetchone()
    assert st_row_2["status"] == "completed" and "72ч" in (st_row_2["error_text"] or "")

    # 7) daily_limit = 0 in config stops job_reply.main() instead of treating 0 as unlimited
    save_leadgen_config(conn, {"vacancies": {"enabled": True, "daily_limit": 0}})
    mon_file = tmp_path / "job_monitor_2026-11-17.json"
    mon_file.write_text(
        json.dumps([{"hash": "h1", "role": "AI Producer", "contact": "@ai_p", "text": "AI UGC @ai_p"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["job_reply.py", str(mon_file), "--no-crm"])
    assert job_reply.main() == 0
    conn.close()
