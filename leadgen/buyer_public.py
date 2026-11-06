#!/usr/bin/env python3
"""Public buyer pages that do not need a new account or a join.

A Whop / Content Rewards owner is a lead only with an x.com profile or a
t.me link. Clipper submission posts and the platform's own profile are not
owners. Discord Bloss and @propinfa are skipped when reading them would mean
joining. A failure here must not fail the Telegram batch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess

_X_PROFILE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?=(?:[?#\"'\s<>]|$))",
    re.I,
)
_TG_PROFILE = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})(?=(?:[?#\"'\s<>]|$))",
    re.I,
)
_DENY = {
    "whop", "contentrewards", "softugc", "i",
    "pamba_ai", "karolzdeb", "usefastlane", "gauravsbuilding", "arcads_ai",
    "rom1trs", "creatify_ai", "makeugc", "fyncas",
}
_TG_DENY = {"joinchat", "share", "addstickers", "proxy", "iv"}
DISCOVER_URL = "https://contentrewards.com/c/discover"
_TOKEN_KEYS = ("DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "BLOSS_DISCORD_TOKEN")


def _owner_row(*, handle: str, telegram: str, source_url: str) -> dict:
    x_url = f"https://x.com/{handle}" if handle else ""
    tg_url = f"https://t.me/{telegram}" if telegram else ""
    social = []
    if x_url:
        social.append({"platform": "x", "url": x_url, "handle": handle})
    key = (handle or telegram).casefold()
    row = {
        "id": hashlib.sha256(f"whop:{key}".encode()).hexdigest()[:24],
        "name": (handle or telegram)[:160],
        "username": "",
        "bio": "",
        "sample": "",
        "links": [item for item in (x_url, tg_url) if item],
        "source": "whop",
        "source_url": source_url,
        "group": "whop",
        "contact_url": x_url or tg_url,
        "project": "",
        "project_url": "",
        "what_they_do": "",
        "icp_match": True,
        "founder_x": x_url,
        "founder_twitter": x_url,
        "social_verified": social,
        "social_contacts": social,
        "telegram_candidates": [],
    }
    if telegram:
        row["telegram_verified"] = [{"username": telegram, "url": tg_url}]
    return row


def owner_contacts(html: str, *, source_url: str) -> list[dict]:
    """Owners with a profile link. Status URLs are clipper posts, not owners."""
    found: dict[str, dict[str, str]] = {}
    for match in _X_PROFILE.finditer(html or ""):
        handle = match.group(1)
        if handle.casefold() in _DENY:
            continue
        slot = found.setdefault(handle.casefold(), {"handle": handle, "tg": ""})
        slot["handle"] = handle
    for match in _TG_PROFILE.finditer(html or ""):
        nick = match.group(1)
        if nick.casefold() in _TG_DENY or nick.casefold() in _DENY:
            continue
        slot = found.setdefault(nick.casefold(), {"handle": "", "tg": nick})
        slot["tg"] = nick
    return [
        _owner_row(handle=slot["handle"], telegram=slot["tg"], source_url=source_url)
        for slot in found.values()
        if slot["handle"] or slot["tg"]
    ]


def _curl(url: str) -> str:
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", "25", "-A", "Mozilla/5.0", url],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise OSError("fetch failed")
    return proc.stdout


def whop_rows(fetch_fn=None) -> list[dict]:
    fetch = fetch_fn or _curl
    try:
        html = fetch(DISCOVER_URL)
    except Exception:
        print("whop: skipped (fetch failed)", flush=True)
        return []
    rows = owner_contacts(html or "", source_url=DISCOVER_URL)
    if not rows:
        print("whop: skipped (no owner with x.com or t.me)", flush=True)
    return rows


def bloss_rows() -> list[dict]:
    if not any(os.getenv(key) for key in _TOKEN_KEYS):
        print("discord bloss: skipped (no discord token)", flush=True)
        return []
    print("discord bloss: skipped (no read-without-join path)", flush=True)
    return []


def propinfa_rows() -> list[dict]:
    print("propinfa: skipped (public channel read needs join; not attempted)", flush=True)
    return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows: list[dict] = []
    for loader in (whop_rows, bloss_rows, propinfa_rows):
        try:
            rows.extend(loader())
        except Exception as exc:
            print(f"{loader.__name__}: skipped ({exc.__class__.__name__})", flush=True)
    with open(args.output, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
