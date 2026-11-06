#!/usr/bin/env python3
"""Active Meta Ad Library video advertisers enriched with decision-maker contacts.

Scrapes active US video ads via scrapecreators.x.v1-facebook-adlibrary-search-ads,
deduplicates advertisers over a 90-day window, and enriches decision-maker
LinkedIn /in/ and work email via find_company_decision_maker.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

try:
    from treg_client import call
except ImportError:
    try:
        from leadgen.treg_client import call
    except ImportError:
        from scripts.treg_client import call

try:
    from supply_side import is_supply_side
except ImportError:
    try:
        from leadgen.supply_side import is_supply_side
    except ImportError:
        from scripts.supply_side import is_supply_side

try:
    from treg_enrich import domain_of, find_company_decision_maker
except ImportError:
    try:
        from leadgen.treg_enrich import domain_of, find_company_decision_maker
    except ImportError:
        from scripts.treg_enrich import domain_of, find_company_decision_maker

ENDPOINT = "scrapecreators.x.v1-facebook-adlibrary-search-ads"
SEARCH_QUERIES = (
    "AI note taker",
    "AI video editor",
    "AI coaching app",
    "crypto wallet app",
    "prop trading challenge",
    "AI language learning",
    "fitness tracker app",
    "shopify app",
    "B2B SaaS CRM",
)
CAP = int(os.getenv("META_ADS_CAP", "15"))
SEEN_DAYS = 90

GIANT_BRANDS = {
    "facebook",
    "instagram",
    "meta",
    "google",
    "apple",
    "tiktok",
    "youtube",
    "amazon",
    "shopify",
    "netflix",
    "microsoft",
    "openai",
    "anthropic",
}
APP_STORE_HOSTS = {
    "apps.apple.com",
    "itunes.apple.com",
    "play.google.com",
}


def queries_for_day(day: date, count: int = 3) -> list[str]:
    total = len(SEARCH_QUERIES)
    if not total or count <= 0:
        return []
    start = (day.toordinal() * count) % total
    return [SEARCH_QUERIES[(start + i) % total] for i in range(min(count, total))]


def fresh_seen(seen: dict[str, str], day: date, keep_days: int = SEEN_DAYS) -> dict[str, str]:
    cutoff = day - timedelta(days=keep_days)
    kept: dict[str, str] = {}
    for key, raw in (seen or {}).items():
        try:
            seen_on = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if seen_on >= cutoff:
            kept[str(key)] = seen_on.isoformat()
    return kept


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").casefold()).strip("-")
    return slug[:64] or "advertiser"


def _is_giant_brand(page_name: str, domain: str = "") -> bool:
    folded = re.sub(r"[^a-z0-9]+", " ", (page_name or "").casefold()).strip()
    if not folded:
        return True
    if folded in GIANT_BRANDS:
        return True
    tokens = set(folded.split())
    if tokens & GIANT_BRANDS and len(tokens) <= 2:
        return True
    if domain:
        host_root = domain.lower().split(".")[0]
        if host_root in GIANT_BRANDS:
            return True
    return False


def _extract_project_url_and_domain(snapshot: dict) -> tuple[str, str]:
    for raw in (snapshot.get("link_url"), snapshot.get("caption"), snapshot.get("website")):
        text = str(raw or "").strip()
        if not text:
            continue
        url = text if "://" in text else f"https://{text}"
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if host in APP_STORE_HOSTS:
            return (text if "://" in text else f"https://{text}"), ""
        dom = domain_of(text)
        if dom:
            return f"https://{dom}", dom
    return "", ""


def _is_video_ad(item: dict) -> bool:
    if item.get("is_active") is False or item.get("isActive") is False:
        return False
    snapshot = item.get("snapshot")
    if not isinstance(snapshot, dict):
        return False
    fmt = str(snapshot.get("display_format") or "").upper()
    videos = snapshot.get("videos")
    return fmt == "VIDEO" or (isinstance(videos, list) and len(videos) > 0)


def parse_ad_candidates(payload: dict | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
    items = (
        output.get("searchResults")
        or output.get("results")
        or payload.get("searchResults")
        or payload.get("results")
        or []
    )
    if not isinstance(items, list):
        return []
    candidates: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or not _is_video_ad(item):
            continue
        snapshot = item.get("snapshot") or {}
        page_name = str(snapshot.get("page_name") or item.get("page_name") or "").strip()
        if not page_name:
            continue
        body_obj = snapshot.get("body")
        body_text = ""
        if isinstance(body_obj, dict):
            body_text = str(body_obj.get("text") or "").strip()
        elif isinstance(body_obj, str):
            body_text = body_obj.strip()
        if not body_text:
            body_text = str(snapshot.get("title") or snapshot.get("link_description") or "").strip()
        body_text = re.sub(r"\s+", " ", body_text).strip()
        project_url, domain = _extract_project_url_and_domain(snapshot)
        if _is_giant_brand(page_name, domain):
            continue
        if is_supply_side(f"{page_name} {body_text}"):
            continue
        cta_text = str(snapshot.get("cta_text") or "Video Ad").strip() or "Video Ad"
        if body_text:
            what_they_do = f"Active Meta video ads ({cta_text}): {body_text}"[:200]
        else:
            what_they_do = f"Active Meta video ads ({cta_text})"[:200]
        candidates.append({
            "page_name": page_name,
            "domain": domain,
            "project_url": project_url,
            "what_they_do": what_they_do,
        })
    return candidates


def _default_search(query: str) -> dict | None:
    return call(
        ENDPOINT,
        query={
            "query": query,
            "trim": "true",
            "status": "ACTIVE",
            "country": "US",
            "media_type": "VIDEO",
        },
        timeout=60,
    )


def _scrape_site_contacts(project_url: str) -> dict[str, str]:
    if not project_url or not domain_of(project_url):
        return {}
    try:
        import requests
        from tg_to_gsheet import email_of_text, linkedin_in_url, x_profile_url
        from yc_ph_leads import extract_tg_from_html
    except ImportError:
        try:
            import requests
            from leadgen.tg_to_gsheet import email_of_text, linkedin_in_url, x_profile_url
            from leadgen.yc_ph_leads import extract_tg_from_html
        except ImportError:
            try:
                import requests
                from scripts.tg_to_gsheet import email_of_text, linkedin_in_url, x_profile_url
                from scripts.yc_ph_leads import extract_tg_from_html
            except ImportError:
                return {}
    try:
        resp = requests.get(
            project_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CreatorOS/1.0)"},
            timeout=8,
        )
        if not resp.ok:
            return {}
        html = resp.text or ""
    except Exception:
        return {}
    out: dict[str, str] = {}
    tg = extract_tg_from_html(html)
    if tg:
        out["username"] = tg
    li = linkedin_in_url(html)
    if li:
        out["founder_linkedin"] = li
    em = email_of_text(html)
    if em:
        out["work_email"] = em
    x_match = re.search(
        r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#\"'\s<>]|$)",
        html,
        re.I,
    )
    if x_match:
        x_url = x_profile_url({"founder_x": x_match.group(0)})
        if x_url:
            out["founder_x"] = x_url
    return out


def collect(
    *,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    cap: int = CAP,
    search_fn=None,
    enrich_fn=None,
) -> tuple[list[dict], dict[str, str]]:
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    caller = _default_search if search_fn is None else search_fn
    enricher = find_company_decision_maker if enrich_fn is None else enrich_fn

    rows: list[dict] = []
    local_keys: set[str] = set()

    for query in queries_for_day(today):
        if len(rows) >= cap:
            break
        try:
            payload = caller(query)
        except Exception:
            payload = None
        for cand in parse_ad_candidates(payload):
            if len(rows) >= cap:
                break
            page_name = cand["page_name"]
            domain = cand["domain"]
            name_key = f"name:{page_name.casefold()}"
            dom_key = f"domain:{domain.casefold()}" if domain else ""
            if name_key in current or name_key in local_keys:
                continue
            if dom_key and (dom_key in current or dom_key in local_keys):
                continue

            local_keys.add(name_key)
            if dom_key:
                local_keys.add(dom_key)

            try:
                dm = enricher(page_name, domain) or {}
            except Exception:
                dm = {}

            founder_linkedin = str(dm.get("founder_linkedin") or "").strip()
            work_email = str(dm.get("work_email") or "").strip()
            project_url = cand["project_url"] or str(dm.get("project_url") or "").strip()
            resolved_domain = domain or domain_of(project_url)
            site_contacts = (
                _scrape_site_contacts(project_url)
                if enrich_fn is None and resolved_domain
                else {}
            )
            if not founder_linkedin and site_contacts.get("founder_linkedin"):
                founder_linkedin = site_contacts["founder_linkedin"]
            if not work_email and site_contacts.get("work_email"):
                work_email = site_contacts["work_email"]
            username = site_contacts.get("username", "")
            founder_x = site_contacts.get("founder_x", "")

            if not (founder_linkedin or work_email or resolved_domain or project_url):
                continue

            slug = _slugify(resolved_domain or page_name)
            links = [u for u in (project_url, founder_linkedin, founder_x) if u]
            row = {
                "id": f"meta_ads:{slug}",
                "name": page_name,
                "username": username,
                "bio": cand["what_they_do"],
                "links": links,
                "source": "meta_ads",
                "role": "Marketing",
                "roles": ["Marketing"],
                "category": "SaaS / AI app",
                "project": page_name,
                "project_url": project_url,
                "what_they_do": cand["what_they_do"],
                "founder_linkedin": founder_linkedin,
                "founder_x": founder_x,
                "work_email": work_email,
                "icp_match": True,
                "lead_tier": "A",
            }
            rows.append(row)
            current[name_key] = stamp
            if dom_key:
                current[dom_key] = stamp
            elif resolved_domain:
                current[f"domain:{resolved_domain.casefold()}"] = stamp

    return rows, current


def _load_seen(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(raw, dict):
        inner = raw.get("advertisers")
        if isinstance(inner, dict):
            return {str(k): str(v) for k, v in inner.items()}
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    return {}


def _save_seen(path: Path, seen: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"advertisers": seen}, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--seen", default=".tg_pipeline/meta_ads_seen.json")
    parser.add_argument("--limit", type=int, default=CAP)
    parser.add_argument("--day", default="", help="YYYY-MM-DD, defaults to today")
    args = parser.parse_args()

    day = date.fromisoformat(args.day) if args.day else date.today()
    seen_path = Path(args.seen)
    before = _load_seen(seen_path)
    try:
        rows, updated = collect(day=day, seen=before, cap=max(0, args.limit))
    except Exception:
        rows, updated = [], before
    _save_seen(seen_path, updated)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"meta_ads: admitted={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
