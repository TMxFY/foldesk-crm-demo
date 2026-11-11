#!/usr/bin/env python3
"""Enrich scraped TG leads: classify into SoftUGC ЦА categories, extract
project name/url, fetch project site description, draft a personalization hook.

Input: JSONL from tg_scrape_posters.py
Output: enriched JSONL with extra fields.
"""
import argparse
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 8

SOCIAL = ("x.com", "twitter.com", "linkedin.com", "instagram.com", "t.me",
          "telegram.me", "telegram.dog", "youtube.com", "youtu.be", "linktr.ee",
          "linktree", "facebook.com", "fb.com", "medium.com", "calendly.com",
          "notion.site", "notion.so", "github.com", "discord.gg", "discord.com",
          "gmail.com", "beacons.ai", "solo.to", "carrd.co")

# ЦА categories (priority order)
CATEGORIES = [
    ("Prop trading", r"\b(prop[- ]?trad\w*|prop[- ]?firm|funded|challenge|проп)\b"),
    ("CEX / биржа", r"\b(exchange|cex\b|бирж\w*|trading platform|broker)\b"),
    ("DeFi / DEX", r"\b(defi|dex\b|tvl|staking|yield|liquidity|lending|borrow|swap|amm|perp\w*|vault\w*)\b"),
    ("Gaming / AI", r"\b(game\w*|gamefi|p2e|playto|ai[- ]?(agent|app|tool|video|startup)|\bai\b|agent\w*|ml\b|нейросет\w*|metaverse)\b"),
    ("SaaS / AI app", r"\b(saas|b2b app|mobile app|ios app|android app|приложени\w*|app developer|indie\w*|micro[- ]?saas|product[- ]?hunt|software product|subscription app|ai app|ai tool|ai startup|gpt[- ]?app)\b"),
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
    u = url.lower()
    return any(s in u for s in SOCIAL)


def pick_project_url(links):
    for l in links:
        if l.startswith(("http://", "https://", "www.")) and not is_social(l):
            return l
    return ""


def project_name_from(url, bio):
    if url:
        d = re.sub(r"^https?://", "", url)
        d = re.sub(r"^www\.", "", d).split("/")[0]
        name = d.split(".")[0]
        if name:
            return name
    if bio:
        m = re.search(r"(?:founder|co[- ]?founder|ceo|cmo|building|of)\s*@([A-Za-z0-9_]{3,})", bio, re.IGNORECASE)
        if m:
            return "@" + m.group(1)
    return ""


def norm_url(url):
    if not url:
        return ""
    if url.startswith("www."):
        return "https://" + url
    return url


def fetch_desc(url):
    url = norm_url(url)
    if not url:
        return ""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        if "text/html" not in r.headers.get("Content-Type", ""):
            return ""
        soup = BeautifulSoup(r.text[:200000], "html.parser")
        desc = ""
        m = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if m and m.get("content"):
            desc = m["content"].strip()
        title = (soup.title.string.strip() if soup.title and soup.title.string else "")
        out = desc or title
        return out[:220]
    except Exception:
        return ""


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


def merge_roles(row, bio):
    """Keep non-empty roles when an empty bio classifies to nothing.

    A row with no roles yet still gets only classify_role(bio), including [].
    A non-empty classification replaces the previous list.
    """
    classified = classify_role(bio)
    raw = row.get("roles") if isinstance(row, dict) else None
    existing = []
    if isinstance(raw, list):
        existing = [str(item) for item in raw if str(item or "").strip()]
    if existing and not classified and not str(bio or "").strip():
        role = str(row.get("role") or "").strip() or existing[0]
        return role, existing
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
    allow_sonnet = bool(api_key) and (
        "PYTEST_CURRENT_TEST" not in os.environ or os.environ.get("FORCE_AUTO_SONNET_IN_TEST") == "1"
    )

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
            and "plug-and-play growth arm" not in raw_ft.lower()
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--fetch", action="store_true", help="fetch project sites")
    p.add_argument("--limit", type=int, default=0, help="debug limit")
    args = p.parse_args()

    rows = [json.loads(l) for l in open(args.input) if l.strip()]
    rows = [r for r in rows if r.get("icp_match")]
    if args.limit:
        rows = rows[:args.limit]

    out = []
    for i, r in enumerate(rows, 1):
        bio = r.get("bio") or ""
        cat = classify_cat(bio)
        purl = pick_project_url(r.get("links") or [])
        existing_project = str(r.get("project") or "").strip()
        if existing_project and not bio.strip():
            pname = r["project"]
        else:
            pname = project_name_from(purl, bio) or r.get("project") or ""
        desc = fetch_desc(purl) if (args.fetch and purl) else ""
        what = desc or bio_detail(bio)
        detail = bio_detail(bio)
        r2 = dict(r)
        role, roles = merge_roles(r, bio)
        r2.update({
            "category": cat,
            "role": role,
            "roles": roles,
            "project": pname,
            "project_url": norm_url(purl) or norm_url(r.get("project_url") or ""),
            "what_they_do": what or detail or (r.get("what_they_do") or ""),
        })
        r2.update(merge_social_fields(r2, extract_social_contacts(r.get("links") or [])))
        out.append(r2)
        tag = f"[{i}/{len(rows)}]"
        print(f"{tag} {r.get('name')} | {cat} | {pname or '-'} | {purl or '-'}", file=sys.stderr)

    apply_vlad_sonnet_or_fallback(out)

    with open(args.output, "w") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Enriched {len(out)} leads -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
