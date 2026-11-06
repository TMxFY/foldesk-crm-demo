#!/usr/bin/env python3
"""Enrich scraped TG leads: classify into CreatorOS ЦА categories, extract
project name/url, fetch project site description, draft a personalization hook.

Input: JSONL from tg_scrape_posters.py
Output: enriched JSONL with extra fields.
"""
import argparse
import json
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

HOOKS = {
    "CEX / биржа": "You're already paying KOLs and affiliates for signups - we get the same registrations from organic short-form at a fraction of the cost, production on our own software. Saw {proj}. Want the numbers on one campaign?",
    "Prop trading": "Prop firms live and die by challenge sales. We run clip networks that send qualified traders straight to {proj}'s checkout - organic, not paid. {detail} Want me to show one funnel?",
    "Медиа / контент": "You have a deep archive ({detail}) but none of it reaches short-form. We cut your episodes/AMAs into a daily clip stream across 10 accounts. Want me to cut one episode of {proj} as a demo?",
    "DeFi / DEX": "Your spaces/AMAs never make it to short-form, and TVL needs new users. We cut them into daily clips across 10 accounts pointing to {proj}. Want a demo cut?",
    "Gaming / AI": "Your users live in short video, but you can't produce at that speed by hand. We run AI UGC volume for launches like {proj}. Want examples from your niche?",
    "SaaS / AI app": "If your user growth has hit a ceiling, it's usually not the product - it's the cost of reach. We built an organic distribution system: in-house short-video production + a network of accounts. 183.5M views in 90 days on our last project, zero live shoots. Saw {proj}. Want to see how it's set up for a product like yours?",
    "Web3 (общее)": "Short-form is the cheapest way to put {proj} in front of buyers right now. We produce 1000+ clips a month and post across a 10-account network. {detail} Want examples from your niche?",
}


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
        hook = HOOKS.get(cat, HOOKS["Web3 (общее)"]).format(
            proj=(pname or "your project"), detail=(detail or ""))
        hook = " ".join(hook.split())
        r2 = dict(r)
        role, roles = merge_roles(r, bio)
        r2.update({
            "category": cat,
            "role": role,
            "roles": roles,
            "project": pname,
            "project_url": norm_url(purl) or norm_url(r.get("project_url") or ""),
            "what_they_do": what or (r.get("what_they_do") or ""),
            "hook": hook,
        })
        r2.update(merge_social_fields(r2, extract_social_contacts(r.get("links") or [])))
        out.append(r2)
        tag = f"[{i}/{len(rows)}]"
        print(f"{tag} {r.get('name')} | {cat} | {pname or '-'} | {purl or '-'}", file=sys.stderr)

    with open(args.output, "w") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Enriched {len(out)} leads -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
