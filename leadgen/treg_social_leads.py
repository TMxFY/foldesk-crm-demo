#!/usr/bin/env python3
"""Every buyer X query, competitor comments, and one subreddit per evening.

X runs the full buyer list (7-day window), not one phrase of nine. Reddit
still rotates one theme a night. People go into the same enrich -> Jev ->
sheet path. Reddit authors with neither a site nor X are dropped here.
Clippers are dropped before that path. A missing CLI or a failed call writes
nothing and does not fail the Telegram batch.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    from treg_client import call, call_status
except ImportError:
    try:
        from leadgen.treg_client import call, call_status
    except ImportError:
        from scripts.treg_client import call, call_status

try:
    from supply_side import is_supply_side
except ImportError:
    try:
        from leadgen.supply_side import is_supply_side
    except ImportError:
        from scripts.supply_side import is_supply_side

# Reddit stays a single theme inside one subreddit: multi-word search is fuzzy.
# X no longer rotates; buyer_queries() is the whole list every night.
REDDIT_ROTATION = (
    ("SaaS", "UGC"),
    ("startups", "launched"),
    ("microsaas", "ads"),
    ("propfirm", "challenge"),
    ("buildinpublic", "build"),
    ("marketing", "creative"),
    ("Entrepreneur", "growing"),
    ("PaidSocial", "scaling"),
    ("artificial", "agent"),
    ("RobloxDevelopers", "hiring"),
    ("FiveM", "server"),
    ("musicmarketing", "label"),
)
# No bare -clipper: "looking for clippers" is a buyer.
_BUYER_CORE = (
    '"looking for clippers" OR "hiring clippers" OR "need clippers" -portfolio -"no followers needed"',
    '"content rewards" OR "paying clippers" OR "pay per view" (course OR students OR community OR skool OR whop)',
    '"looking for" (UGC OR "short form" OR "video editor") (course OR students OR community OR saas OR "prop firm") -portfolio',
    '("my students" OR "my cohort" OR "my community") ("short form" OR tiktok OR reels OR ugc) -portfolio',
    '"looking for" "UGC creators" -portfolio -"open to work"',
    '"hiring" "UGC creators" -portfolio -"open to work"',
    '"need AI UGC" -portfolio -"open to work"',
    '"scaling paid social" -portfolio -"open to work"',
    '"need more creatives" -portfolio -"open to work"',
    '"hiring content creator" -portfolio -"open to work"',
)

_STRONG_DEMAND = re.compile(
    r"looking for.{0,40}\bugc creators?\b|"
    r"hiring.{0,40}\bugc creators?\b|"
    r"\bneed\b.{0,40}\b(?:ai ugc|ugc creators?)\b|"
    r"\b(?:scaling|scale)\b.{0,40}\bpaid social\b|"
    r"\bneed more creatives\b|"
    r"\bhiring\b.{0,40}\bcontent creators?\b",
    re.I,
)
_COMMENT_BUYER_SIGNAL = re.compile(
    r"\b(?:my|our)\s+(?:app|saas|course|students|cohort|community|brand|product|store|shop|game|studio|company|team|ads|videos|launch|campaigns?|clients?)\b|"
    r"\b(?:looking for|hiring|we need\s+(?:ugc|clippers?|creatives?|videos?|ads?|editors?|traffic|views|content)|need clippers|pay per view|paying clippers|we pay)\b",
    re.I,
)
# Separate from clipper queries: only these rows are marked and noise-filtered.
_GROW_CORE = (
    '"we acquire and grow" roblox',
    '"acquisition studio" (roblox OR fortnite OR UEFN)',
    '"we buy and grow" (roblox OR "fortnite maps")',
)
# Vlad segments. Casino words stay exclusions, not the thing we search for.
_LARGE_CORE = (
    '(roblox OR fortnite) (studio OR "user acquisition" OR game) -portfolio -casino -slots',
    '("record label" OR "music label") (artist OR single) (tiktok OR reels) -portfolio',
    '("gta 6" OR fivem OR "gta rp") (server OR map OR roleplay) -portfolio',
    '("looking for UGC" OR "hiring UGC") (skincare OR cosmetics OR supplement OR vitamin) -portfolio',
    '("tiktok shop") (supplement OR skincare OR cosmetics) (brand OR founder) -portfolio',
    '("record label") ("looking for" OR hiring) ("short form" OR ugc OR editor) -portfolio',
    '(fivem OR "gta rp") (server) (tiktok OR trailer OR "short form") -portfolio',
)
_CLASSIC_CORE = (
    '(supplement OR vitamin OR cosmetics OR skincare) (founder OR brand) (tiktok OR ugc) -portfolio',
    '("fitness app" OR "health app" OR "productivity app") (founder OR launched) -portfolio',
    '(astrology OR "fitness course") (ebook OR course OR guide) (tiktok OR ugc) -portfolio',
    '("household" OR detergent OR "cleaning brand") (founder OR brand) (tiktok OR ugc) -portfolio',
    '("make money" OR "side income") (course OR ebook OR guide) (tiktok OR founder) -portfolio -"open to work"',
)
_NICHE_OK = re.compile(
    r"\broblox\b|\bfortnite\b|\buefn\b|mobile game|creative producer",
    re.I,
)
_OFF_NICHE = re.compile(
    r"\bftl\b|\blogistics\b|\bfreight\b|\btrucking\b|\btrucks?\b|"
    r"фур|gold watch|\brolex\b|whatsapp ceo",
    re.I,
)
# Queries and BRAND_HANDLES share this list so a brand reply cannot become a lead.
COMMENT_ACCOUNTS = (
    "Pamba_ai",
    "karolzdeb",
    "UseFastlane",
    "gauravsbuilding",
    "arcads_ai",
    "rom1trs",
    "Creatify_AI",
    "makeugc",
    "FynCas",
)
BRAND_HANDLES = {handle.casefold() for handle in COMMENT_ACCOUNTS}
X_QUERY_CAP = 20
X_COMMENT_CAP = 30
COMMENT_LIMIT = 30
# Nine accounts times three posts would blow the night. One newest post with replies.
POSTS_PER_BRAND = 1
SEEN_DAYS = 90
SKIP_HOSTS = {
    "t.me", "telegram.me", "google.com", "bit.ly", "tinyurl.com", "linktr.ee",
    "producthunt.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
    "youtu.be", "instagram.com", "facebook.com", "reddit.com", "redd.it",
    "github.com", "notion.so", "notion.site", "medium.com",
    "t.co", "buff.ly", "ow.ly", "cutt.ly", "rb.gy", "lnkd.in",
}
X_NOISE = {"home", "search", "explore", "i", "share", "intent", "hashtag", "compose"}
REDDIT_NOISE = {"automoderator", "deleted", "removed", "none", "[deleted]"}
URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+")
X_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#]|$)",
    re.I,
)


def rotation_for(day: date) -> tuple[str, str]:
    return REDDIT_ROTATION[day.toordinal() % len(REDDIT_ROTATION)]


def buyer_queries(day: date) -> list[str]:
    """Every buyer query for this night, with a 7-day window. Not a rotation."""
    since = (day - timedelta(days=7)).isoformat()
    return [
        f"{query} lang:en since:{since}"
        for query in (*_BUYER_CORE, *_GROW_CORE, *_LARGE_CORE, *_CLASSIC_CORE)
    ]


def off_niche(text: str) -> bool:
    """True for freight, trucking, and watch spam. A named game keeps the text."""
    blob = text or ""
    if _NICHE_OK.search(blob):
        return False
    return _OFF_NICHE.search(blob) is not None


def comment_post_queries(day: date) -> tuple[str, ...]:
    """One fresh-post search per comment account. Not two hardcoded strings."""
    since = (day - timedelta(days=7)).isoformat()
    tail = f"-filter:replies lang:en since:{since}"
    return tuple(f"from:{handle} {tail}" for handle in COMMENT_ACCOUNTS)


def strip_operators(query: str) -> str:
    """Drop since/from/lang/minus-words so a 400 can be retried as plain text."""
    text = re.sub(r'-"[^"]*"', " ", query)
    text = re.sub(
        r"(?<!\w)(?:since|until|from|lang|filter|min_faves|min_retweets|min_replies|-filter):[^\s]+",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"(?<![\w:])-[\w][\w-]*", " ", text)
    text = text.replace("(", " ").replace(")", " ")
    return re.sub(r"\s+", " ", text).strip()


def _minus_terms(query: str) -> list[str]:
    quoted = re.findall(r'-"([^"]+)"', query)
    words = re.findall(r"(?<![\w:])-([A-Za-z][\w-]*)", query)
    skip = {"filter", "is", "has"}
    return [term.casefold() for term in quoted + words if term.casefold() not in skip]


def _since_epoch(query: str) -> int | None:
    match = re.search(r"since:(\d{4}-\d{2}-\d{2})", query)
    if not match:
        return None
    day = date.fromisoformat(match.group(1))
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def fresh_seen(seen: dict[str, str], day: date, keep_days: int = SEEN_DAYS) -> dict[str, str]:
    cutoff = day - timedelta(days=keep_days)
    kept: dict[str, str] = {}
    for key, raw in seen.items():
        try:
            seen_on = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if seen_on >= cutoff:
            kept[str(key)] = seen_on.isoformat()
    return kept


def _live_x_call(query: str) -> tuple[int | None, dict | None]:
    return call_status(
        "anyapi.x.search.posts",
        method="POST",
        data={"query": query, "limit": 20, "queryType": "Latest"},
        timeout=60,
    )


def _keep_post(post: dict, since: int | None, bans: list[str]) -> bool:
    text = _text_of(post).casefold()
    if any(ban in text for ban in bans):
        return False
    created = post.get("createdUtc")
    if since is not None and created not in (None, ""):
        try:
            if int(created) < since:
                return False
        except (TypeError, ValueError):
            pass
    return True


def filter_x_payload(payload: dict, query: str) -> dict:
    """Apply since: and minus-words in code after a bare-query retry."""
    since = _since_epoch(query)
    bans = _minus_terms(query)

    def walk(node):
        if isinstance(node, list):
            dicts = [item for item in node if isinstance(item, dict)]
            if dicts and any(_looks_like_post(item) for item in dicts[:8]):
                return [item for item in node if not isinstance(item, dict) or _keep_post(item, since, bans)]
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    walked = walk(copy.deepcopy(payload))
    return walked if isinstance(walked, dict) else payload


def search_x(query: str, *, call_fn=None) -> dict | None:
    """Search as written. On HTTP 400, buyer queries retry without operators.

    A comment query keeps from:. Stripping it would search the whole site and
    reply_post_urls could open someone else's thread. That account is skipped.
    """
    caller = call_fn or _live_x_call
    status, payload = caller(query)
    if status == 400 and re.search(r"(?<!\w)from:", query, flags=re.I):
        print("social: x search 400; skip from: query", flush=True)
        return None
    if status == 400:
        print("social: x search 400; retry without operators", flush=True)
        status, payload = caller(strip_operators(query))
        if status == 400 or not isinstance(payload, dict):
            print("social: x search fallback failed", flush=True)
            return None
        return filter_x_payload(payload, query)
    if not isinstance(payload, dict) or (isinstance(status, int) and status >= 400):
        return None
    return payload


def search_comments(url: str, *, call_fn=None) -> dict | None:
    """Replies on one post. Body is only the catalog fields: url, limit."""
    data = {"url": url, "limit": COMMENT_LIMIT}
    caller = call_fn or call
    payload = caller(
        "anyapi.x.post.comments",
        method="POST",
        data=data,
        timeout=60,
    )
    return payload if isinstance(payload, dict) else None


def search_reddit(subreddit: str, query: str) -> dict | None:
    return call(
        "scrapecreators.x.v1-reddit-subreddit-search",
        query={
            "subreddit": subreddit,
            "query": query,
            "sort": "top",
            "timeframe": "week",
        },
        timeout=60,
    )


def _host(url: str) -> str:
    raw = url if "://" in url else "https://" + url
    return (urlparse(raw).hostname or "").lower().removeprefix("www.")


def _site(url: str) -> str:
    host = _host(url)
    if not host or "." not in host:
        return ""
    if any(host == skip or host.endswith("." + skip) for skip in SKIP_HOSTS):
        return ""
    return url.rstrip(".,;:)")


def _x_handle(url: str) -> str:
    match = X_RE.search(url or "")
    if not match:
        return ""
    handle = match.group(1)
    if handle.casefold() in X_NOISE:
        return ""
    return handle


def _urls(*chunks: object) -> list[str]:
    found: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, str):
            continue
        for raw in URL_RE.findall(chunk):
            url = raw.rstrip(".,;:)")
            if url not in found:
                found.append(url)
    return found


def _posts(payload: dict | None) -> list[dict]:
    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            dicts = [item for item in node if isinstance(item, dict)]
            if dicts and any(_looks_like_post(item) for item in dicts[:8]):
                found.extend(dicts)
                return
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(payload or {})
    return found


def _looks_like_post(item: dict) -> bool:
    return any(key in item for key in (
        "text", "full_text", "selftext", "title", "body",
        "authorUsername", "author", "author_username",
    ))


def _clean_handle(value: object, *, kind: str) -> str:
    text = str(value or "").strip().lstrip("@")
    if not text or " " in text or text.startswith("http"):
        return ""
    if kind == "x":
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", text) or text.casefold() in X_NOISE:
            return ""
        return text
    if text.casefold() in REDDIT_NOISE or not re.fullmatch(r"[A-Za-z0-9_-]{3,30}", text):
        return ""
    return text


def _author_name(post: dict, handle: str) -> str:
    for key in ("authorName", "author_name"):
        text = str(post.get(key) or "").strip()
        if text and not text.startswith("http"):
            return text[:160]
    author = post.get("author")
    if isinstance(author, dict):
        text = str(author.get("name") or "").strip()
        if text:
            return text[:160]
    return handle


def _text_of(post: dict) -> str:
    parts = []
    for key in ("title", "text", "full_text", "selftext", "body"):
        value = post.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " ".join(parts)


def _substance(text: str) -> str:
    return re.sub(r"\s+", " ", URL_RE.sub(" ", text)).strip()


def _project_name(site: str) -> str:
    host = _host(site)
    return host.split(".")[0][:160] if host else ""


def _row(*, source: str, author_key: str, name: str, text: str, source_url: str,
         x_url: str, site: str, group: str) -> dict:
    handle = author_key.split(":", 1)[-1]
    social = []
    if x_url:
        social.append({
            "platform": "x",
            "url": x_url,
            "handle": _x_handle(x_url) or handle,
        })
    bio = re.sub(r"\s+", " ", text).strip()[:1000]
    return {
        "id": hashlib.sha256(f"{source}:{author_key}".encode()).hexdigest()[:24],
        "name": (name or handle)[:160],
        "username": "",
        "bio": bio,
        "sample": bio[:280],
        "links": [item for item in (site, x_url) if item],
        "source": source,
        "source_url": source_url or x_url or site,
        "group": group,
        "contact_url": x_url or site or source_url,
        "project": _project_name(site),
        "project_url": site,
        "what_they_do": bio[:220],
        "icp_match": True,
        "lead_tier": "A" if _STRONG_DEMAND.search(text) else "B",
        "demand_signal": "ugc_creative_demand" if _STRONG_DEMAND.search(text) else "buyer_intent",
        "founder_x": x_url,
        "founder_twitter": x_url,
        "social_verified": social,
        "social_contacts": social,
        "telegram_candidates": [],
    }


def _take(candidates: list[tuple[str, dict | None]], *, seen: set[str], cap: int) -> tuple[list[dict], set[str]]:
    """Keep up to `cap` new leads. Remember rejects and the leads we kept.

    Authors past the cap stay unseen, so a later night can still pick them up
    when this query rotates back.
    """
    rows: list[dict] = []
    remember: set[str] = set()
    local: set[str] = set()
    for key, row in candidates:
        if key in seen or key in local:
            continue
        local.add(key)
        if row is None:
            remember.add(key)
            continue
        if len(rows) >= cap:
            continue
        rows.append(row)
        remember.add(key)
    return rows, remember


def x_candidates(payload: dict | None) -> list[tuple[str, dict | None]]:
    out: list[tuple[str, dict | None]] = []
    for post in _posts(payload):
        if post.get("isReply") is True:
            continue
        handle = _clean_handle(
            post.get("authorUsername") or post.get("author_username") or post.get("username"),
            kind="x",
        )
        if not handle:
            author = post.get("author") or post.get("user") or {}
            if isinstance(author, dict):
                handle = _clean_handle(author.get("username") or author.get("screen_name"), kind="x")
            elif isinstance(author, str):
                handle = _clean_handle(author, kind="x")
        if not handle:
            continue
        text = _text_of(post)
        urls = _urls(text, post.get("url"))
        site = next((_site(url) for url in urls if _site(url)), "")
        key = "x:" + handle.casefold()
        # Search posts only. A short post with no site is noise and stays seen.
        # Comment authors go through comment_candidates and are not burned here.
        if not site and len(_substance(text)) < 40:
            out.append((key, None))
            continue
        x_url = f"https://x.com/{handle}"
        status = str(post.get("url") or "")
        if "x.com/" not in status and "twitter.com/" not in status:
            status = x_url
        out.append((key, _row(
            source="treg_x", author_key=key, name=_author_name(post, handle),
            text=text, source_url=status, x_url=x_url, site=site, group="x",
        )))
    return out


def reddit_candidates(payload: dict | None, subreddit: str) -> list[tuple[str, dict | None]]:
    out: list[tuple[str, dict | None]] = []
    for post in _posts(payload):
        author = post.get("author")
        if isinstance(author, dict):
            author = author.get("name") or author.get("username")
        handle = _clean_handle(author, kind="reddit")
        if not handle:
            continue
        text = _text_of(post)
        urls = _urls(text, post.get("url"), post.get("permalink"))
        site = next((_site(url) for url in urls if _site(url)), "")
        x_url = ""
        for url in urls:
            x_handle = _x_handle(url)
            if x_handle:
                x_url = f"https://x.com/{x_handle}"
                break
        key = "reddit:" + handle.casefold()
        if not site and not x_url:
            out.append((key, None))
            continue
        permalink = str(post.get("permalink") or "")
        if permalink.startswith("/"):
            permalink = "https://www.reddit.com" + permalink
        elif not permalink.startswith("http"):
            raw_url = str(post.get("url") or "")
            permalink = raw_url if "reddit.com" in raw_url else ""
        out.append((key, _row(
            source="treg_reddit", author_key=key, name=handle, text=text,
            source_url=permalink, x_url=x_url, site=site, group=f"r/{subreddit}",
        )))
    return out


def collect(*, day: date, seen: dict[str, str], cap: int,
            x_payload: dict | None, reddit_payload: dict | None,
            subreddit: str) -> tuple[list[dict], dict[str, str]]:
    """Turn the two payloads into lead rows and an updated seen map.

    A missing payload remembers nobody from that source: the call failed, and
    the same posts should be eligible tomorrow.
    """
    current = fresh_seen(seen, day)
    stamp = day.isoformat()
    rows: list[dict] = []
    if x_payload is not None:
        picked, remember = _take(x_candidates(x_payload), seen=set(current), cap=cap)
        rows.extend(picked)
        for key in remember:
            current[key] = stamp
    if reddit_payload is not None:
        picked, remember = _take(
            reddit_candidates(reddit_payload, subreddit), seen=set(current), cap=cap,
        )
        rows.extend(picked)
        for key in remember:
            current[key] = stamp
    return rows, current


def _x_author(post: dict) -> str:
    handle = _clean_handle(
        post.get("authorHandle") or post.get("authorUsername")
        or post.get("author_username") or post.get("username"),
        kind="x",
    )
    if handle:
        return handle
    author = post.get("author") or post.get("user") or {}
    if isinstance(author, dict):
        return _clean_handle(author.get("username") or author.get("screen_name"), kind="x")
    if isinstance(author, str):
        return _clean_handle(author, kind="x")
    return ""


def comment_candidates(payload: dict | None) -> list[tuple[str, dict | None]]:
    """Authors of comments. Not x_candidates: replies are the point here.

    The brand and the founder are skipped. Supply-side pitches are rejects.
    """
    out: list[tuple[str, dict | None]] = []
    for post in _posts(payload):
        handle = _x_author(post)
        if not handle or handle.casefold() in BRAND_HANDLES:
            continue
        text = _text_of(post)
        key = "x:" + handle.casefold()
        urls = _urls(text)
        site = next((_site(url) for url in urls if _site(url)), "")
        if is_supply_side(text):
            out.append((key, None))
            continue
        # The 40-character seen mark belongs to search posts (x_candidates).
        # A short Pamba comment has no site; forgetting the author would block
        # a later comment that is actually a buyer.
        if not site and len(_substance(text)) < 40:
            continue
        if not site and not _COMMENT_BUYER_SIGNAL.search(text) and not _STRONG_DEMAND.search(text):
            continue
        x_url = f"https://x.com/{handle}"
        status = str(post.get("url") or "")
        if "x.com/" not in status and "twitter.com/" not in status:
            status = x_url
        out.append((key, _row(
            source="treg_x", author_key=key, name=_author_name(post, handle),
            text=text, source_url=status, x_url=x_url, site=site, group="x_comment",
        )))
    return out


def reply_post_urls(payload: dict | None, *, limit: int = POSTS_PER_BRAND) -> list[str]:
    urls: list[str] = []
    for post in _posts(payload):
        if post.get("isReply") is True:
            continue
        try:
            replies = int(post.get("replyCount") if post.get("replyCount") is not None else post.get("reply_count") or 0)
        except (TypeError, ValueError):
            replies = 0
        if replies <= 0:
            continue
        url = str(post.get("url") or "")
        if not url.startswith("http") or "status" not in url:
            continue
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _comment_supply_keys(payload: dict | None) -> set[str]:
    keys: set[str] = set()
    for post in _posts(payload):
        handle = _x_author(post)
        if not handle or handle.casefold() in BRAND_HANDLES:
            continue
        if is_supply_side(_text_of(post)):
            keys.add("x:" + handle.casefold())
    return keys


def _env_cap(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def format_social_log(stats: dict) -> str:
    line = (
        f"social: posts={stats['posts']} filtered={stats['filtered']} "
        f"comments={stats['comments']} supply={stats['supply']} admitted={stats['admitted']}"
    )
    if stats.get("x_call_failed"):
        return line + " x_call_failed"
    if stats.get("errors"):
        return line + f" errors={stats['errors']}"
    return line


def discover(*, day: date, seen: dict[str, str], cap: int,
             search_x_fn=search_x, search_reddit_fn=search_reddit,
             search_comments_fn=search_comments, x_cap: int = X_QUERY_CAP,
             comment_cap: int = X_COMMENT_CAP) -> tuple[list[dict], dict[str, str], dict]:
    """All buyer queries plus competitor comments. Reddit stays on its own rotation.

    `cap` is the Reddit cap (TREG_SOCIAL_CAP). X keywords and comments have
    their own caps and do not share a budget.
    """
    current = fresh_seen(seen, day)
    stamp = day.isoformat()
    rows: list[dict] = []
    posts = 0
    filtered = 0
    comments = 0
    supply_keys: set[str] = set()
    x_ok = 0
    x_fail = 0
    errors = 0
    x_cands: list[tuple[str, dict | None]] = []
    for query in buyer_queries(day):
        payload = search_x_fn(query)
        if payload is None:
            x_fail += 1
            errors += 1
            continue
        x_ok += 1
        raw = _posts(payload)
        posts += len(raw)
        filtered += sum(1 for post in raw if post.get("isReply") is True)
        grow = any(core in query for core in _GROW_CORE)
        large = any(core in query for core in _LARGE_CORE)
        classic = any(core in query for core in _CLASSIC_CORE)
        for key, row in x_candidates(payload):
            if row is None:
                filtered += 1
                x_cands.append((key, None))
                continue
            if is_supply_side(f"{row.get('name') or ''} {row.get('bio') or ''}"):
                supply_keys.add(key)
                x_cands.append((key, None))
                continue
            # Clipper leads stay even if the bio says truck. Only grow queries.
            if grow and off_niche(str(row.get("bio") or "")):
                filtered += 1
                x_cands.append((key, None))
                continue
            if grow:
                row["buyer_lane"] = "grow"
            elif large:
                row["buyer_lane"] = "large"
            elif classic:
                row["buyer_lane"] = "classic"
            x_cands.append((key, row))
    picked, remember = _take(x_cands, seen=set(current), cap=x_cap)
    rows.extend(picked)
    for key in remember:
        current[key] = stamp

    comment_cands: list[tuple[str, dict | None]] = []
    for query in comment_post_queries(day):
        payload = search_x_fn(query)
        if payload is None:
            x_fail += 1
            errors += 1
            continue
        x_ok += 1
        for url in reply_post_urls(payload):
            thread = search_comments_fn(url)
            if thread is None:
                errors += 1
                continue
            comments += len(_posts(thread))
            supply_keys.update(_comment_supply_keys(thread))
            comment_cands.extend(comment_candidates(thread))
    picked, remember = _take(comment_cands, seen=set(current), cap=comment_cap)
    rows.extend(picked)
    for key in remember:
        current[key] = stamp

    subreddit, theme = rotation_for(day)
    reddit_payload = search_reddit_fn(subreddit, theme)
    if reddit_payload is None:
        errors += 1
    else:
        picked, remember = _take(
            reddit_candidates(reddit_payload, subreddit), seen=set(current), cap=cap,
        )
        rows.extend(picked)
        for key in remember:
            current[key] = stamp

    stats = {
        "posts": posts,
        "filtered": filtered,
        "comments": comments,
        "supply": len(supply_keys),
        "admitted": len(rows),
        "errors": errors,
    }
    if x_fail and not x_ok:
        stats["x_call_failed"] = True
    return rows, current, stats


def _load_seen(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    authors = raw.get("authors") if isinstance(raw, dict) else None
    return authors if isinstance(authors, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--seen", required=True)
    parser.add_argument("--day", default="", help="YYYY-MM-DD, defaults to today")
    args = parser.parse_args()
    day = date.fromisoformat(args.day) if args.day else date.today()
    cap = max(0, int(os.getenv("TREG_SOCIAL_CAP", "20")))
    seen_path = Path(args.seen)
    before = _load_seen(seen_path)
    try:
        rows, updated, stats = discover(
            day=day, seen=before, cap=cap,
            x_cap=_env_cap("X_QUERY_CAP", X_QUERY_CAP),
            comment_cap=_env_cap("X_COMMENT_CAP", X_COMMENT_CAP),
        )
    except Exception as exc:
        print(f"social: posts=0 filtered=0 comments=0 supply=0 admitted=0 x_call_failed ({exc.__class__.__name__})", flush=True)
        rows, updated = [], before
    else:
        print(format_social_log(stats), flush=True)
    Path(args.output).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    )
    if updated != before:
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps({"authors": updated}, ensure_ascii=False))


if __name__ == "__main__":
    main()
