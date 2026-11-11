import asyncio
import inspect
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.analytics import get_analytics_summary
from app.db import (
    ROOT,
    as_dict,
    connect,
    ensure_airtable_pipeline_stages,
    init_db,
    list_pipeline_stages,
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
    _parse_account_ids,
    create_campaign,
    is_offline_non_icp_contact,
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

    raise KeyError(f"Неизвестный MCP-инструмент: {name}")


def _is_vacancy_lead_item(item: dict) -> bool:
    src_low = str(item.get("lead_source") or item.get("source") or "").lower()
    cat_low = str(item.get("category") or item.get("niche") or "").lower()
    return any(k in src_low for k in ("ваканс", "vacanc", "job_monitor")) or any(
        k in cat_low for k in ("ваканс", "vacanc")
    )


def _auto_enrich_b2b_items_with_sonnet(items: list[dict]) -> list[dict]:
    """Ensure every B2B item without canonical Vlad SoftUGC+revshare copy is drafted via claude-sonnet-5 when API key is configured."""
    from leadgen.outreach_copy import (
        REVSHARE_OFFER_TEXT,
        VLAD_STEP3_TEXT,
        followup_one,
        generate_vlad_sonnet_batch,
    )

    api_key = (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    if not api_key:
        return items
    if "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("FORCE_AUTO_SONNET_IN_TEST") != "1":
        return items

    out_items = [dict(it) if isinstance(it, dict) else it for it in items]
    need_indices: list[int] = []
    need_payloads: list[dict] = []
    for idx, it in enumerate(out_items):
        if not isinstance(it, dict) or _is_vacancy_lead_item(it):
            continue
        raw_s1 = str(it.get("step1_text") or it.get("first_touch") or "").strip()
        raw_low = raw_s1.lower()
        already_canonical = (
            bool(raw_s1)
            and "SoftUGC" in raw_s1
            and REVSHARE_OFFER_TEXT in raw_s1
            and "creatoros" not in raw_low
            and "if your user growth has hit a ceiling" not in raw_low
            and "plug-and-play growth arm" not in raw_low
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
        except Exception:
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
            target["followup_2"] = VLAD_STEP3_TEXT
            target["step3_text"] = VLAD_STEP3_TEXT
    return out_items


def _handle_ingest_discovered_leads(conn: sqlite3.Connection, args: dict) -> dict:
    raw_leads = args.get("leads") if args.get("leads") is not None else args.get("items")
    if not isinstance(raw_leads, list):
        raise ValueError("Параметр leads должен быть списком объектов")

    enroll_in_campaign = bool(args.get("enroll_in_campaign", True))
    raw_cid = args.get("campaign_id")
    campaign_id: int | None = int(raw_cid) if raw_cid is not None else None
    raw_camp_arg = str(args.get("campaign_name") or "").strip()
    fallback_camp_name = raw_camp_arg or "Leadgen Autopilot"

    dict_items = [it for it in raw_leads if isinstance(it, dict)]
    all_vacancies = bool(dict_items) and all(_is_vacancy_lead_item(it) for it in dict_items)

    if campaign_id is None:
        raw_low = raw_camp_arg.lower()
        if any(k in raw_low for k in ("ваканс", "vacanc", "job")) or "(wa)" in raw_low or "whatsapp" in raw_low:
            campaign_name = raw_camp_arg
        elif all_vacancies and raw_low in ("", "leadgen auto (tg)", "leadgen autopilot"):
            campaign_name = "TG Вакансии"
        else:
            from app.leadgen_settings import resolve_target_b2b_tg_campaign_name

            campaign_name = resolve_target_b2b_tg_campaign_name(
                conn,
                raw_camp_arg,
                fallback_default=fallback_camp_name,
            )
    else:
        campaign_name = fallback_camp_name

    def _get_or_create_camp_state(target_cid: int | None, target_cname: str) -> dict[str, Any]:
        if target_cid is not None:
            crow = conn.execute("SELECT * FROM campaigns WHERE id = ?", (target_cid,)).fetchone()
            if crow is None:
                raise LookupError(f"Кампания #{target_cid} не найдена")
            c_dict = as_dict(crow)
            resolved_cid = int(c_dict["id"])
        else:
            all_c = [as_dict(r) for r in conn.execute("SELECT * FROM campaigns ORDER BY id ASC").fetchall()]
            matched = next(
                (c for c in all_c if str(c.get("name") or "").strip().lower() == target_cname.strip().lower()),
                None,
            )
            if matched is not None:
                c_dict = matched
                resolved_cid = int(c_dict["id"])
            else:
                from app.db import CANONICAL_STEP3_IMAGE_URL
                from leadgen.outreach_copy import VLAD_STEP2_TEMPLATE, VLAD_STEP3_TEXT

                is_vacancy_camp = any(k in target_cname.lower() for k in ("ваканс", "vacanc", "job"))
                is_wa_camp = "(wa)" in target_cname.lower() or "whatsapp" in target_cname.lower()
                default_steps = (
                    [
                        {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
                        {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
                        {"step_order": 3, "delay_hours": 72, "template_text": "{followup_2}"},
                    ]
                    if is_vacancy_camp
                    else [
                        {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
                        {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
                        {
                            "step_order": 3,
                            "delay_hours": 24,
                            "template_text": VLAD_STEP3_TEXT,
                            **({} if is_wa_camp else {"image_url": CANONICAL_STEP3_IMAGE_URL}),
                        },
                    ]
                )
                created_camp = create_campaign(
                    conn,
                    name=target_cname,
                    status="draft",
                    steps=default_steps,
                )
                resolved_cid = int(created_camp["id"])
                c_dict = created_camp

        acc_ids = _parse_account_ids(c_dict.get("account_id"), c_dict.get("account_ids_json"))
        acc_id = acc_ids[0] if acc_ids else c_dict.get("account_id")
        existing_rows = conn.execute(
            "SELECT username, phone, lead_id, telegram_user_id FROM campaign_recipients WHERE campaign_id = ?",
            (resolved_cid,),
        ).fetchall()
        return {
            "campaign_id": resolved_cid,
            "camp_dict": c_dict,
            "account_id": acc_id,
            "seen_usernames": {str(r["username"]).lower() for r in existing_rows if r["username"]},
            "seen_phones": {str(r["phone"]) for r in existing_rows if r["phone"]},
            "seen_leads": {int(r["lead_id"]) for r in existing_rows if r["lead_id"]},
            "seen_tg_ids": {int(r["telegram_user_id"]) for r in existing_rows if r["telegram_user_id"]},
        }

    camp_cache: dict[str, dict[str, Any]] = {}
    primary_state: dict[str, Any] | None = None
    camp_dict: dict[str, Any] | None = None
    account_id: int | None = None
    explicit_cid_requested = campaign_id is not None

    if enroll_in_campaign:
        primary_state = _get_or_create_camp_state(campaign_id, campaign_name)
        campaign_id = int(primary_state["campaign_id"])
        camp_dict = primary_state["camp_dict"]
        account_id = primary_state["account_id"]
        camp_cache[str(camp_dict.get("name") or "").strip().lower()] = primary_state

    is_primary_vacancy = bool(
        camp_dict is not None
        and any(k in str(camp_dict.get("name") or "").lower() for k in ("ваканс", "vacanc", "job"))
    )
    if not is_primary_vacancy:
        raw_leads = _auto_enrich_b2b_items_with_sonnet(raw_leads)

    processed = 0
    created_leads = 0
    updated_leads = 0
    enrolled_in_campaign = 0
    skipped_duplicates = 0
    lead_ids: list[int] = []
    now = utc_now()

    for item in raw_leads:
        if not isinstance(item, dict):
            continue
        username = _clean_handle(item.get("username"))
        company = _clean_company_title(item.get("company"))
        explicit_name = (str(item.get("name") or "").strip()) or None
        phone = (str(item.get("phone") or "").strip()) or None
        email = (str(item.get("email") or "").strip()) or None
        website_raw = (str(item.get("website") or item.get("url") or "").strip())
        website = website_raw or None
        url_val = None
        if website_raw:
            url_val = website_raw if re.match(r"^https?://", website_raw, flags=re.IGNORECASE) else f"https://{website_raw}"

        category = (str(item.get("category") or item.get("niche") or "").strip()) or None
        their_angle = (str(item.get("their_angle") or "").strip()) or None
        our_angle = (str(item.get("our_angle") or "").strip()) or None
        hook = (str(item.get("hook") or "").strip()) or None
        lead_source = (str(item.get("lead_source") or "").strip()) or None
        raw_note = (str(item.get("note") or "").strip()) or None
        provider = (str(item.get("provider") or "telegram").strip().lower())
        if provider not in ("telegram", "whatsapp"):
            provider = "telegram"

        tg_id_raw = item.get("telegram_user_id")
        tg_id: int | None = None
        if tg_id_raw is not None and str(tg_id_raw).strip():
            try:
                tg_id = int(str(tg_id_raw).strip())
            except ValueError:
                tg_id = None
        has_direct_messenger_handle = bool(
            username or phone or (tg_id is not None and not (900000 <= tg_id <= 999999))
        )

        display_name = explicit_name or company or (f"{username} · {company}" if username and company else username) or phone
        if not any((display_name, username, phone, tg_id)):
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
        if tg_id is not None:
            row = conn.execute(
                "SELECT * FROM leads WHERE telegram_user_id = ? ORDER BY id ASC LIMIT 1",
                (tg_id,),
            ).fetchone()
            if row is not None:
                existing = as_dict(row)
        if existing is None and (username or phone):
            existing = _find_lead_by_lookup(conn, username=username, phone=phone)
        if existing is None and company:
            row = conn.execute(
                "SELECT * FROM leads WHERE company IS NOT NULL AND lower(company) = lower(?) ORDER BY id ASC LIMIT 1",
                (company,),
            ).fetchone()
            if row is not None:
                existing = as_dict(row)

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
            if existing_tg_id is None:
                existing_tg_id = tg_id if tg_id is not None else (900000 + lid)
                try:
                    conn.execute(
                        "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                        (existing_tg_id, now, lid),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    pass
            tg_id = int(existing_tg_id)
            updated_leads += 1
        else:
            created = create_lead(
                conn,
                account_id=account_id,
                telegram_user_id=tg_id,
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
                next_step="Отправить цепочку рассылки" if (enroll_in_campaign and has_direct_messenger_handle) else None,
            )
            lid = int(created["id"])
            if tg_id is None:
                tg_id = created.get("telegram_user_id") or (900000 + lid)
                try:
                    conn.execute(
                        "UPDATE leads SET telegram_user_id = ? WHERE id = ? AND telegram_user_id IS NULL",
                        (int(tg_id), lid),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    pass
            tg_id = int(tg_id)
            created_leads += 1

        lead_ids.append(lid)

        active_state = primary_state
        if (
            enroll_in_campaign
            and not explicit_cid_requested
            and _is_vacancy_lead_item(item)
            and not is_primary_vacancy
        ):
            vac_key = "tg вакансии"
            if vac_key not in camp_cache:
                camp_cache[vac_key] = _get_or_create_camp_state(None, "TG Вакансии")
            active_state = camp_cache[vac_key]

        target_camp_dict = active_state["camp_dict"] if active_state is not None else camp_dict
        target_campaign_id = int(active_state["campaign_id"]) if active_state is not None else campaign_id
        is_b2b_camp = bool(
            target_camp_dict is not None
            and not any(k in str(target_camp_dict.get("name") or "").lower() for k in ("ваканс", "vacanc", "job"))
        )
        if is_b2b_camp and is_offline_non_icp_contact(
            username=username,
            name=display_name,
            company=company,
            website=url_val,
            their_angle=f"{their_angle or ''} {item.get('step1_text') or item.get('first_touch') or ''}",
        ):
            continue

        if enroll_in_campaign and active_state is not None and target_campaign_id is not None and has_direct_messenger_handle:
            seen_leads = active_state["seen_leads"]
            seen_tg_ids = active_state["seen_tg_ids"]
            seen_usernames = active_state["seen_usernames"]
            seen_phones = active_state["seen_phones"]
            is_dup = (
                lid in seen_leads
                or (tg_id and int(tg_id) in seen_tg_ids)
                or (username and username.lower() in seen_usernames)
                or (phone and phone in seen_phones)
            )
            if is_dup:
                skipped_duplicates += 1
                continue

            step1_text, error_reason = _clean_step1_text(
                item.get("step1_text") or item.get("first_touch"),
                item.get("company"),
                is_b2b=is_b2b_camp,
            )
            step2_text = (str(item.get("step2_text") or item.get("followup_1") or "").strip()) or None
            step3_text = (str(item.get("step3_text") or item.get("followup_2") or "").strip()) or None
            rec_status = "failed" if error_reason else "pending"
            rec_note = raw_note or their_angle
            explicit_audience = (str(item.get("audience") or item.get("target_audience") or "").strip())
            mcp_extra_json = (
                json.dumps({"audience": explicit_audience}, ensure_ascii=False)
                if explicit_audience
                else None
            )

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
                    now,
                    now,
                ),
            )
            seen_leads.add(lid)
            if tg_id:
                seen_tg_ids.add(int(tg_id))
            if username:
                seen_usernames.add(username.lower())
            if phone:
                seen_phones.add(phone)
            enrolled_in_campaign += 1

    if enroll_in_campaign:
        for st in camp_cache.values():
            conn.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (now, int(st["campaign_id"])))
    conn.commit()

    return {
        "ok": True,
        "processed": processed,
        "created_leads": created_leads,
        "updated_leads": updated_leads,
        "enrolled_in_campaign": enrolled_in_campaign,
        "skipped_duplicates": skipped_duplicates,
        "campaign_id": campaign_id,
        "lead_ids": lead_ids,
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
    expected_key = os.getenv("MCP_API_KEY", "").strip()
    if not expected_key:
        return
    auth_header = (request.headers.get("authorization") or "").strip()
    bearer_token = ""
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header[7:].strip()
    header_key = (request.headers.get("x-mcp-key") or "").strip()
    query_key = (request.query_params.get("api_key") or "").strip()

    provided = bearer_token or header_key or query_key
    if provided != expected_key:
        raise HTTPException(
            status_code=401,
            detail="Неверный или отсутствующий MCP_API_KEY",
        )


def get_mcp_server_info(base_url: str = "http://127.0.0.1:8000") -> dict:
    clean_base = str(base_url or "http://127.0.0.1:8000").rstrip("/")
    venv_python = ROOT / ".venv" / "bin" / "python"
    python_cmd = str(venv_python) if venv_python.exists() else sys.executable
    has_key = bool(os.getenv("MCP_API_KEY", "").strip())

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
        base_url = str(request.base_url).rstrip("/")
        return get_mcp_server_info(base_url=base_url)

    @app.post("/mcp")
    async def mcp_http_endpoint(request: Request):
        _verify_mcp_auth(request)
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
        conn = getattr(request.app.state, "conn", None)
        if conn is None:
            conn = connect()
            init_db(conn)
        response_data = handle_jsonrpc_message(conn, payload)
        if response_data is None:
            return Response(status_code=202)
        return JSONResponse(content=response_data)

    @app.get("/mcp/sse")
    async def mcp_sse_endpoint(request: Request):
        _verify_mcp_auth(request)
        api_key = (request.query_params.get("api_key") or "").strip()
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
