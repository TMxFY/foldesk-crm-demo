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
    from supply_side import is_supply_side, sanitize_person_and_company, sniper_reject_reason
except ImportError:
    try:
        from leadgen.supply_side import is_supply_side, sanitize_person_and_company, sniper_reject_reason
    except ImportError:
        from scripts.supply_side import is_supply_side, sanitize_person_and_company, sniper_reject_reason

try:
    from treg_enrich import domain_of, find_company_decision_maker
except ImportError:
    try:
        from leadgen.treg_enrich import domain_of, find_company_decision_maker
    except ImportError:
        from scripts.treg_enrich import domain_of, find_company_decision_maker

ENDPOINT = "scrapecreators.x.v1-facebook-adlibrary-search-ads"
SEARCH_QUERIES = (
    # Cluster 1: Gaming / Roblox / Fortnite / FiveM
    "roblox game",
    "fortnite map code",
    "mobile RPG game",
    "fivem roleplay server",
    # Cluster 2: Consumer AI subscription apps (astrology, coaching, language, fitness, video)
    "AI coaching app",
    "AI astrology app",
    "AI language learning",
    "fitness tracker app",
    "calorie tracker AI",
    "AI note taker",
    "AI video editor",
    # Cluster 3: E-com / Shopify D2C (cosmetics/skincare, supplements, household cleaning)
    "skincare routine serum",
    "daily supplement gummies",
    "eco household detergent",
    "shopify beauty brand",
    # Cluster 4: Crypto B2C wallets, trackers & prop trading
    "crypto wallet app",
    "crypto portfolio tracker",
    "prop trading challenge",
    "funded trader challenge",
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
        ad_blob = f"{page_name} {body_text}"
        if is_supply_side(ad_blob) or sniper_reject_reason(ad_blob):
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
            headers={"User-Agent": "Mozilla/5.0 (compatible; SoftUGC/1.0)"},
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
    x_skip = {"home", "search", "explore", "i", "share", "intent", "hashtag", "compose"}
    for x_match in re.finditer(
        r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#\"'\s<>]|$)",
        html,
        re.I,
    ):
        handle = x_match.group(1)
        if handle.casefold() in x_skip:
            continue
        x_url = x_profile_url({"founder_x": f"https://x.com/{handle}"})
        if x_url:
            out["founder_x"] = x_url
            break
    return out


def _extract_apple_app_id(url: str) -> str:
    m = re.search(r"/id(\d+)\b", str(url or ""))
    return m.group(1) if m else ""


def _builtin_store_resolver(store_url: str) -> dict:
    import html as html_mod
    from urllib.parse import parse_qs, unquote

    import requests

    raw = str(store_url or "").strip()
    if not raw:
        return {}
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    mega_re = re.compile(
        r"\b(?:openai|chatgpt|google|gemini|meta|facebook|instagram|microsoft|apple|amazon|netflix|bytedance|tiktok|xai|grok|anthropic|claude|supercell)\b",
        re.I,
    )

    if host in {"apps.apple.com", "itunes.apple.com"}:
        app_id = _extract_apple_app_id(raw)
        if not app_id:
            return {}
        try:
            resp = requests.get(f"https://itunes.apple.com/lookup?id={app_id}", timeout=6)
            data = resp.json() if hasattr(resp, "json") else json.loads(getattr(resp, "text", "{}") or "{}")
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list) or not results:
                return {}
            item = results[0] if isinstance(results[0], dict) else {}
            dev = str(item.get("sellerName") or item.get("artistName") or "").strip()
            app_name = str(item.get("trackName") or "").strip()
            website = str(item.get("sellerUrl") or "").strip()
            dom = domain_of(website)
            rc = int(item.get("userRatingCount") or 0)
            return {
                "developer": dev,
                "app_name": app_name,
                "website": website,
                "domain": dom,
                "email": "",
                "rating_count": rc,
                "is_megacorp": bool(rc > 50000 or mega_re.search(f"{dev} {app_name} {dom}")),
            }
        except Exception:
            return {}

    if host == "play.google.com":
        pkg = (parse_qs(parsed.query).get("id") or [""])[0].strip()
        if not pkg:
            return {}
        try:
            resp = requests.get(
                f"https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=US",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=6,
            )
            html_txt = str(getattr(resp, "text", "") or "")
        except Exception:
            return {}
        developer = ""
        app_name = ""
        rating_count = 0
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_txt, re.I | re.S):
            try:
                obj = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            nodes = obj if isinstance(obj, list) else [obj]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("name") and not app_name:
                    app_name = str(node["name"]).strip()
                auth = node.get("author")
                if isinstance(auth, dict) and auth.get("name") and not developer:
                    developer = str(auth["name"]).strip()
                agg = node.get("aggregateRating")
                if isinstance(agg, dict) and agg.get("ratingCount") is not None:
                    try:
                        rating_count = int(str(agg["ratingCount"]).replace(",", "").strip())
                    except ValueError:
                        pass
        website = ""
        dom = ""
        for m_href in re.finditer(r'<a\b[^>]*\bhref=["\'](https?://[^"\']+)["\']', html_txt, re.I):
            href = html_mod.unescape(m_href.group(1))
            if "google.com/url" in href:
                q_vals = parse_qs(urlparse(href).query).get("q") or []
                if q_vals:
                    href = unquote(q_vals[0]).strip()
            cand_dom = domain_of(href)
            if cand_dom and not any(bad in cand_dom for bad in ("play.google.com", "gstatic.com", "policies.google.com", "support.google.com")):
                website = href
                dom = cand_dom
                break
        email = ""
        m_mail = re.search(r'href=["\']mailto:([^"\'?]+)', html_txt, re.I)
        if m_mail:
            email = html_mod.unescape(m_mail.group(1)).strip()
        if not (developer or website or email):
            return {}
        return {
            "developer": developer,
            "app_name": app_name,
            "website": website,
            "domain": dom,
            "email": email,
            "rating_count": rating_count,
            "is_megacorp": bool(rating_count > 50000 or mega_re.search(f"{developer} {app_name} {dom}")),
        }

    return {}


def _default_store_resolver(store_url: str) -> dict:
    try:
        from leadgen.monid_client import resolve_app_store_developer
    except ImportError:
        try:
            from monid_client import resolve_app_store_developer
        except ImportError:
            resolve_app_store_developer = None
    if resolve_app_store_developer is not None:
        return resolve_app_store_developer(store_url)
    return _builtin_store_resolver(store_url)


def collect(
    *,
    day: date | None = None,
    seen: dict[str, str] | None = None,
    cap: int = CAP,
    search_fn=None,
    enrich_fn=None,
    store_resolver_fn=None,
) -> tuple[list[dict], dict[str, str]]:
    today = day or date.today()
    current = fresh_seen(seen or {}, today)
    stamp = today.isoformat()
    caller = _default_search if search_fn is None else search_fn
    enricher = find_company_decision_maker if enrich_fn is None else enrich_fn
    resolve_store = _default_store_resolver if store_resolver_fn is None else store_resolver_fn
    allow_store_bridge = bool(store_resolver_fn is not None or enrich_fn is None)

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
            orig_project_url = cand["project_url"]
            orig_host = (
                urlparse(orig_project_url if "://" in orig_project_url else f"https://{orig_project_url}").hostname or ""
            ).lower().removeprefix("www.")
            store_url = orig_project_url if orig_host in APP_STORE_HOSTS else ""

            name_key = f"name:{page_name.casefold()}"
            dom_key = f"domain:{domain.casefold()}" if domain else ""
            if name_key in current or name_key in local_keys:
                continue
            if dom_key and (dom_key in current or dom_key in local_keys):
                continue

            store_info: dict = {}
            resolved_domain = domain
            resolved_project_url = orig_project_url
            if store_url and not domain and allow_store_bridge:
                try:
                    store_info = resolve_store(store_url) or {}
                except Exception:
                    store_info = {}
                try:
                    store_rating = int(store_info.get("rating_count") or 0)
                except (TypeError, ValueError):
                    store_rating = 0
                if store_info.get("is_megacorp") or store_rating > 50000:
                    continue
                if store_info.get("website"):
                    resolved_project_url = str(store_info["website"]).strip()
                resolved_domain = str(store_info.get("domain") or domain_of(resolved_project_url)).strip()
                if resolved_domain:
                    res_dom_key = f"domain:{resolved_domain.casefold()}"
                    if res_dom_key in current or res_dom_key in local_keys:
                        continue

            local_keys.add(name_key)
            if dom_key:
                local_keys.add(dom_key)
            if resolved_domain:
                local_keys.add(f"domain:{resolved_domain.casefold()}")

            enrich_target = str(store_info.get("developer") or page_name).strip()
            try:
                dm = enricher(enrich_target, resolved_domain) or {}
            except Exception:
                dm = {}

            founder_linkedin = str(dm.get("founder_linkedin") or "").strip()
            work_email = str(dm.get("work_email") or "").strip()
            if resolved_project_url and resolved_project_url != store_url:
                project_url = resolved_project_url
            else:
                project_url = str(dm.get("project_url") or "").strip() or orig_project_url
            resolved_domain = resolved_domain or domain_of(project_url)
            site_contacts = (
                _scrape_site_contacts(project_url)
                if (enrich_fn is None or store_resolver_fn is not None) and resolved_domain
                else {}
            )
            if not founder_linkedin and site_contacts.get("founder_linkedin"):
                founder_linkedin = site_contacts["founder_linkedin"]
            if not work_email and site_contacts.get("work_email"):
                work_email = site_contacts["work_email"]
            if not work_email and store_info.get("email"):
                work_email = str(store_info["email"]).strip()
            username = site_contacts.get("username", "")
            founder_x = site_contacts.get("founder_x", "")

            if not (founder_linkedin or work_email or resolved_domain or project_url):
                continue

            slug = _slugify(resolved_domain or page_name)
            links = list(dict.fromkeys(u for u in (project_url, store_url, founder_linkedin, founder_x) if u))
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
