#!/usr/bin/env python3
"""Bounded LinkedIn searches for companies hiring AI UGC marketing talent.

Remote and international cards stay unless their location is Russia or CIS.
Casino wording is dropped. Failed searches do not discard other results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys

try:
    from treg_client import call_status
except ImportError:
    try:
        from leadgen.treg_client import call_status
    except ImportError:
        from scripts.treg_client import call_status

ENDPOINT = "crawl4ai.linkedin.search.jobs"
SEARCHES = (
    {"query": "UGC Creator", "posted": "week", "limit": 15},
    {"query": "Creative Strategist", "posted": "week", "limit": 15},
    {"query": "TikTok Content Creator", "posted": "week", "limit": 15},
    {"query": "Performance Creative", "posted": "week", "limit": 15},
    {"query": "Short-form Content Lead", "posted": "week", "limit": 15},
    {"query": "Creative Producer mobile games", "posted": "week", "limit": 15},
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
    if _casino(title, company):
        return None
    return {
        "id": _lead_id(job_id),
        "name": company,
        "project": company,
        "what_they_do": f"{title} {location}",
        "job_url": url,
        "links": [url],
        "roles": ["Marketing"],
        "lead_tier": "A",
        "demand_signal": "hiring_ugc",
        "source": "linkedin_jobs",
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
