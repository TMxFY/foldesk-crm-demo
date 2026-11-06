#!/usr/bin/env python3
"""Public catalog pages, one request each. A dead source is skipped.

Rows keep a real http(s) site that is not a social host. Casino wording and
.ru/.рф hosts are dropped. An empty body does not fail the batch.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from urllib.parse import urljoin, urlparse

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from lead_market import market_of
except ImportError:
    try:
        from leadgen.lead_market import market_of
    except ImportError:
        from scripts.lead_market import market_of

CAP = 8
TIMEOUT = 8
SOURCES = (
    ("devforum", "https://devforum.roblox.com/c/collaboration/recruitment/82.json"),
    ("fivem", "https://fivem-server.org/servers/english"),
    ("gta", "https://6charts.com/gta-6-servers"),
    ("groover", "https://groover.co/en/influencer/list/tags/label/"),
    ("gumroad", "https://gumroad.com/discover?query=fitness"),
)

_UA = {"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/json"}
_CASINO = re.compile(r"casino|slots|betting", re.I)
_DEVFORUM_SKIP = re.compile(
    r"\[closed\]|\[volunteer|\bvolunteer\b|\bunpaid\b|looking for game testers",
    re.I,
)
_OPENASO_MEGACORP = re.compile(
    r"\b(?:chatgpt|claude|gmail|google|grok|strava|myfitnesspal|flo cycle)\b",
    re.I,
)
_HTTP = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_EMAIL = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
_X = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", re.I)
_LI = re.compile(r"https?://(?:[\w.]+\.)?linkedin\.com/in/([A-Za-z0-9\-_%]+)", re.I)
_TG = re.compile(r"https?://(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,32})", re.I)
_DOMAIN = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+[a-z]{2,24}\b",
    re.I,
)
_APP = re.compile(
    r'"name"\s*:\s*"((?:\\.|[^"\\]){2,160})"\s*,\s*"url"\s*:\s*"(https://openaso\.ai/apps/\d+)"'
)
_GUM = re.compile(
    r'"permalink"\s*:\s*"[^"]+"\s*,\s*"name"\s*:\s*"((?:\\.|[^"\\]){2,160})"'
    r'.{0,2500}?"url"\s*:\s*"(https://[^"]+?/l/[^"]+)"',
    re.S,
)
_ASSET = (".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".svg", ".woff", ".woff2", ".ico", ".webp", ".xml", ".json")
_SOCIAL_HOSTS = (
    "x.com", "twitter.com", "linkedin.com", "instagram.com", "t.me",
    "telegram.me", "telegram.dog", "youtube.com", "youtu.be", "linktr.ee",
    "linktree.com", "facebook.com", "fb.com", "medium.com", "calendly.com",
    "notion.site", "notion.so", "github.com", "discord.gg", "discord.com",
    "gmail.com", "beacons.ai", "solo.to", "carrd.co", "wa.me",
    "whatsapp.com", "api.whatsapp.com", "tiktok.com", "reddit.com",
    "pinterest.com", "threads.net",
)
_SKIP_HOSTS = _SOCIAL_HOSTS + (
    "schema.org", "w3.org", "google.com", "gstatic.com", "googleapis.com",
    "cloudflare.com", "cloudflareinsights.com", "googletagmanager.com",
    "doubleclick.net", "sentry.io", "gravatar.com", "foundation.roblox.com",
    "rbxcdn.com", "create.roblox.com", "corp.roblox.com", "en.help.roblox.com",
    "jsdelivr.net", "unpkg.com", "cookielaw.org", "onetrust.com",
    "fivem.net", "cfx.re", "discourse.org", "stripe.com", "paypal.com",
    "vimeo.com",
)
_X_DENY = {"home", "share", "intent", "i", "search", "explore", "hashtag", "compose"}
_TG_DENY = {"joinchat", "share", "addstickers", "proxy", "iv", "s"}
_NAME_DENY = {"home", "about", "login", "sign in", "charts", "news", "menu"}


def _host(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    return (urlparse(raw).hostname or "").casefold().removeprefix("www.").rstrip(".")


def _social_host(host: str) -> bool:
    return any(host == item or host.endswith("." + item) for item in _SOCIAL_HOSTS)


def is_public_site(url: str) -> str:
    """http(s) site that is not a social host and not .ru/.рф. Else empty."""
    raw = (url or "").strip().rstrip(".,);\"'")
    if not raw.lower().startswith(("http://", "https://")):
        return ""
    host = _host(raw)
    if not host or "." not in host or _social_host(host):
        return ""
    if host == "roblox.com" or any(host == item or host.endswith("." + item) for item in _SKIP_HOSTS):
        return ""
    path = (urlparse(raw).path or "").lower()
    if path.endswith(_ASSET):
        return ""
    if market_of(raw, "") == "ru":
        return ""
    return raw


def _ok_site(url: str, page_url: str) -> str:
    found = is_public_site(url)
    if not found:
        return ""
    if _host(found) == _host(page_url) and (urlparse(found).path or "") in {"", "/"}:
        return ""
    return found


def _best_site(candidates, page_url: str) -> str:
    found = []
    for candidate in candidates:
        ok = _ok_site(candidate, page_url)
        if ok and ok not in found:
            found.append(ok)
    if not found:
        return ""
    page_host = _host(page_url)
    for item in found:
        if _host(item) != page_host:
            return item
    return found[0]


def _json_piece(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def _contacts(blob: str) -> dict:
    founder_x = ""
    for match in _X.finditer(blob or ""):
        handle = match.group(1)
        if handle.casefold() in _X_DENY:
            continue
        founder_x = f"https://x.com/{handle}"
        break
    founder_li = ""
    match = _LI.search(blob or "")
    if match:
        slug = match.group(1).strip("._-")
        if slug:
            founder_li = f"https://www.linkedin.com/in/{slug}"
    username = ""
    tg_url = ""
    for match in _TG.finditer(blob or ""):
        nick = match.group(1)
        if nick.casefold() in _TG_DENY:
            continue
        username = nick
        tg_url = f"https://t.me/{nick}"
        break
    email = ""
    for match in _EMAIL.finditer(blob or ""):
        addr = match.group(0).strip("._-")
        local, _, domain = addr.partition("@")
        if not local or local.casefold() in {"noreply", "no-reply"}:
            continue
        if domain.casefold().endswith((".png", ".jpg", ".jpeg")):
            continue
        email = addr
        break
    return {
        "founder_x": founder_x,
        "founder_linkedin": founder_li,
        "username": username,
        "work_email": email,
        "tg_url": tg_url,
    }


def _row(source: str, page_url: str, name: str, blob: str, site: str) -> dict | None:
    name = " ".join((name or "").split())[:160]
    if len(name) < 2 or name.casefold() in _NAME_DENY:
        return None
    blob = blob or ""
    if _CASINO.search(name) or _CASINO.search(blob) or (site and _CASINO.search(site)):
        return None
    contacts = _contacts(blob)
    chosen = _ok_site(site, page_url) if site else ""
    if not chosen:
        chosen = _best_site(_HTTP.findall(blob), page_url)
    if not chosen and not any(
        contacts[key] for key in ("founder_x", "founder_linkedin", "username", "work_email")
    ):
        return None
    links = []
    for item in (chosen, contacts["founder_x"], contacts["founder_linkedin"], contacts["tg_url"]):
        if item and item not in links:
            links.append(item)
    return {
        "name": name,
        "project": name,
        "project_url": chosen,
        "source": f"catalog_{source}",
        "text": page_url,
        "founder_x": contacts["founder_x"],
        "founder_linkedin": contacts["founder_linkedin"],
        "username": contacts["username"],
        "work_email": contacts["work_email"],
        "links": links,
    }


def _limit(rows: list[dict]) -> list[dict]:
    seen = set()
    kept = []
    for row in rows:
        key = (row.get("project_url") or row.get("name") or "").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(row)
        if len(kept) >= CAP:
            break
    return kept


def _parse_discourse(source: str, data: dict, page_url: str) -> list[dict]:
    topics = (data.get("topic_list") or {}).get("topics") or []
    rows = []
    if not isinstance(topics, list):
        return rows
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        if topic.get("pinned") or topic.get("pinned_globally"):
            continue
        title = str(topic.get("title") or "")
        excerpt = str(topic.get("excerpt") or "")
        if _DEVFORUM_SKIP.search(title) or _DEVFORUM_SKIP.search(excerpt):
            continue
        featured = str(topic.get("featured_link") or "")
        blob = "\n".join(part for part in (title, excerpt, featured) if part)
        site = ""
        for candidate in _HTTP.findall(featured + "\n" + excerpt):
            ok = _ok_site(candidate, page_url)
            if ok and _host(ok) != _host(page_url):
                site = ok
                break
        if not site:
            slug, tid = topic.get("slug"), topic.get("id")
            if slug and tid:
                site = f"https://devforum.roblox.com/t/{slug}/{tid}"
        row = _row(source, page_url, title, blob, site)
        if row:
            rows.append(row)
    return rows


def _cards(soup, source: str, page_url: str) -> list[dict]:
    rows = []
    for card in soup.select("article.card, article.catalog-card"):
        link = card.select_one("h3 a[href], h2 a[href], a[href]")
        if link is None:
            continue
        name = link.get_text(" ", strip=True)
        hrefs = [urljoin(page_url, link.get("href") or "")]
        hrefs.extend(urljoin(page_url, a.get("href") or "") for a in card.find_all("a", href=True))
        blob = card.get_text(" ", strip=True)
        row = _row(source, page_url, name, blob, _best_site(hrefs, page_url))
        if row:
            rows.append(row)
    return rows


def _manufacturers(soup, source: str, page_url: str) -> list[dict]:
    rows = []
    for head in soup.find_all(["h2", "h3"]):
        block = head.find_parent("li")
        if block is None:
            continue
        blob = block.get_text(" ", strip=True)
        domains = []
        for match in _DOMAIN.finditer(blob):
            domains.append("https://" + match.group(0))
        site = _best_site(domains, page_url)
        if not site or _host(site) == _host(page_url):
            continue
        row = _row(source, page_url, head.get_text(" ", strip=True), blob, site)
        if row:
            rows.append(row)
    return rows


def _groover(soup, source: str, page_url: str) -> list[dict]:
    rows = []
    seen = set()
    for link in soup.select("a[href*='/influencer/profile/']"):
        href = urljoin(page_url, link.get("href") or "")
        if href in seen:
            continue
        seen.add(href)
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"^\d+\.", "", slug)
        name = re.sub(r"[-_.]+", " ", slug).strip()
        card = link.find_parent(class_="catalog-card") or link
        blob = card.get_text(" ", strip=True)
        external = _best_site(_HTTP.findall(blob), page_url)
        if external and _host(external) == _host(page_url):
            external = ""
        row = _row(source, page_url, name, blob, external or href)
        if row:
            rows.append(row)
    return rows


def _named_links(soup, source: str, page_url: str) -> list[dict]:
    rows = []
    for link in soup.select("ol li a[href]"):
        name = link.get_text(" ", strip=True)
        href = urljoin(page_url, link.get("href") or "")
        row = _row(source, page_url, name, name, href)
        if row:
            rows.append(row)
    return rows


def _sephora_brands(soup, source: str, page_url: str) -> list[dict]:
    lists = []
    for ordered in soup.find_all("ol"):
        names = []
        for item in ordered.find_all("li", recursive=False):
            name = item.get_text(" ", strip=True)
            if 2 <= len(name) <= 40 and not name.casefold().startswith("all "):
                names.append(name)
        if len(names) >= 8:
            lists.append(names)
    if not lists:
        return []
    rows = []
    for name in max(lists, key=len):
        query = requests.utils.quote(name)
        site = f"https://www.sephora.com/search?keyword={query}"
        row = _row(source, page_url, name, name, site)
        if row:
            rows.append(row)
    return rows


def _openaso(source: str, body: str, page_url: str) -> list[dict]:
    rows = []
    for match in _APP.finditer(body or ""):
        name = _json_piece(match.group(1))
        if _OPENASO_MEGACORP.search(name):
            continue
        row = _row(source, page_url, name, name, match.group(2))
        if row:
            rows.append(row)
    return rows


def _gumroad(source: str, body: str, page_url: str) -> list[dict]:
    text = html.unescape(body or "").replace("\\/", "/").replace("\\u0026", "&")
    rows = []
    for match in _GUM.finditer(text):
        name = _json_piece(match.group(1))
        url = html.unescape(match.group(2))
        row = _row(source, page_url, name, name, url)
        if row:
            rows.append(row)
    return rows


def parse_catalog(source: str, body, page_url: str) -> list[dict]:
    """Name and link rows from one catalog body. No network."""
    if body is None:
        return []
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    text = str(body)
    if not text.strip():
        return []
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "topic_list" in data:
            return _limit(_parse_discourse(source, data, page_url))
    rows = []
    if BeautifulSoup is not None and "<" in text:
        soup = BeautifulSoup(text, "html.parser")
        rows.extend(_cards(soup, source, page_url))
        if source == "manufacturers":
            rows.extend(_manufacturers(soup, source, page_url))
        if source == "groover":
            rows.extend(_groover(soup, source, page_url))
    if source == "gta" and BeautifulSoup is not None and "<" in text:
        rows.extend(_named_links(BeautifulSoup(text, "html.parser"), source, page_url))
    if source == "sephora" and BeautifulSoup is not None and "<" in text:
        rows.extend(_sephora_brands(BeautifulSoup(text, "html.parser"), source, page_url))
    if source.startswith("openaso"):
        rows.extend(_openaso(source, text, page_url))
    if source == "gumroad":
        rows.extend(_gumroad(source, text, page_url))
    return _limit(rows)


def _http_get(url: str) -> str:
    response = requests.get(url, headers=_UA, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text or ""


_DETAIL_MARKERS = (
    "fivem-server.org/server/",
    "groover.co/",
    "devforum.roblox.com/t/",
    "gumroad.com/l/",
)


def _is_detail_card_url(url: str) -> bool:
    low = (url or "").lower()
    if any(marker in low for marker in _DETAIL_MARKERS):
        return True
    return "gumroad.com" in low and "/l/" in low


def _enrich_detail_row(row: dict, getter) -> dict:
    if any(row.get(k) for k in ("founder_x", "founder_linkedin", "username", "work_email")):
        return row
    card_url = str(row.get("project_url") or "")
    if not _is_detail_card_url(card_url):
        return row
    try:
        detail_body = getter(card_url)
    except Exception:
        return row
    if not detail_body:
        return row
    hrefs = []
    text_blob = detail_body
    if BeautifulSoup is not None and "<" in detail_body:
        soup = BeautifulSoup(detail_body, "html.parser")
        for tag in soup.find_all(["head", "script", "style", "link", "noscript"]):
            tag.decompose()
        hrefs = [urljoin(card_url, a.get("href") or "") for a in soup.find_all("a", href=True)]
        text_blob = soup.get_text(" ", strip=True) + "\n" + "\n".join(hrefs)
    contacts = _contacts(text_blob)
    for key in ("founder_x", "founder_linkedin", "username", "work_email"):
        if contacts.get(key) and not row.get(key):
            row[key] = contacts[key]
    candidates = hrefs + _HTTP.findall(text_blob)
    ext_site = _best_site(candidates, card_url)
    if ext_site and _host(ext_site) != _host(card_url):
        row["project_url"] = ext_site
    links = list(row.get("links") or [])
    for item in (row.get("project_url"), contacts.get("founder_x"), contacts.get("founder_linkedin"), contacts.get("tg_url")):
        if item and item not in links:
            links.append(item)
    row["links"] = links
    return row


def collect(fetch=None) -> list[dict]:
    """One short request per catalog. A failed or empty source is skipped."""
    getter = fetch or _http_get
    found = []
    for name, url in SOURCES:
        try:
            body = getter(url)
        except Exception:
            continue
        try:
            rows = parse_catalog(name, body, url)
        except Exception:
            continue
        if fetch is None:
            rows = [_enrich_detail_row(row, getter) for row in rows]
        found.extend(rows)
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        rows = collect()
    except Exception:
        rows = []
    with open(args.output, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"buyer catalogs: {len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
