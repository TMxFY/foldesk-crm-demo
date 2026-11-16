"""Clippers, freelancers, agencies, and service shops sell labour. Buyers own a product."""
from __future__ import annotations

import re
from urllib.parse import urlparse

REJECTED_BUYER_CATEGORIES = frozenset(
    {
        "агентство",
        "сервис / агентство",
        "партнёр, не ца",
        "партнер, не ца",
        "низкий приоритет",
    }
)

NON_BUYER_ROLES = frozenset(
    {
        "Community",
        "Creator/KOL",
        "Dev",
        "Trader",
        "Investor",
        "Other",
    }
)

PLATFORM_AGGREGATOR_HOSTS = (
    "x.com",
    "twitter.com",
    "mobile.twitter.com",
    "t.co",
    "linkedin.com",
    "instagram.com",
    "t.me",
    "telegram.me",
    "telegram.dog",
    "telegram.org",
    "telegra.ph",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "facebook.com",
    "fb.com",
    "reddit.com",
    "pinterest.com",
    "threads.net",
    "vk.com",
    "vc.ru",
    "habr.com",
    "discord.gg",
    "discord.com",
    "wa.me",
    "whatsapp.com",
    "api.whatsapp.com",
    "gmail.com",
    "bit.ly",
    "tinyurl.com",
    "beacons.ai",
    "solo.to",
    "carrd.co",
    "github.com",
    "gitlab.com",
    "lu.ma",
    "luma.com",
    "news.ycombinator.com",
    "ycombinator.com",
    "huggingface.co",
    "itunes.apple.com",
    "apps.apple.com",
    "play.google.com",
    "chromewebstore.google.com",
    "linktr.ee",
    "linktree.com",
    "notion.site",
    "notion.so",
    "calendly.com",
    "medium.com",
    "substack.com",
    "docs.google.com",
    "drive.google.com",
)

PLATFORM_GARBAGE_COMPANY_NAMES = frozenset(
    {
        "lu",
        "luma",
        "news",
        "huggingface",
        "itunes",
        "apps",
        "play",
        "linktr",
        "linktree",
        "notion",
        "calendly",
        "medium",
        "substack",
        "docs",
        "drive",
        "github",
        "gitlab",
        "ycombinator",
        "telegram",
        "twitter",
        "linkedin",
        "instagram",
        "youtube",
        "discord",
    }
)

# Unconditional vendor / agency / freelancer / spammer signals.
# These ALWAYS win, even if the bio also says "Founder", "CEO", "hiring", or "my community".
_UNCONDITIONAL_SUPPLY_OR_AGENCY = re.compile(
    r"got accepted into.{0,40}content rewards|my (?:last|next) payout|"
    r"getting paid for (?:my|our) content|revenue sharing program|"
    r"original content rewards|clipster withdrawal|x pays creators|@xcreators|"
    r"\bfreelancer\b|\bfreelance\s+(?:video|editor|marketer|dev|developer|designer|writer|creator)\b|"
    r"\bvideo\s+creator\b|\bambassador\b|"
    r"\bshiller\b|\bhype\s+builder\b|\bkol\s+manager\b|"
    r"\bcommunity\s+moderator\b|\bcommunity\s+manager\b|"
    r"\bgtm\s+consultanc\w*\b|"
    r"\b(?:marketing|growth|pr|smm|seo|web3|kol|shilling|clipping|ugc|video|dev|development|automation)\s+(?:agenc\w*|consultanc\w*)\b|"
    r"\bconsultanc\w*\s+for\s+(?:ai|web3|crypto|saas|startups|brands|blockchain|tech|b2b|ecom\w*)\b|"
    r"helping\s+(?:projects|brands|online\s+stores|stores|merchants|founders|startups|clients|businesses|companies)\s+"
    r"(?:trend|get\s+consistent|gain\s+real\s+traction|grow|scale|boost|get\s+more\s+sales)|"
    r"\bshopify\s+merchant\s+help\b|\bmerchant\s+help\s+and\s+developer\b|"
    r"разрабатываем\s+ии-агентов|создаю\s+автономных\s+ии-агентов|"
    r"сервис\s+информационной\s+безопасности|разработк\w*\s+(?:ботов|телеграм-ботов|tg-ботов|сайтов)|"
    r"seo-аудит,\s*продвижение\s+и\s+создание\s+сайтов|"
    r"\baccount\s+recovery\s+apis?\b|\bethical\s+hacking\b|"
    r"\btelegram\s+bot\s+setup\b|\bmanaged\s+hosting\b.{0,45}\b(?:music|autofilter|files|24/7)\b|"
    r"\bjava\s+build\s+tool\b|\bold\s+css\s+hack\b|"
    r"\bi\s+clip\b|\bi['’]?m\s+a\s+clipper\b|\bi\s+am\s+a\s+clipper\b|\bwe\s+clip\b|"
    r"\$\s*\d+\s*(?:per|/)\s*(?:clip|video|gig)\b|\bper\s+gig\b|"
    r"\[for\s*hire\]|\bfor\s+hire\b|\bopen\s+to\s+work\b|"
    r"looking\s+for\s+(?:a\s+)?job|looking\s+for\s+work|ищу\s+работу|ищу\s+заказы|"
    r"партн[её]р,\s*не\s*ца|сервис\s*/\s*агентство",
    re.I,
)

_UGC_CREATOR_RE = re.compile(r"\bugc\s+creator\b", re.I)
_BUYING_UGC_CREATOR_RE = re.compile(
    r"\b(?:looking\s+for|hiring|need|seeking|hire|find)\s+(?:a\s+|an\s+|some\s+)?ugc\s+creators?\b",
    re.I,
)

# Explicit clipper-recruitment phrases override soft "portfolio" mentions only.
_BUYER = re.compile(
    r"\bwe\s+acquire\b|\bwe\s+buy\s+and\s+grow\b|\bacquisition\s+studio\b|"
    r"looking\s+for\s+clippers|hiring\s+clippers|need\s+clippers|"
    r"paying\s+clippers|pay\s+per\s+view|"
    r"my\s+students|my\s+cohort|my\s+course",
    re.I,
)
_SUPPLY = re.compile(
    r"\bportfolio\b|no\s+followers\s+needed",
    re.I,
)

_RAW_COHORT_RE = re.compile(
    r"^(?:yc\s*)?[swfx]\d{2}$|^yc\s+(?:winter|summer|fall|spring)\s+20\d{2}$",
    re.I,
)
_INSUFFICIENT_PRODUCT_INFO_RE = re.compile(
    r"(?:недостаточно\s+(?:информации|данных)(?:\s+о\s+продукте)?|"
    r"insufficient\s+information\s+about\s+(?:the\s+)?product|"
    r"not\s+enough\s+information\s+about\s+(?:the\s+)?product)",
    re.I,
)


def supply_reason(text: str) -> str:
    """Matched supply/agency phrase, or empty when this person may be a product buyer."""
    blob = text or ""
    early = _UNCONDITIONAL_SUPPLY_OR_AGENCY.search(blob)
    if early:
        return early.group(0)
    ugc_m = _UGC_CREATOR_RE.search(blob)
    if ugc_m and not _BUYING_UGC_CREATOR_RE.search(blob):
        return ugc_m.group(0)
    if _BUYER.search(blob):
        return ""
    match = _SUPPLY.search(blob)
    return match.group(0) if match else ""


def is_supply_side(text: str) -> bool:
    """True when the author is selling labour/agency services rather than buying traffic for own product."""
    return bool(supply_reason(text))


def is_rejected_buyer_category(category: str | None) -> bool:
    cat = " ".join(str(category or "").strip().lower().split())
    if not cat:
        return False
    if cat in REJECTED_BUYER_CATEGORIES:
        return True
    return any(
        tok in cat
        for tok in ("сервис / агентство", "партнёр, не ца", "партнер, не ца", "низкий приоритет")
    )


def is_raw_cohort_tag(value: str | None) -> bool:
    cleaned = " ".join(str(value or "").strip().split())
    return bool(cleaned and _RAW_COHORT_RE.fullmatch(cleaned))


def has_insufficient_product_info(text: str | None) -> bool:
    return bool(text and _INSUFFICIENT_PRODUCT_INFO_RE.search(str(text)))


def is_platform_or_social_url(url: str | None) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    if not host:
        return False
    return any(host == item or host.endswith("." + item) for item in PLATFORM_AGGREGATOR_HOSTS)


def is_invalid_product_name(name: str | None) -> bool:
    cleaned = " ".join(str(name or "").strip().split())
    if not cleaned:
        return True
    low = cleaned.lower().strip(".-_@/ ")
    if not low or len(low) < 2:
        return True
    if low in {"none", "null", "n/a", "na", "unknown", "your product", "stealth"}:
        return True
    if low in PLATFORM_GARBAGE_COMPANY_NAMES:
        return True
    if is_raw_cohort_tag(cleaned):
        return True
    if has_insufficient_product_info(cleaned):
        return True
    return False


def is_truncated_or_bad_pitch(pitch: str | None, *, min_len: int = 40) -> bool:
    cleaned = str(pitch or "").strip()
    if len(cleaned) < min_len:
        return True
    if is_raw_cohort_tag(cleaned):
        return True
    if has_insufficient_product_info(cleaned):
        return True
    return False

