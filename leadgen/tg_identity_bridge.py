#!/usr/bin/env python3
"""Cross-Platform -> Telegram Identity Bridge for qualified B2B leads.

Turns non-TG leads (domain, brand, founder_x) into verified personal @telegram
contacts using:
1. Founder's X profile bio (Monid twitterdata.io /users) for explicit t.me/<nick>.
2. Targeted web search (`site:t.me "<domain>"` or `"<brand>"`) via Treg.
3. Public https://t.me/<nick> personal-user verification + strict anti-namesake guard.
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
from pathlib import Path
import re
import sys
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import requests

try:
    from creator_external_leads import _is_valid_personal_tme_page
except ImportError:
    try:
        from leadgen.creator_external_leads import _is_valid_personal_tme_page
    except ImportError:
        _is_valid_personal_tme_page = None

try:
    from treg_enrich import domain_of
except ImportError:
    try:
        from leadgen.treg_enrich import domain_of
    except ImportError:
        from scripts.treg_enrich import domain_of


logger = logging.getLogger(__name__)

COST_MONID_X_PROFILE_USD = 0.001
COST_TREG_X_PROFILE_USD = 0.001
COST_CRAWL4AI_SEARCH_USD = 0.00015

_TG_NOISE = {
    "joinchat",
    "share",
    "addstickers",
    "proxy",
    "iv",
    "s",
    "c",
    "boost",
    "socks",
    "login",
    "contact",
}
_TME_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})\b",
    re.I,
)
_X_HANDLE_RE = re.compile(r"([A-Za-z0-9_]{1,15})")
_X_DENY = {"home", "search", "explore", "i", "share", "intent", "hashtag", "compose"}
_GENERIC_BRAND_WORDS = frozenset(
    {
        "video",
        "studio",
        "labs",
        "group",
        "media",
        "digital",
        "creator",
        "creators",
        "official",
        "global",
        "online",
        "house",
        "club",
        "team",
        "network",
        "agency",
        "marketing",
        "app",
        "apps",
        "saas",
        "software",
        "tech",
        "stealthco",
        "tools",
        "tool",
        "mobile",
        "game",
        "games",
        "finance",
        "platform",
        "smart",
        "cloud",
        "data",
        "flow",
        "note",
        "notes",
        "work",
        "works",
        "chat",
        "chats",
        "play",
        "link",
        "links",
        "next",
        "open",
        "fast",
        "auto",
        "core",
        "base",
        "sync",
        "plus",
        "pro",
    }
)
_SUPPORT_TG_HANDLE_RE = re.compile(
    r"(?:^|_)(?:support|help|admin|manager|sales|care|news|channel|chat|community|group)(?:$|_)",
    re.I,
)
_ROLE_EMAIL_PREFIXES = frozenset(
    {
        "info",
        "support",
        "hello",
        "hi",
        "hey",
        "contact",
        "sales",
        "team",
        "admin",
        "office",
        "press",
        "help",
        "care",
        "marketing",
        "billing",
        "privacy",
        "legal",
        "aso",
        "research",
        "jobs",
        "careers",
        "noreply",
        "no-reply",
        "founders",
        "founder",
    }
)


def _is_valid_personal_tg_handle(raw: Any) -> bool:
    u = str(raw or "").strip().lstrip("@")
    if not u or len(u) < 4 or len(u) > 32:
        return False
    low = u.lower()
    if low in _TG_NOISE or low.endswith("bot") or low.endswith("_bot"):
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", u))


def _extract_x_handle(row: dict[str, Any]) -> str:
    for key in ("founder_x", "founder_twitter", "x_url", "twitter_url", "twitter"):
        val = str(row.get(key) or "").strip()
        if not val:
            continue
        if "://" not in val and "/" not in val:
            h = val.lstrip("@")
            if _X_HANDLE_RE.fullmatch(h) and h.casefold() not in _X_DENY:
                return h
            continue
        parsed = urlparse(val if "://" in val else f"https://{val}")
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host in {"x.com", "twitter.com", "mobile.twitter.com"}:
            parts = [p for p in (parsed.path or "").split("/") if p]
            if parts and _X_HANDLE_RE.fullmatch(parts[0]) and parts[0].casefold() not in _X_DENY:
                return parts[0]
    return ""


def _extract_tme_handles(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in _TME_LINK_RE.finditer(text or ""):
        raw_full = m.group(0)
        # Skip channel message previews like t.me/s/channel
        tail_after = (text or "")[m.end() : m.end() + 2]
        nick = m.group(1)
        if nick.lower() == "s" and tail_after.startswith("/"):
            continue
        if not _is_valid_personal_tg_handle(nick):
            continue
        if nick.lower() not in seen:
            seen.add(nick.lower())
            found.append(nick)
    return found


def _call_with_supported_kwargs(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return fn(*args, **kwargs)
        filtered = {k: v for k, v in kwargs.items() if k in params}
        return fn(*args, **filtered)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)


def _check_bridge_budget_guard(
    budget_guard_fn: Callable[..., Any] | None,
    provider: str,
    endpoint: str,
    est_cost: float,
) -> bool:
    if budget_guard_fn is None:
        return True
    try:
        sig = inspect.signature(budget_guard_fn)
        params = list(sig.parameters.values())
        pos_count = sum(
            1 for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
        has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        if has_var_pos or pos_count >= 3:
            res = budget_guard_fn(provider, endpoint, float(est_cost))
        elif pos_count == 2:
            res = budget_guard_fn(provider, float(est_cost))
        else:
            res = _call_with_supported_kwargs(
                budget_guard_fn,
                provider=provider,
                endpoint=endpoint,
                estimated_cost=float(est_cost),
            )
    except Exception as exc:
        logger.debug("bridge budget_guard_fn failed: %s", exc)
        return False
    if isinstance(res, tuple):
        return bool(res[0]) if res else False
    return bool(res)


def _record_bridge_cost(
    cost_record_fn: Callable[..., Any] | None,
    *,
    provider: str,
    endpoint: str,
    lead_key: str,
    cost_usd: float,
    hit: bool,
) -> None:
    if cost_record_fn is None:
        return
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
        logger.debug("bridge cost_record_fn failed: %s", exc)


def _extract_bridge_cost(payload: Any, default_cost: float) -> float:
    if isinstance(payload, dict):
        for meta_key in ("_treg", "_monid", "meta"):
            meta = payload.get(meta_key)
            if isinstance(meta, dict):
                for ck in ("cost_usd", "cost"):
                    val = meta.get(ck)
                    if val is not None and val != "":
                        try:
                            num = float(val)
                            if num > 0:
                                return num
                        except (TypeError, ValueError):
                            continue
        for ck in ("cost_usd", "cost"):
            val = payload.get(ck)
            if val is not None and val != "":
                try:
                    num = float(val)
                    if num > 0:
                        return num
                except (TypeError, ValueError):
                    continue
    return default_cost


def _default_x_profile(
    handle: str,
    *,
    allow_paid: bool = True,
    budget_guard_fn: Callable[..., Any] | None = None,
    cost_record_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
    lead_key: str = "",
) -> dict[str, Any] | None:
    clean = str(handle or "").strip().lstrip("@")
    if not clean or not allow_paid:
        return None
    if _check_bridge_budget_guard(budget_guard_fn, "monid", "twitterdata.io:/users", COST_MONID_X_PROFILE_USD):
        try:
            try:
                from leadgen.monid_client import check_monid_ready, run_monid
            except ImportError:
                from monid_client import check_monid_ready, run_monid
            if monid_fn is not None or check_monid_ready():
                res = (
                    _call_with_supported_kwargs(
                        monid_fn,
                        "twitterdata.io",
                        "/users",
                        query={"username": clean},
                        wait_sec=15,
                        timeout=20,
                    )
                    if monid_fn is not None
                    else run_monid(
                        "twitterdata.io",
                        "/users",
                        query={"username": clean},
                        wait_sec=15,
                        timeout=20,
                    )
                )
                is_hit = isinstance(res, dict) and bool(res)
                cost = _extract_bridge_cost(res, COST_MONID_X_PROFILE_USD) if is_hit else 0.0
                _record_bridge_cost(
                    cost_record_fn,
                    provider="monid",
                    endpoint="twitterdata.io:/users",
                    lead_key=lead_key or clean,
                    cost_usd=cost,
                    hit=is_hit,
                )
                if is_hit:
                    return res
        except Exception as exc:
            logger.debug("monid x_profile lookup failed: %s", exc)
    if _check_bridge_budget_guard(budget_guard_fn, "treg", "scrapecreators.x.user.profile", COST_TREG_X_PROFILE_USD):
        try:
            try:
                from leadgen.treg_client import call as treg_call, check_treg_ready
            except ImportError:
                from treg_client import call as treg_call, check_treg_ready
            if treg_fn is not None or check_treg_ready():
                res = (
                    _call_with_supported_kwargs(
                        treg_fn,
                        "scrapecreators.x.user.profile",
                        query={"handle": clean},
                        timeout=20,
                    )
                    if treg_fn is not None
                    else treg_call("scrapecreators.x.user.profile", query={"handle": clean}, timeout=20)
                )
                is_hit = isinstance(res, dict) and bool(res)
                cost = _extract_bridge_cost(res, COST_TREG_X_PROFILE_USD) if is_hit else 0.0
                _record_bridge_cost(
                    cost_record_fn,
                    provider="treg",
                    endpoint="scrapecreators.x.user.profile",
                    lead_key=lead_key or clean,
                    cost_usd=cost,
                    hit=is_hit,
                )
                if is_hit:
                    return res
        except Exception as exc:
            logger.debug("treg x_profile lookup failed: %s", exc)
    return None


def _default_search(
    query: str,
    *,
    allow_paid: bool = True,
    budget_guard_fn: Callable[..., Any] | None = None,
    cost_record_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    lead_key: str = "",
) -> dict[str, Any] | None:
    if not allow_paid:
        return None
    if not _check_bridge_budget_guard(budget_guard_fn, "treg", "crawl4ai.web.search", COST_CRAWL4AI_SEARCH_USD):
        return None
    try:
        try:
            from leadgen.treg_client import call as treg_call, check_treg_ready
        except ImportError:
            from treg_client import call as treg_call, check_treg_ready
        if treg_fn is not None or check_treg_ready():
            res = (
                _call_with_supported_kwargs(
                    treg_fn,
                    "crawl4ai.web.search",
                    query={"q": query},
                    timeout=25,
                )
                if treg_fn is not None
                else treg_call("crawl4ai.web.search", query={"q": query}, timeout=25)
            )
            is_hit = isinstance(res, dict) and bool(res)
            cost = _extract_bridge_cost(res, COST_CRAWL4AI_SEARCH_USD) if isinstance(res, dict) else 0.0
            _record_bridge_cost(
                cost_record_fn,
                provider="treg",
                endpoint="crawl4ai.web.search",
                lead_key=lead_key or query,
                cost_usd=cost,
                hit=is_hit,
            )
            return res
    except Exception as exc:
        logger.debug("treg search failed: %s", exc)
    return None


def _default_tme_fetch(nick: str) -> Any:
    clean = str(nick or "").strip().lstrip("@")
    if not clean:
        return ""
    try:
        return requests.get(
            f"https://t.me/{clean}",
            headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC/1.0)"},
            timeout=4,
            allow_redirects=False,
        )
    except requests.RequestException:
        return ""


def _default_site_fetch(domain: str) -> str:
    clean = str(domain or "").strip().lower().removeprefix("www.").strip("/")
    if not clean or "." not in clean:
        return ""
    chunks: list[str] = []
    for suffix in ("", "/contact", "/about"):
        try:
            resp = requests.get(
                f"https://{clean}{suffix}",
                headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC/1.0)"},
                timeout=3 if suffix else 4,
                allow_redirects=True,
            )
            if int(getattr(resp, "status_code", 0) or 0) == 200:
                txt = str(getattr(resp, "text", "") or "")[:200_000]
                if txt:
                    chunks.append(txt)
                    if suffix == "" and _extract_tme_handles(txt):
                        break
        except requests.RequestException:
            continue
    return "\n".join(chunks)


_SLUG_GENERIC_TOKENS = frozenset(
    {
        "profile",
        "in",
        "www",
        "http",
        "https",
        "com",
        "linkedin",
        "official",
        "hq",
        "ceo",
        "founder",
        "cofounder",
        "cmo",
        "cto",
        "coo",
        "cfo",
        "cgo",
        "owner",
    }
)
_NON_PERSON_ROW_NAME_WORDS = frozenset(
    {
        "role",
        "hiring",
        "job",
        "jobs",
        "manager",
        "director",
        "engineer",
        "developer",
        "strategist",
        "intern",
        "specialist",
        "coordinator",
        "agency",
        "studio",
        "studios",
        "labs",
        "official",
        "app",
        "apps",
        "saas",
        "shop",
        "store",
        "platform",
        "software",
        "solutions",
        "services",
        "editor",
        "generator",
        "tracker",
        "planner",
        "tutor",
        "ecom",
        "llc",
        "inc",
        "ltd",
        "corp",
        "gmbh",
    }
)


def _linkedin_urls_from_row(row: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for lk_key in ("founder_linkedin", "linkedin_url", "linkedin"):
        val = str(row.get(lk_key) or "").strip()
        if val:
            urls.append(val)
    links = row.get("links")
    if isinstance(links, list):
        for item in links:
            s = str(item or "").strip()
            if s and "linkedin.com/in/" in s.lower() and s not in urls:
                urls.append(s)
    return urls


def _clean_linkedin_slug_parts(lk_url: str) -> tuple[str, list[str]]:
    m_li = re.search(r"linkedin\.com/in/([^/?#\s]+)", str(lk_url or ""), re.I)
    if not m_li:
        return "", []
    raw_slug = unquote(m_li.group(1)).strip().strip("/")
    tokens = [t for t in re.split(r"[-_]+", raw_slug) if t]
    while tokens:
        last = tokens[-1]
        if re.fullmatch(r"\d+", last):
            tokens.pop()
        elif len(tokens) > 2 and re.fullmatch(r"[0-9a-f]{5,}", last, re.I):
            tokens.pop()
        elif re.fullmatch(r"(?=.*\d)[0-9a-f]{5,}", last, re.I):
            tokens.pop()
        else:
            break
    if not tokens:
        return "", []
    return "-".join(tokens), tokens


def _infer_founder_name_from_row(row: dict[str, Any], brand: str = "") -> str:
    if not isinstance(row, dict):
        return ""
    comp_variants = {
        " ".join(str(b or "").split()).strip().casefold()
        for b in (brand, row.get("company"), row.get("project"))
        if str(b or "").strip()
    }
    comp_compact = {c.replace(" ", "") for c in comp_variants}
    brand_tokens = {
        w.casefold()
        for comp in comp_variants
        for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9]{3,}", comp)
        if w.casefold() not in _GENERIC_BRAND_WORDS
    }

    # 1) Explicit founder_name
    raw_fn = " ".join(str(row.get("founder_name") or "").split()).strip()
    if (
        raw_fn
        and raw_fn.casefold() not in comp_variants
        and not re.search(r"https?://|t\.me/|@|\d|[:/|]", raw_fn)
    ):
        fn_words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", raw_fn)]
        if len(fn_words) >= 2:
            return raw_fn

    # 2) Multi-word person `name` distinct from brand
    raw_nm = " ".join(str(row.get("name") or "").split()).strip()
    if (
        raw_nm
        and raw_nm.casefold() not in comp_variants
        and not re.search(r"https?://|t\.me/|@|\d|[:/|–—]", raw_nm)
    ):
        nm_words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", raw_nm)]
        if 2 <= len(nm_words) <= 4:
            lows = {w.casefold() for w in nm_words}
            if (
                not (lows & _NON_PERSON_ROW_NAME_WORDS)
                and not (lows <= _GENERIC_BRAND_WORDS)
                and not (lows & brand_tokens)
            ):
                return raw_nm

    # 3) Parse founder_linkedin / linkedin_url /in/<slug>
    for lk in _linkedin_urls_from_row(row):
        _, parts = _clean_linkedin_slug_parts(lk)
        alpha = [
            p
            for p in parts
            if p.lower() not in _SLUG_GENERIC_TOKENS
            and re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё]{2,}", p)
        ]
        if not (2 <= len(alpha) <= 4):
            continue
        low_alpha = {w.casefold() for w in alpha}
        joined_cf = " ".join(alpha).casefold()
        if (
            joined_cf in comp_variants
            or joined_cf.replace(" ", "") in comp_compact
            or (low_alpha & _NON_PERSON_ROW_NAME_WORDS)
            or (low_alpha <= _GENERIC_BRAND_WORDS)
            or (low_alpha & brand_tokens)
        ):
            continue
        return " ".join(w.capitalize() for w in alpha)

    return ""


def _extract_social_or_email_candidates(row: dict[str, Any]) -> list[tuple[str, str]]:
    cands: list[tuple[str, str]] = []
    seen_local: set[str] = set()

    def _add(nick: str, kind: str) -> None:
        clean = nick.strip().lower()
        if clean and clean not in seen_local:
            seen_local.add(clean)
            cands.append((clean, kind))

    ig = str(row.get("instagram_url") or row.get("instagram") or "").strip()
    m_ig = re.search(r"instagram\.com/([A-Za-z0-9._]{3,32})", ig, re.I)
    if m_ig:
        h_ig = m_ig.group(1).strip(".").replace(".", "_")
        if h_ig.lower() not in {"p", "reel", "reels", "explore", "stories"}:
            _add(h_ig, "instagram_handle_confirmed")

    for lk in _linkedin_urls_from_row(row):
        slug, parts = _clean_linkedin_slug_parts(lk)
        if not slug:
            continue
        _add(slug.replace("-", "_"), "linkedin_handle_confirmed")
        _add(slug.replace("-", ""), "linkedin_handle_confirmed")
        alpha_parts = [
            p.lower()
            for p in parts
            if p.lower() not in _SLUG_GENERIC_TOKENS and re.fullmatch(r"[A-Za-z]{2,}", p)
        ]
        if len(alpha_parts) >= 3:
            _add(f"{alpha_parts[0]}_{alpha_parts[-1]}", "linkedin_handle_confirmed")
            _add(f"{alpha_parts[0]}{alpha_parts[-1]}", "linkedin_handle_confirmed")
        if len(alpha_parts) >= 2:
            _add(f"{alpha_parts[0]}{alpha_parts[-1][0]}", "linkedin_handle_confirmed")

    for em_key in ("work_email", "email"):
        em = str(row.get(em_key) or "").strip()
        if "@" in em:
            local = em.split("@", 1)[0].strip().lower()
            if local and local not in _ROLE_EMAIL_PREFIXES:
                _add(re.sub(r"[.\-]+", "_", local), "email_handle_confirmed")
                _add(re.sub(r"[.\-_]+", "", local), "email_handle_confirmed")
    return cands


def _parse_tme_response(resp: Any, nick: str) -> tuple[bool, str, str]:
    """Validate response is HTTP 200 personal t.me page and return (ok, title, bio)."""
    if not resp:
        return False, "", ""
    if isinstance(resp, str):
        html_txt = resp
    else:
        status = int(getattr(resp, "status_code", 200) or 0)
        is_redir = (
            bool(getattr(resp, "is_redirect", False))
            or (300 <= status < 400)
            or bool((getattr(resp, "headers", {}) or {}).get("Location"))
        )
        if status != 200 or is_redir:
            return False, "", ""
        html_txt = str(getattr(resp, "text", "") or "")

    if _is_valid_personal_tme_page is not None:
        if not _is_valid_personal_tme_page(html_txt, nick):
            return False, "", ""
    else:
        low = html_txt.lower()
        if "tgme_page_extra" not in html_txt or "tgme_page_title" not in html_txt:
            return False, "", ""
        if any(tok in low for tok in ("subscriber", "member", "online", "start bot", "monthly user")):
            return False, "", ""

    title = ""
    bio = ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        BeautifulSoup = None

    if BeautifulSoup is not None:
        soup = BeautifulSoup(html_txt, "html.parser")
        tnode = soup.select_one(".tgme_page_title")
        dnode = soup.select_one(".tgme_page_description")
        title = tnode.get_text(" ", strip=True) if tnode else ""
        bio = dnode.get_text(" ", strip=True) if dnode else ""
    else:
        mt = re.search(r'class="[^"]*\btgme_page_title\b[^"]*"[^>]*>(.*?)</div>', html_txt, re.I | re.S)
        md = re.search(r'class="[^"]*\btgme_page_description\b[^"]*"[^>]*>(.*?)</div>', html_txt, re.I | re.S)
        title = re.sub(r"<[^>]+>", " ", mt.group(1)).strip() if mt else ""
        bio = re.sub(r"<[^>]+>", " ", md.group(1)).strip() if md else ""

    return True, title, bio


def _extract_x_bio_blob(profile: dict[str, Any] | None) -> str:
    if not isinstance(profile, dict):
        return ""
    roots: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    def _push(obj: Any) -> None:
        if isinstance(obj, dict) and id(obj) not in seen_ids:
            seen_ids.add(id(obj))
            roots.append(obj)

    for top in (
        profile,
        profile.get("data"),
        profile.get("user"),
        profile.get("output"),
        profile.get("result"),
        profile.get("legacy"),
    ):
        _push(top)
        if isinstance(top, dict):
            for nested_k in ("legacy", "user", "result"):
                sub = top.get(nested_k)
                _push(sub)
                if isinstance(sub, dict) and isinstance(sub.get("legacy"), dict):
                    _push(sub.get("legacy"))

    parts: list[str] = []
    for src in roots:
        for k in ("bio", "description", "url", "website", "location", "name"):
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        entities = src.get("entities")
        if isinstance(entities, dict):
            parts.append(json.dumps(entities, ensure_ascii=False))
    return " ".join(parts)


def _extract_x_founder_name(profile: dict[str, Any] | None, brand: str) -> str:
    if not isinstance(profile, dict):
        return ""
    candidates: list[str] = []
    for top in (profile, profile.get("data"), profile.get("user"), profile.get("output"), profile.get("result"), profile.get("legacy")):
        if isinstance(top, dict):
            val = top.get("name")
            if isinstance(val, str) and val.strip():
                candidates.append(" ".join(val.split()).strip())
            leg = top.get("legacy")
            if isinstance(leg, dict) and isinstance(leg.get("name"), str) and leg["name"].strip():
                candidates.append(" ".join(leg["name"].split()).strip())
    brand_cf = str(brand or "").strip().casefold()
    for cand in candidates:
        if cand.casefold() == brand_cf:
            continue
        words = re.findall(r"[A-Za-z]{2,18}", cand)
        if 2 <= len(words) <= 3 and not any(w.lower() in _GENERIC_BRAND_WORDS for w in words):
            return " ".join(words)
    return ""


def _distinctive_tokens(row: dict[str, Any], domain: str) -> list[str]:
    tokens: list[str] = []
    if domain:
        dom_low = domain.lower()
        tokens.append(dom_low)
        root = dom_low.split(".")[0]
        if len(root) >= 4 and root not in _GENERIC_BRAND_WORDS:
            tokens.append(root)
    for raw_brand in (row.get("company"), row.get("project")):
        for word in re.findall(r"[A-Za-z0-9]{4,}", str(raw_brand or "").lower()):
            if word not in _GENERIC_BRAND_WORDS and word not in tokens:
                tokens.append(word)
    return tokens


def _identity_confirmed_on_tme(title: str, bio: str, row: dict[str, Any], domain: str) -> bool:
    tme_blob = f"{title} {bio}".casefold()
    # 1) Distinctive brand or domain token (>= 4 chars, whole-word match for word tokens)
    for tok in _distinctive_tokens(row, domain):
        if "." in tok:
            if tok in tme_blob:
                return True
        elif re.search(rf"\b{re.escape(tok)}\b", tme_blob):
            return True
    # 2) Full founder name (2+ alphabetic words, each >= 2 chars)
    brand = str(row.get("company") or row.get("project") or "").strip()
    if not brand:
        brand = str(row.get("name") or "").strip()
    inferred_name = _infer_founder_name_from_row(row, brand)
    if inferred_name:
        words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", inferred_name)]
        if len(words) >= 2:
            if all(re.search(rf"\b{re.escape(w.casefold())}\b", tme_blob) for w in words[:2]):
                return True
            if len(words) >= 3 and (
                re.search(rf"\b{re.escape(words[0].casefold())}\b", tme_blob)
                and re.search(rf"\b{re.escape(words[-1].casefold())}\b", tme_blob)
            ):
                return True
    return False


def _unwrap_search_hits(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for k in ("results", "items", "organic_results"):
        val = payload.get(k)
        if isinstance(val, list) and val:
            dicts = [x for x in val if isinstance(x, dict)]
            if dicts:
                return dicts
    for sub in ("output", "result", "data"):
        child = payload.get(sub)
        if isinstance(child, (dict, list)):
            hits = _unwrap_search_hits(child)
            if hits:
                return hits
    return []


def bridge_lead_to_telegram(
    row: dict[str, Any],
    *,
    x_profile_fn=None,
    search_fn=None,
    tme_fetch_fn=None,
    fetch_site_fn=None,
    allow_paid: bool = True,
    budget_guard_fn: Callable[[str, str, float], bool] | None = None,
    cost_record_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Attempt to resolve a verified personal Telegram handle for a non-TG lead."""
    out = dict(row)
    existing_tg = out.get("username") or out.get("telegram_username") or out.get("tg_username")
    if _is_valid_personal_tg_handle(existing_tg):
        return out

    raw_site = str(out.get("project_url") or out.get("website") or out.get("site") or out.get("url") or "").strip()
    domain = domain_of(raw_site)
    brand = str(out.get("company") or out.get("project") or "").strip()
    if not brand:
        brand = str(out.get("name") or "").strip()
    x_handle = _extract_x_handle(out)
    lead_key = str(out.get("id") or domain or brand or x_handle or "lead").strip()

    def _guarded_x_profile(handle: str) -> dict[str, Any] | None:
        return _default_x_profile(
            handle,
            allow_paid=allow_paid,
            budget_guard_fn=budget_guard_fn,
            cost_record_fn=cost_record_fn,
            treg_fn=treg_fn,
            monid_fn=monid_fn,
            lead_key=lead_key,
        )

    def _guarded_search(query: str) -> dict[str, Any] | None:
        return _default_search(
            query,
            allow_paid=allow_paid,
            budget_guard_fn=budget_guard_fn,
            cost_record_fn=cost_record_fn,
            treg_fn=treg_fn,
            lead_key=lead_key,
        )

    get_x = _guarded_x_profile if x_profile_fn is None else x_profile_fn
    do_search = _guarded_search if search_fn is None else search_fn
    fetch_tme = _default_tme_fetch if tme_fetch_fn is None else tme_fetch_fn
    do_site_fetch = (
        fetch_site_fn
        if fetch_site_fn is not None
        else (
            _default_site_fetch
            if (x_profile_fn is None and search_fn is None and tme_fetch_fn is None)
            else None
        )
    )

    seen_cands: set[str] = set()

    def _try_candidates(stage_cands: list[tuple[str, str, bool]]) -> dict[str, Any] | None:
        for nick, match_kind, require_confirm in stage_cands:
            if not _is_valid_personal_tg_handle(nick):
                continue
            low = nick.lower()
            if low in seen_cands:
                continue
            seen_cands.add(low)
            try:
                resp = fetch_tme(nick)
            except Exception:
                resp = ""
            ok, title, bio = _parse_tme_response(resp, nick)
            if not ok:
                continue
            if require_confirm and not _identity_confirmed_on_tme(title, bio, out, domain):
                continue

            tg_url = f"https://t.me/{nick}"
            out["username"] = nick
            out["telegram_username"] = nick
            out["tg_username"] = nick
            out["telegram_url"] = tg_url
            out["tg_bridge_match"] = match_kind
            out["provider"] = "telegram"
            channels = [str(c) for c in (out.get("contact_channels") or []) if str(c).strip()]
            if "telegram" not in channels:
                channels.insert(0, "telegram")
            out["contact_channels"] = channels
            links = [str(u) for u in (out.get("links") or []) if str(u).strip()]
            if tg_url not in links:
                links.append(tg_url)
            out["links"] = links
            return out
        return None

    # 0) Explicit t.me handles already present in lead links/bio/notes (no remote calls if valid personal DM)
    links_blob = " ".join(str(u) for u in (out.get("links") or []) if u)
    stage0_blob = " ".join(
        p for p in (links_blob, out.get("bio") or "", out.get("sample") or "", out.get("what_they_do") or "") if p
    )
    stage0 = [
        (nick, "links_explicit", bool(_SUPPORT_TG_HANDLE_RE.search(nick)))
        for nick in _extract_tme_handles(stage0_blob)
    ]
    if hit := _try_candidates(stage0):
        return hit

    # 1) Founder's own X profile bio
    x_founder_cands: list[tuple[str, str]] = []
    if x_handle:
        try:
            x_prof = get_x(x_handle)
        except Exception:
            x_prof = None
        bio_blob = _extract_x_bio_blob(x_prof)
        x_founder_name = _extract_x_founder_name(x_prof, brand)
        if x_founder_name and not str(out.get("founder_name") or "").strip():
            out["founder_name"] = x_founder_name
        resolved_x_founder = x_founder_name or _infer_founder_name_from_row(out, brand)
        if resolved_x_founder:
            fw = [w.lower() for w in re.findall(r"[A-Za-z]{2,}", resolved_x_founder)]
            if len(fw) >= 2:
                x_founder_cands.append((f"{fw[0]}_{fw[-1]}", "x_founder_handle"))
                x_founder_cands.append((f"{fw[0]}{fw[-1]}", "x_founder_handle"))
        stage1 = [
            (nick, "x_bio_explicit", bool(_SUPPORT_TG_HANDLE_RE.search(nick)))
            for nick in _extract_tme_handles(bio_blob)
        ]
        if hit := _try_candidates(stage1):
            return hit

    founder_raw = _infer_founder_name_from_row(out, brand)
    if founder_raw and not str(out.get("founder_name") or "").strip():
        out["founder_name"] = founder_raw

    # 2) Direct product website HTML scan for explicit t.me handles
    if domain and do_site_fetch is not None:
        try:
            site_html = do_site_fetch(domain)
        except Exception:
            site_html = ""
        site_nicks = _extract_tme_handles(site_html)
        multi_site = len(site_nicks) > 1
        stage2 = [
            (nick, "website_direct", bool(multi_site or _SUPPORT_TG_HANDLE_RE.search(nick)))
            for nick in site_nicks
        ]
        if hit := _try_candidates(stage2):
            return hit

    # 3) Web search `site:t.me "<domain>"` or `"<brand>"`
    search_target = f'"{domain}"' if domain else (f'"{brand}"' if len(brand) >= 4 else "")
    if search_target:
        query = f"site:t.me {search_target}"
        try:
            s_payload = do_search(query)
        except Exception:
            s_payload = None
        stage3a: list[tuple[str, str, bool]] = []
        for search_hit in _unwrap_search_hits(s_payload):
            hit_url = str(search_hit.get("url") or "").strip()
            hit_title = str(search_hit.get("title") or "").strip()
            hit_snip = str(search_hit.get("snippet") or search_hit.get("description") or "").strip()
            hit_blob = f"{hit_url} {hit_title} {hit_snip}"
            has_exact_domain = bool(domain and domain.lower() in hit_blob.lower())
            for nick in _extract_tme_handles(hit_blob):
                stage3a.append(
                    (
                        nick,
                        "domain_search_explicit" if has_exact_domain else "x_handle_verified",
                        bool((not has_exact_domain) or _SUPPORT_TG_HANDLE_RE.search(nick)),
                    )
                )
        if hit := _try_candidates(stage3a):
            return hit

    # 3b) Secondary person-name + brand search (plus person-name fallback) when multi-word founder name is known
    founder_words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", founder_raw)]
    if len(founder_words) >= 2:
        founder_queries: list[str] = []
        if len(brand) >= 3 and founder_raw.casefold() != brand.casefold():
            founder_queries.append(f'site:t.me "{founder_raw}" "{brand}"')
        founder_queries.append(f'site:t.me "{founder_raw}"')
        for f_query in founder_queries:
            try:
                s_payload_2 = do_search(f_query)
            except Exception:
                s_payload_2 = None
            stage3b: list[tuple[str, str, bool]] = []
            for search_hit in _unwrap_search_hits(s_payload_2):
                hit_blob = " ".join(
                    str(search_hit.get(k) or "").strip() for k in ("url", "title", "snippet", "description")
                )
                for nick in _extract_tme_handles(hit_blob):
                    stage3b.append((nick, "founder_search_verified", True))
            if hit := _try_candidates(stage3b):
                return hit

    # 4) Same-handle / social / personal email / founder-name candidates (strict anti-namesake confirmation required)
    stage4: list[tuple[str, str, bool]] = []
    if x_handle:
        stage4.append((x_handle, "x_handle_verified", True))
    for fn_nick, fn_kind in x_founder_cands:
        stage4.append((fn_nick, fn_kind, True))
    for social_nick, social_kind in _extract_social_or_email_candidates(out):
        stage4.append((social_nick, social_kind, True))
    if founder_raw:
        fw_ascii = [w.lower() for w in re.findall(r"[A-Za-z]{2,}", founder_raw)]
        if len(fw_ascii) >= 2:
            stage4.append((f"{fw_ascii[0]}_{fw_ascii[-1]}", "founder_handle_confirmed", True))
            stage4.append((f"{fw_ascii[0]}{fw_ascii[-1]}", "founder_handle_confirmed", True))
    if hit := _try_candidates(stage4):
        return hit

    return out


def enrich_rows_with_telegram_bridge(
    rows: list[dict[str, Any]],
    *,
    max_rows: int = 20,
    x_profile_fn=None,
    search_fn=None,
    tme_fetch_fn=None,
    fetch_site_fn=None,
    allow_paid: bool = True,
    budget_guard_fn: Callable[[str, str, float], bool] | None = None,
    cost_record_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Run Telegram bridge on up to max_rows non-TG leads in batch."""
    out: list[dict[str, Any]] = []
    attempted = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        existing_tg = row.get("username") or row.get("telegram_username") or row.get("tg_username")
        if _is_valid_personal_tg_handle(existing_tg):
            out.append(dict(row))
            continue
        if attempted >= max(0, int(max_rows)):
            out.append(dict(row))
            continue
        attempted += 1
        out.append(
            bridge_lead_to_telegram(
                row,
                x_profile_fn=x_profile_fn,
                search_fn=search_fn,
                tme_fetch_fn=tme_fetch_fn,
                fetch_site_fn=fetch_site_fn,
                allow_paid=allow_paid,
                budget_guard_fn=budget_guard_fn,
                cost_record_fn=cost_record_fn,
                treg_fn=treg_fn,
                monid_fn=monid_fn,
            )
        )
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Bridge non-TG leads to verified Telegram handles")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-rows", type=int, default=35)
    args = parser.parse_args(argv)

    in_path = Path(args.input)
    rows: list[dict[str, Any]] = []
    if in_path.is_file():
        for line in in_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)

    enriched = enrich_rows_with_telegram_bridge(rows, max_rows=max(0, args.max_rows))
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in enriched:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
