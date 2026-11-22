from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

import pytest

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))

from app.db import connect, init_db  # noqa: E402
from app.leads import search_leads  # noqa: E402
from app.outreach import create_campaign, get_campaign, render_template  # noqa: E402
from leadgen import job_monitor, job_reply  # noqa: E402


def test_resolve_jobs_session_path_priority_and_vps_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("TG_JOBS_SESSION", raising=False)
    monkeypatch.delenv("SCRAPER_TG_SESSION", raising=False)
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)

    # 1. TG_JOBS_SESSION env wins over everything
    monkeypatch.setenv("TG_JOBS_SESSION", "/tmp/env_jobs_reader.session")
    assert (
        job_monitor.resolve_jobs_session_path(
            {"vacancies": {"vps_scraper_session": "/tmp/cfg_jobs.session"}}
        )
        == "/tmp/env_jobs_reader.session"
    )

    # 2. cfg["vacancies"]["vps_scraper_session"] wins when TG_JOBS_SESSION is absent
    monkeypatch.delenv("TG_JOBS_SESSION", raising=False)
    assert (
        job_monitor.resolve_jobs_session_path(
            {"vacancies": {"vps_scraper_session": "/opt/custom/jobs_reader.session"}}
        )
        == "/opt/custom/jobs_reader.session"
    )
    assert (
        job_monitor.resolve_jobs_session_path(
            {"vps_scraper_session": "/opt/direct/jobs_reader.session"}
        )
        == "/opt/direct/jobs_reader.session"
    )

    # 3. VPS runtime / existing VPS session path uses canonical /opt/crmtg/data/scraper_sessions/jobs_reader.session
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    assert (
        job_monitor.resolve_jobs_session_path({})
        == "/opt/crmtg/data/scraper_sessions/jobs_reader.session"
    )

    # 4. Non-VPS fallback chain uses SCRAPER_TG_SESSION when VPS path is absent
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)
    monkeypatch.setattr(job_monitor.os.path, "exists", lambda _p: False)
    monkeypatch.setattr(job_monitor.os.path, "isdir", lambda _p: False)
    scraper_sess = tmp_path / "iso_scraper.session"
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(scraper_sess))
    assert job_monitor.resolve_jobs_session_path({}) == str(scraper_sess)

    # 5. If nothing exists on disk and no env/cfg override, resolves cleanly to VPS default
    monkeypatch.delenv("SCRAPER_TG_SESSION", raising=False)
    assert (
        job_monitor.resolve_jobs_session_path({})
        == "/opt/crmtg/data/scraper_sessions/jobs_reader.session"
    )


def test_default_layers_only_ai_content_and_traffic_and_filter_qualified_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg_path = tmp_path / "missing_leadgen_config.json"
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))

    assert job_monitor.DEFAULT_VACANCY_LAYERS == {
        "AI-контент": True,
        "трафик": True,
        "автоматизация": False,
        "AI-роль": False,
    }
    assert job_monitor.resolve_enabled_layers() == {"AI-контент", "трафик"}

    judged = [
        {
            "src": "AI Jobs",
            "src_user": "ai_jobs",
            "id": 1,
            "date": "2026-11-22T10:00:00+00:00",
            "url": "https://t.me/ai_jobs/1",
            "text": "Ищем AI UGC креатора на 60 роликов Reels/Shorts в месяц @ai_hr",
            "hash": "h1",
            "sig": [1],
            "v": {"fit": True, "layer": "AI-контент", "remote": True, "role": "AI UGC креатор"},
        },
        {
            "src": "Traffic Jobs",
            "src_user": "traffic_jobs",
            "id": 2,
            "date": "2026-11-22T10:05:00+00:00",
            "url": "https://t.me/traffic_jobs/2",
            "text": "Ищем Head of SMM и трафика для контент-завода на AI Reels @traf_hr",
            "hash": "h2",
            "sig": [2],
            "v": {"fit": True, "layer": "трафик", "remote": True, "role": "Head of SMM AI"},
        },
        {
            "src": "Auto Jobs",
            "src_user": "auto_jobs",
            "id": 3,
            "date": "2026-11-22T10:10:00+00:00",
            "url": "https://t.me/auto_jobs/3",
            "text": "Ищем специалиста по автоворонкам и аналитике @auto_hr",
            "hash": "h3",
            "sig": [3],
            "v": {"fit": True, "layer": "автоматизация", "remote": True, "role": "Спец автоворонок"},
        },
        {
            "src": "Exec Jobs",
            "src_user": "exec_jobs",
            "id": 4,
            "date": "2026-11-22T10:15:00+00:00",
            "url": "https://t.me/exec_jobs/4",
            "text": "Ищем AI CTO в IT-продукт, стек ML @cto_hr",
            "hash": "h4",
            "sig": [4],
            "v": {"fit": True, "layer": "AI-роль", "remote": True, "role": "CTO AI"},
        },
        {
            "src": "Office Jobs",
            "src_user": "office_jobs",
            "id": 5,
            "date": "2026-11-22T10:20:00+00:00",
            "url": "https://t.me/office_jobs/5",
            "text": "Ищем AI-креатора на Reels в офис в Москве @msk_hr",
            "hash": "h5",
            "sig": [5],
            "v": {"fit": True, "layer": "AI-контент", "remote": False, "role": "AI-креатор офис"},
        },
    ]

    kept = job_monitor.filter_qualified_fits(judged)
    assert [p["id"] for p in kept] == [1, 2]

    # Sidecar rows must always carry segment="vacancy", language="ru", source="tg_jobs"
    emitted = job_monitor.sidecar(kept)
    assert len(emitted) == 2
    for row in emitted:
        assert row["segment"] == "vacancy"
        assert row["language"] == "ru"
        assert row["source"] == "tg_jobs"

    # Explicit config override can still enable another layer if desired
    kept_override = job_monitor.filter_qualified_fits(
        judged,
        vac_cfg={"layers": {"AI-контент": True, "трафик": False, "автоматизация": True}},
    )
    assert [p["id"] for p in kept_override] == [1, 3]


def test_deterministic_vacancy_junk_rx_triage_and_verdict_reject_dev_n8n_crm_bots_storymakers(
    monkeypatch: pytest.MonkeyPatch,
):
    junk_posts = [
        {
            "text": (
                "Ищем Python-разработчика и backend программиста для API на FastAPI и PostgreSQL, "
                "опыт от 3 лет, удалённо, оплата 250к. Писать в личку @py_dev_hr"
            )
        },
        {
            "text": (
                "Требуется веб-разработчик на React / TypeScript для доработки личного кабинета и вёрстки лендингов, "
                "удалённая работа, полная занятость. Контакт @react_dev_hr"
            )
        },
        {
            "text": (
                "Нужен специалист по n8n и Make.com для настройки интеграции amoCRM и Telegram, "
                "автоматизация отчётов в Google Sheets. Писать @n8n_make_hr"
            )
        },
        {
            "text": (
                "Ищем подрядчика для настройки CRM Битрикс24 и автоматизации воронок продаж, "
                "удалённо, бюджет 60 000 руб. Отклик: @bitrix_crm_hr"
            )
        },
        {
            "text": (
                "Нужно создать Telegram-бота для онлайн-школы с оплатой через ЮKassa и выдачей уроков, "
                "без видео, только бот и сценарии переписки. Контакт @tg_bot_only_hr"
            )
        },
        {
            "text": (
                "Ищем сторисмейкера для ежедневного оформления и ведения сторис эксперта, "
                "оплата 15 000 руб в месяц, простые макеты. Писать @stories_hr"
            )
        },
        {
            "text": (
                "Ищем специалиста на вайб-кодинг через Claude Code для сборки SaaS MVP с нуля за 2 недели, "
                "оплата по договорённости. Писать @vibe_coder_hr"
            )
        },
    ]

    valid_posts = [
        {
            "text": (
                "Ищем AI-креатора для генерации 40 вертикальных роликов Reels и Shorts в месяц с ИИ-аватаром, "
                "оплата $1500/мес, удалённо, долгосрок. Контакт @ai_reels_valid"
            )
        },
        {
            "text": (
                "Ищем подрядчика на систему лидгена: 30 коротких видео Reels/TikTok + настройка чат-бота ManyChat "
                "для сбора заявок из комментариев, бюджет 120 000 руб/мес. Писать @reels_manychat_valid"
            )
        },
        {
            "text": (
                "Мы компания-разработчик AI-приложения, ищем AI UGC креатора чтобы делать 30 вертикальных видео в месяц "
                "под TikTok и Reels, удалённо, бюджет $2000/мес. Контакт @saas_app_hiring_ugc"
            )
        },
        {
            "text": (
                "Ищем AI-креатора на Reels и Shorts (без съёмок видео — через AI-аватары и липсинк), "
                "оплата $1200/мес, удалённо. Писать @ai_avatar_reels_valid"
            )
        },
        {
            "text": (
                "Looking for an AI UGC creator to make 30 reels per month for our subscription app, "
                "remote ongoing contract $1800/mo, reach out to @en_app_reels"
            )
        },
    ]

    for jp in junk_posts:
        assert job_monitor.VACANCY_JUNK_RX.search(jp["text"]), f"Expected junk match for: {jp['text']}"

    for vp in valid_posts:
        assert not job_monitor.VACANCY_JUNK_RX.search(vp["text"]), f"False positive on valid job: {vp['text']}"

    kept = job_monitor.prefilter(junk_posts + valid_posts, min_len=80)
    assert kept == valid_posts

    # verdict() must fail-closed on junk WITHOUT calling ask_llm, and reject disabled layers if LLM returns them
    llm_calls = []

    def _mock_llm(prompt: str, timeout: int = 240) -> str:
        llm_calls.append(prompt)
        return json.dumps(
            {
                "fit": True,
                "layer": "автоматизация",
                "mode": "отдать своим",
                "role": "Автоворонка",
                "why": "Тест отключённого слоя",
                "pay": "",
                "contact": "@hr",
                "remote": True,
                "hot": False,
            }
        )

    monkeypatch.setattr(job_monitor, "ask_llm", _mock_llm)
    v_junk = job_monitor.verdict({"src": "chat", "also": [], "text": junk_posts[0]["text"]})
    assert v_junk["fit"] is False
    assert llm_calls == []

    v_disabled_layer = job_monitor.verdict(
        {"src": "chat", "also": [], "text": "Ищем спеца по воронкам и рассылкам для бизнеса, удалённо @hr_test"}
    )
    assert len(llm_calls) == 1
    assert v_disabled_layer["fit"] is False

    # Verify TRIAGE & VERDICT prompt texts explicitly carry rejection rules and zero CreatorOS
    for prompt_txt in (job_monitor.TRIAGE, job_monitor.VERDICT):
        low = prompt_txt.lower()
        assert "creatoros" not in low
        assert "программист" in low and "разработчик" in low
        assert "n8n" in low and "make" in low and "crm" in low
        assert "ботов" in low and "без видео" in low
        assert "сторисмейкер" in low


def test_job_reply_q20_copy_showcase_url_prompt_override_and_custom_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg_file = tmp_path / "leadgen_config.json"
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))

    vacancy = {
        "role": "AI-креатор для Reels",
        "why": "Генерация 30 роликов в месяц с воронкой",
        "pay": "120 000 руб.",
        "contact": "@vacancy_lead_hr",
        "layer": "AI-контент",
        "mode": "отдать своим",
        "src": "AI Вакансии",
        "url": "https://t.me/ai_vacancies/777",
        "text": "Ищем AI-креатора для Reels и воронки в ManyChat, 120 000 руб @vacancy_lead_hr",
    }

    # 1. Default Step 1 & Step 2 copy without overrides
    step1_default = job_reply.build_default_step1(vacancy)
    f1_default, f2_default = job_reply.build_default_followups(vacancy)
    assert "creatoros" not in step1_default.lower()
    assert "SoftUGC" in step1_default
    assert "ManyChat" in step1_default
    assert "аналитика" in step1_default.lower()
    assert "лидген" in step1_default.lower()
    assert "183,5" in step1_default
    assert job_reply.DEFAULT_SHOWCASE_URL in step1_default
    assert "$80" not in step1_default

    # Step 2 positions 10-sec preview + $80-$100 test series cleanly
    assert "10–15 секунд" in f1_default or "10-15" in f1_default
    assert "$80–$100" in f1_default or "$80-$100" in f1_default
    assert "creatoros" not in f1_default.lower()
    assert "creatoros" not in f2_default.lower()

    # 2. Vacancies config overrides: showcase_url + editable_prompt_override
    vac_cfg_override = {
        "showcase_url": "https://softugc.com/vacancies-showcase",
        "editable_prompt_override": "Усиль акцент на аналитику доходимости в ManyChat и покажи свежие кейсы.",
    }
    rendered_prompt = job_reply.build_reply_prompt(vacancy, "Кейсы SoftUGC", vac_cfg=vac_cfg_override)
    assert "https://softugc.com/vacancies-showcase" in rendered_prompt
    assert "Усиль акцент на аналитику доходимости в ManyChat" in rendered_prompt
    assert "creatoros" not in rendered_prompt.lower()
    assert "система лидгена" in rendered_prompt.lower()
    assert "$80–$100" in rendered_prompt

    # Full prompt override with {text} placeholder works cleanly
    full_prompt = job_reply.build_reply_prompt(
        vacancy,
        "Банк",
        vac_cfg={"editable_prompt_override": "Собери отклик для {role} со ссылкой {showcase_url}. Текст: {text}"},
    )
    assert "AI-креатор для Reels" in full_prompt
    assert job_reply.DEFAULT_SHOWCASE_URL in full_prompt
    assert "JSON" in full_prompt

    # 3. custom_step_templates_json in vacancies config deterministically overrides Step 1/2/3 in draft() & build_vacancy_mcp_lead()
    custom_cfg = {
        "showcase_url": "https://jobs.softugc.com/",
        "custom_step_templates_json": json.dumps(
            {
                "step1_text": "Привет! По {role}: строим лидген (ролики + ManyChat + аналитика). Витрина: {showcase_url}. Тест?",
                "step2_text": "Соберём 10-сек превью или тестовую серию за $80-$100 по {role}.",
                "step3_text": "Задача {role} ещё актуальна?",
            },
            ensure_ascii=False,
        ),
    }
    monkeypatch.setattr(
        job_reply,
        "ask_llm",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("LLM must not run when step1_text is overridden")),
    )
    drafted = job_reply.draft(vacancy, "bank", vac_cfg=custom_cfg)
    assert drafted["text"] == (
        "Привет! По AI-креатор для Reels: строим лидген (ролики + ManyChat + аналитика). "
        "Витрина: https://jobs.softugc.com/. Тест?"
    )
    assert drafted["followup_1"] == "Соберём 10-сек превью или тестовую серию за $80-$100 по AI-креатор для Reels."
    assert drafted["followup_2"] == "Задача AI-креатор для Reels ещё актуальна?"

    mcp_lead = job_reply.build_vacancy_mcp_lead(vacancy, drafted, vac_cfg=custom_cfg)
    assert mcp_lead is not None
    assert mcp_lead["segment"] == "vacancy"
    assert mcp_lead["language"] == "ru"
    assert mcp_lead["source"] == "tg_jobs"
    assert mcp_lead["step1_text"] == drafted["text"]
    assert mcp_lead["step2_text"] == drafted["followup_1"]
    assert mcp_lead["step3_text"] == drafted["followup_2"]


def test_job_reply_reads_campaign_custom_step_templates_json_and_cli_ingests_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "vac_overrides.db"
    conn = connect(db_path)
    init_db(conn)

    # Ensure campaigns.custom_step_templates_json exists (compat even before schema migration lands)
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
    if "custom_step_templates_json" not in cols:
        conn.execute("ALTER TABLE campaigns ADD COLUMN custom_step_templates_json TEXT")
        conn.commit()

    # Check graceful handling when column does NOT exist in an old DB
    old_db = tmp_path / "legacy_schema.db"
    with sqlite3.connect(old_db) as old_conn:
        old_conn.execute("CREATE TABLE campaigns (id INTEGER PRIMARY KEY, name TEXT)")
        old_conn.execute("INSERT INTO campaigns (id, name) VALUES (1, 'TG Вакансии')")
        old_conn.commit()
    assert job_reply.load_campaign_copy_overrides(old_db, campaign_name="TG Вакансии") == {}

    camp = create_campaign(
        conn,
        name="Client Reels & Вакансии RU — TG",
        daily_limit=20,
        status="draft",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{first_touch}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
            {"step_order": 3, "delay_hours": 72, "template_text": "{followup_2}"},
        ],
    )
    camp_override = {
        "showcase_url": "https://softugc.com/ru-cases",
        "step1": "Добрый день! По «{role}»: внедряем систему лидгена (AI-ролики + ManyChat + аналитика). Кейсы: {showcase_url}. Готовы сделать тест?",
        "step2": "Можем сделать 10-сек превью или тестовую серию ($80–$100) по «{role}».",
        "step3": "Подскажите, по «{role}» ещё в поиске?",
    }
    conn.execute(
        "UPDATE campaigns SET custom_step_templates_json = ? WHERE id = ?",
        (json.dumps(camp_override, ensure_ascii=False), camp["id"]),
    )
    conn.commit()

    # Campaign override takes precedence over vacancies config
    resolved = job_reply.resolve_vacancy_copy_settings(
        vac_cfg={"showcase_url": "https://old.example.com", "custom_step_templates_json": '{"step1": "Old"}'},
        db_path=db_path,
        campaign_id=camp["id"],
    )
    assert resolved["showcase_url"] == "https://softugc.com/ru-cases"
    assert "внедряем систему лидгена" in resolved["step1_template"]

    # End-to-end CLI run from monitor sidecar (without LLM!) pushes customized copy into CRM
    cfg_file = tmp_path / "leadgen_config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "vacancies": {
                    "enabled": True,
                    "daily_limit": 10,
                    "layers": {"AI-контент": True, "трафик": True, "автоматизация": False, "AI-роль": False},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))

    mon_path = tmp_path / "job_monitor_2026-11-22.json"
    replies_out = tmp_path / "job_replies_out.json"
    mon_path.write_text(
        json.dumps(
            [
                {
                    "msg_id": 501,
                    "chat_id": -100501,
                    "hash": "vac_ok_1",
                    "role": "AI-продюсер Reels",
                    "why": "Нужна система лидгена на AI-роликах",
                    "pay": "150 000 руб.",
                    "contact": "@vlad_client_hr",
                    "layer": "AI-контент",
                    "mode": "отдать своим",
                    "segment": "vacancy",
                    "language": "ru",
                    "source": "tg_jobs",
                    "src": "AI Jobs RU",
                    "url": "https://t.me/ai_jobs_ru/501",
                    "text": "Ищем AI-продюсера Reels для контент-завода @vlad_client_hr",
                },
                {
                    "msg_id": 502,
                    "chat_id": -100502,
                    "hash": "vac_skip_auto",
                    "role": "Интегратор n8n",
                    "why": "Настройка CRM",
                    "pay": "50 000 руб.",
                    "contact": "@n8n_skip_hr",
                    "layer": "автоматизация",
                    "mode": "руками",
                    "src": "Auto Jobs",
                    "url": "https://t.me/auto_jobs/502",
                    "text": "Ищем спеца по n8n @n8n_skip_hr",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job_reply.py",
            str(mon_path),
            "--out",
            str(replies_out),
            "--db-path",
            str(db_path),
            "--campaign-id",
            str(camp["id"]),
            "--campaign-name",
            "Client Reels & Вакансии RU — TG",
        ],
    )
    rc = job_reply.main()
    assert rc == 0
    assert replies_out.exists()
    out_rows = json.loads(replies_out.read_text(encoding="utf-8"))
    assert len(out_rows) == 1
    assert out_rows[0]["contact"] == "@vlad_client_hr"
    assert "https://softugc.com/ru-cases" in out_rows[0]["step1_text"]
    assert "$80–$100" in out_rows[0]["step2_text"]
    assert "creatoros" not in json.dumps(out_rows, ensure_ascii=False).lower()

    leads = search_leads(conn, q="vlad_client_hr")
    assert len(leads) == 1
    updated_camp = get_campaign(conn, camp["id"])
    assert len(updated_camp["recipients"]) == 1
    rec = updated_camp["recipients"][0]
    assert render_template(updated_camp["steps"][0]["template_text"], rec) == out_rows[0]["step1_text"]
    assert render_template(updated_camp["steps"][1]["template_text"], rec) == out_rows[0]["step2_text"]
    assert render_template(updated_camp["steps"][2]["template_text"], rec) == out_rows[0]["step3_text"]
    conn.close()
