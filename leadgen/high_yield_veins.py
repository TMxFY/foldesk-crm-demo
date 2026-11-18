#!/usr/bin/env python3
"""High-yield SoftUGC buyer veins via Monid & Treg CLI.

Covers three proven paying buyer streams (100% bounded, 90-day deduplicated,
hard API call budget cap <= 25 calls per run):
1. Whop Content Rewards (brands already paying $0.75-$3/1k views to clippers).
2. TikTok Creative Center active US/EU Conversion Video Ads.
3. TikTok Shop US D2C brands actively running affiliate product sales.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import html as html_mod
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
from typing import Any
from urllib.parse import urlparse
import urllib.request

try:
    from monid_client import check_monid_ready, is_megacorp_app, resolve_app_store_developer, run_monid
    from treg_client import call as treg_call, check_treg_ready
    from treg_enrich import domain_of, find_company_decision_maker
    from meta_ad_leads import APP_STORE_HOSTS, _scrape_site_contacts
    from supply_side import is_supply_side, sniper_reject_reason
    from tg_to_gsheet import email_of_text, linkedin_in_url, whatsapp_url, whatsapp_urls, x_profile_url
except ImportError:
    from leadgen.monid_client import check_monid_ready, is_megacorp_app, resolve_app_store_developer, run_monid
    from leadgen.treg_client import call as treg_call, check_treg_ready
    from leadgen.treg_enrich import domain_of, find_company_decision_maker
    from leadgen.meta_ad_leads import APP_STORE_HOSTS, _scrape_site_contacts
    from leadgen.supply_side import is_supply_side, sniper_reject_reason
    from leadgen.tg_to_gsheet import email_of_text, linkedin_in_url, whatsapp_url, whatsapp_urls, x_profile_url


SEEN_DAYS = 90
MAX_API_CALLS_PER_RUN = 25

WHOP_QUERIES = (
    'site:whop.com/discover ("per 1,000 views" OR "content rewards") ("Budget" OR "CPM") -blog -docs -terms',
    'site:apps.whop.com/discover ("per 1,000 views" OR "CPM") -terms',
)

TIKTOK_AD_QUERIES = (
    "AI app",
    "astrology app",
    "calorie tracker",
    "prop trading",
    "roblox game",
    "supplement gummies",
    "skincare serum",
)

TIKTOK_SHOP_QUERIES = (
    "sleep gummies",
    "collagen serum",
    "mushroom supplement",
    "stain remover",
    "whitening strips",
)

_WHOP_PREFIX_RE = re.compile(
    r"^(?:earn\s+\$[\d.,]+\s+per\s+1[,.]?000\s+views(?:\s+clipping)?\s+|"
    r"clipping\s+|join\s+|content\s+rewards\s*[:|\-–—]\s*)+",
    re.I,
)
_WHOP_SUFFIX_RE = re.compile(
    r"\s*(?:\|\s*\$[\d.,]+[kKmM]?\s*budget.*|\|\s*content\s+rewards.*|"
    r"\bby\s+[A-Z0-9][A-Za-z0-9\s.&'-]{1,40}$|\s*[-–—|]\s*whop\b.*)$",
    re.I,
)
_WHOP_NOISE_PATHS = {"/blog", "/docs", "/creators", "/articles", "/about", "/discover", "/brands-terms", "/terms", "/privacy"}
_NOISE_BRAND_NAMES = frozenset({
    "not mention",
    "not mentioned",
    "sponsored",
    "advertisement",
    "ad",
    "ads",
    "unknown",
    "none",
    "n/a",
    "brand",
    "official",
    "tiktok",
    "whop",
    "content rewards",
    "content campaigns",
    "slideshows",
})
_NOISE_BRAND_SUBSTRINGS = (
    "terms of service",
    "privacy policy",
    "content campaigns",
    "content rewards",
    "slideshows",
    "clipper hub",
    "clipping hub",
    "tutorial",
    "platform fee",
)
_NOISE_BRAND_HOWTO_RE = re.compile(r"\bhow[- ]to\b", re.I)
_NOISE_COMPOUND_WORDS = frozenset({
    "not", "mention", "mentioned", "sponsored", "advertisement", "ad", "ads",
    "unknown", "none", "n/a", "na", "brand", "official", "tiktok", "whop",
    "shop", "store", "creator", "creators", "campaigns", "campaign", "rewards",
    "clipper", "clipping", "template", "slideshows", "english",
})
_BRAND_TOKEN_STOPWORDS = frozenset({
    "app", "apps", "inc", "llc", "ltd", "corp", "co", "the", "and", "for", "of",
    "studio", "labs", "media", "shop", "store", "official", "fame", "pro",
    "get", "my", "try", "use", "hey", "join", "www", "web", "tiktok", "whop",
    "unknown", "not", "ugc",
})
_MARKETPLACE_NOISE_DOMAINS = frozenset({
    "amazon.com", "amazon.co.uk", "amazon.de", "walmart.com", "ebay.com", "ebay.co.uk",
    "etsy.com", "target.com", "aliexpress.com", "temu.com", "shop.app",
    "crunchbase.com", "zoominfo.com", "pitchbook.com", "apollo.io", "rocketreach.co",
    "sensortower.com", "kalodata.com", "fastmoss.com", "pipiads.com",
    "apkpure.com", "uptodown.com", "softonic.com", "appbrain.com", "sites.google.com",
    "playstation.com", "xbox.com", "nintendo.com",
    "yelp.com", "trustpilot.com", "glassdoor.com", "indeed.com", "wikipedia.org",
    "reddit.com", "quora.com", "medium.com", "substack.com", "pinterest.com",
    "facebook.com", "instagram.com", "youtube.com", "tiktok.com", "x.com", "twitter.com",
    "linkedin.com", "whop.com", "t.me", "telegram.me",
})
_WHOP_NON_BRAND_TITLES = {
    "content rewards",
    "content campaigns",
    "organization terms of service",
    "which social media platforms pay creators",
    "youtube shorts monetization",
    "what is clipping",
    "for creators",
    "articles",
    "about",
    "discover",
    "home",
}
_WHOP_CLIPPING_WORD_RE = re.compile(r"\s+\bclipping\b.*$", re.I)
_SHOP_LEADING_NOISE = {
    "pack",
    "new",
    "100%",
    "2pcs",
    "3pcs",
    "4pcs",
    "organic",
    "natural",
    "pure",
    "best",
    "hot",
    "sale",
    "sugar",
    "vegan",
    "adults",
    "women",
    "men",
    "sleep",
    "deep",
    "extra",
    "strong",
    "max",
    "advanced",
    "premium",
    "official",
    "set",
    "combo",
    "bundle",
    "mini",
    "pro",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
}
_X_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})\b", re.I)
_TG_EXPLICIT_RE = re.compile(
    r"(?:https?://(?:www\.)?(?:t\.me|telegram\.me)/|\b(?:tg|telegram)\s*[:@\-]\s*@?)([A-Za-z][A-Za-z0-9_]{3,31})\b",
    re.I,
)
_TG_NOISE = {"joinchat", "share", "addstickers", "proxy", "iv", "s", "c", "boost", "contentrewardsbot"}
_X_NOISE = {"home", "search", "explore", "i", "share", "intent", "hashtag", "compose", "whop", "contentrewards"}


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
    return slug[:64] or "lead"


def _rotate_for_day(items: tuple[str, ...], day: date, count: int) -> list[str]:
    total = len(items)
    if not total or count <= 0:
        return []
    start = (day.toordinal() * count) % total
    return [items[(start + i) % total] for i in range(min(count, total))]


def _unwrap_search_results(payload: Any) -> list[dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "items", "organic_results"):
        val = payload.get(key)
        if isinstance(val, list) and val:
            return [r for r in val if isinstance(r, dict)]
    for subkey in ("output", "result", "data"):
        sub = payload.get(subkey)
        if isinstance(sub, list) and sub:
            return [r for r in sub if isinstance(r, dict)]
        if isinstance(sub, dict):
            nested = _unwrap_search_results(sub)
            if nested:
                return nested
    return []


def _default_web_search(query: str) -> dict[str, Any] | None:
    if not check_treg_ready():
        return None
    return treg_call("crawl4ai.web.search", query={"q": query}, timeout=35)


def _is_noise_brand(brand: str) -> bool:
    low = re.sub(r"\s+", " ", str(brand or "").strip().casefold())
    low_clean = re.sub(r"\s*\[[^\]]*\]\s*$", "", low).strip()
    if not low_clean or len(low_clean) < 2:
        return True
    if low in _NOISE_BRAND_NAMES or low_clean in _NOISE_BRAND_NAMES:
        return True
    if _NOISE_BRAND_HOWTO_RE.search(low_clean):
        return True
    if any(sub in low for sub in _NOISE_BRAND_SUBSTRINGS):
        return True
    words = re.findall(r"[a-z0-9/]+", low_clean)
    return bool(words) and all(w in _NOISE_COMPOUND_WORDS for w in words)


def _split_primary_brand(brand: str) -> str:
    return re.split(r"\s*[–—:|]\s*|\s+-\s+", str(brand or "").strip())[0].strip()


def _brand_distinctive_tokens(brand: str) -> list[str]:
    primary = _split_primary_brand(brand)
    tokens: list[str] = []
    for raw in (primary, brand):
        for w in re.findall(r"[A-Za-z0-9]{3,}", str(raw or "").lower()):
            if w not in _BRAND_TOKEN_STOPWORDS and w not in tokens:
                tokens.append(w)
    return tokens


def _discover_brand_landing(
    brand: str,
    *,
    search_fn,
    store_resolver_fn,
    budget: list[int],
) -> tuple[str, str, dict[str, Any], list[str]]:
    """Discover an official domain or App Store / Google Play listing for a brand."""
    if search_fn is None or budget[0] <= 0 or _is_noise_brand(brand):
        return "", "", {}, []
    tokens = _brand_distinctive_tokens(brand)
    if not tokens:
        return "", "", {}, []
    primary = _split_primary_brand(brand) or brand
    query = f'"{primary}" (official website OR apps.apple.com OR play.google.com)'
    budget[0] -= 1
    try:
        payload = search_fn(query)
    except Exception:
        return "", "", {}, []

    store_candidates: list[str] = []
    dom_site_candidates: list[tuple[str, str]] = []
    for hit in _unwrap_search_results(payload):
        url = str(hit.get("url") or "").strip()
        if not url or not url.lower().startswith(("http://", "https://")):
            continue
        title = str(hit.get("title") or "").strip().lower()
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if host in APP_STORE_HOSTS:
            if any(tok in url.lower() or tok in title for tok in tokens):
                store_candidates.append(url)
            continue
        dom = domain_of(url)
        if not dom:
            continue
        if dom in _MARKETPLACE_NOISE_DOMAINS or any(
            dom.endswith("." + bad) for bad in _MARKETPLACE_NOISE_DOMAINS
        ):
            continue
        if any(tok in dom.lower() for tok in tokens):
            dom_site_candidates.append((url, dom))

    if dom_site_candidates:
        best_url, best_dom = dom_site_candidates[0]
        return best_url, best_dom, {}, []

    for s_url in store_candidates[:1]:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            st = store_resolver_fn(s_url) or {}
        except Exception:
            st = {}
        if st.get("is_megacorp") or int(st.get("rating_count") or 0) > 50000:
            return "", "", {"is_megacorp": True, "rating_count": st.get("rating_count", 0)}, [s_url]
        w_url = str(st.get("website") or "").strip()
        w_dom = str(st.get("domain") or domain_of(w_url)).strip()
        return w_url, w_dom, st, [s_url]

    return "", "", {}, []


def _clean_whop_brand(raw_title: str) -> str:
    t = html_mod.unescape(" ".join(str(raw_title or "").split())).strip()
    t = re.sub(r"\s*[-–—|:]\s*(?:Whop|Content Rewards).*$", "", t, flags=re.I).strip()
    t = _WHOP_PREFIX_RE.sub("", t).strip()
    t = _WHOP_SUFFIX_RE.sub("", t).strip()
    t = _WHOP_CLIPPING_WORD_RE.sub("", t).strip()
    t = t.strip(" -–—|:•")
    if len(t) < 2 or t.casefold() in _WHOP_NON_BRAND_TITLES or _is_noise_brand(t):
        return ""
    if any(tok in t.casefold() for tok in ("how to", "guide", "articles", "blog", "clipper hub")):
        return ""
    return t[:80]


def _extract_whop_budget_signal(blob: str, snippet: str = "") -> str:
    text = " ".join(str(blob or "").split())
    budget_m = re.search(r"budget\s*[:=]?\s*(\$[\d,]+(?:\.\d+)?[kKmM]?)", text, re.I)
    if not budget_m:
        budget_m = re.search(r"(\$[\d,]+(?:\.\d+)?[kKmM]?)\s*(?:budget|allocated)", text, re.I)
    cpm_m = re.search(
        r"(\$[\d,]+(?:\.\d+)?)\s*(?:per\s+1[,.]?000\s+views|/\s*1k|cpm)",
        text,
        re.I,
    )
    if not cpm_m:
        cpm_m = re.search(r"cpm\s*[:=]?\s*(\$[\d,]+(?:\.\d+)?)", text, re.I)
    parts: list[str] = []
    if budget_m:
        parts.append(f"{budget_m.group(1)} budget")
    if cpm_m:
        parts.append(f"{cpm_m.group(1)} CPM")
    desc = " ".join(str(snippet or "").split())
    if parts:
        head = f"Active Whop Content Rewards campaign ({', '.join(parts)})"
        return f"{head} — {desc}"[:200] if desc else head[:200]
    return f"Active Whop Content Rewards campaign: {(desc or text)[:130]}".strip()


def _extract_inline_contacts(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for m_x in _X_RE.finditer(text or ""):
        h = m_x.group(1)
        if h.casefold() not in _X_NOISE:
            found["founder_x"] = f"https://x.com/{h}"
            break
    for m_tg in _TG_EXPLICIT_RE.finditer(text or ""):
        u = m_tg.group(1)
        if u.casefold() not in _TG_NOISE and not u.lower().endswith("bot"):
            found["username"] = u
            break
    return found


def _is_whop_doc_or_feed_url(url: str) -> bool:
    parsed = urlparse(url or "")
    path = (parsed.path or "/").rstrip("/").lower() or "/"
    host = (parsed.hostname or "").lower()
    if (
        host.startswith("docs.")
        or any(
            tok in path
            for tok in (
                "/blog/",
                "/articles",
                "/terms",
                "-terms",
                "/privacy",
                "/legal",
                "/pricing",
                "/login",
                "/signup",
                "/support",
                "/help",
                "/content-campaigns",
            )
        )
        or path in _WHOP_NOISE_PATHS
    ):
        return True
    return False


def _merge_enriched_contacts(
    *,
    brand: str,
    domain: str,
    project_url: str,
    extra_text: str,
    enrich_fn,
    site_scrape_fn,
    budget: list[int],
) -> dict[str, str]:
    inline = _extract_inline_contacts(extra_text)
    dm: dict[str, Any] = {}
    if budget[0] > 0 and (brand or domain):
        budget[0] -= 1
        try:
            dm = enrich_fn(brand, domain) or {}
        except Exception:
            dm = {}

    resolved_url = project_url or str(dm.get("project_url") or "").strip()
    resolved_domain = domain or domain_of(resolved_url)
    site_contacts: dict[str, str] = {}
    if resolved_domain and resolved_url:
        try:
            site_contacts = site_scrape_fn(resolved_url) or {}
        except Exception:
            site_contacts = {}

    founder_linkedin = str(dm.get("founder_linkedin") or site_contacts.get("founder_linkedin") or "").strip()
    work_email = str(dm.get("work_email") or site_contacts.get("work_email") or "").strip()
    username = str(site_contacts.get("username") or inline.get("username") or "").strip().lstrip("@")
    founder_x = str(site_contacts.get("founder_x") or inline.get("founder_x") or "").strip()
    if founder_x:
        founder_x = x_profile_url({"founder_x": founder_x}) or founder_x
    return {
        "project_url": resolved_url,
        "domain": resolved_domain,
        "founder_linkedin": founder_linkedin,
        "work_email": work_email,
        "username": username,
        "founder_x": founder_x,
    }


def collect_whop_content_rewards(
    *,
    limit: int = 6,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    search_fn=None,
    store_resolver_fn=None,
    enrich_fn=None,
    site_scrape_fn=None,
    call_budget: list[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    search = _default_web_search if search_fn is None else search_fn
    resolve_store = resolve_app_store_developer if store_resolver_fn is None else store_resolver_fn
    enricher = find_company_decision_maker if enrich_fn is None else enrich_fn
    scraper = _scrape_site_contacts if site_scrape_fn is None else site_scrape_fn
    budget = call_budget if call_budget is not None else [MAX_API_CALLS_PER_RUN]

    rows: list[dict[str, Any]] = []
    local_keys: set[str] = set()

    for query in WHOP_QUERIES:
        if len(rows) >= limit or budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            payload = search(query)
        except Exception:
            payload = None
        for hit in _unwrap_search_results(payload):
            if len(rows) >= limit:
                break
            url = str(hit.get("url") or "").strip()
            title = str(hit.get("title") or "").strip()
            snippet = str(hit.get("snippet") or hit.get("description") or "").strip()
            if not url or _is_whop_doc_or_feed_url(url):
                continue
            brand = _clean_whop_brand(title)
            if not brand:
                continue
            blob = f"{title} {brand} {snippet}"
            if is_megacorp_app(brand, title) or is_supply_side(blob) or sniper_reject_reason(blob):
                continue

            seen_key = f"whop:{_slugify(brand)}"
            if seen_key in current or seen_key in local_keys:
                continue
            local_keys.add(seen_key)

            land_url, land_dom, land_store, land_extra = _discover_brand_landing(
                brand,
                search_fn=search,
                store_resolver_fn=resolve_store,
                budget=budget,
            )
            if (
                land_store.get("is_megacorp")
                or is_megacorp_app(brand, title, land_dom, int(land_store.get("rating_count") or 0))
            ):
                continue

            contacts = _merge_enriched_contacts(
                brand=str(land_store.get("developer") or brand).strip(),
                domain=land_dom,
                project_url=land_url,
                extra_text=f"{snippet} {url}",
                enrich_fn=enricher,
                site_scrape_fn=scraper,
                budget=budget,
            )
            if not contacts["work_email"] and land_store.get("email"):
                contacts["work_email"] = str(land_store["email"]).strip()
            if not any(
                contacts[k]
                for k in ("domain", "work_email", "founder_x", "username")
            ):
                continue

            what_they_do = _extract_whop_budget_signal(f"{title} {snippet}", snippet=snippet)[:200]
            links = [
                u
                for u in (
                    contacts["project_url"],
                    url,
                    *land_extra,
                    contacts["founder_x"],
                    contacts["founder_linkedin"],
                    f"https://t.me/{contacts['username']}" if contacts["username"] else "",
                )
                if u
            ]
            row = {
                "id": seen_key,
                "name": brand,
                "username": contacts["username"],
                "telegram_username": contacts["username"],
                "bio": what_they_do,
                "links": list(dict.fromkeys(links)),
                "source": "whop_content_rewards",
                "source_url": url,
                "role": "Founder",
                "roles": ["Founder"],
                "category": "SaaS / AI app",
                "project": brand,
                "company": brand,
                "project_url": contacts["project_url"],
                "what_they_do": what_they_do,
                "founder_linkedin": contacts["founder_linkedin"],
                "founder_x": contacts["founder_x"],
                "work_email": contacts["work_email"],
                "has_monetization_signal": True,
                "icp_match": True,
                "lead_tier": "A",
            }
            rows.append(row)
            current[seen_key] = stamp

    return rows, current


def _default_tiktok_ads_search(keyword: str) -> dict[str, Any] | None:
    if check_monid_ready():
        res = run_monid(
            "tikhub",
            "/api/v1/tiktok/ads/search_ads",
            body={
                "keyword": keyword,
                "country_code": "US",
                "period": 30,
                "limit": 15,
            },
            wait_sec=25,
            timeout=35,
        )
        if res:
            return res
    if check_treg_ready():
        res = treg_call(
            "tikhub.x.tiktok-ads-search-ads",
            method="POST",
            data={"keyword": keyword, "country_code": "US", "period": 30, "limit": 15},
            timeout=40,
        )
        if res:
            return res
        return treg_call(
            "scrapecreators.x.v1-tiktok-ad-library-search",
            query={"query": keyword},
            timeout=40,
        )
    return None


def _extract_tiktok_ad_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("materials", "ads", "results", "list", "items"):
        val = payload.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    for sub in ("data", "output", "result"):
        child = payload.get(sub)
        if isinstance(child, (dict, list)):
            found = _extract_tiktok_ad_items(child)
            if found:
                return found
    return []


def collect_tiktok_creative_ads(
    *,
    limit: int = 6,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    ads_search_fn=None,
    store_resolver_fn=None,
    web_search_fn=None,
    enrich_fn=None,
    site_scrape_fn=None,
    call_budget: list[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    search_ads = _default_tiktok_ads_search if ads_search_fn is None else ads_search_fn
    resolve_store = resolve_app_store_developer if store_resolver_fn is None else store_resolver_fn
    web_search = web_search_fn if web_search_fn is not None else (_default_web_search if ads_search_fn is None else None)
    enricher = find_company_decision_maker if enrich_fn is None else enrich_fn
    scraper = _scrape_site_contacts if site_scrape_fn is None else site_scrape_fn
    budget = call_budget if call_budget is not None else [MAX_API_CALLS_PER_RUN]

    rows: list[dict[str, Any]] = []
    local_keys: set[str] = set()

    for keyword in _rotate_for_day(TIKTOK_AD_QUERIES, today, 3):
        if len(rows) >= limit or budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            payload = search_ads(keyword)
        except Exception:
            payload = None
        for item in _extract_tiktok_ad_items(payload):
            if len(rows) >= limit:
                break
            ads_info = item.get("ads_info") if isinstance(item.get("ads_info"), dict) else {}
            brand_name = str(
                item.get("brand_name")
                or item.get("advertiser_name")
                or ads_info.get("advertiser_name")
                or ads_info.get("brand_name")
                or item.get("name")
                or ads_info.get("name")
                or item.get("company")
                or item.get("page_name")
                or ""
            ).strip()
            ad_title = str(
                item.get("ad_title")
                or item.get("title")
                or item.get("desc")
                or item.get("caption")
                or ads_info.get("ad_title")
                or ""
            ).strip()
            if not brand_name or _is_noise_brand(brand_name):
                continue
            blob = f"{brand_name} {ad_title}"
            if is_megacorp_app(brand_name, ad_title) or is_supply_side(blob) or sniper_reject_reason(blob):
                continue

            landing_raw = str(
                item.get("landing_page")
                or ads_info.get("landing_page")
                or item.get("landing_url")
                or item.get("url")
                or item.get("app_url")
                or ""
            ).strip()
            store_info: dict[str, Any] = {}
            resolved_url = landing_raw
            resolved_domain = domain_of(landing_raw)
            extra_links: list[str] = []

            if landing_raw:
                low_scheme = landing_raw.lower()
                is_http = low_scheme.startswith(("http://", "https://")) or (
                    "://" not in landing_raw and ":" not in landing_raw.split("/", 1)[0]
                )
                host = (urlparse(landing_raw if "://" in landing_raw else f"https://{landing_raw}").hostname or "").lower().removeprefix("www.")
                if host in APP_STORE_HOSTS:
                    extra_links.append(landing_raw)
                    if budget[0] > 0:
                        budget[0] -= 1
                        try:
                            store_info = resolve_store(landing_raw) or {}
                        except Exception:
                            store_info = {}
                    if store_info.get("is_megacorp") or int(store_info.get("rating_count") or 0) > 50000:
                        continue
                    resolved_url = str(store_info.get("website") or "").strip()
                    resolved_domain = str(store_info.get("domain") or domain_of(resolved_url)).strip()
                elif not resolved_domain or not is_http:
                    if low_scheme.startswith(("http://", "https://")):
                        extra_links.append(landing_raw)
                    resolved_url = ""

            if not resolved_domain and web_search is not None:
                disc_url, disc_dom, disc_store, disc_extra = _discover_brand_landing(
                    brand_name,
                    search_fn=web_search,
                    store_resolver_fn=resolve_store,
                    budget=budget,
                )
                if disc_store.get("is_megacorp") or int(disc_store.get("rating_count") or 0) > 50000:
                    continue
                if disc_dom:
                    resolved_url = disc_url
                    resolved_domain = disc_dom
                if disc_store and not store_info:
                    store_info = disc_store
                extra_links.extend(disc_extra)

            seen_key = (
                f"ttad_dom:{resolved_domain.casefold()}"
                if resolved_domain
                else f"ttad_brand:{_slugify(brand_name)}"
            )
            if seen_key in current or seen_key in local_keys:
                continue
            local_keys.add(seen_key)

            enrich_target = str(store_info.get("developer") or brand_name).strip()
            contacts = _merge_enriched_contacts(
                brand=enrich_target,
                domain=resolved_domain,
                project_url=resolved_url,
                extra_text=ad_title,
                enrich_fn=enricher,
                site_scrape_fn=scraper,
                budget=budget,
            )
            if not contacts["work_email"] and store_info.get("email"):
                contacts["work_email"] = str(store_info["email"]).strip()
            final_url = contacts["project_url"] or resolved_url
            final_domain = contacts["domain"] or resolved_domain
            if not (final_domain or contacts["work_email"] or contacts["founder_x"] or contacts["username"]):
                continue

            what_they_do = f"Active TikTok US ad ({keyword}): {ad_title or brand_name}"[:200]
            links = [
                u
                for u in (
                    final_url,
                    *extra_links,
                    contacts["founder_x"],
                    contacts["founder_linkedin"],
                    f"https://t.me/{contacts['username']}" if contacts["username"] else "",
                )
                if u
            ]
            row = {
                "id": f"tiktok_ads:{_slugify(final_domain or brand_name)}",
                "name": brand_name,
                "username": contacts["username"],
                "telegram_username": contacts["username"],
                "bio": what_they_do,
                "links": list(dict.fromkeys(links)),
                "source": "tiktok_ads",
                "role": "Marketing",
                "roles": ["Marketing"],
                "category": "SaaS / AI app",
                "project": brand_name,
                "company": brand_name,
                "project_url": final_url,
                "what_they_do": what_they_do,
                "founder_linkedin": contacts["founder_linkedin"],
                "founder_x": contacts["founder_x"],
                "work_email": contacts["work_email"],
                "has_monetization_signal": True,
                "icp_match": True,
                "lead_tier": "A",
            }
            rows.append(row)
            current[seen_key] = stamp

    return rows, current


def _default_tiktok_shop_search(search_word: str) -> dict[str, Any] | None:
    if check_monid_ready():
        res = run_monid(
            "tikhub",
            "/api/v1/tiktok/shop/web/fetch_search_products_list",
            query={"search_word": search_word, "page": "1"},
            wait_sec=25,
            timeout=35,
        )
        if res:
            return res
    if check_treg_ready():
        return treg_call(
            "scrapecreators.x.v1-tiktok-shop-search",
            query={"query": search_word, "region": "US"},
            timeout=40,
        )
    return None


def _extract_shop_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("products", "items", "results", "product_list"):
        val = payload.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    for sub in ("data", "output", "result"):
        child = payload.get(sub)
        if isinstance(child, (dict, list)):
            found = _extract_shop_items(child)
            if found:
                return found
    return []


def _extract_shop_brand(item: dict[str, Any]) -> str:
    seller_info = item.get("seller_info") if isinstance(item.get("seller_info"), dict) else {}
    shop_info = item.get("shop") if isinstance(item.get("shop"), dict) else {}
    explicit = str(
        item.get("seller_name")
        or item.get("shop_name")
        or item.get("brand_name")
        or seller_info.get("shop_name")
        or seller_info.get("name")
        or shop_info.get("shop_name")
        or shop_info.get("name")
        or ""
    ).strip()
    if explicit and explicit.casefold() not in _SHOP_LEADING_NOISE and len(explicit) >= 2:
        return explicit[:60]

    title = " ".join(str(item.get("title") or item.get("product_name") or "").split())
    words = [re.sub(r"[^A-Za-z0-9&-]", "", w) for w in title.split()[:4]]
    words = [w for w in words if w]
    if not words:
        return ""
    lead = words[0]
    if lead.casefold() in _SHOP_LEADING_NOISE or lead.isdigit() or len(lead) < 3:
        return ""
    if lead.isupper() or (lead[:1].isupper() and any(ch.isupper() for ch in lead[1:])):
        return lead
    if len(words) >= 2 and words[1].casefold() not in _SHOP_LEADING_NOISE and words[1][:1].isupper():
        return f"{lead} {words[1]}"
    return lead if lead[:1].isupper() else ""


def collect_tiktok_shop_brands(
    *,
    limit: int = 5,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    shop_search_fn=None,
    web_search_fn=None,
    store_resolver_fn=None,
    enrich_fn=None,
    site_scrape_fn=None,
    call_budget: list[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    search_shop = _default_tiktok_shop_search if shop_search_fn is None else shop_search_fn
    web_search = web_search_fn if web_search_fn is not None else (_default_web_search if shop_search_fn is None else None)
    resolve_store = resolve_app_store_developer if store_resolver_fn is None else store_resolver_fn
    enricher = find_company_decision_maker if enrich_fn is None else enrich_fn
    scraper = _scrape_site_contacts if site_scrape_fn is None else site_scrape_fn
    budget = call_budget if call_budget is not None else [MAX_API_CALLS_PER_RUN]

    rows: list[dict[str, Any]] = []
    local_keys: set[str] = set()

    for keyword in _rotate_for_day(TIKTOK_SHOP_QUERIES, today, 2):
        if len(rows) >= limit or budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            payload = search_shop(keyword)
        except Exception:
            payload = None
        for item in _extract_shop_items(payload):
            if len(rows) >= limit:
                break
            brand = _extract_shop_brand(item)
            title = str(item.get("title") or item.get("product_name") or "").strip()
            if not brand or _is_noise_brand(brand):
                continue
            blob = f"{brand} {title}"
            if is_megacorp_app(brand, title) or is_supply_side(blob) or sniper_reject_reason(blob):
                continue

            seen_key = f"ttshop:{_slugify(brand)}"
            if seen_key in current or seen_key in local_keys:
                continue
            local_keys.add(seen_key)

            site_raw = str(item.get("website") or item.get("brand_url") or "").strip()
            dom = domain_of(site_raw)
            safe_site = site_raw if dom else ""
            shop_store: dict[str, Any] = {}
            shop_extra: list[str] = []
            if not dom and web_search is not None:
                disc_url, disc_dom, disc_store, disc_extra = _discover_brand_landing(
                    brand,
                    search_fn=web_search,
                    store_resolver_fn=resolve_store,
                    budget=budget,
                )
                if disc_store.get("is_megacorp") or int(disc_store.get("rating_count") or 0) > 50000:
                    continue
                if disc_dom:
                    safe_site = disc_url
                    dom = disc_dom
                if disc_store:
                    shop_store = disc_store
                shop_extra.extend(disc_extra)

            contacts = _merge_enriched_contacts(
                brand=str(shop_store.get("developer") or brand).strip(),
                domain=dom,
                project_url=safe_site,
                extra_text=title,
                enrich_fn=enricher,
                site_scrape_fn=scraper,
                budget=budget,
            )
            if not contacts["work_email"] and shop_store.get("email"):
                contacts["work_email"] = str(shop_store["email"]).strip()
            if not any(
                contacts[k]
                for k in ("domain", "work_email", "founder_x", "username")
            ):
                continue

            what_they_do = f"TikTok Shop US brand ({keyword}): {title or brand}"[:200]
            links = [
                u
                for u in (
                    contacts["project_url"],
                    *shop_extra,
                    contacts["founder_x"],
                    contacts["founder_linkedin"],
                    f"https://t.me/{contacts['username']}" if contacts["username"] else "",
                )
                if u
            ]
            row = {
                "id": seen_key,
                "name": brand,
                "username": contacts["username"],
                "telegram_username": contacts["username"],
                "bio": what_they_do,
                "links": list(dict.fromkeys(links)),
                "source": "tiktok_shop",
                "role": "Founder",
                "roles": ["Founder"],
                "category": "SaaS / AI app",
                "project": brand,
                "company": brand,
                "project_url": contacts["project_url"],
                "what_they_do": what_they_do,
                "founder_linkedin": contacts["founder_linkedin"],
                "founder_x": contacts["founder_x"],
                "work_email": contacts["work_email"],
                "has_monetization_signal": True,
                "icp_match": True,
                "lead_tier": "A",
            }
            rows.append(row)
            current[seen_key] = stamp

    return rows, current


def _load_seen(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            inner = raw.get("items") if isinstance(raw.get("items"), dict) else raw
            return {str(k): str(v) for k, v in inner.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


D2C_WHATSAPP_QUERIES = (
    '"policies/contact-information" "WhatsApp" skincare OR cosmetics OR serum -pinterest -reddit -youtube',
    '"policies/contact-information" "WhatsApp" supplements OR gummies OR collagen OR vitamins -pinterest -reddit -youtube',
    '"policies/contact-information" "WhatsApp" haircare OR fragrance OR perfume OR bodycare -pinterest -reddit -youtube',
    '"Powered by Shopify" "wa.me/" skincare OR supplements OR cosmetics OR gummies -pinterest -youtube',
    '"pages/contact" "wa.me/" "collections" skincare OR cosmetics OR supplements -pinterest -youtube',
    '"pages/contact-us" "WhatsApp" skincare OR supplements OR collagen "shop" -pinterest -youtube',
    'site:linktr.ee "wa.me/" skincare OR cosmetics OR supplements "shop"',
    'site:instagram.com ("skincare" OR "cosmetics" OR "supplements") "wa.me/" (".com" OR ".co" OR "myshopify")',
)

_D2C_SKIP_HOSTS = (
    "shopify.com",
    "apps.shopify.com",
    "cdn.shopify.com",
    "community.shopify.com",
    "help.shopify.com",
    "shopify.dev",
    "faq.whatsapp.com",
    "whatsapp.com",
    "wa.me",
    "interakt.shop",
    "wati.io",
    "pabbly.com",
    "amazon.",
    "ebay.",
    "walmart.",
    "aliexpress.",
    "alibaba.",
    "youtube.com",
    "youtu.be",
    "reddit.com",
    "pinterest.",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "t.me",
    "telegram.me",
    "t.co",
    "bit.ly",
    "tinyurl.com",
    "linktr.ee",
    "beacons.ai",
    "stan.store",
    "wikipedia.org",
    "github.com",
    "w3.org",
    "schema.org",
    "ogp.me",
    "rdfs.org",
    "xmlns.com",
    "googletagmanager.com",
    "google-analytics.com",
    "googlesyndication.com",
    "doubleclick.net",
    "facebook.net",
    "cookielaw.org",
    "onetrust.com",
    "sentry.io",
    "cloudflare.com",
    "jsdelivr.net",
    "unpkg.com",
    "googleapis.com",
    "gstatic.com",
    "apple.com",
    "google.com",
    "gmail.com",
)
_D2C_NOISE_RE = re.compile(
    r"\b(?:massage(?:\s+treatment)?|home\s+service|walk-in\s+clinic|cat\s+hotel|pet\s+hotel|"
    r"dental|dentist|hospital|clinic|medspa|day\s+spa|\bspa\b|beauty\s+salon|hair\s+salon|barbershop|"
    r"facial\s+treatment|aesthetics\s+clinic|dermatolog\w*|chiropract\w*|pharmacy|restaurant|tattoo|"
    r"grooming|real\s+estate|wholesale\s+supplier|private\s+label\s+manufacturer|"
    r"oem\s+cosmetics|dropshipping\s+expert|shopify\s+expert)\b",
    re.I,
)
_D2C_ECOM_HTML_RE = re.compile(
    r"(?:cdn\.shopify\.com|Shopify\.theme|myshopify\.com|/collections/)",
    re.I,
)
_D2C_TITLE_STRIP_RE = re.compile(
    r"^\s*(?:contact\s+information|contact\s+us|terms\s+of\s+service|privacy\s+policy|about\s+us|policies|refund\s+policy)\s*[-–—|:]+\s*|"
    r"\s*[-–—|:]+\s*(?:contact\s+information|contact\s+us|terms\s+of\s+service|privacy\s+policy|about\s+us|policies|official\s+store|home)\s*$",
    re.I,
)
_D2C_HTTP_URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.I)
_D2C_BARE_DOMAIN_RE = re.compile(
    r"\b([a-z0-9][a-z0-9-]{1,40}\.(?:myshopify\.com|co\.uk|com|co|io|nl|de|fr|uk|us|ca|au|shop|store|skin|beauty))\b",
    re.I,
)
_D2C_ASSET_EXT_RE = re.compile(r"\.(?:svg|png|jpe?g|webp|gif|ico|css|js|woff2?|ttf|xml|json)(?:[?#]|$)", re.I)


def _is_public_ip(ip_text: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return not (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _validate_public_http_url(url: str) -> str:
    clean = str(url or "").strip()
    if not clean.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(clean)
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    if parsed.username or parsed.password:
        return ""
    if parsed.port not in (None, 80, 443):
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "." not in host or host in {"localhost", "localhost.localdomain"}:
        return ""
    try:
        ipaddress.ip_address(host)
        return ""  # reject raw IP literals
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return ""
    resolved_ips = [str(info[4][0]).split("%", 1)[0] for info in infos if info and len(info) >= 5 and info[4]]
    if not resolved_ips or not all(_is_public_ip(ip) for ip in resolved_ips):
        return ""
    return clean


class _SafePublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _validate_public_http_url(newurl):
            raise urllib.error.URLError("Blocked unsafe redirect target")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_d2c_http_get(url: str) -> str:
    clean = _validate_public_http_url(url)
    if not clean:
        return ""
    req = urllib.request.Request(
        clean,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC/1.0)"},
    )
    opener = urllib.request.build_opener(_SafePublicRedirectHandler())
    with opener.open(req, timeout=6) as resp:
        return resp.read(250_000).decode("utf-8", "ignore")


def _d2c_fetch_text(http_get_fn: Any, url: str) -> str:
    try:
        res = http_get_fn(url)
    except Exception:
        return ""
    if isinstance(res, str):
        return res
    if hasattr(res, "status_code") and int(getattr(res, "status_code") or 0) >= 400:
        return ""
    return str(getattr(res, "text", "") or "")


def _is_d2c_skip_domain(dom: str) -> bool:
    if not dom or "." not in dom or ":" in dom:
        return True
    low = dom.lower().removeprefix("www.").rstrip(".")
    try:
        ipaddress.ip_address(low)
        return True
    except ValueError:
        pass
    if low.endswith((".ru", ".su", ".by", ".рф", ".gov", ".edu", ".internal", ".local")):
        return True
    for tok in _D2C_SKIP_HOSTS:
        if tok.endswith("."):
            if low.startswith(tok) or f".{tok}" in low:
                return True
        elif low == tok or low.endswith(f".{tok}"):
            return True
    return False


def _extract_d2c_external_store_urls(raw_html: str) -> list[tuple[str, str]]:
    """Extract ordered (domain, url) store candidates from Linktree/bio HTML, skipping SVG/XML/CDN noise."""
    if not raw_html:
        return []
    href_urls = re.findall(r"""href=["'](https?://[^"']+)["']""", raw_html, flags=re.I)
    ordered_urls = [*href_urls, *_D2C_HTTP_URL_RE.findall(raw_html)]
    out: list[tuple[str, str]] = []
    seen_doms: set[str] = set()
    for raw_u in ordered_urls:
        clean_u = html_mod.unescape(raw_u).strip().rstrip(".,;)")
        if _D2C_ASSET_EXT_RE.search(clean_u):
            continue
        parsed = urlparse(clean_u)
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            continue
        ext_dom = domain_of(clean_u) or ""
        if not ext_dom or _is_d2c_skip_domain(ext_dom) or ext_dom in seen_doms:
            continue
        seen_doms.add(ext_dom)
        out.append((ext_dom, clean_u))
    return out


def _clean_d2c_brand_name(raw_title: str, domain: str) -> str:
    cleaned = html_mod.unescape(" ".join(str(raw_title or "").split())).strip()
    for _ in range(3):
        nxt = _D2C_TITLE_STRIP_RE.sub("", cleaned).strip(" -–—|:")
        if nxt == cleaned:
            break
        cleaned = nxt
    if (
        cleaned
        and 2 <= len(cleaned) <= 60
        and not _is_noise_brand(cleaned)
        and "whatsapp" not in cleaned.lower()
    ):
        return cleaned
    stem = domain.split(".", 1)[0].replace("-", " ").replace("_", " ").strip()
    return stem.title() if stem else domain


def collect_d2c_whatsapp_brands(
    *,
    limit: int = 8,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    call_budget: list[int] | None = None,
    search_fn=None,
    http_get_fn=None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Harvest D2C Shopify/IG cosmetics & supplements brands exposing WhatsApp (wa.me / E.164)."""
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    searcher = _default_web_search if search_fn is None else search_fn
    fetcher = _default_d2c_http_get if http_get_fn is None else http_get_fn
    budget = call_budget if call_budget is not None else [MAX_API_CALLS_PER_RUN]

    try:
        try:
            from leadgen.contact_waterfall import rank_and_pick_best_email
        except ImportError:
            from contact_waterfall import rank_and_pick_best_email
    except Exception:
        rank_and_pick_best_email = None

    rows: list[dict[str, Any]] = []
    local_keys: set[str] = set()

    queries = _rotate_for_day(D2C_WHATSAPP_QUERIES, today, min(5, len(D2C_WHATSAPP_QUERIES)))
    for q in queries:
        if len(rows) >= limit or budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            payload = searcher(q)
        except Exception:
            payload = None

        for hit in _unwrap_search_results(payload):
            if len(rows) >= limit:
                break
            hit_url = str(hit.get("url") or hit.get("link") or "").strip()
            hit_title = str(hit.get("title") or "").strip()
            hit_snip = str(hit.get("snippet") or hit.get("description") or "").strip()
            if not hit_url:
                continue

            hit_host = (urlparse(hit_url).hostname or "").lower().removeprefix("www.")
            bio_extra_html = ""
            cand_urls: list[str] = []
            domain = ""
            if hit_host == "linktr.ee" or hit_host.endswith(".linktr.ee"):
                bio_extra_html = _d2c_fetch_text(fetcher, hit_url)
                ext_cands = _extract_d2c_external_store_urls(bio_extra_html)
                if ext_cands:
                    domain, first_ext_url = ext_cands[0]
                    cand_urls.append(first_ext_url)
            elif hit_host == "instagram.com" or hit_host.endswith(".instagram.com"):
                snip_blob = f"{hit_title}\n{hit_snip}"
                linktree_m = re.search(r"https?://(?:www\.)?linktr\.ee/[A-Za-z0-9._-]+", snip_blob, flags=re.I)
                if linktree_m:
                    bio_extra_html = _d2c_fetch_text(fetcher, linktree_m.group(0))
                    ext_cands = _extract_d2c_external_store_urls(bio_extra_html)
                    if ext_cands:
                        domain, first_ext_url = ext_cands[0]
                        cand_urls.append(first_ext_url)
                if not domain:
                    ext_cands = _extract_d2c_external_store_urls(snip_blob)
                    if ext_cands:
                        domain, first_ext_url = ext_cands[0]
                        cand_urls.append(first_ext_url)
                    else:
                        for dm in _D2C_BARE_DOMAIN_RE.finditer(snip_blob):
                            cand_dom = dm.group(1).lower().removeprefix("www.")
                            if not _is_d2c_skip_domain(cand_dom):
                                domain = cand_dom
                                cand_urls.append(f"https://{cand_dom}/")
                                break
            elif not _is_d2c_skip_domain(hit_host):
                domain = hit_host
                cand_urls.append(hit_url)

            if not domain or _is_d2c_skip_domain(domain):
                continue

            seen_key = f"d2c_wa:{domain.lower()}"
            if seen_key in current or seen_key in local_keys:
                continue
            local_keys.add(seen_key)

            # Free /products.json check + up to 3 HTML contact/root pages
            has_ecom = False
            sample_prod = ""
            prod_raw = _d2c_fetch_text(fetcher, f"https://{domain}/products.json?limit=3")
            if prod_raw:
                try:
                    pj = json.loads(prod_raw)
                    prods = pj.get("products") if isinstance(pj, dict) else None
                    if isinstance(prods, list) and len(prods) >= 1:
                        has_ecom = True
                        first_p = prods[0] if isinstance(prods[0], dict) else {}
                        sample_prod = str(first_p.get("title") or "").strip()[:80]
                except (json.JSONDecodeError, ValueError):
                    pass

            page_blobs: list[str] = [bio_extra_html, hit_snip]
            probe_urls: list[str] = []
            for pu in [
                *cand_urls,
                f"https://{domain}/policies/contact-information",
                f"https://{domain}/pages/contact",
                f"https://{domain}/",
            ]:
                if pu and pu not in probe_urls and domain in pu.lower():
                    probe_urls.append(pu)

            wa_cands = whatsapp_urls(bio_extra_html, exclude_prefixes=("7", "375"))
            wa_url = wa_cands[0] if wa_cands else ""
            html_fetches = 0
            for pu in probe_urls:
                if html_fetches >= 3 or (has_ecom and wa_url and html_fetches >= 1):
                    break
                body = _d2c_fetch_text(fetcher, pu)
                html_fetches += 1
                if not body:
                    continue
                page_blobs.append(body)
                if _D2C_ECOM_HTML_RE.search(body):
                    has_ecom = True
                if not wa_url:
                    body_was = whatsapp_urls(body, exclude_prefixes=("7", "375"))
                    if body_was:
                        wa_url = body_was[0]
            if not wa_url:
                snip_was = whatsapp_urls(hit_snip, exclude_prefixes=("7", "375"))
                if snip_was:
                    wa_url = snip_was[0]

            if not has_ecom or not wa_url:
                continue

            wa_digits = re.sub(r"\D", "", wa_url)
            if not (8 <= len(wa_digits) <= 15) or wa_digits.startswith(("7", "375")):
                continue

            full_blob = "\n".join(b for b in page_blobs if b)
            text_for_gates = re.sub(r"<[^>]+>", " ", full_blob[:12000])
            text_for_gates = f"{hit_title}\n{hit_snip}\n{sample_prod}\n{text_for_gates[:2000]}"
            if (
                _D2C_NOISE_RE.search(text_for_gates)
                or is_supply_side(text_for_gates)
                or sniper_reject_reason(text_for_gates)
                or is_megacorp_app(hit_title, domain)
            ):
                continue

            brand = _clean_d2c_brand_name(hit_title, domain)
            all_emails: list[str] = []
            for blob_item in page_blobs:
                if not blob_item:
                    continue
                clean_blob = re.sub(r"\\?u003[ec]", " ", blob_item, flags=re.I)
                for raw_cand in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", clean_blob):
                    em_cand = email_of_text(raw_cand)
                    if em_cand and em_cand not in all_emails:
                        all_emails.append(em_cand)
            best_email = (
                rank_and_pick_best_email(all_emails, domain=domain)
                if (rank_and_pick_best_email is not None and all_emails)
                else (all_emails[0] if all_emails else "")
            )
            fx = x_profile_url({"links": _D2C_HTTP_URL_RE.findall(full_blob)})
            fli = linkedin_in_url(full_blob)

            canon_site = f"https://{domain}"
            canon_wa = f"https://wa.me/{wa_digits}"
            links = list(
                dict.fromkeys(
                    u for u in (canon_site, canon_wa, fx, fli, hit_url) if u
                )
            )
            clean_snip = " ".join(hit_snip.split())[:110]
            what_they_do = (
                f"D2C Shopify brand ({sample_prod or 'skincare/supplements'}): {clean_snip or brand}"
            )[:180]

            rows.append(
                {
                    "id": seen_key,
                    "name": brand,
                    "username": "",
                    "whatsapp": canon_wa,
                    "phone": f"+{wa_digits}",
                    "provider": "whatsapp",
                    "bio": what_they_do,
                    "links": links,
                    "source": "d2c_whatsapp",
                    "lead_source": "d2c_whatsapp",
                    "role": "Founder",
                    "roles": ["Founder"],
                    "category": "E-com / D2C",
                    "monetization": "ecom",
                    "audience": "buyers",
                    "project": brand,
                    "company": brand,
                    "project_url": canon_site,
                    "url": canon_site,
                    "what_they_do": what_they_do,
                    "their_angle": what_they_do,
                    "founder_linkedin": fli,
                    "founder_x": fx,
                    "work_email": best_email,
                    "email": best_email,
                    "has_monetization_signal": True,
                    "icp_match": True,
                    "icp_score": 88,
                    "lead_tier": "A",
                }
            )
            current[seen_key] = stamp

    return rows, current


APP_STORE_VEIN_QUERIES = (
    "AI calorie tracker",
    "AI language tutor",
    "astrology horoscope",
    "ADHD planner",
    "habit tracker",
    "AI workout coach",
    "flashcards AI",
    "sleep meditation",
)

ATS_HIRING_QUERIES = (
    'site:jobs.ashbyhq.com ("UGC" OR "Short-Form" OR "TikTok" OR "Paid Social" OR "Creator Partnerships")',
    'site:job-boards.greenhouse.io ("UGC" OR "Short-Form" OR "TikTok" OR "Paid Social" OR "Creator")',
)

_ATS_ROLE_RE = re.compile(
    r"\b(ugc|short[- ]?form|tiktok|reels|paid\s+social|creator|performance\s+creative|influencer)\b",
    re.I,
)
_CIS_LOCATION_RE = re.compile(
    r"\b(russia|russian\s+federation|belarus|ukraine|kazakhstan|uzbekistan|moscow|minsk|kyiv)\b",
    re.I,
)
_ATS_NO_BOARD = frozenset({"www", "jobs", "boards", "job-boards", "apply", "app", "embed"})


def _default_apple_search(term: str) -> Any:
    if not check_treg_ready():
        return None
    return treg_call(
        "litescrape.app_store.search.apps",
        query={"term": term, "country": "us", "lang": "en-us"},
        timeout=35,
    )


def _default_play_search(term: str) -> Any:
    if not check_treg_ready():
        return None
    return treg_call(
        "litescrape.google_play.search.apps",
        query={"q": term, "gl": "us", "hl": "en"},
        timeout=35,
    )


def _extract_store_urls(payload: Any, store_kind: str) -> list[tuple[str, str]]:
    items: list[dict[str, Any]] = []
    if isinstance(payload, list):
        items = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        for key in ("results", "apps", "items", "organic_results", "data", "output"):
            val = payload.get(key)
            if isinstance(val, list):
                items = [x for x in val if isinstance(x, dict)]
                break
            if isinstance(val, dict):
                nested = _extract_store_urls(val, store_kind)
                if nested:
                    return nested
    urls: list[tuple[str, str]] = []
    seen_u: set[str] = set()
    for it in items:
        raw_url = str(
            it.get("url")
            or it.get("link")
            or it.get("trackViewUrl")
            or it.get("app_url")
            or ""
        ).strip()
        if not raw_url:
            if store_kind == "apple" and (it.get("trackId") or it.get("id")):
                aid = re.sub(r"\D", "", str(it.get("trackId") or it.get("id")))
                if aid:
                    raw_url = f"https://apps.apple.com/us/app/id{aid}"
            elif store_kind == "play" and (it.get("appId") or it.get("product_id")):
                pkg = str(it.get("appId") or it.get("product_id")).strip()
                if pkg:
                    raw_url = f"https://play.google.com/store/apps/details?id={pkg}"
        if raw_url and raw_url.startswith(("http://", "https://")) and raw_url not in seen_u:
            seen_u.add(raw_url)
            name_hint = str(it.get("title") or it.get("trackName") or it.get("name") or "").strip()
            urls.append((raw_url, name_hint))
    return urls


def collect_scaling_store_apps(
    *,
    limit: int = 6,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    apple_search_fn=None,
    play_search_fn=None,
    store_resolver_fn=None,
    enrich_fn=None,
    site_scrape_fn=None,
    call_budget: list[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Collect scaling subscription/IAP mobile apps in the 300..30,000 rating sweet spot."""
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    apple_search = _default_apple_search if apple_search_fn is None else apple_search_fn
    play_search = _default_play_search if play_search_fn is None else play_search_fn
    resolve_store = resolve_app_store_developer if store_resolver_fn is None else store_resolver_fn
    enricher = find_company_decision_maker if enrich_fn is None else enrich_fn
    scraper = _scrape_site_contacts if site_scrape_fn is None else site_scrape_fn
    budget = call_budget if call_budget is not None else [MAX_API_CALLS_PER_RUN]

    rows: list[dict[str, Any]] = []
    local_keys: set[str] = set()
    queries = _rotate_for_day(APP_STORE_VEIN_QUERIES, today, 2)

    for kw in queries:
        if len(rows) >= limit or budget[0] <= 0:
            break
        candidates: list[tuple[str, str]] = []
        for kind, fn in (("apple", apple_search), ("play", play_search)):
            if budget[0] <= 0 or fn is None:
                continue
            budget[0] -= 1
            try:
                res = fn(kw)
            except Exception:
                res = None
            candidates.extend(_extract_store_urls(res, kind)[:6])

        for s_url, title_hint in candidates:
            if len(rows) >= limit or budget[0] <= 0:
                break
            budget[0] -= 1
            try:
                st = resolve_store(s_url) or {}
            except Exception:
                st = {}
            app_name = str(st.get("app_name") or title_hint or "").strip()
            dev = str(st.get("developer") or "").strip()
            dom = str(st.get("domain") or "").strip()
            website = str(st.get("website") or "").strip()
            rc_raw = st.get("rating_count")
            rc_digits = re.sub(r"\D", "", str(rc_raw)) if rc_raw is not None else ""
            rcount = int(rc_digits) if rc_digits else 0
            if rcount < 300 or rcount > 30000:
                continue
            if st.get("is_megacorp") or is_megacorp_app(dev, app_name, dom, rcount):
                continue
            blob = f"{app_name} {dev} {st.get('description') or ''}"
            if is_supply_side(blob) or sniper_reject_reason(blob):
                continue
            seen_key = f"appstore:{_slugify(dom or app_name or s_url)}"
            if seen_key in current or seen_key in local_keys:
                continue
            local_keys.add(seen_key)

            contacts = _merge_enriched_contacts(
                brand=dev or app_name,
                domain=dom,
                project_url=website,
                extra_text=str(st.get("description") or ""),
                enrich_fn=enricher,
                site_scrape_fn=scraper,
                budget=budget,
            )
            if not contacts["work_email"] and st.get("email"):
                contacts["work_email"] = str(st.get("email")).strip()
            if not any(contacts[k] for k in ("domain", "work_email", "founder_x", "username")):
                continue

            project_name = app_name or dev or dom
            what_they_do = f"Scaling store app ({kw}, {rcount} ratings): {project_name}"[:200]
            links = [
                u
                for u in (
                    contacts["project_url"],
                    s_url,
                    contacts["founder_x"],
                    contacts["founder_linkedin"],
                    f"https://t.me/{contacts['username']}" if contacts["username"] else "",
                )
                if u
            ]
            rows.append(
                {
                    "id": seen_key,
                    "name": dev or project_name,
                    "username": contacts["username"],
                    "telegram_username": contacts["username"],
                    "bio": what_they_do,
                    "links": list(dict.fromkeys(links)),
                    "source": "app_stores",
                    "source_url": s_url,
                    "role": "Founder",
                    "roles": ["Founder"],
                    "category": "SaaS / AI app",
                    "project": project_name,
                    "company": dev or project_name,
                    "project_url": contacts["project_url"],
                    "what_they_do": what_they_do,
                    "founder_linkedin": contacts["founder_linkedin"],
                    "founder_x": contacts["founder_x"],
                    "work_email": contacts["work_email"],
                    "rating_count": rcount,
                    "has_monetization_signal": True,
                    "icp_match": True,
                    "lead_tier": "A",
                }
            )
            current[seen_key] = stamp

    return rows, current


def _default_ashby_jobs(board: str) -> Any:
    if not check_treg_ready():
        return None
    return treg_call("ashby.companies.jobs", query={"board": board}, timeout=30)


def _default_greenhouse_jobs(board_token: str) -> Any:
    if not check_treg_ready():
        return None
    return treg_call("greenhouse.companies.jobs", query={"board_token": board_token}, timeout=30)


def _unwrap_ats_jobs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [j for j in payload if isinstance(j, dict)]
    if isinstance(payload, dict):
        for key in ("jobs", "results", "items", "data", "output"):
            val = payload.get(key)
            if isinstance(val, list):
                return [j for j in val if isinstance(j, dict)]
            if isinstance(val, dict):
                nested = _unwrap_ats_jobs(val)
                if nested:
                    return nested
    return []


def collect_ats_ugc_hiring(
    *,
    limit: int = 5,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    search_fn=None,
    ashby_fn=None,
    greenhouse_fn=None,
    store_resolver_fn=None,
    enrich_fn=None,
    site_scrape_fn=None,
    call_budget: list[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Find funded startups on Ashby/Greenhouse hiring UGC, Short-Form, TikTok or Paid Social roles."""
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    search = _default_web_search if search_fn is None else search_fn
    get_ashby = _default_ashby_jobs if ashby_fn is None else ashby_fn
    get_gh = _default_greenhouse_jobs if greenhouse_fn is None else greenhouse_fn
    resolve_store = resolve_app_store_developer if store_resolver_fn is None else store_resolver_fn
    enricher = find_company_decision_maker if enrich_fn is None else enrich_fn
    scraper = _scrape_site_contacts if site_scrape_fn is None else site_scrape_fn
    budget = call_budget if call_budget is not None else [MAX_API_CALLS_PER_RUN]

    rows: list[dict[str, Any]] = []
    local_keys: set[str] = set()
    boards: list[tuple[str, str, str]] = []

    for q in ATS_HIRING_QUERIES:
        if budget[0] <= 0 or search is None:
            break
        budget[0] -= 1
        try:
            payload = search(q)
        except Exception:
            payload = None
        for hit in _unwrap_search_results(payload):
            u = str(hit.get("url") or "").strip()
            parsed = urlparse(u)
            host = (parsed.hostname or "").lower()
            parts = [p for p in parsed.path.split("/") if p]
            if not parts:
                continue
            slug = parts[0].strip()
            if slug.lower() in _ATS_NO_BOARD or len(slug) < 2:
                continue
            if "ashbyhq.com" in host:
                boards.append(("ashby", slug, str(hit.get("title") or "")))
            elif "greenhouse.io" in host:
                boards.append(("greenhouse", slug, str(hit.get("title") or "")))

    seen_boards: set[tuple[str, str]] = set()
    for ats_kind, slug, title_hint in boards:
        if len(rows) >= limit or budget[0] <= 0:
            break
        key_b = (ats_kind, slug.lower())
        if key_b in seen_boards:
            continue
        seen_boards.add(key_b)
        budget[0] -= 1
        try:
            board_payload = get_ashby(slug) if ats_kind == "ashby" else get_gh(slug)
        except Exception:
            board_payload = None
        jobs = _unwrap_ats_jobs(board_payload)
        if not jobs or len(jobs) > 150:
            continue

        matched_job = None
        for job in jobs:
            jtitle = str(job.get("title") or "")
            loc = job.get("location")
            if isinstance(loc, dict):
                loc = loc.get("name") or ""
            loc_str = str(loc or "")
            if _ATS_ROLE_RE.search(jtitle) and not _CIS_LOCATION_RE.search(loc_str):
                matched_job = job
                break
        if not matched_job:
            continue

        brand = re.sub(r"[-_]+", " ", slug).strip().title()
        jtitle = str(matched_job.get("title") or "").strip()
        if is_megacorp_app(brand, title_hint) or is_supply_side(f"{brand} {jtitle}"):
            continue
        seen_key = f"ats:{ats_kind}:{slug.lower()}"
        if seen_key in current or seen_key in local_keys:
            continue
        local_keys.add(seen_key)

        land_url, land_dom, _, land_extra = _discover_brand_landing(
            brand,
            search_fn=search,
            store_resolver_fn=resolve_store,
            budget=budget,
        )
        contacts = _merge_enriched_contacts(
            brand=brand,
            domain=land_dom,
            project_url=land_url,
            extra_text=str(matched_job.get("absolute_url") or matched_job.get("jobUrl") or ""),
            enrich_fn=enricher,
            site_scrape_fn=scraper,
            budget=budget,
        )
        if not any(contacts[k] for k in ("domain", "work_email", "founder_x", "username")):
            continue

        job_url = str(
            matched_job.get("absolute_url")
            or matched_job.get("jobUrl")
            or f"https://jobs.ashbyhq.com/{slug}"
            if ats_kind == "ashby"
            else f"https://job-boards.greenhouse.io/{slug}"
        )
        what_they_do = f"Hiring '{jtitle}' on {ats_kind.title()} ({len(jobs)} open roles)"[:200]
        links = [
            u
            for u in (
                contacts["project_url"],
                job_url,
                *land_extra,
                contacts["founder_x"],
                contacts["founder_linkedin"],
                f"https://t.me/{contacts['username']}" if contacts["username"] else "",
            )
            if u
        ]
        rows.append(
            {
                "id": seen_key,
                "name": brand,
                "username": contacts["username"],
                "telegram_username": contacts["username"],
                "bio": what_they_do,
                "links": list(dict.fromkeys(links)),
                "source": "ats_hiring",
                "source_url": job_url,
                "role": "Marketing",
                "roles": ["Marketing"],
                "category": "SaaS / AI app",
                "project": brand,
                "company": brand,
                "project_url": contacts["project_url"],
                "what_they_do": what_they_do,
                "founder_linkedin": contacts["founder_linkedin"],
                "founder_x": contacts["founder_x"],
                "work_email": contacts["work_email"],
                "has_monetization_signal": True,
                "icp_match": True,
                "lead_tier": "A",
            }
        )
        current[seen_key] = stamp

    return rows, current


def _default_ph_detailed() -> Any:
    if not check_treg_ready():
        return None
    return treg_call(
        "crawl4ai.producthunt.launches.today_detailed",
        method="POST",
        data={"limit": 15},
        timeout=45,
    )


def _unwrap_ph_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("products", "launches", "items", "results", "data", "output"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):
                nested = _unwrap_ph_items(val)
                if nested:
                    return nested
    return []


def collect_producthunt_detailed(
    *,
    limit: int = 6,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    ph_fetch_fn=None,
    web_search_fn=None,
    store_resolver_fn=None,
    enrich_fn=None,
    site_scrape_fn=None,
    call_budget: list[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Collect today's Product Hunt launches via treg today_detailed."""
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    fetch_ph = _default_ph_detailed if ph_fetch_fn is None else ph_fetch_fn
    web_search = web_search_fn if web_search_fn is not None else (_default_web_search if ph_fetch_fn is None else None)
    resolve_store = resolve_app_store_developer if store_resolver_fn is None else store_resolver_fn
    enricher = find_company_decision_maker if enrich_fn is None else enrich_fn
    scraper = _scrape_site_contacts if site_scrape_fn is None else site_scrape_fn
    budget = call_budget if call_budget is not None else [MAX_API_CALLS_PER_RUN]

    if budget[0] <= 0 or fetch_ph is None:
        return [], current
    budget[0] -= 1
    try:
        payload = fetch_ph()
    except Exception:
        payload = None

    rows: list[dict[str, Any]] = []
    local_keys: set[str] = set()
    for item in _unwrap_ph_items(payload):
        if len(rows) >= limit:
            break
        name = str(item.get("name") or item.get("title") or "").strip()
        tagline = str(item.get("tagline") or item.get("summary") or item.get("description") or "").strip()
        if not name or _is_noise_brand(name):
            continue
        blob = f"{name} {tagline} {item.get('pricing') or ''} {item.get('category') or ''}"
        if is_megacorp_app(name, tagline) or is_supply_side(blob) or sniper_reject_reason(blob):
            continue

        site_raw = str(item.get("website") or item.get("product_url") or item.get("url") or "").strip()
        dom = domain_of(site_raw)
        if not dom or "producthunt.com" in dom:
            site_raw = ""
            dom = ""
        if not dom and web_search is not None:
            land_url, land_dom, _, _ = _discover_brand_landing(
                name,
                search_fn=web_search,
                store_resolver_fn=resolve_store,
                budget=budget,
            )
            if land_dom:
                site_raw = land_url
                dom = land_dom

        seen_key = f"ph_det:{_slugify(dom or name)}"
        if seen_key in current or seen_key in local_keys:
            continue
        local_keys.add(seen_key)

        makers_txt = json.dumps(item.get("makers") or "", ensure_ascii=False)
        contacts = _merge_enriched_contacts(
            brand=name,
            domain=dom,
            project_url=site_raw,
            extra_text=f"{tagline} {makers_txt} {item.get('maker_x') or ''} {item.get('maker_tg') or ''}",
            enrich_fn=enricher,
            site_scrape_fn=scraper,
            budget=budget,
        )
        if not any(contacts[k] for k in ("domain", "work_email", "founder_x", "username")):
            continue

        what_they_do = f"Product Hunt launch: {tagline or name}"[:200]
        ph_url = str(item.get("ph_url") or item.get("url") or "").strip()
        links = [
            u
            for u in (
                contacts["project_url"],
                ph_url if "producthunt.com" in ph_url else "",
                contacts["founder_x"],
                contacts["founder_linkedin"],
                f"https://t.me/{contacts['username']}" if contacts["username"] else "",
            )
            if u
        ]
        rows.append(
            {
                "id": seen_key,
                "name": name,
                "username": contacts["username"],
                "telegram_username": contacts["username"],
                "bio": what_they_do,
                "links": list(dict.fromkeys(links)),
                "source": "ph_detailed",
                "source_url": ph_url or contacts["project_url"],
                "role": "Founder",
                "roles": ["Founder"],
                "category": "SaaS / AI app",
                "project": name,
                "company": name,
                "project_url": contacts["project_url"],
                "what_they_do": what_they_do,
                "founder_linkedin": contacts["founder_linkedin"],
                "founder_x": contacts["founder_x"],
                "work_email": contacts["work_email"],
                "has_monetization_signal": True,
                "icp_match": True,
                "lead_tier": "A",
            }
        )
        current[seen_key] = stamp

    return rows, current


def _save_seen(path: Path, seen: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"items": seen}, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def collect_all_veins(
    *,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    limit: int = 24,
    veins: tuple[str, ...] = ("whop", "tiktok_ads", "tiktok_shop"),
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    today = day or date.today()
    merged_seen = fresh_seen(seen or {}, today)
    all_rows: list[dict[str, Any]] = []

    vein_set = {v.strip().lower() for v in veins if v.strip()}
    per_vein_cap = max(2, min(6, limit // max(1, len(vein_set)) + 1))

    if "d2c_whatsapp" in vein_set and len(all_rows) < limit:
        d_rows, merged_seen = collect_d2c_whatsapp_brands(
            limit=min(per_vein_cap, limit - len(all_rows)),
            day=today,
            seen=merged_seen,
            call_budget=[10],
        )
        all_rows.extend(d_rows)
    if "whop" in vein_set and len(all_rows) < limit:
        w_rows, merged_seen = collect_whop_content_rewards(
            limit=min(per_vein_cap, limit - len(all_rows)),
            day=today,
            seen=merged_seen,
            call_budget=[10],
        )
        all_rows.extend(w_rows)
    if "tiktok_ads" in vein_set and len(all_rows) < limit:
        t_rows, merged_seen = collect_tiktok_creative_ads(
            limit=min(per_vein_cap, limit - len(all_rows)),
            day=today,
            seen=merged_seen,
            call_budget=[10],
        )
        all_rows.extend(t_rows)
    if "tiktok_shop" in vein_set and len(all_rows) < limit:
        s_rows, merged_seen = collect_tiktok_shop_brands(
            limit=min(per_vein_cap, limit - len(all_rows)),
            day=today,
            seen=merged_seen,
            call_budget=[10],
        )
        all_rows.extend(s_rows)
    if "app_stores" in vein_set and len(all_rows) < limit:
        a_rows, merged_seen = collect_scaling_store_apps(
            limit=min(per_vein_cap, limit - len(all_rows)),
            day=today,
            seen=merged_seen,
            call_budget=[10],
        )
        all_rows.extend(a_rows)
    if "ats_hiring" in vein_set and len(all_rows) < limit:
        h_rows, merged_seen = collect_ats_ugc_hiring(
            limit=min(per_vein_cap, limit - len(all_rows)),
            day=today,
            seen=merged_seen,
            call_budget=[10],
        )
        all_rows.extend(h_rows)
    if "ph_detailed" in vein_set and len(all_rows) < limit:
        p_rows, merged_seen = collect_producthunt_detailed(
            limit=min(per_vein_cap, limit - len(all_rows)),
            day=today,
            seen=merged_seen,
            call_budget=[8],
        )
        all_rows.extend(p_rows)

    return all_rows[:limit], merged_seen


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Collect SoftUGC high-yield veins")
    parser.add_argument("--output", required=True)
    parser.add_argument("--seen", default="data/leadgen_state/high_yield_seen.json")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--veins", default="whop,tiktok_ads,tiktok_shop,app_stores,ats_hiring,ph_detailed,d2c_whatsapp")
    parser.add_argument("--day", default="", help="YYYY-MM-DD, defaults to today")
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.day) if args.day else date.today()
    seen_path = Path(args.seen)
    before = _load_seen(seen_path)
    veins = tuple(v.strip() for v in args.veins.split(",") if v.strip())
    try:
        rows, updated = collect_all_veins(day=day, seen=before, limit=max(0, args.limit), veins=veins)
    except Exception:
        rows, updated = [], before
    _save_seen(seen_path, updated)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"high_yield_veins: admitted={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
