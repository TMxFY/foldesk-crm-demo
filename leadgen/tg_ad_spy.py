#!/usr/bin/env python3
"""Telegram Ad-Spy: intercept active advertisers buying posts in tech/SaaS/AI channels.

Scans recent posts in monitored Telegram channels, detects paid placements via UTM/ref
links, #реклама/erid/promo markers, or external @handle CTAs, and resolves the
advertiser's personal founder @username, website, or X handle.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
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

try:
    from leadgen import tg_web_client
except ImportError:
    import tg_web_client  # type: ignore

_ORIG_TG_WEB_CLIENT = tg_web_client

DEFAULT_SESSION = (
    os.environ.get("TG_SESSION")
    or os.environ.get("SCRAPER_TG_SESSION")
    or "/Users/timofei/.claude/skill-telegram/tim3.session"
)
DEFAULT_SEEN_PATH = "leadgen_state/ad_spy_seen.json"
DEFAULT_AD_SPY_CHANNELS: tuple[str, ...] = (
    # Core existing channels
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
    "ai_founders",
    "microconf",
    "startups",
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
    # Consumer AI / Apps & ML (EN + CIS/Global)
    "seeallochnaya",
    "denissexy",
    "ai_machinelearning_big_data",
    "serge_ai",
    "forklogAI",
    "data_secrets",
    "dl_stories",
    "abstractDL",
    "boris_again",
    "deep_learning_school_news",
    "ai_product",
    "aioftheday",
    "neuraldeep",
    "gonzo_ML_podcasts",
    "geosemanticaai",
    "its_capitan",
    "gpt_news",
    "cyberyozh_official",
    "huggingface",
    "openai_news",
    "bytebytego",
    # SaaS / Product / Startups / Indie Makers / VC
    "epicgrowth",
    "gopractice",
    "ProductMindset",
    "productsense",
    "ordinarypm",
    "brainbox_vc",
    "temno",
    "Startup_Jedi_RU",
    "DigitalStageXTech",
    "saas_founders",
    "mspodcast",
    "godnolytika",
    "leadsnotes",
    "lomovx",
    "mspiridonov",
    "petrochenkow",
    "ilya_krasinsky",
    "oshestakovdigital",
    "kgrbnv",
    "solokumi",
    "bulavkinaonduty",
    "katyagrows",
    "pplsense",
    "fuckup_files",
    "levelsio",
    "producthunt",
    "b2b_saas",
    # Mobile UA & Subscription Apps / ASO / App Growth
    "app_growth",
    "aso_ru",
    "asotrips",
    "aso_pro",
    "app2top",
    "android_dev_ru",
    # E-com / Shopify / D2C / Retail / SEO
    "retail_ru",
    "e_pepper",
    "rushagency_ru",
    "moremoney",
    "ad_tech",
    "shopify_plus",
    "wb_sellers",
    "oborot_ru",
    "ozon_business",
    # Crypto / Fintech / Prop Trading / Web3 Builders
    "tondev_news",
    "toncore",
    "tontech",
    "incrypted",
    "frank_media",
    "fm_invest",
    "smartlabnews",
    "markettwits",
    "bitkogan",
    "tradingview",
    "ftmo_official",
    "MyTonWalletEn",
    "tonkeeper_ru",
    "tinkoff_invest_official",
    "SMLT_IR",
    # Growth / Performance Marketing / Paid Traffic / Telegram Ads
    "dnative",
    "tkachuk_aliaksey",
    "epicgrowth_marketing",
    "pmm_1",
    "pmmfaq",
    "t_tutorial",
    "tginfo",
    "fb_killa",
    "greed_traf",
    "fromspytobuy",
    "solobuyernotess",
    "tyver_spy",
    "cpa_lenta",
    "partnerkin",
    "protraffic",
    "kiber_mastera",
    "marketing_digest",
)

AD_MARKER_RE = re.compile(
    r"(#реклама|#ad\b|#sponsored|#promo\b|#partner\b|#collab\b|\berid\s*[:=]?\s*[A-Za-z0-9\-]+|\bsponsored(?:\s+by)?\b|"
    r"(?:today['’]s\s+)?sponsor\s*:|"
    r"paid\s+partnership|brought\s+to\s+you\s+by|in\s+partnership\s+with|partnered\s+with|partner\s+post|"
    r"promo\s*code|coupon\s*code|discount\s*code|use\s+code\s+[A-Z0-9_-]{3,}|\b\d{1,2}%\s+off\b|affiliate\b|"
    r"партнерский\s+материал|партнёрский\s+материал|промокод\w*|на\s+правах\s+рекламы)",
    re.IGNORECASE,
)

CTA_RE = re.compile(
    r"(переход\w*|подпис\w*|читай\w*|забирай\w*|попробуй\w*|тестируй\w*|"
    r"запустил\w*|рекоменду\w*|совету\w*|по\s+ссылк\w*|бесплатн\w*|"
    r"check\s+(?:out|this\s+out)|try\s+(?:it|for\s+free)|free\s+trial|start\s+(?:your\s+)?free|"
    r"get\s+started|book\s+a\s+demo|request\s+(?:a\s+)?demo|learn\s+more|"
    r"join\b|follow\b|sign\s+up|launch\w*|grab\b|claim\b|visit\b|download\b|dm\s+@|built\s+by|shipped\b)",
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


_JUNK_HOST_TOKENS = (
    "fragment.com",
    "fragment_monitor",
    "tonviewer.com",
    "tonviewer",
    "getgems.io",
    "getgems",
    "ton.diamonds",
)
_JUNK_HANDLE_RE = re.compile(
    r"(?:^|_)(?:fragment|fragment_monitor|tonviewer|getgems|casino|slots|betting|1win|1xbet|mostbet|giveaway|airdrop|rozygrysh)(?:$|_)",
    re.IGNORECASE,
)
_JUNK_TEXT_RE = re.compile(
    r"(?:fragment\.com|fragment_monitor|tonviewer(?:\.com)?|getgems(?:\.io)?|"
    r"\b(?:username\s+auction|nft\s+auction|buy\s+usernames?|usernames?\s+for\s+sale|"
    r"аукцион\w*\s+(?:юзернейм\w*|никнейм\w*|ников|номеров)|выкуп\w*\s+юзернейм\w*|купить\s+юзернейм\w*|номер\w*\s+\+888|"
    r"casino|казино|slots|слоты|букмекер\w*|betting|sportsbook|ставки\s+на\s+спорт|1win|1xbet|mostbet|pin-?up|"
    r"фриспин\w*|freespin\w*|лудоман\w*|онлайн[- ]?казино|рулетк\w*|"
    r"giveaway\w*|розыгрыш\w*|гивэве\w*|airdrop\w*|аирдроп\w*|раздача\s+призов|конкурс\s+репостов|tap[- ]to[- ]earn|тапалк\w*)\b)",
    re.IGNORECASE,
)


def is_ad_spy_junk_target(url: str = "", handle: str = "", text: str = "") -> bool:
    """Filter Fragment/username auctions, TON explorers/NFT markets, casino/betting, giveaways, and bots."""
    raw_url = (url or "").strip()
    url_low = raw_url.lower()
    raw_handle = (handle or "").strip().lstrip("@")
    handle_low = raw_handle.lower()
    body = (text or "").strip()

    if raw_handle:
        if is_bot_handle(handle_low) or _JUNK_HANDLE_RE.search(handle_low):
            return True

    if url_low:
        if any(tok in url_low for tok in _JUNK_HOST_TOKENS):
            return True
        if re.search(r"(?:t\.me|telegram\.me|telegram\.dog)/[a-z0-9_]+bot(?:[/?#]|$)", url_low):
            return True
        if "?start=" in url_low or "?startgroup=" in url_low or "&start=" in url_low:
            return True

    if body and _JUNK_TEXT_RE.search(body):
        return True
    return False


def _should_use_web_first(client, *custom_fns) -> bool:
    if any(fn is not None for fn in custom_fns):
        return True
    if client is None or isinstance(client, TelegramClient):
        return True
    if tg_web_client is not _ORIG_TG_WEB_CLIENT:
        return True
    if tg_web_client is not None and getattr(tg_web_client, "has_patched_io", lambda: False)():
        return True
    return False


async def _resolve_tg_target(
    client,
    handle: str,
    *,
    probe_fn=None,
    posts_fn=None,
    http_get_fn=None,
) -> dict:
    """Resolve a mentioned @handle via Web-first (`tg_web_client`) and fall back to Telethon only on 'unknown'."""
    clean = handle.strip().lstrip("@")
    empty_res = {"id": 0, "username": "", "name": "", "company": "", "bio": "", "site": ""}
    if not clean or is_ad_spy_junk_target(handle=clean):
        return empty_res

    if _should_use_web_first(client, probe_fn, posts_fn, http_get_fn) and (
        probe_fn is not None or tg_web_client is not None
    ):
        try:
            probed = (
                probe_fn(clean)
                if probe_fn is not None
                else tg_web_client.probe_tme_handle_web(clean, http_get_fn=http_get_fn)
            )
        except Exception:
            probed = {"kind": "unknown"}
        kind = str((probed or {}).get("kind") or "unknown").lower()
        p_title = str((probed or {}).get("title") or "").strip()
        p_bio = str((probed or {}).get("bio") or "").strip()
        p_subs = int((probed or {}).get("subscribers") or 0)

        if kind == "user":
            return {
                "id": 0,
                "username": clean,
                "name": p_title or clean,
                "company": "",
                "bio": p_bio,
                "site": "",
            }
        if kind in ("bot", "missing"):
            return empty_res
        if kind == "channel":
            post_texts: list[str] = []
            info: dict = {}
            try:
                if posts_fn is not None:
                    res = posts_fn(clean, 5)
                    if isinstance(res, tuple) and len(res) == 2:
                        info = dict(res[0] or {})
                        post_texts = [str(p).strip() for p in (res[1] or []) if str(p or "").strip()]
                    else:
                        post_texts = [str(p).strip() for p in (res or []) if str(p or "").strip()]
                elif tg_web_client is not None:
                    info, post_texts = tg_web_client.fetch_channel_posts_web(
                        clean,
                        limit=5,
                        http_get_fn=http_get_fn,
                    )
            except Exception:
                info, post_texts = {}, []

            about = str(info.get("about") or info.get("bio") or p_bio or "").strip()
            title = str(info.get("title") or p_title or clean).strip()
            subs = int(info.get("subscribers") or p_subs or 0)
            parsed = parse_channel_founder_info(
                about=about,
                posts=post_texts,
                channel_username=clean,
                channel_title=title,
                subscribers=subs,
            )
            owner_username = parsed["username"] if parsed else ""
            site = parsed["site"] if parsed else extract_external_site([about] + post_texts)
            if owner_username and not is_ad_spy_junk_target(handle=owner_username):
                try:
                    u_probe = (
                        probe_fn(owner_username)
                        if probe_fn is not None
                        else tg_web_client.probe_tme_handle_web(owner_username, http_get_fn=http_get_fn)
                    )
                except Exception:
                    u_probe = {"kind": "unknown"}
                u_kind = str((u_probe or {}).get("kind") or "unknown").lower()
                if u_kind == "user":
                    return {
                        "id": 0,
                        "username": owner_username,
                        "name": str(u_probe.get("title") or "").strip() or title,
                        "company": title,
                        "bio": about,
                        "site": clean_product_url(site) or site,
                    }
                if u_kind == "unknown" and client is not None:
                    u_res = await _resolve_personal_user(client, owner_username)
                    return {
                        "id": u_res.get("id") or 0,
                        "username": owner_username,
                        "name": u_res.get("name") or title,
                        "company": title,
                        "bio": about,
                        "site": clean_product_url(site) or site,
                    }
            return {
                "id": 0,
                "username": "",
                "name": title,
                "company": title,
                "bio": about,
                "site": clean_product_url(site) or site,
            }
        # Only fall through to Telethon when Web returns "unknown" and client is not None.
        if client is None:
            return empty_res

    if client is None:
        return empty_res

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
        about = ""
        title = clean
        site = ""
        try:
            full = await client(GetFullChannelRequest(channel=entity))
            about = getattr(full.full_chat, "about", "") or ""
            chat_obj = full.chats[0] if getattr(full, "chats", None) else entity
            title = getattr(chat_obj, "title", "") or clean
            subs = getattr(full.full_chat, "participants_count", None) or getattr(entity, "participants_count", 0) or 0
            post_texts = []
            try:
                msgs = await client.get_messages(entity, limit=3)
                post_texts = [
                    (getattr(m, "message", None) or "").strip()
                    for m in (msgs or [])
                    if (getattr(m, "message", None) or "").strip()
                ]
            except Exception:
                post_texts = []
            parsed = parse_channel_founder_info(
                about=about,
                posts=post_texts,
                channel_username=clean,
                channel_title=title,
                subscribers=subs,
            )
            owner_username = parsed["username"] if parsed else ""
            site = parsed["site"] if parsed else extract_external_site([about] + post_texts)
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
        return {
            "id": getattr(entity, "id", 0) or 0,
            "username": "",
            "name": title,
            "company": title,
            "bio": about,
            "site": clean_product_url(site) or site,
        }

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

    def _extract_msg_text(node) -> str:
        for a_tag in node.find_all("a", href=True):
            href = html.unescape(str(a_tag.get("href") or "")).strip()
            label = a_tag.get_text(" ", strip=True)
            if href.startswith(("http://", "https://")) and href not in label:
                a_tag.replace_with(f"{label} {href} ")
        return " ".join(node.get_text(" ", strip=True).split())

    out: list[str] = []
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_text, "html.parser")
        wraps = soup.select(".tgme_widget_message_wrap")
        if wraps:
            for wrap in wraps:
                node = wrap.select_one(".tgme_widget_message_text")
                txt = _extract_msg_text(node) if node is not None else ""
                extras: list[str] = []
                for btn in wrap.select("a.tgme_widget_message_inline_button[href], a.tgme_widget_message_link_preview[href]"):
                    href = html.unescape(str(btn.get("href") or "")).strip()
                    if not href.startswith(("http://", "https://")):
                        continue
                    if re.match(r"^https?://(?:t\.me|telegram\.me)/[^/]+/\d+", href, re.I):
                        continue
                    if href not in txt and href not in extras:
                        extras.append(href)
                combined = " ".join([txt, *extras]).strip()
                if combined:
                    out.append(combined)
        else:
            for node in soup.select(".tgme_widget_message_text"):
                txt = _extract_msg_text(node)
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
    host_channel_keys = {c.strip().lstrip("@").lower() for c in ordered_channels}
    if len(ordered_channels) > 1 and cursor > 0:
        shift = cursor % len(ordered_channels)
        ordered_channels = ordered_channels[shift:] + ordered_channels[:shift]
    deadline = (time.monotonic() + float(max_runtime_sec)) if max_runtime_sec and max_runtime_sec > 0 else None
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    discovered: list[dict] = []
    channels_scanned = 0

    try:
        with open(output_path, "w", encoding="utf-8") as out_fh:
            for raw_ch in ordered_channels:
                if deadline is not None and time.monotonic() >= deadline:
                    print("# tg_ad_spy: runtime budget reached; saving partial state", file=sys.stderr)
                    break
                ch = raw_ch.strip().lstrip("@")
                if not ch:
                    continue
                ch_low = ch.lower()
                channels_scanned += 1
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
                    if client is None:
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
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    text = (getattr(msg, "message", None) or "").strip()
                    if not text:
                        continue
                    sig = detect_ad_post(text, host_channel=ch)
                    if not sig["is_ad"]:
                        continue

                    site = sig["product_site"]
                    tg_targets = [h for h in sig["tg_targets"] if not is_ad_spy_junk_target(handle=h)]
                    if is_ad_spy_junk_target(
                        url=site,
                        handle=(tg_targets[0] if tg_targets else ""),
                        text=text,
                    ):
                        continue
                    if not site and not tg_targets:
                        continue

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

                    if is_ad_spy_junk_target(
                        url=site,
                        handle=resolved_username,
                        text=resolved_bio,
                    ):
                        continue

                    if resolved_username and resolved_username.lower() == ch_low:
                        resolved_username = ""

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
                        if not resolved_username and meta["tg_username"] and meta["tg_username"].lower() != ch_low:
                            resolved_username = meta["tg_username"]
                        if meta["x_handle"]:
                            x_handle = meta["x_handle"]

                    if not x_handle:
                        for m_x in X_URL_RE.finditer(text):
                            cand = m_x.group(1)
                            if cand.lower() not in X_SKIP_HANDLES:
                                x_handle = cand
                                break

                    site_low = site.lower() if site else ""
                    site_host = urlparse(site).netloc.removeprefix("www.").lower() if site else ""
                    adv_keys = [
                        k
                        for k in (
                            resolved_username.lower() if resolved_username else "",
                            x_handle.lower() if x_handle else "",
                            site_low,
                            site_host,
                        )
                        if k and k not in host_channel_keys
                    ]
                    dedup_key = (resolved_username or x_handle or site).lower()
                    if not dedup_key or not adv_keys:
                        continue
                    if any(k in seen for k in adv_keys):
                        continue
                    for k in adv_keys:
                        seen.add(k)

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

                    fallback_id = (
                        int.from_bytes(hashlib.blake2b(dedup_key.encode("utf-8"), digest_size=4).digest(), "big")
                        & 0x7FFFFFFF
                    ) or 1
                    row: dict = {
                        "id": resolved_id if resolved_id else None,
                        "dedup_id": fallback_id,
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
        step = channels_scanned if 0 < channels_scanned < len(ordered_channels) else 1
        _save_seen(seen_path, seen, cursor=cursor + step)
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
