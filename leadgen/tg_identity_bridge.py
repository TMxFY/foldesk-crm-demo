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
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlparse

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


def _default_x_profile(handle: str) -> dict[str, Any] | None:
    clean = str(handle or "").strip().lstrip("@")
    if not clean:
        return None
    try:
        try:
            from leadgen.monid_client import check_monid_ready, run_monid
        except ImportError:
            from monid_client import check_monid_ready, run_monid
        if check_monid_ready():
            res = run_monid(
                "twitterdata.io",
                "/users",
                query={"username": clean},
                wait_sec=15,
                timeout=20,
            )
            if isinstance(res, dict) and res:
                return res
    except Exception:
        pass
    try:
        try:
            from leadgen.treg_client import call as treg_call, check_treg_ready
        except ImportError:
            from treg_client import call as treg_call, check_treg_ready
        if check_treg_ready():
            res = treg_call("scrapecreators.x.user.profile", query={"handle": clean}, timeout=20)
            if isinstance(res, dict) and res:
                return res
    except Exception:
        pass
    return None


def _default_search(query: str) -> dict[str, Any] | None:
    try:
        try:
            from leadgen.treg_client import call as treg_call, check_treg_ready
        except ImportError:
            from treg_client import call as treg_call, check_treg_ready
        if check_treg_ready():
            return treg_call("crawl4ai.web.search", query={"q": query}, timeout=25)
    except Exception:
        pass
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
    try:
        resp = requests.get(
            f"https://{clean}",
            headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC/1.0)"},
            timeout=4,
            allow_redirects=True,
        )
        if int(getattr(resp, "status_code", 0) or 0) == 200:
            return str(getattr(resp, "text", "") or "")[:200_000]
    except requests.RequestException:
        pass
    return ""


def _extract_social_or_email_candidates(row: dict[str, Any]) -> list[tuple[str, str]]:
    cands: list[tuple[str, str]] = []
    ig = str(row.get("instagram_url") or row.get("instagram") or "").strip()
    m_ig = re.search(r"instagram\.com/([A-Za-z0-9._]{3,32})", ig, re.I)
    if m_ig:
        h_ig = m_ig.group(1).strip(".").replace(".", "_")
        if h_ig.lower() not in {"p", "reel", "reels", "explore", "stories"}:
            cands.append((h_ig, "instagram_handle_confirmed"))
    for lk_key in ("founder_linkedin", "linkedin_url", "linkedin"):
        lk = str(row.get(lk_key) or "").strip()
        m_li = re.search(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)", lk, re.I)
        if m_li:
            slug = re.sub(r"-[0-9a-f]{6,}$", "", m_li.group(1), flags=re.I)
            slug = re.sub(r"-\d+$", "", slug)
            for variant in (slug.replace("-", "_"), slug.replace("-", "")):
                cands.append((variant, "linkedin_handle_confirmed"))
    for em_key in ("work_email", "email"):
        em = str(row.get(em_key) or "").strip()
        if "@" in em:
            local = em.split("@", 1)[0].strip().lower()
            if local and local not in _ROLE_EMAIL_PREFIXES:
                for variant in (local.replace(".", "_"), local.replace(".", "")):
                    cands.append((variant, "email_handle_confirmed"))
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
        for k in ("bio", "description", "url", "website", "location"):
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        entities = src.get("entities")
        if isinstance(entities, dict):
            parts.append(json.dumps(entities, ensure_ascii=False))
    return " ".join(parts)


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
    raw_name = " ".join(str(row.get("founder_name") or row.get("name") or "").split()).strip()
    comp_norm = " ".join(str(row.get("company") or row.get("project") or "").split()).strip()
    if raw_name and raw_name.casefold() != comp_norm.casefold():
        words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", raw_name)]
        if len(words) >= 2:
            if all(re.search(rf"\b{re.escape(w.casefold())}\b", tme_blob) for w in words[:2]):
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
) -> dict[str, Any]:
    """Attempt to resolve a verified personal Telegram handle for a non-TG lead."""
    out = dict(row)
    existing_tg = out.get("username") or out.get("telegram_username") or out.get("tg_username")
    if _is_valid_personal_tg_handle(existing_tg):
        return out

    get_x = _default_x_profile if x_profile_fn is None else x_profile_fn
    do_search = _default_search if search_fn is None else search_fn
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

    raw_site = str(out.get("project_url") or out.get("website") or out.get("site") or out.get("url") or "").strip()
    domain = domain_of(raw_site)
    brand = str(out.get("company") or out.get("project") or "").strip()
    if not brand:
        brand = str(out.get("name") or "").strip()
    x_handle = _extract_x_handle(out)

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

    # 0) Explicit t.me handles already present in lead links/bio (no remote calls if valid personal DM)
    links_blob = " ".join(str(u) for u in (out.get("links") or []) if u)
    stage0 = [
        (nick, "links_explicit", bool(_SUPPORT_TG_HANDLE_RE.search(nick)))
        for nick in _extract_tme_handles(f"{links_blob} {out.get('bio') or ''}")
    ]
    if hit := _try_candidates(stage0):
        return hit

    # 1) Founder's own X profile bio
    if x_handle:
        try:
            x_prof = get_x(x_handle)
        except Exception:
            x_prof = None
        bio_blob = _extract_x_bio_blob(x_prof)
        stage1 = [
            (nick, "x_bio_explicit", bool(_SUPPORT_TG_HANDLE_RE.search(nick)))
            for nick in _extract_tme_handles(bio_blob)
        ]
        if hit := _try_candidates(stage1):
            return hit

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
    found_domain_explicit = False
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
            if has_exact_domain:
                found_domain_explicit = True
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

    # 3b) Secondary person-name + brand search when multi-word founder name is known
    founder_raw = " ".join(str(out.get("founder_name") or out.get("name") or "").split()).strip()
    founder_words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", founder_raw)]
    if (
        not found_domain_explicit
        and len(founder_words) >= 2
        and founder_raw.casefold() != brand.casefold()
        and len(brand) >= 3
    ):
        try:
            s_payload_2 = do_search(f'site:t.me "{founder_raw}" "{brand}"')
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

    # 4) Same-handle / social / personal email candidates (strict anti-namesake confirmation required)
    stage4: list[tuple[str, str, bool]] = []
    if x_handle:
        stage4.append((x_handle, "x_handle_verified", True))
    for social_nick, social_kind in _extract_social_or_email_candidates(out):
        stage4.append((social_nick, social_kind, True))
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
