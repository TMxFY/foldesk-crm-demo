#!/usr/bin/env python3
"""Bounded external discovery for CreatorOS.

Sources covered by this module:

* GitHub repositories and Show HN launches;
* Hugging Face Spaces and Models;
* App Store and Chrome Web Store listings;
* public X/LinkedIn launch signals via DuckDuckGo's HTML search endpoint.

The module only discovers public candidates.  It deliberately does not mass
scrape profiles or send outreach.  Every source has a small hard limit and a
failure in one source is isolated from the others.

The output is JSONL in the same broad shape used by the existing CreatorOS
enrichment/qualification scripts.  ``--qualify`` runs the shared Jev
classifier from ``scripts.typesafe_leads``; TypeSafe failures are fail-open so
we never throw away a candidate merely because the optional AI service is
unavailable.
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
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional in minimal runners
    BeautifulSoup = None

try:
    from leadgen.typesafe_leads import (
        creator_filter, qualify_creator_rows, qualify_identity_candidates, identity_filter,
    )
    from leadgen.identity_enrichment import (
        candidate_state,
        generate_telegram_candidates,
        verify_telegram_candidates as verify_identity_candidates,
    )
except Exception:
    try:
        from typesafe_leads import (
            creator_filter, qualify_creator_rows, qualify_identity_candidates, identity_filter,
        )
        from identity_enrichment import (
            candidate_state,
            generate_telegram_candidates,
            verify_telegram_candidates as verify_identity_candidates,
        )
    except Exception:
        try:
            from scripts.typesafe_leads import (
                creator_filter, qualify_creator_rows, qualify_identity_candidates, identity_filter,
            )
            from scripts.identity_enrichment import (
                candidate_state,
                generate_telegram_candidates,
                verify_telegram_candidates as verify_identity_candidates,
            )
        except Exception:  # pragma: no cover
            creator_filter = qualify_creator_rows = qualify_identity_candidates = None
            candidate_state = generate_telegram_candidates = identity_filter = verify_identity_candidates = None


UA = {
    "User-Agent": (
        "CreatorOS-lead-discovery/1.0 (+public bounded research; "
        "contact: operations@example.invalid)"
    )
}
TIMEOUT = 20
DEFAULT_LIMIT = 20

_X_HOSTS = {"x.com", "twitter.com"}
_LINKEDIN_HOSTS = {"linkedin.com"}
_SOCIAL_NOISE = {"i", "intent", "search", "hashtag", "home", "explore"}


def log(*parts: Any) -> None:
    print(*parts, file=sys.stderr, flush=True)


def _get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(url, headers=UA, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def _get_text(url: str, *, params: dict[str, Any] | None = None) -> str:
    response = requests.get(url, headers=UA, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def clean_handle(value: str) -> str:
    """Normalize a public username without guessing from a person's name."""
    value = (value or "").strip().lstrip("@")
    value = re.sub(r"^https?://", "", value, flags=re.I)
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    value = re.sub(r"[^A-Za-z0-9_.-]", "", value)
    return value[:64]


def handle_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.strip("/").split("/")
    if not path:
        return ""
    if host in {"x.com", "twitter.com", "github.com", "huggingface.co"}:
        # Ignore repository/model paths; a first path component is still a
        # useful candidate only for /in-style social profile URLs below.
        if host == "huggingface.co" and path[0] in {"spaces", "models", "datasets"}:
            return clean_handle(path[1] if len(path) > 1 else "")
        if host == "github.com" and len(path) > 1:
            return clean_handle(path[0])
        return clean_handle(path[0])
    if host == "linkedin.com" and len(path) >= 2 and path[0] in {"in", "company"}:
        return clean_handle(path[1])
    return ""


def social_profiles(*values: str | Iterable[str]) -> dict[str, Any]:
    """Extract usable X/LinkedIn contact profiles from public evidence.

    A profile URL present in a source is considered an explicit contact.  X
    status URLs are normalized to the author's profile; LinkedIn company
    pages are intentionally excluded because they are not a person-level
    outreach contact.  We never invent a profile from a display name.
    """
    found: dict[str, dict[str, str]] = {}
    for value in values:
        items = [value] if isinstance(value, str) else list(value)
        for item in items:
            for raw_url in extract_urls(item):
                parsed = urlparse(raw_url)
                host = (parsed.netloc or "").lower().removeprefix("www.")
                parts = [p for p in parsed.path.strip("/").split("/") if p]
                if host in _X_HOSTS and parts:
                    handle = clean_handle(parts[0])
                    if not handle or handle.casefold() in _SOCIAL_NOISE:
                        continue
                    found.setdefault("x", {
                        "platform": "x",
                        "handle": handle,
                        "url": f"https://x.com/{handle}",
                        "match": "explicit_source",
                        "verification_status": "verified_source_url",
                    })
                elif host in _LINKEDIN_HOSTS and len(parts) >= 2 and parts[0].lower() == "in":
                    handle = clean_handle(parts[1])
                    if not handle:
                        continue
                    found.setdefault("linkedin", {
                        "platform": "linkedin",
                        "handle": handle,
                        "url": f"https://www.linkedin.com/in/{handle}",
                        "match": "explicit_source",
                        "verification_status": "verified_source_url",
                    })
    contacts = list(found.values())
    return {
        "social_contacts": contacts,
        "social_verified": contacts,
        "founder_x": found.get("x", {}).get("url", ""),
        "founder_twitter": found.get("x", {}).get("url", ""),
        "founder_linkedin": found.get("linkedin", {}).get("url", ""),
        "x_url": found.get("x", {}).get("url", ""),
        "linkedin_url": found.get("linkedin", {}).get("url", ""),
    }


def extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s<>\]\)\"']+", text or "")
    return list(dict.fromkeys(u.rstrip(".,;:") for u in urls))


def _github_owner_links(owner: dict[str, Any]) -> list[str]:
    """Fetch the public GitHub user card for its optional X handle."""
    links = []
    twitter = owner.get("twitter_username")
    if twitter:
        links.append(f"https://x.com/{clean_handle(str(twitter))}")
    api_url = owner.get("url")
    if api_url and not twitter:
        try:
            profile = _get_json(api_url)
            twitter = profile.get("twitter_username")
            if twitter:
                links.append(f"https://x.com/{clean_handle(str(twitter))}")
            if profile.get("blog"):
                links.append(str(profile["blog"]))
            if profile.get("bio"):
                links.extend(extract_urls(str(profile["bio"])))
        except Exception:
            pass
    return links


def unwrap_search_url(url: str) -> str:
    """Turn a DuckDuckGo redirect into the actual public result URL."""
    parsed = urlparse(html.unescape(url or ""))
    target = parse_qs(parsed.query).get("uddg", [""])[0]
    return unquote(target) if target else url


def telegram_candidates(*values: str | Iterable[str]) -> list[dict[str, str]]:
    """Return explicit Telegram links plus conservative handle aliases.

    A handle copied from GitHub/X/LinkedIn is only a *candidate* until the
    optional verifier confirms a public Telegram page.  We never turn a name
    such as ``John Smith`` into a guessed Telegram username.
    """
    explicit: list[str] = []
    aliases: list[str] = []
    for value in values:
        if isinstance(value, str):
            items = [value]
        else:
            items = list(value)
        for item in items:
            for url in extract_urls(item):
                if re.search(r"(?:t\.me|telegram\.me|telegram\.dog)/", url, re.I):
                    handle = clean_handle(url.rsplit("/", 1)[-1])
                    if handle:
                        explicit.append(handle)
                else:
                    handle = handle_from_url(url)
                    if handle:
                        aliases.append(handle)
            # Handles are accepted only when the original field is clearly a
            # username, not arbitrary prose or a full person's name.
            if re.fullmatch(r"@?[A-Za-z0-9_][A-Za-z0-9_.-]{2,63}", item.strip()):
                aliases.append(clean_handle(item))
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for handle in explicit:
        key = handle.casefold()
        if key not in seen:
            seen.add(key)
            result.append({"username": handle, "url": f"https://t.me/{handle}", "match": "explicit"})
    for handle in aliases:
        key = handle.casefold()
        if key not in seen:
            seen.add(key)
            result.append({"username": handle, "url": f"https://t.me/{handle}", "match": "same_handle_candidate"})
    return result[:5]


def verify_telegram_candidates(candidates: list[dict[str, str]], *, max_checks: int = 3) -> list[dict[str, str]]:
    """Verify only a few public t.me pages; never require Telegram auth."""
    verified: list[dict[str, str]] = []
    for candidate in candidates[:max_checks]:
        try:
            text = _get_text(candidate["url"])
            low = BeautifulSoup(text, "html.parser").get_text(" ", strip=True).lower() if BeautifulSoup else text.lower()
            # Telegram's public landing page has an open/join marker.  Pages
            # that are clearly not found are not treated as a hit.
            not_found = any(x in low for x in ("username is not found", "page not found", "this page doesn't exist"))
            if not not_found and ("telegram" in low or "tgme_page" in text):
                item = dict(candidate)
                item["verified"] = "1"
                verified.append(item)
        except requests.RequestException:
            continue
        finally:
            time.sleep(0.15)
    return verified


async def verify_telegram_with_telethon(rows: list[dict[str, Any]], session: str | None = None) -> None:
    """Resolve only ranked public candidates through the authenticated TG session.

    Explicit Telegram links are accepted as direct contacts.  Same-handle
    guesses are kept only when Telethon resolves the exact username and the
    shared identity verifier finds either a matching display name or repeated
    non-generic handles across public platforms.  Ambiguous matches stay out
    of the production sheet.
    """
    if not generate_telegram_candidates or not verify_identity_candidates:
        return
    try:
        from telethon import TelegramClient
    except Exception:
        return
    api_id = int(os.environ.get("TG_API_ID", "0") or 0)
    api_hash = os.environ.get("TG_API_HASH", "")
    session = session or os.environ.get("TG_SESSION", "")
    if not api_id or not api_hash or not session:
        log("Telegram identity verification skipped: TG_API_ID/TG_API_HASH/TG_SESSION missing")
        return
    client = TelegramClient(session, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log("Telegram identity verification skipped: session is not authorised")
            return
        for row in rows:
            candidates = generate_telegram_candidates(row, max_candidates=5)
            verified = await verify_identity_candidates(client, row, candidates)
            accepted = []
            for item in verified:
                if not item.verified:
                    continue
                state = candidate_state(row, item)
                state["verification_status"] = item.verification_status
                identity = None
                try:
                    if qualify_identity_candidates:
                        judgments = await qualify_identity_candidates([state])
                        identity = judgments[0] if judgments else None
                except Exception:
                    identity = None
                route = identity_filter(identity) if identity_filter else "keep"
                # Without Jev, exact Telethon verification is still enough for
                # explicit links; inferred matches require a stronger status.
                if route == "drop" or (route == "uncertain" and item.match_type != "explicit_telegram"):
                    continue
                accepted.append({
                    "username": item.telegram_username or item.username,
                    "url": f"https://t.me/{item.telegram_username or item.username}",
                    "match": item.match_type,
                    "verification_status": item.verification_status,
                    "telegram_id": item.telegram_id,
                    "telegram_name": item.telegram_name,
                    "evidence": item.evidence,
                })
            row["telegram_verified"] = accepted[:3]
            if accepted:
                row["identity_verified"] = True
    finally:
        await client.disconnect()


def _base_row(*, name: str, bio: str, source: str, source_url: str, project: str = "", project_url: str = "", links: Iterable[str] = ()) -> dict[str, Any]:
    links = list(dict.fromkeys(u for u in links if u))
    socials = social_profiles(links, source_url, bio)
    row: dict[str, Any] = {
        "id": hashlib.sha256(f"{source}:{source_url}".encode()).hexdigest()[:24],
        "name": name[:160],
        "username": "",
        "bio": bio[:1000],
        "links": links,
        "source": source,
        "source_url": source_url,
        "contact_url": source_url,
        "contact_type": urlparse(source_url).netloc.lower().removeprefix("www."),
        "project": project[:160],
        "project_url": project_url,
        "what_they_do": bio[:220],
        "icp_match": True,
        "telegram_candidates": telegram_candidates(links, bio),
    }
    row.update(socials)
    return row


def github_and_show_hn(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    since = (date.today() - timedelta(days=30)).isoformat()
    try:
        payload = _get_json("https://api.github.com/search/repositories", params={
            "q": f"created:>={since}", "sort": "stars", "order": "desc", "per_page": min(limit, 50),
        })
        for repo in payload.get("items", [])[:limit]:
            owner = repo.get("owner") or {}
            source_url = repo.get("html_url") or ""
            links = [u for u in (repo.get("homepage"), owner.get("html_url"), source_url) if u]
            links.extend(_github_owner_links(owner))
            rows.append(_base_row(
                name=repo.get("name") or owner.get("login") or "GitHub project",
                bio=" | ".join(x for x in (repo.get("description"), " ".join(repo.get("topics") or [])) if x),
                source="github",
                source_url=source_url,
                project=repo.get("name") or "",
                project_url=repo.get("homepage") or source_url,
                links=links,
            ) | {"github_handle": owner.get("login") or "", "github_stars": repo.get("stargazers_count", 0)})
    except Exception as exc:
        log("github discovery failed:", exc)

    try:
        payload = _get_json("https://hn.algolia.com/api/v1/search_by_date", params={
            "tags": "show_hn", "numericFilters": f"created_at_i>{int(time.time()) - 30 * 86400}",
            "hitsPerPage": min(limit, 50),
        })
        for hit in payload.get("hits", [])[:limit]:
            source_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            links = [source_url, f"https://news.ycombinator.com/user?id={hit.get('author')}" if hit.get("author") else ""]
            if hit.get("author"):
                try:
                    profile = _get_json(f"https://hacker-news.firebaseio.com/v0/user/{hit['author']}.json") or {}
                    links.extend(extract_urls(str(profile.get("about") or "")))
                except Exception:
                    pass
            rows.append(_base_row(
                name=hit.get("title") or "Show HN project", bio=hit.get("story_text") or hit.get("title") or "",
                source="show_hn", source_url=source_url, project=hit.get("title") or "", project_url=hit.get("url") or "", links=links,
            ) | {"hn_handle": hit.get("author") or ""})
    except Exception as exc:
        log("Show HN discovery failed:", exc)
    return rows


def huggingface(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for endpoint, source, key in (("spaces", "huggingface_space", "spaces"), ("models", "huggingface_model", "models")):
        try:
            payload = _get_json(f"https://huggingface.co/api/{endpoint}", params={"sort": "createdAt", "direction": -1, "limit": min(limit, 50)})
            for item in payload[:limit]:
                ident = item.get("id") or ""
                url = f"https://huggingface.co/{key}/{ident}" if ident else "https://huggingface.co"
                author = item.get("author") or (ident.split("/", 1)[0] if "/" in ident else "")
                bio = " | ".join(str(x) for x in (item.get("pipeline_tag"), item.get("library_name"), *(item.get("tags") or [])[:5]) if x)
                rows.append(_base_row(name=ident or author or "Hugging Face project", bio=bio, source=source, source_url=url, project=ident, project_url=url, links=[url, f"https://huggingface.co/{author}" if author else ""]) | {"huggingface_handle": author})
        except Exception as exc:
            log(f"Hugging Face {endpoint} discovery failed:", exc)
    return rows


def _parse_store_links(text: str, pattern: str) -> list[str]:
    return list(dict.fromkeys(re.findall(pattern, html.unescape(text))))


def app_stores(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for term in ("AI app", "SaaS", "productivity"):
        try:
            payload = _get_json("https://itunes.apple.com/search", params={"term": term, "entity": "software", "country": "us", "limit": min(limit, 50)})
            for item in payload.get("results", [])[:limit]:
                url = item.get("trackViewUrl") or ""
                rows.append(_base_row(name=item.get("trackName") or "App Store app", bio=item.get("description") or item.get("primaryGenreName") or "", source="app_store", source_url=url, project=item.get("trackName") or "", project_url=item.get("sellerUrl") or url, links=[url, item.get("sellerUrl") or ""]) | {"app_store": "apple", "developer": item.get("artistName") or ""})
        except Exception as exc:
            log("App Store discovery failed:", exc)
        # Three small, focused queries give better coverage than one generic
        # store result page.  ``dedupe`` collapses the occasional overlap.

    try:
        query = quote_plus("AI productivity")
        text = _get_text(f"https://chromewebstore.google.com/search/{query}")
        links = _parse_store_links(text, r"https://chromewebstore\.google\.com/detail/[A-Za-z0-9._%-]+/[A-Za-z0-9_-]+")
        for url in links[:limit]:
            slug = url.rstrip("/").split("/")[-2].replace("-", " ")
            rows.append(_base_row(name=slug.title(), bio="Chrome Web Store extension; AI/productivity", source="chrome_web_store", source_url=url, project=slug.title(), project_url=url, links=[url]) | {"app_store": "chrome"})
    except Exception as exc:
        log("Chrome Web Store discovery failed:", exc)
    return rows


def launch_signals(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    queries = (
        'site:x.com ("just launched" OR "now live" OR "looking for beta users") (AI OR SaaS)',
        'site:linkedin.com/in ("founder" OR "co-founder") ("launched" OR "now live") AI startup',
    )
    for query in queries:
        try:
            text = _get_text("https://html.duckduckgo.com/html/", params={"q": query})
            if BeautifulSoup:
                soup = BeautifulSoup(text, "html.parser")
                result_nodes = soup.select(".result")
                for node in result_nodes[:limit]:
                    anchor = node.select_one("a.result__a")
                    if not anchor or not anchor.get("href"):
                        continue
                    url = unwrap_search_url(anchor["href"])
                    snippet = node.select_one(".result__snippet")
                    bio = snippet.get_text(" ", strip=True) if snippet else anchor.get_text(" ", strip=True)
                    source = "x_launch" if "x.com" in url else "linkedin_launch"
                    rows.append(_base_row(name=anchor.get_text(" ", strip=True), bio=bio, source=source, source_url=url, project_url=url, links=[url]) | {"launch_query": query})
        except Exception as exc:
            log("launch-signal discovery failed:", exc)
    return rows


def dedupe(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = (row.get("project_url") or row.get("source_url") or row.get("name") or "").strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        # Merge same-handle Telegram and social candidates from duplicate source
        # records.  Social URLs are explicit evidence; inferred same-handle
        # aliases are not promoted to contacts here.
        row["telegram_candidates"] = telegram_candidates(row.get("links") or [], row.get("bio") or "", row.get("github_handle") or "", row.get("hn_handle") or "")
        row.update(social_profiles(row.get("links") or [], row.get("source_url") or ""))
        out.append(row)
    return out


async def qualify(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows or qualify_creator_rows is None:
        return rows
    try:
        judgments = await qualify_creator_rows(rows)
    except Exception as exc:
        log("TypeSafe qualification unavailable; keeping candidates:", exc)
        return rows
    out: list[dict[str, Any]] = []
    for row, judgment in zip(rows, judgments):
        row = dict(row)
        if judgment:
            row.update(judgment)
            row["typesafe_route"] = creator_filter(judgment) if creator_filter else "uncertain"
        else:
            row["typesafe_route"] = "uncertain"
        # Do not silently drop uncertain candidates: OpenRouter can review them
        # in the existing pipeline, and a missing AI response must not lose a lead.
        if row["typesafe_route"] != "drop":
            out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--sources", default="github,show_hn,huggingface,app_stores,launch_signals")
    parser.add_argument("--no-qualify", action="store_true")
    parser.add_argument("--verify-telegram", action="store_true")
    parser.add_argument("--verify-telegram-telethon", action="store_true",
                        help="also resolve candidates with the authenticated Telethon session")
    args = parser.parse_args()
    limit = max(1, min(args.limit, 100))
    selected = {x.strip() for x in args.sources.split(",") if x.strip()}
    rows: list[dict[str, Any]] = []
    if selected & {"github", "show_hn"}:
        rows.extend(github_and_show_hn(limit))
    if "huggingface" in selected:
        rows.extend(huggingface(limit))
    if "app_stores" in selected:
        rows.extend(app_stores(limit))
    if "launch_signals" in selected:
        rows.extend(launch_signals(limit))
    rows = dedupe(rows)
    if args.verify_telegram:
        for row in rows:
            verified = verify_telegram_candidates(row.get("telegram_candidates") or [])
            row["telegram_verified"] = verified
            if verified:
                row["telegram_username"] = verified[0]["username"]
                row["telegram_url"] = verified[0]["url"]
    if args.verify_telegram_telethon:
        asyncio.run(verify_telegram_with_telethon(rows))
        for row in rows:
            if not row.get("telegram_verified"):
                # The public t.me page check is only a reachability hint;
                # don't expose it as a verified contact when Telethon was
                # requested and could not confirm the exact identity.
                row.pop("telegram_username", None)
                row.pop("telegram_url", None)
    if not args.no_qualify:
        rows = asyncio.run(qualify(rows))
    with open(args.output, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    log(f"external discovery: {len(rows)} rows -> {args.output}")


if __name__ == "__main__":
    main()
