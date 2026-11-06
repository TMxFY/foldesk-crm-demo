#!/usr/bin/env python3
"""Scrape Telegram message authors + their bios.

The production pipeline uses message authors because they produce more ICP
leads in measured runs. ``--mode members`` is available as an optional source
for groups that expose their participant list.

Usage:
  python3 tg_scrape_posters.py --session <path> --groups token2049official \
      --mode posters --msg-limit 400 --delay 0.5 --output leads.jsonl
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, InputPeerChannel, PeerChannel

try:
    from outreach_copy import dm_allowed
    from tg_to_gsheet import first_touch_for
except ImportError:
    try:
        from leadgen.outreach_copy import dm_allowed
        from leadgen.tg_to_gsheet import first_touch_for
    except ImportError:
        from scripts.outreach_copy import dm_allowed
        from scripts.tg_to_gsheet import first_touch_for

_DEFAULT_STATE_DIR = os.environ.get("LEADGEN_STATE_DIR") or (
    "/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline"
    if os.path.isdir("/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline")
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "leadgen_state")
)
ENTITY_CACHE_PATH = os.path.join(_DEFAULT_STATE_DIR, "entity_cache.json")
PROFILE_CACHE_PATH = os.environ.get(
    "TG_PROFILE_CACHE_PATH", os.path.join(_DEFAULT_STATE_DIR, "profile_cache.json")
)
CURSOR_PATH = os.environ.get(
    "TG_CURSOR_PATH", os.path.join(_DEFAULT_STATE_DIR, "group_cursors.json")
)


def load_json_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json_state(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"# warning: could not save state {path}: {e}", file=sys.stderr)


def load_entity_cache():
    """Load username -> {channel_id, access_hash, title} cache; tolerant of missing/corrupt file."""
    try:
        with open(ENTITY_CACHE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_entity_cache(cache):
    """Persist entity cache atomically-ish (write tmp + rename)."""
    try:
        os.makedirs(os.path.dirname(ENTITY_CACHE_PATH), exist_ok=True)
        tmp = ENTITY_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ENTITY_CACHE_PATH)
    except Exception as e:
        print(f"# warning: could not save entity cache: {e}", file=sys.stderr)


async def resolve_entity(client, group, entity_cache):
    """Return an entity usable for get_messages without re-resolving the username.

    Uses cached InputPeerChannel when possible; falls back to get_entity once
    (and caches the result) when the cache is missing or stale.
    """
    cached = entity_cache.get(group)
    if cached:
        try:
            return InputPeerChannel(int(cached["channel_id"]), int(cached["access_hash"]))
        except (KeyError, TypeError, ValueError):
            entity_cache.pop(group, None)  # corrupt entry -> drop and re-resolve

    try:
        if group.startswith("id") and group[2:].isdigit():
            entity = await client.get_entity(PeerChannel(int(group[2:])))
        else:
            entity = await client.get_entity(group)
    except FloodWaitError as fw:
        # Username-resolve rate-limited: skip this group, don't crash the run.
        print(f"# {group}: resolve flood-wait {fw.seconds}s -> skip group", file=sys.stderr)
        return None
    except Exception as e:
        print(f"# {group}: get_entity failed: {e}", file=sys.stderr)
        return None
    if isinstance(entity, Channel):
        entity_cache[group] = {
            "channel_id": entity.id,
            "access_hash": entity.access_hash,
            "title": getattr(entity, "title", None),
        }
        save_entity_cache(entity_cache)
    return entity

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
    r"wallet|dao|dapp|metaverse|airdrop|memecoin|staking|"
    r"крипт\w*|токен\w*|блокчейн\w*|бирж\w*|кошел\w*)\b",
    re.IGNORECASE,
)
LINK_RE = re.compile(r"(https?://[^\s]+|t\.me/[^\s]+|www\.[^\s]+)")
SELL_ROLES = {"Founder", "Marketing", "Community", "Creator/KOL", "Dev"}


def extract_links(text):
    return list(dict.fromkeys(LINK_RE.findall(text))) if text else []


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


def is_match(bio, role, sample=""):
    """Cheap admission into the Jev gate.

    Crypto keywords are not the decision. Anyone who left a real bio, a link,
    or a substantive message is passed to Jev. Investors and empty profiles
    are dropped here so Jev is not spent on them.
    """
    if role == "Investor":
        return False
    text = (bio or "").strip()
    snippet = (sample or "").strip()
    if extract_links(text) or extract_links(snippet):
        return True
    if role in SELL_ROLES and (len(text) >= 8 or len(snippet) >= 20):
        return True
    if len(text) >= 20 or len(snippet) >= 40:
        return True
    return False


async def scrape_group(client, group, msg_limit, delay, fh, exclude_paths,
                       mode="members", member_limit=100,
                       send=False, send_delay=45.0, max_send=0, send_state=None,
                       entity_cache=None, profile_cache=None, cursors=None):
    if entity_cache is None:
        entity_cache = {}
    if profile_cache is None:
        profile_cache = {}
    if cursors is None:
        cursors = {}
    entity = await resolve_entity(client, group, entity_cache)
    if entity is None:
        return []

    # Shared state for the sender loop below.  In posters mode these are
    # filled from scraped messages; in members mode they stay empty, but the
    # profile-failure paths still touch them, so they must exist either way.
    message_ids_by_sender = {}
    sample_by_sender = {}
    failed_message_ids = set()

    # Participant enumeration is the primary source.  Some broadcast/super
    # groups hide members; callers can use --mode posters as a fallback.
    if mode == "members":
        try:
            participants = []
            async for user in client.iter_participants(entity, limit=member_limit):
                participants.append(user)
            if not participants:
                print(f"# {group}: Telegram returned no visible participants (not a member-enumerable group)", file=sys.stderr)
        except FloodWaitError as fw:
            print(f"# {group}: participant FloodWait {fw.seconds}s -> skip group", file=sys.stderr)
            return []
        except Exception as e:
            print(f"# {group}: participant list unavailable ({e}); trying message authors", file=sys.stderr)
            mode = "posters"
    if mode == "posters":
        previous_cursor = int(cursors.get(group, 0) or 0)
        try:
            if previous_cursor:
                messages = [m async for m in client.iter_messages(
                    entity,
                    min_id=previous_cursor,
                    limit=msg_limit,
                    reverse=True,
                )]
                print(
                    f"# {group}: reading only messages newer than {previous_cursor} "
                    "(oldest first)",
                    file=sys.stderr,
                )
            else:
                messages = await client.get_messages(entity, limit=msg_limit)
        except Exception as e:
            # Cached InputPeerChannel may be stale (access_hash rotated / kicked) —
            # drop it and retry once with a fresh username resolve.
            if group in entity_cache:
                entity_cache.pop(group, None)
                save_entity_cache(entity_cache)
                print(f"# {group}: cached entity failed ({e}); re-resolving", file=sys.stderr)
                entity = await resolve_entity(client, group, entity_cache)
                if entity is None:
                    return []
                try:
                    if previous_cursor:
                        messages = [m async for m in client.iter_messages(
                            entity,
                            min_id=previous_cursor,
                            limit=msg_limit,
                            reverse=True,
                        )]
                    else:
                        messages = await client.get_messages(entity, limit=msg_limit)
                except Exception as e2:
                    print(f"# {group}: message read failed after re-resolve: {e2}", file=sys.stderr)
                    return []
            else:
                print(f"# {group}: message read failed: {e}", file=sys.stderr)
                return []
        pending_cursor = max(
            [int(getattr(m, "id", 0) or 0) for m in messages] or
            [previous_cursor]
        )

        # Keep the message IDs behind each *user* sender.  Channel/anonymous
        # posts are not person leads and can never be resolved with
        # GetFullUserRequest; treating them as retryable would pin the cursor
        # forever on one permanent non-user message.
        for message in messages:
            sid = getattr(message, "sender_id", None)
            mid = int(getattr(message, "id", 0) or 0)
            from_id = getattr(message, "from_id", None)
            is_non_user = bool(getattr(message, "post", False)) or (
                from_id is not None
                and from_id.__class__.__name__ in {"PeerChannel", "PeerChat"}
            )
            if sid is not None and mid and not is_non_user:
                message_ids_by_sender.setdefault(sid, []).append(mid)
                text = (getattr(message, "message", None) or "").strip()
                if text:
                    prev_id, _prev = sample_by_sender.get(sid, (0, ""))
                    if mid >= prev_id:
                        sample_by_sender[sid] = (mid, text[:280])

    # dedupe against previously scraped ids
    exclude = set()
    if exclude_paths:
        for path in exclude_paths:
            try:
                for line in open(path):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        exclude.add(json.loads(line).get("id"))
                    except (json.JSONDecodeError, AttributeError):
                        exclude.add(int(line))
            except FileNotFoundError:
                pass
    print(f"# {group}: excluding {len(exclude)} already-scraped ids", file=sys.stderr)

    if mode == "members":
        sender_ids = [u.id for u in participants if getattr(u, "id", None) not in exclude]
        user_by_id = {u.id: u for u in participants}
        print(f"# {group}: {len(sender_ids)} participants", file=sys.stderr)
    else:
        # unique sender ids, preserving order (most recent first), skip channel/anon posts
        sender_ids = []
        seen = set()
        for m in messages:
            sid = m.sender_id
            if sid is None or sid in seen or sid in exclude:
                continue
            seen.add(sid)
            sender_ids.append(sid)
        user_by_id = {getattr(m, "sender_id", None): getattr(m, "sender", None)
                      for m in messages if getattr(m, "sender_id", None) is not None}
        print(f"# {group}: {len(messages)} msgs -> {len(sender_ids)} unique senders", file=sys.stderr)

    rows = []
    cur_delay = delay
    for sid in sender_ids:
        cache_key = str(sid)
        cached = profile_cache.get(cache_key)
        cache_ttl = max(60, int(os.environ.get("TG_PROFILE_CACHE_TTL", "604800")))
        profile = cached.get("profile") if isinstance(cached, dict) else None
        fresh = (profile and time.time() - float(cached.get("fetched_at", 0)) < cache_ttl)
        user = user_by_id.get(sid)
        try:
            if not fresh:
                full = await client(GetFullUserRequest(sid))
                user = next((u for u in full.users if getattr(u, "id", None) == sid), user)
                if user is None:
                    failed_message_ids.update(message_ids_by_sender.get(sid, []))
                    continue
                profile = {
                    "id": sid, "username": getattr(user, "username", None),
                    "first_name": getattr(user, "first_name", None),
                    "last_name": getattr(user, "last_name", None),
                    "bio": full.full_user.about or "",
                    "bot": bool(getattr(user, "bot", False)),
                    "deleted": bool(getattr(user, "deleted", False)),
                    "verified": bool(getattr(user, "verified", False)),
                    "premium": bool(getattr(user, "premium", False)),
                }
                profile_cache[cache_key] = {"fetched_at": time.time(), "profile": profile}
        except FloodWaitError as fw:
            failed_message_ids.update(message_ids_by_sender.get(sid, []))
            wait = min(fw.seconds, 120)
            print(f"# {group}: FloodWait {fw.seconds}s -> sleep {wait}s", file=sys.stderr)
            await asyncio.sleep(wait)
            cur_delay = min(cur_delay * 1.5, 6.0)
            continue
        except Exception:
            failed_message_ids.update(message_ids_by_sender.get(sid, []))
            continue  # retry inaccessible/transient profile lookups next run

        if not profile:
            failed_message_ids.update(message_ids_by_sender.get(sid, []))
            continue
        if profile.get("bot") or profile.get("deleted"):
            continue
        bio = profile.get("bio", "")
        name = " ".join(p for p in [profile.get("first_name"), profile.get("last_name")] if p)
        username = profile.get("username")
        role = categorize(bio)
        sample = ""
        if mode == "posters":
            sample = sample_by_sender.get(sid, (0, ""))[1]
        row = {
            "group": group,
            "id": sid,
            "username": username,
            "name": name,
            "bio": bio,
            "sample": sample,
            "links": extract_links(bio) or extract_links(sample),
            "role": role,
            "icp_match": is_match(bio, role, sample),
            "verified": profile.get("verified", False),
            "premium": profile.get("premium", False),
        }
        rows.append(row)
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()

        # optional: send personalized first-touch to ICP matches with a username
        if (send and row["icp_match"] and row["username"] and user
                and send_state is not None and row["id"] not in send_state["sent"]
                and (max_send == 0 or send_state["count"] < max_send)):
            text = build_message(row)
            ok = await try_send(client, user, text, send_state)
            row["message_sent"] = ok
            row["message_text"] = text if ok else None
            # rewrite the line we just wrote with send status
            fh.write(json.dumps({"_update_sent": row["id"], "sent": ok}) + "\n")
            fh.flush()
            await asyncio.sleep(send_delay)

        await asyncio.sleep(cur_delay)
    # Advance the cursor only after the whole group completed.  If Telegram
    # FloodWait or a transient profile failure interrupts the group, the next
    # run retries those messages instead of silently skipping them.
    if mode == "posters":
        if failed_message_ids:
            safe_cursor = min(failed_message_ids) - 1
            cursors[group] = max(previous_cursor, safe_cursor)
            print(
                f"# {group}: {len(failed_message_ids)} message(s) retryable; "
                f"cursor held at {cursors[group]}",
                file=sys.stderr,
            )
        else:
            cursors[group] = max(previous_cursor, pending_cursor)
    return rows


def build_message(row):
    """Original first touch: category pain, or a Latin angle when it exists."""
    product = row.get("project") or row.get("name") or ""
    return first_touch_for(row.get("category") or "", product, row.get("their_angle") or "")


async def try_send(client, user, text, send_state):
    if not dm_allowed(user):
        return False
    try:
        await client.send_message(user, text)
        send_state["sent"].add(user.id)
        send_state["count"] += 1
        print(f"#   -> sent to @{getattr(user,'username',user.id)}", file=sys.stderr)
        return True
    except FloodWaitError as fw:
        wait = min(fw.seconds, 300)
        print(f"#   FloodWait on send {fw.seconds}s -> sleep {wait}s", file=sys.stderr)
        await asyncio.sleep(wait)
        return False
    except Exception as e:
        print(f"#   send failed to {getattr(user,'username',user.id)}: {e}", file=sys.stderr)
        return False


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--session", required=True)
    p.add_argument("--groups", required=True)
    p.add_argument("--msg-limit", type=int, default=400)
    p.add_argument("--mode", choices=("members", "posters"), default="posters",
                   help="posters = message authors (default); members = participant list")
    p.add_argument("--member-limit", type=int, default=100)
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--output", required=True)
    p.add_argument("--append", action="store_true", help="append to --output instead of replacing it")
    p.add_argument("--exclude", action="append", default=[],
                   help="jsonl files with previously scraped ids to skip")
    p.add_argument("--send", action="store_true", help="DM ICP matches")
    p.add_argument("--send-delay", type=float, default=45.0)
    p.add_argument("--max-send", type=int, default=0, help="0 = no cap")
    p.add_argument("--sent-file", default="/tmp/tg_sent_ids.json",
                   help="tracks already-messaged user ids")
    args = p.parse_args()

    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")
    if not api_id or not api_hash:
        print("TG_API_ID / TG_API_HASH not set", file=sys.stderr)
        sys.exit(1)

    groups = [g.strip() for g in args.groups.split(",") if g.strip()]

    send_state = None
    if args.send:
        send_state = {"sent": set(), "count": 0}
        try:
            send_state["sent"] = set(json.load(open(args.sent_file)))
        except Exception:
            pass

    client = TelegramClient(args.session, api_id, api_hash)
    entity_cache = load_entity_cache()
    profile_cache = load_json_state(PROFILE_CACHE_PATH)
    cursors = load_json_state(CURSOR_PATH)
    total = 0
    try:
        await client.start()
        with open(args.output, "a" if args.append else "w") as fh:
            for g in groups:
                print(f"# scraping {args.mode} in {g} ...", file=sys.stderr)
                rows = await scrape_group(client, g, args.msg_limit, args.delay, fh,
                                          args.exclude, send=args.send,
                                          send_delay=args.send_delay,
                                          max_send=args.max_send,
                                          send_state=send_state,
                                          entity_cache=entity_cache,
                                          profile_cache=profile_cache,
                                          cursors=cursors,
                                          mode=args.mode,
                                          member_limit=args.member_limit)
                matched = sum(1 for r in rows if r["icp_match"])
                print(f"# {g}: {len(rows)} users, {matched} ICP-match", file=sys.stderr)
                total += len(rows)
                save_json_state(PROFILE_CACHE_PATH, profile_cache)
                save_json_state(CURSOR_PATH, cursors)
    finally:
        await client.disconnect()
        if send_state is not None:
            json.dump(sorted(send_state["sent"]), open(args.sent_file, "w"))
            print(f"# sent this run: {send_state['count']}", file=sys.stderr)
    print(f"# TOTAL users: {total} -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
