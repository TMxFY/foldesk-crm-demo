#!/usr/bin/env python3
"""Web/HTTP-first Telegram resolution & channel preview client (`$0` first, paid fallback).

Provides:
- `probe_tme_handle_web`: classifies `https://t.me/<handle>` as
  ``user`` | ``channel`` | ``bot`` | ``missing`` | ``unknown`` using a 5s
  no-redirect GET backed by a thread-safe 7-day atomic JSON cache
  (`tme_probe_cache.json` under `LEADGEN_STATE_DIR`).
- `fetch_channels_info_batch`: batches up to 20 usernames per
  `tikhub.x.telegram-web-batch-channel-info` (`~$0.008/call`) with budget guard
  and `$0` `https://t.me/s/<ch>` HTTP fallback.
- `fetch_channel_posts_web`: free `https://t.me/s/<ch>` (`5s`) ->
  `tikhub.x.telegram-web-fetch-channel-posts` (`$0.001`) ->
  `crawl4ai.telegram.channel.posts` (`$0.0009`).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import html as html_mod
import inspect
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

try:
    from leadgen.treg_client import call as treg_call, check_treg_ready
except ImportError:
    try:
        from treg_client import call as treg_call, check_treg_ready  # type: ignore[no-redef]
    except ImportError:
        treg_call = None  # type: ignore[assignment]
        check_treg_ready = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

TME_CACHE_TTL_SEC = 7 * 86400
COST_TIKHUB_BATCH_CHANNEL_INFO_USD = 0.008
COST_TIKHUB_CHANNEL_POSTS_USD = 0.001
COST_CRAWL4AI_CHANNEL_POSTS_USD = 0.0009

_BATCH_SIZE = 20
_CACHE_LOCK = threading.RLock()
_ORIG_REQUESTS_GET = requests.get

_VALID_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")
_NOISE_HANDLES = frozenset(
    {
        "joinchat",
        "addstickers",
        "addemoji",
        "addlist",
        "addtheme",
        "share",
        "proxy",
        "socks",
        "login",
        "contact",
        "boost",
        "s",
        "c",
        "iv",
    }
)
_SUBS_COUNT_RE = re.compile(
    r"(\d[\d\s\u00a0\u202f.,]*)\s*(?:(k|m|b|к|м|тыс\.?|млн\.?)(?=$|[^A-Za-zА-Яа-яЁё]))?",
    re.IGNORECASE,
)
_CHANNEL_EXTRA_MARKERS = (
    "subscriber",
    "subscribers",
    "подписчик",
    "member",
    "members",
    "участник",
    "online",
    "онлайн",
)
_MISSING_TEXT_MARKERS = (
    "username is not found",
    "page not found",
    "this page doesn't exist",
    "account does not exist",
    "user not found",
)
_CAPTCHA_ANTIBOT_RE = re.compile(
    r"(?:cf-challenge|cloudflare|verify you are human|captcha|g-recaptcha|hcaptcha|turnstile)",
    re.IGNORECASE,
)


def _normalize_handle(raw: str) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    if "t.me/" in txt.lower() or "telegram.me/" in txt.lower() or "telegram.dog/" in txt.lower():
        txt = re.sub(
            r"^https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?",
            "",
            txt,
            flags=re.IGNORECASE,
        )
        txt = txt.split("/", 1)[0]
    txt = txt.split("?", 1)[0].split("#", 1)[0].strip().lstrip("@")
    if not txt or txt.lower() in _NOISE_HANDLES:
        return ""
    if not _VALID_HANDLE_RE.fullmatch(txt):
        return ""
    return txt


def _default_state_dir() -> Path:
    custom = (
        os.environ.get("LEADGEN_STATE_DIR")
        or os.environ.get("CRMTG_LEADGEN_STATE_DIR")
        or ""
    ).strip()
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent.parent / "data" / "leadgen_state"


def _resolve_cache_path(cache_path: Path | str | None) -> Path:
    if cache_path is not None:
        return Path(cache_path)
    return _default_state_dir() / "tme_probe_cache.json"


def _read_cache_entries(path: Path) -> dict[str, dict[str, Any]]:
    with _CACHE_LOCK:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        entries = raw.get("entries") if isinstance(raw.get("entries"), dict) else raw
        out: dict[str, dict[str, Any]] = {}
        for k, v in entries.items():
            if isinstance(k, str) and isinstance(v, dict):
                out[k.lower()] = dict(v)
        return out


def _write_cache_entry(path: Path, key: str, payload: dict[str, Any], now_ts: float) -> None:
    with _CACHE_LOCK:
        entries = _read_cache_entries(path)
        fresh_entries: dict[str, dict[str, Any]] = {}
        for ek, ev in entries.items():
            try:
                ts_val = float(ev.get("ts") or ev.get("cached_at") or ev.get("fetched_at") or 0.0)
            except (TypeError, ValueError):
                ts_val = 0.0
            if now_ts - ts_val < TME_CACHE_TTL_SEC:
                fresh_entries[ek] = ev
        fresh_entries[key.lower()] = {
            "handle": str(payload.get("handle") or key),
            "kind": str(payload.get("kind") or "unknown"),
            "title": str(payload.get("title") or ""),
            "bio": str(payload.get("bio") or ""),
            "subscribers": int(payload.get("subscribers") or 0),
            "ts": float(now_ts),
        }
        try:
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
                json.dump(fresh_entries, tmp_fh, ensure_ascii=False, indent=2)
            os.replace(tmp_name, path)
        except OSError as exc:
            logger.debug("Failed to persist tme_probe_cache %s: %s", path, exc)


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


def _parse_subscriber_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    raw = str(value).strip()
    if not raw:
        return None
    m = _SUBS_COUNT_RE.search(raw)
    if not m:
        return None
    num_txt = m.group(1).replace("\u00a0", "").replace("\u202f", "").replace(" ", "").strip()
    suffix = (m.group(2) or "").strip().rstrip(".").casefold()
    mult = {
        "k": 1_000,
        "к": 1_000,
        "тыс": 1_000,
        "m": 1_000_000,
        "м": 1_000_000,
        "млн": 1_000_000,
        "b": 1_000_000_000,
    }.get(suffix, 1)
    if mult > 1:
        if "," in num_txt and "." not in num_txt:
            parts = num_txt.split(",")
            num_txt = num_txt.replace(",", ".") if (len(parts) == 2 and len(parts[1]) != 3) else num_txt.replace(",", "")
        else:
            num_txt = num_txt.replace(",", "")
    else:
        num_txt = re.sub(r"(?<=\d),(?=\d{3}\b)", "", num_txt)
        if "," in num_txt and "." not in num_txt:
            num_txt = num_txt.replace(",", ".")
    try:
        parsed = float(num_txt)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return int(round(parsed * mult))


def _node_text_with_links(node: Any) -> str:
    for a_tag in node.find_all("a", href=True):
        href = html_mod.unescape(str(a_tag.get("href") or "")).strip()
        label = a_tag.get_text(" ", strip=True)
        if href.startswith(("http://", "https://")) and href not in label:
            a_tag.replace_with(f"{label} {href} ")
    return " ".join(node.get_text(" ", strip=True).split())


def _extract_tme_page_fields(html_text: str) -> tuple[str, str, str, str, str]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        BeautifulSoup = None

    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_text, "html.parser")
        title_node = soup.select_one(".tgme_page_title") or soup.select_one(".tgme_channel_info_header_title")
        extra_node = soup.select_one(".tgme_page_extra") or soup.select_one(".tgme_channel_info_counter")
        desc_node = soup.select_one(".tgme_page_description") or soup.select_one(".tgme_channel_info_description")
        action_node = soup.select_one(".tgme_action_button_new, .tgme_page_action")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        extra = extra_node.get_text(" ", strip=True) if extra_node else ""
        bio = _node_text_with_links(desc_node) if desc_node else ""
        action = action_node.get_text(" ", strip=True) if action_node else ""
        full_text = soup.get_text(" ", strip=True)
        return title, extra, bio, action, full_text

    def _rx(cls_name: str) -> str:
        m = re.search(rf'class="[^"]*\b{cls_name}\b[^"]*"[^>]*>(.*?)</div>', html_text, re.I | re.S)
        return " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split()) if m else ""

    title = _rx("tgme_page_title") or _rx("tgme_channel_info_header_title")
    extra = _rx("tgme_page_extra") or _rx("tgme_channel_info_counter")
    bio = _rx("tgme_page_description") or _rx("tgme_channel_info_description")
    action = _rx("tgme_action_button_new") or _rx("tgme_page_action")
    full_text = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", html_text)).split())
    return title, extra, bio, action, full_text


def _make_probe_result(
    handle: str,
    kind: str,
    *,
    title: str = "",
    bio: str = "",
    subscribers: int = 0,
    cached: bool = False,
) -> dict[str, Any]:
    return {
        "handle": handle,
        "kind": kind,
        "title": title,
        "bio": bio,
        "subscribers": int(subscribers or 0),
        "cached": bool(cached),
    }


def _http_fetch_url(
    url: str,
    *,
    http_get_fn: Callable[..., Any] | None,
    timeout: float,
    allow_redirects: bool,
) -> tuple[int, str, bool, str]:
    """Return ``(status_code, html_text, is_redirect, location)``; status 0 on network error."""
    try:
        if http_get_fn is not None:
            resp = _call_with_supported_kwargs(
                http_get_fn,
                url,
                timeout=float(timeout),
                allow_redirects=allow_redirects,
            )
        else:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC/1.0)"},
                timeout=float(timeout),
                allow_redirects=allow_redirects,
            )
    except Exception as exc:
        logger.debug("HTTP GET failed for %s: %s", url, exc)
        return 0, "", False, ""

    if resp is None:
        return 0, "", False, ""
    if isinstance(resp, str):
        return (200 if resp.strip() else 0), resp, False, ""
    if isinstance(resp, Mapping):
        status = int(resp.get("status_code") or (200 if (resp.get("text") or resp.get("html")) else 0))
        headers = resp.get("headers") or {}
        loc = str(headers.get("Location") or headers.get("location") or "")
        is_redir = bool(resp.get("is_redirect")) or (300 <= status < 400) or bool(loc)
        body = str(resp.get("text") or resp.get("html") or resp.get("body") or "")
        return status, body, is_redir, loc

    status = int(getattr(resp, "status_code", 200) or 0)
    headers = getattr(resp, "headers", {}) or {}
    loc = str(headers.get("Location") or headers.get("location") or "")
    is_redir = bool(getattr(resp, "is_redirect", False)) or (300 <= status < 400) or bool(loc)
    body = str(getattr(resp, "text", "") or "")
    return status, body, is_redir, loc


def probe_tme_handle_web(
    handle: str,
    *,
    http_get_fn: Callable[..., Any] | None = None,
    timeout: float = 5.0,
    cache_path: Path | str | None = None,
) -> dict[str, Any]:
    """Classify `https://t.me/<handle>` as user/channel/bot/missing/unknown with 7d JSON cache."""
    clean = _normalize_handle(handle)
    if not clean:
        return _make_probe_result("", "missing")

    cache_enabled = (
        cache_path is not None
        or bool((os.environ.get("LEADGEN_STATE_DIR") or "").strip())
        or http_get_fn is None
    )
    resolved_cache_path = _resolve_cache_path(cache_path)
    now_ts = time.time()

    if cache_enabled:
        entries = _read_cache_entries(resolved_cache_path)
        entry = entries.get(clean.lower())
        if isinstance(entry, dict) and entry.get("kind"):
            try:
                ts_val = float(entry.get("ts") or entry.get("cached_at") or entry.get("fetched_at") or 0.0)
            except (TypeError, ValueError):
                ts_val = 0.0
            if now_ts - ts_val < TME_CACHE_TTL_SEC:
                return _make_probe_result(
                    str(entry.get("handle") or clean),
                    str(entry.get("kind") or "unknown"),
                    title=str(entry.get("title") or ""),
                    bio=str(entry.get("bio") or ""),
                    subscribers=int(entry.get("subscribers") or 0),
                    cached=True,
                )

    status, html_text, is_redir, loc = _http_fetch_url(
        f"https://t.me/{clean}",
        http_get_fn=http_get_fn,
        timeout=float(timeout or 5.0),
        allow_redirects=False,
    )

    result: dict[str, Any]
    if status in (404, 410):
        result = _make_probe_result(clean, "missing")
    elif is_redir:
        kind = "missing" if ("telegram.org" in loc.lower() or 300 <= status < 400) else "unknown"
        result = _make_probe_result(clean, kind)
    elif status == 0 or status >= 400 or not html_text.strip():
        result = _make_probe_result(clean, "unknown")
    elif _CAPTCHA_ANTIBOT_RE.search(html_text):
        result = _make_probe_result(clean, "unknown")
    else:
        title, extra, bio, action, full_text = _extract_tme_page_fields(html_text)
        extra_low = extra.lower()
        action_low = action.lower()
        full_low = full_text.lower()
        has_channel_header = "tgme_channel_info_header_title" in html_text
        has_page_title = "tgme_page_title" in html_text or has_channel_header
        has_page_extra = "tgme_page_extra" in html_text or "tgme_channel_info_counter" in html_text

        if any(tok in full_low for tok in _MISSING_TEXT_MARKERS):
            result = _make_probe_result(clean, "missing")
        elif not title and (
            "tgme_page" in html_text
            or "if you have telegram, you can contact" in full_low
            or "telegram: contact" in html_text.lower()
        ):
            result = _make_probe_result(clean, "missing")
        elif not title or not (has_page_title and has_page_extra):
            result = _make_probe_result(clean, "unknown")
        elif (
            any(tok in extra_low for tok in _CHANNEL_EXTRA_MARKERS)
            or has_channel_header
            or (("view in telegram" in action_low or "preview channel" in action_low) and "@" not in extra_low)
        ):
            subs = _parse_subscriber_count(extra) or 0
            result = _make_probe_result(clean, "channel", title=title, bio=bio, subscribers=subs)
        elif (
            clean.lower().endswith("bot")
            or "start bot" in action_low
            or "start bot" in full_low
            or "monthly user" in extra_low
            or re.search(r"\bbot\b", extra_low)
        ):
            result = _make_probe_result(clean, "bot", title=title, bio=bio)
        else:
            result = _make_probe_result(clean, "user", title=title, bio=bio)

    if cache_enabled and result["kind"] != "unknown":
        _write_cache_entry(resolved_cache_path, clean, result, now_ts)
    return result


def _parse_tme_s_channel_and_posts(html_text: str, limit: int) -> tuple[dict[str, Any], list[str]]:
    if not html_text or _CAPTCHA_ANTIBOT_RE.search(html_text):
        return {}, []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        BeautifulSoup = None

    info: dict[str, Any] = {}
    posts: list[str] = []

    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_text, "html.parser")
        title_node = soup.select_one(".tgme_channel_info_header_title") or soup.select_one(".tgme_page_title")
        if title_node is not None:
            t = title_node.get_text(" ", strip=True)
            if t:
                info["title"] = t
        desc_node = soup.select_one(".tgme_channel_info_description") or soup.select_one(".tgme_page_description")
        if desc_node is not None:
            about = _node_text_with_links(desc_node)
            if about:
                info["about"] = about
                info["bio"] = about
        for counter_node in soup.select(".tgme_channel_info_counter, .tgme_page_extra"):
            txt = counter_node.get_text(" ", strip=True)
            low = txt.lower()
            if "subscriber" in low or "подпис" in low or "member" in low or re.search(r"^\d", low):
                parsed_subs = _parse_subscriber_count(txt)
                if parsed_subs is not None:
                    info["subscribers"] = parsed_subs
                    break
        wraps = soup.select(".tgme_widget_message_wrap")
        if wraps:
            for wrap in wraps:
                node = wrap.select_one(".tgme_widget_message_text")
                txt = _node_text_with_links(node) if node is not None else ""
                extras: list[str] = []
                for btn in wrap.select("a.tgme_widget_message_inline_button[href], a.tgme_widget_message_link_preview[href]"):
                    href = html_mod.unescape(str(btn.get("href") or "")).strip()
                    if not href.startswith(("http://", "https://")):
                        continue
                    if re.match(r"^https?://(?:t\.me|telegram\.me)/[^/]+/\d+", href, re.I):
                        continue
                    if href not in txt and href not in extras:
                        extras.append(href)
                combined = " ".join([txt, *extras]).strip()
                if combined:
                    posts.append(combined)
        else:
            for node in soup.select(".tgme_widget_message_text"):
                txt = _node_text_with_links(node)
                if txt:
                    posts.append(txt)
    else:
        m_title = re.search(r'class="[^"]*\btgme_(?:channel_info_header_title|page_title)\b[^"]*"[^>]*>(.*?)</div>', html_text, re.I | re.S)
        if m_title:
            info["title"] = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", m_title.group(1))).split())
        m_desc = re.search(r'class="[^"]*\btgme_(?:channel_info_description|page_description)\b[^"]*"[^>]*>(.*?)</div>', html_text, re.I | re.S)
        if m_desc:
            about = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", m_desc.group(1))).split())
            if about:
                info["about"] = about
                info["bio"] = about
        for m in re.finditer(r'class="[^"]*\btgme_widget_message_text\b[^"]*"[^>]*>(.*?)</div>', html_text, re.I | re.S):
            inner = re.sub(
                r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
                lambda am: f"{re.sub(r'<[^>]+>', ' ', am.group(2)).strip()} {html_mod.unescape(am.group(1))} ",
                m.group(1),
                flags=re.I | re.S,
            )
            txt = " ".join(html_mod.unescape(re.sub(r"<[^>]+>", " ", inner)).split())
            if txt:
                posts.append(txt)

    if limit <= 0:
        return info, []
    return info, posts[-limit:]


def _check_budget(
    budget_guard_fn: Callable[..., Any] | None,
    provider: str,
    endpoint: str,
    estimated_cost: float,
    *,
    conn: Any = None,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    if budget_guard_fn is not None:
        try:
            sig = inspect.signature(budget_guard_fn)
            params = list(sig.parameters.values())
            accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            accepts_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
            names = {p.name for p in params}
            if accepts_var_kw:
                res = budget_guard_fn(provider, estimated_cost, endpoint=endpoint)
            elif {"provider", "estimated_cost", "endpoint"} <= names:
                res = budget_guard_fn(provider=provider, endpoint=endpoint, estimated_cost=estimated_cost)
            elif {"provider", "estimated_cost"} <= names:
                res = budget_guard_fn(provider=provider, estimated_cost=estimated_cost)
            elif accepts_var_pos or len(params) >= 2:
                res = budget_guard_fn(provider, estimated_cost)
            elif len(params) == 1:
                res = budget_guard_fn(provider)
            else:
                res = budget_guard_fn()
            if isinstance(res, tuple) and res:
                return bool(res[0])
            if isinstance(res, Mapping):
                if "paid_allowed" in res:
                    return bool(res["paid_allowed"])
                if "allowed" in res:
                    return bool(res["allowed"])
            return bool(res)
        except Exception as exc:
            logger.warning("tg_web_client budget_guard_fn failed (%s): %s", endpoint, exc)
            return False

    if conn is not None:
        try:
            from leadgen.budget_guard import check_and_reserve_budget

            allowed, _reason = check_and_reserve_budget(
                conn,
                provider=provider,
                estimated_cost=estimated_cost,
                cfg=cfg,
            )
            return bool(allowed)
        except ImportError:
            return True
        except Exception as exc:
            logger.warning("tg_web_client budget DB check failed (%s): %s", endpoint, exc)
            return False
    return True


def _record_cost(
    cost_record_fn: Callable[..., Any] | None,
    *,
    conn: Any,
    provider: str,
    endpoint: str,
    lead_key: str,
    cost_usd: float,
    hit: bool,
) -> None:
    if cost_record_fn is not None:
        try:
            _call_with_supported_kwargs(
                cost_record_fn,
                provider=provider,
                endpoint=endpoint,
                lead_key=lead_key,
                cost_usd=float(cost_usd),
                hit=bool(hit),
            )
        except Exception as exc:
            logger.debug("tg_web_client cost_record_fn failed (%s): %s", endpoint, exc)
        return
    if conn is not None and cost_usd > 0.0:
        try:
            from leadgen.budget_guard import record_enrichment_spend

            record_enrichment_spend(
                conn,
                provider=provider,
                endpoint=endpoint,
                lead_key=lead_key,
                cost_usd=float(cost_usd),
                hit=bool(hit),
            )
        except Exception as exc:
            logger.debug("tg_web_client budget spend record failed (%s): %s", endpoint, exc)


def _invoke_treg(
    treg_fn: Callable[..., Any] | None,
    endpoint: str,
    *,
    method: str | None = None,
    query: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    timeout: int = 20,
) -> Any:
    if treg_fn is not None:
        try:
            return _call_with_supported_kwargs(
                treg_fn,
                endpoint,
                method=method,
                query=query,
                data=data,
                timeout=timeout,
            )
        except Exception as exc:
            logger.debug("tg_web_client treg_fn failed (%s): %s", endpoint, exc)
            return None
    if treg_call is None or check_treg_ready is None or not check_treg_ready():
        return None
    return treg_call(endpoint, method=method, query=query, data=data, timeout=timeout)


def _normalize_channel_info_dict(raw: Mapping[str, Any], fallback_handle: str = "") -> dict[str, Any] | None:
    uname = _normalize_handle(
        str(raw.get("username") or raw.get("channel") or raw.get("handle") or fallback_handle)
    )
    if not uname:
        return None
    title = str(raw.get("title") or raw.get("name") or uname).strip()
    about = str(raw.get("description") or raw.get("about") or raw.get("bio") or "").strip()
    counters = raw.get("counters") if isinstance(raw.get("counters"), Mapping) else {}
    subs = None
    for cand in (
        raw.get("subscribers"),
        raw.get("subscribers_count"),
        raw.get("participants_count"),
        raw.get("members_count"),
        raw.get("members"),
        counters.get("subscribers"),
    ):
        subs = _parse_subscriber_count(cand)
        if subs is not None:
            break
    out: dict[str, Any] = {
        "username": uname,
        "title": title or uname,
        "about": about,
        "bio": about,
        "subscribers": subs if subs is not None else 0,
    }
    if raw.get("linked_chat_id") is not None:
        out["linked_chat_id"] = raw.get("linked_chat_id")
    return out


def _extract_batch_channel_map(payload: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not payload:
        return out

    items: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(payload, list):
        for elem in payload:
            if isinstance(elem, Mapping):
                items.append(("", elem))
    elif isinstance(payload, Mapping):
        containers = [payload]
        for key in ("output", "result", "data"):
            sub = payload.get(key)
            if isinstance(sub, Mapping):
                containers.append(sub)
            elif isinstance(sub, list):
                for elem in sub:
                    if isinstance(elem, Mapping):
                        items.append(("", elem))
        for cont in containers:
            for list_key in ("channels", "items", "results", "list"):
                val = cont.get(list_key)
                if isinstance(val, list):
                    for elem in val:
                        if isinstance(elem, Mapping):
                            items.append(("", elem))
                elif isinstance(val, Mapping):
                    for map_k, map_v in val.items():
                        if isinstance(map_v, Mapping):
                            items.append((str(map_k), map_v))
            if not items:
                for map_k, map_v in cont.items():
                    if map_k in ("code", "ok", "status", "message", "_treg", "meta"):
                        continue
                    if isinstance(map_v, Mapping) and any(
                        fld in map_v for fld in ("title", "name", "description", "about", "bio", "subscribers", "username")
                    ):
                        items.append((str(map_k), map_v))

    for hint_handle, raw_entry in items:
        norm = _normalize_channel_info_dict(raw_entry, fallback_handle=hint_handle)
        if norm:
            out[norm["username"].lower()] = norm
    return out


def _extract_posts_and_info(payload: Any, channel: str, limit: int) -> tuple[dict[str, Any], list[str]]:
    if not payload:
        return {}, []
    info: dict[str, Any] = {}
    candidates: list[Any] = []

    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, Mapping):
        containers: list[Mapping[str, Any]] = [payload]
        for key in ("output", "result", "data"):
            sub = payload.get(key)
            if isinstance(sub, Mapping):
                containers.append(sub)
        for cont in containers:
            if not info:
                ch_obj = cont.get("channel_info") if isinstance(cont.get("channel_info"), Mapping) else cont
                if isinstance(ch_obj, Mapping) and any(
                    k in ch_obj for k in ("title", "description", "about", "bio", "subscribers", "linked_chat_id")
                ):
                    norm = _normalize_channel_info_dict(ch_obj, fallback_handle=channel)
                    if norm:
                        info = norm
            if not candidates:
                for key in ("posts", "messages", "items", "results", "rows", "list"):
                    val = cont.get(key)
                    if isinstance(val, list):
                        candidates = val
                        break

    posts: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            txt = item.strip()
        elif isinstance(item, Mapping):
            txt = str(item.get("text") or item.get("message") or item.get("content") or "").strip()
        else:
            txt = ""
        if txt:
            posts.append(txt)
        if limit > 0 and len(posts) >= limit:
            break
    return info, posts


def fetch_channels_info_batch(
    handles: list[str],
    *,
    treg_fn: Callable[..., Any] | None = None,
    allow_paid: bool = True,
    budget_guard_fn: Callable[..., Any] | None = None,
    cost_record_fn: Callable[..., Any] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    conn: Any = None,
    cfg: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch channel info in chunks of 20 via TikHub batch endpoint with `$0` `t.me/s/` fallback."""
    clean_handles: list[str] = []
    seen: set[str] = set()
    for raw in handles or ():
        ch = _normalize_handle(raw)
        if ch and ch.lower() not in seen:
            seen.add(ch.lower())
            clean_handles.append(ch)
    if not clean_handles:
        return {}

    results: dict[str, dict[str, Any]] = {}
    treg_available = treg_fn is not None or (check_treg_ready is not None and check_treg_ready())

    if allow_paid and treg_available:
        for i in range(0, len(clean_handles), _BATCH_SIZE):
            batch = clean_handles[i : i + _BATCH_SIZE]
            if not _check_budget(
                budget_guard_fn,
                "treg",
                "tikhub.x.telegram-web-batch-channel-info",
                COST_TIKHUB_BATCH_CHANNEL_INFO_USD,
                conn=conn,
                cfg=cfg,
            ):
                break
            payload = _invoke_treg(
                treg_fn,
                "tikhub.x.telegram-web-batch-channel-info",
                query={"channels": ",".join(batch)},
                timeout=30,
            )
            parsed_map = _extract_batch_channel_map(payload)
            if payload is not None:
                _record_cost(
                    cost_record_fn,
                    conn=conn,
                    provider="treg",
                    endpoint="tikhub.x.telegram-web-batch-channel-info",
                    lead_key=f"batch:{batch[0]}:{len(batch)}",
                    cost_usd=COST_TIKHUB_BATCH_CHANNEL_INFO_USD,
                    hit=bool(parsed_map),
                )
            for k, v in parsed_map.items():
                v.setdefault("source", "tikhub_batch")
                results[k] = v

    for ch in clean_handles:
        key = ch.lower()
        if key in results:
            continue
        status, html_text, is_redir, _loc = _http_fetch_url(
            f"https://t.me/s/{ch}",
            http_get_fn=http_get_fn,
            timeout=5.0,
            allow_redirects=False,
        )
        if status == 200 and not is_redir and html_text:
            info, _ = _parse_tme_s_channel_and_posts(html_text, limit=0)
            if info:
                info["username"] = ch
                info.setdefault("title", ch)
                info.setdefault("about", "")
                info.setdefault("bio", info["about"])
                info.setdefault("subscribers", 0)
                info["source"] = "tme_s_http"
                results[key] = info

    return results


def fetch_channel_posts_web(
    channel: str,
    limit: int = 20,
    *,
    treg_fn: Callable[..., Any] | None = None,
    allow_paid: bool = True,
    budget_guard_fn: Callable[..., Any] | None = None,
    cost_record_fn: Callable[..., Any] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    conn: Any = None,
    cfg: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Fetch channel metadata + recent posts via free `t.me/s/` -> TikHub -> Crawl4AI."""
    ch = _normalize_handle(channel)
    if not ch:
        return {}, []

    merged_info: dict[str, Any] = {}
    status, html_text, is_redir, _loc = _http_fetch_url(
        f"https://t.me/s/{ch}",
        http_get_fn=http_get_fn,
        timeout=5.0,
        allow_redirects=False,
    )
    if status == 200 and not is_redir and html_text:
        web_info, web_posts = _parse_tme_s_channel_and_posts(html_text, limit=limit)
        if web_info:
            web_info.setdefault("username", ch)
            merged_info.update(web_info)
        if web_posts:
            return merged_info, web_posts

    treg_available = treg_fn is not None or (check_treg_ready is not None and check_treg_ready())
    if not allow_paid or not treg_available:
        return merged_info, []

    if _check_budget(
        budget_guard_fn,
        "treg",
        "tikhub.x.telegram-web-fetch-channel-posts",
        COST_TIKHUB_CHANNEL_POSTS_USD,
        conn=conn,
        cfg=cfg,
    ):
        tk_payload = _invoke_treg(
            treg_fn,
            "tikhub.x.telegram-web-fetch-channel-posts",
            query={"channel": ch, "limit": str(limit)},
            timeout=20,
        )
        tk_info, tk_posts = _extract_posts_and_info(tk_payload, ch, limit)
        if tk_payload is not None:
            _record_cost(
                cost_record_fn,
                conn=conn,
                provider="treg",
                endpoint="tikhub.x.telegram-web-fetch-channel-posts",
                lead_key=f"ch:{ch}",
                cost_usd=COST_TIKHUB_CHANNEL_POSTS_USD,
                hit=bool(tk_posts),
            )
        if tk_info:
            merged_info = {**merged_info, **{k: v for k, v in tk_info.items() if v not in ("", None)}}
        if tk_posts:
            return merged_info, tk_posts

    if _check_budget(
        budget_guard_fn,
        "treg",
        "crawl4ai.telegram.channel.posts",
        COST_CRAWL4AI_CHANNEL_POSTS_USD,
        conn=conn,
        cfg=cfg,
    ):
        c4_payload = _invoke_treg(
            treg_fn,
            "crawl4ai.telegram.channel.posts",
            method="POST",
            data={"channel": ch, "limit": limit},
            timeout=20,
        )
        c4_info, c4_posts = _extract_posts_and_info(c4_payload, ch, limit)
        if c4_payload is not None:
            _record_cost(
                cost_record_fn,
                conn=conn,
                provider="treg",
                endpoint="crawl4ai.telegram.channel.posts",
                lead_key=f"ch:{ch}",
                cost_usd=COST_CRAWL4AI_CHANNEL_POSTS_USD,
                hit=bool(c4_posts),
            )
        if c4_info:
            merged_info = {**merged_info, **{k: v for k, v in c4_info.items() if v not in ("", None)}}
        if c4_posts:
            return merged_info, c4_posts

    return merged_info, []


_ORIG_PROBE_TME_HANDLE_WEB = probe_tme_handle_web
_ORIG_FETCH_CHANNEL_POSTS_WEB = fetch_channel_posts_web


def has_patched_io() -> bool:
    """Return True when HTTP or web-client functions have been monkeypatched at runtime."""
    return (
        probe_tme_handle_web is not _ORIG_PROBE_TME_HANDLE_WEB
        or fetch_channel_posts_web is not _ORIG_FETCH_CHANNEL_POSTS_WEB
        or requests.get is not _ORIG_REQUESTS_GET
    )
