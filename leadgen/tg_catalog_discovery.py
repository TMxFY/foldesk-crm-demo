#!/usr/bin/env python3
"""Wave B Telegram channel & public group catalog discovery and safe RO joining.

Deep module owning:
- Curated global/EN channel pool (`CURATED_CHANNEL_POOL`, zero RU porn/casino/Fragment/spam)
- Free `$0` discovery from public TMA catalogs (`findmini.app`, `SzczepanBEN` awesome-tma,
  `tapps.center`) + existing `tg_channel_graph_state.json`
- Optional budget-gated `crawl4ai.web.search` (`site:t.me/` and `site:tgstat.com/channel/`)
  without ever fetching `tgstat.com` directly over HTTP
- Web validation via `tg_web_client.probe_tme_handle_web` (7-day cache), rejecting bots,
  missing handles, gambling/adult/scam/Fragment spam
- Atomic, deduplicated persistence into `discovered_ad_spy_channels.txt`,
  `discovered_graph_seeds.txt`, `discovered_comment_channels.txt`, and `tg_catalog_state.json`
- Safe, isolated RO public group joining (`MAX_RO_JOINS_PER_DAY = 2` via UTC-day ledger
  `ro_join_ledger.json`) with automatic captcha/approval deferral to `manual_join_queue.jsonl`
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

try:
    from telethon.errors import (
        ChatWriteForbiddenError,
        FloodWaitError,
        InviteRequestSentError,
        UserBannedInChannelError,
    )
    from telethon.tl.functions.channels import JoinChannelRequest
except ImportError:  # pragma: no cover - telethon is installed in runtime venv
    ChatWriteForbiddenError = type("ChatWriteForbiddenError", (Exception,), {})  # type: ignore[misc,assignment]
    FloodWaitError = type("FloodWaitError", (Exception,), {})  # type: ignore[misc,assignment]
    InviteRequestSentError = type("InviteRequestSentError", (Exception,), {})  # type: ignore[misc,assignment]
    UserBannedInChannelError = type("UserBannedInChannelError", (Exception,), {})  # type: ignore[misc,assignment]
    JoinChannelRequest = None  # type: ignore[assignment]

try:
    from leadgen import tg_web_client
except ImportError:
    import tg_web_client  # type: ignore[no-redef]

try:
    from leadgen import treg_client
except ImportError:
    try:
        import treg_client  # type: ignore[no-redef]
    except ImportError:
        treg_client = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MAX_RO_JOINS_PER_DAY = 2

# 70 curated EN/global handles across Consumer AI, SaaS founders, Mobile UA/ASO,
# E-com/D2C, Prop Trading, and Telegram Stars/TMA news & founder chats.
CURATED_CHANNEL_POOL: tuple[str, ...] = (
    # Consumer AI & AI Builders (12)
    "huggingface",
    "openai_news",
    "ai_founders",
    "ai_products",
    "ai_newz",
    "bytebytego",
    "aitoolsclub",
    "latent_space",
    "aibreakfast",
    "therundownai",
    "bensbites",
    "deeplearning_ai",
    # SaaS Founders & Indie Makers (14)
    "saasgrowth",
    "indiehackers",
    "microconf",
    "startups",
    "nocodefounders",
    "producthunt_news",
    "b2bsaas",
    "bootstrapped",
    "growthhacking",
    "saas_insider",
    "indie_makers",
    "levelsio",
    "saas_founders",
    "producthunt",
    # Mobile UA / ASO / Subscription Apps (10)
    "mobileapps_dev",
    "appgrowth",
    "app_growth",
    "aso_pro",
    "asotrips",
    "app2top",
    "mobile_ua_club",
    "sub_apps_growth",
    "aso_insider",
    "paywall_lab",
    # E-com / Shopify / D2C (10)
    "shopifyeasy",
    "shopify_merchants",
    "dtc_brands",
    "ecommerce_news",
    "d2c_marketing",
    "shopify_plus",
    "ad_tech",
    "ecom_founders",
    "dtc_operators",
    "ecom_growth_lab",
    # Prop Trading / Quant / Fintech (10)
    "ftmo_official",
    "tradingview",
    "prop_firms_news",
    "quant_trading_hub",
    "fintech_builders",
    "algo_trading_club",
    "funded_traders_chat",
    "fx_prop_desk",
    "fintech_founders",
    "prop_trading_insider",
    # Telegram Stars / TMA News & Founder Chats (14)
    "tondev_news",
    "toncore",
    "tontech",
    "tginfo",
    "t_tutorial",
    "tma_builders",
    "telegram_mini_apps",
    "stars_economy",
    "ton_apps_news",
    "tma_founders_chat",
    "miniapps_dev",
    "ton_builders",
    "tg_ads_builders",
    "telegram_dev_news",
)

# 20 curated GCC/MENA business, startup, e-commerce, crypto, and health-tech handles.
CURATED_MENA_CHANNEL_POOL: tuple[str, ...] = (
    "dubai_startups",
    "dubaibusinesschat",
    "uae_entrepreneurs",
    "menastartups",
    "dubaicryptochat",
    "dubai_ecommerce",
    "saudi_entrepreneurs",
    "riyadh_startups",
    "mena_founders",
    "dubai_founders",
    "gcc_ecommerce",
    "dubai_marketing",
    "uae_business_network",
    "mena_tech_founders",
    "arab_startups",
    "abu_dhabi_founders",
    "saudi_tech_hub",
    "mena_healthtech",
    "dubai_fintech",
    "gcc_shopify_founders",
)

CAPTCHA_BOT_SIGNATURES: tuple[str, ...] = (
    "captcha",
    "shieldy",
    "combot",
    "missrose",
    "rose_bot",
    "shieldy_bot",
    "combot_bot",
    "missrose_bot",
    "captcha_bot",
    "grouphelp",
    "grouphelpbot",
    "watchdog",
    "verify_bot",
    "human_verify",
    "join_captcha",
)

DEFERRED_JOIN_ERROR_NAMES = frozenset(
    {
        "InviteRequestSentError",
        "ChatWriteForbiddenError",
        "UserBannedInChannelError",
    }
)

PUBLIC_TMA_CATALOG_URLS: tuple[tuple[str, str], ...] = (
    ("findmini", "https://findmini.app/"),
    (
        "awesome_tma",
        "https://raw.githubusercontent.com/SzczepanBEN/awesome-telegram-mini-apps/main/README.md",
    ),
    ("tapps_center", "https://tapps.center/"),
)

PAID_SEARCH_QUERIES: tuple[str, ...] = (
    'site:t.me/ "SaaS" OR "indie hackers" OR "AI founders" subscribers',
    'site:tgstat.com/channel/ "SaaS" OR "AI" OR "startups" OR "mini app"',
    'site:t.me/ "Shopify" OR "D2C" OR "prop trading" OR "ASO" channel',
    'site:tgstat.com/channel/ "Telegram Mini Apps" OR "Telegram Stars" OR "TON" OR "mobile UA"',
)

MENA_PAID_SEARCH_QUERIES: tuple[str, ...] = (
    "site:t.me/ dubai founders",
    "site:t.me/ riyadh startups",
    "site:t.me/ gcc ecommerce",
    "site:t.me/ saudi entrepreneurs",
    "site:t.me/ دبي مشاريع",
    "site:t.me/ السعودية تجارة الكترونية",
)

_STATE_LOCK = threading.RLock()
_VALID_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")
_TME_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?(?!\+)([A-Za-z][A-Za-z0-9_]{2,31})(?=[/?#\"'<>\s)]|$)",
    re.IGNORECASE,
)
_TGSTAT_URL_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)?tgstat\.(?:com|ru)/(?:en/|ru/)?channel/@?([A-Za-z][A-Za-z0-9_]{2,31})(?=[/?#\"'<>\s)]|$)",
    re.IGNORECASE,
)
_AT_HANDLE_RE = re.compile(
    r"(?:^|[\s(\[\"'<>,;:])@([A-Za-z][A-Za-z0-9_]{3,31})(?=[/?#\"'<>\s),;:]|$)"
)
_NOISE_HANDLES = frozenset(
    {
        "joinchat",
        "addstickers",
        "addemoji",
        "addlist",
        "addtheme",
        "addchannel",
        "share",
        "proxy",
        "socks",
        "login",
        "contact",
        "boost",
        "s",
        "c",
        "iv",
        "setlanguage",
        "confirm",
        "bg",
        "gifts",
        "nft",
        "invoice",
        "auth",
        "botfather",
        "stickers",
        "gif",
        "like",
        "vote",
        "telegram",
        "tgstat",
        "business",
        "wallet",
        "stars",
        "premium",
        "fragment",
        "durov",
        "apps",
        "miniapps",
        "tapps",
        "findmini",
        "sensor",
        "contest",
        "tips",
        "faq",
        "privacy",
    }
)
_JUNK_HANDLE_RE = re.compile(
    r"(?:^|_)(?:"
    r"fragment|fragment_monitor|tonviewer|getgems|casino|slots|betting|bets|"
    r"1win|1xbet|mostbet|pinup|pin_up|giveaway|airdrop|rozygrysh|"
    r"porn|porno|xxx|nsfw|escort|hentai|sex|sliv|onlyfans_leak|"
    r"scam|drainer|carding|obnal|darknet|hamster_kombat|tap_to_earn|tapalk"
    r")(?:$|_)|"
    r"(?:casino|slots|roulette|gambl|betting|juicybets|bets$|minigame|goblinmine|"
    r"mines_ton|ton_?mines|tondoge|dogechat|dogecoin|taptoearn|tap2earn|clicker|"
    r"giveaway|airdrop|farmcoin|cryptofarm|hash2cash|footballlot|luckyknight|"
    r"rocketcase|mooncards|tonjiggle|hamster|gameru$)",
    re.IGNORECASE,
)
_JUNK_TEXT_RE = re.compile(
    r"(?:"
    r"fragment\.com|fragment_monitor|tonviewer(?:\.com)?|getgems(?:\.io)?|"
    r"\b(?:username\s+auction|nft\s+auction|buy\s+usernames?|usernames?\s+for\s+sale|"
    r"аукцион\w*\s+(?:юзернейм\w*|никнейм\w*|ников|номеров)|выкуп\w*\s+юзернейм\w*|"
    r"купить\s+юзернейм\w*|номер\w*\s+\+888|"
    r"casino|казино|slots|слоты|букмекер\w*|betting|sportsbook|ставки\s+на\s+спорт|"
    r"gambl\w*|гембл\w*|mini[\s\-]*game\w*|мини[- ]?игр\w*|play[\s\-]*(?:to|2)[\s\-]*earn|"
    r"1win|1xbet|mostbet|pin-?up|фриспин\w*|freespin\w*|лудоман\w*|онлайн[- ]?казино|рулетк\w*|"
    r"porn|porno|порно|xxx|nsfw|escort|эскорт|hentai|хентай|интим\s+чат|секс[- ]?чат|"
    r"сливы?\s+(?:девушек|18\+|онлифанс)|onlyfans\s+leak|"
    r"giveaway\w*|розыгрыш\w*|гивэве\w*|airdrop\w*|аирдроп\w*|раздача\s+призов|"
    r"tap[- ]?(?:to|2)[- ]?earn|тапалк\w*|clicker\w*|кликер\w*|meme[\s\-]*coin\w*|"
    r"фарм\w*\s+монет|крипто[- ]?игр\w*|carding|кардинг|obnal|обнал|wallet\s+drainer|крипто\s*скам"
    r")\b)",
    re.IGNORECASE,
)
_CHAT_SIGNAL_RE = re.compile(
    r"(?:^|_|\b)(?:chat|group|club|community|discuss(?:ion)?|forum|comments?|members|online|"
    r"чат|клуб|комьюнити|форум|обсуждени\w*|сообществ\w*)(?:$|_|\b)",
    re.IGNORECASE,
)
_DEFERRED_ERROR_TEXT_RE = re.compile(
    r"(?:invite\s*request\s*sent|request\s+to\s+join|approval\s+required|"
    r"chat\s*write\s*forbidden|user\s*banned\s*in\s*channel|"
    r"you\s+have\s+successfully\s+requested\s+to\s+join)",
    re.IGNORECASE,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _resolve_state_dir(state_dir: Path | str | None = None) -> Path:
    raw = str(state_dir or "").strip()
    if raw:
        return Path(raw)
    env_dir = (
        os.environ.get("LEADGEN_STATE_DIR")
        or os.environ.get("CRMTG_LEADGEN_STATE_DIR")
        or ""
    ).strip()
    if env_dir:
        return Path(env_dir)
    return Path(__file__).resolve().parent.parent / "data" / "leadgen_state"


def _atomic_write_text(path: Path, content: str) -> None:
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
        tmp_fh.write(content)
    os.replace(tmp_name, path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _read_json_safe(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


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


def _normalize_tg_handle(raw: str) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    if txt.startswith("+"):
        return ""
    if re.search(r"(?:t\.me|telegram\.me|telegram\.dog)/", txt, re.IGNORECASE):
        m_tme = _TME_URL_RE.search(txt)
        if not m_tme:
            return ""
        txt = m_tme.group(1)
    elif re.search(r"tgstat\.(?:com|ru)/", txt, re.IGNORECASE):
        m_tgs = _TGSTAT_URL_RE.search(txt)
        if not m_tgs:
            return ""
        txt = m_tgs.group(1)
    txt = txt.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0].strip().lstrip("@")
    if not txt or txt.lower() in _NOISE_HANDLES:
        return ""
    if not _VALID_HANDLE_RE.fullmatch(txt):
        return ""
    return txt


def _is_bot_handle(handle: str) -> bool:
    low = (handle or "").strip().lstrip("@").lower()
    return not low or low in _NOISE_HANDLES or low.endswith("bot")


def _is_junk_channel(handle: str, title: str = "", bio: str = "") -> bool:
    clean = _normalize_tg_handle(handle)
    if not clean:
        return True
    low = clean.lower()
    if _is_bot_handle(low) or _JUNK_HANDLE_RE.search(low):
        return True
    combined = "\n".join(part for part in (clean, title, bio) if part)
    if combined and _JUNK_TEXT_RE.search(combined):
        return True
    return False


def _is_chat_channel(handle: str, title: str = "", bio: str = "") -> bool:
    combined = " ".join(part for part in (handle, title, bio) if part)
    return bool(_CHAT_SIGNAL_RE.search(combined))


def _extract_handles_from_text(text: str) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    seen_low: set[str] = set()

    def _add(raw_cand: str) -> None:
        clean = _normalize_tg_handle(raw_cand)
        if not clean or _is_bot_handle(clean):
            return
        low = clean.lower()
        if low in seen_low or _JUNK_HANDLE_RE.search(low):
            return
        seen_low.add(low)
        found.append(clean)

    for rx in (_TME_URL_RE, _TGSTAT_URL_RE, _AT_HANDLE_RE):
        for match in rx.finditer(text):
            _add(match.group(1))
    return found


def _read_handle_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _existing_handle_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for raw in _read_handle_lines(path):
        tok = raw.split("#", 1)[0].strip()
        if not tok:
            continue
        clean = _normalize_tg_handle(tok)
        if clean:
            keys.add(clean.lower())
        else:
            keys.add(tok.lower())
    return keys


def _append_unique_handles_atomic(path: Path, new_handles: Sequence[str]) -> tuple[list[str], list[str]]:
    """Preserve existing lines (including `id<digits>` tokens), append new normalized handles atomically."""
    with _STATE_LOCK:
        preserved: list[str] = []
        seen_keys: set[str] = set()
        for raw in _read_handle_lines(path):
            tok = raw.split("#", 1)[0].strip()
            if not tok:
                continue
            low = tok.lower()
            if low.startswith("id") and low[2:].isdigit():
                if low not in seen_keys:
                    seen_keys.add(low)
                    preserved.append(low)
                continue
            clean = _normalize_tg_handle(tok)
            if clean and not _is_bot_handle(clean):
                c_low = clean.lower()
                if c_low not in seen_keys:
                    seen_keys.add(c_low)
                    preserved.append(clean)
            elif low not in seen_keys:
                seen_keys.add(low)
                preserved.append(tok)

        added: list[str] = []
        for raw in new_handles:
            clean = _normalize_tg_handle(raw)
            if not clean or _is_bot_handle(clean):
                continue
            c_low = clean.lower()
            if c_low not in seen_keys:
                seen_keys.add(c_low)
                preserved.append(clean)
                added.append(clean)

        content = ("\n".join(preserved) + "\n") if preserved else ""
        _atomic_write_text(path, content)
        return added, preserved


def _http_get_text(
    url: str,
    *,
    http_get_fn: Callable[..., Any] | None = None,
    timeout: float = 8.0,
) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "tgstat." in host:
        raise ValueError(f"Direct HTTP fetch to {host} is forbidden (returns 403); use crawl4ai.web.search")
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
                headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC/1.0)"},
                timeout=float(timeout),
                allow_redirects=True,
            )
    except Exception as exc:
        logger.debug("Catalog HTTP fetch failed (%s): %s", url, exc)
        return ""

    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, Mapping):
        status = int(resp.get("status_code") or 200)
        if status >= 400:
            return ""
        return str(resp.get("text") or resp.get("html") or resp.get("body") or "")
    status = int(getattr(resp, "status_code", 200) or 0)
    if status >= 400:
        return ""
    return str(getattr(resp, "text", "") or "")


def _collect_tma_catalog_handles(
    *,
    http_get_fn: Callable[..., Any] | None = None,
) -> tuple[list[str], dict[str, int]]:
    handles: list[str] = []
    seen_low: set[str] = set()
    counts: dict[str, int] = {}
    for source_name, url in PUBLIC_TMA_CATALOG_URLS:
        body = _http_get_text(url, http_get_fn=http_get_fn, timeout=8.0)
        extracted = _extract_handles_from_text(body)
        added_here = 0
        for h in extracted:
            low = h.lower()
            if low not in seen_low:
                seen_low.add(low)
                handles.append(h)
                added_here += 1
        counts[source_name] = added_here
    return handles, counts


def _collect_graph_state_handles(state_dir: Path) -> list[str]:
    handles: list[str] = []
    seen_low: set[str] = set()

    def _absorb(val: Any) -> None:
        if isinstance(val, str):
            tok = val.strip().lstrip("@")
            if tok.lower().startswith("id") and tok[2:].isdigit():
                return
            clean = _normalize_tg_handle(tok)
            if clean and not _is_bot_handle(clean):
                low = clean.lower()
                if low not in seen_low:
                    seen_low.add(low)
                    handles.append(clean)
        elif isinstance(val, (list, tuple)):
            if len(val) == 2 and isinstance(val[0], (str, int)) and isinstance(val[1], int):
                if isinstance(val[0], str):
                    _absorb(val[0])
            else:
                for item in val:
                    _absorb(item)

    for fname in ("tg_channel_graph_state.json", "tg_channel_graph_seen.json"):
        payload = _read_json_safe(state_dir / fname, None)
        if isinstance(payload, list):
            _absorb(payload)
        elif isinstance(payload, Mapping):
            for key in ("discovered", "channels", "handles", "frontier", "seeds", "seen", "seen_channels"):
                if key in payload:
                    _absorb(payload.get(key))
    return handles


def _is_paid_search_allowed(allow_paid: bool) -> bool:
    if not allow_paid:
        return False
    raw = str(os.environ.get("BUDGET_PAID_ALLOWED") or "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    return True


def _walk_payload_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, Mapping):
        for v in value.values():
            out.extend(_walk_payload_strings(v))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_walk_payload_strings(item))
    return out


def _collect_paid_search_handles(
    *,
    allow_paid: bool,
    treg_fn: Callable[..., Any] | None = None,
    search_cursor: int = 0,
    market_profile: str = "global",
) -> tuple[list[str], int, bool]:
    if not _is_paid_search_allowed(allow_paid):
        return [], search_cursor, False
    if treg_fn is None and (treg_client is None or not treg_client.check_treg_ready()):
        return [], search_cursor, False

    eff_profile = str(market_profile or "global").strip().lower()
    queries = MENA_PAID_SEARCH_QUERIES if eff_profile == "gcc_december" else PAID_SEARCH_QUERIES
    handles: list[str] = []
    seen_low: set[str] = set()
    used_any = False
    n_queries = len(queries)
    step = min(2, n_queries)

    for offset in range(step):
        q = queries[(search_cursor + offset) % n_queries]
        try:
            if treg_fn is not None:
                payload = _call_with_supported_kwargs(
                    treg_fn,
                    "crawl4ai.web.search",
                    query={"q": q},
                    timeout=20,
                )
            else:
                payload = treg_client.call(
                    "crawl4ai.web.search",
                    query={"q": q},
                    timeout=20,
                )
            used_any = True
        except Exception as exc:
            logger.debug("crawl4ai.web.search failed (%s): %s", q, exc)
            continue
        if not payload:
            continue
        joined = "\n".join(_walk_payload_strings(payload))
        for h in _extract_handles_from_text(joined):
            low = h.lower()
            if low not in seen_low:
                seen_low.add(low)
                handles.append(h)

    next_cursor = (search_cursor + step) % n_queries if used_any else search_cursor
    return handles, next_cursor, used_any


def _probe_candidate(
    handle: str,
    *,
    state_dir: Path,
    probe_fn: Callable[..., Any] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    try:
        if probe_fn is not None:
            res = _call_with_supported_kwargs(
                probe_fn,
                handle,
                http_get_fn=http_get_fn,
                cache_path=state_dir / "tme_probe_cache.json",
            )
        else:
            res = tg_web_client.probe_tme_handle_web(
                handle,
                http_get_fn=http_get_fn,
                cache_path=state_dir / "tme_probe_cache.json",
            )
    except Exception as exc:
        logger.debug("probe failed for %s: %s", handle, exc)
        return {"handle": handle, "kind": "unknown", "title": "", "bio": "", "subscribers": 0}
    if not isinstance(res, Mapping):
        return {"handle": handle, "kind": "unknown", "title": "", "bio": "", "subscribers": 0}
    return {
        "handle": _normalize_tg_handle(str(res.get("handle") or handle)) or handle,
        "kind": str(res.get("kind") or "unknown").strip().lower(),
        "title": str(res.get("title") or "").strip(),
        "bio": str(res.get("bio") or "").strip(),
        "subscribers": int(res.get("subscribers") or 0),
    }


def discover_and_replenish_catalogs(
    state_dir: Path | str,
    *,
    limit: int = 40,
    allow_paid: bool = True,
    market_profile: str = "global",
    http_get_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    probe_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Discover, validate, filter, and atomically persist TG channel/group catalogs."""
    state_path = _resolve_state_dir(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)
    eff_limit = max(0, int(limit))
    eff_profile = "gcc_december" if str(market_profile or "global").strip().lower() == "gcc_december" else "global"

    catalog_state_path = state_path / "tg_catalog_state.json"
    ad_spy_path = state_path / "discovered_ad_spy_channels.txt"
    graph_seeds_path = state_path / "discovered_graph_seeds.txt"
    comment_channels_path = state_path / "discovered_comment_channels.txt"

    prev_state = _read_json_safe(catalog_state_path, {})
    if not isinstance(prev_state, dict):
        prev_state = {}
    search_cursor = max(0, int(prev_state.get("search_cursor") or 0))
    curated_cursor = max(0, int(prev_state.get("curated_cursor") or 0))
    mena_cursor = max(0, int(prev_state.get("mena_curated_cursor") or 0))

    # 1. Gather candidate handles from external $0 + graph + optional paid search + curated pool
    tma_handles, tma_counts = _collect_tma_catalog_handles(http_get_fn=http_get_fn)
    graph_handles = _collect_graph_state_handles(state_path)
    paid_handles, next_search_cursor, paid_search_used = _collect_paid_search_handles(
        allow_paid=allow_paid,
        treg_fn=treg_fn,
        search_cursor=search_cursor,
        market_profile=eff_profile,
    )

    pool_len = len(CURATED_CHANNEL_POOL)
    rotated_curated = (
        [CURATED_CHANNEL_POOL[(curated_cursor + idx) % pool_len] for idx in range(pool_len)]
        if pool_len
        else []
    )
    mena_len = len(CURATED_MENA_CHANNEL_POOL)
    rotated_mena = (
        [CURATED_MENA_CHANNEL_POOL[(mena_cursor + idx) % mena_len] for idx in range(mena_len)]
        if mena_len
        else []
    )

    existing_ad_keys = _existing_handle_keys(ad_spy_path)
    existing_graph_keys = _existing_handle_keys(graph_seeds_path)
    already_cataloged = existing_ad_keys & existing_graph_keys

    ordered_candidates: list[str] = []
    seen_candidates: set[str] = set()
    deferred_known: list[str] = []

    if eff_profile == "gcc_december":
        candidate_buckets = (
            *paid_handles[:15],
            *rotated_mena,
            *tma_handles[:15],
            *rotated_curated,
            *tma_handles[15:],
            *paid_handles[15:],
            *graph_handles,
        )
    else:
        candidate_buckets = (
            *tma_handles[:15],
            *paid_handles[:15],
            *rotated_curated,
            *tma_handles[15:],
            *paid_handles[15:],
            *graph_handles,
        )
    for raw in candidate_buckets:
        clean = _normalize_tg_handle(raw)
        if not clean or _is_bot_handle(clean):
            continue
        low = clean.lower()
        if low in seen_candidates:
            continue
        seen_candidates.add(low)
        if low in already_cataloged:
            deferred_known.append(clean)
        else:
            ordered_candidates.append(clean)
    ordered_candidates.extend(deferred_known)

    # 2. Validate via probe_tme_handle_web (7d cache) and filter junk
    accepted_handles: list[str] = []
    chat_handles: list[str] = []
    rejected: list[dict[str, str]] = []
    probed_count = 0
    max_probes = max(eff_limit * 3, eff_limit) if eff_limit > 0 else 0

    for cand in ordered_candidates:
        if len(accepted_handles) >= eff_limit or probed_count >= max_probes:
            break
        if _is_junk_channel(cand):
            rejected.append({"handle": cand, "reason": "junk_handle"})
            continue

        probed_count += 1
        info = _probe_candidate(
            cand,
            state_dir=state_path,
            probe_fn=probe_fn,
            http_get_fn=http_get_fn,
        )
        kind = info["kind"]
        resolved_handle = info["handle"] or cand

        if kind != "channel" or _is_bot_handle(resolved_handle):
            rejected.append({"handle": resolved_handle, "reason": f"kind:{kind}"})
            continue
        if _is_junk_channel(resolved_handle, info["title"], info["bio"]):
            rejected.append({"handle": resolved_handle, "reason": "junk_content"})
            continue

        accepted_handles.append(resolved_handle)
        if _is_chat_channel(resolved_handle, info["title"], info["bio"]):
            chat_handles.append(resolved_handle)

    comment_targets = chat_handles if chat_handles else accepted_handles

    # 3. Atomically persist deduplicated catalogs & run state
    ad_spy_added, all_ad_spy = _append_unique_handles_atomic(ad_spy_path, accepted_handles)
    graph_added, all_graph = _append_unique_handles_atomic(graph_seeds_path, accepted_handles)
    comment_added, all_comment = _append_unique_handles_atomic(comment_channels_path, comment_targets)

    run_ts = _utc_now_iso()
    next_curated_cursor = (curated_cursor + max(1, min(eff_limit, pool_len))) % pool_len if (pool_len and eff_limit > 0) else curated_cursor
    next_mena_cursor = (
        (mena_cursor + max(1, min(eff_limit, mena_len))) % mena_len
        if (eff_profile == "gcc_december" and mena_len and eff_limit > 0)
        else mena_cursor
    )
    state_payload = {
        "last_run_at": run_ts,
        "last_run_ts": time.time(),
        "market_profile": eff_profile,
        "search_cursor": next_search_cursor,
        "curated_cursor": next_curated_cursor,
        "mena_curated_cursor": next_mena_cursor,
        "paid_search_used": paid_search_used,
        "last_counts": {
            "candidates": len(ordered_candidates),
            "probed": probed_count,
            "accepted": len(accepted_handles),
            "rejected": len(rejected),
            "ad_spy_added": len(ad_spy_added),
            "graph_added": len(graph_added),
            "comment_added": len(comment_added),
            "total_ad_spy": len(all_ad_spy),
            "total_graph_seeds": len(all_graph),
            "total_comment_channels": len(all_comment),
        },
        "sources": {
            **tma_counts,
            "graph_state": len(graph_handles),
            "web_search": len(paid_handles),
            "curated_pool": pool_len,
        },
    }
    _atomic_write_json(catalog_state_path, state_payload)

    return {
        "state_dir": str(state_path),
        "discovered": accepted_handles,
        "ad_spy_channels": ad_spy_added,
        "graph_seeds": graph_added,
        "comment_channels": comment_added,
        "total_ad_spy_channels": len(all_ad_spy),
        "total_graph_seeds": len(all_graph),
        "total_comment_channels": len(all_comment),
        "probed_count": probed_count,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "paid_search_used": paid_search_used,
        "sources": state_payload["sources"],
        "run_at": run_ts,
    }


def _detect_captcha_signature(text: str) -> str:
    low = (text or "").lower()
    if not low:
        return ""
    for sig in CAPTCHA_BOT_SIGNATURES:
        if sig in low:
            return sig
    return ""


def _is_deferred_join_exception(exc: BaseException) -> tuple[bool, str, str]:
    cls_name = type(exc).__name__
    msg = str(exc or "")
    if isinstance(exc, (InviteRequestSentError, ChatWriteForbiddenError, UserBannedInChannelError)) or cls_name in DEFERRED_JOIN_ERROR_NAMES:
        return True, f"error:{cls_name}", cls_name
    sig = _detect_captcha_signature(msg)
    if sig:
        return True, f"captcha_error:{sig}", sig
    if _DEFERRED_ERROR_TEXT_RE.search(msg):
        return True, f"error_text:{cls_name}", cls_name
    return False, "", ""


def _load_manual_join_queue_rows(queue_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not queue_path.is_file():
        return rows
    try:
        lines = queue_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    seen_low: set[str] = set()
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        clean = _normalize_tg_handle(str(obj.get("handle") or ""))
        if not clean:
            continue
        low = clean.lower()
        if low in seen_low:
            continue
        seen_low.add(low)
        obj["handle"] = clean
        rows.append(obj)
    return rows


def _append_manual_join_queue(
    queue_path: Path,
    *,
    handle: str,
    reason: str,
    signature: str = "",
) -> bool:
    clean = _normalize_tg_handle(handle)
    if not clean:
        return False
    low = clean.lower()
    with _STATE_LOCK:
        rows = _load_manual_join_queue_rows(queue_path)
        if any(str(r.get("handle") or "").lower() == low for r in rows):
            return False
        rows.append(
            {
                "handle": clean,
                "url": f"https://t.me/{clean}",
                "reason": reason,
                "signature": signature,
                "deferred_at": _utc_now_iso(),
            }
        )
        content = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
        _atomic_write_text(queue_path, content)
        return True


@contextmanager
def _ro_join_process_lock(state_path: Path):
    """Serialize ro_join_ledger.json check/reserve/update across threads AND OS processes."""
    lock_path = state_path / ".ro_join_ledger.lock"
    state_path.mkdir(parents=True, exist_ok=True)
    with _STATE_LOCK:
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _read_join_ledger_unlocked(ledger_path: Path, utc_day: str) -> tuple[dict[str, Any], int, set[str], set[str]]:
    data = _read_json_safe(ledger_path, {})
    if not isinstance(data, dict):
        data = {}
    days = data.get("days") if isinstance(data.get("days"), dict) else {}
    day_entry = days.get(utc_day) if isinstance(days.get(utc_day), dict) else None
    if day_entry is None and str(data.get("date") or "") == utc_day:
        day_entry = {
            "count": int(data.get("count") or 0),
            "handles": list(data.get("handles") or []),
            "in_flight": list(data.get("in_flight") or []),
        }
    if day_entry is None:
        return data, 0, set(), set()
    joined_set = {
        h
        for h in (_normalize_tg_handle(str(x)).lower() for x in (day_entry.get("handles") or []))
        if h
    }
    in_flight_set = {
        h
        for h in (_normalize_tg_handle(str(x)).lower() for x in (day_entry.get("in_flight") or []))
        if h and h not in joined_set
    }
    count = max(len(joined_set), int(day_entry.get("count") or 0)) + len(in_flight_set)
    return data, count, joined_set, in_flight_set


def _read_join_ledger(ledger_path: Path, utc_day: str) -> tuple[dict[str, Any], int, set[str]]:
    with _ro_join_process_lock(ledger_path.parent):
        data, count, joined_set, _in_flight = _read_join_ledger_unlocked(ledger_path, utc_day)
        return data, count, joined_set


def _write_join_ledger_unlocked(
    ledger_path: Path,
    utc_day: str,
    data: dict[str, Any],
    joined_set: set[str],
    in_flight_set: set[str],
) -> int:
    days = dict(data.get("days") or {}) if isinstance(data.get("days"), dict) else {}
    ordered = sorted(joined_set)
    in_flight_ordered = sorted(h for h in in_flight_set if h not in joined_set)
    new_count = len(ordered)
    days[utc_day] = {
        "count": new_count,
        "handles": ordered,
        "in_flight": in_flight_ordered,
        "updated_at": _utc_now_iso(),
    }
    recent_days = dict(sorted(days.items())[-14:])
    payload = {
        "date": utc_day,
        "count": new_count,
        "handles": ordered,
        "days": recent_days,
    }
    _atomic_write_json(ledger_path, payload)
    return new_count + len(in_flight_ordered)


def _record_join_ledger(ledger_path: Path, utc_day: str, handle: str) -> int:
    clean = _normalize_tg_handle(handle).lower()
    with _ro_join_process_lock(ledger_path.parent):
        data, _, joined_set, in_flight_set = _read_join_ledger_unlocked(ledger_path, utc_day)
        if clean:
            joined_set.add(clean)
            in_flight_set.discard(clean)
        _write_join_ledger_unlocked(ledger_path, utc_day, data, joined_set, in_flight_set)
        return len(joined_set) + len(in_flight_set)


def _try_reserve_join_slot(
    ledger_path: Path,
    utc_day: str,
    handle: str,
    effective_cap: int,
) -> tuple[bool, int, str]:
    clean = _normalize_tg_handle(handle).lower()
    with _ro_join_process_lock(ledger_path.parent):
        data, used_total, joined_set, in_flight_set = _read_join_ledger_unlocked(ledger_path, utc_day)
        if clean in joined_set:
            return False, used_total, "already_joined_today"
        if clean in in_flight_set:
            return False, used_total, "already_in_flight"
        if used_total >= effective_cap:
            return False, used_total, "daily_cap_reached"
        in_flight_set.add(clean)
        reserved_total = _write_join_ledger_unlocked(ledger_path, utc_day, data, joined_set, in_flight_set)
        return True, reserved_total, "reserved"


def _release_reserved_join_slot(ledger_path: Path, utc_day: str, handle: str) -> int:
    clean = _normalize_tg_handle(handle).lower()
    with _ro_join_process_lock(ledger_path.parent):
        data, _, joined_set, in_flight_set = _read_join_ledger_unlocked(ledger_path, utc_day)
        if clean in in_flight_set:
            in_flight_set.discard(clean)
            return _write_join_ledger_unlocked(ledger_path, utc_day, data, joined_set, in_flight_set)
        return len(joined_set) + len(in_flight_set)


_DISALLOWED_SESSION_PATH_RE = re.compile(r"(?:^|/)(?:data/sessions|sessions)(?:/|$)")
_OUTREACH_BASENAME_RE = re.compile(r"^(?:acc_?\d+|tim\d+|phone_checker_1)(?:\.session)?$")


def _is_isolated_ro_session_path(session_path: str | Path) -> bool:
    raw = str(session_path or "").strip()
    if not raw:
        return False
    candidates = [raw]
    try:
        expanded = str(Path(raw).expanduser())
        candidates.append(expanded)
        if Path(expanded).exists():
            candidates.append(os.path.realpath(expanded))
    except OSError:
        pass
    for cand in candidates:
        norm = cand.replace("\\", "/").lower()
        if "outreach" in norm:
            return False
        if "/scraper_sessions/" not in norm:
            if _DISALLOWED_SESSION_PATH_RE.search(norm):
                return False
        base = os.path.basename(norm)
        if _OUTREACH_BASENAME_RE.match(base):
            return False
    norm0 = os.path.realpath(str(Path(raw).expanduser())).replace("\\", "/").lower() if Path(raw).expanduser().exists() else raw.replace("\\", "/").lower()
    base0 = os.path.basename(norm0)
    return "/scraper_sessions/" in norm0 or base0.startswith("signals_ro")


def _assert_isolated_client(client: Any, *, conn: sqlite3.Connection | None = None) -> None:
    if client is None:
        return
    sess = getattr(client, "session", None)
    filename = str(getattr(sess, "filename", "") or "").strip()
    if not filename:
        return
    if not _is_isolated_ro_session_path(filename):
        raise ValueError(
            f"Refusing to join groups with non-isolated or outreach session '{filename}'. Pass an isolated RO session."
        )
    cand_path = Path(filename).expanduser()
    if cand_path.is_file():
        try:
            from leadgen import vps_autonomous_replenish as _repl

            sha = _repl._extract_sqlite_auth_key_sha256(cand_path)
        except Exception:
            sha = None
        if sha:
            live_dirs = [Path(__file__).resolve().parent.parent / "data" / "sessions"]
            if conn is not None:
                try:
                    live_dirs = _repl._collect_outreach_session_dirs(conn)
                    for row in conn.execute("SELECT session_blob, session_string, session_path FROM accounts").fetchall():
                        for cand_sha in (
                            _repl._extract_blob_auth_key_sha256(row[0]),
                            _repl._extract_string_session_auth_key_sha256(row[1]),
                        ):
                            if cand_sha and cand_sha == sha:
                                raise ValueError(
                                    f"Refusing to join groups: session '{filename}' shares auth_key with outreach account."
                                )
                except ValueError:
                    raise
                except Exception:
                    pass
            for sdir in live_dirs:
                if not sdir.is_dir():
                    continue
                for disk_sess in sdir.glob("*.session"):
                    try:
                        if _repl._extract_sqlite_auth_key_sha256(disk_sess) == sha:
                            raise ValueError(
                                f"Refusing to join groups: session '{filename}' shares auth_key with '{disk_sess}'."
                            )
                    except ValueError:
                        raise
                    except Exception:
                        continue


def _run_coro(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


async def _inspect_client_messages_for_captcha(client: Any, target: str) -> str:
    get_msgs = getattr(client, "get_messages", None)
    if not callable(get_msgs):
        return ""
    try:
        msgs = await get_msgs(target, limit=5)
    except Exception:
        return ""
    for msg in msgs or []:
        if isinstance(msg, str):
            text = msg
            sender_uname = ""
        else:
            text = str(getattr(msg, "message", None) or getattr(msg, "text", None) or "")
            sender = getattr(msg, "sender", None)
            sender_uname = str(getattr(sender, "username", "") or "")
        sig = _detect_captcha_signature(f"{sender_uname} {text}")
        if sig:
            return sig
    return ""


def _inspect_join_result_for_captcha(res: Any) -> tuple[bool, bool, str, str]:
    """Return ``(is_deferred, joined_occurred, reason, signature)`` for `join_fn` return values."""
    if res is False:
        return False, False, "join_returned_false", ""
    if isinstance(res, Mapping):
        deferred_flag = bool(
            res.get("deferred") or res.get("requires_approval") or res.get("captcha") or res.get("captcha_detected")
        )
        joined_occurred = bool(res.get("joined")) if deferred_flag else bool(res.get("joined") or res.get("ok"))
        if deferred_flag:
            sig = _detect_captcha_signature(
                " ".join(
                    str(res.get(k) or "")
                    for k in ("reason", "error", "signature", "bot", "bots", "messages", "recent_messages")
                )
            )
            return True, joined_occurred, str(res.get("reason") or "captcha_or_approval"), sig or "captcha"
        status = str(res.get("status") or "").strip().lower()
        if status in {"deferred", "captcha", "manual", "approval_required"}:
            return True, bool(res.get("joined")), f"status:{status}", status
        text_blob = " ".join(
            str(res.get(k) or "")
            for k in ("reason", "error", "signature", "bot", "bots", "messages", "recent_messages", "text")
        )
        sig = _detect_captcha_signature(text_blob)
        if sig:
            return True, joined_occurred, f"captcha_signature:{sig}", sig
        if _DEFERRED_ERROR_TEXT_RE.search(text_blob):
            return True, joined_occurred, "approval_or_forbidden", ""
        return False, True, "", ""
    if isinstance(res, str):
        sig = _detect_captcha_signature(res)
        if sig:
            return True, False, f"captcha_signature:{sig}", sig
    return False, True, "", ""


def safe_join_public_groups(
    handles: Sequence[str],
    *,
    state_dir: Path | str,
    client: Any = None,
    join_fn: Callable[[str], Any] | None = None,
    max_joins_per_day: int = MAX_RO_JOINS_PER_DAY,
) -> dict[str, Any]:
    """Join public TG groups using an explicit isolated client/join_fn with 2/day UTC ledger & captcha deferral."""
    state_path = _resolve_state_dir(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)
    _assert_isolated_client(client)

    effective_cap = min(max(0, int(max_joins_per_day)), MAX_RO_JOINS_PER_DAY)
    utc_day = _utc_today_str()
    ledger_path = state_path / "ro_join_ledger.json"
    queue_path = state_path / "manual_join_queue.jsonl"
    probe_cache_path = state_path / "tme_probe_cache.json"

    _, used_before, joined_today = _read_join_ledger(ledger_path, utc_day)
    manual_handles = {str(r.get("handle") or "").lower() for r in _load_manual_join_queue_rows(queue_path)}
    probe_cache = _read_json_safe(probe_cache_path, {})
    if not isinstance(probe_cache, dict):
        probe_cache = {}

    normalized: list[str] = []
    seen_input: set[str] = set()
    for raw in handles or ():
        clean = _normalize_tg_handle(str(raw))
        if not clean:
            continue
        low = clean.lower()
        if low not in seen_input:
            seen_input.add(low)
            normalized.append(clean)

    joined: list[str] = []
    deferred: list[str] = []
    deferred_details: list[dict[str, str]] = []
    skipped: list[str] = []
    skipped_details: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    if client is None and join_fn is None:
        for clean in normalized:
            skipped.append(clean)
            skipped_details.append({"handle": clean, "reason": "no_client_or_join_fn"})
        return {
            "state_dir": str(state_path),
            "utc_day": utc_day,
            "effective_cap": effective_cap,
            "used_before": used_before,
            "used_after": used_before,
            "remaining_quota": max(0, effective_cap - used_before),
            "joined": joined,
            "deferred": deferred,
            "deferred_details": deferred_details,
            "skipped": skipped,
            "skipped_details": skipped_details,
            "skipped_reason": "no_client_or_join_fn",
            "errors": errors,
        }

    def _defer(handle_val: str, reason_val: str, sig_val: str = "") -> None:
        _append_manual_join_queue(
            queue_path,
            handle=handle_val,
            reason=reason_val,
            signature=sig_val,
        )
        manual_handles.add(handle_val.lower())
        if handle_val not in deferred:
            deferred.append(handle_val)
            deferred_details.append({"handle": handle_val, "reason": reason_val, "signature": sig_val})

    def _skip(handle_val: str, reason_val: str) -> None:
        skipped.append(handle_val)
        skipped_details.append({"handle": handle_val, "reason": reason_val})

    async def _run_joins_async() -> int:
        used_now = used_before
        for clean in normalized:
            low = clean.lower()
            if _is_bot_handle(clean) or _is_junk_channel(clean):
                _skip(clean, "bot_or_junk")
                continue
            if low in manual_handles:
                _skip(clean, "already_deferred_manual")
                continue
            if low in joined_today:
                _skip(clean, "already_joined_today")
                continue

            cached_entry = probe_cache.get(low) if isinstance(probe_cache.get(low), Mapping) else {}
            cached_text = f"{clean} {cached_entry.get('title', '')} {cached_entry.get('bio', '')}"
            pre_sig = _detect_captcha_signature(cached_text)
            if pre_sig:
                _defer(clean, f"precheck_captcha:{pre_sig}", pre_sig)
                continue

            if used_now >= effective_cap:
                _skip(clean, "daily_cap_reached")
                continue

            # Pre-join message inspection if client exposes get_messages without failing
            if join_fn is None and client is not None:
                pre_msg_sig = await _inspect_client_messages_for_captcha(client, clean)
                if pre_msg_sig:
                    _defer(clean, f"precheck_messages:{pre_msg_sig}", pre_msg_sig)
                    continue

            reserved, used_now, reserve_reason = _try_reserve_join_slot(
                ledger_path, utc_day, clean, effective_cap
            )
            if not reserved:
                _skip(clean, reserve_reason)
                continue

            try:
                if join_fn is not None:
                    raw_res = join_fn(clean)
                    if inspect.isawaitable(raw_res):
                        raw_res = await raw_res
                    is_def, did_join, def_reason, def_sig = _inspect_join_result_for_captcha(raw_res)
                    if did_join:
                        used_now = _record_join_ledger(ledger_path, utc_day, clean)
                        joined_today.add(low)
                        joined.append(clean)
                    else:
                        used_now = _release_reserved_join_slot(ledger_path, utc_day, clean)
                    if is_def:
                        _defer(clean, def_reason, def_sig)
                    elif not did_join:
                        _skip(clean, def_reason or "join_declined")
                    continue

                # Telethon client path
                if JoinChannelRequest is None:
                    _release_reserved_join_slot(ledger_path, utc_day, clean)
                    raise RuntimeError("Telethon JoinChannelRequest is unavailable")
                await client(JoinChannelRequest(clean))
                used_now = _record_join_ledger(ledger_path, utc_day, clean)
                joined_today.add(low)
                joined.append(clean)

                post_sig = await _inspect_client_messages_for_captcha(client, clean)
                if post_sig:
                    _defer(clean, f"postjoin_captcha:{post_sig}", post_sig)
            except BaseException as exc:
                used_now = _release_reserved_join_slot(ledger_path, utc_day, clean)
                is_def, def_reason, def_sig = _is_deferred_join_exception(exc)
                if is_def:
                    _defer(clean, def_reason, def_sig)
                elif isinstance(exc, FloodWaitError) or type(exc).__name__ == "FloodWaitError":
                    errors.append({"handle": clean, "error": f"FloodWaitError:{exc}"})
                    _skip(clean, "flood_wait")
                    break
                else:
                    errors.append({"handle": clean, "error": f"{type(exc).__name__}: {exc}"})
                    _skip(clean, f"error:{type(exc).__name__}")
        return used_now

    _run_coro(_run_joins_async())
    _, used_after, _ = _read_join_ledger(ledger_path, utc_day)
    return {
        "state_dir": str(state_path),
        "utc_day": utc_day,
        "effective_cap": effective_cap,
        "used_before": used_before,
        "used_after": used_after,
        "remaining_quota": max(0, effective_cap - used_after),
        "joined": joined,
        "deferred": deferred,
        "deferred_details": deferred_details,
        "skipped": skipped,
        "skipped_details": skipped_details,
        "errors": errors,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Discover & replenish Telegram channel/group catalogs with safe RO joining",
    )
    ap.add_argument(
        "--state-dir",
        default="",
        help="State directory for discovered_*.txt, tg_catalog_state.json, ro_join_ledger.json",
    )
    ap.add_argument("--limit", type=int, default=40, help="Max new verified handles to accept per run")
    ap.add_argument(
        "--no-paid",
        action="store_true",
        help="Disable optional crawl4ai.web.search paid discovery ($0 catalogs + graph + curated only)",
    )
    ap.add_argument(
        "--safe-join",
        action="store_true",
        help="Attempt capped safe_join_public_groups when an isolated RO --session is explicitly supplied",
    )
    ap.add_argument(
        "--max-joins-per-day",
        type=int,
        default=MAX_RO_JOINS_PER_DAY,
        help=f"Daily RO join cap (hard-capped at {MAX_RO_JOINS_PER_DAY})",
    )
    ap.add_argument(
        "--session",
        default="",
        help="Optional isolated RO Telethon .session path (signals_ro* / phone_checker_*)",
    )
    ap.add_argument(
        "--handles",
        default="",
        help="Optional comma-separated handles for --safe-join (defaults to discovered_comment_channels.txt)",
    )
    ap.add_argument(
        "--market-profile",
        choices=["global", "gcc_december"],
        default="global",
        help="Market profile for channel & search discovery (default: global)",
    )
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)
    state_path = _resolve_state_dir(args.state_dir)

    summary = discover_and_replenish_catalogs(
        state_path,
        limit=args.limit,
        allow_paid=not args.no_paid,
        market_profile=args.market_profile,
    )

    if args.safe_join:
        raw_handles = [h.strip() for h in str(args.handles or "").split(",") if h.strip()]
        if not raw_handles:
            raw_handles = list(summary.get("comment_channels") or ())
            if not raw_handles:
                raw_handles = [
                    line.strip()
                    for line in _read_handle_lines(state_path / "discovered_comment_channels.txt")
                    if line.strip() and not line.strip().lower().startswith("id")
                ]

        sess = str(args.session or "").strip()
        api_id = int(os.getenv("TG_API_ID", "0") or 0)
        api_hash = str(os.getenv("TG_API_HASH", "") or "").strip()
        if sess and _is_isolated_ro_session_path(sess) and api_id and api_hash:
            from telethon import TelegramClient

            async def _run_cli_joins() -> dict[str, Any]:
                client = TelegramClient(sess, api_id, api_hash)
                await client.start()
                try:
                    return safe_join_public_groups(
                        raw_handles,
                        state_dir=state_path,
                        client=client,
                        max_joins_per_day=args.max_joins_per_day,
                    )
                finally:
                    await client.disconnect()

            summary["safe_join"] = _run_coro(_run_cli_joins())
        else:
            summary["safe_join"] = safe_join_public_groups(
                raw_handles,
                state_dir=state_path,
                client=None,
                max_joins_per_day=args.max_joins_per_day,
            )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
