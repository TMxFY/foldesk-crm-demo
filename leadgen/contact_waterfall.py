#!/usr/bin/env python3
"""Bounded two-tier contact waterfall for SoftUGC B2B leads.

Tier 1 (free):
  - Bounded crawl of lead project domain (root + up to 2 discovered
    contact/about/team links or /contact, /about, /terms probes, max 4 internal
    pages) plus up to 2 bio-hub pages (linktr.ee, linktree.com, beacons.ai,
    carrd.co, solo.to).
  - Ranks and upgrades emails via rank_and_pick_best_email.
  - Fills missing X, LinkedIn /in/, TG @username, WhatsApp, Instagram.
  - Falls back to find_company_decision_maker when founder LinkedIn is missing.
  - Runs tg_identity_bridge.bridge_lead_to_telegram to resolve personal TG.

Tier 2 (paid treg fallback, hard-capped <= $0.01/lead and <= max budget):
  - Runs only for icp_score >= 80 leads still lacking TG, WhatsApp, X, and a
    personal/exec email.
  - Uses crawl4ai.people.email.find -> blitzapi.people.email.find when LinkedIn
    is known, or leadmagic.x.employee-finder (+ crawl4ai.people.email.find)
    when only domain is known, then re-probes Telegram identity bridge once.
"""
from __future__ import annotations

import argparse
import html as html_mod
import inspect
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from leadgen.treg_client import call as treg_call, check_treg_ready, walk_strings
    from leadgen.treg_enrich import domain_of, find_company_decision_maker
    from leadgen.tg_identity_bridge import bridge_lead_to_telegram
    from leadgen.tg_to_gsheet import linkedin_in_url, whatsapp_url, x_profile_url
except ImportError:
    from treg_client import call as treg_call, check_treg_ready, walk_strings
    from treg_enrich import domain_of, find_company_decision_maker
    from tg_identity_bridge import bridge_lead_to_telegram
    from tg_to_gsheet import linkedin_in_url, whatsapp_url, x_profile_url


USER_AGENT = "Mozilla/5.0 (compatible; SoftUGC/1.0)"
HTTP_HEADERS = {"User-Agent": USER_AGENT}
HTTP_TIMEOUT_S = 6
MAX_INTERNAL_PAGES = 4
MAX_DISCOVERED_INTERNAL = 2
MAX_BIO_HUB_PAGES = 2
PER_LEAD_CAP_USD = 0.01

COST_CRAWL4AI_EMAIL_USD = 0.00095
COST_BLITZAPI_EMAIL_USD = 0.001
COST_LEADMAGIC_EMP_USD = 0.00375

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


def _has_personal_tg(row: dict[str, Any]) -> str:
    for key in ("username", "telegram_username", "tg_username"):
        raw = str(row.get(key) or "").strip().lstrip("@")
        if _is_valid_tg_handle(raw):
            return raw
    return ""


def _resolve_icp_score(row: dict[str, Any]) -> int:
    raw = row.get("icp_score")
    if raw is None or str(raw).strip() == "":
        return 85
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 85


def _extract_payload_cost(payload: Any, default_cost: float) -> float:
    if isinstance(payload, dict):
        meta = payload.get("_treg")
        candidates = [
            meta.get("cost_usd") if isinstance(meta, dict) else None,
            meta.get("cost") if isinstance(meta, dict) else None,
            payload.get("cost_usd"),
            payload.get("cost"),
        ]
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
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
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


def _pick_best_decision_maker(payload: Any) -> dict[str, str]:
    people = _unwrap_person_dicts(payload)
    if not people:
        return {}

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
    chosen = scored[0][2]

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
    out: dict[str, str] = {}
    if li_raw:
        out["founder_linkedin"] = li_raw
    if person_nm:
        out["founder_name"] = person_nm
    if email:
        out["work_email"] = email
    return out


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


def enrich_lead_contacts(
    row: dict[str, Any],
    *,
    allow_paid_tier2: bool = True,
    budget_usd: list[float] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    dm_finder_fn: Callable[[str, str], dict[str, Any]] | None = None,
    tg_bridge_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run two-tier contact waterfall on a single lead row without mutating input."""
    out = dict(row)
    raw_links = out.get("links")
    if isinstance(raw_links, (list, tuple)):
        links_list = [str(u).strip() for u in raw_links if str(u or "").strip()]
    elif isinstance(raw_links, str) and raw_links.strip():
        links_list = [raw_links.strip()]
    else:
        links_list = []

    project_url = str(out.get("project_url") or out.get("website") or out.get("site") or out.get("url") or "").strip()
    dom = domain_of(project_url) or domain_of(str(out.get("domain") or ""))
    company = str(out.get("company") or out.get("project") or out.get("name") or "").strip()

    email_pool: list[str] = []
    for key in ("work_email", "email"):
        val = str(out.get(key) or "").strip()
        if val:
            email_pool.append(val)

    # --- Tier 1: free local crawl + DM lookup + TG identity bridge ---
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
    if not _extract_whatsapp_url(out.get("whatsapp") or "") and crawled.get("whatsapp"):
        out["whatsapp"] = crawled["whatsapp"]
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

    if not linkedin_in_url(out.get("founder_linkedin") or "") and (company or dom):
        dm_fn = find_company_decision_maker if dm_finder_fn is None else dm_finder_fn
        try:
            dm_found = dm_fn(company, dom) or {}
        except Exception:
            dm_found = {}
        if isinstance(dm_found, dict):
            li = linkedin_in_url(dm_found.get("founder_linkedin") or "")
            if li and not linkedin_in_url(out.get("founder_linkedin") or ""):
                out["founder_linkedin"] = li
            dm_email = str(dm_found.get("work_email") or "").strip()
            if dm_email:
                email_pool.append(dm_email)
            dm_name = str(dm_found.get("founder_name") or "").strip()
            if dm_name and not str(out.get("founder_name") or "").strip():
                out["founder_name"] = dm_name
            dm_site = str(dm_found.get("project_url") or "").strip()
            if dm_site and not project_url:
                project_url = dm_site
                out["project_url"] = dm_site
                dom = dom or domain_of(dm_site)

    best_tier1_email = rank_and_pick_best_email(email_pool, domain=dom)
    if best_tier1_email:
        out["work_email"] = best_tier1_email
        out["email"] = best_tier1_email

    bridge_fn = bridge_lead_to_telegram if tg_bridge_fn is None else tg_bridge_fn
    try:
        bridged = bridge_fn(out)
        _merge_bridge_result(out, bridged)
    except Exception:
        pass

    # --- Tier 2: paid treg fallback ---
    lead_cost = 0.0
    tier2_unlocked_contact = False

    has_tg = bool(_has_personal_tg(out))
    has_wa = bool(_extract_whatsapp_url(out.get("whatsapp") or ""))
    has_x = bool(x_profile_url(out) or _extract_x_url_from_text(str(out.get("founder_x") or "")))
    has_personal_email = is_personal_or_exec_email(str(out.get("work_email") or ""), dom)
    icp_score = _resolve_icp_score(out)
    budget_ok = budget_usd is None or (len(budget_usd) > 0 and budget_usd[0] + 1e-9 >= 0.001)

    if (
        allow_paid_tier2
        and budget_ok
        and icp_score >= 80
        and not (has_tg or has_wa or has_x or has_personal_email)
    ):
        active_treg = treg_fn if treg_fn is not None else (treg_call if check_treg_ready() else None)
        if active_treg is not None:
            max_for_lead = min(PER_LEAD_CAP_USD, float(budget_usd[0]) if budget_usd is not None else PER_LEAD_CAP_USD)
            unlocked_identity = False

            def _remaining() -> float:
                return max(0.0, max_for_lead - lead_cost)

            def _call_treg_capped(ep: str, **call_kw: Any) -> Any:
                cap_str = f"{max(0.0001, _remaining()):.5f}"
                try:
                    sig = inspect.signature(active_treg)
                    params = sig.parameters
                    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                    if accepts_var_kw or "headers" in params:
                        call_kw = {**call_kw, "headers": {"X-Treg-Route-Max-Cost": cap_str}}
                except (TypeError, ValueError):
                    pass
                return active_treg(ep, **call_kw)

            li_url = linkedin_in_url(out.get("founder_linkedin") or "")
            if li_url:
                if _remaining() + 1e-9 >= COST_CRAWL4AI_EMAIL_USD:
                    try:
                        p1 = _call_treg_capped("crawl4ai.people.email.find", query={"linkedin": li_url})
                        lead_cost += _extract_payload_cost(p1, COST_CRAWL4AI_EMAIL_USD)
                    except Exception:
                        p1 = None
                    c1_emails = _extract_emails_from_treg_payload(p1)
                    email_pool.extend(c1_emails)
                    picked1 = rank_and_pick_best_email(c1_emails, domain=dom)
                    new_or_personal_1 = bool(picked1) and (
                        picked1 != best_tier1_email or is_personal_or_exec_email(picked1, dom)
                    )
                    if new_or_personal_1:
                        unlocked_identity = True
                        tier2_unlocked_contact = True
                    elif _remaining() + 1e-9 >= COST_BLITZAPI_EMAIL_USD:
                        try:
                            p2 = _call_treg_capped(
                                "blitzapi.people.email.find",
                                method="POST",
                                data={"person_linkedin_url": li_url},
                            )
                            lead_cost += _extract_payload_cost(p2, COST_BLITZAPI_EMAIL_USD)
                        except Exception:
                            p2 = None
                        c2_emails = _extract_emails_from_treg_payload(p2)
                        email_pool.extend(c2_emails)
                        if rank_and_pick_best_email(c2_emails, domain=dom):
                            unlocked_identity = True
                            tier2_unlocked_contact = True

            elif dom and _remaining() + 1e-9 >= COST_LEADMAGIC_EMP_USD:
                try:
                    emp_payload = _call_treg_capped(
                        "leadmagic.x.employee-finder",
                        method="POST",
                        data={"company_domain": dom, "limit": 3},
                    )
                    lead_cost += _extract_payload_cost(emp_payload, COST_LEADMAGIC_EMP_USD)
                except Exception:
                    emp_payload = None

                dm_info = _pick_best_decision_maker(emp_payload)
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
                if not has_good_dm_email and person_nm and dom and _remaining() + 1e-9 >= COST_CRAWL4AI_EMAIL_USD:
                    try:
                        p_name = _call_treg_capped(
                            "crawl4ai.people.email.find",
                            query={"name": person_nm, "domain": dom},
                        )
                        lead_cost += _extract_payload_cost(p_name, COST_CRAWL4AI_EMAIL_USD)
                    except Exception:
                        p_name = None
                    c_name_emails = _extract_emails_from_treg_payload(p_name)
                    email_pool.extend(c_name_emails)
                    if rank_and_pick_best_email(c_name_emails, domain=dom):
                        unlocked_identity = True
                        tier2_unlocked_contact = True

            actual_spent = round(lead_cost, 6)
            if budget_usd is not None and actual_spent > 0:
                budget_usd[0] = max(0.0, round(budget_usd[0] - actual_spent, 6))
            lead_cost = actual_spent

            if unlocked_identity and not _has_personal_tg(out):
                try:
                    bridged2 = bridge_fn(out)
                    _merge_bridge_result(out, bridged2)
                    if _has_personal_tg(out):
                        tier2_unlocked_contact = True
                except Exception:
                    pass

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
    if os.getenv("BOT_TOKEN") == "e2e-not-a-secret":
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
