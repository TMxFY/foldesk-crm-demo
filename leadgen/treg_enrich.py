#!/usr/bin/env python3
"""Attach treg data to leads before they are written to a sheet.

SoftUGC: work email, mailbox check, LinkedIn, plus a weekly US search-volume
and competitor-ad note.
ForexSbor: YouTube size and an X handle when the channel description already
names them, plus the same weekly demand note.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import inspect
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

try:
    from treg_client import call, walk_strings
except ImportError:
    try:
        from leadgen.treg_client import call, walk_strings
    except ImportError:
        from scripts.treg_client import call, walk_strings

ROUTE_CAP = {
    "X-Treg-Route-Max-Cost": os.getenv("TREG_ROUTE_MAX_COST", "0.02"),
    "X-Treg-Route-Waterfall": "0",
}
LEAD_CAP = max(0, int(os.getenv("TREG_LEAD_CAP", "24")))
CACHE_DAYS = 7
SKIP_HOSTS = {
    "t.me", "telegram.me", "google.com", "bit.ly", "tinyurl.com", "linktr.ee",
    "producthunt.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
    "youtu.be", "instagram.com", "facebook.com", "calendar.app.google",
    "notion.site", "notion.so", "github.com", "behance.net",
    "apps.apple.com", "itunes.apple.com", "play.google.com", "tiktok.com",
    "whop.com", "apps.whop.com", "fonts.googleapis.com", "fonts.gstatic.com",
    "gstatic.com", "cloudflare.com", "cdn.jsdelivr.net",
    "licdn.com", "googleusercontent.com", "sensortower.com", "kalodata.com",
    "fastmoss.com", "pipiads.com", "apkpure.com", "uptodown.com",
    "softonic.com", "appbrain.com", "crunchbase.com", "zoominfo.com",
    "trustpilot.com", "amazon.com", "shop.app",
}
CREATOR_KEYWORDS = ["AI UGC", "short form video ads", "prop firm challenge"]
FOREX_KEYWORDS = ["forex trading signals", "crypto trading channel"]
AD_COMPANY = {"creator": "Creatify", "forex": "FTMO"}


_TRACKING_SUBDOMAIN_LABELS = frozenset(
    {
        "lp",
        "t",
        "go",
        "get",
        "try",
        "shop",
        "store",
        "offer",
        "offers",
        "promo",
        "interview",
        "pages",
        "page",
        "click",
        "track",
        "link",
        "links",
        "m",
        "my",
        "web",
        "join",
        "start",
        "quiz",
        "blog",
        "info",
        "app",
    }
)
_MULTI_PART_PUBLIC_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "com.au",
        "net.au",
        "org.au",
        "com.br",
        "co.za",
        "co.nz",
        "com.sg",
        "co.il",
        "co.jp",
        "co.kr",
        "com.tr",
        "com.mx",
        "co.in",
        "com.ua",
        "com.ae",
    }
)


def root_company_domain(url_or_domain: str) -> str:
    """Strip tracking/landing subdomains (`lp.`, `t.`, `shop.`, `interview.`, etc.) preserving apex domain."""
    host = domain_of(url_or_domain)
    if not host:
        raw = str(url_or_domain or "").strip().lower().removeprefix("www.")
        host = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rstrip(".")
    if not host or "." not in host:
        return host
    parts = [p for p in host.split(".") if p]
    if len(parts) < 3:
        return host
    suffix_len = 2 if ".".join(parts[-2:]) in _MULTI_PART_PUBLIC_SUFFIXES else 1
    while len(parts) > suffix_len + 1 and parts[0] in _TRACKING_SUBDOMAIN_LABELS:
        parts = parts[1:]
    return ".".join(parts)


_GLUED_HTTP_SPLIT_RE = re.compile(r"(?i)(?<=.)(?=https?://)")
_GLUED_TRAILING_HTTP_RE = re.compile(r"(\.[a-z]{2,10})https?$", re.IGNORECASE)


def _iter_split_urls(text: str) -> Iterable[str]:
    """Yield HTTP(S) URLs from text after splitting glued consecutive URLs."""
    for chunk in _GLUED_HTTP_SPLIT_RE.split(str(text or "")):
        for match in re.finditer(r"https?://[^\s<>\]\)\"']+", chunk):
            yield match.group(0).rstrip(".,;:)")


def domain_of(url: str) -> str:
    raw_in = str(url or "").strip()
    if not raw_in:
        return ""
    raw_in = _GLUED_HTTP_SPLIT_RE.split(raw_in, maxsplit=1)[0].strip()
    if not raw_in:
        return ""
    if "://" in raw_in:
        parsed = urlparse(raw_in)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
    else:
        if ":" in raw_in.split("/", 1)[0]:
            return ""
        parsed = urlparse("https://" + raw_in)
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    host = _GLUED_TRAILING_HTTP_RE.sub(r"\1", host)
    if not host or "." not in host:
        return ""
    if any(host == skip or host.endswith("." + skip) for skip in SKIP_HOSTS):
        return ""
    return host


def infer_company_domain(company: str) -> str:
    text = (company or "").strip().lower().strip("/")
    if not text or " " in text:
        return ""
    if not re.fullmatch(r"(?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", text):
        return ""
    return domain_of(text)


def person_name(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) < 5 or " " not in text:
        return ""
    if re.search(r"https?://|t\.me/|@", text):
        return ""
    words = [w for w in re.split(r"\s+", text) if re.search(r"[A-Za-zА-Яа-я]", w)]
    if len(words) < 2:
        return ""
    return " ".join(words[:3])


def _first_email(payload: dict) -> str:
    for text in walk_strings(payload):
        match = re.search(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", text, re.I)
        if match and not match.group(0).lower().endswith((".png", ".jpg")):
            return match.group(0)
    return ""


def _first_linkedin(payload: dict) -> str:
    for text in walk_strings(payload):
        match = re.search(r"https?://(?:[\w.]+\.)?linkedin\.com/in/[A-Za-z0-9_\-%]+", text)
        if match:
            return match.group(0)
    return ""


def _first_project_url(payload: dict) -> str:
    for text in walk_strings(payload):
        for candidate in _iter_split_urls(text):
            dom = domain_of(candidate)
            if dom:
                return f"https://{dom}"
    return ""


_GENERIC_BRAND_STOPWORDS = frozenset({
    "app", "apps", "inc", "llc", "ltd", "corp", "co", "com", "org", "net", "io", "ai",
    "the", "and", "for", "with", "from", "of", "in", "on", "to", "studio", "studios",
    "labs", "media", "shop", "store", "official", "content", "campaigns", "campaign",
    "rewards", "organization", "terms", "service", "services", "policy", "privacy",
    "sponsored", "not", "mention", "mentioned", "fame", "group", "team", "platform",
    "online", "slideshow", "slideshows", "english",
    "get", "my", "try", "use", "hey", "join", "www", "web", "tiktok", "whop", "unknown",
})


def _search_hits_from_payload(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    containers: list[dict] = [payload]
    for sub in ("output", "result", "data"):
        child = payload.get(sub)
        if isinstance(child, list):
            return [r for r in child if isinstance(r, dict)]
        if isinstance(child, dict):
            containers.append(child)
    for container in containers:
        for key in ("results", "items", "organic_results"):
            rows = container.get(key)
            if isinstance(rows, list) and rows:
                dicts = [r for r in rows if isinstance(r, dict)]
                if dicts:
                    return dicts
    return []


def _brand_match_tokens(company: str, domain: str) -> list[str]:
    tokens: list[str] = []
    dom_low = (domain or "").strip().lower().removeprefix("www.")
    if dom_low and "." in dom_low:
        tokens.append(dom_low)
        root = dom_low.split(".", 1)[0]
        if len(root) >= 3 and root not in _GENERIC_BRAND_STOPWORDS and root not in tokens:
            tokens.append(root)
    for word in re.findall(r"[A-Za-z0-9]{3,}", company or ""):
        low = word.lower()
        if low not in _GENERIC_BRAND_STOPWORDS and low not in tokens:
            tokens.append(low)
    return tokens


_FORMER_ROLE_RE = re.compile(
    r"\b(?:former|ex[\s\-]+|previously)\s+"
    r"(?:co[\s\-]?founders?|founders?|ceo|chief\s+executive\s+officer|"
    r"cmo|chief\s+marketing\s+officer|cgo|chief\s+growth\s+officer|owners?|"
    r"(?:vp|vice\s+president)(?:\s+of|,)?\s+(?:marketing|growth|ua|user\s+acquisition)|"
    r"head\s+of\s+(?:growth|ua|user\s+acquisition|marketing|performance|brand|content)|"
    r"(?:marketing|growth|ua)\s+director|director\s+of\s+(?:marketing|growth|ua|user\s+acquisition))",
    re.I,
)
_ASSISTANT_TO_RE = re.compile(
    r"\b(?:(?:executive|personal|virtual|administrative)\s+assistant\s+to|"
    r"assistant\s+to|intern\s+to|chief\s+of\s+staff\s+to|"
    r"supporting|assisting|reporting\s+to|working\s+(?:with|for))"
    r"\s+(?:the\s+)?(?:ceo|founder|co[\s\-]?founder|owner|cmo|vp)\b",
    re.I,
)
_SLOT1_ROLE_RE = re.compile(
    r"\b(?:"
    r"co[\s\-]?founders?|"
    r"founders?|"
    r"ceo|"
    r"chief\s+executive\s+officer|"
    r"(?<!product\s)(?<!process\s)(?<!program\s)(?<!project\s)(?<!system\s)"
    r"(?<!platform\s)(?<!technical\s)(?<!data\s)(?<!account\s)owners?"
    r")\b",
    re.I,
)
_SLOT2_ROLE_RE = re.compile(
    r"\b(?:"
    r"cmo|chief\s+marketing\s+officer|"
    r"cgo|chief\s+growth\s+officer|"
    r"(?:vp|vice\s+president)(?:\s+of|,)?\s+(?:marketing|growth|ua|user\s+acquisition|performance\s+marketing)|"
    r"head\s+of\s+(?:growth|ua|user\s+acquisition|marketing|performance(?:\s+marketing)?|paid\s+acquisition|digital\s+marketing|brand|content)|"
    r"(?:marketing|growth|ua|user\s+acquisition|performance\s+marketing)\s+director|"
    r"director\s+of\s+(?:marketing|growth|ua|user\s+acquisition|performance\s+marketing)|"
    r"(?:growth|marketing|ua|user\s+acquisition)\s+lead"
    r")\b",
    re.I,
)
_EXEC_ROLE_RE = re.compile(
    r"\b(?:"
    r"coo|cro|chief\s+(?:operating|revenue|commercial)\s+officer|"
    r"president|managing\s+director|general\s+manager|"
    r"head\s+of\s+(?:partnerships|bd|business\s+development|sales|revenue)|"
    r"(?:vp|vice\s+president)(?:\s+of|,)?\s+(?:sales|revenue|partnerships|business\s+development)"
    r")\b",
    re.I,
)
_NON_DM_ROLE_RE = re.compile(
    r"\b(?:"
    r"interns?|trainees?|apprentices?|students?|"
    r"(?:software|backend|frontend|full[\s\-]?stack|qa|test|devops|data|ml|ai|cloud|mobile|ios|android)\s+(?:engineer|developer)s?|"
    r"programmers?|coders?|"
    r"recruiters?|talent\s+acquisition|hr\s+(?:manager|specialist|generalist)|"
    r"customer\s+(?:support|service|success)|support\s+(?:specialist|agent|engineer)|"
    r"(?:virtual|executive|personal|administrative)\s+assistant|"
    r"assistant\s+to\s+(?:the\s+)?(?:ceo|founder|co[\s\-]?founder|owner|cmo|vp)|"
    r"receptionist|office\s+manager|"
    r"accountants?|bookkeepers?|copywriters?|video\s+editors?|graphic\s+designers?"
    r")\b",
    re.I,
)
_ROLE_PHRASE_RE = re.compile(
    r"(?:"
    r"co[\s\-]?founder|founder|ceo|chief\s+executive\s+officer|owner|"
    r"cmo|chief\s+marketing\s+officer|cgo|chief\s+growth\s+officer|"
    r"(?:vp|vice\s+president)(?:\s+of|,)?\s+(?:marketing|growth|ua|user\s+acquisition|performance\s+marketing|sales|revenue)|"
    r"head\s+of\s+(?:growth|ua|user\s+acquisition|marketing|performance(?:\s+marketing)?|paid\s+acquisition|digital\s+marketing|brand|content|partnerships|bd|business\s+development|sales|revenue)|"
    r"(?:marketing|growth|ua|user\s+acquisition|performance\s+marketing)\s+director|"
    r"director\s+of\s+(?:marketing|growth|ua|user\s+acquisition|performance\s+marketing)|"
    r"(?:growth|marketing|ua|user\s+acquisition)\s+lead|"
    r"coo|cro|managing\s+director|general\s+manager|president"
    r")(?:\s*(?:&|and|/|\+)\s*(?:"
    r"co[\s\-]?founder|founder|ceo|owner|cmo|cgo|coo|cro|"
    r"(?:vp|vice\s+president)(?:\s+of|,)?\s+(?:marketing|growth|ua|user\s+acquisition)|"
    r"head\s+of\s+(?:growth|ua|user\s+acquisition|marketing|brand|content)"
    r"))*",
    re.I,
)
_NON_PERSON_NAME_WORDS = frozenset({
    "founder", "cofounder", "co-founder", "ceo", "cmo", "cto", "coo", "cfo", "cgo",
    "owner", "vp", "director", "marketing", "growth", "president", "officer",
    "linkedin", "telegram", "official", "careers", "career", "jobs", "job", "team",
    "inc", "llc", "ltd", "corp", "company", "studio", "studios", "labs", "advisor",
})
_TG_NOISE_HANDLES = frozenset({
    "joinchat", "share", "addstickers", "proxy", "iv", "s", "c", "boost", "socks", "login", "contact",
})
_SUPPORT_TG_HANDLE_RE = re.compile(
    r"(?:^|_)(?:support|help|admin|manager|sales|care|news|channel|chat|community|group)(?:$|_)",
    re.I,
)
_TME_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})\b",
    re.I,
)
_SLUG_GENERIC_TOKENS = frozenset({
    "profile", "in", "www", "http", "https", "com", "linkedin", "official", "hq",
    "ceo", "founder", "cofounder", "cmo", "cto", "coo", "cfo", "cgo", "owner",
})
_ROLE_EMAIL_PREFIXES = frozenset({
    "info", "support", "hello", "hi", "hey", "contact", "sales", "team", "admin",
    "office", "press", "help", "care", "marketing", "billing", "privacy", "legal",
    "aso", "research", "jobs", "careers", "noreply", "no-reply", "founders", "founder",
})


def _is_valid_personal_tg_handle(raw: Any) -> bool:
    u = str(raw or "").strip().lstrip("@")
    if not u or len(u) < 4 or len(u) > 32:
        return False
    low = u.lower()
    if low in _TG_NOISE_HANDLES or low.endswith("bot") or low.endswith("_bot"):
        return False
    if _SUPPORT_TG_HANDLE_RE.search(u):
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", u))


def _first_personal_tg_handle(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("telegram_username", "username", "tg_username"):
            val = str(payload.get(key) or "").strip().lstrip("@")
            if _is_valid_personal_tg_handle(val):
                return val
    for text in walk_strings(payload):
        for match in _TME_LINK_RE.finditer(text or ""):
            nick = match.group(1)
            tail_after = (text or "")[match.end() : match.end() + 2]
            if nick.lower() == "s" and tail_after.startswith("/"):
                continue
            if _is_valid_personal_tg_handle(nick):
                return nick
    return ""


def _person_name_from_linkedin_slug(url: str, *, clean_company: str, tokens: list[str]) -> str:
    norm = _normalize_linkedin_in_url(url)
    if not norm:
        return ""
    slug = unquote(norm.rsplit("/in/", 1)[-1]).strip().strip("/")
    parts = [p for p in re.split(r"[-_]+", slug) if p]
    while parts:
        last = parts[-1]
        if re.fullmatch(r"\d+", last):
            parts.pop()
        elif len(parts) > 2 and re.fullmatch(r"[0-9a-f]{5,}", last, re.I):
            parts.pop()
        elif re.fullmatch(r"(?=.*\d)[0-9a-f]{5,}", last, re.I):
            parts.pop()
        else:
            break
    alpha = [
        p for p in parts
        if p.lower() not in _SLUG_GENERIC_TOKENS
        and re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}", p)
    ]
    if not (2 <= len(alpha) <= 4):
        return ""
    token_set = {t.lower() for t in tokens if t}
    if clean_company:
        token_set.update(w.lower() for w in re.findall(r"[A-Za-z0-9]{2,}", clean_company))
    low_alpha = [w.lower() for w in alpha]
    if any(w in _NON_PERSON_NAME_WORDS or w in _GENERIC_BRAND_STOPWORDS or w in token_set for w in low_alpha):
        return ""
    joined = " ".join(w.capitalize() for w in alpha)
    if _SLOT2_ROLE_RE.search(joined) or _NON_DM_ROLE_RE.search(joined):
        return ""
    return joined


@dataclass
class _DecisionMakerCandidate:
    founder_name: str
    founder_linkedin: str
    work_email: str
    project_url: str
    role_title: str
    role_bucket: str
    matches_slot1: bool
    matches_slot2: bool
    is_exec: bool
    is_non_dm: bool
    slot1_score: int
    slot2_score: int
    exec_score: int
    order: int
    telegram_username: str = ""


def _normalize_linkedin_in_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    match = re.search(r"linkedin\.com/in/([A-Za-z0-9_\-%]+)", raw, re.I)
    if not match:
        return ""
    slug = match.group(1).strip("._-")
    if not slug:
        return ""
    return f"https://www.linkedin.com/in/{slug}"


def _linkedin_dedup_key(url: str) -> str:
    norm = _normalize_linkedin_in_url(url)
    if not norm:
        return ""
    slug = norm.rsplit("/in/", 1)[-1]
    return unquote(slug).strip().lower()


def _normalize_name_key(name: str) -> str:
    return " ".join((name or "").lower().split())


def _matching_project_url(payload: Any, tokens: list[str]) -> str:
    for text in walk_strings(payload):
        for candidate in _iter_split_urls(text):
            dom = domain_of(candidate)
            if dom and any(
                tok in dom or re.search(rf"\b{re.escape(tok)}\b", candidate.lower())
                for tok in tokens
            ):
                return f"https://{dom}"
    return ""


def _sanitize_role_text(text: str) -> str:
    cleaned = _ASSISTANT_TO_RE.sub(" ", text or "")
    cleaned = _FORMER_ROLE_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _slot1_score(text: str, *, in_headline: bool, brand_in_title: bool) -> int:
    cleaned = _sanitize_role_text(text)
    if not _SLOT1_ROLE_RE.search(cleaned):
        return 0
    low = cleaned.lower()
    base = 22
    if re.search(r"\b(?:ceo|chief\s+executive\s+officer)\b", low) and re.search(r"\bfounders?\b", low):
        base = 40
    elif re.search(r"\b(?:ceo|chief\s+executive\s+officer)\b", low):
        base = 36
    elif re.search(r"\bfounders?\b", low) and not re.search(r"\bco[\s\-]?founders?\b", low):
        base = 34
    elif re.search(r"\bco[\s\-]?founders?\b", low):
        base = 30
    return base + (20 if in_headline else 0) + (10 if brand_in_title else 0)


def _slot2_score(text: str, *, in_headline: bool, brand_in_title: bool) -> int:
    cleaned = _sanitize_role_text(text)
    if not _SLOT2_ROLE_RE.search(cleaned):
        return 0
    low = cleaned.lower()
    base = 18
    if re.search(r"\b(?:cmo|chief\s+marketing\s+officer|cgo|chief\s+growth\s+officer)\b", low):
        base = 40
    elif re.search(r"\b(?:vp|vice\s+president)(?:\s+of|,)?\s+(?:marketing|growth|ua|user\s+acquisition)", low):
        base = 35
    elif re.search(r"\b(?:head\s+of\s+(?:growth|ua|user\s+acquisition|marketing)|(?:marketing|growth|ua)\s+director|director\s+of\s+(?:marketing|growth|ua|user\s+acquisition))\b", low):
        base = 30
    elif re.search(r"\bhead\s+of\s+(?:performance|paid\s+acquisition|digital\s+marketing|brand|content)\b", low):
        base = 24
    return base + (20 if in_headline else 0) + (10 if brand_in_title else 0)


def _clean_person_name_candidate(raw: str, clean_company: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", str(raw or ""))
    text = re.sub(r",\s*(?:mba|phd|cpa|md|cfa)\b.*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -–—|·,:")
    if len(text) < 4 or " " not in text:
        return ""
    if re.search(r"https?://|t\.me/|@|\d|[|/]", text):
        return ""
    if clean_company and text.lower() == clean_company.lower():
        return ""
    words = [w for w in re.split(r"\s+", text) if re.search(r"[A-Za-zА-Яа-я]", w)]
    if not (2 <= len(words) <= 4):
        return ""
    lowered_words = {w.strip(".,:;").lower() for w in words}
    if lowered_words & _NON_PERSON_NAME_WORDS:
        return ""
    if _SLOT2_ROLE_RE.search(text) or _NON_DM_ROLE_RE.search(text):
        return ""
    return " ".join(words)


def _extract_role_title_from_headline(headline: str, tokens: list[str]) -> str:
    if not headline:
        return ""
    clauses = [c.strip() for c in re.split(r"\s*[|·;]\s*|\s+[-–—]\s+", headline) if c.strip()]
    for clause in clauses:
        sanitized = _sanitize_role_text(clause)
        if not (
            _SLOT1_ROLE_RE.search(sanitized)
            or _SLOT2_ROLE_RE.search(sanitized)
            or _EXEC_ROLE_RE.search(sanitized)
        ):
            continue
        trimmed = re.sub(r"\s+(?:at|@|with|for)\s+.+$", "", clause, flags=re.I).strip(" ,.-")
        if "," in trimmed and tokens:
            head_part, tail_part = trimmed.rsplit(",", 1)
            tail_low = tail_part.strip().lower()
            if any(re.search(rf"\b{re.escape(tok)}\b", tail_low) for tok in tokens):
                trimmed = head_part.strip(" ,.-")
        if trimmed and len(trimmed) <= 80:
            return trimmed
    match = _ROLE_PHRASE_RE.search(_sanitize_role_text(headline))
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else ""


def _parse_candidate_from_hit(
    hit: dict,
    *,
    clean_company: str,
    clean_domain: str,
    tokens: list[str],
    default_project_url: str,
    order: int,
) -> _DecisionMakerCandidate | None:
    linkedin = _normalize_linkedin_in_url(_first_linkedin(hit))
    email = _first_email(hit)
    tg_nick = _first_personal_tg_handle(hit)

    explicit_name = _clean_person_name_candidate(
        str(hit.get("founder_name") or hit.get("name") or ""),
        clean_company,
    )
    raw_title = str(hit.get("title") or "").strip()
    stripped_title = re.sub(
        r"\s*(?:[|\-–—]\s*(?:LinkedIn|Telegram)|on\s+(?:LinkedIn|Telegram))\s*$",
        "",
        raw_title,
        flags=re.I,
    ).strip()
    segments = [s.strip() for s in re.split(r"\s*[|·]\s*|\s+[-–—]\s+", stripped_title) if s.strip()]

    parsed_name = explicit_name
    headline = ""
    if not parsed_name and segments:
        candidate_name = _clean_person_name_candidate(segments[0], clean_company)
        rest_start = 1
        if not candidate_name:
            role_split = re.split(
                r"[:,]\s+(?=(?:co[\s\-]?founder|founder|ceo|cmo|cgo|cto|coo|cfo|owner|vp|head|director|president|chief)\b)",
                segments[0],
                maxsplit=1,
                flags=re.I,
            )
            if len(role_split) == 2:
                candidate_name = _clean_person_name_candidate(role_split[0], clean_company)
                if candidate_name:
                    headline = " - ".join([role_split[1].strip(), *segments[1:]]).strip(" -")
        if not candidate_name and len(segments) >= 2:
            seg0_low = segments[0].lower()
            if (clean_company and seg0_low == clean_company.lower()) or any(
                re.search(rf"\b{re.escape(tok)}\b", seg0_low) for tok in tokens
            ):
                candidate_name = _clean_person_name_candidate(segments[1], clean_company)
                if candidate_name:
                    rest_start = 2
        if candidate_name:
            parsed_name = candidate_name
            if not headline:
                headline = " - ".join(segments[rest_start:]).strip()
        else:
            headline = " - ".join(segments).strip()
    elif segments:
        if len(segments) > 1 and _clean_person_name_candidate(segments[0], clean_company):
            headline = " - ".join(segments[1:]).strip()
        else:
            headline = " - ".join(segments).strip()

    if not parsed_name and linkedin:
        parsed_name = _person_name_from_linkedin_slug(
            linkedin,
            clean_company=clean_company,
            tokens=tokens,
        )

    explicit_role = str(hit.get("role_title") or hit.get("role") or "").strip()
    if explicit_role:
        headline = explicit_role if not headline else f"{explicit_role} | {headline}"

    if not linkedin and not parsed_name and not email and not tg_nick:
        return None

    snippet_blob = " ".join(str(hit.get(k) or "") for k in ("snippet", "description")).strip()
    title_low = raw_title.lower()
    brand_in_title = bool(
        (clean_domain and clean_domain in title_low)
        or any(re.search(rf"\b{re.escape(tok)}\b", title_low) for tok in tokens)
    )

    h_clean = _sanitize_role_text(headline)
    s_clean = _sanitize_role_text(snippet_blob)
    h_raw_role = _FORMER_ROLE_RE.sub(" ", headline)
    s_raw_role = _FORMER_ROLE_RE.sub(" ", snippet_blob)

    h_s1 = _slot1_score(headline, in_headline=True, brand_in_title=brand_in_title)
    h_s2 = _slot2_score(headline, in_headline=True, brand_in_title=brand_in_title)
    h_exec = bool(_EXEC_ROLE_RE.search(h_clean))
    h_nondm = bool(_NON_DM_ROLE_RE.search(h_raw_role)) and not (h_s1 or h_s2 or h_exec)

    if h_nondm:
        return None

    s_s1 = _slot1_score(snippet_blob, in_headline=False, brand_in_title=brand_in_title) if not h_s1 else 0
    s_s2 = _slot2_score(snippet_blob, in_headline=False, brand_in_title=brand_in_title) if not h_s2 else 0
    s_exec = bool(_EXEC_ROLE_RE.search(s_clean))

    slot1_score = max(h_s1, s_s1)
    slot2_score = max(h_s2, s_s2)
    matches_slot1 = slot1_score > 0
    matches_slot2 = slot2_score > 0
    is_exec = (h_exec or s_exec) and not matches_slot1 and not matches_slot2

    s_nondm = bool(_NON_DM_ROLE_RE.search(s_raw_role)) and not (matches_slot1 or matches_slot2 or is_exec)
    if s_nondm:
        return None

    role_title = explicit_role or _extract_role_title_from_headline(headline, tokens)
    if not role_title and (matches_slot1 or matches_slot2 or is_exec):
        role_match = _ROLE_PHRASE_RE.search(s_clean)
        if role_match:
            role_title = re.sub(r"\s+", " ", role_match.group(0)).strip()

    if matches_slot1:
        role_bucket = "founder"
    elif matches_slot2:
        role_bucket = "marketing"
    elif is_exec:
        role_bucket = "executive"
    else:
        role_bucket = "other"

    proj_url = default_project_url or _matching_project_url(hit, tokens)
    return _DecisionMakerCandidate(
        founder_name=parsed_name,
        founder_linkedin=linkedin,
        work_email=email,
        project_url=proj_url,
        role_title=role_title,
        role_bucket=role_bucket,
        matches_slot1=matches_slot1,
        matches_slot2=matches_slot2,
        is_exec=is_exec,
        is_non_dm=False,
        slot1_score=slot1_score,
        slot2_score=slot2_score,
        exec_score=15 + (20 if h_exec else 0) + (10 if brand_in_title else 0) if is_exec else 0,
        order=order,
        telegram_username=tg_nick,
    )


def _merge_candidate(target: _DecisionMakerCandidate, incoming: _DecisionMakerCandidate) -> None:
    if not target.founder_name and incoming.founder_name:
        target.founder_name = incoming.founder_name
    if not target.founder_linkedin and incoming.founder_linkedin:
        target.founder_linkedin = incoming.founder_linkedin
    if not target.work_email and incoming.work_email:
        target.work_email = incoming.work_email
    if not target.project_url and incoming.project_url:
        target.project_url = incoming.project_url
    if not target.role_title and incoming.role_title:
        target.role_title = incoming.role_title
    if not target.telegram_username and incoming.telegram_username:
        target.telegram_username = incoming.telegram_username
    target.matches_slot1 = target.matches_slot1 or incoming.matches_slot1
    target.matches_slot2 = target.matches_slot2 or incoming.matches_slot2
    target.is_exec = target.is_exec or incoming.is_exec
    target.slot1_score = max(target.slot1_score, incoming.slot1_score)
    target.slot2_score = max(target.slot2_score, incoming.slot2_score)
    target.exec_score = max(target.exec_score, incoming.exec_score)
    if target.role_bucket == "other" and incoming.role_bucket != "other":
        target.role_bucket = incoming.role_bucket


def _deduplicate_candidates(candidates: list[_DecisionMakerCandidate]) -> list[_DecisionMakerCandidate]:
    unique: list[_DecisionMakerCandidate] = []
    by_li: dict[str, int] = {}
    by_name: dict[str, int] = {}
    by_tg: dict[str, int] = {}
    for cand in candidates:
        li_key = _linkedin_dedup_key(cand.founder_linkedin)
        name_key = _normalize_name_key(cand.founder_name)
        tg_key = cand.telegram_username.strip().lower()
        existing_idx = by_li.get(li_key) if li_key else None
        if existing_idx is None and name_key:
            existing_idx = by_name.get(name_key)
        if existing_idx is None and tg_key:
            existing_idx = by_tg.get(tg_key)
        if existing_idx is not None:
            _merge_candidate(unique[existing_idx], cand)
            if li_key and li_key not in by_li:
                by_li[li_key] = existing_idx
            if name_key and name_key not in by_name:
                by_name[name_key] = existing_idx
            if tg_key and tg_key not in by_tg:
                by_tg[tg_key] = existing_idx
            continue
        idx = len(unique)
        unique.append(cand)
        if li_key:
            by_li[li_key] = idx
        if name_key:
            by_name[name_key] = idx
        if tg_key:
            by_tg[tg_key] = idx
    return unique


def _candidate_to_dict(cand: _DecisionMakerCandidate, role_bucket_override: str | None = None) -> dict[str, str]:
    out = {
        "founder_name": cand.founder_name,
        "founder_linkedin": cand.founder_linkedin,
        "work_email": cand.work_email,
        "project_url": cand.project_url,
        "role_title": cand.role_title,
        "role_bucket": role_bucket_override or cand.role_bucket,
    }
    if cand.telegram_username:
        out["telegram_username"] = cand.telegram_username
        out["username"] = cand.telegram_username
    return out


def _select_prioritized_candidates(
    candidates: list[_DecisionMakerCandidate],
    limit: int,
) -> list[dict[str, str]]:
    if not candidates or limit <= 0:
        return []

    if limit == 1:
        best = max(
            candidates,
            key=lambda c: (
                1 if c.matches_slot1 else 0,
                c.slot1_score,
                1 if c.matches_slot2 else 0,
                c.slot2_score,
                1 if c.is_exec else 0,
                c.exec_score,
                1 if c.telegram_username else 0,
                1 if c.founder_linkedin else 0,
                1 if c.work_email else 0,
                -c.order,
            ),
        )
        bucket = "founder" if best.matches_slot1 else ("marketing" if best.matches_slot2 else best.role_bucket)
        return [_candidate_to_dict(best, bucket)]

    selected_entries: list[tuple[_DecisionMakerCandidate, str]] = []
    used_ids: set[int] = set()

    slot1_cands = [c for c in candidates if c.matches_slot1]
    slot2_cands = [c for c in candidates if c.matches_slot2]

    best_pair: tuple[tuple[int, int, int, int, int], _DecisionMakerCandidate, _DecisionMakerCandidate] | None = None
    for c1 in slot1_cands:
        for c2 in slot2_cands:
            if c1 is c2:
                continue
            pair_key = (
                c1.slot1_score + c2.slot2_score,
                min(c1.slot1_score, c2.slot2_score),
                (1 if c1.telegram_username else 0) + (1 if c2.telegram_username else 0),
                (1 if c1.founder_linkedin else 0) + (1 if c2.founder_linkedin else 0),
                -(c1.order + c2.order),
            )
            if best_pair is None or pair_key > best_pair[0]:
                best_pair = (pair_key, c1, c2)

    if best_pair is not None:
        _, c1, c2 = best_pair
        selected_entries.append((c1, "founder"))
        selected_entries.append((c2, "marketing"))
        used_ids.add(id(c1))
        used_ids.add(id(c2))
    else:
        if slot1_cands:
            c1 = max(
                slot1_cands,
                key=lambda c: (c.slot1_score, 1 if c.telegram_username else 0, 1 if c.founder_linkedin else 0, -c.order),
            )
            selected_entries.append((c1, "founder"))
            used_ids.add(id(c1))
        if slot2_cands and len(selected_entries) < limit:
            rem_s2 = [c for c in slot2_cands if id(c) not in used_ids]
            if rem_s2:
                c2 = max(
                    rem_s2,
                    key=lambda c: (c.slot2_score, 1 if c.telegram_username else 0, 1 if c.founder_linkedin else 0, -c.order),
                )
                selected_entries.append((c2, "marketing"))
                used_ids.add(id(c2))

    if len(selected_entries) < limit:
        remaining_dms = [
            c
            for c in candidates
            if id(c) not in used_ids and (c.matches_slot1 or c.matches_slot2 or c.is_exec)
        ]
        remaining_dms.sort(
            key=lambda c: (
                1 if c.matches_slot1 else 0,
                1 if c.matches_slot2 else 0,
                max(c.slot1_score, c.slot2_score, c.exec_score),
                1 if c.telegram_username else 0,
                1 if c.founder_linkedin else 0,
                -c.order,
            ),
            reverse=True,
        )
        for cand in remaining_dms:
            if len(selected_entries) >= limit:
                break
            bucket = "founder" if cand.matches_slot1 else ("marketing" if cand.matches_slot2 else cand.role_bucket)
            selected_entries.append((cand, bucket))
            used_ids.add(id(cand))

    if not selected_entries:
        fallback = max(
            candidates,
            key=lambda c: (1 if c.telegram_username else 0, 1 if c.founder_linkedin else 0, 1 if c.work_email else 0, -c.order),
        )
        selected_entries.append((fallback, fallback.role_bucket))

    return [_candidate_to_dict(cand, bucket) for cand, bucket in selected_entries[:limit]]


def _invoke_search_caller(caller: Callable[..., Any], q: str) -> Any:
    try:
        sig = inspect.signature(caller)
        params = list(sig.parameters.values())
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        if not accepts_var_kw and len(params) == 1 and params[0].name not in {"endpoint", "ep"}:
            return caller(q)
    except (TypeError, ValueError):
        pass
    return caller("crawl4ai.web.search", query={"q": q})


def _filter_brand_hits(hits: list[dict], clean_domain: str, tokens: list[str]) -> list[dict]:
    matched_hits: list[dict] = []
    for hit in hits:
        blob = " ".join(
            str(hit.get(k) or "")
            for k in (
                "title",
                "snippet",
                "description",
                "url",
                "link",
                "founder_linkedin",
                "founder_name",
                "role_title",
                "work_email",
                "telegram_username",
                "username",
            )
        ).lower()
        if not blob:
            continue
        if clean_domain and clean_domain in blob:
            matched_hits.append(hit)
        elif any(re.search(rf"\b{re.escape(tok)}\b", blob) for tok in tokens):
            matched_hits.append(hit)
    return matched_hits


def find_company_decision_makers(
    company: str,
    domain: str = "",
    *,
    limit: int = 2,
    treg_fn: Callable[..., Any] | None = None,
) -> list[dict[str, str]]:
    """Find up to `limit` distinct company decision makers prioritized by Founder/CEO then CMO/Growth."""
    if limit <= 0:
        return []
    clean_company = (company or "").strip()
    raw_domain = (domain or "").strip()
    if "://" in raw_domain or "/" in raw_domain:
        clean_domain = domain_of(raw_domain) or raw_domain.lower().removeprefix("www.").split("/", 1)[0]
    else:
        clean_domain = raw_domain.lower().removeprefix("www.")
    if clean_domain:
        clean_domain = root_company_domain(clean_domain) or clean_domain
    target = clean_company or clean_domain
    if not target:
        return []
    tokens = _brand_match_tokens(clean_company, clean_domain)
    if not tokens:
        return []

    caller = call if treg_fn is None else treg_fn
    target_quote = clean_domain or clean_company
    primary_query = (
        f'site:linkedin.com/in "{target_quote}" '
        '(Founder OR "Co-Founder" OR CEO OR Owner OR CMO OR "VP Marketing" OR "Head of Growth" OR "Head of UA" OR "Marketing Director")'
    )
    payload = _invoke_search_caller(caller, primary_query)
    matched_hits = _filter_brand_hits(_search_hits_from_payload(payload), clean_domain, tokens)
    if not matched_hits:
        return []

    valid_domain = domain_of(clean_domain) if clean_domain else ""
    default_project_url = f"https://{valid_domain}" if valid_domain else ""

    def _collect_candidates(hits_batch: list[dict], start_order: int = 0) -> list[_DecisionMakerCandidate]:
        out: list[_DecisionMakerCandidate] = []
        for idx, hit in enumerate(hits_batch):
            cand = _parse_candidate_from_hit(
                hit,
                clean_company=clean_company,
                clean_domain=clean_domain,
                tokens=tokens,
                default_project_url=default_project_url,
                order=start_order + idx,
            )
            if cand is not None:
                out.append(cand)
        return out

    candidates = _deduplicate_candidates(_collect_candidates(matched_hits, 0))

    if limit > 1 and candidates:
        has_slot1 = any(c.matches_slot1 for c in candidates)
        has_slot2 = any(c.matches_slot2 for c in candidates)
        followup_query = ""
        if has_slot1 and not has_slot2:
            followup_query = (
                f'site:linkedin.com/in "{target_quote}" '
                '(CMO OR "VP Marketing" OR "Head of Growth" OR "Head of UA" OR "Marketing Director")'
            )
        elif has_slot2 and not has_slot1:
            followup_query = (
                f'site:linkedin.com/in "{target_quote}" '
                '(Founder OR "Co-Founder" OR CEO OR Owner)'
            )
        if followup_query:
            try:
                extra_payload = _invoke_search_caller(caller, followup_query)
            except StopIteration:
                extra_payload = None
            extra_hits = _filter_brand_hits(_search_hits_from_payload(extra_payload), clean_domain, tokens)
            if extra_hits:
                candidates = _deduplicate_candidates(
                    candidates + _collect_candidates(extra_hits, len(matched_hits))
                )

    if not default_project_url:
        shared_project_url = next((c.project_url for c in candidates if c.project_url), "")
        if shared_project_url:
            for cand in candidates:
                if not cand.project_url:
                    cand.project_url = shared_project_url

    return _select_prioritized_candidates(candidates, limit)


def find_company_decision_maker(company: str, domain: str = "") -> dict[str, str]:
    """Return the primary company decision maker dict (backward-compatible wrapper)."""
    matches = find_company_decision_makers(company, domain, limit=1)
    if not matches:
        return {}
    return {k: v for k, v in matches[0].items() if v}


def _status(payload: dict) -> str:
    output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
    for key in ("status", "result", "state"):
        value = output.get(key) if isinstance(output, dict) else None
        if isinstance(value, str) and value:
            return value
    if isinstance(output, dict) and output.get("valid") is True:
        return "valid"
    if isinstance(output, dict) and output.get("verified") is True:
        return "valid"
    return ""


def find_email(name: str, domain: str) -> dict:
    payload = call(
        "treg.people.email.find",
        method="POST",
        data={"full_name": name, "domain": domain},
        headers=ROUTE_CAP,
    )
    if not payload:
        return {}
    found = {"work_email": _first_email(payload)}
    linkedin = _first_linkedin(payload)
    if linkedin:
        found["founder_linkedin"] = linkedin
    tg_nick = _first_personal_tg_handle(payload)
    if tg_nick:
        found["telegram_username"] = tg_nick
        found["username"] = tg_nick
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    if isinstance(output, dict):
        fn_raw = str(output.get("full_name") or output.get("founder_name") or "").strip()
        clean_fn = _clean_person_name_candidate(fn_raw, "")
        if clean_fn:
            found["founder_name"] = clean_fn
    if isinstance(output, dict) and output.get("verified") is True and found.get("work_email"):
        found["email_status"] = "valid"
    return {k: v for k, v in found.items() if v}


def verify_email(email: str) -> str:
    payload = call(
        "treg.people.email.verify",
        method="POST",
        data={"email": email},
        headers=ROUTE_CAP,
    )
    if not payload:
        return ""
    return _status(payload)


def youtube_stats(url: str) -> dict:
    payload = call(
        "scrapecreators.youtube.channel.profile",
        query={"url": url},
    )
    if not payload:
        return {}
    output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
    subs = ""
    if isinstance(output, dict):
        subs = str(output.get("subscribers") or output.get("subscriberCount") or "")
    if not subs:
        for text in walk_strings(payload):
            match = re.search(r"(\d[\d,]{2,})\s+subscribers", text, re.I)
            if match:
                subs = match.group(1).replace(",", "")
                break
    return {"yt_subs": subs} if subs else {}


def keyword_volumes(keywords: list[str]) -> str:
    payload = call(
        "serpstat.google.keywords.volume",
        method="POST",
        data={
            "method": "SerpstatKeywordProcedure.getKeywordsInfo",
            "id": "1",
            "params": {"keywords": keywords, "se": "g_us"},
        },
    )
    if not payload:
        return ""
    bits = []
    blob = json.dumps(payload, ensure_ascii=False)
    for keyword in keywords:
        window = blob.lower()
        # Serpstat nests region_queries_count next to the keyword. A nearby
        # integer is enough for a one-line sheet note.
        idx = window.find(keyword.lower())
        if idx < 0:
            continue
        chunk = blob[idx : idx + 400]
        match = re.search(r"region_queries_count\"?\s*[:=]\s*(\d+)", chunk)
        if match:
            bits.append(f"{keyword} {int(match.group(1)):,}/mo")
    return "US search: " + "; ".join(bits) if bits else ""


def competitor_ads(company: str) -> str:
    payload = call(
        "scrapecreators.x.v1-facebook-adlibrary-company-ads",
        query={"companyName": company, "trim": "true", "status": "ACTIVE"},
    )
    if not payload:
        return ""
    texts = []
    for text in walk_strings(payload):
        clean = " ".join(text.split())
        if 40 <= len(clean) <= 180 and clean not in texts:
            texts.append(clean)
        if len(texts) == 2:
            break
    if not texts:
        return ""
    return f"{company} ads: " + " · ".join(texts)


def market_note(kind: str, cache_path: Path) -> dict:
    try:
        cached = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cached = {}
    fresh = cached.get(kind) if isinstance(cached.get(kind), dict) else {}
    if fresh.get("at", 0) > time.time() - CACHE_DAYS * 86400 and fresh.get("text"):
        return fresh
    keywords = CREATOR_KEYWORDS if kind == "creator" else FOREX_KEYWORDS
    demand = keyword_volumes(keywords)
    ads = competitor_ads(AD_COMPANY[kind])
    text = " | ".join(p for p in (demand, ads) if p)
    fresh = {"at": time.time(), "text": text, "demand": demand, "ads": ads} if text else fresh
    if text:
        cached[kind] = fresh
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cached, ensure_ascii=False))
        except OSError:
            pass
    return fresh if isinstance(fresh, dict) else {}


def _is_role_email(email: str) -> bool:
    raw = str(email or "").strip()
    if "@" not in raw:
        return False
    local = raw.split("@", 1)[0].strip().lower()
    return local in _ROLE_EMAIL_PREFIXES


def _merge_dm_into_row(row: dict[str, Any], found: dict[str, str]) -> None:
    for k, v in found.items():
        if not v:
            continue
        if k in ("username", "telegram_username", "tg_username"):
            if _is_valid_personal_tg_handle(v) and not _is_valid_personal_tg_handle(row.get(k)):
                row[k] = v
        elif not row.get(k):
            row[k] = v


def _preserve_tg_and_founder_fields(
    row: dict[str, Any],
    *,
    company: str,
    domain: str,
    person_nm: str,
) -> None:
    tg = ""
    for k in ("username", "telegram_username", "tg_username"):
        cand = str(row.get(k) or "").strip().lstrip("@")
        if _is_valid_personal_tg_handle(cand):
            tg = cand
            break
    if not tg:
        tg = _first_personal_tg_handle(
            {
                "links": row.get("links") or [],
                "bio": row.get("bio") or "",
                "what_they_do": row.get("what_they_do") or "",
                "about": row.get("about") or "",
                "sample": row.get("sample") or "",
            }
        )
    if tg:
        tg_url = f"https://t.me/{tg}"
        if not _is_valid_personal_tg_handle(row.get("username")):
            row["username"] = tg
        if not _is_valid_personal_tg_handle(row.get("telegram_username")):
            row["telegram_username"] = tg
        if not str(row.get("telegram_url") or "").strip():
            row["telegram_url"] = tg_url
        if not str(row.get("provider") or "").strip():
            row["provider"] = "telegram"
        if isinstance(row.get("links"), list) and tg_url not in row["links"]:
            row["links"].append(tg_url)

    if not str(row.get("founder_name") or "").strip():
        inferred_fn = person_nm
        if not inferred_fn and row.get("founder_linkedin"):
            inferred_fn = _person_name_from_linkedin_slug(
                str(row.get("founder_linkedin") or ""),
                clean_company=company,
                tokens=_brand_match_tokens(company, domain),
            )
        if inferred_fn:
            row["founder_name"] = inferred_fn


def enrich_creator_rows(rows: list[dict], cache_path: Path) -> list[dict]:
    try:
        from tg_to_gsheet import harvest_direct
    except ImportError:
        try:
            from leadgen.tg_to_gsheet import harvest_direct
        except ImportError:
            from scripts.tg_to_gsheet import harvest_direct
    note = market_note("creator", cache_path)
    used = 0
    out = []
    for row in rows:
        row = harvest_direct(dict(row))
        if note.get("demand"):
            row["treg_demand"] = note["demand"]
        if note.get("ads"):
            row["treg_ads"] = note["ads"]
        if not (row.get("project_url") or "").strip():
            inferred = infer_company_domain(row.get("name") or row.get("project") or "")
            if inferred:
                row["project_url"] = f"https://{inferred}"
        domain = domain_of(row.get("project_url") or "")
        company = str(row.get("company") or row.get("project") or "").strip()
        raw_name = str(row.get("name") or "").strip()
        person_nm = _clean_person_name_candidate(str(row.get("founder_name") or ""), company)
        if not person_nm and raw_name:
            if not company or raw_name.casefold() != company.casefold():
                person_nm = _clean_person_name_candidate(raw_name, company)

        is_company_row = (row.get("source") == "linkedin_jobs") or (not person_nm and bool(company or domain))
        if is_company_row:
            has_usable_email = bool(row.get("work_email")) and not _is_role_email(str(row.get("work_email") or ""))
            has_tg = _is_valid_personal_tg_handle(row.get("username") or row.get("telegram_username"))
            if used < LEAD_CAP and not row.get("founder_linkedin") and not has_usable_email and not has_tg:
                dm_company = (row.get("project") or company or raw_name).strip() if row.get("source") == "linkedin_jobs" else (company or raw_name).strip()
                if dm_company or domain:
                    found = find_company_decision_maker(dm_company, domain)
                    _merge_dm_into_row(row, found)
                    used += 1
        else:
            if used < LEAD_CAP and person_nm and domain and not row.get("work_email"):
                found = find_email(person_nm, domain)
                _merge_dm_into_row(row, found)
                if row.get("work_email") and not row.get("email_status"):
                    status = verify_email(row["work_email"])
                    if status:
                        row["email_status"] = status
                used += 1

        _preserve_tg_and_founder_fields(row, company=company or raw_name, domain=domain, person_nm=person_nm)
        out.append(row)
    return out


def enrich_forex_result(result: dict, cache_path: Path) -> dict:
    result = dict(result)
    about = result.get("about") or ""
    yt = re.search(r"https?://(?:www\.)?youtube\.com/(?:@[\w.\-]+|channel/[\w.\-]+|c/[\w.\-]+)", about)
    if yt and not result.get("yt_subs"):
        result["youtube"] = result.get("youtube") or yt.group(0)
        result.update(youtube_stats(result["youtube"]))
    x = re.search(r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{2,30})", about)
    if x and not result.get("x_handle"):
        result["x_handle"] = "@" + x.group(1)
    note = market_note("forex", cache_path).get("text") or ""
    if note and note not in about:
        result["about"] = (about + " | " + note).strip(" |")
    if result.get("x_handle") and result["x_handle"] not in (result.get("about") or ""):
        result["about"] = (result.get("about") or "") + f" | X: {result['x_handle']}"
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache", default="")
    args = parser.parse_args()
    rows = [json.loads(line) for line in open(args.input) if line.strip()]
    cache = Path(args.cache or os.path.join(os.path.dirname(args.input), "treg_market.json"))
    enriched = enrich_creator_rows(rows, cache)
    with open(args.output, "w") as handle:
        for row in enriched:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    filled = sum(1 for row in enriched if row.get("work_email"))
    print(f"treg: {filled}/{len(enriched)} leads got a work email", flush=True)


if __name__ == "__main__":
    main()
