#!/usr/bin/env python3
"""Canonical SoftUGC outreach copy engine (multi-profile deterministic + LLM-guided sequences)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
import re
import ssl
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlparse

SOFTUGC_PITCH_TEXT = (
    "We run SoftUGC. It's organic distribution using AI short-form video and a network "
    "of accounts we own. Works as a growth arm that runs itself: 183.5M views in 90 days, "
    "no shoots, traffic costs 13 to 40x less than standard clippers."
)
REVSHARE_OFFER_TEXT = (
    "We work on revshare or CPA and you only cover the production costs. "
    "Want to see 3 example reels for your brand, to check how it could work for you?"
)
WHALE_PITCH_TEXT = (
    "We run SoftUGC: full-scale AI short-form mass production on our owned account network, "
    "delivering 183.5M views in 90 days and 1 500+ reels/mo with zero shoots, "
    "13-40x cheaper than standard clippers."
)
WHALE_CTA_TEMPLATE = "Want to see 3 demo reels for {brand}?"
WHALE_OFFER_TEXT = (
    f"{WHALE_PITCH_TEXT} Want to see 3 demo reels for your brand?"
)
WHALE_WHATSAPP_STEP3_TEXT = (
    "Hey, bumping this once in case it got buried. "
    "Happy to send 3 quick demo reels for {brand} whenever you want to take a look."
)
VLAD_STEP2_TEMPLATE = (
    "Hey, regarding last message, we could bring you 500K-2M reach to your targeted audience "
    "(which equals highly qualified {audience})\n\n"
    "So if you’re down just lmk if you wanna see the demo reels and I’ll send them to you, "
    "so we could break the ice here"
)
VLAD_STEP3_TEXT = "You’re doing me dirty mate... :((("
VLAD_STEP3_IMAGE_URL = "/media/outreach/4fa85f083f9e2911.jpg"
DEFAULT_VACANCY_SHOWCASE_URL = "https://tmxfy.github.io/"

VALID_AUDIENCES = frozenset({"buyers", "students", "users", "players", "listeners"})
SUPPORTED_COPY_PROFILES = frozenset(
    {
        "whale_mass",
        "vacancy_ru",
        "vacancy_en",
        "ru_expert",
        "social_manual_bridge",
        "ofm",
        "b2b_legacy",
    }
)
PROFILE_METADATA: dict[str, dict[str, Any]] = {
    "whale_mass": {"language": "en", "delays": (0, 48, 96), "allows_step3_image": True},
    "vacancy_ru": {"language": "ru", "delays": (0, 24, 72), "allows_step3_image": False},
    "vacancy_en": {"language": "en", "delays": (0, 24, 72), "allows_step3_image": False},
    "ru_expert": {"language": "ru", "delays": (0, 48), "allows_step3_image": False},
    "social_manual_bridge": {"language": None, "delays": (0,), "allows_step3_image": False},
    "ofm": {"language": "en", "delays": (0, 24, 24), "allows_step3_image": True},
    "b2b_legacy": {"language": "en", "delays": (0, 24, 24), "allows_step3_image": True},
}

_LEGACY_BRAND_RE = re.compile(r"\bCreator\s*OS\b", re.I)
_REVSHARE_CPA_RE = re.compile(r"\b(?:rev[- ]?share|revenue[- ]?share|cpa)\b", re.I)
_URL_STRIP_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+|@[A-Za-z0-9_]+", re.I)
_NEUTRAL_TECH_TOKENS_RE = re.compile(
    r"\b(?:SoftUGC|AI|UGC|WhatsApp|Telegram|CPM|CTR|KPI|ROI|B2B|D2C|SaaS|IG|TikTok|Shorts|Reels|YouTube|Whop|Content\s+Rewards|Tim\s+RbM|RbM|faceless)\b",
    re.I,
)


@dataclass(frozen=True)
class OutreachStepSpec:
    """Single step definition in an outreach cadence."""

    step_order: int
    delay_hours: int
    template_text: str
    image_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_order": self.step_order,
            "delay_hours": self.delay_hours,
            "template_text": self.template_text,
            "image_url": self.image_url,
        }


@dataclass(frozen=True)
class OutreachSequence:
    """Complete resolved outreach sequence across Step 1..3 for a lead/campaign."""

    copy_profile: str
    language: str
    provider: str
    step1_text: str
    step2_text: str
    step3_text: str
    step3_image_url: str | None
    audience: str
    steps: tuple[OutreachStepSpec, ...]
    used_llm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "copy_profile": self.copy_profile,
            "language": self.language,
            "provider": self.provider,
            "first_touch": self.step1_text,
            "step1_text": self.step1_text,
            "hook": self.step1_text,
            "followup_1": self.step2_text,
            "step2_text": self.step2_text,
            "followup_2": self.step3_text,
            "step3_text": self.step3_text,
            "step3_image_url": self.step3_image_url,
            "audience": self.audience,
            "used_llm": self.used_llm,
            "steps": [s.to_dict() for s in self.steps],
        }

_NON_PERSON_RE = re.compile(
    r"\b(?:"
    r"labs|studio|group|capital|network|exchange|pay|wallet|chain|protocol|finance|"
    r"marketing|partnership|community|media|tech|coin|token|dao|ai|io|xyz|app|bot|soft|crm|"
    r"founder|ceo|cto|cmo|bd|head|lead|dev|growth|kol|web3|crypto|defi|nft|tg|ws|official|support|"
    r"team|agency|global|ventures|digital|club|hub|game|games|gaming|sports|play|store|shop|cloud|data|"
    r"hi|hey|hello|there|admin|user|owner|manager|sales|info|contact|product|company"
    r")\b",
    re.I,
)
_PLACEHOLDER_RE = re.compile(
    r"(?:\[[^\]]*\]|\{[^}]*\}|u\+[0-9a-f]{4}|\b(?:hi|hey|hello)\s+there\b)",
    re.I,
)
_HN_PREFIX_RE = re.compile(r"^(?:show|ask|launch|tell)\s+hn\s*:\s*", re.I)
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_GENERIC_SITE_BASES = frozenset(
    {
        "t",
        "x",
        "twitter",
        "linkedin",
        "itunes",
        "apps",
        "play",
        "github",
        "gitlab",
        "npmjs",
        "pypi",
        "medium",
        "substack",
        "youtube",
        "youtu",
        "instagram",
        "facebook",
        "tiktok",
        "discord",
        "notion",
        "linktr",
        "linktree",
        "beacons",
        "carrd",
        "docs",
        "drive",
        "chromewebstore",
        "lu",
        "luma",
        "news",
        "huggingface",
        "calendly",
        "ycombinator",
    }
)
_COMMON_FIRST_NAMES = frozenset(
    {
        "alex", "alexander", "alexey", "andrew", "andrey", "anna", "anton", "artem",
        "ben", "bob", "brian", "chris", "dan", "daniel", "david", "denis", "dima",
        "dmitry", "elena", "emma", "eric", "evgeny", "george", "igor", "ilya", "ivan",
        "jack", "jake", "james", "jane", "jason", "jeff", "jeremy", "john", "josh",
        "julia", "kate", "katya", "kevin", "kirill", "leo", "liam", "luke", "maksim",
        "maria", "mark", "matt", "matthew", "max", "maxim", "michael", "mike", "misha",
        "natalia", "nick", "nikita", "noah", "oleg", "olga", "olya", "pasha", "paul",
        "pavel", "peter", "roman", "ryan", "sam", "sarah", "sasha", "sergey", "simon",
        "sophia", "stanislav", "steve", "tim", "tom", "valeria", "victor", "vlad",
        "vladimir", "will", "william", "yuri",
    }
)


def _extract_personal_first_name(raw_name: str, raw_company: str = "") -> str:
    """Return a clean Latin personal first name, or '' if raw_name is a brand/handle/placeholder."""
    text = (raw_name or "").strip()
    if not text or _PLACEHOLDER_RE.search(text):
        return ""
    if text.startswith("@") or "://" in text or "t.me/" in text.lower():
        return ""
    if re.search(r"\d", text) or "." in text or "_" in text:
        return ""
    comp_clean = re.sub(r"[^a-z0-9]+", "", (raw_company or "").lower())
    name_clean = re.sub(r"[^a-z0-9]+", "", text.lower())
    if comp_clean and name_clean and (name_clean == comp_clean or name_clean in comp_clean):
        return ""
    if _NON_PERSON_RE.search(text):
        return ""
    ascii_text = re.sub(r"[^\x00-\x7F]+", " ", text).strip()
    tokens = [t for t in re.split(r"[\s|/,&\-]+", ascii_text) if t]
    if not tokens or len(tokens) > 3:
        return ""
    first = tokens[0]
    if not re.fullmatch(r"[A-Z][a-z]{1,13}", first):
        return ""
    if _NON_PERSON_RE.search(first):
        return ""
    return first


def _looks_like_person_only(text: str) -> bool:
    """Detect strings that are just a human name ('Anna Bozina', 'Olya M', 'Валерия') rather than a brand."""
    s = (text or "").strip()
    if not s:
        return True
    if _CYRILLIC_RE.search(s):
        return True
    if _NON_PERSON_RE.search(s):
        return False
    tokens = [t for t in re.split(r"\s+", s) if t]
    if len(tokens) == 3 and re.fullmatch(r"[A-Z][a-z]{1,14}", tokens[0]) and re.fullmatch(r"[A-Z]\.?", tokens[1]) and re.fullmatch(r"[A-Z][a-z]{1,16}", tokens[2]):
        return True
    if len(tokens) == 2 and re.fullmatch(r"[A-Z][a-z]{1,14}", tokens[0]) and re.fullmatch(r"[A-Z]\.?|[A-Z][a-z]{1,16}", tokens[1]):
        if tokens[0].lower() in _COMMON_FIRST_NAMES or len(tokens[1].rstrip(".")) == 1:
            return True
    if len(tokens) == 1 and tokens[0].lower() in _COMMON_FIRST_NAMES:
        return True
    return False


def _humanize_slug(slug: str, their_angle: str = "") -> str:
    """Convert lowercase slugs ('dontgetrekt', 'django-hurricane', '311wrapped') into clean titles."""
    raw = (slug or "").strip()
    if not raw:
        return ""
    norm = re.sub(r"[^a-z0-9]", "", raw.lower())
    if their_angle and norm:
        for m in re.finditer(r"\b([A-Z0-9][A-Za-z0-9\.\-]+(?:\s+[A-Z0-9][A-Za-z0-9\.\-]+)?)\b", their_angle[:140]):
            cand = m.group(1).strip()
            if re.sub(r"[^a-z0-9]", "", cand.lower()) == norm:
                return cand
    if "-" in raw or "_" in raw:
        parts = [p for p in re.split(r"[\-_]+", raw) if p]
        return " ".join(p.upper() if p.lower() in {"ai", "io", "ui", "ux", "db", "os", "sdk", "api"} else p[:1].upper() + p[1:] for p in parts)
    m_num = re.fullmatch(r"(\d+)([a-z]{3,})", raw)
    if m_num:
        return f"{m_num.group(1)} {m_num.group(2).capitalize()}"
    known_compounds = {
        "dontgetrekt": "DontGetRekt",
        "looptranscribe": "LoopTranscribe",
        "infinitychat": "InfinityChat",
        "screenstudio": "ScreenStudio",
        "cryptocanal": "CryptoCanal",
    }
    if raw.lower() in known_compounds:
        return known_compounds[raw.lower()]
    if raw == raw.lower() and len(raw) <= 22:
        return raw[:1].upper() + raw[1:]
    return raw


def _brand_from_site(site: str, their_angle: str = "", *, allow_ru_tlds: bool = False) -> str:
    s = (site or "").strip()
    if not s:
        return ""
    host = (urlparse(s if "://" in s else f"https://{s}").hostname or "").lower().removeprefix("www.")
    if not host or (not allow_ru_tlds and host.endswith((".ru", ".su", ".рф", ".xn--p1ai"))):
        return ""
    parts = [p for p in host.split(".") if p]
    if not parts:
        return ""
    base = parts[-2] if len(parts) >= 3 and parts[0] in {"app", "get", "try", "use", "go", "my", "hey", "docs", "join"} else parts[0]
    if not base or base in _GENERIC_SITE_BASES or len(base) < 2:
        return ""
    return _humanize_slug(base, their_angle=their_angle)


_RU_NON_PERSON_FIRST_RE = re.compile(
    r"^(?:школа|агентство|студия|канал|блог|проект|вакансия|ищем|нужен|нужна|нужны|требуется|"
    r"работа|команда|отдел|сервис|клиника|бренд|магазин|онлайн|курс|клуб|академия|сообщество|"
    r"подработка|продюсер|маркетинг|реклама|видео|рилс|контент|smm|hr|ceo|founder)$",
    re.I,
)


def _extract_personal_first_name_ru(raw_name: str, raw_company: str = "") -> str:
    """Extract a clean personal first name (Cyrillic or Latin) for Russian DMs, or ''."""
    text = (raw_name or "").strip()
    if not text or _PLACEHOLDER_RE.search(text):
        return ""
    if text.startswith("@") or "://" in text or "t.me/" in text.lower():
        return ""
    if re.search(r"\d", text) or "." in text or "_" in text:
        return ""
    if _NON_PERSON_RE.search(text):
        return ""
    comp_clean = re.sub(r"[^a-zа-яё0-9]+", "", (raw_company or "").lower())
    name_clean = re.sub(r"[^a-zа-яё0-9]+", "", text.lower())
    if comp_clean and name_clean and (name_clean == comp_clean or name_clean in comp_clean):
        return ""
    tokens = [t for t in re.split(r"[\s|/,&\-]+", text) if t]
    if not tokens or len(tokens) > 3:
        return ""
    first = tokens[0]
    if _RU_NON_PERSON_FIRST_RE.match(first):
        return ""
    if re.fullmatch(r"[А-ЯЁ][а-яё]{1,14}", first) or re.fullmatch(r"[A-Z][a-z]{1,13}", first):
        return first
    return ""


def _looks_like_ru_person_only(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return True
    if _NON_PERSON_RE.search(s):
        return False
    tokens = [t for t in re.split(r"\s+", s) if t]
    if not tokens or len(tokens) > 3:
        return False
    if any(_RU_NON_PERSON_FIRST_RE.match(t) for t in tokens):
        return False
    if len(tokens) == 1 and re.fullmatch(r"[А-ЯЁ][а-яё]{1,14}", tokens[0]):
        return True
    if len(tokens) in (2, 3) and all(re.fullmatch(r"[А-ЯЁ][а-яё]{1,16}\.?", t) for t in tokens):
        return True
    return _looks_like_person_only(s) if not _CYRILLIC_RE.search(s) else False


def clean_brand_name(
    raw_name: str,
    site: str = "",
    their_angle: str = "",
    *,
    allow_cyrillic: bool = False,
) -> str:
    """Extract a clean product/brand name (Latin only by default, or Cyrillic when allow_cyrillic=True)."""
    raw = (raw_name or "").strip()
    raw = _PLACEHOLDER_RE.sub("", raw).strip()
    raw = _HN_PREFIX_RE.sub("", raw).strip()
    raw = re.sub(r"^\d+[\.\)]\s*", "", raw).strip()
    raw = re.sub(r"https?://\S+", "", raw, flags=re.I).strip().strip("/")

    person_checker = _looks_like_ru_person_only if allow_cyrillic else _looks_like_person_only

    # Handle "Jeremy | OpenEden" -> "OpenEden", vs "OpenEden | RWA" -> "OpenEden"
    if "|" in raw:
        pieces = [p.strip() for p in raw.split("|") if p.strip()]
        if len(pieces) >= 2 and person_checker(pieces[0]) and not person_checker(pieces[1]):
            raw = pieces[1]
        elif pieces:
            raw = pieces[0]

    # Strip trailing parenthetical ("ScreenStudio (YC W26)" -> "ScreenStudio")
    raw = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
    # Strip " — subtitle" or " - subtitle" if brand precedes description
    raw = re.split(r"\s+[—–:]\s+|\s+-\s+", raw, maxsplit=1)[0].strip()

    if raw.startswith("@") or "_" in raw or len(raw) > 36 or person_checker(raw):
        raw = ""

    if raw and raw == raw.lower() and " " not in raw:
        raw = _humanize_slug(raw, their_angle=their_angle)

    if not raw:
        raw = _brand_from_site(site, their_angle=their_angle, allow_ru_tlds=allow_cyrillic)

    if not raw and their_angle and not _CYRILLIC_RE.search(their_angle[:60]):
        m = re.search(
            r"\b(?:founder|co-founder|ceo|cmo|creator|head|growth|building)\s+(?:at|of|@)\s+([A-Z][A-Za-z0-9\.\-]{1,22}(?:\s+[A-Z][A-Za-z0-9\.\-]{1,18})?)",
            their_angle,
            flags=re.I,
        )
        if m:
            cand = m.group(1).strip().lstrip("@")
            if not _looks_like_person_only(cand):
                raw = _humanize_slug(cand, their_angle=their_angle)

    if not allow_cyrillic and _CYRILLIC_RE.search(raw):
        return ""
    return raw.strip(" .,;:-/!?")


def _clean_company_for_ping(company: str, site: str = "", name: str = "") -> str:
    """Extract a concise, human-looking company/product name without URLs or placeholders."""
    brand = clean_brand_name(company, site=site)
    if not brand and name and not _extract_personal_first_name(name, ""):
        brand = clean_brand_name(name, site=site)
    if not brand or len(brand) < 2:
        return "your product"
    if len(brand) > 24:
        brand = brand[:24].rstrip()
    return brand


def micro_ping_first_touch(
    name: str,
    company: str,
    category: str = "",
    site: str = "",
    angle: str = "",
) -> str:
    """Short 1-line question (<=120 chars, no links, no case dump) to break Telegram banner blindness."""
    del category, angle
    comp = _clean_company_for_ping(company, site=site, name=name)
    first_name = _extract_personal_first_name(name, comp)
    if first_name:
        msg = (
            f"Hey {first_name}, saw {comp} — are you already running organic "
            f"IG/TikTok reels for it, or mostly focusing on product?"
        )
        if len(msg) > 120:
            msg = f"Hey {first_name}, saw {comp} — running organic IG/TikTok reels yet, or focused on product?"
    else:
        msg = (
            f"Hey, saw {comp} — are you already running organic "
            f"IG/TikTok reels for it, or mostly focusing on product?"
        )
        if len(msg) > 120:
            msg = f"Hey, saw {comp} — running organic IG/TikTok reels yet, or mostly focused on product?"
    return msg[:120].strip()


_ML_TRAINING_NOISE_RE = re.compile(
    r"(?:\b(?:model\s+training|ai\s+training|ml\s+training|llm\s+training|pre-?training|"
    r"robot\w*\s+training|training\s+robot\w*|training\s+data\w*|"
    r"fine-?tuning\s+courses?|voice\s+ai|audio\s+transcription|speech-to-text)\b|"
    r"обучен\w*\s+(?:модел\w*|нейросет\w*|llm|ии|агент\w*|робот\w*)|машинн\w*\s+обучен\w*|дообучен\w*)",
    re.I,
)
_AUD_EDTECH_RE = re.compile(
    r"\b(?:edtech|online\s+course\w*|cohort\w*|bootcamp\w*|tutoring|language\s+learning|"
    r"exam\s+prep|flashcard\w*|study\s+app|learning\s+platform|academy|обучен\w*|курс\w*|школ\w*)\b",
    re.I,
)
_AUD_GAMING_CAT_RE = re.compile(r"\b(?:gaming|gamefi|igaming|gamedev)\b", re.I)
_AUD_GAMING_DESC_RE = re.compile(
    r"\b(?:roblox|fivem|minecraft|fortnite|steam\s+game|mobile\s+game|browser\s+game|"
    r"multiplayer\s+game|puzzle|arcade|shooter|rpg\b|mmorpg|roguelike|survivors\b|"
    r"play-to-earn|p2e\b|esports|game\s+studio)\b",
    re.I,
)
_AUD_AUDIO_RE = re.compile(
    r"\b(?:podcast\w*|music\s+artist\w*|record\s+label|spotify|soundcloud|radio\s+show|"
    r"audiobook\w*|streaming\s+music|подкаст\w*|лейбл)\b",
    re.I,
)
_AUD_ECOM_CAT_RE = re.compile(
    r"(?:\be-?com(?:merce)?\b|\bd2c\b|\bdtc\b|\bretail\b|\bmerch\b|агентств|agency|agencies|сервис\s*/\s*агентств)",
    re.I,
)
_AUD_ECOM_DESC_RE = re.compile(
    r"\b(?:shopify\s+brand|shopify\s+store|d2c\s+brand|dtc\s+brand|skincare|cosmetics|"
    r"supplement\w*|apparel|clothing\s+brand|jewelry|physical\s+product\w*|merch\s+store)\b",
    re.I,
)
_SOFTWARE_CAT_RE = re.compile(
    r"(?:\bsaas\b|\bdefi\b|\bdex\b|\bcex\b|\bfintech\b|\binfra\b|\bdev\b|проп|платёж|токен)",
    re.I,
)


def infer_lead_audience(
    category: str = "",
    company: str = "",
    their_angle: str = "",
    site: str = "",
) -> str:
    """Infer Vlad's Step 2 {audience} noun: buyers | students | users | players | listeners."""
    cat = (category or "").strip()
    cat_low = cat.lower()
    is_software = bool(_SOFTWARE_CAT_RE.search(cat_low))
    desc = f"{company or ''} {their_angle or ''} {site or ''}".strip()
    clean_all = _ML_TRAINING_NOISE_RE.sub(" ", f"{cat} {desc}")
    clean_desc = _ML_TRAINING_NOISE_RE.sub(" ", desc)

    if _AUD_EDTECH_RE.search(clean_all):
        return "students"
    if _AUD_GAMING_CAT_RE.search(cat_low) and not re.search(r"\bai\b", cat_low):
        return "players"
    if _AUD_GAMING_DESC_RE.search(clean_desc):
        return "players"
    if cat_low in {"podcast", "music", "audio", "подкаст", "музыка", "аудио"}:
        return "listeners"
    if not is_software and _AUD_AUDIO_RE.search(clean_all):
        return "listeners"
    if _AUD_ECOM_CAT_RE.search(cat_low):
        return "buyers"
    if not is_software and _AUD_ECOM_DESC_RE.search(clean_desc):
        return "buyers"
    return "users"


def _infer_subniche_pain(
    brand: str,
    category: str = "",
    their_angle: str = "",
    source: str = "",
) -> str:
    """Build Vlad's individualized Paragraph 1 ('Saw {Brand}. You're probably running into...')."""
    src = (source or "").strip().lower()
    cat_low = (category or "").strip().lower()
    blob = f"{cat_low} {(their_angle or '').lower()}"
    clean_b = brand.rstrip(".!?") if brand else ""
    opener = f"Saw {clean_b}." if clean_b else "Came across what you're building."

    if src == "linkedin_jobs" or "hiring" in src:
        subject = brand or "your team"
        return (
            f"Saw {subject} is hiring on the content side. Relying on a single in-house creator "
            "makes it tough to test enough hook variations and scale daily volume without burning budget."
        )
    if src == "meta_ads" or "active meta video ads" in blob:
        subject = brand or "your team"
        return (
            f"Saw {subject} is running video ads right now. Paid social works until creative fatigue "
            "kicks in and CAC creeps up faster than your team can ship fresh variations."
        )
    if re.search(r"(?:агентств|agency|agencies|\bbd\b|advisory|consulting|venture\s+studio|kol\s+marketing|pr\s+agency|marketing\s+partner)", blob):
        return (
            f"{opener} You're probably running into the same thing most agencies hit: "
            "every new client means more manual BD, and outreach just scales 1:1 with your hours. "
            "Makes it tough to get consistent deal flow when you're always starting from scratch."
        )
    if _AUD_GAMING_DESC_RE.search(blob) or (cat_low == "gaming" or "gamefi" in blob or "igaming" in blob):
        return (
            f"{opener} You're probably running into the same thing most game studios hit: "
            "paid UA eats margin fast, and posting from one brand page rarely brings enough steady organic players to compound."
        )
    if re.search(r"(?:проп|prop\s*trad|prop\s*firm|\bcex\b|бирж|exchange|trading\s+platform|funded\s+trader|broker)", blob):
        return (
            f"{opener} You're probably running into the same thing most trading platforms hit: "
            "KOL promos spike signups for a day and die off, while paid ads get restricted or priced out fast."
        )
    if re.search(r"(?:\bdefi\b|\bdex\b|\btvl\b|staking|liquidity|yield|perp|lending|web3|protocol|wallet|on-?chain|\brwa\b|token)", blob):
        return (
            f"{opener} You're probably running into the same thing most Web3 teams hit: "
            "X posts and AMAs mostly recycle the same crypto-native crowd, while bringing in fresh users outside that bubble costs a fortune."
        )
    if re.search(r"(?:платёж|финтех|fintech|payment|neobank|remittance|card\b|checkout|invoic|payroll|banking)", blob):
        return (
            f"{opener} You're probably running into the same thing most fintech teams hit: "
            "paid acquisition gets expensive fast, and trust-building video content takes too much manual work to ship daily."
        )
    if _AUD_EDTECH_RE.search(blob):
        return (
            f"{opener} You're probably running into the same thing most education brands hit: "
            "ad and webinar CPLs keep climbing, while organic short-form takes too much founder time to produce at volume."
        )
    if _AUD_ECOM_DESC_RE.search(blob) or re.search(r"(?:\be-?com\b|\bd2c\b|\bdtc\b|shopify|physical\s+product)", blob):
        return (
            f"{opener} You're probably running into the same thing most consumer brands hit: "
            "ad CPMs keep climbing, and shipping product out to dozens of creators takes weeks just to find one winning reel."
        )
    if re.search(r"(?:медиа|\bmedia\b|podcast|newsletter|youtube|community|conference|event|hackathon|show\b)", blob):
        return (
            f"{opener} You're probably running into the same thing most media brands hit: "
            "you already have strong long-form content, but turning it into daily short-form reach across multiple pages takes a full editing crew."
        )
    if re.search(r"(?:инфраструктур|\bdev\b|developer|open-?source|\bsdk\b|\bapi\b|\bcli\b|self-?hosted|postgres|docker|kubernetes|github)", blob):
        return (
            f"{opener} You're probably running into the same thing most dev-tool teams hit: "
            "launch posts on HN or Product Hunt give a short two-day spike, and after that organic discovery flattens out."
        )
    return (
        f"{opener} You're probably running into the same thing most AI and SaaS teams hit: "
        "launch spikes fade fast, paid CAC creeps up, and posting from a single brand page rarely compounds into steady user signups."
    )


_RU_BODY = (
    "Мы запустили SoftUGC: это органическая дистрибуция через короткие AI-ролики "
    "и собственную сетку аккаунтов. 183.5M просмотров за 90 дней без съёмок, "
    "трафик выходит в 13-40 раз дешевле обычных клипперов."
)
_RU_PAIN = (
    "Стабильно доставать целевую аудиторию становится дорого, когда продакшн "
    "и дистрибуция коротких роликов завязаны на ручную работу."
)
_RU_OFFER = (
    "Работаем по revshare или CPA, с вашей стороны только себестоимость продакшна. "
    "Показать 3 примера рилсов под ваш бренд, чтобы прикинуть, как это сработает?"
)


def _product(product: str, russian: bool) -> str:
    clean = (product or "").strip()
    if clean:
        return clean
    return "твой продукт" if russian else "your product"


def dm_allowed(user) -> bool:
    """True only for a Telegram user. Channels and groups are not DMs."""
    return type(user).__name__ == "User"


def sanitize_copy_text(text: str) -> str:
    """Normalize line endings and scrub any legacy brand token to SoftUGC."""
    s = str(text or "").replace("\u2028", "\n").replace("\r\n", "\n")
    s = _LEGACY_BRAND_RE.sub("SoftUGC", s)
    return s.strip()


def normalize_provider(provider: str | None = None) -> Literal["telegram", "whatsapp"]:
    """Normalize messaging provider to 'telegram' or 'whatsapp'."""
    raw = (provider or "telegram").strip().lower()
    if raw in ("", "telegram", "tg"):
        return "telegram"
    if raw in ("whatsapp", "wa"):
        return "whatsapp"
    raise ValueError(f"Unsupported provider {provider!r}; expected 'telegram' or 'whatsapp'")


def parse_pool_usd_value(raw_pool: int | float | str | None) -> int | None:
    """Parse integer USD pool size from numeric or formatted currency strings."""
    if raw_pool is None or isinstance(raw_pool, bool):
        return None
    if isinstance(raw_pool, (int, float)):
        val = int(raw_pool)
        return val if val >= 0 else None
    text = str(raw_pool).strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*([kKmM])?", text)
    if not m:
        return None
    num = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        num *= 1000
    elif suffix == "m":
        num *= 1_000_000
    return int(num)


def whop_content_rewards_hook(brand: str = "", pool_usd: int | float | str | None = None) -> str:
    """Build personalized Paragraph 2 hook for Whop Content Rewards campaigns with pool_usd >= 5000."""
    pool_int = parse_pool_usd_value(pool_usd)
    if pool_int is None or pool_int < 5000:
        return ""
    pool_str = f"${pool_int:,}"
    clean_b = brand.strip().rstrip(".!?") if brand else ""
    subject = f"{clean_b}'s {pool_str} Content Rewards campaign" if clean_b else f"your {pool_str} Content Rewards campaign"
    return (
        f"Saw {subject}. We can run your next cycle or a parallel test through our own owned account network "
        "at a fraction of standard clipper CPM without junk clips."
    )


def render_whale_offer(brand: str = "") -> str:
    """Render Whale mass-production offer & demo-reel CTA without revshare or CPA."""
    target = (brand or "").strip().rstrip(".!?") or "your brand"
    return f"{WHALE_PITCH_TEXT} {WHALE_CTA_TEMPLATE.format(brand=target)}"


def render_whale_whatsapp_step3(brand: str = "") -> str:
    """Render clean WhatsApp Step 3 text follow-up without any meme caption or image."""
    target = (brand or "").strip().rstrip(".!?") or "your brand"
    return WHALE_WHATSAPP_STEP3_TEXT.format(brand=target)


def resolve_copy_profile(
    copy_profile: str | None = None,
    *,
    segment: str | None = None,
    language: str | None = None,
    source: str | None = None,
    default_profile: str = "b2b_legacy",
) -> str:
    """Resolve canonical copy_profile from explicit profile, segment, and language."""
    del source
    cp = (copy_profile or "").strip().lower()
    seg = (segment or "").strip().lower()
    lang = (language or "").strip().lower()

    aliases = {
        "whale": "whale_mass",
        "whales": "whale_mass",
        "whale_mass": "whale_mass",
        "vacancy_ru": "vacancy_ru",
        "client_reels_ru": "vacancy_ru",
        "vacancy_en": "vacancy_en",
        "client_reels_en": "vacancy_en",
        "ru_expert": "ru_expert",
        "ru_experts": "ru_expert",
        "social_manual_bridge": "social_manual_bridge",
        "social_manual": "social_manual_bridge",
        "manual_social": "social_manual_bridge",
        "ofm": "ofm",
        "b2b_legacy": "b2b_legacy",
        "legacy": "b2b_legacy",
        "revshare": "b2b_legacy",
    }
    if cp in aliases:
        return aliases[cp]
    if cp in ("vacancy", "client_reels", "vacancy_assisted"):
        return "vacancy_ru" if lang == "ru" else "vacancy_en"
    if cp not in ("", "default", "auto"):
        raise ValueError(f"Unknown copy_profile: {copy_profile!r}")

    if seg in ("whale", "whales"):
        return "whale_mass"
    if seg in ("ru_expert", "ru_experts"):
        return "ru_expert"
    if seg in ("social_manual", "manual_social", "social_manual_bridge"):
        return "social_manual_bridge"
    if seg in ("vacancy", "client_reels", "vacancy_assisted"):
        return "vacancy_ru" if lang == "ru" else "vacancy_en"
    if seg == "ofm":
        return "ofm"
    return default_profile


def _infer_lead_lang_hint(lead: Mapping[str, Any] | None) -> Literal["ru", "en"]:
    if not lead:
        return "en"
    explicit = str(lead.get("language") or "").strip().lower()
    if explicit in ("ru", "en"):
        return explicit  # type: ignore[return-value]
    src = str(lead.get("source") or lead.get("lead_source") or "").strip().lower()
    if src in {"hh_ru", "profi_ru", "kwork", "ru_experts", "tg_jobs_ru", "vacancies_ru", "tg_jobs"}:
        return "ru"
    site = str(lead.get("website") or lead.get("project_url") or lead.get("site") or lead.get("url") or "").lower()
    if re.search(r"\.(?:ru|su|рф|by|kz)(?:/|$|:)", site):
        return "ru"
    probe = " ".join(
        str(lead.get(k) or "")
        for k in ("name", "company", "project", "title", "role", "their_angle", "what_they_do", "note", "bio")
    )
    if _CYRILLIC_RE.search(probe):
        return "ru"
    return "en"


def resolve_profile_language(
    copy_profile: str,
    language: str | None = None,
    *,
    lead: Mapping[str, Any] | None = None,
    russian: bool = False,
) -> Literal["en", "ru"]:
    """Resolve and strictly enforce copy language compatibility with copy_profile."""
    meta = PROFILE_METADATA.get(copy_profile)
    if meta is None:
        raise ValueError(f"Unknown copy_profile: {copy_profile!r}")
    fixed_lang: str | None = meta["language"]
    raw_lang = (language or "").strip().lower()
    if raw_lang:
        if raw_lang not in ("en", "ru"):
            raise ValueError(f"Unsupported language {language!r}; expected 'en' or 'ru'")
        if fixed_lang is not None and raw_lang != fixed_lang:
            raise ValueError(
                f"Language mismatch: copy_profile={copy_profile!r} requires language={fixed_lang!r}, got {raw_lang!r}"
            )
        return raw_lang  # type: ignore[return-value]
    if russian:
        if fixed_lang == "en":
            raise ValueError(f"Language mismatch: copy_profile={copy_profile!r} requires language='en', got russian=True")
        return "ru"
    if fixed_lang in ("en", "ru"):
        return fixed_lang  # type: ignore[return-value]
    if copy_profile == "social_manual_bridge":
        return _infer_lead_lang_hint(lead)
    return "en"


def check_copy_language(text: str, expected_language: Literal["en", "ru"]) -> tuple[bool, str]:
    """Verify generated or customized outreach copy matches expected_language ('en' or 'ru')."""
    s = str(text or "").strip()
    if not s:
        return True, ""
    if expected_language == "en":
        if _CYRILLIC_RE.search(s):
            return False, "Russian Cyrillic text is not allowed in an English ('en') campaign"
        stripped = _URL_STRIP_RE.sub(" ", s)
        if not re.search(r"[A-Za-z]", stripped):
            return False, "English copy must contain Latin text"
        return True, ""
    if expected_language == "ru":
        cleaned = _URL_STRIP_RE.sub(" ", s)
        cleaned = _NEUTRAL_TECH_TOKENS_RE.sub(" ", cleaned)
        cyr_cnt = len(re.findall(r"[А-Яа-яЁё]", cleaned))
        lat_cnt = len(re.findall(r"[A-Za-z]", cleaned))
        if cyr_cnt < 4 or cyr_cnt < lat_cnt:
            return False, "English/non-Cyrillic text is not allowed in a Russian ('ru') campaign"
        return True, ""
    return False, f"Unsupported expected_language: {expected_language!r}"


def step3_image_for_profile(
    copy_profile: str,
    *,
    provider: str = "telegram",
    custom_step_templates: dict[str, Any] | str | None = None,
) -> str | None:
    """Return Step 3 image URL for profile+provider; NEVER attaches meme on WhatsApp."""
    prov = normalize_provider(provider)
    if prov == "whatsapp":
        return None
    meta = PROFILE_METADATA.get(copy_profile)
    if not meta or not meta.get("allows_step3_image"):
        return None
    parsed = _parse_custom_step_templates(custom_step_templates)
    if "step3_image_url" in parsed or "image_url" in parsed:
        raw_img = parsed.get("step3_image_url") if "step3_image_url" in parsed else parsed.get("image_url")
        cleaned = str(raw_img or "").strip()
        return cleaned or None
    return VLAD_STEP3_IMAGE_URL


def _parse_custom_step_templates(raw: dict[str, Any] | str | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("custom_step_templates_json must decode to a JSON object")
        return dict(loaded)
    if isinstance(raw, Mapping):
        return dict(raw)
    raise ValueError(f"Unsupported custom_step_templates type: {type(raw).__name__}")


def _safe_format_template(template: str, ctx: Mapping[str, Any]) -> str:
    """Replace known {token} placeholders without raising KeyError on unknown braces."""
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in ctx:
            return str(ctx[key])
        low = key.lower()
        if low in ctx:
            return str(ctx[low])
        return match.group(0)

    return re.sub(r"\{([A-Za-z0-9_]+)\}", _sub, str(template or ""))


def _extract_step_override(
    custom: Mapping[str, Any],
    step_order: int,
) -> tuple[str | None, int | None, str | None]:
    """Return (template_text_or_None, delay_hours_or_None, image_url_or_None) from custom_step_templates."""
    key_map = {
        1: ("step1", "step1_text", "step1_template", "first_touch", "1"),
        2: ("step2", "step2_text", "step2_template", "followup_1", "2"),
        3: ("step3", "step3_text", "step3_template", "followup_2", "3"),
    }
    text_override: str | None = None
    delay_override: int | None = None
    image_override: str | None = None

    for k in key_map.get(step_order, ()):
        if k in custom and custom[k] is not None:
            val = custom[k]
            if isinstance(val, Mapping):
                if "template_text" in val or "text" in val:
                    text_override = str(val.get("template_text") if "template_text" in val else val.get("text") or "")
                if val.get("delay_hours") is not None:
                    delay_override = max(0, int(val["delay_hours"]))
                if "image_url" in val:
                    image_override = str(val.get("image_url") or "").strip() or None
            else:
                text_override = str(val)
            break

    delay_key = f"step{step_order}_delay_hours"
    if delay_override is None and custom.get(delay_key) is not None:
        delay_override = max(0, int(custom[delay_key]))

    steps_block = custom.get("steps")
    if isinstance(steps_block, Mapping):
        entry = steps_block.get(str(step_order)) or steps_block.get(step_order)
        if isinstance(entry, Mapping):
            if text_override is None and ("template_text" in entry or "text" in entry):
                text_override = str(entry.get("template_text") if "template_text" in entry else entry.get("text") or "")
            if delay_override is None and entry.get("delay_hours") is not None:
                delay_override = max(0, int(entry["delay_hours"]))
            if image_override is None and "image_url" in entry:
                image_override = str(entry.get("image_url") or "").strip() or None
        elif isinstance(entry, str) and text_override is None:
            text_override = entry
    elif isinstance(steps_block, list):
        for idx, item in enumerate(steps_block, start=1):
            if not isinstance(item, Mapping):
                continue
            item_order = int(item.get("step_order") or idx)
            if item_order == step_order:
                if text_override is None and ("template_text" in item or "text" in item):
                    text_override = str(item.get("template_text") if "template_text" in item else item.get("text") or "")
                if delay_override is None and item.get("delay_hours") is not None:
                    delay_override = max(0, int(item["delay_hours"]))
                if image_override is None and "image_url" in item:
                    image_override = str(item.get("image_url") or "").strip() or None
                break

    return text_override, delay_override, image_override


def _resolve_showcase_url(
    showcase_url: str = "",
    custom: Mapping[str, Any] | None = None,
    lead: Mapping[str, Any] | None = None,
) -> str:
    for cand in (
        showcase_url,
        (custom or {}).get("showcase_url") if custom else "",
        (lead or {}).get("showcase_url") if lead else "",
        DEFAULT_VACANCY_SHOWCASE_URL,
    ):
        val = str(cand or "").strip()
        if val:
            return val
    return DEFAULT_VACANCY_SHOWCASE_URL


def _build_whale_mass_steps(
    *,
    brand: str,
    niche: str,
    category: str,
    site: str,
    source: str,
    pool_usd: int | float | str | None,
    audience: str,
    provider: Literal["telegram", "whatsapp"],
) -> tuple[str, str, str]:
    whop_hook = ""
    if (source or "").strip().lower() == "whop_content_rewards":
        whop_hook = whop_content_rewards_hook(brand=brand, pool_usd=pool_usd)
    pain_para = whop_hook or _infer_subniche_pain(
        brand=brand,
        category=category,
        their_angle=niche,
        source=source,
    )
    step1 = f"Hey,\n\n{pain_para}\n\n{render_whale_offer(brand)}"
    step2 = VLAD_STEP2_TEMPLATE.format(audience=audience)
    step3 = render_whale_whatsapp_step3(brand) if provider == "whatsapp" else VLAD_STEP3_TEXT
    return step1, step2, step3


def _build_vacancy_ru_steps(
    *,
    brand: str,
    role: str,
    showcase_url: str,
) -> tuple[str, str, str]:
    clean_role = role.strip()
    if clean_role:
        opener = f"Добрый день! По задаче: {clean_role}."
        role_ctx = f" ({clean_role})"
        role_close = f" по «{clean_role}»"
    elif brand:
        opener = f"Добрый день! Увидел запрос по контенту для {brand}."
        role_ctx = f" для {brand}"
        role_close = f" для {brand}"
    else:
        opener = "Добрый день! По вашей задаче по коротким роликам."
        role_ctx = ""
        role_close = ""

    step1 = (
        f"{opener}\n\n"
        "Мы строим не разовые ролики, а систему лидгена (короткие AI-ролики + воронка под трафик и заявки) "
        "без живых съёмок.\n\n"
        "Релевантные кейсы: faceless-канал на 183,5 млн просмотров за 90 дней (94,8% холодный охват) "
        f"и потоковый выпуск до 90 роликов в месяц на проект. Витрина и кейсы: {showcase_url}\n\n"
        "Можем начать с небольшого тестового запуска, чтобы вы проверили формат на своих метриках. "
        "Какой объём роликов в месяц и какая площадка сейчас в приоритете?"
    )
    step2 = (
        f"Добрый день! Возвращаюсь по задаче{role_ctx}. "
        "Можем проверить формат на тестовой серии роликов ($80-100 за стартовый тест-пак, "
        "либо сначала соберём тестовый фрагмент 10-15 сек под ваш стиль), чтобы вы оценили удержание и качество до основного запуска. "
        "Если актуально, скиньте 1-2 референса или короткое ТЗ."
    )
    step3 = (
        f"Подскажите, задача{role_close} ещё открыта или уже закрыли поиск? "
        "Если актуально, можем созвониться на 10 минут и наметить тест."
    )
    return step1, step2, step3


def _build_vacancy_en_steps(
    *,
    brand: str,
    role: str,
    showcase_url: str,
) -> tuple[str, str, str]:
    ctx = role.strip() or brand.strip()
    opener = f"Hi,\n\nSaw {ctx}." if ctx else "Hi,\n\nSaw you're hiring for short-form video."
    ctx_suffix = f" for {ctx}" if ctx else ""

    step1 = (
        f"{opener} Instead of relying on one freelancer, we build a complete short-form leadgen system "
        "(AI UGC reels + conversion funnel) that ships daily volume with zero shoots.\n\n"
        "Relevant proof: 183.5M views in 90 days (94.8% non-follower reach) and up to 90 reels/mo per project. "
        f"Showcase & case studies: {showcase_url}\n\n"
        "We can start with a small test run so you see real metrics before scaling. "
        "What monthly reel volume and platform are you prioritizing?"
    )
    step2 = (
        f"Following up on my note{ctx_suffix}. We can spin up a starter test series "
        "($80-100 test batch, or a quick 10-15s custom sample cut first) tailored to your brand "
        "so you can benchmark hooks and retention before committing to volume. Send 1-2 references if you'd like to test it."
    )
    step3 = (
        f"Quick check-in{ctx_suffix}: is this role still open, or did you already lock someone in? "
        "Happy to hop on a 10-minute call if you want to test it."
    )
    return step1, step2, step3


def _build_ru_expert_steps(
    *,
    first_name_ru: str,
    brand: str,
    niche: str,
) -> tuple[str, str]:
    hello = f"Привет, {first_name_ru}!" if first_name_ru else "Привет!"
    target = brand.strip()
    if target:
        saw = f"увидел {target}"
    elif niche.strip():
        saw = "наткнулся на твой канал и контент"
    else:
        saw = "увидел твой блог и подачу"

    step1 = (
        f"{hello}\n\n"
        f"Это Тим (Tim RbM), {saw}. Видно сильную экспертизу и живую аудиторию, но у большинства экспертов "
        "контент живёт отдельно и не собирается в предсказуемую систему монетизации.\n\n"
        "Мы помогаем экспертам выстраивать продюсирование и контент-воронку под ключ: от упаковки и коротких "
        "роликов до прогрева и продаж без выгорания на рутине. Если откликается, можем созвониться на 15 минут "
        "и разобрать точки роста именно под твою аудиторию. Удобно на этой неделе?"
    )
    step2 = (
        "Привет ещё раз! Поднимаю сообщение, вдруг потерялось в потоке чатов. "
        "Могу за 15 минут подсказать пару рабочих связок по воронке и монетизации контента под твою нишу, "
        "без обязательств, просто предметно обменяться идеями. Актуально на этой неделе?"
    )
    return step1, step2


def _build_social_manual_bridge_step1(
    *,
    language: Literal["en", "ru"],
    first_name: str,
    brand: str,
) -> str:
    if language == "ru":
        greeting = f"Привет, {first_name}!" if first_name else "Привет!"
        target = brand or "ваш проект"
        return (
            f"{greeting} Сделали короткий 15-сек пример ролика под {target}, "
            "куда удобнее скинуть, в WhatsApp или Telegram?"
        )
    greeting = f"Hey {first_name}" if first_name else "Hey"
    target = brand or "your brand"
    return (
        f"{greeting}, put together a quick 15-sec custom sample reel for {target}, "
        "is WhatsApp or Telegram better to send it over?"
    )


def first_touch(
    product: str,
    niche: str,
    russian: bool = False,
    *,
    source: str = "",
    category: str = "",
    site: str = "",
    copy_profile: str = "",
    segment: str = "",
    language: str = "",
    provider: str = "telegram",
    pool_usd: int | float | str | None = None,
    role: str = "",
    name: str = "",
    showcase_url: str = "",
    custom_step_templates: dict[str, Any] | str | None = None,
) -> str:
    """Generate canonical SoftUGC Step 1 text for legacy B2B or any v2 outreach copy profile."""
    has_v2_params = any(
        (
            bool(copy_profile),
            bool(segment),
            bool(language),
            normalize_provider(provider) != "telegram",
            pool_usd is not None,
            bool(role),
            bool(showcase_url),
            custom_step_templates is not None,
        )
    )
    if not has_v2_params:
        if russian:
            clean_name = _product(product, True)
            return sanitize_copy_text(
                "Привет,\n\n"
                f"Видел {clean_name}. {_RU_PAIN}\n\n"
                f"{_RU_BODY}\n\n"
                f"{_RU_OFFER}"
            )
        brand = clean_brand_name(product, site=site, their_angle=niche)
        pain_para = _infer_subniche_pain(
            brand=brand,
            category=category,
            their_angle=niche,
            source=source,
        )
        return sanitize_copy_text(
            "Hey,\n\n"
            f"{pain_para}\n\n"
            f"{SOFTUGC_PITCH_TEXT}\n\n"
            f"{REVSHARE_OFFER_TEXT}"
        )

    lead_row = {
        "company": product,
        "name": name,
        "their_angle": niche,
        "source": source,
        "category": category,
        "website": site,
        "pool_usd": pool_usd,
        "role": role,
    }
    seq = build_outreach_sequence(
        lead_row,
        copy_profile=copy_profile or None,
        segment=segment or None,
        language=language or ("ru" if russian else None),
        provider=provider,
        custom_step_templates=custom_step_templates,
        showcase_url=showcase_url,
        use_llm=False,
    )
    return seq.step1_text


_GENERIC_OR_BAD_DRAFT_RE = re.compile(
    r"(?:"
    r"scaling\s+reach\s+for\s+|"
    r"if\s+your\s+user\s+growth\s+has\s+hit\s+a\s+ceiling|"
    r"on\s+our\s+last\s+project,\s+zero\s+live\s+shoots|"
    r"reaching\s+qualified\s+buyers\s+consistently\s+is\s+expensive|"
    r"scaling\s+that\s+audience\s+consistently\s+with\s+high-quality\s+content|"
    r"built\s+softugc\s+as\s+a\s+plug-and-play\s+organic\s+distribution\s+arm|"
    r"solve\s+that\s+with\s+(?:creatoros|softugc),\s+a\s+plug-and-play\s+growth\s+arm|"
    r"i\s+can['’]?t\s+write\s+this\s+one|"
    r"cloudflare|just\s+a\s+moment|"
    r"\(generated\)"
    r")",
    re.I,
)
_OLD_ENDING_SENTENCE_RE = re.compile(
    r"(?:(?:\s*[,;]?\s*(?:and|plus|so)\s+|\s+)(?:"
    r"We\s+(?:also\s+)?(?:work\s+on|do|offer|run\s+on)\s+revshare[^.!?\n]*[.!?]|"
    r"No\s+upfront\s+retainer[^.!?\n]*[.!?]|"
    r"You\s+only\s+cover\s+production[^.!?\n]*[.!?]|"
    r"(?:Want|Would\s+you\s+like)\s+(?:me\s+)?to\s+(?:walk|show|see|send|share)[^?\n]*\?|"
    r"Want\s+to\s+see\s+(?:how|3\s+example|a\s+few|some)[^?\n]*\?|"
    r"Worth\s+a\s+quick\s+(?:look|chat|walkthrough)[^?\n]*\?|"
    r"Open\s+to\s+(?:seeing|a\s+quick)[^?\n]*\?|"
    r"Should\s+I\s+(?:send|show|walk)[^?\n]*\?|"
    r"Can\s+I\s+(?:send|show|walk)[^?\n]*\?|"
    r"Would\s+(?:it\s+make\s+sense|seeing)[^?\n]*\?|"
    r"Interested\s+in\s+seeing[^?\n]*\?|"
    r"Happy\s+to\s+(?:send|show|walk)[^.!?\n]*[.!?]|"
    r"Let\s+me\s+know\s+if\s+you(?:'d|\s+would|\s+want)[^.!?\n]*[.!?]"
    r")|^(?:"
    r"We\s+(?:also\s+)?(?:work\s+on|do|offer|run\s+on)\s+revshare[^.!?\n]*[.!?]|"
    r"No\s+upfront\s+retainer[^.!?\n]*[.!?]|"
    r"You\s+only\s+cover\s+production[^.!?\n]*[.!?]|"
    r"(?:Want|Would\s+you\s+like)\s+(?:me\s+)?to\s+(?:walk|show|see|send|share)[^?\n]*\?|"
    r"Want\s+to\s+see\s+(?:how|3\s+example|a\s+few|some)[^?\n]*\?|"
    r"Worth\s+a\s+quick\s+(?:look|chat|walkthrough)[^?\n]*\?|"
    r"Open\s+to\s+(?:seeing|a\s+quick)[^?\n]*\?|"
    r"Should\s+I\s+(?:send|show|walk)[^?\n]*\?|"
    r"Can\s+I\s+(?:send|show|walk)[^?\n]*\?|"
    r"Would\s+(?:it\s+make\s+sense|seeing)[^?\n]*\?|"
    r"Interested\s+in\s+seeing[^?\n]*\?|"
    r"Happy\s+to\s+(?:send|show|walk)[^.!?\n]*[.!?]|"
    r"Let\s+me\s+know\s+if\s+you(?:'d|\s+would|\s+want)[^.!?\n]*[.!?]"
    r"))+\s*$",
    re.I,
)


def _strip_revshare_sentences(text: str) -> str:
    """Remove trailing revshare/CPA offer sentences from an existing Step 1 draft."""
    cleaned = text.replace(REVSHARE_OFFER_TEXT, "").strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
    out_paras: list[str] = []
    for p in paragraphs:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", p) if s.strip()]
        kept = [s for s in sentences if not _REVSHARE_CPA_RE.search(s)]
        if kept:
            out_paras.append(" ".join(kept))
    return "\n\n".join(out_paras).strip()


def upgrade_to_vlad_step1(
    existing_text: str | None,
    *,
    company: str = "",
    name: str = "",
    site: str = "",
    category: str = "",
    their_angle: str = "",
    source: str = "",
    copy_profile: str = "",
    segment: str = "",
    pool_usd: int | float | str | None = None,
) -> str:
    """Keep genuine custom LLM-written pain/pitch paragraphs and upgrade CTA to target offer; rebuild generic/broken drafts."""
    profile = resolve_copy_profile(copy_profile or None, segment=segment or None, source=source or None)
    raw = str(existing_text or "").replace("\u2028", "\n").replace("\r\n", "\n").strip()
    brand = clean_brand_name(company or name, site=site, their_angle=their_angle)

    if profile == "whale_mass":
        if (source or "").strip().lower() == "whop_content_rewards":
            pool_int = parse_pool_usd_value(pool_usd)
            if pool_int is not None and pool_int >= 5000 and "content rewards" not in raw.lower():
                return first_touch(
                    brand or company or name,
                    their_angle,
                    russian=False,
                    source=source,
                    category=category,
                    site=site,
                    copy_profile="whale_mass",
                    pool_usd=pool_usd,
                )

    is_genuine_custom = (
        len(raw) >= 150
        and "183.5" in raw
        and not _CYRILLIC_RE.search(raw)
        and not _PLACEHOLDER_RE.search(raw)
        and not _GENERIC_OR_BAD_DRAFT_RE.search(raw)
    )
    if not is_genuine_custom:
        return first_touch(
            brand or company or name,
            their_angle,
            russian=False,
            source=source,
            category=category,
            site=site,
            copy_profile=profile if profile != "b2b_legacy" else "",
            segment=segment,
            pool_usd=pool_usd,
        )

    cleaned = re.sub(r"\bCreatorOS\b", "SoftUGC", raw, flags=re.I)
    cleaned = re.sub(r"\bWe\s+run\s+SoftUGC\s+It's\b", "We run SoftUGC. It's", cleaned)
    cleaned = re.sub(r"\s*[—–]\s*", ", ", cleaned)
    cleaned = re.sub(r"\b\d+[\.\)]\s+([A-Z][A-Za-z0-9]+)", r"\1", cleaned)

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
    # Normalize greeting to clean "Hey,"
    if paragraphs and re.match(r"^(?:hey|hi)\b", paragraphs[0], flags=re.I) and len(paragraphs[0]) <= 32:
        paragraphs[0] = "Hey,"
    elif paragraphs and re.match(r"^(?:hey|hi)\s*,", paragraphs[0], flags=re.I):
        rest = re.sub(r"^(?:hey|hi)\s*,\s*", "", paragraphs[0], flags=re.I).strip()
        paragraphs = ["Hey,", rest] + paragraphs[1:]
    else:
        paragraphs.insert(0, "Hey,")

    if profile == "whale_mass":
        joined_body = _strip_revshare_sentences("\n\n".join(paragraphs))
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", joined_body) if p.strip()]
        while len(paragraphs) > 2:
            tail = paragraphs[-1]
            stripped_tail = _OLD_ENDING_SENTENCE_RE.sub("", tail).strip()
            if not stripped_tail:
                paragraphs.pop()
            else:
                stripped_tail = re.sub(r"(?:[,;:\-\s]+|,\s*(?:and|plus|so))$", "", stripped_tail, flags=re.I).strip()
                if stripped_tail and stripped_tail[-1] not in ".!?":
                    stripped_tail += "."
                paragraphs[-1] = stripped_tail
                break
        if len(paragraphs) >= 2:
            trimmed = _OLD_ENDING_SENTENCE_RE.sub("", paragraphs[-1]).strip()
            if trimmed and "183.5" in trimmed:
                trimmed = re.sub(r"(?:[,;:\-\s]+|,\s*(?:and|plus|so))$", "", trimmed, flags=re.I).strip()
                if trimmed and trimmed[-1] not in ".!?":
                    trimmed += "."
                paragraphs[-1] = trimmed
        body_now = "\n\n".join(paragraphs)
        if not any("183.5" in p for p in paragraphs) or _REVSHARE_CPA_RE.search(body_now):
            return first_touch(
                brand or company or name,
                their_angle,
                russian=False,
                source=source,
                category=category,
                site=site,
                copy_profile="whale_mass",
                pool_usd=pool_usd,
            )
        target_brand = brand or "your brand"
        body_low = body_now.lower()
        if "owned account network" in body_low and ("1 500+" in body_low or "1,500+" in body_low):
            paragraphs.append(WHALE_CTA_TEMPLATE.format(brand=target_brand))
        else:
            paragraphs.append(
                "Our owned account network ships 1 500+ reels/mo, 13-40x cheaper than standard clippers. "
                + WHALE_CTA_TEMPLATE.format(brand=target_brand)
            )
        return sanitize_copy_text("\n\n".join(paragraphs))

    if any(REVSHARE_OFFER_TEXT in p for p in paragraphs):
        return "\n\n".join(paragraphs)

    # Drop standalone short CTA paragraph at the end if present
    while len(paragraphs) > 2:
        tail = paragraphs[-1]
        stripped_tail = _OLD_ENDING_SENTENCE_RE.sub("", tail).strip()
        if not stripped_tail:
            paragraphs.pop()
        else:
            stripped_tail = re.sub(r"(?:[,;:\-\s]+|,\s*(?:and|plus|so))$", "", stripped_tail, flags=re.I).strip()
            if stripped_tail and stripped_tail[-1] not in ".!?":
                stripped_tail += "."
            paragraphs[-1] = stripped_tail
            break

    # Also strip trailing old revshare/CTA sentence attached to the 183.5M paragraph
    if len(paragraphs) >= 2:
        trimmed = _OLD_ENDING_SENTENCE_RE.sub("", paragraphs[-1]).strip()
        if trimmed and "183.5" in trimmed:
            trimmed = re.sub(r"(?:[,;:\-\s]+|,\s*(?:and|plus|so))$", "", trimmed, flags=re.I).strip()
            if trimmed and trimmed[-1] not in ".!?":
                trimmed += "."
            paragraphs[-1] = trimmed

    if not any("183.5" in p for p in paragraphs):
        return first_touch(
            brand or company or name,
            their_angle,
            russian=False,
            source=source,
            category=category,
            site=site,
        )

    paragraphs.append(REVSHARE_OFFER_TEXT)
    return "\n\n".join(paragraphs)


def followup_one(
    product: str = "",
    niche: str = "",
    russian: bool = False,
    *,
    audience: str = "",
    category: str = "",
    site: str = "",
    copy_profile: str = "",
    segment: str = "",
    language: str = "",
    provider: str = "telegram",
    role: str = "",
    showcase_url: str = "",
    custom_step_templates: dict[str, Any] | str | None = None,
) -> str:
    has_v2 = any(
        (
            bool(copy_profile),
            bool(segment),
            bool(language),
            normalize_provider(provider) != "telegram",
            bool(role),
            bool(showcase_url),
            custom_step_templates is not None,
        )
    )
    if not has_v2:
        if russian:
            name = _product(product, True)
            return f"Рассказать, как мы можем получить это для {name}?"
        aud = (audience or "").strip().lower()
        if aud not in VALID_AUDIENCES:
            aud = infer_lead_audience(category=category, company=product, their_angle=niche, site=site)
        return VLAD_STEP2_TEMPLATE.format(audience=aud)

    seq = build_outreach_sequence(
        {
            "company": product,
            "their_angle": niche,
            "category": category,
            "website": site,
            "audience": audience,
            "role": role,
        },
        copy_profile=copy_profile or None,
        segment=segment or None,
        language=language or ("ru" if russian else None),
        provider=provider,
        custom_step_templates=custom_step_templates,
        showcase_url=showcase_url,
        use_llm=False,
    )
    return seq.step2_text


def followup_two(
    product: str = "",
    russian: bool = False,
    *,
    provider: str = "telegram",
    copy_profile: str = "",
    segment: str = "",
    language: str = "",
    role: str = "",
    site: str = "",
    niche: str = "",
    custom_step_templates: dict[str, Any] | str | None = None,
) -> str:
    prov = normalize_provider(provider)
    has_v2 = any(
        (
            bool(copy_profile),
            bool(segment),
            bool(language),
            prov != "telegram",
            bool(role),
            custom_step_templates is not None,
        )
    )
    if not has_v2:
        if russian:
            name = _product(product, True)
            return f"Интересно органическое продвижение {name}?"
        return VLAD_STEP3_TEXT

    seq = build_outreach_sequence(
        {
            "company": product,
            "their_angle": niche,
            "website": site,
            "role": role,
        },
        copy_profile=copy_profile or None,
        segment=segment or None,
        language=language or ("ru" if russian else None),
        provider=prov,
        custom_step_templates=custom_step_templates,
        use_llm=False,
    )
    return seq.step3_text


def interest_reply(russian: bool = False) -> str:
    if russian:
        return "15 минут по твоему продукту. Когда удобно?"
    return "15 minutes on your product. What time works?"


def _how_it_works(product: str, russian: bool) -> str:
    name = _product(product, russian)
    if russian:
        return (
            f"Один короткий ролик на аккаунтах, которые ведём. "
            f"Платишь, только если {name} продаёт. 15 минут. Когда удобно?"
        )
    return (
        f"One short cut on accounts we run. "
        f"You pay only if {name} sells. 15 minutes. What time works?"
    )


def _send_cases(product: str, russian: bool) -> str:
    name = _product(product, russian)
    if russian:
        return f"Больше цифр здесь нет. 15 минут по {name}. Когда удобно?"
    return f"No more numbers here. 15 minutes on {name}. What time works?"


def _not_now(product: str, russian: bool) -> str:
    name = _product(product, russian)
    if russian:
        return f"Понял. Про {name} напишу позже."
    return f"Understood. I will write about {name} later."


def _price(product: str, russian: bool) -> str:
    name = _product(product, russian)
    if russian:
        return (
            f"Платишь, только если {name} продаёт. "
            f"Сумму назовём на 15-минутном созвоне."
        )
    return f"You pay only if {name} sells. The amount is set on a 15-minute call."


def scenario_message(scenario: str, product: str, niche: str = "", russian: bool = False) -> str:
    if scenario == "no_reply_1":
        return followup_one(product, niche, russian=russian)
    if scenario == "no_reply_2":
        return followup_two(product, russian=russian)
    if scenario == "interested":
        return interest_reply(russian=russian)
    if scenario == "how_it_works":
        return _how_it_works(product, russian)
    if scenario == "send_cases":
        return _send_cases(product, russian)
    if scenario == "not_now":
        return _not_now(product, russian)
    if scenario == "price":
        return _price(product, russian)
    raise ValueError(f"unknown scenario: {scenario}")


VLAD_SONNET_SYSTEM_PROMPT = (
    "You write B2B cold Telegram DMs for SoftUGC in natural founder-to-founder English.\n"
    "Reference gold-standard example approved by Vlad:\n"
    "Paragraph 2 (pain):\n"
    "\"Saw Crypto Executives. You're probably running into the same thing most agencies hit: "
    "every new client means more manual BD, and outreach just scales 1:1 with your hours. "
    "Makes it tough to get consistent deal flow when you're always starting from scratch.\"\n"
    "Paragraph 3 (SoftUGC pitch):\n"
    "\"We run SoftUGC. It's organic distribution using AI short-form video and a network of accounts we own. "
    "Works as a growth arm that runs itself: 183.5M views in 90 days, no shoots, traffic costs 13 to 40x less than standard clippers.\"\n\n"
    "Hard rules for EVERY item:\n"
    "1. \"pain\": 2 short, concrete sentences in English. If \"brand\" is non-empty, start with \"Saw <brand>.\" "
    "(if \"brand\" is empty, start with \"Came across what you're building.\"). Diagnose the REAL acquisition/distribution "
    "bottleneck specific to THIS exact product (from what_they_do & website) - never paste their bio/slogan back verbatim, "
    "never compliment them, NO em-dashes (— or –), NO placeholders, NO \"(generated)\" label, ZERO Cyrillic.\n"
    "2. \"pitch\": 2 short sentences in English presenting SoftUGC lightly tailored to how organic AI short-form video + "
    "our owned account network drives their specific funnel. MUST keep exact facts: \"SoftUGC\", \"183.5M views in 90 days\", "
    "\"no shoots\", \"13 to 40x less than standard clippers\". NO em-dashes (— or –).\n"
    "3. \"audience\": pick the single most accurate noun for their end customers from STRICTLY "
    "[\"buyers\", \"students\", \"users\", \"players\", \"listeners\"].\n\n"
    "Return ONLY a valid JSON array: [{\"i\": 0, \"pain\": \"...\", \"pitch\": \"...\", \"audience\": \"users\"}, ...]"
)

WHALE_SONNET_SYSTEM_PROMPT = (
    "You write enterprise B2B cold DMs for SoftUGC mass production in natural founder-to-founder English.\n"
    "Hard rules for EVERY item:\n"
    "1. NEVER mention revshare, revenue-share, or CPA anywhere.\n"
    "2. \"pain\": 2 short, concrete sentences in English. If source is \"whop_content_rewards\" and pool_usd >= 5000, "
    "reference their $<pool_usd> Content Rewards campaign and offer to run their next cycle or parallel test through our "
    "owned account network at a fraction of clipper CPM without junk clips. Otherwise start with \"Saw <brand>.\" "
    "(or \"Came across what you're building.\" if brand is empty) and diagnose their real short-form scale bottleneck.\n"
    "3. \"pitch\": 2 short sentences in English pitching SoftUGC mass AI short-form production. MUST keep exact facts: "
    "\"SoftUGC\", \"183.5M views in 90 days\", \"owned account network\", \"1 500+ reels/mo\", "
    "\"13-40x cheaper than standard clippers\". NO em-dashes (— or –), NO placeholders, ZERO Cyrillic.\n"
    "4. \"audience\": pick one of [\"buyers\", \"students\", \"users\", \"players\", \"listeners\"].\n\n"
    "Return ONLY a valid JSON array: [{\"i\": 0, \"pain\": \"...\", \"pitch\": \"...\", \"audience\": \"users\"}, ...]"
)

VACANCY_RU_SYSTEM_PROMPT = (
    "Ты пишешь деловой отклик на RU-вакансию / запрос роликов от лица команды SoftUGC (Тим).\n"
    "Правила:\n"
    "1. Строго 100% на русском языке (допустимы только SoftUGC, AI, UGC, Reels, Shorts, TikTok и ссылки).\n"
    "2. Позиционирование: строим систему лидгена (короткие ролики + воронка), не разовый фриланс.\n"
    "3. Обязательно упомяни кейс 183,5 млн просмотров за 90 дней, поток до 90 роликов/мес, "
    "ссылку на витрину showcase_url и предложение начать с теста (без цены $80-100 в Step 1).\n"
    "4. НЕ используй длинные тире (— и –) и устаревшие бренды.\n"
    "Верни ТОЛЬКО JSON массив: [{\"i\": 0, \"step1_text\": \"...\", \"audience\": \"users\"}, ...]"
)

VACANCY_EN_SYSTEM_PROMPT = (
    "You write concise business outreach replies to EN video/content vacancies and client reels inquiries for SoftUGC.\n"
    "Rules:\n"
    "1. English only, ZERO Cyrillic, NO em-dashes (— or –), NO revshare/CPA.\n"
    "2. Position SoftUGC as a full short-form leadgen system (AI UGC reels + funnel), mention 183.5M views in 90 days, "
    "up to 90 reels/mo, include showcase_url, and offer a small test batch (save $80-100 pricing details for Step 2).\n"
    "Return ONLY a valid JSON array: [{\"i\": 0, \"step1_text\": \"...\", \"audience\": \"users\"}, ...]"
)

RU_EXPERT_SYSTEM_PROMPT = (
    "Ты пишешь мягкое личное первое сообщение в Telegram от Тима (Tim RbM) российскому эксперту про продюсирование.\n"
    "Правила:\n"
    "1. Строго на русском языке, тёплый личный тон без канцелярита и длинных тире (— и –).\n"
    "2. НЕ называй цену/фикс 100-300K ₽ и проценты в холодном первом сообщении.\n"
    "3. Коротко отметь подачу/экспертизу, предложи выстроить продюсирование и контент-воронку под ключ и созвониться на 15 минут.\n"
    "Верни ТОЛЬКО JSON массив: [{\"i\": 0, \"step1_text\": \"...\", \"audience\": \"buyers\"}, ...]"
)

SOCIAL_MANUAL_BRIDGE_SYSTEM_PROMPT = (
    "You write a strict 1-line DM bridge for high-ICP leads without Telegram/WhatsApp found yet.\n"
    "Rules:\n"
    "1. Strictly ONE line (no newlines), matching the requested language ('en' or 'ru').\n"
    "2. Ask whether WhatsApp or Telegram is better to send a 15-sec custom sample reel for their brand.\n"
    "3. NO em-dashes (— or –).\n"
    "Return ONLY a valid JSON array: [{\"i\": 0, \"step1_text\": \"...\", \"audience\": \"users\"}, ...]"
)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except (ImportError, OSError):
        return ctx
    return ctx


def _build_format_context(
    lead: Mapping[str, Any],
    *,
    brand: str,
    first_name: str,
    role: str,
    audience: str,
    showcase_url: str,
    pool_usd: int | float | str | None,
) -> dict[str, Any]:
    pool_int = parse_pool_usd_value(pool_usd)
    pool_fmt = f"${pool_int:,}" if pool_int is not None else ""
    return {
        "brand": brand or "your brand",
        "Brand": brand or "your brand",
        "company": brand or str(lead.get("company") or lead.get("project") or "your product"),
        "name": str(lead.get("name") or first_name or ""),
        "first_name": first_name,
        "role": role or brand or "коротких роликов",
        "showcase_url": showcase_url,
        "audience": audience,
        "pool_usd": str(pool_int or ""),
        "pool": pool_fmt,
        "niche": str(lead.get("category") or lead.get("niche") or ""),
        "their_angle": str(lead.get("their_angle") or lead.get("what_they_do") or ""),
        "website": str(lead.get("website") or lead.get("project_url") or lead.get("site") or ""),
        "site": str(lead.get("website") or lead.get("project_url") or lead.get("site") or ""),
    }


def _default_steps_for_profile(
    profile: str,
    lead: Mapping[str, Any],
    *,
    brand: str,
    first_name: str,
    role: str,
    audience: str,
    lang: Literal["en", "ru"],
    prov: Literal["telegram", "whatsapp"],
    showcase: str,
) -> tuple[str, str, str]:
    niche = str(lead.get("their_angle") or lead.get("what_they_do") or lead.get("one_liner") or lead.get("note") or "")
    category = str(lead.get("category") or lead.get("niche") or "")
    site = str(lead.get("website") or lead.get("project_url") or lead.get("site") or lead.get("url") or "")
    source = str(lead.get("source") or lead.get("lead_source") or "")
    pool_usd = lead.get("pool_usd") if lead.get("pool_usd") is not None else lead.get("bounty_usd")

    if profile == "whale_mass":
        return _build_whale_mass_steps(
            brand=brand,
            niche=niche,
            category=category,
            site=site,
            source=source,
            pool_usd=pool_usd,
            audience=audience,
            provider=prov,
        )
    if profile == "vacancy_ru":
        return _build_vacancy_ru_steps(brand=brand, role=role, showcase_url=showcase)
    if profile == "vacancy_en":
        return _build_vacancy_en_steps(brand=brand, role=role, showcase_url=showcase)
    if profile == "ru_expert":
        s1, s2 = _build_ru_expert_steps(first_name_ru=first_name, brand=brand, niche=niche)
        return s1, s2, ""
    if profile == "social_manual_bridge":
        s1 = _build_social_manual_bridge_step1(language=lang, first_name=first_name, brand=brand)
        return s1, "", ""
    if profile == "ofm":
        s1 = (
            "Hey,\n\n"
            "Saw you're scaling creators and recruiting VAs for IG and X traffic. "
            "Hiring and warming manual accounts takes weeks, burns budget, and still hits shadowbans before steady volume kicks in.\n\n"
            f"{SOFTUGC_PITCH_TEXT}\n\n"
            f"{REVSHARE_OFFER_TEXT}"
        )
        s2 = VLAD_STEP2_TEMPLATE.format(audience=audience)
        s3 = render_whale_whatsapp_step3(brand) if prov == "whatsapp" else VLAD_STEP3_TEXT
        return s1, s2, s3

    # b2b_legacy
    if lang == "ru":
        prod_name = _product(brand or str(lead.get("company") or ""), True)
        s1 = (
            "Привет,\n\n"
            f"Видел {prod_name}. {_RU_PAIN}\n\n"
            f"{_RU_BODY}\n\n"
            f"{_RU_OFFER}"
        )
        s2 = f"Рассказать, как мы можем получить это для {prod_name}?"
        s3 = f"Интересно органическое продвижение {prod_name}?"
        return s1, s2, s3

    pain_para = _infer_subniche_pain(brand=brand, category=category, their_angle=niche, source=source)
    s1 = f"Hey,\n\n{pain_para}\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
    s2 = VLAD_STEP2_TEMPLATE.format(audience=audience)
    s3 = render_whale_whatsapp_step3(brand) if prov == "whatsapp" else VLAD_STEP3_TEXT
    return s1, s2, s3


def build_outreach_sequence(
    lead: Mapping[str, Any],
    *,
    copy_profile: str | None = None,
    segment: str | None = None,
    language: str | None = None,
    provider: str | None = None,
    custom_step_templates: dict[str, Any] | str | None = None,
    showcase_url: str = "",
    use_llm: bool = False,
    llm_result: Mapping[str, Any] | None = None,
) -> OutreachSequence:
    """Build complete validated Step 1..N sequence for any SoftUGC copy profile and provider."""
    prov = normalize_provider(provider or str(lead.get("provider") or "telegram"))
    req_lang_raw = (language or "").strip().lower() or None
    lead_lang_raw = str(lead.get("language") or "").strip().lower() or None

    profile = resolve_copy_profile(
        copy_profile if copy_profile is not None else lead.get("copy_profile"),
        segment=segment if segment is not None else lead.get("segment"),
        language=req_lang_raw or lead_lang_raw,
        source=str(lead.get("source") or lead.get("lead_source") or ""),
        default_profile="b2b_legacy",
    )
    lang = resolve_profile_language(
        profile,
        req_lang_raw or lead_lang_raw,
        lead=lead,
    )
    custom = _parse_custom_step_templates(
        custom_step_templates if custom_step_templates is not None else lead.get("custom_step_templates_json")
    )
    showcase = _resolve_showcase_url(showcase_url=showcase_url, custom=custom, lead=lead)

    allow_cyr = lang == "ru"
    raw_comp = str(lead.get("company") or lead.get("project") or lead.get("brand") or "").strip()
    raw_name = str(lead.get("name") or lead.get("founder_name") or "").strip()
    site = str(lead.get("website") or lead.get("project_url") or lead.get("site") or lead.get("url") or "").strip()
    angle = str(lead.get("their_angle") or lead.get("what_they_do") or lead.get("one_liner") or lead.get("note") or "").strip()
    category = str(lead.get("category") or lead.get("niche") or "").strip()
    role = str(lead.get("role") or lead.get("vacancy_title") or lead.get("title") or "").strip()
    pool_usd = lead.get("pool_usd") if lead.get("pool_usd") is not None else lead.get("bounty_usd")

    brand = clean_brand_name(
        raw_comp or ("" if allow_cyr else raw_name),
        site=site,
        their_angle=angle,
        allow_cyrillic=allow_cyr,
    )
    first_name = (
        _extract_personal_first_name_ru(raw_name, brand)
        if allow_cyr
        else _extract_personal_first_name(raw_name, brand)
    )
    aud = str((llm_result or {}).get("audience") or lead.get("audience") or "").strip().lower()
    if aud not in VALID_AUDIENCES:
        aud = infer_lead_audience(category=category, company=brand or raw_comp, their_angle=angle, site=site)

    s1, s2, s3 = _default_steps_for_profile(
        profile,
        lead,
        brand=brand,
        first_name=first_name,
        role=role,
        audience=aud,
        lang=lang,
        prov=prov,
        showcase=showcase,
    )

    used_llm = False
    if use_llm and llm_result and str(llm_result.get("step1_text") or "").strip():
        cand_s1 = sanitize_copy_text(str(llm_result.get("step1_text") or ""))
        ok_lang, _ = check_copy_language(cand_s1, lang)
        if (
            ok_lang
            and not _PLACEHOLDER_RE.search(cand_s1)
            and not _GENERIC_OR_BAD_DRAFT_RE.search(cand_s1)
            and (profile != "whale_mass" or not _REVSHARE_CPA_RE.search(cand_s1))
        ):
            s1 = cand_s1
            used_llm = True

    fmt_ctx = _build_format_context(
        lead,
        brand=brand,
        first_name=first_name,
        role=role,
        audience=aud,
        showcase_url=showcase,
        pool_usd=pool_usd,
    )
    default_delays = list(PROFILE_METADATA[profile]["delays"])
    step_texts = [s1, s2, s3]

    ov1, d1, _ = _extract_step_override(custom, 1)
    ov2, d2, _ = _extract_step_override(custom, 2)
    ov3, d3, _ = _extract_step_override(custom, 3)

    if ov1 is not None:
        step_texts[0] = _safe_format_template(ov1, fmt_ctx)
        used_llm = False
    if ov2 is not None:
        step_texts[1] = _safe_format_template(ov2, fmt_ctx)
    if ov3 is not None:
        step_texts[2] = _safe_format_template(ov3, fmt_ctx)

    # On WhatsApp, never use the meme caption even if passed through a shared default template
    if prov == "whatsapp" and step_texts[2].strip() == VLAD_STEP3_TEXT:
        step_texts[2] = render_whale_whatsapp_step3(brand)

    delays = [
        d1 if d1 is not None else default_delays[0],
        d2 if d2 is not None else (default_delays[1] if len(default_delays) > 1 else 24),
        d3 if d3 is not None else (default_delays[2] if len(default_delays) > 2 else 72),
    ]

    # Enforce active step count per profile unless custom override explicitly adds an extra step
    max_steps = len(default_delays)
    if ov3 is not None and ov3.strip():
        max_steps = max(max_steps, 3)
    elif ov2 is not None and ov2.strip():
        max_steps = max(max_steps, 2)

    final_texts: list[str] = []
    for idx in range(3):
        if idx >= max_steps:
            final_texts.append("")
            continue
        cleaned_step = sanitize_copy_text(step_texts[idx])
        if profile == "social_manual_bridge" and idx == 0:
            cleaned_step = " ".join(cleaned_step.splitlines()).strip()
        if cleaned_step:
            ok_lang, reason = check_copy_language(cleaned_step, lang)
            if not ok_lang:
                raise ValueError(f"Language mismatch in step {idx + 1} for profile {profile!r}: {reason}")
        final_texts.append(cleaned_step)

    step3_img = step3_image_for_profile(profile, provider=prov, custom_step_templates=custom)

    step_specs: list[OutreachStepSpec] = []
    for idx in range(max_steps):
        if not final_texts[idx] and idx > 0:
            continue
        step_specs.append(
            OutreachStepSpec(
                step_order=len(step_specs) + 1,
                delay_hours=delays[idx],
                template_text=final_texts[idx],
                image_url=step3_img if idx == 2 else None,
            )
        )

    return OutreachSequence(
        copy_profile=profile,
        language=lang,
        provider=prov,
        step1_text=final_texts[0],
        step2_text=final_texts[1],
        step3_text=final_texts[2],
        step3_image_url=step3_img,
        audience=aud,
        steps=tuple(step_specs),
        used_llm=used_llm,
    )


def _post_chat_completions(
    *,
    system_prompt: str,
    user_items: list[dict[str, Any]],
    model: str,
    timeout: int,
    llm_post_fn: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    import urllib.request

    if llm_post_fn is not None:
        raw_res = llm_post_fn(system_prompt=system_prompt, items=user_items, model=model, timeout=timeout)
        if isinstance(raw_res, list):
            return [obj for obj in raw_res if isinstance(obj, dict)]
        if isinstance(raw_res, str):
            content = raw_res.strip()
        else:
            raise RuntimeError(f"Unsupported llm_post_fn return type: {type(raw_res).__name__}")
    else:
        key = (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
        base = (os.environ.get("LLM_BASE_URL") or "https://relay.fast/v1").rstrip("/")
        if not key:
            raise RuntimeError("RELAYFAST_API_KEY not set")
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_items, ensure_ascii=False)},
                ],
                "temperature": 0.4,
                "max_tokens": max(600, 380 * len(user_items)),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = str(payload["choices"][0]["message"]["content"] or "").strip()

    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", content, flags=re.DOTALL)
    if not m:
        raise RuntimeError(f"{model} returned non-JSON: {content[:200]}")
    parsed = json.loads(m.group(0))
    return [obj for obj in parsed if isinstance(obj, dict)]


def _system_prompt_for_profile(
    profile: str,
    *,
    custom: Mapping[str, Any] | None = None,
) -> str:
    prompt_map = {
        "whale_mass": WHALE_SONNET_SYSTEM_PROMPT,
        "vacancy_ru": VACANCY_RU_SYSTEM_PROMPT,
        "vacancy_en": VACANCY_EN_SYSTEM_PROMPT,
        "ru_expert": RU_EXPERT_SYSTEM_PROMPT,
        "social_manual_bridge": SOCIAL_MANUAL_BRIDGE_SYSTEM_PROMPT,
        "ofm": VLAD_SONNET_SYSTEM_PROMPT,
        "b2b_legacy": VLAD_SONNET_SYSTEM_PROMPT,
    }
    base_prompt = prompt_map.get(profile, VLAD_SONNET_SYSTEM_PROMPT)
    override = ""
    if custom:
        override = sanitize_copy_text(
            str(custom.get("system_prompt") or custom.get("prompt_override") or custom.get("llm_prompt") or "")
        )
    if override:
        return f"{base_prompt}\n\nAdditional campaign instructions:\n{override}"
    return base_prompt


def _validate_and_assemble_llm_step1(
    *,
    profile: str,
    lang: Literal["en", "ru"],
    lead: Mapping[str, Any],
    brand: str,
    obj: Mapping[str, Any],
    prov: Literal["telegram", "whatsapp"],
    showcase: str,
    custom: Mapping[str, Any],
) -> tuple[str, str, bool]:
    """Validate LLM output object for profile; returns (step1_text, audience, used_llm)."""
    aud = str(obj.get("audience") or "").strip().lower()
    if aud not in VALID_AUDIENCES:
        aud = infer_lead_audience(
            category=str(lead.get("category") or ""),
            company=brand or str(lead.get("company") or ""),
            their_angle=str(lead.get("their_angle") or ""),
            site=str(lead.get("website") or ""),
        )

    if profile in ("b2b_legacy", "ofm", "whale_mass"):
        pain = sanitize_copy_text(re.sub(r"\s*[—–]\s*", ": ", str(obj.get("pain") or "")))
        pitch = sanitize_copy_text(re.sub(r"\s*[—–]\s*", ": ", str(obj.get("pitch") or "")))
        pain = re.sub(r"\s*\(generated\)\s*", "", pain, flags=re.I).strip()
        pitch = re.sub(r"\s*\(generated\)\s*", "", pitch, flags=re.I).strip()

        if profile == "whale_mass":
            source = str(lead.get("source") or lead.get("lead_source") or "").strip().lower()
            pool_usd = lead.get("pool_usd") if lead.get("pool_usd") is not None else lead.get("bounty_usd")
            pool_int = parse_pool_usd_value(pool_usd)
            if source == "whop_content_rewards" and pool_int is not None and pool_int >= 5000:
                if "content rewards" not in pain.lower() or f"{pool_int:,}" not in pain:
                    pain = whop_content_rewards_hook(brand=brand, pool_usd=pool_int)

            valid_whale_llm = (
                bool(pain)
                and bool(pitch)
                and "183.5" in pitch
                and "softugc" in pitch.lower()
                and not _REVSHARE_CPA_RE.search(pitch)
                and not _REVSHARE_CPA_RE.search(pain)
                and not _CYRILLIC_RE.search(pain + " " + pitch)
                and not _PLACEHOLDER_RE.search(pain + " " + pitch)
            )
            if valid_whale_llm:
                p_low = pitch.lower()
                has_scale = (
                    "owned account network" in p_low
                    and ("1 500+" in p_low or "1,500+" in p_low)
                    and ("13-40x" in p_low or "13 to 40x" in p_low)
                )
                effective_pitch = pitch if has_scale else WHALE_PITCH_TEXT
                target_brand = brand or "your brand"
                s1 = f"Hey,\n\n{pain}\n\n{effective_pitch}\n\n{WHALE_CTA_TEMPLATE.format(brand=target_brand)}"
                return sanitize_copy_text(s1), aud, True
        else:
            valid_legacy = (
                bool(pain)
                and bool(pitch)
                and "183.5" in pitch
                and "softugc" in pitch.lower()
                and not _CYRILLIC_RE.search(pain + " " + pitch)
                and not _PLACEHOLDER_RE.search(pain + " " + pitch)
            )
            if valid_legacy:
                s1 = f"Hey,\n\n{pain}\n\n{pitch}\n\n{REVSHARE_OFFER_TEXT}"
                return sanitize_copy_text(s1), aud, True
    else:
        raw_s1 = sanitize_copy_text(re.sub(r"\s*[—–]\s*", ", ", str(obj.get("step1_text") or obj.get("text") or "")))
        ok_lang, _ = check_copy_language(raw_s1, lang)
        if (
            raw_s1
            and ok_lang
            and not _PLACEHOLDER_RE.search(raw_s1)
            and not _GENERIC_OR_BAD_DRAFT_RE.search(raw_s1)
        ):
            if profile == "social_manual_bridge":
                one_line = " ".join(raw_s1.splitlines()).strip()
                low_line = one_line.lower()
                if "whatsapp" in low_line and "telegram" in low_line:
                    return one_line, aud, True
            elif profile in ("vacancy_ru", "vacancy_en"):
                if showcase in raw_s1 and ("$80" not in raw_s1):
                    return raw_s1, aud, True
            elif profile == "ru_expert":
                if not re.search(r"(?:100|200|300)\s*[kк]\s*₽", raw_s1, re.I):
                    return raw_s1, aud, True

    seq = build_outreach_sequence(
        lead,
        copy_profile=profile,
        language=lang,
        provider=prov,
        custom_step_templates=custom,
        showcase_url=showcase,
        use_llm=False,
    )
    return seq.step1_text, seq.audience, False


def generate_vlad_sonnet_batch(
    leads: list[dict],
    *,
    model: str = "claude-sonnet-5",
    timeout: int = 90,
    copy_profile: str = "",
    segment: str = "",
    language: str = "",
    provider: str = "telegram",
    custom_step_templates: dict[str, Any] | str | None = None,
    llm_post_fn: Callable[..., Any] | None = None,
) -> list[dict[str, str]]:
    """Generate individualized Step 1 + Step 2 {audience} via claude-sonnet-5 with deterministic fallback."""
    if not leads:
        return []

    prov = normalize_provider(provider)
    custom = _parse_custom_step_templates(custom_step_templates)

    resolved_profiles: list[str] = []
    resolved_langs: list[Literal["en", "ru"]] = []
    brands: list[str] = []
    showcases: list[str] = []

    for l in leads:
        req_lang = (language or "").strip().lower() or None
        lead_lang = str(l.get("language") or "").strip().lower() or None
        prof = resolve_copy_profile(
            copy_profile or l.get("copy_profile"),
            segment=segment or l.get("segment"),
            language=req_lang or lead_lang,
            source=str(l.get("source") or l.get("lead_source") or ""),
            default_profile="b2b_legacy",
        )
        lang = resolve_profile_language(prof, req_lang or lead_lang, lead=l)
        allow_cyr = lang == "ru"
        raw_comp = str(l.get("company") or l.get("project") or l.get("name") or "").strip()
        site = str(l.get("website") or l.get("project_url") or l.get("site") or "").strip()
        angle = str(l.get("their_angle") or l.get("one_liner") or "").strip()
        brand = clean_brand_name(raw_comp, site=site, their_angle=angle, allow_cyrillic=allow_cyr)
        resolved_profiles.append(prof)
        resolved_langs.append(lang)
        brands.append(brand)
        showcases.append(_resolve_showcase_url(custom=custom, lead=l))

    groups: dict[tuple[str, Literal["en", "ru"]], list[int]] = {}
    for idx, (prof, lang) in enumerate(zip(resolved_profiles, resolved_langs)):
        groups.setdefault((prof, lang), []).append(idx)

    by_idx_obj: dict[int, dict[str, Any]] = {}
    for (prof, lang), idxs in groups.items():
        items = []
        for idx in idxs:
            l = leads[idx]
            angle = str(l.get("their_angle") or l.get("what_they_do") or l.get("one_liner") or "").strip()
            site = str(l.get("website") or l.get("project_url") or l.get("site") or "").strip()
            items.append(
                {
                    "i": idx,
                    "brand": brands[idx],
                    "language": lang,
                    "category": str(l.get("category") or "").strip(),
                    "website": site,
                    "what_they_do": angle[:600],
                    "source": str(l.get("source") or l.get("lead_source") or "").strip(),
                    "pool_usd": l.get("pool_usd"),
                    "role": str(l.get("role") or l.get("title") or "").strip(),
                    "showcase_url": showcases[idx],
                }
            )
        sys_prompt = _system_prompt_for_profile(prof, custom=custom)
        parsed = _post_chat_completions(
            system_prompt=sys_prompt,
            user_items=items,
            model=model,
            timeout=timeout,
            llm_post_fn=llm_post_fn,
        )
        for obj in parsed:
            if "i" in obj:
                try:
                    by_idx_obj[int(obj["i"])] = obj
                except (TypeError, ValueError):
                    continue

    out: list[dict[str, str]] = []
    for idx, l in enumerate(leads):
        prof = resolved_profiles[idx]
        lang = resolved_langs[idx]
        obj = by_idx_obj.get(idx) or {}
        step1_text, aud, _ = _validate_and_assemble_llm_step1(
            profile=prof,
            lang=lang,
            lead=l,
            brand=brands[idx],
            obj=obj,
            prov=prov,
            showcase=showcases[idx],
            custom=custom,
        )
        # Re-apply custom step1 override if explicitly provided in custom_step_templates
        ov1, _, _ = _extract_step_override(custom, 1)
        if ov1 is not None:
            seq = build_outreach_sequence(
                l,
                copy_profile=prof,
                language=lang,
                provider=prov,
                custom_step_templates=custom,
                showcase_url=showcases[idx],
                use_llm=False,
            )
            step1_text = seq.step1_text
            aud = seq.audience
        out.append({"step1_text": step1_text, "audience": aud})
    return out


def generate_outreach_copy_batch(
    leads: list[Mapping[str, Any]],
    *,
    copy_profile: str | None = None,
    segment: str | None = None,
    language: str | None = None,
    provider: str = "telegram",
    custom_step_templates: dict[str, Any] | str | None = None,
    showcase_url: str = "",
    use_llm: bool = True,
    model: str = "claude-sonnet-5",
    timeout: int = 90,
    llm_post_fn: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Generate full multi-step outreach sequences (deterministic + optional LLM personalization) for any profile."""
    if not leads:
        return []

    prov = normalize_provider(provider)
    custom = _parse_custom_step_templates(custom_step_templates)
    key = (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    can_call_llm = bool(use_llm and (llm_post_fn is not None or key))

    llm_by_idx: dict[int, dict[str, str]] = {}
    if can_call_llm:
        try:
            llm_rows = generate_vlad_sonnet_batch(
                [dict(l) for l in leads],
                model=model,
                timeout=timeout,
                copy_profile=copy_profile or "",
                segment=segment or "",
                language=language or "",
                provider=prov,
                custom_step_templates=custom,
                llm_post_fn=llm_post_fn,
            )
            for idx, r in enumerate(llm_rows):
                llm_by_idx[idx] = r
        except Exception:
            llm_by_idx = {}

    results: list[dict[str, Any]] = []
    for idx, lead in enumerate(leads):
        seq = build_outreach_sequence(
            lead,
            copy_profile=copy_profile,
            segment=segment,
            language=language,
            provider=prov,
            custom_step_templates=custom,
            showcase_url=showcase_url,
            use_llm=bool(llm_by_idx.get(idx)),
            llm_result=llm_by_idx.get(idx),
        )
        results.append(seq.to_dict())
    return results


def build_ofm_vlad_copy_batch(rows: list[dict]) -> list[dict]:
    """Build Vlad's canonical 3-step English SoftUGC sequence for OFM agencies/buyers."""
    try:
        from app.runtime_config import should_allow_auto_sonnet as _allow_auto_sonnet

        sonnet_ok = _allow_auto_sonnet()
    except ImportError:
        sonnet_ok = (
            os.environ.get("CRMTG_VPS_RUNTIME") == "1"
            or os.environ.get("CRMTG_EPHEMERAL_RUNTIME") != "1"
            or os.environ.get("CRMTG_AUTO_SONNET") == "1"
        )
    api_key = (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    allow_sonnet = bool(api_key) and sonnet_ok

    ofm_fallback_step1 = (
        "Hey,\n\n"
        "Saw you're scaling creators and recruiting VAs for IG and X traffic. "
        "Hiring and warming manual accounts takes weeks, burns budget, and still hits shadowbans before steady volume kicks in.\n\n"
        f"{SOFTUGC_PITCH_TEXT}\n\n"
        f"{REVSHARE_OFFER_TEXT}"
    )

    sonnet_payloads: list[dict] = []
    for r in rows:
        channels_txt = ", ".join(str(c) for c in (r.get("channels") or []) if str(c).strip())
        sig = str(r.get("signal_summary") or "").strip()
        sample = str(r.get("sample_message") or r.get("their_angle") or "").strip()
        raw_comp = str(r.get("company") or r.get("project") or "").strip()
        person_name = str(r.get("name") or "").strip()
        if (
            raw_comp.lower()
            in {
                "ofm",
                "ofm agency",
                "franceofm",
                "ofmfrancenetwork",
                "networkofm",
                "mofmnetwork",
                "redditworldofm",
                "aimodelnetwork",
                "adultdeskai",
            }
            or (person_name and raw_comp.casefold() == person_name.casefold())
            or any(ord(ch) >= 0x2600 for ch in raw_comp)
        ):
            raw_comp = ""
        angle_parts = [p for p in (sig, channels_txt and f"Channels: {channels_txt}", sample[:420]) if p]
        sonnet_payloads.append(
            {
                "company": raw_comp,
                "name": str(r.get("name") or "").strip(),
                "website": str(r.get("project_url") or r.get("url") or "").strip(),
                "category": "OFM Agency",
                "their_angle": " | ".join(angle_parts)
                or "OFM agency scaling Instagram and X creator traffic via VAs",
                "source": str(r.get("source") or "ofm_tg"),
            }
        )

    sonnet_results: list[dict[str, str]] = [{} for _ in rows]
    if allow_sonnet and sonnet_payloads:
        chunks = [
            (list(range(start, min(start + 15, len(sonnet_payloads)))), sonnet_payloads[start : start + 15])
            for start in range(0, len(sonnet_payloads), 15)
        ]
        if len(chunks) > 1:
            with ThreadPoolExecutor(max_workers=min(3, len(chunks))) as pool:
                futs = [
                    (idxs, pool.submit(generate_vlad_sonnet_batch, chunk_p, model="claude-sonnet-5"))
                    for idxs, chunk_p in chunks
                ]
                for idxs, fut in futs:
                    try:
                        gen_list = fut.result()
                    except Exception:
                        gen_list = []
                    for i, gen_obj in zip(idxs, gen_list):
                        if isinstance(gen_obj, dict):
                            sonnet_results[i] = gen_obj
        else:
            try:
                gen_list = generate_vlad_sonnet_batch(chunks[0][1], model="claude-sonnet-5")
            except Exception:
                gen_list = []
            for i, gen_obj in zip(chunks[0][0], gen_list):
                if isinstance(gen_obj, dict):
                    sonnet_results[i] = gen_obj

    out: list[dict] = []
    for r, pay, gen_obj in zip(rows, sonnet_payloads, sonnet_results):
        s1 = str(gen_obj.get("step1_text") or "").strip()
        aud = str(gen_obj.get("audience") or "").strip().lower()
        if aud not in VALID_AUDIENCES:
            aud = "buyers"
        if (
            not s1
            or "SoftUGC" not in s1
            or REVSHARE_OFFER_TEXT not in s1
            or "Came across your project." in s1
        ):
            s1 = ofm_fallback_step1
        s1 = re.sub(r"\bCreatorOS\b", "SoftUGC", s1, flags=re.I)
        f1 = followup_one(
            str(pay.get("company") or "").strip() or "your agency",
            str(pay.get("their_angle") or ""),
            audience=aud,
            category="OFM",
            site=str(pay.get("website") or ""),
        )
        out.append(
            {
                "first_touch": s1,
                "step1_text": s1,
                "hook": s1,
                "audience": aud,
                "followup_1": f1,
                "step2_text": f1,
                "followup_2": VLAD_STEP3_TEXT,
                "step3_text": VLAD_STEP3_TEXT,
            }
        )
    return out


def big_client_offer() -> str:
    """Team note only. Do not paste this into a lead message."""
    return (
        "Текст для команды, не для отправки лиду. В рассылку это не ставить.\n"
        "Для кого: крупные — Roblox-подобные, лейблы, GTA.\n"
        "Что: клиппинг и AI UGC.\n"
        "Оплата за результат.\n"
        "Доказательство: кейс Roblox. Цифры вписывает Влад, миллионы не выдумывать."
    )
