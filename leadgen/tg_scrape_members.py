#!/usr/bin/env python3
"""Scrape Telegram group members + bios, filter to CreatorOS ICP, save JSONL.

Safe pacing: gentle per-request delay, respects FloodWait (sleeps the requested
time and slows down), writes incrementally so partial progress is kept.

Usage:
  python3 tg_scrape_members.py --session <path> \
      --groups token2049official,KBW_Official --pool 250 --delay 1.8 \
      --output leads.jsonl
"""
import argparse
import asyncio
import json
import os
import re
import sys

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.users import GetFullUserRequest

ROLE_PATTERNS = [
    ("Founder", r"\b(founder|co[- ]?founder|ceo|owner|основател\w*|соосновател\w*)\b"),
    ("Marketing", r"\b(marketing|growth|cmo|brand|performance|traffic|pr\b|маркет\w*|арбитраж\w*)\b"),
    ("Community", r"\b(community|moderator|модератор\w*|комьюнити)\b"),
    ("Dev", r"\b(developer|dev\b|engineer|solidity|blockchain dev|разработ\w*|fullstack|cto)\b"),
    ("Investor", r"\b(investor|vc\b|venture|fund\b|инвестор\w*|фонд)\b"),
    ("Creator/KOL", r"\b(creator|kol\b|influencer|блогер\w*|контент\w*|ambassador)\b"),
    ("Trader", r"\b(trader|trading|трейдер\w*)\b"),
]

ICP_KEYWORDS = re.compile(
    r"\b(web3|crypto|blockchain|token|defi|nft|gamefi|gaming|launchpad|exchange|"
    r"wallet|dao|dapp|metaverse|airdrop|memecoin|l1|l2|zk|layer\s?2|staking|"
    r"крипт\w*|токен\w*|блокчейн\w*|бирж\w*|кошел\w*)\b",
    re.IGNORECASE,
)

LINK_RE = re.compile(r"(https?://[^\s]+|t\.me/[^\s]+|www\.[^\s]+)")

# Roles we sell to (per SOP we do NOT sell to pure VC/investors)
SELL_ROLES = {"Founder", "Marketing", "Community", "Creator/KOL", "Dev"}


def extract_links(text):
    if not text:
        return []
    return list(dict.fromkeys(LINK_RE.findall(text)))


def categorize(bio):
    if not bio:
        return "Unknown"
    low = bio.lower()
    for role, pat in ROLE_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            return role
    if ICP_KEYWORDS.search(bio):
        return "Project/Web3"
    return "Other"


def is_match(bio, role):
    if role == "Investor":
        return False
    if role in SELL_ROLES:
        return True
    return bool(bio and ICP_KEYWORDS.search(bio))


async def scrape_group(client, group, pool, delay, fh):
    rows = []
    try:
        entity = await client.get_entity(group)
    except Exception as e:
        print(f"# {group}: get_entity failed: {e}", file=sys.stderr)
        return rows

    try:
        participants = await client.get_participants(entity, limit=pool)
    except Exception as e:
        print(f"# {group}: get_participants failed: {e}", file=sys.stderr)
        return rows

    cur_delay = delay
    for i, u in enumerate(participants):
        if getattr(u, "bot", False) or getattr(u, "deleted", False):
            continue
        bio = None
        for attempt in range(3):
            try:
                full = await client(GetFullUserRequest(u))
                bio = full.full_user.about
                break
            except FloodWaitError as fw:
                wait = min(fw.seconds, 120)
                print(f"# {group}: FloodWait {fw.seconds}s -> sleep {wait}s, slow down", file=sys.stderr)
                await asyncio.sleep(wait)
                cur_delay = min(cur_delay * 1.5, 6.0)
            except Exception:
                bio = None
                break
        name = " ".join(p for p in [u.first_name, u.last_name] if p)
        role = categorize(bio)
        row = {
            "group": group,
            "id": u.id,
            "username": getattr(u, "username", None),
            "name": name,
            "bio": bio,
            "links": extract_links(bio),
            "role": role,
            "icp_match": is_match(bio, role),
            "verified": getattr(u, "verified", False),
            "premium": getattr(u, "premium", False),
        }
        rows.append(row)
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        await asyncio.sleep(cur_delay)
    return rows


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--session", required=True)
    p.add_argument("--groups", required=True)
    p.add_argument("--pool", type=int, default=250, help="participant pool per group")
    p.add_argument("--delay", type=float, default=1.8)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")
    if not api_id or not api_hash:
        print("TG_API_ID / TG_API_HASH not set", file=sys.stderr)
        sys.exit(1)

    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    client = TelegramClient(args.session, api_id, api_hash)
    total = 0
    try:
        await client.start()
        with open(args.output, "w") as fh:
            for g in groups:
                print(f"# scraping {g} (pool {args.pool}) ...", file=sys.stderr)
                rows = await scrape_group(client, g, args.pool, args.delay, fh)
                matched = sum(1 for r in rows if r["icp_match"])
                print(f"# {g}: {len(rows)} scraped, {matched} ICP-match", file=sys.stderr)
                total += len(rows)
    finally:
        await client.disconnect()
    print(f"# TOTAL scraped: {total} -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
