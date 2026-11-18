from __future__ import annotations

import copy
import glob
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.db import CANONICAL_STEP3_IMAGE_URL, ROOT, as_dict, utc_now, utc_today
from app.outreach import create_campaign, get_campaign, update_campaign
from leadgen.outreach_copy import VLAD_STEP2_TEMPLATE, VLAD_STEP3_TEXT
from leadgen.tg_to_gsheet import whatsapp_url

CASES_BANK_PATH = ROOT / "leadgen" / "job_cases_bank.md"


def _resolve_config_json_path() -> Path:
    custom = os.environ.get("LEADGEN_CONFIG_PATH", "").strip()
    if custom:
        return Path(custom)
    return ROOT / "data" / "leadgen_config.json"


def _resolve_state_dir() -> Path:
    custom = os.environ.get("LEADGEN_STATE_DIR", "").strip()
    if custom:
        return Path(custom)
    return ROOT / "data" / "leadgen_state"


def _read_cases_bank() -> str:
    if CASES_BANK_PATH.exists():
        try:
            return CASES_BANK_PATH.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


DEFAULT_LEADGEN_CONFIG: dict[str, Any] = {
    "enabled": True,
    "auto_push_to_crm": True,
    "total_daily_quota": 50,
    "b2b": {
        "enabled": True,
        "touch_mode": "direct",  # "direct" | "micro_ping"
        "target_campaign_tg": "Leadgen Auto (TG)",
        "target_campaign_wa": "Leadgen Auto (WA)",
        "target_campaign_ofm": "SoftUGC — OFM (TG)",
        "ofm_outreach_enabled": False,
        "ofm_daily_limit": 25,
        "campaign_daily_limit": 30,
        "sources": {
            "tg_groups": True,
            "tg_comments": True,
            "channel_graph": True,
            "tg_ad_spy": True,
            "whop_content_rewards": True,
            "tiktok_ads": True,
            "tiktok_shop": True,
            "app_stores": True,
            "ats_hiring": True,
            "ph_detailed": True,
            "d2c_whatsapp": True,
            "ofm_groups": True,
            "facebook_groups": True,
            "meta_ads": True,
            "yc_ph": True,
            "social_x_reddit": True,
            "buyer_catalogs": True,
            "linkedin_jobs": True,
            "tg_identity_bridge": True,
        },
        "tg_groups_old": ["producthunt_heroes", "id1613398776"],
        "tg_groups_new": [
            "productcircle_chat",
            "solofounderschat",
            "saasfounders",
            "indiehackers",
            "saas_chat",
            "founders_chat",
            "startupchat",
            "buildinpublic",
            "appfounders",
            "marketing_chat",
        ],
        "tg_comment_channels": [],
        "ofm_tg_groups": [
            "FranceOFM",
            "ofmfrancenetwork",
            "networkOFM",
            "mofmnetwork",
            "RedditWorldOFM",
            "aimodelnetwork",
            "AdultDeskAI",
        ],
        "fb_group_urls": [
            "https://www.facebook.com/groups/ShopifyStorePros",
            "https://www.facebook.com/groups/UnofficialShopifyPodcast",
            "https://www.facebook.com/groups/startugc",
            "https://www.facebook.com/groups/278248382953814",
            "https://www.facebook.com/groups/595540634883097",
        ],
        "limits": {
            "yc_limit": 50,
            "ph_limit": 0,
            "msg_limit": 1800,
            "lurker_limit": 1000,
            "external_limit": 0,
        },
    },
    "vacancies": {
        "enabled": False,
        "touch_mode": "direct",  # "direct" | "micro_ping"
        "campaign_name": "TG Вакансии",
        "daily_limit": 20,
        "remote_only": True,
        "hot_priority": True,
        "layers": {
            "AI-контент": True,
            "автоматизация": True,
            "трафик": True,
            "AI-роль": True,
        },
        "source_words": (
            r"ваканс|vacanc|работ[аыу]|\bjob|фриланс|freelance|биржа|удал[её]н|подработ"
            r"|\bhr\b|hr'?tg|найм|hiring|карьер|career|заказ"
        ),
        "source_stop": r"kurilo|аренда|прокат|spam|premium bot|wallet",
        "extra_chats": ["itconnectbelgrade"],
        "pause_between_chats": 1.5,
    },
    "cases_bank_md": "",
}


def _clean_chat_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = re.split(r"[\n,;]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(x) for x in raw]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        val = str(p or "").strip()
        if not val:
            continue
        val = re.sub(r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/", "", val, flags=re.I)
        val = re.sub(r"^(?:t\.me|telegram\.me)/", "", val, flags=re.I)
        val = val.strip().lstrip("@").strip("/")
        if not val:
            continue
        low = val.lower()
        if low not in seen:
            seen.add(low)
            out.append(val)
    return out


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _normalize_config(cfg: dict[str, Any]) -> dict[str, Any]:
    merged = _deep_merge(DEFAULT_LEADGEN_CONFIG, cfg or {})
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["auto_push_to_crm"] = bool(merged.get("auto_push_to_crm", True))

    try:
        total_quota = int(merged.get("total_daily_quota", 50))
    except (TypeError, ValueError):
        raise ValueError("total_daily_quota должна быть целым числом от 0 до 500") from None
    if not (0 <= total_quota <= 500):
        raise ValueError("total_daily_quota должна быть от 0 до 500")
    merged["total_daily_quota"] = total_quota

    b2b = merged.get("b2b") or {}
    b2b["enabled"] = bool(b2b.get("enabled", True))
    b2b_mode = str(b2b.get("touch_mode") or "direct").strip().lower()
    if b2b_mode in ("full", "micro_ping"):
        b2b_mode = "direct"
    if b2b_mode != "direct":
        raise ValueError("b2b.touch_mode должен быть 'direct'")
    b2b["touch_mode"] = b2b_mode

    b2b["target_campaign_tg"] = str(b2b.get("target_campaign_tg") or "Leadgen Auto (TG)").strip() or "Leadgen Auto (TG)"
    b2b["target_campaign_wa"] = str(b2b.get("target_campaign_wa") or "Leadgen Auto (WA)").strip() or "Leadgen Auto (WA)"
    b2b["target_campaign_ofm"] = str(b2b.get("target_campaign_ofm") or "SoftUGC — OFM (TG)").strip() or "SoftUGC — OFM (TG)"
    b2b["ofm_outreach_enabled"] = bool(b2b.get("ofm_outreach_enabled", False))
    try:
        ofm_limit = int(b2b.get("ofm_daily_limit", 25))
    except (TypeError, ValueError):
        raise ValueError("b2b.ofm_daily_limit должен быть от 1 до 500") from None
    if not (1 <= ofm_limit <= 500):
        raise ValueError("b2b.ofm_daily_limit должен быть от 1 до 500")
    b2b["ofm_daily_limit"] = ofm_limit

    try:
        b2b_limit = int(b2b.get("campaign_daily_limit", 30))
    except (TypeError, ValueError):
        raise ValueError("b2b.campaign_daily_limit должен быть от 1 до 500") from None
    if not (0 <= b2b_limit <= 500):
        raise ValueError("b2b.campaign_daily_limit должен быть от 0 до 500")
    b2b["campaign_daily_limit"] = b2b_limit

    raw_sources = b2b.get("sources") or {}
    caller_sources = ((cfg or {}).get("b2b") or {}).get("sources")
    new_vein_keys = {
        "whop_content_rewards",
        "tiktok_ads",
        "tiktok_shop",
        "tg_identity_bridge",
        "app_stores",
        "ats_hiring",
        "ph_detailed",
        "d2c_whatsapp",
        "ofm_groups",
        "facebook_groups",
    }
    restricted_legacy_cfg = (
        isinstance(caller_sources, dict)
        and len(caller_sources) > 0
        and caller_sources.get("meta_ads") is False
        and caller_sources.get("social_x_reddit") is False
    )
    norm_sources = {}
    for sk, default_v in DEFAULT_LEADGEN_CONFIG["b2b"]["sources"].items():
        if isinstance(caller_sources, dict) and sk in caller_sources:
            norm_sources[sk] = bool(caller_sources[sk])
        elif restricted_legacy_cfg and sk in new_vein_keys:
            norm_sources[sk] = False
        else:
            norm_sources[sk] = bool(raw_sources.get(sk, default_v))
    for sk, sv in raw_sources.items():
        if sk not in norm_sources:
            norm_sources[str(sk)] = bool(sv)
    b2b["sources"] = norm_sources

    b2b["tg_groups_old"] = _clean_chat_list(b2b.get("tg_groups_old", DEFAULT_LEADGEN_CONFIG["b2b"]["tg_groups_old"]))
    b2b["tg_groups_new"] = _clean_chat_list(b2b.get("tg_groups_new", DEFAULT_LEADGEN_CONFIG["b2b"]["tg_groups_new"]))
    b2b["tg_comment_channels"] = _clean_chat_list(b2b.get("tg_comment_channels", []))
    b2b["ofm_tg_groups"] = _clean_chat_list(b2b.get("ofm_tg_groups", DEFAULT_LEADGEN_CONFIG["b2b"]["ofm_tg_groups"]))
    raw_fb_urls = b2b.get("fb_group_urls", [])
    clean_fb: list[str] = []
    if isinstance(raw_fb_urls, str):
        raw_fb_urls = [u.strip() for u in re.split(r"[\s,]+", raw_fb_urls) if u.strip()]
    if isinstance(raw_fb_urls, list):
        for fb_u in raw_fb_urls:
            s_fb = str(fb_u or "").strip()
            if not s_fb.startswith(("http://", "https://")):
                continue
            parsed_fb = urlparse(s_fb)
            fb_host = (parsed_fb.hostname or "").lower().removeprefix("www.").rstrip(".")
            if (fb_host == "facebook.com" or fb_host.endswith(".facebook.com") or fb_host == "fb.com") and "/groups/" in parsed_fb.path.lower():
                if s_fb not in clean_fb:
                    clean_fb.append(s_fb)
    b2b["fb_group_urls"] = clean_fb

    raw_limits = b2b.get("limits") or {}
    norm_limits = {}
    for lk, def_val in DEFAULT_LEADGEN_CONFIG["b2b"]["limits"].items():
        try:
            lv = int(raw_limits.get(lk, def_val))
        except (TypeError, ValueError):
            raise ValueError(f"Лимит {lk} должен быть неотрицательным числом") from None
        if lv < 0:
            raise ValueError(f"Лимит {lk} не может быть отрицательным")
        norm_limits[lk] = lv
    b2b["limits"] = norm_limits
    merged["b2b"] = b2b

    vac = merged.get("vacancies") or {}
    vac["enabled"] = bool(vac.get("enabled", False))
    vac_mode = str(vac.get("touch_mode") or "direct").strip().lower()
    if vac_mode == "full":
        vac_mode = "direct"
    if vac_mode not in ("direct", "micro_ping"):
        raise ValueError("vacancies.touch_mode должен быть 'direct' или 'micro_ping'")
    vac["touch_mode"] = vac_mode
    vac["campaign_name"] = str(vac.get("campaign_name") or "TG Вакансии").strip() or "TG Вакансии"

    try:
        vac_limit = int(vac.get("daily_limit", 20))
    except (TypeError, ValueError):
        raise ValueError("vacancies.daily_limit должен быть от 0 до 500") from None
    if not (0 <= vac_limit <= 500):
        raise ValueError("vacancies.daily_limit должен быть от 0 до 500")
    vac["daily_limit"] = vac_limit
    vac["remote_only"] = bool(vac.get("remote_only", True))
    vac["hot_priority"] = bool(vac.get("hot_priority", True))

    raw_layers = vac.get("layers") or {}
    norm_layers = {}
    for layer_k, def_l in DEFAULT_LEADGEN_CONFIG["vacancies"]["layers"].items():
        norm_layers[layer_k] = bool(raw_layers.get(layer_k, def_l))
    for layer_k, layer_v in raw_layers.items():
        if layer_k not in norm_layers:
            norm_layers[str(layer_k)] = bool(layer_v)
    vac["layers"] = norm_layers

    vac["source_words"] = str(
        vac.get("source_words") or DEFAULT_LEADGEN_CONFIG["vacancies"]["source_words"]
    ).strip()
    vac["source_stop"] = str(
        vac.get("source_stop") or DEFAULT_LEADGEN_CONFIG["vacancies"]["source_stop"]
    ).strip()
    vac["extra_chats"] = _clean_chat_list(vac.get("extra_chats", ["itconnectbelgrade"]))

    try:
        pause = float(vac.get("pause_between_chats", 1.5))
    except (TypeError, ValueError):
        pause = 1.5
    vac["pause_between_chats"] = max(0.0, round(pause, 2))
    merged["vacancies"] = vac

    if not merged.get("cases_bank_md"):
        merged["cases_bank_md"] = _read_cases_bank()
    else:
        merged["cases_bank_md"] = str(merged["cases_bank_md"])

    return merged


def find_existing_softugc_tg_campaign(conn: sqlite3.Connection | None) -> dict[str, Any] | None:
    """Find existing non-vacancy, non-WA, non-OFM SoftUGC TG B2B campaign (preferring 'SoftUGC — База Все2*')."""
    if conn is None:
        return None
    try:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY id ASC").fetchall()
    except Exception:
        return None

    from app.hygiene import is_ofm_campaign_name

    fallback_softugc: dict[str, Any] | None = None
    for row in rows:
        cdict = as_dict(row)
        cname = str(cdict.get("name") or "").strip()
        cname_low = cname.lower()
        if not cname_low:
            continue
        if any(k in cname_low for k in ("ваканс", "vacanc", "job")):
            continue
        if "(wa)" in cname_low or "whatsapp" in cname_low:
            continue
        if is_ofm_campaign_name(cname, cdict.get("leadgen_sources_json")):
            continue
        if cname_low.startswith("softugc — база все2"):
            return cdict
        if fallback_softugc is None and "softugc" in cname_low:
            fallback_softugc = cdict
    return fallback_softugc


def resolve_target_b2b_tg_campaign_name(
    conn: sqlite3.Connection | None,
    requested_name: str | None = None,
    *,
    fallback_default: str = "Leadgen Auto (TG)",
) -> str:
    """Resolve TG B2B campaign name: if empty/default, prefer existing 'SoftUGC — База Все2*' / non-OFM 'SoftUGC*' in DB."""
    from app.hygiene import is_ofm_campaign_name

    clean_req = str(requested_name or "").strip()
    req_low = clean_req.lower()
    if clean_req and is_ofm_campaign_name(clean_req):
        clean_req = ""
        req_low = ""
    if clean_req and req_low not in ("leadgen auto (tg)", "leadgen autopilot"):
        return clean_req
    found = find_existing_softugc_tg_campaign(conn)
    if found is not None and str(found.get("name") or "").strip():
        return str(found["name"]).strip()
    return clean_req or fallback_default


def get_leadgen_config(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    stored: dict[str, Any] = {}
    json_path = _resolve_config_json_path()
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                stored = _deep_merge(stored, data)
        except Exception:
            pass

    if conn is not None:
        try:
            row = conn.execute(
                "SELECT value FROM kv_settings WHERE key = 'leadgen_config'"
            ).fetchone()
            if row and row["value"]:
                db_data = json.loads(row["value"])
                if isinstance(db_data, dict):
                    stored = _deep_merge(stored, db_data)
        except Exception:
            pass

    cfg = _normalize_config(stored)
    if conn is not None:
        cfg["b2b"]["target_campaign_tg"] = resolve_target_b2b_tg_campaign_name(
            conn,
            cfg["b2b"].get("target_campaign_tg"),
            fallback_default="Leadgen Auto (TG)",
        )
    disk_bank = _read_cases_bank()
    if disk_bank and not stored.get("cases_bank_md"):
        cfg["cases_bank_md"] = disk_bank
    return cfg


def save_leadgen_config(
    conn: sqlite3.Connection | None,
    payload: dict[str, Any],
    *,
    sync_cases_file: bool = False,
    sync_campaign_limits: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Конфигурация должна быть объектом JSON")
    inner = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    current = get_leadgen_config(conn)
    merged = _deep_merge(current, inner)
    inner_sources = (inner.get("b2b") or {}).get("sources")
    new_vein_keys = (
        "whop_content_rewards",
        "tiktok_ads",
        "tiktok_shop",
        "tg_identity_bridge",
        "app_stores",
        "ats_hiring",
        "ph_detailed",
        "d2c_whatsapp",
        "ofm_groups",
        "facebook_groups",
    )
    if (
        isinstance(inner_sources, dict)
        and inner_sources.get("meta_ads") is False
        and inner_sources.get("social_x_reddit") is False
        and not any(k in inner_sources for k in new_vein_keys)
    ):
        for new_k in new_vein_keys:
            merged.setdefault("b2b", {}).setdefault("sources", {})[new_k] = False
    normalized = _normalize_config(merged)

    should_write_cases_file = bool(
        sync_cases_file
        or payload.get("sync_cases_file")
        or os.environ.get("LEADGEN_WRITE_CASES_BANK") == "1"
    )
    if isinstance(inner.get("cases_bank_md"), str) and inner["cases_bank_md"].strip():
        normalized["cases_bank_md"] = inner["cases_bank_md"]
        if should_write_cases_file:
            try:
                CASES_BANK_PATH.parent.mkdir(parents=True, exist_ok=True)
                CASES_BANK_PATH.write_text(normalized["cases_bank_md"], encoding="utf-8")
            except OSError:
                pass

    now = utc_now()
    serialized = json.dumps(normalized, ensure_ascii=False, indent=2)

    if conn is not None:
        conn.execute(
            """
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES ('leadgen_config', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (serialized, now),
        )
        conn.commit()
        if bool(sync_campaign_limits or payload.get("sync_campaign_limits")):
            sync_leadgen_campaigns(conn, normalized, sync_campaign_limits=True)

    json_path = _resolve_config_json_path()
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(serialized, encoding="utf-8")
    except OSError:
        pass

    return normalized


def get_leadgen_status(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    state_dir = _resolve_state_dir()
    last_run_summary: dict[str, Any] | None = None
    summary_file = state_dir / "last_run_summary.json"
    if summary_file.exists():
        try:
            data = json.loads(summary_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                last_run_summary = data
        except Exception:
            pass

    latest_vacancy_report: dict[str, Any] | None = None
    if state_dir.is_dir():
        candidates = sorted(
            glob.glob(str(state_dir / "job_monitor_[0-9]*.json")),
            key=lambda p: (os.path.basename(p), os.path.getmtime(p)),
        )
        if candidates:
            latest_path = Path(candidates[-1])
            try:
                items = json.loads(latest_path.read_text(encoding="utf-8"))
                count = len(items) if isinstance(items, list) else 0
                hot_count = (
                    sum(1 for it in items if isinstance(it, dict) and it.get("hot"))
                    if isinstance(items, list)
                    else 0
                )
                latest_vacancy_report = {
                    "file": latest_path.name,
                    "path": str(latest_path),
                    "vacancies_count": count,
                    "hot_count": hot_count,
                }
            except Exception:
                pass

    from app.hygiene import read_direct_x_leads, read_ofm_review_leads

    direct_x_payload = read_direct_x_leads(state_dir, limit=12)
    direct_x_leads_count = int(direct_x_payload.get("total") or 0)
    recent_direct_x_leads = list(direct_x_payload.get("items") or [])[-12:]
    ofm_review_payload = read_ofm_review_leads(state_dir, limit=25)
    ofm_review_count = int(ofm_review_payload.get("total") or 0)
    recent_ofm_review_leads = list(ofm_review_payload.get("items") or [])[:25]
    recent_b2b_leads: list[dict[str, Any]] = []
    wa_leads_count = 0
    crm_counts = {
        "total_leadgen_leads": 0,
        "vacancies_leads": 0,
        "b2b_leads": 0,
        "whatsapp_leads_count": 0,
        "active_leadgen_campaigns": 0,
        "direct_x_leads_count": direct_x_leads_count,
        "ofm_review_count": ofm_review_count,
    }
    if conn is not None:
        try:
            rows = conn.execute(
                """
                SELECT lead_source, COALESCE(provider, 'telegram') AS provider, COALESCE(username, '') AS username
                FROM leads
                WHERE lead_source IS NOT NULL AND trim(lead_source) != ''
                """
            ).fetchall()
            vac_c = 0
            b2b_c = 0
            b2b_kws = (
                "leadgen",
                "google sheet",
                "yc",
                "product hunt",
                "meta",
                "tiktok",
                "tg",
                "ad spy",
                "channel",
                "social",
                "reddit",
                "whop",
                "catalog",
                "linkedin",
                "app_store",
                "app_stores",
                "ats_hiring",
                "ph_detailed",
                "d2c_whatsapp",
                "shopify",
                "fb_groups",
                "facebook_groups",
            )
            for r in rows:
                src = str(r["lead_source"] or "").lower()
                prov = str(r["provider"] or "telegram").strip().lower()
                uname_val = str(r["username"] or "").strip()
                if "ваканс" in src or "job" in src:
                    vac_c += 1
                elif prov == "whatsapp" and not uname_val:
                    continue
                elif any(kw in src for kw in b2b_kws):
                    b2b_c += 1
            crm_counts["vacancies_leads"] = vac_c
            crm_counts["b2b_leads"] = b2b_c
            crm_counts["total_leadgen_leads"] = vac_c + b2b_c

            wa_row = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE lower(COALESCE(provider, '')) = 'whatsapp' AND lower(COALESCE(status, '')) != 'lost'"
            ).fetchone()
            wa_leads_count = int(wa_row[0] or 0) if wa_row else 0
            crm_counts["whatsapp_leads_count"] = wa_leads_count

            recent_rows = conn.execute(
                """
                SELECT id, name, company, username, phone, provider, url, niche, lead_source, status, created_at
                FROM leads
                WHERE lead_source IS NOT NULL AND trim(lead_source) != ''
                  AND lower(lead_source) NOT LIKE '%ваканс%'
                  AND lower(lead_source) NOT LIKE '%job%'
                ORDER BY id DESC
                LIMIT 12
                """
            ).fetchall()
            recent_b2b_leads = [as_dict(rr) for rr in recent_rows]

            camp_rows = conn.execute(
                "SELECT id, name, leadgen_enabled, status FROM campaigns"
            ).fetchall()
            active_lg = 0
            for cr in camp_rows:
                cdict = as_dict(cr)
                cname_low = str(cdict.get("name") or "").lower()
                enabled = bool(int(cdict.get("leadgen_enabled") or 0)) or (
                    "leadgen" in cname_low or "ваканс" in cname_low
                )
                if enabled and cdict.get("status") != "completed":
                    active_lg += 1
            crm_counts["active_leadgen_campaigns"] = active_lg
        except Exception:
            pass

    return {
        "state_dir": str(state_dir),
        "last_run_summary": last_run_summary,
        "latest_vacancy_report": latest_vacancy_report,
        "crm_leadgen_counts": crm_counts,
        "direct_x_leads_count": direct_x_leads_count,
        "whatsapp_leads_count": wa_leads_count,
        "ofm_review_count": ofm_review_count,
        "recent_b2b_leads": recent_b2b_leads,
        "recent_direct_x_leads": recent_direct_x_leads,
        "recent_ofm_review_leads": recent_ofm_review_leads,
    }


DEFAULT_B2B_STEPS = [
    {"step_order": 1, "delay_hours": 0, "template_text": "{first_touch}"},
    {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
    {
        "step_order": 3,
        "delay_hours": 24,
        "template_text": VLAD_STEP3_TEXT,
        "image_url": CANONICAL_STEP3_IMAGE_URL,
    },
]
DEFAULT_B2B_WA_STEPS = [
    {"step_order": 1, "delay_hours": 0, "template_text": "{first_touch}"},
    {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
    {
        "step_order": 3,
        "delay_hours": 24,
        "template_text": VLAD_STEP3_TEXT,
    },
]
DEFAULT_VACANCY_STEPS = [
    {"step_order": 1, "delay_hours": 0, "template_text": "{first_touch}"},
    {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
    {"step_order": 3, "delay_hours": 72, "template_text": "{followup_2}"},
]
DEFAULT_LEADGEN_STEPS = DEFAULT_B2B_STEPS


def _has_whatsapp_readiness(conn: sqlite3.Connection) -> bool:
    try:
        acc_cnt = conn.execute(
            """
            SELECT COUNT(*) FROM accounts
            WHERE lower(COALESCE(provider, '')) = 'whatsapp'
              AND status = 'connected'
              AND COALESCE(health, 'active') NOT IN ('banned', 'frozen', 'unauthorized')
            """
        ).fetchone()[0]
        if int(acc_cnt or 0) > 0:
            return True
        lead_cnt = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE lower(COALESCE(provider, '')) = 'whatsapp'"
        ).fetchone()[0]
        return int(lead_cnt or 0) > 0
    except sqlite3.Error:
        return False


def sync_leadgen_campaigns(
    conn: sqlite3.Connection,
    config: dict[str, Any] | None = None,
    *,
    sync_campaign_limits: bool = False,
    sync_leadgen_enabled: bool = False,
) -> dict[str, Any]:
    allow_limit_sync = bool(
        sync_campaign_limits
        or (isinstance(config, dict) and config.get("sync_campaign_limits"))
    )
    allow_enabled_sync = bool(
        sync_leadgen_enabled
        or (isinstance(config, dict) and "sync_leadgen_enabled" in config)
    )
    cfg = _normalize_config(config) if isinstance(config, dict) else get_leadgen_config(conn)
    b2b = cfg["b2b"]
    vac = cfg["vacancies"]

    b2b_enabled_sources = [
        k for k, v in (b2b.get("sources") or {}).items() if v and k != "ofm_groups"
    ]
    vac_enabled_layers = [f"vacancies:{k}" for k, v in (vac.get("layers") or {}).items() if v]
    if not vac_enabled_layers:
        vac_enabled_layers = ["vacancies"]

    all_existing_camps = [
        as_dict(r)
        for r in conn.execute("SELECT * FROM campaigns ORDER BY id ASC").fetchall()
    ]
    existing_names_low = {
        str(c.get("name") or "").strip().lower() for c in all_existing_camps
    }
    wa_ready = _has_whatsapp_readiness(conn)

    tg_name = resolve_target_b2b_tg_campaign_name(
        conn,
        b2b.get("target_campaign_tg"),
        fallback_default="Leadgen Auto (TG)",
    )
    targets: list[dict[str, Any]] = []
    if b2b.get("enabled", True):
        daily_lim = max(1, min(500, int(b2b.get("campaign_daily_limit") or 30)))
        quota = max(0, min(500, int(b2b.get("campaign_daily_limit") or 30)))
        wa_name = str(b2b.get("target_campaign_wa") or "Leadgen Auto (WA)").strip()
        if tg_name:
            targets.append(
                {
                    "name": tg_name,
                    "daily_limit": daily_lim,
                    "leadgen_daily_quota": quota,
                    "leadgen_sources": b2b_enabled_sources,
                    "default_steps": DEFAULT_B2B_STEPS,
                    "is_b2b": True,
                }
            )
        if (
            wa_name
            and wa_name.lower() != tg_name.lower()
            and (wa_ready or wa_name.lower() in existing_names_low)
        ):
            from app.whatsapp_gateway import DEFAULT_WA_DAILY_LIMIT

            wa_daily_lim = min(daily_lim, DEFAULT_WA_DAILY_LIMIT)
            targets.append(
                {
                    "name": wa_name,
                    "daily_limit": wa_daily_lim,
                    "leadgen_daily_quota": min(quota, wa_daily_lim),
                    "leadgen_sources": b2b_enabled_sources,
                    "default_steps": DEFAULT_B2B_WA_STEPS,
                    "is_b2b": True,
                }
            )

    ofm_name = str(b2b.get("target_campaign_ofm") or "SoftUGC — OFM (TG)").strip()
    if (
        b2b.get("ofm_outreach_enabled", False)
        and ofm_name
        and ofm_name.lower() != tg_name.lower()
    ):
        ofm_lim = max(1, min(500, int(b2b.get("ofm_daily_limit") or 25)))
        targets.append(
            {
                "name": ofm_name,
                "daily_limit": ofm_lim,
                "leadgen_daily_quota": ofm_lim,
                "leadgen_sources": ["ofm_groups"],
                "default_steps": DEFAULT_B2B_STEPS,
                "is_b2b": True,
            }
        )

    raw_vac_lim = vac.get("daily_limit")
    resolved_vac_lim = 20 if raw_vac_lim is None else int(raw_vac_lim)
    if vac.get("enabled", False) and resolved_vac_lim > 0:
        vac_name = str(vac.get("campaign_name") or "TG Вакансии").strip()
        vac_lim = max(1, min(500, resolved_vac_lim))
        vac_quota = max(0, min(500, resolved_vac_lim))
        if vac_name:
            targets.append(
                {
                    "name": vac_name,
                    "daily_limit": vac_lim,
                    "leadgen_daily_quota": vac_quota,
                    "leadgen_sources": vac_enabled_layers,
                    "default_steps": DEFAULT_VACANCY_STEPS,
                    "is_b2b": False,
                }
            )

    synced_campaigns: list[dict[str, Any]] = []
    for spec in targets:
        spec_name_low = str(spec["name"] or "").strip().lower()
        matched_existing = next(
            (c for c in all_existing_camps if str(c.get("name") or "").strip().lower() == spec_name_low),
            None,
        )
        if matched_existing is not None:
            cid = int(matched_existing["id"])
            existing_status = str(matched_existing.get("status") or "").strip().lower()
            existing_daily_limit = int(matched_existing.get("daily_limit") or 0)
            existing_leadgen_enabled = bool(int(matched_existing.get("leadgen_enabled") or 0))
            keep_paused_disabled = (
                "softugc" in spec_name_low
                and existing_status == "paused"
                and not existing_leadgen_enabled
            )
            effective_daily_limit = (
                spec["daily_limit"] if allow_limit_sync else existing_daily_limit
            )
            if allow_enabled_sync:
                req_enabled = (
                    bool(config.get("sync_leadgen_enabled"))
                    if isinstance(config, dict) and "sync_leadgen_enabled" in config
                    else True
                )
                effective_leadgen_enabled = False if keep_paused_disabled else req_enabled
            else:
                effective_leadgen_enabled = existing_leadgen_enabled

            updated = update_campaign(
                conn,
                cid,
                daily_limit=effective_daily_limit,
                leadgen_enabled=effective_leadgen_enabled,
                leadgen_daily_quota=spec["leadgen_daily_quota"],
                leadgen_sources=spec["leadgen_sources"],
            )
            cur_steps = updated.get("steps") or []
            needs_b2b_step_sync = bool(spec.get("is_b2b")) and (
                len(cur_steps) == 3
                and (
                    any("{followup_" in str(s.get("template_text") or "") for s in cur_steps)
                    or any("\u2028" in str(s.get("template_text") or "") for s in cur_steps)
                    or int(cur_steps[2].get("delay_hours") or 0) == 72
                )
            )
            if not cur_steps or needs_b2b_step_sync:
                updated = update_campaign(conn, cid, steps=spec["default_steps"])
            synced_campaigns.append(updated)
        else:
            created = create_campaign(
                conn,
                name=spec["name"],
                daily_limit=spec["daily_limit"],
                status="draft",
                steps=spec["default_steps"],
                leadgen_enabled=True,
                leadgen_daily_quota=spec["leadgen_daily_quota"],
                leadgen_sources=spec["leadgen_sources"],
            )
            synced_campaigns.append(created)

    return {
        "ok": True,
        "synced_count": len(synced_campaigns),
        "campaigns": synced_campaigns,
    }


def _ingest_leads_without_campaign_limit_mutation(
    conn: sqlite3.Connection,
    *,
    call_mcp_tool_handler: Any,
    leads_slice: list[dict[str, Any]],
    enroll_in_campaign: bool,
    campaign_name: str,
    default_quota: int,
    default_sources: list[str],
) -> tuple[int, int]:
    if not leads_slice:
        return 0, 0
    before_ids = {
        int(r["id"]) for r in conn.execute("SELECT id FROM campaigns").fetchall()
    }
    res = call_mcp_tool_handler(
        conn,
        "ingest_discovered_leads",
        {
            "leads": leads_slice,
            "enroll_in_campaign": enroll_in_campaign,
            "campaign_name": campaign_name,
        },
    )
    added = int(res.get("enrolled_in_campaign") or res.get("created_leads") or 0)
    after_ids = {
        int(r["id"]) for r in conn.execute("SELECT id FROM campaigns").fetchall()
    }
    new_cids = sorted(after_ids - before_ids)
    created_valid = 0
    for ncid in new_cids:
        rec_cnt = int(
            conn.execute(
                "SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id = ?",
                (ncid,),
            ).fetchone()[0]
            or 0
        )
        if rec_cnt == 0:
            conn.execute("DELETE FROM campaign_steps WHERE campaign_id = ?", (ncid,))
            conn.execute("DELETE FROM campaigns WHERE id = ?", (ncid,))
            conn.commit()
        else:
            update_campaign(
                conn,
                ncid,
                leadgen_enabled=True,
                leadgen_daily_quota=default_quota,
                leadgen_sources=default_sources,
            )
            created_valid += 1
    return added, created_valid


def _load_push_watermark(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {"files": {}, "vacancies": {}, "ofm_files": {}}
    try:
        row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'leadgen_push_watermark'"
        ).fetchone()
        if row and row["value"]:
            parsed = json.loads(row["value"])
            if isinstance(parsed, dict):
                if isinstance(parsed.get("files"), dict):
                    out["files"] = dict(parsed["files"])
                if isinstance(parsed.get("vacancies"), dict):
                    out["vacancies"] = dict(parsed["vacancies"])
                if isinstance(parsed.get("ofm_files"), dict):
                    out["ofm_files"] = dict(parsed["ofm_files"])
    except Exception:
        pass
    return out


def _save_push_watermark(conn: sqlite3.Connection, watermark: dict[str, Any]) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('leadgen_push_watermark', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (json.dumps(watermark, ensure_ascii=False), now),
    )
    conn.commit()


def push_discovered_now(conn: sqlite3.Connection) -> dict[str, Any]:
    cfg = get_leadgen_config(conn)
    if not cfg.get("enabled", True):
        return {
            "ok": True,
            "ingested": 0,
            "campaigns_synced": 0,
            "message": "Авто-лидген отключён в настройках",
        }

    from app.hygiene import is_ofm_campaign_name

    total_quota = int(cfg.get("total_daily_quota") or 50)
    today = utc_today()
    today_rows = conn.execute(
        """
        SELECT c.name, c.leadgen_sources_json, COUNT(*) AS cnt
        FROM campaign_recipients cr
        JOIN campaigns c ON c.id = cr.campaign_id
        WHERE c.leadgen_enabled = 1
          AND cr.created_at LIKE ?
        GROUP BY c.id
        """,
        (f"{today}%",),
    ).fetchall()
    already_today = sum(
        int(r["cnt"] or 0)
        for r in today_rows
        if not is_ofm_campaign_name(r["name"], r["leadgen_sources_json"])
    )
    remaining_quota = max(0, total_quota - int(already_today or 0))
    ofm_outreach_active = bool((cfg.get("b2b") or {}).get("ofm_outreach_enabled", False))
    if remaining_quota <= 0 and total_quota > 0 and not ofm_outreach_active:
        return {
            "ok": True,
            "ingested": 0,
            "campaigns_synced": 0,
            "message": f"Дневная квота лидгена ({total_quota}) на сегодня уже выбрана",
        }

    state_dir = _resolve_state_dir()
    ingested_total = 0
    campaigns_created = 0
    wm = _load_push_watermark(conn)
    wm_files: dict[str, Any] = wm["files"]
    wm_vac: dict[str, Any] = wm["vacancies"]
    wm_dirty = False
    from app.mcp_server import call_mcp_tool_handler

    # 1. Ready vacancy replies (job_replies_*.json or job_monitor_*.json with prebuilt fields)
    vac_cfg = cfg["vacancies"]
    vac_enabled_layers = [
        f"vacancies:{k}" for k, v in (vac_cfg.get("layers") or {}).items() if v
    ] or ["vacancies"]
    raw_vac_push_lim = vac_cfg.get("daily_limit")
    resolved_vac_push_lim = 20 if raw_vac_push_lim is None else int(raw_vac_push_lim)
    if vac_cfg.get("enabled", False) and resolved_vac_push_lim > 0 and state_dir.is_dir() and remaining_quota > 0:
        from datetime import datetime, timezone
        from app.outreach import VACANCY_MAX_STEP1_AGE_HOURS, _VACANCY_JUNK_ROLE_RE
        from leadgen.job_reply import build_default_followups, build_vacancy_mcp_lead

        reply_files = sorted(
            glob.glob(str(state_dir / "job_replies_[0-9]*.json")),
            key=lambda p: (os.path.basename(p), os.path.getmtime(p)),
        )
        monitor_files = sorted(
            glob.glob(str(state_dir / "job_monitor_[0-9]*.json")),
            key=lambda p: (os.path.basename(p), os.path.getmtime(p)),
        )
        if monitor_files:
            mon_path = Path(monitor_files[-1])
            mon_st = mon_path.stat()
            rep_path = Path(reply_files[-1]) if reply_files else None
            rep_st = rep_path.stat() if rep_path and rep_path.is_file() else None
            vac_sig = {
                "mon_mtime_ns": int(mon_st.st_mtime_ns),
                "mon_size": int(mon_st.st_size),
                "rep_file": rep_path.name if rep_path else "",
                "rep_mtime_ns": int(rep_st.st_mtime_ns) if rep_st else 0,
                "rep_size": int(rep_st.st_size) if rep_st else 0,
            }
            prev_vac = wm_vac.get(mon_path.name) if isinstance(wm_vac.get(mon_path.name), dict) else None
            vac_unchanged_done = bool(
                prev_vac
                and prev_vac.get("done") is True
                and all(prev_vac.get(k) == v for k, v in vac_sig.items())
            )
            if not vac_unchanged_done:
                replies_by_hash: dict[str, dict[str, Any]] = {}
                if rep_path is not None:
                    try:
                        for r in json.loads(rep_path.read_text(encoding="utf-8")):
                            if isinstance(r, dict) and r.get("hash") and r.get("text"):
                                replies_by_hash[str(r["hash"])] = r
                    except Exception:
                        pass

                try:
                    mon_items = json.loads(mon_path.read_text(encoding="utf-8"))
                except Exception:
                    mon_items = []
                enabled_layers = {k for k, v in (vac_cfg.get("layers") or {}).items() if v}
                vac_leads: list[dict[str, Any]] = []
                if isinstance(mon_items, list):
                    if vac_cfg.get("hot_priority", True):
                        mon_items = sorted(mon_items, key=lambda x: not bool((x or {}).get("hot")))
                    for item in mon_items:
                        if not isinstance(item, dict):
                            continue
                        posted_raw = str(item.get("posted_at") or item.get("date") or "").strip()
                        if posted_raw:
                            try:
                                p_norm = posted_raw[:-1] + "+00:00" if posted_raw.endswith(("Z", "z")) else posted_raw
                                p_dt = datetime.fromisoformat(p_norm)
                                if p_dt.tzinfo is None:
                                    p_dt = p_dt.replace(tzinfo=timezone.utc)
                                if (datetime.now(timezone.utc) - p_dt).total_seconds() / 3600.0 > VACANCY_MAX_STEP1_AGE_HOURS:
                                    continue
                            except ValueError:
                                pass
                        item_blob = f"{item.get('role') or ''} {item.get('why') or ''} {item.get('text') or ''}"
                        if _VACANCY_JUNK_ROLE_RE.search(item_blob):
                            continue
                        layer = str(item.get("layer") or "AI-контент").strip()
                        if enabled_layers and layer and layer not in enabled_layers:
                            continue
                        if vac_cfg.get("remote_only", True) and item.get("remote") is False:
                            continue
                        draft_obj = replies_by_hash.get(str(item.get("hash") or ""))
                        if not draft_obj and item.get("text_reply"):
                            f1, f2 = build_default_followups(item)
                            draft_obj = {
                                "text": str(item["text_reply"]),
                                "followup_1": str(item.get("followup_1") or f1),
                                "followup_2": str(item.get("followup_2") or f2),
                            }
                        if not draft_obj:
                            continue
                        mcp_lead = build_vacancy_mcp_lead(
                            item,
                            draft_obj,
                            touch_mode=str(vac_cfg.get("touch_mode") or "direct"),
                        )
                        if mcp_lead is not None:
                            vac_leads.append(mcp_lead)

                vac_limit = min(remaining_quota, resolved_vac_push_lim)
                start_vac_idx = (
                    min(max(0, int(prev_vac.get("offset") or 0)), len(vac_leads))
                    if (
                        prev_vac
                        and not prev_vac.get("done")
                        and all(prev_vac.get(k) == v for k, v in vac_sig.items())
                    )
                    else 0
                )
                pending_vac_leads = vac_leads[start_vac_idx:]
                if pending_vac_leads and vac_limit > 0:
                    slice_vac = pending_vac_leads[:vac_limit]
                    added, created_c = _ingest_leads_without_campaign_limit_mutation(
                        conn,
                        call_mcp_tool_handler=call_mcp_tool_handler,
                        leads_slice=slice_vac,
                        enroll_in_campaign=bool(cfg.get("auto_push_to_crm", True)),
                        campaign_name=str(vac_cfg.get("campaign_name") or "TG Вакансии"),
                        default_quota=max(0, min(500, int(vac_cfg.get("daily_limit") or 20))),
                        default_sources=vac_enabled_layers,
                    )
                    ingested_total += added
                    campaigns_created += created_c
                    remaining_quota = max(0, remaining_quota - added)
                    new_vac_offset = start_vac_idx + len(slice_vac)
                    wm_vac[mon_path.name] = {
                        **vac_sig,
                        "offset": new_vac_offset,
                        "done": new_vac_offset >= len(vac_leads),
                    }
                    wm_dirty = True
                elif not pending_vac_leads:
                    wm_vac[mon_path.name] = {
                        **vac_sig,
                        "offset": len(vac_leads),
                        "done": True,
                    }
                    wm_dirty = True

    # 2. Local JSONL files in state_dir (*.treg.jsonl / *.final.jsonl / discovered_leads.jsonl) with per-file watermark
    b2b_cfg = cfg["b2b"]
    b2b_enabled_sources = [
        k for k, v in (b2b_cfg.get("sources") or {}).items() if v and k != "ofm_groups"
    ]
    if b2b_cfg.get("enabled", True) and state_dir.is_dir() and remaining_quota > 0:
        from app.hygiene import (
            _has_messenger_contact,
            append_direct_x_lead,
            clean_crm_lead_names,
            sniper_lead_violation_reason,
        )
        import leadgen.crm_mcp_push as _crm_mcp_push
        from leadgen.lead_market import market_of
        from leadgen.supply_side import (
            NON_BUYER_ROLES,
            has_insufficient_product_info,
            is_invalid_product_name,
            is_platform_or_social_url,
            is_rejected_buyer_category,
            is_supply_side,
            pick_best_product_url,
        )
        from leadgen.tg_enrich_leads import classify_role
        from leadgen.tg_to_gsheet import keep_for_sheet

        raw_candidates = (
            glob.glob(str(state_dir / "*.treg.jsonl"))
            + glob.glob(str(state_dir / "*.final.jsonl"))
            + glob.glob(str(state_dir / "discovered_leads*.jsonl"))
        )
        jsonl_candidates = sorted(
            [
                p
                for p in raw_candidates
                if os.path.isfile(p)
                and os.path.getsize(p) > 0
                and os.path.basename(p) not in ("direct_x_leads.jsonl", "ofm_review_leads.jsonl")
            ],
            key=os.path.getmtime,
        )
        b2b_limit = min(remaining_quota, int(b2b_cfg.get("campaign_daily_limit") or 30))
        b2b_quota_default = max(0, min(500, int(b2b_cfg.get("campaign_daily_limit") or 30)))
        touch_mode_val = str(b2b_cfg.get("touch_mode") or "direct")
        leads: list[dict[str, Any]] = []

        for cand_raw in jsonl_candidates:
            if b2b_limit <= 0 or len(leads) >= b2b_limit:
                break
            cand_path = Path(cand_raw)
            fname = cand_path.name
            st = cand_path.stat()
            cur_size = int(st.st_size)
            cur_mtime_ns = int(st.st_mtime_ns)
            prev_f = wm_files.get(fname) if isinstance(wm_files.get(fname), dict) else None
            if (
                prev_f is not None
                and prev_f.get("done") is True
                and int(prev_f.get("size") or -1) == cur_size
                and int(prev_f.get("mtime_ns") or -1) == cur_mtime_ns
            ):
                continue

            all_lines = cand_path.read_text(encoding="utf-8").splitlines()
            if prev_f is not None and (
                cur_size > int(prev_f.get("size") or 0)
                or (
                    cur_size == int(prev_f.get("size") or 0)
                    and int(prev_f.get("mtime_ns") or -1) == cur_mtime_ns
                    and not prev_f.get("done")
                )
            ):
                start_offset = min(max(0, int(prev_f.get("offset") or 0)), len(all_lines))
            else:
                start_offset = 0

            sheet_like: list[list[Any]] = []
            next_offset = len(all_lines)
            reached_end = True

            for line_idx in range(start_offset, len(all_lines)):
                if len(leads) >= b2b_limit and not sheet_like:
                    next_offset = line_idx
                    reached_end = False
                    break
                line = all_lines[line_idx].strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, list):
                    sheet_like.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                if "ofm_tier" in item or str(item.get("source") or item.get("lead_source") or "").strip().lower().startswith("ofm"):
                    continue
                cand = dict(item)
                uname = str(cand.get("username") or cand.get("tg_username") or cand.get("telegram_username") or "").strip().lstrip("@")
                if uname and not cand.get("username"):
                    cand["username"] = uname
                raw_phone = str(cand.get("phone") or "").strip()
                wa_raw = str(cand.get("whatsapp") or "").strip()
                canon_wa = whatsapp_url(wa_raw) or (whatsapp_url(raw_phone) if "wa.me" in raw_phone.lower() or "whatsapp" in raw_phone.lower() else "")
                phone_digits = ""
                if canon_wa:
                    phone_digits = re.sub(r"\D", "", canon_wa)
                elif raw_phone:
                    cand_digits = re.sub(r"\D", "", raw_phone.split("?", 1)[0].split("#", 1)[0])
                    if 8 <= len(cand_digits) <= 15:
                        phone_digits = cand_digits
                if phone_digits:
                    phone_val = f"+{phone_digits}"
                    cand["phone"] = phone_val
                    if canon_wa or str(cand.get("provider") or "").strip().lower() == "whatsapp":
                        cand["whatsapp"] = f"https://wa.me/{phone_digits}"
                if uname:
                    cand["provider"] = "telegram"
                elif cand.get("phone") and (canon_wa or str(cand.get("provider") or "").lower() == "whatsapp"):
                    cand["provider"] = "whatsapp"
                if not cand.get("project") and cand.get("company"):
                    cand["project"] = cand["company"]
                best_url = pick_best_product_url(cand)
                if best_url and (
                    not cand.get("project_url") or is_platform_or_social_url(cand.get("project_url"))
                ):
                    cand["project_url"] = best_url
                if not cand.get("roles") and not cand.get("role"):
                    bio_txt = str(cand.get("bio") or "").strip()
                    inferred_role = classify_role(bio_txt) if bio_txt else ""
                    if inferred_role:
                        cand["role"] = inferred_role
                        cand["roles"] = [inferred_role]
                    elif "group" not in cand and not is_invalid_product_name(cand.get("project") or cand.get("company")):
                        cand["roles"] = ["Founder"]

                name_s = str(cand.get("name") or cand.get("company") or cand.get("project") or "")
                angle_s = str(cand.get("their_angle") or cand.get("what_they_do") or cand.get("bio") or "")
                site_s = str(cand.get("project_url") or "")
                if market_of(site_s, f"{name_s} {angle_s}") == "ru":
                    continue
                blob = "\n".join(
                    str(x)
                    for x in (
                        name_s,
                        str(cand.get("company") or ""),
                        str(cand.get("project") or ""),
                        angle_s,
                        str(cand.get("bio") or ""),
                        str(cand.get("our_angle") or ""),
                    )
                    if x
                )
                raw_score = cand.get("icp_score")
                if raw_score is not None and str(raw_score).strip() != "":
                    try:
                        if float(raw_score) < 80:
                            continue
                    except (TypeError, ValueError):
                        continue

                if (
                    is_supply_side(blob)
                    or has_insufficient_product_info(blob)
                    or is_rejected_buyer_category(cand.get("category") or cand.get("niche"))
                    or bool(
                        sniper_lead_violation_reason(
                            name=name_s,
                            company=str(cand.get("company") or cand.get("project") or ""),
                            niche=str(cand.get("category") or cand.get("niche") or ""),
                            note=f"{angle_s}\n{cand.get('bio') or ''}".strip(),
                            description=str(cand.get("what_they_do") or ""),
                            url=site_s,
                            lead_source=str(cand.get("lead_source") or cand.get("source") or ""),
                            vacancies_enabled=False,
                        )
                    )
                ):
                    continue
                roles_set = {str(x).strip() for x in (cand.get("roles") or []) if str(x or "").strip()}
                if cand.get("role"):
                    roles_set.add(str(cand["role"]).strip())
                if roles_set and not (roles_set & {"Founder", "Marketing"}) and (roles_set & NON_BUYER_ROLES):
                    continue
                if not _has_messenger_contact(
                    username=cand.get("username"),
                    phone=cand.get("phone"),
                    telegram_user_id=cand.get("telegram_user_id"),
                ):
                    append_direct_x_lead(
                        state_dir,
                        {
                            "name": cand.get("name") or cand.get("company") or cand.get("project"),
                            "company": cand.get("company") or cand.get("project"),
                            "email": cand.get("email") or cand.get("work_email"),
                            "x_url": cand.get("x_url") or cand.get("founder_x") or cand.get("founder_twitter") or cand.get("twitter"),
                            "linkedin_url": cand.get("linkedin_url") or cand.get("founder_linkedin"),
                            "url": site_s,
                            "category": cand.get("category") or cand.get("niche"),
                            "their_angle": angle_s,
                            "lead_source": cand.get("lead_source") or cand.get("source"),
                            "icp_score": cand.get("icp_score"),
                        },
                    )
                    continue
                if not keep_for_sheet(cand):
                    continue

                built = _crm_mcp_push.build_lead_payload_from_dict(cand, touch_mode=touch_mode_val)
                c_name, c_comp = clean_crm_lead_names(
                    built.get("name"), built.get("company"), built.get("url") or built.get("website")
                )
                if c_name:
                    built["name"] = c_name
                built["company"] = c_comp
                if _has_messenger_contact(
                    username=built.get("username"),
                    phone=built.get("phone"),
                    telegram_user_id=built.get("telegram_user_id"),
                ):
                    leads.append(built)

            if sheet_like:
                packed_sheet = _crm_mcp_push.pack_tab_leads_for_mcp(
                    "Все2", sheet_like, use_llm=False, touch_mode=touch_mode_val
                )
                for p_item in packed_sheet:
                    if not _has_messenger_contact(
                        username=p_item.get("username"),
                        phone=p_item.get("phone"),
                        telegram_user_id=p_item.get("telegram_user_id"),
                    ):
                        continue
                    if sniper_lead_violation_reason(
                        name=str(p_item.get("name") or ""),
                        company=str(p_item.get("company") or ""),
                        niche=str(p_item.get("category") or p_item.get("niche") or ""),
                        note=str(p_item.get("their_angle") or p_item.get("note") or ""),
                        url=str(p_item.get("url") or p_item.get("website") or ""),
                        lead_source=str(p_item.get("lead_source") or ""),
                        vacancies_enabled=False,
                    ):
                        continue
                    leads.append(p_item)

            wm_files[fname] = {
                "mtime_ns": cur_mtime_ns,
                "size": cur_size,
                "offset": next_offset,
                "done": reached_end,
            }
            wm_dirty = True

        if leads and b2b_limit > 0:
            batch = leads[:b2b_limit]
            tg_leads = [l for l in batch if str(l.get("provider") or "telegram") != "whatsapp"]
            wa_leads = [l for l in batch if str(l.get("provider") or "") == "whatsapp"]
            if tg_leads:
                target_tg_camp = resolve_target_b2b_tg_campaign_name(
                    conn,
                    b2b_cfg.get("target_campaign_tg"),
                    fallback_default="Leadgen Auto (TG)",
                )
                added_tg, created_tg = _ingest_leads_without_campaign_limit_mutation(
                    conn,
                    call_mcp_tool_handler=call_mcp_tool_handler,
                    leads_slice=tg_leads,
                    enroll_in_campaign=bool(cfg.get("auto_push_to_crm", True)),
                    campaign_name=target_tg_camp,
                    default_quota=b2b_quota_default,
                    default_sources=b2b_enabled_sources,
                )
                ingested_total += added_tg
                campaigns_created += created_tg
            if wa_leads:
                added_wa, created_wa = _ingest_leads_without_campaign_limit_mutation(
                    conn,
                    call_mcp_tool_handler=call_mcp_tool_handler,
                    leads_slice=wa_leads,
                    enroll_in_campaign=bool(cfg.get("auto_push_to_crm", True)),
                    campaign_name=str(b2b_cfg.get("target_campaign_wa") or "Leadgen Auto (WA)"),
                    default_quota=b2b_quota_default,
                    default_sources=b2b_enabled_sources,
                )
                ingested_total += added_wa
                campaigns_created += created_wa

    # 3. OFM review leads -> separate isolated OFM campaign (only when explicitly enabled)
    ofm_path = state_dir / "ofm_review_leads.jsonl"
    if ofm_outreach_active and state_dir.is_dir() and ofm_path.is_file() and ofm_path.stat().st_size > 0:
        from app.hygiene import _normalize_ofm_review_record, _ofm_review_canonical_keys
        from leadgen.outreach_copy import build_ofm_vlad_copy_batch

        ofm_camp_name = str(b2b_cfg.get("target_campaign_ofm") or "SoftUGC — OFM (TG)").strip() or "SoftUGC — OFM (TG)"
        ofm_daily_lim = max(1, min(500, int(b2b_cfg.get("ofm_daily_limit") or 25)))
        ofm_push_cap = max(100, ofm_daily_lim)
        wm_ofm = wm.setdefault("ofm_files", {})
        ofm_st = ofm_path.stat()
        ofm_size = int(ofm_st.st_size)
        ofm_mtime_ns = int(ofm_st.st_mtime_ns)
        prev_ofm = wm_ofm.get(ofm_path.name) if isinstance(wm_ofm.get(ofm_path.name), dict) else None
        ofm_already_done = bool(
            prev_ofm is not None
            and prev_ofm.get("done") is True
            and int(prev_ofm.get("size") or -1) == ofm_size
            and int(prev_ofm.get("mtime_ns") or -1) == ofm_mtime_ns
        )
        if not ofm_already_done:
            ofm_lines = ofm_path.read_text(encoding="utf-8").splitlines()
            if prev_ofm is not None and (
                ofm_size > int(prev_ofm.get("size") or 0)
                or (
                    ofm_size == int(prev_ofm.get("size") or 0)
                    and int(prev_ofm.get("mtime_ns") or -1) == ofm_mtime_ns
                    and not prev_ofm.get("done")
                )
            ):
                ofm_start = min(max(0, int(prev_ofm.get("offset") or 0)), len(ofm_lines))
            else:
                ofm_start = 0

            ofm_raw_batch: list[dict[str, Any]] = []
            ofm_seen_keys: set[str] = set()
            ofm_next_offset = len(ofm_lines)
            ofm_reached_end = True
            for line_idx in range(ofm_start, len(ofm_lines)):
                if len(ofm_raw_batch) >= ofm_push_cap:
                    ofm_next_offset = line_idx
                    ofm_reached_end = False
                    break
                raw_ln = ofm_lines[line_idx].strip()
                if not raw_ln:
                    continue
                try:
                    parsed_row = json.loads(raw_ln)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed_row, dict):
                    continue
                norm = _normalize_ofm_review_record(parsed_row)
                uname_o = str(norm.get("username") or "").strip().lstrip("@")
                if not uname_o:
                    continue
                ckeys = _ofm_review_canonical_keys(norm)
                if ckeys and (ckeys & ofm_seen_keys):
                    continue
                ofm_seen_keys.update(ckeys)
                ofm_raw_batch.append(norm)

            if ofm_raw_batch:
                drafted_batch = build_ofm_vlad_copy_batch(ofm_raw_batch)
                ofm_payload: list[dict[str, Any]] = []
                for norm, drafted in zip(ofm_raw_batch, drafted_batch):
                    market_tag = str(norm.get("market") or "EN").strip() or "EN"
                    grp_tag = str(norm.get("source_group") or "").strip()
                    sig_tag = str(norm.get("signal_summary") or "").strip()
                    sample_tag = str(norm.get("sample_message") or "").strip()
                    note_txt = f"[{norm.get('ofm_tier') or 'traffic_buyer'}] {grp_tag} ({market_tag})\n{sample_tag}".strip()
                    ofm_payload.append(
                        {
                            "name": norm.get("name") or norm.get("username") or "OFM Buyer",
                            "username": norm.get("username"),
                            "company": norm.get("company") or grp_tag or "OFM Agency",
                            "url": norm.get("project_url") or norm.get("url") or norm.get("tg_url") or "",
                            "website": norm.get("project_url") or norm.get("url") or "",
                            "niche": f"OFM ({market_tag})",
                            "category": "OFM / AI UGC",
                            "lead_source": norm.get("source") or "ofm_tg",
                            "source": norm.get("source") or "ofm_tg",
                            "ofm_tier": norm.get("ofm_tier") or "traffic_buyer",
                            "icp_score": int(norm.get("icp_score") or 90),
                            "their_angle": sig_tag,
                            "note": note_txt,
                            "first_touch": drafted["first_touch"],
                            "step1_text": drafted["step1_text"],
                            "hook": drafted["hook"],
                            "audience": drafted["audience"],
                            "followup_1": drafted["followup_1"],
                            "step2_text": drafted["step2_text"],
                            "followup_2": drafted["followup_2"],
                            "step3_text": drafted["step3_text"],
                            "provider": "telegram",
                        }
                    )
                added_ofm, created_ofm = _ingest_leads_without_campaign_limit_mutation(
                    conn,
                    call_mcp_tool_handler=call_mcp_tool_handler,
                    leads_slice=ofm_payload,
                    enroll_in_campaign=bool(cfg.get("auto_push_to_crm", True)),
                    campaign_name=ofm_camp_name,
                    default_quota=ofm_daily_lim,
                    default_sources=["ofm_groups"],
                )
                ingested_total += added_ofm
                campaigns_created += created_ofm

            wm_ofm[ofm_path.name] = {
                "mtime_ns": ofm_mtime_ns,
                "size": ofm_size,
                "offset": ofm_next_offset,
                "done": ofm_reached_end,
            }
            wm_dirty = True

    if wm_dirty:
        _save_push_watermark(conn, wm)

    return {
        "ok": True,
        "ingested": ingested_total,
        "campaigns_synced": campaigns_created,
        "message": (
            f"Создано новых рассылок: {campaigns_created}, залито лидов: {ingested_total}"
        ),
    }
