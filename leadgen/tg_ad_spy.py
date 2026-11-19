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
import time
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

DEFAULT_SESSION = (
    os.environ.get("TG_SESSION")
    or os.environ.get("SCRAPER_TG_SESSION")
    or "/Users/timofei/.claude/skill-telegram/tim3.session"
)
DEFAULT_SEEN_PATH = "leadgen_state/ad_spy_seen.json"
DEFAULT_AD_SPY_CHANNELS: tuple[str, ...] = (
    "gonzo_ML",
    "ai_newz",
    "startupoftheday",
    "productradar_official",
    "indiehackers_ru",
    "vc_blogs",
    "toncoin_rus",
    "forklog",
    "rbc_crypto",
    "gamedev_ru",
    "neurochannel",
    "ecom_digest",
    "saasgrowth",
    "indiehackers",
    "marketingmemes",
    "devtools_daily",
)

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
        raw = resp.read(524288)
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


def _load_spy_cursor(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return max(0, int(data.get("cursor") or 0))
    except (OSError, ValueError, TypeError):
        pass
    return 0


def _save_seen(path: str, seen: set[str], *, cursor: int | None = None) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    payload: dict = {"seen": sorted(seen)}
    if cursor is not None:
        payload["cursor"] = max(0, int(cursor))
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
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


def _extract_post_texts(payload, limit: int) -> list[str]:
    if not payload:
        return []
    candidates = None
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for container in (payload, payload.get("output"), payload.get("result"), payload.get("data")):
            if not isinstance(container, dict):
                continue
            for key in ("rows", "posts", "messages", "items", "results", "list"):
                val = container.get(key)
                if isinstance(val, list):
                    candidates = val
                    break
            if candidates is not None:
                break
    out: list[str] = []
    for item in candidates or []:
        if isinstance(item, str):
            txt = item.strip()
        elif isinstance(item, dict):
            txt = str(item.get("text") or item.get("message") or item.get("content") or "").strip()
        else:
            txt = ""
        if txt:
            out.append(txt)
        if len(out) >= limit:
            break
    return out


def _parse_tme_s_html(html_text: str, limit: int) -> list[str]:
    if not html_text:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        BeautifulSoup = None

    out: list[str] = []
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_text, "html.parser")
        for node in soup.select(".tgme_widget_message_text"):
            for a_tag in node.find_all("a", href=True):
                href = html.unescape(str(a_tag.get("href") or "")).strip()
                label = a_tag.get_text(" ", strip=True)
                if href.startswith(("http://", "https://")) and href not in label:
                    a_tag.replace_with(f"{label} {href} ")
            txt = " ".join(node.get_text(" ", strip=True).split())
            if txt:
                out.append(txt)
        return out[-limit:] if limit > 0 else []

    for m in re.finditer(r'class="[^"]*\btgme_widget_message_text\b[^"]*"[^>]*>(.*?)</div>', html_text, re.I | re.S):
        inner = m.group(1)
        inner = re.sub(
            r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
            lambda am: f"{re.sub(r'<[^>]+>', ' ', am.group(2)).strip()} {html.unescape(am.group(1))} ",
            inner,
            flags=re.I | re.S,
        )
        txt = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", inner)).split())
        if txt:
            out.append(txt)
    return out[-limit:] if limit > 0 else []


def fetch_public_channel_posts_http(
    channel: str,
    limit: int = 30,
    *,
    monid_fn=None,
    treg_fn=None,
    http_get_fn=None,
) -> list[str]:
    """Fetch public channel posts without risking Telethon FloodWait."""
    ch = str(channel or "").strip().lstrip("@")
    if not ch:
        return []
    custom_mode = any(fn is not None for fn in (monid_fn, treg_fn, http_get_fn))

    # In production, check fast free https://t.me/s/<channel> FIRST (~0.3s) so 12+ channels
    # complete in a few seconds instead of spending 20-45s per channel polling Monid/Treg.
    if not custom_mode:
        try:
            html_txt = default_fetch_html(f"https://t.me/s/{ch}")
            posts = _parse_tme_s_html(str(html_txt or ""), limit)
            if posts:
                return posts
        except Exception:
            pass

    # 1) Monid litescrape /telegram/posts
    try:
        if monid_fn is not None:
            m_payload = monid_fn(ch, limit)
        elif not custom_mode:
            try:
                from leadgen.monid_client import check_monid_ready, run_monid
            except ImportError:
                from monid_client import check_monid_ready, run_monid
            m_payload = (
                run_monid("litescrape", "/telegram/posts", query={"channel": ch}, wait_sec=8, timeout=12)
                if check_monid_ready()
                else None
            )
        else:
            m_payload = None
        posts = _extract_post_texts(m_payload, limit)
        if posts:
            return posts
    except Exception:
        pass

    # 2) Treg crawl4ai.telegram.channel.posts
    try:
        if treg_fn is not None:
            t_payload = treg_fn(ch, limit)
        elif not custom_mode:
            try:
                from leadgen.treg_client import call as treg_call, check_treg_ready
            except ImportError:
                from treg_client import call as treg_call, check_treg_ready
            t_payload = (
                treg_call("crawl4ai.telegram.channel.posts", method="POST", data={"channel": ch, "limit": limit}, timeout=12)
                if check_treg_ready()
                else None
            )
        else:
            t_payload = None
        posts = _extract_post_texts(t_payload, limit)
        if posts:
            return posts
    except Exception:
        pass

    # 3) Public https://t.me/s/<channel> HTML fallback (custom test hooks)
    if custom_mode:
        try:
            tme_url = f"https://t.me/s/{ch}"
            if http_get_fn is not None:
                html_txt = http_get_fn(tme_url) or ""
            else:
                html_txt = default_fetch_html(tme_url)
            posts = _parse_tme_s_html(str(html_txt or ""), limit)
            if posts:
                return posts
        except Exception:
            pass
    return []


async def scan_ad_channels(
    client,
    channels: list[str],
    msg_limit: int = 40,
    seen_path: str = DEFAULT_SEEN_PATH,
    output_path: str = "ad_spy_leads.jsonl",
    delay: float = 1.0,
    fetch_html=default_fetch_html,
    public_posts_fn=None,
    max_runtime_sec: float = 150.0,
) -> list[dict]:
    from types import SimpleNamespace

    seen = _load_seen(seen_path)
    cursor = _load_spy_cursor(seen_path)
    ordered_channels = [c for c in channels if c and c.strip()]
    if len(ordered_channels) > 1 and cursor > 0:
        shift = cursor % len(ordered_channels)
        ordered_channels = ordered_channels[shift:] + ordered_channels[:shift]
    deadline = (time.monotonic() + float(max_runtime_sec)) if max_runtime_sec and max_runtime_sec > 0 else None
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    discovered: list[dict] = []

    try:
        with open(output_path, "w", encoding="utf-8") as out_fh:
            for raw_ch in ordered_channels:
                if deadline is not None and time.monotonic() >= deadline:
                    print("# tg_ad_spy: runtime budget reached; saving partial state", file=sys.stderr)
                    break
                ch = raw_ch.strip().lstrip("@")
                if not ch:
                    continue
                messages = None
                if public_posts_fn is not None:
                    try:
                        raw_posts = public_posts_fn(ch, msg_limit) or []
                    except Exception:
                        raw_posts = []
                    if raw_posts:
                        messages = [
                            SimpleNamespace(id=idx + 1, message=str(txt))
                            for idx, txt in enumerate(raw_posts[:msg_limit])
                            if str(txt or "").strip()
                        ]
                if messages is None:
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
                    if deadline is not None and time.monotonic() >= deadline:
                        break
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
    finally:
        _save_seen(seen_path, seen, cursor=cursor + 1)
    return discovered


async def _async_main(args: argparse.Namespace) -> None:
    raw_channels = args.channels or os.getenv("TG_AD_SPY_CHANNELS", "")
    channels = [c.strip() for c in raw_channels.split(",") if c.strip()]
    if not channels and not getattr(args, "no_default_channels", False):
        channels = list(DEFAULT_AD_SPY_CHANNELS)
    if not channels:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8"):
            pass
        print("# tg_ad_spy: no channels configured; skipping (0 leads)", file=sys.stderr)
        return

    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")
    if not api_id or not api_hash:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8"):
            pass
        print("# tg_ad_spy: TG_API_ID / TG_API_HASH not set; skipping (0 leads)", file=sys.stderr)
        return

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
            public_posts_fn=fetch_public_channel_posts_http,
            max_runtime_sec=float(getattr(args, "max_runtime_sec", 150.0) or 150.0),
        )
        print(f"# tg_ad_spy: discovered {len(rows)} advertiser leads -> {args.output}", file=sys.stderr)
    finally:
        await client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description="Telegram Ad-Spy lead scraper")
    ap.add_argument("--session", default=DEFAULT_SESSION)
    ap.add_argument("--channels", default=os.getenv("TG_AD_SPY_CHANNELS", ""))
    ap.add_argument("--no-default-channels", action="store_true")
    ap.add_argument("--msg-limit", type=int, default=40)
    ap.add_argument("--seen", default=DEFAULT_SEEN_PATH)
    ap.add_argument("--output", required=True)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--max-runtime-sec", type=float, default=150.0)
    args = ap.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
