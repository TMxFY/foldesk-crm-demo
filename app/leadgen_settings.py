from __future__ import annotations

import copy
import glob
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.db import CANONICAL_STEP3_IMAGE_URL, ROOT, as_dict, utc_now, utc_today
from app.outreach import create_campaign, get_campaign, update_campaign
from leadgen.outreach_copy import VLAD_STEP2_TEMPLATE, VLAD_STEP3_TEXT

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
    creatoros_dir = Path("/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline")
    if creatoros_dir.is_dir() and "PYTEST_CURRENT_TEST" not in os.environ:
        return creatoros_dir
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
        "campaign_daily_limit": 30,
        "sources": {
            "tg_groups": True,
            "tg_comments": True,
            "channel_graph": True,
            "tg_ad_spy": True,
            "meta_ads": True,
            "yc_ph": True,
            "social_x_reddit": True,
            "buyer_catalogs": True,
            "linkedin_jobs": True,
        },
        "tg_groups_old": ["solofounders", "producthunt_heroes", "id1613398776"],
        "tg_groups_new": ["solofounderschat", "shopifyeasy", "productcircle_chat"],
        "tg_comment_channels": [],
        "limits": {
            "yc_limit": 50,
            "ph_limit": 0,
            "msg_limit": 1800,
            "lurker_limit": 1000,
            "external_limit": 20,
        },
    },
    "vacancies": {
        "enabled": True,
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

    try:
        b2b_limit = int(b2b.get("campaign_daily_limit", 30))
    except (TypeError, ValueError):
        raise ValueError("b2b.campaign_daily_limit должен быть от 1 до 500") from None
    if not (0 <= b2b_limit <= 500):
        raise ValueError("b2b.campaign_daily_limit должен быть от 0 до 500")
    b2b["campaign_daily_limit"] = b2b_limit

    raw_sources = b2b.get("sources") or {}
    norm_sources = {}
    for sk, default_v in DEFAULT_LEADGEN_CONFIG["b2b"]["sources"].items():
        norm_sources[sk] = bool(raw_sources.get(sk, default_v))
    for sk, sv in raw_sources.items():
        if sk not in norm_sources:
            norm_sources[str(sk)] = bool(sv)
    b2b["sources"] = norm_sources

    b2b["tg_groups_old"] = _clean_chat_list(b2b.get("tg_groups_old", DEFAULT_LEADGEN_CONFIG["b2b"]["tg_groups_old"]))
    b2b["tg_groups_new"] = _clean_chat_list(b2b.get("tg_groups_new", DEFAULT_LEADGEN_CONFIG["b2b"]["tg_groups_new"]))
    b2b["tg_comment_channels"] = _clean_chat_list(b2b.get("tg_comment_channels", []))

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
    vac["enabled"] = bool(vac.get("enabled", True))
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
    """Find existing non-vacancy, non-WA SoftUGC TG B2B campaign (preferring 'SoftUGC — База Все2*')."""
    if conn is None:
        return None
    try:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY id ASC").fetchall()
    except Exception:
        return None

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
    """Resolve TG B2B campaign name: if empty/default, prefer existing 'SoftUGC — База Все2*' / 'SoftUGC*' in DB."""
    clean_req = str(requested_name or "").strip()
    req_low = clean_req.lower()
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
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Конфигурация должна быть объектом JSON")
    inner = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    current = get_leadgen_config(conn)
    merged = _deep_merge(current, inner)
    normalized = _normalize_config(merged)

    if isinstance(inner.get("cases_bank_md"), str) and inner["cases_bank_md"].strip():
        normalized["cases_bank_md"] = inner["cases_bank_md"]
        if "PYTEST_CURRENT_TEST" not in os.environ or os.environ.get("LEADGEN_WRITE_CASES_BANK") == "1":
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

    crm_counts = {
        "total_leadgen_leads": 0,
        "vacancies_leads": 0,
        "b2b_leads": 0,
        "active_leadgen_campaigns": 0,
    }
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT lead_source FROM leads WHERE lead_source IS NOT NULL AND trim(lead_source) != ''"
            ).fetchall()
            vac_c = 0
            b2b_c = 0
            for r in rows:
                src = str(r["lead_source"] or "").lower()
                if "ваканс" in src or "job" in src:
                    vac_c += 1
                elif any(
                    kw in src
                    for kw in (
                        "leadgen",
                        "google sheet",
                        "yc",
                        "product hunt",
                        "meta",
                        "tg",
                        "ad spy",
                        "channel",
                        "social",
                        "reddit",
                        "whop",
                        "catalog",
                        "linkedin",
                    )
                ):
                    b2b_c += 1
            crm_counts["vacancies_leads"] = vac_c
            crm_counts["b2b_leads"] = b2b_c
            crm_counts["total_leadgen_leads"] = vac_c + b2b_c

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


def sync_leadgen_campaigns(
    conn: sqlite3.Connection,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = _normalize_config(config) if isinstance(config, dict) else get_leadgen_config(conn)
    b2b = cfg["b2b"]
    vac = cfg["vacancies"]

    b2b_enabled_sources = [k for k, v in (b2b.get("sources") or {}).items() if v]
    vac_enabled_layers = [f"vacancies:{k}" for k, v in (vac.get("layers") or {}).items() if v]
    if not vac_enabled_layers:
        vac_enabled_layers = ["vacancies"]

    targets: list[dict[str, Any]] = []
    if b2b.get("enabled", True):
        daily_lim = max(1, min(500, int(b2b.get("campaign_daily_limit") or 30)))
        quota = max(0, min(500, int(b2b.get("campaign_daily_limit") or 30)))
        tg_name = resolve_target_b2b_tg_campaign_name(
            conn,
            b2b.get("target_campaign_tg"),
            fallback_default="Leadgen Auto (TG)",
        )
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
        if wa_name and wa_name.lower() != tg_name.lower():
            targets.append(
                {
                    "name": wa_name,
                    "daily_limit": daily_lim,
                    "leadgen_daily_quota": quota,
                    "leadgen_sources": b2b_enabled_sources,
                    "default_steps": DEFAULT_B2B_WA_STEPS,
                    "is_b2b": True,
                }
            )

    if vac.get("enabled", True):
        vac_name = str(vac.get("campaign_name") or "TG Вакансии").strip()
        vac_lim = max(1, min(500, int(vac.get("daily_limit") or 20)))
        vac_quota = max(0, min(500, int(vac.get("daily_limit") or 20)))
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

    all_existing_camps = [
        as_dict(r)
        for r in conn.execute("SELECT * FROM campaigns ORDER BY id ASC").fetchall()
    ]
    synced_campaigns: list[dict[str, Any]] = []
    for spec in targets:
        spec_name_low = str(spec["name"] or "").strip().lower()
        matched_existing = next(
            (c for c in all_existing_camps if str(c.get("name") or "").strip().lower() == spec_name_low),
            None,
        )
        if matched_existing is not None:
            cid = int(matched_existing["id"])
            keep_paused_disabled = (
                "softugc" in spec_name_low
                and str(matched_existing.get("status") or "") == "paused"
                and int(matched_existing.get("leadgen_enabled") or 0) == 0
            )
            updated = update_campaign(
                conn,
                cid,
                daily_limit=spec["daily_limit"],
                leadgen_enabled=(False if keep_paused_disabled else True),
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


def push_discovered_now(conn: sqlite3.Connection) -> dict[str, Any]:
    cfg = get_leadgen_config(conn)
    sync_res = sync_leadgen_campaigns(conn, cfg)
    if not cfg.get("enabled", True):
        return {
            "ok": True,
            "ingested": 0,
            "campaigns_synced": sync_res["synced_count"],
            "message": "Авто-лидген отключён в настройках",
        }

    total_quota = int(cfg.get("total_daily_quota") or 50)
    today = utc_today()
    already_today = conn.execute(
        """
        SELECT COUNT(*) FROM campaign_recipients cr
        JOIN campaigns c ON c.id = cr.campaign_id
        WHERE c.leadgen_enabled = 1 AND cr.created_at LIKE ?
        """,
        (f"{today}%",),
    ).fetchone()[0]
    remaining_quota = max(0, total_quota - int(already_today or 0))
    if remaining_quota <= 0 and total_quota > 0:
        return {
            "ok": True,
            "ingested": 0,
            "campaigns_synced": sync_res["synced_count"],
            "message": f"Дневная квота лидгена ({total_quota}) на сегодня уже выбрана",
        }

    state_dir = _resolve_state_dir()
    ingested_total = 0
    from app.mcp_server import call_mcp_tool_handler

    # 1. Ready vacancy replies (job_replies_*.json or job_monitor_*.json with prebuilt fields)
    vac_cfg = cfg["vacancies"]
    if vac_cfg.get("enabled", True) and state_dir.is_dir() and remaining_quota > 0:
        from leadgen.job_reply import build_default_followups, build_vacancy_mcp_lead

        reply_files = sorted(
            glob.glob(str(state_dir / "job_replies_[0-9]*.json")),
            key=lambda p: (os.path.basename(p), os.path.getmtime(p)),
        )
        monitor_files = sorted(
            glob.glob(str(state_dir / "job_monitor_[0-9]*.json")),
            key=lambda p: (os.path.basename(p), os.path.getmtime(p)),
        )
        replies_by_hash: dict[str, dict[str, Any]] = {}
        if reply_files:
            try:
                for r in json.loads(Path(reply_files[-1]).read_text(encoding="utf-8")):
                    if isinstance(r, dict) and r.get("hash") and r.get("text"):
                        replies_by_hash[str(r["hash"])] = r
            except Exception:
                pass

        if monitor_files:
            try:
                mon_items = json.loads(Path(monitor_files[-1]).read_text(encoding="utf-8"))
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

            vac_limit = min(remaining_quota, int(vac_cfg.get("daily_limit") or 20))
            if vac_leads and vac_limit > 0:
                res = call_mcp_tool_handler(
                    conn,
                    "ingest_discovered_leads",
                    {
                        "leads": vac_leads[:vac_limit],
                        "enroll_in_campaign": bool(cfg.get("auto_push_to_crm", True)),
                        "campaign_name": str(vac_cfg.get("campaign_name") or "TG Вакансии"),
                    },
                )
                added = int(res.get("enrolled_in_campaign") or res.get("created_leads") or 0)
                ingested_total += added
                remaining_quota = max(0, remaining_quota - added)

    # 2. Local JSONL files in state_dir (*.treg.jsonl / *.final.jsonl / discovered_leads.jsonl)
    b2b_cfg = cfg["b2b"]
    if b2b_cfg.get("enabled", True) and state_dir.is_dir() and remaining_quota > 0:
        from leadgen.crm_mcp_push import load_leads_from_jsonl

        jsonl_candidates = sorted(
            glob.glob(str(state_dir / "*.treg.jsonl"))
            + glob.glob(str(state_dir / "*.final.jsonl"))
            + glob.glob(str(state_dir / "discovered_leads*.jsonl")),
            key=os.path.getmtime,
        )
        if jsonl_candidates:
            latest_jsonl = Path(jsonl_candidates[-1])
            leads = load_leads_from_jsonl(
                latest_jsonl,
                touch_mode=str(b2b_cfg.get("touch_mode") or "direct"),
                use_llm=False,
            )
            b2b_limit = min(remaining_quota, int(b2b_cfg.get("campaign_daily_limit") or 30))
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
                    res_tg = call_mcp_tool_handler(
                        conn,
                        "ingest_discovered_leads",
                        {
                            "leads": tg_leads,
                            "enroll_in_campaign": bool(cfg.get("auto_push_to_crm", True)),
                            "campaign_name": target_tg_camp,
                        },
                    )
                    ingested_total += int(res_tg.get("enrolled_in_campaign") or res_tg.get("created_leads") or 0)
                if wa_leads:
                    res_wa = call_mcp_tool_handler(
                        conn,
                        "ingest_discovered_leads",
                        {
                            "leads": wa_leads,
                            "enroll_in_campaign": bool(cfg.get("auto_push_to_crm", True)),
                            "campaign_name": str(b2b_cfg.get("target_campaign_wa") or "Leadgen Auto (WA)"),
                        },
                    )
                    ingested_total += int(res_wa.get("enrolled_in_campaign") or res_wa.get("created_leads") or 0)

    return {
        "ok": True,
        "ingested": ingested_total,
        "campaigns_synced": sync_res["synced_count"],
        "message": (
            f"Синхронизировано рассылок: {sync_res['synced_count']}, залито лидов: {ingested_total}"
        ),
    }
