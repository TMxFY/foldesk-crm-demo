#!/usr/bin/env python3
"""Canonical B2B SoftUGC outreach generator (Vlad's 3-step sequence via claude-sonnet-5 + deterministic fallback)."""
from __future__ import annotations

import json
import os
import re
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
VLAD_STEP2_TEMPLATE = (
    "Hey, regarding last message, we could bring you 500K-2M reach to your targeted audience "
    "(which equals highly qualified {audience})\n\n"
    "So if you’re down just lmk if you wanna see the demo reels and I’ll send them to you, "
    "so we could break the ice here"
)
VLAD_STEP3_TEXT = "You’re doing me dirty mate... :((("
VLAD_STEP3_IMAGE_URL = "/media/outreach/4fa85f083f9e2911.jpg"

VALID_AUDIENCES = frozenset({"buyers", "students", "users", "players", "listeners"})

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
        "beacons",
        "carrd",
        "docs",
        "drive",
        "chromewebstore",
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


def _brand_from_site(site: str, their_angle: str = "") -> str:
    s = (site or "").strip()
    if not s:
        return ""
    host = (urlparse(s if "://" in s else f"https://{s}").hostname or "").lower().removeprefix("www.")
    if not host or host.endswith((".ru", ".su", ".рф", ".xn--p1ai")):
        return ""
    parts = [p for p in host.split(".") if p]
    if not parts:
        return ""
    base = parts[-2] if len(parts) >= 3 and parts[0] in {"app", "get", "try", "use", "go", "my", "hey", "docs", "join"} else parts[0]
    if not base or base in _GENERIC_SITE_BASES or len(base) < 2:
        return ""
    return _humanize_slug(base, their_angle=their_angle)


def clean_brand_name(raw_name: str, site: str = "", their_angle: str = "") -> str:
    """Extract a clean English product/brand name, or '' if only a person/handle/slug noise exists."""
    raw = (raw_name or "").strip()
    raw = _PLACEHOLDER_RE.sub("", raw).strip()
    raw = _HN_PREFIX_RE.sub("", raw).strip()
    raw = re.sub(r"^\d+[\.\)]\s*", "", raw).strip()
    raw = re.sub(r"https?://\S+", "", raw, flags=re.I).strip().strip("/")

    # Handle "Jeremy | OpenEden" -> "OpenEden", vs "OpenEden | RWA" -> "OpenEden"
    if "|" in raw:
        pieces = [p.strip() for p in raw.split("|") if p.strip()]
        if len(pieces) >= 2 and _looks_like_person_only(pieces[0]) and not _looks_like_person_only(pieces[1]):
            raw = pieces[1]
        elif pieces:
            raw = pieces[0]

    # Strip trailing parenthetical ("ScreenStudio (YC W26)" -> "ScreenStudio")
    raw = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
    # Strip " — subtitle" or " - subtitle" if brand precedes description
    raw = re.split(r"\s+[—–:]\s+|\s+-\s+", raw, maxsplit=1)[0].strip()

    if raw.startswith("@") or "_" in raw or len(raw) > 36 or _looks_like_person_only(raw):
        raw = ""

    if raw and raw == raw.lower() and " " not in raw:
        raw = _humanize_slug(raw, their_angle=their_angle)

    if not raw:
        raw = _brand_from_site(site, their_angle=their_angle)

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

    if _CYRILLIC_RE.search(raw):
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


def first_touch(
    product: str,
    niche: str,
    russian: bool = False,
    *,
    source: str = "",
    category: str = "",
    site: str = "",
) -> str:
    """Generate Vlad's canonical 3-paragraph (+ greeting) SoftUGC first touch."""
    if russian:
        name = _product(product, True)
        return (
            "Привет,\n\n"
            f"Видел {name}. {_RU_PAIN}\n\n"
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
    return (
        "Hey,\n\n"
        f"{pain_para}\n\n"
        f"{SOFTUGC_PITCH_TEXT}\n\n"
        f"{REVSHARE_OFFER_TEXT}"
    )


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


def upgrade_to_vlad_step1(
    existing_text: str | None,
    *,
    company: str = "",
    name: str = "",
    site: str = "",
    category: str = "",
    their_angle: str = "",
    source: str = "",
) -> str:
    """Keep genuine custom LLM-written pain/pitch paragraphs and upgrade CTA to Vlad's exact offer; rebuild generic/broken drafts."""
    raw = str(existing_text or "").replace("\u2028", "\n").replace("\r\n", "\n").strip()
    brand = clean_brand_name(company or name, site=site, their_angle=their_angle)

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
) -> str:
    if russian:
        name = _product(product, True)
        return f"Рассказать, как мы можем получить это для {name}?"
    aud = (audience or "").strip().lower()
    if aud not in VALID_AUDIENCES:
        aud = infer_lead_audience(category=category, company=product, their_angle=niche, site=site)
    return VLAD_STEP2_TEMPLATE.format(audience=aud)


def followup_two(product: str = "", russian: bool = False) -> str:
    if russian:
        name = _product(product, True)
        return f"Интересно органическое продвижение {name}?"
    return VLAD_STEP3_TEXT


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


def generate_vlad_sonnet_batch(
    leads: list[dict],
    *,
    model: str = "claude-sonnet-5",
    timeout: int = 90,
) -> list[dict[str, str]]:
    """Generate individualized Vlad Step 1 + Step 2 {audience} for a batch of leads via claude-sonnet-5 on Relay Fast."""
    import urllib.request

    if not leads:
        return []

    key = (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    base = (os.environ.get("LLM_BASE_URL") or "https://relay.fast/v1").rstrip("/")
    if not key:
        raise RuntimeError("RELAYFAST_API_KEY not set")

    items = []
    brands = []
    for idx, l in enumerate(leads):
        raw_comp = str(l.get("company") or l.get("name") or "").strip()
        site = str(l.get("website") or l.get("site") or "").strip()
        angle = str(l.get("their_angle") or l.get("one_liner") or "").strip()
        brand = clean_brand_name(raw_comp, site=site, their_angle=angle)
        brands.append(brand)
        items.append(
            {
                "i": idx,
                "brand": brand,
                "category": str(l.get("category") or "").strip(),
                "website": site,
                "what_they_do": angle[:600],
            }
        )

    try:
        from leadgen import relay_route
    except ImportError:
        import relay_route  # type: ignore[no-redef]

    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": VLAD_SONNET_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            "temperature": 0.4,
            "max_tokens": max(600, 380 * len(items)),
        },
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=relay_route._ssl_context(), timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    content = str(payload["choices"][0]["message"]["content"] or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", content, flags=re.DOTALL)
    if not m:
        raise RuntimeError(f"claude-sonnet-5 returned non-JSON: {content[:200]}")
    parsed = json.loads(m.group(0))
    by_i = {int(obj.get("i", -1)): obj for obj in parsed if isinstance(obj, dict)}

    out: list[dict[str, str]] = []
    for idx, l in enumerate(leads):
        obj = by_i.get(idx) or {}
        pain = re.sub(r"\s*[—–]\s*", ": ", str(obj.get("pain") or "").strip())
        pitch = re.sub(r"\s*[—–]\s*", ": ", str(obj.get("pitch") or "").strip())
        pain = re.sub(r"\s*\(generated\)\s*", "", pain, flags=re.I).strip()
        pitch = re.sub(r"\s*\(generated\)\s*", "", pitch, flags=re.I).strip()
        aud = str(obj.get("audience") or "").strip().lower()
        if aud not in VALID_AUDIENCES:
            aud = infer_lead_audience(
                category=str(l.get("category") or ""),
                company=brands[idx] or str(l.get("company") or ""),
                their_angle=str(l.get("their_angle") or ""),
                site=str(l.get("website") or ""),
            )

        if (
            pain
            and pitch
            and "183.5" in pitch
            and "softugc" in pitch.lower()
            and not _CYRILLIC_RE.search(pain + " " + pitch)
            and not _PLACEHOLDER_RE.search(pain + " " + pitch)
        ):
            step1_text = f"Hey,\n\n{pain}\n\n{pitch}\n\n{REVSHARE_OFFER_TEXT}"
        else:
            step1_text = first_touch(
                brands[idx] or str(l.get("company") or l.get("name") or ""),
                str(l.get("their_angle") or ""),
                russian=False,
                source=str(l.get("source") or ""),
                category=str(l.get("category") or ""),
                site=str(l.get("website") or ""),
            )
        out.append({"step1_text": step1_text, "audience": aud})
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
