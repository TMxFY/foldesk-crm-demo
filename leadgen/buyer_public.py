#!/usr/bin/env python3
"""Whop / Content Rewards public buyer discovery (no account login or join).

Parses active and recently finished clipping / UGC bounty pools from public
Whop / Content Rewards HTML, embedded Next.js / JSON payloads, and search
snippets. Extracts pool size (`pool_usd`), campaign status (`active` /
`finished`), brand domain / `project_url`, Discord invite / owner info, and
X / Telegram handles so whale leads flow into `contact_waterfall` cleanly.

Discord Bloss and @propinfa are skipped when reading them would mean joining.
A failure here must never fail the Telegram batch.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping
import hashlib
import html as html_mod
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import urllib.request

DISCOVER_URL = "https://contentrewards.com/c/discover"
DEFAULT_WHOP_MIN_POOL_USD = 5000
_TOKEN_KEYS = ("DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "BLOSS_DISCORD_TOKEN")

_WHOP_SEARCH_QUERIES = (
    'site:whop.com ("content rewards" OR "clipping") ("$5,000" OR "$10,000" OR "$15,000" OR "$20,000" OR "$25,000" OR "$50,000" OR "$5K" OR "$10K") -blog -terms',
    'site:whop.com ("content rewards" OR "bounty" OR "clipping") ("ended" OR "finished" OR "completed" OR "100% paid" OR "budget") -blog -terms',
    'site:contentrewards.com ("$" OR "pool" OR "budget" OR "ended" OR "active") -terms -privacy',
    'site:whop.com ("x.com/" OR "t.me/" OR "discord.gg/") ("content rewards" OR "clipping" OR "ugc") -blog -terms',
    'site:whop.com ("saas" OR "ai" OR "fitness" OR "app" OR "trading") ("content rewards" OR "per 1,000 views") -blog -terms',
    'site:whop.com ("bounty" OR "creator program" OR "affiliates") ("x.com/" OR "t.me/" OR "discord.gg/") -blog -terms',
)

_X_PROFILE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter|mobile\.twitter)\.com/@?([A-Za-z0-9_]{1,15})(?=(?:/?(?:[?#\"'\s<>.,;:\)]|$)))",
    re.I,
)
_X_TEXT_HANDLE = re.compile(
    r"\b(?:x|twitter)\s*[:\-]\s*@([A-Za-z0-9_]{1,15})\b",
    re.I,
)
_TG_PROFILE = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/@?([A-Za-z][A-Za-z0-9_]{3,31})(?=(?:/?(?:[?#\"'\s<>.,;:\)]|$)))",
    re.I,
)
_TG_TEXT_HANDLE = re.compile(
    r"(?:\b(?:tg|telegram)\s*[:\-]?\s*@|tg://resolve\?domain=)([A-Za-z][A-Za-z0-9_]{3,31})\b",
    re.I,
)
_LI_PROFILE = re.compile(
    r"https?://(?:[\w.]+\.)?linkedin\.com/in/([A-Za-z0-9_\-%]+)",
    re.I,
)
_IG_PROFILE = re.compile(
    r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})(?=(?:/?(?:[?#\"'\s<>.,;:\)]|$)))",
    re.I,
)
_DISCORD_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9\-]{2,64})(?=(?:/?(?:[?#\"'\s<>.,;:\)]|$)))",
    re.I,
)
_DISCORD_OWNER_RE = re.compile(
    r"\bdiscord(?:\s+(?:owner|admin|founder|creator|contact|user|username|handle))?\s*[:\-]\s*@?([A-Za-z0-9_.]{2,32}(?:#\d{4})?)\b",
    re.I,
)
_GENERIC_OWNER_HANDLE_RE = re.compile(
    r"\b(?:owner|creator|founder)\s*[:\-]\s*@([A-Za-z0-9_.]{2,32}(?:#\d{4})?)\b",
    re.I,
)
_DISCORD_USER_URL_RE = re.compile(
    r"https?://(?:www\.)?discord(?:app)?\.com/users/(\d{15,22})",
    re.I,
)
_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b"
)
_HTTP_URL_RE = re.compile(
    r"https?://[^\s<>\"'\)\]\},;]+",
    re.I,
)
_BARE_DOMAIN_RE = re.compile(
    r"(?:^|[\s(\[:])(?:www\.)?([a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\."
    r"(?:com|io|ai|app|co|org|net|xyz|gg|so|dev|club|pro|trade|finance|fit|shop|store|tv|me|us|uk|de|ae))"
    r"(?=$|[/?#\s),;\]])",
    re.I,
)

_CPM_PHRASE_RE = re.compile(
    r"\$\s*\d+(?:[.,]\d+)?(?:\s*[-–—to]+\s*\$?\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?:per\s+1[,.]?000(?:\s+views?)?|per\s+1k(?:\s+views?)?|/\s*1[,.]?000(?:\s+views?)?|/\s*1k(?:\s+views?)?|"
    r"per\s+(?:view|clip|video|post)\b|\bcpm\b)",
    re.I,
)
_SUB_PRICE_RE = re.compile(
    r"\$\s*\d+(?:[.,]\d+)?\s*(?:/\s*(?:mo|month|yr|year|wk|week)|per\s+(?:month|year|week))\b",
    re.I,
)
_PAID_OF_TOTAL_RE = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?\s*(?:/|of|out\s+of)\s*\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?",
    re.I,
)
_LABELED_POOL_BEFORE_RE = re.compile(
    r"\b(?:total\s+|prize\s+|bounty\s+|reward\s+|rewards\s+|campaign\s+|clipping\s+|remaining\s+)?"
    r"(?:pool|budget|bounty|prize\s*pool|allocated|allocation|total\s*rewards?)"
    r"\s*(?:is|of|:|=|[-–—])?\s*(?:\$\s*|\bUSD\s*)(\d[\d,]*(?:\s\d{3})*(?:\.\d+)?)\s*([kKmM])?"
    r"(?=\+|[^A-Za-z0-9]|$)",
    re.I,
)
_LABELED_POOL_AFTER_RE = re.compile(
    r"(?:\$\s*|\bUSD\s*)(\d[\d,]*(?:\s\d{3})*(?:\.\d+)?)\s*([kKmM])?\+?\s*"
    r"(?:total\s+)?(?:pool|budget|bounty|prize\s*pool|rewards?\s*pool|allocated|in\s+rewards|total\s+rewards|campaign\s+pool|paid\s*out)\b",
    re.I,
)
_GENERAL_USD_RE = re.compile(
    r"(?:\$\s*(\d[\d,]*(?:\s\d{3})*(?:\.\d+)?)\s*([kKmM])?(?=\+|[^A-Za-z0-9]|$)"
    r"|\bUSD\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?(?=\+|[^A-Za-z0-9]|$)"
    r"|(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?\s*USD\b)",
    re.I,
)

_FINISHED_STATUS_VALUES = frozenset({
    "finished", "ended", "completed", "closed", "expired",
    "inactive", "archived", "depleted", "past", "paused_ended",
})
_ACTIVE_STATUS_VALUES = frozenset({
    "active", "live", "open", "running", "accepting", "ongoing",
})
_FINISHED_TEXT_RE = re.compile(
    r"(?:"
    r"\b(?:status|state)\s*[:\-]\s*(?:finished|ended|completed|closed|expired|past)\b|"
    r"\b(?:campaign|bounty|rewards?|pool|submissions?)\s+(?:has\s+)?(?:ended|finished|completed|closed|expired)\b|"
    r"\b(?:recently\s+)?(?:finished|ended|completed|expired)\b|"
    r"\b(?:budget\s+(?:depleted|exhausted|reached|filled)|100%\s*(?:paid|claimed|filled)|"
    r"no\s+longer\s+accepting|submissions\s+closed|payout\s+complete|past\s+campaign)\b"
    r")",
    re.I,
)
_ACTIVE_TEXT_RE = re.compile(
    r"(?:\b(?:status|state)\s*[:\-]\s*(?:active|live|open|running)\b|\baccepting\s+submissions\b|\blive\s+now\b)",
    re.I,
)
_CAMPAIGN_BUDGET_SIGNAL_RE = re.compile(
    r"(?:"
    r"content\s+rewards|per\s+1[,.]?000\s+views?|/\s*1k\s+views?|\bcpm\b|\bbounty\b|"
    r"\bclipping\b|\brewards?\s+pool\b|\bprize\s+pool\b|\bbudget\b|\bcreator\s+program\b|"
    r"\bpaying\s+clippers\b|\bpay\s+per\s+view\b|whop\.com/(?:discover|content-rewards)|contentrewards\.com"
    r")",
    re.I,
)
_BANNED_GAMING_RE = re.compile(
    r"\b(?:roblox|fortnite|fivem|minecraft\s+server|gta\s*rp)\b",
    re.I,
)
_SUPPLY_SPAM_RE = re.compile(
    r"(?:"
    r"got\s+accepted\s+into.{0,40}content\s+rewards|"
    r"my\s+(?:last|next)\s+payout|getting\s+paid\s+for\s+(?:my|our)\s+content|"
    r"clipster\s+withdrawal|x\s+pays\s+creators|@xcreators|"
    r"\bi\s+clip\b|\bi['’]?m\s+a\s+clipper\b|\bi\s+am\s+a\s+clipper\b|\bwe\s+clip\b|"
    r"\[for\s*hire\]|\bfor\s+hire\b|looking\s+for\s+(?:a\s+)?(?:job|work)"
    r")",
    re.I,
)

_DENY = {
    "whop", "contentrewards", "softugc", "i", "home", "search", "explore",
    "intent", "hashtag", "compose", "share", "login", "signup", "settings",
    "privacy", "tos", "pamba_ai", "karolzdeb", "usefastlane", "gauravsbuilding",
    "arcads_ai", "rom1trs", "creatify_ai", "makeugc", "fyncas",
}
_TG_DENY = {
    "joinchat", "share", "addstickers", "proxy", "iv", "s", "c", "boost",
    "socks", "login", "contact", "addlist", "contentrewardsbot",
}
_IG_DENY = {
    "p", "reel", "reels", "stories", "explore", "accounts", "about", "legal",
    "privacy", "whop", "contentrewards",
}
_LI_DENY = {"share", "example", "username", "yourname"}
_DISCORD_INVITE_DENY = {
    "login", "register", "download", "channels", "assets", "api", "oauth2",
    "terms", "privacy", "safety", "support", "app", "nitro", "servers",
}
_DISCORD_OWNER_NOISE = {
    "server", "link", "join", "here", "channel", "community", "invite",
    "discord", "http", "https", "gg", "com", "none", "n/a", "na", "dm", "only",
}
_EMAIL_SKIP_DOMAINS = {
    "whop.com", "contentrewards.com", "example.com", "example.org", "sentry.io",
    "wixpress.com", "test.com", "gmail.com", "yahoo.com", "outlook.com",
    "hotmail.com", "icloud.com", "proton.me", "protonmail.com",
}
_NON_BRAND_SLUGS = {
    "discover", "content-rewards", "contentrewards", "c", "bounties", "bounty",
    "clipping", "affiliates", "creators", "brands", "home", "about", "terms",
    "privacy", "blog", "articles", "login", "signup", "support", "help",
}
_NON_BRAND_ANCHOR_WORDS = {
    "website", "site", "visit", "visit site", "visit website", "join", "join now",
    "apply", "apply now", "clip now", "view", "view campaign", "details", "learn more",
    "discord", "join discord", "telegram", "twitter", "x", "link", "here", "owner",
    "more", "submit", "open",
}
_TRACKING_QUERY_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "fbclid", "gclid", "mc_cid", "mc_eid", "a",
}
_ASSET_EXTS = (
    ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".ico", ".bmp", ".avif",
    ".css", ".js", ".mjs", ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip",
    ".mp4", ".mov", ".webm", ".json", ".xml",
)
_PLATFORM_HOSTS = (
    "whop.com", "apps.whop.com", "contentrewards.com", "contentrewards.io",
    "x.com", "twitter.com", "mobile.twitter.com", "t.co",
    "t.me", "telegram.me", "telegram.dog", "telegram.org", "telegra.ph",
    "discord.gg", "discord.com", "discordapp.com",
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com",
    "fb.com", "linkedin.com", "lnkd.in", "reddit.com", "threads.net", "pinterest.com",
    "vk.com", "linktr.ee", "linktree.com", "beacons.ai", "solo.to", "carrd.co",
    "bento.me", "bio.site", "campsite.bio", "stan.store", "taplink.cc",
    "bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "buff.ly", "rb.gy", "cutt.ly",
    "shorturl.at", "notion.site", "notion.so", "docs.google.com", "drive.google.com",
    "forms.gle", "typeform.com", "airtable.com", "calendly.com", "github.com",
    "gitlab.com", "medium.com", "substack.com", "producthunt.com", "gumroad.com",
    "skool.com", "patreon.com", "stripe.com", "buy.stripe.com",
    "apps.apple.com", "itunes.apple.com", "play.google.com",
    "google.com", "gstatic.com", "fonts.googleapis.com", "fonts.gstatic.com",
    "cloudflare.com", "cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com",
    "w3.org", "schema.org", "googletagmanager.com", "google-analytics.com",
    "amazonaws.com", "cloudfront.net", "vercel-insights.com", "sentry.io",
    "intercom.io", "crisp.chat",
)

_BRAND_PREFIX_RE = re.compile(
    r"^(?:earn\s+\$[\d.,]+\s+per\s+1[,.]?000\s+views(?:\s+clipping)?\s+|"
    r"(?:active|finished|ended|completed)\s*[:|\-–—]\s*|"
    r"clipping\s+|join\s+|content\s+rewards\s*[:|\-–—]\s*)+",
    re.I,
)
_BRAND_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—|]\s*\$[\d.,]+[kKmM]?\s*(?:budget|pool|content\s+rewards|bounty|rewards)?.*|"
    r"\|\s*content\s+rewards.*|"
    r"\s*[-–—|:]\s*(?:whop|content\s+rewards)\b.*|"
    r"\s*\((?:active|finished|ended|completed|\$[\d.,]+[kKmM]?[^)]*)\)\s*|"
    r"\s*\[(?:active|finished|ended|completed)\]\s*)$",
    re.I,
)
_CARD_MARKER_RE = re.compile(
    r"\b(?:card|campaign|bounty|reward|pool|listing|discover-card|experience|program-item)\b",
    re.I,
)
_CONTAINER_MARKER_RE = re.compile(
    r"\b(?:list|grid|feed|container|wrapper|layout|page|header|footer|nav|sidebar|modal|section-wrap)\b",
    re.I,
)
_HEADING_CLASS_RE = re.compile(r"\b(?:title|brand|company|name|heading)\b", re.I)
_VOID_HTML_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


def _resolve_buyer_public_seen_path() -> Path:
    state_dir_raw = str(os.environ.get("LEADGEN_STATE_DIR") or "").strip()
    if state_dir_raw:
        return Path(state_dir_raw) / "buyer_public_seen.json"
    return Path(__file__).resolve().parents[1] / "data" / "leadgen_state" / "buyer_public_seen.json"


def _resolve_leadgen_config_path() -> Path:
    custom = str(os.environ.get("LEADGEN_CONFIG_PATH") or "").strip()
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parents[1] / "data" / "leadgen_config.json"


def resolve_whop_min_pool_usd(
    cfg: Mapping[str, Any] | None = None,
    *,
    min_pool_usd: int | None = None,
) -> int:
    """Resolve configurable minimum Whop bounty pool in USD (default 5000)."""
    if min_pool_usd is not None:
        return max(0, int(min_pool_usd))
    env_raw = str(os.environ.get("WHOP_MIN_POOL_USD") or "").strip()
    if env_raw:
        try:
            return max(0, int(float(env_raw)))
        except ValueError:
            pass
    active_cfg: Mapping[str, Any] | None = cfg
    if active_cfg is None:
        cfg_path = _resolve_leadgen_config_path()
        if cfg_path.is_file():
            try:
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    active_cfg = loaded
            except (OSError, json.JSONDecodeError):
                active_cfg = None
    if isinstance(active_cfg, Mapping):
        for section in ("whales", "b2b"):
            sub = active_cfg.get(section)
            if isinstance(sub, Mapping) and sub.get("whop_min_pool_usd") is not None:
                try:
                    return max(0, int(float(sub["whop_min_pool_usd"])))
                except (TypeError, ValueError):
                    pass
        if active_cfg.get("whop_min_pool_usd") is not None:
            try:
                return max(0, int(float(active_cfg["whop_min_pool_usd"])))
            except (TypeError, ValueError):
                pass
    return DEFAULT_WHOP_MIN_POOL_USD


def _parse_usd_number(num_str: str, suffix: str | None) -> int | None:
    cleaned = str(num_str or "").replace(",", "").replace(" ", "").strip()
    if not cleaned:
        return None
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if val < 0:
        return None
    mult = {"k": 1000, "m": 1_000_000}.get((suffix or "").lower(), 1)
    return int(round(val * mult))


def parse_pool_usd(text: str) -> int | None:
    """Extract integer USD pool amount from `$5K`, `$10,000`, `$55k`, `$101,000`, etc."""
    raw = html_mod.unescape(str(text or "")).strip()
    if not raw:
        return None
    scrubbed = _CPM_PHRASE_RE.sub(" ", raw)
    scrubbed = _SUB_PRICE_RE.sub(" ", scrubbed)

    labeled_vals: list[int] = []
    for match in _PAID_OF_TOTAL_RE.finditer(scrubbed):
        total = _parse_usd_number(match.group(3), match.group(4))
        if total is not None:
            labeled_vals.append(total)
    for pattern in (_LABELED_POOL_BEFORE_RE, _LABELED_POOL_AFTER_RE):
        for match in pattern.finditer(scrubbed):
            amt = _parse_usd_number(match.group(1), match.group(2))
            if amt is not None:
                labeled_vals.append(amt)
    if labeled_vals:
        return max(labeled_vals)

    general_vals: list[int] = []
    for match in _GENERAL_USD_RE.finditer(scrubbed):
        num = match.group(1) or match.group(3) or match.group(5)
        suf = match.group(2) or match.group(4) or match.group(6)
        amt = _parse_usd_number(num, suf)
        if amt is not None:
            general_vals.append(amt)
    return max(general_vals) if general_vals else None


def detect_campaign_status(
    text: str,
    *,
    item: Mapping[str, Any] | None = None,
) -> Literal["active", "finished"]:
    """Classify a Whop / Content Rewards campaign as `active` or `finished`."""
    if isinstance(item, Mapping):
        for key in ("campaign_status", "status", "state"):
            val = str(item.get(key) or "").strip().lower()
            if val in _FINISHED_STATUS_VALUES:
                return "finished"
            if val in _ACTIVE_STATUS_VALUES:
                return "active"
        for bool_key, expected_finished in (
            ("is_active", False),
            ("isActive", False),
            ("active", False),
            ("ended", True),
            ("is_ended", True),
            ("finished", True),
            ("is_finished", True),
            ("closed", True),
        ):
            if bool_key in item and item[bool_key] is expected_finished:
                return "finished"
        paid_raw = item.get("paid_out") if item.get("paid_out") is not None else item.get("budget_used")
        total_raw = (
            item.get("pool_usd")
            if item.get("pool_usd") is not None
            else item.get("total_budget") or item.get("budget")
        )
        if paid_raw is not None and total_raw is not None:
            paid_amt = paid_raw if isinstance(paid_raw, (int, float)) else parse_pool_usd(str(paid_raw))
            total_amt = total_raw if isinstance(total_raw, (int, float)) else parse_pool_usd(str(total_raw))
            if paid_amt is not None and total_amt is not None and total_amt > 0 and paid_amt >= total_amt:
                return "finished"

    clean = html_mod.unescape(str(text or ""))
    for match in _PAID_OF_TOTAL_RE.finditer(clean):
        paid = _parse_usd_number(match.group(1), match.group(2))
        total = _parse_usd_number(match.group(3), match.group(4))
        if paid is not None and total is not None and total > 0 and paid >= total:
            return "finished"

    if _ACTIVE_TEXT_RE.search(clean) and not _FINISHED_TEXT_RE.search(clean):
        return "active"
    if _FINISHED_TEXT_RE.search(clean):
        return "finished"
    return "active"


def _load_buyer_public_state(seen_path: Path) -> tuple[int, list[str]]:
    if not seen_path.is_file():
        return 0, []
    try:
        obj = json.loads(seen_path.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            idx = int(obj.get("query_idx") or 0)
            seen = [str(x) for x in (obj.get("seen_ids") or []) if str(x).strip()]
            return idx, seen
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0, []
    return 0, []


def _save_buyer_public_state(seen_path: Path, query_idx: int, seen_ids: list[str]) -> None:
    try:
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(
            json.dumps(
                {"query_idx": int(query_idx) % len(_WHOP_SEARCH_QUERIES), "seen_ids": seen_ids[-2000:]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _is_platform_host(host: str) -> bool:
    h = (host or "").lower().removeprefix("www.").rstrip(".")
    if not h or "." not in h:
        return True
    return any(h == p or h.endswith("." + p) for p in _PLATFORM_HOSTS)


def _normalize_external_url(raw: str) -> tuple[str, str]:
    """Return `(normalized_url, domain)` for a real external brand URL, else `('', '')`."""
    s = html_mod.unescape(str(raw or "")).strip().rstrip(".,;:)'\"]")
    if not s or " " in s or s.startswith("@"):
        return "", ""
    low = s.lower()
    if low.startswith(("mailto:", "tel:", "javascript:", "data:", "tg:")):
        return "", ""
    if "://" not in s:
        s = f"https://{s}"
    parsed = urlparse(s)
    if parsed.scheme.lower() not in {"http", "https"}:
        return "", ""
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    if _is_platform_host(host):
        return "", ""
    path_low = (parsed.path or "").lower()
    if any(path_low.endswith(ext) for ext in _ASSET_EXTS):
        return "", ""
    filtered_q = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_QUERY_PARAMS
    ]
    clean_path = parsed.path if parsed.path not in ("", "/") else ""
    norm_url = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), clean_path, "", urlencode(filtered_q), ""))
    return norm_url, host


def _normalize_whop_source_url(raw: str) -> str:
    s = html_mod.unescape(str(raw or "")).strip().rstrip(".,;:)'\"]")
    if not s.lower().startswith(("http://", "https://")):
        return ""
    parsed = urlparse(s)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not (host == "whop.com" or host.endswith(".whop.com") or host == "contentrewards.com" or host.endswith(".contentrewards.com")):
        return ""
    filtered_q = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_QUERY_PARAMS
    ]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", urlencode(filtered_q), ""))


def _is_whop_noise_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "/").rstrip("/").lower() or "/"
    if host.startswith("docs."):
        return True
    return any(
        tok in path
        for tok in ("/blog", "/articles", "/terms", "-terms", "/privacy", "/legal", "/login", "/signup", "/support", "/help")
    )


def _clean_brand_name(raw: str) -> str:
    t = html_mod.unescape(" ".join(str(raw or "").split())).strip()
    if not t:
        return ""
    t = _BRAND_SUFFIX_RE.sub("", t).strip()
    t = _BRAND_PREFIX_RE.sub("", t).strip()
    t = _BRAND_SUFFIX_RE.sub("", t).strip()
    t = re.sub(r"https?://\S+", "", t).strip()
    t = t.strip(" -–—|:•·[]()")
    low = t.casefold()
    if len(t) < 2 or len(t) > 80:
        return ""
    if t.startswith("@") or "." in t and " " not in t and _is_platform_host(t):
        return ""
    if low in _NON_BRAND_SLUGS or low in _NON_BRAND_ANCHOR_WORDS or low in {"unknown", "none", "n/a", "na", "brand", "official"}:
        return ""
    if any(tok in low for tok in ("terms of service", "privacy policy", "how to", "clipper hub", "content rewards")):
        return ""
    return t


def _brand_from_domain(domain: str) -> str:
    if not domain:
        return ""
    parts = [p for p in domain.lower().split(".") if p]
    if not parts:
        return ""
    base = parts[-2] if len(parts) >= 3 and parts[0] in {"app", "get", "try", "use", "go", "my", "hey"} else parts[0]
    if not base or len(base) < 2 or base in _NON_BRAND_SLUGS:
        return ""
    if "-" in base or "_" in base:
        tokens = [tok for tok in re.split(r"[\-_]+", base) if tok]
        return " ".join(tok.upper() if tok in {"ai", "io", "ui", "os"} else tok.capitalize() for tok in tokens)
    return base[:1].upper() + base[1:]


def _brand_from_whop_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if "whop.com" not in host and "contentrewards.com" not in host:
        return ""
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    for seg in reversed(parts):
        low = seg.lower()
        if low in _NON_BRAND_SLUGS or low in {"c", "d", "p", "campaign", "campaigns"}:
            continue
        if re.fullmatch(r"[0-9a-f]{6,}", low) or (len(low) >= 16 and not "-" in low and any(c.isdigit() for c in low)):
            continue
        words = [w for w in re.split(r"[\-_]+", seg) if w]
        if words:
            return " ".join(w.upper() if w.lower() in {"ai", "io", "fx"} else w.capitalize() for w in words)[:80]
    return ""


def _extract_discord_info(text: str, *, item: Mapping[str, Any] | None = None, links: Iterable[str] = ()) -> dict[str, str]:
    discord_url = ""
    discord_invite = ""
    discord_owner = ""

    def _accept_invite_match(raw_url: str, code: str) -> None:
        nonlocal discord_url, discord_invite
        if not code or code.lower() in _DISCORD_INVITE_DENY:
            return
        if not discord_invite:
            discord_invite = code
            lower_u = raw_url.lower()
            if "discord.com/invite/" in lower_u or "discordapp.com/invite/" in lower_u:
                discord_url = f"https://discord.com/invite/{code}"
            else:
                discord_url = f"https://discord.gg/{code}"

    if isinstance(item, Mapping):
        for key in ("discord_url", "discord_invite", "discord", "discordLink", "discord_link"):
            val = str(item.get(key) or "").strip()
            if not val:
                continue
            m = _DISCORD_INVITE_RE.search(val)
            if m:
                _accept_invite_match(val, m.group(1))
            elif re.fullmatch(r"[A-Za-z0-9\-]{3,32}", val) and val.lower() not in _DISCORD_INVITE_DENY:
                _accept_invite_match(f"https://discord.gg/{val}", val)
        for key in ("discord_owner", "discord_username", "discord_handle", "creator_discord", "owner_discord"):
            val = str(item.get(key) or "").strip().lstrip("@")
            if val and val.lower() not in _DISCORD_OWNER_NOISE:
                discord_owner = val
                break

    combined_sources = [str(u) for u in links if u] + [str(text or "")]
    for blob in combined_sources:
        if not discord_invite:
            m = _DISCORD_INVITE_RE.search(blob)
            if m:
                _accept_invite_match(m.group(0), m.group(1))
        if not discord_owner:
            m_user = _DISCORD_USER_URL_RE.search(blob)
            if m_user:
                discord_owner = f"discord.com/users/{m_user.group(1)}"
            else:
                for m_own in _DISCORD_OWNER_RE.finditer(blob):
                    cand = m_own.group(1).strip().rstrip(".,;:")
                    if cand and cand.lower() not in _DISCORD_OWNER_NOISE and not cand.lower().startswith("discord."):
                        discord_owner = cand
                        break
    if discord_invite and not discord_owner:
        m_gen = _GENERIC_OWNER_HANDLE_RE.search(str(text or ""))
        if m_gen:
            cand = m_gen.group(1).strip().rstrip(".,;:")
            if cand and cand.lower() not in _DISCORD_OWNER_NOISE:
                discord_owner = cand
    return {
        "discord_url": discord_url,
        "discord_invite": discord_invite,
        "discord_owner": discord_owner,
    }


def resolve_discord_invite(
    invite: str,
    *,
    fetch_json_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Fetch public Discord invite metadata (no guild join or bot token needed)."""
    raw = str(invite or "").strip()
    if not raw:
        return {}
    m = _DISCORD_INVITE_RE.search(raw)
    code = m.group(1) if m else raw.strip("/").split("/")[-1]
    if not code or not re.fullmatch(r"[A-Za-z0-9\-]{2,64}", code) or code.lower() in _DISCORD_INVITE_DENY:
        return {}
    api_url = f"https://discord.com/api/v9/invites/{code}?with_counts=true"
    payload: Any = None
    if fetch_json_fn is not None:
        try:
            payload = fetch_json_fn(api_url)
        except Exception:
            return {}
    else:
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception:
            return {}
    if not isinstance(payload, dict):
        return {}
    guild = payload.get("guild") if isinstance(payload.get("guild"), dict) else {}
    inviter = payload.get("inviter") if isinstance(payload.get("inviter"), dict) else {}
    owner = str(inviter.get("username") or inviter.get("global_name") or "").strip()
    g_desc = str(guild.get("description") or "").strip()
    g_name = str(guild.get("name") or "").strip()
    ext_url, ext_dom = "", ""
    for m_u in _HTTP_URL_RE.finditer(g_desc):
        u, d = _normalize_external_url(m_u.group(0))
        if d:
            ext_url, ext_dom = u, d
            break
    x_match = _X_PROFILE.search(g_desc)
    x_handle = x_match.group(1) if x_match and x_match.group(1).casefold() not in _DENY else ""
    tg_match = _TG_PROFILE.search(g_desc)
    tg_handle = (
        tg_match.group(1)
        if tg_match and tg_match.group(1).casefold() not in _TG_DENY and not tg_match.group(1).lower().endswith("bot")
        else ""
    )
    return {
        "discord_url": f"https://discord.gg/{code}",
        "discord_invite": code,
        "guild_name": g_name,
        "guild_description": g_desc,
        "member_count": int(payload.get("approximate_member_count") or 0),
        "discord_owner": owner,
        "project_url": ext_url,
        "domain": ext_dom,
        "x_handle": x_handle,
        "tg_handle": tg_handle,
    }


def _extract_contacts_from_blob(
    text: str,
    *,
    links: Iterable[str] = (),
    item: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract X, TG, LinkedIn, Instagram, Email, project_url/domain, Discord from text/links/dict."""
    x_handles: list[str] = []
    tg_handles: list[str] = []
    seen_x: set[str] = set()
    seen_tg: set[str] = set()
    project_url = ""
    domain = ""
    source_whop_url = ""
    founder_linkedin = ""
    instagram_url = ""
    work_email = ""

    def _add_x(handle: str) -> None:
        h = str(handle or "").strip().lstrip("@")
        if h and re.fullmatch(r"[A-Za-z0-9_]{1,15}", h) and h.casefold() not in _DENY and h.casefold() not in seen_x:
            seen_x.add(h.casefold())
            x_handles.append(h)

    def _add_tg(nick: str) -> None:
        u = str(nick or "").strip().lstrip("@")
        if (
            u
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", u)
            and u.casefold() not in _TG_DENY
            and u.casefold() not in _DENY
            and not u.lower().endswith("bot")
            and u.casefold() not in seen_tg
        ):
            seen_tg.add(u.casefold())
            tg_handles.append(u)

    def _inspect_url(raw_u: str) -> None:
        nonlocal project_url, domain, source_whop_url, founder_linkedin, instagram_url
        su = html_mod.unescape(str(raw_u or "")).strip()
        if not su:
            return
        mx = _X_PROFILE.search(su)
        if mx:
            _add_x(mx.group(1))
        mtg = _TG_PROFILE.search(su)
        if mtg:
            _add_tg(mtg.group(1))
        if not founder_linkedin:
            mli = _LI_PROFILE.search(su)
            if mli and mli.group(1).lower() not in _LI_DENY:
                founder_linkedin = f"https://www.linkedin.com/in/{mli.group(1)}"
        if not instagram_url:
            mig = _IG_PROFILE.search(su)
            if mig and mig.group(1).lower() not in _IG_DENY:
                instagram_url = f"https://www.instagram.com/{mig.group(1)}"
        if not source_whop_url:
            wu = _normalize_whop_source_url(su)
            if wu and not _is_whop_noise_url(wu):
                source_whop_url = wu
        if not project_url:
            nu, nd = _normalize_external_url(su)
            if nd:
                project_url, domain = nu, nd

    if isinstance(item, Mapping):
        for wkey in ("project_url", "website", "website_url", "external_url", "brand_url", "company_url", "homepage", "site"):
            if item.get(wkey):
                nu, nd = _normalize_external_url(str(item[wkey]))
                if nd:
                    project_url, domain = nu, nd
                    break
        if not domain and item.get("domain"):
            nu, nd = _normalize_external_url(str(item["domain"]))
            if nd:
                project_url, domain = nu, nd
        for ukey in ("url", "whop_url", "campaign_url"):
            if item.get(ukey):
                _inspect_url(str(item[ukey]))
        for xkey in ("founder_x", "founder_twitter", "twitter", "x_url", "x"):
            val = str(item.get(xkey) or "").strip()
            if val:
                mx = _X_PROFILE.search(val)
                _add_x(mx.group(1) if mx else val)
        for tgkey in ("telegram_username", "username", "telegram", "telegram_url", "tg"):
            val = str(item.get(tgkey) or "").strip()
            if val:
                mtg = _TG_PROFILE.search(val)
                _add_tg(mtg.group(1) if mtg else val)
        for emkey in ("work_email", "email", "contact_email", "owner_email"):
            val = str(item.get(emkey) or "").strip()
            if val and "@" in val:
                em_m = _EMAIL_RE.search(val)
                if em_m:
                    em = em_m.group(1).lower()
                    if em.split("@", 1)[1] not in _EMAIL_SKIP_DOMAINS:
                        work_email = em
                        break

    for u in links:
        _inspect_url(u)
    for m_u in _HTTP_URL_RE.finditer(text or ""):
        _inspect_url(m_u.group(0))
    for mx_t in _X_TEXT_HANDLE.finditer(text or ""):
        _add_x(mx_t.group(1))
    for mtg_t in _TG_TEXT_HANDLE.finditer(text or ""):
        _add_tg(mtg_t.group(1))

    if not project_url:
        for md in _BARE_DOMAIN_RE.finditer(text or ""):
            nu, nd = _normalize_external_url(md.group(1))
            if nd:
                project_url, domain = nu, nd
                break

    if not work_email:
        for em_m in _EMAIL_RE.finditer(text or ""):
            em = em_m.group(1).lower()
            host = em.split("@", 1)[1]
            if host not in _EMAIL_SKIP_DOMAINS and not em.startswith(("noreply@", "no-reply@")):
                work_email = em
                break

    if not project_url and work_email and "@" in work_email:
        em_host = work_email.split("@", 1)[1]
        if em_host not in _EMAIL_SKIP_DOMAINS:
            nu, nd = _normalize_external_url(em_host)
            if nd:
                project_url, domain = nu, nd

    discord = _extract_discord_info(text, item=item, links=links)
    return {
        "x_handles": x_handles,
        "tg_handles": tg_handles,
        "project_url": project_url,
        "domain": domain,
        "source_whop_url": source_whop_url,
        "founder_linkedin": founder_linkedin,
        "instagram_url": instagram_url,
        "work_email": work_email,
        **discord,
    }


def _build_whop_row(
    *,
    handle: str = "",
    telegram: str = "",
    brand: str = "",
    owner_name: str = "",
    source_url: str = DISCOVER_URL,
    project_url: str = "",
    domain: str = "",
    discord_url: str = "",
    discord_invite: str = "",
    discord_owner: str = "",
    founder_linkedin: str = "",
    instagram_url: str = "",
    work_email: str = "",
    pool_usd: int | None = None,
    campaign_status: Literal["active", "finished"] = "active",
    snippet: str = "",
) -> dict[str, Any]:
    x_url = f"https://x.com/{handle}" if handle else ""
    tg_url = f"https://t.me/{telegram}" if telegram else ""
    social: list[dict[str, str]] = []
    if x_url:
        social.append({"platform": "x", "url": x_url, "handle": handle})
    if founder_linkedin:
        li_slug = founder_linkedin.rstrip("/").rsplit("/", 1)[-1]
        social.append({"platform": "linkedin", "url": founder_linkedin, "handle": li_slug})

    clean_brand = _clean_brand_name(brand) or _brand_from_whop_url(source_url) or _brand_from_domain(domain)
    display_name = (
        owner_name.strip()
        or clean_brand
        or handle
        or telegram
        or discord_owner
        or discord_invite
        or domain
    )[:160]
    dedup_key = (
        handle
        or telegram
        or domain
        or discord_invite
        or re.sub(r"[^a-z0-9]+", "-", clean_brand.casefold()).strip("-")
        or source_url
    ).casefold()
    icp_score = 92 if (pool_usd is not None and pool_usd >= 10000) else 85

    status_label = "Active" if campaign_status == "active" else "Recently finished"
    pool_label = f" (${pool_usd:,} pool)" if pool_usd is not None else ""
    clean_snip = " ".join(html_mod.unescape(str(snippet or "")).split())
    clean_snip = re.sub(r"https?://\S+", "", clean_snip).strip(" -–—|·")
    summary = f"{status_label} Whop Content Rewards campaign{pool_label}"
    if clean_snip and clean_snip.casefold() not in {
        clean_brand.casefold(), "owner", "website", "discord", "x", "telegram"
    }:
        summary = f"{summary} — {clean_snip}"
    summary = summary[:240]

    links: list[str] = []
    seen_links: set[str] = set()
    for candidate_link in (
        project_url,
        x_url,
        tg_url,
        discord_url,
        founder_linkedin,
        instagram_url,
        source_url if source_url and source_url != DISCOVER_URL else "",
    ):
        u = str(candidate_link or "").strip()
        if u and u.casefold() not in seen_links:
            seen_links.add(u.casefold())
            links.append(u)

    row: dict[str, Any] = {
        "id": hashlib.sha256(f"whop:{dedup_key}".encode()).hexdigest()[:24],
        "name": display_name,
        "username": telegram,
        "telegram_username": telegram,
        "bio": summary,
        "sample": clean_snip[:300],
        "links": links,
        "source": "whop_content_rewards",
        "source_url": source_url or DISCOVER_URL,
        "group": "whop_content_rewards",
        "segment": "whale",
        "pool_usd": pool_usd,
        "campaign_status": campaign_status,
        "icp_score": icp_score,
        "icp_match": True,
        "has_monetization_signal": True,
        "monetization": "paid_product",
        "lead_tier": "A",
        "role": "Founder",
        "roles": ["Founder"],
        "category": "SaaS / AI app",
        "contact_url": tg_url or x_url or project_url or discord_url or source_url,
        "project": clean_brand,
        "company": clean_brand,
        "project_url": project_url,
        "website": project_url,
        "domain": domain,
        "what_they_do": summary[:200],
        "founder_x": x_url,
        "founder_twitter": x_url,
        "founder_linkedin": founder_linkedin,
        "instagram_url": instagram_url,
        "work_email": work_email,
        "email": work_email,
        "discord_url": discord_url,
        "discord_invite": discord_invite,
        "discord_owner": discord_owner,
        "social_verified": social,
        "social_contacts": social,
        "telegram_candidates": [],
    }
    if telegram:
        row["telegram_verified"] = [{"username": telegram, "url": tg_url}]
    return row


class _WhopHTMLParser(HTMLParser):
    """Extract Next.js JSON payloads, candidate campaign cards, headings, links, and visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.next_data_blobs: list[str] = []
        self.ld_json_blobs: list[str] = []
        self.cards: list[dict[str, Any]] = []
        self.all_links: list[str] = []
        self.all_headings: list[str] = []
        self.all_text_parts: list[str] = []

        self._skip_depth = 0
        self._capture_script: str | None = None
        self._script_buf: list[str] = []
        self._heading_stack_depths: list[int] = []
        self._heading_buf: list[str] = []
        self._anchor_href = ""
        self._anchor_buf: list[str] = []
        self._in_anchor = False

        self._card_depth = 0
        self._card_tag = ""
        self._cur_card: dict[str, Any] | None = None
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "script":
            sid = attr_map.get("id", "")
            stype = attr_map.get("type", "").lower()
            if sid == "__NEXT_DATA__":
                self._capture_script = "next"
                self._script_buf = []
            elif "ld+json" in stype:
                self._capture_script = "ld"
                self._script_buf = []
            else:
                self._skip_depth += 1
            return
        if t in {"style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if self._skip_depth > 0 or t in _VOID_HTML_TAGS:
            return

        self._tag_stack.append(t)
        cls = attr_map.get("class", "")
        el_id = attr_map.get("id", "")
        marker_blob = f"{cls} {el_id} {attr_map.get('data-testid', '')} {attr_map.get('data-test', '')}"
        has_campaign_data_attr = any(
            k.startswith(("data-campaign", "data-bounty", "data-reward", "data-pool"))
            for k in attr_map
        )
        is_card_start = (
            t in {"article", "tr"}
            or (t == "li" and "nav" not in marker_blob.lower() and "menu" not in marker_blob.lower())
            or (
                t in {"div", "section", "a"}
                and (has_campaign_data_attr or _CARD_MARKER_RE.search(marker_blob))
                and not _CONTAINER_MARKER_RE.search(marker_blob)
            )
        )
        if self._card_depth == 0 and is_card_start:
            self._card_depth = 1
            self._card_tag = t
            self._cur_card = {"headings": [], "links": [], "whop_anchors": [], "text_parts": []}
        elif self._card_depth > 0:
            self._card_depth += 1

        if t in {"h1", "h2", "h3", "h4"} or _HEADING_CLASS_RE.search(marker_blob):
            if not self._heading_stack_depths:
                self._heading_buf = []
            self._heading_stack_depths.append(len(self._tag_stack))

        if t == "a":
            href = attr_map.get("href", "").strip()
            if href:
                self.all_links.append(href)
                if self._cur_card is not None:
                    self._cur_card["links"].append(href)
                self._anchor_href = href
                self._anchor_buf = []
                self._in_anchor = True

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "script" and self._capture_script is not None:
            raw = "".join(self._script_buf).strip()
            if raw:
                if self._capture_script == "next":
                    self.next_data_blobs.append(raw)
                else:
                    self.ld_json_blobs.append(raw)
            self._capture_script = None
            self._script_buf = []
            return
        if t in {"script", "style", "noscript", "svg"} and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0 or t in _VOID_HTML_TAGS:
            return

        if t == "a" and self._in_anchor:
            anchor_text = " ".join("".join(self._anchor_buf).split()).strip()
            if anchor_text and ("whop.com" in self._anchor_href.lower() or "contentrewards.com" in self._anchor_href.lower()):
                if self._cur_card is not None:
                    self._cur_card["whop_anchors"].append(anchor_text)
                else:
                    self.all_headings.append(anchor_text)
            self._in_anchor = False
            self._anchor_href = ""
            self._anchor_buf = []

        cur_depth = len(self._tag_stack)
        if self._heading_stack_depths and cur_depth == self._heading_stack_depths[-1]:
            self._heading_stack_depths.pop()
            if not self._heading_stack_depths:
                heading_txt = " ".join("".join(self._heading_buf).split()).strip()
                if heading_txt:
                    self.all_headings.append(heading_txt)
                    if self._cur_card is not None:
                        self._cur_card["headings"].append(heading_txt)
                self._heading_buf = []

        if self._tag_stack:
            self._tag_stack.pop()
        if self._card_depth > 0:
            self._card_depth -= 1
            if self._card_depth == 0 and self._cur_card is not None:
                self.cards.append(self._cur_card)
                self._cur_card = None
                self._card_tag = ""

    def handle_data(self, data: str) -> None:
        if self._capture_script is not None:
            self._script_buf.append(data)
            return
        if self._skip_depth > 0:
            return
        txt = data.strip()
        if not txt:
            return
        self.all_text_parts.append(txt)
        if self._cur_card is not None:
            self._cur_card["text_parts"].append(txt)
        if self._heading_stack_depths:
            self._heading_buf.append(txt)
        if self._in_anchor:
            self._anchor_buf.append(txt)


def _walk_json_campaign_dicts(node: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_json_campaign_dicts(item, out)
        return
    if not isinstance(node, dict):
        return
    has_title = any(bool(str(node.get(k) or "").strip()) for k in ("title", "name", "brand", "company", "project"))
    has_campaign_signal = any(
        k in node and node.get(k) not in (None, "")
        for k in (
            "pool_usd", "budget", "total_budget", "reward_pool", "total_pool",
            "prize_pool", "bounty", "cpm", "campaign_status", "paid_out",
            "discord_invite", "discord_url", "whop_url", "campaign_url",
        )
    )
    if has_title and has_campaign_signal:
        out.append(node)
        return
    for val in node.values():
        if isinstance(val, (dict, list)):
            _walk_json_campaign_dicts(val, out)


def _is_banned_or_supply(text: str, *, title: str = "") -> bool:
    blob = f"{title} {text}".strip()
    if not blob:
        return False
    return bool(_BANNED_GAMING_RE.search(blob) or _SUPPLY_SPAM_RE.search(blob))


def _rows_from_campaign_unit(
    *,
    text: str,
    links: Iterable[str] = (),
    headings: Iterable[str] = (),
    item: Mapping[str, Any] | None = None,
    default_source_url: str = DISCOVER_URL,
    min_pool_usd: int | None = None,
    allow_bare_owner: bool = True,
) -> list[dict[str, Any]]:
    if _is_banned_or_supply(text, title=" ".join(headings)):
        return []

    raw_pool: int | None = None
    if isinstance(item, Mapping):
        for pkey in ("pool_usd", "total_budget", "budget", "reward_pool", "total_pool", "prize_pool", "bounty"):
            val = item.get(pkey)
            if isinstance(val, (int, float)) and val >= 0:
                raw_pool = int(round(val))
                break
            if isinstance(val, str) and val.strip():
                parsed_p = parse_pool_usd(val)
                if parsed_p is not None:
                    raw_pool = parsed_p
                    break
    if raw_pool is None:
        raw_pool = parse_pool_usd(text)

    if min_pool_usd is not None and raw_pool is not None and raw_pool < min_pool_usd:
        return []

    status = detect_campaign_status(text, item=item)
    extracted = _extract_contacts_from_blob(text, links=links, item=item)
    eff_source_url = extracted["source_whop_url"] or default_source_url

    brand = ""
    owner_name = ""
    if isinstance(item, Mapping):
        for bkey in ("project", "company", "brand", "title", "name"):
            cand = _clean_brand_name(str(item.get(bkey) or ""))
            if cand:
                brand = cand
                break
        owner_name = str(item.get("owner_name") or item.get("founder_name") or "").strip()
    if not brand:
        for h in headings:
            cand = _clean_brand_name(h)
            if cand:
                brand = cand
                break

    x_handles: list[str] = extracted["x_handles"]
    tg_handles: list[str] = extracted["tg_handles"]
    has_direct_social = bool(x_handles or tg_handles or extracted["work_email"] or extracted["founder_linkedin"])
    has_brand_or_discord = bool(extracted["project_url"] or extracted["discord_url"] or extracted["discord_owner"])
    has_budget_signal = raw_pool is not None or bool(_CAMPAIGN_BUDGET_SIGNAL_RE.search(f"{text} {eff_source_url}"))

    if not has_direct_social and not (has_brand_or_discord and has_budget_signal and (brand or extracted["domain"] or extracted["discord_invite"])):
        return []
    if not allow_bare_owner and not has_budget_signal and not has_direct_social:
        return []

    # Merge primary X and primary TG when unit represents a single brand/campaign/item;
    # for flat contact-only HTML without brand/domain/pool/item, preserve 1-row-per-handle compatibility.
    if brand or extracted["project_url"] or extracted["discord_url"] or raw_pool is not None or item is not None:
        primary_x = x_handles[0] if x_handles else ""
        primary_tg = tg_handles[0] if tg_handles else ""
        rows = [
            _build_whop_row(
                handle=primary_x,
                telegram=primary_tg,
                brand=brand,
                owner_name=owner_name,
                source_url=eff_source_url,
                project_url=extracted["project_url"],
                domain=extracted["domain"],
                discord_url=extracted["discord_url"],
                discord_invite=extracted["discord_invite"],
                discord_owner=extracted["discord_owner"],
                founder_linkedin=extracted["founder_linkedin"],
                instagram_url=extracted["instagram_url"],
                work_email=extracted["work_email"],
                pool_usd=raw_pool,
                campaign_status=status,
                snippet=text,
            )
        ]
        extra_handles: list[tuple[str, str]] = [(hx, "") for hx in x_handles[1:]] + [
            ("", htg) for htg in tg_handles[1:]
        ]
        for ex_x, ex_tg in extra_handles:
            rows.append(
                _build_whop_row(
                    handle=ex_x,
                    telegram=ex_tg,
                    brand=brand,
                    source_url=eff_source_url,
                    project_url=extracted["project_url"],
                    domain=extracted["domain"],
                    pool_usd=raw_pool,
                    campaign_status=status,
                    snippet=text,
                )
            )
        return rows

    # Flat contact list without a shared brand/campaign wrapper (preserve exact 1-row-per-handle semantics)
    found: dict[str, dict[str, str]] = {}
    for handle in x_handles:
        slot = found.setdefault(handle.casefold(), {"handle": handle, "tg": ""})
        slot["handle"] = handle
    for nick in tg_handles:
        slot = found.setdefault(nick.casefold(), {"handle": "", "tg": nick})
        slot["tg"] = nick
    return [
        _build_whop_row(
            handle=slot["handle"],
            telegram=slot["tg"],
            source_url=eff_source_url,
            founder_linkedin=extracted["founder_linkedin"],
            instagram_url=extracted["instagram_url"],
            work_email=extracted["work_email"],
            pool_usd=raw_pool,
            campaign_status=status,
            snippet=text,
        )
        for slot in found.values()
        if slot["handle"] or slot["tg"]
    ]


def _rows_from_json_payload(
    parsed_obj: Any,
    *,
    source_url: str,
    min_pool_usd: int | None,
) -> list[dict[str, Any]]:
    json_items: list[dict[str, Any]] = []
    _walk_json_campaign_dicts(parsed_obj, json_items)
    extracted: list[dict[str, Any]] = []
    for item in json_items:
        item_text = " ".join(
            str(item.get(k) or "")
            for k in ("title", "name", "brand", "company", "description", "summary", "snippet", "status")
        )
        extracted.extend(
            _rows_from_campaign_unit(
                text=item_text,
                item=item,
                default_source_url=source_url,
                min_pool_usd=min_pool_usd,
            )
        )
    return extracted


def _extract_campaign_rows(
    html_or_json: str,
    *,
    source_url: str = DISCOVER_URL,
    min_pool_usd: int | None = None,
) -> list[dict[str, Any]]:
    raw = str(html_or_json or "").strip()
    if not raw:
        return []

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _append_rows(cands: Iterable[dict[str, Any]]) -> None:
        for r in cands:
            rid = str(r.get("id") or "")
            if rid and rid not in seen_ids:
                seen_ids.add(rid)
                rows.append(r)

    if raw.startswith(("{", "[")):
        try:
            parsed_json = json.loads(raw)
        except json.JSONDecodeError:
            parsed_json = None
        if parsed_json is not None:
            _append_rows(_rows_from_json_payload(parsed_json, source_url=source_url, min_pool_usd=min_pool_usd))
            return rows

    parser = _WhopHTMLParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass

    for blob in parser.next_data_blobs + parser.ld_json_blobs:
        try:
            parsed_blob = json.loads(blob)
        except json.JSONDecodeError:
            continue
        _append_rows(_rows_from_json_payload(parsed_blob, source_url=source_url, min_pool_usd=min_pool_usd))

    usable_cards: list[dict[str, Any]] = []
    for card in parser.cards:
        c_text = " ".join(card["text_parts"])
        if card["links"] or parse_pool_usd(c_text) is not None or _CAMPAIGN_BUDGET_SIGNAL_RE.search(c_text):
            usable_cards.append(card)

    if len(usable_cards) >= 2:
        for card in usable_cards:
            card_text = " ".join(card["text_parts"])
            headings = list(card["headings"]) + list(card["whop_anchors"])
            _append_rows(
                _rows_from_campaign_unit(
                    text=card_text,
                    links=card["links"],
                    headings=headings,
                    default_source_url=source_url,
                    min_pool_usd=min_pool_usd,
                )
            )
        return rows

    if rows and not parser.all_links:
        return rows

    full_text = " ".join(parser.all_text_parts) if parser.all_text_parts else raw
    _append_rows(
        _rows_from_campaign_unit(
            text=full_text,
            links=parser.all_links,
            headings=parser.all_headings,
            default_source_url=source_url,
            min_pool_usd=min_pool_usd,
        )
    )
    return rows


def extract_whop_campaigns(
    html_or_json: str,
    *,
    source_url: str = DISCOVER_URL,
    min_pool_usd: int | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Parse active and finished Whop / Content Rewards campaigns filtered by `whop_min_pool_usd`."""
    eff_min = resolve_whop_min_pool_usd(cfg, min_pool_usd=min_pool_usd)
    return _extract_campaign_rows(html_or_json, source_url=source_url, min_pool_usd=eff_min)


def owner_contacts(
    html: str,
    *,
    source_url: str = DISCOVER_URL,
    min_pool_usd: int | None = None,
) -> list[dict[str, Any]]:
    """Owners with a profile link, brand domain, or Discord invite. Status URLs are clipper posts."""
    return _extract_campaign_rows(html or "", source_url=source_url, min_pool_usd=min_pool_usd)


def _curl(url: str) -> str:
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", "25", "-A", "Mozilla/5.0", url],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise OSError("fetch failed")
    return proc.stdout


def _unwrap_search_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "items", "organic_results"):
        val = payload.get(key)
        if isinstance(val, list) and val:
            return [item for item in val if isinstance(item, dict)]
    for sub in ("output", "result", "data"):
        child = payload.get(sub)
        if isinstance(child, list) and child:
            return [item for item in child if isinstance(item, dict)]
        if isinstance(child, dict):
            nested = _unwrap_search_results(child)
            if nested:
                return nested
    return []


def _rows_from_search_item(item: Mapping[str, Any], *, min_pool_usd: int) -> list[dict[str, Any]]:
    item_url = str(item.get("url") or "").strip()
    if item_url and _is_whop_noise_url(item_url):
        return []
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("snippet") or item.get("description") or item.get("text") or "").strip()
    extra_links = [str(u).strip() for u in (item.get("links") or []) if str(u or "").strip()]
    if item_url:
        extra_links.insert(0, item_url)
    text_blob = " ".join(part for part in (title, snippet) if part)
    return _rows_from_campaign_unit(
        text=text_blob,
        links=extra_links,
        headings=[title] if title else (),
        item=item,
        default_source_url=item_url or DISCOVER_URL,
        min_pool_usd=min_pool_usd,
    )


def _whop_treg_search_rows(
    *,
    seen_path: Path | None = None,
    seen_ids: set[str] | None = None,
    min_pool_usd: int | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    try:
        from leadgen.treg_client import call as treg_call, check_treg_ready
    except ImportError:
        try:
            from treg_client import call as treg_call, check_treg_ready
        except ImportError:
            return []
    if not check_treg_ready():
        return []

    eff_min_pool = resolve_whop_min_pool_usd(cfg, min_pool_usd=min_pool_usd)
    st_path = seen_path if seen_path is not None else _resolve_buyer_public_seen_path()
    saved_idx, saved_list = _load_buyer_public_state(st_path)
    already_seen = set(saved_list)
    if seen_ids is not None:
        already_seen.update(seen_ids)

    n_q = len(_WHOP_SEARCH_QUERIES)
    start_idx = saved_idx % n_q
    ordered_queries = [_WHOP_SEARCH_QUERIES[(start_idx + i) % n_q] for i in range(n_q)]
    collected: list[dict[str, Any]] = []
    batch_seen: set[str] = set()
    queries_tried = 0
    for query in ordered_queries[:3]:
        queries_tried += 1
        payload = treg_call(
            "crawl4ai.web.search",
            query={"q": query},
            timeout=25,
        )
        if not isinstance(payload, dict):
            continue
        results = _unwrap_search_results(payload)
        batch_rows: list[dict[str, Any]] = []
        for item in results:
            for row in _rows_from_search_item(item, min_pool_usd=eff_min_pool):
                rid = str(row.get("id") or "")
                if rid and rid not in already_seen and rid not in batch_seen:
                    batch_seen.add(rid)
                    batch_rows.append(row)
        if batch_rows:
            collected.extend(batch_rows)
            break
    _save_buyer_public_state(st_path, (start_idx + max(1, queries_tried)) % n_q, saved_list)
    return collected


def _enrich_rows_with_discord(
    rows: list[dict[str, Any]],
    *,
    discord_fetch_fn: Callable[[str], Any] | None = None,
) -> list[dict[str, Any]]:
    for row in rows:
        invite = str(row.get("discord_invite") or "").strip()
        if not invite:
            continue
        meta = resolve_discord_invite(invite, fetch_json_fn=discord_fetch_fn)
        if not meta:
            continue
        if meta.get("discord_owner") and not row.get("discord_owner"):
            row["discord_owner"] = meta["discord_owner"]
        if meta.get("project_url") and not row.get("project_url"):
            row["project_url"] = meta["project_url"]
            row["website"] = meta["project_url"]
            row["domain"] = meta.get("domain") or ""
            if meta["project_url"] not in row["links"]:
                row["links"].insert(0, meta["project_url"])
        if meta.get("guild_name") and not row.get("project"):
            clean_g = _clean_brand_name(meta["guild_name"])
            if clean_g:
                row["project"] = clean_g
                row["company"] = clean_g
        if meta.get("x_handle") and not row.get("founder_x"):
            x_u = f"https://x.com/{meta['x_handle']}"
            row["founder_x"] = x_u
            row["founder_twitter"] = x_u
            row["social_verified"].append({"platform": "x", "url": x_u, "handle": meta["x_handle"]})
            if x_u not in row["links"]:
                row["links"].append(x_u)
        if meta.get("tg_handle") and not row.get("username"):
            tg_h = meta["tg_handle"]
            tg_u = f"https://t.me/{tg_h}"
            row["username"] = tg_h
            row["telegram_username"] = tg_h
            row["telegram_verified"] = [{"username": tg_h, "url": tg_u}]
            if tg_u not in row["links"]:
                row["links"].append(tg_u)
    return rows


def whop_rows(
    fetch_fn: Callable[[str], str] | None = None,
    *,
    min_pool_usd: int | None = None,
    cfg: Mapping[str, Any] | None = None,
    discover_urls: Iterable[str] | None = None,
    resolve_discord: bool = False,
    discord_fetch_fn: Callable[[str], Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect active and recently finished Whop / Content Rewards whale buyer leads."""
    eff_min_pool = resolve_whop_min_pool_usd(cfg, min_pool_usd=min_pool_usd)
    fetch = fetch_fn or _curl
    urls = tuple(discover_urls) if discover_urls is not None else (DISCOVER_URL,)
    rows: list[dict[str, Any]] = []
    for url in urls:
        html = ""
        try:
            html = fetch(url)
        except Exception:
            html = ""
        if html:
            rows.extend(_extract_campaign_rows(html, source_url=url, min_pool_usd=eff_min_pool))

    if resolve_discord or discord_fetch_fn is not None:
        _enrich_rows_with_discord(rows, discord_fetch_fn=discord_fetch_fn)

    if fetch_fn is not None:
        return rows

    seen_path = _resolve_buyer_public_seen_path()
    saved_idx, saved_list = _load_buyer_public_state(seen_path)
    already_seen = set(saved_list)

    fresh_rows: list[dict[str, Any]] = []
    batch_ids: set[str] = set()
    for r in rows:
        rid = str(r.get("id") or "")
        if rid and rid not in already_seen and rid not in batch_ids:
            batch_ids.add(rid)
            fresh_rows.append(r)

    treg_rows = _whop_treg_search_rows(
        seen_path=seen_path,
        seen_ids=(already_seen | batch_ids),
        min_pool_usd=eff_min_pool,
        cfg=cfg,
    )
    if resolve_discord or discord_fetch_fn is not None:
        _enrich_rows_with_discord(treg_rows, discord_fetch_fn=discord_fetch_fn)

    next_idx, latest_saved = _load_buyer_public_state(seen_path)
    if not treg_rows:
        next_idx = (saved_idx + 1) % len(_WHOP_SEARCH_QUERIES)
    for r in treg_rows:
        rid = str(r.get("id") or "")
        if rid and rid not in already_seen and rid not in batch_ids:
            batch_ids.add(rid)
            fresh_rows.append(r)

    updated_seen = list(latest_saved)
    for r in fresh_rows:
        rid = str(r.get("id") or "")
        if rid and rid not in updated_seen:
            updated_seen.append(rid)
    _save_buyer_public_state(seen_path, next_idx, updated_seen)

    if not fresh_rows:
        print("whop: skipped (no fresh owner or verified brand campaign)", flush=True)
    return fresh_rows


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
    parser.add_argument("--min-pool-usd", type=int, default=None)
    args = parser.parse_args()
    rows: list[dict] = []
    try:
        rows.extend(whop_rows(min_pool_usd=args.min_pool_usd))
    except Exception as exc:
        print(f"whop_rows: skipped ({exc.__class__.__name__})", flush=True)
    for loader in (bloss_rows, propinfa_rows):
        try:
            rows.extend(loader())
        except Exception as exc:
            print(f"{loader.__name__}: skipped ({exc.__class__.__name__})", flush=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
