#!/usr/bin/env python3
"""Lightweight Monid CLI client and App Store / Google Play developer resolver.

Calls go through the authenticated ``monid`` binary (~/.local/bin/monid or
/usr/local/bin/monid) so workspace tokens never enter this repository.
"""
from __future__ import annotations

import html as html_mod
import json
import os
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

try:
    from treg_client import call as treg_call, check_treg_ready
except ImportError:
    try:
        from leadgen.treg_client import call as treg_call, check_treg_ready
    except ImportError:
        from scripts.treg_client import call as treg_call, check_treg_ready

try:
    from treg_enrich import domain_of
except ImportError:
    try:
        from leadgen.treg_enrich import domain_of
    except ImportError:
        from scripts.treg_enrich import domain_of


MEGACORP_TOKENS = frozenset(
    {
        "openai",
        "chatgpt",
        "google",
        "gemini",
        "meta",
        "facebook",
        "instagram",
        "microsoft",
        "apple",
        "amazon",
        "netflix",
        "bytedance",
        "tiktok",
        "xai",
        "grok",
        "anthropic",
        "claude",
        "perplexity",
        "spotify",
        "adobe",
        "canva",
        "notion",
        "supercell",
        "activision",
        "blizzard",
        "callofduty",
        "playstation",
        "xbox",
        "nintendo",
        "genshin",
        "hoyoverse",
        "mihoyo",
        "fortnite",
        "epicgames",
        "roblox",
        "minecraft",
        "valorant",
        "riotgames",
        "ubisoft",
    }
)
MEGACORP_PHRASES = (
    "call of duty",
    "league of legends",
    "grand theft auto",
    "electronic arts",
    "epic games",
    "riot games",
)

_PRIVACY_NOISE_DOMAINS = frozenset(
    {
        "sites.google.com",
        "play.google.com",
        "support.google.com",
        "policies.google.com",
        "accounts.google.com",
        "apps.apple.com",
        "itunes.apple.com",
        "freeprivacypolicy.com",
        "app-privacy-policy.com",
        "termsfeed.com",
        "iubenda.com",
        "privacypolicies.com",
        "appbrain.com",
        "apkpure.com",
        "uptodown.com",
        "softonic.com",
        "cnet.com",
    }
)

_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_APPLE_ID_RE = re.compile(r"/id(\d+)\b")


def monid_bin() -> str | None:
    found = shutil.which("monid")
    if found:
        return found
    for candidate in (
        os.path.expanduser("~/.local/bin/monid"),
        "/usr/local/bin/monid",
        "/opt/crmtg/.venv/bin/monid",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def check_monid_ready(*, binary_fn=None) -> bool:
    """Return True iff monid CLI is installed, enabled, and backed by credentials."""
    if os.getenv("MONID_ENRICH", "1").strip().lower() in {"0", "false", "no"}:
        return False
    resolve_bin = monid_bin if binary_fn is None else binary_fn
    if not resolve_bin():
        return False
    if (os.environ.get("MONID_API_KEY") or "").strip():
        return True
    cfg_raw = (os.environ.get("MONID_CONFIG_PATH") or "").strip()
    candidates = (
        [os.path.expanduser(cfg_raw)]
        if cfg_raw
        else [
            os.path.expanduser("~/.config/monid/credentials.yaml"),
            "/root/.config/monid/credentials.yaml",
            os.path.expanduser("~/.monid/credentials.yaml"),
            "/root/.monid/credentials.yaml",
        ]
    )
    for cred_path in candidates:
        if not os.path.isfile(cred_path):
            continue
        try:
            content = open(cred_path, encoding="utf-8").read().strip()
        except OSError:
            continue
        if content:
            return True
    return False


def _parse_monid_stdout(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, dict):
        return {"items": parsed} if isinstance(parsed, list) else None

    status_val = str(parsed.get("status") or "").strip().upper()
    if status_val in {"FAILED", "ERROR", "CANCELLED"}:
        return None

    prov_resp = parsed.get("providerResponse")
    if isinstance(prov_resp, dict):
        try:
            if int(prov_resp.get("httpStatus") or 200) >= 400:
                return None
        except (TypeError, ValueError):
            pass

    if "output" in parsed:
        out = parsed.get("output")
        if out is None:
            return None
        if isinstance(out, dict):
            return out
        if isinstance(out, list):
            return {"items": out}
        return {"value": out}

    if "result" in parsed and isinstance(parsed.get("result"), dict):
        return parsed["result"]
    if "data" in parsed and isinstance(parsed.get("data"), dict):
        return parsed["data"]
    return parsed


def run_monid(
    provider: str,
    endpoint: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    wait_sec: int = 25,
    timeout: int = 35,
    binary_fn=None,
) -> dict[str, Any] | None:
    """Execute ``monid run`` synchronously and return unwrapped output dict."""
    if os.getenv("MONID_ENRICH", "1").strip().lower() in {"0", "false", "no"}:
        return None
    binary = (monid_bin if binary_fn is None else binary_fn)()
    if not binary:
        return None

    cmd = [
        binary,
        "run",
        "-p",
        provider,
        "-e",
        endpoint,
        "-w",
        str(int(wait_sec)),
        "-j",
    ]
    if query is not None:
        cmd.extend(["--query", json.dumps(query, ensure_ascii=False)])
    if body is not None:
        cmd.extend(["-i", json.dumps(body, ensure_ascii=False)])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _parse_monid_stdout(proc.stdout)


_COMPACT_COUNT_RE = re.compile(r"([\d.,]+)\s*([kmb])?\b", re.I)


def _parse_compact_count(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        return max(0, int(val))
    raw = str(val or "").strip()
    if not raw:
        return 0
    m = _COMPACT_COUNT_RE.search(raw)
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get((m.group(2) or "").lower(), 1)
    return max(0, int(round(num * mult)))


def _clean_external_website(raw_url: str) -> tuple[str, str]:
    text = html_mod.unescape(str(raw_url or "")).strip().rstrip(".,);\"'")
    if not text:
        return "", ""
    if "google.com/url" in text:
        q_vals = parse_qs(urlparse(text).query).get("q") or []
        if q_vals:
            text = unquote(q_vals[0]).strip()
    if "://" not in text and ":" not in text.split("/", 1)[0] and "." in text and " " not in text:
        text = "https://" + text.lstrip("/")
    if not text.lower().startswith(("http://", "https://")):
        return "", ""
    dom = domain_of(text)
    if not dom or dom in _PRIVACY_NOISE_DOMAINS or any(
        dom.endswith("." + bad) for bad in _PRIVACY_NOISE_DOMAINS
    ):
        return "", ""
    return text, dom


def is_megacorp_app(developer: str = "", app_name: str = "", domain: str = "", rating_count: int = 0) -> bool:
    if int(rating_count or 0) > 50000:
        return True
    blob = re.sub(r"[^a-z0-9]+", " ", f"{developer} {app_name} {domain}".casefold()).strip()
    if not blob:
        return False
    words = set(blob.split())
    if words & MEGACORP_TOKENS:
        return True
    if any(p in blob for p in MEGACORP_PHRASES):
        return True
    dom_root = (domain or "").lower().split(".")[0]
    return bool(dom_root and dom_root in MEGACORP_TOKENS)


def _default_http_get(url: str) -> Any:
    import requests

    return requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        },
        timeout=8,
    )


def _extract_json_ld_play(html_text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text or "",
        re.I | re.S,
    ):
        try:
            obj = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        items = obj if isinstance(obj, list) else [obj]
        for node in items:
            if not isinstance(node, dict):
                continue
            if node.get("name") and not out.get("app_name"):
                out["app_name"] = str(node["name"]).strip()
            author = node.get("author")
            if isinstance(author, dict) and author.get("name") and not out.get("developer"):
                out["developer"] = str(author["name"]).strip()
            elif isinstance(author, str) and author.strip() and not out.get("developer"):
                out["developer"] = author.strip()
            agg = node.get("aggregateRating")
            if isinstance(agg, dict) and agg.get("ratingCount") is not None:
                try:
                    out["rating_count"] = int(str(agg["ratingCount"]).replace(",", "").strip())
                except ValueError:
                    pass
    return out


def _extract_play_html_contacts(html_text: str) -> dict[str, Any]:
    res = _extract_json_ld_play(html_text)
    website = ""
    domain = ""
    for m in re.finditer(r'<a\b[^>]*\bhref=["\'](https?://[^"\']+)["\']', html_text or "", re.I):
        href = html_mod.unescape(m.group(1))
        if "google.com/url" in href or (
            "play.google.com" not in href
            and "gstatic.com" not in href
            and "policies.google" not in href
        ):
            w, d = _clean_external_website(href)
            if w and d:
                website, domain = w, d
                break

    email = ""
    for m_mail in re.finditer(r'href=["\']mailto:([^"\'?]+)', html_text or "", re.I):
        cand = html_mod.unescape(m_mail.group(1)).strip()
        if _EMAIL_RE.fullmatch(cand):
            email = cand
            break
    if not email:
        m_em = _EMAIL_RE.search(html_text or "")
        if m_em:
            cand = m_em.group(0)
            if not cand.lower().endswith((".png", ".jpg", ".svg")):
                email = cand

    res["website"] = website
    res["domain"] = domain
    res["email"] = email
    return res


def _search_developer_website(developer: str, app_name: str, search_fn) -> tuple[str, str]:
    dev_clean = " ".join(str(developer or "").split()).strip()
    app_clean = " ".join(str(app_name or "").split()).strip()
    if not dev_clean:
        return "", ""
    q = (
        f'"{dev_clean}" "{app_clean}" official website '
        "-play.google.com -appbrain -apkpure -uptodown -softonic -cnet"
        if app_clean
        else f'"{dev_clean}" official website -play.google.com -appbrain -apkpure'
    )
    try:
        payload = search_fn(q)
    except Exception:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    candidates: list[str] = []
    search_containers: list[Any] = [payload]
    if isinstance(payload.get("output"), dict):
        search_containers.append(payload["output"])
    if isinstance(payload.get("result"), dict):
        search_containers.append(payload["result"])
    elif isinstance(payload.get("result"), list):
        search_containers.append({"results": payload["result"]})
    for container in search_containers:
        for key in ("results", "items", "organic_results"):
            rows = container.get(key) if isinstance(container, dict) else None
            if isinstance(rows, list):
                for r in rows:
                    if isinstance(r, dict) and r.get("url"):
                        candidates.append(str(r["url"]))
    for cand in candidates:
        w, d = _clean_external_website(cand)
        if w and d:
            return w, d
    return "", ""


def _unwrap_store_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("output"), dict):
        return payload["output"]
    if isinstance(payload.get("result"), dict):
        return payload["result"]
    return payload


def _unwrap_dev_field(val: Any) -> tuple[str, str]:
    if isinstance(val, dict):
        name = str(val.get("name") or val.get("sellerName") or val.get("title") or "").strip()
        site = str(val.get("website") or val.get("sellerUrl") or val.get("url") or "").strip()
        return name, site
    return str(val or "").strip(), ""


def resolve_app_store_developer(
    store_url: str,
    *,
    monid_fn=None,
    treg_fn=None,
    http_get_fn=None,
    search_fn=None,
) -> dict[str, Any]:
    """Resolve an App Store / Google Play URL to developer metadata, website & rating scale."""
    raw_url = str(store_url or "").strip()
    if not raw_url:
        return {}
    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    host = (parsed.hostname or "").lower().removeprefix("www.")

    custom_mode = any(fn is not None for fn in (monid_fn, treg_fn, http_get_fn))
    get_http = _default_http_get if http_get_fn is None else http_get_fn

    # 1) Apple App Store
    if host in {"apps.apple.com", "itunes.apple.com"}:
        m_id = _APPLE_ID_RE.search(parsed.path or "")
        if not m_id:
            return {}
        app_id = m_id.group(1)
        developer = ""
        app_name = ""
        website = ""
        domain = ""
        rating_count = 0

        try:
            resp = get_http(f"https://itunes.apple.com/lookup?id={app_id}")
            if isinstance(resp, str):
                data = json.loads(resp)
            elif hasattr(resp, "json"):
                data = resp.json()
            elif isinstance(resp, dict):
                data = resp
            else:
                data = {}
            results = data.get("results") if isinstance(data, dict) else None
            if isinstance(results, list) and results:
                item = results[0] if isinstance(results[0], dict) else {}
                developer = str(item.get("sellerName") or item.get("artistName") or "").strip()
                app_name = str(item.get("trackName") or "").strip()
                website, domain = _clean_external_website(item.get("sellerUrl") or "")
                try:
                    rating_count = int(item.get("userRatingCount") or 0)
                except (TypeError, ValueError):
                    rating_count = 0
        except Exception:
            pass

        if not (developer and website):
            payload: dict[str, Any] | None = None
            if monid_fn is not None:
                payload = monid_fn("litescrape", "/app-store/product", query={"product_id": app_id})
            elif not custom_mode and check_monid_ready():
                payload = run_monid("litescrape", "/app-store/product", query={"product_id": app_id})
            if not payload:
                if treg_fn is not None:
                    payload = treg_fn("litescrape.app_store.product", query={"product_id": app_id})
                elif not custom_mode and check_treg_ready():
                    payload = treg_call("litescrape.app_store.product", query={"product_id": app_id})
            inner = _unwrap_store_payload(payload)
            if inner:
                pinfo = inner.get("product_info") if isinstance(inner.get("product_info"), dict) else inner
                dev_contact = inner.get("developer_contact") if isinstance(inner.get("developer_contact"), dict) else {}
                dev_name, dev_site = _unwrap_dev_field(
                    pinfo.get("developer") if pinfo.get("developer") is not None else inner.get("developer")
                )
                developer = developer or dev_name or str(pinfo.get("sellerName") or "").strip()
                app_name = app_name or str(pinfo.get("title") or inner.get("title") or "").strip()
                if not website:
                    website, domain = _clean_external_website(
                        dev_contact.get("website") or pinfo.get("sellerUrl") or inner.get("website") or dev_site or ""
                    )
                if not rating_count:
                    ratings_dict = inner.get("ratings") if isinstance(inner.get("ratings"), dict) else {}
                    rating_count = _parse_compact_count(
                        pinfo.get("rating_count")
                        or inner.get("rating_count")
                        or ratings_dict.get("total_count")
                    )

        if not (developer or app_name or website):
            return {}
        return {
            "developer": developer,
            "app_name": app_name,
            "website": website,
            "domain": domain,
            "email": "",
            "rating_count": rating_count,
            "is_megacorp": is_megacorp_app(developer, app_name, domain, rating_count),
        }

    # 2) Google Play Store
    if host == "play.google.com":
        pkg = (parse_qs(parsed.query).get("id") or [""])[0].strip()
        if not pkg:
            return {}

        developer = ""
        app_name = ""
        website = ""
        domain = ""
        email = ""
        rating_count = 0

        payload = None
        if monid_fn is not None:
            payload = monid_fn("litescrape", "/google-play/product", query={"product_id": pkg})
        elif not custom_mode and check_monid_ready():
            payload = run_monid("litescrape", "/google-play/product", query={"product_id": pkg})

        if not payload:
            if treg_fn is not None:
                payload = treg_fn("litescrape.google_play.product", query={"product_id": pkg})
            elif not custom_mode and check_treg_ready():
                payload = treg_call("litescrape.google_play.product", query={"product_id": pkg})

        inner = _unwrap_store_payload(payload)
        if inner:
            pinfo = inner.get("product_info") if isinstance(inner.get("product_info"), dict) else {}
            about = inner.get("about_this_app") if isinstance(inner.get("about_this_app"), dict) else {}
            dev_c = inner.get("developer_contact") if isinstance(inner.get("developer_contact"), dict) else {}
            ratings = inner.get("ratings") if isinstance(inner.get("ratings"), dict) else {}

            app_name = str(pinfo.get("title") or inner.get("title") or "").strip()
            dev_name, dev_site = _unwrap_dev_field(
                pinfo.get("developer") if pinfo.get("developer") is not None else inner.get("developer")
            )
            developer = str(
                dev_name
                or about.get("offered_by")
                or dev_c.get("name")
                or ""
            ).strip()
            website, domain = _clean_external_website(
                dev_c.get("website") or pinfo.get("developer_website") or dev_site or ""
            )
            raw_em = str(dev_c.get("support_email") or dev_c.get("alternative_email") or "").strip()
            if _EMAIL_RE.fullmatch(raw_em):
                email = raw_em
            raw_rc = pinfo.get("reviews") or ratings.get("total_count") or ratings.get("rating_count") or 0
            rating_count = _parse_compact_count(raw_rc)

        if not (developer and website) and (http_get_fn is not None or not custom_mode):
            try:
                resp = get_http(f"https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=US")
                html_txt = resp if isinstance(resp, str) else str(getattr(resp, "text", "") or "")
                if html_txt:
                    parsed_html = _extract_play_html_contacts(html_txt)
                    developer = developer or str(parsed_html.get("developer") or "").strip()
                    app_name = app_name or str(parsed_html.get("app_name") or "").strip()
                    if not website and parsed_html.get("website"):
                        website = str(parsed_html["website"])
                        domain = str(parsed_html["domain"])
                    email = email or str(parsed_html.get("email") or "").strip()
                    if not rating_count:
                        rating_count = int(parsed_html.get("rating_count") or 0)
            except Exception:
                pass

        if not website and developer:
            if search_fn is not None:
                website, domain = _search_developer_website(developer, app_name, search_fn)
            elif not custom_mode and check_treg_ready():
                website, domain = _search_developer_website(
                    developer,
                    app_name,
                    lambda q: treg_call("crawl4ai.web.search", query={"q": q}),
                )

        if not (developer or app_name or website or email):
            return {}
        return {
            "developer": developer,
            "app_name": app_name,
            "website": website,
            "domain": domain,
            "email": email,
            "rating_count": rating_count,
            "is_megacorp": is_megacorp_app(developer, app_name, domain, rating_count),
        }

    return {}
