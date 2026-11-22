from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

from fastapi.testclient import TestClient

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))

from app.db import connect, init_db, utc_now
from app.hygiene import audit_crm_hygiene, heal_crm_hygiene
from app.main import create_app
from app.outreach import create_campaign
from app.telegram_gateway import FakeGateway


def _insert_lead(
    conn: sqlite3.Connection,
    *,
    name: str,
    company: str | None = None,
    username: str | None = None,
    phone: str | None = None,
    telegram_user_id: int | None = None,
    dialog_id: int | None = None,
    status: str = "new",
    segment: str = "b2b",
    language: str = "en",
    exclude_from_airtable: int = 0,
    manual_action_url: str | None = None,
    manual_draft_text: str | None = None,
    niche: str | None = None,
    note: str | None = None,
    description: str | None = None,
    url: str | None = None,
    lead_source: str | None = "",
    pool_usd: int | None = None,
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO leads (
            username, phone, telegram_user_id, dialog_id,
            name, company, status, segment, language, exclude_from_airtable,
            manual_action_url, manual_draft_text, niche, note, description,
            url, lead_source, pool_usd, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            phone,
            telegram_user_id,
            dialog_id,
            name,
            company,
            status,
            segment,
            language,
            exclude_from_airtable,
            manual_action_url,
            manual_draft_text,
            niche,
            note,
            description,
            url,
            lead_source,
            pool_usd,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_heal_crm_hygiene_preserves_v2_queues_and_segments_while_quarantining_b2b_junk(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "hygiene_v2_queues.db")
    init_db(conn)

    # 1. Valid open v2 queue & segment leads that MUST be preserved
    social_manual_id = _insert_lead(
        conn,
        name="Jordan X",
        company="Acme Health",
        segment="social_manual",
        language="en",
        manual_action_url="https://x.com/jordan_acme",
        manual_draft_text="Saw Acme Health — quick 15s AI UGC demo?",
        niche="HealthTech",
        description="Paid telehealth and longevity membership ($99/mo)",
        url="https://acmehealth.com",
        lead_source="d2c_whatsapp",
        pool_usd=18000,
    )
    social_manual_raw_id = _insert_lead(
        conn,
        name="Nordic Founder",
        company="Nordic Labs",
        segment="social_manual",
        language="en",
        manual_action_url="",
        manual_draft_text="Want 3 sample reels for Nordic Labs?",
        niche="SaaS",
        description="Open-source AI SDK & subscription workflow platform, portfolio of 4 apps, ищем ugc creator",
        url="https://linkedin.com/in/nordic_founder",
        lead_source="",
    )
    social_manual_blank_all_id = _insert_lead(
        conn,
        name="Blank Manual Social",
        company="QuietBrand",
        segment="social_manual",
        language="en",
        manual_action_url=None,
        niche="E-com / D2C",
        description="DTC beauty brand, open-source community tools, portfolio showcase",
        lead_source="d2c_whatsapp",
    )
    vac_assisted_id = _insert_lead(
        conn,
        name="Kwork Buyer",
        company="VideoProject",
        segment="vacancy_assisted",
        language="ru",
        manual_action_url="https://kwork.ru/projects/888001",
        manual_draft_text="Добрый день! Сделаем серию AI Reels под ключ.",
        niche="Vacancy",
        note="Подработка: монтаж коротких видео, ищу человека на серию роликов",
        description="Нужен freelancer с portfolio для marketing agency или бренда, ищем ugc creator, open-source проект",
        lead_source="tg jobs",
    )
    vac_assisted_blank_url_id = _insert_lead(
        conn,
        name="Profi Buyer NoUrl",
        company="EduBrand",
        segment="vacancy_assisted",
        language="ru",
        manual_action_url="",
        manual_draft_text="Добрый день! Отклик по заказу на ролики.",
        niche="Vacancy",
        description="Нужен freelancer для монтажа роликов, отправьте portfolio, ищем ugc creator",
        url="https://profi.ru/order/12345",
        lead_source="",
    )
    vacancy_id = _insert_lead(
        conn,
        name="Employer TG",
        company="ReelsBrand",
        username="employer_reels_tg",
        segment="vacancy",
        language="ru",
        niche="Вакансии",
        note="Ищу человека на монтаж Reels, 25 роликов в месяц, нужен freelancer / portfolio",
        description="Вакансия из TG-канала: marketing agency / open-source startup, ищем ugc creator",
        lead_source="tg_jobs",
    )
    client_reels_id = _insert_lead(
        conn,
        name="Client Reels Buyer",
        company="ScaleFlow",
        username="scaleflow_reels",
        segment="client_reels",
        language="en",
        niche="Vacancy",
        note="Looking for ongoing short-form reels production, send portfolio",
        description="Job post for AI UGC / Reels freelancer on open-source SaaS",
        lead_source="vacancies_en",
    )
    ru_expert_id = _insert_lead(
        conn,
        name="Анна Коуч",
        company="Школа Анны",
        username="anna_ru_expert",
        segment="ru_expert",
        language="ru",
        exclude_from_airtable=1,
        niche="Коучинг и маркетинг",
        note="IT ментор, солопренёр и таргетолог, ведёт Telegram-канал и продаёт наставничество",
        description="Продвижение личного бренда эксперта через Reels",
        url="https://t.me/anna_ru_expert",
        lead_source="channel",
    )
    ru_expert_no_tg_id = _insert_lead(
        conn,
        name="Ольга Блогер",
        company="Блог Ольги",
        username=None,
        segment="ru_expert",
        language="ru",
        exclude_from_airtable=1,
        niche="Психология",
        description="Эксперт без TG-ника, найденная по Instagram",
        url="https://instagram.com/olga_expert_ru",
        lead_source="ru_experts",
    )

    # 2. Invalid / contactless B2B / Roblox-FiveM / job-seeker leads that MUST still be quarantined
    contactless_b2b_id = _insert_lead(
        conn,
        name="Contactless B2B",
        company="GhostSaaS",
        segment="b2b",
        manual_action_url="https://x.com/should_not_exempt_b2b",
        niche="SaaS",
        description="Paid B2B SaaS platform",
        lead_source="Cold outreach",
    )
    contactless_whale_id = _insert_lead(
        conn,
        name="Contactless Whale",
        company="WhaleNoContact",
        segment="whale",
        niche="Fintech SaaS",
        description="Enterprise treasury SaaS",
        lead_source="yc",
        pool_usd=50000,
    )
    junk_b2b_agency_id = _insert_lead(
        conn,
        name="Alex FINPR",
        company="FINPR Agency",
        username="finpr_bd_junk",
        segment="b2b",
        niche="Сервис / агентство",
        description="PR & Growth for Crypto",
        lead_source="Cold outreach",
    )
    junk_whale_gaming_id = _insert_lead(
        conn,
        name="Roblox Dev",
        company="BloxStudio",
        username="roblox_whale_junk",
        segment="whale",
        niche="Gaming / Roblox",
        description="Roblox obby game studio looking for players",
        url="https://roblox.com/games/123",
        lead_source="Cold outreach",
    )
    junk_vacancy_roblox_id = _insert_lead(
        conn,
        name="FiveM RP Server",
        company="GTA RP",
        username="fivem_rp_owner",
        segment="vacancy",
        niche="Gaming",
        description="Ищем видеомонтажёра для нашего FiveM GTA RP / Roblox сервера",
        lead_source="tg_jobs",
    )
    junk_assisted_job_seeker_id = _insert_lead(
        conn,
        name="Ivan Editor",
        company="Freelance",
        segment="vacancy_assisted",
        manual_action_url="https://kwork.ru/user/ivan",
        description="Ищу работу видеомонтажёром, ищу заказы на рилс [FOR HIRE]",
        lead_source="",
    )
    junk_social_for_hire_id = _insert_lead(
        conn,
        name="Self Promo Creator",
        company="CreatorBio",
        segment="social_manual",
        manual_action_url="https://x.com/self_promo_for_hire",
        description="[FOR HIRE] I am a video editor looking for work",
        lead_source="d2c_whatsapp",
    )

    heal_res = heal_crm_hygiene(conn)
    assert heal_res["quarantined_leads"] == 7

    preserved_ids = (
        social_manual_id,
        social_manual_raw_id,
        social_manual_blank_all_id,
        vac_assisted_id,
        vac_assisted_blank_url_id,
        vacancy_id,
        client_reels_id,
        ru_expert_id,
        ru_expert_no_tg_id,
    )
    for lid in preserved_ids:
        row = conn.execute("SELECT status, airtable_status, next_step FROM leads WHERE id = ?", (lid,)).fetchone()
        assert row["status"] == "new", f"Expected lead {lid} to remain 'new', got {dict(row)}"
        assert row["airtable_status"] != "Cancelled"

    quarantined_ids = (
        contactless_b2b_id,
        contactless_whale_id,
        junk_b2b_agency_id,
        junk_whale_gaming_id,
        junk_vacancy_roblox_id,
        junk_assisted_job_seeker_id,
        junk_social_for_hire_id,
    )
    for lid in quarantined_ids:
        row = conn.execute("SELECT status, airtable_status, next_step FROM leads WHERE id = ?", (lid,)).fetchone()
        assert row["status"] == "lost", f"Expected junk/contactless lead {lid} to become 'lost', got {dict(row)}"
        assert row["airtable_status"] == "Cancelled"
        assert str(row["next_step"] or "").startswith("Авто-карантин Sniper ICP: ")

    audit_after = audit_crm_hygiene(conn)
    assert audit_after["ok"] is True
    assert audit_after["contactless_lead_count"] == 0
    assert audit_after["active_junk_lead_count"] == 0
    conn.close()


def test_heal_crm_hygiene_campaign_recipients_respects_segments(tmp_path: Path) -> None:
    conn = connect(tmp_path / "hygiene_v2_recipients.db")
    init_db(conn)
    now = utc_now()

    ru_camp = create_campaign(
        conn,
        name="РФ Эксперты — Личный Бренд",
        daily_limit=30,
        status="running",
        segment="ru_expert",
        language="ru",
        exclude_from_airtable=True,
    )
    reels_camp = create_campaign(
        conn,
        name="Client Reels — Main EN",
        daily_limit=30,
        status="running",
        segment="client_reels",
        language="en",
    )
    b2b_camp = create_campaign(
        conn,
        name="SoftUGC — Whales EN",
        daily_limit=30,
        status="running",
        segment="whale",
        language="en",
    )

    ru_lead_id = _insert_lead(
        conn,
        name="Максим Эксперт",
        company="Блог Максима",
        username="maxim_ru_coach",
        segment="ru_expert",
        language="ru",
        exclude_from_airtable=1,
        niche="Маркетинг",
        description="IT ментор и солопренёр, ведёт курс и консультации",
        lead_source="ru_experts",
    )
    reels_lead_id = _insert_lead(
        conn,
        name="Brand Reels",
        company="GlowCo",
        username="glow_reels_buyer",
        segment="client_reels",
        language="en",
        niche="Vacancy",
        description="Подработка / job post for AI reels",
        lead_source="tg_jobs",
    )
    junk_b2b_lead_id = _insert_lead(
        conn,
        name="Agency Lead",
        company="Marketacross",
        username="marketacross_bd",
        segment="whale",
        language="en",
        niche="Агентство",
        description="PR & Growth for Crypto",
        lead_source="Cold outreach",
    )

    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, username, name, company, category, their_angle,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES
            (?, ?, 'maxim_ru_coach', 'Максим Эксперт', 'Блог Максима', 'Маркетинг', 'IT ментор и солопренёр', 'Привет', 0, 'pending', ?, ?),
            (?, NULL, NULL, 'RU No Contact', 'Нет Контакта', 'Коучинг', 'Эксперт', 'Привет', 0, 'pending', ?, ?),
            (?, ?, 'glow_reels_buyer', 'Brand Reels', 'GlowCo', 'Vacancy', 'Ищу человека, подработка, монтаж Reels', 'Hey GlowCo', 0, 'pending', ?, ?),
            (?, ?, 'marketacross_bd', 'Agency Lead', 'Marketacross', 'Агентство', 'PR & Growth for Crypto', 'Hey Marketacross', 0, 'pending', ?, ?),
            (?, ?, 'maxim_ru_coach', 'Максим в B2B', 'Блог Максима', 'Маркетинг', 'IT ментор и солопренёр', 'Привет', 0, 'pending', ?, ?)
        """,
        (
            ru_camp["id"], ru_lead_id, now, now,
            ru_camp["id"], now, now,
            reels_camp["id"], reels_lead_id, now, now,
            b2b_camp["id"], junk_b2b_lead_id, now, now,
            b2b_camp["id"], ru_lead_id, now, now,
        ),
    )
    conn.commit()

    heal_res = heal_crm_hygiene(conn)
    assert heal_res["quarantined_recipients"] == 2
    assert heal_res["quarantined_leads"] == 1

    statuses = [
        (r["name"], r["status"])
        for r in conn.execute("SELECT name, status FROM campaign_recipients ORDER BY id ASC").fetchall()
    ]
    assert statuses == [
        ("Максим Эксперт", "pending"),
        ("RU No Contact", "completed"),
        ("Brand Reels", "pending"),
        ("Agency Lead", "completed"),
        ("Максим в B2B", "pending"),
    ]
    assert audit_crm_hygiene(conn)["ok"] is True
    conn.close()


def test_heal_crm_hygiene_handles_legacy_campaigns_table_without_segment(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "legacy_no_camp_segment.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            provider TEXT DEFAULT 'telegram',
            status TEXT DEFAULT 'connected',
            session_path TEXT,
            session_string TEXT,
            health TEXT,
            last_error TEXT,
            updated_at TEXT
        );
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            telegram_user_id INTEGER,
            username TEXT,
            name TEXT,
            phone TEXT,
            status TEXT DEFAULT 'new',
            note TEXT,
            next_step TEXT,
            created_at TEXT,
            updated_at TEXT,
            description TEXT,
            url TEXT,
            dialog_id INTEGER,
            folder_id INTEGER,
            amount INTEGER DEFAULT 0,
            monthly_value INTEGER DEFAULT 0,
            company TEXT,
            niche TEXT,
            lead_source TEXT,
            airtable_status TEXT
        );
        CREATE TABLE campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            leadgen_sources_json TEXT DEFAULT '[]'
        );
        CREATE TABLE campaign_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            lead_id INTEGER,
            telegram_user_id INTEGER,
            username TEXT,
            name TEXT,
            phone TEXT,
            company TEXT,
            note TEXT,
            website TEXT,
            category TEXT,
            their_angle TEXT,
            status TEXT DEFAULT 'pending',
            error_text TEXT,
            updated_at TEXT
        );
        CREATE TABLE kv_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    now = utc_now()
    conn.execute("INSERT INTO campaigns (name) VALUES ('Legacy B2B Campaign')")
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, username, name, company, category, their_angle, status
        ) VALUES
            (1, 'finpr_legacy', 'FINPR', 'FINPR Agency', 'Агентство', 'PR & Growth', 'pending'),
            (1, 'valid_legacy', 'Monetor', 'Monetor', 'Fintech SaaS', 'Paid expense tracker ($12/mo)', 'pending')
        """
    )
    conn.commit()

    res = heal_crm_hygiene(conn)
    assert res["quarantined_recipients"] == 1
    rows = conn.execute("SELECT username, status FROM campaign_recipients ORDER BY id ASC").fetchall()
    assert [(r["username"], r["status"]) for r in rows] == [
        ("finpr_legacy", "completed"),
        ("valid_legacy", "pending"),
    ]
    assert audit_crm_hygiene(conn)["ok"] is True
    conn.close()


def test_startup_create_app_keeps_manual_social_and_assisted_jobs_queues_populated(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "1")
    monkeypatch.setenv("CLOUD_BACKUP_ENABLED", "0")
    monkeypatch.setenv("DISABLE_BOT_ALERTS", "1")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))

    conn = connect(tmp_path / "e2e_startup_queues.db")
    init_db(conn)

    # Seed manual queue leads BEFORE create_app() runs heal_crm_hygiene(conn) on startup!
    social_id = _insert_lead(
        conn,
        name="Sarah Ecom",
        company="GlowSerum",
        segment="social_manual",
        language="en",
        manual_action_url="https://x.com/sarahecom",
        manual_draft_text="Saw GlowSerum — quick 15s AI UGC demo?",
        niche="E-com / D2C",
        description="Skincare DTC subscription brand",
        lead_source="d2c_whatsapp",
        pool_usd=25000,
    )
    assisted_id = _insert_lead(
        conn,
        name="Kwork Employer",
        company="CourseLab",
        segment="vacancy_assisted",
        language="ru",
        manual_action_url="https://kwork.ru/projects/555123",
        manual_draft_text="Добрый день! Сделаем серию AI Reels под ваш курс.",
        niche="Vacancy",
        note="Нужен монтаж рилс и шортс, подработка на месяц",
        lead_source="",
    )

    app = create_app(conn=conn, gateway=FakeGateway())
    try:
        with TestClient(app) as client:
            social_resp = client.get("/api/queues/manual-social")
            assert social_resp.status_code == 200
            social_data = social_resp.json()
            assert [item["id"] for item in social_data["items"]] == [social_id]
            assert social_data["items"][0]["status"] == "new"

            assisted_resp = client.get("/api/queues/assisted-jobs")
            assert assisted_resp.status_code == 200
            assisted_data = assisted_resp.json()
            assert [item["id"] for item in assisted_data["items"]] == [assisted_id]
            assert assisted_data["items"][0]["status"] == "new"
    finally:
        conn.close()
