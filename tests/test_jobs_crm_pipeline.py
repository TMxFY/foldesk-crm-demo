import json
from pathlib import Path
import sys
import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import connect, init_db
from app.leads import search_leads
from app.outreach import get_campaign, render_template
from leadgen.crm_mcp_push import push_leads_via_mcp
from leadgen.job_reply import (
    build_default_followups,
    build_vacancy_mcp_lead,
    main as job_reply_main,
)


@pytest.fixture()
def db_conn(tmp_path):
    conn = connect(tmp_path / "test_jobs_crm.db")
    init_db(conn)
    return conn


def test_build_vacancy_mcp_lead_variant_a_direct_and_variant_b_micro_ping():
    vacancy = {
        "head": "🔥 AI-креатор реалистичного сериала",
        "role": "AI-креатор реалистичного сериала",
        "why": "Съёмка видео по сценарию с ИИ, долгосрочная занятость от 45 тыс. руб./мес.",
        "pay": "от 45 тыс. руб. в мес.",
        "contact": "@natavovlan",
        "layer": "AI-контент",
        "mode": "отдать своим",
        "hot": True,
        "src": "Вакансии • AI • вайбкодинг",
        "url": "https://t.me/vakansii_chatgpt/51503",
        "hash": "06efe45be8cd3295",
    }
    full_reply = (
        "Добрый день! По AI-креатору на реалистичный сериал.\n\n"
        "Делаю сериалы с ИИ-персонажами под ключ на Kling, Seedance и ElevenLabs.\n\n"
        "Кейсы:\n• Шоурил: https://tmxfy.github.io/\n\n"
        "Сколько серий в месяц нужно и какой хронометраж?"
    )
    draft_obj = {
        "text": full_reply,
        "followup_1": "Добрый день! Если пришлёте сценарий первой сцены, соберу тестовый фрагмент 10–15 сек.",
        "followup_2": "Подскажите, задача по сериалу ещё открыта или уже нашли креатора?",
        "cases": "183,5 млн просмотров + ИИ-сериал про банкротство",
    }

    # Вариант А (direct, по умолчанию): step1_text — полный отклик, step2/step3 — фоллоу-апы
    lead_a = build_vacancy_mcp_lead(vacancy, draft_obj, touch_mode="direct")
    assert lead_a is not None
    assert lead_a["username"] == "natavovlan"
    assert lead_a["company"] == "AI-креатор реалистичного сериала"
    assert lead_a["category"] == "AI-контент"
    assert lead_a["url"] == "https://t.me/vakansii_chatgpt/51503"
    assert lead_a["step1_text"] == full_reply
    assert lead_a["step2_text"] == draft_obj["followup_1"]
    assert lead_a["step3_text"] == draft_obj["followup_2"]
    assert "от 45 тыс. руб." in lead_a["their_angle"]

    # Вариант Б (micro_ping): step1_text — короткий вопрос без ссылок, step2_text — полный отклик
    lead_b = build_vacancy_mcp_lead(vacancy, draft_obj, touch_mode="micro_ping")
    assert lead_b is not None
    assert "https://" not in lead_b["step1_text"]
    assert "AI-креатор реалистичного сериала" in lead_b["step1_text"]
    assert lead_b["step2_text"] == full_reply
    assert lead_b["step3_text"] == draft_obj["followup_1"]


def test_push_vacancy_leads_to_crm_via_mcp(db_conn):
    vacancy = {
        "role": "UGC-креатор для AI-проекта",
        "why": "Съёмка коротких видео для TikTok, Reels и Shorts",
        "pay": "от 20.000 + KPI",
        "contact": "@wqwww90",
        "layer": "AI-контент",
        "mode": "отдать своим",
        "src": "AI ВАКАНСИИ НЕЙРОСЕТИ",
        "url": "https://t.me/vakansii_ai/1922",
    }
    f1, f2 = build_default_followups(vacancy)
    draft_obj = {
        "text": "Добрый день! По UGC-креатору в AI-проект.\n\nКакой объём роликов в месяц нужен?",
        "followup_1": f1,
        "followup_2": f2,
    }
    mcp_lead = build_vacancy_mcp_lead(vacancy, draft_obj, touch_mode="direct")
    assert mcp_lead is not None

    res = push_leads_via_mcp(
        [mcp_lead],
        enroll_in_campaign=True,
        campaign_name="TG Вакансии",
        conn=db_conn,
    )
    assert res["ok"] is True
    assert res["created_leads"] == 1
    assert res["enrolled_in_campaign"] == 1
    cid = res["campaign_id"]
    assert cid is not None

    leads = search_leads(db_conn, q="wqwww90")
    assert len(leads) == 1
    assert leads[0]["username"] == "wqwww90"
    assert leads[0]["company"] == "UGC-креатор для AI-проекта"
    assert leads[0]["niche"] == "AI-контент"

    camp = get_campaign(db_conn, cid)
    assert camp["name"] == "TG Вакансии"
    assert len(camp["steps"]) == 3
    assert len(camp["recipients"]) == 1
    rec = camp["recipients"][0]
    assert rec["username"] == "wqwww90"
    assert render_template(camp["steps"][0]["template_text"], rec) == draft_obj["text"]
    assert render_template(camp["steps"][1]["template_text"], rec) == f1
    assert render_template(camp["steps"][2]["template_text"], rec) == f2


def test_job_reply_cli_ingests_cached_replies_into_crm(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_crm.db"
    init_db(connect(db_path))

    monitor_path = tmp_path / "job_monitor_2026-11-06.json"
    replies_in = tmp_path / "job_replies_in.json"
    replies_out = tmp_path / "job_replies_out.json"

    monitor_path.write_text(
        json.dumps(
            [
                {
                    "msg_id": 101,
                    "chat_id": -1001,
                    "hash": "abc12345",
                    "role": "Технический специалист по чат-ботам",
                    "why": "Разработка Telegram-ботов и воронок",
                    "pay": "договорная",
                    "contact": "@ironcat222",
                    "layer": "автоматизация",
                    "mode": "отдать своим",
                    "src": "Вакансии Боты",
                    "url": "https://t.me/bots_jobs/101",
                    "text": "Ищем техспеца по ботам @ironcat222",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    replies_in.write_text(
        json.dumps(
            [
                {
                    "hash": "abc12345",
                    "contact": "@ironcat222",
                    "role": "Технический специалист по чат-ботам",
                    "text": "Добрый день! По техспецу по чат-ботам.\n\nКакая CRM используется?",
                }
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
            str(monitor_path),
            "--from-replies",
            str(replies_in),
            "--out",
            str(replies_out),
            "--db-path",
            str(db_path),
            "--campaign-name",
            "TG Вакансии",
            "--touch-mode",
            "direct",
        ],
    )
    rc = job_reply_main()
    assert rc == 0
    assert replies_out.exists()

    conn = connect(db_path)
    leads = search_leads(conn, q="ironcat222")
    assert len(leads) == 1
    assert leads[0]["campaign_status"] == "pending"
