#!/usr/bin/env python3
"""Attach treg data to leads before they are written to a sheet.

CreatorOS: work email, mailbox check, LinkedIn, plus a weekly US search-volume
and competitor-ad note.
ForexSbor: YouTube size and an X handle when the channel description already
names them, plus the same weekly demand note.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

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
}
CREATOR_KEYWORDS = ["AI UGC", "short form video ads", "prop firm challenge"]
FOREX_KEYWORDS = ["forex trading signals", "crypto trading channel"]
AD_COMPANY = {"creator": "Creatify", "forex": "FTMO"}


def domain_of(url: str) -> str:
    if not url:
        return ""
    raw = url if "://" in url else "https://" + url
    host = (urlparse(raw).hostname or "").lower().removeprefix("www.")
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
        for match in re.finditer(r"https?://[^\s<>\]\)\"']+", text):
            candidate = match.group(0).rstrip(".,;:)")
            dom = domain_of(candidate)
            if dom:
                return f"https://{dom}"
    return ""


def find_company_decision_maker(company: str, domain: str = "") -> dict:
    clean_company = (company or "").strip()
    target = clean_company or domain
    if not target:
        return {}
    payload = call(
        "crawl4ai.web.search",
        query={
            "q": (
                f'site:linkedin.com/in "{target}" '
                "(Founder OR CEO OR CMO OR Marketing OR Growth)"
            ),
        },
    )
    found: dict[str, str] = {}
    if payload:
        linkedin = _first_linkedin(payload)
        if linkedin:
            found["founder_linkedin"] = linkedin
        email = _first_email(payload)
        if email:
            found["work_email"] = email
        if not domain:
            site = _first_project_url(payload)
            if site:
                found["project_url"] = site
    return {k: v for k, v in found.items() if v}


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
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    if output.get("verified") is True and found.get("work_email"):
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
        if row.get("source") == "linkedin_jobs":
            if used < LEAD_CAP and not row.get("founder_linkedin") and not row.get("work_email"):
                company = (row.get("project") or row.get("name") or "").strip()
                if company or domain:
                    found = find_company_decision_maker(company, domain)
                    for k, v in found.items():
                        if v and not row.get(k):
                            row[k] = v
                    used += 1
        else:
            name = person_name(row.get("name") or "")
            if used < LEAD_CAP and name and domain and not row.get("work_email"):
                found = find_email(name, domain)
                row.update(found)
                if row.get("work_email") and not row.get("email_status"):
                    status = verify_email(row["work_email"])
                    if status:
                        row["email_status"] = status
                used += 1
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
