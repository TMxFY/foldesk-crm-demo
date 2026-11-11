#!/usr/bin/env python3
"""Parse fresh startups from YC Directory + Product Hunt + BetaList + MicroLaunch
+ Uneed + Indie Hackers, find founders, check Telegram by username,
append to Google Sheet 'Все2'.

Sources:
  - YC: Algolia API (public key from ycombinator.com page) + company pages (Inertia data-page JSON)
  - PH: firecrawl scrape of homepage + product/makers pages
  - BetaList / MicroLaunch / Uneed / Indie Hackers: bs4 HTML parsing,
    plain requests with firecrawl fallback when the page is JS-rendered

Output: /tmp/yc_ph_leads.jsonl + rows appended to sheet.
Dedup: .tg_pipeline/yc_ph_seen.json + existing names/sites in the sheet.
"""
import argparse
import html as html_mod
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

try:
    from relay_route import chain, note_failure
except ImportError:
    try:
        from leadgen.relay_route import chain, note_failure
    except ImportError:
        from scripts.relay_route import chain, note_failure

try:
    from outreach_copy import (
        REVSHARE_OFFER_TEXT,
        SOFTUGC_PITCH_TEXT,
        first_touch as template_first_touch,
        upgrade_to_vlad_step1,
    )
except ImportError:
    try:
        from leadgen.outreach_copy import (
            REVSHARE_OFFER_TEXT,
            SOFTUGC_PITCH_TEXT,
            first_touch as template_first_touch,
            upgrade_to_vlad_step1,
        )
    except ImportError:
        from scripts.outreach_copy import (
            REVSHARE_OFFER_TEXT,
            SOFTUGC_PITCH_TEXT,
            first_touch as template_first_touch,
            upgrade_to_vlad_step1,
        )

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.environ.get("LEADGEN_STATE_DIR") or (
    "/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline"
    if os.path.isdir("/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline")
    else os.path.join(ROOT, "data", "leadgen_state")
)
SEEN_PATH = os.path.join(STATE_DIR, "yc_ph_seen.json")
OUT_JSONL = "/tmp/yc_ph_leads.jsonl"
PENDING_DIR = os.path.join(STATE_DIR, "pending_sheet_appends")
SID = "1Z4KwmUMNuA9Q562y3Vvxa40AFcqGaMyAXAqr7QlbmOQ"
SHEET = "Все2"
SESSION = "/Users/timofei/.claude/skill-telegram/tim2.session"
GSHEETS_TOKEN_BIN = "/Users/timofei/.local/bin/gsheets-token"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# YC batches to try (newest first); batch facet values in Algolia
YC_BATCHES = ["Winter 2027", "Fall 2026", "Summer 2026", "Winter 2026", "Fall 2025", "Summer 2025"]

CATEGORY = "SaaS / AI app"
CAT_LABEL = "SaaS"

SOFTUGC_PRODUCT = (
    "SoftUGC — organic distribution using AI short-form video and a network of accounts we own. "
    "Works as a growth arm that runs itself: 183.5M views in 90 days, no shoots, traffic costs "
    "13 to 40x less than standard clippers. We work on revshare or CPA and clients only cover production costs."
)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_env():
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return
    # project .env (FIRECRAWL_API_KEY, LLM keys)
    try:
        for line in open(os.path.join(ROOT, ".env")):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                key, value = k.strip(), v.strip().strip('"').strip("'")
                if not os.environ.get(key):
                    os.environ[key] = value
    except FileNotFoundError:
        pass
    # telegram creds
    try:
        for line in open(os.path.expanduser("~/.telegram/.env")):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line and "=" in line:
                k, v = line.split("=", 1)
                key, value = k.strip(), v.strip().strip('"').strip("'")
                if not os.environ.get(key):
                    os.environ[key] = value
    except FileNotFoundError:
        pass

    # Relay Fast is the gateway. LLM_API_KEY is the older name for the same slot.
    if not os.environ.get("RELAYFAST_API_KEY") and os.environ.get("LLM_API_KEY"):
        os.environ["RELAYFAST_API_KEY"] = os.environ["LLM_API_KEY"]


# ---------------- YC ----------------

def yc_algolia_key():
    """Fetch public Algolia app/key embedded in the YC directory page.

    The object carries extra fields (e.g. a signed key with valid_until), so
    parse the whole JSON after the assignment instead of matching a fixed
    key layout.
    """
    r = requests.get("https://www.ycombinator.com/companies", headers=UA, timeout=30)
    m = re.search(r"window\.AlgoliaOpts = (\{.*?\});", r.text, re.S)
    if not m:
        raise RuntimeError("AlgoliaOpts not found on YC page")
    try:
        opts = json.loads(m.group(1))
        return opts["app"], opts["key"]
    except (json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"AlgoliaOpts unparseable: {e}")


def yc_fetch_batch(app, key, batch, limit):
    url = "https://45bwzj1sgc-dsn.algolia.net/1/indexes/YCCompany_By_Launch_Date_production/query"
    headers = {
        "X-Algolia-Application-Id": app,
        "X-Algolia-API-Key": key,
        "Content-Type": "application/json",
    }
    body = {"query": "", "hitsPerPage": limit, "filters": f'batch:"{batch}"'}
    r = requests.post(url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    return r.json().get("hits", [])


def yc_company_details(slug):
    """Parse company page (Inertia data-page JSON) -> founders, twitter."""
    r = requests.get(f"https://www.ycombinator.com/companies/{slug}", headers=UA, timeout=30)
    html = r.text
    m = re.search(r'data-page="([^"]+)"', html)
    if not m:
        return {}
    try:
        page = json.loads(html_mod.unescape(m.group(1)))
    except json.JSONDecodeError:
        return {}
    co = page.get("props", {}).get("company") or {}
    founders = [f for f in (co.get("founders") or []) if f.get("is_active", True)]
    tw = co.get("twitter_url") or ""
    tw_user = ""
    m2 = re.search(r"(?:twitter\.com|x\.com)/(@?\w+)", tw)
    if m2:
        tw_user = m2.group(1).lstrip("@")
    res = {
        "founders": founders,
        "twitter": tw_user,
        "one_liner": co.get("one_liner") or "",
        "website": co.get("website") or "",
        "batch": co.get("batch") or "",
    }
    site_tg = extract_tg_from_html(html)
    if site_tg:
        res["site_tg"] = site_tg
    return res


def _social_user(url, host_pat):
    """Extract username/handle from a social URL."""
    m = re.search(host_pat, url or "")
    return m.group(1) if m else ""


def scrape_yc(limit, seen_keys=None):
    app, key = yc_algolia_key()
    leads = []
    fetch_limit = max(limit * 3, 100) if seen_keys is not None else max(1, limit // 2)
    for batch in YC_BATCHES:
        if len(leads) >= limit:
            break
        try:
            hits = yc_fetch_batch(app, key, batch, fetch_limit)
        except Exception as e:
            log(f"YC batch {batch}: {e}")
            continue
        log(f"YC batch {batch}: {len(hits)} hits")
        for h in hits:
            if len(leads) >= limit:
                break
            slug = h.get("slug")
            if not slug:
                continue
            if seen_keys:
                h_name = (h.get("name") or "").strip().lower()
                h_site = norm_site(h.get("website"))
                if (h_name and h_name in seen_keys) or (h_site and h_site in seen_keys):
                    continue
            det = {}
            try:
                det = yc_company_details(slug)
            except Exception as e:
                log(f"  YC {slug}: {e}")
            time.sleep(1.2)
            founders = det.get("founders") or []
            f0 = founders[0] if founders else {}
            lead_item = {
                "name": h.get("name") or "",
                "one_liner": (det.get("one_liner") or h.get("one_liner") or "")[:90],
                "site": det.get("website") or h.get("website") or "",
                "founder_name": f0.get("full_name") or "",
                # founder-level socials (fall back to company twitter if founder has none)
                "founder_twitter": _social_user(f0.get("twitter_url"), r"(?:twitter\.com|x\.com)/@?(\w+)") or det.get("twitter") or "",
                "founder_linkedin": _social_user(f0.get("linkedin_url"), r"linkedin\.com/in/([\w\-]+)") or "",
                "founder_ph": "",
                "source": "yc",
                "batch_or_date": det.get("batch") or batch,
                "yc_slug": slug,
            }
            if det.get("site_tg"):
                lead_item["site_tg"] = det["site_tg"]
            leads.append(lead_item)
    return leads


# ---------------- Product Hunt ----------------

def fc_scrape(url, retries=3):
    key = (os.environ.get("FIRECRAWL_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")
    for attempt in range(retries):
        try:
            r = requests.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"url": url, "formats": ["html"], "waitFor": 3000},
                timeout=60,
            )
        except requests.RequestException as e:
            if attempt + 1 >= retries:
                raise RuntimeError(f"firecrawl request failed: {e}") from e
            wait = min(5 * (attempt + 1), 30)
            log(f"  firecrawl request error, retrying in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 429:
            # Firecrawl returns Retry-After on quota throttling.  Bound the
            # delay so a scheduled run cannot hang indefinitely.
            try:
                wait = max(1, min(int(float(r.headers.get("Retry-After", ""))), 60))
            except (TypeError, ValueError):
                wait = 10 * (attempt + 1)
            log(f"  firecrawl 429, waiting {wait}s")
            time.sleep(wait)
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"firecrawl authentication failed (HTTP {r.status_code})")
        r.raise_for_status()
        try:
            d = r.json()
        except ValueError as e:
            raise RuntimeError("firecrawl returned invalid JSON") from e
        return (d.get("data") or {}).get("html") or ""
    raise RuntimeError(f"firecrawl rate-limited after {retries} attempts: {url}")


def ph_parse_homepage(html):
    """Extract (slug, name, tagline) for today's launches."""
    items = []
    # each launch card: <a href=".../products/SLUG"><span ...></span>NAME</a> ... <span ...text-secondary">TAGLINE</span>
    for m in re.finditer(
        r'href="https://www\.producthunt\.com/products/([\w-]+)"[^>]*>.*?'
        r'([A-Za-z0-9][^<>]{1,80})</a>.*?'
        r'text-base text-secondary">([^<]{3,200})</span>',
        html, re.S):
        slug, name, tagline = m.group(1), html_mod.unescape(m.group(2).strip()), html_mod.unescape(m.group(3).strip())
        if slug and name:
            items.append((slug, name, tagline))
    # dedup keep order
    seen, out = set(), []
    for it in items:
        if it[0] not in seen:
            seen.add(it[0])
            out.append(it)
    return out


def ph_product_details(slug):
    """Product page -> website; makers page -> first maker (name, ph username)."""
    site, maker_name, maker_ph = "", "", ""
    try:
        html = fc_scrape(f"https://www.producthunt.com/products/{slug}")
        m = re.search(r'<a href="([^"]+)"[^>]*data-test="visit-website-button"', html)
        if m:
            site = m.group(1).split("?")[0]
    except Exception as e:
        log(f"  PH {slug} product: {e}")
    time.sleep(3)
    try:
        html = fc_scrape(f"https://www.producthunt.com/products/{slug}/makers")
        # maker cards: <a ... href=".../@USERNAME">...<img ... alt="FULL NAME" ...
        pairs = re.findall(
            r'href="https://www\.producthunt\.com/@(\w+)"[^>]*>.*?alt="([^"]+)"',
            html, re.S)
        if pairs:
            maker_ph, maker_name = pairs[0][0], html_mod.unescape(pairs[0][1])
        else:
            users = re.findall(r'producthunt\.com/@(\w+)', html)
            names = re.findall(r'alt="([^"]+)" class="rounded-full"', html)
            if users:
                seen = set()
                uniq = [u for u in users if not (u in seen or seen.add(u))]
                maker_ph = uniq[0]
            if names:
                maker_name = html_mod.unescape(names[0])
    except Exception as e:
        log(f"  PH {slug} makers: {e}")
    return site, maker_name, maker_ph


def ph_user_socials(ph_username):
    """Fetch a maker's personal PH page -> (twitter, linkedin) usernames."""
    tw, li = "", ""
    if not ph_username:
        return tw, li
    try:
        html = fc_scrape(f"https://www.producthunt.com/@{ph_username}", retries=2)
        m = re.search(r"(?:twitter\.com|x\.com)/@?(\w+)", html)
        if m:
            tw = m.group(1)
        m2 = re.search(r"linkedin\.com/in/([\w\-]+)", html)
        if m2:
            li = m2.group(1)
    except Exception as e:
        log(f"  PH @{ph_username} socials: {e}")
    return tw, li


def scrape_ph(limit):
    if limit <= 0:
        return []
    html = fc_scrape("https://www.producthunt.com/")
    items = ph_parse_homepage(html)
    log(f"PH homepage: {len(items)} launches")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    leads = []
    for slug, name, tagline in items[:limit]:
        site, maker_name, maker_ph = "", "", ""
        try:
            site, maker_name, maker_ph = ph_product_details(slug)
        except Exception as e:
            log(f"  PH {slug}: {e}")
        time.sleep(3)
        # founder socials from the maker's personal PH page (twitter + linkedin)
        tw, li = "", ""
        if maker_ph:
            try:
                tw, li = ph_user_socials(maker_ph)
            except Exception as e:
                log(f"  PH {slug} socials: {e}")
            time.sleep(3)
        leads.append({
            "name": name,
            "one_liner": tagline[:90],
            "site": site,
            "founder_name": maker_name,
            "founder_twitter": tw,
            "founder_linkedin": li,
            "founder_ph": maker_ph,
            # Keep the short source value used by llm_first_touch and older
            # downstream records.
            "source": "ph",
            "batch_or_date": today,
            "ph_slug": slug,
        })
    return leads


# -------- BetaList / MicroLaunch / Uneed / Indie Hackers --------

def fetch_html(url):
    """Fetch raw HTML: plain requests first, firecrawl fallback for JS-heavy pages."""
    html = ""
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.ok and "text/html" in (r.headers.get("Content-Type") or ""):
            html = r.text
    except Exception as e:
        log(f"  fetch {url}: {e}")
    if len(html) < 1500 and os.environ.get("FIRECRAWL_API_KEY"):
        try:
            html = fc_scrape(url, retries=2)
            time.sleep(3)
        except Exception as e:
            log(f"  firecrawl fallback {url}: {e}")
    return html


def _strip_query(url):
    return url.split("?")[0].split("#")[0].rstrip("/") if url else ""


def _meta_description(soup):
    tag = soup.find("meta", attrs={"name": "description"})
    if tag and tag.get("content"):
        return html_mod.unescape(tag["content"]).strip()
    return ""


def _uniq(items):
    seen, out = set(), []
    for it in items:
        if it[0] in seen:
            continue
        seen.add(it[0])
        out.append(it)
    return out


def bl_parse_homepage(html):
    """BetaList homepage -> [(slug, name, tagline)]."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    items = []
    for a in soup.select('a.absolute.inset-0[href^="/startups/"]'):
        slug = (a.get("href") or "").split("/startups/")[-1].split("?")[0].strip("/")
        if not slug:
            continue
        name, tagline = "", ""
        card = a.parent
        for _ in range(3):
            if card is None:
                break
            el = card.select_one("span.font-medium.text-gray-900")
            if el:
                name = el.get_text(strip=True)
                t = card.select_one("span.text-base.text-gray-500")
                tagline = t.get_text(strip=True) if t else ""
                break
            card = card.parent
        items.append((slug, html_mod.unescape(name), html_mod.unescape(tagline)))
    return _uniq(items)


def bl_startup_site(slug):
    """BetaList /startups/{slug}/visit 301-redirects to the external site."""
    try:
        r = requests.get(f"https://betalist.com/startups/{slug}/visit",
                         headers=UA, timeout=20, allow_redirects=False)
        loc = r.headers.get("Location") or ""
        if r.status_code in (301, 302, 303, 307, 308) and loc.startswith("http"):
            if "betalist.com" not in urllib.parse.urlparse(loc).netloc.lower():
                return _strip_query(loc)
    except Exception as e:
        log(f"  BL {slug} visit: {e}")
    return ""


def scrape_betalist(limit):
    if limit <= 0:
        return []
    html = fetch_html("https://betalist.com/")
    if not html:
        log("BetaList: empty homepage, skipping")
        return []
    items = bl_parse_homepage(html)
    log(f"BetaList homepage: {len(items)} startups")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    leads = []
    for slug, name, tagline in items[:limit]:
        site = bl_startup_site(slug)
        time.sleep(1)
        leads.append({
            "name": name or slug.replace("-", " ").title(),
            "one_liner": tagline[:90],
            "site": site,
            "founder_name": "",
            "founder_twitter": "",
            "founder_linkedin": "",
            "founder_ph": "",
            "source": "betalist",
            "batch_or_date": today,
            "bl_slug": slug,
        })
    return leads


ML_SKIP_HOSTS = {"microlaunch.net", "microlaunch.workers.dev", "passionfroot.me",
                 "stimpack.io", "tiun.io", "hackwithus.dev"}


def ml_parse_homepage(html):
    """MicroLaunch homepage -> [(slug, name)]."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    items = []
    for a in soup.select('a[href^="/deals/"]'):
        slug = (a.get("href") or "").split("/deals/")[-1].split("?")[0].strip("/")
        if not re.fullmatch(r"[a-z0-9\-]+", slug):
            continue
        img = a.find("img")
        name = (img.get("alt") or "").strip() if img else ""
        if not name:
            name = a.get_text(strip=True)
        items.append((slug, html_mod.unescape(name)))
    return _uniq(items)


def ml_deal_details(slug):
    """MicroLaunch deal page -> (site, one_liner)."""
    from bs4 import BeautifulSoup
    html = fetch_html(f"https://microlaunch.net/deals/{slug}")
    if not html:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    site = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "ref=microlaunch" not in href:
            continue
        host = urllib.parse.urlparse(href).netloc.lower().replace("www.", "")
        if host and host not in ML_SKIP_HOSTS:
            site = _strip_query(href)
            break
    return site, _meta_description(soup)


def scrape_microlaunch(limit):
    if limit <= 0:
        return []
    html = fetch_html("https://microlaunch.net/")
    if not html:
        log("MicroLaunch: empty homepage, skipping")
        return []
    items = ml_parse_homepage(html)
    log(f"MicroLaunch homepage: {len(items)} deals")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    leads = []
    for slug, name in items[:limit]:
        site, one_liner = "", ""
        try:
            site, one_liner = ml_deal_details(slug)
        except Exception as e:
            log(f"  ML {slug}: {e}")
        time.sleep(1)
        leads.append({
            "name": name or slug.replace("-", " ").title(),
            "one_liner": one_liner[:90],
            "site": site,
            "founder_name": "",
            "founder_twitter": "",
            "founder_linkedin": "",
            "founder_ph": "",
            "source": "microlaunch",
            "batch_or_date": today,
            "ml_slug": slug,
        })
    return leads


UN_SKIP_HOSTS = {"chatgpt.com", "claude.ai", "grok.com", "perplexity.ai",
                 "go.klibon.com", "youtube.com"}


def un_parse_homepage(html):
    """Uneed homepage -> [(slug, name)]."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    items = []
    for a in soup.select('a[href^="/tool/"]'):
        slug = (a.get("href") or "").split("/tool/")[-1].split("?")[0].strip("/")
        if not slug:
            continue
        name = (a.get("aria-label") or "").strip()
        if not name:
            img = a.find("img")
            alt = (img.get("alt") or "").strip() if img else ""
            if alt.lower().startswith("logo of "):
                name = alt[8:].strip()
            elif alt:
                name = alt
        if not name:
            name = a.get_text(strip=True)
        items.append((slug, html_mod.unescape(name)))
    return _uniq(items)


def un_tool_details(slug):
    """Uneed tool page -> (site, one_liner)."""
    from bs4 import BeautifulSoup
    html = fetch_html(f"https://www.uneed.best/tool/{slug}")
    if not html:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    site = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "ref=uneed" not in href:
            continue
        host = urllib.parse.urlparse(href).netloc.lower().replace("www.", "")
        if not host or host.endswith("uneed.best") or host in UN_SKIP_HOSTS:
            continue
        site = _strip_query(href)
        break
    return site, _meta_description(soup)


def scrape_uneed(limit):
    if limit <= 0:
        return []
    html = fetch_html("https://www.uneed.best/")
    if not html:
        log("Uneed: empty homepage, skipping")
        return []
    items = un_parse_homepage(html)
    log(f"Uneed homepage: {len(items)} tools")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    leads = []
    for slug, name in items[:limit]:
        site, one_liner = "", ""
        try:
            site, one_liner = un_tool_details(slug)
        except Exception as e:
            log(f"  UN {slug}: {e}")
        time.sleep(1)
        leads.append({
            "name": name or slug.replace("-", " ").title(),
            "one_liner": one_liner[:90],
            "site": site,
            "founder_name": "",
            "founder_twitter": "",
            "founder_linkedin": "",
            "founder_ph": "",
            "source": "uneed",
            "batch_or_date": today,
            "un_slug": slug,
        })
    return leads


IH_SKIP_HOSTS = {"indiehackers.com", "twitter.com", "x.com", "instagram.com",
                 "linkedin.com", "facebook.com", "youtube.com",
                 "storage.googleapis.com", "gstatic.com"}


def ih_parse_products(html):
    """Indie Hackers /products -> [(slug, name, tagline)]."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    items = []
    for a in soup.select('a.product-card__link[href^="/product/"]'):
        slug = (a.get("href") or "").split("/product/")[-1].split("?")[0].strip("/")
        if not slug:
            continue
        name, tagline = "", ""
        scope = a
        for _ in range(4):
            if scope is None:
                break
            el = scope.select_one("span.product-card__name")
            if el:
                name = el.get_text(strip=True)
                t = scope.select_one("span.product-card__tagline")
                tagline = t.get_text(strip=True) if t else ""
                break
            scope = scope.parent
        items.append((slug, html_mod.unescape(name), html_mod.unescape(tagline)))
    return _uniq(items)


def ih_product_details(slug):
    """IH product page -> (site, founder_twitter)."""
    from bs4 import BeautifulSoup
    html = fetch_html(f"https://www.indiehackers.com/product/{slug}")
    if not html:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    site, tw = "", ""
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        host = urllib.parse.urlparse(href).netloc.lower().replace("www.", "")
        if not tw:
            m = re.match(r"^https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,20})/?$", href)
            if m and m.group(1).lower() != "indiehackers":
                tw = m.group(1)
                continue
        if not site and host and not any(host == h or host.endswith("." + h) for h in IH_SKIP_HOSTS):
            site = _strip_query(href)
        if site and tw:
            break
    return site, tw


def scrape_indiehackers(limit):
    if limit <= 0:
        return []
    html = fetch_html("https://www.indiehackers.com/products")
    if not html:
        log("Indie Hackers: empty /products, skipping")
        return []
    items = ih_parse_products(html)
    log(f"Indie Hackers /products: {len(items)} products")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    leads = []
    for slug, name, tagline in items[:limit]:
        site, tw = "", ""
        try:
            site, tw = ih_product_details(slug)
        except Exception as e:
            log(f"  IH {slug}: {e}")
        time.sleep(1)
        leads.append({
            "name": name or slug.replace("-", " ").title(),
            "one_liner": tagline[:90],
            "site": site,
            "founder_name": "",
            "founder_twitter": tw,
            "founder_linkedin": "",
            "founder_ph": "",
            "source": "indiehackers",
            "batch_or_date": today,
            "ih_slug": slug,
        })
    return leads


# ---------------- Site research ----------------

def fetch_site_text(url):
    """Fetch homepage text: meta description + headings + visible body text.
    Uses plain requests+BeautifulSoup; falls back to firecrawl for JS-heavy sites
    (empty/short text). Returns trimmed text (~2500 chars) or ''."""
    if not url:
        return ""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    text = ""
    try:
        r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
        if r.ok and "text/html" in (r.headers.get("Content-Type") or ""):
            text = html_to_text(r.text)
    except Exception as e:
        log(f"  site fetch {url}: {e}")
    if len(text) < 200 and os.environ.get("FIRECRAWL_API_KEY"):
        try:
            html = fc_scrape(url, retries=2)
            t2 = html_to_text(html)
            if len(t2) > len(text):
                text = t2
        except Exception as e:
            log(f"  firecrawl fallback {url}: {e}")
    return text[:2500]


def html_to_text(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    parts = []
    # meta description / og
    for attr, val in (("name", "description"), ("property", "og:description")):
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            parts.append(tag["content"].strip())
    if soup.title and soup.title.string:
        parts.append(soup.title.string.strip())
    for tag in soup.find_all(["h1", "h2", "h3"])[:12]:
        t = tag.get_text(" ", strip=True)
        if t:
            parts.append(t)
    for junk in soup(["script", "style", "noscript", "svg", "footer", "nav"]):
        junk.decompose()
    body = soup.get_text(" ", strip=True)
    body = re.sub(r"\s{2,}", " ", body)
    parts.append(body[:1800])
    return "\n".join(p for p in parts if p)


# ---------------- LLM (Relay Fast) ----------------

def llm_chat(messages, max_tokens=2000):
    base = (os.environ.get("LLM_BASE_URL") or "https://relay.fast/v1").rstrip("/")
    key = os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise RuntimeError("RELAYFAST_API_KEY not set")
    last = "LLM failed"
    for model in chain():
        try:
            r = requests.post(
                base + "/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "max_tokens": max_tokens,
                      "temperature": 0.4},
                timeout=90)
        except requests.RequestException as exc:
            last = str(exc)
            if note_failure(model, 0, ""):
                log(f"  {model} unreachable, next model")
                continue
            raise
        if r.status_code >= 400:
            last = f"{model} {r.status_code}"
            if note_failure(model, r.status_code, r.text[:400]):
                log(f"  {model} unavailable ({r.status_code}), next model")
                continue
            r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        if isinstance(content, str) and content.strip():
            return content
        last = f"{model} empty content"
        if note_failure(model, 0, ""):
            log(f"  {model} empty content, next model")
            continue
        raise RuntimeError(last)
    raise RuntimeError(last)


def llm_angles_batch(leads):
    """One batched call: their_angle + our_angle for all leads (JSON in/out)."""
    items = []
    for i, l in enumerate(leads):
        items.append({
            "i": i,
            "name": l.get("name") or "",
            "one_liner": l.get("one_liner") or "",
            "site_text": (l.get("site_text") or "")[:1600],
        })
    prompt = (
        "You are researching startups for a B2B outreach team. For EACH startup below, based on the "
        "provided context (website text OR Telegram bio / description — whatever is given), write two "
        "fields in RUSSIAN:\n"
        '1. "their_angle": 1-2 konkretnyh predlozhenija — CHTO imenno delaet produkt, DLJA KOGO, '
        "kakaja u nih model (podpiska/usage/marketplace itd). Tolko po dannomu kontekstu, bez obschih fraz "
        'vrode "innovacionnaja platforma" i bez vydumyvanija detalej, kotoryh net v kontekste.\n'
        '2. "our_angle": 1-2 predlozhenija — kak im zajdet SoftUGC. My: ' + SOFTUGC_PRODUCT + "\n"
        "Vyberi podhodjaschee napravlenie pod ih produkt: klipping arhiva kontenta / AI UGC pod zapusk / "
        "klipping pod instally i podpiski / klipping pod referalov / "
        "prodvizhenie fizicheskih tovarov / platnaja reklama (pokupnoj trafik na korotkoe video). "
        "Konkretno, kak my im pomozhem.\n\n"
        "Startups (JSON):\n" + json.dumps(items, ensure_ascii=False) + "\n\n"
        'Reply with ONLY a JSON array, one object per startup, same order: '
        '[{"i": 0, "their_angle": "...", "our_angle": "..."}, ...]'
    )
    raw = llm_chat([{"role": "user", "content": prompt}], max_tokens=3000)
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise RuntimeError("LLM angles: no JSON array in response")
    arr = json.loads(m.group(0))
    by_i = {}
    for o in arr:
        try:
            by_i[int(o.get("i"))] = o
        except (TypeError, ValueError):
            continue
    return by_i


def llm_workers():
    """Concurrent first_touch calls against the relay API (external, safe to
    batch 10-15 at a time)."""
    try:
        return max(1, int(os.environ.get("LLM_CONCURRENCY") or "12"))
    except ValueError:
        return 12


def parallel_first_touch(leads):
    """Generate first_touch for many leads concurrently, with one retry pass.
    Returns a list of texts ('' where every model failed)."""
    def gen(lead):
        try:
            return llm_first_touch(lead)
        except Exception as e:
            log(f"  first_touch for {lead.get('name')}: {e}")
            return ""
    workers = llm_workers()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        texts = list(ex.map(gen, leads))
    missing = [i for i, t in enumerate(texts) if not t]
    if missing:
        log(f"first_touch retrying {len(missing)} failed")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fixed = list(ex.map(gen, [leads[i] for i in missing]))
        for i, t in zip(missing, fixed):
            texts[i] = t
    return texts


_RU_NAME_RE = re.compile(r"^[А-Яа-яЁё][А-Яа-яЁё-]+(?:\s+[А-Яа-яЁё][А-Яа-яЁё-]+)*\s*$")
# Short neutral Latin names that are equally Russian in practice.
_RU_LATIN_NAMES = {"sasha", "pasha", "dima", "misha", "kolya", "olya", "katya", "lena"}


def wants_russian(lead):
    """Russian-language outreach when the person's name is Russian.

    A Cyrillic first+last name (or a known Russian short name in Latin) means
    the founder is a Russian speaker; write to them in Russian. Everything
    else keeps the English message.
    """
    name = (lead.get("founder_name") or lead.get("name") or "").strip()
    if not name:
        return False
    if _RU_NAME_RE.match(name):
        return True
    first = name.split()[0].lower().strip(".,")
    return first in _RU_LATIN_NAMES


def _first_touch_prompt(lead, russian):
    """Build the first-touch prompt in the lead's language (Russian for a
    Russian founder name, English otherwise)."""
    name = (lead.get("name") or "").strip() or "your product"
    src = lead.get("source")
    if src == "yc":
        seen = " (we saw you on YC " + (lead.get("batch_or_date") or "") + ")"
    elif src == "ph":
        seen = " (we saw you on Product Hunt)"
    elif src == "betalist":
        seen = " (we saw you on BetaList)"
    elif src == "microlaunch":
        seen = " (we saw you on MicroLaunch)"
    elif src == "uneed":
        seen = " (we saw you on Uneed)"
    elif src == "indiehackers":
        seen = " (we saw you on Indie Hackers)"
    else:
        seen = ""
    if russian:
        lang_rules = (
            "GREETING NAME RULES (critical):\n"
            "- Use the founder's Russian first name in the greeting, in the informal "
            "form people use in business Telegram chats (Илья, Катя). NEVER use placeholders "
            "like [Имя], [Founder], or 'здравствуйте' formality. Just 'Привет, Илья,' or 'Привет,'.\n"
            "- Do NOT address a company/brand name as if it were a person.\n\n"
            "LANGUAGE RULE (absolute):\n"
            "- The ENTIRE message must be in RUSSIAN ONLY. Latin letters are allowed only in "
            "the product name, numbers, and metric units (183.5M, 13-40x).\n"
            "- Confident but plain business Russian, no officialese ('осуществляем деятельность'), "
            "no crude slang. ты/вы — on ты, as is normal in founder Telegram chats.\n"
        )
        lang_line = "3 коротких абзаца, РУССКИМ языком, уверенно и по делу, без воды:\n"
        retry_line = (
            "\n\nYOUR PREVIOUS ANSWER WAS NOT FULLY IN RUSSIAN — THIS IS FORBIDDEN. "
            "Rewrite the ENTIRE message in Russian, translating every non-Russian idea. "
            "Latin letters are allowed only in the product name and numbers."
        )
    else:
        lang_rules = (
            "GREETING NAME RULES (critical):\n"
            "- NEVER use placeholders like [Founder's Name], [Name], [Founder], 'there' (as in 'Hi there').\n"
            "- Use a personal name ONLY if it is a normal English/Western first name (e.g. Alex, Maria, John). "
            "If the name is a username, brand, contains digits, underscores, non-Latin or looks odd — do NOT "
            "use any name. Just write 'Hey,' or 'Hi,'.\n"
            "- Do NOT address a company/brand name as if it were a person (no 'Hey Uniswap Labs').\n\n"
            "LANGUAGE RULE (absolute):\n"
            "- The ENTIRE message must be in English ONLY. Zero Cyrillic characters, zero words in any "
            "other language.\n"
            "- The context below may be in Russian or mixed. TRANSLATE the idea into natural English — "
            "never copy a non-English phrase into the message, not even a single word or product slogan.\n"
            "- If the product name itself is non-Latin, transliterate it or refer to 'your product'.\n"
        )
        lang_line = "3 short paragraphs, English, confident and concrete, no fluff:\n"
        retry_line = (
            "\n\nYOUR PREVIOUS ANSWER CONTAINED CYRILLIC CHARACTERS — THIS IS FORBIDDEN. "
            "Rewrite the ENTIRE message in English only, translating every non-English idea."
        )
    prompt = (
        "Write a short personalized first outreach message (Telegram DM) to the founder/team of a company. "
        "STRICT 4-paragraph structure, " + lang_line +
        "1. Paragraph 1: ONLY 'Hey,' ('Привет,' if Russian).\n"
        "2. Paragraph 2: Start with 'Saw " + name + ".' and go STRAIGHT to the individualized growth/acquisition pain "
        "inferred from their product/category (e.g. 'You're probably running into the same thing most [agencies / AI teams / "
        "Web3 teams / consumer brands / trading platforms] hit: ...'). 2 short natural sentences. "
        "Do NOT compliment their product, do NOT paste their bio back to them, NO em-dashes (—).\n"
        "3. Paragraph 3 (our SoftUGC presentation + 183.5M case): "
        f"'{SOFTUGC_PITCH_TEXT}' (you may lightly adapt 2-3 words to fit their category, keeping SoftUGC, 183.5M views in 90 days, no shoots, 13 to 40x less).\n"
        "4. Paragraph 4 (EXACT closing offer & CTA): "
        f"'{REVSHARE_OFFER_TEXT}'\n\n"
        + lang_rules + "\n"
        f"Startup/product name: {name}\n"
        f"Category: {(lead.get('category') or '').strip()}\n"
        f"What they do (use ONLY to infer the pain, do NOT describe it back to them): {(lead.get('site_text') or lead.get('one_liner') or '')[:900]}\n"
        f"Our product: {SOFTUGC_PRODUCT}\n\n"
        "Output ONLY the message text, no quotes, no '(generated)' markers, no commentary."
    )
    return prompt, retry_line


def llm_first_touch(lead):
    """Canonical Vlad first touch via claude-sonnet-5 on Relay Fast (falls back to sub-niche template only offline/in tests)."""
    name = (lead.get("name") or "").strip()
    niche = (lead.get("one_liner") or "").strip() or (lead.get("their_angle") or "").strip()
    src = (lead.get("source") or "").strip()
    cat = (lead.get("category") or "").strip()
    site = (lead.get("site") or lead.get("url") or lead.get("project_url") or "").strip()
    ru = wants_russian(lead)
    llm_disabled = os.environ.get("LEADGEN_FIRST_TOUCH_LLM", "").strip().lower() in {"0", "false", "no"}
    if (
        not ru
        and not llm_disabled
        and "PYTEST_CURRENT_TEST" not in os.environ
        and (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY"))
    ):
        try:
            from outreach_copy import generate_vlad_sonnet_batch
        except ImportError:
            from leadgen.outreach_copy import generate_vlad_sonnet_batch
        res = generate_vlad_sonnet_batch(
            [
                {
                    "company": name,
                    "name": lead.get("founder_name") or name,
                    "category": cat,
                    "website": site,
                    "their_angle": (lead.get("site_text") or niche)[:700],
                    "source": src,
                }
            ]
        )
        if res and res[0].get("step1_text"):
            if res[0].get("audience"):
                lead["audience"] = res[0]["audience"]
            return res[0]["step1_text"]
    return template_first_touch(
        name,
        niche,
        russian=ru,
        source=src,
        category=cat,
        site=site,
    )


def _humanize_prompt(russian):
    if russian:
        return (
            "Перепиши это аутрич-сообщение так, чтобы оно выглядело как сообщение реального человека "
            "в Telegram, а не как AI-копирайт. Правила:\n"
            "- Сохрани структуру и все факты (боль, презентация SoftUGC, кейс 183.5M просмотров, "
            "в 13-40 раз дешевле, CTA) — меняй только формулировки.\n"
            "- НИКАКИХ длинных тире (—) и средних тире (–). Используй запятые, точки или скобки.\n"
            "- Убери канцелярит и AI-обороты: 'в современном мире', 'динамично развиваемся', "
            "'инновационные решения'. Короче и проще предложения.\n"
            "- Разный ритм: никакие два абзаца не начинаются одинаково.\n"
            "- Только русский, латиница допустима только в названии продукта и числах. "
            "Выведи ТОЛЬКО переписанное сообщение, ничего больше.\n\n"
            "СООБЩЕНИЕ:\n"
        )
    return (
        "Rewrite this outreach message so it reads like a real person typed it in a Telegram DM, "
        "not like AI copy. Rules:\n"
        "- Keep the structure and all facts (pain, SoftUGC presentation, 183.5M views case, "
        "13-40x cheaper, CTA) — change wording only.\n"
        "- NO em-dashes (—) or en-dashes (–) anywhere. Use commas, periods, or parentheses instead.\n"
        "- Cut AI-sounding filler: 'plug-and-play', 'in today's landscape', 'brutal', 'I suspect', "
        "'figured I'd reach out', 'game', 'grind'. Shorter, plainer sentences.\n"
        "- Vary rhythm: no two paragraphs starting the same way, no perfectly parallel clauses.\n"
        "- English only, no Cyrillic. Output ONLY the rewritten message, nothing else.\n\n"
        "MESSAGE:\n"
    )


def _ru_share(text):
    """Fraction of Cyrillic letters among all letters — Russian text is ~1.0."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "А" <= c <= "я" or c in "Ёё") / len(letters)


def _has_case_number(text):
    """183.5M or 183,5M — Russian drafts use the comma decimal separator."""
    return "183.5" in text or "183,5" in text


def humanize_outreach(text, name="", russian=False):
    """De-AI the draft: kill em/en dashes and template cadence. Falls back to the
    dash-stripped original if the LLM pass fails or breaks constraints."""
    try:
        out = llm_chat(
            [{"role": "user", "content": _humanize_prompt(russian) + text}],
            max_tokens=400).strip().strip('"')
        ok = len(out) >= 150 and _has_case_number(out)
        if ok and russian and _ru_share(out) >= 0.5:
            return out
        if ok and not russian and not re.search(r"[А-Яа-яЁё]", out):
            return out
        log(f"  humanize rejected output for {name}, using original")
    except Exception as e:
        log(f"  humanize failed for {name}: {e}; using original")
    return re.sub(r"\s*[—–]\s*", ", ", text)


def enrich_with_llm(leads):
    """Fill their_angle / our_angle (batched) and first_touch (per-lead) for TG leads."""
    if not leads:
        return
    log(f"=== LLM enrich: {len(leads)} leads ===")
    try:
        angles = llm_angles_batch(leads)
    except Exception as e:
        log(f"LLM angles batch failed: {e}; falling back to per-lead calls")
        angles = {}
        for i, l in enumerate(leads):
            try:
                one = llm_angles_batch([l])
                angles.update({i: o for _, o in one.items()})
            except Exception as e2:
                log(f"  angles for {l.get('name')}: {e2}")
            time.sleep(2)
    for i, l in enumerate(leads):
        a = angles.get(i) or {}
        l["their_angle"] = (a.get("their_angle") or "").strip()
        l["our_angle"] = (a.get("our_angle") or "").strip()
    log(f"angles filled: {sum(1 for l in leads if l.get('their_angle'))}/{len(leads)}")
    texts = parallel_first_touch(leads)
    for l, t in zip(leads, texts):
        l["first_touch"] = t
    log(f"first_touch filled: {sum(1 for t in texts if t)}/{len(leads)}")


def has_outreach_contact(lead):
    """True when the lead has Telegram, personal LinkedIn, or X/Twitter."""
    if not isinstance(lead, dict):
        return False
    return bool(
        (lead.get("tg_username") or "").strip()
        or (lead.get("founder_linkedin") or "").strip()
        or (lead.get("founder_twitter") or "").strip()
    )


def validate_lead(l, require_tg=True):
    """Quality gate: all outreach fields must be non-empty and concrete."""
    problems = []
    if require_tg:
        if not (l.get("tg_username") or "").strip():
            problems.append("no tg")
    else:
        if not has_outreach_contact(l):
            problems.append("no contact")
    ta = (l.get("their_angle") or "").strip()
    if len(ta) < 30:
        problems.append("their_angle empty/too short")
    if not (l.get("our_angle") or "").strip():
        problems.append("our_angle empty")
    if not (l.get("first_touch") or "").strip():
        problems.append("first_touch empty")
    return problems


# ---------------- Telegram lookup ----------------

def tg_lookup(usernames):
    """Check t.me/{username} existence via Telethon. Returns {username: tg_id}."""
    if not usernames:
        return {}
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    found = {}

    async def run():
        client = TelegramClient(SESSION, api_id, api_hash)
        await client.connect()
        try:
            for u in usernames:
                try:
                    ent = await client.get_entity(u)
                    found[u] = getattr(ent, "id", None)
                    log(f"  TG @{u}: FOUND id={found[u]}")
                except FloodWaitError as e:
                    log(f"  TG @{u}: flood wait {e.seconds}s, stopping TG lookups")
                    break
                except Exception:
                    log(f"  TG @{u}: not found")
                time.sleep(2.5)
        finally:
            await client.disconnect()

    import asyncio
    asyncio.run(run())
    return found


_TG_HTML_RE = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,32})(?:[/?#\"'\s<>]|$)",
    re.I,
)
_TG_SKIP_PATHS = {
    "share", "iv", "joinchat", "addstickers", "s",
    "proxy", "socks", "login", "contact", "boost",
}


def extract_tg_from_html(html: str) -> str:
    """Extract the first valid Telegram username from HTML."""
    for m in _TG_HTML_RE.finditer(html or ""):
        handle = m.group(1)
        low = handle.lower()
        if low in _TG_SKIP_PATHS or low.endswith("bot"):
            continue
        return handle
    return ""


def extract_tg_from_site(url: str) -> str:
    """Fetch homepage HTML and extract the first valid Telegram username."""
    if not url:
        return ""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        r = requests.get(url, headers=UA, timeout=12, allow_redirects=True)
        if r.ok:
            return extract_tg_from_html(r.text)
    except Exception:
        pass
    return ""


def tg_candidates(lead):
    """Candidate usernames for TG lookup, in priority order.

    Only high-confidence candidates: direct site t.me handle, exact twitter
    handle, PH username, full-name concatenation, or first_name + company_slug.
    First-name-only is too generic (false positives: @michael, @tony etc. are
    almost never the actual founder).
    """
    cands = []
    site_tg = (lead.get("site_tg") or "").strip().lstrip("@")
    if site_tg:
        cands.append(site_tg)
    tw = (lead.get("founder_twitter") or "").strip().lstrip("@")
    if tw:
        cands.append(tw)
    ph = (lead.get("founder_ph") or "").strip()
    if ph:
        cands.append(ph)
        cands.append(ph.replace("_", ""))
    # name-based fallback: firstnamelastname lowercased, letters only
    fn = (lead.get("founder_name") or "").strip()
    parts = [p for p in re.split(r"\s+", fn) if p] if fn else []
    if len(parts) >= 2:
        cands.append(re.sub(r"[^a-z0-9_]", "", (parts[0] + parts[-1]).lower()))
    if parts:
        first = re.sub(r"[^a-z]", "", parts[0].lower())
        raw_name = (lead.get("name") or "").strip()
        comp = re.sub(r"[^a-z0-9]", "", raw_name.split()[0].lower()) if raw_name else ""
        if len(first) >= 3 and 3 <= len(comp) <= 12:
            cands.append(f"{first}{comp}")
            cands.append(f"{first}_{comp}")
    # dedup, keep order, reasonable length
    seen, out = set(), []
    for c in cands:
        if c and 4 <= len(c) <= 32 and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:3]  # max 3 attempts per founder


# ---------------- Sheet ----------------

def gsheets_token():
    return subprocess.check_output([GSHEETS_TOKEN_BIN], text=True).strip()


def sheet_existing():
    """Read existing names (col E), sites (col F) and Telegram handles (col G)."""
    token = gsheets_token()
    sheet_enc = urllib.parse.quote(SHEET)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{sheet_enc}%21E:G?majorDimension=COLUMNS"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    cols = r.json().get("values", [])
    names = {
        n.strip().lower() for n in (cols[0] if cols else [])
        if n and len(n.strip()) >= 3 and n != "Название"
    }
    sites = set()
    if len(cols) > 1:
        for s in cols[1]:
            site = norm_site(s)
            if site:
                sites.add(site)
    handles = set()
    if len(cols) > 2:
        for contact in cols[2]:
            handles.update(re.findall(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", contact or "", re.I))
    return names, sites, {h.lower() for h in handles}


def norm_site(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u).rstrip("/")
    host = u.split("/", 1)[0]
    if host in {"t.co", "bit.ly", "tinyurl.com", "x.com", "twitter.com", "t.me", "telegram.me"}:
        return ""
    return u


def _tw_url(u):
    u = (u or "").strip().lstrip("@")
    if not u:
        return ""
    return u if u.startswith("http") else f"https://x.com/{u}"


def _li_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    return u if u.startswith("http") else f"https://www.linkedin.com/in/{u}"


def keep_for_sheet_source(lead):
    """Product Hunt contacts are not written to the sheet."""
    return (lead or {}).get("source") != "ph"


def to_row(lead):
    tg = (lead.get("tg_username") or "").strip().lstrip("@")
    x = _tw_url(lead.get("founder_twitter"))
    linkedin = _li_url(lead.get("founder_linkedin"))
    if tg:
        contact = f"https://t.me/{tg}"
    elif linkedin:
        contact = linkedin
    elif x:
        contact = x
    else:
        contact = ""
    channels = [c for c in ("TG" if tg else "", "X" if x else "", "LinkedIn" if linkedin else "") if c]
    return [
        " + ".join(channels),      # contact channels
        "🟢",                     # Приоритет
        "🟢",                     # Статус/Фит
        CAT_LABEL,                # Категория
        lead.get("name") or "",   # Название
        norm_site(lead.get("site")),  # Сайт
        contact,                  # Контакт
        lead.get("their_angle") or "",  # Их угол
        lead.get("our_angle") or "",    # Наш угол
        "",                       # Хук (не используется)
        lead.get("first_touch") or "",  # Первое касание
        "",                       # Статус
        x,                                      # M: Founder X
        (f"producthunt.com/@{lead.get('founder_ph')}" if lead.get("founder_ph") else ""),  # N: Founder PH
        linkedin,                               # O: Founder LinkedIn
    ]


def sheet_append(rows):
    if not rows:
        return {}
    try:
        from tg_to_gsheet import route_rows
    except ImportError:
        try:
            from leadgen.tg_to_gsheet import route_rows
        except ImportError:
            from scripts.tg_to_gsheet import route_rows
    tg_rows, x_rows, direct_rows = route_rows(rows)
    batches = [(SHEET, tg_rows), ("X", x_rows), ("Direct", direct_rows)]
    if not any(b[1] for b in batches):
        batches = [(SHEET, rows)]
    token = gsheets_token()
    last_resp = {}
    updated_rows = 0
    updated_cells = 0
    for tab, tab_rows in batches:
        if not tab_rows:
            continue
        sheet_enc = urllib.parse.quote(tab)
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/"
               f"{sheet_enc}%21A:O:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS")
        r = requests.post(url, headers={"Authorization": f"Bearer {token}",
                                        "Content-Type": "application/json"},
                          json={"values": tab_rows}, timeout=30)
        if not r.ok:
            os.makedirs(PENDING_DIR, exist_ok=True)
            pending = os.path.join(PENDING_DIR, f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_yc_ph.json")
            with open(pending, "w", encoding="utf-8") as f:
                json.dump({"values": tab_rows}, f, ensure_ascii=False)
            detail = r.text.replace(token, "<REDACTED>")[:1000]
            raise RuntimeError(f"Google Sheets append failed ({r.status_code}); rows saved to {pending}: {detail}")
        data = r.json()
        last_resp = data
        upd = data.get("updates") or {}
        updated_rows += int(upd.get("updatedRows") or 0)
        updated_cells += int(upd.get("updatedCells") or 0)
    if updated_rows or updated_cells:
        last_resp = dict(last_resp)
        last_resp["updates"] = {"updatedRows": updated_rows, "updatedCells": updated_cells}
    return last_resp


# ---------------- main ----------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--yc-limit", type=int, default=15)
    p.add_argument("--ph-limit", type=int, default=0)
    p.add_argument("--betalist-limit", type=int, default=10)
    p.add_argument("--microlaunch-limit", type=int, default=10)
    p.add_argument("--uneed-limit", type=int, default=10)
    p.add_argument("--indiehackers-limit", type=int, default=10)
    p.add_argument("--no-tg", action="store_true", help="skip Telegram lookup")
    p.add_argument("--no-llm", action="store_true", help="skip site research + LLM enrichment")
    p.add_argument("--no-sheet", action="store_true", help="skip sheet append (dry run)")
    args = p.parse_args()

    load_env()
    os.makedirs(STATE_DIR, exist_ok=True)

    # dedup state
    try:
        seen = json.load(open(SEEN_PATH))
    except (FileNotFoundError, json.JSONDecodeError):
        seen = {"keys": []}
    seen_keys = set(seen.get("keys", []))

    log("=== scraping YC ===")
    try:
        yc_leads = scrape_yc(args.yc_limit, seen_keys=seen_keys) if args.yc_limit > 0 else []
    except Exception as e:
        # Keep Product Hunt/YC as a best-effort stage: a transient directory
        # outage must not discard already collected Telegram leads or prevent
        # the scheduled pipeline from completing.
        log(f"YC scrape failed (non-fatal): {e}")
        yc_leads = []
    log(f"YC: {len(yc_leads)} leads")
    log("=== scraping Product Hunt ===")
    try:
        ph_leads = scrape_ph(args.ph_limit) if args.ph_limit > 0 else []
    except Exception as e:
        log(f"PH scrape failed (non-fatal): {e}")
        ph_leads = []
    log(f"PH: {len(ph_leads)} leads")
    log("=== scraping BetaList ===")
    try:
        bl_leads = scrape_betalist(args.betalist_limit) if args.betalist_limit > 0 else []
    except Exception as e:
        log(f"BetaList scrape failed (non-fatal): {e}")
        bl_leads = []
    log(f"BetaList: {len(bl_leads)} leads")
    log("=== scraping MicroLaunch ===")
    try:
        ml_leads = scrape_microlaunch(args.microlaunch_limit) if args.microlaunch_limit > 0 else []
    except Exception as e:
        log(f"MicroLaunch scrape failed (non-fatal): {e}")
        ml_leads = []
    log(f"MicroLaunch: {len(ml_leads)} leads")
    log("=== scraping Uneed ===")
    try:
        un_leads = scrape_uneed(args.uneed_limit) if args.uneed_limit > 0 else []
    except Exception as e:
        log(f"Uneed scrape failed (non-fatal): {e}")
        un_leads = []
    log(f"Uneed: {len(un_leads)} leads")
    log("=== scraping Indie Hackers ===")
    try:
        ih_leads = scrape_indiehackers(args.indiehackers_limit) if args.indiehackers_limit > 0 else []
    except Exception as e:
        log(f"Indie Hackers scrape failed (non-fatal): {e}")
        ih_leads = []
    log(f"Indie Hackers: {len(ih_leads)} leads")

    leads = yc_leads + ph_leads + bl_leads + ml_leads + un_leads + ih_leads

    # dedup: against seen-file and existing sheet
    try:
        ex_names, ex_sites, ex_tg = sheet_existing()
        log(f"sheet existing: {len(ex_names)} names, {len(ex_sites)} sites, {len(ex_tg)} tg")
    except Exception as e:
        log(f"WARN sheet read failed ({e}); dedup vs seen-file only")
        ex_names, ex_sites, ex_tg = set(), set(), set()

    fresh = []
    for l in leads:
        key = norm_site(l.get("site")) or (l.get("name") or "").strip().lower()
        name_l = (l.get("name") or "").strip().lower()
        if not key:
            continue
        if key in seen_keys or name_l in seen_keys:
            continue
        if len(name_l) >= 3 and name_l in ex_names:
            continue
        if key and key in ex_sites:
            continue
        l["_dedup_key"] = key
        fresh.append(l)
    log(f"fresh after dedup: {len(fresh)} (of {len(leads)})")

    # Website direct t.me discovery before Telegram lookup
    if fresh and not args.no_llm and not args.no_tg:
        site_checked = 0
        for l in fresh:
            if site_checked >= 20:
                break
            if l.get("site") and not l.get("site_tg") and (
                not l.get("founder_twitter") or l.get("source") == "yc"
            ):
                site_tg = extract_tg_from_site(l.get("site"))
                if site_tg:
                    l["site_tg"] = site_tg
                site_checked += 1

    # Telegram lookup
    if not args.no_tg and fresh:
        cand_map = {}  # username -> lead index
        for i, l in enumerate(fresh):
            for c in tg_candidates(l):
                cand_map.setdefault(c, i)
        log(f"=== TG lookup: {len(cand_map)} candidates ===")
        found = tg_lookup(list(cand_map.keys()))
        for u, tg_id in found.items():
            l = fresh[cand_map[u]]
            if not l.get("tg_username"):
                l["tg_username"] = u
                l["tg_id"] = tg_id
        log(f"TG found: {sum(1 for l in fresh if l.get('tg_username'))} founders")
        still_fresh = []
        for l in fresh:
            handle = (l.get("tg_username") or "").strip().lstrip("@").lower()
            if handle and handle in ex_tg:
                log(f"dedup existing tg @{handle} ({l.get('name')})")
                continue
            still_fresh.append(l)
        fresh = still_fresh

    # write JSONL
    with open(OUT_JSONL, "w") as f:
        for l in fresh:
            l.pop("_dedup_key", None)
            f.write(json.dumps(l, ensure_ascii=False) + "\n")
    log(f"wrote {len(fresh)} -> {OUT_JSONL}")

    # Sheet append routes TG -> Все2, LinkedIn -> Direct, X -> X.
    outreach_leads = [l for l in fresh if has_outreach_contact(l)]
    log(f"leads with outreach contact: {len(outreach_leads)} (of {len(fresh)} fresh)")

    # research their sites, then LLM-enrich (their_angle / our_angle / first_touch)
    if outreach_leads and not args.no_llm:
        log("=== site research ===")
        for l in outreach_leads:
            try:
                l["site_text"] = fetch_site_text(l.get("site"))
                log(f"  {l.get('name')}: {len(l.get('site_text') or '')} chars")
            except Exception as e:
                log(f"  {l.get('name')}: site fetch failed: {e}")
                l["site_text"] = ""
            time.sleep(1.5)
        enrich_with_llm(outreach_leads)

    # quality gate: drop leads with incomplete outreach data
    good, rows = [], []
    for l in outreach_leads:
        if not keep_for_sheet_source(l):
            log(f"SKIP {l.get('name')}: source ph is not written to the sheet")
            continue
        problems = validate_lead(l, require_tg=False)
        if problems:
            log(f"SKIP {l.get('name')} (@{l.get('tg_username')}): {'; '.join(problems)}")
            continue
        good.append(l)
        rows.append(to_row(l))
    log(f"validated: {len(good)}/{len(outreach_leads)} leads pass quality gate")
    if args.no_sheet:
        log("dry run: not appending to sheet")
    elif rows:
        try:
            resp = sheet_append(rows)
        except Exception as e:
            # The rows are durable in PENDING_DIR; keep the source dedup state
            # advancing so each failed run does not create another copy.
            log(str(e))
        else:
            upd = (resp.get("updates") or {})
            log(f"sheet append: updatedRows={upd.get('updatedRows')} updatedCells={upd.get('updatedCells')}")

    # persist seen (skip on dry-run so --no-sheet does not burn unseen leads)
    if not args.no_sheet:
        for l in fresh:
            seen_keys.add(norm_site(l.get("site")) or (l.get("name") or "").strip().lower())
            seen_keys.add((l.get("name") or "").strip().lower())
        seen["keys"] = sorted(k for k in seen_keys if k)
        with open(SEEN_PATH, "w") as f:
            json.dump(seen, f, ensure_ascii=False, indent=1)

    # summary for stdout
    summary = {
        "yc": len(yc_leads),
        "ph": len(ph_leads),
        "betalist": len(bl_leads),
        "microlaunch": len(ml_leads),
        "uneed": len(un_leads),
        "indiehackers": len(ih_leads),
        "fresh": len(fresh),
        "tg_found": sum(1 for l in fresh if l.get("tg_username")),
        "social_found": sum(1 for l in fresh if l.get("founder_twitter") or l.get("founder_linkedin")),
        "rows_appended": 0 if args.no_sheet else len(rows),
        "validated": len(good),
        "examples": [
            {"name": l.get("name"), "founder": l.get("founder_name"), "site": l.get("site"),
             "tg": l.get("tg_username"),
             "their_angle": l.get("their_angle"), "our_angle": l.get("our_angle"),
             "first_touch": l.get("first_touch")}
            for l in good[:5]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # best-effort CRM MCP push (never fails the pipeline)
    if not args.no_sheet and rows:
        try:
            r = subprocess.run(
                ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "crm_mcp_push.py")],
                check=False, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                log(f"crm_mcp_push exited {r.returncode}: {(r.stderr or '').strip()[:300]}")
        except Exception as e:
            log(f"crm_mcp_push failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
