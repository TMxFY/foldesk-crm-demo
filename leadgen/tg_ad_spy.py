#!/usr/bin/env python3
"""Telegram Ad-Spy: intercept active advertisers buying posts in tech/SaaS/AI channels.

Scans recent posts in monitored Telegram channels, detects paid placements via UTM/ref
links, #реклама/erid/promo markers, or external @handle CTAs, and resolves the
advertiser's personal founder @username, website, or X handle.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import urllib.request
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.users import GetFullUserRequest

try:
    from leadgen.tg_channel_graph import (
        HANDLE_RE,
        SKIP_SITE_HOSTS,
        URL_RE,
        extract_external_site,
        is_bot_handle,
        parse_channel_founder_info,
    )
except ImportError:
    from tg_channel_graph import (  # type: ignore
        HANDLE_RE,
        SKIP_SITE_HOSTS,
        URL_RE,
        extract_external_site,
        is_bot_handle,
        parse_channel_founder_info,
    )

DEFAULT_SESSION = "/Users/timofei/.claude/skill-telegram/tim3.session"
DEFAULT_SEEN_PATH = "leadgen_state/ad_spy_seen.json"

AD_MARKER_RE = re.compile(
    r"(#реклама|#ad\b|#sponsored|\berid\s*[:=]?\s*[A-Za-z0-9\-]+|\bsponsored\b|"
    r"партнерский\s+материал|партнёрский\s+материал|промокод\w*|на\s+правах\s+рекламы)",
    re.IGNORECASE,
)

CTA_RE = re.compile(
    r"(переход\w*|подпис\w*|читай\w*|забирай\w*|попробуй\w*|тестируй\w*|"
    r"запустил\w*|рекоменду\w*|совету\w*|по\s+ссылк\w*|бесплатн\w*|"
    r"check\s+out|try\s+it|join\b|follow\b|sign\s+up|launch\w*)",
    re.IGNORECASE,
)

UTM_PARAM_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref", "via"}

X_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})\b",
    re.IGNORECASE,
)
X_SKIP_HANDLES = {"home", "search", "explore", "i", "share", "intent", "hashtag", "compose"}
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def clean_product_url(raw_url: str) -> str:
    cleaned = (raw_url or "").strip().rstrip(".,;:!?)\"'>]")
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return ""
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if not host or "." not in host or host in SKIP_SITE_HOSTS:
        return ""
    if any(host.endswith("." + sk) for sk in SKIP_SITE_HOSTS):
        return ""
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    return f"{scheme}://{host}"


def has_tracking_params(raw_url: str) -> bool:
    cleaned = (raw_url or "").strip().rstrip(".,;:!?)\"'>]")
    try:
        parsed = urlparse(cleaned)
        qs = parse_qs(parsed.query or "")
        for key in qs:
            if key.lower() in UTM_PARAM_KEYS:
                return True
    except Exception:
        return False
    return False


def detect_ad_post(text: str, host_channel: str = "") -> dict:
    """Inspect a channel post and return ad signals (`is_ad`, `product_site`, `tg_targets`, `reasons`)."""
    body = (text or "").strip()
    if not body:
        return {"is_ad": False, "product_site": "", "tg_targets": [], "reasons": []}

    host_low = (host_channel or "").strip().lstrip("@").lower()
    reasons: list[str] = []
    product_site = ""
    tracked_site = ""

    for raw_url in URL_RE.findall(body):
        base = clean_product_url(raw_url)
        if not base:
            continue
        if not product_site:
            product_site = base
        if has_tracking_params(raw_url):
            tracked_site = base
            if "utm_or_ref_link" not in reasons:
                reasons.append("utm_or_ref_link")

    if tracked_site:
        product_site = tracked_site

    if AD_MARKER_RE.search(body):
        reasons.append("ad_marker")

    tg_targets: list[str] = []
    for match in HANDLE_RE.finditer(body):
        handle = match.group(1).strip()
        low = handle.lower()
        if low == host_low or is_bot_handle(low):
            continue
        if handle not in tg_targets:
            tg_targets.append(handle)

    if tg_targets and CTA_RE.search(body):
        reasons.append("external_tg_cta")

    is_ad = bool(reasons) and bool(product_site or tg_targets)
    return {
        "is_ad": is_ad,
        "product_site": product_site,
        "tg_targets": tg_targets,
        "reasons": reasons,
    }


def default_fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LeadgenAdSpy/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read(65536)
    return raw.decode("utf-8", errors="ignore")


def parse_site_html_contacts(html_text: str, fallback_domain: str = "") -> dict:
    """Extract page <title>, t.me handle, and x.com handle from HTML."""
    title = ""
    m_title = TITLE_RE.search(html_text or "")
    if m_title:
        title = html.unescape(re.sub(r"\s+", " ", m_title.group(1)).strip())[:120]
    if not title and fallback_domain:
        host = urlparse(fallback_domain).netloc.removeprefix("www.")
        title = host.split(".")[0].capitalize()

    tg_username = ""
    for m_tg in re.finditer(r"https?://(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})\b", html_text or "", re.I):
        cand = m_tg.group(1)
        if not is_bot_handle(cand):
            tg_username = cand
            break

    x_handle = ""
    for m_x in X_URL_RE.finditer(html_text or ""):
        cand = m_x.group(1)
        if cand.lower() not in X_SKIP_HANDLES:
            x_handle = cand
            break

    return {
        "title": title,
        "tg_username": tg_username,
        "x_handle": x_handle,
    }


def _load_seen(path: str) -> set[str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x).lower() for x in data}
        if isinstance(data, dict):
            return {str(x).lower() for x in data.get("seen", [])}
    except (OSError, ValueError):
        pass
    return set()


def _save_seen(path: str, seen: set[str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(seen)}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


async def _resolve_tg_target(client, handle: str) -> dict:
    """Resolve a mentioned @handle either as a personal user or as a channel with a founder in bio."""
    clean = handle.strip().lstrip("@")
    try:
        entity = await client.get_entity(clean)
    except Exception:
        entity = None

    if entity is not None and (
        entity.__class__.__name__ == "FakeChannel"
        or getattr(entity, "broadcast", False)
        or getattr(entity, "megagroup", False)
        or hasattr(entity, "participants_count")
    ):
        try:
            full = await client(GetFullChannelRequest(channel=entity))
            about = getattr(full.full_chat, "about", "") or ""
            chat_obj = full.chats[0] if getattr(full, "chats", None) else entity
            title = getattr(chat_obj, "title", "") or clean
            subs = getattr(full.full_chat, "participants_count", None) or getattr(entity, "participants_count", 0) or 0
            parsed = parse_channel_founder_info(
                about=about,
                posts=[],
                channel_username=clean,
                channel_title=title,
                subscribers=subs,
            )
            owner_username = parsed["username"] if parsed else ""
            site = parsed["site"] if parsed else extract_external_site([about])
            if owner_username:
                u_res = await _resolve_personal_user(client, owner_username)
                return {
                    "id": u_res.get("id") or getattr(entity, "id", 0),
                    "username": owner_username,
                    "name": u_res.get("name") or title,
                    "company": title,
                    "bio": about,
                    "site": clean_product_url(site) or site,
                }
        except Exception:
            pass

    u_res = await _resolve_personal_user(client, clean)
    return {
        "id": u_res.get("id") or getattr(entity, "id", 0) or 0,
        "username": clean,
        "name": u_res.get("name") or clean,
        "company": "",
        "bio": u_res.get("bio") or "",
        "site": "",
    }


async def _resolve_personal_user(client, username: str) -> dict:
    try:
        res = await client(GetFullUserRequest(username))
        user = res.users[0] if getattr(res, "users", None) else None
        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        name = " ".join(p for p in (first, last) if p).strip()
        bio = getattr(getattr(res, "full_user", None), "about", "") or ""
        return {"id": getattr(user, "id", 0), "name": name, "bio": bio}
    except Exception:
        return {"id": 0, "name": "", "bio": ""}


async def scan_ad_channels(
    client,
    channels: list[str],
    msg_limit: int = 40,
    seen_path: str = DEFAULT_SEEN_PATH,
    output_path: str = "ad_spy_leads.jsonl",
    delay: float = 1.0,
    fetch_html=default_fetch_html,
) -> list[dict]:
    seen = _load_seen(seen_path)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    discovered: list[dict] = []

    with open(output_path, "w", encoding="utf-8") as out_fh:
        for raw_ch in channels:
            ch = raw_ch.strip().lstrip("@")
            if not ch:
                continue
            try:
                entity = await client.get_entity(ch)
                messages = await client.get_messages(entity, limit=msg_limit)
            except FloodWaitError as fw:
                await asyncio.sleep(min(fw.seconds, 60))
                continue
            except Exception as exc:
                print(f"# ad_spy error reading {ch}: {exc}", file=sys.stderr)
                continue

            for msg in messages or []:
                text = (getattr(msg, "message", None) or "").strip()
                if not text:
                    continue
                sig = detect_ad_post(text, host_channel=ch)
                if not sig["is_ad"]:
                    continue

                site = sig["product_site"]
                tg_targets = sig["tg_targets"]

                resolved_username = ""
                resolved_name = ""
                resolved_company = ""
                resolved_bio = ""
                resolved_id = 0
                x_handle = ""

                if tg_targets:
                    target_info = await _resolve_tg_target(client, tg_targets[0])
                    resolved_username = target_info.get("username") or ""
                    resolved_name = target_info.get("name") or ""
                    resolved_company = target_info.get("company") or ""
                    resolved_bio = target_info.get("bio") or ""
                    resolved_id = target_info.get("id") or 0
                    if not site and target_info.get("site"):
                        site = target_info["site"]

                if site and (not resolved_username or not resolved_company):
                    html_text = ""
                    if fetch_html is not None:
                        try:
                            html_text = fetch_html(site) or ""
                        except Exception:
                            html_text = ""
                    meta = parse_site_html_contacts(html_text, fallback_domain=site)
                    if not resolved_company and meta["title"]:
                        resolved_company = meta["title"]
                    if not resolved_username and meta["tg_username"]:
                        resolved_username = meta["tg_username"]
                    if meta["x_handle"]:
                        x_handle = meta["x_handle"]

                if not x_handle:
                    for m_x in X_URL_RE.finditer(text):
                        cand = m_x.group(1)
                        if cand.lower() not in X_SKIP_HANDLES:
                            x_handle = cand
                            break

                dedup_key = (resolved_username or x_handle or site).lower()
                if not dedup_key or dedup_key in seen:
                    continue
                if not (resolved_username or x_handle or site):
                    continue
                seen.add(dedup_key)
                if site:
                    seen.add(site.lower())

                contact_channels: list[str] = []
                links: list[str] = []
                if site:
                    links.append(site)
                if resolved_username:
                    contact_channels.append("telegram")
                    links.append(f"https://t.me/{resolved_username}")
                if x_handle:
                    contact_channels.append("x")
                    links.append(f"https://x.com/{x_handle}")
                if not contact_channels and site:
                    contact_channels.append("site")

                if not resolved_company and site:
                    host = urlparse(site).netloc.removeprefix("www.")
                    resolved_company = host.split(".")[0].capitalize()

                row: dict = {
                    "id": resolved_id or hash(dedup_key) & 0x7FFFFFFF,
                    "username": resolved_username,
                    "telegram_username": resolved_username,
                    "name": resolved_name or resolved_company or resolved_username,
                    "company": resolved_company or resolved_name,
                    "site": site,
                    "project_url": site,
                    "bio": resolved_bio or text[:240],
                    "source": "tg_ad_spy",
                    "ad_host_channel": ch,
                    "sample_messages": [text],
                    "contact_channels": contact_channels,
                    "links": list(dict.fromkeys(links)),
                }
                if x_handle:
                    x_url = f"https://x.com/{x_handle}"
                    row["founder_x"] = x_url
                    row["social_verified"] = [{"platform": "x", "handle": x_handle, "url": x_url}]

                discovered.append(row)
                out_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                out_fh.flush()

            if delay > 0:
                await asyncio.sleep(delay)

    _save_seen(seen_path, seen)
    return discovered


async def _async_main(args: argparse.Namespace) -> None:
    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")
    if not api_id or not api_hash:
        print("TG_API_ID / TG_API_HASH not set", file=sys.stderr)
        sys.exit(1)

    raw_channels = args.channels or os.getenv("TG_AD_SPY_CHANNELS", "")
    channels = [c.strip() for c in raw_channels.split(",") if c.strip()]
    if not channels:
        print("No channels provided via --channels or TG_AD_SPY_CHANNELS", file=sys.stderr)
        sys.exit(1)

    client = TelegramClient(args.session, api_id, api_hash)
    await client.start()
    try:
        rows = await scan_ad_channels(
            client=client,
            channels=channels,
            msg_limit=args.msg_limit,
            seen_path=args.seen,
            output_path=args.output,
            delay=args.delay,
        )
        print(f"# tg_ad_spy: discovered {len(rows)} advertiser leads -> {args.output}", file=sys.stderr)
    finally:
        await client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description="Telegram Ad-Spy lead scraper")
    ap.add_argument("--session", default=DEFAULT_SESSION)
    ap.add_argument("--channels", default=os.getenv("TG_AD_SPY_CHANNELS", ""))
    ap.add_argument("--msg-limit", type=int, default=40)
    ap.add_argument("--seen", default=DEFAULT_SEEN_PATH)
    ap.add_argument("--output", required=True)
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
