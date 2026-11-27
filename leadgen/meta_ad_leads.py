#!/usr/bin/env python3
"""Active Meta Ad Library video advertisers enriched with decision-maker contacts.

Scrapes active multi-GEO video ads via scrapecreators.x.v1-facebook-adlibrary-search-ads,
extracts Click-to-WhatsApp CTAs and FB/IG metadata, deduplicates advertisers over a
90-day window, and enriches decision-maker contacts via find_company_decision_maker.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import inspect
import json
import os
import re
import signal
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

try:
    from treg_client import call
except ImportError:
    try:
        from leadgen.treg_client import call
    except ImportError:
        from scripts.treg_client import call

try:
    from supply_side import is_supply_side, sanitize_person_and_company, sniper_reject_reason
except ImportError:
    try:
        from leadgen.supply_side import is_supply_side, sanitize_person_and_company, sniper_reject_reason
    except ImportError:
        from scripts.supply_side import is_supply_side, sanitize_person_and_company, sniper_reject_reason

try:
    from treg_enrich import domain_of, find_company_decision_maker
except ImportError:
    try:
        from leadgen.treg_enrich import domain_of, find_company_decision_maker
    except ImportError:
        from scripts.treg_enrich import domain_of, find_company_decision_maker

ENDPOINT = "scrapecreators.x.v1-facebook-adlibrary-search-ads"
SEARCH_QUERIES = (
    # Cluster 1: Subscription consumer / AI apps (high-LTV $50-$100+/yr)
    "AI coaching app",
    "AI astrology app",
    "AI language learning",
    "fitness tracker app",
    "calorie tracker AI",
    "AI note taker",
    "AI video editor",
    "AI therapy app",
    "AI trading assistant",
    "sleep tracker subscription",
    "AI productivity workspace",
    "AI tutor app",
    # Cluster 2: Crypto / Fintech / Prop-trading
    "crypto wallet app",
    "crypto portfolio tracker",
    "prop trading challenge",
    "funded trader challenge",
    "forex prop firm",
    "crypto exchange app",
    "options trading app",
    "wealth management app",
    "copy trading platform",
    # Cluster 3: Large Shopify / D2C brands (high-AOV / subscription LTV $50-$100+)
    "skincare routine serum",
    "daily supplement gummies",
    "eco household detergent",
    "shopify beauty brand",
    "longevity supplement subscription",
    "peptide skincare serum",
    "hair growth serum subscription",
    "protein greens subscription",
    "red light therapy device",
    "smart sleep mattress",
    # Cluster 4: High-ticket infobiz / coaching & mentorship
    "executive coaching program",
    "business mastermind cohort",
    "high ticket coaching",
    "real estate investing mentorship",
    "trading mentorship program",
    "fitness coaching transformation",
    "wealth building workshop",
)
TARGET_COUNTRIES = (
    "US",
    "GB",
    "AE",
    "SA",
    "DE",
    "FR",
    "NL",
    "ES",
    "IT",
    "SE",
    "CA",
    "AU",
)
GCC_COUNTRIES = ("SA", "AE", "QA", "KW", "BH", "OM")
MENA_EXTENDED_COUNTRIES = ("EG", "JO")
ARABIC_GCC_SEARCH_QUERIES = (
    "عطور",
    "العناية بالبشرة",
    "عيادة تجميل",
    "عيادة أسنان",
    "عيادة جلدية",
    "تحاليل طبية",
    "مكملات غذائية",
    "تطبيق ذكاء اصطناعي",
    "متجر سلة",
    "dubai aesthetic clinic",
    "riyadh skincare brand",
    "saudi perfume brand",
    "dubai fitness app",
    "telehealth saudi",
    "weight loss clinic dubai",
)
CAP = int(os.getenv("META_ADS_CAP", "80"))
SEEN_DAYS = 90
_NON_PERSONAL_TG_SUFFIXES = (
    "_official",
    "official",
    "_channel",
    "channel",
    "_news",
    "_announcements",
    "_announcement",
    "_community",
    "_group",
    "_chat",
    "_support",
    "_helpdesk",
    "_updates",
    "_alerts",
    "bot",
    "_bot",
)


def _is_personal_tg_username(handle: str) -> bool:
    u = str(handle or "").strip().lstrip("@")
    if not u or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", u):
        return False
    low = u.lower()
    return not any(low.endswith(sfx) for sfx in _NON_PERSONAL_TG_SUFFIXES)

GIANT_BRANDS = {
    "facebook",
    "instagram",
    "meta",
    "google",
    "apple",
    "tiktok",
    "youtube",
    "amazon",
    "shopify",
    "netflix",
    "microsoft",
    "openai",
    "anthropic",
}
APP_STORE_HOSTS = {
    "apps.apple.com",
    "itunes.apple.com",
    "play.google.com",
}
_NON_PROJECT_HOSTS = {
    "wa.me",
    "api.whatsapp.com",
    "whatsapp.com",
    "wa.link",
    "fb.com",
    "fb.me",
    "m.me",
    "facebook.com",
    "l.facebook.com",
    "lm.facebook.com",
    "instagram.com",
    "l.instagram.com",
    "t.me",
    "telegram.me",
    "telegram.dog",
    "linktr.ee",
    "beacons.ai",
    "bit.ly",
    "tinyurl.com",
}
_IG_RESERVED_HANDLES = frozenset(
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
        "share",
    }
)
_IG_URL_RE = re.compile(
    r"""(?:https?://)?(?:www\.|m\.)?instagram\.com/([A-Za-z0-9._]{1,30})(?=[/?#"'<>\s,;:\)]|$)""",
    re.I,
)
_IG_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_WA_ME_RE = re.compile(
    r"(?:https?://)?(?:www\.)?wa\.me/(?:c/)?([%\d+][%\d\s().\-]*)",
    re.I,
)
_WA_API_RE = re.compile(
    r"(?:(?:https?://)?(?:www\.)?(?:api\.whatsapp\.com|whatsapp\.com)/send(?:/)?\?|whatsapp://send\?)"
    r"[^#\s\"'<>]*?\bphone=(\+?[%\d][%\d\s().\-]{6,})",
    re.I,
)
_WA_LABEL_E164_RE = re.compile(
    r"(?:whatsapp|wa)\s*(?:/\s*tel[^:\n]{0,18})?\s*[:\-–—]?\s*(\+\d[\d\s().\-]{6,17}\d)",
    re.I,
)
_TEL_PHONE_RE = re.compile(r"\btel:\s*(\+?\d[\d\s().\-]{6,17}\d)", re.I)
_E164_PHONE_RE = re.compile(r"(?<![\w+])(\+\d[\d\s().\-]{6,17}\d)(?!\s*%)")
_BARE_PHONE_RE = re.compile(r"^\s*\+?(\d[\d\s().\-]{7,18}\d)\s*$")
_TOLLFREE_WA_PREFIXES = ("1800", "1833", "1844", "1855", "1866", "1877", "1888", "44800", "44808")


def queries_for_day(
    day: date,
    count: int = 3,
    cursor: int = 0,
    *,
    market_profile: str = "global",
) -> list[str]:
    if str(market_profile or "global").strip().lower() == "gcc_december":
        gcc_pool = list(ARABIC_GCC_SEARCH_QUERIES)
        core_pool = list(SEARCH_QUERIES)
        total_out = max(1, int(count))
        gcc_n = max(1, int(round(total_out * 0.7)))
        core_n = max(0, total_out - gcc_n)
        g_start = ((day.toordinal() + max(0, int(cursor))) * gcc_n) % len(gcc_pool)
        c_start = ((day.toordinal() + max(0, int(cursor))) * max(1, core_n)) % len(core_pool)
        out = [gcc_pool[(g_start + i) % len(gcc_pool)] for i in range(min(gcc_n, len(gcc_pool)))]
        for i in range(min(core_n, len(core_pool))):
            q = core_pool[(c_start + i) % len(core_pool)]
            if q not in out:
                out.append(q)
        return out[:total_out]
    total = len(SEARCH_QUERIES)
    if not total or count <= 0:
        return []
    start = ((day.toordinal() + max(0, int(cursor))) * count) % total
    return [SEARCH_QUERIES[(start + i) % total] for i in range(min(count, total))]


def countries_for_day(
    day: date,
    count: int = 6,
    cursor: int = 0,
    *,
    market_profile: str = "global",
) -> list[str]:
    """Return a deterministic daily slice of TARGET_COUNTRIES for multi-GEO rotation."""
    if str(market_profile or "global").strip().lower() == "gcc_december":
        weighted_pool = (
            "SA", "AE", "QA", "KW", "SA", "AE", "BH", "OM",
            "EG", "JO", "SA", "AE",
        )
        total_out = max(1, int(count))
        start = ((day.toordinal() + max(0, int(cursor))) * total_out) % len(weighted_pool)
        return [weighted_pool[(start + i) % len(weighted_pool)] for i in range(total_out)]
    total = len(TARGET_COUNTRIES)
    if not total or count <= 0:
        return []
    start = ((day.toordinal() + max(0, int(cursor))) * count) % total
    return [TARGET_COUNTRIES[(start + i) % total] for i in range(min(count, total))]


def fresh_seen(seen: dict[str, str], day: date, keep_days: int = SEEN_DAYS) -> dict[str, str]:
    cutoff = day - timedelta(days=keep_days)
    kept: dict[str, str] = {}
    for key, raw in (seen or {}).items():
        try:
            seen_on = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if seen_on >= cutoff:
            kept[str(key)] = seen_on.isoformat()
    return kept


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").casefold()).strip("-")
    return slug[:64] or "advertiser"


def _is_giant_brand(page_name: str, domain: str = "") -> bool:
    folded = re.sub(r"[^a-z0-9]+", " ", (page_name or "").casefold()).strip()
    if not folded:
        return True
    if folded in GIANT_BRANDS:
        return True
    tokens = set(folded.split())
    if tokens & GIANT_BRANDS and len(tokens) <= 2:
        return True
    if domain:
        host_root = domain.lower().split(".")[0]
        if host_root in GIANT_BRANDS:
            return True
    return False


def _unwrap_fb_redirect(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host in {"l.facebook.com", "lm.facebook.com"} and parsed.path.rstrip("/") == "/l.php":
        u_vals = parse_qs(parsed.query).get("u") or []
        if u_vals:
            return unquote(u_vals[0]).strip()
    return text


def _is_non_project_host(host: str) -> bool:
    clean = (host or "").lower().removeprefix("www.").strip(".")
    if not clean:
        return True
    return any(clean == skip or clean.endswith("." + skip) for skip in _NON_PROJECT_HOSTS)


def _snapshot_cards(snapshot: dict) -> list[dict]:
    cards = snapshot.get("cards")
    if not isinstance(cards, list):
        return []
    return [c for c in cards if isinstance(c, dict)]


def _snapshot_body_text(snapshot: dict) -> str:
    body_obj = snapshot.get("body")
    body_text = ""
    if isinstance(body_obj, dict):
        body_text = str(body_obj.get("text") or "").strip()
    elif isinstance(body_obj, str):
        body_text = body_obj.strip()
    if not body_text:
        body_text = str(snapshot.get("title") or snapshot.get("link_description") or "").strip()
    if not body_text:
        for card in _snapshot_cards(snapshot):
            c_body = card.get("body")
            if isinstance(c_body, dict):
                body_text = str(c_body.get("text") or "").strip()
            elif isinstance(c_body, str):
                body_text = c_body.strip()
            if not body_text:
                body_text = str(card.get("title") or card.get("link_description") or "").strip()
            if body_text:
                break
    return re.sub(r"\s+", " ", body_text).strip()


def _extract_project_url_and_domain(snapshot: dict) -> tuple[str, str]:
    cards = _snapshot_cards(snapshot)
    raw_fields: list[Any] = [
        snapshot.get("link_url"),
        snapshot.get("caption"),
        snapshot.get("website"),
    ]
    for card in cards:
        raw_fields.extend((card.get("link_url"), card.get("caption"), card.get("website")))

    for raw in raw_fields:
        text = _unwrap_fb_redirect(str(raw or "").strip())
        if not text:
            continue
        low = text.lower()
        if low.startswith(("tel:", "mailto:", "whatsapp:")):
            continue
        candidates: list[str] = []
        if re.search(r"\s", text):
            candidates.extend(m.group(0).rstrip(".,;:)") for m in re.finditer(r"https?://[^\s<>\]\)\"']+", text))
        else:
            candidates.append(text)
        for cand in candidates:
            url = cand if "://" in cand else f"https://{cand}"
            host = (urlparse(url).hostname or "").lower().removeprefix("www.")
            if host in APP_STORE_HOSTS:
                return url, ""
            if _is_non_project_host(host):
                continue
            dom = domain_of(url)
            if dom and not _is_non_project_host(dom):
                return f"https://{dom}", dom
    return "", ""


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", unquote(str(value or "")))


def _is_plausible_wa_digits(digits: str) -> bool:
    if not (8 <= len(digits) <= 15):
        return False
    if len(set(digits)) == 1 or digits.startswith("1234567") or digits.startswith(_TOLLFREE_WA_PREFIXES):
        return False
    return True


def extract_whatsapp_from_snapshot(snapshot: dict) -> tuple[str, str, str]:
    """Extract Click-to-WhatsApp URL, E.164 phone, and cta_type from an ad snapshot."""
    if not isinstance(snapshot, dict):
        return "", "", ""
    cards = _snapshot_cards(snapshot)
    cta_type = str(snapshot.get("cta_type") or "").strip().upper()
    if not cta_type:
        for card in cards:
            c_type = str(card.get("cta_type") or "").strip().upper()
            if c_type:
                cta_type = c_type
                break
    cta_text = str(snapshot.get("cta_text") or "").strip()
    if not cta_text:
        for card in cards:
            c_txt = str(card.get("cta_text") or "").strip()
            if c_txt:
                cta_text = c_txt
                break

    primary_raw: list[str] = [
        str(snapshot.get("link_url") or ""),
        str(snapshot.get("caption") or ""),
        str(snapshot.get("website") or ""),
    ]
    for card in cards:
        primary_raw.extend((
            str(card.get("link_url") or ""),
            str(card.get("caption") or ""),
            str(card.get("website") or ""),
        ))

    secondary_raw: list[str] = [
        str(snapshot.get("link_description") or ""),
        str(snapshot.get("title") or ""),
        _snapshot_body_text(snapshot),
    ]
    for card in cards:
        c_body = card.get("body")
        c_body_str = str(c_body.get("text") or "") if isinstance(c_body, dict) else str(c_body or "")
        secondary_raw.extend((
            str(card.get("link_description") or ""),
            str(card.get("title") or ""),
            c_body_str,
        ))

    def _expand_variants(values: list[str]) -> list[str]:
        out: list[str] = []
        for raw in values:
            s = raw.strip()
            if not s:
                continue
            out.append(s)
            unwrapped = _unwrap_fb_redirect(s)
            if unwrapped and unwrapped != s:
                out.append(unwrapped)
        return out

    primary = _expand_variants(primary_raw)
    secondary = _expand_variants(secondary_raw)
    all_texts = primary + secondary

    for text in all_texts:
        for regex in (_WA_ME_RE, _WA_API_RE):
            for m in regex.finditer(text):
                digits = _digits(m.group(1))
                if _is_plausible_wa_digits(digits):
                    return f"https://wa.me/{digits}", f"+{digits}", cta_type

    for text in all_texts:
        for m in _WA_LABEL_E164_RE.finditer(text):
            digits = _digits(m.group(1))
            if _is_plausible_wa_digits(digits):
                return f"https://wa.me/{digits}", f"+{digits}", cta_type

    for text in primary:
        for regex in (_TEL_PHONE_RE, _E164_PHONE_RE):
            for m in regex.finditer(text):
                digits = _digits(m.group(1))
                if _is_plausible_wa_digits(digits):
                    return f"https://wa.me/{digits}", f"+{digits}", cta_type

    is_wa_cta = cta_type == "WHATSAPP_MESSAGE" or "whatsapp" in cta_text.casefold()
    if is_wa_cta:
        for text in secondary:
            for regex in (_TEL_PHONE_RE, _E164_PHONE_RE):
                for m in regex.finditer(text):
                    digits = _digits(m.group(1))
                    if _is_plausible_wa_digits(digits):
                        return f"https://wa.me/{digits}", f"+{digits}", cta_type
        for text in primary:
            if "://" in text and not text.lower().startswith("tel:"):
                continue
            m = _BARE_PHONE_RE.match(text)
            if m:
                digits = _digits(m.group(1))
                if _is_plausible_wa_digits(digits):
                    return f"https://wa.me/{digits}", f"+{digits}", cta_type

    return "", "", cta_type


def _normalize_fb_page_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    url = text if "://" in text else f"https://{text}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in {"facebook.com", "m.facebook.com", "mbasic.facebook.com", "fb.com"}:
        return ""
    path_low = (parsed.path or "").lower()
    if any(seg in path_low for seg in ("/ads/library", "/l.php", "/groups/", "/events/", "/sharer")):
        return ""
    clean_path = parsed.path or "/"
    if clean_path == "/profile.php":
        id_vals = parse_qs(parsed.query).get("id") or []
        if id_vals and id_vals[0].strip().isdigit():
            return f"https://www.facebook.com/profile.php?id={id_vals[0].strip()}"
        return ""
    if clean_path == "/":
        return ""
    return f"https://www.facebook.com{clean_path.rstrip('/')}"


def _clean_ig_handle(raw: str) -> str:
    candidate = str(raw or "").strip().lstrip("@")
    if not candidate or not _IG_HANDLE_RE.fullmatch(candidate):
        return ""
    if candidate.casefold() in _IG_RESERVED_HANDLES or not any(c.isalnum() for c in candidate):
        return ""
    return candidate


def extract_social_metadata(item: dict, snapshot: dict) -> dict[str, str]:
    """Extract Facebook Page ID/URL and Instagram handle/URL from an ad item/snapshot."""
    item = item if isinstance(item, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}

    fb_page_id = ""
    for raw_id in (
        snapshot.get("page_id"),
        snapshot.get("pageId"),
        item.get("page_id"),
        item.get("pageId"),
    ):
        val = str(raw_id or "").strip()
        if val:
            fb_page_id = val
            break

    fb_page_url = ""
    fb_url_candidates = [
        snapshot.get("page_profile_uri"),
        snapshot.get("page_profile_url"),
        snapshot.get("page_url"),
        snapshot.get("fb_page_url"),
        item.get("page_profile_uri"),
        item.get("page_profile_url"),
        item.get("page_url"),
        item.get("fb_page_url"),
        snapshot.get("link_url"),
    ]
    for raw_fb in fb_url_candidates:
        norm_fb = _normalize_fb_page_url(str(raw_fb or ""))
        if norm_fb:
            fb_page_url = norm_fb
            break

    if not fb_page_id and fb_page_url:
        m_id = re.search(r"(?:\?id=|facebook\.com/)(\d{5,})(?:$|[/?#])", fb_page_url)
        if m_id:
            fb_page_id = m_id.group(1)

    if not fb_page_url and fb_page_id:
        fb_page_url = f"https://www.facebook.com/{fb_page_id}"

    ig_handle = ""
    for raw_ig_url in (
        snapshot.get("instagram_profile_uri"),
        snapshot.get("instagram_url"),
        snapshot.get("ig_url"),
        item.get("instagram_profile_uri"),
        item.get("instagram_url"),
        item.get("ig_url"),
    ):
        m_ig = _IG_URL_RE.search(str(raw_ig_url or ""))
        if m_ig:
            cand_h = _clean_ig_handle(m_ig.group(1))
            if cand_h:
                ig_handle = cand_h
                break

    if not ig_handle:
        for raw_ig_h in (
            snapshot.get("instagram_handle"),
            snapshot.get("instagram_username"),
            snapshot.get("ig_handle"),
            snapshot.get("ig_username"),
            snapshot.get("instagram_actor_name"),
            item.get("instagram_handle"),
            item.get("instagram_username"),
            item.get("ig_handle"),
            item.get("ig_username"),
            item.get("instagram_actor_name"),
        ):
            cand_h = _clean_ig_handle(str(raw_ig_h or ""))
            if cand_h:
                ig_handle = cand_h
                break

    if not ig_handle:
        fallback_texts = [
            str(snapshot.get("link_url") or ""),
            str(snapshot.get("caption") or ""),
            str(snapshot.get("website") or ""),
            _snapshot_body_text(snapshot),
        ]
        for card in _snapshot_cards(snapshot):
            fallback_texts.extend((str(card.get("link_url") or ""), str(card.get("caption") or "")))
        for raw_txt in fallback_texts:
            unwrapped = _unwrap_fb_redirect(raw_txt)
            m_ig = _IG_URL_RE.search(unwrapped)
            if m_ig:
                cand_h = _clean_ig_handle(m_ig.group(1))
                if cand_h:
                    ig_handle = cand_h
                    break

    ig_url = f"https://instagram.com/{ig_handle}" if ig_handle else ""
    return {
        "fb_page_id": fb_page_id,
        "fb_page_url": fb_page_url,
        "instagram_handle": ig_handle,
        "instagram_url": ig_url,
    }


def _is_video_ad(item: dict) -> bool:
    if item.get("is_active") is False or item.get("isActive") is False:
        return False
    snapshot = item.get("snapshot")
    if not isinstance(snapshot, dict):
        return False
    fmt = str(snapshot.get("display_format") or "").upper()
    videos = snapshot.get("videos")
    if fmt == "VIDEO" or (isinstance(videos, list) and len(videos) > 0):
        return True
    for card in _snapshot_cards(snapshot):
        if card.get("video_hd_url") or card.get("video_sd_url") or card.get("watermarked_video_sd_url"):
            return True
    return False


def parse_ad_candidates(payload: dict | None, *, country_code: str = "") -> list[dict]:
    if not isinstance(payload, dict):
        return []
    output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
    items = (
        output.get("searchResults")
        or output.get("results")
        or payload.get("searchResults")
        or payload.get("results")
        or []
    )
    if not isinstance(items, list):
        return []
    default_cc = str(country_code or "").strip().upper() or "US"
    candidates: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or not _is_video_ad(item):
            continue
        snapshot = item.get("snapshot") or {}
        page_name = str(snapshot.get("page_name") or item.get("page_name") or "").strip()
        if not page_name:
            continue
        body_text = _snapshot_body_text(snapshot)
        project_url, domain = _extract_project_url_and_domain(snapshot)
        if _is_giant_brand(page_name, domain):
            continue
        ad_blob = f"{page_name} {body_text}"
        if is_supply_side(ad_blob) or sniper_reject_reason(ad_blob):
            continue
        wa_url, wa_phone, cta_type = extract_whatsapp_from_snapshot(snapshot)
        social = extract_social_metadata(item, snapshot)
        item_cc = str(
            country_code
            or item.get("country_code")
            or item.get("country")
            or snapshot.get("country_code")
            or default_cc
        ).strip().upper() or "US"
        cta_text = str(snapshot.get("cta_text") or "Video Ad").strip() or "Video Ad"
        if body_text:
            what_they_do = f"Active Meta video ads ({cta_text}): {body_text}"[:200]
        else:
            what_they_do = f"Active Meta video ads ({cta_text})"[:200]
        candidates.append({
            "page_name": page_name,
            "domain": domain,
            "project_url": project_url,
            "what_they_do": what_they_do,
            "whatsapp": wa_url,
            "phone": wa_phone,
            "cta_type": cta_type,
            "fb_page_id": social["fb_page_id"],
            "fb_page_url": social["fb_page_url"],
            "instagram_handle": social["instagram_handle"],
            "instagram_url": social["instagram_url"],
            "country_code": item_cc,
        })
    return candidates


def _default_search(query: str, country: str = "US") -> dict | None:
    cc = str(country or "US").strip().upper() or "US"
    return call(
        ENDPOINT,
        query={
            "query": query,
            "trim": "true",
            "status": "ACTIVE",
            "country": cc,
            "media_type": "VIDEO",
        },
        timeout=60,
    )


def _call_search(search_fn: Callable[..., Any], query: str, country: str) -> dict | None:
    try:
        sig = inspect.signature(search_fn)
    except (TypeError, ValueError):
        return search_fn(query)
    params = list(sig.parameters.values())
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
    has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    if "country" in sig.parameters or has_var_kw:
        return search_fn(query, country=country)
    positional = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2 or has_var_pos:
        return search_fn(query, country)
    return search_fn(query)


def _scrape_site_contacts(project_url: str) -> dict[str, str]:
    if not project_url or not domain_of(project_url):
        return {}
    try:
        import requests
        from tg_to_gsheet import email_of_text, linkedin_in_url, x_profile_url
        from yc_ph_leads import extract_tg_from_html
    except ImportError:
        try:
            import requests
            from leadgen.tg_to_gsheet import email_of_text, linkedin_in_url, x_profile_url
            from leadgen.yc_ph_leads import extract_tg_from_html
        except ImportError:
            try:
                import requests
                from scripts.tg_to_gsheet import email_of_text, linkedin_in_url, x_profile_url
                from scripts.yc_ph_leads import extract_tg_from_html
            except ImportError:
                return {}
    try:
        resp = requests.get(
            project_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC/1.0)"},
            timeout=8,
        )
        if not resp.ok:
            return {}
        html = resp.text or ""
    except Exception:
        return {}
    out: dict[str, str] = {}
    tg = extract_tg_from_html(html)
    if tg:
        out["username"] = tg
    li = linkedin_in_url(html)
    if li:
        out["founder_linkedin"] = li
    em = email_of_text(html)
    if em:
        out["work_email"] = em
    x_skip = {"home", "search", "explore", "i", "share", "intent", "hashtag", "compose"}
    for x_match in re.finditer(
        r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#\"'\s<>]|$)",
        html,
        re.I,
    ):
        handle = x_match.group(1)
        if handle.casefold() in x_skip:
            continue
        x_url = x_profile_url({"founder_x": f"https://x.com/{handle}"})
        if x_url:
            out["founder_x"] = x_url
            break
    return out


def _extract_apple_app_id(url: str) -> str:
    m = re.search(r"/id(\d+)\b", str(url or ""))
    return m.group(1) if m else ""


def _builtin_store_resolver(store_url: str) -> dict:
    import html as html_mod
    from urllib.parse import parse_qs, unquote

    import requests

    raw = str(store_url or "").strip()
    if not raw:
        return {}
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    mega_re = re.compile(
        r"\b(?:openai|chatgpt|google|gemini|meta|facebook|instagram|microsoft|apple|amazon|netflix|bytedance|tiktok|xai|grok|anthropic|claude|supercell)\b",
        re.I,
    )

    if host in {"apps.apple.com", "itunes.apple.com"}:
        app_id = _extract_apple_app_id(raw)
        if not app_id:
            return {}
        try:
            resp = requests.get(f"https://itunes.apple.com/lookup?id={app_id}", timeout=6)
            data = resp.json() if hasattr(resp, "json") else json.loads(getattr(resp, "text", "{}") or "{}")
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list) or not results:
                return {}
            item = results[0] if isinstance(results[0], dict) else {}
            dev = str(item.get("sellerName") or item.get("artistName") or "").strip()
            app_name = str(item.get("trackName") or "").strip()
            website = str(item.get("sellerUrl") or "").strip()
            dom = domain_of(website)
            rc = int(item.get("userRatingCount") or 0)
            return {
                "developer": dev,
                "app_name": app_name,
                "website": website,
                "domain": dom,
                "email": "",
                "rating_count": rc,
                "is_megacorp": bool(rc > 50000 or mega_re.search(f"{dev} {app_name} {dom}")),
            }
        except Exception:
            return {}

    if host == "play.google.com":
        pkg = (parse_qs(parsed.query).get("id") or [""])[0].strip()
        if not pkg:
            return {}
        try:
            resp = requests.get(
                f"https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=US",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=6,
            )
            html_txt = str(getattr(resp, "text", "") or "")
        except Exception:
            return {}
        developer = ""
        app_name = ""
        rating_count = 0
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_txt, re.I | re.S):
            try:
                obj = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            nodes = obj if isinstance(obj, list) else [obj]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("name") and not app_name:
                    app_name = str(node["name"]).strip()
                auth = node.get("author")
                if isinstance(auth, dict) and auth.get("name") and not developer:
                    developer = str(auth["name"]).strip()
                agg = node.get("aggregateRating")
                if isinstance(agg, dict) and agg.get("ratingCount") is not None:
                    try:
                        rating_count = int(str(agg["ratingCount"]).replace(",", "").strip())
                    except ValueError:
                        pass
        website = ""
        dom = ""
        for m_href in re.finditer(r'<a\b[^>]*\bhref=["\'](https?://[^"\']+)["\']', html_txt, re.I):
            href = html_mod.unescape(m_href.group(1))
            if "google.com/url" in href:
                q_vals = parse_qs(urlparse(href).query).get("q") or []
                if q_vals:
                    href = unquote(q_vals[0]).strip()
            cand_dom = domain_of(href)
            if cand_dom and not any(bad in cand_dom for bad in ("play.google.com", "gstatic.com", "policies.google.com", "support.google.com")):
                website = href
                dom = cand_dom
                break
        email = ""
        m_mail = re.search(r'href=["\']mailto:([^"\'?]+)', html_txt, re.I)
        if m_mail:
            email = html_mod.unescape(m_mail.group(1)).strip()
        if not (developer or website or email):
            return {}
        return {
            "developer": developer,
            "app_name": app_name,
            "website": website,
            "domain": dom,
            "email": email,
            "rating_count": rating_count,
            "is_megacorp": bool(rating_count > 50000 or mega_re.search(f"{developer} {app_name} {dom}")),
        }

    return {}


def _default_store_resolver(store_url: str) -> dict:
    try:
        from leadgen.monid_client import resolve_app_store_developer
    except ImportError:
        try:
            from monid_client import resolve_app_store_developer
        except ImportError:
            resolve_app_store_developer = None
    if resolve_app_store_developer is not None:
        return resolve_app_store_developer(store_url)
    return _builtin_store_resolver(store_url)


class _SigtermInterrupt(BaseException):
    """Raised on SIGTERM so inner except-Exception handlers cannot swallow shutdown."""


def _rotate_country_window(
    countries: Sequence[str],
    *,
    cursor: int = 0,
    max_countries: int = 3,
) -> list[str]:
    """Rotate a bounded GEO window using cursor when more than max_countries are supplied."""
    cleaned = [str(c).strip().upper() for c in countries if str(c).strip()]
    if not cleaned:
        return ["US"]
    window = max(1, int(max_countries))
    if len(cleaned) <= window:
        return cleaned
    c_idx = max(0, int(cursor))
    start = (c_idx * window) % len(cleaned)
    return [cleaned[(start + i) % len(cleaned)] for i in range(window)]


def _deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _record_row_seen(seen_map: dict[str, str], row: dict[str, Any], stamp: str) -> None:
    pname = str(row.get("name") or "").strip()
    if pname:
        seen_map[f"name:{pname.casefold()}"] = stamp
    pdom = domain_of(str(row.get("project_url") or ""))
    if pdom:
        seen_map[f"domain:{pdom.casefold()}"] = stamp
    fb_id = str(row.get("fb_page_id") or "").strip()
    if fb_id:
        seen_map[f"fb:{fb_id}"] = stamp


def _extract_tg_handles(
    site_contacts: dict[str, Any],
    dm: dict[str, Any],
) -> tuple[str, str]:
    """Split personal Telegram handle vs public channel/group t.me URL."""
    site_tg = str(
        site_contacts.get("username") or site_contacts.get("telegram_username") or ""
    ).strip().lstrip("@")
    dm_cands = (
        str(dm.get("telegram_username") or "").strip().lstrip("@"),
        str(dm.get("username") or "").strip().lstrip("@"),
    )
    username = ""
    tg_group_url = ""
    if site_tg:
        if _is_personal_tg_username(site_tg):
            username = site_tg
        elif not site_tg.lower().endswith(("bot", "_bot")):
            tg_group_url = f"https://t.me/{site_tg}"
    for dm_tg in dm_cands:
        if not dm_tg:
            continue
        if _is_personal_tg_username(dm_tg):
            if not username:
                username = dm_tg
            break
        if not tg_group_url and not dm_tg.lower().endswith(("bot", "_bot")):
            tg_group_url = f"https://t.me/{dm_tg}"
    return username, tg_group_url


def collect(
    *,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    cap: int = CAP,
    countries: Sequence[str] | None = None,
    cursor: int = 0,
    query_count: int | None = None,
    max_runtime_sec: float | None = None,
    on_row: Callable[[dict], None] | None = None,
    rotate_countries: bool = False,
    max_countries: int | None = None,
    market_profile: str = "global",
    search_fn=None,
    enrich_fn=None,
    store_resolver_fn=None,
) -> tuple[list[dict], dict[str, str]]:
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    caller = _default_search if search_fn is None else search_fn
    enricher = find_company_decision_maker if enrich_fn is None else enrich_fn
    resolve_store = _default_store_resolver if store_resolver_fn is None else store_resolver_fn
    allow_store_bridge = bool(store_resolver_fn is not None or enrich_fn is None)
    deadline = (
        (time.monotonic() + max(0.0, float(max_runtime_sec)))
        if max_runtime_sec is not None
        else None
    )

    rows: list[dict] = []
    local_keys: set[str] = set()

    q_count = int(query_count) if query_count is not None else (3 if countries is not None else 6)
    selected_queries = queries_for_day(today, count=q_count, cursor=cursor, market_profile=market_profile)
    if countries is not None:
        selected_countries = [str(c).strip().upper() for c in countries if str(c).strip()]
        if rotate_countries or (max_countries is not None and int(max_countries) > 0):
            window = int(max_countries) if (max_countries is not None and int(max_countries) > 0) else 3
            selected_countries = _rotate_country_window(
                selected_countries,
                cursor=cursor,
                max_countries=window,
            )
    else:
        selected_countries = countries_for_day(
            today,
            count=len(selected_queries) or 6,
            cursor=cursor,
            market_profile=market_profile,
        )
    if not selected_countries:
        selected_countries = ["US"]

    total_steps = max(len(selected_queries), len(selected_countries)) if selected_queries else 0
    search_plan = [
        (selected_queries[i % len(selected_queries)], selected_countries[i % len(selected_countries)])
        for i in range(total_steps)
    ]

    stopped = False
    try:
        for query, country in search_plan:
            if len(rows) >= cap or _deadline_expired(deadline):
                break
            try:
                payload = _call_search(caller, query, country)
            except Exception:
                payload = None
            for cand in parse_ad_candidates(payload, country_code=country):
                if len(rows) >= cap or _deadline_expired(deadline):
                    stopped = True
                    break
                page_name = cand["page_name"]
                domain = cand["domain"]
                orig_project_url = cand["project_url"]
                orig_host = (
                    urlparse(orig_project_url if "://" in orig_project_url else f"https://{orig_project_url}").hostname or ""
                ).lower().removeprefix("www.")
                store_url = orig_project_url if orig_host in APP_STORE_HOSTS else ""

                name_key = f"name:{page_name.casefold()}"
                dom_key = f"domain:{domain.casefold()}" if domain else ""
                fb_key = f"fb:{cand['fb_page_id']}" if cand["fb_page_id"] else ""
                if name_key in current or name_key in local_keys:
                    continue
                if dom_key and (dom_key in current or dom_key in local_keys):
                    continue
                if fb_key and (fb_key in current or fb_key in local_keys):
                    continue
                if (
                    str(market_profile or "global").strip().lower() == "gcc_december"
                    and cand.get("country_code") in MENA_EXTENDED_COUNTRIES
                    and not store_url
                    and not domain
                ):
                    continue

                store_info: dict = {}
                resolved_domain = domain
                resolved_project_url = orig_project_url
                if store_url and not domain and allow_store_bridge:
                    try:
                        store_info = resolve_store(store_url) or {}
                    except Exception:
                        store_info = {}
                    try:
                        store_rating = int(store_info.get("rating_count") or 0)
                    except (TypeError, ValueError):
                        store_rating = 0
                    if store_info.get("is_megacorp") or store_rating > 50000:
                        continue
                    if store_info.get("website"):
                        resolved_project_url = str(store_info["website"]).strip()
                    resolved_domain = str(store_info.get("domain") or domain_of(resolved_project_url)).strip()
                    if resolved_domain:
                        res_dom_key = f"domain:{resolved_domain.casefold()}"
                        if res_dom_key in current or res_dom_key in local_keys:
                            continue

                if _deadline_expired(deadline):
                    stopped = True
                    break

                local_keys.add(name_key)
                if dom_key:
                    local_keys.add(dom_key)
                if fb_key:
                    local_keys.add(fb_key)
                if resolved_domain:
                    local_keys.add(f"domain:{resolved_domain.casefold()}")

                enrich_target = str(store_info.get("developer") or page_name).strip()
                try:
                    dm = enricher(enrich_target, resolved_domain) or {}
                except Exception:
                    dm = {}

                founder_name = str(dm.get("founder_name") or "").strip()
                founder_linkedin = str(dm.get("founder_linkedin") or "").strip()
                work_email = str(dm.get("work_email") or "").strip()
                if resolved_project_url and resolved_project_url != store_url:
                    project_url = resolved_project_url
                else:
                    project_url = str(dm.get("project_url") or "").strip() or orig_project_url
                resolved_domain = resolved_domain or domain_of(project_url)
                site_contacts = (
                    _scrape_site_contacts(project_url)
                    if (enrich_fn is None or store_resolver_fn is not None) and resolved_domain
                    else {}
                )
                if not founder_name and site_contacts.get("founder_name"):
                    founder_name = str(site_contacts["founder_name"]).strip()
                if not founder_linkedin and site_contacts.get("founder_linkedin"):
                    founder_linkedin = site_contacts["founder_linkedin"]
                if not work_email and site_contacts.get("work_email"):
                    work_email = site_contacts["work_email"]
                if not work_email and store_info.get("email"):
                    work_email = str(store_info["email"]).strip()
                username, tg_group_url = _extract_tg_handles(site_contacts, dm)
                founder_x = str(site_contacts.get("founder_x") or dm.get("founder_x") or "").strip()

                wa_url = cand["whatsapp"]
                wa_phone = cand["phone"]
                fb_page_id = cand["fb_page_id"]
                fb_page_url = cand["fb_page_url"]
                ig_handle = cand["instagram_handle"]
                ig_url = cand["instagram_url"]

                if not (
                    founder_linkedin
                    or work_email
                    or resolved_domain
                    or project_url
                    or wa_url
                    or wa_phone
                    or ig_url
                    or fb_page_url
                    or tg_group_url
                    or username
                    or founder_x
                ):
                    continue

                slug = _slugify(resolved_domain or page_name)
                links = list(
                    dict.fromkeys(
                        u
                        for u in (
                            project_url,
                            store_url,
                            wa_url,
                            f"https://t.me/{username}" if username else "",
                            tg_group_url,
                            founder_linkedin,
                            founder_x,
                            ig_url,
                            fb_page_url,
                        )
                        if u
                    )
                )
                provider = "telegram" if username else ("whatsapp" if wa_url else "")
                row = {
                    "id": f"meta_ads:{slug}",
                    "name": page_name,
                    "founder_name": founder_name,
                    "username": username,
                    "telegram_username": username,
                    "telegram_group": tg_group_url,
                    "whatsapp": wa_url,
                    "phone": wa_phone,
                    "provider": provider,
                    "cta_type": cand["cta_type"],
                    "bio": cand["what_they_do"],
                    "links": links,
                    "source": "meta_ads",
                    "segment": "whale",
                    "role": "Marketing",
                    "roles": ["Marketing"],
                    "category": "SaaS / AI app",
                    "project": page_name,
                    "project_url": project_url,
                    "what_they_do": cand["what_they_do"],
                    "founder_linkedin": founder_linkedin,
                    "founder_x": founder_x,
                    "work_email": work_email,
                    "fb_page_id": fb_page_id,
                    "fb_page_url": fb_page_url,
                    "instagram_handle": ig_handle,
                    "instagram_url": ig_url,
                    "country_code": cand["country_code"],
                    "has_monetization_signal": True,
                    "monetization": "subscription",
                    "icp_match": True,
                    "lead_tier": "A",
                }
                rows.append(row)
                current[name_key] = stamp
                if dom_key:
                    current[dom_key] = stamp
                elif resolved_domain:
                    current[f"domain:{resolved_domain.casefold()}"] = stamp
                if fb_key:
                    current[fb_key] = stamp
                if on_row is not None:
                    on_row(row)
            if stopped:
                break
    except _SigtermInterrupt:
        stopped = True

    return rows, current


def _load_seen(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(raw, dict):
        inner = raw.get("advertisers")
        if isinstance(inner, dict):
            return {str(k): str(v) for k, v in inner.items()}
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    return {}


def _load_cursor(path: Path) -> int:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return max(0, int(raw.get("cursor") or 0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return 0


def _save_seen(path: Path, seen: dict[str, str], *, cursor: int | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"advertisers": seen}
        if cursor is not None:
            payload["cursor"] = max(0, int(cursor))
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--seen", default=".tg_pipeline/meta_ads_seen.json")
    parser.add_argument("--limit", type=int, default=CAP)
    parser.add_argument("--day", default="", help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--country", default="", help="Comma-separated 2-letter GEO codes (optional)")
    parser.add_argument("--max-runtime-sec", type=float, default=210.0)
    parser.add_argument("--max-countries", type=int, default=3)
    parser.add_argument("--market-profile", default="global", choices=["global", "gcc_december"])
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.day) if args.day else date.today()
    countries = (
        [c.strip().upper() for c in args.country.split(",") if c.strip()]
        if args.country.strip()
        else None
    )
    seen_path = Path(args.seen)
    before = _load_seen(seen_path)
    cursor = _load_cursor(seen_path)
    updated = fresh_seen(before, day)
    stamp = day.isoformat()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    admitted_count = 0
    shutting_down = False

    def _handle_sigterm(_signum: int, _frame: Any) -> None:
        if not shutting_down:
            raise _SigtermInterrupt("meta_ad_leads terminated by SIGTERM")

    prev_sigterm = None
    try:
        prev_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError):
        prev_sigterm = None

    max_rt = float(args.max_runtime_sec)
    effective_runtime = max_rt if max_rt > 0 else None

    try:
        with open(out_path, "w", encoding="utf-8") as handle:
            def _emit_row(row: dict[str, Any]) -> None:
                nonlocal admitted_count
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                admitted_count += 1
                _record_row_seen(updated, row, stamp)

            try:
                _rows, returned_updated = collect(
                    day=day,
                    seen=before,
                    cap=max(0, args.limit),
                    countries=countries,
                    cursor=cursor,
                    query_count=12,
                    max_runtime_sec=effective_runtime,
                    on_row=_emit_row,
                    rotate_countries=True,
                    max_countries=max(1, int(args.max_countries)),
                    market_profile=args.market_profile,
                )
                updated = returned_updated
            except _SigtermInterrupt:
                print("meta_ads: sigterm_graceful_stop", file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"meta_ads: collect_error={type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        shutting_down = True
        if prev_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, OSError):
                pass
        _save_seen(seen_path, updated, cursor=cursor + 1)
    print(f"meta_ads: admitted={admitted_count}", flush=True)


if __name__ == "__main__":
    main()
