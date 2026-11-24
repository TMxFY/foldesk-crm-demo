#!/usr/bin/env python3
"""Telegram Mini Apps (TMA) whale buyer discovery and bot-to-human resolution.

Discovers paid/subscription Telegram Mini Apps across:
1. findmini.app category pages (`stars`, `wallets`, `exchanges`, `astrology`,
   `homework_help`, `marketplaces`, `travel`, `music_and_shazam`);
2. Curated sections of GitHub `awesome-telegram-mini-apps` READMEs (`Finance`,
   `Tools`, `Wallets`, `Social`, `AI Agents`, `Shopping` — strictly skipping
   `Games`, `NFT`, `Staking`, `Contents`, `Legend`, `Contributing`, `About`);
3. tapps.center Next.js (`__NEXT_DATA__`, `__next_f`, HTML links);
4. Optional bounded `crawl4ai.web.search` (`"telegram mini app" AI OR VPN OR
   fintech site:t.me`) only when `allow_paid=True` and `BUDGET_PAID_ALLOWED != "0"`.

Rejects tap-to-earn, clicker, airdrop, casino/gambling, giveaway, and referral-farm
junk, resolves bot handles via `$0` `t.me` web probes and channel post inspection,
and strictly enforces the Sacred Invariant that `*bot` handles stay in
`tma_bot_handle` and NEVER enter `username`, `telegram_username`, or `tg_username`.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
import hashlib
import html as html_mod
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - optional minimal runtime fallback
    BeautifulSoup = None  # type: ignore[assignment]

try:
    from leadgen import tg_web_client
    from leadgen.supply_side import PLATFORM_AGGREGATOR_HOSTS
    from leadgen.tg_channel_graph import (
        SKIP_SITE_HOSTS,
        is_ad_manager_handle,
        is_bot_handle,
        parse_channel_founder_info,
    )
    from leadgen.tg_to_gsheet import email_of_text
    from leadgen.treg_client import call as treg_call, check_treg_ready
except ImportError:  # pragma: no cover - direct script execution fallback
    import tg_web_client  # type: ignore[no-redef]
    from supply_side import PLATFORM_AGGREGATOR_HOSTS  # type: ignore[no-redef]
    from tg_channel_graph import (  # type: ignore[no-redef]
        SKIP_SITE_HOSTS,
        is_ad_manager_handle,
        is_bot_handle,
        parse_channel_founder_info,
    )
    from tg_to_gsheet import email_of_text  # type: ignore[no-redef]
    from treg_client import call as treg_call, check_treg_ready  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

SEEN_DAYS = 90
MAX_PROBES_PER_CANDIDATE = 8
MAX_CHANNEL_POST_FETCHES = 2
DEFAULT_HTTP_TIMEOUT_SEC = 10.0

DEFAULT_FINDMINI_CATEGORIES: tuple[str, ...] = (
    "stars",
    "wallets",
    "exchanges",
    "astrology",
    "homework_help",
    "marketplaces",
    "travel",
    "music_and_shazam",
)

AWESOME_TMA_URLS: tuple[str, ...] = (
    "https://raw.githubusercontent.com/SzczepanBEN/awesome-telegram-mini-apps/main/README.md",
    "https://raw.githubusercontent.com/telegram-mini-apps-dev/awesome-telegram-mini-apps/main/README.md",
)

TAPPS_CENTER_URL = "https://tapps.center/"
TMA_SEARCH_QUERY = '"telegram mini app" AI OR VPN OR fintech site:t.me'

ALLOWED_AWESOME_SECTIONS: frozenset[str] = frozenset(
    {
        "finance",
        "tools",
        "wallets",
        "social",
        "ai agents",
        "shopping",
    }
)
SKIPPED_AWESOME_SECTIONS: frozenset[str] = frozenset(
    {
        "games",
        "gaming",
        "nft",
        "nfts",
        "staking",
        "contents",
        "table of contents",
        "legend",
        "contributing",
        "about",
        "license",
    }
)

_CANONICAL_SECTION_LABELS: dict[str, str] = {
    "finance": "Finance",
    "tools": "Tools",
    "wallets": "Wallets",
    "social": "Social",
    "ai agents": "AI Agents",
    "shopping": "Shopping",
}

_NOISE_TG_HANDLES: frozenset[str] = frozenset(
    {
        "joinchat",
        "share",
        "addstickers",
        "addemoji",
        "addlist",
        "addtheme",
        "proxy",
        "socks",
        "iv",
        "boost",
        "s",
        "c",
        "login",
        "contact",
    }
)

# Canonical personal-handle suffix blocklist shared with vps_autonomous_replenish.py.
_NON_PERSONAL_TG_SUFFIXES: tuple[str, ...] = (
    "_official",
    "official",
    "_channel",
    "channel",
    "_news",
    "_announcements",
    "_announcement",
    "_community",
    "_group",
    "_chat",
    "_support",
    "_helpdesk",
    "_updates",
    "_alerts",
    "bot",
    "_bot",
)

_PERSONAL_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_LOOSE_TG_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")
_HANDLE_IN_TEXT_RE = re.compile(
    r"(?:@|(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?|tg://resolve\?domain=)"
    r"([A-Za-z][A-Za-z0-9_]{2,31})\b",
    re.I,
)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>()\[\]\"']+", re.I)
_X_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#]|$)",
    re.I,
)
_X_NOISE = frozenset({"home", "search", "explore", "i", "share", "intent", "hashtag", "compose"})

_HIGH_INTENT_CONTACT_HINT_RE = re.compile(
    r"(?:founder|co[- ]?founder|owner|ceo|creator|built\s+by|author|"
    r"contact|support|help|dm\b|pm\b|reach\s+out|inquir\w*|feedback|team|"
    r"основател\w*|соосновател\w*|владел\w*|создател\w*|фаундер\w*|автор\w*|"
    r"связ\w*|поддержк\w*|личк\w*|лс\b|вопрос\w*)",
    re.I,
)

_REJECT_CATEGORY_EXACT: frozenset[str] = frozenset(
    {
        "games",
        "gaming",
        "game",
        "gambling",
        "casino",
        "slots",
        "roulette",
        "betting",
        "airdrop",
        "airdrops",
        "tap-to-earn",
        "tap to earn",
        "spin-to-earn",
        "spin to earn",
        "clicker",
        "nft",
        "staking",
        "meme coin",
        "memecoin",
        "giveaway",
        "referral farm",
        "farm coin",
    }
)

_REJECT_TMA_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tap-to-earn", re.compile(r"\btap[\s\-]*(?:to|2)[\s\-]*earn\b|тапалк\w*", re.I)),
    ("spin-to-earn", re.compile(r"\bspin[\s\-]*(?:to|2)[\s\-]*earn\b|\bspin\s+the\s+wheel\b", re.I)),
    ("clicker", re.compile(r"\bclicker\w*\b|\btapper\w*\b|кликер\w*", re.I)),
    ("airdrop", re.compile(r"\bairdrop\w*\b|аирдроп\w*|эйрдроп\w*", re.I)),
    ("casino", re.compile(r"\bcasino\w*\b|казино", re.I)),
    ("gambling", re.compile(r"\bgambl\w*\b|гембл\w*|лудоман\w*", re.I)),
    ("slots", re.compile(r"\bslots\b|\bslot[\s\-]*machine\w*\b|слоты|фриспин\w*|freespin\w*", re.I)),
    ("roulette", re.compile(r"\broulette\w*\b|рулетк\w*", re.I)),
    ("betting", re.compile(r"\bbetting\b|\bsportsbook\b|\bbookmaker\b|\bставки\b|букмекер\w*", re.I)),
    ("meme_coin", re.compile(r"\bmeme[\s\-]*coins?\b|мем[\s\-]*коин\w*", re.I)),
    ("toncoin_miner", re.compile(r"\b(?:ton(?:coin)?|crypto|cloud)[\s\-]*min(?:er|ing)\w*\b|майнер\w*", re.I)),
    ("giveaway", re.compile(r"\bgiveaway\w*\b|розыгрыш\w*", re.I)),
    ("referral_farm", re.compile(r"\breferral\s+farm\w*\b|\bref[\s\-]*farm\w*\b", re.I)),
    ("farm_coin", re.compile(r"\bfarm(?:ing)?\s+coins?\b|\bcoin\s+farm(?:ing)?\b", re.I)),
)

_FINDMINI_NON_APP_SLUGS: frozenset[str] = frozenset(
    {
        "category",
        "categories",
        "tag",
        "tags",
        "about",
        "submit",
        "add",
        "blog",
        "news",
        "privacy",
        "terms",
        "tos",
        "faq",
        "contact",
        "login",
        "signup",
        "register",
        "search",
        "page",
        "static",
        "assets",
        "images",
        "api",
        "top",
        "new",
        "popular",
        "best",
        "trending",
        "collections",
        "collection",
        "catalog",
        "bot-catalog",
        "en",
        "ru",
        "uk",
        "es",
        "de",
        "fr",
        "home",
        "index",
    }
)

_GENERIC_CTA_LABELS: frozenset[str] = frozenset(
    {
        "open",
        "launch",
        "start",
        "open app",
        "open mini app",
        "launch app",
        "open in telegram",
        "telegram",
        "bot",
        "link",
        "website",
        "site",
        "view",
        "details",
        "more",
        "try",
        "run",
        "visit",
        "app",
    }
)

_SKIP_WEBSITE_HOSTS: frozenset[str] = frozenset(
    {
        *SKIP_SITE_HOSTS,
        *PLATFORM_AGGREGATOR_HOSTS,
        "findmini.app",
        "tapps.center",
        "ton.app",
        "miniapps.ai",
        "telegram-mini-apps.github.io",
        "raw.githubusercontent.com",
        "img.shields.io",
        "shields.io",
        "badgen.net",
        "googleusercontent.com",
        "gstatic.com",
        "w3.org",
        "schema.org",
        "apple.com",
        "google.com",
    }
)
_STATIC_ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".woff", ".woff2")


def _default_state_dir() -> Path:
    custom = (
        os.environ.get("LEADGEN_STATE_DIR")
        or os.environ.get("CRMTG_LEADGEN_STATE_DIR")
        or ""
    ).strip()
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent.parent / "data" / "leadgen_state"


def _is_paid_allowed(allow_paid: bool) -> bool:
    if not allow_paid:
        return False
    raw = str(os.environ.get("BUDGET_PAID_ALLOWED") or "").strip().lower()
    return raw not in {"0", "false", "no"}


def _is_real_personal_tg_handle(raw_username: Any) -> bool:
    u = str(raw_username or "").strip().lstrip("@")
    if not u or not _PERSONAL_HANDLE_RE.fullmatch(u):
        return False
    low = u.lower()
    if low in _NOISE_TG_HANDLES or any(low.endswith(sfx) for sfx in _NON_PERSONAL_TG_SUFFIXES):
        return False
    return True


def _normalize_tg_handle(raw: Any) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    if "tg://resolve" in txt.lower():
        m = re.search(r"domain=([A-Za-z][A-Za-z0-9_]{2,31})", txt, re.I)
        txt = m.group(1) if m else ""
    elif any(host in txt.lower() for host in ("t.me/", "telegram.me/", "telegram.dog/")):
        txt = re.sub(
            r"^https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?",
            "",
            txt,
            flags=re.I,
        )
        txt = txt.split("/", 1)[0]
    txt = txt.split("?", 1)[0].split("#", 1)[0].strip().lstrip("@")
    if not txt or txt.lower() in _NOISE_TG_HANDLES:
        return ""
    if not _LOOSE_TG_HANDLE_RE.fullmatch(txt):
        return ""
    return txt


def _extract_tg_handle_from_url(url: str) -> tuple[str, bool]:
    """Return `(handle, is_miniapp_url)` for `t.me/...` or `tg://resolve` links."""
    raw = str(url or "").strip()
    if not raw:
        return "", False
    low = raw.lower()
    if not (
        "t.me/" in low
        or "telegram.me/" in low
        or "telegram.dog/" in low
        or low.startswith("tg://resolve")
    ):
        return "", False
    handle = _normalize_tg_handle(raw)
    if not handle:
        return "", False
    is_miniapp = bool(
        is_bot_handle(handle)
        or "startapp=" in low
        or re.search(r"/(?:app|start)(?:[/?#]|$)", low)
    )
    return handle, is_miniapp


def _clean_external_website(raw_url: Any) -> str:
    raw = str(raw_url or "").strip().rstrip(".,;:!?)\"'>]")
    if not raw or " " in raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    if not host or "." not in host:
        return ""
    if host in _SKIP_WEBSITE_HOSTS or any(host.endswith("." + sk) for sk in _SKIP_WEBSITE_HOSTS):
        return ""
    if (parsed.path or "").lower().endswith(_STATIC_ASSET_EXTS):
        return ""
    return raw


def _call_with_supported_kwargs(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return fn(*args, **kwargs)
        filtered = {k: v for k, v in kwargs.items() if k in params}
        return fn(*args, **filtered)
    except (TypeError, ValueError):
        return fn(*args)


def _extract_handles_and_urls(
    texts: Sequence[str],
    extra_urls: Sequence[str] = (),
) -> tuple[list[str], list[str], str]:
    """Extract ordered unique TG handles, non-bot links, and best external product website."""
    handles: list[str] = []
    seen_h: set[str] = set()
    links: list[str] = []
    seen_l: set[str] = set()
    website = ""

    def _add_handle(raw_h: str) -> None:
        h = _normalize_tg_handle(raw_h)
        if h and h.lower() not in seen_h:
            seen_h.add(h.lower())
            handles.append(h)

    def _add_url(raw_u: str) -> None:
        nonlocal website
        u = str(raw_u or "").strip().rstrip(".,;:!?)\"'>]")
        if not u:
            return
        h, _ = _extract_tg_handle_from_url(u)
        if h:
            _add_handle(h)
            if not is_bot_handle(h):
                norm_tg = f"https://t.me/{h}"
                if norm_tg.lower() not in seen_l:
                    seen_l.add(norm_tg.lower())
                    links.append(norm_tg)
            return
        ext = _clean_external_website(u)
        if ext:
            if not website:
                website = ext
            if ext.lower() not in seen_l:
                seen_l.add(ext.lower())
                links.append(ext)

    for url_item in extra_urls:
        _add_url(url_item)

    for block in texts:
        text = str(block or "")
        if not text:
            continue
        for url_match in _URL_IN_TEXT_RE.findall(text):
            _add_url(url_match)
        for handle_match in _HANDLE_IN_TEXT_RE.finditer(text):
            _add_handle(handle_match.group(1))

    return handles, links, website


def _build_candidate_dict(
    *,
    name: str,
    description: str,
    category: str,
    source: str,
    source_url: str = "",
    tma_bot_handle: str = "",
    tg_handles: Sequence[str] = (),
    links: Sequence[str] = (),
    website: str = "",
    slug: str = "",
) -> dict[str, Any]:
    clean_name = " ".join(str(name or "").split()).strip()
    clean_desc = " ".join(str(description or "").split()).strip()
    clean_cat = " ".join(str(category or "").split()).strip()

    ordered_handles: list[str] = []
    seen_h: set[str] = set()

    def _push_h(raw_h: Any) -> None:
        h = _normalize_tg_handle(raw_h)
        if h and h.lower() not in seen_h:
            seen_h.add(h.lower())
            ordered_handles.append(h)

    _push_h(tma_bot_handle)
    for item in tg_handles:
        _push_h(item)

    bot_handle = _normalize_tg_handle(tma_bot_handle)
    if bot_handle and not is_bot_handle(bot_handle):
        bot_handle = ""
    if not bot_handle:
        for h in ordered_handles:
            if is_bot_handle(h):
                bot_handle = h
                break

    clean_site = _clean_external_website(website)
    dedup_links: list[str] = []
    seen_l: set[str] = set()
    for raw_l in [clean_site, *links]:
        val = str(raw_l or "").strip()
        if not val:
            continue
        h, _ = _extract_tg_handle_from_url(val)
        if h and is_bot_handle(h):
            continue
        if h:
            val = f"https://t.me/{h}"
        elif not clean_site:
            cand_site = _clean_external_website(val)
            if cand_site:
                clean_site = cand_site
                val = cand_site
        if val.lower() not in seen_l:
            seen_l.add(val.lower())
            dedup_links.append(val)

    return {
        "name": clean_name or bot_handle or (ordered_handles[0] if ordered_handles else slug),
        "description": clean_desc,
        "category": clean_cat,
        "source": source,
        "source_url": str(source_url or "").strip(),
        "tma_bot_handle": bot_handle,
        "tg_handles": ordered_handles,
        "links": dedup_links,
        "website": clean_site,
        "slug": str(slug or "").strip(),
    }


def is_rejected_tma_app(name: str, description: str = "", category: str = "") -> tuple[bool, str]:
    """Reject tap-to-earn, clickers, airdrops, gambling, giveaways, and coin/ref farms."""
    cat_low = " ".join(str(category or "").strip().lower().split())
    if cat_low in _REJECT_CATEGORY_EXACT:
        return True, f"category:{cat_low}"

    blob = " ".join(
        part.strip()
        for part in (str(name or ""), str(description or ""), str(category or ""))
        if part and str(part).strip()
    )
    if not blob:
        return False, ""

    for tag, pattern in _REJECT_TMA_RULES:
        if pattern.search(blob):
            return True, tag
    return False, ""


def _parse_findmini_internal_slug(href: str) -> str:
    raw = str(href or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlparse(raw)
        except ValueError:
            return ""
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host != "findmini.app":
            return ""
        path = parsed.path or ""
    elif raw.startswith("/"):
        path = raw.split("?", 1)[0].split("#", 1)[0]
    else:
        return ""

    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return ""
    if parts[0].lower() in _FINDMINI_NON_APP_SLUGS:
        return ""
    if len(parts) == 2 and parts[0].lower() in {"app", "apps", "bot", "bots", "mini-app", "mini-apps"}:
        slug = parts[1]
    elif len(parts) == 1:
        slug = parts[0]
    else:
        return ""
    if slug.lower() in _FINDMINI_NON_APP_SLUGS or not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", slug):
        return ""
    return slug


def _card_anchors(node: Any) -> list[Any]:
    """Return `node` itself (if it is an `<a href>`) plus all descendant `<a href>` tags."""
    anchors: list[Any] = []
    if getattr(node, "name", None) == "a" and node.has_attr("href"):
        anchors.append(node)
    anchors.extend(node.find_all("a", href=True))
    return anchors


def _card_app_identity_keys(node: Any) -> tuple[set[str], set[str], set[str]]:
    """Return `(internal_slugs, bot_handles, all_tg_handles)` inside a DOM subtree."""
    slugs: set[str] = set()
    bot_handles: set[str] = set()
    tg_handles: set[str] = set()
    for a_tag in _card_anchors(node):
        href = html_mod.unescape(str(a_tag.get("href") or "")).strip()
        slug = _parse_findmini_internal_slug(href)
        if slug:
            slugs.add(slug.lower())
        h, _ = _extract_tg_handle_from_url(href)
        if h:
            tg_handles.add(h.lower())
            if is_bot_handle(h):
                bot_handles.add(h.lower())
    return slugs, bot_handles, tg_handles


def _is_multi_card_container(node: Any) -> bool:
    slugs, bot_handles, tg_handles = _card_app_identity_keys(node)
    if len(slugs) > 1 or len(bot_handles) > 1:
        return True
    if not slugs and not bot_handles and len(tg_handles) > 1:
        return True
    return False


def _find_card_container(anchor: Any) -> Any:
    """Walk up at most 4 levels to find the enclosing single-app card element."""
    best = anchor
    cur = anchor.parent
    for _ in range(4):
        if cur is None or getattr(cur, "name", None) in {
            "body",
            "html",
            "main",
            "ul",
            "ol",
            "table",
            "tbody",
            "[document]",
        }:
            break
        if _is_multi_card_container(cur):
            break
        cls = " ".join(cur.get("class") or []).lower()
        tag_name = str(getattr(cur, "name", "") or "").lower()
        best = cur
        if tag_name in {"article", "li", "tr"} or any(tok in cls for tok in ("card", "app", "item", "row")):
            if cur.find(["h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "div"]):
                return cur
        cur = cur.parent
    return best


def _extract_card_title_and_desc(container: Any, anchors: Sequence[Any]) -> tuple[str, str]:
    title = ""
    for selector in ("h1", "h2", "h3", "h4", "h5", "h6", "[class*='title']", "[class*='name']", "strong", "b"):
        node = container.select_one(selector)
        if node is not None:
            cand = " ".join(node.get_text(" ", strip=True).split())
            if cand and cand.lower() not in _GENERIC_CTA_LABELS:
                title = cand
                break
    if not title:
        for a_tag in anchors:
            cand = " ".join(a_tag.get_text(" ", strip=True).split())
            if cand and cand.lower() not in _GENERIC_CTA_LABELS:
                title = cand
                break

    desc = ""
    for selector in ("p", "[class*='desc']", "[class*='summary']", "[class*='subtitle']", "[class*='excerpt']"):
        for node in container.select(selector):
            cand = " ".join(node.get_text(" ", strip=True).split())
            if cand and cand != title and cand.lower() not in _GENERIC_CTA_LABELS:
                desc = cand
                break
        if desc:
            break

    if not desc:
        raw_text = " ".join(container.get_text(" ", strip=True).split())
        if title and raw_text.startswith(title):
            raw_text = raw_text[len(title) :].strip(" -–—:|")
        for cta in sorted(_GENERIC_CTA_LABELS, key=len, reverse=True):
            raw_text = re.sub(rf"\b{re.escape(cta)}\b$", "", raw_text, flags=re.I).strip(" -–—:|")
        if raw_text and raw_text != title:
            desc = raw_text
    return title, desc


def parse_findmini_category_html(html: str, category: str) -> list[dict[str, Any]]:
    """Parse app card links (`/bot_slug/`, `t.me/...`), titles, and descriptions from findmini HTML."""
    raw_html = str(html or "").strip()
    if not raw_html:
        return []

    candidates: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw_html, "html.parser")
        grouped: dict[int, tuple[Any, list[Any]]] = {}
        for a_tag in soup.find_all("a", href=True):
            href = html_mod.unescape(str(a_tag.get("href") or "")).strip()
            slug = _parse_findmini_internal_slug(href)
            tg_h, _ = _extract_tg_handle_from_url(href)
            if not slug and not tg_h:
                continue
            card = _find_card_container(a_tag)
            cid = id(card)
            if cid not in grouped:
                grouped[cid] = (card, [])
            grouped[cid][1].append(a_tag)

        for card, anchors in grouped.values():
            hrefs: list[str] = []
            slug = ""
            bot_handle = ""
            tg_handles: list[str] = []
            source_url = ""
            for a_tag in _card_anchors(card):
                href = html_mod.unescape(str(a_tag.get("href") or "")).strip()
                if not href:
                    continue
                if href.startswith("/"):
                    hrefs.append(urljoin("https://findmini.app/", href))
                else:
                    hrefs.append(href)
                cand_slug = _parse_findmini_internal_slug(href)
                if cand_slug and not slug:
                    slug = cand_slug
                    source_url = urljoin("https://findmini.app/", f"/{cand_slug}/")
                tg_h, is_mini = _extract_tg_handle_from_url(href)
                if tg_h:
                    if tg_h not in tg_handles:
                        tg_handles.append(tg_h)
                    if (is_bot_handle(tg_h) or is_mini) and not bot_handle and is_bot_handle(tg_h):
                        bot_handle = tg_h
                    if not source_url:
                        source_url = f"https://t.me/{tg_h}"

            if not bot_handle and slug:
                slug_h = _normalize_tg_handle(slug)
                if slug_h:
                    if slug_h not in tg_handles:
                        tg_handles.append(slug_h)
                    if is_bot_handle(slug_h):
                        bot_handle = slug_h

            title, desc = _extract_card_title_and_desc(card, anchors)
            _, extra_links, website = _extract_handles_and_urls([title, desc], extra_urls=hrefs)
            dedup_key = (bot_handle or slug or (tg_handles[0] if tg_handles else "") or title).lower()
            if not dedup_key or dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            candidates.append(
                _build_candidate_dict(
                    name=title,
                    description=desc,
                    category=category,
                    source="findmini",
                    source_url=source_url,
                    tma_bot_handle=bot_handle,
                    tg_handles=tg_handles,
                    links=extra_links,
                    website=website,
                    slug=slug,
                )
            )
        return candidates

    # Regex fallback when bs4 is unavailable
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw_html, re.I | re.S):
        href = html_mod.unescape(m.group(1)).strip()
        label = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).split())
        slug = _parse_findmini_internal_slug(href)
        tg_h, _ = _extract_tg_handle_from_url(href)
        if not slug and not tg_h:
            continue
        bot_handle = tg_h if (tg_h and is_bot_handle(tg_h)) else (slug if slug and is_bot_handle(slug) else "")
        handles = [h for h in (tg_h, _normalize_tg_handle(slug)) if h]
        dedup_key = (bot_handle or slug or (handles[0] if handles else "") or label).lower()
        if not dedup_key or dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        candidates.append(
            _build_candidate_dict(
                name=label,
                description="",
                category=category,
                source="findmini",
                source_url=urljoin("https://findmini.app/", href) if href.startswith("/") else href,
                tma_bot_handle=bot_handle,
                tg_handles=handles,
                slug=slug,
            )
        )
    return candidates


def _normalize_markdown_heading(raw_heading: str) -> str:
    txt = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", raw_heading)
    txt = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", txt)
    txt = re.sub(r"[^\w\s-]", " ", txt, flags=re.UNICODE)
    return " ".join(txt.split()).casefold()


def _clean_markdown_text(text: str) -> str:
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"[*_`~]+", "", cleaned)
    return " ".join(cleaned.split()).strip(" -–—:|")


def parse_awesome_tma_markdown(md_text: str) -> list[dict[str, Any]]:
    """Parse ONLY Finance, Tools, Wallets, Social, AI Agents, and Shopping sections."""
    raw_md = str(md_text or "")
    if not raw_md.strip():
        return []

    candidates: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    in_code_fence = False
    in_allowed_section = False
    skip_subsection = False
    active_category = ""

    bullet_re = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.+)$")
    md_link_re = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    heading_re = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")

    for line in raw_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue

        h_match = heading_re.match(line)
        if h_match:
            level = len(h_match.group(1))
            norm_h = _normalize_markdown_heading(h_match.group(2))
            if level <= 2:
                skip_subsection = False
                if norm_h in ALLOWED_AWESOME_SECTIONS:
                    in_allowed_section = True
                    active_category = _CANONICAL_SECTION_LABELS[norm_h]
                else:
                    in_allowed_section = False
                    active_category = ""
            else:
                if in_allowed_section:
                    skip_subsection = norm_h in SKIPPED_AWESOME_SECTIONS
            continue

        if not in_allowed_section or skip_subsection:
            continue

        entry_text = ""
        b_match = bullet_re.match(line)
        if b_match:
            entry_text = b_match.group(1).strip()
        elif stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r"[:\-\s]*", c) for c in cells):
                continue
            if cells and cells[0].lower() in {"name", "app", "title", "project"}:
                continue
            entry_text = " - ".join(c for c in cells if c)

        if not entry_text:
            continue

        # Strip markdown image badges before reading links/text.
        clean_entry = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", entry_text).strip()
        link_pairs = [
            (m.group(1).strip(), html_mod.unescape(m.group(2).strip()))
            for m in md_link_re.finditer(clean_entry)
        ]
        urls = [u for _, u in link_pairs]
        handles, links, website = _extract_handles_and_urls([clean_entry], extra_urls=urls)
        if not handles and not website:
            continue

        name = ""
        for label, _url in link_pairs:
            clean_lbl = _clean_markdown_text(label)
            if clean_lbl and clean_lbl.lower() not in _GENERIC_CTA_LABELS:
                name = clean_lbl
                break
        if not name:
            bold_m = re.search(r"\*\*([^*]+)\*\*", clean_entry)
            if bold_m:
                name = _clean_markdown_text(bold_m.group(1))
        if not name:
            plain_entry = re.sub(r"https?://\S+", "", _clean_markdown_text(clean_entry))
            plain_entry = re.sub(r"\(\s*\)", "", plain_entry)
            prefix = re.split(r"\s+[—–-]\s+|:\s+", plain_entry, maxsplit=1)[0]
            name = prefix.strip(" -–—:|()")

        desc = _clean_markdown_text(clean_entry)
        if name and desc.lower().startswith(name.lower()):
            desc = desc[len(name) :].strip(" -–—:|")

        bot_handle = next((h for h in handles if is_bot_handle(h)), "")
        source_url = f"https://t.me/{bot_handle}" if bot_handle else (f"https://t.me/{handles[0]}" if handles else website)
        dedup_key = (bot_handle or (handles[0] if handles else "") or website or name).lower()
        if not dedup_key or dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        candidates.append(
            _build_candidate_dict(
                name=name,
                description=desc,
                category=active_category,
                source="awesome_tma",
                source_url=source_url,
                tma_bot_handle=bot_handle,
                tg_handles=handles,
                links=links,
                website=website,
            )
        )

    return candidates


def _extract_tapps_candidate_from_mapping(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    name = ""
    for key in ("name", "title", "appName", "app_name"):
        val = obj.get(key)
        if isinstance(val, str) and len(val.strip()) >= 2:
            name = " ".join(val.split())
            break
    if not name:
        return None

    desc = ""
    for key in ("description", "shortDescription", "short_description", "tagline", "subtitle", "summary", "about"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            desc = " ".join(val.split())
            break

    cat = ""
    for key in ("category", "categoryName", "category_name"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            cat = val.strip()
            break
        if isinstance(val, Mapping) and isinstance(val.get("name"), str):
            cat = val["name"].strip()
            break
    if not cat:
        for list_key in ("categories", "tags"):
            vals = obj.get(list_key)
            if isinstance(vals, list) and vals:
                first = vals[0]
                if isinstance(first, str) and first.strip():
                    cat = first.strip()
                    break
                if isinstance(first, Mapping) and isinstance(first.get("name"), str):
                    cat = first["name"].strip()
                    break

    raw_urls: list[str] = []
    raw_handles: list[str] = []
    bot_handle = ""

    for key in (
        "bot_username",
        "botUsername",
        "bot_handle",
        "botHandle",
        "bot",
        "username",
        "handle",
    ):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            h = _normalize_tg_handle(val)
            if h:
                raw_handles.append(h)
                if not bot_handle and (is_bot_handle(h) or "bot" in key.lower()):
                    bot_handle = h

    for key in (
        "tg_link",
        "tgLink",
        "telegram_url",
        "telegramUrl",
        "tmeUrl",
        "tme_url",
        "bot_url",
        "botUrl",
        "app_url",
        "appUrl",
        "channel",
        "channelUrl",
        "channel_url",
        "support",
        "supportUrl",
        "support_url",
        "website",
        "websiteUrl",
        "website_url",
        "homepage",
        "site",
        "web_url",
        "url",
        "link",
    ):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            raw_urls.append(val.strip())

    slug = str(obj.get("slug") or "").strip()
    handles, links, website = _extract_handles_and_urls([desc], extra_urls=raw_urls)
    for h in raw_handles:
        if h not in handles:
            handles.append(h)

    has_tg_signal = bool(bot_handle or handles)
    has_app_desc_url = bool(desc and (website or slug or any("t.me/" in u.lower() for u in raw_urls)))
    if not has_tg_signal and not has_app_desc_url:
        return None
    if not bot_handle:
        bot_handle = next((h for h in handles if is_bot_handle(h)), "")

    source_url = next((u for u in raw_urls if "t.me/" in u.lower()), "") or (
        urljoin(TAPPS_CENTER_URL, f"/app/{slug}") if slug else ""
    )
    return _build_candidate_dict(
        name=name,
        description=desc,
        category=cat,
        source="tapps_center",
        source_url=source_url,
        tma_bot_handle=bot_handle,
        tg_handles=handles,
        links=links,
        website=website,
        slug=slug,
    )


def _walk_json_for_tapps(node: Any, out: list[dict[str, Any]], seen_ids: set[int]) -> None:
    nid = id(node)
    if nid in seen_ids:
        return
    if isinstance(node, Mapping):
        seen_ids.add(nid)
        cand = _extract_tapps_candidate_from_mapping(node)
        if cand is not None:
            out.append(cand)
        for val in node.values():
            if isinstance(val, (Mapping, list)):
                _walk_json_for_tapps(val, out, seen_ids)
    elif isinstance(node, list):
        seen_ids.add(nid)
        for item in node:
            if isinstance(item, (Mapping, list)):
                _walk_json_for_tapps(item, out, seen_ids)


def _extract_rsc_payload_texts(raw_html: str) -> list[str]:
    """Decode `self.__next_f.push([1, "..."])` chunks safely without regex backtracking."""
    decoded: list[str] = []
    decoder = json.JSONDecoder()
    marker = "__next_f.push("
    idx = 0
    while True:
        pos = raw_html.find(marker, idx)
        if pos < 0:
            break
        arr_start = raw_html.find("[", pos + len(marker))
        if arr_start < 0 or arr_start - pos > 64:
            idx = pos + len(marker)
            continue
        try:
            payload, end_pos = decoder.raw_decode(raw_html, arr_start)
        except json.JSONDecodeError:
            idx = pos + len(marker)
            continue
        if isinstance(payload, list):
            for elem in payload:
                if isinstance(elem, str) and elem:
                    decoded.append(elem)
        idx = max(pos + len(marker), end_pos)
    return decoded


def _scan_json_fragments_in_text(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    objs: list[Any] = []
    idx = 0
    n = len(text)
    while idx < n:
        pos = text.find("{", idx)
        arr_pos = text.find("[", idx)
        if pos < 0 or (0 <= arr_pos < pos):
            pos = arr_pos
        if pos < 0:
            break
        # Only attempt raw_decode near plausible app payloads to keep scanning fast.
        snippet = text[pos : pos + 240]
        if not any(tok in snippet for tok in ('"name"', '"title"', '"apps"', '"miniApps"', '"bot', '"t.me/')):
            idx = pos + 1
            continue
        try:
            obj, end_idx = decoder.raw_decode(text, pos)
            objs.append(obj)
            idx = max(pos + 1, end_idx)
        except json.JSONDecodeError:
            idx = pos + 1
    return objs


def parse_tapps_center_html(html: str) -> list[dict[str, Any]]:
    """Parse Next.js `__NEXT_DATA__`, `__next_f` RSC chunks, and href links from tapps.center HTML."""
    raw_html = str(html or "").strip()
    if not raw_html:
        return []

    raw_candidates: list[dict[str, Any]] = []

    # 1) __NEXT_DATA__ JSON block
    next_data_match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        raw_html,
        re.I | re.S,
    )
    if next_data_match:
        try:
            next_json = json.loads(html_mod.unescape(next_data_match.group(1).strip()))
            _walk_json_for_tapps(next_json, raw_candidates, set())
        except json.JSONDecodeError:
            pass

    # 2) Next.js App Router self.__next_f.push RSC payloads
    for rsc_chunk in _extract_rsc_payload_texts(raw_html):
        for frag in _scan_json_fragments_in_text(rsc_chunk):
            _walk_json_for_tapps(frag, raw_candidates, set())

    # 3) HTML href fallback / supplement
    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw_html, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            href = html_mod.unescape(str(a_tag.get("href") or "")).strip()
            tg_h, _ = _extract_tg_handle_from_url(href)
            app_slug = ""
            if not tg_h:
                m_slug = re.match(r"^(?:https?://(?:www\.)?tapps\.center)?/(?:apps?|mini-apps?)/([A-Za-z0-9_.-]{2,64})/?$", href, re.I)
                if m_slug:
                    app_slug = m_slug.group(1)
            if not tg_h and not app_slug:
                continue
            card = _find_card_container(a_tag)
            title, desc = _extract_card_title_and_desc(card, [a_tag])
            hrefs = [
                html_mod.unescape(str(link_tag.get("href") or "")).strip()
                for link_tag in _card_anchors(card)
            ]
            handles, links, website = _extract_handles_and_urls([title, desc], extra_urls=hrefs)
            if tg_h and tg_h not in handles:
                handles.insert(0, tg_h)
            bot_handle = next((h for h in handles if is_bot_handle(h)), "")
            raw_candidates.append(
                _build_candidate_dict(
                    name=title or app_slug or tg_h,
                    description=desc,
                    category="",
                    source="tapps_center",
                    source_url=href if href.startswith("http") else urljoin(TAPPS_CENTER_URL, href),
                    tma_bot_handle=bot_handle,
                    tg_handles=handles,
                    links=links,
                    website=website,
                    slug=app_slug,
                )
            )
    else:
        for m in re.finditer(r'<a[^>]+href=["\'](https?://(?:t\.me|telegram\.me)/[^"\']+)["\'][^>]*>(.*?)</a>', raw_html, re.I | re.S):
            href = html_mod.unescape(m.group(1)).strip()
            tg_h, _ = _extract_tg_handle_from_url(href)
            if not tg_h:
                continue
            label = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).split())
            raw_candidates.append(
                _build_candidate_dict(
                    name=label or tg_h,
                    description="",
                    category="",
                    source="tapps_center",
                    source_url=href,
                    tma_bot_handle=tg_h if is_bot_handle(tg_h) else "",
                    tg_handles=[tg_h],
                )
            )

    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in raw_candidates:
        keys = [
            k.lower()
            for k in (
                item.get("tma_bot_handle") or "",
                *(item.get("tg_handles") or ()),
                item.get("slug") or "",
                item.get("name") or "",
            )
            if str(k or "").strip()
        ]
        if not keys or any(k in seen_keys for k in keys):
            continue
        seen_keys.update(keys)
        deduped.append(item)
    return deduped


def _safe_probe(
    probe_fn: Callable[..., Any],
    handle: str,
    *,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    try:
        res = _call_with_supported_kwargs(probe_fn, handle, cache_path=cache_path)
    except Exception as exc:
        logger.debug("tma_apps probe failed for %s: %s", handle, exc)
        return {"handle": handle, "kind": "unknown", "title": "", "bio": "", "subscribers": 0}
    if isinstance(res, Mapping):
        return dict(res)
    return {"handle": handle, "kind": "unknown", "title": "", "bio": "", "subscribers": 0}


def _safe_fetch_posts(
    fetch_posts_fn: Callable[..., Any],
    channel: str,
    *,
    limit: int = 10,
    allow_paid: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    try:
        res = _call_with_supported_kwargs(
            fetch_posts_fn,
            channel,
            limit=limit,
            allow_paid=allow_paid,
        )
    except Exception as exc:
        logger.debug("tma_apps fetch_posts failed for %s: %s", channel, exc)
        return {}, []
    if isinstance(res, tuple) and len(res) == 2:
        info = dict(res[0]) if isinstance(res[0], Mapping) else {}
        raw_posts = res[1] if isinstance(res[1], Sequence) and not isinstance(res[1], (str, bytes)) else []
        posts = [str(p).strip() for p in raw_posts if str(p or "").strip()]
        return info, posts
    if isinstance(res, list):
        return {}, [str(p).strip() for p in res if str(p or "").strip()]
    return {}, []


def _scan_text_handles_with_priority(
    texts: Sequence[str],
    *,
    exclude_handles: set[str],
) -> list[tuple[int, str]]:
    """Extract candidate TG handles from text blocks ranked by founder/contact line context."""
    ranked: dict[str, int] = {}
    for src_idx, block in enumerate(texts):
        for line in str(block or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            has_hint = bool(_HIGH_INTENT_CONTACT_HINT_RE.search(stripped))
            for m in _HANDLE_IN_TEXT_RE.finditer(stripped):
                h = _normalize_tg_handle(m.group(1))
                if not h or h.lower() in exclude_handles:
                    continue
                if is_ad_manager_handle(h, stripped):
                    continue
                score = 3 if has_hint else (2 if src_idx == 0 else 1)
                prev = ranked.get(h, 0)
                if score > prev:
                    ranked[h] = score
    return sorted(((score, h) for h, score in ranked.items()), key=lambda x: (-x[0], x[1].lower()))


def resolve_tma_to_human_lead(
    candidate: Mapping[str, Any],
    *,
    probe_fn: Callable[..., Any] | None = None,
    fetch_posts_fn: Callable[..., Any] | None = None,
    cache_path: Path | None = None,
    allow_paid: bool = True,
) -> dict[str, Any]:
    """Resolve a TMA bot/channel candidate to a verified personal TG handle, official channels, and product website.

    Sacred Invariant: `*bot` / `*_bot` handles remain exclusively in `tma_bot_handle`
    and are NEVER assigned to `username`, `telegram_username`, or `tg_username`.
    """
    eff_probe = probe_fn if probe_fn is not None else tg_web_client.probe_tme_handle_web
    eff_fetch_posts = fetch_posts_fn if fetch_posts_fn is not None else tg_web_client.fetch_channel_posts_web
    eff_allow_paid = _is_paid_allowed(allow_paid)

    raw_name = str(candidate.get("name") or candidate.get("title") or candidate.get("project") or candidate.get("company") or "").strip()
    raw_desc = str(candidate.get("description") or candidate.get("bio") or candidate.get("what_they_do") or "").strip()
    raw_cat = str(candidate.get("category") or candidate.get("tma_category") or "").strip()

    seed_handles: list[str] = []
    handle_priority: dict[str, int] = {}

    def _enqueue(raw_h: Any, prio: int = 1) -> None:
        h = _normalize_tg_handle(raw_h)
        if not h:
            return
        low = h.lower()
        if low not in handle_priority:
            seed_handles.append(h)
        handle_priority[low] = max(handle_priority.get(low, 0), prio)

    # Seed from explicit candidate fields without trusting raw `username` blindly.
    bot_handle = ""
    raw_bot_cand = _normalize_tg_handle(candidate.get("tma_bot_handle"))
    if raw_bot_cand and is_bot_handle(raw_bot_cand):
        bot_handle = raw_bot_cand

    for key in ("owner_handle", "channel_handle", "tma_bot_handle", "username", "telegram_username", "tg_username"):
        val = _normalize_tg_handle(candidate.get(key))
        if val:
            if is_bot_handle(val) and not bot_handle:
                bot_handle = val
            _enqueue(val, prio=3 if key == "owner_handle" else 2)

    raw_links = candidate.get("links") if isinstance(candidate.get("links"), Sequence) and not isinstance(candidate.get("links"), (str, bytes)) else []
    extra_urls = [
        str(candidate.get("website") or ""),
        str(candidate.get("project_url") or ""),
        str(candidate.get("site") or ""),
        str(candidate.get("source_url") or ""),
        *(str(x) for x in raw_links),
    ]
    extracted_handles, initial_links, initial_website = _extract_handles_and_urls([raw_name, raw_desc], extra_urls=extra_urls)
    for h in extracted_handles:
        if is_bot_handle(h) and not bot_handle:
            bot_handle = h
        _enqueue(h, prio=2)

    if isinstance(candidate.get("tg_handles"), Sequence) and not isinstance(candidate.get("tg_handles"), (str, bytes)):
        for h in candidate["tg_handles"]:
            nh = _normalize_tg_handle(h)
            if nh:
                if is_bot_handle(nh) and not bot_handle:
                    bot_handle = nh
                _enqueue(nh, prio=2)

    if not seed_handles and candidate.get("slug"):
        _enqueue(candidate.get("slug"), prio=1)

    websites: list[str] = []
    if initial_website:
        websites.append(initial_website)
    official_channels: list[str] = []
    non_personal_tg_links: list[str] = []
    verified_users: list[tuple[int, str, dict[str, Any]]] = []
    bio_fragments: list[str] = [raw_desc] if raw_desc else []
    email_candidates: list[str] = []
    x_urls: list[str] = []

    def _collect_meta_from_texts(texts: Sequence[str]) -> None:
        _, _, ext_site = _extract_handles_and_urls(texts)
        if ext_site and ext_site not in websites:
            websites.append(ext_site)
        for txt in texts:
            em = email_of_text(txt)
            if em and em not in email_candidates:
                email_candidates.append(em)
            for xm in _X_URL_RE.finditer(txt or ""):
                xh = xm.group(1)
                if xh.lower() not in _X_NOISE:
                    xu = f"https://x.com/{xh}"
                    if xu not in x_urls:
                        x_urls.append(xu)

    _collect_meta_from_texts([raw_desc, *extra_urls])

    probed: dict[str, dict[str, Any]] = {}
    queue: list[str] = list(seed_handles)
    channel_fetches = 0

    while queue and len(probed) < MAX_PROBES_PER_CANDIDATE:
        cur = queue.pop(0)
        low = cur.lower()
        if low in probed:
            continue

        probe = _safe_probe(eff_probe, cur, cache_path=cache_path)
        probed[low] = probe
        kind = str(probe.get("kind") or "").strip().lower()
        p_handle = _normalize_tg_handle(probe.get("handle") or cur) or cur
        p_title = str(probe.get("title") or "").strip()
        p_bio = str(probe.get("bio") or "").strip()

        if p_bio and p_bio not in bio_fragments:
            bio_fragments.append(p_bio)
        _collect_meta_from_texts([p_bio])

        if kind == "bot" or is_bot_handle(p_handle):
            if not bot_handle:
                bot_handle = p_handle
            for prio, found_h in _scan_text_handles_with_priority([p_bio], exclude_handles={low}):
                _enqueue(found_h, prio=prio)
                if found_h.lower() not in probed and found_h not in queue:
                    queue.append(found_h)
            continue

        if kind == "channel":
            if p_handle not in official_channels:
                official_channels.append(p_handle)
            ch_texts = [p_bio]
            if channel_fetches < MAX_CHANNEL_POST_FETCHES:
                channel_fetches += 1
                ch_info, ch_posts = _safe_fetch_posts(
                    eff_fetch_posts,
                    p_handle,
                    limit=10,
                    allow_paid=eff_allow_paid,
                )
                ch_about = str(ch_info.get("about") or ch_info.get("bio") or "").strip()
                if ch_about and ch_about not in bio_fragments:
                    bio_fragments.append(ch_about)
                ch_title = str(ch_info.get("title") or p_title).strip()
                try:
                    ch_subs = int(ch_info.get("subscribers") or probe.get("subscribers") or 0)
                except (TypeError, ValueError):
                    ch_subs = 0
                founder_info = parse_channel_founder_info(
                    about=ch_about or p_bio,
                    posts=ch_posts,
                    channel_username=p_handle,
                    channel_title=ch_title,
                    subscribers=ch_subs,
                )
                if founder_info and founder_info.get("username"):
                    _enqueue(founder_info["username"], prio=4)
                    if founder_info["username"].lower() not in probed and founder_info["username"] not in queue:
                        queue.insert(0, founder_info["username"])
                if founder_info and founder_info.get("site"):
                    clean_f_site = _clean_external_website(founder_info["site"])
                    if clean_f_site and clean_f_site not in websites:
                        websites.append(clean_f_site)
                ch_texts.extend([ch_about, *ch_posts])
            _collect_meta_from_texts(ch_texts)
            for prio, found_h in _scan_text_handles_with_priority(ch_texts, exclude_handles={low}):
                _enqueue(found_h, prio=prio)
                if found_h.lower() not in probed and found_h not in queue:
                    queue.append(found_h)
            continue

        if kind == "user":
            # Enforce personal-handle invariant even if upstream probe labels a bot/official account as "user".
            if _is_real_personal_tg_handle(p_handle) and not is_bot_handle(p_handle):
                verified_users.append((handle_priority.get(low, 1), p_handle, probe))
            elif not is_bot_handle(p_handle):
                tg_url = f"https://t.me/{p_handle}"
                if tg_url not in non_personal_tg_links:
                    non_personal_tg_links.append(tg_url)

    # Pick highest-priority verified personal user.
    final_username = ""
    user_display_name = ""
    user_bio = ""
    if verified_users:
        verified_users.sort(key=lambda item: (-item[0], item[1].lower()))
        _prio, cand_u, cand_probe = verified_users[0]
        if _is_real_personal_tg_handle(cand_u) and not is_bot_handle(cand_u):
            final_username = cand_u
            user_display_name = str(cand_probe.get("title") or "").strip()
            user_bio = str(cand_probe.get("bio") or "").strip()

    # Final Sacred Invariant check: NEVER leak *bot into username fields.
    if final_username and (is_bot_handle(final_username) or not _is_real_personal_tg_handle(final_username)):
        final_username = ""

    final_website = websites[0] if websites else ""
    final_domain = ""
    if final_website:
        try:
            final_domain = (urlparse(final_website).hostname or "").lower().removeprefix("www.")
        except ValueError:
            final_domain = ""

    final_links: list[str] = []
    seen_out_links: set[str] = set()
    for link_cand in [
        final_website,
        *(f"https://t.me/{ch}" for ch in official_channels),
        f"https://t.me/{final_username}" if final_username else "",
        *non_personal_tg_links,
        *initial_links,
        *x_urls,
    ]:
        val = str(link_cand or "").strip()
        if not val:
            continue
        h, _ = _extract_tg_handle_from_url(val)
        if h and is_bot_handle(h):
            continue
        if val.lower() not in seen_out_links:
            seen_out_links.add(val.lower())
            final_links.append(val)

    project_name = raw_name or (final_domain.split(".")[0].capitalize() if final_domain else bot_handle)
    combined_bio = " | ".join(frag for frag in ([user_bio] if user_bio else []) + bio_fragments if frag)[:500]
    dedup_key = (final_username or bot_handle or final_website or project_name).lower()
    dedup_id = (
        int.from_bytes(hashlib.blake2b(dedup_key.encode("utf-8"), digest_size=4).digest(), "big")
        & 0x7FFFFFFF
    ) or 1

    contact_channels: list[str] = []
    if final_username:
        contact_channels.append("telegram")
    if final_website:
        contact_channels.append("site")
    if x_urls:
        contact_channels.append("x")
    if email_candidates:
        contact_channels.append("email")

    row: dict[str, Any] = {
        "dedup_id": dedup_id,
        "lead_source": "tma_apps",
        "source": "tma_apps",
        "segment": "whale",
        "category": "tma",
        "tma_category": raw_cat,
        "tma_bot_handle": bot_handle,
        "username": final_username,
        "telegram_username": final_username,
        "tg_username": final_username,
        "provider": "telegram" if final_username else "",
        "name": user_display_name or project_name or final_username,
        "company": project_name,
        "project": project_name,
        "bio": combined_bio,
        "what_they_do": raw_desc or combined_bio,
        "website": final_website,
        "project_url": final_website,
        "site": final_website,
        "company_domain": final_domain,
        "telegram_group": f"https://t.me/{official_channels[0]}" if official_channels else "",
        "links": final_links,
        "contact_channels": contact_channels,
        "source_url": str(candidate.get("source_url") or (f"https://t.me/{bot_handle}" if bot_handle else final_website)),
    }
    if x_urls:
        row["founder_x"] = x_urls[0]
    if email_candidates:
        row["work_email"] = email_candidates[0]
    return row


def _http_get_text(
    url: str,
    *,
    http_get_fn: Callable[..., Any] | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SEC,
) -> tuple[int, str]:
    """Fetch URL safely, returning `(status_code, body)` and never raising on network/4xx errors."""
    try:
        if http_get_fn is not None:
            resp = _call_with_supported_kwargs(
                http_get_fn,
                url,
                timeout=float(timeout),
                allow_redirects=True,
            )
        else:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC-TMA/1.0)"},
                timeout=float(timeout),
                allow_redirects=True,
            )
    except Exception as exc:
        logger.debug("tma_apps HTTP GET failed for %s: %s", url, exc)
        return 0, ""

    if resp is None:
        return 0, ""
    if isinstance(resp, str):
        return (200 if resp.strip() else 0), resp
    if isinstance(resp, tuple) and len(resp) >= 2:
        try:
            return int(resp[0] or 0), str(resp[1] or "")
        except (TypeError, ValueError):
            return 0, ""
    if isinstance(resp, Mapping):
        body = str(resp.get("text") or resp.get("html") or resp.get("body") or "")
        status = int(resp.get("status_code") or (200 if body.strip() else 0))
        return status, body
    status = int(getattr(resp, "status_code", 200) or 0)
    body = str(getattr(resp, "text", "") or "")
    return status, body


def _unwrap_search_results(payload: Any) -> list[dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, Mapping):
        return []
    for key in ("results", "items", "organic_results"):
        val = payload.get(key)
        if isinstance(val, list) and val:
            return [item for item in val if isinstance(item, dict)]
    for sub_key in ("output", "result", "data"):
        sub = payload.get(sub_key)
        if isinstance(sub, list) and sub:
            return [item for item in sub if isinstance(item, dict)]
        if isinstance(sub, Mapping):
            nested = _unwrap_search_results(sub)
            if nested:
                return nested
    return []


def _parse_search_candidates(payload: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in _unwrap_search_results(payload):
        url = str(item.get("url") or item.get("link") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        snippet = str(item.get("snippet") or item.get("description") or item.get("content") or "").strip()
        handles, links, website = _extract_handles_and_urls([title, snippet], extra_urls=[url])
        if not handles and not website:
            continue
        bot_handle = next((h for h in handles if is_bot_handle(h)), "")
        candidates.append(
            _build_candidate_dict(
                name=title or bot_handle or (handles[0] if handles else ""),
                description=snippet,
                category="search",
                source="crawl4ai_search",
                source_url=url,
                tma_bot_handle=bot_handle,
                tg_handles=handles,
                links=links,
                website=website,
            )
        )
    return candidates


def _load_seen(seen_path: Path) -> dict[str, str]:
    if not seen_path.is_file():
        return {}
    try:
        raw = json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    today = date.today()
    cutoff = today - timedelta(days=SEEN_DAYS)
    out: dict[str, str] = {}

    if isinstance(raw, list):
        for item in raw:
            key = str(item or "").strip().lower()
            if key:
                out[key] = today.isoformat()
        return out

    if isinstance(raw, Mapping):
        mapping = raw.get("seen") if isinstance(raw.get("seen"), Mapping) else raw
        for k, v in mapping.items():
            key = str(k or "").strip().lower()
            if not key:
                continue
            day_str = str(v or "").strip()[:10]
            try:
                seen_day = date.fromisoformat(day_str)
            except ValueError:
                out[key] = today.isoformat()
                continue
            if seen_day >= cutoff:
                out[key] = seen_day.isoformat()
    return out


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp_fh:
        tmp_name = tmp_fh.name
        tmp_fh.write(text)
    os.replace(tmp_name, path)


def _save_seen(seen_path: Path, seen_map: Mapping[str, str]) -> None:
    payload = {
        "seen": dict(sorted(seen_map.items())),
        "updated_at": date.today().isoformat(),
    }
    _atomic_write_text(seen_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _candidate_dedup_keys(cand: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    bot_h = _normalize_tg_handle(cand.get("tma_bot_handle"))
    if bot_h:
        keys.append(f"bot:{bot_h.lower()}")
    for raw_h in cand.get("tg_handles") or ():
        nh = _normalize_tg_handle(raw_h)
        if nh:
            prefix = "bot" if is_bot_handle(nh) else "tg"
            keys.append(f"{prefix}:{nh.lower()}")
    site = _clean_external_website(cand.get("website") or "")
    if site:
        try:
            host = (urlparse(site).hostname or "").lower().removeprefix("www.")
            if host:
                keys.append(f"site:{host}")
        except ValueError:
            pass
    slug = str(cand.get("slug") or "").strip().lower()
    if slug:
        keys.append(f"slug:{slug}")
    if not keys:
        name_k = str(cand.get("name") or "").strip().lower()
        if name_k:
            keys.append(f"name:{name_k}")
    return list(dict.fromkeys(keys))


def _interleave_buckets(buckets: Sequence[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    max_len = max((len(b) for b in buckets), default=0)
    for idx in range(max_len):
        for bucket in buckets:
            if idx < len(bucket):
                ordered.append(bucket[idx])
    return ordered


def collect_tma_leads(
    output_path: Path | str,
    seen_path: Path | str,
    *,
    limit: int = 40,
    allow_paid: bool = True,
    categories: Sequence[str] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    probe_fn: Callable[..., Any] | None = None,
    fetch_posts_fn: Callable[..., Any] | None = None,
    cache_path: Path | str | None = None,
) -> dict[str, Any]:
    """Collect, filter, deduplicate, resolve, and atomically persist TMA whale leads."""
    out_file = Path(output_path)
    seen_file = Path(seen_path)
    eff_cache = Path(cache_path) if cache_path is not None else seen_file.parent / "tme_probe_cache.json"
    bounded_limit = max(0, int(limit))
    eff_paid = _is_paid_allowed(allow_paid)

    eff_categories = (
        tuple(str(c).strip() for c in categories if str(c).strip())
        if categories is not None
        else DEFAULT_FINDMINI_CATEGORIES
    )

    errors: list[str] = []
    source_buckets: list[list[dict[str, Any]]] = []
    source_counts: dict[str, int] = {
        "findmini": 0,
        "awesome_tma": 0,
        "tapps_center": 0,
        "crawl4ai_search": 0,
    }

    # 1. findmini.app target categories ($0)
    findmini_bucket: list[dict[str, Any]] = []
    for cat in eff_categories:
        cat_url = f"https://findmini.app/category/{cat}/"
        status, html_text = _http_get_text(cat_url, http_get_fn=http_get_fn)
        if status == 404:
            status, html_text = _http_get_text(f"https://findmini.app/{cat}/", http_get_fn=http_get_fn)
        if status == 0 or status >= 400 or not html_text.strip():
            if status >= 400 or status == 0:
                errors.append(f"findmini:{cat}:http_{status}")
            continue
        parsed = parse_findmini_category_html(html_text, cat)
        findmini_bucket.extend(parsed)
    source_counts["findmini"] = len(findmini_bucket)
    source_buckets.append(findmini_bucket)

    # 2. GitHub awesome-telegram-mini-apps curated sections ($0)
    awesome_bucket: list[dict[str, Any]] = []
    for gh_url in AWESOME_TMA_URLS:
        status, md_text = _http_get_text(gh_url, http_get_fn=http_get_fn)
        if status == 0 or status >= 400 or not md_text.strip():
            errors.append(f"awesome_tma:{gh_url}:http_{status}")
            continue
        awesome_bucket.extend(parse_awesome_tma_markdown(md_text))
    source_counts["awesome_tma"] = len(awesome_bucket)
    source_buckets.append(awesome_bucket)

    # 3. tapps.center ($0)
    tapps_bucket: list[dict[str, Any]] = []
    status, tapps_html = _http_get_text(TAPPS_CENTER_URL, http_get_fn=http_get_fn)
    if status == 0 or status >= 400 or not tapps_html.strip():
        errors.append(f"tapps_center:http_{status}")
    else:
        tapps_bucket.extend(parse_tapps_center_html(tapps_html))
    source_counts["tapps_center"] = len(tapps_bucket)
    source_buckets.append(tapps_bucket)

    # 4. Optional paid crawl4ai.web.search (strictly gated by allow_paid & BUDGET_PAID_ALLOWED)
    paid_search_used = False
    search_bucket: list[dict[str, Any]] = []
    if eff_paid:
        treg_ready = treg_fn is not None or check_treg_ready()
        if treg_ready:
            paid_search_used = True
            try:
                payload = (
                    _call_with_supported_kwargs(
                        treg_fn,
                        "crawl4ai.web.search",
                        query={"q": TMA_SEARCH_QUERY},
                        timeout=25,
                    )
                    if treg_fn is not None
                    else treg_call("crawl4ai.web.search", query={"q": TMA_SEARCH_QUERY}, timeout=25)
                )
                search_bucket.extend(_parse_search_candidates(payload))
            except Exception as exc:
                errors.append(f"crawl4ai_search:{exc.__class__.__name__}")
    source_counts["crawl4ai_search"] = len(search_bucket)
    source_buckets.append(search_bucket)

    seen_map = _load_seen(seen_file)
    today_iso = date.today().isoformat()
    batch_seen_keys: set[str] = set()
    emitted: list[dict[str, Any]] = []
    rejected_count = 0
    skipped_seen_count = 0

    for cand in _interleave_buckets(source_buckets):
        if len(emitted) >= bounded_limit:
            break

        rejected, _reason = is_rejected_tma_app(
            str(cand.get("name") or ""),
            str(cand.get("description") or ""),
            str(cand.get("category") or ""),
        )
        if rejected:
            rejected_count += 1
            continue

        dkeys = _candidate_dedup_keys(cand)
        if not dkeys:
            continue
        if any(k in seen_map or k in batch_seen_keys for k in dkeys):
            skipped_seen_count += 1
            continue

        row = resolve_tma_to_human_lead(
            cand,
            probe_fn=probe_fn,
            fetch_posts_fn=fetch_posts_fn,
            cache_path=eff_cache,
            allow_paid=eff_paid,
        )

        # Enforce Sacred Invariant and non-empty contact/product presence.
        for u_field in ("username", "telegram_username", "tg_username"):
            val = str(row.get(u_field) or "").strip()
            if val and (is_bot_handle(val) or not _is_real_personal_tg_handle(val)):
                row[u_field] = ""
        row["lead_source"] = "tma_apps"
        row["source"] = "tma_apps"
        row["segment"] = "whale"
        row["category"] = "tma"

        if not any((row.get("username"), row.get("tma_bot_handle"), row.get("website"), row.get("links"))):
            continue

        batch_seen_keys.update(dkeys)
        for k in dkeys:
            seen_map[k] = today_iso
        if row.get("username"):
            u_key = f"tg:{str(row['username']).lower()}"
            batch_seen_keys.add(u_key)
            seen_map[u_key] = today_iso

        emitted.append(row)

    jsonl_payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in emitted)
    if jsonl_payload:
        jsonl_payload += "\n"
    _atomic_write_text(out_file, jsonl_payload)
    _save_seen(seen_file, seen_map)

    return {
        "discovered": len(emitted),
        "written": len(emitted),
        "with_personal_tg": sum(1 for r in emitted if r.get("username")),
        "with_website": sum(1 for r in emitted if r.get("website")),
        "rejected": rejected_count,
        "skipped_seen": skipped_seen_count,
        "paid_search_used": paid_search_used,
        "sources": source_counts,
        "errors": errors,
        "output_path": str(out_file),
        "seen_path": str(seen_file),
    }


def _parse_categories_arg(raw_values: Sequence[str] | None) -> list[str] | None:
    if not raw_values:
        return None
    cats: list[str] = []
    for item in raw_values:
        for part in str(item).split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in cats:
                cats.append(cleaned)
    return cats or None


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for Telegram Mini Apps lead discovery."""
    state_dir = _default_state_dir()
    parser = argparse.ArgumentParser(
        description="Discover Telegram Mini Apps whale leads and resolve bot -> human founder contacts.",
    )
    parser.add_argument(
        "--output",
        default=str(state_dir / "tma_apps_leads.jsonl"),
        help="Destination JSONL path for discovered TMA leads.",
    )
    parser.add_argument(
        "--seen",
        default=str(state_dir / "tma_apps_seen.json"),
        help="Path to atomic seen-state JSON file (default: tma_apps_seen.json).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximum number of TMA leads to resolve and emit per run.",
    )
    parser.add_argument(
        "--no-paid",
        action="store_true",
        help="Disable paid crawl4ai.web.search and paid channel post fallbacks ($0 web only).",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=None,
        help="Optional findmini categories (comma- or space-separated).",
    )
    args = parser.parse_args(argv)

    summary = collect_tma_leads(
        output_path=Path(args.output),
        seen_path=Path(args.seen),
        limit=max(0, int(args.limit)),
        allow_paid=not bool(args.no_paid),
        categories=_parse_categories_arg(args.categories),
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
