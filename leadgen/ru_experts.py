#!/usr/bin/env python3
"""Deep discovery module for Russian experts (producing offer: 100-300K RUB fix + %).

Supports three independent discovery channels with zero hardcoded niche lock-in:
1. Telegram channels (`seed_channels` -> `treg` similar-channels & channel-info +
   `monid` `litescrape /telegram/posts`), extracting personal `@username` from
   channel description / pinned posts while rejecting bots, ad exchanges, and
   agency aggregators.
2. RU profile Telegram chats (`seed_chats` parsed via injected Telethon client
   or message sequences) with personal-expert vs agency/spam filtering.
3. Instagram -> Telegram (`monid` `anyapi /instagram.profile_contact` ->
   `bioLinks` / Taplink / `t.me/` -> resolve personal `@username` from TG channel bio).

Every emitted lead row enforces Airtable isolation and RU segmentation:
- `segment="ru_expert"`
- `language="ru"`
- `exclude_from_airtable=1`
- `source="ru_experts"`
- `provider="telegram"`
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import html as html_mod
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

try:
    from leadgen.treg_client import call as treg_call, check_treg_ready
except ImportError:
    try:
        from treg_client import call as treg_call, check_treg_ready  # type: ignore[no-redef]
    except ImportError:
        treg_call = None  # type: ignore[assignment]
        check_treg_ready = None  # type: ignore[assignment]

try:
    from leadgen.monid_client import check_monid_ready, run_monid
except ImportError:
    try:
        from monid_client import check_monid_ready, run_monid  # type: ignore[no-redef]
    except ImportError:
        check_monid_ready = None  # type: ignore[assignment]
        run_monid = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

COST_TREG_SIMILAR_CHANNELS_USD = 0.001
COST_TREG_CHANNEL_INFO_USD = 0.001
COST_MONID_TG_POSTS_USD = 0.00015
COST_MONID_IG_OCTOPUS_USD = 0.0
COST_MONID_IG_CHAMELEON_USD = 0.00175

DEFAULT_MIN_SUBS = 1000
DEFAULT_MAX_SUBS = 50000

_TG_NOISE_HANDLES = frozenset(
    {
        "joinchat",
        "addstickers",
        "share",
        "proxy",
        "socks",
        "iv",
        "boost",
        "login",
        "contact",
        "telegram",
        "s",
        "c",
    }
)

_IG_RESERVED_PATHS = frozenset(
    {
        "p",
        "reel",
        "reels",
        "stories",
        "explore",
        "accounts",
        "direct",
        "tv",
        "s",
        "about",
        "legal",
        "privacy",
    }
)

_SKIP_EXTERNAL_HOSTS = frozenset(
    {
        "t.me",
        "telegram.me",
        "telegram.dog",
        "telegram.org",
        "telegra.ph",
        "instagram.com",
        "facebook.com",
        "fb.com",
        "vk.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "x.com",
        "twitter.com",
        "linkedin.com",
        "threads.net",
        "dzen.ru",
        "zen.yandex.ru",
        "ok.ru",
        "whatsapp.com",
        "wa.me",
        "api.whatsapp.com",
        "bit.ly",
        "clck.ru",
        "t.co",
        "goo.gl",
    }
)

_MULTI_LINK_HOSTS = frozenset(
    {
        "taplink.cc",
        "taplink.ru",
        "linktr.ee",
        "linktree.com",
        "beacons.ai",
        "mssg.me",
        "hipolink.me",
        "hipolink.ru",
        "meconnect.ru",
        "aqulas.me",
        "bento.me",
        "solo.to",
        "carrd.co",
        "bio.site",
        "campsite.bio",
        "l.instagram.com",
    }
)

_MAJOR_NON_TAPLINK_HOSTS = frozenset(
    {
        "t.me",
        "telegram.me",
        "telegram.dog",
        "instagram.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "vk.com",
        "facebook.com",
        "x.com",
        "twitter.com",
        "wa.me",
        "api.whatsapp.com",
        "apps.apple.com",
        "play.google.com",
    }
)

_KNOWN_EXCHANGE_OR_SERVICE_HANDLES = frozenset(
    {
        "telega_in",
        "telegain",
        "tgstat",
        "tgstat_bot",
        "telemetr",
        "telemetr_me",
        "epicstars",
        "admitad",
        "combot",
        "crosser_bot",
        "shieldy_bot",
        "botfather",
        "controllerbot",
        "livegrambot",
    }
)

_HANDLE_REJECT_SUBSTRS = (
    "ad_manager",
    "ads_manager",
    "admanager",
    "pr_manager",
    "prmanager",
    "reklama",
    "reklam_",
    "_reklam",
    "birzha",
    "ad_birzha",
    "exchange",
    "telega_in",
    "tgstat",
    "telemetr",
    "epicstars",
    "support",
    "helpdesk",
    "moderator",
    "safeguard",
    "assistant",
    "assist_",
    "_assist",
)

_HANDLE_REJECT_TOKENS = frozenset(
    {
        "ad",
        "ads",
        "pr",
        "reklama",
        "reklam",
        "birzha",
        "exchange",
        "manager",
        "menedzher",
        "assistant",
        "assist",
        "support",
        "help",
        "mod",
        "moderator",
        "promo",
        "admin",
        "agency",
        "aggregator",
        "bot",
    }
)

_AD_EXCHANGE_OR_MANAGER_LINE_RE = re.compile(
    r"(?:"
    r"по\s+(?:всем\s+)?вопросам\s+реклам\w*|по\s+реклам\w*|реклам[аеы]\s*[:—\-]|"
    r"купить\s+реклам\w*|прайс[\s\-]*реклам\w*|размещени[ея]\s+реклам\w*|"
    r"рекламн\w*\s+бирж\w*|бирж\w*\s+реклам\w*|telega\.in|tgstat|telemetr|epicstars|"
    r"менеджер\s+по\s+реклам\w*|рекламн\w*\s+менеджер|pr[\s\-]*менеджер|"
    r"(?:^|\b)(?:менеджер|ассистент|помощник|поддержк\w*|саппорт)\s*[:—\-]|"
    r"ad\s*manager|for\s+ads\b|buying\s+ads|pr\s*manager|ad\s*exchange"
    r")",
    re.IGNORECASE,
)

_PERSONAL_CUE_RE = re.compile(
    r"(?:"
    r"личк[аеу]|в\s+лс\b|личн\w*\s+(?:вопрос\w*|сообщени\w*|консультац\w*|аккаунт\w*|тг|telegram)|"
    r"связ[ььи]\s+со\s+мной|(?:для\s+)?связ[иь](?:\s*[:—\-]|$)|связаться(?:\s+со\s+мной)?|"
    r"написать\s+мне|пиши(?:те)?\s+(?:мне|лично|в\s+(?:личку|лс|тг|tg|telegram|директ))|"
    r"мо[ий]\s+контакт\w*|закреп(?:л[её]нн\w*\s+пост)?|"
    r"автор(?:\s+канала)?\b|основател\w*|соосновател\w*|создател\w*|эксперт\b|"
    r"консультац\w*|запис[ььи]\s+на\s+(?:разбор|консультац|диагностик|сесси)|"
    r"разбор\w*|наставничеств\w*|коуч[\s\-]*сесси\w*|мой\s+(?:тг|telegram|профиль)|"
    r"сотрудничеств\w*\s+со\s+мной|(?:^|[|,;•\n])\s*вопросы\s*[:—\-]|"
    r"\bdm\s+me\b|\bpersonal\b|\bfounder\b|\bowner\b"
    r")",
    re.IGNORECASE,
)

_NEGATED_AGENCY_RE = re.compile(
    r"(?:без|не|против|вместо|уш[её]л\s+из|ушла\s+из)\s+"
    r"(?:(?:продюсерск\w*\s+центр\w*|маркетинг\w*\s+агентств\w*|рекламн\w*\s+агентств\w*|"
    r"smm[\s\-]*агентств\w*|агентств\w*)(?:\s*(?:и|,|или|/)\s*)?)+",
    re.IGNORECASE,
)

_AGENCY_AGGREGATOR_RE = re.compile(
    r"(?:"
    r"бирж\w*\s+реклам\w*|рекламн\w*\s+бирж\w*|каталог\s+(?:каналов|экспертов|специалистов|курсов)|"
    r"агрегатор\s+(?:экспертов|курсов|каналов|специалистов|блогов)|"
    r"база\s+(?:экспертов|каналов|блогеров)|реестр\s+(?:экспертов|специалистов)|"
    r"подбор\s+(?:экспертов|блогеров|каналов)|маркетплейс\s+экспертов|"
    r"продюсерск\w*\s+центр|продюсерск\w*\s+агентств\w*|рекламн\w*\s+агентств\w*|"
    r"маркетинг\w*\s+агентств\w*|smm[\s\-]*агентств\w*|digital[\s\-]*агентств\w*|"
    r"диджитал[\s\-]*агентств\w*|pr[\s\-]*агентств\w*|контент[\s\-]*агентств\w*|"
    r"агентство\s+полного\s+цикла|(?:мы|наш[ае]?)\s*[—\-]?\s*агентство|"
    r"продакшн[\s\-]*студи\w*|marketing\s+agency|production\s+agency|ad\s+exchange|"
    r"бирж\w*\s+фриланс\w*|чат\s+ваканси[йя]|канал\s+с\s+вакансиями"
    r")",
    re.IGNORECASE,
)

_SPAM_OR_FREELANCER_PITCH_RE = re.compile(
    r"(?:"
    r"\b(?:казино|casino|1win|1xbet|mostbet|фриспин\w*|букмекер\w*|ставки\s+на\s+спорт)\b|"
    r"заработок\s+без\s+вложени\w*|пассивн\w*\s+доход\s+от\s+\d+|vip[\s\-]*сигнал\w*|"
    r"#резюме|#ищу_?работу|ищу\s+работу\b|мо[её]\s+резюме|в\s+поисках\s+работы|"
    r"(?:сделаю|настрою|смонтирую)\s+(?:рилс|монтаж|таргет|сайт|бота)\s+(?:недорого|за\s+отзыв|под\s+ключ)"
    r")",
    re.IGNORECASE,
)

_RU_EXPERT_SIGNAL_RE = re.compile(
    r"(?:"
    r"эксперт\w*|психолог\w*|психотерапевт\w*|коуч\w*|нутрициолог\w*|диетолог\w*|"
    r"наставни\w*|ментор\w*|консультац\w*|консультиру\w*|разбор\w*|диагностик\w*|"
    r"авторск\w*\s+(?:курс|программ|методик|клуб)|мой\s+курс|свой\s+курс|"
    r"онлайн[\s\-]*(?:школ\w*|курс\w*|программ\w*|ведени\w*)|инфопродукт\w*|"
    r"обучаю|преподаю|вебинар\w*|мастермайнд\w*|запис[ььи]\s+на\s+|"
    r"врач\w*|эндокринолог\w*|юрист\w*|адвокат\w*|финансист\w*|бухгалтер\w*|"
    r"риелтор\w*|брокер\w*|стилист\w*|астролог\w*|тренер\w*|фитнес|"
    r"маркетолог\w*|продюсер\w*|секс[оу]лог\w*|логопед\w*|педагог\w*|"
    r"монетизац\w*|продукт\w*|услуг\w*|клиент\w*"
    r")",
    re.IGNORECASE,
)

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_TG_USERNAME_VALID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_IG_HANDLE_VALID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._]{0,28}[A-Za-z0-9])?$")

_AT_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])@([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_\.])"
)
_TME_HANDLE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:(s)/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/(\d+))?(?=[/?#\"'<>\s),.;:!?]|$)",
    re.IGNORECASE,
)
_TG_RESOLVE_RE = re.compile(
    r"tg://resolve\?[^\"'<>\s]*\bdomain=([A-Za-z][A-Za-z0-9_]{3,31})",
    re.IGNORECASE,
)
_IG_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})(?:[/?#]|$)",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_IG_EXPLICIT_TG_MENTION_RE = re.compile(
    r"(?:тг|tg|телеграм\w*|telegram|телега)\s*[:\-—/]?\s*@([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_\.])",
    re.IGNORECASE,
)
_CAPTCHA_ANTIBOT_RE = re.compile(
    r"(?:verify you are human|smartcaptcha|cf-challenge|g-recaptcha|hcaptcha|are you a robot)",
    re.IGNORECASE,
)
_SUBS_COUNT_RE = re.compile(
    r"([\d][\d\s\u00a0\u202f.,]*)\s*(тыс\.?|млн\.?|k|m|b|к|м)?\b",
    re.IGNORECASE,
)


def _normalize_term_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        items: Iterable[Any] = re.split(r"[\n,;]+", raw)
    elif isinstance(raw, Iterable):
        items = raw
    else:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = " ".join(str(item or "").split()).strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return tuple(out)


@dataclass(frozen=True)
class RuExpertsFilter:
    """Config-driven filter for Russian expert discovery (no hardcoded niche lock-in)."""

    niches: tuple[str, ...] = ()
    min_subs: int = DEFAULT_MIN_SUBS
    max_subs: int = DEFAULT_MAX_SUBS
    stop_words: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        norm_niches = _normalize_term_tuple(self.niches)
        norm_stops = _normalize_term_tuple(self.stop_words)
        min_v = max(0, int(self.min_subs if self.min_subs is not None else DEFAULT_MIN_SUBS))
        raw_max = int(self.max_subs if self.max_subs is not None else DEFAULT_MAX_SUBS)
        max_v = max(min_v, raw_max)
        object.__setattr__(self, "niches", norm_niches)
        object.__setattr__(self, "stop_words", norm_stops)
        object.__setattr__(self, "min_subs", min_v)
        object.__setattr__(self, "max_subs", max_v)


def filter_from_config(cfg: Mapping[str, Any] | None = None) -> RuExpertsFilter:
    """Build `RuExpertsFilter` from full `leadgen_config` or `ru_experts` section."""
    if not isinstance(cfg, Mapping):
        return RuExpertsFilter()
    raw_sec = cfg.get("ru_experts")
    section = raw_sec if isinstance(raw_sec, Mapping) else cfg
    try:
        min_subs = int(section.get("min_subs", DEFAULT_MIN_SUBS))
    except (TypeError, ValueError):
        min_subs = DEFAULT_MIN_SUBS
    try:
        max_subs = int(section.get("max_subs", DEFAULT_MAX_SUBS))
    except (TypeError, ValueError):
        max_subs = DEFAULT_MAX_SUBS
    return RuExpertsFilter(
        niches=section.get("niches") or (),  # type: ignore[arg-type]
        min_subs=min_subs,
        max_subs=max_subs,
        stop_words=section.get("stop_words") or (),  # type: ignore[arg-type]
    )


def parse_subscriber_count(value: Any) -> int | None:
    """Parse integer/compact subscriber counts ('12.4K', '15 тыс', '11.4M', '15 200')."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value < 0:
            return None
        return int(value)
    raw = str(value).strip()
    if not raw:
        return None
    m = _SUBS_COUNT_RE.search(raw)
    if not m:
        return None
    num_txt = m.group(1).replace("\u00a0", "").replace("\u202f", "").replace(" ", "").strip()
    suffix = (m.group(2) or "").strip().rstrip(".").casefold()
    mult_map = {
        "k": 1_000,
        "к": 1_000,
        "тыс": 1_000,
        "m": 1_000_000,
        "м": 1_000_000,
        "млн": 1_000_000,
        "b": 1_000_000_000,
    }
    mult = mult_map.get(suffix, 1)
    if mult > 1:
        # Handle 12,5K vs 1,200K
        if "," in num_txt and "." not in num_txt:
            parts = num_txt.split(",")
            if len(parts) == 2 and len(parts[1]) != 3:
                num_txt = num_txt.replace(",", ".")
            else:
                num_txt = num_txt.replace(",", "")
        else:
            num_txt = num_txt.replace(",", "")
    else:
        num_txt = re.sub(r"(?<=\d),(?=\d{3}\b)", "", num_txt)
        if "," in num_txt and "." not in num_txt:
            num_txt = num_txt.replace(",", ".")
    try:
        parsed = float(num_txt)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return int(round(parsed * mult))


def normalize_tg_handle(raw: str) -> str:
    """Normalize `@handle`, `t.me/handle`, or `tg://resolve?domain=handle` to bare username."""
    txt = str(raw or "").strip()
    if not txt:
        return ""
    if txt.lower().startswith("tg://"):
        parsed_tg = urlparse(txt)
        dom = (parse_qs(parsed_tg.query).get("domain") or [""])[0].strip().lstrip("@")
        if _TG_USERNAME_VALID_RE.fullmatch(dom) and dom.casefold() not in _TG_NOISE_HANDLES:
            return dom
        return ""
    if "t.me/" in txt.lower() or "telegram.me/" in txt.lower() or "telegram.dog/" in txt.lower():
        if "+" in txt.split("/")[-1]:
            return ""
        m = _TME_HANDLE_RE.search(txt)
        if not m:
            return ""
        handle = m.group(2).strip()
        if handle.casefold() in _TG_NOISE_HANDLES or not _TG_USERNAME_VALID_RE.fullmatch(handle):
            return ""
        return handle
    cleaned = txt.lstrip("@").split("?", 1)[0].split("#", 1)[0].strip("/")
    if cleaned.casefold() in _TG_NOISE_HANDLES or not _TG_USERNAME_VALID_RE.fullmatch(cleaned):
        return ""
    return cleaned


def normalize_ig_handle(raw: str) -> str:
    """Normalize `@handle` or `instagram.com/<handle>` to bare IG handle."""
    txt = str(raw or "").strip()
    if not txt:
        return ""
    m = _IG_URL_RE.search(txt)
    if m:
        cand = m.group(1).strip().strip("/")
    else:
        cand = txt.lstrip("@").split("?", 1)[0].split("#", 1)[0].strip("/")
    if not cand or cand.casefold() in _IG_RESERVED_PATHS:
        return ""
    if not _IG_HANDLE_VALID_RE.fullmatch(cand):
        return ""
    return cand


def is_bot_handle(username: str) -> bool:
    """Return True if username is empty, a TG noise path, or a bot/service handle."""
    low = normalize_tg_handle(username).casefold()
    if not low:
        return True
    if low in _TG_NOISE_HANDLES or low in _KNOWN_EXCHANGE_OR_SERVICE_HANDLES:
        return True
    return low.endswith("bot")


def _is_ad_or_manager_handle(username: str, clause: str = "") -> bool:
    low = normalize_tg_handle(username).casefold()
    if not low:
        return True
    if any(sub in low for sub in _HANDLE_REJECT_SUBSTRS):
        if not _PERSONAL_CUE_RE.search(clause):
            return True
    tokens = [tok for tok in re.split(r"_+", low) if tok]
    if any(tok in _HANDLE_REJECT_TOKENS for tok in tokens):
        if not _PERSONAL_CUE_RE.search(clause):
            return True
    if _AD_EXCHANGE_OR_MANAGER_LINE_RE.search(clause) and not _PERSONAL_CUE_RE.search(clause):
        return True
    return False


def is_agency_or_aggregator(text: str, *, title: str = "") -> bool:
    """Detect ad exchanges, agency aggregators, production centers, and catalogs."""
    combined = "\n".join(part for part in (title, text) if part).strip()
    if not combined:
        return False
    cleaned = _NEGATED_AGENCY_RE.sub(" ", combined)
    return _AGENCY_AGGREGATOR_RE.search(cleaned) is not None


def is_spam_or_freelancer_pitch(text: str) -> bool:
    """Detect casino/betting spam, job-seeker resumes, and cheap service-for-hire pitches."""
    if not text:
        return False
    return _SPAM_OR_FREELANCER_PITCH_RE.search(text) is not None


def _cyrillic_stem(word: str) -> str:
    """Conservative Russian inflection stemmer for niche/stop-word matching."""
    low = word.casefold()
    if len(low) < 6 or not _CYRILLIC_RE.search(low):
        return low
    for suffix in (
        "ость",
        "ение",
        "ание",
        "ство",
        "ия",
        "ие",
        "ий",
        "ии",
        "ые",
        "ой",
        "ая",
        "ое",
        "ов",
        "ев",
        "ам",
        "ям",
        "ах",
        "ях",
        "а",
        "у",
        "е",
        "ы",
        "и",
        "ю",
        "я",
        "ь",
    ):
        if low.endswith(suffix) and len(low) - len(suffix) >= 5:
            return low[: -len(suffix)]
    return low


def _term_matches_text(term: str, text_low: str) -> bool:
    clean = " ".join((term or "").split()).strip().casefold()
    if not clean:
        return False
    words = [w for w in re.split(r"[\s/\-]+", clean) if w]
    if not words:
        return False
    if len(words) == 1:
        w = words[0]
        if len(w) <= 3 and w.isascii() and w.isalnum():
            return re.search(rf"\b{re.escape(w)}\b", text_low) is not None
        if clean in text_low:
            return True
        stem = _cyrillic_stem(w)
        return stem in text_low
    if clean in text_low:
        return True
    return all(
        (re.search(rf"\b{re.escape(w)}\b", text_low) is not None)
        if (len(w) <= 3 and w.isascii() and w.isalnum())
        else (_cyrillic_stem(w) in text_low)
        for w in words
    )


def matches_ru_expert_filter(
    text: str,
    filter_cfg: RuExpertsFilter,
    *,
    subs: int | None = None,
    title: str = "",
    require_subs: bool = False,
) -> tuple[bool, list[str], str]:
    """Evaluate text & subscriber count against config-driven `RuExpertsFilter`."""
    if subs is None:
        if require_subs:
            return False, [], "missing_subscribers"
    elif subs < filter_cfg.min_subs or subs > filter_cfg.max_subs:
        return False, [], "subs_out_of_range"

    combined = "\n".join(part for part in (title, text) if part).strip()
    if is_agency_or_aggregator(text, title=title):
        return False, [], "agency_or_aggregator"

    combined_low = combined.casefold()
    for stop_word in filter_cfg.stop_words:
        if _term_matches_text(stop_word, combined_low):
            return False, [], f"stop_word:{stop_word}"

    matched_niches: list[str] = []
    if filter_cfg.niches:
        for niche in filter_cfg.niches:
            if _term_matches_text(niche, combined_low):
                matched_niches.append(niche)
        if not matched_niches:
            return False, [], "niche_mismatch"

    return True, matched_niches, ""


def _clause_around_span(line: str, start: int, end: int) -> str:
    """Slice local clause inside a line around a single handle mention."""
    left_sep = max(line.rfind("|", 0, start), line.rfind(";", 0, start), line.rfind("•", 0, start))
    right_candidates = [pos for pos in (line.find("|", end), line.find(";", end), line.find("•", end)) if pos != -1]
    right_sep = min(right_candidates) if right_candidates else len(line)
    sub = line[left_sep + 1 : right_sep].strip()
    local_start = max(0, start - (left_sep + 1) - 48)
    local_end = min(len(sub), end - (left_sep + 1) + 32)
    return sub[local_start:local_end]


def _iter_tg_mentions_in_line(line: str) -> list[tuple[str, str]]:
    """Return [(handle, local_clause), ...] from `@handle` and direct `t.me/handle` links."""
    found: list[tuple[str, str]] = []
    for m in _AT_MENTION_RE.finditer(line):
        found.append((m.group(1), _clause_around_span(line, m.start(), m.end())))
    for m in _TME_HANDLE_RE.finditer(line):
        if m.group(1) or m.group(3):
            # `/s/<ch>` or `/<ch>/123` is a channel/post link, not a personal DM contact.
            continue
        found.append((m.group(2), _clause_around_span(line, m.start(), m.end())))
    for m in _TG_RESOLVE_RE.finditer(line):
        found.append((m.group(1), _clause_around_span(line, m.start(), m.end())))
    return found


def extract_personal_tg_username(
    about: str = "",
    pinned_posts: Sequence[str] = (),
    posts: Sequence[str] = (),
    *,
    channel_username: str = "",
) -> str:
    """Extract personal `@username` from channel bio/pinned/posts, rejecting bots/ads/agencies."""
    chan_low = normalize_tg_handle(channel_username).casefold()
    if is_agency_or_aggregator(about):
        return ""

    prioritized: list[tuple[int, str]] = []
    seen_handles: set[str] = set()

    def _scan_block(block: str, *, base_priority: int, require_personal_cue: bool) -> None:
        for raw_line in (block or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            for raw_handle, clause in _iter_tg_mentions_in_line(line):
                handle = normalize_tg_handle(raw_handle)
                if not handle:
                    continue
                low = handle.casefold()
                if low == chan_low or is_bot_handle(low):
                    continue
                if _is_ad_or_manager_handle(low, clause):
                    continue
                has_cue = _PERSONAL_CUE_RE.search(clause) is not None
                if require_personal_cue and not has_cue:
                    continue
                if low not in seen_handles:
                    seen_handles.add(low)
                    score = base_priority + (10 if has_cue else 0)
                    prioritized.append((score, handle))

    _scan_block(about, base_priority=30, require_personal_cue=False)
    for p_post in pinned_posts:
        _scan_block(p_post, base_priority=20, require_personal_cue=False)
    for post in posts:
        _scan_block(post, base_priority=10, require_personal_cue=True)

    if not prioritized:
        return ""
    prioritized.sort(key=lambda pair: -pair[0])
    return prioritized[0][1]


def _extract_external_links(texts: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for block in texts:
        for raw_url in _HTTP_URL_RE.findall(block or ""):
            cleaned = raw_url.rstrip(".,;:!?)\"'>]")
            try:
                parsed = urlparse(cleaned)
            except ValueError:
                continue
            host = (parsed.hostname or "").casefold().removeprefix("www.")
            if not host or "." not in host or host in _SKIP_EXTERNAL_HOSTS:
                continue
            if any(host.endswith("." + skip) for skip in _SKIP_EXTERNAL_HOSTS):
                continue
            key = cleaned.casefold()
            if key not in seen:
                seen.add(key)
                out.append(cleaned)
    return out


def _extract_budget_guard_result(res: Any) -> bool:
    if isinstance(res, tuple) and res:
        return bool(res[0])
    if isinstance(res, Mapping):
        if "paid_allowed" in res:
            return bool(res["paid_allowed"])
        if "allowed" in res:
            return bool(res["allowed"])
        return True
    return bool(res)


def _check_budget(
    budget_guard_fn: Callable[..., Any] | None,
    provider: str,
    endpoint: str,
    estimated_cost: float,
    *,
    conn: Any = None,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    if budget_guard_fn is not None:
        try:
            sig = inspect.signature(budget_guard_fn)
            params = list(sig.parameters.values())
            accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            accepts_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
            names = {p.name for p in params}
            if accepts_var_kw:
                res = budget_guard_fn(provider, estimated_cost, endpoint=endpoint)
            elif {"provider", "estimated_cost", "endpoint"} <= names:
                res = budget_guard_fn(provider=provider, endpoint=endpoint, estimated_cost=estimated_cost)
            elif {"provider", "estimated_cost"} <= names:
                res = budget_guard_fn(provider=provider, estimated_cost=estimated_cost)
            elif accepts_var_pos or len(params) >= 2:
                res = budget_guard_fn(provider, estimated_cost)
            elif len(params) == 1:
                res = budget_guard_fn(provider)
            else:
                res = budget_guard_fn()
            return _extract_budget_guard_result(res)
        except Exception as exc:
            logger.warning("budget_guard_fn check failed (%s): %s", endpoint, exc)
            return False

    if conn is not None:
        try:
            from leadgen.budget_guard import check_and_reserve_budget

            allowed, _reason = check_and_reserve_budget(
                conn,
                provider=provider,
                estimated_cost=estimated_cost,
                cfg=cfg,
            )
            return bool(allowed)
        except ImportError:
            return True
        except Exception as exc:
            logger.warning("budget_guard DB check failed (%s): %s", endpoint, exc)
            return False
    return True


def _record_spend(
    conn: Any,
    *,
    provider: str,
    endpoint: str,
    lead_key: str,
    cost_usd: float,
    hit: bool,
) -> None:
    if conn is None or cost_usd <= 0.0:
        return
    try:
        from leadgen.budget_guard import record_enrichment_spend

        record_enrichment_spend(
            conn,
            provider=provider,
            endpoint=endpoint,
            lead_key=lead_key,
            cost_usd=cost_usd,
            hit=hit,
        )
    except ImportError:
        return
    except Exception as exc:
        logger.debug("budget_guard spend record failed (%s): %s", endpoint, exc)


def _invoke_treg(
    treg_fn: Callable[..., Any] | None,
    endpoint: str,
    *,
    query: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    custom_mode: bool = False,
) -> Any:
    if treg_fn is not None:
        try:
            sig = inspect.signature(treg_fn)
            params = sig.parameters
            accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            kwargs: dict[str, Any] = {}
            if query is not None and (accepts_var_kw or "query" in params):
                kwargs["query"] = query
            if data is not None and (accepts_var_kw or "data" in params):
                kwargs["data"] = data
            return treg_fn(endpoint, **kwargs)
        except Exception as exc:
            logger.debug("Injected treg_fn failed for %s: %s", endpoint, exc)
            return None

    if custom_mode or treg_call is None or check_treg_ready is None:
        return None
    if not check_treg_ready():
        return None
    return treg_call(endpoint, query=query, data=data, timeout=30)


def _invoke_monid(
    monid_fn: Callable[..., Any] | None,
    provider: str,
    endpoint: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    custom_mode: bool = False,
) -> Any:
    if monid_fn is not None:
        try:
            sig = inspect.signature(monid_fn)
            params = sig.parameters
            accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            kwargs: dict[str, Any] = {}
            if query is not None and (accepts_var_kw or "query" in params):
                kwargs["query"] = query
            if body is not None and (accepts_var_kw or "body" in params):
                kwargs["body"] = body
            return monid_fn(provider, endpoint, **kwargs)
        except Exception as exc:
            logger.debug("Injected monid_fn failed for %s %s: %s", provider, endpoint, exc)
            return None

    if custom_mode or run_monid is None or check_monid_ready is None:
        return None
    if not check_monid_ready():
        return None
    return run_monid(provider, endpoint, query=query, body=body, wait_sec=15, timeout=25)


def _fetch_http_text(url: str, http_get_fn: Callable[..., Any] | None, *, custom_mode: bool) -> str:
    if http_get_fn is not None:
        try:
            resp = http_get_fn(url)
        except Exception as exc:
            logger.debug("Injected http_get_fn failed for %s: %s", url, exc)
            return ""
    elif custom_mode:
        return ""
    else:
        try:
            import requests

            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC-Leadgen/1.0)"},
                timeout=10,
            )
            if getattr(resp, "status_code", 200) >= 400:
                return ""
        except Exception as exc:
            logger.debug("HTTP GET failed for %s: %s", url, exc)
            return ""

    if resp is None:
        return ""
    if isinstance(resp, str):
        text = resp
    elif isinstance(resp, Mapping):
        text = str(resp.get("text") or resp.get("html") or resp.get("body") or "")
    elif hasattr(resp, "text"):
        text = str(getattr(resp, "text") or "")
    else:
        text = str(resp)
    if _CAPTCHA_ANTIBOT_RE.search(text):
        logger.info("Anti-bot/CAPTCHA gate encountered at %s; skipping without bypass", url)
        return ""
    return text


def _unwrap_data_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    for key in ("output", "result", "data", "channel_info"):
        sub = payload.get(key)
        if isinstance(sub, Mapping):
            # Recurse once if wrapper contains nested `data`
            inner = sub.get("data")
            if isinstance(inner, Mapping):
                return dict(inner)
            return dict(sub)
    return dict(payload)


def _extract_similar_channel_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_items: list[Any] = payload
    elif isinstance(payload, Mapping):
        data = _unwrap_data_dict(payload)
        raw_items = []
        for key in ("similar", "channels", "items", "results", "list"):
            cand = data.get(key) if key in data else payload.get(key)
            if isinstance(cand, list):
                raw_items = cand
                break
    else:
        return []

    out: list[dict[str, Any]] = []
    for item in raw_items:
        if isinstance(item, str):
            ch = normalize_tg_handle(item)
            if ch:
                out.append({"username": ch})
        elif isinstance(item, Mapping):
            ch = normalize_tg_handle(
                str(item.get("username") or item.get("channel") or item.get("handle") or item.get("url") or "")
            )
            if ch:
                entry = dict(item)
                entry["username"] = ch
                out.append(entry)
    return out


def _extract_channel_info_fields(payload: Any, fallback_handle: str) -> dict[str, Any] | None:
    if not payload:
        return None
    if isinstance(payload, Mapping):
        if payload.get("ok") is False or payload.get("is_channel") is False:
            return None
        if str(payload.get("type") or "").strip().casefold() == "user":
            return None
    data = _unwrap_data_dict(payload)
    if not data:
        return None
    if data.get("is_channel") is False or str(data.get("type") or "").strip().casefold() == "user":
        return None

    title = str(data.get("title") or data.get("name") or "").strip()
    about = str(data.get("description") or data.get("about") or data.get("bio") or "").strip()
    pinned_raw = data.get("pinned_post") or data.get("pinned_message")
    pinned = ""
    if isinstance(pinned_raw, Mapping):
        pinned = str(pinned_raw.get("text") or pinned_raw.get("message") or "").strip()
    elif isinstance(pinned_raw, str):
        pinned = pinned_raw.strip()

    counters = data.get("counters") if isinstance(data.get("counters"), Mapping) else {}
    subs = None
    for raw_sub in (
        data.get("subscribers"),
        data.get("subscribers_count"),
        data.get("participants_count"),
        data.get("members_count"),
        data.get("members"),
        counters.get("subscribers"),
    ):
        subs = parse_subscriber_count(raw_sub)
        if subs is not None:
            break

    username = normalize_tg_handle(str(data.get("username") or fallback_handle)) or fallback_handle
    if not any((title, about, pinned, subs is not None)):
        return None
    return {
        "username": username,
        "title": title or username,
        "about": about,
        "pinned_post": pinned,
        "subscribers": subs,
    }


def _extract_posts_from_monid(payload: Any, limit: int) -> tuple[list[str], list[str]]:
    if not payload:
        return [], []
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, Mapping):
        containers: list[Any] = [payload]
        for key in ("output", "result", "data"):
            sub = payload.get(key)
            if isinstance(sub, Mapping):
                containers.append(sub)
        for container in containers:
            for key in ("posts", "messages", "items", "results", "rows", "list"):
                val = container.get(key)
                if isinstance(val, list):
                    candidates = val
                    break
            if candidates:
                break

    pinned: list[str] = []
    regular: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            txt = item.strip()
            is_pinned = False
        elif isinstance(item, Mapping):
            txt = str(item.get("text") or item.get("message") or item.get("content") or "").strip()
            is_pinned = bool(item.get("pinned") or item.get("is_pinned"))
        else:
            continue
        if not txt:
            continue
        if is_pinned:
            pinned.append(txt)
        else:
            regular.append(txt)
        if len(pinned) + len(regular) >= limit:
            break
    return pinned, regular


def _parse_tme_channel_html(html_text: str, limit: int) -> tuple[dict[str, Any], list[str]]:
    """Parse public `https://t.me/s/<channel>` HTML into channel info + post texts."""
    if not html_text:
        return {}, []
    info: dict[str, Any] = {}
    posts: list[str] = []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        BeautifulSoup = None

    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_text, "html.parser")
        title_node = soup.select_one(".tgme_channel_info_header_title") or soup.select_one(".tgme_page_title")
        if title_node:
            info["title"] = title_node.get_text(" ", strip=True)
        desc_node = soup.select_one(".tgme_channel_info_description") or soup.select_one(".tgme_page_description")
        if desc_node:
            info["about"] = desc_node.get_text("\n", strip=True)
        for counter_node in soup.select(".tgme_channel_info_counter, .tgme_page_extra"):
            txt = counter_node.get_text(" ", strip=True)
            if "subscriber" in txt.lower() or "подпис" in txt.lower() or re.search(r"^\d[\d\s.,]*[kKmM]?", txt):
                parsed_subs = parse_subscriber_count(txt)
                if parsed_subs is not None:
                    info["subscribers"] = parsed_subs
                    break
        for node in soup.select(".tgme_widget_message_text"):
            txt = node.get_text(" ", strip=True)
            if txt:
                posts.append(txt)
        return info, posts[-limit:] if limit > 0 else []

    m_desc = re.search(
        r'class="[^"]*tgme_(?:channel_info|page)_description[^"]*"[^>]*>(.*?)</div>',
        html_text,
        re.I | re.S,
    )
    if m_desc:
        info["about"] = html_mod.unescape(re.sub(r"<[^>]+>", " ", m_desc.group(1))).strip()
    for m_msg in re.finditer(r'class="[^"]*\btgme_widget_message_text\b[^"]*"[^>]*>(.*?)</div>', html_text, re.I | re.S):
        txt = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", m_msg.group(1))).split())
        if txt:
            posts.append(txt)
    return info, posts[-limit:] if limit > 0 else []


def _compute_ru_expert_icp_score(
    *,
    matched_niches: Sequence[str],
    subs: int | None,
    has_product_signal: bool,
    has_channel: bool,
) -> int:
    score = 72
    if matched_niches:
        score += min(12, 6 * len(matched_niches))
    if subs is not None and 2000 <= subs <= 35000:
        score += 8
    elif subs is not None:
        score += 4
    if has_product_signal:
        score += 5
    if has_channel:
        score += 3
    return min(98, score)


def build_ru_expert_row(
    *,
    username: str,
    name: str,
    bio: str,
    discovery_channel: str,
    matched_niches: Sequence[str] = (),
    their_angle: str = "",
    channel_username: str = "",
    channel_title: str = "",
    subscribers: int | None = None,
    telegram_user_id: int | None = None,
    instagram_handle: str = "",
    website: str = "",
    links: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a canonical RU-expert lead dict with strict Airtable isolation."""
    clean_username = normalize_tg_handle(username)
    if not clean_username:
        raise ValueError(f"Invalid personal Telegram username: {username!r}")

    clean_channel = normalize_tg_handle(channel_username)
    clean_ig = normalize_ig_handle(instagram_handle)
    all_links: list[str] = [f"https://t.me/{clean_username}"]
    if clean_channel and clean_channel.casefold() != clean_username.casefold():
        all_links.append(f"https://t.me/{clean_channel}")
    if clean_ig:
        all_links.append(f"https://www.instagram.com/{clean_ig}/")
    if website:
        all_links.append(website)
    for link in links:
        val = str(link or "").strip()
        if val:
            all_links.append(val)
    dedup_links = list(dict.fromkeys(all_links))

    display_name = " ".join(str(name or "").split()).strip() or channel_title or clean_username
    company_val = " ".join(str(channel_title or display_name).split()).strip()
    clean_bio = " ".join(str(bio or "").split()).strip()[:500]
    clean_angle = " ".join(str(their_angle or clean_bio).split()).strip()[:320]
    has_product_signal = bool(_RU_EXPERT_SIGNAL_RE.search(f"{display_name}\n{clean_bio}\n{clean_angle}"))
    icp_score = _compute_ru_expert_icp_score(
        matched_niches=matched_niches,
        subs=subscribers,
        has_product_signal=has_product_signal,
        has_channel=bool(clean_channel),
    )

    return {
        "id": telegram_user_id,
        "lead_key": f"ru_expert:{clean_username.casefold()}",
        "username": clean_username,
        "telegram_username": clean_username,
        "telegram_user_id": telegram_user_id,
        "name": display_name,
        "company": company_val,
        "bio": clean_bio,
        "what_they_do": clean_bio or clean_angle,
        "their_angle": clean_angle,
        "category": matched_niches[0] if matched_niches else "РФ-эксперт",
        "matched_niches": list(matched_niches),
        "channel_username": clean_channel,
        "channel_title": channel_title or company_val,
        "subscribers": subscribers,
        "instagram_handle": clean_ig,
        "instagram_url": f"https://www.instagram.com/{clean_ig}/" if clean_ig else "",
        "website": website,
        "project_url": website or (f"https://t.me/{clean_channel}" if clean_channel else ""),
        "links": dedup_links,
        "discovery_channel": discovery_channel,
        "segment": "ru_expert",
        "language": "ru",
        "exclude_from_airtable": 1,
        "source": "ru_experts",
        "lead_source": "ru_experts",
        "provider": "telegram",
        "icp_score": icp_score,
        "icp_match": True,
    }


def discover_ru_experts_from_channels(
    seed_channels: Sequence[str],
    *,
    filter_cfg: RuExpertsFilter | None = None,
    include_seeds: bool = True,
    max_similar_per_seed: int = 20,
    max_channels: int = 100,
    posts_limit: int = 15,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    budget_guard_fn: Callable[..., Any] | None = None,
    conn: Any = None,
    cfg: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Channel 1: Expand `seed_channels` via treg similar-channels/info + monid posts."""
    active_filter = filter_cfg or filter_from_config(cfg)
    custom_mode = any(fn is not None for fn in (treg_fn, monid_fn, http_get_fn, budget_guard_fn))

    normalized_seeds: list[str] = []
    seen_seeds: set[str] = set()
    for raw_seed in seed_channels or ():
        ch = normalize_tg_handle(raw_seed)
        if ch and ch.casefold() not in seen_seeds:
            seen_seeds.add(ch.casefold())
            normalized_seeds.append(ch)
    if not normalized_seeds:
        return []

    candidate_order: list[str] = []
    candidate_inline: dict[str, dict[str, Any]] = {}
    seen_candidates: set[str] = set()

    def _enqueue_candidate(ch_handle: str, inline_meta: dict[str, Any] | None = None) -> None:
        key = ch_handle.casefold()
        if key not in seen_candidates:
            seen_candidates.add(key)
            candidate_order.append(ch_handle)
        if inline_meta:
            existing = candidate_inline.get(key, {})
            merged = {**existing, **inline_meta}
            candidate_inline[key] = merged

    if include_seeds:
        for seed in normalized_seeds:
            _enqueue_candidate(seed)

    treg_halted = False
    monid_halted = False
    for seed in normalized_seeds:
        if len(candidate_order) >= max_channels or treg_halted:
            break
        if not _check_budget(
            budget_guard_fn,
            "treg",
            "tikhub.x.telegram-web-fetch-similar-channels",
            COST_TREG_SIMILAR_CHANNELS_USD,
            conn=conn,
            cfg=cfg,
        ):
            treg_halted = True
            break
        sim_payload = _invoke_treg(
            treg_fn,
            "tikhub.x.telegram-web-fetch-similar-channels",
            query={"channel": seed},
            custom_mode=custom_mode,
        )
        _record_spend(
            conn,
            provider="treg",
            endpoint="tikhub.x.telegram-web-fetch-similar-channels",
            lead_key=f"ru_expert_seed:{seed}",
            cost_usd=COST_TREG_SIMILAR_CHANNELS_USD,
            hit=bool(sim_payload),
        )
        for entry in _extract_similar_channel_entries(sim_payload)[:max_similar_per_seed]:
            _enqueue_candidate(entry["username"], entry)
            if len(candidate_order) >= max_channels:
                break

    rows: list[dict[str, Any]] = []
    seen_personal_users: set[str] = set()

    for ch in candidate_order[:max_channels]:
        inline = candidate_inline.get(ch.casefold())
        info: dict[str, Any] | None = None

        if not treg_halted:
            if _check_budget(
                budget_guard_fn,
                "treg",
                "tikhub.x.telegram-web-fetch-channel-info",
                COST_TREG_CHANNEL_INFO_USD,
                conn=conn,
                cfg=cfg,
            ):
                info_payload = _invoke_treg(
                    treg_fn,
                    "tikhub.x.telegram-web-fetch-channel-info",
                    query={"channel": ch},
                    custom_mode=custom_mode,
                )
                info = _extract_channel_info_fields(info_payload, ch)
                _record_spend(
                    conn,
                    provider="treg",
                    endpoint="tikhub.x.telegram-web-fetch-channel-info",
                    lead_key=f"ru_expert_ch:{ch}",
                    cost_usd=COST_TREG_CHANNEL_INFO_USD,
                    hit=info is not None,
                )
            else:
                treg_halted = True

        if info is None and inline:
            info = _extract_channel_info_fields(inline, ch)

        html_posts: list[str] = []
        if info is None and http_get_fn is not None:
            html_txt = _fetch_http_text(f"https://t.me/s/{ch}", http_get_fn, custom_mode=custom_mode)
            html_info, html_posts = _parse_tme_channel_html(html_txt, posts_limit)
            if html_info:
                info = _extract_channel_info_fields(html_info, ch)

        if info is None:
            continue

        ch_title = info["title"]
        ch_about = info["about"]
        ch_subs = info["subscribers"]
        pinned_posts: list[str] = [info["pinned_post"]] if info.get("pinned_post") else []

        # Pre-filter on subscribers, agencies/aggregators, and stop_words before fetching posts.
        if ch_subs is not None and (ch_subs < active_filter.min_subs or ch_subs > active_filter.max_subs):
            continue
        if is_agency_or_aggregator(ch_about, title=ch_title):
            continue
        pre_low = f"{ch_title}\n{ch_about}".casefold()
        if any(_term_matches_text(sw, pre_low) for sw in active_filter.stop_words):
            continue

        regular_posts: list[str] = []
        if not monid_halted:
            if _check_budget(
                budget_guard_fn,
                "monid",
                "/telegram/posts",
                COST_MONID_TG_POSTS_USD,
                conn=conn,
                cfg=cfg,
            ):
                posts_payload = _invoke_monid(
                    monid_fn,
                    "litescrape",
                    "/telegram/posts",
                    query={"channel": ch},
                    custom_mode=custom_mode,
                )
                m_pinned, m_regular = _extract_posts_from_monid(posts_payload, posts_limit)
                _record_spend(
                    conn,
                    provider="monid",
                    endpoint="/telegram/posts",
                    lead_key=f"ru_expert_ch:{ch}",
                    cost_usd=COST_MONID_TG_POSTS_USD,
                    hit=bool(m_pinned or m_regular),
                )
                pinned_posts.extend(m_pinned)
                regular_posts.extend(m_regular)
            else:
                monid_halted = True

        if not pinned_posts and not regular_posts:
            if html_posts:
                regular_posts = html_posts
            elif http_get_fn is not None:
                html_txt = _fetch_http_text(f"https://t.me/s/{ch}", http_get_fn, custom_mode=custom_mode)
                _, parsed_posts = _parse_tme_channel_html(html_txt, posts_limit)
                regular_posts.extend(parsed_posts)

        combined_text = "\n".join(
            part for part in [ch_about, *pinned_posts, *regular_posts] if part
        )
        ok, matched_niches, _reason = matches_ru_expert_filter(
            combined_text,
            active_filter,
            subs=ch_subs,
            title=ch_title,
            require_subs=False,
        )
        if not ok:
            continue

        personal_username = extract_personal_tg_username(
            ch_about,
            pinned_posts=pinned_posts,
            posts=regular_posts,
            channel_username=ch,
        )
        if not personal_username:
            continue

        user_key = personal_username.casefold()
        if user_key in seen_personal_users:
            continue
        seen_personal_users.add(user_key)

        external_links = _extract_external_links([ch_about, *pinned_posts, *regular_posts])
        website = external_links[0] if external_links else ""
        angle_parts = [p for p in [ch_about, *(pinned_posts[:1] or regular_posts[:1])] if p]
        rows.append(
            build_ru_expert_row(
                username=personal_username,
                name=ch_title,
                bio=ch_about or (pinned_posts[0] if pinned_posts else ""),
                discovery_channel="tg_channels",
                matched_niches=matched_niches,
                their_angle=" | ".join(angle_parts),
                channel_username=ch,
                channel_title=ch_title,
                subscribers=ch_subs,
                website=website,
                links=external_links,
            )
        )

    return rows


def _extract_msg_sender_fields(msg: Any) -> dict[str, Any] | None:
    """Normalize Telethon Message or dict message into sender + text fields."""
    if isinstance(msg, Mapping):
        if msg.get("post"):
            return None
        sender = msg.get("sender") if isinstance(msg.get("sender"), Mapping) else msg
        if sender.get("bot") or sender.get("is_bot") or sender.get("deleted"):
            return None
        sender_id = msg.get("sender_id") or sender.get("id") or sender.get("user_id")
        username = str(sender.get("username") or msg.get("username") or "").strip()
        first = str(sender.get("first_name") or "").strip()
        last = str(sender.get("last_name") or "").strip()
        full_name = str(sender.get("name") or "").strip() or " ".join(p for p in (first, last) if p).strip()
        bio = str(sender.get("bio") or sender.get("about") or msg.get("bio") or "").strip()
        text = str(msg.get("message") or msg.get("text") or "").strip()
        subs = parse_subscriber_count(msg.get("subscribers") or sender.get("subscribers"))
        return {
            "sender_id": int(sender_id) if isinstance(sender_id, int) else None,
            "username": username,
            "name": full_name,
            "bio": bio,
            "text": text,
            "subs": subs,
        }

    if bool(getattr(msg, "post", False)):
        return None
    from_id = getattr(msg, "from_id", None)
    if from_id is not None and from_id.__class__.__name__ in {"PeerChannel", "PeerChat"}:
        return None
    sender = getattr(msg, "sender", None)
    if sender is not None:
        if bool(getattr(sender, "bot", False)) or bool(getattr(sender, "deleted", False)):
            return None
    raw_sid = getattr(msg, "sender_id", None) or (getattr(sender, "id", None) if sender is not None else None)
    sender_id = int(raw_sid) if isinstance(raw_sid, int) else None
    username = str(getattr(sender, "username", None) or getattr(msg, "username", None) or "").strip()
    first = str(getattr(sender, "first_name", None) or "").strip()
    last = str(getattr(sender, "last_name", None) or "").strip()
    full_name = " ".join(p for p in (first, last) if p).strip() or str(getattr(sender, "name", None) or "").strip()
    bio = str(getattr(sender, "bio", None) or getattr(sender, "about", None) or "").strip()
    text = str(getattr(msg, "message", None) or getattr(msg, "text", None) or "").strip()
    return {
        "sender_id": sender_id,
        "username": username,
        "name": full_name,
        "bio": bio,
        "text": text,
        "subs": None,
    }


async def _fetch_chat_messages(client: Any, chat_ref: str, msg_limit: int) -> list[Any]:
    try:
        entity = await client.get_entity(chat_ref)
    except Exception as exc:
        logger.debug("get_entity failed for chat %s: %s", chat_ref, exc)
        return []
    if entity is None:
        return []
    if hasattr(client, "get_messages") and callable(client.get_messages):
        try:
            res = await client.get_messages(entity, limit=msg_limit)
            if isinstance(res, list):
                return res
            if hasattr(res, "messages") and isinstance(res.messages, list):
                return list(res.messages)
            return list(res or [])
        except Exception as exc:
            logger.debug("get_messages failed for chat %s: %s", chat_ref, exc)
            return []
    if hasattr(client, "iter_messages") and callable(client.iter_messages):
        collected: list[Any] = []
        try:
            async for item in client.iter_messages(entity, limit=msg_limit):
                collected.append(item)
        except Exception as exc:
            logger.debug("iter_messages failed for chat %s: %s", chat_ref, exc)
        return collected
    return []


async def _resolve_full_user_bio(client: Any, sender_id: int, cache: dict[int, dict[str, Any]]) -> dict[str, Any]:
    if sender_id in cache:
        return cache[sender_id]
    if client is None or not callable(client):
        return {}
    try:
        from telethon.tl.functions.users import GetFullUserRequest

        full = await client(GetFullUserRequest(sender_id))
        raw_users = getattr(full, "users", None)
        users = list(raw_users) if isinstance(raw_users, (list, tuple)) else []
        user = next((u for u in users if getattr(u, "id", None) == sender_id), users[0] if users else None)
        full_user = getattr(full, "full_user", None)
        raw_uname = getattr(user, "username", None)
        raw_first = getattr(user, "first_name", None)
        raw_last = getattr(user, "last_name", None)
        raw_about = getattr(full_user, "about", None)
        data = {
            "username": raw_uname.strip() if isinstance(raw_uname, str) else "",
            "first_name": raw_first.strip() if isinstance(raw_first, str) else "",
            "last_name": raw_last.strip() if isinstance(raw_last, str) else "",
            "bio": raw_about.strip() if isinstance(raw_about, str) else "",
            "bot": bool(getattr(user, "bot", False)) if user is not None else False,
            "deleted": bool(getattr(user, "deleted", False)) if user is not None else False,
        }
        cache[sender_id] = data
        return data
    except Exception as exc:
        logger.debug("GetFullUserRequest fallback for %s: %s", sender_id, exc)
        cache[sender_id] = {}
        return {}


async def _eval_llm_filter(llm_filter_fn: Callable[[Mapping[str, Any]], Any], candidate: Mapping[str, Any]) -> bool:
    try:
        res = llm_filter_fn(candidate)
        if inspect.isawaitable(res):
            res = await res
        if isinstance(res, Mapping):
            return bool(res.get("fit", res.get("keep", False)))
        return bool(res)
    except Exception as exc:
        logger.warning("llm_filter_fn error: %s", exc)
        return False


async def discover_ru_experts_from_chats(
    seed_chats: Sequence[str],
    *,
    client: Any = None,
    messages_by_chat: Mapping[str, Sequence[Any]] | None = None,
    filter_cfg: RuExpertsFilter | None = None,
    msg_limit: int = 200,
    resolve_full_user: bool = True,
    llm_filter_fn: Callable[[Mapping[str, Any]], Any] | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Channel 2: Discover personal RU experts posting in profile Telegram chats."""
    active_filter = filter_cfg or filter_from_config(cfg)
    full_user_cache: dict[int, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    seen_usernames: set[str] = set()

    for raw_chat in seed_chats or ():
        chat_str = str(raw_chat or "").strip()
        if not chat_str:
            continue
        chat_norm = normalize_tg_handle(chat_str) or chat_str.lstrip("@")

        raw_messages: Sequence[Any] = ()
        if messages_by_chat is not None:
            for key_cand in (chat_str, chat_norm, f"@{chat_norm}", chat_norm.casefold()):
                if key_cand in messages_by_chat:
                    raw_messages = messages_by_chat[key_cand]
                    break
        elif client is not None:
            raw_messages = await _fetch_chat_messages(client, chat_norm, msg_limit)

        if not raw_messages:
            continue

        grouped: dict[str, dict[str, Any]] = {}
        for msg in raw_messages:
            parsed = _extract_msg_sender_fields(msg)
            if not parsed:
                continue
            sid = parsed["sender_id"]
            if (
                resolve_full_user
                and client is not None
                and sid is not None
                and (not parsed["bio"] or not parsed["username"])
            ):
                resolved = await _resolve_full_user_bio(client, sid, full_user_cache)
                if resolved.get("bot") or resolved.get("deleted"):
                    continue
                if resolved.get("username") and not parsed["username"]:
                    parsed["username"] = resolved["username"]
                if resolved.get("bio") and not parsed["bio"]:
                    parsed["bio"] = resolved["bio"]
                if not parsed["name"]:
                    parsed["name"] = " ".join(
                        p for p in (resolved.get("first_name", ""), resolved.get("last_name", "")) if p
                    ).strip()

            clean_user = normalize_tg_handle(parsed["username"])
            if not clean_user or is_bot_handle(clean_user):
                continue
            if _is_ad_or_manager_handle(clean_user, parsed["bio"]):
                continue

            ukey = clean_user.casefold()
            slot = grouped.setdefault(
                ukey,
                {
                    "username": clean_user,
                    "sender_id": sid,
                    "name": parsed["name"],
                    "bio": parsed["bio"],
                    "messages": [],
                    "subs": parsed["subs"],
                },
            )
            if parsed["name"] and not slot["name"]:
                slot["name"] = parsed["name"]
            if parsed["bio"] and not slot["bio"]:
                slot["bio"] = parsed["bio"]
            if parsed["subs"] is not None and slot["subs"] is None:
                slot["subs"] = parsed["subs"]
            if parsed["text"] and parsed["text"] not in slot["messages"]:
                slot["messages"].append(parsed["text"])

        for ukey, author in grouped.items():
            if ukey in seen_usernames:
                continue
            sample_msgs = author["messages"][:5]
            combined_text = "\n".join(part for part in [author["bio"], *sample_msgs] if part)
            if not combined_text.strip():
                continue
            if is_spam_or_freelancer_pitch(combined_text):
                continue

            ok, matched_niches, _reason = matches_ru_expert_filter(
                combined_text,
                active_filter,
                subs=author["subs"],
                title=author["name"],
                require_subs=False,
            )
            if not ok:
                continue

            external_links = _extract_external_links([author["bio"], *sample_msgs])
            has_expert_signal = bool(_RU_EXPERT_SIGNAL_RE.search(combined_text)) or bool(external_links)
            if llm_filter_fn is not None:
                if not await _eval_llm_filter(
                    llm_filter_fn,
                    {
                        "username": author["username"],
                        "name": author["name"],
                        "bio": author["bio"],
                        "messages": sample_msgs,
                        "chat": chat_norm,
                    },
                ):
                    continue
            elif not active_filter.niches and not has_expert_signal:
                continue

            seen_usernames.add(ukey)
            linked_channel = ""
            for m_ch in _TME_HANDLE_RE.finditer(combined_text):
                cand_ch = normalize_tg_handle(m_ch.group(2))
                if cand_ch and cand_ch.casefold() != ukey and not is_bot_handle(cand_ch):
                    linked_channel = cand_ch
                    break

            rows.append(
                build_ru_expert_row(
                    username=author["username"],
                    name=author["name"],
                    bio=author["bio"] or (sample_msgs[0] if sample_msgs else ""),
                    discovery_channel="tg_chats",
                    matched_niches=matched_niches,
                    their_angle=" | ".join(sample_msgs[:2]) or author["bio"],
                    channel_username=linked_channel,
                    subscribers=author["subs"],
                    telegram_user_id=author["sender_id"],
                    website=external_links[0] if external_links else "",
                    links=[*external_links, f"https://t.me/{chat_norm}"],
                )
            )

    return rows


def _unwrap_monid_ig_profile(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    if str(payload.get("status") or "").strip().upper() in {"FAILED", "ERROR", "CANCELLED"}:
        return {}
    for key in ("output", "result", "data"):
        sub = payload.get(key)
        if isinstance(sub, Mapping):
            return dict(sub)
    return dict(payload)


def _has_ig_profile_content(profile: Mapping[str, Any]) -> bool:
    return any(
        bool(profile.get(k))
        for k in ("bio", "bioLinks", "externalUrl", "displayName", "title", "handle")
    )


def _needs_chameleon_ig_fallback(profile: Mapping[str, Any]) -> bool:
    if not _has_ig_profile_content(profile):
        return True
    has_links = bool(profile.get("bioLinks")) or bool(profile.get("externalUrl"))
    has_bio_tg = bool(_extract_tme_targets_from_text(str(profile.get("bio") or "")))
    return not (has_links or has_bio_tg)


def _extract_ig_bio_links(raw_bio_links: Any) -> list[tuple[str, str]]:
    """Return [(url, label), ...] from `bioLinks` (supports strings and dicts)."""
    if not isinstance(raw_bio_links, Iterable) or isinstance(raw_bio_links, (str, bytes)):
        return []
    out: list[tuple[str, str]] = []
    for item in raw_bio_links:
        if isinstance(item, str):
            u = item.strip()
            if u:
                out.append((u, ""))
        elif isinstance(item, Mapping):
            u = str(item.get("url") or item.get("link") or item.get("lynx_url") or "").strip()
            lbl = str(item.get("title") or item.get("text") or "").strip()
            if u:
                out.append((u, lbl))
    return out


def _unwrap_instagram_redirect(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if host == "l.instagram.com":
        q = parse_qs(parsed.query).get("u") or []
        if q:
            return unquote(q[0]).strip()
    return url


def _extract_tme_targets_from_text(text: str, *, label: str = "") -> list[tuple[str, bool]]:
    """Return [(tg_handle, explicit_personal_hint), ...] from text/HTML."""
    raw = (text or "").replace("\\/", "/")
    unescaped = html_mod.unescape(raw)
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def _add(cand: str, explicit_personal: bool) -> None:
        norm = normalize_tg_handle(cand)
        if not norm or is_bot_handle(norm):
            return
        key = norm.casefold()
        if key not in seen:
            seen.add(key)
            out.append((norm, explicit_personal))

    for m in _TME_HANDLE_RE.finditer(unescaped):
        is_channel_post_or_s = bool(m.group(1) or m.group(3))
        clause = _clause_around_span(unescaped, m.start(), m.end())
        explicit_personal = (not is_channel_post_or_s) and (
            _PERSONAL_CUE_RE.search(f"{label} {clause}") is not None
        )
        _add(m.group(2), explicit_personal)

    for m in _TG_RESOLVE_RE.finditer(unescaped):
        clause = _clause_around_span(unescaped, m.start(), m.end())
        explicit_personal = _PERSONAL_CUE_RE.search(f"{label} {clause}") is not None
        _add(m.group(1), explicit_personal)

    for m in _IG_EXPLICIT_TG_MENTION_RE.finditer(unescaped):
        _add(m.group(1), True)

    return out


def _is_multi_link_or_landing_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if not host or "." not in host:
        return False
    if host in _MULTI_LINK_HOSTS:
        return True
    if host in _MAJOR_NON_TAPLINK_HOSTS or any(host.endswith("." + h) for h in _MAJOR_NON_TAPLINK_HOSTS):
        return False
    return True


def _extract_taplink_text_and_tg(html_text: str, base_url: str) -> tuple[str, list[tuple[str, bool]]]:
    """Extract visible text + Telegram links from Taplink / bio landing HTML."""
    if not html_text:
        return "", []
    cleaned_raw = html_mod.unescape(html_text.replace("\\/", "/"))
    targets = _extract_tme_targets_from_text(cleaned_raw)
    visible_text = ""

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        BeautifulSoup = None

    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_text, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            href = urljoin(base_url, html_mod.unescape(str(a_tag.get("href") or "").strip()))
            label = a_tag.get_text(" ", strip=True)
            for h_cand, exp_pers in _extract_tme_targets_from_text(href, label=label):
                if all(existing[0].casefold() != h_cand.casefold() for existing in targets):
                    targets.append((h_cand, exp_pers))
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        visible_text = " ".join(soup.get_text(" ", strip=True).split())
    else:
        visible_text = " ".join(re.sub(r"<[^>]+>", " ", cleaned_raw).split())

    return visible_text[:2000], targets


def discover_ru_experts_from_instagram(
    ig_handles: Sequence[str],
    *,
    filter_cfg: RuExpertsFilter | None = None,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    budget_guard_fn: Callable[..., Any] | None = None,
    conn: Any = None,
    cfg: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Channel 3: IG -> `anyapi /instagram.profile_contact` -> bioLinks/Taplink/t.me -> personal TG."""
    active_filter = filter_cfg or filter_from_config(cfg)
    custom_mode = any(fn is not None for fn in (treg_fn, monid_fn, http_get_fn, budget_guard_fn))

    rows: list[dict[str, Any]] = []
    seen_personal_users: set[str] = set()
    monid_halted = False
    treg_halted = False

    for raw_ig in ig_handles or ():
        if monid_halted:
            break
        ig_handle = normalize_ig_handle(raw_ig)
        if not ig_handle:
            continue

        if not _check_budget(
            budget_guard_fn,
            "monid",
            "/instagram.profile_contact",
            COST_MONID_IG_OCTOPUS_USD,
            conn=conn,
            cfg=cfg,
        ):
            monid_halted = True
            break

        ig_payload = _invoke_monid(
            monid_fn,
            "anyapi",
            "/instagram.profile_contact",
            body={"handle": ig_handle, "route": "octopus"},
            custom_mode=custom_mode,
        )
        profile = _unwrap_monid_ig_profile(ig_payload)
        if _needs_chameleon_ig_fallback(profile):
            if _check_budget(
                budget_guard_fn,
                "monid",
                "/instagram.profile_contact",
                COST_MONID_IG_CHAMELEON_USD,
                conn=conn,
                cfg=cfg,
            ):
                fallback_payload = _invoke_monid(
                    monid_fn,
                    "anyapi",
                    "/instagram.profile_contact",
                    body={"handle": ig_handle, "route": "chameleon"},
                    custom_mode=custom_mode,
                )
                fallback_profile = _unwrap_monid_ig_profile(fallback_payload)
                _record_spend(
                    conn,
                    provider="monid",
                    endpoint="/instagram.profile_contact",
                    lead_key=f"ru_expert_ig:{ig_handle}",
                    cost_usd=COST_MONID_IG_CHAMELEON_USD,
                    hit=_has_ig_profile_content(fallback_profile),
                )
                if _has_ig_profile_content(fallback_profile):
                    profile = {**profile, **fallback_profile}
            else:
                monid_halted = True
                if not _has_ig_profile_content(profile):
                    break

        if not _has_ig_profile_content(profile):
            continue
        if profile.get("private") is True and not any(
            profile.get(k) for k in ("bio", "bioLinks", "externalUrl")
        ):
            continue

        ig_bio = str(profile.get("bio") or "").strip()
        ig_name = str(
            profile.get("displayName") or profile.get("title") or profile.get("fullName") or ig_handle
        ).strip()
        ext_url = _unwrap_instagram_redirect(str(profile.get("externalUrl") or "").strip())
        bio_links = [
            (_unwrap_instagram_redirect(u), lbl)
            for u, lbl in _extract_ig_bio_links(profile.get("bioLinks"))
        ]

        tg_targets: list[tuple[str, bool]] = []
        seen_tg_targets: set[str] = set()

        def _add_targets(items: Sequence[tuple[str, bool]]) -> None:
            for cand, exp_pers in items:
                key = cand.casefold()
                if key not in seen_tg_targets:
                    seen_tg_targets.add(key)
                    tg_targets.append((cand, exp_pers))

        _add_targets(_extract_tme_targets_from_text(ig_bio))
        if ext_url:
            _add_targets(_extract_tme_targets_from_text(ext_url))
        for link_url, link_lbl in bio_links:
            _add_targets(_extract_tme_targets_from_text(link_url, label=link_lbl))

        taplink_texts: list[str] = []
        landing_urls: list[str] = []
        for url_cand, _lbl in [(ext_url, ""), *bio_links]:
            if url_cand and _is_multi_link_or_landing_url(url_cand) and url_cand not in landing_urls:
                landing_urls.append(url_cand)

        for landing_url in landing_urls[:3]:
            html_txt = _fetch_http_text(landing_url, http_get_fn, custom_mode=custom_mode)
            if not html_txt:
                continue
            vis_txt, found_targets = _extract_taplink_text_and_tg(html_txt, landing_url)
            if vis_txt:
                taplink_texts.append(vis_txt)
            _add_targets(found_targets)

        if not tg_targets:
            continue

        for tg_cand, explicit_personal in tg_targets:
            ch_info: dict[str, Any] | None = None
            if not treg_halted:
                if _check_budget(
                    budget_guard_fn,
                    "treg",
                    "tikhub.x.telegram-web-fetch-channel-info",
                    COST_TREG_CHANNEL_INFO_USD,
                    conn=conn,
                    cfg=cfg,
                ):
                    info_payload = _invoke_treg(
                        treg_fn,
                        "tikhub.x.telegram-web-fetch-channel-info",
                        query={"channel": tg_cand},
                        custom_mode=custom_mode,
                    )
                    ch_info = _extract_channel_info_fields(info_payload, tg_cand)
                    _record_spend(
                        conn,
                        provider="treg",
                        endpoint="tikhub.x.telegram-web-fetch-channel-info",
                        lead_key=f"ru_expert_ig_tg:{tg_cand}",
                        cost_usd=COST_TREG_CHANNEL_INFO_USD,
                        hit=ch_info is not None,
                    )
                else:
                    treg_halted = True

            html_posts_fallback: list[str] = []
            if ch_info is None and http_get_fn is not None and not explicit_personal:
                tme_html = _fetch_http_text(f"https://t.me/s/{tg_cand}", http_get_fn, custom_mode=custom_mode)
                parsed_info, html_posts_fallback = _parse_tme_channel_html(tme_html, 10)
                if parsed_info:
                    ch_info = _extract_channel_info_fields(parsed_info, tg_cand)

            if ch_info is not None:
                ch_title = ch_info["title"]
                ch_about = ch_info["about"]
                ch_subs = ch_info["subscribers"]
                pinned_posts: list[str] = [ch_info["pinned_post"]] if ch_info.get("pinned_post") else []
                regular_posts: list[str] = []

                personal_username = extract_personal_tg_username(
                    ch_about,
                    pinned_posts=pinned_posts,
                    channel_username=tg_cand,
                )
                if not personal_username and not monid_halted:
                    if _check_budget(
                        budget_guard_fn,
                        "monid",
                        "/telegram/posts",
                        COST_MONID_TG_POSTS_USD,
                        conn=conn,
                        cfg=cfg,
                    ):
                        posts_payload = _invoke_monid(
                            monid_fn,
                            "litescrape",
                            "/telegram/posts",
                            query={"channel": tg_cand},
                            custom_mode=custom_mode,
                        )
                        m_pinned, m_regular = _extract_posts_from_monid(posts_payload, 10)
                        _record_spend(
                            conn,
                            provider="monid",
                            endpoint="/telegram/posts",
                            lead_key=f"ru_expert_ig_tg:{tg_cand}",
                            cost_usd=COST_MONID_TG_POSTS_USD,
                            hit=bool(m_pinned or m_regular),
                        )
                        pinned_posts.extend(m_pinned)
                        regular_posts.extend(m_regular)
                    else:
                        monid_halted = True
                if not personal_username and not pinned_posts and not regular_posts and html_posts_fallback:
                    regular_posts.extend(html_posts_fallback)
                if not personal_username:
                    personal_username = extract_personal_tg_username(
                        ch_about,
                        pinned_posts=pinned_posts,
                        posts=regular_posts,
                        channel_username=tg_cand,
                    )

                if not personal_username:
                    continue

                combined_text = "\n".join(
                    part for part in [ig_bio, *taplink_texts, ch_about, *pinned_posts, *regular_posts] if part
                )
                ok, matched_niches, _reason = matches_ru_expert_filter(
                    combined_text,
                    active_filter,
                    subs=ch_subs,
                    title=f"{ig_name} {ch_title}",
                    require_subs=False,
                )
                if not ok:
                    continue

                user_key = personal_username.casefold()
                if user_key in seen_personal_users:
                    continue
                seen_personal_users.add(user_key)

                rows.append(
                    build_ru_expert_row(
                        username=personal_username,
                        name=ig_name or ch_title,
                        bio=ch_about or ig_bio,
                        discovery_channel="ig_to_tg",
                        matched_niches=matched_niches,
                        their_angle=ig_bio or ch_about,
                        channel_username=tg_cand,
                        channel_title=ch_title,
                        subscribers=ch_subs,
                        instagram_handle=ig_handle,
                        website=landing_urls[0] if landing_urls else ext_url,
                        links=[u for u in (ext_url, *(u for u, _ in bio_links)) if u],
                    )
                )
                break

            # Fallback only when IG/Taplink explicitly gave a personal TG contact (not a channel).
            if explicit_personal and not _is_ad_or_manager_handle(tg_cand, ig_bio):
                combined_text = "\n".join(part for part in [ig_bio, *taplink_texts] if part)
                ok, matched_niches, _reason = matches_ru_expert_filter(
                    combined_text,
                    active_filter,
                    subs=None,
                    title=ig_name,
                    require_subs=False,
                )
                if not ok:
                    continue
                user_key = tg_cand.casefold()
                if user_key in seen_personal_users:
                    continue
                seen_personal_users.add(user_key)
                rows.append(
                    build_ru_expert_row(
                        username=tg_cand,
                        name=ig_name,
                        bio=ig_bio,
                        discovery_channel="ig_to_tg",
                        matched_niches=matched_niches,
                        their_angle=ig_bio,
                        instagram_handle=ig_handle,
                        website=landing_urls[0] if landing_urls else ext_url,
                        links=[u for u in (ext_url, *(u for u, _ in bio_links)) if u],
                    )
                )
                break

    return rows


async def discover_ru_experts(
    *,
    cfg: Mapping[str, Any] | None = None,
    filter_cfg: RuExpertsFilter | None = None,
    seed_channels: Sequence[str] | None = None,
    seed_chats: Sequence[str] | None = None,
    ig_handles: Sequence[str] | None = None,
    client: Any = None,
    messages_by_chat: Mapping[str, Sequence[Any]] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    budget_guard_fn: Callable[..., Any] | None = None,
    llm_filter_fn: Callable[[Mapping[str, Any]], Any] | None = None,
    conn: Any = None,
    daily_limit: int | None = None,
) -> dict[str, Any]:
    """Run all 3 RU-expert discovery channels, deduplicate, and enforce Airtable isolation."""
    raw_sec = cfg.get("ru_experts") if isinstance(cfg, Mapping) and isinstance(cfg.get("ru_experts"), Mapping) else cfg
    sec = raw_sec if isinstance(raw_sec, Mapping) else {}
    active_filter = filter_cfg or filter_from_config(cfg)

    resolved_channels = list(seed_channels) if seed_channels is not None else list(sec.get("seed_channels") or ())
    resolved_chats = list(seed_chats) if seed_chats is not None else list(sec.get("seed_chats") or ())
    ig_enabled = bool(sec.get("ig_to_tg_enabled", True))
    resolved_igs = list(ig_handles) if ig_handles is not None else (list(sec.get("ig_handles") or ()) if ig_enabled else [])

    effective_limit = daily_limit
    if effective_limit is None and "daily_limit" in sec:
        try:
            effective_limit = int(sec["daily_limit"])
        except (TypeError, ValueError):
            effective_limit = None

    ch_rows = (
        discover_ru_experts_from_channels(
            resolved_channels,
            filter_cfg=active_filter,
            treg_fn=treg_fn,
            monid_fn=monid_fn,
            http_get_fn=http_get_fn,
            budget_guard_fn=budget_guard_fn,
            conn=conn,
            cfg=cfg,
        )
        if resolved_channels
        else []
    )
    chat_rows = (
        await discover_ru_experts_from_chats(
            resolved_chats,
            client=client,
            messages_by_chat=messages_by_chat,
            filter_cfg=active_filter,
            llm_filter_fn=llm_filter_fn,
            cfg=cfg,
        )
        if resolved_chats
        else []
    )
    ig_rows = (
        discover_ru_experts_from_instagram(
            resolved_igs,
            filter_cfg=active_filter,
            treg_fn=treg_fn,
            monid_fn=monid_fn,
            http_get_fn=http_get_fn,
            budget_guard_fn=budget_guard_fn,
            conn=conn,
            cfg=cfg,
        )
        if resolved_igs and ig_enabled
        else []
    )

    merged: list[dict[str, Any]] = []
    by_user: dict[str, dict[str, Any]] = {}
    for row in (*ch_rows, *chat_rows, *ig_rows):
        # Re-assert sacred invariants on every emitted row.
        row["segment"] = "ru_expert"
        row["language"] = "ru"
        row["exclude_from_airtable"] = 1
        row["source"] = "ru_experts"
        row["lead_source"] = "ru_experts"
        row["provider"] = "telegram"

        ukey = str(row.get("username") or "").casefold()
        if not ukey:
            continue
        existing = by_user.get(ukey)
        if existing is None:
            by_user[ukey] = row
            merged.append(row)
        else:
            existing["links"] = list(dict.fromkeys([*(existing.get("links") or ()), *(row.get("links") or ())]))
            if not existing.get("instagram_handle") and row.get("instagram_handle"):
                existing["instagram_handle"] = row["instagram_handle"]
                existing["instagram_url"] = row["instagram_url"]
            if not existing.get("channel_username") and row.get("channel_username"):
                existing["channel_username"] = row["channel_username"]
                existing["channel_title"] = row["channel_title"]
                existing["subscribers"] = row["subscribers"]

    if effective_limit is not None and effective_limit > 0:
        merged = merged[:effective_limit]

    return {
        "status": "ok",
        "rows": merged,
        "stats": {
            "channels_found": len(ch_rows),
            "chats_found": len(chat_rows),
            "ig_to_tg_found": len(ig_rows),
            "total_unique": len(merged),
        },
    }


def _load_cli_config(config_path: str | None = None) -> dict[str, Any]:
    if config_path:
        p = Path(config_path)
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
        return {}
    try:
        from app.leadgen_settings import get_leadgen_config

        return get_leadgen_config()
    except Exception as exc:
        logger.debug("Fallback to JSON file config in ru_experts CLI: %s", exc)
    env_path = os.environ.get("LEADGEN_CONFIG_PATH") or str(
        Path(__file__).resolve().parents[1] / "data" / "leadgen_config.json"
    )
    p = Path(env_path)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discover RU experts across TG channels, chats, and IG->TG.")
    ap.add_argument("--config", default="", help="Optional path to leadgen_config.json")
    ap.add_argument("--output", default="", help="Optional JSONL output path")
    ap.add_argument("--limit", type=int, default=0, help="Override daily_limit (0 = use config)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    cfg = _load_cli_config(args.config or None)
    res = asyncio.run(
        discover_ru_experts(
            cfg=cfg,
            daily_limit=args.limit if args.limit > 0 else None,
        )
    )
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for row in res["rows"]:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(res["stats"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
