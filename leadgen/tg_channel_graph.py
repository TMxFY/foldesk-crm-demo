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
import time
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
from telethon.tl.types import PeerChannel

try:
    from supply_side import PLATFORM_AGGREGATOR_HOSTS as _SS_PLATFORM_HOSTS
except ImportError:
    try:
        from leadgen.supply_side import PLATFORM_AGGREGATOR_HOSTS as _SS_PLATFORM_HOSTS
    except ImportError:
        _SS_PLATFORM_HOSTS = ()

try:
    from leadgen import tg_web_client
except ImportError:
    try:
        import tg_web_client  # type: ignore
    except ImportError:
        tg_web_client = None  # type: ignore

_ORIG_TG_WEB_CLIENT = tg_web_client

DEFAULT_SESSION = (
    os.environ.get("TG_SESSION")
    or os.environ.get("SCRAPER_TG_SESSION")
    or "/Users/timofei/.claude/skill-telegram/tim3.session"
)
DEFAULT_STATE_PATH = "leadgen_state/channel_graph_seen.json"
DEFAULT_GRAPH_SEED_CHANNELS: tuple[str, ...] = (
    "startupoftheday",
    "productradar_official",
    "gonzo_ML",
    "ai_newz",
    "indiehackers_ru",
    "neurochannel",
    "gamedev_ru",
    "ton_builders",
    "ai_founders",
    "microconf",
    "startups",
    "saasgrowth",
    "indiehackers",
    "nocodefounders",
    "producthunt_news",
    "b2bsaas",
    "bootstrapped",
    "growthhacking",
    "shopifyeasy",
    "shopify_merchants",
    "dtc_brands",
    "ecommerce_news",
    "mobileapps_dev",
    "appgrowth",
    "saas_insider",
    "ai_products",
    "indie_makers",
    "d2c_marketing",
    # +32 verified fresh seeds across Consumer AI, Mobile UA/ASO, Subscription Apps,
    # Shopify/D2C, Prop Trading/Fintech, and CIS/Dubai Global Founders:
    "seeallochnaya",
    "denissexy",
    "ai_product",
    "aioftheday",
    "forklogAI",
    "data_secrets",
    "dl_stories",
    "abstractDL",
    "its_capitan",
    "neuraldeep",
    "huggingface",
    "bytebytego",
    "app_growth",
    "aso_ru",
    "asotrips",
    "aso_pro",
    "app2top",
    "android_dev_ru",
    "retail_ru",
    "e_pepper",
    "shopify_plus",
    "ad_tech",
    "rushagency_ru",
    "ftmo_official",
    "tradingview",
    "frank_media",
    "tondev_news",
    "epicgrowth",
    "gopractice",
    "ProductMindset",
    "productsense",
    "ordinarypm",
    "brainbox_vc",
    "temno",
    "Startup_Jedi_RU",
    "saas_founders",
    "ilya_krasinsky",
    "oshestakovdigital",
    "mspiridonov",
    "petrochenkow",
    "levelsio",
    "dnative",
    "fb_killa",
    "t_tutorial",
    "fromspytobuy",
)
FALLBACK_EN_GRAPH_SEEDS: tuple[str, ...] = (
    "saasgrowth",
    "indiehackers",
    "nocodefounders",
    "ai_founders",
    "microconf",
    "startups",
    "producthunt_news",
    "growthhacking",
    "b2bsaas",
    "bootstrapped",
    "shopifyeasy",
    "shopify_merchants",
    "dtc_brands",
    "ecommerce_news",
    "mobileapps_dev",
    "appgrowth",
    "saas_insider",
    "ai_products",
    "indie_makers",
    "d2c_marketing",
    # +30 fresh global/EN/app/AI/D2C/prop/founder seeds:
    "levelsio",
    "bytebytego",
    "huggingface",
    "openai_news",
    "app_growth",
    "aso_ru",
    "asotrips",
    "aso_pro",
    "app2top",
    "shopify_plus",
    "e_pepper",
    "retail_ru",
    "ftmo_official",
    "tradingview",
    "markettwits",
    "bitkogan",
    "frank_media",
    "tondev_news",
    "tontech",
    "epicgrowth",
    "gopractice",
    "ProductMindset",
    "productsense",
    "saas_founders",
    "Startup_Jedi_RU",
    "brainbox_vc",
    "ai_product",
    "aioftheday",
    "its_capitan",
    "t_tutorial",
)
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
    r"(основател\w*|соосновател\w*|владел\w*|создател\w*|руководител\w*|"
    r"автор\w*|фаундер\w*|founder|co[- ]?founder|"
    r"ceo|owner|creator|built\s+by|making|building|связь|лс|личк\w*|dm\b|contact)",
    re.IGNORECASE,
)

HANDLE_RE = re.compile(
    r"(?:@|(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?)([A-Za-z][A-Za-z0-9_]{3,31})\b",
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
    "substack.com",
    "linkedin.com",
    "facebook.com",
    "bit.ly",
    "t.co",
    "lu.ma",
    "luma.com",
    "news.ycombinator.com",
    "ycombinator.com",
    "huggingface.co",
    "itunes.apple.com",
    "apps.apple.com",
    "play.google.com",
    "linktr.ee",
    "linktree.com",
    "notion.site",
    "notion.so",
    "calendly.com",
    "docs.google.com",
    "drive.google.com",
    *_SS_PLATFORM_HOSTS,
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


def _load_graph_state(path: str) -> tuple[set[str], set[str], list[tuple[object, int]]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x).lower() for x in data if str(x or "").strip()}, set(), []
        if isinstance(data, dict):
            raw_seen = data.get("seen") if "seen" in data else data.get("seen_channels", [])
            seen = {str(x).lower() for x in (raw_seen or []) if str(x or "").strip()}
            expanded = {str(x).lower() for x in (data.get("expanded") or []) if str(x or "").strip()}
            frontier: list[tuple[object, int]] = []
            for item in data.get("frontier") or []:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    raw_target = item[0]
                    if isinstance(raw_target, int):
                        ch_ref: object = raw_target
                    else:
                        ch_str = str(raw_target or "").strip().lstrip("@").lower()
                        if not ch_str:
                            continue
                        ch_ref = int(ch_str[2:]) if (ch_str.startswith("id") and ch_str[2:].isdigit()) else ch_str
                    try:
                        d = int(item[1])
                    except (TypeError, ValueError):
                        continue
                    frontier.append((ch_ref, d))
            return seen, expanded, frontier
    except (OSError, ValueError):
        pass
    return set(), set(), []


def _load_seen(path: str) -> set[str]:
    seen, _, _ = _load_graph_state(path)
    return seen


def _save_seen(
    path: str,
    seen: set[str],
    frontier: list[tuple[object, int]] | None = None,
    expanded: set[str] | None = None,
) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    payload: dict[str, object] = {
        "seen": sorted(seen),
        "expanded": sorted(expanded or ()),
    }
    if frontier:
        payload["frontier"] = [[ch, int(d)] for ch, d in frontier]
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
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


def _load_discovered_comment_channels(paths: list[str]) -> list[object]:
    """Parse channel handles or id<digits> tokens from discovered_comment_channels.txt."""
    out: list[object] = []
    seen_tokens: set[str] = set()
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for raw in lines:
            tok = raw.strip()
            if not tok or tok.startswith("#"):
                continue
            tok = re.sub(r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?", "", tok, flags=re.I)
            tok = tok.lstrip("@").split("/", 1)[0].split("?", 1)[0].strip()
            if not tok:
                continue
            low = tok.lower()
            if low in seen_tokens:
                continue
            if low.startswith("id") and low[2:].isdigit():
                seen_tokens.add(low)
                out.append(int(low[2:]))
            elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", tok) and not is_bot_handle(tok):
                seen_tokens.add(low)
                out.append(tok)
    return out


def _extract_linked_channels_from_messages(msgs: list[object], host_key: str, *, skip_handles: set[str] | None = None) -> list[object]:
    """Extract forwarded channel IDs and mentioned @handles / t.me links from channel posts."""
    discovered: list[object] = []
    local_seen: set[str] = set()
    skip_low = {h.lower() for h in (skip_handles or set()) if h}
    for m in msgs or []:
        if isinstance(m, str):
            text = m.strip()
        else:
            fwd = getattr(m, "fwd_from", None)
            if fwd is not None:
                from_id = getattr(fwd, "from_id", None)
                fwd_ch_id = getattr(from_id, "channel_id", None)
                if fwd_ch_id is None:
                    fwd_ch_id = getattr(fwd, "channel_id", None)
                if isinstance(fwd_ch_id, int) and fwd_ch_id > 0:
                    key = f"id{fwd_ch_id}"
                    if key not in local_seen and key != host_key:
                        local_seen.add(key)
                        discovered.append(int(fwd_ch_id))
            text = (getattr(m, "message", None) or getattr(m, "text", None) or "").strip()
        if text:
            for match in HANDLE_RE.finditer(text):
                handle = match.group(1).strip()
                low = handle.lower()
                if not handle or low == host_key or low in skip_low or is_bot_handle(low):
                    continue
                if low not in local_seen:
                    local_seen.add(low)
                    discovered.append(handle)
    return discovered


def _should_use_web_first(client: object, *custom_fns: object) -> bool:
    if any(fn is not None for fn in custom_fns):
        return True
    if client is None or isinstance(client, TelegramClient):
        return True
    if tg_web_client is not _ORIG_TG_WEB_CLIENT:
        return True
    if tg_web_client is not None and getattr(tg_web_client, "has_patched_io", lambda: False)():
        return True
    return False


async def _resolve_user_web_or_client(
    client: object,
    username: str,
    *,
    use_web: bool,
    probe_fn=None,
    http_get_fn=None,
) -> dict:
    if use_web and (probe_fn is not None or tg_web_client is not None):
        try:
            probed = (
                probe_fn(username)
                if probe_fn is not None
                else tg_web_client.probe_tme_handle_web(username, http_get_fn=http_get_fn)
            )
        except Exception:
            probed = {"kind": "unknown"}
        kind = str((probed or {}).get("kind") or "unknown").lower()
        if kind == "user":
            return {
                "id": None,
                "name": str(probed.get("title") or "").strip(),
                "user_bio": str(probed.get("bio") or "").strip(),
                "bot": False,
            }
        if kind == "bot":
            return {"bot": True}
        if kind in ("missing", "channel"):
            return {"skip": True}
        if client is None:
            return {"id": None, "name": "", "user_bio": "", "bot": False}
    if client is None:
        return {"id": None, "name": "", "user_bio": "", "bot": False}
    return await _resolve_user_profile(client, username)


def _is_channel_entity(entity: object) -> bool:
    if entity is None:
        return False
    if hasattr(entity, "title") or getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False) or hasattr(entity, "participants_count"):
        return True
    if getattr(entity, "first_name", None) is not None and not hasattr(entity, "title"):
        return False
    return True


async def crawl_channel_graph(
    client,
    seeds: list[str],
    depth: int = 2,
    limit: int = 30,
    state_path: str = DEFAULT_STATE_PATH,
    output_path: str = "channel_graph_leads.jsonl",
    linked_chats_path: str | None = None,
    delay: float = 1.0,
    max_runtime_sec: float = 150.0,
    *,
    web_posts_fn=None,
    probe_fn=None,
    http_get_fn=None,
) -> list[dict]:
    state_dir = os.path.dirname(os.path.abspath(state_path))
    state_discovered_path = os.path.join(state_dir, "discovered_comment_channels.txt")
    if not linked_chats_path:
        linked_chats_path = state_discovered_path

    seen_channels, expanded_channels, saved_frontier = _load_graph_state(state_path)
    seen_founders: set[str] = set()
    queue: deque[tuple[object, int]] = deque()
    queued_keys: set[str] = set()
    rescan_keys: set[str] = set()
    deferred_frontier: list[tuple[object, int]] = []
    deadline = (time.monotonic() + float(max_runtime_sec)) if max_runtime_sec and max_runtime_sec > 0 else None
    use_web = _should_use_web_first(client, web_posts_fn, probe_fn, http_get_fn)

    def _target_key(obj: object) -> str:
        if isinstance(obj, int):
            return f"id{obj}"
        if isinstance(obj, str):
            return obj.strip().lstrip("@").lower()
        uname = getattr(obj, "username", None)
        if uname:
            return str(uname).strip().lstrip("@").lower()
        oid = getattr(obj, "id", None)
        return f"id{oid}" if oid is not None else str(obj).strip().lstrip("@").lower()

    def _fetch_web(ch_name: str, post_lim: int) -> tuple[dict, list[str]]:
        if web_posts_fn is not None:
            res = web_posts_fn(ch_name, post_lim)
            if isinstance(res, tuple) and len(res) == 2:
                return dict(res[0] or {}), [str(x) for x in (res[1] or []) if str(x or "").strip()]
            return {}, [str(x) for x in (res or []) if str(x or "").strip()]
        if tg_web_client is not None:
            return tg_web_client.fetch_channel_posts_web(ch_name, limit=post_lim, http_get_fn=http_get_fn)
        return {}, []

    # Prioritize unexpanded frontier items first while preserving seen_channels.
    prioritized_frontier = sorted(
        saved_frontier,
        key=lambda item: (0 if _target_key(item[0]) not in expanded_channels else 1),
    )
    for ch_ref, ch_depth in prioritized_frontier:
        ch_key = _target_key(ch_ref)
        if ch_key and ch_key not in queued_keys and (
            ch_key not in seen_channels or (ch_depth < depth and ch_key not in expanded_channels)
        ):
            queue.append((ch_ref, ch_depth))
            queued_keys.add(ch_key)

    unseen_seeds: list[str] = []
    unexpanded_seeds: list[str] = []
    for s in seeds:
        clean = s.strip().lstrip("@")
        s_key = clean.lower()
        if not clean or s_key in queued_keys:
            continue
        if saved_frontier and s_key in expanded_channels:
            continue
        if not saved_frontier and s_key in seen_channels and s_key in expanded_channels:
            continue
        if s_key not in seen_channels:
            unseen_seeds.append(clean)
        else:
            unexpanded_seeds.append(clean)

    for clean in (*unseen_seeds, *unexpanded_seeds):
        s_key = clean.lower()
        if s_key not in queued_keys:
            queue.append((clean, 0))
            queued_keys.add(s_key)

    if depth > 0:
        unexpanded_seen = [
            c for c in sorted(seen_channels)
            if c not in expanded_channels and c not in queued_keys
        ]
        for cand in unexpanded_seen[:25]:
            queue.append((cand, max(0, depth - 1)))
            queued_keys.add(cand)

    # When initial queue is empty and seeds are exhausted, replenish from:
    # 1) discovered_comment_channels.txt in dirname(state_path)
    # 2) unexpanded FALLBACK_EN_GRAPH_SEEDS / seen channels
    # 3) re-scanning recent posts of seed channels for newly linked/forwarded channels
    had_initial_work = bool(queue)
    if not had_initial_work and seeds:
        cand_paths = [state_discovered_path]
        if linked_chats_path and os.path.abspath(linked_chats_path) != os.path.abspath(state_discovered_path):
            cand_paths.append(linked_chats_path)
        for disc_ref in _load_discovered_comment_channels(cand_paths):
            disc_key = _target_key(disc_ref)
            if disc_key and disc_key not in queued_keys and (
                disc_key not in seen_channels or (depth > 0 and disc_key not in expanded_channels)
            ):
                queue.append((disc_ref, 0))
                queued_keys.add(disc_key)

        for fb in FALLBACK_EN_GRAPH_SEEDS:
            fb_key = fb.strip().lstrip("@").lower()
            if fb_key and fb_key not in queued_keys and (
                fb_key not in seen_channels or (depth > 0 and fb_key not in expanded_channels)
            ):
                queue.append((fb, 0))
                queued_keys.add(fb_key)

        if depth > 0:
            unexpanded_seen = [c for c in sorted(seen_channels) if c not in expanded_channels]
            for cand in unexpanded_seen[:12]:
                if cand not in queued_keys:
                    queue.append((cand, 0))
                    queued_keys.add(cand)

        for s in seeds:
            clean = s.strip().lstrip("@")
            s_key = clean.lower()
            if clean and s_key in seen_channels and s_key not in queued_keys:
                queue.append((clean, 0))
                queued_keys.add(s_key)
                rescan_keys.add(s_key)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    discovered_rows: list[dict] = []
    seen_expansions = 0
    max_seen_expansions = 20

    async def _resolve_entity_lazy(tgt: object) -> object | None:
        if hasattr(tgt, "id"):
            return tgt
        if client is None:
            return None
        try:
            return await client.get_entity(tgt)
        except FloodWaitError:
            raise
        except Exception:
            if isinstance(tgt, int):
                try:
                    return await client.get_entity(PeerChannel(tgt))
                except FloodWaitError:
                    raise
                except Exception:
                    return None
            return None

    def _enqueue_linked_from_msgs(msgs_list: list[object], host_k: str, c_depth: int, skip_u: set[str] | None = None) -> None:
        next_d = min(c_depth + 1, depth) if depth > 0 else 0
        for cand in _extract_linked_channels_from_messages(msgs_list, host_k, skip_handles=skip_u):
            cand_key = _target_key(cand)
            if (
                cand_key
                and cand_key not in queued_keys
                and cand_key not in seen_channels
            ):
                queued_keys.add(cand_key)
                queue.append((cand, next_d))

    with open(output_path, "w", encoding="utf-8") as out_fh:
        while queue and len(discovered_rows) < limit:
            if deadline is not None and time.monotonic() >= deadline:
                print("# tg_channel_graph: runtime budget reached; saving partial state", file=sys.stderr)
                break
            target, cur_depth = queue.popleft()
            target_key = _target_key(target)
            entity = target if hasattr(target, "id") else None

            if entity is None and (not use_web or isinstance(target, int)):
                try:
                    entity = await _resolve_entity_lazy(target)
                except FloodWaitError as fw:
                    deferred_frontier.append((target, cur_depth))
                    await asyncio.sleep(min(fw.seconds, 60))
                    continue
                if entity is None:
                    continue

            if entity is not None and not _is_channel_entity(entity):
                continue

            ch_username = (getattr(entity, "username", None) or (target if isinstance(target, str) else target_key)).lstrip("@")
            ch_key = ch_username.lower()
            already_seen = ch_key in seen_channels or target_key in seen_channels
            already_expanded = ch_key in expanded_channels or target_key in expanded_channels
            fetch_ok = already_seen

            if already_seen and (ch_key in rescan_keys or target_key in rescan_keys):
                rescan_msgs: list[object] = []
                if use_web and isinstance(ch_username, str) and not (ch_username.startswith("id") and ch_username[2:].isdigit()):
                    try:
                        _, rescan_web_posts = _fetch_web(ch_username, 10)
                        rescan_msgs = list(rescan_web_posts)
                    except Exception:
                        rescan_msgs = []
                if not rescan_msgs and client is not None:
                    try:
                        if entity is None:
                            entity = await _resolve_entity_lazy(target)
                        if entity is not None:
                            rescan_msgs = list(await client.get_messages(entity, limit=10) or [])
                    except FloodWaitError as fw:
                        await asyncio.sleep(min(fw.seconds, 60))
                    except Exception:
                        pass
                if rescan_msgs:
                    _enqueue_linked_from_msgs(rescan_msgs, ch_key, cur_depth)

            if not already_seen:
                try:
                    about = ""
                    ch_title = ch_username
                    subscribers = 0
                    linked_chat_id = None
                    post_texts: list[str] = []
                    msgs_for_links: list[object] = []
                    used_web_data = False

                    if use_web and not (ch_username.startswith("id") and ch_username[2:].isdigit()):
                        try:
                            web_info, web_posts = _fetch_web(ch_username, 5)
                        except Exception:
                            web_info, web_posts = {}, []
                        if web_info or web_posts:
                            used_web_data = True
                            about = str(web_info.get("about") or web_info.get("bio") or "").strip()
                            ch_title = str(web_info.get("title") or getattr(entity, "title", None) or ch_username).strip()
                            ch_username = str(web_info.get("username") or ch_username).lstrip("@")
                            ch_key = ch_username.lower()
                            subscribers = int(web_info.get("subscribers") or getattr(entity, "participants_count", 0) or 0)
                            linked_chat_id = web_info.get("linked_chat_id")
                            post_texts = [str(p).strip() for p in (web_posts or []) if str(p or "").strip()]
                            msgs_for_links = list(post_texts)

                            if linked_chat_id is None and client is not None:
                                try:
                                    if entity is None:
                                        entity = await _resolve_entity_lazy(target)
                                    if entity is not None and _is_channel_entity(entity):
                                        full = await client(GetFullChannelRequest(channel=entity))
                                        linked_chat_id = getattr(full.full_chat, "linked_chat_id", None)
                                        if not about:
                                            about = getattr(full.full_chat, "about", "") or ""
                                        chat_obj = full.chats[0] if getattr(full, "chats", None) else entity
                                        if not web_info.get("title"):
                                            ch_title = getattr(chat_obj, "title", "") or ch_title
                                        if not subscribers:
                                            subscribers = (
                                                getattr(full.full_chat, "participants_count", None)
                                                or getattr(chat_obj, "participants_count", None)
                                                or 0
                                            )
                                except FloodWaitError as fw:
                                    await asyncio.sleep(min(fw.seconds, 10))
                                except Exception as exc:
                                    print(f"# channel_graph GetFullChannel fallback on {ch_username}: {exc}", file=sys.stderr)

                    if not used_web_data:
                        if client is None:
                            continue
                        if entity is None:
                            entity = await _resolve_entity_lazy(target)
                        if entity is None or not _is_channel_entity(entity):
                            continue
                        full = await client(GetFullChannelRequest(channel=entity))
                        about = getattr(full.full_chat, "about", "") or ""
                        chat_obj = full.chats[0] if getattr(full, "chats", None) else entity
                        ch_title = getattr(chat_obj, "title", "") or getattr(entity, "title", "") or ch_username
                        ch_username = getattr(chat_obj, "username", None) or ch_username
                        ch_key = ch_username.lower()
                        subscribers = (
                            getattr(full.full_chat, "participants_count", None)
                            or getattr(chat_obj, "participants_count", None)
                            or 0
                        )
                        linked_chat_id = getattr(full.full_chat, "linked_chat_id", None)

                        msgs = await client.get_messages(entity, limit=5)
                        msgs_for_links = list(msgs or [])
                        post_texts = [
                            (getattr(m, "message", None) or "").strip()
                            for m in (msgs or [])
                            if (getattr(m, "message", None) or "").strip()
                        ]

                    seen_channels.add(ch_key)
                    if target_key:
                        seen_channels.add(target_key)
                    fetch_ok = True

                    parsed = parse_channel_founder_info(
                        about=about,
                        posts=post_texts,
                        channel_username=ch_username,
                        channel_title=ch_title,
                        subscribers=subscribers,
                    )
                    skip_linked = {parsed["username"]} if parsed else set()
                    if cur_depth < depth:
                        _enqueue_linked_from_msgs(msgs_for_links, ch_key, cur_depth, skip_u=skip_linked)
                    if parsed:
                        _append_linked_chat(linked_chats_path, linked_chat_id)
                        founder_uname = parsed["username"].lstrip("@")
                        if founder_uname.lower() not in seen_founders:
                            uinfo = await _resolve_user_web_or_client(
                                client,
                                founder_uname,
                                use_web=use_web,
                                probe_fn=probe_fn,
                                http_get_fn=http_get_fn,
                            )
                            if not uinfo.get("bot") and not uinfo.get("skip"):
                                seen_founders.add(founder_uname.lower())
                                site = parsed["site"]
                                combined_bio = about.strip()
                                if uinfo.get("user_bio") and uinfo["user_bio"] not in combined_bio:
                                    combined_bio = f"{uinfo['user_bio']} | {combined_bio}".strip(" |")
                                links = [f"https://t.me/{founder_uname}"]
                                if site:
                                    links.insert(0, site)
                                fallback_eid = int.from_bytes(ch_key.encode("utf-8")[:8].ljust(8, b"\0"), "little") & 0x7FFFFFFF or 1
                                row = {
                                    "id": uinfo.get("id") or None,
                                    "dedup_id": fallback_eid,
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
                    deferred_frontier.append((getattr(entity, "username", None) or target, cur_depth))
                    await asyncio.sleep(min(fw.seconds, 60))
                    continue
                except Exception as exc:
                    print(f"# channel_graph error on {ch_username}: {exc}", file=sys.stderr)

            should_expand = (
                (already_seen or fetch_ok)
                and (cur_depth < depth)
                and not already_expanded
                and (not already_seen or seen_expansions < max_seen_expansions)
            )
            if should_expand:
                if len(discovered_rows) >= limit:
                    deferred_frontier.append((getattr(entity, "username", None) or target, cur_depth))
                elif client is not None:
                    if already_seen:
                        seen_expansions += 1
                    try:
                        if entity is None:
                            entity = await _resolve_entity_lazy(target)
                        if entity is not None:
                            recs = await client(GetChannelRecommendationsRequest(channel=entity))
                            expanded_channels.add(ch_key)
                            if target_key:
                                expanded_channels.add(target_key)
                            for rec_chat in getattr(recs, "chats", []) or []:
                                rec_uname = (
                                    getattr(rec_chat, "username", None)
                                    or f"id{getattr(rec_chat, 'id', '')}"
                                ).lower()
                                if (
                                    rec_uname
                                    and rec_uname not in queued_keys
                                    and (
                                        rec_uname not in seen_channels
                                        or ((cur_depth + 1) < depth and rec_uname not in expanded_channels)
                                    )
                                ):
                                    queued_keys.add(rec_uname)
                                    queue.append((rec_chat, cur_depth + 1))
                    except FloodWaitError as fw:
                        deferred_frontier.append((getattr(entity, "username", None) or target, cur_depth))
                        await asyncio.sleep(min(fw.seconds, 60))
                    except Exception as exc:
                        print(f"# recommendations error on {ch_username}: {exc}", file=sys.stderr)
            elif fetch_ok and cur_depth < depth and not already_expanded and len(discovered_rows) >= limit:
                deferred_frontier.append((getattr(entity, "username", None) or target, cur_depth))

            if delay > 0:
                await asyncio.sleep(delay)

    remaining_frontier: list[tuple[object, int]] = []
    seen_frontier_keys: set[str] = set()
    for q_target, q_depth in [*deferred_frontier, *list(queue)]:
        if isinstance(q_target, int):
            q_ref: object = q_target
        elif isinstance(q_target, str):
            q_ref = q_target.strip().lstrip("@").lower()
        else:
            uname = getattr(q_target, "username", None)
            oid = getattr(q_target, "id", None)
            if uname:
                q_ref = str(uname).strip().lstrip("@").lower()
            elif oid is not None:
                q_ref = int(oid)
            else:
                q_ref = ""
        q_key = _target_key(q_ref) if q_ref != "" else ""
        if (
            q_key
            and q_key not in seen_frontier_keys
            and q_key not in rescan_keys
            and (q_key not in seen_channels or (q_depth < depth and q_key not in expanded_channels))
        ):
            seen_frontier_keys.add(q_key)
            remaining_frontier.append((q_ref, int(q_depth)))

    remaining_frontier.sort(key=lambda item: (0 if _target_key(item[0]) not in expanded_channels else 1))
    _save_seen(
        state_path,
        seen_channels,
        frontier=remaining_frontier[:500],
        expanded=expanded_channels,
    )
    return discovered_rows


async def _async_main(args: argparse.Namespace) -> None:
    raw_seeds = args.seeds or os.getenv("TG_GRAPH_SEED_CHANNELS", "")
    seeds = [s.strip() for s in raw_seeds.split(",") if s.strip()]
    if not seeds and not getattr(args, "no_default_seeds", False):
        seeds = list(DEFAULT_GRAPH_SEED_CHANNELS)
    if not seeds:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8"):
            pass
        print("# tg_channel_graph: no seed channels configured; skipping (0 leads)", file=sys.stderr)
        return

    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")
    if not api_id or not api_hash:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8"):
            pass
        print("# tg_channel_graph: TG_API_ID / TG_API_HASH not set; skipping (0 leads)", file=sys.stderr)
        return

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
            max_runtime_sec=float(getattr(args, "max_runtime_sec", 150.0) or 150.0),
        )
        print(f"# tg_channel_graph: discovered {len(rows)} founder leads -> {args.output}", file=sys.stderr)
    finally:
        await client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description="Recursive Telegram founder channel graph crawler")
    ap.add_argument("--session", default=DEFAULT_SESSION)
    ap.add_argument("--seeds", default=os.getenv("TG_GRAPH_SEED_CHANNELS", ""))
    ap.add_argument("--no-default-seeds", action="store_true")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--state", "--seen", dest="state", default=DEFAULT_STATE_PATH)
    ap.add_argument("--output", required=True)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--max-runtime-sec", type=float, default=150.0)
    args = ap.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
