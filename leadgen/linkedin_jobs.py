#!/usr/bin/env python3
"""Bounded LinkedIn searches for companies hiring AI UGC marketing talent.

Remote and international cards stay unless their location is Russia or CIS.
Casino wording is dropped. Failed searches do not discard other results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any, Mapping
from urllib.parse import urlparse

try:
    from treg_client import call_status
except ImportError:
    try:
        from leadgen.treg_client import call_status
    except ImportError:
        from scripts.treg_client import call_status

try:
    from treg_enrich import domain_of as _treg_domain_of, infer_company_domain as _treg_infer_company_domain
except ImportError:
    try:
        from leadgen.treg_enrich import domain_of as _treg_domain_of, infer_company_domain as _treg_infer_company_domain
    except ImportError:
        _treg_domain_of = None
        _treg_infer_company_domain = None

ENDPOINT = "crawl4ai.linkedin.search.jobs"
SEARCHES = (
    {"query": "UGC Creator", "posted": "week", "limit": 15},
    {"query": "Creative Strategist", "posted": "week", "limit": 15},
    {"query": "TikTok Content Creator", "posted": "week", "limit": 15},
    {"query": "Performance Creative", "posted": "week", "limit": 15},
    {"query": "Short-form Content Lead", "posted": "week", "limit": 15},
    {"query": "TikTok Shop Manager", "posted": "week", "limit": 15},
    {"query": "User Acquisition Manager", "posted": "week", "limit": 15},
    {"query": "Influencer Marketing Manager", "posted": "week", "limit": 15},
    {"query": "Music Marketing Manager", "posted": "week", "limit": 15},
)
CAP = 24

_BLOCKED_LOCATIONS = (
    "russia",
    "russian federation",
    "belarus",
    "ukraine",
    "kazakhstan",
    "uzbekistan",
    "armenia",
    "azerbaijan",
    "georgia",
    "moldova",
    "kyrgyzstan",
    "tajikistan",
    "turkmenistan",
)
_CASINO = ("casino", "slots", "betting", "igaming", "poker", "gambling")
_GAMING_BAN = ("roblox", "fortnite", "fivem", "mobile games", "mobile game", "game studio")
_JOB_BOARD_AND_FREE_HOSTS = frozenset({
    "linkedin.com",
    "licdn.com",
    "indeed.com",
    "glassdoor.com",
    "wellfound.com",
    "angel.co",
    "ziprecruiter.com",
    "monster.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "bamboohr.com",
    "smartrecruiters.com",
    "workday.com",
    "myworkdayjobs.com",
    "icims.com",
    "recruitee.com",
    "breezy.hr",
    "jobvite.com",
    "applytojob.com",
    "rippling.com",
    "t.me",
    "telegram.me",
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "google.com",
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "icloud.com",
    "me.com",
    "proton.me",
    "protonmail.com",
    "aol.com",
    "mail.ru",
    "yandex.ru",
    "yandex.com",
})
_URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+", re.I)
_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@([A-Z0-9.\-]+\.[A-Z]{2,})", re.I)


def _lead_id(job_id: str) -> str:
    return hashlib.sha256(f"linkedin_jobs:{job_id}".encode()).hexdigest()[:24]


def _text(value) -> str:
    return str(value or "").strip()


def _allowed_location(location: str) -> bool:
    folded = location.casefold()
    return bool(folded) and not any(token in folded for token in _BLOCKED_LOCATIONS)


def _casino(title: str, company: str) -> bool:
    blob = f"{title} {company}".casefold()
    return any(word in blob for word in _CASINO)


def _banned_niche(title: str, company: str) -> bool:
    blob = f"{title} {company}".casefold()
    return _casino(title, company) or any(word in blob for word in _GAMING_BAN)


def _clean_company_domain(raw_url_or_domain: str) -> str:
    raw = _text(raw_url_or_domain).rstrip(".,;:)")
    if not raw:
        return ""
    candidate = _treg_domain_of(raw) if _treg_domain_of is not None else ""
    if not candidate:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        candidate = (parsed.hostname or "").lower().removeprefix("www.")
    if not candidate or "." not in candidate:
        return ""
    if any(candidate == skip or candidate.endswith("." + skip) for skip in _JOB_BOARD_AND_FREE_HOSTS):
        return ""
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", candidate):
        return ""
    return candidate


def extract_company_domain_hint(card: Mapping[str, Any]) -> tuple[str, str]:
    """Extract `(company_domain, project_url)` from a LinkedIn job card when available."""
    for field in (
        "project_url",
        "company_url",
        "company_website",
        "website",
        "site",
        "company_domain",
        "domain",
    ):
        raw = _text(card.get(field))
        dom = _clean_company_domain(raw)
        if dom:
            if raw.lower().startswith(("http://", "https://")):
                return dom, raw.rstrip("/")
            return dom, f"https://{dom}"

    links = card.get("links")
    if isinstance(links, (list, tuple)):
        for item in links:
            raw = _text(item)
            dom = _clean_company_domain(raw)
            if dom:
                url = raw if raw.lower().startswith(("http://", "https://")) else f"https://{dom}"
                return dom, url.rstrip("/")

    for text_field in ("description", "company_description", "summary", "about"):
        blob = _text(card.get(text_field))
        if not blob:
            continue
        for m in _URL_RE.finditer(blob):
            dom = _clean_company_domain(m.group(0))
            if dom:
                return dom, f"https://{dom}"

    for email_field in ("email", "contact_email", "work_email", "description", "summary"):
        blob = _text(card.get(email_field))
        if not blob:
            continue
        for m in _EMAIL_RE.finditer(blob):
            dom = _clean_company_domain(m.group(1))
            if dom:
                return dom, f"https://{dom}"

    company = _text(card.get("company"))
    if company:
        inferred = (
            _treg_infer_company_domain(company)
            if _treg_infer_company_domain is not None
            else (
                _clean_company_domain(company)
                if " " not in company and re.fullmatch(r"(?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", company, re.I)
                else ""
            )
        )
        dom = _clean_company_domain(inferred)
        if dom:
            return dom, f"https://{dom}"

    return "", ""


def _lead(card: dict) -> dict | None:
    title = _text(card.get("title"))
    company = _text(card.get("company"))
    location = _text(card.get("location"))
    url = _text(card.get("url"))
    job_id = _text(card.get("job_id"))
    if not (title and company and location and url and job_id):
        return None
    if not _allowed_location(location):
        return None
    if _banned_niche(title, company):
        return None
    company_domain, project_url = extract_company_domain_hint(card)
    links = [url]
    if project_url and project_url != url:
        links.append(project_url)
    return {
        "id": _lead_id(job_id),
        "name": company,
        "company": company,
        "project": company,
        "what_they_do": f"{title} {location}",
        "job_url": url,
        "company_domain": company_domain,
        "project_url": project_url,
        "links": links,
        "roles": ["Marketing"],
        "lead_tier": "A",
        "demand_signal": "hiring_ugc",
        "source": "linkedin_jobs",
        "segment": "vacancy",
        "language": "en",
        "founder_x": "",
        "username": "",
    }


def rows_from_payload(payload, seen: set[str] | None = None) -> list[dict]:
    """Lead rows from one jobs payload. No network."""
    if not isinstance(payload, dict):
        return []
    cards = payload.get("rows")
    if not isinstance(cards, list):
        return []
    leads = []
    seen = set() if seen is None else seen
    for card in cards:
        if not isinstance(card, dict):
            continue
        lead = _lead(card)
        if lead is None or lead["id"] in seen:
            continue
        seen.add(lead["id"])
        leads.append(lead)
        if len(seen) >= CAP:
            break
    return leads


def _payload_from_status(endpoint, *, method=None, data=None, **_ignored):
    """One jobs body. Raises unless call_status returned HTTP 200 and a dict."""
    status, body = call_status(endpoint, method=method, data=data, timeout=90)
    if status == 200 and isinstance(body, dict):
        return body
    raise RuntimeError("linkedin jobs search failed")


def collect(call=None) -> list[dict]:
    """Run all searches, deduplicate by job id, and enforce one total cap."""
    caller = _payload_from_status if call is None else call
    leads = []
    seen: set[str] = set()
    for search in SEARCHES:
        if len(seen) >= CAP:
            break
        try:
            payload = caller(ENDPOINT, method="POST", data=dict(search))
        except Exception:
            continue
        leads.extend(rows_from_payload(payload, seen))
    return leads


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        rows = collect()
    except Exception:
        rows = []
    with open(args.output, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if not rows:
        print("linkedin jobs: 0", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
