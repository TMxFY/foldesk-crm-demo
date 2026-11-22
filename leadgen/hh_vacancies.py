#!/usr/bin/env python3
"""HeadHunter (api.hh.ru) remote video/UGC vacancy collector and router.

Authenticated collection via ``HH_API_TOKEN`` (``Authorization: Bearer <token>``)
and mandatory ``HH-User-Agent: SoftUGC-Leadgen/1.0 (ops@softugc.com)``.
Anonymous requests to ``api.hh.ru/vacancies`` return HTTP 403; missing tokens
or 401/403 responses return a structured ``{"status": "auth_required", ...}``
result without crashing the leadgen pipeline.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import html
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

HH_API_BASE_URL = "https://api.hh.ru"
HH_VACANCIES_URL = f"{HH_API_BASE_URL}/vacancies"
HH_USER_AGENT = "SoftUGC-Leadgen/1.0 (ops@softugc.com)"
DEFAULT_SCHEDULE = "remote"
DEFAULT_PERIOD_DAYS = 7
DEFAULT_SHOWCASE_URL = "https://tmxfy.github.io/"
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "рилсмейкер",
    "UGC креатор",
    "AI видео",
    "контент креатор",
    "монтаж reels shorts",
    "видеомейкер",
    "контент продюсер",
)

_TG_NOISE_HANDLES = frozenset(
    {
        "joinchat",
        "addlist",
        "share",
        "addstickers",
        "proxy",
        "iv",
        "s",
        "c",
        "boost",
        "socks",
        "bg",
        "setlanguage",
        "confirm",
        "telegram",
        "username",
        "here",
        "hh_ru",
        "softugc",
    }
)
_TG_URL_RE = re.compile(
    r"(?<![A-Za-z0-9._/\-])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z][A-Za-z0-9_]{3,31})(?=$|[/?#\"'\s<>),.;:])",
    re.IGNORECASE,
)
_TG_AT_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])@([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_]*\.[A-Za-z]{2,})(?=$|[\s,.;:!?)\]}\"'<>])",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b", re.IGNORECASE)
_WA_PHONE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:wa\.me/(?:c/)?|api\.whatsapp\.com/send\?[^#]*?\bphone=)(\+?[0-9][0-9\s().\-]{7,18})",
    re.IGNORECASE,
)
_E164_TEXT_RE = re.compile(r"(?<![A-Za-z0-9+])(\+\d[\d\s().\-]{8,16}\d)(?![\dA-Za-z])")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_WS_RE = re.compile(r"\s+")

_UNCONDITIONAL_JUNK_RE = re.compile(
    r"(?:"
    r"стори[сз][\s_-]*мейкер|story[\s_-]*mak|(?:оформлени|ведени|монтаж)\w*\s+стори[сз]|"
    r"иллюстра(?:тор|ц)\w*[\s\S]{0,80}детск\w*\s+книг|"
    r"(?:монтаж|нарезк|нарезчик|склейк|рилсмейкер)\w*[\s\S]{0,35}\bмем(?:ов|ы|ами|ам)?\b|"
    r"\b(?:roblox|fortnite|fivem|казино|букмекер\w*|гемблинг|gambling|casino|igaming)\b|"
    r"#резюме|#помогу|\bищу\s+работу\b"
    r")",
    re.IGNORECASE,
)
_DEV_TITLE_JUNK_RE = re.compile(
    r"(?:"
    r"программист|разработчик|\bdeveloper\b|software\s+engineer|"
    r"\bfront[\s-]?end\b|\bback[\s-]?end\b|\bfull[\s-]?stack\b|\bdevops\b|"
    r"\bqa\b|тестировщик|\b1[сc]\b|data\s+scientist|ml[\s-]+engineer|"
    r"системн\w+\s+администратор|сисадмин|инженер[\s-]+программист|"
    r"\b(?:python|java|golang|php|c\+\+|node\.?js|rust|ios|android|flutter|unity|unreal)\b|"
    r"бухгалтер|оператор\s+колл|менеджер\s+по\s+продажам|курьер|водитель|"
    r"seo[\s-]+специалист|директолог|таргетолог"
    r")",
    re.IGNORECASE,
)
_VIDEO_SIGNAL_RE = re.compile(
    r"(?:"
    r"рилс|reels|shorts|шортс|tiktok|тикток|\bugc\b|видео|video|"
    r"монтаж|клипмейкер|нейровидео|нейрокреатор|ai[\s-]*(?:видео|ролик|контент|аватар)|"
    r"вертикальн\w*\s+ролик|контент[\s-]*(?:креатор|продюсер|завод)|"
    r"capcut|premiere|after\s+effects|kling|heygen|sora|higgsfield"
    r")",
    re.IGNORECASE,
)
_TITLE_VIDEO_ROLE_RE = re.compile(
    r"(?:рилс|reels|shorts|шортс|tiktok|тикток|\bugc\b|видео|video|монтаж|креатор|creator|продюсер|producer|клипмейкер)",
    re.IGNORECASE,
)
_AUTOMATION_BOT_JUNK_RE = re.compile(
    r"(?:\bn8n\b|\bmake\.com\b|разработк\w*\s+(?:телеграм|tg|чат)[\s-]*бот\w*|(?:чат|telegram|tg)[\s-]*бот\w*\s+на\s+(?:python|aiogram|n8n|make))",
    re.IGNORECASE,
)
_TEXT_ONLY_COPY_RE = re.compile(r"\bбез\s+видео\b", re.IGNORECASE)
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_STOP_WORDS_HOST_RE = re.compile(r"(?:^|\.)(?:hh\.ru|t\.me|telegram\.me|vk\.com|instagram\.com|youtube\.com)$", re.IGNORECASE)


@dataclass(frozen=True)
class HHVacancyConfig:
    """Configuration contract for collecting remote HeadHunter vacancies."""

    token: str | None = None
    user_agent: str = HH_USER_AGENT
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    schedule: str = DEFAULT_SCHEDULE
    period_days: int = DEFAULT_PERIOD_DAYS
    per_page: int = 50
    max_pages: int = 1
    limit: int = 100
    timeout_s: float = 20.0
    hydrate_details: bool = True
    showcase_url: str = DEFAULT_SHOWCASE_URL
    custom_draft_template: str = ""


class HHCollectionResult(TypedDict):
    """Structured result returned by ``collect_hh_vacancies``."""

    status: Literal["ok", "auth_required", "rate_limited", "error"]
    rows: list[dict[str, Any]]
    error: str
    stats: dict[str, int]


def _clean_text(value: Any) -> str:
    return _MULTI_WS_RE.sub(" ", str(value or "")).strip()


def _strip_html(raw: Any) -> str:
    text = str(raw or "")
    if not text:
        return ""
    unescaped = html.unescape(_HTML_TAG_RE.sub(" ", text))
    return _MULTI_WS_RE.sub(" ", unescaped).strip()


def _clean_url(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s or " " in s:
        return ""
    if s.startswith(("http://", "https://")):
        return s
    if "." in s and not s.startswith("@"):
        return f"https://{s}"
    return ""


def _extract_external_domain(raw_url: str) -> str:
    cleaned = _clean_url(raw_url)
    if not cleaned:
        return ""
    host = (urlparse(cleaned).hostname or "").lower().removeprefix("www.").rstrip(".")
    if not host or "." not in host or _STOP_WORDS_HOST_RE.search(host):
        return ""
    return host


def _normalize_phone(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    if len(digits) == 10 and digits.startswith("9"):
        digits = f"7{digits}"
    elif len(digits) == 11 and digits.startswith("8") and digits[1] in ("3", "4", "8", "9"):
        digits = f"7{digits[1:]}"
    if not (10 <= len(digits) <= 15):
        return ""
    if len(set(digits)) <= 1 or digits.startswith("1234567"):
        return ""
    return f"+{digits}"


def _format_hh_salary(salary: Any) -> str:
    if not isinstance(salary, Mapping):
        return ""
    s_from = salary.get("from")
    s_to = salary.get("to")
    curr = str(salary.get("currency") or "").strip().upper()
    if s_from is None and s_to is None:
        return ""
    if s_from is not None and s_to is not None:
        amount = f"{s_from}–{s_to}"
    elif s_from is not None:
        amount = f"от {s_from}"
    else:
        amount = f"до {s_to}"
    return f"{amount} {curr}".strip()


def _vacancy_text_blobs(vacancy: Mapping[str, Any]) -> tuple[str, str]:
    """Return (raw_html_and_snippet_blob, stripped_plain_text)."""
    snippet = vacancy.get("snippet") if isinstance(vacancy.get("snippet"), Mapping) else {}
    parts_raw = [
        str(vacancy.get("description") or ""),
        str(snippet.get("requirement") or ""),
        str(snippet.get("responsibility") or ""),
    ]
    raw_blob = "\n".join(p for p in parts_raw if p.strip())
    plain_parts = [_strip_html(p) for p in parts_raw if p.strip()]
    plain_text = "\n".join(p for p in plain_parts if p).strip()
    return raw_blob, plain_text


def _is_remote_vacancy(vacancy: Mapping[str, Any], title: str, plain_text: str) -> bool:
    schedule = vacancy.get("schedule")
    sched_id = str(schedule.get("id") or "").strip() if isinstance(schedule, Mapping) else ""
    work_formats = vacancy.get("work_format")
    has_remote_format = isinstance(work_formats, Sequence) and any(
        isinstance(wf, Mapping) and str(wf.get("id") or "").strip().upper() == "REMOTE"
        for wf in work_formats
    )
    if has_remote_format or sched_id == "remote":
        return True
    if not sched_id:
        return True
    blob = f"{title} {plain_text}"
    return bool(re.search(r"удален|удалён|\bremote\b", blob, re.IGNORECASE))


def is_junk_hh_vacancy(vacancy: Mapping[str, Any]) -> tuple[bool, str]:
    """Return ``(True, reason)`` for programmer/dev/storymaker/non-remote/non-video junk."""
    title = _clean_text(vacancy.get("name") or vacancy.get("title"))
    if not title:
        return True, "empty_title"
    _, plain_text = _vacancy_text_blobs(vacancy)
    combined = f"{title}\n{plain_text}"

    m_unc = _UNCONDITIONAL_JUNK_RE.search(combined)
    if m_unc:
        return True, f"unconditional_junk:{m_unc.group(0).lower()}"

    m_dev = _DEV_TITLE_JUNK_RE.search(title)
    if m_dev:
        return True, f"dev_or_non_video_title:{m_dev.group(0).lower()}"

    if not _is_remote_vacancy(vacancy, title, plain_text):
        sched = vacancy.get("schedule")
        sched_id = str(sched.get("id") or "") if isinstance(sched, Mapping) else ""
        return True, f"non_remote_schedule:{sched_id}"

    if not _VIDEO_SIGNAL_RE.search(combined):
        return True, "no_video_ugc_signal"

    if _TEXT_ONLY_COPY_RE.search(plain_text) and not _TITLE_VIDEO_ROLE_RE.search(title):
        return True, "text_only_copywriter"

    if _AUTOMATION_BOT_JUNK_RE.search(combined) and not _TITLE_VIDEO_ROLE_RE.search(title):
        return True, "automation_or_bot_dev"

    return False, ""


def _extract_tg_usernames(blobs: Sequence[str]) -> list[str]:
    found: list[str] = []
    seen_low: set[str] = set()
    for blob in blobs:
        if not blob:
            continue
        for regex in (_TG_URL_RE, _TG_AT_RE):
            for match in regex.finditer(blob):
                handle = match.group(1).strip()
                low = handle.lower()
                if len(handle) < 4 or low in _TG_NOISE_HANDLES:
                    continue
                if low.endswith("bot") or low.endswith("_bot"):
                    continue
                if low not in seen_low:
                    seen_low.add(low)
                    found.append(handle)
    return found


def _extract_phones(contacts: Mapping[str, Any], blobs: Sequence[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw_val: Any) -> None:
        norm = _normalize_phone(raw_val)
        if norm and norm not in seen:
            seen.add(norm)
            found.append(norm)

    raw_phones = contacts.get("phones")
    if isinstance(raw_phones, Sequence) and not isinstance(raw_phones, (str, bytes)):
        for entry in raw_phones:
            if isinstance(entry, str):
                _add(entry)
            elif isinstance(entry, Mapping):
                candidate = (
                    entry.get("formatted")
                    or entry.get("phone")
                    or f"{entry.get('country') or ''}{entry.get('city') or ''}{entry.get('number') or ''}"
                )
                _add(candidate)

    for blob in blobs:
        if not blob:
            continue
        for match in _WA_PHONE_RE.finditer(blob):
            _add(match.group(1))
        for match in _E164_TEXT_RE.finditer(blob):
            _add(match.group(1))
    return found


def extract_hh_contacts(
    vacancy: Mapping[str, Any],
    employer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract Telegram usernames, phones, email, and employer site URLs from a vacancy."""
    contacts = vacancy.get("contacts") if isinstance(vacancy.get("contacts"), Mapping) else {}
    vac_emp = vacancy.get("employer") if isinstance(vacancy.get("employer"), Mapping) else {}
    merged_emp: dict[str, Any] = dict(vac_emp)
    if isinstance(employer, Mapping):
        for k, v in employer.items():
            if v and not merged_emp.get(k):
                merged_emp[k] = v

    raw_blob, plain_text = _vacancy_text_blobs(vacancy)
    phone_comments: list[str] = []
    raw_phones = contacts.get("phones")
    if isinstance(raw_phones, Sequence) and not isinstance(raw_phones, (str, bytes)):
        for entry in raw_phones:
            if isinstance(entry, Mapping) and entry.get("comment"):
                phone_comments.append(str(entry["comment"]))

    contact_blobs = [
        raw_blob,
        plain_text,
        str(contacts.get("name") or ""),
        *phone_comments,
    ]
    usernames = _extract_tg_usernames(contact_blobs)
    phones = _extract_phones(contacts, [raw_blob, plain_text])

    email = _clean_text(contacts.get("email"))
    if not email:
        for match in _EMAIL_RE.finditer(plain_text):
            cand = match.group(1).strip()
            low = cand.lower()
            if not low.startswith(("noreply@", "no-reply@")) and not low.endswith("@hh.ru"):
                email = cand
                break

    employer_site_url = _clean_url(merged_emp.get("site_url"))
    employer_alternate_url = _clean_url(merged_emp.get("alternate_url"))
    alternate_url = _clean_url(vacancy.get("alternate_url")) or _clean_url(vacancy.get("apply_alternate_url"))
    project_url = employer_site_url or employer_alternate_url

    return {
        "usernames": usernames,
        "username": usernames[0] if usernames else "",
        "phones": phones,
        "phone": phones[0] if phones else "",
        "email": email,
        "contact_name": _clean_text(contacts.get("name")),
        "employer_name": _clean_text(merged_emp.get("name")),
        "employer_site_url": employer_site_url,
        "employer_alternate_url": employer_alternate_url,
        "project_url": project_url,
        "company_domain": _extract_external_domain(employer_site_url),
        "alternate_url": alternate_url,
    }


def _infer_vacancy_layer(title: str, plain_text: str) -> str:
    blob = f"{title} {plain_text}".lower()
    if any(k in blob for k in ("трафик", "продюсер", "smm", "воронк", "growth", "маркетинг")):
        if not any(k in title.lower() for k in ("рилс", "reels", "shorts", "ugc", "монтаж", "видео")):
            return "трафик"
    return "AI-контент"


def _infer_pitch_focus(title: str, plain_text: str) -> str:
    blob = f"{title} {plain_text}".lower()
    if any(k in blob for k in ("ai", "нейро", "kling", "sora", "heygen", "higgsfield", "аватар")):
        return (
            "Мы закрываем это через систему видеолидгена и AI UGC: реалистичные ролики и конвейер "
            "под Reels/Shorts (до 90 роликов в месяц на проект) плюс привязку к воронке и аналитике."
        )
    if any(k in blob for k in ("продюсер", "трафик", "воронк")):
        return (
            "Строим под такие задачи системный лидген на коротких видео: контент-конвейер для Reels/Shorts/TikTok "
            "(свои креаторы + AI UGC при необходимости) и аналитика воронки до заявки."
        )
    return (
        "Мы закрываем полный цикл короткого видео под Reels/Shorts/TikTok: своя команда креаторов и "
        "AI UGC-продакшн на потоке (до 90 роликов в месяц), сценарии, монтаж и воронка под лиды."
    )


def build_hh_outreach_draft(
    vacancy: Mapping[str, Any],
    *,
    showcase_url: str = DEFAULT_SHOWCASE_URL,
    custom_template: str = "",
) -> str:
    """Build a personalized Russian outreach draft for a HeadHunter vacancy."""
    title = _clean_text(vacancy.get("name") or vacancy.get("title") or "видео-контент")
    emp = vacancy.get("employer") if isinstance(vacancy.get("employer"), Mapping) else {}
    employer_name = _clean_text(vacancy.get("employer_name") or emp.get("name"))
    _, plain_text = _vacancy_text_blobs(vacancy)
    if not plain_text:
        plain_text = _clean_text(vacancy.get("what_they_do"))
    resolved_showcase = _clean_url(showcase_url) or DEFAULT_SHOWCASE_URL
    pay = _format_hh_salary(vacancy.get("salary")) or _clean_text(vacancy.get("pay"))

    if custom_template.strip():
        mapping = {
            "title": title,
            "role": title,
            "company": employer_name or title,
            "employer_name": employer_name or title,
            "showcase_url": resolved_showcase,
            "pay": pay,
        }
        rendered = custom_template
        for key, val in mapping.items():
            rendered = rendered.replace("{" + key + "}", str(val))
        return rendered.strip()

    company_clause = f" в {employer_name}" if employer_name else ""
    focus_line = _infer_pitch_focus(title, plain_text)
    lines = [
        f"Добрый день! По вакансии «{title}»{company_clause}.",
        "",
        focus_line,
        "",
        "Ключевой кейс: faceless-канал на коротких роликах — 183,5 млн просмотров за 90 дней, "
        "241K подписчиков и 94,8% холодного охвата; коммерческим проектам держим темп до 90 роликов/мес.",
        f"Витрина и примеры работ: {resolved_showcase}",
        "",
        "Для оценки качества в деле можем собрать тестовый фрагмент 10–15 секунд или обсудить тестовую серию. "
        "Какой объём роликов в месяц сейчас планируете и на какую площадку основной упор?",
    ]
    return "\n".join(lines)


def _build_followup_texts(title: str) -> tuple[str, str]:
    f1 = (
        f"Добрый день! Возвращаюсь по задаче «{title}». "
        "Если пришлёте 1–2 референса или ТЗ, соберу тестовый фрагмент 10–15 сек под ваш формат, "
        "а при интересе — обсудим тестовую серию под ваши метрики."
    )
    f2 = (
        f"Подскажите, задача по «{title}» ещё открыта или уже нашли команду? "
        "Если актуально — можем созвониться на 10 минут и наметить тестовый запуск."
    )
    return f1, f2


def normalize_hh_vacancy(
    vacancy: Mapping[str, Any],
    *,
    employer: Mapping[str, Any] | None = None,
    keyword: str = "",
    showcase_url: str = DEFAULT_SHOWCASE_URL,
    custom_template: str = "",
) -> dict[str, Any] | None:
    """Validate, extract contacts, draft RU outreach, and route one HH vacancy row."""
    rejected, _reason = is_junk_hh_vacancy(vacancy)
    if rejected:
        return None

    contacts = extract_hh_contacts(vacancy, employer=employer)
    title = _clean_text(vacancy.get("name") or vacancy.get("title") or "Вакансия")
    _, plain_text = _vacancy_text_blobs(vacancy)
    vac_id = str(vacancy.get("id") or "").strip() or hashlib.sha256(
        (contacts["alternate_url"] or title).encode("utf-8")
    ).hexdigest()[:12]
    salary_text = _format_hh_salary(vacancy.get("salary"))
    layer = _infer_vacancy_layer(title, plain_text)
    employer_name = contacts["employer_name"]
    company = employer_name or title
    display_name = contacts["contact_name"] or company
    draft_text = build_hh_outreach_draft(
        {**dict(vacancy), "employer_name": employer_name},
        showcase_url=showcase_url,
        custom_template=custom_template,
    )
    f1, f2 = _build_followup_texts(title)

    has_direct = bool(contacts["username"] or contacts["phone"])
    segment = "vacancy" if has_direct else "vacancy_assisted"
    if contacts["username"]:
        provider = "telegram"
    elif contacts["phone"]:
        provider = "whatsapp"
    else:
        provider = "manual"

    note_parts = [f"Вакансия: {title}"]
    if salary_text:
        note_parts.append(f"Оплата: {salary_text}")
    if contacts["alternate_url"]:
        note_parts.append(f"hh.ru: {contacts['alternate_url']}")

    row: dict[str, Any] = {
        "id": hashlib.sha256(f"hh_ru:{vac_id}".encode("utf-8")).hexdigest()[:24],
        "vacancy_id": vac_id,
        "source": "hh_ru",
        "lead_source": "hh_ru",
        "segment": segment,
        "language": "ru",
        "provider": provider,
        "keyword": keyword,
        "title": title,
        "role": title,
        "name": display_name,
        "company": company,
        "project": company,
        "employer_name": employer_name,
        "employer_site_url": contacts["employer_site_url"],
        "employer_url": contacts["employer_alternate_url"],
        "project_url": contacts["project_url"],
        "website": contacts["project_url"],
        "company_domain": contacts["company_domain"],
        "url": contacts["alternate_url"],
        "job_url": contacts["alternate_url"],
        "alternate_url": contacts["alternate_url"],
        "username": contacts["username"],
        "telegram_username": contacts["username"],
        "usernames": contacts["usernames"],
        "phone": contacts["phone"],
        "phones": contacts["phones"],
        "email": contacts["email"],
        "category": layer,
        "niche": layer,
        "pay": salary_text,
        "salary": salary_text,
        "posted_at": str(vacancy.get("published_at") or "").strip(),
        "description": plain_text[:4000],
        "what_they_do": plain_text[:400] or title,
        "their_angle": f"{title} | {salary_text}".strip(" |") if salary_text else title,
        "our_angle": "AI UGC + контент-система для Reels/Shorts",
        "note": "\n".join(note_parts),
        "manual_action_url": contacts["alternate_url"] if segment == "vacancy_assisted" else "",
        "manual_draft_text": draft_text if segment == "vacancy_assisted" else "",
        "step1_text": draft_text,
        "step2_text": f1,
        "step3_text": f2,
    }
    return row


def _invoke_http_get(
    http_get_fn: Callable[..., Any],
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 20.0,
) -> tuple[int, Any]:
    """Call ``requests.get`` or an injected ``http_get_fn`` and return ``(status_code, payload)``."""
    kwargs: dict[str, Any] = {}
    try:
        sig = inspect.signature(http_get_fn)
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if accepts_var_kw or "params" in sig.parameters:
            kwargs["params"] = params
        if accepts_var_kw or "headers" in sig.parameters:
            kwargs["headers"] = headers
        if accepts_var_kw or "timeout" in sig.parameters:
            kwargs["timeout"] = timeout_s
        response = http_get_fn(url, **kwargs)
    except (TypeError, ValueError):
        response = http_get_fn(url, params=params, headers=headers)

    if isinstance(response, tuple) and len(response) == 2:
        return int(response[0]), response[1]
    if isinstance(response, dict):
        return 200, response
    status = int(getattr(response, "status_code", 200))
    payload: Any = {}
    if hasattr(response, "json") and callable(response.json):
        try:
            payload = response.json()
        except Exception as exc:
            logger.debug("Non-JSON response from %s (status=%s): %s", url, status, exc)
            payload = {"raw_text": str(getattr(response, "text", "") or "")}
    return status, payload


def _hydrate_vacancy_and_employer(
    item: Mapping[str, Any],
    *,
    http_get_fn: Callable[..., Any],
    headers: dict[str, str],
    timeout_s: float,
    employer_cache: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None, int | None]:
    """Fetch detail vacancy and employer site_url when omitted from search items.

    Returns ``(hydrated_vacancy, employer_payload, auth_error_status)``.
    """
    hydrated = dict(item)
    vac_id = str(item.get("id") or "").strip()
    if "description" not in hydrated and (item.get("url") or vac_id):
        detail_url = str(item.get("url") or "").strip() or f"{HH_VACANCIES_URL}/{vac_id}"
        try:
            st, detail_body = _invoke_http_get(
                http_get_fn,
                detail_url,
                params=None,
                headers=headers,
                timeout_s=timeout_s,
            )
            if st == 401:
                return hydrated, None, 401
            if st == 200 and isinstance(detail_body, Mapping) and (
                "description" in detail_body or "contacts" in detail_body
            ):
                for key, val in detail_body.items():
                    if val is not None:
                        hydrated[key] = val
        except Exception as exc:
            logger.debug("HH detail fetch failed for %s: %s", detail_url, exc)

    emp = hydrated.get("employer") if isinstance(hydrated.get("employer"), Mapping) else {}
    emp_id = str(emp.get("id") or "").strip()
    emp_url = str(emp.get("url") or "").strip() or (f"{HH_API_BASE_URL}/employers/{emp_id}" if emp_id else "")
    emp_payload: dict[str, Any] | None = None
    if not emp.get("site_url") and emp_url:
        cache_key = emp_id or emp_url
        if cache_key in employer_cache:
            emp_payload = employer_cache[cache_key]
        else:
            try:
                est, ebody = _invoke_http_get(
                    http_get_fn,
                    emp_url,
                    params=None,
                    headers=headers,
                    timeout_s=timeout_s,
                )
                if est == 200 and isinstance(ebody, Mapping) and ("site_url" in ebody or "alternate_url" in ebody):
                    emp_payload = dict(ebody)
                else:
                    emp_payload = {}
            except Exception as exc:
                logger.debug("HH employer fetch failed for %s: %s", emp_url, exc)
                emp_payload = {}
            employer_cache[cache_key] = emp_payload

    return hydrated, emp_payload, None


def collect_hh_vacancies(
    *,
    token: str | None = None,
    keywords: Sequence[str] | None = None,
    schedule: str = DEFAULT_SCHEDULE,
    period: int = DEFAULT_PERIOD_DAYS,
    per_page: int = 50,
    max_pages: int = 1,
    limit: int = 100,
    user_agent: str = HH_USER_AGENT,
    timeout_s: float = 20.0,
    hydrate_details: bool = True,
    showcase_url: str = DEFAULT_SHOWCASE_URL,
    custom_draft_template: str = "",
    seen_ids: Iterable[str] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    config: HHVacancyConfig | None = None,
) -> HHCollectionResult:
    """Collect, filter, and route fresh remote RU video/UGC vacancies from HeadHunter API."""
    eff_token = token if token is not None else (config.token if config and config.token is not None else os.environ.get("HH_API_TOKEN", ""))
    resolved_token = str(eff_token or "").strip()
    stats = {
        "keywords_queried": 0,
        "raw_items": 0,
        "skipped_seen": 0,
        "skipped_junk": 0,
        "direct_vacancy_rows": 0,
        "assisted_rows": 0,
    }
    if not resolved_token:
        return {
            "status": "auth_required",
            "rows": [],
            "error": "HH_API_TOKEN is missing; register an app on dev.hh.ru and set HH_API_TOKEN",
            "stats": stats,
        }

    eff_keywords = tuple(keywords) if keywords is not None else (config.keywords if config else DEFAULT_KEYWORDS)
    eff_schedule = schedule or (config.schedule if config else DEFAULT_SCHEDULE)
    eff_period = int(period if period is not None else (config.period_days if config else DEFAULT_PERIOD_DAYS))
    eff_per_page = max(1, min(100, int(per_page if per_page is not None else (config.per_page if config else 50))))
    eff_max_pages = max(1, int(max_pages if max_pages is not None else (config.max_pages if config else 1)))
    eff_limit = max(1, int(limit if limit is not None else (config.limit if config else 100)))
    eff_ua = (user_agent or (config.user_agent if config else HH_USER_AGENT)).strip() or HH_USER_AGENT
    eff_timeout = float(timeout_s if timeout_s is not None else (config.timeout_s if config else 20.0))
    eff_hydrate = bool(hydrate_details if config is None else config.hydrate_details)
    eff_showcase = showcase_url or (config.showcase_url if config else DEFAULT_SHOWCASE_URL)
    eff_template = custom_draft_template or (config.custom_draft_template if config else "")

    getter = requests.get if http_get_fn is None else http_get_fn
    headers = {
        "Authorization": f"Bearer {resolved_token}",
        "HH-User-Agent": eff_ua,
        "Accept": "application/json",
    }

    seen_set: set[str] = {str(s).strip() for s in (seen_ids or ()) if str(s or "").strip()}
    employer_cache: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    search_errors: list[str] = []
    rate_limited = False
    succeeded_searches = 0

    for kw in eff_keywords:
        if len(rows) >= eff_limit:
            break
        kw_clean = str(kw or "").strip()
        if not kw_clean:
            continue
        stats["keywords_queried"] += 1

        for page in range(eff_max_pages):
            if len(rows) >= eff_limit:
                break
            params = {
                "text": kw_clean,
                "schedule": eff_schedule,
                "period": eff_period,
                "per_page": eff_per_page,
                "page": page,
            }
            try:
                status_code, body = _invoke_http_get(
                    getter,
                    HH_VACANCIES_URL,
                    params=params,
                    headers=headers,
                    timeout_s=eff_timeout,
                )
            except Exception as exc:
                search_errors.append(f"{kw_clean}: {type(exc).__name__}: {exc}")
                break

            if status_code in (401, 403):
                return {
                    "status": "auth_required",
                    "rows": [],
                    "error": f"HeadHunter API returned HTTP {status_code} for keyword {kw_clean!r}",
                    "stats": stats,
                }
            if status_code == 429:
                rate_limited = True
                search_errors.append(f"{kw_clean}: HTTP 429 rate limited")
                break
            if status_code >= 400 or not isinstance(body, Mapping):
                search_errors.append(f"{kw_clean}: HTTP {status_code}")
                break

            succeeded_searches += 1
            items = body.get("items")
            if not isinstance(items, list):
                break

            for raw_item in items:
                if len(rows) >= eff_limit:
                    break
                if not isinstance(raw_item, Mapping):
                    continue
                stats["raw_items"] += 1
                dedup_key = str(raw_item.get("id") or raw_item.get("alternate_url") or "").strip()
                if dedup_key and dedup_key in seen_set:
                    stats["skipped_seen"] += 1
                    continue
                if dedup_key:
                    seen_set.add(dedup_key)

                # Filter obvious junk early before paying extra detail/employer requests.
                if "description" in raw_item:
                    early_junk, _ = is_junk_hh_vacancy(raw_item)
                    if early_junk:
                        stats["skipped_junk"] += 1
                        continue
                else:
                    raw_title = _clean_text(raw_item.get("name") or raw_item.get("title"))
                    if raw_title and (_DEV_TITLE_JUNK_RE.search(raw_title) or _UNCONDITIONAL_JUNK_RE.search(raw_title)):
                        stats["skipped_junk"] += 1
                        continue

                hydrated_item = dict(raw_item)
                emp_payload: dict[str, Any] | None = None
                if eff_hydrate:
                    hydrated_item, emp_payload, auth_err = _hydrate_vacancy_and_employer(
                        hydrated_item,
                        http_get_fn=getter,
                        headers=headers,
                        timeout_s=eff_timeout,
                        employer_cache=employer_cache,
                    )
                    if auth_err in (401, 403):
                        return {
                            "status": "auth_required",
                            "rows": [],
                            "error": f"HeadHunter detail API returned HTTP {auth_err}",
                            "stats": stats,
                        }

                normalized = normalize_hh_vacancy(
                    hydrated_item,
                    employer=emp_payload,
                    keyword=kw_clean,
                    showcase_url=eff_showcase,
                    custom_template=eff_template,
                )
                if normalized is None:
                    stats["skipped_junk"] += 1
                    continue
                rows.append(normalized)
                if normalized["segment"] == "vacancy":
                    stats["direct_vacancy_rows"] += 1
                else:
                    stats["assisted_rows"] += 1

            try:
                total_pages = max(1, int(body.get("pages") or 1))
            except (TypeError, ValueError):
                total_pages = 1
            if page + 1 >= total_pages or not items:
                break

        if rate_limited:
            break

    if rows:
        return {
            "status": "ok",
            "rows": rows,
            "error": "; ".join(search_errors) if search_errors else "",
            "stats": stats,
        }
    if rate_limited and succeeded_searches == 0:
        return {
            "status": "rate_limited",
            "rows": [],
            "error": "; ".join(search_errors) or "HTTP 429 rate limited",
            "stats": stats,
        }
    if search_errors and succeeded_searches == 0:
        return {
            "status": "error",
            "rows": [],
            "error": "; ".join(search_errors),
            "stats": stats,
        }
    return {
        "status": "ok",
        "rows": [],
        "error": "",
        "stats": stats,
    }


def _load_dotenv_if_needed() -> None:
    if os.environ.get("CRMTG_SKIP_DOTENV") == "1" or os.environ.get("HH_API_TOKEN"):
        return
    for candidate in (WORKTREE_ROOT / ".env", Path("/opt/crmtg/.env")):
        if not candidate.is_file():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError as exc:
            logger.debug("Could not read %s: %s", candidate, exc)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint writing discovered hh.ru vacancy rows to JSONL."""
    parser = argparse.ArgumentParser(description="Collect remote RU video/UGC vacancies from HeadHunter API")
    parser.add_argument("--output", required=True, help="Path to output JSONL file")
    parser.add_argument("--period", type=int, default=DEFAULT_PERIOD_DAYS)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--keyword", action="append", default=None, help="Override/repeat search keyword")
    parser.add_argument("--no-hydrate", action="store_true", help="Skip /vacancies/{id} & /employers/{id} lookups")
    parser.add_argument("--showcase-url", default=DEFAULT_SHOWCASE_URL)
    args = parser.parse_args(list(argv) if argv is not None else None)

    _load_dotenv_if_needed()
    result = collect_hh_vacancies(
        keywords=tuple(args.keyword) if args.keyword else None,
        period=args.period,
        max_pages=args.max_pages,
        limit=args.limit,
        hydrate_details=not args.no_hydrate,
        showcase_url=args.showcase_url,
    )
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False) for row in result["rows"]]
    out_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")

    summary = {
        "status": result["status"],
        "rows_written": len(result["rows"]),
        "error": result["error"],
        "stats": result["stats"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
