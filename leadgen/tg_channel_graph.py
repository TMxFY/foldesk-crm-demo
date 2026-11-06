#!/usr/bin/env python3
"""Recursive Telegram founder channel graph crawler via GetChannelRecommendations.

Crawls similar channels starting from seed founder/SaaS/AI channels in BFS order,
extracts personal founder @username + external product website from channel bio/posts,
and records linked discussion chats into discovered_comment_channels.txt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import deque
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl import functions
from telethon.tl.functions.channels import (
    GetChannelRecommendationsRequest,
    GetFullChannelRequest,
)
from telethon.tl.functions.users import GetFullUserRequest

DEFAULT_SESSION = "/Users/timofei/.claude/skill-telegram/tim3.session"
DEFAULT_STATE_PATH = "leadgen_state/channel_graph_seen.json"
MAX_SUBSCRIBERS = 100_000

CASINO_SPAM_RE = re.compile(
    r"\b(казино|casino|slots|слоты|ставки|betting|1win|1xbet|mostbet|фриспин\w*|"
    r"freespin\w*|лудоман\w*|букмекер\w*|розыгрыш\w*|халяв\w*|мемы\s+дня)\b",
    re.IGNORECASE,
)

AD_LINE_RE = re.compile(
    r"(по\s+всем\s+вопросам\s+реклам\w*|по\s+реклам\w*|менеджер\s+по\s+реклам\w*|"
    r"ad\s*manager|for\s+ads|buying\s+ads|реклама\s*[:—\-]|pr\s*manager)",
    re.IGNORECASE,
)

FOUNDER_HINT_RE = re.compile(
    r"(основател\w*|соосновател\w*|автор\w*|фаундер\w*|founder|co[- ]?founder|"
    r"ceo|owner|creator|built\s+by|making|building|связь|лс|личк\w*|dm\b|contact)",
    re.IGNORECASE,
)

HANDLE_RE = re.compile(
    r"(?:@|(?:https?://)?(?:t\.me|telegram\.me)/)([A-Za-z][A-Za-z0-9_]{3,31})\b",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+", re.IGNORECASE)

SKIP_SITE_HOSTS = {
    "t.me",
    "telegram.me",
    "telegram.org",
    "telegra.ph",
    "x.com",
    "twitter.com",
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "vk.com",
    "vc.ru",
    "habr.com",
    "medium.com",
    "linkedin.com",
    "facebook.com",
    "bit.ly",
    "t.co",
}

NOISE_HANDLES = {
    "joinchat",
    "addstickers",
    "share",
    "proxy",
    "socks",
    "iv",
    "boost",
    "s",
    "c",
}


def is_bot_handle(username: str) -> bool:
    low = (username or "").strip().lstrip("@").lower()
    return not low or low in NOISE_HANDLES or low.endswith("bot") or low.endswith("_bot")


def is_ad_manager_handle(username: str, line: str) -> bool:
    low = (username or "").lower()
    if any(tok in low for tok in ("ad_manager", "ads_", "_ads", "_pr", "reklama", "pr_manager")):
        if not FOUNDER_HINT_RE.search(line or ""):
            return True
    if AD_LINE_RE.search(line or "") and not FOUNDER_HINT_RE.search(line or ""):
        return True
    return False


def extract_external_site(texts: list[str]) -> str:
    for text in texts:
        for raw_url in URL_RE.findall(text or ""):
            cleaned = raw_url.rstrip(".,;:!?)\"'>]")
            try:
                parsed = urlparse(cleaned)
            except Exception:
                continue
            host = (parsed.netloc or "").lower().removeprefix("www.")
            if not host or "." not in host or host in SKIP_SITE_HOSTS:
                continue
            if any(host.endswith("." + sk) for sk in SKIP_SITE_HOSTS):
                continue
            return cleaned
    return ""


def parse_channel_founder_info(
    about: str,
    posts: list[str],
    channel_username: str = "",
    channel_title: str = "",
    subscribers: int = 0,
) -> dict | None:
    """Extract personal founder @username and external site from channel bio & posts."""
    if subscribers and int(subscribers) > MAX_SUBSCRIBERS:
        return None

    combined_text = "\n".join([channel_title or "", about or ""] + list(posts or []))
    if CASINO_SPAM_RE.search(combined_text):
        return None

    chan_low = (channel_username or "").strip().lstrip("@").lower()
    preferred_candidates: list[str] = []
    fallback_candidates: list[str] = []

    sources = [about or ""] + list(posts or [])
    for src_idx, block in enumerate(sources):
        for line in (block or "").splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            for match in HANDLE_RE.finditer(line_stripped):
                handle = match.group(1).strip()
                low = handle.lower()
                if low == chan_low or is_bot_handle(low):
                    continue
                if is_ad_manager_handle(low, line_stripped):
                    continue
                if FOUNDER_HINT_RE.search(line_stripped) or src_idx == 0:
                    if handle not in preferred_candidates:
                        preferred_candidates.append(handle)
                else:
                    if handle not in fallback_candidates:
                        fallback_candidates.append(handle)

    candidates = preferred_candidates or fallback_candidates
    if not candidates:
        return None

    owner_username = candidates[0]
    site = extract_external_site([about or ""] + list(posts or []))
    return {
        "username": owner_username,
        "site": site,
    }


def _load_seen(path: str) -> set[str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x).lower() for x in data}
        if isinstance(data, dict):
            return {str(x).lower() for x in data.get("seen_channels", [])}
    except (OSError, ValueError):
        pass
    return set()


def _save_seen(path: str, seen: set[str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seen_channels": sorted(seen)}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _append_linked_chat(linked_chats_path: str, linked_chat_id: int | None) -> None:
    if not linked_chat_id:
        return
    token = f"id{int(linked_chat_id)}"
    os.makedirs(os.path.dirname(os.path.abspath(linked_chats_path)), exist_ok=True)
    existing: set[str] = set()
    if os.path.exists(linked_chats_path):
        try:
            with open(linked_chats_path, encoding="utf-8") as f:
                existing = {line.strip() for line in f if line.strip()}
        except OSError:
            pass
    if token not in existing:
        with open(linked_chats_path, "a", encoding="utf-8") as f:
            f.write(token + "\n")


async def _resolve_user_profile(client, username: str) -> dict:
    try:
        res = await client(GetFullUserRequest(username))
        user = res.users[0] if getattr(res, "users", None) else None
        if user and getattr(user, "bot", False):
            return {"bot": True}
        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        full_name = " ".join(p for p in (first, last) if p).strip()
        user_bio = getattr(getattr(res, "full_user", None), "about", "") or ""
        return {
            "id": getattr(user, "id", None),
            "name": full_name,
            "user_bio": user_bio,
            "bot": False,
        }
    except Exception:
        return {"id": None, "name": "", "user_bio": "", "bot": False}


async def crawl_channel_graph(
    client,
    seeds: list[str],
    depth: int = 2,
    limit: int = 30,
    state_path: str = DEFAULT_STATE_PATH,
    output_path: str = "channel_graph_leads.jsonl",
    linked_chats_path: str | None = None,
    delay: float = 1.0,
) -> list[dict]:
    if not linked_chats_path:
        state_dir = os.path.dirname(os.path.abspath(state_path))
        linked_chats_path = os.path.join(state_dir, "discovered_comment_channels.txt")

    seen_channels = _load_seen(state_path)
    seen_founders: set[str] = set()
    queue: deque[tuple[object, int]] = deque()
    queued_keys: set[str] = set()

    for s in seeds:
        clean = s.strip().lstrip("@")
        if clean and clean.lower() not in queued_keys:
            queue.append((clean, 0))
            queued_keys.add(clean.lower())

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    discovered_rows: list[dict] = []

    with open(output_path, "w", encoding="utf-8") as out_fh:
        while queue and len(discovered_rows) < limit:
            target, cur_depth = queue.popleft()
            target_key = (
                getattr(target, "username", None)
                or (f"id{target.id}" if hasattr(target, "id") else str(target))
            )
            target_key = str(target_key).lstrip("@").lower()

            try:
                entity = target if hasattr(target, "id") else await client.get_entity(target)
            except FloodWaitError as fw:
                await asyncio.sleep(min(fw.seconds, 60))
                continue
            except Exception:
                continue

            ch_username = (getattr(entity, "username", None) or target_key).lstrip("@")
            ch_key = ch_username.lower()

            if ch_key not in seen_channels:
                seen_channels.add(ch_key)
                try:
                    full = await client(GetFullChannelRequest(channel=entity))
                    about = getattr(full.full_chat, "about", "") or ""
                    chat_obj = full.chats[0] if getattr(full, "chats", None) else entity
                    ch_title = getattr(chat_obj, "title", "") or getattr(entity, "title", "") or ch_username
                    ch_username = getattr(chat_obj, "username", None) or ch_username
                    subscribers = (
                        getattr(full.full_chat, "participants_count", None)
                        or getattr(chat_obj, "participants_count", None)
                        or 0
                    )
                    linked_chat_id = getattr(full.full_chat, "linked_chat_id", None)

                    msgs = await client.get_messages(entity, limit=3)
                    post_texts = [
                        (getattr(m, "message", None) or "").strip()
                        for m in (msgs or [])
                        if (getattr(m, "message", None) or "").strip()
                    ]

                    parsed = parse_channel_founder_info(
                        about=about,
                        posts=post_texts,
                        channel_username=ch_username,
                        channel_title=ch_title,
                        subscribers=subscribers,
                    )
                    if parsed:
                        _append_linked_chat(linked_chats_path, linked_chat_id)
                        founder_uname = parsed["username"].lstrip("@")
                        if founder_uname.lower() not in seen_founders:
                            uinfo = await _resolve_user_profile(client, founder_uname)
                            if not uinfo.get("bot"):
                                seen_founders.add(founder_uname.lower())
                                site = parsed["site"]
                                combined_bio = about.strip()
                                if uinfo.get("user_bio") and uinfo["user_bio"] not in combined_bio:
                                    combined_bio = f"{uinfo['user_bio']} | {combined_bio}".strip(" |")
                                links = [f"https://t.me/{founder_uname}"]
                                if site:
                                    links.insert(0, site)
                                row = {
                                    "id": uinfo.get("id") or getattr(entity, "id", 0),
                                    "username": founder_uname,
                                    "name": uinfo.get("name") or ch_title,
                                    "bio": combined_bio,
                                    "site": site,
                                    "project_url": site,
                                    "source": "tg_channel_graph",
                                    "channel_username": ch_username,
                                    "channel_title": ch_title,
                                    "subscribers": int(subscribers or 0),
                                    "sample_messages": post_texts[:3] or [about],
                                    "contact_channels": ["telegram"],
                                    "links": links,
                                }
                                discovered_rows.append(row)
                                out_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                                out_fh.flush()
                except FloodWaitError as fw:
                    await asyncio.sleep(min(fw.seconds, 60))
                except Exception as exc:
                    print(f"# channel_graph error on {ch_username}: {exc}", file=sys.stderr)

            if cur_depth < depth and len(discovered_rows) < limit:
                try:
                    recs = await client(GetChannelRecommendationsRequest(channel=entity))
                    for rec_chat in getattr(recs, "chats", []) or []:
                        rec_uname = (
                            getattr(rec_chat, "username", None)
                            or f"id{getattr(rec_chat, 'id', '')}"
                        ).lower()
                        if rec_uname and rec_uname not in seen_channels and rec_uname not in queued_keys:
                            queued_keys.add(rec_uname)
                            queue.append((rec_chat, cur_depth + 1))
                except Exception as exc:
                    print(f"# recommendations error on {ch_username}: {exc}", file=sys.stderr)

            if delay > 0:
                await asyncio.sleep(delay)

    _save_seen(state_path, seen_channels)
    return discovered_rows


async def _async_main(args: argparse.Namespace) -> None:
    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")
    if not api_id or not api_hash:
        print("TG_API_ID / TG_API_HASH not set", file=sys.stderr)
        sys.exit(1)

    raw_seeds = args.seeds or os.getenv("TG_GRAPH_SEED_CHANNELS", "")
    seeds = [s.strip() for s in raw_seeds.split(",") if s.strip()]
    if not seeds:
        print("No seed channels provided via --seeds or TG_GRAPH_SEED_CHANNELS", file=sys.stderr)
        sys.exit(1)

    client = TelegramClient(args.session, api_id, api_hash)
    await client.start()
    try:
        rows = await crawl_channel_graph(
            client=client,
            seeds=seeds,
            depth=args.depth,
            limit=args.limit,
            state_path=args.state,
            output_path=args.output,
            delay=args.delay,
        )
        print(f"# tg_channel_graph: discovered {len(rows)} founder leads -> {args.output}", file=sys.stderr)
    finally:
        await client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description="Recursive Telegram founder channel graph crawler")
    ap.add_argument("--session", default=DEFAULT_SESSION)
    ap.add_argument("--seeds", default=os.getenv("TG_GRAPH_SEED_CHANNELS", ""))
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--state", "--seen", dest="state", default=DEFAULT_STATE_PATH)
    ap.add_argument("--output", required=True)
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
