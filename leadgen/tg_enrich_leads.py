#!/usr/bin/env python3
"""Enrich scraped TG leads: classify into SoftUGC ЦА categories, extract
project name/url, fetch project site description, draft a personalization hook.

Input: JSONL from tg_scrape_posters.py
Output: enriched JSONL with extra fields.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from supply_side import sanitize_person_and_company
except ImportError:
    try:
        from leadgen.supply_side import sanitize_person_and_company
    except ImportError:
        from scripts.supply_side import sanitize_person_and_company

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 5
MAX_ENRICH_WORKERS = 12

SOCIAL = (
    "x.com",
    "twitter.com",
    "mobile.twitter.com",
    "t.co",
    "linkedin.com",
    "instagram.com",
    "t.me",
    "telegram.me",
    "telegram.dog",
    "telegra.ph",
    "youtube.com",
    "youtu.be",
    "linktr.ee",
    "linktree.com",
    "facebook.com",
    "fb.com",
    "medium.com",
    "substack.com",
    "calendly.com",
    "notion.site",
    "notion.so",
    "github.com",
    "gitlab.com",
    "discord.gg",
    "discord.com",
    "gmail.com",
    "beacons.ai",
    "solo.to",
    "carrd.co",
    "lu.ma",
    "luma.com",
    "news.ycombinator.com",
    "ycombinator.com",
    "huggingface.co",
    "itunes.apple.com",
    "apps.apple.com",
    "play.google.com",
    "chromewebstore.google.com",
    "docs.google.com",
    "drive.google.com",
    "forms.gle",
    "typeform.com",
    "producthunt.com",
    "reddit.com",
    "tiktok.com",
    "whop.com",
    "apps.whop.com",
    "wa.me",
    "whatsapp.com",
)

_BLOCKED_PROJECT_TOKENS = frozenset(
    {
        "lu",
        "luma",
        "news",
        "huggingface",
        "itunes",
        "apps",
        "play",
        "whop",
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

# ЦА categories (priority order)
CATEGORIES = [
    (
        "Проп трейдинг",
        r"\b(prop[- ]?trad\w*|prop[- ]?firm\w*|funded[- ]?trad\w*|funded[- ]?account\w*|"
        r"(?:prop|funded|trading|forex|crypto)\s+challenge\w*|проп[- ]?(?:трейд\w*|фирм\w*)|проп)\b",
    ),
    ("CEX / биржа", r"\b(exchange|cex\b|бирж\w*|trading platform|broker)\b"),
    ("DeFi / DEX", r"\b(defi|dex\b|tvl|staking|yield|liquidity|lending|borrow|swap|amm|perp\w*|vault\w*)\b"),
    ("Gaming / AI", r"\b(game\w*|gamefi|p2e|playto|ai[- ]?(agent|app|tool|video|startup)|\bai\b|agent\w*|ml\b|нейросет\w*|metaverse)\b"),
    ("SaaS / AI app", r"\b(saas|b2b app|mobile app|ios app|android app|приложени\w*|app developer|indie\w*|micro[- ]?saas|product[- ]?hunt|software product|subscription app|ai app|ai tool|ai startup|gpt[- ]?app)\b"),
    ("E-com / D2C", r"\b(shopify|e-?com(?:merce)?|d2c|dtc|skincare|cosmetics|supplements?|serum|gummies|collagen|haircare|fragrance|bodycare|online store|cosmetic brand|beauty brand)\b"),
    ("Медиа / контент", r"\b(podcast\w*|подкаст\w*|ama\b|media|медиа|newsletter|youtube|host\b|эфир\w*|content|show\b|канал)\b"),
]
CAT_RE = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in CATEGORIES]

# Роль по био — продаём founders/growth/marketing/community. Порядок = приоритет.
ROLES = [
    ("Founder", r"\b(founder|co[- ]?founder|ceo|cto|founding|owner|основател\w*|соосновател\w*|building|creator of)\b"),
    ("Marketing", r"\b(cmo|marketing|growth|marketer|smm|brand|head of marketing|маркет\w*|маркетолог|pr\b|promotion)\b"),
    ("Community", r"\b(community|community manager|ambassador|moderator|комьюнити|bd\b|business development|partnership\w*|bde\b)\b"),
    ("Creator/KOL", r"\b(kol\b|influencer|blogger|блогер|content creator|youtuber|podcaster|автор)\b"),
    ("Dev", r"\b(developer|dev\b|engineer|solidity|rust|fullstack|разработчик|программист)\b"),
    ("Trader", r"\b(trader|trading|трейдер|investor|инвестор)\b"),
]
ROLE_RE = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in ROLES]


def classify_role(bio):
    if not bio:
        return ""
    for name, rx in ROLE_RE:
        if rx.search(bio):
            return name
    return ""


def classify_cat(bio):
    if not bio:
        return "Web3 (общее)"
    for name, rx in CAT_RE:
        if rx.search(bio):
            return name
    return "Web3 (общее)"


def is_social(url):
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else "https://" + raw.lstrip("/"))
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in SOCIAL)


def pick_project_url(links):
    for l in links:
        if l.startswith(("http://", "https://", "www.")) and not is_social(l):
            return l
    return ""


def project_name_from(url, bio):
    if url and not is_social(url):
        parsed = urlparse(url if "://" in url else "https://" + url.lstrip("/"))
        host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
        parts = [p for p in host.split(".") if p]
        if parts:
            base = parts[-2] if len(parts) >= 3 and parts[0] in {"app", "get", "try", "use", "go", "my", "hey"} else parts[0]
            if base and base not in _BLOCKED_PROJECT_TOKENS:
                return base
    if bio:
        m = re.search(r"(?:founder|co[- ]?founder|ceo|cmo|building|of)\s*@([A-Za-z0-9_]{3,})", bio, re.IGNORECASE)
        if m and m.group(1).lower() not in _BLOCKED_PROJECT_TOKENS:
            return "@" + m.group(1)
    return ""


def norm_url(url):
    if not url:
        return ""
    if url.startswith("www."):
        return "https://" + url
    return url


_SITE_MONETIZATION_RE = re.compile(
    r"\bpricing\b|\$\s*\d+(?:\.\d+)?\s*(?:/\s*(?:mo|month|yr|year|wk|week)|per\s+month)|"
    r"\$\s*/\s*mo\b|\bper\s+month\b|\bsubscri(?:be|ption)\b|\bfree\s+trial\b|"
    r"\badd\s+to\s+cart\b|\bcheckout\b|apps\.apple\.com|play\.google\.com|"
    r"\bshopify\b|myshopify\.com|\bfunded\b|\bchallenge\b|\brevshare\b",
    re.IGNORECASE,
)
_SITE_AGENCY_RE = re.compile(
    r"\bour\s+clients\b|\bcase\s+studies\b|\bbook\s+a\s+discovery\s+call\b|"
    r"\bmarketing\s+agenc\w*\b|\bpr\s+agenc\w*\b|"
    r"\bsoftware\s+development\s+company\b|\bwe\s+build\s+custom\b",
    re.IGNORECASE,
)


def inspect_site_signals(html_text: str, page_url: str = "") -> dict:
    raw = (html_text or "")[:200000]
    if not raw:
        return {"desc": "", "has_monetization_signal": False, "has_agency_signal": False, "pricing_url": ""}
    soup = BeautifulSoup(raw, "html.parser")
    desc = ""
    m = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if m and m.get("content"):
        desc = m["content"].strip()
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    out_desc = (desc or title)[:220]

    visible_text = soup.get_text(" ", strip=True)
    hrefs = [str(a.get("href") or "").strip() for a in soup.find_all("a", href=True)]
    combined = f"{visible_text}\n" + "\n".join(hrefs)

    pricing_url = ""
    if page_url:
        base_host = (urlparse(page_url).hostname or "").lower().removeprefix("www.")
        for href in hrefs:
            if not href:
                continue
            resolved = urljoin(page_url, href)
            parsed_h = urlparse(resolved)
            h_host = (parsed_h.hostname or "").lower().removeprefix("www.")
            h_path = (parsed_h.path or "").rstrip("/").lower()
            if h_path in {"/pricing", "/plans"} and (not base_host or h_host == base_host):
                pricing_url = resolved
                break

    has_monetization = bool(_SITE_MONETIZATION_RE.search(combined) or pricing_url)
    has_agency = bool(_SITE_AGENCY_RE.search(visible_text) or _SITE_AGENCY_RE.search(out_desc))
    return {
        "desc": out_desc,
        "has_monetization_signal": has_monetization,
        "has_agency_signal": has_agency,
        "pricing_url": pricing_url,
    }


_CF_CHALLENGE_PHRASES = (
    "just a moment",
    "attention required",
    "enable javascript and cookies",
    "verifying you are human",
    "access denied",
)


def fetch_site_profile(url: str) -> dict:
    url = norm_url(url)
    empty = {"desc": "", "has_monetization_signal": False, "has_agency_signal": False}
    if not url:
        return empty
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        status = getattr(r, "status_code", 200)
        if not getattr(r, "ok", status < 400) or (isinstance(status, int) and status >= 400):
            return empty
        if "text/html" not in r.headers.get("Content-Type", ""):
            return empty
        info = inspect_site_signals(r.text or "", page_url=url)
        if any(tok in (info.get("desc") or "").lower() for tok in _CF_CHALLENGE_PHRASES):
            return empty
        pricing_url = info.get("pricing_url") or ""
        if pricing_url and pricing_url.rstrip("/") != url.rstrip("/"):
            try:
                pr = requests.get(pricing_url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
                p_status = getattr(pr, "status_code", 200)
                if (
                    getattr(pr, "ok", p_status < 400)
                    and not (isinstance(p_status, int) and p_status >= 400)
                    and "text/html" in pr.headers.get("Content-Type", "")
                ):
                    p_info = inspect_site_signals(pr.text or "", page_url=pricing_url)
                    info["has_monetization_signal"] = bool(
                        info["has_monetization_signal"] or p_info["has_monetization_signal"]
                    )
                    info["has_agency_signal"] = bool(
                        info["has_agency_signal"] or p_info["has_agency_signal"]
                    )
            except Exception:
                pass
        return {
            "desc": info["desc"],
            "has_monetization_signal": bool(info["has_monetization_signal"]),
            "has_agency_signal": bool(info["has_agency_signal"]),
        }
    except Exception:
        return empty


def fetch_desc(url):
    return fetch_site_profile(url)["desc"]


def bio_detail(bio):
    if not bio:
        return ""
    # first sentence-ish fragment, no links, trimmed
    txt = re.sub(LINK_STRIP, "", bio)
    txt = " ".join(txt.split())
    return txt[:90]


def extract_social_contacts(links):
    """Keep explicit founder-level X/LinkedIn URLs from a Telegram bio."""
    found = {}
    for raw in links or []:
        parsed = urlparse(str(raw))
        host = (parsed.netloc or "").lower().removeprefix("www.")
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if host in {"x.com", "twitter.com"} and parts and parts[0].lower() not in {"i", "intent", "search", "home"}:
            handle = parts[0]
            found.setdefault("x", {
                "platform": "x", "handle": handle,
                "url": f"https://x.com/{handle}",
                "match": "explicit_source", "verification_status": "verified_source_url",
            })
        elif host == "linkedin.com" and len(parts) >= 2 and parts[0].lower() == "in":
            handle = parts[1]
            found.setdefault("linkedin", {
                "platform": "linkedin", "handle": handle,
                "url": f"https://www.linkedin.com/in/{handle}",
                "match": "explicit_source", "verification_status": "verified_source_url",
            })
    contacts = list(found.values())
    return {
        "social_verified": contacts,
        "social_contacts": contacts,
        "founder_x": found.get("x", {}).get("url", ""),
        "founder_twitter": found.get("x", {}).get("url", ""),
        "founder_linkedin": found.get("linkedin", {}).get("url", ""),
    }


_BUYER_ROLES = frozenset({"Founder", "Marketing"})


def merge_roles(row, bio):
    """Keep pre-tagged buyer roles or non-empty roles when bio classifies weakly.

    A row with no buyer roles yet gets classify_role(bio) normally.
    Pre-tagged Founder/Marketing roles are preserved when bio classifies empty or
    to a non-buyer role (e.g., Trader/Creator/KOL/Dev in ad/reward copy).
    """
    classified = classify_role(bio)
    raw = row.get("roles") if isinstance(row, dict) else None
    existing = []
    if isinstance(raw, list):
        existing = [str(item) for item in raw if str(item or "").strip()]
    single_role = str(row.get("role") or "").strip() if isinstance(row, dict) else ""

    if classified in _BUYER_ROLES:
        return classified, [classified]

    existing_buyer = next((r for r in [single_role, *existing] if r in _BUYER_ROLES), "")
    if existing_buyer:
        kept_roles = existing if existing_buyer in existing else [existing_buyer, *existing]
        return existing_buyer, kept_roles

    if existing and not classified:
        role = single_role or existing[0]
        return role, existing
    if not classified and single_role:
        return single_role, [single_role]
    return classified, ([classified] if classified else [])


def merge_social_fields(row, extracted):
    """Keep founder X already on the row when extract found nothing.

    A non-empty extract wins, so a link in the bio replaces a stale handle.
    """
    merged = dict(extracted or {})
    for key in ("founder_x", "founder_twitter"):
        if str(merged.get(key) or "").strip():
            continue
        previous = str((row or {}).get(key) or "").strip()
        if previous:
            merged[key] = previous
    return merged


LINK_STRIP = re.compile(r"(https?://[^\s]+|t\.me/[^\s]+|www\.[^\s]+)")


def apply_vlad_sonnet_or_fallback(rows: list[dict]) -> list[dict]:
    """Populate first_touch, followup_1 ({audience}), and followup_2 = VLAD_STEP3_TEXT via claude-sonnet-5 when configured."""
    try:
        from outreach_copy import (
            REVSHARE_OFFER_TEXT,
            VLAD_STEP3_TEXT,
            first_touch as _vlad_first_touch,
            followup_one,
            generate_vlad_sonnet_batch,
            infer_lead_audience,
        )
    except ImportError:
        from leadgen.outreach_copy import (
            REVSHARE_OFFER_TEXT,
            VLAD_STEP3_TEXT,
            first_touch as _vlad_first_touch,
            followup_one,
            generate_vlad_sonnet_batch,
            infer_lead_audience,
        )

    api_key = (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    try:
        from app.runtime_config import should_allow_auto_sonnet as _allow_auto_sonnet

        sonnet_runtime_ok = _allow_auto_sonnet()
    except ImportError:
        sonnet_runtime_ok = (
            os.environ.get("CRMTG_VPS_RUNTIME") == "1"
            or os.environ.get("CRMTG_EPHEMERAL_RUNTIME") != "1"
            or os.environ.get("CRMTG_AUTO_SONNET") == "1"
        )
    allow_sonnet = bool(api_key) and sonnet_runtime_ok

    need_idx: list[int] = []
    need_payloads: list[dict] = []
    for idx, r in enumerate(rows):
        cat_low = str(r.get("category") or "").lower()
        src_low = str(r.get("lead_source") or r.get("source") or "").lower()
        if any(k in cat_low or k in src_low for k in ("ваканс", "vacanc", "job_monitor")):
            continue
        raw_ft = str(r.get("first_touch") or r.get("step1_text") or "").strip()
        already_canonical = (
            bool(raw_ft)
            and "SoftUGC" in raw_ft
            and REVSHARE_OFFER_TEXT in raw_ft
            and "creatoros" not in raw_ft.lower()
            and "if your user growth has hit a ceiling" not in raw_ft.lower()
            and "plug-and-play" not in raw_ft.lower()
        )
        if already_canonical:
            aud = str(r.get("audience") or "").strip().lower() or infer_lead_audience(
                category=str(r.get("category") or ""),
                company=str(r.get("project") or r.get("company") or ""),
                their_angle=str(r.get("what_they_do") or r.get("their_angle") or ""),
                site=str(r.get("project_url") or r.get("website") or ""),
            )
            r["first_touch"] = raw_ft
            r["step1_text"] = raw_ft
            r["hook"] = raw_ft
            r["audience"] = aud
            r["followup_1"] = followup_one(
                str(r.get("project") or r.get("company") or ""),
                str(r.get("what_they_do") or ""),
                audience=aud,
                category=str(r.get("category") or ""),
                site=str(r.get("project_url") or ""),
            )
            r["followup_2"] = VLAD_STEP3_TEXT
            continue

        if allow_sonnet:
            need_idx.append(idx)
            need_payloads.append(
                {
                    "company": r.get("project") or r.get("company") or r.get("name") or "",
                    "name": r.get("name") or "",
                    "website": r.get("project_url") or r.get("website") or "",
                    "category": r.get("category") or "",
                    "their_angle": r.get("what_they_do") or r.get("bio") or "",
                    "source": r.get("lead_source") or r.get("source") or "",
                }
            )

    sonnet_done: set[int] = set()
    if allow_sonnet and need_payloads:
        for start in range(0, len(need_payloads), 15):
            chunk_i = need_idx[start : start + 15]
            chunk_p = need_payloads[start : start + 15]
            try:
                gen_list = generate_vlad_sonnet_batch(chunk_p, model="claude-sonnet-5")
            except Exception:
                continue
            for row_i, gen_obj, pay in zip(chunk_i, gen_list, chunk_p):
                if not isinstance(gen_obj, dict):
                    continue
                s1 = str(gen_obj.get("step1_text") or "").strip()
                aud = str(gen_obj.get("audience") or "").strip().lower()
                if not s1:
                    continue
                target = rows[row_i]
                target["first_touch"] = s1
                target["step1_text"] = s1
                target["hook"] = s1
                target["audience"] = aud
                target["followup_1"] = followup_one(
                    str(pay.get("company") or ""),
                    str(pay.get("their_angle") or ""),
                    audience=aud,
                    category=str(pay.get("category") or ""),
                    site=str(pay.get("website") or ""),
                )
                target["followup_2"] = VLAD_STEP3_TEXT
                sonnet_done.add(row_i)

    for idx, r in enumerate(rows):
        if idx in sonnet_done:
            continue
        raw_ft = str(r.get("first_touch") or r.get("step1_text") or "").strip()
        if raw_ft and "SoftUGC" in raw_ft and REVSHARE_OFFER_TEXT in raw_ft:
            continue
        pname = str(r.get("project") or "")
        what = str(r.get("what_they_do") or "")
        cat = str(r.get("category") or "")
        purl = str(r.get("project_url") or "")
        aud = infer_lead_audience(category=cat, company=pname, their_angle=what, site=purl)
        ft = _vlad_first_touch(pname, what, russian=False, category=cat, site=purl)
        r["first_touch"] = ft
        r["step1_text"] = ft
        r["hook"] = ft
        r["audience"] = aud
        r["followup_1"] = followup_one(pname, what, audience=aud, category=cat, site=purl)
        r["followup_2"] = VLAD_STEP3_TEXT

    return rows


def enrich_rows(
    rows: list[dict],
    *,
    fetch: bool = False,
    max_workers: int = MAX_ENRICH_WORKERS,
    skip_copy: bool = False,
) -> list[dict]:
    prepared: list[tuple[dict, str, str, str, str]] = []
    urls_to_fetch: list[str] = []
    seen_urls: set[str] = set()

    strong_cats = {
        "SaaS / AI app",
        "E-com / D2C",
        "CEX / биржа",
        "Проп трейдинг",
        "DeFi / DEX",
        "Gaming / AI",
        "Платёжки / финтех",
    }
    commercial_sources = {
        "whop_content_rewards",
        "tiktok_ads",
        "tiktok_shop",
        "meta_ads",
        "app_store",
        "app_stores",
        "d2c_whatsapp",
        "fb_groups",
        "facebook_groups",
    }
    for r in rows:
        bio = r.get("bio") or ""
        existing_cat = str(r.get("category") or "").strip()
        classified_cat = classify_cat(bio)
        src_val = str(r.get("source") or r.get("lead_source") or "").strip()
        if existing_cat == "E-com / D2C" or str(r.get("monetization") or "").strip().lower() == "ecom":
            cat = "E-com / D2C" if classified_cat in ("Web3 (общее)", "Медиа / контент", "E-com / D2C") else existing_cat or classified_cat
        elif existing_cat in strong_cats and (
            classified_cat == "Web3 (общее)"
            or (src_val in commercial_sources and classified_cat in ("Медиа / контент", "E-com / D2C"))
        ):
            cat = existing_cat
        elif bio.strip():
            cat = classified_cat
        else:
            cat = existing_cat or classified_cat
        purl = pick_project_url(r.get("links") or []) or (
            r.get("project_url") if (r.get("project_url") and not is_social(r.get("project_url"))) else ""
        )
        norm_purl = norm_url(purl)
        existing_project = str(r.get("project") or r.get("company") or "").strip()
        pname = existing_project or project_name_from(norm_purl, bio) or ""
        prepared.append((r, bio, cat, norm_purl, pname))
        if fetch and norm_purl and norm_purl not in seen_urls:
            seen_urls.add(norm_purl)
            urls_to_fetch.append(norm_purl)

    fetched_by_url: dict[str, dict] = {}
    if fetch and urls_to_fetch:
        workers = max(1, min(int(max_workers or MAX_ENRICH_WORKERS), len(urls_to_fetch)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for url_key, site_prof in zip(urls_to_fetch, pool.map(fetch_site_profile, urls_to_fetch)):
                fetched_by_url[url_key] = site_prof

    out: list[dict] = []
    for i, (r, bio, cat, norm_purl, pname) in enumerate(prepared, 1):
        site_prof = fetched_by_url.get(norm_purl) or {
            "desc": "",
            "has_monetization_signal": False,
            "has_agency_signal": False,
        }
        desc = site_prof["desc"]
        what = desc or bio_detail(bio)
        detail = bio_detail(bio)
        r2 = dict(r)
        role, roles = merge_roles(r, bio)
        raw_existing_purl = r.get("project_url") or ""
        final_purl = norm_purl or (
            norm_url(raw_existing_purl)
            if raw_existing_purl and not is_social(raw_existing_purl)
            else ""
        )
        clean_name, clean_proj = sanitize_person_and_company(
            r.get("name"),
            pname or r.get("company"),
            site=final_purl,
            bio=bio or what,
        )
        inline_blob = "\n".join(
            str(x)
            for x in (
                bio,
                r.get("what_they_do") or "",
                " ".join(str(u) for u in (r.get("links") or [])),
                final_purl,
            )
            if x
        )
        has_monetization = bool(
            r.get("has_monetization_signal")
            or site_prof["has_monetization_signal"]
            or _SITE_MONETIZATION_RE.search(inline_blob)
        )
        has_agency = bool(
            r.get("has_agency_signal")
            or site_prof["has_agency_signal"]
            or _SITE_AGENCY_RE.search(inline_blob)
        )
        r2.update({
            "name": clean_name if r.get("name") is not None else clean_name,
            "category": cat,
            "role": role,
            "roles": roles,
            "project": clean_proj,
            "project_url": final_purl,
            "what_they_do": what or detail or (r.get("what_they_do") or ""),
            "has_monetization_signal": has_monetization,
            "has_agency_signal": has_agency,
        })
        if "company" in r2:
            r2["company"] = clean_proj
        r2.update(merge_social_fields(r2, extract_social_contacts(r.get("links") or [])))
        out.append(r2)
        tag = f"[{i}/{len(prepared)}]"
        print(f"{tag} {r2.get('name')} | {cat} | {clean_proj or '-'} | {final_purl or '-'}", file=sys.stderr)

    if not skip_copy:
        apply_vlad_sonnet_or_fallback(out)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--fetch", action="store_true", help="fetch project sites")
    p.add_argument("--skip-copy", action="store_true", help="defer LLM copy generation to post-qualify curation")
    p.add_argument("--limit", type=int, default=0, help="debug limit")
    args = p.parse_args()

    rows = [json.loads(l) for l in open(args.input) if l.strip()]
    rows = [r for r in rows if r.get("icp_match")]
    if args.limit:
        rows = rows[:args.limit]

    out = enrich_rows(
        rows,
        fetch=bool(args.fetch),
        max_workers=MAX_ENRICH_WORKERS,
        skip_copy=bool(args.skip_copy),
    )

    with open(args.output, "w") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Enriched {len(out)} leads -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
