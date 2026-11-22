#!/usr/bin/env python3
"""Bounded contact waterfall for SoftUGC leads (Tier 1 free + Layer 0.5 monid + Tier 2 treg).

Tier 1 (free):
  - Bounded crawl of lead project domain (root + up to 2 discovered
    contact/about/team links or /contact, /about, /terms probes, max 4 internal
    pages) plus up to 2 bio-hub pages (linktr.ee, linktree.com, beacons.ai,
    carrd.co, solo.to).
  - Ranks and upgrades emails via rank_and_pick_best_email.
  - Fills missing X, LinkedIn /in/, TG @username, WhatsApp, Instagram.
  - Falls back to up to 2 decision makers (find_company_decision_makers) when
    founder LinkedIn is missing, storing the 2nd DM in secondary_contact_json.
  - Runs tg_identity_bridge.bridge_lead_to_telegram to resolve personal TG.

Layer 0.5 (monid FB/IG business contact):
  - Runs when TG and WhatsApp are still missing and FB page or IG handle is present:
    anyapi /facebook.page_contact (route="giraffe") and
    anyapi /instagram.profile_contact (route="octopus" -> fallback "chameleon").

Tier 2 (paid treg email + phone waterfall, $0.04/lead default or $0.15 super-whale):
  - Runs for icp_score >= 80 leads whenever BOTH TG and WhatsApp are still missing
    (X and email NEVER block finding a mobile phone for WA/TG).
  - Discovers missing DM LinkedIn/email via leadmagic/crawl4ai/blitzapi and re-probes
    Telegram identity bridge once.
  - Phone waterfall per DM (up to 2 DMs):
      a) contactout.people.phone.available ($0 pre-check; if explicitly False,
         still tries crawl4ai $0.00095 and caps treg.people.phone.find at <= $0.01
         unless super-whale)
      b) crawl4ai.people.phone.find ($0.00095 explicit endpoint)
      c) treg.people.phone.find (POST, X-Treg-Route-Max-Cost: <remaining_cap>)
  - Free HLR line-type filter (hlrlookup.people.phone.validate, $0):
    drops LANDLINE / TOLL_FREE; keeps MOBILE / FIXED_LINE_OR_MOBILE / unknown.
"""
from __future__ import annotations

import argparse
import html as html_mod
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from leadgen import treg_enrich as _treg_enrich_mod
    from leadgen.monid_client import check_monid_ready, run_monid
    from leadgen.treg_client import call as treg_call, check_treg_ready, walk_strings
    from leadgen.treg_enrich import domain_of, find_company_decision_maker
    from leadgen.tg_identity_bridge import bridge_lead_to_telegram
    from leadgen.tg_to_gsheet import linkedin_in_url, whatsapp_url, x_profile_url
except ImportError:
    import treg_enrich as _treg_enrich_mod
    from monid_client import check_monid_ready, run_monid
    from treg_client import call as treg_call, check_treg_ready, walk_strings
    from treg_enrich import domain_of, find_company_decision_maker
    from tg_identity_bridge import bridge_lead_to_telegram
    from tg_to_gsheet import linkedin_in_url, whatsapp_url, x_profile_url


_ORIG_RUN_MONID = run_monid
_ORIG_FIND_DM = find_company_decision_maker
logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; SoftUGC/1.0)"
HTTP_HEADERS = {"User-Agent": USER_AGENT}
HTTP_TIMEOUT_S = 6
MAX_INTERNAL_PAGES = 4
MAX_DISCOVERED_INTERNAL = 2
MAX_BIO_HUB_PAGES = 2
PER_LEAD_CAP_USD = 0.04
SUPER_WHALE_CAP_USD = 0.15
SUPER_WHALE_ICP_MIN = 92
SUPER_WHALE_POOL_MIN_USD = 10000
SECONDARY_DM_FALLBACK_DAYS = 6

COST_CRAWL4AI_EMAIL_USD = 0.00095
COST_CRAWL4AI_PHONE_USD = 0.00095
COST_CRAWL4AI_SEARCH_USD = 0.00015
COST_BLITZAPI_EMAIL_USD = 0.001
COST_LEADMAGIC_EMP_USD = 0.00375
COST_TREG_PHONE_MIN_USD = 0.001
COST_MONID_FB_CONTACT_USD = 0.0012
COST_MONID_IG_OCTOPUS_USD = 0.0
COST_MONID_IG_CHAMELEON_USD = 0.00175
CONTACTOUT_NEG_TREG_CAP_USD = 0.01

_HLR_DROP_TYPES = frozenset(
    {
        "LANDLINE",
        "FIXED_LINE",
        "FIXED",
        "TOLL_FREE",
        "VOIP_LANDLINE",
        "PREMIUM_RATE",
        "SHARED_COST",
        "PAGER",
        "BAD_FORMAT",
        "INVALID",
        "INVALID_NUMBER",
        "UNALLOCATED",
        "DEAD",
        "DISCONNECTED",
    }
)
_HLR_KEEP_TYPES = frozenset({"MOBILE", "FIXED_LINE_OR_MOBILE"})

BIO_HUB_HOSTS = (
    "linktr.ee",
    "linktree.com",
    "beacons.ai",
    "carrd.co",
    "solo.to",
)

_IMAGE_OR_ASSET_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".svg",
    ".gif",
    ".ico",
    ".bmp",
    ".avif",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
    ".zip",
)

_AUTOMATED_LOCALS = frozenset(
    {
        "noreply",
        "no-reply",
        "no_reply",
        "donotreply",
        "do-not-reply",
        "do_not_reply",
        "bounce",
        "bounces",
        "mailer-daemon",
        "mailerdaemon",
        "postmaster",
        "daemon",
        "auto",
        "automated",
        "autoreply",
        "auto-reply",
        "auto_reply",
        "notification",
        "notifications",
        "unsubscribe",
        "abuse",
        "webmaster",
        "hostmaster",
    }
)

_DISPOSABLE_HOSTS = frozenset(
    {
        "sentry.io",
        "wixpress.com",
        "example.com",
        "example.org",
        "example.net",
        "test.com",
        "localhost",
        "invalid",
        "domain.com",
        "yourdomain.com",
        "company.com",
        "website.com",
        "email.com",
        "mailinator.com",
        "guerrillamail.com",
        "tempmail.com",
        "temp-mail.org",
        "10minutemail.com",
        "trashmail.com",
        "yopmail.com",
        "sharklasers.com",
        "dispostable.com",
        "getnada.com",
        "throwawaymail.com",
        "mailsac.com",
        "noreply.github.com",
    }
)

_EXEC_LOCALS = frozenset(
    {
        "ceo",
        "founder",
        "cofounder",
        "co-founder",
        "owner",
        "cmo",
        "growth",
        "marketing",
        "partnerships",
        "collab",
        "bd",
    }
)

_GENERIC_BUCKET_LOCALS = frozenset(
    {
        "support",
        "help",
        "info",
        "contact",
        "hello",
        "hi",
        "team",
        "sales",
        "admin",
        "privacy",
        "legal",
        "press",
        "billing",
        "office",
        "hr",
        "careers",
        "jobs",
        "security",
        "dmca",
    }
)

_WARM_GENERIC_LOCALS = frozenset({"hello", "hi", "team", "contact", "info"})
_FALLBACK_GENERIC_LOCALS = frozenset({"support", "help", "privacy", "legal"})

_EMAIL_FULL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_EMAIL_FIND_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MAILTO_RE = re.compile(r"""mailto:([^"'\s<>?#]+)""", re.I)
_OBFUSCATED_AT_RE = re.compile(r"\s*(?:\[at\]|\(at\)|\{at\})\s*", re.I)
_OBFUSCATED_DOT_RE = re.compile(r"\s*(?:\[dot\]|\(dot\)|\{dot\})\s*", re.I)

_TG_RESERVED = frozenset(
    {
        "joinchat",
        "share",
        "s",
        "c",
        "iv",
        "proxy",
        "contentrewardsbot",
        "addstickers",
        "socks",
        "login",
        "contact",
        "boost",
        "addlist",
    }
)
_TME_RE = re.compile(
    r"""(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/@?([A-Za-z][A-Za-z0-9_]{3,31})(?=[/?#"'<>\s,;:\)]|$)""",
    re.I,
)
_TG_EXPLICIT_RE = re.compile(
    r"""(?:\btg\s*:\s*@|\btelegram\s*:\s*@|tg://resolve\?domain=)([A-Za-z][A-Za-z0-9_]{3,31})(?=[/?#"'<>\s,;:\)]|$)""",
    re.I,
)
_TG_LOW_PRIORITY_HINTS = ("community", "channel", "announcements", "news", "chat", "official", "group")

_X_RESERVED = frozenset(
    {
        "home",
        "search",
        "i",
        "share",
        "intent",
        "explore",
        "hashtag",
        "compose",
        "login",
        "signup",
        "settings",
        "privacy",
        "tos",
    }
)
_X_RE = re.compile(
    r"""https?://(?:www\.)?(?:x|twitter|mobile\.twitter)\.com/@?([A-Za-z0-9_]{1,15})(?=[/?#"'<>\s,;:\)]|$)""",
    re.I,
)

_IG_RESERVED = frozenset(
    {
        "p",
        "reel",
        "reels",
        "stories",
        "explore",
        "accounts",
        "direct",
        "tv",
        "ar",
        "about",
        "legal",
        "privacy",
        "developer",
        "oauth",
    }
)
_IG_RE = re.compile(
    r"""(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})(?=[/?#"'<>\s,;:\)]|$)""",
    re.I,
)

_HREF_RE = re.compile(r"""<a\b[^>]*\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>(.*?)</a>""", re.I | re.S)
_URL_IN_TEXT_RE = re.compile(r"""https?://[^\s"'<>\]\)]+""", re.I)

_INTERNAL_LINK_KEYWORDS = (
    "contact",
    "about",
    "team",
    "founder",
    "company",
    "impressum",
    "support",
    "terms",
    "privacy",
    "legal",
)
_PROBE_PATHS = ("/contact", "/about", "/terms")

_DM_TIER2_RE = re.compile(
    r"\b(?:co-?\s*founder|founder|ceo|chief executive|owner|cmo|chief marketing|growth|marketing|head)\b",
    re.I,
)
_DM_TIER1_RE = re.compile(r"\b(?:co-?\s*founder|founder|ceo|chief executive|owner)\b", re.I)
_GENERIC_PERSON_NAMES = frozenset(
    {
        "unknown",
        "founder",
        "co-founder",
        "cofounder",
        "ceo",
        "owner",
        "team",
        "admin",
        "marketing",
        "support",
        "info",
        "contact",
        "official",
        "brand",
        "company",
        "none",
        "n/a",
    }
)


def _clean_email_candidate(raw: Any) -> str:
    text = unquote(str(raw or "")).strip()
    if not text:
        return ""
    if text.lower().startswith("mailto:"):
        text = text[7:]
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = text.strip("<>\"'()[]{}.,;: ")
    return text.lower()


def _is_disposable_or_bogus_email(email: str) -> bool:
    if not email or "@" not in email or ".." in email:
        return True
    if not _EMAIL_FULL_RE.fullmatch(email):
        return True
    local, _, host = email.partition("@")
    if not local or not host:
        return True
    if local.startswith(".") or local.endswith(".") or host.startswith(".") or host.endswith("."):
        return True
    if email.endswith(_IMAGE_OR_ASSET_EXTS) or host.endswith(_IMAGE_OR_ASSET_EXTS):
        return True
    if "sentry" in host or "wixpress" in host:
        return True
    for bad_host in _DISPOSABLE_HOSTS:
        if host == bad_host or host.endswith("." + bad_host):
            return True

    local_base = local.split("+", 1)[0]
    local_norm = re.sub(r"[-_.]+", "", local_base)
    if local_base in _AUTOMATED_LOCALS or local_norm in _AUTOMATED_LOCALS:
        return True
    if local_base.startswith(("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "mailer-daemon", "bounce")):
        return True
    if re.fullmatch(r"[0-9a-f]{16,}", local_norm):
        return True
    return False


def _domain_matches(host: str, target_domain: str) -> bool:
    if not host or not target_domain:
        return False
    return host == target_domain or host.endswith("." + target_domain)


def _normalize_target_domain(domain: str) -> str:
    raw = str(domain or "").strip().lower().removeprefix("www.")
    if not raw:
        return ""
    if "://" in raw or "/" in raw:
        dom = domain_of(raw)
        if dom:
            return dom
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        return (parsed.hostname or "").lower().removeprefix("www.")
    return raw


def is_personal_or_exec_email(email: str, domain: str = "") -> bool:
    """Return True for valid non-disposable emails with a personal or exec local-part."""
    clean = _clean_email_candidate(email)
    if _is_disposable_or_bogus_email(clean):
        return False

    local, _, _host = clean.partition("@")
    local_base = local.split("+", 1)[0]
    local_norm = re.sub(r"[-_.]+", "", local_base)

    if local_base in _EXEC_LOCALS or local_norm in _EXEC_LOCALS:
        return True
    if local_base in _GENERIC_BUCKET_LOCALS or local_norm in _GENERIC_BUCKET_LOCALS:
        return False

    alpha_tokens = re.findall(r"[a-z]+", local_base)
    if not alpha_tokens:
        return False
    if alpha_tokens[0] in _GENERIC_BUCKET_LOCALS:
        return False
    if all(tok in (_GENERIC_BUCKET_LOCALS | {"us", "eu", "uk", "hq", "mail"}) for tok in alpha_tokens):
        return False
    return any(len(tok) >= 3 and tok not in _GENERIC_BUCKET_LOCALS for tok in alpha_tokens)


def _score_email_tuple(email: str, target_domain: str, idx: int) -> tuple[int, int, int] | None:
    clean = _clean_email_candidate(email)
    if _is_disposable_or_bogus_email(clean):
        return None

    local, _, host = clean.partition("@")
    local_base = local.split("+", 1)[0]
    local_norm = re.sub(r"[-_.]+", "", local_base)
    dom_match = _domain_matches(host, target_domain)
    personal = is_personal_or_exec_email(clean, target_domain)

    if dom_match and personal:
        if local_base in {"founder", "cofounder", "co-founder", "ceo", "owner"} or local_norm in {"founder", "cofounder", "ceo", "owner"}:
            sub = 3
        elif local_base not in _EXEC_LOCALS and local_norm not in _EXEC_LOCALS:
            sub = 2
        else:
            sub = 1
        return (100, sub, -idx)

    if dom_match and (local_base in _WARM_GENERIC_LOCALS or local_norm in _WARM_GENERIC_LOCALS):
        sub = 5 - ["hello", "hi", "team", "contact", "info"].index(local_base) if local_base in _WARM_GENERIC_LOCALS else 1
        return (70, sub, -idx)

    if not dom_match and personal:
        return (45, 2 if local_base not in _EXEC_LOCALS else 1, -idx)

    if dom_match and local_base in (_GENERIC_BUCKET_LOCALS - _WARM_GENERIC_LOCALS - _FALLBACK_GENERIC_LOCALS):
        return (30, 1, -idx)

    if not dom_match and local_base in _WARM_GENERIC_LOCALS:
        return (25, 1, -idx)

    if local_base in _FALLBACK_GENERIC_LOCALS or local_norm in _FALLBACK_GENERIC_LOCALS:
        sub = 2 if local_base in {"support", "help"} else 1
        return (15, sub, -idx)

    return (15, 0, -idx)


def rank_and_pick_best_email(emails: Iterable[str], domain: str = "") -> str:
    """Rank clean email candidates and return the highest-scoring address, or ''."""
    if isinstance(emails, str):
        items: Iterable[str] = [emails]
    elif emails is None:
        items = ()
    else:
        items = emails

    target_domain = _normalize_target_domain(domain)
    best_rank: tuple[int, int, int] | None = None
    best_email = ""
    seen: set[str] = set()

    for idx, raw in enumerate(items):
        clean = _clean_email_candidate(raw)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        scored = _score_email_tuple(clean, target_domain, idx)
        if scored is None:
            continue
        if best_rank is None or scored > best_rank:
            best_rank = scored
            best_email = clean

    return best_email


def _is_bio_hub_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return False
    return any(host == hub or host.endswith("." + hub) for hub in BIO_HUB_HOSTS)


def _normalize_http_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        if ":" in raw.split("/", 1)[0]:
            return ""
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    clean = parsed._replace(fragment="").geturl()
    return clean


def _url_dedup_key(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{path}{query}"


def _fetch_html(url: str, http_get_fn: Callable[..., Any] | None = None) -> str:
    try:
        if http_get_fn is None:
            resp = requests.get(
                url,
                headers=HTTP_HEADERS,
                timeout=HTTP_TIMEOUT_S,
                allow_redirects=True,
            )
        else:
            kwargs: dict[str, Any] = {}
            try:
                sig = inspect.signature(http_get_fn)
                params = sig.parameters
                accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                if accepts_var_kw or "headers" in params:
                    kwargs["headers"] = HTTP_HEADERS
                if accepts_var_kw or "timeout" in params:
                    kwargs["timeout"] = HTTP_TIMEOUT_S
            except (TypeError, ValueError):
                kwargs = {"headers": HTTP_HEADERS, "timeout": HTTP_TIMEOUT_S}
            resp = http_get_fn(url, **kwargs)
    except Exception:
        return ""

    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    try:
        if hasattr(resp, "status_code"):
            try:
                if int(getattr(resp, "status_code") or 0) >= 400:
                    return ""
            except (TypeError, ValueError):
                pass
        if hasattr(resp, "ok") and getattr(resp, "ok") is False:
            return ""
        return str(getattr(resp, "text", "") or "")
    except Exception:
        return ""


def _is_valid_tg_handle(handle: str) -> bool:
    u = str(handle or "").strip().lstrip("@")
    if not u or len(u) < 4 or len(u) > 32:
        return False
    low = u.lower()
    if low in _TG_RESERVED or low.endswith("bot") or low.endswith("_bot"):
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", u))


def _extract_emails_from_text(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    unescaped = html_mod.unescape(text or "")
    deobfuscated = _OBFUSCATED_DOT_RE.sub(".", _OBFUSCATED_AT_RE.sub("@", unescaped))

    def _add(cand: str) -> None:
        clean = _clean_email_candidate(cand)
        if not clean or clean in seen or _is_disposable_or_bogus_email(clean):
            return
        seen.add(clean)
        found.append(clean)

    for m in _MAILTO_RE.finditer(unescaped):
        _add(m.group(1))
    for blob in (unescaped, deobfuscated):
        for m in _EMAIL_FIND_RE.finditer(blob):
            _add(m.group(0))
    return found


def _extract_tg_handles_from_text(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    unescaped = html_mod.unescape(text or "")

    for pattern in (_TME_RE, _TG_EXPLICIT_RE):
        for m in pattern.finditer(unescaped):
            handle = m.group(1)
            tail = unescaped[m.end() : m.end() + 2]
            if handle.lower() == "s" and tail.startswith("/"):
                continue
            if not _is_valid_tg_handle(handle):
                continue
            low = handle.lower()
            if low not in seen:
                seen.add(low)
                found.append(handle)
    return found


def _pick_best_tg_handle(handles: Iterable[str]) -> str:
    items = [h for h in handles if _is_valid_tg_handle(h)]
    if not items:
        return ""
    for h in items:
        low = h.lower()
        if not any(tok in low for tok in _TG_LOW_PRIORITY_HINTS):
            return h
    return items[0]


def _extract_x_url_from_text(text: str) -> str:
    unescaped = html_mod.unescape(text or "")
    for m in _X_RE.finditer(unescaped):
        handle = m.group(1)
        if handle.casefold() in _X_RESERVED:
            continue
        return f"https://x.com/{handle}"
    return ""


def _extract_ig_url_from_text(text: str) -> str:
    unescaped = html_mod.unescape(text or "")
    for m in _IG_RE.finditer(unescaped):
        handle = m.group(1).strip(".")
        low = handle.lower()
        if not handle or low in _IG_RESERVED or ".." in handle:
            continue
        if low.endswith((".js", ".css", ".png", ".jpg", ".svg", ".html", ".php")):
            continue
        return f"https://instagram.com/{handle}"
    return ""


def _discover_links_from_html(
    html_text: str,
    base_url: str,
    target_dom: str,
) -> tuple[list[str], list[str]]:
    internal: list[tuple[int, str]] = []
    bio_hubs: list[str] = []
    seen_internal: set[str] = set()
    seen_bio: set[str] = set()

    unescaped = html_mod.unescape(html_text or "")
    for m in _HREF_RE.finditer(unescaped):
        raw_href = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        anchor_text = re.sub(r"<[^>]+>", " ", m.group(4) or "").strip().lower()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_url = _normalize_http_url(urljoin(base_url, raw_href))
        if not abs_url:
            continue

        if _is_bio_hub_url(abs_url):
            bkey = _url_dedup_key(abs_url)
            if bkey not in seen_bio:
                seen_bio.add(bkey)
                bio_hubs.append(abs_url)
            continue

        if not target_dom:
            continue
        parsed = urlparse(abs_url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if not _domain_matches(host, target_dom):
            continue
        path_low = (parsed.path or "").lower()
        if path_low.endswith(_IMAGE_OR_ASSET_EXTS):
            continue
        if _url_dedup_key(abs_url) == _url_dedup_key(base_url):
            continue

        haystack = f"{path_low} {anchor_text}"
        priority = -1
        for idx, kw in enumerate(_INTERNAL_LINK_KEYWORDS):
            if kw in haystack:
                priority = len(_INTERNAL_LINK_KEYWORDS) - idx
                break
        if priority < 0:
            continue

        ikey = _url_dedup_key(abs_url)
        if ikey not in seen_internal:
            seen_internal.add(ikey)
            internal.append((priority, abs_url))

    for m in _URL_IN_TEXT_RE.finditer(unescaped):
        cand_url = _normalize_http_url(m.group(0).rstrip(".,;:)'\""))
        if cand_url and _is_bio_hub_url(cand_url):
            bkey = _url_dedup_key(cand_url)
            if bkey not in seen_bio:
                seen_bio.add(bkey)
                bio_hubs.append(cand_url)

    internal.sort(key=lambda item: -item[0])
    return [u for _, u in internal[:MAX_DISCOVERED_INTERNAL]], bio_hubs


def _extract_whatsapp_url(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    return whatsapp_url(raw) or whatsapp_url(unquote(raw))


def crawl_domain_and_bio_links(
    project_url: str,
    extra_links: Iterable[str] = (),
    http_get_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Crawl lead domain (up to 4 internal pages) and up to 2 bio-hub URLs."""
    extra_list = [str(u).strip() for u in (extra_links or ()) if str(u or "").strip()]
    norm_root = _normalize_http_url(project_url)
    root_is_bio = _is_bio_hub_url(norm_root) if norm_root else False
    dom = "" if root_is_bio else domain_of(norm_root)

    fetched_keys: set[str] = set()
    page_blobs: list[str] = []
    bio_queue: list[str] = []
    seen_bio_keys: set[str] = set()

    def _enqueue_bio(u: str) -> None:
        norm = _normalize_http_url(u)
        if not norm or not _is_bio_hub_url(norm):
            return
        k = _url_dedup_key(norm)
        if k not in seen_bio_keys:
            seen_bio_keys.add(k)
            bio_queue.append(norm)

    if root_is_bio and norm_root:
        _enqueue_bio(norm_root)
    for item in extra_list:
        _enqueue_bio(item)

    internal_count = 0
    if dom and norm_root:
        root_key = _url_dedup_key(norm_root)
        fetched_keys.add(root_key)
        internal_count += 1
        root_html = _fetch_html(norm_root, http_get_fn=http_get_fn)
        if root_html:
            page_blobs.append(root_html)
            discovered_internal, discovered_bio = _discover_links_from_html(root_html, norm_root, dom)
        else:
            discovered_internal, discovered_bio = [], []

        for bu in discovered_bio:
            _enqueue_bio(bu)

        internal_queue: list[str] = list(discovered_internal[:MAX_DISCOVERED_INTERNAL])
        if len(internal_queue) < MAX_DISCOVERED_INTERNAL:
            parsed_root = urlparse(norm_root)
            origin = f"{parsed_root.scheme}://{parsed_root.netloc}"
            for suffix in _PROBE_PATHS:
                if internal_count + len(internal_queue) >= MAX_INTERNAL_PAGES:
                    break
                probe_url = f"{origin}{suffix}"
                if _url_dedup_key(probe_url) not in fetched_keys and _url_dedup_key(probe_url) not in {
                    _url_dedup_key(u) for u in internal_queue
                }:
                    internal_queue.append(probe_url)

        for target_url in internal_queue:
            if internal_count >= MAX_INTERNAL_PAGES:
                break
            tkey = _url_dedup_key(target_url)
            if tkey in fetched_keys:
                continue
            fetched_keys.add(tkey)
            internal_count += 1
            html_body = _fetch_html(target_url, http_get_fn=http_get_fn)
            if html_body:
                page_blobs.append(html_body)
                _, more_bio = _discover_links_from_html(html_body, target_url, dom)
                for bu in more_bio:
                    _enqueue_bio(bu)

    bio_fetched = 0
    for bio_url in bio_queue:
        if bio_fetched >= MAX_BIO_HUB_PAGES:
            break
        bkey = _url_dedup_key(bio_url)
        if bkey in fetched_keys:
            continue
        fetched_keys.add(bkey)
        bio_fetched += 1
        bio_html = _fetch_html(bio_url, http_get_fn=http_get_fn)
        if bio_html:
            page_blobs.append(bio_html)

    # Also extract contacts directly from extra_links text without extra HTTP fetches
    all_blobs = page_blobs + extra_list

    emails: list[str] = []
    seen_emails: set[str] = set()
    tg_handles: list[str] = []
    seen_tg: set[str] = set()
    whatsapp = ""
    founder_x = ""
    founder_linkedin = ""
    instagram_url = ""

    for blob in all_blobs:
        for em in _extract_emails_from_text(blob):
            if em not in seen_emails:
                seen_emails.add(em)
                emails.append(em)
        for tg in _extract_tg_handles_from_text(blob):
            if tg.lower() not in seen_tg:
                seen_tg.add(tg.lower())
                tg_handles.append(tg)
        if not whatsapp:
            wa = _extract_whatsapp_url(blob)
            if wa:
                whatsapp = wa
        if not founder_x:
            fx = _extract_x_url_from_text(blob)
            if fx:
                founder_x = fx
        if not founder_linkedin:
            li = linkedin_in_url(blob)
            if li:
                slug = li.rsplit("/", 1)[-1].lower()
                if slug not in {"share", "example", "username", "yourname"}:
                    founder_linkedin = li
        if not instagram_url:
            ig = _extract_ig_url_from_text(blob)
            if ig:
                instagram_url = ig

    tg_username = _pick_best_tg_handle(tg_handles)
    discovered_urls: list[str] = []
    for u in (
        bio_queue[:MAX_BIO_HUB_PAGES]
        + ([f"https://t.me/{tg_username}"] if tg_username else [])
        + [u for u in (whatsapp, founder_x, founder_linkedin, instagram_url) if u]
    ):
        if u and u not in discovered_urls:
            discovered_urls.append(u)

    return {
        "emails": emails,
        "telegram_username": tg_username,
        "whatsapp": whatsapp,
        "founder_x": founder_x,
        "founder_linkedin": founder_linkedin,
        "instagram_url": instagram_url,
        "discovered_links": discovered_urls,
        "pages_fetched": internal_count + bio_fetched,
    }


def _has_personal_tg(row: Mapping[str, Any]) -> str:
    for key in ("username", "telegram_username", "tg_username"):
        raw = str(row.get(key) or "").strip().lstrip("@")
        if _is_valid_tg_handle(raw):
            return raw
    return ""


def _resolve_icp_score(row: Mapping[str, Any]) -> int:
    raw = row.get("icp_score")
    if raw is None or str(raw).strip() == "":
        return 85
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 85


def _parse_pool_usd(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        return max(0, int(val))
    raw = str(val or "").strip()
    if not raw:
        return 0
    cleaned = re.sub(r"[,$\s_]", "", raw)
    try:
        return max(0, int(float(cleaned)))
    except ValueError:
        return 0


def resolve_per_lead_cap_usd(
    row: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
) -> float:
    """Return $0.04 default cap, or $0.15 for super-whales (icp >= 92 or pool_usd >= 10000)."""
    enr_cfg = cfg.get("enrichment") if isinstance(cfg, Mapping) and isinstance(cfg.get("enrichment"), Mapping) else {}
    default_cap = float(enr_cfg.get("per_lead_cap_usd") or PER_LEAD_CAP_USD)
    super_cap = float(enr_cfg.get("super_whale_cap_usd") or SUPER_WHALE_CAP_USD)
    icp_min = int(enr_cfg.get("super_whale_icp_min") or SUPER_WHALE_ICP_MIN)
    pool_min = int(enr_cfg.get("super_whale_pool_min_usd") or SUPER_WHALE_POOL_MIN_USD)

    icp_score = _resolve_icp_score(row)
    pool_usd = _parse_pool_usd(row.get("pool_usd"))
    if icp_score >= icp_min or pool_usd >= pool_min:
        return max(default_cap, super_cap)
    return default_cap


def _extract_payload_cost(payload: Any, default_cost: float) -> float:
    if isinstance(payload, dict):
        candidates: list[Any] = []
        for meta_key in ("_treg", "_monid", "meta"):
            meta = payload.get(meta_key)
            if isinstance(meta, dict):
                candidates.extend([meta.get("cost_usd"), meta.get("cost")])
        candidates.extend([payload.get("cost_usd"), payload.get("cost")])
        for val in candidates:
            if val is None or val == "":
                continue
            try:
                num = float(val)
                if num > 0:
                    return num
            except (TypeError, ValueError):
                continue
    return default_cost


def _extract_emails_from_treg_payload(payload: Any) -> list[str]:
    if not payload:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add_from_str(text: str) -> None:
        for em in _extract_emails_from_text(text):
            if em not in seen:
                seen.add(em)
                found.append(em)

    if isinstance(payload, dict):
        for key in ("work_email", "email", "business_email", "personal_email", "found_email"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                _add_from_str(val)

    for text in walk_strings(payload):
        _add_from_str(text)
    return found


def _unwrap_person_dicts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("employees", "people", "profiles", "results", "items"):
        val = payload.get(key)
        if isinstance(val, list) and val:
            dicts = [item for item in val if isinstance(item, dict)]
            if dicts:
                return dicts
    for sub_key in ("output", "result", "data"):
        child = payload.get(sub_key)
        if isinstance(child, (dict, list)):
            nested = _unwrap_person_dicts(child)
            if nested:
                return nested
    if any(k in payload for k in ("linkedin", "linkedin_url", "profile_url", "full_name", "first_name", "title", "job_title")):
        return [payload]
    return []


def _extract_person_name(person: dict[str, Any]) -> str:
    for key in ("full_name", "name", "person_name"):
        val = " ".join(str(person.get(key) or "").split()).strip()
        if val:
            return val
    first = " ".join(str(person.get("first_name") or "").split()).strip()
    last = " ".join(str(person.get("last_name") or "").split()).strip()
    return f"{first} {last}".strip()


def _extract_person_role(person: dict[str, Any]) -> str:
    for key in ("role", "role_title", "title", "job_title", "headline", "position", "occupation"):
        val = " ".join(str(person.get(key) or "").split()).strip()
        if val:
            return val
    return ""


def _pick_decision_makers(payload: Any, *, limit: int = 2) -> list[dict[str, str]]:
    people = _unwrap_person_dicts(payload)
    if not people or limit <= 0:
        return []

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, person in enumerate(people):
        title_blob = " ".join(
            str(person.get(k) or "")
            for k in ("title", "job_title", "role", "headline", "position", "occupation")
        ).strip()
        if _DM_TIER1_RE.search(title_blob):
            tier = 2
        elif _DM_TIER2_RE.search(title_blob):
            tier = 1
        else:
            tier = 0
        scored.append((tier, -idx, person))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    picked: list[dict[str, str]] = []
    seen_li: set[str] = set()
    seen_names: set[str] = set()

    for _, _, chosen in scored:
        li_raw = ""
        for key in ("linkedin_url", "linkedin", "profile_url", "url"):
            val = str(chosen.get(key) or "").strip()
            if val:
                li = linkedin_in_url(val)
                if li:
                    li_raw = li
                    break

        person_nm = _extract_person_name(chosen)
        email = rank_and_pick_best_email(_extract_emails_from_treg_payload(chosen))
        role = _extract_person_role(chosen)

        li_key = li_raw.casefold()
        nm_key = person_nm.casefold()
        if li_key and li_key in seen_li:
            continue
        if nm_key and nm_key in seen_names and not li_key:
            continue
        if not (li_raw or person_nm or email):
            continue

        if li_key:
            seen_li.add(li_key)
        if nm_key:
            seen_names.add(nm_key)

        entry: dict[str, str] = {}
        if li_raw:
            entry["founder_linkedin"] = li_raw
        if person_nm:
            entry["founder_name"] = person_nm
        if email:
            entry["work_email"] = email
        if role:
            entry["role"] = role
        picked.append(entry)
        if len(picked) >= limit:
            break

    return picked


def _pick_best_decision_maker(payload: Any) -> dict[str, str]:
    dms = _pick_decision_makers(payload, limit=1)
    return dms[0] if dms else {}


def _is_generic_or_empty_name(name_val: str, company: str, dom: str) -> bool:
    clean = " ".join(str(name_val or "").split()).strip()
    if not clean:
        return True
    low = clean.casefold()
    if low in _GENERIC_PERSON_NAMES:
        return True
    if company and low == " ".join(company.split()).casefold():
        return True
    if dom:
        dom_low = dom.casefold()
        if low == dom_low or low == dom_low.split(".", 1)[0]:
            return True
    return False


def _merge_bridge_result(out: dict[str, Any], bridged: Any) -> None:
    if not isinstance(bridged, dict):
        return
    tg = _has_personal_tg(bridged)
    if tg and not _has_personal_tg(out):
        out["username"] = tg
        out["telegram_username"] = tg
        out["tg_username"] = tg
        out["telegram_url"] = bridged.get("telegram_url") or f"https://t.me/{tg}"
    for key in ("tg_bridge_match", "contact_channels"):
        if bridged.get(key) and not out.get(key):
            out[key] = bridged[key]
    if isinstance(bridged.get("links"), list):
        existing = [str(u).strip() for u in (out.get("links") or []) if str(u or "").strip()]
        seen = {u.casefold() for u in existing}
        for u in bridged["links"]:
            su = str(u or "").strip()
            if su and su.casefold() not in seen:
                seen.add(su.casefold())
                existing.append(su)
        out["links"] = existing


def _normalize_phone_e164(raw: Any) -> str:
    """Normalize a phone string or wa.me URL to E.164 '+<digits>', or ''."""
    text = str(raw or "").strip()
    if not text:
        return ""
    wa = _extract_whatsapp_url(text)
    if wa:
        digits = re.sub(r"\D", "", wa)
        if 7 <= len(digits) <= 15:
            return f"+{digits}"
    if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}(?:[T\s].*)?", text):
        return ""
    digits = re.sub(r"\D", "", text)
    if text.startswith("00") and len(digits) > 9:
        digits = digits[2:]
    if not (7 <= len(digits) <= 15):
        return ""
    if len(set(digits)) == 1:
        return ""
    return f"+{digits}"


def _extract_ig_handle(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" not in text and "instagram.com" not in text.lower() and "/" not in text:
        handle = text.lstrip("@").strip(".")
        low = handle.lower()
        if handle and low not in _IG_RESERVED and re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
            return handle
        return ""
    ig_url = _extract_ig_url_from_text(text if "://" in text else f"https://{text.lstrip('/')}")
    if not ig_url:
        return ""
    return ig_url.rsplit("/", 1)[-1]


def _unwrap_monid_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("output", "result", "data"):
        child = payload.get(key)
        if isinstance(child, dict):
            return child
    return payload


def _invoke_monid(
    caller: Callable[..., Any],
    provider: str,
    endpoint: str,
    *,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        sig = inspect.signature(caller)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if accepts_var_kw or "body" in params:
            return caller(provider, endpoint, body=body)
        if "data" in params:
            return caller(provider, endpoint, data=body)
        if "input" in params:
            return caller(provider, endpoint, input=body)
        return caller(provider, endpoint, body)
    except Exception:
        return None


def _collect_urls_from_field(val: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(val, str):
        s = val.strip()
        if s:
            for m in _URL_IN_TEXT_RE.finditer(s):
                urls.append(m.group(0).rstrip(".,;:)'\""))
            if not urls:
                norm = _normalize_http_url(s)
                if norm:
                    urls.append(norm)
    elif isinstance(val, dict):
        for k in ("url", "href", "link", "externalUrl", "website"):
            urls.extend(_collect_urls_from_field(val.get(k)))
    elif isinstance(val, (list, tuple)):
        for item in val:
            urls.extend(_collect_urls_from_field(item))
    return urls


def fetch_fb_ig_business_contacts(
    row: Mapping[str, Any],
    monid_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Layer 0.5 (monid): extract phone, website, links, and email from FB Page & IG profile.

    - Facebook Page (`fb_page_id` or `fb_page_url`): calls `anyapi` `/facebook.page_contact`
      with `route="giraffe"`.
    - Instagram Profile (`instagram_handle` or `instagram_url`): calls `anyapi`
      `/instagram.profile_contact` with `route="octopus"`, falling back to `route="chameleon"`
      when octopus fails or returns zero contacts/links.
    """
    empty: dict[str, Any] = {
        "phones": [],
        "phone": "",
        "emails": [],
        "email": "",
        "website": "",
        "links": [],
        "whatsapp": "",
        "telegram_username": "",
        "cost_usd": 0.0,
        "sources": [],
    }
    if monid_fn is not None:
        caller = monid_fn
    elif run_monid is not _ORIG_RUN_MONID or check_monid_ready():
        caller = run_monid
    else:
        return empty

    phones: list[str] = []
    seen_phones: set[str] = set()
    emails: list[str] = []
    seen_emails: set[str] = set()
    links: list[str] = []
    seen_links: set[str] = set()
    text_blobs: list[str] = []
    sources: list[str] = []
    website = ""
    total_cost = 0.0

    def _add_phone(raw_p: Any) -> bool:
        if isinstance(raw_p, dict):
            added = False
            for k in ("phone", "number", "value", "publicPhone", "e164", "raw"):
                if raw_p.get(k) and _add_phone(raw_p.get(k)):
                    added = True
            return added
        if isinstance(raw_p, (list, tuple)):
            added = False
            for item in raw_p:
                if _add_phone(item):
                    added = True
            return added
        norm = _normalize_phone_e164(raw_p)
        if not norm:
            return False
        key = norm.lstrip("+")
        if key not in seen_phones:
            seen_phones.add(key)
            phones.append(norm)
        return True

    def _add_email(raw_e: Any) -> bool:
        if isinstance(raw_e, (list, tuple)):
            added = False
            for item in raw_e:
                if _add_email(item):
                    added = True
            return added
        clean = _clean_email_candidate(raw_e)
        if not clean or _is_disposable_or_bogus_email(clean):
            return False
        if clean not in seen_emails:
            seen_emails.add(clean)
            emails.append(clean)
        return True

    def _add_link(raw_u: Any) -> bool:
        added = False
        for u in _collect_urls_from_field(raw_u):
            norm = _normalize_http_url(u)
            if not norm:
                continue
            key = norm.casefold()
            if key not in seen_links:
                seen_links.add(key)
                links.append(norm)
            text_blobs.append(norm)
            added = True
        return added

    def _maybe_set_website(raw_u: Any) -> None:
        nonlocal website
        for u in _collect_urls_from_field(raw_u):
            norm = _normalize_http_url(u)
            if not norm:
                continue
            _add_link(norm)
            if not website and not _is_bio_hub_url(norm) and domain_of(norm):
                website = norm

    # 1) Facebook Page contact
    fb_target = str(row.get("fb_page_url") or row.get("fb_page_id") or "").strip()
    if fb_target:
        raw_fb = _invoke_monid(
            caller,
            "anyapi",
            "/facebook.page_contact",
            body={"page": fb_target, "route": "giraffe"},
        )
        if isinstance(raw_fb, dict) and raw_fb:
            total_cost += _extract_payload_cost(raw_fb, COST_MONID_FB_CONTACT_USD)
            sources.append("facebook.page_contact:giraffe")
            fb_data = _unwrap_monid_payload(raw_fb)
            _add_phone(fb_data.get("phone"))
            _add_phone(fb_data.get("phones"))
            _add_email(fb_data.get("email"))
            _add_email(fb_data.get("emails"))
            _maybe_set_website(fb_data.get("website"))
            _add_link(fb_data.get("links"))
            if isinstance(fb_data.get("about"), str):
                text_blobs.append(fb_data["about"])
            for cand_u in links:
                if not website and not _is_bio_hub_url(cand_u) and domain_of(cand_u):
                    website = cand_u
                    break

    # 2) Instagram Profile contact (route="octopus" -> fallback route="chameleon")
    ig_handle = _extract_ig_handle(row.get("instagram_handle") or row.get("instagram_url") or "")
    if ig_handle:
        def _ingest_ig(payload: dict[str, Any]) -> bool:
            ig_data = _unwrap_monid_payload(payload)
            got = False
            if _add_phone(ig_data.get("publicPhone")):
                got = True
            if _add_phone(ig_data.get("phones")):
                got = True
            if _add_email(ig_data.get("publicEmail")):
                got = True
            if _add_email(ig_data.get("emails")):
                got = True
            if _add_link(ig_data.get("bioLinks")):
                got = True
            if _add_link(ig_data.get("externalUrl")):
                _maybe_set_website(ig_data.get("externalUrl"))
                got = True
            if isinstance(ig_data.get("bio"), str) and ig_data["bio"].strip():
                text_blobs.append(ig_data["bio"])
                for em in _extract_emails_from_text(ig_data["bio"]):
                    if _add_email(em):
                        got = True
            return got

        raw_oct = _invoke_monid(
            caller,
            "anyapi",
            "/instagram.profile_contact",
            body={"handle": ig_handle, "route": "octopus"},
        )
        oct_got = False
        if isinstance(raw_oct, dict) and raw_oct:
            total_cost += _extract_payload_cost(raw_oct, COST_MONID_IG_OCTOPUS_USD)
            sources.append("instagram.profile_contact:octopus")
            oct_got = _ingest_ig(raw_oct)

        if not oct_got:
            raw_cham = _invoke_monid(
                caller,
                "anyapi",
                "/instagram.profile_contact",
                body={"handle": ig_handle, "route": "chameleon"},
            )
            if isinstance(raw_cham, dict) and raw_cham:
                total_cost += _extract_payload_cost(raw_cham, COST_MONID_IG_CHAMELEON_USD)
                sources.append("instagram.profile_contact:chameleon")
                _ingest_ig(raw_cham)

    joined_blobs = "\n".join(text_blobs)
    wa_found = _extract_whatsapp_url(joined_blobs)
    tg_handles = _extract_tg_handles_from_text(joined_blobs)
    tg_found = _pick_best_tg_handle(tg_handles)
    best_email = rank_and_pick_best_email(emails, domain=domain_of(website))

    return {
        "phones": phones,
        "phone": phones[0] if phones else "",
        "emails": emails,
        "email": best_email,
        "website": website,
        "links": links,
        "whatsapp": wa_found,
        "telegram_username": tg_found,
        "cost_usd": round(total_cost, 6),
        "sources": sources,
    }


def _extract_phones_with_hints_from_payload(payload: Any) -> list[tuple[str, str]]:
    """Extract ordered unique (phone_e164, line_type_hint) tuples from treg phone payloads."""
    if not payload:
        return []
    found: list[tuple[str, str]] = []
    seen_digits: set[str] = set()
    _skip_keys = frozenset({"_treg", "meta", "request_parameters", "id", "uuid", "timestamp", "created_at", "took_ms"})
    _phone_keys = frozenset(
        {
            "phone",
            "phones",
            "phone_number",
            "phone_numbers",
            "mobile_phone",
            "mobile_phones",
            "mobile",
            "direct_phone",
            "direct_phones",
            "number",
            "e164",
            "telephone",
            "telephone_number",
            "formatted_telephone_number",
            "publicphone",
            "value",
        }
    )

    def _add(raw_val: Any, hint: str = "") -> None:
        norm = _normalize_phone_e164(raw_val)
        if not norm:
            return
        dkey = norm.lstrip("+")
        if dkey not in seen_digits:
            seen_digits.add(dkey)
            found.append((norm, hint.strip().upper()))

    def _walk(node: Any, inherited_hint: str = "") -> None:
        if isinstance(node, dict):
            local_hint = str(
                node.get("line_type")
                or node.get("telephone_number_type")
                or node.get("line")
                or node.get("type")
                or node.get("kind")
                or inherited_hint
                or ""
            ).strip()
            for k, v in node.items():
                kl = str(k).lower()
                if kl in _skip_keys:
                    continue
                if kl in _phone_keys:
                    if isinstance(v, (str, int)):
                        _add(v, local_hint)
                    elif isinstance(v, dict):
                        sub_hint = str(
                            v.get("line_type")
                            or v.get("telephone_number_type")
                            or v.get("line")
                            or v.get("type")
                            or v.get("kind")
                            or local_hint
                            or ""
                        ).strip()
                        for pk in ("value", "number", "phone", "e164", "raw", "formatted"):
                            if v.get(pk):
                                _add(v.get(pk), sub_hint)
                        _walk(v, sub_hint)
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, (str, int)):
                                _add(item, local_hint)
                            else:
                                _walk(item, local_hint)
                elif isinstance(v, (dict, list)):
                    _walk(v, inherited_hint)
        elif isinstance(node, list):
            for item in node:
                _walk(item, inherited_hint)

    _walk(payload)
    # Prefer entries explicitly hinted as mobile first when multiple candidates exist
    found.sort(key=lambda item: (0 if "MOBILE" in item[1] else (2 if item[1] in _HLR_DROP_TYPES else 1)))
    return found


def _parse_hlr_line_type(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    containers: list[dict[str, Any]] = [payload]
    for sub in ("output", "result", "data"):
        child = payload.get(sub)
        if isinstance(child, dict):
            containers.append(child)
    for cont in containers:
        results = cont.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            cont = results[0]
        for key in ("telephone_number_type", "line_type", "number_type", "type"):
            val = cont.get(key)
            if isinstance(val, str) and val.strip():
                return re.sub(r"[\s\-]+", "_", val.strip().upper())
    return ""


def validate_phone_hlr(
    phone: str,
    treg_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Free HLR line-type check via hlrlookup.people.phone.validate ($0).

    Drops numbers explicitly classified as LANDLINE or TOLL_FREE.
    Keeps MOBILE, FIXED_LINE_OR_MOBILE, or unknown.
    """
    e164 = _normalize_phone_e164(phone)
    digits_no_plus = e164.lstrip("+")
    if not digits_no_plus:
        return {"phone": "", "line_type": "INVALID", "keep": False, "raw": None}

    active_treg = treg_fn if treg_fn is not None else (treg_call if check_treg_ready() else None)
    if active_treg is None:
        return {"phone": e164, "line_type": "UNKNOWN", "keep": True, "raw": None}

    try:
        sig = inspect.signature(active_treg)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        call_kw: dict[str, Any] = {}
        if accepts_var_kw or "method" in params:
            call_kw["method"] = "POST"
        if accepts_var_kw or "data" in params:
            call_kw["data"] = {"telephone_number": digits_no_plus}
        resp = active_treg("hlrlookup.people.phone.validate", **call_kw)
    except Exception:
        resp = None

    line_type = _parse_hlr_line_type(resp)
    if not line_type:
        return {"phone": e164, "line_type": "UNKNOWN", "keep": True, "raw": resp}
    keep = line_type not in _HLR_DROP_TYPES
    return {"phone": e164, "line_type": line_type, "keep": keep, "raw": resp}


def _parse_contactout_phone_available(payload: Any) -> bool | None:
    if not isinstance(payload, dict):
        return None
    containers: list[dict[str, Any]] = [payload]
    for sub in ("output", "result", "data"):
        child = payload.get(sub)
        if isinstance(child, dict):
            containers.append(child)
    for cont in containers:
        for key in ("available", "has_phone", "phone_available", "hasPhone"):
            if key in cont and isinstance(cont[key], bool):
                return cont[key]
        status = cont.get("phone_status") or cont.get("status")
        if isinstance(status, str):
            low = status.strip().lower()
            if low in {"unavailable", "none", "not_found", "false", "no"}:
                return False
            if low in {"available", "found", "true", "yes"}:
                return True
    return None


def _check_budget_guard(
    budget_guard_fn: Callable[..., Any] | None,
    provider: str,
    est_cost: float,
    *,
    endpoint: str = "",
) -> bool:
    if budget_guard_fn is None:
        return True
    try:
        sig = inspect.signature(budget_guard_fn)
        params = list(sig.parameters.values())
        pos_count = sum(
            1 for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
        has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        if has_var_pos or pos_count >= 3:
            res = budget_guard_fn(provider, endpoint, float(est_cost))
        elif pos_count == 2:
            res = budget_guard_fn(provider, float(est_cost))
        elif any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            res = budget_guard_fn(provider=provider, endpoint=endpoint, estimated_cost=float(est_cost))
        else:
            kw: dict[str, Any] = {}
            pnames = {p.name for p in params}
            if "provider" in pnames:
                kw["provider"] = provider
            if "endpoint" in pnames:
                kw["endpoint"] = endpoint
            if "estimated_cost" in pnames:
                kw["estimated_cost"] = float(est_cost)
            res = budget_guard_fn(**kw)
    except Exception as exc:
        logger.debug("budget_guard_fn failed: %s", exc)
        return False
    if isinstance(res, tuple):
        return bool(res[0]) if res else False
    return bool(res)


def _record_cost_event(
    cost_record_fn: Callable[..., Any] | None,
    *,
    provider: str,
    endpoint: str,
    lead_key: str,
    cost_usd: float,
    hit: bool,
) -> None:
    if cost_record_fn is None:
        return
    try:
        cost_record_fn(
            provider=provider,
            endpoint=endpoint,
            lead_key=lead_key,
            cost_usd=float(cost_usd),
            hit=bool(hit),
        )
    except TypeError:
        try:
            cost_record_fn(provider, endpoint, lead_key, float(cost_usd), bool(hit))
        except Exception as exc:
            logger.debug("cost_record_fn fallback failed: %s", exc)
    except Exception as exc:
        logger.debug("cost_record_fn failed: %s", exc)


def _call_treg_with_cap(
    active_treg: Callable[..., Any],
    endpoint: str,
    *,
    max_cost: float | None = None,
    **call_kw: Any,
) -> Any:
    try:
        sig = inspect.signature(active_treg)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if max_cost is not None and (accepts_var_kw or "headers" in params):
            cap_str = f"{max(0.0001, max_cost):.5f}"
            call_kw = {**call_kw, "headers": {"X-Treg-Route-Max-Cost": cap_str}}
        filtered_kw = call_kw if accepts_var_kw else {k: v for k, v in call_kw.items() if k in params}
    except (TypeError, ValueError):
        filtered_kw = call_kw
    return active_treg(endpoint, **filtered_kw)


def _build_secondary_contact_json(dm: Mapping[str, Any]) -> str:
    payload = {
        "name": str(dm.get("founder_name") or dm.get("name") or "").strip(),
        "role": str(dm.get("role") or dm.get("role_title") or dm.get("title") or "").strip(),
        "founder_linkedin": str(dm.get("founder_linkedin") or "").strip(),
        "phone": str(dm.get("phone") or "").strip(),
        "telegram_username": str(dm.get("telegram_username") or dm.get("username") or "").strip().lstrip("@"),
        "whatsapp": str(dm.get("whatsapp") or "").strip(),
        "fallback_after_days": SECONDARY_DM_FALLBACK_DAYS,
    }
    return json.dumps(payload, ensure_ascii=False)


def _add_dm_candidate(dm_candidates: list[dict[str, str]], cand: Mapping[str, Any]) -> None:
    if not isinstance(cand, Mapping):
        return
    li = linkedin_in_url(cand.get("founder_linkedin") or cand.get("linkedin_url") or "")
    name = " ".join(str(cand.get("founder_name") or cand.get("name") or "").split()).strip()
    role = " ".join(str(cand.get("role") or cand.get("role_title") or cand.get("title") or "").split()).strip()
    raw_email = _clean_email_candidate(cand.get("work_email") or cand.get("email") or "")
    email = "" if (not raw_email or _is_disposable_or_bogus_email(raw_email)) else raw_email
    phone = _normalize_phone_e164(cand.get("phone") or "")
    tg = str(cand.get("telegram_username") or cand.get("username") or "").strip().lstrip("@")
    wa = _extract_whatsapp_url(cand.get("whatsapp") or "")

    if not any((li, name, email, phone)):
        return

    li_key = li.casefold()
    nm_key = name.casefold()
    for existing in dm_candidates:
        ex_li = existing.get("founder_linkedin", "").casefold()
        ex_nm = existing.get("founder_name", "").casefold()
        if (li_key and ex_li and li_key == ex_li) or (nm_key and ex_nm and nm_key == ex_nm):
            if li and not existing.get("founder_linkedin"):
                existing["founder_linkedin"] = li
            if name and not existing.get("founder_name"):
                existing["founder_name"] = name
            if role and not existing.get("role"):
                existing["role"] = role
            if email and not existing.get("work_email"):
                existing["work_email"] = email
            if phone and not existing.get("phone"):
                existing["phone"] = phone
            if tg and not existing.get("telegram_username"):
                existing["telegram_username"] = tg
            if wa and not existing.get("whatsapp"):
                existing["whatsapp"] = wa
            return

    if len(dm_candidates) >= 2:
        return

    entry: dict[str, str] = {}
    if li:
        entry["founder_linkedin"] = li
    if name:
        entry["founder_name"] = name
    if role:
        entry["role"] = role
    if email:
        entry["work_email"] = email
    if phone:
        entry["phone"] = phone
    if tg:
        entry["telegram_username"] = tg
    if wa:
        entry["whatsapp"] = wa
    dm_candidates.append(entry)


def _run_phone_waterfall_for_dm(
    dm: dict[str, str],
    *,
    dom: str,
    lead_key: str,
    active_treg: Callable[..., Any],
    remaining_fn: Callable[[], float],
    is_super_whale: bool,
    hlr_cache: dict[str, dict[str, Any]],
    budget_guard_fn: Callable[..., Any] | None = None,
    cost_record_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run Layer 1 phone waterfall + free HLR mobile filter for one DM.

    Logic for ContactOut pre-check (`contactout.people.phone.available`, $0):
    - Only runs when `founder_linkedin` is present.
    - If it explicitly returns `available=False` / `has_phone=False`, we STILL allow
      `crawl4ai.people.phone.find` ($0.00095) and cap `treg.people.phone.find` at
      `<= $0.01` unless the lead is a super-whale (`icp >= 92` or `pool_usd >= 10000`).
    """
    dm_li = linkedin_in_url(dm.get("founder_linkedin") or "")
    dm_name = str(dm.get("founder_name") or "").strip()
    dm_email = str(dm.get("work_email") or "").strip()
    if not (dm_li or (dm_name and dom) or dm_email):
        return {"phone": "", "line_type": "", "cost": 0.0}

    spent = 0.0

    def _pick_first_mobile(candidates: list[tuple[str, str]]) -> tuple[str, str]:
        for cand_phone, line_hint in candidates:
            digits = cand_phone.lstrip("+")
            if digits in hlr_cache:
                hlr = hlr_cache[digits]
            else:
                hlr = validate_phone_hlr(cand_phone, treg_fn=active_treg)
                hlr_cache[digits] = hlr
            if not hlr.get("keep"):
                continue
            hlr_type = str(hlr.get("line_type") or "UNKNOWN").upper()
            if hlr_type == "UNKNOWN" and line_hint and line_hint.upper() in _HLR_DROP_TYPES:
                continue
            return str(hlr["phone"]), hlr_type
        return "", ""

    # a) Free ContactOut availability pre-check ($0) when LinkedIn URL is known
    co_explicit_false = False
    if dm_li:
        try:
            co_resp = _call_treg_with_cap(
                active_treg,
                "contactout.people.phone.available",
                query={"profile": dm_li},
            )
            co_avail = _parse_contactout_phone_available(co_resp)
            if co_avail is False:
                co_explicit_false = True
        except Exception as exc:
            logger.debug("contactout phone availability pre-check failed: %s", exc)

    # b) crawl4ai.people.phone.find ($0.00095 explicit endpoint; not in treg auto-router)
    rem_after_prev = max(0.0, remaining_fn() - spent)
    if rem_after_prev + 1e-9 >= COST_CRAWL4AI_PHONE_USD and _check_budget_guard(
        budget_guard_fn, "treg", COST_CRAWL4AI_PHONE_USD, endpoint="crawl4ai.people.phone.find"
    ):
        if dm_li:
            c4_query = {"linkedin": dm_li}
        elif dm_name and dom:
            c4_query = {"name": dm_name, "domain": dom}
        else:
            c4_query = {"email": dm_email}
        try:
            c4_resp = _call_treg_with_cap(
                active_treg,
                "crawl4ai.people.phone.find",
                max_cost=rem_after_prev,
                query=c4_query,
            )
        except Exception:
            c4_resp = None
        c4_cands = _extract_phones_with_hints_from_payload(c4_resp)
        c4_cost = _extract_payload_cost(c4_resp, COST_CRAWL4AI_PHONE_USD) if (c4_cands or _extract_payload_cost(c4_resp, 0.0) > 0) else 0.0
        if c4_cost > 0:
            spent += c4_cost
            _record_cost_event(
                cost_record_fn,
                provider="treg",
                endpoint="crawl4ai.people.phone.find",
                lead_key=lead_key,
                cost_usd=c4_cost,
                hit=bool(c4_cands),
            )
        if c4_cands:
            mob_phone, mob_type = _pick_first_mobile(c4_cands)
            if mob_phone:
                return {"phone": mob_phone, "line_type": mob_type, "cost": round(spent, 6)}

    # c) treg.people.phone.find (POST, routed waterfall capped by X-Treg-Route-Max-Cost)
    rem_for_treg = max(0.0, remaining_fn() - spent)
    if co_explicit_false and not is_super_whale:
        rem_for_treg = min(rem_for_treg, CONTACTOUT_NEG_TREG_CAP_USD)

    if rem_for_treg + 1e-9 >= COST_TREG_PHONE_MIN_USD and _check_budget_guard(
        budget_guard_fn, "treg", min(rem_for_treg, COST_TREG_PHONE_MIN_USD), endpoint="treg.people.phone.find"
    ):
        if dm_li:
            treg_body: dict[str, Any] = {"linkedin_url": dm_li}
        elif dm_name and dom:
            treg_body = {"domain": dom, "full_name": dm_name}
            if dm_email:
                treg_body["email"] = dm_email
        else:
            treg_body = {"email": dm_email}

        try:
            tr_resp = _call_treg_with_cap(
                active_treg,
                "treg.people.phone.find",
                max_cost=rem_for_treg,
                method="POST",
                data=treg_body,
            )
        except Exception:
            tr_resp = None
        tr_cands = _extract_phones_with_hints_from_payload(tr_resp)
        tr_cost = _extract_payload_cost(tr_resp, COST_TREG_PHONE_MIN_USD) if (tr_cands or _extract_payload_cost(tr_resp, 0.0) > 0) else 0.0
        if tr_cost > 0:
            spent += tr_cost
            _record_cost_event(
                cost_record_fn,
                provider="treg",
                endpoint="treg.people.phone.find",
                lead_key=lead_key,
                cost_usd=tr_cost,
                hit=bool(tr_cands),
            )
        if tr_cands:
            mob_phone, mob_type = _pick_first_mobile(tr_cands)
            if mob_phone:
                return {"phone": mob_phone, "line_type": mob_type, "cost": round(spent, 6)}

    return {"phone": "", "line_type": "", "cost": round(spent, 6)}


def _make_bridge_budget_guard(
    *,
    paid_enabled: bool,
    remaining_fn: Callable[[], float],
    budget_guard_fn: Callable[..., Any] | None,
) -> Callable[[str, str, float], bool]:
    def _guard(provider: str, endpoint: str, est_cost: float) -> bool:
        if not paid_enabled:
            return False
        if remaining_fn() + 1e-9 < float(est_cost):
            return False
        return _check_budget_guard(budget_guard_fn, provider, float(est_cost), endpoint=endpoint)

    return _guard


def enrich_lead_contacts(
    row: dict[str, Any],
    *,
    allow_paid_tier2: bool = True,
    budget_usd: list[float] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
    dm_finder_fn: Callable[[str, str], dict[str, Any]] | None = None,
    dm_multi_finder_fn: Callable[..., list[dict[str, Any]]] | None = None,
    tg_bridge_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    budget_guard_fn: Callable[..., Any] | None = None,
    cost_record_fn: Callable[..., Any] | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run multi-layer contact waterfall on a single lead row without mutating input."""
    out = dict(row)
    custom_mode = any(
        fn is not None
        for fn in (monid_fn, treg_fn, http_get_fn, dm_finder_fn, dm_multi_finder_fn, tg_bridge_fn)
    )
    raw_links = out.get("links")
    if isinstance(raw_links, (list, tuple)):
        links_list = [str(u).strip() for u in raw_links if str(u or "").strip()]
    elif isinstance(raw_links, str) and raw_links.strip():
        links_list = [raw_links.strip()]
    else:
        links_list = []

    project_url = str(out.get("project_url") or out.get("website") or out.get("site") or out.get("url") or "").strip()
    dom = domain_of(project_url) or domain_of(str(out.get("domain") or out.get("company_domain") or ""))
    company = str(out.get("company") or out.get("project") or out.get("name") or "").strip()
    lead_key = str(out.get("id") or dom or company or "lead").strip()

    email_pool: list[str] = []
    for key in ("work_email", "email"):
        val = str(out.get(key) or "").strip()
        if val:
            email_pool.append(val)

    # --- Tier 1: free local crawl ---
    crawled = crawl_domain_and_bio_links(project_url, extra_links=links_list, http_get_fn=http_get_fn)
    email_pool.extend(crawled.get("emails") or [])

    best_tier1_email = rank_and_pick_best_email(email_pool, domain=dom)
    if best_tier1_email:
        out["work_email"] = best_tier1_email
        out["email"] = best_tier1_email

    existing_x = x_profile_url(out)
    if not str(out.get("founder_x") or "").strip():
        if existing_x:
            out["founder_x"] = existing_x
        elif crawled.get("founder_x"):
            out["founder_x"] = crawled["founder_x"]
    if not linkedin_in_url(out.get("founder_linkedin") or "") and crawled.get("founder_linkedin"):
        out["founder_linkedin"] = crawled["founder_linkedin"]
    existing_wa = _extract_whatsapp_url(out.get("whatsapp") or out.get("phone") or "")
    if not existing_wa and crawled.get("whatsapp"):
        existing_wa = _extract_whatsapp_url(crawled["whatsapp"])
    if existing_wa:
        out["whatsapp"] = existing_wa
        wa_digits = re.sub(r"\D", "", existing_wa)
        if wa_digits and not str(out.get("phone") or "").strip():
            out["phone"] = f"+{wa_digits}"
    if not str(out.get("instagram_url") or "").strip() and crawled.get("instagram_url"):
        out["instagram_url"] = crawled["instagram_url"]

    existing_tg = _has_personal_tg(out)
    if not existing_tg and crawled.get("telegram_username"):
        tg_handle = crawled["telegram_username"]
        out["username"] = tg_handle
        out["telegram_username"] = tg_handle
    elif existing_tg:
        if not str(out.get("username") or "").strip():
            out["username"] = existing_tg
        if not str(out.get("telegram_username") or "").strip():
            out["telegram_username"] = existing_tg

    lead_cost = 0.0
    tier2_unlocked_contact = False
    lead_cap = resolve_per_lead_cap_usd(out, cfg=cfg)
    max_for_lead = min(lead_cap, float(budget_usd[0])) if budget_usd is not None and len(budget_usd) > 0 else lead_cap
    icp_score = _resolve_icp_score(out)
    is_super_whale = icp_score >= SUPER_WHALE_ICP_MIN or _parse_pool_usd(out.get("pool_usd")) >= SUPER_WHALE_POOL_MIN_USD

    def _remaining() -> float:
        return max(0.0, max_for_lead - lead_cost)

    active_treg = treg_fn if treg_fn is not None else (treg_call if check_treg_ready() else None)
    hlr_cache: dict[str, dict[str, Any]] = {}

    # --- Layer 0.5 (monid): FB Page & IG Business Contact when TG/WA still missing ---
    has_fb_or_ig = bool(
        str(out.get("fb_page_url") or out.get("fb_page_id") or "").strip()
        or _extract_ig_handle(out.get("instagram_handle") or out.get("instagram_url") or "")
    )
    monid_allowed = allow_paid_tier2 and ((monid_fn is not None) or (not custom_mode and check_monid_ready()))
    budget_ok_monid = budget_usd is None or (len(budget_usd) > 0 and budget_usd[0] > 0.0)
    if (
        has_fb_or_ig
        and monid_allowed
        and budget_ok_monid
        and not (_has_personal_tg(out) or _extract_whatsapp_url(out.get("whatsapp") or ""))
    ):
        has_fb_target = bool(str(out.get("fb_page_url") or out.get("fb_page_id") or "").strip())
        est_monid_cost = COST_MONID_FB_CONTACT_USD if has_fb_target else COST_MONID_IG_CHAMELEON_USD
        monid_ep_hint = "anyapi:/facebook.page_contact" if has_fb_target else "anyapi:/instagram.profile_contact"
        if _remaining() + 1e-9 >= est_monid_cost and _check_budget_guard(
            budget_guard_fn, "monid", est_monid_cost, endpoint=monid_ep_hint
        ):
            fb_ig = fetch_fb_ig_business_contacts(out, monid_fn=monid_fn)
            monid_cost = float(fb_ig.get("cost_usd") or 0.0)
            if monid_cost > 0:
                lead_cost += monid_cost
                _record_cost_event(
                    cost_record_fn,
                    provider="monid",
                    endpoint=",".join(fb_ig.get("sources") or ["monid"]),
                    lead_key=lead_key,
                    cost_usd=monid_cost,
                    hit=bool(
                        fb_ig.get("phone")
                        or fb_ig.get("email")
                        or fb_ig.get("whatsapp")
                        or fb_ig.get("telegram_username")
                        or fb_ig.get("website")
                    ),
                )
            email_pool.extend(fb_ig.get("emails") or [])
            new_links_from_monid: list[str] = []
            seen_existing_links = {u.casefold() for u in links_list}
            for u in fb_ig.get("links") or []:
                if u and u.casefold() not in seen_existing_links:
                    seen_existing_links.add(u.casefold())
                    links_list.append(u)
                    new_links_from_monid.append(u)

            discovered_site = str(fb_ig.get("website") or "").strip()
            if discovered_site and not project_url:
                project_url = discovered_site
                out["project_url"] = discovered_site
                dom = dom or domain_of(discovered_site)
                if monid_cost > 0:
                    tier2_unlocked_contact = True

            if (discovered_site or any(_is_bio_hub_url(u) for u in new_links_from_monid)) and not (
                _has_personal_tg(out) or _extract_whatsapp_url(out.get("whatsapp") or "")
            ):
                extra_crawled = crawl_domain_and_bio_links(
                    discovered_site if discovered_site else "",
                    extra_links=new_links_from_monid,
                    http_get_fn=http_get_fn,
                )
                email_pool.extend(extra_crawled.get("emails") or [])
                if not str(out.get("founder_x") or "").strip() and extra_crawled.get("founder_x"):
                    out["founder_x"] = extra_crawled["founder_x"]
                if not linkedin_in_url(out.get("founder_linkedin") or "") and extra_crawled.get("founder_linkedin"):
                    out["founder_linkedin"] = extra_crawled["founder_linkedin"]
                if not _extract_whatsapp_url(out.get("whatsapp") or "") and extra_crawled.get("whatsapp"):
                    out["whatsapp"] = extra_crawled["whatsapp"]
                    extra_wa_digits = re.sub(r"\D", "", extra_crawled["whatsapp"])
                    if extra_wa_digits and not str(out.get("phone") or "").strip():
                        out["phone"] = f"+{extra_wa_digits}"
                if not _has_personal_tg(out) and extra_crawled.get("telegram_username"):
                    out["username"] = extra_crawled["telegram_username"]
                    out["telegram_username"] = extra_crawled["telegram_username"]
                for disc_u in extra_crawled.get("discovered_links") or []:
                    if disc_u and disc_u.casefold() not in seen_existing_links:
                        seen_existing_links.add(disc_u.casefold())
                        links_list.append(disc_u)

            if fb_ig.get("whatsapp") and not _extract_whatsapp_url(out.get("whatsapp") or ""):
                out["whatsapp"] = fb_ig["whatsapp"]
                wa_digits = re.sub(r"\D", "", fb_ig["whatsapp"])
                if wa_digits and not str(out.get("phone") or "").strip():
                    out["phone"] = f"+{wa_digits}"
                if monid_cost > 0:
                    tier2_unlocked_contact = True
            if fb_ig.get("telegram_username") and not _has_personal_tg(out):
                out["username"] = fb_ig["telegram_username"]
                out["telegram_username"] = fb_ig["telegram_username"]
                if monid_cost > 0:
                    tier2_unlocked_contact = True

            if fb_ig.get("phones") and not str(out.get("phone") or "").strip():
                for monid_phone in fb_ig["phones"]:
                    digits = monid_phone.lstrip("+")
                    if active_treg is not None:
                        if digits in hlr_cache:
                            hlr = hlr_cache[digits]
                        else:
                            hlr = validate_phone_hlr(monid_phone, treg_fn=active_treg)
                            hlr_cache[digits] = hlr
                        if not hlr.get("keep"):
                            continue
                        out["phone"] = str(hlr["phone"])
                        out["phone_line_type"] = str(hlr.get("line_type") or "UNKNOWN")
                    else:
                        out["phone"] = monid_phone
                    if monid_cost > 0:
                        tier2_unlocked_contact = True
                    break

    # --- Tier 1 DM lookup (supports up to 2 decision makers) ---
    dm_candidates: list[dict[str, str]] = []
    if linkedin_in_url(out.get("founder_linkedin") or "") or (
        str(out.get("founder_name") or "").strip()
        and not _is_generic_or_empty_name(str(out.get("founder_name") or ""), company, dom)
    ):
        _add_dm_candidate(
            dm_candidates,
            {
                "founder_linkedin": out.get("founder_linkedin") or "",
                "founder_name": out.get("founder_name") or "",
                "role": out.get("role") or "",
                "work_email": out.get("work_email") or "",
            },
        )

    need_dm_lookup = (
        (not linkedin_in_url(out.get("founder_linkedin") or ""))
        or (dm_multi_finder_fn is not None and len(dm_candidates) < 2)
        or (
            not custom_mode
            and not out.get("secondary_contact_json")
            and len(dm_candidates) < 2
            and not (_has_personal_tg(out) or _extract_whatsapp_url(out.get("whatsapp") or ""))
        )
    )
    if need_dm_lookup and (company or dom):
        dm_rows: list[dict[str, Any]] = []
        if dm_multi_finder_fn is not None:
            try:
                sig = inspect.signature(dm_multi_finder_fn)
                if "limit" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    res_multi = dm_multi_finder_fn(company, dom, limit=2)
                else:
                    res_multi = dm_multi_finder_fn(company, dom)
                if isinstance(res_multi, list):
                    dm_rows = [r for r in res_multi if isinstance(r, dict)]
                elif isinstance(res_multi, dict) and res_multi:
                    dm_rows = [res_multi]
            except Exception:
                dm_rows = []
        elif dm_finder_fn is not None:
            try:
                res_one = dm_finder_fn(company, dom) or {}
                if isinstance(res_one, list):
                    dm_rows = [r for r in res_one if isinstance(r, dict)]
                elif isinstance(res_one, dict) and res_one:
                    dm_rows = [res_one]
            except Exception:
                dm_rows = []
        else:
            dm_search_allowed = (
                allow_paid_tier2
                and (_remaining() + 1e-9 >= COST_CRAWL4AI_SEARCH_USD)
                and _check_budget_guard(
                    budget_guard_fn,
                    "treg",
                    COST_CRAWL4AI_SEARCH_USD,
                    endpoint="crawl4ai.web.search",
                )
            )
            multi_fn = getattr(_treg_enrich_mod, "find_company_decision_makers", None)
            if callable(multi_fn):
                if dm_search_allowed:
                    base_dm_treg = treg_fn if treg_fn is not None else (treg_call if check_treg_ready() else None)

                    def _dm_search_treg_wrapper(ep: str, **call_kw: Any) -> Any:
                        if base_dm_treg is None:
                            return None
                        if _remaining() + 1e-9 < COST_CRAWL4AI_SEARCH_USD:
                            return None
                        if not _check_budget_guard(
                            budget_guard_fn,
                            "treg",
                            COST_CRAWL4AI_SEARCH_USD,
                            endpoint=str(ep or "crawl4ai.web.search"),
                        ):
                            return None
                        res_s = base_dm_treg(ep, **call_kw)
                        if isinstance(res_s, dict):
                            s_cost = _extract_payload_cost(res_s, COST_CRAWL4AI_SEARCH_USD)
                            if s_cost > 0:
                                nonlocal lead_cost
                                lead_cost += s_cost
                                _record_cost_event(
                                    cost_record_fn,
                                    provider="treg",
                                    endpoint=str(ep or "crawl4ai.web.search"),
                                    lead_key=lead_key,
                                    cost_usd=s_cost,
                                    hit=bool(res_s),
                                )
                        return res_s

                    try:
                        sig = inspect.signature(multi_fn)
                        kw: dict[str, Any] = {"limit": 2}
                        if "treg_fn" in sig.parameters or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                        ):
                            kw["treg_fn"] = _dm_search_treg_wrapper
                        if base_dm_treg is not None:
                            res_multi = multi_fn(company, dom, **kw)
                            if isinstance(res_multi, list):
                                dm_rows = [r for r in res_multi if isinstance(r, dict)]
                    except Exception:
                        dm_rows = []
            elif find_company_decision_maker is not _ORIG_FIND_DM or (
                dm_search_allowed and treg_fn is None and check_treg_ready()
            ):
                try:
                    res_one = find_company_decision_maker(company, dom) or {}
                    if isinstance(res_one, dict) and res_one:
                        dm_rows = [res_one]
                    if find_company_decision_maker is _ORIG_FIND_DM and check_treg_ready():
                        lead_cost += COST_CRAWL4AI_SEARCH_USD
                        _record_cost_event(
                            cost_record_fn,
                            provider="treg",
                            endpoint="crawl4ai.web.search",
                            lead_key=lead_key,
                            cost_usd=COST_CRAWL4AI_SEARCH_USD,
                            hit=bool(dm_rows),
                        )
                except Exception:
                    dm_rows = []

        for dm_found in dm_rows[:2]:
            _add_dm_candidate(dm_candidates, dm_found)
            dm_email = str(dm_found.get("work_email") or "").strip()
            if dm_email:
                email_pool.append(dm_email)
            dm_site = str(dm_found.get("project_url") or "").strip()
            if dm_site and not project_url:
                project_url = dm_site
                out["project_url"] = dm_site
                dom = dom or domain_of(dm_site)

        if dm_candidates:
            primary_dm = dm_candidates[0]
            if primary_dm.get("founder_linkedin") and not linkedin_in_url(out.get("founder_linkedin") or ""):
                out["founder_linkedin"] = primary_dm["founder_linkedin"]
            if primary_dm.get("founder_name") and not str(out.get("founder_name") or "").strip():
                out["founder_name"] = primary_dm["founder_name"]

    best_tier1_email = rank_and_pick_best_email(email_pool, domain=dom)
    if best_tier1_email:
        out["work_email"] = best_tier1_email
        out["email"] = best_tier1_email

    bridge_fn = bridge_lead_to_telegram if tg_bridge_fn is None else tg_bridge_fn
    _bridge_budget_guard = _make_bridge_budget_guard(
        paid_enabled=bool(allow_paid_tier2),
        remaining_fn=_remaining,
        budget_guard_fn=budget_guard_fn,
    )

    def _bridge_cost_record(
        *,
        provider: str,
        endpoint: str,
        lead_key: str = lead_key,
        cost_usd: float = 0.0,
        hit: bool = False,
    ) -> None:
        nonlocal lead_cost
        if cost_usd > 0:
            lead_cost += float(cost_usd)
        _record_cost_event(
            cost_record_fn,
            provider=provider,
            endpoint=endpoint,
            lead_key=lead_key,
            cost_usd=float(cost_usd),
            hit=bool(hit),
        )

    def _run_bridge(row_in: dict[str, Any]) -> Any:
        if tg_bridge_fn is not None:
            return bridge_fn(row_in)
        bridge_allow_paid = bool(
            allow_paid_tier2
            and (budget_usd is None or (len(budget_usd) > 0 and _remaining() > 0.0))
        )
        try:
            sig = inspect.signature(bridge_fn)
            params = sig.parameters
            if "allow_paid" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                return bridge_fn(
                    row_in,
                    allow_paid=bridge_allow_paid,
                    budget_guard_fn=_bridge_budget_guard,
                    cost_record_fn=_bridge_cost_record,
                    treg_fn=treg_fn,
                    monid_fn=monid_fn,
                )
        except (TypeError, ValueError):
            pass
        return bridge_fn(row_in)

    try:
        bridged = _run_bridge(out)
        _merge_bridge_result(out, bridged)
    except Exception as exc:
        logger.debug("tier1 tg_bridge failed: %s", exc)

    # --- Tier 2: paid treg email + phone waterfall + HLR filter ---
    has_tg = bool(_has_personal_tg(out))
    has_wa = bool(_extract_whatsapp_url(out.get("whatsapp") or ""))
    has_personal_email = is_personal_or_exec_email(str(out.get("work_email") or ""), dom)
    budget_ok = budget_usd is None or (len(budget_usd) > 0 and budget_usd[0] + 1e-9 >= COST_CRAWL4AI_PHONE_USD)

    if (
        allow_paid_tier2
        and budget_ok
        and icp_score >= 80
        and not (has_tg or has_wa)
    ):
        if active_treg is not None:
            unlocked_identity = False
            li_url = linkedin_in_url(out.get("founder_linkedin") or "")

            # Step 2A: Email & missing DM discovery (when no personal email or no LinkedIn)
            if li_url and not has_personal_email:
                if _remaining() + 1e-9 >= COST_CRAWL4AI_EMAIL_USD and _check_budget_guard(
                    budget_guard_fn, "treg", COST_CRAWL4AI_EMAIL_USD, endpoint="crawl4ai.people.email.find"
                ):
                    try:
                        p1 = _call_treg_with_cap(
                            active_treg,
                            "crawl4ai.people.email.find",
                            max_cost=_remaining(),
                            query={"linkedin": li_url},
                        )
                    except Exception:
                        p1 = None
                    c1_emails = _extract_emails_from_treg_payload(p1)
                    c1_cost = _extract_payload_cost(p1, COST_CRAWL4AI_EMAIL_USD) if (c1_emails or _extract_payload_cost(p1, 0.0) > 0) else 0.0
                    if c1_cost > 0:
                        lead_cost += c1_cost
                        _record_cost_event(
                            cost_record_fn,
                            provider="treg",
                            endpoint="crawl4ai.people.email.find",
                            lead_key=lead_key,
                            cost_usd=c1_cost,
                            hit=bool(c1_emails),
                        )
                    email_pool.extend(c1_emails)
                    picked1 = rank_and_pick_best_email(c1_emails, domain=dom)
                    new_or_personal_1 = bool(picked1) and (
                        picked1 != best_tier1_email or is_personal_or_exec_email(picked1, dom)
                    )
                    if new_or_personal_1:
                        unlocked_identity = True
                        tier2_unlocked_contact = True
                    elif _remaining() + 1e-9 >= COST_BLITZAPI_EMAIL_USD and _check_budget_guard(
                        budget_guard_fn, "treg", COST_BLITZAPI_EMAIL_USD, endpoint="blitzapi.people.email.find"
                    ):
                        try:
                            p2 = _call_treg_with_cap(
                                active_treg,
                                "blitzapi.people.email.find",
                                max_cost=_remaining(),
                                method="POST",
                                data={"person_linkedin_url": li_url},
                            )
                        except Exception:
                            p2 = None
                        c2_emails = _extract_emails_from_treg_payload(p2)
                        c2_cost = _extract_payload_cost(p2, COST_BLITZAPI_EMAIL_USD) if (c2_emails or _extract_payload_cost(p2, 0.0) > 0) else 0.0
                        if c2_cost > 0:
                            lead_cost += c2_cost
                            _record_cost_event(
                                cost_record_fn,
                                provider="treg",
                                endpoint="blitzapi.people.email.find",
                                lead_key=lead_key,
                                cost_usd=c2_cost,
                                hit=bool(c2_emails),
                            )
                        email_pool.extend(c2_emails)
                        if rank_and_pick_best_email(c2_emails, domain=dom):
                            unlocked_identity = True
                            tier2_unlocked_contact = True

            elif not li_url and dom and _remaining() + 1e-9 >= COST_LEADMAGIC_EMP_USD and _check_budget_guard(
                budget_guard_fn, "treg", COST_LEADMAGIC_EMP_USD, endpoint="leadmagic.x.employee-finder"
            ):
                try:
                    emp_payload = _call_treg_with_cap(
                        active_treg,
                        "leadmagic.x.employee-finder",
                        max_cost=_remaining(),
                        method="POST",
                        data={"company_domain": dom, "limit": 5},
                    )
                except Exception:
                    emp_payload = None
                emp_dms = _pick_decision_makers(emp_payload, limit=2)
                emp_cost = _extract_payload_cost(emp_payload, COST_LEADMAGIC_EMP_USD) if (emp_dms or _extract_payload_cost(emp_payload, 0.0) > 0) else 0.0
                if emp_cost > 0:
                    lead_cost += emp_cost
                    _record_cost_event(
                        cost_record_fn,
                        provider="treg",
                        endpoint="leadmagic.x.employee-finder",
                        lead_key=lead_key,
                        cost_usd=emp_cost,
                        hit=bool(emp_dms),
                    )
                for dm_found in emp_dms:
                    _add_dm_candidate(dm_candidates, dm_found)

                dm_info = emp_dms[0] if emp_dms else {}
                if dm_info.get("founder_linkedin") and not linkedin_in_url(out.get("founder_linkedin") or ""):
                    out["founder_linkedin"] = dm_info["founder_linkedin"]
                    unlocked_identity = True
                    tier2_unlocked_contact = True
                if dm_info.get("founder_name"):
                    if not str(out.get("founder_name") or "").strip():
                        out["founder_name"] = dm_info["founder_name"]
                        unlocked_identity = True
                    if _is_generic_or_empty_name(str(out.get("name") or ""), company, dom):
                        out["name"] = dm_info["founder_name"]
                if dm_info.get("work_email"):
                    email_pool.append(dm_info["work_email"])
                    unlocked_identity = True
                    tier2_unlocked_contact = True

                person_nm = str(dm_info.get("founder_name") or out.get("founder_name") or "").strip()
                best_so_far = rank_and_pick_best_email(email_pool, domain=dom)
                has_good_dm_email = bool(dm_info.get("work_email")) or is_personal_or_exec_email(best_so_far, dom)
                if (
                    not has_good_dm_email
                    and person_nm
                    and dom
                    and _remaining() + 1e-9 >= COST_CRAWL4AI_EMAIL_USD
                    and _check_budget_guard(
                        budget_guard_fn, "treg", COST_CRAWL4AI_EMAIL_USD, endpoint="crawl4ai.people.email.find"
                    )
                ):
                    try:
                        p_name = _call_treg_with_cap(
                            active_treg,
                            "crawl4ai.people.email.find",
                            max_cost=_remaining(),
                            query={"name": person_nm, "domain": dom},
                        )
                    except Exception:
                        p_name = None
                    c_name_emails = _extract_emails_from_treg_payload(p_name)
                    c_name_cost = _extract_payload_cost(p_name, COST_CRAWL4AI_EMAIL_USD) if (c_name_emails or _extract_payload_cost(p_name, 0.0) > 0) else 0.0
                    if c_name_cost > 0:
                        lead_cost += c_name_cost
                        _record_cost_event(
                            cost_record_fn,
                            provider="treg",
                            endpoint="crawl4ai.people.email.find",
                            lead_key=lead_key,
                            cost_usd=c_name_cost,
                            hit=bool(c_name_emails),
                        )
                    email_pool.extend(c_name_emails)
                    if rank_and_pick_best_email(c_name_emails, domain=dom):
                        unlocked_identity = True
                        tier2_unlocked_contact = True

            if unlocked_identity and not _has_personal_tg(out):
                try:
                    bridged2 = _run_bridge(out)
                    _merge_bridge_result(out, bridged2)
                    if _has_personal_tg(out):
                        tier2_unlocked_contact = True
                except Exception as exc:
                    logger.debug("tier2 tg_bridge failed: %s", exc)

            # Step 2B: Layer 1 Phone Waterfall + HLR Filter (for up to 2 DMs)
            # Only runs if TG, WA, and mobile phone are still missing.
            if not (
                _has_personal_tg(out)
                or _extract_whatsapp_url(out.get("whatsapp") or "")
                or str(out.get("phone") or "").strip()
            ):
                if not dm_candidates:
                    fallback_name = str(out.get("founder_name") or "").strip()
                    fallback_email = rank_and_pick_best_email(email_pool, domain=dom)
                    if fallback_name and not _is_generic_or_empty_name(fallback_name, company, dom):
                        _add_dm_candidate(
                            dm_candidates,
                            {"founder_name": fallback_name, "work_email": fallback_email},
                        )
                    elif fallback_email and is_personal_or_exec_email(fallback_email, dom):
                        _add_dm_candidate(dm_candidates, {"work_email": fallback_email})

                for dm_item in dm_candidates[:2]:
                    if dm_item.get("phone"):
                        continue
                    wf_res = _run_phone_waterfall_for_dm(
                        dm_item,
                        dom=dom,
                        lead_key=lead_key,
                        active_treg=active_treg,
                        remaining_fn=_remaining,
                        is_super_whale=is_super_whale,
                        hlr_cache=hlr_cache,
                        budget_guard_fn=budget_guard_fn,
                        cost_record_fn=cost_record_fn,
                    )
                    lead_cost += float(wf_res.get("cost") or 0.0)
                    if wf_res.get("phone"):
                        dm_item["phone"] = str(wf_res["phone"])
                        dm_item["phone_line_type"] = str(wf_res.get("line_type") or "UNKNOWN")
                        tier2_unlocked_contact = True

                # Choose best DM as primary (prefer one with phone/TG/WA)
                if dm_candidates:
                    dm0 = dm_candidates[0]
                    dm1 = dm_candidates[1] if len(dm_candidates) > 1 else None
                    if not dm0.get("phone") and dm1 and dm1.get("phone"):
                        dm_candidates[0], dm_candidates[1] = dm1, dm0
                        primary_dm = dm_candidates[0]
                        if primary_dm.get("founder_linkedin"):
                            out["founder_linkedin"] = primary_dm["founder_linkedin"]
                        if primary_dm.get("founder_name"):
                            out["founder_name"] = primary_dm["founder_name"]
                        if primary_dm.get("work_email"):
                            email_pool.append(primary_dm["work_email"])
                    if dm_candidates[0].get("phone"):
                        out["phone"] = dm_candidates[0]["phone"]
                        if dm_candidates[0].get("phone_line_type"):
                            out["phone_line_type"] = dm_candidates[0]["phone_line_type"]

    # Persist 2nd DM into secondary_contact_json when present
    if len(dm_candidates) >= 2:
        out["secondary_contact_json"] = _build_secondary_contact_json(dm_candidates[1])

    actual_spent = round(lead_cost, 6)
    if budget_usd is not None and len(budget_usd) > 0 and actual_spent > 0:
        budget_usd[0] = max(0.0, round(budget_usd[0] - actual_spent, 6))
    lead_cost = actual_spent

    # --- Final canonicalization ---
    final_email = rank_and_pick_best_email(email_pool, domain=dom)
    out["work_email"] = final_email
    out["email"] = final_email
    out["lead_cost"] = round(lead_cost, 6)

    seen_links: set[str] = set()
    merged_links: list[str] = []

    def _add_link(url: Any) -> None:
        su = str(url or "").strip()
        if not su or not su.lower().startswith(("http://", "https://")):
            return
        key = su.casefold()
        if key not in seen_links:
            seen_links.add(key)
            merged_links.append(su)

    for existing_u in links_list + (out.get("links") if isinstance(out.get("links"), list) else []):
        _add_link(existing_u)
    _add_link(project_url)
    for disc_u in crawled.get("discovered_links") or []:
        _add_link(disc_u)
    final_tg = _has_personal_tg(out)
    if final_tg:
        _add_link(f"https://t.me/{final_tg}")
    for key in ("whatsapp", "founder_x", "founder_linkedin", "instagram_url", "telegram_url"):
        _add_link(out.get(key))
    out["links"] = merged_links

    has_any_contact = bool(
        final_tg
        or _extract_whatsapp_url(out.get("whatsapp") or "")
        or str(out.get("phone") or "").strip()
        or x_profile_url(out)
        or str(out.get("founder_x") or "").strip()
        or linkedin_in_url(out.get("founder_linkedin") or "")
        or str(out.get("instagram_url") or "").strip()
        or final_email
    )
    if tier2_unlocked_contact:
        out["contact_waterfall_tier"] = "tier2"
    elif has_any_contact:
        out["contact_waterfall_tier"] = "tier1"
    else:
        out["contact_waterfall_tier"] = "none"

    return out


def _stdlib_load_dotenv(env_path: Path) -> None:
    if (os.getenv("CRMTG_E2E_SANDBOX") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    if (os.getenv("CRMTG_SKIP_DOTENV") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        if key:
            os.environ.setdefault(key, val)


def _load_env() -> None:
    env_path = ROOT / ".env"
    try:
        try:
            from leadgen import vps_autonomous_replenish
        except ImportError:
            import vps_autonomous_replenish
        vps_autonomous_replenish._load_dotenv_if_allowed(env_path)
    except Exception:
        _stdlib_load_dotenv(env_path)


def main(argv: list[str] | None = None) -> int:
    _load_env()

    parser = argparse.ArgumentParser(description="Two-tier contact waterfall enrichment for JSONL leads")
    parser.add_argument("--input", required=True, help="Input JSONL path")
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument("--max-budget-usd", type=float, default=0.10, help="Total paid Tier-2 USD cap")
    parser.add_argument("--no-paid-tier2", action="store_true", help="Disable paid treg Tier-2 fallback")
    args = parser.parse_args(argv)

    in_path = Path(args.input)
    rows: list[dict[str, Any]] = []
    if in_path.is_file():
        for line in in_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)

    initial_budget = max(0.0, float(args.max_budget_usd))
    budget = [initial_budget]
    enriched = [
        enrich_lead_contacts(
            row,
            allow_paid_tier2=not args.no_paid_tier2,
            budget_usd=budget,
        )
        for row in rows
    ]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in enriched:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    t1 = sum(1 for r in enriched if r.get("contact_waterfall_tier") == "tier1")
    t2 = sum(1 for r in enriched if r.get("contact_waterfall_tier") == "tier2")
    t0 = sum(1 for r in enriched if r.get("contact_waterfall_tier") == "none")
    spent = max(0.0, round(initial_budget - budget[0], 6))
    print(
        f"contact_waterfall: total={len(enriched)} tier1={t1} tier2={t2} none={t0} spent=${spent:.5f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
