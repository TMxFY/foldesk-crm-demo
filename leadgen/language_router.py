#!/usr/bin/env python3
"""Deep module for lead language detection, segment classification, campaign routing, and copy guards."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import Any, Literal
from urllib.parse import urlparse

LeadLanguage = Literal["ru", "en", "ar"]

CAMPAIGN_WHALES_EN_TG = "Whales EN — TG"
CAMPAIGN_WHALES_EN_WA = "Whales EN — WA"
CAMPAIGN_VACANCY_RU_TG = "Client Reels & Вакансии RU — TG"
CAMPAIGN_VACANCY_RU_WA = "Client Reels & Вакансии RU — WA"
CAMPAIGN_VACANCY_EN_TG = "Client Reels & Вакансии EN — TG"
CAMPAIGN_RU_EXPERTS_TG = "РФ-Эксперты (Продюсирование) — TG"
CAMPAIGN_OFM_TG = "SoftUGC — OFM (TG)"
CAMPAIGN_GCC_MENA_TG = "GCC / MENA — TG"
CAMPAIGN_GCC_MENA_WA = "GCC / MENA — WA"
QUEUE_MANUAL_SOCIAL = "Manual Social Queue"
QUEUE_ASSISTED_JOB = "Assisted Job Queue"

EXPLICIT_SEGMENTS: frozenset[str] = frozenset(
    {
        "whale",
        "vacancy",
        "client_reels",
        "ru_expert",
        "social_manual",
        "vacancy_assisted",
        "ofm",
        "gcc_mena",
    }
)

_CYRILLIC_TEXT_FIELDS: tuple[str, ...] = (
    "name",
    "first_name",
    "last_name",
    "bio",
    "what_they_do",
    "title",
    "description",
    "note",
    "company",
    "project",
    "sample",
    "one_liner",
    "their_angle",
)
_DOMAIN_FIELDS: tuple[str, ...] = (
    "company_domain",
    "domain",
    "website",
    "url",
    "project_url",
    "site",
    "site_url",
    "employer_url",
    "email",
    "manual_action_url",
    "alternate_url",
    "source_url",
    "link",
    "links",
)
_RU_DOMAIN_SUFFIXES: tuple[str, ...] = (".ru", ".рф", ".by", ".kz", ".xn--p1ai")
_GCC_COUNTRY_CODES: frozenset[str] = frozenset({"SA", "AE", "QA", "KW", "BH", "OM", "EG", "JO"})
_GCC_DOMAIN_SUFFIXES: tuple[str, ...] = (".sa", ".ae", ".qa", ".kw", ".bh", ".om", ".eg", ".jo")

_RU_EXACT_SOURCES: frozenset[str] = frozenset(
    {
        "hh_ru",
        "hh_vacancies",
        "hh.ru",
        "hh",
        "profi_ru",
        "profi.ru",
        "profi",
        "kwork",
        "kwork.ru",
        "ru_experts",
        "ru_expert",
        "tg_jobs_ru",
        "vacancies_ru",
        "tg_jobs",
        "tg_vacancies",
        "tg вакансии",
    }
)
_RU_SOURCE_PREFIXES: tuple[str, ...] = (
    "hh_ru",
    "hh_vacancies",
    "hh.ru",
    "profi_ru",
    "profi.ru",
    "kwork",
    "ru_expert",
    "tg_jobs_ru",
    "vacancies_ru",
    "tg вакансии",
)

_VACANCY_EXACT_SOURCES: frozenset[str] = frozenset(
    {
        "hh_ru",
        "hh_vacancies",
        "hh.ru",
        "hh",
        "profi_ru",
        "profi.ru",
        "profi",
        "kwork",
        "kwork.ru",
        "upwork",
        "linkedin_jobs",
        "tg_jobs",
        "tg_jobs_ru",
        "tg_jobs_en",
        "vacancies_ru",
        "vacancies",
        "job_monitor",
        "tg_vacancies",
        "tg вакансии",
    }
)
_VACANCY_SOURCE_PREFIXES: tuple[str, ...] = (
    "hh_ru",
    "hh_vacancies",
    "hh.ru",
    "profi_ru",
    "profi.ru",
    "kwork",
    "upwork",
    "linkedin_jobs",
    "tg_jobs",
    "vacancies_ru",
    "job_monitor",
    "tg вакансии",
)

_WHALE_EXACT_SOURCES: frozenset[str] = frozenset({"yc", "meta_ads", "ph_detailed"})
_WHALE_SOURCE_PREFIXES: tuple[str, ...] = (
    "meta_ad",
    "whop",
    "app_store",
    "tiktok",
    "crypto",
    "yc_",
    "ph_detailed",
)

_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_EN_MARKER_RE = re.compile(r"(?:^|[_\-\s:])en(?:$|[_\-\s:])")
_TOKEN_SPLIT_RE = re.compile(r"[\s,;]+")

_URL_RE = re.compile(
    r"(?:https?://\S+|www\.\S+|"
    r"(?:t\.me|telegram\.me|wa\.me|youtube\.com|youtu\.be|"
    r"instagram\.com|tiktok\.com|linkedin\.com|hh\.ru|profi\.ru|kwork\.ru|softugc\.com)(?:/\S*)?|"
    r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|io|ai|co|app|dev|org|net|ru|by|kz|me|рф|sa|ae|qa|kw|bh|om|eg|jo)(?:/\S*)?)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{2,}")
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z0-9_]+\}")
_METRIC_RE = re.compile(r"\b\d+(?:[.,]\d+)?[kKmMкКмМ%xх]*\b")
_NEUTRAL_BRAND_RE = re.compile(
    r"\b(?:"
    r"softugc|ai|ugc|ofm|tg|wa|ru|en|ar|b2b|saas|d2c|ecom|e-com|crm|api|sdk|"
    r"cpm|cpa|roi|ltv|ctr|kpi|hr|ceo|cmo|bd|ua|pr|smm|seo|kol|web3|defi|revshare|"
    r"reels|shorts|tiktok|instagram|snapchat|youtube|telegram|whatsapp|linkedin|"
    r"upwork|kwork|profi|manychat|heygen|kling|seedance|omni|elevenlabs|"
    r"higgsfield|nano|banana|pro|faceless|runway|midjourney|chatgpt|claude|"
    r"sora|veo|whop|meta|shopify|salla|zid|airtable|stripe|github|product|hunt|"
    r"app|store|google|play|founder|hh|gcc|mena|ksa|uae"
    r")\b",
    re.IGNORECASE,
)

_MIN_RU_CYRILLIC_CHARS = 2
_MIN_RU_CYRILLIC_RATIO = 0.35
_MIN_AR_CHARS = 4
_MIN_AR_RATIO = 0.35
_MIN_EN_LATIN_CHARS = 2


@dataclass(frozen=True, slots=True)
class LeadRoute:
    """Resolved routing classification for a single lead row."""

    language: LeadLanguage
    segment: str
    provider: str
    target_campaign: str


def _row_get(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _iter_strings(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        s = value.strip()
        if s:
            yield s
        return
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        for item in value:
            yield from _iter_strings(item)


def _extract_hostname(token: str) -> str:
    raw = token.strip().strip("<>\"'()")
    if not raw or raw.startswith("@"):
        return ""
    low = raw.casefold()
    if low.startswith("mailto:"):
        raw = raw[7:].strip()
    if "@" in raw and "://" not in raw:
        _, _, domain_part = raw.partition("@")
        raw = domain_part.strip().split("/", 1)[0]
    if not raw:
        return ""
    if "://" not in raw:
        raw = "//" + raw.lstrip("/")
    try:
        return (urlparse(raw).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _hostname_matches_suffixes(host: str, suffixes: tuple[str, ...]) -> bool:
    if not host:
        return False
    candidates = [host]
    try:
        candidates.append(host.encode("ascii").decode("idna").casefold())
    except UnicodeError:
        pass
    return any(cand.endswith(suffixes) for cand in candidates)


def _row_has_domain_suffix(row: Mapping[str, Any], suffixes: tuple[str, ...]) -> bool:
    for field in _DOMAIN_FIELDS:
        for val in _iter_strings(_row_get(row, field)):
            for token in _TOKEN_SPLIT_RE.split(val):
                if token and _hostname_matches_suffixes(_extract_hostname(token), suffixes):
                    return True
    return False


def _row_has_ru_domain(row: Mapping[str, Any]) -> bool:
    return _row_has_domain_suffix(row, _RU_DOMAIN_SUFFIXES)


def _row_has_gcc_domain(row: Mapping[str, Any]) -> bool:
    return _row_has_domain_suffix(row, _GCC_DOMAIN_SUFFIXES)


def _row_sources(row: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("source", "lead_source"):
        val = _clean_str(_row_get(row, key)).casefold()
        if val and val not in out:
            out.append(val)
    return out


def _is_ru_source(src: str) -> bool:
    if not src:
        return False
    if src in _RU_EXACT_SOURCES:
        return True
    if any(src.startswith(prefix) for prefix in _RU_SOURCE_PREFIXES):
        return True
    if src.startswith("tg_jobs") and not _EN_MARKER_RE.search(src):
        return True
    return False


def _is_vacancy_source(src: str) -> bool:
    if not src:
        return False
    if src in _VACANCY_EXACT_SOURCES:
        return True
    return any(src.startswith(prefix) for prefix in _VACANCY_SOURCE_PREFIXES)


def _is_whale_source(src: str) -> bool:
    if not src:
        return False
    if src in _WHALE_EXACT_SOURCES:
        return True
    return any(src.startswith(prefix) for prefix in _WHALE_SOURCE_PREFIXES)


def _has_tg_contact(row: Mapping[str, Any]) -> bool:
    for key in ("username", "telegram_username", "tg_username", "tg"):
        val = _clean_str(_row_get(row, key)).lstrip("@")
        if val and val.casefold() not in {"none", "null", "n/a", "-"}:
            return True
    tg_uid = _row_get(row, "telegram_user_id")
    if tg_uid not in (None, "", 0, "0", False):
        return True
    if _row_get(row, "has_tg") is True:
        return True
    for url_key in ("url", "tg_url", "telegram_url"):
        url_val = _clean_str(_row_get(row, url_key)).casefold()
        if "t.me/" in url_val or "telegram.me/" in url_val:
            return True
    return False


def _has_wa_contact(row: Mapping[str, Any]) -> bool:
    for key in ("whatsapp", "whatsapp_jid", "wa_url"):
        val = _clean_str(_row_get(row, key))
        if val and val.casefold() not in {"none", "null", "n/a", "-"}:
            return True
    if _row_get(row, "whatsapp_exists") is True or _row_get(row, "has_wa") is True:
        return True
    phone = _clean_str(_row_get(row, "phone"))
    prov = _clean_str(_row_get(row, "provider")).casefold()
    return bool(
        phone
        and prov == "whatsapp"
        and _row_get(row, "whatsapp_exists") is not False
        and not bool(_row_get(row, "wa_check_pending"))
    )


def _is_marked_vacancy_assisted(row: Mapping[str, Any], sources: list[str]) -> bool:
    for flag in ("assisted", "vacancy_assisted", "requires_manual_action", "manual_only"):
        if bool(_row_get(row, flag)):
            return True
    queue_val = _clean_str(_row_get(row, "queue") or _row_get(row, "target_queue")).casefold()
    if queue_val in {"vacancy_assisted", "assisted", "assisted_job", "assisted_job_queue"}:
        return True
    status_val = _clean_str(_row_get(row, "status")).casefold()
    if status_val in {"vacancy_assisted", "assisted_job"}:
        return True
    if any("assisted" in src for src in sources):
        return True
    manual_url = _clean_str(_row_get(row, "manual_action_url"))
    return bool(manual_url and not (_has_tg_contact(row) or _has_wa_contact(row)))


def _is_arabic_lang_tag(language: Any) -> bool:
    raw = _clean_str(language).casefold()
    return raw in {"ar", "ara", "arabic"} or raw.startswith(("ar-", "ar_"))


def _normalize_language(language: str | None) -> LeadLanguage:
    raw = _clean_str(language).casefold()
    if raw in {"ru", "rus", "russian"} or raw.startswith(("ru-", "ru_")):
        return "ru"
    if _is_arabic_lang_tag(raw):
        return "ar"
    return "en"


def _normalize_provider(provider: str | None) -> str:
    raw = _clean_str(provider).casefold()
    if raw in {"wa", "whatsapp"}:
        return "whatsapp"
    return "telegram"


def _cfg_nested_str(cfg: Mapping[str, Any] | None, section: str, key: str) -> str:
    if not isinstance(cfg, Mapping):
        return ""
    sub = cfg.get(section)
    if not isinstance(sub, Mapping):
        return ""
    return _clean_str(sub.get(key))


def is_gcc_mena_lead(row: Mapping[str, Any]) -> bool:
    """Return True when a lead exhibits GCC/MENA country, domain, Arabic script, or AR hint signals."""
    cc = _clean_str(_row_get(row, "country_code")).upper()
    if cc in _GCC_COUNTRY_CODES:
        return True
    for key in ("language_hint", "language"):
        if _is_arabic_lang_tag(_row_get(row, key)):
            return True
    for src in _row_sources(row):
        if src == "ar" or src.startswith(("ar_", "ar-", "gcc", "mena")) or "_gcc" in src or "_mena" in src:
            return True
    if _row_has_gcc_domain(row):
        return True
    for field in _CYRILLIC_TEXT_FIELDS + ("pain_quote", "role"):
        for text in _iter_strings(_row_get(row, field)):
            if _ARABIC_RE.search(text):
                return True
    return False


def detect_lead_language(row: Mapping[str, Any]) -> LeadLanguage:
    """Detect whether a lead should receive RU, AR, or EN outreach based on text, domains, and source."""
    for field in _CYRILLIC_TEXT_FIELDS:
        for text in _iter_strings(_row_get(row, field)):
            if _CYRILLIC_RE.search(text):
                return "ru"
    if _row_has_ru_domain(row):
        return "ru"
    for src in _row_sources(row):
        if _is_ru_source(src):
            return "ru"
    for key in ("language", "language_hint"):
        if _is_arabic_lang_tag(_row_get(row, key)):
            return "ar"
    for field in _CYRILLIC_TEXT_FIELDS:
        for text in _iter_strings(_row_get(row, field)):
            if _ARABIC_RE.search(text):
                return "ar"
    return "en"


def detect_lead_segment(row: Mapping[str, Any]) -> str:
    """Classify a lead into its outreach segment from explicit segment or source/contact signals."""
    explicit = _clean_str(_row_get(row, "segment")).casefold()
    if explicit in EXPLICIT_SEGMENTS:
        return explicit

    sources = _row_sources(row)
    for src in sources:
        if src.startswith("ofm"):
            return "ofm"
    for src in sources:
        if src.startswith("ru_expert"):
            return "ru_expert"
    if any(_is_vacancy_source(src) for src in sources):
        if _is_marked_vacancy_assisted(row, sources):
            return "vacancy_assisted"
        return "vacancy"
    if any(_is_whale_source(src) for src in sources):
        return "whale"
    return "b2b"


def resolve_target_campaign(
    segment: str,
    language: str,
    provider: str,
    cfg: Mapping[str, Any] | None = None,
) -> str:
    """Resolve the canonical CRM campaign or queue name for (segment, language, provider)."""
    seg = _clean_str(segment).casefold()
    lang = _normalize_language(language)
    prov = _normalize_provider(provider)

    if seg == "ru_expert":
        return _cfg_nested_str(cfg, "ru_experts", "target_campaign") or CAMPAIGN_RU_EXPERTS_TG
    if seg == "ofm":
        return _cfg_nested_str(cfg, "b2b", "target_campaign_ofm") or CAMPAIGN_OFM_TG
    if seg == "social_manual":
        return QUEUE_MANUAL_SOCIAL
    if seg == "vacancy_assisted":
        return QUEUE_ASSISTED_JOB
    if seg == "gcc_mena" or lang == "ar":
        return CAMPAIGN_GCC_MENA_WA if prov == "whatsapp" else CAMPAIGN_GCC_MENA_TG
    if seg == "whale":
        return CAMPAIGN_WHALES_EN_WA if prov == "whatsapp" else CAMPAIGN_WHALES_EN_TG
    if seg in {"vacancy", "client_reels"}:
        if lang == "ru":
            return CAMPAIGN_VACANCY_RU_WA if prov == "whatsapp" else CAMPAIGN_VACANCY_RU_TG
        return CAMPAIGN_WHALES_EN_WA if prov == "whatsapp" else CAMPAIGN_VACANCY_EN_TG

    if lang == "ru":
        return CAMPAIGN_VACANCY_RU_WA if prov == "whatsapp" else CAMPAIGN_VACANCY_RU_TG

    cfg_key = "target_campaign_wa" if prov == "whatsapp" else "target_campaign_tg"
    configured = _cfg_nested_str(cfg, "b2b", cfg_key)
    if configured and "база все2" not in configured.casefold():
        return configured
    return CAMPAIGN_WHALES_EN_WA if prov == "whatsapp" else CAMPAIGN_WHALES_EN_TG


def _strip_non_prose_tokens(text: str) -> str:
    cleaned = _URL_RE.sub(" ", text)
    cleaned = _EMAIL_RE.sub(" ", cleaned)
    cleaned = _HANDLE_RE.sub(" ", cleaned)
    cleaned = _PLACEHOLDER_RE.sub(" ", cleaned)
    cleaned = _METRIC_RE.sub(" ", cleaned)
    return cleaned


def _strip_neutral_tokens(text: str) -> str:
    return _NEUTRAL_BRAND_RE.sub(" ", _strip_non_prose_tokens(text))


_CYRILLIC_WORD_RE = re.compile(r"[А-Яа-яЁё]+")
_EN_DISALLOWED_CYRILLIC_WORDS: frozenset[str] = frozenset(
    {
        "привет",
        "здравствуйте",
        "добрый",
        "доброе",
        "спасибо",
        "пока",
        "мы",
        "наш",
        "смотри",
        "смотрите",
        "подскажите",
        "хотите",
        "можем",
        "актуально",
        "интересно",
        "коллеги",
        "ребята",
        "друзья",
        "давай",
        "давайте",
    }
)


def assert_copy_matches_language(
    text: str,
    expected_language: LeadLanguage,
    *,
    allow_cyrillic_name: bool = True,
) -> tuple[bool, str]:
    """Validate that generated or template outreach copy matches the campaign's expected language."""
    lang = _clean_str(expected_language).casefold()
    if lang not in {"ru", "en", "ar"}:
        return False, f"Unsupported expected_language: {expected_language!r}"

    raw = _clean_str(text)
    if not raw:
        return False, "Outreach copy text is empty"

    if lang == "ar":
        prose = _strip_neutral_tokens(raw)
        if _CYRILLIC_RE.search(prose):
            return False, "AR campaign copy must not contain Cyrillic text"
        ar_count = len(_ARABIC_RE.findall(prose))
        if ar_count < _MIN_AR_CHARS:
            return False, "AR campaign copy must contain Arabic text"
        latin_count = len(_LATIN_RE.findall(prose))
        ar_ratio = ar_count / (ar_count + latin_count)
        if ar_ratio < _MIN_AR_RATIO:
            return (
                False,
                f"AR campaign copy has predominantly non-Arabic text ({ar_ratio:.0%} Arabic)",
            )
        return True, ""

    if lang == "en":
        prose = _strip_non_prose_tokens(raw)
        cyr_words = _CYRILLIC_WORD_RE.findall(prose)
        cyr_count = sum(len(w) for w in cyr_words)
        latin_count = len(_LATIN_RE.findall(prose))
        if latin_count < _MIN_EN_LATIN_CHARS:
            if cyr_count > 0:
                return False, "EN campaign copy must not be Russian text"
            return False, "EN campaign copy lacks English prose"
        if cyr_words:
            if not allow_cyrillic_name:
                return False, "EN campaign template must not contain Cyrillic text"
            if len(cyr_words) >= 2:
                return False, "EN campaign copy contains multi-word Cyrillic text"
            single_word = cyr_words[0]
            if (
                not single_word[0].isupper()
                or single_word.casefold() in _EN_DISALLOWED_CYRILLIC_WORDS
                or not re.search(
                    rf"\b(?:Hey|Hi|Hello|Dear)[,\s]+{re.escape(single_word)}\b",
                    prose,
                )
            ):
                return False, "EN campaign copy contains unexpected Cyrillic word"
        return True, ""

    prose = _strip_neutral_tokens(raw)
    cyr_count = len(_CYRILLIC_RE.findall(prose))
    if cyr_count < _MIN_RU_CYRILLIC_CHARS:
        return False, "RU campaign copy must contain Cyrillic text"

    latin_count = len(_LATIN_RE.findall(prose))
    total_letters = cyr_count + latin_count
    cyr_ratio = cyr_count / total_letters
    if cyr_ratio < _MIN_RU_CYRILLIC_RATIO:
        return (
            False,
            f"RU campaign copy has predominantly non-Cyrillic text ({cyr_ratio:.0%} Cyrillic)",
        )
    return True, ""


def route_lead(
    row: Mapping[str, Any],
    *,
    provider: str | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> LeadRoute:
    """Compute language, segment, active provider, and target campaign in one typed call."""
    language = detect_lead_language(row)
    segment = detect_lead_segment(row)
    if (
        isinstance(cfg, Mapping)
        and str(cfg.get("active_market_profile") or "").strip().lower() == "gcc_december"
        and segment in {"whale", "b2b"}
        and is_gcc_mena_lead(row)
    ):
        segment = "gcc_mena"
    if provider is not None:
        resolved_provider = _normalize_provider(provider)
    elif _has_tg_contact(row):
        resolved_provider = "telegram"
    elif _has_wa_contact(row):
        resolved_provider = "whatsapp"
    else:
        resolved_provider = _normalize_provider(_clean_str(_row_get(row, "provider")))
    campaign = resolve_target_campaign(segment, language, resolved_provider, cfg=cfg)
    return LeadRoute(
        language=language,
        segment=segment,
        provider=resolved_provider,
        target_campaign=campaign,
    )


__all__ = [
    "CAMPAIGN_GCC_MENA_TG",
    "CAMPAIGN_GCC_MENA_WA",
    "CAMPAIGN_OFM_TG",
    "CAMPAIGN_RU_EXPERTS_TG",
    "CAMPAIGN_VACANCY_EN_TG",
    "CAMPAIGN_VACANCY_RU_TG",
    "CAMPAIGN_VACANCY_RU_WA",
    "CAMPAIGN_WHALES_EN_TG",
    "CAMPAIGN_WHALES_EN_WA",
    "EXPLICIT_SEGMENTS",
    "LeadLanguage",
    "LeadRoute",
    "QUEUE_ASSISTED_JOB",
    "QUEUE_MANUAL_SOCIAL",
    "assert_copy_matches_language",
    "detect_lead_language",
    "detect_lead_segment",
    "is_gcc_mena_lead",
    "resolve_target_campaign",
    "route_lead",
]
