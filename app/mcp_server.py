import asyncio
import hmac
import inspect
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.analytics import get_analytics_summary
from app.crm_auth import resolve_request_crm_user
from app.runtime_config import should_allow_auto_sonnet
from app.security import is_auth_enforced
from app.db import (
    ROOT,
    as_dict,
    connect,
    ensure_airtable_pipeline_stages,
    init_db,
    list_pipeline_stages,
    locked_conn,
    normalize_status,
    status_to_airtable,
    utc_now,
)
from app.events import get_today_focus, record_lead_event
from app.inbox import get_inbox_counts, list_inbox, list_messages
from app.leads import create_lead, lead_card, search_leads, update_lead
from app.notes import add_note
from app.outreach import (
    _clean_company_title,
    _clean_handle,
    _clean_step1_text,
    _is_qa_campaign_name,
    _parse_account_ids,
    create_campaign,
    is_legacy_exhausted_recipient,
    is_offline_non_icp_contact,
    is_vacancy_campaign,
)
from leadgen.language_router import (
    CAMPAIGN_OFM_TG,
    CAMPAIGN_RU_EXPERTS_TG,
    CAMPAIGN_VACANCY_EN_TG,
    CAMPAIGN_VACANCY_RU_TG,
    CAMPAIGN_VACANCY_RU_WA,
    CAMPAIGN_WHALES_EN_TG,
    CAMPAIGN_WHALES_EN_WA,
    detect_lead_language,
    detect_lead_segment,
    resolve_target_campaign,
)
from leadgen.outreach_copy import (
    DEFAULT_VACANCY_SHOWCASE_URL,
    SUPPORTED_COPY_PROFILES,
    VLAD_STEP2_TEMPLATE,
    VLAD_STEP3_TEXT,
    build_outreach_sequence,
    check_copy_language,
    parse_pool_usd_value,
    resolve_copy_profile,
    sanitize_copy_text,
)
from leadgen.supply_side import (
    NON_BUYER_ROLES,
    PLATFORM_GARBAGE_COMPANY_NAMES,
    has_insufficient_product_info,
    is_raw_cohort_tag,
    is_rejected_buyer_category,
    is_platform_or_social_url,
    is_supply_side,
    is_truncated_or_bad_pitch,
    pick_best_product_url,
)
from app.reminders import (
    complete_reminder,
    create_reminder,
    list_reminders,
    normalize_due,
    postpone_reminder,
)
from app.tasks import create_task, list_tasks, set_task_done

MCP_PROTOCOL_VERSION = "2024-11-05"
logger = logging.getLogger(__name__)
SERVER_INFO = {
    "name": "foldesk-crm-mcp",
    "version": "1.0.0",
    "title": "Foldesk CRM MCP Server",
    "description": (
        "MCP-сервер Foldesk CRM для AI-агентов: полное чтение воронки, карточек, "
        "переписок, аналитики и расписания + изменение сделок, заметок, задач, "
        "календаря и синхронизация с Airtable (без отправки сообщений в мессенджеры)."
    ),
}

TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "get_analytics_summary",
        "description": (
            "Полная аналитика CRM по воронке, бюджетам, конверсиям, рассылкам, "
            "аккаунтам и графикам за выбранный период."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["7d", "30d", "90d", "all"],
                    "default": "30d",
                    "description": "Период аналитики: 7d, 30d, 90d или all.",
                }
            },
        },
    },
    {
        "name": "get_pipeline_stages",
        "description": "Получить список всех стадий воронки CRM (включая 7 стадий Airtable).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ensure_airtable_stages": {
                    "type": "boolean",
                    "default": False,
                    "description": "Гарантировать наличие всех 7 стадий Airtable в БД.",
                }
            },
        },
    },
    {
        "name": "search_leads",
        "description": (
            "Поиск и фильтрация сделок (лидов) по тексту, стадии воронки, папке, "
            "мессенджеру, компании, нише и минимальному бюджету."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": "", "description": "Поисковый запрос"},
                "status": {"type": "string", "default": "", "description": "Статус / стадия сделки"},
                "folder_id": {"type": ["integer", "null"], "description": "ID папки"},
                "provider": {"type": "string", "default": "", "description": "telegram или whatsapp"},
                "company": {"type": "string", "default": "", "description": "Фильтр по компании"},
                "niche": {"type": "string", "default": "", "description": "Фильтр по нише"},
                "min_amount": {"type": ["integer", "null"], "description": "Минимальный бюджет сделки"},
                "limit": {"type": "integer", "default": 50, "description": "Максимум результатов"},
            },
        },
    },
    {
        "name": "get_lead_full_card",
        "description": (
            "Получить полную карточку лида со всеми полями, заметками, задачами, "
            "последними 50 сообщениями переписки, открытыми напоминаниями и созвонами в календаре."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": ["integer", "null"], "description": "ID лида"},
                "username": {"type": "string", "description": "Telegram @username лида (если lead_id не указан)"},
                "name": {"type": "string", "description": "Имя или компания лида (если lead_id не указан)"},
            },
        },
    },
    {
        "name": "create_or_update_lead",
        "description": (
            "Создать нового лида или обновить существующего (статус из 7 стадий Airtable/CRM, "
            "бюджет amount, monthly_value, компания, ниша, оффер, следующий шаг, дата фоллоу-апа)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": ["integer", "null"], "description": "ID лида для обновления"},
                "name": {"type": ["string", "null"], "description": "Имя контакта"},
                "username": {"type": ["string", "null"], "description": "Telegram username"},
                "phone": {"type": ["string", "null"], "description": "Телефон"},
                "status": {
                    "type": ["string", "null"],
                    "description": (
                        "Стадия: New / In conversation / Call scheduled / Offer sent / "
                        "On hold / Signed / Cancelled (или new, talking, call_scheduled, waiting, on_hold, won, lost)"
                    ),
                },
                "amount": {"type": ["integer", "null"], "description": "Бюджет сделки"},
                "monthly_value": {"type": ["integer", "null"], "description": "Ежемесячный чек (MRR)"},
                "currency": {"type": ["string", "null"], "description": "Валюта (USD, EUR, RUB)"},
                "company": {"type": ["string", "null"], "description": "Название компании"},
                "niche": {"type": ["string", "null"], "description": "Ниша клиента"},
                "offer_type": {
                    "type": ["array", "string", "null"],
                    "items": {"type": "string"},
                    "description": "Тип оффера (список или строка)",
                },
                "offer_details": {"type": ["string", "null"], "description": "Детали оффера"},
                "next_step": {"type": ["string", "null"], "description": "Следующий шаг по сделке"},
                "next_follow_up": {"type": ["string", "null"], "description": "Дата следующего контакта (YYYY-MM-DD)"},
                "lead_source": {"type": ["string", "null"], "description": "Источник лида"},
                "note": {"type": ["string", "null"], "description": "Заметка в карточке"},
                "url": {"type": ["string", "null"], "description": "Ссылка на сайт или профиль"},
                "email": {"type": ["string", "null"], "description": "Email"},
                "provider": {"type": ["string", "null"], "description": "telegram или whatsapp"},
            },
        },
    },
    {
        "name": "add_lead_note",
        "description": "Добавить текстовую заметку в таймлайн карточки лида.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": "integer", "description": "ID лида"},
                "body": {"type": "string", "description": "Текст заметки"},
            },
            "required": ["lead_id", "body"],
        },
    },
    {
        "name": "manage_lead_tasks",
        "description": "Управление чек-лист задачами по лиду: список (list), создание (create), выполнение (complete), возврат в работу (reopen).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "complete", "reopen"],
                    "description": "Действие над задачей",
                },
                "lead_id": {"type": ["integer", "null"], "description": "ID лида (для list и create)"},
                "task_id": {"type": ["integer", "null"], "description": "ID задачи (для complete и reopen)"},
                "title": {"type": "string", "default": "", "description": "Текст новой задачи (для create)"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_reminders",
        "description": "Управление напоминаниями по лидам: список (list), создание (create), перенос срока (postpone), закрытие (complete).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "postpone", "complete"],
                    "description": "Действие с напоминанием",
                },
                "scope": {
                    "type": "string",
                    "enum": ["open", "overdue", "all"],
                    "default": "open",
                    "description": "Фильтр для list: open, overdue, all",
                },
                "lead_id": {"type": ["integer", "null"], "description": "ID лида (для create или фильтрации list)"},
                "reminder_id": {"type": ["integer", "null"], "description": "ID напоминания (для postpone и complete)"},
                "due_at": {"type": "string", "default": "", "description": "Срок напоминания ISO 8601 (YYYY-MM-DDTHH:MM)"},
                "text": {"type": "string", "default": "", "description": "Текст напоминания"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "get_calendar_schedule",
        "description": "Получить расписание из календаря CRM (созвоны, фоллоу-апы, напоминания) за период.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": ["string", "null"], "description": "Начало периода (YYYY-MM-DD)"},
                "end_date": {"type": ["string", "null"], "description": "Конец периода (YYYY-MM-DD)"},
                "event_type": {"type": ["string", "null"], "description": "Фильтр по типу: call, followup, reminder, task"},
                "lead_id": {"type": ["integer", "null"], "description": "Фильтр по ID лида"},
            },
        },
    },
    {
        "name": "schedule_calendar_event",
        "description": "Создать, обновить, перенести (reschedule), завершить (complete), отменить (cancel) или удалить (delete) событие в календаре CRM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "reschedule", "complete", "cancel", "delete"],
                    "default": "create",
                    "description": "Действие над событием календаря",
                },
                "event_id": {"type": ["integer", "null"], "description": "ID события в таблице calendar_events"},
                "uid": {"type": ["string", "null"], "description": "Универсальный идентификатор элемента (например event:1, reminder:2, lead_followup:3)"},
                "title": {"type": "string", "default": "", "description": "Название события / созвона"},
                "start_at": {"type": "string", "default": "", "description": "Время начала ISO 8601 (YYYY-MM-DDTHH:MM:SS)"},
                "end_at": {"type": ["string", "null"], "description": "Время окончания ISO 8601"},
                "event_type": {
                    "type": "string",
                    "default": "call",
                    "description": "Тип события: call, followup, meeting, internal",
                },
                "lead_id": {"type": ["integer", "null"], "description": "ID привязанного лида"},
                "meet_link": {"type": ["string", "null"], "description": "Ссылка на Google Meet / Zoom / Telegram Call"},
                "notes": {"type": ["string", "null"], "description": "Заметки к встрече"},
            },
        },
    },
    {
        "name": "list_inbox_dialogs",
        "description": (
            "Чтение диалогов и переписок из Inbox (ТОЛЬКО ЧТЕНИЕ, без отправки сообщений). "
            "Если указан dialog_id — возвращает диалог и его сообщения, иначе список диалогов и счётчики."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chip": {
                    "type": "string",
                    "enum": ["all", "waiting", "unread", "our_turn", "groups"],
                    "default": "all",
                    "description": "Фильтр вкладки Inbox",
                },
                "account_id": {"type": ["integer", "null"], "description": "ID аккаунта (опционально)"},
                "dialog_id": {"type": ["integer", "null"], "description": "ID конкретного диалога для чтения истории сообщений"},
                "limit": {"type": "integer", "default": 30, "description": "Лимит диалогов или сообщений"},
            },
        },
    },
    {
        "name": "get_focus_today",
        "description": "Сводка «Фокус дня»: кто ждёт ответа в чатах, горящие напоминания, открытые задачи, созвоны на сегодня и зависшие сделки.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "stale_days": {"type": "integer", "default": 3, "description": "Порог дней без обновлений для зависших лидов"},
            },
        },
    },
    {
        "name": "sync_airtable",
        "description": "Запустить двустороннюю или одностороннюю синхронизацию сделок с Airtable (или проверить статус конфигурации).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["both", "pull", "push", "status"],
                    "default": "both",
                    "description": "Направление синхронизации: both, pull, push или status",
                }
            },
        },
    },
    {
        "name": "ingest_discovered_leads",
        "description": (
            "Загрузить список найденных лидов в CRM (создание/обновление сделок в leads) "
            "и при необходимости автоматически добавить их в кампанию рассылки (campaign_recipients)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "leads": {
                    "type": "array",
                    "description": "Список найденных лидов",
                    "items": {
                        "type": "object",
                        "properties": {
                            "username": {"type": ["string", "null"]},
                            "name": {"type": ["string", "null"]},
                            "company": {"type": ["string", "null"]},
                            "phone": {"type": ["string", "null"]},
                            "email": {"type": ["string", "null"]},
                            "website": {"type": ["string", "null"]},
                            "url": {"type": ["string", "null"]},
                            "category": {"type": ["string", "null"]},
                            "niche": {"type": ["string", "null"]},
                            "audience": {"type": ["string", "null"]},
                            "their_angle": {"type": ["string", "null"]},
                            "our_angle": {"type": ["string", "null"]},
                            "hook": {"type": ["string", "null"]},
                            "step1_text": {"type": ["string", "null"]},
                            "step2_text": {"type": ["string", "null"]},
                            "step3_text": {"type": ["string", "null"]},
                            "lead_source": {"type": ["string", "null"]},
                            "note": {"type": ["string", "null"]},
                            "telegram_user_id": {"type": ["integer", "string", "null"]},
                            "provider": {"type": ["string", "null"]},
                            "x_url": {"type": ["string", "null"]},
                            "linkedin_url": {"type": ["string", "null"]},
                            "instagram_url": {"type": ["string", "null"]},
                            "whatsapp": {"type": ["string", "null"]},
                            "icp_score": {"type": ["number", "integer", "null"]},
                            "source": {"type": ["string", "null"]},
                            "segment": {"type": ["string", "null"]},
                            "language": {"type": ["string", "null"]},
                            "exclude_from_airtable": {"type": ["integer", "boolean", "null"]},
                            "company_domain": {"type": ["string", "null"]},
                            "pool_usd": {"type": ["integer", "number", "string", "null"]},
                            "secondary_contact_json": {"type": ["string", "object", "null"]},
                            "manual_action_url": {"type": ["string", "null"]},
                            "manual_draft_text": {"type": ["string", "null"]},
                            "role": {"type": ["string", "null"]},
                            "title": {"type": ["string", "null"]},
                        },
                    },
                },
                "enroll_in_campaign": {
                    "type": "boolean",
                    "default": True,
                    "description": "Добавлять ли лидов сразу в кампанию рассылки (campaign_recipients).",
                },
                "campaign_id": {
                    "type": ["integer", "null"],
                    "default": None,
                    "description": "ID кампании рассылки. Если не указан — ищется или создаётся кампания по campaign_name.",
                },
                "campaign_name": {
                    "type": ["string", "null"],
                    "default": "Leadgen Autopilot",
                    "description": "Имя кампании для поиска или создания, если campaign_id не передан.",
                },
            },
            "required": ["leads"],
        },
    },
    {
        "name": "list_direct_x_leads",
        "description": (
            "Прочитать сохранённых целевых B2B-лидов без Telegram/телефона (с email/X/сайтом) "
            "из direct_x_leads.jsonl (ТОЛЬКО ЧТЕНИЕ)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Максимум возвращаемых записей.",
                },
                "query": {
                    "type": "string",
                    "default": "",
                    "description": "Поисковый фильтр по имени, компании, email или URL.",
                },
            },
        },
    },
    {
        "name": "list_ofm_review_leads",
        "description": (
            "Прочитать изолированную очередь OFM-байеров (@FranceOFM, TG/FB OFM группы) "
            "из ofm_review_leads.jsonl для ручного ревью без добавления в рассылки (ТОЛЬКО ЧТЕНИЕ)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Максимум возвращаемых записей.",
                },
                "query": {
                    "type": "string",
                    "default": "",
                    "description": "Фильтр по @username, группе, рынку или сигналу.",
                },
            },
        },
    },
]

RESOURCES_SCHEMA: list[dict[str, Any]] = [
    {
        "uri": "crm://analytics/summary",
        "name": "Сводная аналитика Foldesk CRM",
        "description": "Текущие показатели воронки, выручки, рассылок и аккаунтов",
        "mimeType": "application/json",
    },
    {
        "uri": "crm://focus/today",
        "name": "Фокус дня CRM",
        "description": "Горящие ответы, напоминания, созвоны и зависшие сделки на сегодня",
        "mimeType": "application/json",
    },
    {
        "uri": "crm://pipeline/stages",
        "name": "Стадии воронки CRM",
        "description": "Список всех стадий пайплайна и их ключей",
        "mimeType": "application/json",
    },
]


def _normalize_iso_datetime(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("Укажите дату и время события")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text}T10:00:00"
    return normalize_due(text)


_GENERIC_OR_ROLE_COMPANIES = frozenset(
    {
        "saas",
        "ai",
        "ai saas",
        "b2b",
        "b2b saas",
        "web3",
        "crypto",
        "defi",
        "dex",
        "cex",
        "fintech",
        "gaming",
        "media",
        "edtech",
        "ecom",
        "ecommerce",
        "startup",
        "stealth",
        "project",
        "app",
        "bot",
        "channel",
        "none",
        "unknown",
        "вакансия",
        "рилсмейкер",
        "монтажер",
        "монтажёр",
        "таргетолог",
        "маркетолог",
        "сценарист",
        "дизайнер",
        "копирайтер",
        "продюсер",
    }
    | set(PLATFORM_GARBAGE_COMPANY_NAMES)
)
_ROLE_LIKE_COMPANY_RE = re.compile(
    r"(?:рилсмейкер|ai[\-\s]?креатор|ugc[\-\s]?креатор|нейрокреатор|"
    r"техническ\w*\s+специалист|специалист\s+по|менеджер\s+по|маркетолог|"
    r"монтаж[её]р|сценарист|таргетолог|продюсер|ваканси[яи]|"
    r"\b(?:video|reels|ugc|ai)\s+(?:creator|editor|producer)\b)",
    re.I,
)
_COMPETITOR_DENYLIST_RE = re.compile(
    r"\b(?:crmchat|matrixreach|firsay)\b|(?:crmchat|matrixreach|firsay)\.(?:ai|io|com|co|app|xyz)",
    re.I,
)
_LEGACY_1300_DB_RE = re.compile(
    r"(?:legacy[_\s-]*1300|old[_\s-]*base|(?:^|[^0-9])1300(?:\+|[^0-9]|$)|старая\s+база|отработанная\s+база)",
    re.I,
)


def _is_non_mergeable_company(company: str | None) -> bool:
    cleaned = " ".join(str(company or "").strip().split())
    if len(cleaned) < 4:
        return True
    low = cleaned.lower()
    if low in _GENERIC_OR_ROLE_COMPANIES:
        return True
    if is_raw_cohort_tag(cleaned):
        return True
    if _ROLE_LIKE_COMPANY_RE.search(cleaned):
        return True
    return False


def _norm_dedup_domain(raw_url: str | None) -> str:
    s = str(raw_url or "").strip()
    if not s or is_platform_or_social_url(s):
        return ""
    parsed = urlparse(s if "://" in s else f"https://{s}")
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    if not host or "." not in host:
        return ""
    base = host.split(".")[0]
    if base in PLATFORM_GARBAGE_COMPANY_NAMES:
        return ""
    return host


def _norm_phone_key(phone: str | None) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits if len(digits) >= 7 else ""


def _norm_company_key(company: str | None) -> str:
    if _is_non_mergeable_company(company):
        return ""
    cleaned = re.sub(r"\(\s*YC\s+[A-Za-z0-9\s]+\)", "", str(company or ""), flags=re.I)
    key = re.sub(r"[^a-z0-9а-яё]+", "", cleaned.lower())
    return key if len(key) >= 3 and key not in _GENERIC_OR_ROLE_COMPANIES else ""


def _norm_person_name_key(name: str | None, company: str | None = None) -> str:
    raw = str(name or "").strip()
    if not raw or raw.startswith("@") or "://" in raw:
        return ""
    if "|" in raw:
        raw = raw.split("|", 1)[0].strip()
    raw = re.sub(r"\(@[^)]+\)", "", raw).strip()
    cleaned = re.sub(r"[^a-zа-яё\s\-]+", " ", raw.lower()).strip()
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    if len(tokens) < 2:
        return ""
    joined = " ".join(tokens[:3])
    comp_key = _norm_company_key(company)
    if comp_key and re.sub(r"[^a-z0-9а-яё]+", "", joined) == comp_key:
        return ""
    if _ROLE_LIKE_COMPANY_RE.search(joined):
        return ""
    return joined


def _find_lead_by_lookup(
    conn: sqlite3.Connection,
    *,
    lead_id: int | None = None,
    username: str | None = None,
    name: str | None = None,
    phone: str | None = None,
) -> dict | None:
    if lead_id is not None:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
        if row is not None:
            return as_dict(row)
    clean_username = (username or "").strip().lstrip("@")
    if clean_username:
        row = conn.execute(
            "SELECT * FROM leads WHERE username IS NOT NULL AND lower(username) = lower(?) ORDER BY id ASC LIMIT 1",
            (clean_username,),
        ).fetchone()
        if row is not None:
            return as_dict(row)
    clean_phone = (phone or "").strip()
    if clean_phone:
        row = conn.execute(
            "SELECT * FROM leads WHERE phone IS NOT NULL AND phone = ? ORDER BY id ASC LIMIT 1",
            (clean_phone,),
        ).fetchone()
        if row is not None:
            return as_dict(row)
    clean_name = (name or "").strip()
    if clean_name:
        row = conn.execute(
            """
            SELECT * FROM leads
            WHERE (name IS NOT NULL AND lower(name) = lower(?))
               OR (company IS NOT NULL AND lower(company) = lower(?))
            ORDER BY id ASC LIMIT 1
            """,
            (clean_name, clean_name),
        ).fetchone()
        if row is not None:
            return as_dict(row)
        if len(clean_name) >= 4 and clean_name.lower() not in _GENERIC_OR_ROLE_COMPANIES:
            row = conn.execute(
                """
                SELECT * FROM leads
                WHERE (name IS NOT NULL AND lower(name) LIKE lower(?))
                   OR (company IS NOT NULL AND lower(company) LIKE lower(?))
                ORDER BY id ASC LIMIT 1
                """,
                (f"%{clean_name}%", f"%{clean_name}%"),
            ).fetchone()
            if row is not None:
                return as_dict(row)
    return None


def _fallback_list_calendar_items(
    conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    event_type: str | None = None,
    lead_id: int | None = None,
) -> dict:
    start_prefix = (start_date or "").strip()[:10] if start_date else None
    end_prefix = (end_date or "").strip()[:10] if end_date else None
    wanted_type = (event_type or "").strip().lower() or None
    wanted_lead = int(lead_id) if lead_id is not None else None

    items: list[dict] = []

    def _in_range(dt_str: str | None) -> bool:
        if not dt_str:
            return False
        day = str(dt_str).strip()[:10]
        if start_prefix and day < start_prefix:
            return False
        if end_prefix and day > end_prefix:
            return False
        return True

    ev_sql = """
        SELECT ce.*, l.name AS lead_name, l.username AS lead_username, l.company AS lead_company
        FROM calendar_events ce
        LEFT JOIN leads l ON l.id = ce.lead_id
        ORDER BY ce.start_at ASC, ce.id ASC
    """
    for row in conn.execute(ev_sql).fetchall():
        ev = as_dict(row)
        if wanted_lead is not None and ev.get("lead_id") != wanted_lead:
            continue
        ev_t = str(ev.get("event_type") or "call").strip().lower()
        if wanted_type and ev_t != wanted_type:
            continue
        if not _in_range(ev.get("start_at")):
            continue
        items.append(
            {
                "uid": f"event:{ev['id']}",
                "id": int(ev["id"]),
                "source_kind": "calendar_event",
                "title": ev.get("title") or "Созвон",
                "event_type": ev_t,
                "start_at": ev.get("start_at"),
                "end_at": ev.get("end_at"),
                "all_day": bool(ev.get("all_day")),
                "status": ev.get("status") or "scheduled",
                "lead_id": ev.get("lead_id"),
                "lead_name": ev.get("lead_name") or ev.get("lead_company") or ev.get("lead_username"),
                "meet_link": ev.get("meet_link"),
                "notes": ev.get("notes"),
            }
        )

    if not wanted_type or wanted_type == "reminder":
        rem_sql = """
            SELECT r.*, l.name AS lead_name, l.username AS lead_username, l.company AS lead_company
            FROM reminders r
            JOIN leads l ON l.id = r.lead_id
            ORDER BY r.due_at ASC, r.id ASC
        """
        for row in conn.execute(rem_sql).fetchall():
            rem = as_dict(row)
            if wanted_lead is not None and int(rem["lead_id"]) != wanted_lead:
                continue
            if not _in_range(rem.get("due_at")):
                continue
            items.append(
                {
                    "uid": f"reminder:{rem['id']}",
                    "id": int(rem["id"]),
                    "source_kind": "reminder",
                    "title": rem.get("text") or "Напоминание",
                    "event_type": "reminder",
                    "start_at": rem.get("due_at"),
                    "end_at": None,
                    "all_day": False,
                    "status": "completed" if rem.get("done_at") else "scheduled",
                    "lead_id": int(rem["lead_id"]),
                    "lead_name": rem.get("lead_name") or rem.get("lead_company") or rem.get("lead_username"),
                    "meet_link": None,
                    "notes": None,
                }
            )

    if not wanted_type or wanted_type in ("followup", "follow_up"):
        for row in conn.execute(
            "SELECT id, name, username, company, next_follow_up, next_step, status FROM leads WHERE next_follow_up IS NOT NULL AND trim(next_follow_up) != ''"
        ).fetchall():
            ld = as_dict(row)
            if wanted_lead is not None and int(ld["id"]) != wanted_lead:
                continue
            nf = str(ld["next_follow_up"]).strip()
            start_iso = f"{nf}T10:00:00" if len(nf) == 10 else nf
            if not _in_range(start_iso):
                continue
            label = ld.get("name") or ld.get("company") or ld.get("username") or f"Лид #{ld['id']}"
            items.append(
                {
                    "uid": f"lead_followup:{ld['id']}",
                    "id": int(ld["id"]),
                    "source_kind": "lead_followup",
                    "title": f"Фоллоу-ап: {label}" + (f" ({ld['next_step']})" if ld.get("next_step") else ""),
                    "event_type": "followup",
                    "start_at": start_iso,
                    "end_at": None,
                    "all_day": len(nf) == 10,
                    "status": "scheduled",
                    "lead_id": int(ld["id"]),
                    "lead_name": label,
                    "meet_link": None,
                    "notes": ld.get("next_step"),
                }
            )

    items.sort(key=lambda x: (str(x.get("start_at") or ""), str(x.get("uid") or "")))
    return {
        "items": items,
        "count": len(items),
        "start_date": start_prefix,
        "end_date": end_prefix,
    }


def _handle_schedule_calendar_event(conn: sqlite3.Connection, arguments: dict) -> dict:
    action = str(arguments.get("action") or "create").strip().lower()
    event_id = arguments.get("event_id")
    uid = str(arguments.get("uid") or "").strip() or None
    title = str(arguments.get("title") or "").strip()
    start_at = str(arguments.get("start_at") or "").strip()
    end_at = arguments.get("end_at")
    event_type = str(arguments.get("event_type") or "call").strip().lower() or "call"
    lead_id = int(arguments["lead_id"]) if arguments.get("lead_id") is not None else None
    meet_link = (str(arguments["meet_link"]).strip() or None) if arguments.get("meet_link") else None
    notes = (str(arguments["notes"]).strip() or None) if arguments.get("notes") else None

    if uid and not event_id and uid.startswith(("ev:", "event:")):
        try:
            event_id = int(uid.split(":", 1)[1])
        except ValueError:
            pass

    try:
        import app.calendar_events as cal_mod
    except ImportError:
        cal_mod = None

    if action == "create":
        if not title:
            if lead_id is not None:
                lrow = conn.execute("SELECT name, company, username FROM leads WHERE id = ?", (lead_id,)).fetchone()
                if lrow is None:
                    raise LookupError("Лид не найден")
                label = lrow["name"] or lrow["company"] or lrow["username"] or f"Лид #{lead_id}"
                title = f"Созвон: {label}"
            else:
                raise ValueError("Укажите название события (title)")
        norm_start = _normalize_iso_datetime(start_at)
        norm_end = _normalize_iso_datetime(end_at) if end_at else None

        if cal_mod and hasattr(cal_mod, "create_calendar_event"):
            sig = inspect.signature(cal_mod.create_calendar_event)
            kwargs: dict[str, Any] = {}
            for k, v in (
                ("title", title),
                ("start_at", norm_start),
                ("end_at", norm_end),
                ("event_type", event_type),
                ("lead_id", lead_id),
                ("meet_link", meet_link),
                ("notes", notes),
            ):
                if k in sig.parameters:
                    kwargs[k] = v
            ev = cal_mod.create_calendar_event(conn, **kwargs)
            return {"ok": True, "action": "create", "event": ev}

        if lead_id is not None:
            lcheck = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if lcheck is None:
                raise LookupError("Лид не найден")
        now = utc_now()
        cur = conn.execute(
            """
            INSERT INTO calendar_events (
                title, event_type, start_at, end_at, all_day,
                lead_id, meet_link, notes, status, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, 'scheduled', 'crm', ?, ?)
            """,
            (title, event_type, norm_start, norm_end, lead_id, meet_link, notes, now, now),
        )
        new_id = int(cur.lastrowid)
        if lead_id is not None and event_type == "call":
            conn.execute(
                """
                UPDATE leads
                SET status = CASE WHEN status IN ('new', 'talking') THEN 'call_scheduled' ELSE status END,
                    airtable_status = CASE WHEN status IN ('new', 'talking') THEN 'Call scheduled' ELSE airtable_status END,
                    next_follow_up = COALESCE(?, next_follow_up),
                    updated_at = ?
                WHERE id = ?
                """,
                (norm_start[:10], now, lead_id),
            )
            record_lead_event(
                conn,
                lead_id,
                "reminder_set",
                f"Назначен созвон: {title} ({norm_start[:16].replace('T', ' ')})",
                {"event_id": new_id, "start_at": norm_start, "meet_link": meet_link},
                created_at=now,
            )
        conn.commit()
        row = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (new_id,)).fetchone()
        ev = as_dict(row)
        ev["uid"] = f"event:{new_id}"
        return {"ok": True, "action": "create", "event": ev}

    if action == "reschedule" and (uid or event_id is not None):
        clean_start = start_at.strip()
        if len(clean_start) == 10 and clean_start[4] == "-" and clean_start[7] == "-":
            _normalize_iso_datetime(clean_start)
            resched_start = clean_start
        else:
            resched_start = _normalize_iso_datetime(clean_start)

        resched_end = None
        if end_at:
            clean_end = str(end_at).strip()
            if len(clean_end) == 10 and clean_end[4] == "-" and clean_end[7] == "-":
                _normalize_iso_datetime(clean_end)
                resched_end = clean_end
            else:
                resched_end = _normalize_iso_datetime(clean_end)

        if cal_mod and hasattr(cal_mod, "reschedule_calendar_item"):
            target_uid = uid or f"ev:{int(event_id)}"
            res = cal_mod.reschedule_calendar_item(
                conn, uid=target_uid, new_start_at=resched_start, new_end_at=resched_end
            )
            return {"ok": True, "action": "reschedule", "event": res}

        norm_start = _normalize_iso_datetime(start_at)
        if uid and uid.startswith(("rem:", "reminder:")):
            rem_id = int(uid.split(":", 1)[1])
            updated = postpone_reminder(conn, rem_id, norm_start)
            return {"ok": True, "action": "reschedule", "reminder": updated}
        if uid and uid.startswith(("followup:", "lead_followup:")):
            lid = int(uid.split(":", 1)[1])
            updated = update_lead(conn, lid, next_follow_up=norm_start[:10])
            return {"ok": True, "action": "reschedule", "lead": updated}

    if event_id is None:
        raise ValueError("Укажите event_id или uid события")
    eid = int(event_id)
    existing = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (eid,)).fetchone()
    if existing is None:
        raise LookupError("Событие календаря не найдено")

    now = utc_now()
    if action == "delete":
        if cal_mod and hasattr(cal_mod, "delete_calendar_event"):
            cal_mod.delete_calendar_event(conn, eid)
        else:
            conn.execute("DELETE FROM calendar_events WHERE id = ?", (eid,))
            conn.commit()
        return {"ok": True, "action": "delete", "deleted_id": eid}

    updates: dict[str, Any] = {}
    if action == "complete":
        updates["status"] = "completed"
    elif action == "cancel":
        updates["status"] = "cancelled"
    elif action in ("update", "reschedule"):
        if title:
            updates["title"] = title
        if start_at:
            updates["start_at"] = _normalize_iso_datetime(start_at)
        if end_at is not None:
            updates["end_at"] = _normalize_iso_datetime(end_at) if end_at else None
        if "event_type" in arguments and arguments["event_type"]:
            updates["event_type"] = event_type
        if "lead_id" in arguments:
            updates["lead_id"] = lead_id
        if "meet_link" in arguments:
            updates["meet_link"] = meet_link
        if "notes" in arguments:
            updates["notes"] = notes
    else:
        raise ValueError(f"Неизвестное действие schedule_calendar_event: {action}")

    if updates:
        assigns = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE calendar_events SET {assigns}, updated_at = ? WHERE id = ?",
            [*updates.values(), now, eid],
        )
        conn.commit()
    row = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (eid,)).fetchone()
    ev = as_dict(row)
    ev["uid"] = f"event:{eid}"
    return {"ok": True, "action": action, "event": ev}


def call_mcp_tool_handler(conn: sqlite3.Connection, name: str, arguments: dict | None = None) -> dict:
    args = dict(arguments or {})

    if name == "get_analytics_summary":
        period = str(args.get("period") or "30d").strip() or "30d"
        try:
            sig = inspect.signature(get_analytics_summary)
            if "period" in sig.parameters:
                data = get_analytics_summary(conn, period=period)
            else:
                data = get_analytics_summary(conn)
        except TypeError:
            data = get_analytics_summary(conn)
        if isinstance(data, dict) and "period" not in data:
            data["period"] = period
        return data

    if name == "get_pipeline_stages":
        if args.get("ensure_airtable_stages"):
            stages = ensure_airtable_pipeline_stages(conn)
        else:
            stages = list_pipeline_stages(conn)
        return {"stages": stages, "count": len(stages)}

    if name == "search_leads":
        query = str(args.get("query") or args.get("q") or "").strip()
        status = str(args.get("status") or "").strip() or None
        folder_id = args.get("folder_id")
        provider = str(args.get("provider") or "").strip() or None
        company = str(args.get("company") or "").strip().casefold()
        niche = str(args.get("niche") or "").strip().casefold()
        min_amount = args.get("min_amount")
        limit = max(1, min(int(args.get("limit") or 50), 500))

        found = search_leads(
            conn,
            q=query,
            status=status,
            folder_id=folder_id,
            provider=provider,
        )
        if company:
            found = [
                ld for ld in found if company in str(ld.get("company") or "").casefold()
            ]
        if niche:
            found = [
                ld for ld in found if niche in str(ld.get("niche") or "").casefold()
            ]
        if min_amount is not None and str(min_amount).strip() != "":
            min_val = int(min_amount)
            found = [
                ld
                for ld in found
                if max(int(ld.get("amount") or 0), int(ld.get("monthly_value") or 0)) >= min_val
            ]
        sliced = found[:limit]
        return {"leads": sliced, "count": len(sliced), "total_matched": len(found)}

    if name == "get_lead_full_card":
        lead_id = args.get("lead_id")
        if lead_id is None:
            matched = _find_lead_by_lookup(
                conn,
                username=args.get("username"),
                name=args.get("name"),
                phone=args.get("phone"),
            )
            if matched is None:
                raise LookupError("Лид не найден по указанным параметрам")
            lead_id = int(matched["id"])
        else:
            lead_id = int(lead_id)

        card = lead_card(conn, lead_id)

        # Last 50 messages from linked dialog
        dlg_id = card.get("dialog_id")
        if not dlg_id:
            dlg_row = conn.execute(
                "SELECT id FROM dialogs WHERE lead_id = ? ORDER BY last_message_at DESC, id DESC LIMIT 1",
                (lead_id,),
            ).fetchone()
            if dlg_row is not None:
                dlg_id = int(dlg_row["id"])
        messages = []
        if dlg_id:
            try:
                messages = list_messages(conn, int(dlg_id), limit=50)
            except LookupError:
                messages = []
        card["messages"] = messages

        # Open reminders for this lead
        rem_rows = conn.execute(
            "SELECT * FROM reminders WHERE lead_id = ? AND done_at IS NULL ORDER BY due_at ASC, id ASC",
            (lead_id,),
        ).fetchall()
        card["reminders"] = [as_dict(r) for r in rem_rows]

        # Calendar events for this lead
        cal_rows = conn.execute(
            "SELECT * FROM calendar_events WHERE lead_id = ? ORDER BY start_at ASC, id ASC",
            (lead_id,),
        ).fetchall()
        card["calendar_events"] = [as_dict(r) for r in cal_rows]
        return card

    if name == "create_or_update_lead":
        ensure_airtable_pipeline_stages(conn)
        lead_id = args.get("lead_id")
        if lead_id is None and (args.get("username") or args.get("phone")):
            existing = _find_lead_by_lookup(
                conn,
                username=args.get("username"),
                phone=args.get("phone"),
            )
            if existing is not None:
                lead_id = int(existing["id"])

        allowed_fields = (
            "name",
            "username",
            "phone",
            "status",
            "amount",
            "monthly_value",
            "currency",
            "company",
            "niche",
            "offer_type",
            "offer_details",
            "next_step",
            "next_follow_up",
            "lead_source",
            "note",
            "url",
            "email",
            "provider",
            "description",
        )
        payload = {
            k: args[k]
            for k in allowed_fields
            if k in args and args[k] is not None
        }
        if "status" in payload:
            norm_st = normalize_status(str(payload["status"]), conn)
            payload["status"] = norm_st

        if lead_id is not None:
            updated = update_lead(conn, int(lead_id), **payload)
            return {"action": "updated", "lead": updated}
        created = create_lead(conn, **payload)
        return {"action": "created", "lead": created}

    if name == "add_lead_note":
        if args.get("lead_id") is None:
            raise ValueError("Укажите lead_id")
        note = add_note(conn, int(args["lead_id"]), str(args.get("body") or ""))
        return {"ok": True, "note": note}

    if name == "manage_lead_tasks":
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            if args.get("lead_id") is None:
                raise ValueError("Для получения списка задач укажите lead_id")
            tasks = list_tasks(conn, int(args["lead_id"]))
            return {"action": "list", "lead_id": int(args["lead_id"]), "tasks": tasks}
        if action == "create":
            if args.get("lead_id") is None:
                raise ValueError("Для создания задачи укажите lead_id")
            task = create_task(conn, int(args["lead_id"]), str(args.get("title") or ""))
            return {"action": "create", "task": task}
        if action in ("complete", "reopen"):
            if args.get("task_id") is None:
                raise ValueError("Укажите task_id")
            task = set_task_done(conn, int(args["task_id"]), done=(action == "complete"))
            return {"action": action, "task": task}
        raise ValueError(f"Неизвестное действие manage_lead_tasks: {action}")

    if name == "manage_reminders":
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            scope = str(args.get("scope") or "open").strip().lower() or "open"
            items = list_reminders(conn, scope=scope)
            if args.get("lead_id") is not None:
                lid = int(args["lead_id"])
                items = [r for r in items if int(r.get("lead_id") or 0) == lid]
            return {"action": "list", "scope": scope, "reminders": items, "count": len(items)}
        if action == "create":
            if args.get("lead_id") is None:
                raise ValueError("Укажите lead_id для напоминания")
            raw_due = str(args.get("due_at") or "").strip()
            if len(raw_due) == 10 and raw_due[4] == "-" and raw_due[7] == "-":
                raw_due = f"{raw_due}T10:00:00"
            rem = create_reminder(
                conn,
                int(args["lead_id"]),
                raw_due,
                str(args.get("text") or ""),
            )
            return {"action": "create", "reminder": rem}
        if action == "postpone":
            if args.get("reminder_id") is None:
                raise ValueError("Укажите reminder_id")
            raw_due = str(args.get("due_at") or "").strip()
            if len(raw_due) == 10 and raw_due[4] == "-" and raw_due[7] == "-":
                raw_due = f"{raw_due}T10:00:00"
            rem = postpone_reminder(conn, int(args["reminder_id"]), raw_due)
            return {"action": "postpone", "reminder": rem}
        if action == "complete":
            if args.get("reminder_id") is None:
                raise ValueError("Укажите reminder_id")
            rem = complete_reminder(conn, int(args["reminder_id"]))
            return {"action": "complete", "reminder": rem}
        raise ValueError(f"Неизвестное действие manage_reminders: {action}")

    if name == "get_calendar_schedule":
        start_date = args.get("start_date")
        end_date = args.get("end_date")
        event_type = args.get("event_type")
        lead_id = int(args["lead_id"]) if args.get("lead_id") is not None else None
        try:
            from app.calendar_events import list_calendar_items

            sig = inspect.signature(list_calendar_items)
            call_kwargs: dict[str, Any] = {}
            for k, v in (
                ("start_date", start_date),
                ("end_date", end_date),
                ("event_type", event_type),
                ("lead_id", lead_id),
            ):
                if k in sig.parameters and v is not None:
                    call_kwargs[k] = v
            res = list_calendar_items(conn, **call_kwargs)
            if isinstance(res, list):
                return {"items": res, "count": len(res)}
            return res
        except (ImportError, AttributeError, TypeError):
            return _fallback_list_calendar_items(
                conn,
                start_date=start_date,
                end_date=end_date,
                event_type=event_type,
                lead_id=lead_id,
            )

    if name == "schedule_calendar_event":
        return _handle_schedule_calendar_event(conn, args)

    if name == "list_inbox_dialogs":
        dialog_id = args.get("dialog_id")
        limit = max(1, min(int(args.get("limit") or 30), 200))
        if dialog_id is not None:
            did = int(dialog_id)
            dlg_row = conn.execute("SELECT * FROM dialogs WHERE id = ?", (did,)).fetchone()
            if dlg_row is None:
                raise LookupError("Диалог не найден")
            msgs = list_messages(conn, did, limit=limit)
            return {
                "dialog": as_dict(dlg_row),
                "messages": msgs,
                "messages_count": len(msgs),
            }
        chip_raw = str(args.get("chip") or "all").strip().lower()
        chip = None if chip_raw in ("", "all") else chip_raw
        account_id = int(args["account_id"]) if args.get("account_id") is not None else None
        dialogs = list_inbox(conn, account_id=account_id, chip=chip)
        counts = get_inbox_counts(conn, account_id=account_id)
        sliced = list(dialogs)[:limit]
        return {
            "chip": chip_raw or "all",
            "dialogs": sliced,
            "count": len(sliced),
            "counts": counts,
        }

    if name == "get_focus_today":
        stale_days = int(args.get("stale_days") or 3)
        focus = get_today_focus(conn, stale_days=stale_days)
        if "today_calls" not in focus:
            today_prefix = utc_now()[:10]
            cal_today = conn.execute(
                """
                SELECT ce.*, l.name AS lead_name, l.company AS lead_company
                FROM calendar_events ce
                LEFT JOIN leads l ON l.id = ce.lead_id
                WHERE substr(ce.start_at, 1, 10) = ? AND ce.status = 'scheduled'
                ORDER BY ce.start_at ASC
                """,
                (today_prefix,),
            ).fetchall()
            focus["today_calls"] = [as_dict(r) for r in cal_today]
            focus["counts"]["today_calls"] = len(focus["today_calls"])
        return focus

    if name == "sync_airtable":
        direction = str(args.get("direction") or "both").strip().lower() or "both"
        try:
            import app.airtable_sync as at_mod

            if direction == "status" and hasattr(at_mod, "get_airtable_config"):
                return at_mod.get_airtable_config(conn)
            if hasattr(at_mod, "sync_airtable"):
                return at_mod.sync_airtable(conn, direction=direction)
        except ImportError:
            pass
        rows = dict(
            conn.execute(
                "SELECT key, value FROM kv_settings WHERE key LIKE 'airtable_%'"
            ).fetchall()
        )
        return {
            "ok": False,
            "direction": direction,
            "configured": bool(
                (rows.get("airtable_api_key") or rows.get("airtable_token"))
                and rows.get("airtable_base_id")
            ),
            "detail": "Модуль синхронизации Airtable не настроен или токен не задан",
        }

    if name == "ingest_discovered_leads":
        return _handle_ingest_discovered_leads(conn, args)

    if name == "list_direct_x_leads":
        return _handle_list_direct_x_leads(args)

    if name == "list_ofm_review_leads":
        return _handle_list_ofm_review_leads(args)

    raise KeyError(f"Неизвестный MCP-инструмент: {name}")


def _handle_list_ofm_review_leads(args: dict) -> dict:
    from app.hygiene import read_ofm_review_leads
    from app.leadgen_settings import _resolve_state_dir

    raw_limit = args.get("limit", 50)
    try:
        limit = int(raw_limit) if raw_limit is not None else 50
    except (TypeError, ValueError):
        limit = 50
    query = str(args.get("query") or "").strip()
    return read_ofm_review_leads(_resolve_state_dir(), limit=limit, query=query)


def _handle_list_direct_x_leads(args: dict) -> dict:
    from app.hygiene import read_direct_x_leads
    from app.leadgen_settings import _resolve_state_dir

    raw_limit = args.get("limit", 50)
    try:
        limit = int(raw_limit) if raw_limit is not None else 50
    except (TypeError, ValueError):
        limit = 50
    query = str(args.get("query") or "").strip()
    return read_direct_x_leads(_resolve_state_dir(), limit=limit, query=query)


_SOCIAL_OR_BOARD_HOSTS = {
    "t.me",
    "telegram.me",
    "wa.me",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "instagram.com",
    "hh.ru",
    "profi.ru",
    "kwork.ru",
    "upwork.com",
    "github.com",
}


def _mcp_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.Error as exc:
        logger.debug("PRAGMA table_info(%s) failed: %s", table_name, exc)
        return set()
    return {str(row[1]) for row in rows if len(row) > 1}


def _extract_company_domain(
    item: dict[str, Any],
    website_raw: str,
    url_val: str | None,
) -> str | None:
    raw_dom = str(item.get("company_domain") or item.get("domain") or "").strip().lower()
    if raw_dom:
        if "://" in raw_dom:
            raw_dom = urlparse(raw_dom).netloc or raw_dom
        raw_dom = raw_dom.split("/")[0].split(":")[0].strip().lstrip(".")
        if raw_dom.startswith("www."):
            raw_dom = raw_dom[4:]
        if "." in raw_dom and raw_dom not in _SOCIAL_OR_BOARD_HOSTS:
            return raw_dom

    candidate = str(url_val or website_raw or "").strip()
    if not candidate:
        return None
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    host = (parsed.netloc or "").split(":")[0].strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or host in _SOCIAL_OR_BOARD_HOSTS:
        return None
    return host


def _serialize_secondary_contact(item: dict[str, Any]) -> str | None:
    raw = (
        item.get("secondary_contact_json")
        if item.get("secondary_contact_json") is not None
        else item.get("secondary_contact")
        if item.get("secondary_contact") is not None
        else item.get("secondary_dm")
    )
    if raw is None or raw == "" or raw == [] or raw == {}:
        return None
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, ensure_ascii=False)
    raw_s = str(raw).strip()
    if not raw_s:
        return None
    try:
        parsed = json.loads(raw_s)
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.debug("Non-JSON secondary_contact string kept as wrapped JSON: %s", exc)
    return json.dumps({"contact": raw_s}, ensure_ascii=False)


def _resolve_manual_action_url(
    item: dict[str, Any],
    *,
    x_url: str | None,
    linkedin_url: str | None,
    instagram_url: str | None,
    url_val: str | None,
) -> str | None:
    for cand in (
        item.get("manual_action_url"),
        item.get("apply_url"),
        item.get("job_url"),
        item.get("profile_url"),
        linkedin_url,
        instagram_url,
        x_url,
        url_val,
    ):
        val = str(cand or "").strip()
        if val:
            return val
    return None


def _persist_lead_v2_fields(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    segment: str | None,
    language: str | None,
    exclude_from_airtable: int | None,
    company_domain: str | None,
    pool_usd: int | None,
    secondary_contact_json: str | None,
    manual_action_url: str | None,
    manual_draft_text: str | None,
    now: str,
    is_new: bool,
    existing: dict[str, Any] | None = None,
    explicit_keys: set[str] | None = None,
) -> bool:
    cols = _mcp_table_columns(conn, "leads")
    if not cols:
        return False

    updates: list[str] = []
    params: list[Any] = []
    ex_row = existing or {}
    exp = explicit_keys or set()

    if "segment" in cols and segment:
        prev_seg = str(ex_row.get("segment") or "").strip().lower()
        if (
            is_new
            or not prev_seg
            or (prev_seg == "b2b" and segment != "b2b")
            or ("segment" in exp and segment != prev_seg)
        ):
            updates.append("segment = ?")
            params.append(str(segment))

    if "language" in cols and language:
        prev_lang = str(ex_row.get("language") or "").strip().lower()
        if is_new or not prev_lang or ("language" in exp and language != prev_lang):
            updates.append("language = ?")
            params.append(str(language))

    if "exclude_from_airtable" in cols and exclude_from_airtable is not None:
        ex_int = 1 if int(exclude_from_airtable or 0) else 0
        prev_ex = int(ex_row.get("exclude_from_airtable") or 0)
        if is_new or (ex_int == 1 and prev_ex == 0):
            updates.append("exclude_from_airtable = ?")
            params.append(ex_int)

    if "company_domain" in cols and company_domain:
        prev_dom = str(ex_row.get("company_domain") or "").strip()
        if is_new or not prev_dom or ("company_domain" in exp and company_domain != prev_dom):
            updates.append("company_domain = ?")
            params.append(company_domain)

    if "pool_usd" in cols and pool_usd is not None:
        prev_pool = ex_row.get("pool_usd")
        if (
            is_new
            or prev_pool is None
            or int(pool_usd) > int(prev_pool or 0)
            or ("pool_usd" in exp and int(pool_usd) != int(prev_pool or 0))
        ):
            updates.append("pool_usd = ?")
            params.append(int(pool_usd))

    if "secondary_contact_json" in cols and secondary_contact_json:
        prev_sec = str(ex_row.get("secondary_contact_json") or "").strip()
        if is_new or not prev_sec or ("secondary_contact" in exp and secondary_contact_json != prev_sec):
            updates.append("secondary_contact_json = ?")
            params.append(secondary_contact_json)

    if "manual_action_url" in cols and manual_action_url:
        prev_url = str(ex_row.get("manual_action_url") or "").strip()
        if is_new or not prev_url or ("manual_action_url" in exp and manual_action_url != prev_url):
            updates.append("manual_action_url = ?")
            params.append(manual_action_url)

    if "manual_draft_text" in cols and manual_draft_text:
        prev_draft = str(ex_row.get("manual_draft_text") or "").strip()
        if is_new or not prev_draft or ("manual_draft_text" in exp and manual_draft_text != prev_draft):
            updates.append("manual_draft_text = ?")
            params.append(manual_draft_text)

    if not updates:
        return False
    if "updated_at" in cols:
        updates.append("updated_at = ?")
        params.append(now)
    params.append(int(lead_id))
    conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", params)
    return True


def _persist_campaign_v2_fields(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    segment: str,
    language: str,
    copy_profile: str,
    exclude_from_airtable: int,
) -> None:
    cols = _mcp_table_columns(conn, "campaigns")
    updates: list[str] = []
    params: list[Any] = []
    for col_name, col_val in (
        ("segment", str(segment or "b2b")),
        ("language", str(language or "en")),
        ("copy_profile", str(copy_profile or "default")),
        ("exclude_from_airtable", 1 if int(exclude_from_airtable or 0) else 0),
    ):
        if col_name in cols:
            updates.append(f"{col_name} = ?")
            params.append(col_val)
    if not updates:
        return
    params.append(int(campaign_id))
    conn.execute(f"UPDATE campaigns SET {', '.join(updates)} WHERE id = ?", params)


def _campaign_v2_defaults_for_name(
    cname: str,
    *,
    item_segment: str | None = None,
    item_lang: str | None = None,
) -> dict[str, Any]:
    from app.db import CANONICAL_STEP3_IMAGE_URL
    from app.hygiene import is_ofm_campaign_name
    from app.leadgen_settings import (
        DEFAULT_RU_EXPERTS_STEPS,
        DEFAULT_VACANCY_STEPS,
        DEFAULT_VACANCY_WA_STEPS,
        DEFAULT_WHALES_TG_STEPS,
        DEFAULT_WHALES_WA_STEPS,
    )
    from app.whatsapp_gateway import DEFAULT_WA_DAILY_LIMIT

    low = cname.strip().lower()
    is_wa_camp = "(wa)" in low or "whatsapp" in low or " — wa" in low
    is_ofm_camp = is_ofm_campaign_name(cname)

    if low == CAMPAIGN_WHALES_EN_TG.lower():
        return {
            "segment": "whale",
            "language": "en",
            "copy_profile": "whale_mass",
            "exclude_from_airtable": 0,
            "daily_limit": 30,
            "steps": DEFAULT_WHALES_TG_STEPS,
        }
    if low == CAMPAIGN_WHALES_EN_WA.lower():
        return {
            "segment": "whale",
            "language": "en",
            "copy_profile": "whale_mass",
            "exclude_from_airtable": 0,
            "daily_limit": DEFAULT_WA_DAILY_LIMIT,
            "steps": DEFAULT_WHALES_WA_STEPS,
        }
    if low == CAMPAIGN_VACANCY_RU_TG.lower():
        return {
            "segment": "vacancy",
            "language": "ru",
            "copy_profile": "vacancy_ru",
            "exclude_from_airtable": 0,
            "daily_limit": 20,
            "steps": DEFAULT_VACANCY_STEPS,
        }
    if low == CAMPAIGN_VACANCY_RU_WA.lower():
        return {
            "segment": "vacancy",
            "language": "ru",
            "copy_profile": "vacancy_ru",
            "exclude_from_airtable": 0,
            "daily_limit": DEFAULT_WA_DAILY_LIMIT,
            "steps": DEFAULT_VACANCY_WA_STEPS,
        }
    if low == CAMPAIGN_VACANCY_EN_TG.lower():
        return {
            "segment": "vacancy",
            "language": "en",
            "copy_profile": "vacancy_en",
            "exclude_from_airtable": 0,
            "daily_limit": 20,
            "steps": DEFAULT_VACANCY_STEPS,
        }
    if low == CAMPAIGN_RU_EXPERTS_TG.lower() or "эксперт" in low or item_segment == "ru_expert":
        return {
            "segment": "ru_expert",
            "language": "ru",
            "copy_profile": "ru_expert",
            "exclude_from_airtable": 1,
            "daily_limit": 20,
            "steps": DEFAULT_RU_EXPERTS_STEPS,
        }
    if any(k in low for k in ("ваканс", "vacanc", "job", "client reels")):
        lang = "en" if (" en" in low or "en —" in low) else ("ru" if " ru" in low or "ваканс" in low else (item_lang or "ru"))
        profile = "vacancy_en" if lang == "en" else "vacancy_ru"
        return {
            "segment": "vacancy",
            "language": lang,
            "copy_profile": profile,
            "exclude_from_airtable": 0,
            "daily_limit": DEFAULT_WA_DAILY_LIMIT if is_wa_camp else 20,
            "steps": DEFAULT_VACANCY_WA_STEPS if is_wa_camp else DEFAULT_VACANCY_STEPS,
        }
    if is_ofm_camp:
        return {
            "segment": "ofm",
            "language": "en",
            "copy_profile": "ofm",
            "exclude_from_airtable": 0,
            "daily_limit": 25,
            "steps": [
                {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
                {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
                {
                    "step_order": 3,
                    "delay_hours": 24,
                    "template_text": VLAD_STEP3_TEXT,
                    **({} if is_wa_camp else {"image_url": CANONICAL_STEP3_IMAGE_URL}),
                },
            ],
        }
    if item_segment == "whale":
        return {
            "segment": "whale",
            "language": "en",
            "copy_profile": "whale_mass",
            "exclude_from_airtable": 0,
            "daily_limit": DEFAULT_WA_DAILY_LIMIT if is_wa_camp else 30,
            "steps": DEFAULT_WHALES_WA_STEPS if is_wa_camp else DEFAULT_WHALES_TG_STEPS,
        }
    return {
        "segment": "b2b",
        "language": "en",
        "copy_profile": "b2b_legacy",
        "exclude_from_airtable": 0,
        "daily_limit": DEFAULT_WA_DAILY_LIMIT if is_wa_camp else 20,
        "steps": [
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
            {
                "step_order": 3,
                "delay_hours": 24,
                "template_text": VLAD_STEP3_TEXT,
                **({} if is_wa_camp else {"image_url": CANONICAL_STEP3_IMAGE_URL}),
            },
        ],
    }


def _is_vacancy_lead_item(item: dict) -> bool:
    if detect_lead_segment(item) in ("vacancy", "client_reels", "vacancy_assisted"):
        return True
    src_low = str(item.get("lead_source") or item.get("source") or "").lower()
    cat_low = str(item.get("category") or item.get("niche") or "").lower()
    return any(k in src_low for k in ("ваканс", "vacanc", "job_monitor")) or any(
        k in cat_low for k in ("ваканс", "vacanc")
    )


def _auto_enrich_b2b_items_with_sonnet(
    items: list[dict],
    *,
    is_ofm_camp: bool = False,
    skip_v2_revshare: bool = False,
) -> list[dict]:
    """Ensure every legacy B2B item without canonical Vlad SoftUGC+revshare copy is drafted via claude-sonnet-5 when API key & CRMTG_AUTO_SONNET / VPS runtime allow it."""
    from leadgen.outreach_copy import (
        REVSHARE_OFFER_TEXT,
        VLAD_STEP3_TEXT as _VLAD_STEP3_TEXT,
        build_ofm_vlad_copy_batch,
        followup_one,
        generate_vlad_sonnet_batch,
    )

    if skip_v2_revshare and not is_ofm_camp:
        return items

    if is_ofm_camp:
        ofm_items = [dict(it) if isinstance(it, dict) else it for it in items]
        need_ofm_indices = [
            idx
            for idx, it in enumerate(ofm_items)
            if isinstance(it, dict)
            and not (
                str(it.get("step1_text") or it.get("first_touch") or "").strip()
                and "SoftUGC" in str(it.get("step1_text") or it.get("first_touch") or "")
                and REVSHARE_OFFER_TEXT in str(it.get("step1_text") or it.get("first_touch") or "")
            )
        ]
        if need_ofm_indices:
            drafted_list = build_ofm_vlad_copy_batch([ofm_items[i] for i in need_ofm_indices])
            for idx, drafted in zip(need_ofm_indices, drafted_list):
                ofm_items[idx].update(drafted)
        return ofm_items

    api_key = (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    if not api_key:
        return items
    if not should_allow_auto_sonnet():
        return items

    from app.hygiene import _has_messenger_contact, sniper_lead_violation_reason

    out_items = [dict(it) if isinstance(it, dict) else it for it in items]
    need_indices: list[int] = []
    need_payloads: list[dict] = []
    for idx, it in enumerate(out_items):
        if not isinstance(it, dict) or _is_vacancy_lead_item(it):
            continue
        it_seg = detect_lead_segment(it)
        if it_seg in ("whale", "vacancy", "client_reels", "ru_expert", "social_manual", "vacancy_assisted"):
            continue
        if not _has_messenger_contact(
            username=it.get("username"),
            phone=it.get("phone"),
            telegram_user_id=it.get("telegram_user_id"),
        ):
            continue
        raw_icp = it.get("icp_score")
        if raw_icp is not None and str(raw_icp).strip() != "":
            try:
                if float(raw_icp) < 70:
                    continue
            except (TypeError, ValueError):
                continue
        if sniper_lead_violation_reason(
            name=str(it.get("name") or ""),
            company=str(it.get("company") or it.get("project") or ""),
            niche=str(it.get("category") or it.get("niche") or ""),
            note=str(it.get("their_angle") or it.get("note") or ""),
            url=str(it.get("website") or it.get("url") or ""),
            lead_source=str(it.get("lead_source") or it.get("source") or ""),
            vacancies_enabled=False,
        ):
            continue
        raw_s1 = str(it.get("step1_text") or it.get("first_touch") or "").strip()
        raw_low = raw_s1.lower()
        already_canonical = (
            bool(raw_s1)
            and "SoftUGC" in raw_s1
            and REVSHARE_OFFER_TEXT in raw_s1
            and "creatoros" not in raw_low
            and "if your user growth has hit a ceiling" not in raw_low
            and "plug-and-play" not in raw_low
        )
        if already_canonical:
            continue
        need_indices.append(idx)
        need_payloads.append(
            {
                "company": it.get("company") or it.get("project") or it.get("name") or "",
                "name": it.get("name") or "",
                "website": it.get("website") or it.get("url") or "",
                "category": it.get("category") or it.get("niche") or "",
                "their_angle": it.get("their_angle") or it.get("note") or "",
                "source": it.get("lead_source") or it.get("source") or "",
            }
        )

    if not need_payloads:
        return out_items

    for chunk_start in range(0, len(need_payloads), 15):
        chunk_idx = need_indices[chunk_start : chunk_start + 15]
        chunk_pay = need_payloads[chunk_start : chunk_start + 15]
        try:
            gen_list = generate_vlad_sonnet_batch(chunk_pay, model="claude-sonnet-5")
        except Exception as exc:
            logger.debug("Auto Sonnet batch failed: %s", exc)
            continue
        for item_i, gen_res, pay in zip(chunk_idx, gen_list, chunk_pay):
            if not isinstance(gen_res, dict):
                continue
            s1 = str(gen_res.get("step1_text") or "").strip()
            aud = str(gen_res.get("audience") or "").strip().lower()
            if not s1:
                continue
            target = out_items[item_i]
            target["first_touch"] = s1
            target["step1_text"] = s1
            if aud:
                target["audience"] = aud
            f1 = followup_one(
                str(pay.get("company") or pay.get("name") or ""),
                str(pay.get("their_angle") or ""),
                audience=aud,
                category=str(pay.get("category") or ""),
                site=str(pay.get("website") or ""),
            )
            target["followup_1"] = f1
            target["step2_text"] = f1
            target["followup_2"] = _VLAD_STEP3_TEXT
            target["step3_text"] = _VLAD_STEP3_TEXT
    return out_items


def _handle_ingest_discovered_leads(conn: sqlite3.Connection, args: dict) -> dict:
    raw_leads = args.get("leads") if args.get("leads") is not None else args.get("items")
    if not isinstance(raw_leads, list):
        raise ValueError("Параметр leads должен быть списком объектов")

    enroll_in_campaign = bool(args.get("enroll_in_campaign", True))
    raw_cid = args.get("campaign_id")
    campaign_id: int | None = int(raw_cid) if raw_cid is not None else None
    raw_camp_arg = str(args.get("campaign_name") or "").strip()

    from app.leadgen_settings import (
        _resolve_state_dir,
        get_leadgen_config,
        is_legacy_baza_vse2_name,
        resolve_target_b2b_tg_campaign_name,
    )
    from app.hygiene import (
        append_direct_x_lead,
        clean_crm_lead_names,
        is_ofm_campaign_name as _is_ofm_camp_name_fn,
        sniper_lead_violation_reason,
    )

    leadgen_cfg = get_leadgen_config(conn)
    if campaign_id is None and is_legacy_baza_vse2_name(raw_camp_arg):
        raw_camp_arg = CAMPAIGN_WHALES_EN_TG

    fallback_camp_name = raw_camp_arg or "Leadgen Autopilot"
    dict_items = [it for it in raw_leads if isinstance(it, dict)]
    all_vacancies = bool(dict_items) and all(_is_vacancy_lead_item(it) for it in dict_items)
    all_wa_only = bool(dict_items) and all(
        str(it.get("provider") or "").strip().lower() == "whatsapp"
        and not _clean_handle(it.get("username"))
        and bool(str(it.get("phone") or it.get("whatsapp") or "").strip())
        for it in dict_items
    )

    if campaign_id is None:
        raw_low = raw_camp_arg.lower()
        if (
            any(k in raw_low for k in ("ваканс", "vacanc", "job", "эксперт"))
            or "(wa)" in raw_low
            or "whatsapp" in raw_low
            or " — wa" in raw_low
            or _is_ofm_camp_name_fn(raw_camp_arg)
            or raw_low in {
                CAMPAIGN_WHALES_EN_TG.lower(),
                CAMPAIGN_WHALES_EN_WA.lower(),
                CAMPAIGN_VACANCY_RU_TG.lower(),
                CAMPAIGN_VACANCY_RU_WA.lower(),
                CAMPAIGN_VACANCY_EN_TG.lower(),
                CAMPAIGN_RU_EXPERTS_TG.lower(),
            }
        ):
            campaign_name = raw_camp_arg
        elif all_vacancies and raw_low in ("", "leadgen auto (tg)", "leadgen autopilot"):
            first_lang = detect_lead_language(dict_items[0])
            campaign_name = resolve_target_campaign("vacancy", first_lang, "telegram", leadgen_cfg)
        elif all_wa_only and not _is_qa_campaign_name(raw_camp_arg):
            campaign_name = str(
                (leadgen_cfg.get("b2b") or {}).get("target_campaign_wa") or CAMPAIGN_WHALES_EN_WA
            ).strip() or CAMPAIGN_WHALES_EN_WA
        elif raw_camp_arg:
            if raw_low == "leadgen autopilot":
                existing_ap = conn.execute(
                    "SELECT name FROM campaigns WHERE lower(trim(name)) = 'leadgen autopilot' ORDER BY id ASC LIMIT 1"
                ).fetchone()
                campaign_name = (
                    str(existing_ap["name"])
                    if existing_ap is not None
                    else resolve_target_b2b_tg_campaign_name(
                        conn,
                        raw_camp_arg,
                        fallback_default=raw_camp_arg,
                    )
                )
            else:
                campaign_name = resolve_target_b2b_tg_campaign_name(
                    conn,
                    raw_camp_arg,
                    fallback_default=CAMPAIGN_WHALES_EN_TG,
                )
        else:
            campaign_name = resolve_target_b2b_tg_campaign_name(
                conn,
                "",
                fallback_default=fallback_camp_name,
            )
    else:
        campaign_name = fallback_camp_name

    def _lookup_existing_campaign(target_cid: int | None, target_cname: str) -> dict[str, Any] | None:
        if target_cid is not None:
            crow = conn.execute("SELECT * FROM campaigns WHERE id = ?", (target_cid,)).fetchone()
            if crow is None:
                raise LookupError(f"Кампания #{target_cid} не найдена")
            return as_dict(crow)
        all_c = [as_dict(r) for r in conn.execute("SELECT * FROM campaigns ORDER BY id ASC").fetchall()]
        return next(
            (c for c in all_c if str(c.get("name") or "").strip().lower() == target_cname.strip().lower()),
            None,
        )

    def _get_or_create_camp_state(
        target_cid: int | None,
        target_cname: str,
        *,
        item_segment: str | None = None,
        item_lang: str | None = None,
    ) -> dict[str, Any]:
        c_dict = _lookup_existing_campaign(target_cid, target_cname)
        if c_dict is not None:
            resolved_cid = int(c_dict["id"])
        else:
            v2_meta = _campaign_v2_defaults_for_name(
                target_cname,
                item_segment=item_segment,
                item_lang=item_lang,
            )
            created_camp = create_campaign(
                conn,
                name=target_cname,
                status="draft",
                daily_limit=int(v2_meta["daily_limit"]),
                steps=v2_meta["steps"],
            )
            resolved_cid = int(created_camp["id"])
            _persist_campaign_v2_fields(
                conn,
                resolved_cid,
                segment=str(v2_meta["segment"]),
                language=str(v2_meta["language"]),
                copy_profile=str(v2_meta["copy_profile"]),
                exclude_from_airtable=int(v2_meta["exclude_from_airtable"]),
            )
            conn.commit()
            refreshed = conn.execute("SELECT * FROM campaigns WHERE id = ?", (resolved_cid,)).fetchone()
            c_dict = as_dict(refreshed) if refreshed is not None else created_camp

        acc_ids = _parse_account_ids(c_dict.get("account_id"), c_dict.get("account_ids_json"))
        acc_id = acc_ids[0] if acc_ids else c_dict.get("account_id")
        existing_rows = conn.execute(
            "SELECT username, phone, lead_id, telegram_user_id, name, company, website FROM campaign_recipients WHERE campaign_id = ?",
            (resolved_cid,),
        ).fetchall()
        return {
            "campaign_id": resolved_cid,
            "camp_dict": c_dict,
            "account_id": acc_id,
            "added_pending": 0,
            "seen_usernames": {str(r["username"]).lower() for r in existing_rows if r["username"]},
            "seen_phones": {str(r["phone"]) for r in existing_rows if r["phone"]},
            "seen_leads": {int(r["lead_id"]) for r in existing_rows if r["lead_id"]},
            "seen_tg_ids": {int(r["telegram_user_id"]) for r in existing_rows if r["telegram_user_id"]},
        }

    def _build_global_dedup_index() -> dict[str, set]:
        g_tg_ids: set[int] = set()
        g_usernames: set[str] = set()
        g_phones: set[str] = set()
        g_leads: set[int] = set()
        g_domains: set[str] = set()
        g_person_company: set[tuple[str, str]] = set()
        archived_usernames: set[str] = set()
        archived_tg_ids: set[int] = set()
        archived_phones: set[str] = set()
        archived_domains: set[str] = set()
        lead_id_by_username: dict[str, int] = {}
        lead_id_by_phone: dict[str, int] = {}
        lead_id_by_tg_id: dict[int, int] = {}
        lead_id_by_domain: dict[str, int] = {}

        # 1) All non-QA campaign_recipients across ALL campaigns
        rec_rows = conn.execute(
            """
            SELECT cr.lead_id, cr.telegram_user_id, cr.username, cr.phone,
                   cr.name, cr.company, cr.website, cr.note, cr.extra_json,
                   c.name AS camp_name
            FROM campaign_recipients cr
            JOIN campaigns c ON c.id = cr.campaign_id
            """
        ).fetchall()
        for r in rec_rows:
            cname = str(r["camp_name"] or "")
            if _is_qa_campaign_name(cname):
                continue
            rec_lid = int(r["lead_id"]) if r["lead_id"] else None
            if rec_lid is not None:
                g_leads.add(rec_lid)
            tid = r["telegram_user_id"]
            if tid and not (900000 <= int(tid) <= 999999):
                g_tg_ids.add(int(tid))
                if rec_lid is not None:
                    lead_id_by_tg_id.setdefault(int(tid), rec_lid)
            u = (_clean_handle(r["username"]) or "").lower()
            if u:
                g_usernames.add(u)
                if rec_lid is not None:
                    lead_id_by_username.setdefault(u, rec_lid)
            pk = _norm_phone_key(r["phone"])
            if pk:
                g_phones.add(pk)
                if rec_lid is not None:
                    lead_id_by_phone.setdefault(pk, rec_lid)
            dom = _norm_dedup_domain(r["website"])
            if dom:
                g_domains.add(dom)
                if rec_lid is not None:
                    lead_id_by_domain.setdefault(dom, rec_lid)
            pk_name = _norm_person_name_key(r["name"], r["company"])
            ck_comp = _norm_company_key(r["company"])
            if pk_name and ck_comp:
                g_person_company.add((pk_name, ck_comp))

            if _LEGACY_1300_DB_RE.search(f"{cname} {r['note'] or ''} {r['extra_json'] or ''}"):
                if u:
                    archived_usernames.add(u)
                if tid and not (900000 <= int(tid) <= 999999):
                    archived_tg_ids.add(int(tid))
                if pk:
                    archived_phones.add(pk)
                if dom:
                    archived_domains.add(dom)

        # 2) Outbound messages in dialogs + leads (already contacted outside/inside campaigns)
        out_dlg_rows = conn.execute(
            """
            SELECT DISTINCT d.peer_id, d.username, d.phone, d.lead_id
            FROM dialogs d
            JOIN messages m ON m.dialog_id = d.id
            WHERE m.direction = 'out'
            """
        ).fetchall()
        for r in out_dlg_rows:
            dlg_lid = int(r["lead_id"]) if r["lead_id"] else None
            if dlg_lid is not None:
                g_leads.add(dlg_lid)
            pid = r["peer_id"]
            if pid is not None and str(pid).lstrip("-").isdigit():
                tid_i = int(pid)
                if tid_i > 0 and not (900000 <= tid_i <= 999999):
                    g_tg_ids.add(tid_i)
                    if dlg_lid is not None:
                        lead_id_by_tg_id.setdefault(tid_i, dlg_lid)
            u = (_clean_handle(r["username"]) or "").lower()
            if u:
                g_usernames.add(u)
                if dlg_lid is not None:
                    lead_id_by_username.setdefault(u, dlg_lid)
            pk = _norm_phone_key(r["phone"])
            if pk:
                g_phones.add(pk)
                if dlg_lid is not None:
                    lead_id_by_phone.setdefault(pk, dlg_lid)

        out_lead_rows = conn.execute(
            """
            SELECT DISTINCT l.id, l.telegram_user_id, l.username, l.phone, l.name, l.company, l.url
            FROM leads l
            JOIN dialogs d ON (d.lead_id = l.id OR d.id = l.dialog_id)
            JOIN messages m ON m.dialog_id = d.id
            WHERE m.direction = 'out'
            """
        ).fetchall()
        for r in out_lead_rows:
            out_lid = int(r["id"])
            g_leads.add(out_lid)
            tid = r["telegram_user_id"]
            if tid and not (900000 <= int(tid) <= 999999):
                g_tg_ids.add(int(tid))
                lead_id_by_tg_id.setdefault(int(tid), out_lid)
            u = (_clean_handle(r["username"]) or "").lower()
            if u:
                g_usernames.add(u)
                lead_id_by_username.setdefault(u, out_lid)
            pk = _norm_phone_key(r["phone"])
            if pk:
                g_phones.add(pk)
                lead_id_by_phone.setdefault(pk, out_lid)
            dom = _norm_dedup_domain(r["url"])
            if dom:
                g_domains.add(dom)
                lead_id_by_domain.setdefault(dom, out_lid)
            pk_name = _norm_person_name_key(r["name"], r["company"])
            ck_comp = _norm_company_key(r["company"])
            if pk_name and ck_comp:
                g_person_company.add((pk_name, ck_comp))

        # 3) Leads table archive 1300+ markers & person+company/domain index for existing leads
        lead_cols = _mcp_table_columns(conn, "leads")
        extra_v2_select = ", ".join(
            col
            for col in (
                "segment",
                "language",
                "exclude_from_airtable",
                "company_domain",
                "pool_usd",
                "secondary_contact_json",
                "manual_action_url",
                "manual_draft_text",
            )
            if col in lead_cols
        )
        select_cols = (
            "id, telegram_user_id, username, phone, name, company, url, note, description, lead_source"
            + (f", {extra_v2_select}" if extra_v2_select else "")
        )
        all_lead_rows = conn.execute(f"SELECT {select_cols} FROM leads").fetchall()
        leads_by_person_company: dict[tuple[str, str], dict] = {}
        leads_by_domain: dict[str, dict] = {}
        for r in all_lead_rows:
            ld = as_dict(r)
            lid_i = int(ld["id"])
            u_l = (_clean_handle(ld.get("username")) or "").lower()
            if u_l:
                lead_id_by_username.setdefault(u_l, lid_i)
            pk_l = _norm_phone_key(ld.get("phone"))
            if pk_l:
                lead_id_by_phone.setdefault(pk_l, lid_i)
            tid_l = ld.get("telegram_user_id")
            if tid_l and not (900000 <= int(tid_l) <= 999999):
                lead_id_by_tg_id.setdefault(int(tid_l), lid_i)
            pk_name = _norm_person_name_key(ld.get("name"), ld.get("company"))
            ck_comp = _norm_company_key(ld.get("company"))
            if pk_name and ck_comp and (pk_name, ck_comp) not in leads_by_person_company:
                leads_by_person_company[(pk_name, ck_comp)] = ld
            dom = _norm_dedup_domain(ld.get("company_domain") or ld.get("url"))
            if dom:
                lead_id_by_domain.setdefault(dom, lid_i)
                if dom not in leads_by_domain:
                    leads_by_domain[dom] = ld

            marker_blob = f"{ld.get('note') or ''} {ld.get('description') or ''} {ld.get('lead_source') or ''}"
            if _LEGACY_1300_DB_RE.search(marker_blob):
                g_leads.add(lid_i)
                u = u_l
                if u:
                    g_usernames.add(u)
                    archived_usernames.add(u)
                tid = tid_l
                if tid and not (900000 <= int(tid) <= 999999):
                    g_tg_ids.add(int(tid))
                    archived_tg_ids.add(int(tid))
                pk = pk_l
                if pk:
                    g_phones.add(pk)
                    archived_phones.add(pk)
                if dom:
                    g_domains.add(dom)
                    archived_domains.add(dom)
                if pk_name and ck_comp:
                    g_person_company.add((pk_name, ck_comp))

        return {
            "tg_ids": g_tg_ids,
            "usernames": g_usernames,
            "phones": g_phones,
            "leads": g_leads,
            "domains": g_domains,
            "person_company": g_person_company,
            "archived_usernames": archived_usernames,
            "archived_tg_ids": archived_tg_ids,
            "archived_phones": archived_phones,
            "archived_domains": archived_domains,
            "leads_by_person_company": leads_by_person_company,
            "leads_by_domain": leads_by_domain,
            "lead_id_by_username": lead_id_by_username,
            "lead_id_by_phone": lead_id_by_phone,
            "lead_id_by_tg_id": lead_id_by_tg_id,
            "lead_id_by_domain": lead_id_by_domain,
        }

    camp_cache: dict[str, dict[str, Any]] = {}
    primary_state: dict[str, Any] | None = None
    camp_dict: dict[str, Any] | None = None
    account_id: int | None = None
    explicit_cid_requested = campaign_id is not None

    if explicit_cid_requested and campaign_id is not None:
        crow = _lookup_existing_campaign(campaign_id, campaign_name)
        assert crow is not None
        camp_dict = crow
        campaign_name = str(crow.get("name") or campaign_name)
        if enroll_in_campaign:
            primary_state = _get_or_create_camp_state(campaign_id, campaign_name)
            camp_dict = primary_state["camp_dict"]
            account_id = primary_state["account_id"]
            camp_cache[str(camp_dict.get("name") or "").strip().lower()] = primary_state
    elif enroll_in_campaign and not dict_items:
        primary_state = _get_or_create_camp_state(None, campaign_name)
        campaign_id = int(primary_state["campaign_id"])
        camp_dict = primary_state["camp_dict"]
        account_id = primary_state["account_id"]
        camp_cache[str(camp_dict.get("name") or "").strip().lower()] = primary_state
    elif enroll_in_campaign and raw_camp_arg:
        existing_prim = _lookup_existing_campaign(None, campaign_name)
        if existing_prim is not None:
            camp_dict = existing_prim

    primary_v2_meta = _campaign_v2_defaults_for_name(campaign_name)
    is_primary_vacancy = bool(
        (camp_dict is not None and is_vacancy_campaign(camp_dict))
        or primary_v2_meta["segment"] in ("vacancy", "client_reels")
    )
    is_primary_ofm = bool(
        (camp_dict is not None and _is_ofm_camp_name_fn(camp_dict.get("name"), camp_dict.get("leadgen_sources_json")))
        or _is_ofm_camp_name_fn(campaign_name)
    )
    skip_v2_revshare = bool(
        primary_v2_meta["copy_profile"] in ("whale_mass", "vacancy_ru", "vacancy_en", "ru_expert")
        or str((camp_dict or {}).get("copy_profile") or "") in ("whale_mass", "vacancy_ru", "vacancy_en", "ru_expert")
    )
    if not is_primary_vacancy:
        raw_leads = _auto_enrich_b2b_items_with_sonnet(
            raw_leads,
            is_ofm_camp=is_primary_ofm,
            skip_v2_revshare=skip_v2_revshare,
        )

    global_dedup = _build_global_dedup_index()
    state_dir = _resolve_state_dir()
    showcase_url = str((leadgen_cfg.get("vacancies") or {}).get("showcase_url") or DEFAULT_VACANCY_SHOWCASE_URL).strip()

    processed = 0
    created_leads = 0
    updated_leads = 0
    enrolled_in_campaign = 0
    skipped_duplicates = 0
    skipped_non_icp = 0
    quarantined = 0
    saved_direct_x_leads = 0
    lead_ids: list[int] = []
    lead_id_by_index: list[int | None] = [None] * len(raw_leads)
    lead_results: list[dict[str, Any]] = [
        {"index": idx, "lead_id": None, "status": "skipped", "reason": "unprocessed"}
        for idx in range(len(raw_leads))
    ]
    primary_resolved_cid: int | None = campaign_id
    now = utc_now()

    def _record_skip(idx: int, reason: str, lid_val: int | None = None) -> None:
        lead_id_by_index[idx] = lid_val
        lead_results[idx] = {
            "index": idx,
            "lead_id": lid_val,
            "status": "skipped",
            "reason": reason,
        }

    for item_idx, item in enumerate(raw_leads):
        if not isinstance(item, dict):
            _record_skip(item_idx, "invalid_item")
            continue
        raw_uname = _clean_handle(item.get("username"))
        had_bot_username = bool(raw_uname and raw_uname.lower().endswith("bot"))
        username = None if had_bot_username else raw_uname
        raw_company = _clean_company_title(item.get("company"))
        explicit_name = (str(item.get("name") or "").strip()) or None
        phone = (str(item.get("phone") or item.get("whatsapp") or "").strip()) or None
        email = (str(item.get("email") or item.get("work_email") or "").strip()) or None
        x_url = (str(item.get("x_url") or item.get("founder_x") or item.get("founder_twitter") or item.get("twitter") or "").strip()) or None
        linkedin_url = (str(item.get("linkedin_url") or item.get("founder_linkedin") or "").strip()) or None
        instagram_url = (str(item.get("instagram_url") or item.get("instagram") or item.get("ig_url") or "").strip()) or None
        website_raw = pick_best_product_url(item)
        website = website_raw or None
        url_val = None
        if website_raw:
            url_val = website_raw if re.match(r"^https?://", website_raw, flags=re.IGNORECASE) else f"https://{website_raw}"

        category = (str(item.get("category") or item.get("niche") or "").strip()) or None
        their_angle = (str(item.get("their_angle") or "").strip()) or None
        our_angle = (str(item.get("our_angle") or "").strip()) or None
        hook = (str(item.get("hook") or "").strip()) or None
        lead_source = (str(item.get("lead_source") or item.get("source") or "").strip()) or None
        raw_note = (str(item.get("note") or "").strip()) or None

        raw_provider = str(item.get("provider") or "").strip().lower()
        tg_id_raw = item.get("telegram_user_id")
        if raw_provider in ("telegram", "whatsapp"):
            provider = raw_provider
        elif bool(item.get("tg_phone_direct")) and (tg_id_raw is not None or phone):
            provider = "telegram"
        elif item.get("whatsapp") and not username and not tg_id_raw:
            provider = "whatsapp"
        else:
            provider = "telegram"

        tg_id: int | None = None
        if tg_id_raw is not None and str(tg_id_raw).strip():
            try:
                tg_id = int(str(tg_id_raw).strip())
            except ValueError:
                tg_id = None
        real_tg_id = tg_id if (tg_id is not None and tg_id > 0 and not (900000 <= tg_id <= 999999)) else None
        has_direct_messenger_handle = bool(username or phone or real_tg_id is not None)
        if (
            had_bot_username
            and not has_direct_messenger_handle
            and not any((email, x_url, linkedin_url, instagram_url, website))
            and not _is_qa_campaign_name(campaign_name)
        ):
            skipped_non_icp += 1
            quarantined += 1
            _record_skip(item_idx, "bot_username_blocked")
            continue

        pool_usd = parse_pool_usd_value(item.get("pool_usd"))
        company_domain = _extract_company_domain(item, website_raw, url_val)
        secondary_contact_json = _serialize_secondary_contact(item)
        manual_action_url = _resolve_manual_action_url(
            item,
            x_url=x_url,
            linkedin_url=linkedin_url,
            instagram_url=instagram_url,
            url_val=url_val,
        )
        raw_manual_draft = (str(item.get("manual_draft_text") or "").strip()) or None

        detect_payload = dict(item)
        if lead_source and not detect_payload.get("source"):
            detect_payload["source"] = lead_source
        if website and not detect_payload.get("website"):
            detect_payload["website"] = website
        if linkedin_url and not detect_payload.get("linkedin_url"):
            detect_payload["linkedin_url"] = linkedin_url
        if instagram_url and not detect_payload.get("instagram_url"):
            detect_payload["instagram_url"] = instagram_url
        if manual_action_url and not detect_payload.get("manual_action_url"):
            detect_payload["manual_action_url"] = manual_action_url
        if pool_usd is not None and detect_payload.get("pool_usd") is None:
            detect_payload["pool_usd"] = pool_usd

        detected_segment = detect_lead_segment(detect_payload)
        detected_language = detect_lead_language(detect_payload)
        is_queue_only_segment = detected_segment in ("social_manual", "vacancy_assisted")

        company = raw_company
        display_name = (
            explicit_name
            or company
            or (f"{username} · {company}" if username and company else username)
            or phone
            or email
            or linkedin_url
            or instagram_url
            or x_url
            or manual_action_url
            or website
        )
        if not any((display_name, username, phone, tg_id, email, x_url, linkedin_url, instagram_url, manual_action_url, website)):
            _record_skip(item_idx, "empty_item")
            continue

        explicit_v2_keys: set[str] = set()
        if str(item.get("segment") or "").strip():
            explicit_v2_keys.add("segment")
        if str(item.get("language") or "").strip().lower() in ("ru", "en"):
            explicit_v2_keys.add("language")
        if item.get("exclude_from_airtable") is not None:
            explicit_v2_keys.add("exclude_from_airtable")
        if str(item.get("company_domain") or item.get("domain") or "").strip():
            explicit_v2_keys.add("company_domain")
        if item.get("pool_usd") is not None:
            explicit_v2_keys.add("pool_usd")
        if any(item.get(k) not in (None, "", [], {}) for k in ("secondary_contact_json", "secondary_contact", "secondary_dm")):
            explicit_v2_keys.add("secondary_contact")
        if any(str(item.get(k) or "").strip() for k in ("manual_action_url", "apply_url", "job_url", "profile_url")):
            explicit_v2_keys.add("manual_action_url")
        if raw_manual_draft:
            explicit_v2_keys.add("manual_draft_text")

        is_item_vacancy = _is_vacancy_lead_item(item) or detected_segment in ("vacancy", "client_reels", "vacancy_assisted")
        is_item_wa_only = (provider == "whatsapp" and not username and bool(phone))

        # Resolve target campaign BEFORE touching `leads` so all ICP/supply/denylist filters run first!
        target_camp_name = campaign_name
        target_camp_dict: dict[str, Any] | None = camp_dict
        target_campaign_id: int | None = campaign_id
        active_state: dict[str, Any] | None = primary_state

        if not explicit_cid_requested and not is_queue_only_segment:
            raw_arg_low = raw_camp_arg.lower()
            is_generic_or_empty_arg = (
                not raw_camp_arg
                or raw_arg_low in ("leadgen autopilot", "leadgen auto (tg)")
                or is_legacy_baza_vse2_name(raw_camp_arg)
            )
            if _is_qa_campaign_name(raw_camp_arg):
                target_camp_name = raw_camp_arg
            elif is_generic_or_empty_arg:
                if detected_segment in ("whale", "vacancy", "client_reels", "ru_expert"):
                    target_camp_name = resolve_target_campaign(
                        detected_segment,
                        detected_language,
                        provider,
                        leadgen_cfg,
                    )
                elif detected_segment == "ofm":
                    if bool((leadgen_cfg.get("b2b") or {}).get("ofm_outreach_enabled")):
                        target_camp_name = str(
                            (leadgen_cfg.get("b2b") or {}).get("target_campaign_ofm") or CAMPAIGN_OFM_TG
                        ).strip() or CAMPAIGN_OFM_TG
                    else:
                        target_camp_name = campaign_name
                elif is_item_wa_only:
                    wa_legacy = _lookup_existing_campaign(None, "Leadgen Auto (WA)")
                    wa_v2 = _lookup_existing_campaign(None, CAMPAIGN_WHALES_EN_WA)
                    if wa_v2 is not None:
                        target_camp_name = str(wa_v2["name"])
                    elif wa_legacy is not None and not raw_camp_arg:
                        target_camp_name = str(wa_legacy["name"])
                    else:
                        target_camp_name = str(
                            (leadgen_cfg.get("b2b") or {}).get("target_campaign_wa") or CAMPAIGN_WHALES_EN_WA
                        ).strip() or CAMPAIGN_WHALES_EN_WA
                else:
                    target_camp_name = campaign_name
            else:
                if is_item_vacancy and not is_primary_vacancy:
                    target_camp_name = resolve_target_campaign(
                        "vacancy",
                        detected_language,
                        provider,
                        leadgen_cfg,
                    )
                elif is_item_wa_only and "(wa)" not in raw_arg_low and "whatsapp" not in raw_arg_low and " — wa" not in raw_arg_low:
                    target_camp_name = resolve_target_campaign(
                        "whale" if detected_segment == "whale" else "b2b",
                        detected_language,
                        "whatsapp",
                        leadgen_cfg,
                    )
                elif detected_segment == "ru_expert" and "эксперт" not in raw_arg_low:
                    target_camp_name = resolve_target_campaign(
                        "ru_expert",
                        "ru",
                        provider,
                        leadgen_cfg,
                    )
                else:
                    target_camp_name = raw_camp_arg

            target_camp_dict = _lookup_existing_campaign(None, target_camp_name)
            if target_camp_dict is not None:
                target_campaign_id = int(target_camp_dict["id"])
        elif is_queue_only_segment:
            active_state = None
            if not explicit_cid_requested:
                target_campaign_id = None
                target_camp_dict = None

        inferred_camp_v2 = _campaign_v2_defaults_for_name(
            target_camp_name,
            item_segment=None if is_queue_only_segment else detected_segment,
            item_lang=detected_language,
        )
        camp_segment = str((target_camp_dict or {}).get("segment") or inferred_camp_v2["segment"] or "").strip().lower()
        effective_segment = detected_segment
        if not is_queue_only_segment and detected_segment == "b2b" and camp_segment in ("whale", "vacancy", "client_reels", "ru_expert", "ofm"):
            effective_segment = camp_segment

        explicit_item_lang = str(item.get("language") or "").strip().lower()
        effective_language = explicit_item_lang if explicit_item_lang in ("ru", "en") else detected_language

        camp_exclude = int((target_camp_dict or {}).get("exclude_from_airtable") or inferred_camp_v2.get("exclude_from_airtable") or 0)
        item_exclude = int(bool(item.get("exclude_from_airtable")))
        exclude_from_airtable_val = 1 if (
            effective_segment == "ru_expert"
            or camp_exclude == 1
            or item_exclude == 1
        ) else 0

        is_qa_camp = _is_qa_campaign_name(target_camp_name)
        is_wa_target_camp = "(wa)" in target_camp_name.lower() or "whatsapp" in target_camp_name.lower() or " — wa" in target_camp_name.lower()
        if enroll_in_campaign and explicit_cid_requested and not is_queue_only_segment and is_item_wa_only and not is_wa_target_camp and not is_qa_camp:
            skipped_non_icp += 1
            quarantined += 1
            _record_skip(item_idx, "wa_campaign_mismatch")
            continue

        is_ofm_target_camp = _is_ofm_camp_name_fn(
            target_camp_name,
            (target_camp_dict or {}).get("leadgen_sources_json"),
        )
        is_legacy_vac_camp = any(k in target_camp_name.lower() for k in ("ваканс", "vacanc", "job", "client reels"))
        is_ru_expert_item = effective_segment == "ru_expert"
        is_vacancy_or_reels = is_item_vacancy or is_legacy_vac_camp or effective_segment in ("vacancy", "client_reels", "vacancy_assisted")
        is_b2b_camp = (
            not is_vacancy_or_reels
            and not is_ofm_target_camp
            and not is_ru_expert_item
            and effective_segment in ("b2b", "whale", "social_manual", "ofm")
        )

        if is_b2b_camp and effective_segment in ("b2b", "whale"):
            cleaned_name, cleaned_comp = clean_crm_lead_names(
                explicit_name or company or display_name,
                company,
                url_val or website_raw,
            )
            company = cleaned_comp
            if explicit_name or cleaned_name:
                explicit_name = cleaned_name or explicit_name
                display_name = cleaned_name or display_name

        raw_s1 = item.get("step1_text") if item.get("step1_text") is not None else item.get("first_touch")
        full_blob = " ".join(
            str(x)
            for x in (
                username or "",
                display_name or "",
                company or "",
                website_raw,
                email or "",
                their_angle or "",
                our_angle or "",
                raw_note or "",
                str(raw_s1 or ""),
            )
            if x
        )

        # 1. Competitor denylist & archive 1300+ (blocked BEFORE `leads` insert!)
        uname_low = username.lower() if username else ""
        phone_key = _norm_phone_key(phone)
        domain_key = _norm_dedup_domain(company_domain or url_val or website_raw) if is_b2b_camp else ""
        person_key = _norm_person_name_key(explicit_name or display_name, company) if is_b2b_camp else ""
        comp_key = _norm_company_key(company) if is_b2b_camp else ""

        if not is_qa_camp:
            if _COMPETITOR_DENYLIST_RE.search(full_blob):
                skipped_duplicates += 1
                _record_skip(item_idx, "competitor_denylist")
                continue
            if is_vacancy_or_reels and not is_ofm_target_camp:
                from app.outreach import _VACANCY_JUNK_ROLE_RE

                if _VACANCY_JUNK_ROLE_RE.search(full_blob):
                    skipped_duplicates += 1
                    _record_skip(item_idx, "vacancy_junk_role")
                    continue
            if (
                is_legacy_exhausted_recipient(item, campaign_name=target_camp_name)
                or (uname_low and uname_low in global_dedup["archived_usernames"])
                or (real_tg_id and real_tg_id in global_dedup["archived_tg_ids"])
                or (phone_key and phone_key in global_dedup["archived_phones"])
                or (domain_key and domain_key in global_dedup["archived_domains"])
            ):
                skipped_duplicates += 1
                _record_skip(item_idx, "legacy_exhausted")
                continue

        # 2. Strict B2B / Whale Product-Buyer ICP / Sniper / Score / Contactless gate BEFORE `leads` insert!
        if is_b2b_camp and not is_qa_camp:
            if (
                "ofm_tier" in item
                or (lead_source or "").strip().lower().startswith("ofm")
                or re.search(
                    r"\b(?:onlyfans|only[\s\-]?fans|fanvue|fansly|loyalfans|chatteurs?|ofm\s+agency|agence\s+ofm)\b|\bof\s+(?:model|agency|creator|traffic|page)\b",
                    full_blob,
                    re.I,
                )
            ):
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, "ofm_in_b2b")
                continue
            raw_icp_score = item.get("icp_score")
            if raw_icp_score is not None and str(raw_icp_score).strip() != "":
                try:
                    if float(raw_icp_score) < 70:
                        skipped_non_icp += 1
                        quarantined += 1
                        _record_skip(item_idx, "low_icp_score")
                        continue
                except (TypeError, ValueError):
                    skipped_non_icp += 1
                    quarantined += 1
                    _record_skip(item_idx, "invalid_icp_score")
                    continue

            sniper_reason = sniper_lead_violation_reason(
                name=display_name or "",
                company=company or "",
                niche=category or "",
                note=f"{raw_note or ''}\n{their_angle or ''}\n{str(item.get('bio') or '')}".strip(),
                description=str(item.get("description") or item.get("what_they_do") or ""),
                url=url_val or website_raw or (manual_action_url if is_queue_only_segment else "") or "",
                lead_source=lead_source or "",
                vacancies_enabled=False,
            )
            if sniper_reason:
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, f"sniper:{sniper_reason}")
                continue

            if is_offline_non_icp_contact(
                username=username,
                name=display_name,
                company=company,
                website=url_val,
                their_angle=f"{their_angle or ''} {raw_s1 or ''}",
            ):
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, "offline_non_icp")
                continue
            supply_blob = "\n".join(
                str(x)
                for x in (
                    explicit_name or "",
                    company or "",
                    their_angle or "",
                    str(item.get("bio") or ""),
                    raw_note or "",
                )
                if x
            )
            if is_supply_side(supply_blob) or is_rejected_buyer_category(category):
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, "supply_side_or_rejected_category")
                continue
            roles_set = {str(x).strip() for x in (item.get("roles") or []) if str(x or "").strip()}
            if item.get("role"):
                roles_set.add(str(item["role"]).strip())
            if roles_set and not (roles_set & {"Founder", "Marketing"}) and (roles_set & NON_BUYER_ROLES):
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, "non_buyer_role")
                continue
            if (
                has_insufficient_product_info(their_angle)
                or has_insufficient_product_info(our_angle)
                or has_insufficient_product_info(raw_note)
                or has_insufficient_product_info(str(raw_s1 or ""))
                or is_raw_cohort_tag(company)
                or is_raw_cohort_tag(display_name)
            ):
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, "insufficient_product_info")
                continue
            if raw_s1 is not None and str(raw_s1).strip() != "" and is_truncated_or_bad_pitch(str(raw_s1), min_len=40):
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, "bad_pitch")
                continue

            if not has_direct_messenger_handle and not is_queue_only_segment:
                if email or x_url or linkedin_url or url_val or website:
                    if append_direct_x_lead(
                        state_dir,
                        {
                            "name": display_name,
                            "company": company,
                            "email": email,
                            "x_url": x_url,
                            "linkedin_url": linkedin_url,
                            "url": url_val or website,
                            "category": category,
                            "their_angle": their_angle,
                            "lead_source": lead_source,
                            "icp_score": raw_icp_score,
                        },
                    ):
                        saved_direct_x_leads += 1
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, "missing_direct_messenger_handle")
                continue
        elif is_ofm_target_camp and not is_qa_camp:
            ofm_sniper_reason = sniper_lead_violation_reason(
                name=display_name or "",
                company=company or "",
                niche=category or "",
                note=f"{raw_note or ''}\n{their_angle or ''}\n{str(item.get('bio') or '')}".strip(),
                description=str(item.get("description") or item.get("what_they_do") or ""),
                url=url_val or website_raw or "",
                lead_source=lead_source or "",
                vacancies_enabled=False,
                ofm_enabled=True,
            )
            if ofm_sniper_reason:
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, f"ofm_sniper:{ofm_sniper_reason}")
                continue
            if raw_s1 is not None and str(raw_s1).strip() != "" and is_truncated_or_bad_pitch(str(raw_s1), min_len=40):
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, "bad_pitch")
                continue

        if is_queue_only_segment:
            if not any((manual_action_url, linkedin_url, instagram_url, x_url, url_val, email, has_direct_messenger_handle)) and not is_qa_camp:
                skipped_non_icp += 1
                quarantined += 1
                _record_skip(item_idx, "missing_action_url_or_handle")
                continue
        elif not has_direct_messenger_handle and not is_qa_camp:
            skipped_non_icp += 1
            quarantined += 1
            _record_skip(item_idx, "missing_direct_messenger_handle")
            continue

        processed += 1

        note_parts = []
        if raw_note:
            note_parts.append(raw_note)
        if their_angle and their_angle not in note_parts:
            note_parts.append(f"Их угол: {their_angle}" if raw_note else their_angle)
        if our_angle and our_angle not in note_parts:
            note_parts.append(f"Наш угол: {our_angle}")
        combined_note = "\n".join(note_parts).strip() or None

        existing = None
        if real_tg_id is not None:
            row = conn.execute(
                "SELECT * FROM leads WHERE telegram_user_id = ? ORDER BY id ASC LIMIT 1",
                (real_tg_id,),
            ).fetchone()
            if row is not None:
                existing = as_dict(row)
        if existing is None and (username or phone):
            existing = _find_lead_by_lookup(conn, username=username, phone=phone)
        if existing is None and is_queue_only_segment and manual_action_url and "manual_action_url" in _mcp_table_columns(conn, "leads"):
            row = conn.execute(
                "SELECT * FROM leads WHERE manual_action_url = ? ORDER BY id ASC LIMIT 1",
                (manual_action_url,),
            ).fetchone()
            if row is not None:
                existing = as_dict(row)
        if existing is None and is_b2b_camp and person_key and comp_key:
            existing = global_dedup["leads_by_person_company"].get((person_key, comp_key))
        if existing is None and is_b2b_camp and domain_key:
            cand_dom_lead = global_dedup["leads_by_domain"].get(domain_key)
            if cand_dom_lead is not None:
                existing_u = (_clean_handle(cand_dom_lead.get("username")) or "").lower()
                if not existing_u or not uname_low or existing_u == uname_low or domain_key in global_dedup["domains"]:
                    existing = cand_dom_lead
        if existing is None and is_b2b_camp and company and not _is_non_mergeable_company(company):
            row = conn.execute(
                "SELECT * FROM leads WHERE company IS NOT NULL AND lower(company) = lower(?) ORDER BY id ASC LIMIT 1",
                (company,),
            ).fetchone()
            if row is not None:
                cand_co = as_dict(row)
                cand_u = (_clean_handle(cand_co.get("username")) or "").lower()
                cand_person = _norm_person_name_key(cand_co.get("name"), cand_co.get("company"))
                if not cand_u or not uname_low or cand_u == uname_low or (person_key and cand_person == person_key):
                    existing = cand_co

        # Check global cross-campaign / outbound-message dedup BEFORE creating a brand-new `leads` row!
        is_global_dup = False
        if not is_qa_camp:
            existing_lid_check = int(existing["id"]) if existing is not None else None
            is_global_dup = bool(
                (existing_lid_check is not None and existing_lid_check in global_dedup["leads"])
                or (real_tg_id is not None and real_tg_id in global_dedup["tg_ids"])
                or (uname_low and uname_low in global_dedup["usernames"])
                or (phone_key and phone_key in global_dedup["phones"])
                or (is_b2b_camp and domain_key and domain_key in global_dedup["domains"])
                or (is_b2b_camp and person_key and comp_key and (person_key, comp_key) in global_dedup["person_company"])
            )
            if is_global_dup and existing is None:
                matched_lid = (
                    (global_dedup["lead_id_by_tg_id"].get(real_tg_id) if real_tg_id is not None else None)
                    or (global_dedup["lead_id_by_username"].get(uname_low) if uname_low else None)
                    or (global_dedup["lead_id_by_phone"].get(phone_key) if phone_key else None)
                    or (global_dedup["lead_id_by_domain"].get(domain_key) if (is_b2b_camp and domain_key) else None)
                )
                if matched_lid is not None:
                    matched_row = conn.execute("SELECT * FROM leads WHERE id = ?", (matched_lid,)).fetchone()
                    if matched_row is not None:
                        existing = as_dict(matched_row)
            if is_global_dup and existing is None:
                skipped_duplicates += 1
                _record_skip(item_idx, "global_duplicate")
                continue

        # Prepare queue draft for social_manual / vacancy_assisted when needed
        resolved_manual_draft: str | None = None
        if raw_manual_draft:
            resolved_manual_draft = sanitize_copy_text(raw_manual_draft)
        elif is_queue_only_segment:
            queue_profile = resolve_copy_profile(
                None,
                segment=effective_segment,
                language=effective_language,
                source=lead_source,
            )
            queue_seq = build_outreach_sequence(
                {
                    "name": display_name,
                    "company": company,
                    "website": website,
                    "url": url_val,
                    "category": category,
                    "niche": category,
                    "their_angle": their_angle,
                    "our_angle": our_angle,
                    "note": raw_note,
                    "source": lead_source,
                    "lead_source": lead_source,
                    "pool_usd": pool_usd,
                    "role": item.get("role"),
                    "title": item.get("title"),
                    "manual_action_url": manual_action_url,
                },
                copy_profile=queue_profile,
                segment=effective_segment,
                language=effective_language,
                provider=provider,
                showcase_url=showcase_url,
            )
            resolved_manual_draft = queue_seq.step1_text

        lead_acc_id = (
            (active_state or {}).get("account_id")
            if active_state is not None
            else account_id
        )

        if existing is not None:
            lid = int(existing["id"])
            upd_fields: dict[str, Any] = {}
            if company and not existing.get("company"):
                upd_fields["company"] = company
            if company and not existing.get("description"):
                upd_fields["description"] = company
            if category and not existing.get("niche"):
                upd_fields["niche"] = category
            if url_val and not existing.get("url"):
                upd_fields["url"] = url_val
            if lead_source and not existing.get("lead_source"):
                upd_fields["lead_source"] = lead_source
            if email and not existing.get("email"):
                upd_fields["email"] = email
            if phone and not existing.get("phone"):
                upd_fields["phone"] = phone
            if username and not existing.get("username"):
                upd_fields["username"] = username
            if (
                provider == "telegram"
                and str(existing.get("provider") or "").strip().lower() != "telegram"
                and not existing.get("dialog_id")
                and (real_tg_id is not None or bool(item.get("tg_phone_direct")) or bool(username))
            ):
                upd_fields["provider"] = "telegram"
            if our_angle and not existing.get("offer_details"):
                upd_fields["offer_details"] = our_angle

            old_note = (existing.get("note") or "").strip()
            if combined_note:
                if not old_note:
                    upd_fields["note"] = combined_note
                else:
                    additions = [
                        part for part in note_parts
                        if part and part not in old_note
                    ]
                    if additions:
                        upd_fields["note"] = old_note + "\n" + "\n".join(additions)

            if upd_fields:
                update_lead(conn, lid, **upd_fields)
            existing_tg_id = existing.get("telegram_user_id")
            is_existing_synthetic = (
                existing_tg_id is not None and 900000 <= int(existing_tg_id) <= 999999
            )
            if (existing_tg_id is None and has_direct_messenger_handle) or (
                real_tg_id is not None and is_existing_synthetic
            ):
                existing_tg_id = real_tg_id if real_tg_id is not None else (900000 + lid)
                try:
                    conn.execute(
                        "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                        (existing_tg_id, now, lid),
                    )
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    logger.debug("Skipping duplicate synthetic telegram_user_id update for lead %s: %s", lid, exc)
            tg_id = int(existing_tg_id) if existing_tg_id is not None else None
            v2_updated = _persist_lead_v2_fields(
                conn,
                lid,
                segment=effective_segment,
                language=effective_language,
                exclude_from_airtable=exclude_from_airtable_val,
                company_domain=company_domain,
                pool_usd=pool_usd,
                secondary_contact_json=secondary_contact_json,
                manual_action_url=manual_action_url,
                manual_draft_text=resolved_manual_draft,
                now=now,
                is_new=False,
                existing=existing,
                explicit_keys=explicit_v2_keys,
            )
            had_lead_changes = bool(upd_fields) or v2_updated
            if not is_global_dup or had_lead_changes or (uname_low and uname_low == (_clean_handle(existing.get("username")) or "").lower()):
                updated_leads += 1
            lead_id_by_index[item_idx] = lid
            lead_results[item_idx] = {
                "index": item_idx,
                "lead_id": lid,
                "status": "updated" if (had_lead_changes or not is_global_dup) else "skipped",
                "reason": (
                    "manual_queue_only"
                    if is_queue_only_segment
                    else ("global_duplicate" if is_global_dup else ("updated" if had_lead_changes else "unchanged"))
                ),
            }
        else:
            next_step_text = (
                "Ручное действие в очереди"
                if is_queue_only_segment
                else ("Отправить цепочку рассылки" if (enroll_in_campaign and has_direct_messenger_handle) else None)
            )
            created = create_lead(
                conn,
                account_id=lead_acc_id,
                telegram_user_id=real_tg_id,
                username=username,
                name=display_name,
                phone=phone,
                company=company,
                description=company,
                niche=category,
                url=url_val,
                lead_source=lead_source,
                email=email,
                note=combined_note,
                offer_details=our_angle,
                provider=provider,
                next_step=next_step_text,
            )
            lid = int(created["id"])
            if real_tg_id is None and has_direct_messenger_handle:
                tg_id = created.get("telegram_user_id") or (900000 + lid)
                try:
                    conn.execute(
                        "UPDATE leads SET telegram_user_id = ? WHERE id = ? AND telegram_user_id IS NULL",
                        (int(tg_id), lid),
                    )
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    logger.debug("Skipping duplicate synthetic telegram_user_id insert for lead %s: %s", lid, exc)
                tg_id = int(tg_id)
            else:
                tg_id = real_tg_id
            _persist_lead_v2_fields(
                conn,
                lid,
                segment=effective_segment,
                language=effective_language,
                exclude_from_airtable=exclude_from_airtable_val,
                company_domain=company_domain,
                pool_usd=pool_usd,
                secondary_contact_json=secondary_contact_json,
                manual_action_url=manual_action_url,
                manual_draft_text=resolved_manual_draft,
                now=now,
                is_new=True,
                existing=created,
                explicit_keys=explicit_v2_keys,
            )
            created_leads += 1
            if person_key and comp_key:
                global_dedup["leads_by_person_company"].setdefault((person_key, comp_key), created)
            if domain_key:
                global_dedup["leads_by_domain"].setdefault(domain_key, created)
                global_dedup["lead_id_by_domain"].setdefault(domain_key, lid)
            if uname_low:
                global_dedup["lead_id_by_username"].setdefault(uname_low, lid)
            if phone_key:
                global_dedup["lead_id_by_phone"].setdefault(phone_key, lid)
            if real_tg_id is not None:
                global_dedup["lead_id_by_tg_id"].setdefault(real_tg_id, lid)
            lead_id_by_index[item_idx] = lid
            lead_results[item_idx] = {
                "index": item_idx,
                "lead_id": lid,
                "status": "created",
                "reason": "manual_queue_only" if is_queue_only_segment else ("lead_only" if not enroll_in_campaign else "created"),
            }

        lead_ids.append(lid)

        # Keep social_manual and vacancy_assisted strictly OUT of campaign_recipients!
        if enroll_in_campaign and not is_queue_only_segment and has_direct_messenger_handle:
            if active_state is None:
                cache_key = target_camp_name.strip().lower()
                if cache_key not in camp_cache:
                    camp_cache[cache_key] = _get_or_create_camp_state(
                        target_campaign_id if explicit_cid_requested else None,
                        target_camp_name,
                        item_segment=effective_segment,
                        item_lang=effective_language,
                    )
                active_state = camp_cache[cache_key]

            target_camp_dict = active_state["camp_dict"]
            target_campaign_id = int(active_state["campaign_id"])
            target_camp_name = str(target_camp_dict.get("name") or target_camp_name)
            if primary_resolved_cid is None:
                primary_resolved_cid = target_campaign_id

            # Ensure lead inherits campaign exclude_from_airtable = 1 if target campaign excludes Airtable
            final_camp_exclude = int(target_camp_dict.get("exclude_from_airtable") or 0)
            if final_camp_exclude == 1 and exclude_from_airtable_val == 0:
                if _persist_lead_v2_fields(
                    conn,
                    lid,
                    segment=None,
                    language=None,
                    exclude_from_airtable=1,
                    company_domain=None,
                    pool_usd=None,
                    secondary_contact_json=None,
                    manual_action_url=None,
                    manual_draft_text=None,
                    now=now,
                    is_new=False,
                    existing={"exclude_from_airtable": 0},
                ):
                    if lead_results[item_idx]["status"] == "skipped":
                        lead_results[item_idx]["status"] = "updated"

            seen_leads = active_state["seen_leads"]
            seen_tg_ids = active_state["seen_tg_ids"]
            seen_usernames = active_state["seen_usernames"]
            seen_phones = active_state["seen_phones"]
            is_dup = (
                is_global_dup
                or lid in seen_leads
                or (tg_id and int(tg_id) in seen_tg_ids)
                or (username and username.lower() in seen_usernames)
                or (phone and phone in seen_phones)
            )
            if is_dup:
                skipped_duplicates += 1
                lead_results[item_idx]["reason"] = (
                    "already_in_campaign" if lid in seen_leads else "duplicate_recipient"
                )
                continue

            camp_profile_raw = str(target_camp_dict.get("copy_profile") or "").strip()
            target_camp_lang = str(target_camp_dict.get("language") or inferred_camp_v2.get("language") or "").strip().lower()
            is_wa_target_camp = (
                "(wa)" in target_camp_name.lower()
                or "whatsapp" in target_camp_name.lower()
                or " — wa" in target_camp_name.lower()
            )
            if (
                effective_segment == "b2b"
                and camp_profile_raw in ("", "default", "b2b_legacy")
                and not is_wa_target_camp
                and target_camp_name.strip().lower() != CAMPAIGN_WHALES_EN_TG.lower()
            ):
                resolved_profile = "b2b_legacy"
            else:
                resolved_profile = resolve_copy_profile(
                    camp_profile_raw if camp_profile_raw not in ("", "default") else None,
                    segment=effective_segment,
                    language=target_camp_lang or effective_language,
                    source=lead_source,
                    default_profile="whale_mass" if is_wa_target_camp else "b2b_legacy",
                )

            raw_step2 = item.get("step2_text") if item.get("step2_text") is not None else item.get("followup_1")
            raw_step3 = item.get("step3_text") if item.get("step3_text") is not None else item.get("followup_2")
            explicit_audience = (str(item.get("audience") or item.get("target_audience") or "").strip())

            if resolved_profile in ("b2b_legacy", "ofm"):
                step1_text, error_reason = _clean_step1_text(
                    raw_s1,
                    company,
                    is_b2b=(is_b2b_camp or is_ofm_target_camp),
                )
                step2_text = (sanitize_copy_text(str(raw_step2 or "")).strip()) or None
                step3_text = (sanitize_copy_text(str(raw_step3 or "")).strip()) or None
            else:
                custom_tpls: dict[str, Any] | None = None
                raw_tpl_json = target_camp_dict.get("custom_step_templates_json")
                if raw_tpl_json:
                    try:
                        parsed_tpl = json.loads(str(raw_tpl_json))
                        if isinstance(parsed_tpl, dict):
                            custom_tpls = parsed_tpl
                    except (TypeError, ValueError) as exc:
                        logger.debug("Invalid custom_step_templates_json on campaign %s: %s", target_campaign_id, exc)

                expected_copy_lang: str = (
                    "ru"
                    if resolved_profile in ("vacancy_ru", "ru_expert")
                    else "en"
                    if resolved_profile in ("whale_mass", "vacancy_en")
                    else (target_camp_lang if target_camp_lang in ("ru", "en") else effective_language)
                )
                seq = build_outreach_sequence(
                    {
                        "name": display_name,
                        "company": company,
                        "website": website,
                        "url": url_val,
                        "category": category,
                        "niche": category,
                        "their_angle": their_angle,
                        "our_angle": our_angle,
                        "note": raw_note,
                        "source": lead_source,
                        "lead_source": lead_source,
                        "pool_usd": pool_usd,
                        "role": item.get("role"),
                        "title": item.get("title"),
                        "audience": explicit_audience,
                        "manual_action_url": manual_action_url,
                    },
                    copy_profile=resolved_profile,
                    segment=effective_segment,
                    language=expected_copy_lang,
                    provider="whatsapp" if is_wa_target_camp else provider,
                    custom_step_templates=custom_tpls,
                    showcase_url=showcase_url,
                )
                raw_s1_str = str(raw_s1 or "").strip()
                whale_legacy_offer_re = re.compile(r"\b(?:rev[\s\-]?share|cpa|creatoros)\b", re.I)
                whale_revshare_leak = (
                    resolved_profile == "whale_mass"
                    and bool(raw_s1_str)
                    and bool(whale_legacy_offer_re.search(raw_s1_str))
                )
                if raw_s1 is not None and raw_s1_str and not whale_revshare_leak:
                    step1_text, error_reason = _clean_step1_text(raw_s1, company, is_b2b=False)
                    if not error_reason and step1_text and not is_qa_camp and expected_copy_lang in ("ru", "en"):
                        lang_ok, lang_reason = check_copy_language(step1_text, expected_copy_lang)  # type: ignore[arg-type]
                        if not lang_ok:
                            error_reason = f"language_mismatch_{expected_copy_lang}: {lang_reason}"
                else:
                    step1_text = seq.step1_text
                    error_reason = None
                    if step1_text and not is_qa_camp and expected_copy_lang in ("ru", "en"):
                        lang_ok, lang_reason = check_copy_language(step1_text, expected_copy_lang)  # type: ignore[arg-type]
                        if not lang_ok:
                            error_reason = f"language_mismatch_{expected_copy_lang}: {lang_reason}"

                step2_text = (sanitize_copy_text(str(raw_step2 or "")).strip()) or (seq.step2_text or None)
                if raw_step3 is not None and str(raw_step3).strip():
                    step3_text = sanitize_copy_text(str(raw_step3)).strip() or None
                else:
                    step3_text = seq.step3_text or None
                if not explicit_audience and seq.audience:
                    explicit_audience = seq.audience

            rec_status = "failed" if error_reason else "pending"
            rec_note = raw_note or their_angle
            mcp_extra_json = (
                json.dumps({"audience": explicit_audience}, ensure_ascii=False)
                if explicit_audience
                else None
            )

            rec_created_at = now
            raw_posted = str(item.get("posted_at") or "").strip()
            if raw_posted:
                from datetime import datetime as _dt

                try:
                    p_norm = raw_posted[:-1] + "+00:00" if raw_posted.endswith(("Z", "z")) else raw_posted
                    _dt.fromisoformat(p_norm)
                    rec_created_at = raw_posted
                except ValueError as exc:
                    logger.debug("Invalid posted_at timestamp %r: %s", raw_posted, exc)

            conn.execute(
                """
                INSERT INTO campaign_recipients (
                    campaign_id, lead_id, telegram_user_id, username, name,
                    phone, company, note, website, category, their_angle,
                    our_angle, hook, step1_text, step2_text, step3_text,
                    extra_json, current_step, status, last_sent_at, error_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?)
                """,
                (
                    target_campaign_id,
                    lid,
                    tg_id,
                    username,
                    display_name or username or phone or f"Лид #{lid}",
                    phone,
                    company,
                    rec_note,
                    website,
                    category,
                    their_angle,
                    our_angle,
                    hook,
                    step1_text,
                    step2_text,
                    step3_text,
                    mcp_extra_json,
                    rec_status,
                    error_reason,
                    rec_created_at,
                    now,
                ),
            )
            seen_leads.add(lid)
            global_dedup["leads"].add(lid)
            if tg_id:
                seen_tg_ids.add(int(tg_id))
                if not (900000 <= int(tg_id) <= 999999):
                    global_dedup["tg_ids"].add(int(tg_id))
            if username:
                seen_usernames.add(username.lower())
                global_dedup["usernames"].add(username.lower())
            if phone:
                seen_phones.add(phone)
                if phone_key:
                    global_dedup["phones"].add(phone_key)
            if domain_key:
                global_dedup["domains"].add(domain_key)
            if person_key and comp_key:
                global_dedup["person_company"].add((person_key, comp_key))
            if rec_status == "pending":
                active_state["added_pending"] = int(active_state.get("added_pending", 0)) + 1
            enrolled_in_campaign += 1

    if enroll_in_campaign:
        for st in camp_cache.values():
            cid_u = int(st["campaign_id"])
            st_seg = str(st["camp_dict"].get("segment") or "b2b").strip().lower()
            if (
                int(st.get("added_pending", 0)) > 0
                and str(st["camp_dict"].get("status") or "").lower() == "completed"
                and not is_vacancy_campaign(st["camp_dict"])
                and st_seg != "ru_expert"
            ):
                conn.execute(
                    "UPDATE campaigns SET status = 'running', updated_at = ? WHERE id = ? AND status = 'completed'",
                    (now, cid_u),
                )
            else:
                conn.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (now, cid_u))
    conn.commit()

    return {
        "ok": True,
        "processed": processed,
        "created_leads": created_leads,
        "updated_leads": updated_leads,
        "enrolled_in_campaign": enrolled_in_campaign,
        "skipped_duplicates": skipped_duplicates,
        "skipped_non_icp": skipped_non_icp,
        "quarantined": quarantined,
        "saved_direct_x_leads": saved_direct_x_leads,
        "campaign_id": primary_resolved_cid,
        "lead_ids": lead_ids,
        "lead_id_by_index": lead_id_by_index,
        "lead_results": lead_results,
    }


def _read_mcp_resource(conn: sqlite3.Connection, uri: str) -> dict:
    clean_uri = str(uri or "").strip()
    if clean_uri == "crm://analytics/summary":
        data = call_mcp_tool_handler(conn, "get_analytics_summary", {"period": "30d"})
    elif clean_uri == "crm://focus/today":
        data = call_mcp_tool_handler(conn, "get_focus_today", {})
    elif clean_uri == "crm://pipeline/stages":
        data = call_mcp_tool_handler(conn, "get_pipeline_stages", {})
    else:
        raise LookupError(f"Ресурс не найден: {clean_uri}")
    return {
        "contents": [
            {
                "uri": clean_uri,
                "mimeType": "application/json",
                "text": json.dumps(data, ensure_ascii=False, indent=2),
            }
        ]
    }


def handle_jsonrpc_message(conn: sqlite3.Connection, payload: dict | list) -> dict | list | None:
    if isinstance(payload, list):
        responses = []
        for item in payload:
            resp = handle_jsonrpc_message(conn, item)
            if resp is not None:
                responses.append(resp)
        return responses if responses else None

    if not isinstance(payload, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    req_id = payload.get("id")
    method = str(payload.get("method") or "").strip()
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

    # Notifications have no id and expect no response
    if req_id is None and method.startswith("notifications/"):
        return None

    if method == "initialize":
        client_version = params.get("protocolVersion") or MCP_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": client_version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "notifications/initialized":
        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS_SCHEMA},
        }

    if method == "tools/call":
        tool_name = str(params.get("name") or "").strip()
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not tool_name:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Не указано имя инструмента (name)"},
            }
        try:
            result_data = call_mcp_tool_handler(conn, tool_name, arguments)
            formatted = json.dumps(result_data, ensure_ascii=False, indent=2)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": formatted}],
                    "structuredContent": result_data,
                    "isError": False,
                },
            }
        except KeyError as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": str(exc)},
            }
        except (ValueError, LookupError) as exc:
            err_payload = {"error": str(exc), "tool": tool_name}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(err_payload, ensure_ascii=False, indent=2),
                        }
                    ],
                    "structuredContent": err_payload,
                    "isError": True,
                },
            }
        except Exception as exc:
            err_payload = {"error": f"Ошибка выполнения {tool_name}: {exc}", "tool": tool_name}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(err_payload, ensure_ascii=False, indent=2),
                        }
                    ],
                    "structuredContent": err_payload,
                    "isError": True,
                },
            }

    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resources": RESOURCES_SCHEMA},
        }

    if method == "resources/read":
        uri = str(params.get("uri") or "").strip()
        try:
            res = _read_mcp_resource(conn, uri)
            return {"jsonrpc": "2.0", "id": req_id, "result": res}
        except LookupError as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": str(exc)},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _verify_mcp_auth(request: Request) -> None:
    app_state = getattr(getattr(request, "app", None), "state", None)
    db_conn = getattr(app_state, "conn", None) if app_state is not None else None
    crm_user = (
        resolve_request_crm_user(db_conn, request) if db_conn is not None else None
    )
    if crm_user is not None and crm_user.get("role") == "admin":
        return

    enforced = is_auth_enforced()
    expected_key = os.getenv("MCP_API_KEY", "").strip()
    if not expected_key:
        if enforced:
            if crm_user is not None:
                raise HTTPException(
                    status_code=403,
                    detail="Forbidden: admin role required",
                )
            raise HTTPException(
                status_code=401,
                detail="Unauthorized: MCP_API_KEY is required in production",
            )
        return

    auth_header = (request.headers.get("authorization") or "").strip()
    bearer_token = ""
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header[7:].strip()
    header_key = (
        request.headers.get("x-mcp-api-key")
        or request.headers.get("x-mcp-key")
        or ""
    ).strip()
    query_key = "" if enforced else (request.query_params.get("api_key") or "").strip()

    provided = bearer_token or header_key or query_key
    if provided and hmac.compare_digest(
        provided.encode("utf-8"), expected_key.encode("utf-8")
    ):
        return

    if crm_user is not None:
        raise HTTPException(
            status_code=403,
            detail="Forbidden: admin role required",
        )
    raise HTTPException(
        status_code=401,
        detail="Неверный или отсутствующий MCP_API_KEY",
    )


def get_mcp_server_info(base_url: str = "http://127.0.0.1:8000") -> dict:
    clean_base = str(base_url or "http://127.0.0.1:8000").rstrip("/")
    venv_python = ROOT / ".venv" / "bin" / "python"
    python_cmd = str(venv_python) if venv_python.exists() else sys.executable
    has_key = bool(os.getenv("MCP_API_KEY", "").strip()) or is_auth_enforced()

    stdio_config = {
        "mcpServers": {
            "foldesk-crm": {
                "command": python_cmd,
                "args": ["-m", "app.mcp_server"],
                "cwd": str(ROOT),
            }
        }
    }
    http_entry: dict[str, Any] = {
        "url": f"{clean_base}/mcp",
        "transport": "streamable-http",
    }
    if has_key:
        http_entry["headers"] = {"Authorization": "Bearer <MCP_API_KEY>"}
    else:
        http_entry["headers"] = {"Authorization": "Bearer <опционально: MCP_API_KEY>"}

    http_config = {
        "mcpServers": {
            "foldesk-crm-remote": http_entry,
        }
    }

    return {
        "status": "ready",
        "server": SERVER_INFO,
        "protocol_version": MCP_PROTOCOL_VERSION,
        "auth_required": has_key,
        "safety_policy": "Чтение всей CRM + управление сделками, задачами, календарём и Airtable (без отправки сообщений в мессенджеры).",
        "endpoints": {
            "http": f"{clean_base}/mcp",
            "sse": f"{clean_base}/mcp/sse",
            "info": f"{clean_base}/api/mcp/info",
        },
        "tools_count": len(TOOLS_SCHEMA),
        "tools": [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": list((t.get("inputSchema", {}).get("properties") or {}).keys()),
            }
            for t in TOOLS_SCHEMA
        ],
        "configs": {
            "stdio": stdio_config,
            "http": http_config,
        },
    }


def register_mcp_routes(app: FastAPI) -> None:
    @app.get("/api/mcp/info")
    def mcp_info(request: Request):
        if is_auth_enforced():
            _verify_mcp_auth(request)
        base_url = str(request.base_url).rstrip("/")
        return get_mcp_server_info(base_url=base_url)

    @app.post("/mcp")
    async def mcp_http_endpoint(request: Request):
        def _auth_sync() -> None:
            app_state = getattr(getattr(request, "app", None), "state", None)
            db_c = getattr(app_state, "conn", None) if app_state is not None else None
            with locked_conn(db_c):
                _verify_mcp_auth(request)

        await asyncio.to_thread(_auth_sync)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                },
            )

        def _exec_rpc_sync() -> dict | list | None:
            app_state = getattr(getattr(request, "app", None), "state", None)
            shared_conn = getattr(app_state, "conn", None) if app_state is not None else None
            owns_conn = shared_conn is None
            local_conn = connect() if owns_conn else shared_conn
            try:
                if owns_conn:
                    init_db(local_conn)
                with locked_conn(local_conn):
                    return handle_jsonrpc_message(local_conn, payload)
            finally:
                if owns_conn and local_conn is not None:
                    local_conn.close()

        response_data = await asyncio.to_thread(_exec_rpc_sync)
        if response_data is None:
            return Response(status_code=202)
        return JSONResponse(content=response_data)

    @app.get("/mcp/sse")
    async def mcp_sse_endpoint(request: Request):
        _verify_mcp_auth(request)
        api_key = (
            ""
            if is_auth_enforced()
            else (request.query_params.get("api_key") or "").strip()
        )
        post_endpoint = f"/mcp?api_key={api_key}" if api_key else "/mcp"

        async def sse_stream():
            yield f"event: endpoint\ndata: {post_endpoint}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    await asyncio.sleep(15)
                except asyncio.CancelledError:
                    break
                yield ": ping\n\n"

        return StreamingResponse(
            sse_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def run_stdio_server(db_path: str | Path | None = None) -> None:
    conn = connect(db_path)
    init_db(conn)
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            err = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            sys.stdout.write(json.dumps(err, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        resp = handle_jsonrpc_message(conn, payload)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
