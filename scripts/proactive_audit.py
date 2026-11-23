#!/usr/bin/env python3
"""Proactive DB, ICP hygiene & server leadgen readiness auditor for crmtg.

Exits with code 1 whenever:
  1. Reply-alert invariants fail (colleague bot self-echo, bots, team/self DMs,
     closed clients, or non-lead DMs in `bot_reply_notifications` / `leads`).
  2. Sniper ICP hygiene invariants fail (`active_junk_recipient_count > 0`,
     `active_junk_lead_count > 0`, `contactless_lead_count > 0`, or dirty titles).
  3. Server leadgen readiness fails when `--enforce-vps-readiness` or
     `CRMTG_VPS_RUNTIME=1` is set, or via live `--health-url` (`icp_hygiene_ok`
     or `leadgen_ready` is False, e.g. missing `SCRAPER_TG_SESSION`, `treg`,
     LLM key, or `leadgen_stage_errors` timeouts).

Usage:
  python scripts/proactive_audit.py --db data/crm.db
  python scripts/proactive_audit.py --db data/crm.db --heal
  python scripts/proactive_audit.py --db data/crm.db --enforce-vps-readiness
  python scripts/proactive_audit.py --health-url http://127.0.0.1:18084/api/health?deep=1
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.accounts import (  # noqa: E402
    compute_account_pool_summary,
    reconcile_disk_sessions_with_db,
)
from app.airtable_sync import prune_redundant_airtable_synced_events  # noqa: E402
from app.analytics import _sync_running_and_all_campaign_replies, get_analytics_summary  # noqa: E402
from app.db import (  # noqa: E402
    DEFAULT_DB,
    connect,
    init_db,
    is_newer_dialog_snapshot_unbacked,
    is_recipient_contacted_outreach,
    is_vacancy_campaign_meta,
    is_waiting_our_answer,
    resolve_session,
    utc_now,
)
from app.digest import (  # noqa: E402
    audit_reply_notifications,
    build_ineligible_peer_context,
    heal_reply_notifications,
    is_ineligible_reply_notify_peer,
)
from app.funnel_analytics import ensure_funnel_columns  # noqa: E402
from app.inbox import (  # noqa: E402
    _PERSONAL_HYGIENE_ERRORS,
    _norm_ts,
    clean_tg_username_lookup,
    get_inbox_counts,
    list_inbox,
    list_messages,
    repair_dialog_last_from_peer_drift,
)
from app.leads import create_lead, search_leads  # noqa: E402
from app.leadgen_settings import get_leadgen_config  # noqa: E402
from app.main import load_env_file  # noqa: E402
from app.outreach import (  # noqa: E402
    _build_recipient_dialogs_index,
    _dialog_has_client_reply,
    _find_recipient_dialogs,
    _latest_dialog_in_meta,
)
from app.runtime_config import is_vps_runtime  # noqa: E402
from app.telegram_gateway import (  # noqa: E402
    classify_proxy_mode,
    extract_auth_key_sha256_from_sqlite,
    extract_auth_key_sha256_from_string,
    extract_string_session_from_sqlite,
    resolve_nodemaven_config,
    write_sqlite_session_from_string,
)

from app.hygiene import (  # noqa: E402
    STAGE_ERROR_TTL_SECONDS,
    _audit_per_campaign_leadgen_status,
    _parse_iso_ts_utc,
    audit_crm_hygiene as _audit_crm_hygiene,
    audit_server_leadgen_readiness as _audit_server_leadgen_readiness,
    heal_crm_hygiene as _raw_heal_crm_hygiene,
    inspect_leadgen_run_anomalies,
    read_stage_errors_list,
)
from leadgen.language_router import assert_copy_matches_language  # noqa: E402

ANALYTICS_SUMMARY_MAX_BYTES = 60 * 1024
AIRTABLE_SYNCED_24H_MAX = 500
LEAD_EVENTS_MAX_BYTES = 10 * 1024 * 1024
DISCONNECTS_1H_MAX_PER_ACCOUNT = 10
DEFAULT_DAILY_ENRICHMENT_CAP_USD = 5.0
MAX_UNGUARDED_DAILY_ENRICHMENT_CAP_USD = 100.0
_CREATOROS_RE = re.compile(r"\bCreator\s*OS\b|creatoros", re.IGNORECASE)
_PLACEHOLDER_TOKEN_RE = re.compile(r"\{[A-Za-z0-9_]+\}")


def _audit_ssot_statistics(conn: sqlite3.Connection) -> dict:
    """Verify SSOT analytics math, waiting parity, <=60KB payload budget, and dropped account visibility."""
    summary = get_analytics_summary(conn, period="30d")
    issues: list[str] = []
    deep_sum = (summary.get("deep_outreach") or {}).get("summary") or {}
    contacted = int(deep_sum.get("contacted") or 0)
    read_cnt = int(deep_sum.get("read") or 0)
    unseen_cnt = int(deep_sum.get("unseen_total") or 0)
    if contacted > 0 and read_cnt + unseen_cnt != contacted:
        issues.append(f"read({read_cnt})+unseen({unseen_cnt})!=contacted({contacted})")

    payload_bytes = len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
    if payload_bytes > ANALYTICS_SUMMARY_MAX_BYTES:
        issues.append(f"analytics_summary_payload_bytes({payload_bytes})>{ANALYTICS_SUMMARY_MAX_BYTES}")

    inbox_waiting = int(
        (get_inbox_counts(conn, account_id=0, crm_only=True) or {}).get("waiting") or 0
    )
    analytics_waiting = int(deep_sum.get("waiting_our_answer") or 0)
    inbox_rows = list_inbox(conn, account_id=0, crm_only=True, limit=0)
    eligible_dlg_ids = {int(d["id"]) for d in inbox_rows if d.get("id") is not None}
    inbox_linked_waiting = sum(
        1
        for d in inbox_rows
        if d.get("is_waiting_our_answer") and d.get("lead_id") is not None
    )
    leads_crm_linked_waiting = sum(
        1
        for ld in search_leads(conn)
        if ld.get("dialog_id") is not None
        and int(ld["dialog_id"]) in eligible_dlg_ids
        and is_waiting_our_answer(
            ld.get("last_from_peer"), ld.get("status"), ld.get("airtable_status")
        )
    )
    if inbox_waiting != analytics_waiting or inbox_linked_waiting != leads_crm_linked_waiting:
        issues.append(
            f"ssot_waiting_parity_mismatch(inbox={inbox_waiting},analytics={analytics_waiting},"
            f"inbox_linked={inbox_linked_waiting},leads_linked={leads_crm_linked_waiting})"
        )

    accounts_obj = summary.get("accounts") or {}
    dropped_items = accounts_obj.get("dropped_accounts") or []
    dropped_ids = {int(a["id"]) for a in dropped_items if a.get("id") is not None}
    db_dropped = conn.execute(
        "SELECT id, COALESCE(telegram_username, telegram_name, phone, '') AS label, status FROM accounts "
        "WHERE lower(COALESCE(status, '')) NOT IN ('connected', 'active', 'online')"
    ).fetchall()
    missing_dropped = [
        f"#{r['id']}({r['label'] or r['status']})"
        for r in db_dropped
        if int(r["id"]) not in dropped_ids
    ]
    if missing_dropped:
        issues.append(f"dropped_accounts_hidden={missing_dropped}")

    # Verify daily_flow excludes non-CRM personal dialogs (peer with no lead & no campaign_recipient)
    personal_msgs_7d = conn.execute(
        """
        SELECT COUNT(*) FROM messages m
        JOIN dialogs d ON d.id = m.dialog_id
        WHERE d.peer_type = 'user'
          AND d.lead_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM campaign_recipients cr
              WHERE (d.peer_id IS NOT NULL AND cr.telegram_user_id = d.peer_id)
                 OR (d.username IS NOT NULL AND cr.username IS NOT NULL AND lower(cr.username) = lower(d.username))
          )
        """
    ).fetchone()[0]

    return {
        "ok": not issues,
        "issues": issues,
        "contacted": contacted,
        "read": read_cnt,
        "unseen_total": unseen_cnt,
        "analytics_payload_bytes": payload_bytes,
        "waiting_parity": {
            "inbox": inbox_waiting,
            "analytics": analytics_waiting,
            "inbox_linked_leads": inbox_linked_waiting,
            "leads_crm_linked": leads_crm_linked_waiting,
            "unlinked_waiting_dialogs": max(0, inbox_waiting - inbox_linked_waiting),
        },
        "dropped_accounts_count": len(dropped_items),
        "db_dropped_accounts_count": len(db_dropped),
        "ignored_personal_messages_total": int(personal_msgs_7d or 0),
    }


def _extract_blob_auth_key_sha256(blob: bytes | bytearray | memoryview | None) -> str | None:
    if not blob:
        return None
    raw = bytes(blob)
    if len(raw) < 128:
        return None
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as tf:
            tf.write(raw)
            tmp_path = Path(tf.name)
        return extract_auth_key_sha256_from_sqlite(tmp_path)
    except OSError:
        return None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _resolve_db_session_path(session_path: str | None, *, db_path: Path | None) -> Path | None:
    raw = str(session_path or "").strip()
    if not raw or raw.startswith("data/sessions/demo"):
        return None
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    if db_path is not None:
        is_canonical = db_path.resolve() == DEFAULT_DB.resolve()
        candidates = [
            db_path.parent.parent / raw,
            db_path.parent / raw,
            db_path.parent / "sessions" / p.name,
        ]
        for cand in candidates:
            if cand.exists():
                return cand
        if not is_canonical:
            return None
    return resolve_session(raw)


def _audit_session_auth_keys_and_parity(
    conn: sqlite3.Connection,
    *,
    db_path: Path | None = None,
    scraper_sessions_dir: Path | None = None,
) -> dict:
    """Verify 0 duplicate auth_key SHA-256 across accounts + scraper_sessions and DB/disk parity."""
    is_canonical = db_path is None or db_path.resolve() == DEFAULT_DB.resolve()
    rows = conn.execute(
        """
        SELECT id, phone, telegram_username, session_path, session_string, session_blob, status, health
        FROM accounts
        WHERE COALESCE(provider, 'telegram') != 'whatsapp'
        ORDER BY id ASC
        """
    ).fetchall()

    owners_by_sha: dict[str, list[dict]] = {}
    parity_mismatches: list[dict] = []

    for r in rows:
        acc_id = int(r["id"])
        label = str(r["telegram_username"] or r["phone"] or r["session_path"] or f"#{acc_id}")
        str_sess = str(r["session_string"] or "").strip()
        str_sha = extract_auth_key_sha256_from_string(str_sess) if str_sess else None
        disk_p = _resolve_db_session_path(r["session_path"], db_path=db_path)
        disk_sha = (
            extract_auth_key_sha256_from_sqlite(disk_p)
            if (disk_p is not None and disk_p.is_file())
            else None
        )
        blob_sha = (
            _extract_blob_auth_key_sha256(r["session_blob"])
            if (not str_sha and r["session_blob"])
            else None
        )

        if str_sha and disk_p is not None and disk_p.is_file() and disk_sha != str_sha:
            parity_mismatches.append(
                {
                    "account_id": acc_id,
                    "label": label,
                    "session_path": str(disk_p),
                    "db_sha256": str_sha[:16],
                    "disk_sha256": (disk_sha[:16] if disk_sha else None),
                }
            )

        for sha in {s for s in (str_sha, disk_sha, blob_sha) if s}:
            owners_by_sha.setdefault(sha, []).append(
                {
                    "owner": f"account:{acc_id}",
                    "kind": "account",
                    "account_id": acc_id,
                    "label": label,
                }
            )

    eff_scraper_dir = scraper_sessions_dir
    if eff_scraper_dir is None:
        env_dir = os.environ.get("SCRAPER_SESSIONS_DIR", "").strip()
        if env_dir:
            eff_scraper_dir = Path(env_dir).expanduser()
        elif db_path is not None:
            cand_dir = db_path.parent / "scraper_sessions"
            if cand_dir.is_dir():
                eff_scraper_dir = cand_dir
            elif is_canonical and is_vps_runtime() and Path("/opt/crmtg/data/scraper_sessions").is_dir():
                eff_scraper_dir = Path("/opt/crmtg/data/scraper_sessions")

    seen_scraper_files: set[Path] = set()
    if eff_scraper_dir is not None and eff_scraper_dir.is_dir():
        for sess_file in sorted(eff_scraper_dir.glob("*.session")):
            res_f = sess_file.resolve()
            seen_scraper_files.add(res_f)
            s_sha = extract_auth_key_sha256_from_sqlite(res_f)
            if s_sha:
                owners_by_sha.setdefault(s_sha, []).append(
                    {
                        "owner": f"scraper:{sess_file.name}",
                        "kind": "scraper_session",
                        "path": str(sess_file),
                    }
                )

    env_scraper_sess = os.environ.get("SCRAPER_TG_SESSION", "").strip()
    if env_scraper_sess and (is_canonical or scraper_sessions_dir is None):
        sp = Path(env_scraper_sess).expanduser()
        if sp.is_file() and sp.resolve() not in seen_scraper_files:
            s_sha = extract_auth_key_sha256_from_sqlite(sp)
            if s_sha:
                owners_by_sha.setdefault(s_sha, []).append(
                    {
                        "owner": f"scraper:{sp.name}",
                        "kind": "scraper_session",
                        "path": str(sp),
                    }
                )

    duplicate_auth_keys: list[dict] = []
    for sha, owners in owners_by_sha.items():
        distinct_owners = {o["owner"] for o in owners}
        if len(distinct_owners) >= 2:
            duplicate_auth_keys.append({"sha256": sha[:16], "owners": owners})

    return {
        "ok": len(duplicate_auth_keys) == 0 and len(parity_mismatches) == 0,
        "duplicate_auth_key_count": len(duplicate_auth_keys),
        "duplicate_auth_keys": duplicate_auth_keys,
        "session_parity_mismatch_count": len(parity_mismatches),
        "session_parity_mismatches": parity_mismatches,
    }


def _audit_airtable_sync_bloat(
    conn: sqlite3.Connection,
    *,
    max_24h_events: int = AIRTABLE_SYNCED_24H_MAX,
    max_table_bytes: int = LEAD_EVENTS_MAX_BYTES,
) -> dict:
    """Verify airtable_synced event count < 500 in 24h and lead_events size < 10 MB."""
    from datetime import datetime, timedelta, timezone

    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(lead_events)").fetchall()}
    except sqlite3.Error:
        cols = set()
    if not cols:
        return {"ok": True, "issues": [], "airtable_synced_24h": 0, "lead_events_bytes": 0}

    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    recent_cnt = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM lead_events
            WHERE event_type = 'airtable_synced'
              AND replace(substr(created_at, 1, 19), 'T', ' ') >= ?
            """,
            (cutoff_24h,),
        ).fetchone()[0]
        or 0
    )

    table_bytes = 0
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = 'lead_events'"
        ).fetchone()
        if row is not None and row[0] is not None:
            table_bytes = int(row[0])
    except sqlite3.OperationalError:
        text_cols = [c for c in ("event_type", "title", "meta_json", "created_at") if c in cols]
        sum_expr = " + ".join(f"length(CAST(COALESCE({c}, '') AS BLOB))" for c in text_cols)
        if sum_expr:
            sum_expr = f"{sum_expr} + 64"
        else:
            sum_expr = "64"
        table_bytes = int(
            conn.execute(f"SELECT COALESCE(SUM({sum_expr}), 0) FROM lead_events").fetchone()[0]
            or 0
        )

    issues: list[str] = []
    if recent_cnt >= int(max_24h_events):
        issues.append(f"airtable_synced_24h({recent_cnt})>={max_24h_events}")
    if table_bytes >= int(max_table_bytes):
        issues.append(f"lead_events_bytes({table_bytes})>={max_table_bytes}")

    return {
        "ok": not issues,
        "issues": issues,
        "airtable_synced_24h": recent_cnt,
        "lead_events_bytes": table_bytes,
    }


def _audit_dialog_last_from_peer_drift(conn: sqlite3.Connection) -> dict:
    """Verify dialogs.last_from_peer agrees with latest cached message direction (except unbacked live snapshots)."""
    dlg_rows = conn.execute(
        "SELECT id, account_id, username, title, last_from_peer, last_message_at, last_message_preview FROM dialogs"
    ).fetchall()
    if not dlg_rows:
        return {"ok": True, "checked": 0, "drift_count": 0, "drifted_dialogs": []}

    dlg_map = {int(r["id"]): r for r in dlg_rows}
    latest_by_dlg: dict[int, tuple[tuple[str, int], str, str, str]] = {}
    dids = list(dlg_map.keys())
    for start in range(0, len(dids), 400):
        chunk = dids[start : start + 400]
        ph = ",".join("?" for _ in chunk)
        for m in conn.execute(
            f"SELECT id, dialog_id, direction, text, sent_at FROM messages WHERE dialog_id IN ({ph})",
            chunk,
        ).fetchall():
            direction = str(m["direction"] or "").strip().lower()
            if direction not in ("in", "out"):
                continue
            did = int(m["dialog_id"])
            key = (_norm_ts(m["sent_at"]), int(m["id"]))
            prev = latest_by_dlg.get(did)
            if prev is None or key > prev[0]:
                latest_by_dlg[did] = (
                    key,
                    direction,
                    str(m["sent_at"] or ""),
                    str(m["text"] or ""),
                )

    drifted: list[dict] = []
    for did, (_key, m_dir, m_sent_at, m_text) in latest_by_dlg.items():
        drow = dlg_map[did]
        cur_lfp = int(drow["last_from_peer"] or 0)
        exp_lfp = 1 if m_dir == "in" else 0
        if cur_lfp == exp_lfp:
            continue
        if is_newer_dialog_snapshot_unbacked(
            drow["last_message_preview"],
            drow["last_message_at"],
            cur_lfp,
            m_text,
            m_sent_at,
            m_dir,
        ):
            continue
        drifted.append(
            {
                "dialog_id": did,
                "account_id": drow["account_id"],
                "username": drow["username"],
                "last_from_peer": cur_lfp,
                "expected_last_from_peer": exp_lfp,
            }
        )

    return {
        "ok": len(drifted) == 0,
        "checked": len(latest_by_dlg),
        "drift_count": len(drifted),
        "drifted_dialogs": drifted[:20],
    }


def _resolve_audit_state_dir(
    *,
    db_path: Path | None = None,
    state_dir: Path | None = None,
) -> Path:
    if state_dir is not None:
        return state_dir
    env_sd = os.environ.get("LEADGEN_STATE_DIR", "").strip()
    if env_sd:
        return Path(env_sd).expanduser()
    if db_path is not None and db_path.resolve() != DEFAULT_DB.resolve():
        return db_path.parent / "leadgen_state"
    return ROOT / "data" / "leadgen_state"


def _audit_leadgen_starvation_and_anomalies(
    conn: sqlite3.Connection,
    *,
    db_path: Path | None = None,
    state_dir: Path | None = None,
    critical_low_mark: int = 15,
) -> dict:
    """Verify per-campaign leadgen starvation (pending<15 or ingested_72h==0), 0-bridge streak, static dumps & rc!=0 crashes."""
    from datetime import datetime, timezone

    eff_state_dir = _resolve_audit_state_dir(db_path=db_path, state_dir=state_dir)
    campaigns = _audit_per_campaign_leadgen_status(
        conn, state_dir=eff_state_dir, critical_low_mark=critical_low_mark
    )
    campaigns_starved: list[dict] = []
    for c in campaigns:
        pending = int(c.get("pending") or 0)
        ingested_72h = int(c.get("ingested_72h") or 0)
        not_running_b2b = str(c.get("starvation_reason") or "") == "b2b_tg_campaign_not_running"
        is_starved = not_running_b2b or pending < int(critical_low_mark) or ingested_72h == 0
        if is_starved:
            enriched = dict(c)
            if not_running_b2b:
                enriched["starvation_reason"] = "b2b_tg_campaign_not_running"
            else:
                enriched["starvation_reason"] = (
                    "pending_below_mark_and_zero_72h"
                    if (pending < int(critical_low_mark) and ingested_72h == 0)
                    else ("pending_below_mark" if pending < int(critical_low_mark) else "zero_ingested_72h")
                )
            campaigns_starved.append(enriched)

    anomalies = inspect_leadgen_run_anomalies(eff_state_dir)
    stage_errors_active: list[dict] = []
    now_dt = datetime.now(timezone.utc)
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM kv_settings WHERE key = 'leadgen_stage_errors'"
        ).fetchone()
    except sqlite3.Error:
        row = None
    row_dt = _parse_iso_ts_utc(row["updated_at"]) if row is not None else None
    for item in read_stage_errors_list(conn):
        item_dt = _parse_iso_ts_utc(item.get("at")) or row_dt
        if item_dt is not None and (now_dt - item_dt).total_seconds() > STAGE_ERROR_TTL_SECONDS:
            continue
        try:
            rc = int(item.get("returncode", item.get("rc", 0)) or 0)
        except (TypeError, ValueError):
            rc = -1
        if rc != 0 or bool(item.get("timeout")) or "sigsegv" in str(item.get("error") or "").lower():
            stage_errors_active.append(item)

    last_run = anomalies.get("last_run") or {}
    last_run_rc = 0
    for key in ("rc", "returncode", "exit_code"):
        if last_run.get(key) is not None:
            try:
                last_run_rc = int(last_run[key])
                break
            except (TypeError, ValueError):
                last_run_rc = -1

    stage_crash_alert = bool(stage_errors_active) or (last_run_rc != 0)
    starvation_alert = bool(campaigns_starved)
    tg_bridged_zero_alert = bool(anomalies.get("tg_bridged_zero_alert"))
    static_dump_alert = bool(anomalies.get("static_dump_alert"))

    issues: list[str] = []
    if starvation_alert:
        labels = [
            f"camp#{c['campaign_id']}({c['name']}:pending={c['pending']},72h={c['ingested_72h']})"
            for c in campaigns_starved
        ]
        issues.append(f"leadgen_starvation={labels}")
        for c in campaigns_starved:
            if c.get("starvation_reason") == "b2b_tg_campaign_not_running":
                if "b2b_tg_campaign_not_running" not in issues:
                    issues.append("b2b_tg_campaign_not_running")
                if (
                    c.get("issue_code") == "whales_en_tg_starved"
                    or "whales" in str(c.get("name") or "").lower()
                ) and "whales_en_tg_starved" not in issues:
                    issues.append("whales_en_tg_starved")
    if tg_bridged_zero_alert:
        issues.append(f"tg_bridged_zero_streak={anomalies.get('tg_bridged_zero_streak')}")
    if static_dump_alert:
        issues.append(f"static_dump_repeats={len(anomalies.get('static_dump_repeats') or [])}")
    if stage_crash_alert:
        issues.append(
            f"stage_crash_or_nonzero_rc(active_errors={len(stage_errors_active)},last_run_rc={last_run_rc})"
        )

    return {
        "ok": not issues,
        "issues": issues,
        "campaigns": campaigns,
        "campaigns_starved": campaigns_starved,
        "starvation_alert": starvation_alert,
        "tg_bridged_zero_streak": int(anomalies.get("tg_bridged_zero_streak") or 0),
        "tg_bridged_zero_alert": tg_bridged_zero_alert,
        "static_dump_repeats": anomalies.get("static_dump_repeats") or [],
        "static_dump_alert": static_dump_alert,
        "stage_crash_alert": stage_crash_alert,
        "stage_errors_active": stage_errors_active,
        "last_run_rc": last_run_rc,
    }


def _audit_proxy_modes(conn: sqlite3.Connection, *, strict_static_proxy: bool = False) -> dict:
    """Warn on rotating NodeMaven proxies by default; fail only when --strict-static-proxy is set."""
    pool = compute_account_pool_summary(conn)
    rotating = list(pool.get("rotating_proxy_accounts") or [])
    if not rotating:
        nm_cfg = resolve_nodemaven_config(conn)
        if nm_cfg is not None:
            for r in conn.execute(
                """
                SELECT id, phone, telegram_username, proxy_url
                FROM accounts
                WHERE status = 'connected' AND COALESCE(provider, 'telegram') != 'whatsapp'
                ORDER BY id ASC
                """
            ).fetchall():
                pmode = classify_proxy_mode(r["proxy_url"], nodemaven_configured=True)
                if pmode == "rotating_nodemaven":
                    rotating.append(
                        {
                            "id": int(r["id"]),
                            "phone": r["phone"],
                            "telegram_username": r["telegram_username"],
                            "proxy_mode": pmode,
                        }
                    )

    warning_msg = ""
    if rotating:
        handles = ", ".join(
            f"#{a['id']}({a.get('telegram_username') or a.get('phone') or ''})" for a in rotating
        )
        warning_msg = (
            f"WARNING: {len(rotating)} connected account(s) use rotating NodeMaven proxy ({handles}); "
            "swap to static residential ISP SOCKS5 recommended"
        )
    ok = (not strict_static_proxy) or (len(rotating) == 0)
    return {
        "ok": ok,
        "strict_static_proxy": bool(strict_static_proxy),
        "rotating_proxy_count": len(rotating),
        "rotating_proxy_accounts": rotating,
        "warning": warning_msg or None,
    }


def _audit_old_lead_message_visibility(conn: sqlite3.Connection) -> dict:
    """Verify every client/contacted reply is visible in Inbox, marked replied, and has readable latest msg."""
    inelig_ctx = build_ineligible_peer_context(conn)
    dialogs_idx = _build_recipient_dialogs_index(conn)
    visible_rows = list_inbox(conn, 0, "", crm_only=True, limit=0)
    visible_ids = {int(r["id"]) for r in visible_rows if r.get("id") is not None}

    hidden_replied_dialogs: list[dict] = []
    unmarked_replied_recipients: list[dict] = []
    invisible_latest_messages: list[dict] = []

    lead_ids_by_tg: dict[int, list[int]] = {}
    lead_ids_by_uname: dict[str, list[int]] = {}
    won_lead_ids: set[int] = set()
    all_lead_ids: set[int] = set()
    for lr in conn.execute(
        "SELECT id, telegram_user_id, username, status FROM leads"
    ).fetchall():
        lid = int(lr["id"])
        all_lead_ids.add(lid)
        if str(lr["status"] or "").strip().lower() in ("won", "signed"):
            won_lead_ids.add(lid)
        if lr["telegram_user_id"] is not None:
            lead_ids_by_tg.setdefault(int(lr["telegram_user_id"]), []).append(lid)
        cu = clean_tg_username_lookup(lr["username"])
        if cu:
            lead_ids_by_uname.setdefault(cu, []).append(lid)

    recs_all = conn.execute(
        """
        SELECT cr.id, cr.campaign_id, cr.lead_id, cr.telegram_user_id, cr.username, cr.phone,
               cr.status, cr.current_step, cr.last_sent_at, cr.sent_account_id,
               cr.assigned_account_id, cr.error_text, cr.created_at,
               c.name AS camp_name, c.leadgen_sources_json AS camp_sources
        FROM campaign_recipients cr
        LEFT JOIN campaigns c ON c.id = cr.campaign_id
        """
    ).fetchall()
    recs_by_lid: dict[int, list[dict]] = {}
    recs_by_tg: dict[int, list[dict]] = {}
    recs_by_u: dict[str, list[dict]] = {}
    for rr in recs_all:
        rd = dict(rr)
        rd["_is_vac"] = is_vacancy_campaign_meta(rd.get("camp_name"), rd.get("camp_sources"))
        if rd.get("lead_id") is not None:
            recs_by_lid.setdefault(int(rd["lead_id"]), []).append(rd)
        if rd.get("telegram_user_id") is not None:
            recs_by_tg.setdefault(int(rd["telegram_user_id"]), []).append(rd)
        ru = clean_tg_username_lookup(rd.get("username"))
        if ru:
            recs_by_u.setdefault(ru, []).append(rd)

    dlg_rows = conn.execute(
        """
        SELECT d.id, d.account_id, d.peer_id, d.username, d.title, d.phone,
               d.lead_id, d.is_bot, d.last_from_peer, d.last_message_at,
               d.last_message_preview,
               EXISTS(SELECT 1 FROM messages m WHERE m.dialog_id = d.id AND m.direction = 'in') AS has_in_msg,
               EXISTS(SELECT 1 FROM messages m WHERE m.dialog_id = d.id) AS has_any_msg
        FROM dialogs d
        WHERE d.is_group = 0 AND COALESCE(d.peer_type, 'user') = 'user'
        """
    ).fetchall()
    for drow in dlg_rows:
        did = int(drow["id"])
        acc_id = int(drow["account_id"]) if drow["account_id"] is not None else None
        peer_id = int(drow["peer_id"]) if drow["peer_id"] is not None else None
        uname = clean_tg_username_lookup(drow["username"])
        has_reply_sig = int(drow["last_from_peer"] or 0) == 1 or bool(drow["has_in_msg"])
        has_body = bool(str(drow["last_message_preview"] or "").strip()) or bool(drow["has_any_msg"])
        if not (has_reply_sig and has_body):
            continue
        inelig, _ = is_ineligible_reply_notify_peer(
            conn,
            peer_id=peer_id,
            username=uname or None,
            phone=drow["phone"],
            account_id=acc_id,
            is_bot=bool(drow["is_bot"]),
            preloaded_ctx=inelig_ctx,
        )
        if inelig:
            continue

        matched_lids: set[int] = set()
        if drow["lead_id"] is not None and int(drow["lead_id"]) in all_lead_ids:
            matched_lids.add(int(drow["lead_id"]))
        if peer_id is not None and peer_id in lead_ids_by_tg:
            matched_lids.update(lead_ids_by_tg[peer_id])
        if uname and uname in lead_ids_by_uname:
            matched_lids.update(lead_ids_by_uname[uname])

        matched_recs: list[dict] = []
        seen_rids: set[int] = set()
        for bucket in (
            *(recs_by_lid.get(lid, ()) for lid in matched_lids),
            recs_by_tg.get(peer_id, ()) if peer_id is not None else (),
            recs_by_u.get(uname, ()) if uname else (),
        ):
            for rec in bucket:
                rid = int(rec["id"])
                if rid not in seen_rids:
                    seen_rids.add(rid)
                    matched_recs.append(rec)

        vac_recs = [r for r in matched_recs if r["_is_vac"]]
        reg_recs = [r for r in matched_recs if not r["_is_vac"]]
        has_won_lead = any(lid in won_lead_ids for lid in matched_lids)
        if vac_recs and not reg_recs and not has_won_lead:
            continue

        has_valid_contacted_rec = any(
            int(r.get("current_step") or 0) >= 1
            and bool(str(r.get("last_sent_at") or "").strip())
            and is_recipient_contacted_outreach(
                r.get("status"),
                r.get("current_step"),
                r.get("last_sent_at"),
                r.get("error_text"),
            )
            and (
                (r.get("sent_account_id") if r.get("sent_account_id") is not None else r.get("assigned_account_id"))
                in (None, acc_id)
            )
            for r in reg_recs
        )
        if (matched_lids or has_valid_contacted_rec) and did not in visible_ids:
            hidden_replied_dialogs.append(
                {
                    "dialog_id": did,
                    "account_id": acc_id,
                    "username": drow["username"],
                    "title": drow["title"],
                }
            )

    # Check 2: contacted non-vacancy non-hygiene recipients with an incoming reply must be 'replied'
    for rr in recs_all:
        rec = dict(rr)
        if is_vacancy_campaign_meta(rec.get("camp_name"), rec.get("camp_sources")):
            continue
        st = str(rec.get("status") or "").strip().lower()
        if st not in ("in_progress", "completed", "failed"):
            continue
        if not is_recipient_contacted_outreach(
            st,
            rec.get("current_step"),
            rec.get("last_sent_at"),
            rec.get("error_text"),
        ):
            continue
        if int(rec.get("current_step") or 0) < 1 or not str(rec.get("last_sent_at") or "").strip():
            continue
        dialogs = _find_recipient_dialogs(
            conn,
            rec,
            account_id=rec.get("sent_account_id") or rec.get("assigned_account_id"),
            dialogs_index=dialogs_idx,
        )
        replied_cands = [d for d in dialogs if _dialog_has_client_reply(conn, d, rec)]
        if not replied_cands:
            dialogs_any = _find_recipient_dialogs(
                conn,
                rec,
                account_id=None,
                ignore_assigned_account=True,
                dialogs_index=dialogs_idx,
            )
            replied_cands = [d for d in dialogs_any if _dialog_has_client_reply(conn, d, rec)]
        rec_last_sent = _norm_ts(rec.get("last_sent_at"))
        for d in replied_cands:
            d_inelig, _ = is_ineligible_reply_notify_peer(
                conn,
                peer_id=d.get("peer_id"),
                username=d.get("username"),
                phone=d.get("phone"),
                account_id=d.get("account_id"),
                is_bot=bool(d.get("is_bot")),
                preloaded_ctx=inelig_ctx,
            )
            if d_inelig:
                continue
            _, incoming_sent_at = _latest_dialog_in_meta(conn, d)
            if incoming_sent_at and rec_last_sent and rec_last_sent > incoming_sent_at:
                continue
            unmarked_replied_recipients.append(
                {
                    "recipient_id": int(rec["id"]),
                    "campaign_id": int(rec["campaign_id"]),
                    "status": st,
                    "username": rec.get("username"),
                    "dialog_id": int(d["id"]),
                }
            )
            break

    # Check 3: every visible CRM dialog with preview + timestamp must show its latest msg in list_messages
    for d in visible_rows:
        snap_ts = _norm_ts(d.get("last_message_at"))
        snap_prev = str(d.get("last_message_preview") or "").strip()
        if not snap_ts or not snap_prev:
            continue
        did = int(d["id"])
        msgs = list_messages(conn, did, 50)
        if not msgs:
            invisible_latest_messages.append(
                {"dialog_id": did, "reason": "empty_list_messages", "last_message_at": snap_ts}
            )
            continue
        newest_ts = max(_norm_ts(m.get("sent_at")) for m in msgs)
        if is_newer_dialog_snapshot_unbacked(
            snap_prev,
            d.get("last_message_at"),
            d.get("last_from_peer"),
            msgs[-1].get("text"),
            msgs[-1].get("sent_at"),
            msgs[-1].get("direction"),
        ):
            invisible_latest_messages.append(
                {
                    "dialog_id": did,
                    "reason": "latest_msg_older_than_snapshot",
                    "last_message_at": snap_ts,
                    "newest_msg_at": newest_ts,
                }
            )

    ok = (
        len(hidden_replied_dialogs) == 0
        and len(unmarked_replied_recipients) == 0
        and len(invisible_latest_messages) == 0
    )
    return {
        "ok": ok,
        "visible_crm_dialogs_count": len(visible_ids),
        "hidden_replied_dialog_count": len(hidden_replied_dialogs),
        "unmarked_replied_recipient_count": len(unmarked_replied_recipients),
        "invisible_latest_message_count": len(invisible_latest_messages),
        "hidden_replied_dialogs": hidden_replied_dialogs[:20],
        "unmarked_replied_recipients": unmarked_replied_recipients[:20],
        "invisible_latest_messages": invisible_latest_messages[:20],
    }


def _heal_crm_hygiene(conn) -> dict:
    return _raw_heal_crm_hygiene(conn, clear_stale_stage_errors=True)


def _heal_lead_dialog_links(conn: sqlite3.Connection) -> dict:
    """Backfill NULL leads.dialog_id from latest linked user dialog (d.lead_id = l.id)."""
    rows = conn.execute(
        """
        SELECT l.id AS lead_id,
               (
                   SELECT d.id
                   FROM dialogs d
                   WHERE d.lead_id = l.id AND d.peer_type = 'user'
                   ORDER BY COALESCE(d.last_message_at, d.updated_at) DESC, d.id DESC
                   LIMIT 1
               ) AS dlg_id
        FROM leads l
        WHERE l.dialog_id IS NULL
          AND EXISTS (
              SELECT 1 FROM dialogs d2
              WHERE d2.lead_id = l.id AND d2.peer_type = 'user'
          )
        """
    ).fetchall()
    repaired_ids: list[int] = []
    for row in rows:
        if row["dlg_id"] is None:
            continue
        conn.execute(
            "UPDATE leads SET dialog_id = ? WHERE id = ? AND dialog_id IS NULL",
            (int(row["dlg_id"]), int(row["lead_id"])),
        )
        repaired_ids.append(int(row["lead_id"]))
    if repaired_ids:
        conn.commit()
    return {"repaired": len(repaired_ids), "lead_ids": repaired_ids[:50]}


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(r[1]) for r in rows if len(r) > 1}


def _audit_ru_expert_airtable_isolation(conn: sqlite3.Connection) -> dict:
    """Verify every ru_expert lead and campaign enforces exclude_from_airtable == 1."""
    lead_cols = _table_columns(conn, "leads")
    camp_cols = _table_columns(conn, "campaigns")
    rec_cols = _table_columns(conn, "campaign_recipients")
    violations: list[dict] = []

    bad_leads_count = 0
    if "segment" in lead_cols:
        if "exclude_from_airtable" in lead_cols:
            rows = conn.execute(
                """
                SELECT id, name, username, segment, COALESCE(exclude_from_airtable, 0) AS excl
                FROM leads
                WHERE lower(trim(COALESCE(segment, ''))) = 'ru_expert'
                  AND COALESCE(exclude_from_airtable, 0) != 1
                ORDER BY id ASC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, name, username, segment, 0 AS excl
                FROM leads
                WHERE lower(trim(COALESCE(segment, ''))) = 'ru_expert'
                ORDER BY id ASC
                """
            ).fetchall()
        bad_leads_count = len(rows)
        for r in rows[:20]:
            violations.append(
                {
                    "kind": "ru_expert_lead_not_excluded",
                    "lead_id": int(r["id"]),
                    "username": r["username"],
                    "exclude_from_airtable": int(r["excl"]),
                }
            )

    bad_camps_count = 0
    if "segment" in camp_cols:
        if "exclude_from_airtable" in camp_cols:
            c_rows = conn.execute(
                """
                SELECT id, name, segment, COALESCE(exclude_from_airtable, 0) AS excl
                FROM campaigns
                WHERE lower(trim(COALESCE(segment, ''))) = 'ru_expert'
                  AND COALESCE(exclude_from_airtable, 0) != 1
                ORDER BY id ASC
                """
            ).fetchall()
        else:
            c_rows = conn.execute(
                """
                SELECT id, name, segment, 0 AS excl
                FROM campaigns
                WHERE lower(trim(COALESCE(segment, ''))) = 'ru_expert'
                ORDER BY id ASC
                """
            ).fetchall()
        bad_camps_count = len(c_rows)
        for cr in c_rows[:20]:
            violations.append(
                {
                    "kind": "ru_expert_campaign_not_excluded",
                    "campaign_id": int(cr["id"]),
                    "name": cr["name"],
                    "exclude_from_airtable": int(cr["excl"]),
                }
            )

    bad_linked_count = 0
    if (
        "segment" in camp_cols
        and "lead_id" in rec_cols
        and "campaign_id" in rec_cols
        and "exclude_from_airtable" in lead_cols
    ):
        linked_rows = conn.execute(
            """
            SELECT DISTINCT l.id AS lead_id, l.username, c.id AS campaign_id,
                   COALESCE(l.exclude_from_airtable, 0) AS excl
            FROM campaign_recipients cr
            JOIN campaigns c ON c.id = cr.campaign_id
            JOIN leads l ON l.id = cr.lead_id
            WHERE lower(trim(COALESCE(c.segment, ''))) = 'ru_expert'
              AND COALESCE(l.exclude_from_airtable, 0) != 1
            ORDER BY l.id ASC
            """
        ).fetchall()
        bad_linked_count = len(linked_rows)
        for lr in linked_rows[:20]:
            violations.append(
                {
                    "kind": "ru_expert_campaign_lead_not_excluded",
                    "lead_id": int(lr["lead_id"]),
                    "campaign_id": int(lr["campaign_id"]),
                    "username": lr["username"],
                    "exclude_from_airtable": int(lr["excl"]),
                }
            )

    return {
        "ok": len(violations) == 0,
        "unisolated_ru_expert_lead_count": bad_leads_count,
        "unisolated_ru_expert_campaign_count": bad_camps_count,
        "unisolated_linked_lead_count": bad_linked_count,
        "violations": violations[:20],
    }


def _heal_ru_expert_airtable_isolation(conn: sqlite3.Connection) -> dict:
    """Ensure every ru_expert lead and campaign sets exclude_from_airtable = 1."""
    lead_cols = _table_columns(conn, "leads")
    camp_cols = _table_columns(conn, "campaigns")
    rec_cols = _table_columns(conn, "campaign_recipients")
    healed_leads = 0
    healed_camps = 0
    if "segment" in lead_cols and "exclude_from_airtable" in lead_cols:
        cur = conn.execute(
            """
            UPDATE leads
            SET exclude_from_airtable = 1
            WHERE lower(trim(COALESCE(segment, ''))) = 'ru_expert'
              AND COALESCE(exclude_from_airtable, 0) != 1
            """
        )
        healed_leads += int(cur.rowcount or 0)
    if "segment" in camp_cols and "exclude_from_airtable" in camp_cols:
        cur_c = conn.execute(
            """
            UPDATE campaigns
            SET exclude_from_airtable = 1
            WHERE lower(trim(COALESCE(segment, ''))) = 'ru_expert'
              AND COALESCE(exclude_from_airtable, 0) != 1
            """
        )
        healed_camps += int(cur_c.rowcount or 0)
    if (
        "segment" in camp_cols
        and "lead_id" in rec_cols
        and "campaign_id" in rec_cols
        and "exclude_from_airtable" in lead_cols
    ):
        cur_l = conn.execute(
            """
            UPDATE leads
            SET exclude_from_airtable = 1
            WHERE id IN (
                SELECT DISTINCT cr.lead_id
                FROM campaign_recipients cr
                JOIN campaigns c ON c.id = cr.campaign_id
                WHERE cr.lead_id IS NOT NULL
                  AND lower(trim(COALESCE(c.segment, ''))) = 'ru_expert'
            )
              AND COALESCE(exclude_from_airtable, 0) != 1
            """
        )
        healed_leads += int(cur_l.rowcount or 0)
    if healed_leads or healed_camps:
        conn.commit()
    return {"healed_leads": healed_leads, "healed_campaigns": healed_camps}


def _has_non_placeholder_prose(text: str) -> bool:
    stripped = _PLACEHOLDER_TOKEN_RE.sub(" ", str(text or ""))
    stripped = re.sub(r"https?://\S+|www\.\S+|@[A-Za-z0-9_]+", " ", stripped)
    return bool(re.search(r"[A-Za-z\u0400-\u04FF]", stripped))


def _extract_custom_step_texts(custom_raw: str) -> list[tuple[str, str]]:
    try:
        parsed = json.loads(custom_raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    out: list[tuple[str, str]] = []
    for key in (
        "step1",
        "step1_text",
        "step1_template",
        "first_touch",
        "1",
        "step2",
        "step2_text",
        "followup_1",
        "2",
        "step3",
        "step3_text",
        "followup_2",
        "3",
    ):
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            out.append((key, val))
        elif isinstance(val, dict):
            txt = val.get("template_text") if "template_text" in val else val.get("text")
            if isinstance(txt, str) and txt.strip():
                out.append((key, txt))
    steps_val = parsed.get("steps")
    if isinstance(steps_val, dict):
        for s_key, entry in steps_val.items():
            if isinstance(entry, str) and entry.strip():
                out.append((f"steps.{s_key}", entry))
            elif isinstance(entry, dict):
                txt = entry.get("template_text") if "template_text" in entry else entry.get("text")
                if isinstance(txt, str) and txt.strip():
                    out.append((f"steps.{s_key}", txt))
    elif isinstance(steps_val, list):
        for idx, entry in enumerate(steps_val, start=1):
            if isinstance(entry, str) and entry.strip():
                out.append((f"steps[{idx}]", entry))
            elif isinstance(entry, dict):
                txt = entry.get("template_text") if "template_text" in entry else entry.get("text")
                if isinstance(txt, str) and txt.strip():
                    out.append((f"steps.{entry.get('step_order') or idx}", txt))
    return out


def _extract_custom_step1_texts(custom_raw: str) -> list[str]:
    step1_keys = {"step1", "step1_text", "step1_template", "first_touch", "1", "steps.1", "steps[1]"}
    return [txt for lbl, txt in _extract_custom_step_texts(custom_raw) if lbl in step1_keys]


def _audit_ru_language_campaigns(conn: sqlite3.Connection) -> dict:
    """Verify RU and EN campaigns never hold mismatched-language templates/copies or CreatorOS."""
    camp_cols = _table_columns(conn, "campaigns")
    if "language" not in camp_cols:
        return {"ok": True, "checked_campaigns": 0, "violation_count": 0, "violations": []}

    has_custom_col = "custom_step_templates_json" in camp_cols
    sel_custom = ", custom_step_templates_json" if has_custom_col else ", NULL AS custom_step_templates_json"
    lang_camps = conn.execute(
        f"""
        SELECT id, name, status, lower(trim(COALESCE(language, ''))) AS lang {sel_custom}
        FROM campaigns
        WHERE lower(trim(COALESCE(language, ''))) IN ('ru', 'en')
        ORDER BY id ASC
        """
    ).fetchall()
    violations: list[dict] = []

    for c in lang_camps:
        cid = int(c["id"])
        cname = str(c["name"] or "")
        lang = str(c["lang"] or "").strip().lower()
        if _CREATOROS_RE.search(cname):
            violations.append(
                {
                    "kind": f"creatoros_in_{lang}_campaign_name",
                    "language": lang,
                    "campaign_id": cid,
                    "campaign_name": cname,
                }
            )

        custom_raw = str(c["custom_step_templates_json"] or "").strip()
        if custom_raw:
            if _CREATOROS_RE.search(custom_raw):
                violations.append(
                    {
                        "kind": f"creatoros_in_{lang}_custom_templates",
                        "language": lang,
                        "campaign_id": cid,
                        "campaign_name": cname,
                    }
                )
            for key_lbl, s_txt in _extract_custom_step_texts(custom_raw):
                if _has_non_placeholder_prose(s_txt):
                    ok_lang, reason = assert_copy_matches_language(
                        s_txt, lang, allow_cyrillic_name=False  # type: ignore[arg-type]
                    )
                    if not ok_lang:
                        violations.append(
                            {
                                "kind": f"non_{lang}_custom_template",
                                "language": lang,
                                "step_key": key_lbl,
                                "campaign_id": cid,
                                "campaign_name": cname,
                                "reason": reason,
                            }
                        )

        step_rows = conn.execute(
            "SELECT step_order, template_text FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order ASC",
            (cid,),
        ).fetchall()
        for sr in step_rows:
            step_num = int(sr["step_order"] or 0)
            tmpl = str(sr["template_text"] or "")
            if _CREATOROS_RE.search(tmpl):
                violations.append(
                    {
                        "kind": f"creatoros_in_{lang}_step_template",
                        "language": lang,
                        "campaign_id": cid,
                        "campaign_name": cname,
                        "step_order": step_num,
                    }
                )
            if _has_non_placeholder_prose(tmpl):
                ok_lang, reason = assert_copy_matches_language(
                    tmpl, lang, allow_cyrillic_name=False  # type: ignore[arg-type]
                )
                if not ok_lang:
                    violations.append(
                        {
                            "kind": f"non_{lang}_step_template",
                            "language": lang,
                            "step_order": step_num,
                            "campaign_id": cid,
                            "campaign_name": cname,
                            "reason": reason,
                            "sample": tmpl[:120],
                        }
                    )

        rec_rows = conn.execute(
            """
            SELECT id, username, step1_text, status
            FROM campaign_recipients
            WHERE campaign_id = ?
              AND lower(COALESCE(status, 'pending')) IN ('pending', 'in_progress')
              AND step1_text IS NOT NULL
              AND trim(step1_text) != ''
            """,
            (cid,),
        ).fetchall()
        for rr in rec_rows:
            s1_rec = str(rr["step1_text"] or "")
            if _CREATOROS_RE.search(s1_rec):
                violations.append(
                    {
                        "kind": f"creatoros_in_{lang}_recipient_step1",
                        "language": lang,
                        "campaign_id": cid,
                        "recipient_id": int(rr["id"]),
                        "username": rr["username"],
                    }
                )
            elif _has_non_placeholder_prose(s1_rec):
                ok_lang, reason = assert_copy_matches_language(
                    s1_rec, lang, allow_cyrillic_name=True  # type: ignore[arg-type]
                )
                if not ok_lang:
                    violations.append(
                        {
                            "kind": f"non_{lang}_recipient_step1",
                            "language": lang,
                            "campaign_id": cid,
                            "recipient_id": int(rr["id"]),
                            "username": rr["username"],
                            "reason": reason,
                        }
                    )

    return {
        "ok": len(violations) == 0,
        "checked_campaigns": len(lang_camps),
        "violation_count": len(violations),
        "violations": violations[:20],
    }


def _resolve_enrichment_daily_cap_usd(
    conn: sqlite3.Connection,
    *,
    daily_cap_usd: float | None = None,
) -> float:
    if daily_cap_usd is not None:
        return float(daily_cap_usd)
    try:
        cfg = get_leadgen_config(conn)
        enr = cfg.get("enrichment") if isinstance(cfg, dict) else None
        if isinstance(enr, dict) and enr.get("daily_budget_cap_usd") is not None:
            parsed = float(enr["daily_budget_cap_usd"])
            if math.isfinite(parsed) and parsed >= 0.0:
                return min(parsed, MAX_UNGUARDED_DAILY_ENRICHMENT_CAP_USD)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    return DEFAULT_DAILY_ENRICHMENT_CAP_USD


def _audit_enrichment_ledger_bounds(
    conn: sqlite3.Connection,
    *,
    daily_cap_usd: float | None = None,
) -> dict:
    """Verify enrichment_ledger daily spend per provider and per day stays within guarded caps."""
    cols = _table_columns(conn, "enrichment_ledger")
    if not cols:
        return {
            "ok": True,
            "daily_cap_usd": DEFAULT_DAILY_ENRICHMENT_CAP_USD,
            "checked_days": 0,
            "violation_count": 0,
            "violations": [],
        }

    cap = _resolve_enrichment_daily_cap_usd(conn, daily_cap_usd=daily_cap_usd)
    violations: list[dict] = []

    raw_rows = conn.execute(
        "SELECT id, day, provider, endpoint, cost_usd FROM enrichment_ledger ORDER BY id ASC"
    ).fetchall()
    for r in raw_rows:
        try:
            cost_val = float(r["cost_usd"])
        except (TypeError, ValueError):
            cost_val = math.nan
        if not math.isfinite(cost_val) or cost_val < 0.0:
            violations.append(
                {
                    "kind": "invalid_enrichment_cost_usd",
                    "id": int(r["id"]),
                    "day": r["day"],
                    "provider": r["provider"],
                    "cost_usd": r["cost_usd"],
                }
            )

    prov_rows = conn.execute(
        """
        SELECT day,
               lower(trim(COALESCE(provider, ''))) AS provider,
               COALESCE(SUM(cost_usd), 0.0) AS total_usd,
               COUNT(*) AS calls
        FROM enrichment_ledger
        GROUP BY day, lower(trim(COALESCE(provider, '')))
        ORDER BY day DESC, provider ASC
        """
    ).fetchall()
    for pr in prov_rows:
        prov_total = float(pr["total_usd"] or 0.0)
        if not math.isfinite(prov_total) or prov_total > cap + 1e-6:
            violations.append(
                {
                    "kind": "provider_daily_spend_exceeded",
                    "day": pr["day"],
                    "provider": pr["provider"],
                    "total_usd": round(prov_total, 6),
                    "daily_cap_usd": cap,
                    "calls": int(pr["calls"] or 0),
                }
            )

    day_rows = conn.execute(
        """
        SELECT day,
               COALESCE(SUM(cost_usd), 0.0) AS total_usd,
               COUNT(*) AS calls
        FROM enrichment_ledger
        GROUP BY day
        ORDER BY day DESC
        """
    ).fetchall()
    for dr in day_rows:
        day_total = float(dr["total_usd"] or 0.0)
        if not math.isfinite(day_total) or day_total > cap + 1e-6:
            violations.append(
                {
                    "kind": "total_daily_spend_exceeded",
                    "day": dr["day"],
                    "total_usd": round(day_total, 6),
                    "daily_cap_usd": cap,
                    "calls": int(dr["calls"] or 0),
                }
            )

    return {
        "ok": len(violations) == 0,
        "daily_cap_usd": cap,
        "checked_days": len(day_rows),
        "violation_count": len(violations),
        "violations": violations[:20],
    }


def _collect_v2_audit_reports(conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
    return (
        _audit_ru_expert_airtable_isolation(conn),
        _audit_ru_language_campaigns(conn),
        _audit_enrichment_ledger_bounds(conn),
    )


def _audit_local_db(
    db_path: Path,
    *,
    heal: bool,
    since_hours: int | None,
    enforce_vps_readiness: bool = False,
    strict_static_proxy: bool = False,
    check_leadgen_starvation: bool = False,
    scraper_sessions_dir: Path | None = None,
    leadgen_state_dir: Path | None = None,
) -> int:
    if not db_path.exists():
        print(f"proactive_audit: ERROR db not found: {db_path}", file=sys.stderr)
        return 1

    check_readiness = bool(enforce_vps_readiness or is_vps_runtime())
    readiness_report: dict | None = None

    if heal:
        conn = connect(db_path)
        try:
            healed_replies = heal_reply_notifications(conn)
            _sync_running_and_all_campaign_replies(conn)
            healed_hygiene = _heal_crm_hygiene(conn)
            healed_drift = repair_dialog_last_from_peer_drift(conn)
            healed_events = prune_redundant_airtable_synced_events(conn)
            healed_sessions = reconcile_disk_sessions_with_db(conn)
            healed_links = _heal_lead_dialog_links(conn)
            healed_ru_iso = _heal_ru_expert_airtable_isolation(conn)
            print(
                "proactive_audit: healed "
                + json.dumps(
                    {
                        "reply_notifications": healed_replies,
                        "crm_hygiene": healed_hygiene,
                        "dialog_drift": healed_drift,
                        "airtable_events_pruned": healed_events,
                        "session_parity": healed_sessions,
                        "lead_dialog_links": healed_links,
                        "ru_expert_isolation": healed_ru_iso,
                    },
                    ensure_ascii=False,
                )
            )
            reply_report = audit_reply_notifications(conn, since_hours=since_hours)
            hygiene_report = _audit_crm_hygiene(conn)
            stats_report = _audit_ssot_statistics(conn)
            vis_report = _audit_old_lead_message_visibility(conn)
            sess_report = _audit_session_auth_keys_and_parity(
                conn, db_path=db_path, scraper_sessions_dir=scraper_sessions_dir
            )
            bloat_report = _audit_airtable_sync_bloat(conn)
            drift_report = _audit_dialog_last_from_peer_drift(conn)
            leadgen_anomalies = _audit_leadgen_starvation_and_anomalies(
                conn, db_path=db_path, state_dir=leadgen_state_dir
            )
            proxy_report = _audit_proxy_modes(conn, strict_static_proxy=strict_static_proxy)
            ru_iso_report, ru_lang_report, ledger_report = _collect_v2_audit_reports(conn)
            if check_readiness:
                readiness_report = _audit_server_leadgen_readiness(
                    conn, enforce_vps_checks=True
                )
        finally:
            conn.close()
    else:
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            reply_report = audit_reply_notifications(conn, since_hours=since_hours)
            hygiene_report = _audit_crm_hygiene(conn)
            stats_report = _audit_ssot_statistics(conn)
            vis_report = _audit_old_lead_message_visibility(conn)
            sess_report = _audit_session_auth_keys_and_parity(
                conn, db_path=db_path, scraper_sessions_dir=scraper_sessions_dir
            )
            bloat_report = _audit_airtable_sync_bloat(conn)
            drift_report = _audit_dialog_last_from_peer_drift(conn)
            leadgen_anomalies = _audit_leadgen_starvation_and_anomalies(
                conn, db_path=db_path, state_dir=leadgen_state_dir
            )
            proxy_report = _audit_proxy_modes(conn, strict_static_proxy=strict_static_proxy)
            ru_iso_report, ru_lang_report, ledger_report = _collect_v2_audit_reports(conn)
            if check_readiness:
                readiness_report = _audit_server_leadgen_readiness(
                    conn, enforce_vps_checks=True
                )
        finally:
            conn.close()

    if check_leadgen_starvation:
        overall_ok = bool(not leadgen_anomalies.get("starvation_alert"))
    else:
        overall_ok = (
            bool(reply_report.get("ok"))
            and bool(hygiene_report.get("ok"))
            and bool(stats_report.get("ok"))
            and bool(vis_report.get("ok"))
            and bool(sess_report.get("ok"))
            and bool(bloat_report.get("ok"))
            and bool(drift_report.get("ok"))
            and bool(leadgen_anomalies.get("ok"))
            and bool(proxy_report.get("ok"))
            and bool(ru_iso_report.get("ok"))
            and bool(ru_lang_report.get("ok"))
            and bool(ledger_report.get("ok"))
        )
        if readiness_report is not None and not bool(readiness_report.get("ok")):
            overall_ok = False

    combined = dict(reply_report)
    combined["ok"] = overall_ok
    combined["icp_hygiene_ok"] = bool(hygiene_report.get("ok"))
    combined["crm_hygiene"] = hygiene_report
    combined["ssot_stats_ok"] = bool(stats_report.get("ok"))
    combined["ssot_stats"] = stats_report
    combined["message_visibility_ok"] = bool(vis_report.get("ok"))
    combined["message_visibility"] = vis_report
    combined["sessions_integrity_ok"] = bool(sess_report.get("ok"))
    combined["sessions_integrity"] = sess_report
    combined["airtable_sync_bloat_ok"] = bool(bloat_report.get("ok"))
    combined["airtable_sync_bloat"] = bloat_report
    combined["dialog_direction_drift_ok"] = bool(drift_report.get("ok"))
    combined["dialog_direction_drift"] = drift_report
    combined["leadgen_starvation_and_anomalies_ok"] = bool(leadgen_anomalies.get("ok"))
    combined["leadgen_starvation_and_anomalies"] = leadgen_anomalies
    combined["proxy_modes_ok"] = bool(proxy_report.get("ok"))
    combined["proxy_modes"] = proxy_report
    combined["ru_expert_isolation_ok"] = bool(ru_iso_report.get("ok"))
    combined["ru_expert_isolation"] = ru_iso_report
    combined["ru_language_campaigns_ok"] = bool(ru_lang_report.get("ok"))
    combined["ru_language_campaigns"] = ru_lang_report
    combined["enrichment_ledger_bounds_ok"] = bool(ledger_report.get("ok"))
    combined["enrichment_ledger_bounds"] = ledger_report
    if readiness_report is not None:
        combined["leadgen_ready"] = bool(readiness_report.get("ok"))
        combined["leadgen_readiness"] = readiness_report

    print(json.dumps(combined, ensure_ascii=False, indent=2))
    if proxy_report.get("warning"):
        print(proxy_report["warning"], file=sys.stderr)

    reasons: list[str] = []
    if check_leadgen_starvation:
        if leadgen_anomalies.get("starvation_alert"):
            reasons.extend(
                i for i in (leadgen_anomalies.get("issues") or []) if "leadgen_starvation=" in i
            )
    else:
        if not reply_report.get("ok"):
            reasons.append(
                f"reply_notifications({reply_report.get('false_notification_count', 0)} false notification(s), "
                f"{reply_report.get('bot_username_lead_count', 0)} bot-username lead(s))"
            )
        if not hygiene_report.get("ok"):
            reasons.append(
                "crm_hygiene("
                f"active_junk_recipients={hygiene_report.get('active_junk_recipient_count', 0)}, "
                f"active_junk_leads={hygiene_report.get('active_junk_lead_count', 0)}, "
                f"contactless_leads={hygiene_report.get('contactless_lead_count', 0)}, "
                f"dirty_title_leads={hygiene_report.get('dirty_title_lead_count', 0)})"
            )
        if not stats_report.get("ok"):
            reasons.append(f"ssot_stats({', '.join(stats_report.get('issues') or [])})")
        if not vis_report.get("ok"):
            reasons.append(
                "message_visibility("
                f"hidden_replied_dialogs={vis_report.get('hidden_replied_dialog_count', 0)}, "
                f"unmarked_replied_recipients={vis_report.get('unmarked_replied_recipient_count', 0)}, "
                f"invisible_latest_messages={vis_report.get('invisible_latest_message_count', 0)})"
            )
        if not sess_report.get("ok"):
            reasons.append(
                "sessions_integrity("
                f"duplicate_auth_keys={sess_report.get('duplicate_auth_key_count', 0)}, "
                f"parity_mismatches={sess_report.get('session_parity_mismatch_count', 0)})"
            )
        if not bloat_report.get("ok"):
            reasons.append(f"airtable_bloat({', '.join(bloat_report.get('issues') or [])})")
        if not drift_report.get("ok"):
            reasons.append(f"dialog_direction_drift({drift_report.get('drift_count', 0)})")
        if not leadgen_anomalies.get("ok"):
            reasons.extend(leadgen_anomalies.get("issues") or [])
        if not proxy_report.get("ok"):
            reasons.append(
                f"strict_static_proxy(rotating_accounts={proxy_report.get('rotating_proxy_count', 0)})"
            )
        if not ru_iso_report.get("ok"):
            reasons.append(
                "ru_expert_isolation("
                f"leads={ru_iso_report.get('unisolated_ru_expert_lead_count', 0)}, "
                f"campaigns={ru_iso_report.get('unisolated_ru_expert_campaign_count', 0)}, "
                f"linked={ru_iso_report.get('unisolated_linked_lead_count', 0)})"
            )
        if not ru_lang_report.get("ok"):
            reasons.append(
                f"ru_language_campaigns(violations={ru_lang_report.get('violation_count', 0)})"
            )
        if not ledger_report.get("ok"):
            reasons.append(
                f"enrichment_ledger_bounds(violations={ledger_report.get('violation_count', 0)}, "
                f"cap={ledger_report.get('daily_cap_usd')})"
            )
        if readiness_report is not None and not readiness_report.get("ok"):
            missing = readiness_report.get("missing_components") or []
            reasons.append(
                f"leadgen_readiness(missing_components={missing}, "
                f"stage_timeout_count={readiness_report.get('stage_timeout_count', 0)})"
            )

    if reasons:
        print(f"PROACTIVE AUDIT FAILED: {'; '.join(reasons)}", file=sys.stderr)
        return 1

    print("proactive_audit: OK")
    return 0


def _audit_health_url(
    url: str,
    *,
    strict_static_proxy: bool = False,
    check_leadgen_starvation: bool = False,
    api_key: str | None = None,
) -> int:
    headers = {"User-Agent": "Mozilla/5.0 crmtg-proactive-audit/1.0"}
    eff_key = (api_key or os.environ.get("MCP_API_KEY") or "").strip()
    if eff_key:
        headers["Authorization"] = f"Bearer {eff_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    print(json.dumps(data, ensure_ascii=False, indent=2))

    account_pool = data.get("account_pool") if isinstance(data.get("account_pool"), dict) else {}
    rotating_accs = list(account_pool.get("rotating_proxy_accounts") or [])
    if rotating_accs:
        handles = ", ".join(
            f"#{a.get('id')}({a.get('telegram_username') or a.get('phone') or ''})"
            for a in rotating_accs
        )
        print(
            f"WARNING: {len(rotating_accs)} connected account(s) on rotating NodeMaven proxy ({handles}); "
            "swap to static residential ISP SOCKS5 recommended",
            file=sys.stderr,
        )

    failed_checks: list[str] = []
    if check_leadgen_starvation:
        starved = [
            c
            for c in (data.get("leadgen_campaigns") or [])
            if int(c.get("pending") or 0) < 15 or int(c.get("ingested_72h") or 0) == 0
        ]
        if starved or data.get("leadgen_starvation_alert") is True:
            failed_checks.append(f"leadgen_starvation={starved or data.get('campaigns_starved')}")
    else:
        if data.get("db_ok") is not True:
            failed_checks.append(f"db_ok={data.get('db_ok')!r}")
        if data.get("reply_alert_guard_ok") is not True:
            failed_checks.append(f"reply_alert_guard_ok={data.get('reply_alert_guard_ok')!r}")
        if int(data.get("false_reply_alerts_24h", 1)) != 0:
            failed_checks.append(
                f"false_reply_alerts_24h={data.get('false_reply_alerts_24h')!r}"
            )
        if data.get("icp_hygiene_ok") is not True:
            failed_checks.append(f"icp_hygiene_ok={data.get('icp_hygiene_ok')!r}")
        if data.get("leadgen_ready") is not True:
            failed_checks.append(f"leadgen_ready={data.get('leadgen_ready')!r}")
        if data.get("tg_lease_ready") is False:
            failed_checks.append("tg_lease_ready=False")
        disc_map = data.get("disconnects_1h_by_account")
        if isinstance(disc_map, dict):
            over = {k: v for k, v in disc_map.items() if int(v or 0) > DISCONNECTS_1H_MAX_PER_ACCOUNT}
            if over:
                failed_checks.append(f"disconnects_1h>{DISCONNECTS_1H_MAX_PER_ACCOUNT}:{over}")
        if data.get("leadgen_starvation_alert") is True:
            failed_checks.append(f"leadgen_starvation_alert=True({data.get('campaigns_starved')})")
        if int(data.get("tg_bridged_zero_streak") or 0) >= 3:
            failed_checks.append(f"tg_bridged_zero_streak={data.get('tg_bridged_zero_streak')}")
        if data.get("static_dump_alert") is True:
            failed_checks.append("static_dump_alert=True")
        if strict_static_proxy and rotating_accs:
            failed_checks.append(f"strict_static_proxy_rotating_count={len(rotating_accs)}")

        # Optional sibling check on /api/analytics/summary payload <= 60 KB when accessible
        try:
            parsed_u = urllib.parse.urlparse(url)
            summary_url = urllib.parse.urlunparse(
                (parsed_u.scheme, parsed_u.netloc, "/api/analytics/summary", "", "", "")
            )
            s_req = urllib.request.Request(summary_url, headers=headers)
            with urllib.request.urlopen(s_req, timeout=10) as s_resp:
                raw_summary = s_resp.read()
                if len(raw_summary) > ANALYTICS_SUMMARY_MAX_BYTES:
                    failed_checks.append(
                        f"analytics_summary_payload_bytes={len(raw_summary)}>{ANALYTICS_SUMMARY_MAX_BYTES}"
                    )
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403, 404):
                failed_checks.append(f"analytics_summary_http={exc.code}")
        except Exception:
            pass

    if failed_checks:
        readiness_obj = (
            data.get("leadgen_readiness")
            if isinstance(data.get("leadgen_readiness"), dict)
            else {}
        )
        missing_components = readiness_obj.get("missing_components") or []
        print(
            f"PROACTIVE HEALTH AUDIT FAILED: {', '.join(failed_checks)}; "
            f"missing_components={missing_components}",
            file=sys.stderr,
        )
        return 1

    print("proactive_audit (health): OK")
    return 0


def _run_self_test() -> int:
    """Verify proactive_audit actually fails (ok=False) on injected violations, then heals to ok=True."""
    with tempfile.TemporaryDirectory(prefix="proactive_audit_selftest_") as td:
        db_path = Path(td) / "selftest.db"
        conn = connect(db_path)
        try:
            init_db(conn)
            ensure_funnel_columns(conn)
            now = utc_now()
            acc_id = int(
                conn.execute(
                    """
                    INSERT INTO accounts (phone, session_path, status, health, telegram_user_id, telegram_username, created_at, updated_at)
                    VALUES ('+79990001122', 's1.session', 'connected', 'active', 1001, 'sender_self', ?, ?)
                    """,
                    (now, now),
                ).lastrowid
            )
            camp_id = int(
                conn.execute(
                    """
                    INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, created_at, updated_at)
                    VALUES ('SoftUGC SelfTest', ?, ?, 20, 'running', ?, ?)
                    """,
                    (acc_id, f"[{acc_id}]", now, now),
                ).lastrowid
            )
            # Bug 1: bot username lead
            create_lead(
                conn,
                name="Bot Lead",
                username="helperbot",
                telegram_user_id=7001,
                status="lost",
                account_id=acc_id,
            )
            # Bug 2: unmarked completed recipient with incoming reply in dialog
            conn.execute(
                """
                INSERT INTO campaign_recipients (
                    campaign_id, telegram_user_id, username, name, step1_text,
                    current_step, status, sent_account_id, assigned_account_id, last_sent_at, created_at, updated_at
                ) VALUES (?, 7002, 'late_completed_lead', 'Late Lead', 'Hi', 1, 'completed', ?, ?, '2026-11-10T10:00:00', ?, ?)
                """,
                (camp_id, acc_id, acc_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, username, title, is_group,
                    unread_count, last_message_at, last_message_preview, last_from_peer,
                    created_at, updated_at
                ) VALUES (?, 'user', 7002, 'late_completed_lead', 'Late Lead', 0, 1, '2026-11-18T12:00:00', 'Да, давайте обсудим!', 1, ?, ?)
                """,
                (acc_id, now, now),
            )
            conn.commit()

            rep_before = audit_reply_notifications(conn)
            vis_before = _audit_old_lead_message_visibility(conn)
            if rep_before.get("ok") is not False:
                print("SELFTEST FAILED: audit_reply_notifications missed bot-username lead", file=sys.stderr)
                return 1
            if vis_before.get("ok") is not False or vis_before.get("unmarked_replied_recipient_count", 0) < 1:
                print(
                    f"SELFTEST FAILED: _audit_old_lead_message_visibility missed unmarked completed reply: {vis_before}",
                    file=sys.stderr,
                )
                return 1

            heal_reply_notifications(conn)
            _sync_running_and_all_campaign_replies(conn)
            _heal_crm_hygiene(conn)

            rep_after = audit_reply_notifications(conn)
            hyg_after = _audit_crm_hygiene(conn)
            stat_after = _audit_ssot_statistics(conn)
            vis_after = _audit_old_lead_message_visibility(conn)
            if not (
                rep_after.get("ok")
                and hyg_after.get("ok")
                and stat_after.get("ok")
                and vis_after.get("ok")
            ):
                print(
                    f"SELFTEST FAILED after heal: rep={rep_after} hyg={hyg_after} stat={stat_after} vis={vis_after}",
                    file=sys.stderr,
                )
                return 1

            # Prove Check 1 (hidden replied dialog) & Check 3 (invisible latest msg) also fail loudly
            from unittest.mock import patch

            with patch(__name__ + ".list_inbox", return_value=[]):
                c1_fail = _audit_old_lead_message_visibility(conn)
                if c1_fail.get("ok") is not False or c1_fail.get("hidden_replied_dialog_count", 0) < 1:
                    print(
                        f"SELFTEST FAILED: Check 1 missed hidden replied dialog: {c1_fail}",
                        file=sys.stderr,
                    )
                    return 1

            with patch(
                __name__ + ".list_messages",
                return_value=[{"text": "Old msg", "sent_at": "2026-10-01T00:00:00", "direction": "out"}],
            ):
                c3_fail = _audit_old_lead_message_visibility(conn)
                if c3_fail.get("ok") is not False or c3_fail.get("invisible_latest_message_count", 0) < 1:
                    print(
                        f"SELFTEST FAILED: Check 3 missed invisible latest message: {c3_fail}",
                        file=sys.stderr,
                    )
                    return 1

            # --- New checks (a)-(i) + strict static proxy negative/positive fixtures ---
            def _write_sqlite_sess(dest: Path, key_byte: bytes) -> None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                sc = sqlite3.connect(dest)
                try:
                    sc.execute(
                        "CREATE TABLE IF NOT EXISTS sessions ("
                        "dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER, "
                        "auth_key BLOB, takeout_id INTEGER)"
                    )
                    sc.execute("DELETE FROM sessions")
                    sc.execute(
                        "INSERT INTO sessions (dc_id, server_address, port, auth_key, takeout_id) "
                        "VALUES (2, '149.154.167.50', 443, ?, NULL)",
                        (key_byte * 256,),
                    )
                    sc.commit()
                finally:
                    sc.close()

            # (a) Duplicate auth_key SHA-256 across account + scraper_sessions & (b) DB/disk parity mismatch
            tmp_root = Path(td)
            sess_dir = tmp_root / "sessions"
            scraper_dir = tmp_root / "scraper_sessions"
            acc_sess_file = sess_dir / "s1.session"
            scraper_sess_file = scraper_dir / "dup.session"
            _write_sqlite_sess(acc_sess_file, b"\x11")
            _write_sqlite_sess(scraper_sess_file, b"\x11")
            canon_str = extract_string_session_from_sqlite(acc_sess_file)
            if not canon_str:
                print("SELFTEST FAILED: could not extract canon StringSession", file=sys.stderr)
                return 1
            conn.execute(
                "UPDATE accounts SET session_path = ?, session_string = ? WHERE id = ?",
                ("sessions/s1.session", canon_str, acc_id),
            )
            conn.commit()
            dup_res = _audit_session_auth_keys_and_parity(
                conn, db_path=db_path, scraper_sessions_dir=scraper_dir
            )
            if dup_res.get("ok") is not False or dup_res.get("duplicate_auth_key_count", 0) < 1:
                print(f"SELFTEST FAILED: duplicate auth_key missed: {dup_res}", file=sys.stderr)
                return 1

            # Fix duplicate scraper key -> inject parity mismatch on disk
            _write_sqlite_sess(scraper_sess_file, b"\x22")
            _write_sqlite_sess(acc_sess_file, b"\x99")
            par_res = _audit_session_auth_keys_and_parity(
                conn, db_path=db_path, scraper_sessions_dir=scraper_dir
            )
            if par_res.get("ok") is not False or par_res.get("session_parity_mismatch_count", 0) < 1:
                print(f"SELFTEST FAILED: DB/disk parity mismatch missed: {par_res}", file=sys.stderr)
                return 1
            write_sqlite_session_from_string(canon_str, acc_sess_file)
            sess_clean = _audit_session_auth_keys_and_parity(
                conn, db_path=db_path, scraper_sessions_dir=scraper_dir
            )
            if sess_clean.get("ok") is not True:
                print(f"SELFTEST FAILED: clean sessions failed: {sess_clean}", file=sys.stderr)
                return 1

            # (c) Airtable sync bloat (>= 500 in 24h & lead_events byte threshold)
            bloat_lead_id = int(
                create_lead(
                    conn,
                    name="Acme SaaS Co",
                    company="Acme SaaS Co",
                    niche="B2B SaaS",
                    lead_source="yc",
                    username="acme_founder_clean",
                    telegram_user_id=7010,
                    status="new",
                    account_id=acc_id,
                )["id"]
            )
            conn.executemany(
                "INSERT INTO lead_events (lead_id, event_type, title, meta_json, created_at) "
                "VALUES (?, 'airtable_synced', 'Synced', '{}', ?)",
                [(bloat_lead_id, now) for _ in range(505)],
            )
            conn.commit()
            bloat_fail = _audit_airtable_sync_bloat(conn)
            if bloat_fail.get("ok") is not False or bloat_fail.get("airtable_synced_24h", 0) < 500:
                print(f"SELFTEST FAILED: airtable bloat 500+ missed: {bloat_fail}", file=sys.stderr)
                return 1
            if _audit_airtable_sync_bloat(conn, max_table_bytes=128).get("ok") is not False:
                print("SELFTEST FAILED: lead_events byte limit missed", file=sys.stderr)
                return 1
            prune_redundant_airtable_synced_events(conn)
            conn.commit()
            bloat_clean = _audit_airtable_sync_bloat(conn)
            if bloat_clean.get("ok") is not True:
                print(f"SELFTEST FAILED: bloat persisted after prune: {bloat_clean}", file=sys.stderr)
                return 1

            # (d) Per-campaign B2B starvation (not masked by OFM!) & (e) 0-bridge streak / static dump / rc!=0
            camp_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
            if "leadgen_enabled" in camp_cols:
                ofm_camp_id = int(
                    conn.execute(
                        "INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, leadgen_enabled, created_at, updated_at) "
                        "VALUES ('OFM Healthy', ?, ?, 20, 'running', 1, ?, ?)",
                        (acc_id, f"[{acc_id}]", now, now),
                    ).lastrowid
                )
                b2b_camp_id = int(
                    conn.execute(
                        "INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, leadgen_enabled, created_at, updated_at) "
                        "VALUES ('B2B Starved', ?, ?, 20, 'running', 1, ?, ?)",
                        (acc_id, f"[{acc_id}]", now, now),
                    ).lastrowid
                )
                for idx in range(20):
                    conn.execute(
                        "INSERT INTO campaign_recipients (campaign_id, telegram_user_id, username, name, step1_text, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 'Hi', 'pending', ?, ?)",
                        (ofm_camp_id, 8100 + idx, f"ofm_lead_{idx}", f"OFM Studio {idx}", now, now),
                    )
                state_dir = tmp_root / "leadgen_state"
                state_dir.mkdir(parents=True, exist_ok=True)
                (state_dir / "leadgen_run_history.jsonl").write_text(
                    "\n".join(
                        json.dumps(
                            {
                                "timestamp": now,
                                "tg_bridged_count": 0,
                                "source_hashes": {"direct_x": "deadbeefsame"},
                            }
                        )
                        for _ in range(3)
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (state_dir / "last_run_summary.json").write_text(
                    json.dumps({"timestamp": now, "rc": -11, "tg_bridged_count": 0}),
                    encoding="utf-8",
                )
                conn.commit()

                lg_fail = _audit_leadgen_starvation_and_anomalies(
                    conn, db_path=db_path, state_dir=state_dir
                )
                starved_ids = {int(c["campaign_id"]) for c in (lg_fail.get("campaigns_starved") or [])}
                if (
                    lg_fail.get("ok") is not False
                    or b2b_camp_id not in starved_ids
                    or ofm_camp_id in starved_ids
                    or not lg_fail.get("tg_bridged_zero_alert")
                    or not lg_fail.get("static_dump_alert")
                    or not lg_fail.get("stage_crash_alert")
                ):
                    print(f"SELFTEST FAILED: leadgen starvation/anomalies missed: {lg_fail}", file=sys.stderr)
                    return 1

                for idx in range(16):
                    conn.execute(
                        "INSERT INTO campaign_recipients (campaign_id, telegram_user_id, username, name, step1_text, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 'Hi', 'pending', ?, ?)",
                        (b2b_camp_id, 8200 + idx, f"b2b_lead_{idx}", f"B2B Corp {idx}", now, now),
                    )
                (state_dir / "leadgen_run_history.jsonl").write_text(
                    "\n".join(
                        [
                            json.dumps({"timestamp": now, "tg_bridged_count": 2, "source_hashes": {"direct_x": "h1"}}),
                            json.dumps({"timestamp": now, "tg_bridged_count": 4, "source_hashes": {"direct_x": "h2"}}),
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (state_dir / "last_run_summary.json").write_text(
                    json.dumps({"timestamp": now, "rc": 0, "tg_bridged_count": 4}),
                    encoding="utf-8",
                )
                conn.commit()
                lg_clean = _audit_leadgen_starvation_and_anomalies(
                    conn, db_path=db_path, state_dir=state_dir
                )
                if lg_clean.get("ok") is not True:
                    print(f"SELFTEST FAILED: leadgen healthy failed: {lg_clean}", file=sys.stderr)
                    return 1

                # Negative ratchet check: b2b.enabled=True & autopilot=1 with Whales EN — TG stuck in draft
                conn.execute(
                    "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('leadgen_config', ?, ?)",
                    (json.dumps({"enabled": True, "b2b": {"enabled": True}}), now),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('outreach_autopilot_enabled', '1', ?)",
                    (now,),
                )
                conn.execute("UPDATE campaigns SET status = 'draft' WHERE id = ?", (b2b_camp_id,))
                draft_whales_id = int(
                    conn.execute(
                        "INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, leadgen_enabled, created_at, updated_at) "
                        "VALUES ('Whales EN — TG', NULL, '[]', 30, 'draft', 1, ?, ?)",
                        (now, now),
                    ).lastrowid
                )
                conn.commit()
                draft_fail = _audit_leadgen_starvation_and_anomalies(
                    conn, db_path=db_path, state_dir=state_dir
                )
                draft_starved_ids = {
                    int(c["campaign_id"]) for c in (draft_fail.get("campaigns_starved") or [])
                }
                if (
                    draft_fail.get("ok") is not False
                    or draft_whales_id not in draft_starved_ids
                    or not any(
                        "b2b_tg_campaign_not_running" in iss or "whales_en_tg_starved" in iss
                        for iss in (draft_fail.get("issues") or [])
                    )
                ):
                    print(
                        f"SELFTEST FAILED: draft Whales EN — TG missed: {draft_fail}",
                        file=sys.stderr,
                    )
                    return 1
                conn.execute("DELETE FROM campaigns WHERE id = ?", (draft_whales_id,))
                conn.execute("UPDATE campaigns SET status = 'running' WHERE id = ?", (b2b_camp_id,))
                conn.execute("DELETE FROM kv_settings WHERE key IN ('leadgen_config', 'outreach_autopilot_enabled')")
                conn.commit()

            # (f) Dialog last_from_peer vs latest messages.direction drift
            drift_dlg_id = int(
                conn.execute(
                    """
                    INSERT INTO dialogs (
                        account_id, peer_type, peer_id, username, title, is_group,
                        unread_count, last_message_at, last_message_preview, last_from_peer,
                        lead_id, created_at, updated_at
                    ) VALUES (?, 'user', 7010, 'acme_founder_clean', 'Acme SaaS Co', 0, 1, '2026-11-18T15:00:00', 'Sent follow-up', 1, ?, ?, ?)
                    """,
                    (acc_id, bloat_lead_id, now, now),
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) "
                "VALUES (?, 9001, 'out', 'Sent follow-up', '2026-11-18T15:00:00', ?, ?)",
                (drift_dlg_id, now, now),
            )
            conn.commit()
            drift_fail = _audit_dialog_last_from_peer_drift(conn)
            if drift_fail.get("ok") is not False or drift_fail.get("drift_count", 0) < 1:
                print(f"SELFTEST FAILED: dialog direction drift missed: {drift_fail}", file=sys.stderr)
                return 1
            repair_dialog_last_from_peer_drift(conn)
            conn.commit()
            drift_clean = _audit_dialog_last_from_peer_drift(conn)
            if drift_clean.get("ok") is not True:
                print(f"SELFTEST FAILED: drift persisted after repair: {drift_clean}", file=sys.stderr)
                return 1

            # (g) SSOT waiting parity mismatch & (h) analytics payload > 60 KB
            with patch(__name__ + ".get_inbox_counts", return_value={"waiting": 99}):
                par_fail = _audit_ssot_statistics(conn)
                if par_fail.get("ok") is not False:
                    print(f"SELFTEST FAILED: SSOT waiting parity mismatch missed: {par_fail}", file=sys.stderr)
                    return 1
            oversized_summary = {
                "deep_outreach": {"summary": {"contacted": 0, "read": 0, "unseen_total": 0, "waiting_our_answer": 0}},
                "accounts": {"dropped_accounts": []},
                "bloat": "x" * (ANALYTICS_SUMMARY_MAX_BYTES + 2048),
            }
            with patch(__name__ + ".get_analytics_summary", return_value=oversized_summary):
                size_fail = _audit_ssot_statistics(conn)
                if size_fail.get("ok") is not False:
                    print(f"SELFTEST FAILED: >60KB analytics summary limit missed: {size_fail}", file=sys.stderr)
                    return 1
            if _audit_ssot_statistics(conn).get("ok") is not True:
                print(f"SELFTEST FAILED: clean SSOT stats failed: {_audit_ssot_statistics(conn)}", file=sys.stderr)
                return 1

            # (i) Rotating proxy WARNING vs --strict-static-proxy FAIL + live health endpoint gates
            conn.execute(
                "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('nodemaven_user', 'nm_user', ?)",
                (now,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('nodemaven_pass', 'nm_pass', ?)",
                (now,),
            )
            conn.commit()
            rot_warn = _audit_proxy_modes(conn, strict_static_proxy=False)
            rot_strict = _audit_proxy_modes(conn, strict_static_proxy=True)
            if rot_warn.get("ok") is not True or not rot_warn.get("warning") or rot_strict.get("ok") is not False:
                print(
                    f"SELFTEST FAILED: proxy warning/strict split broken: warn={rot_warn} strict={rot_strict}",
                    file=sys.stderr,
                )
                return 1

            import contextlib
            import io

            class _FakeResp:
                def __init__(self, payload: bytes):
                    self._payload = payload

                def read(self) -> bytes:
                    return self._payload

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            def _mock_health_call(health_payload: dict, *, summary_payload: dict | None = None, strict: bool = False):
                def _fake_urlopen(req, timeout=10):
                    u = getattr(req, "full_url", str(req))
                    if "/api/analytics/summary" in u:
                        return _FakeResp(json.dumps(summary_payload or {"ok": True}).encode("utf-8"))
                    return _FakeResp(json.dumps(health_payload).encode("utf-8"))

                out_buf = io.StringIO()
                err_buf = io.StringIO()
                with (
                    patch("urllib.request.urlopen", side_effect=_fake_urlopen),
                    contextlib.redirect_stdout(out_buf),
                    contextlib.redirect_stderr(err_buf),
                ):
                    rc_h = _audit_health_url(
                        "http://127.0.0.1:18084/api/health?deep=1",
                        strict_static_proxy=strict,
                    )
                return rc_h, err_buf.getvalue()

            base_health = {
                "db_ok": True,
                "reply_alert_guard_ok": True,
                "false_reply_alerts_24h": 0,
                "icp_hygiene_ok": True,
                "leadgen_ready": True,
                "tg_lease_ready": True,
                "disconnects_1h_by_account": {"1": 0},
                "leadgen_starvation_alert": False,
                "tg_bridged_zero_streak": 0,
                "static_dump_alert": False,
                "account_pool": {"rotating_proxy_accounts": []},
            }
            if _mock_health_call({**base_health, "tg_lease_ready": False})[0] != 1:
                print("SELFTEST FAILED: tg_lease_ready=False missed by _audit_health_url", file=sys.stderr)
                return 1
            if _mock_health_call({**base_health, "disconnects_1h_by_account": {"1": 11}})[0] != 1:
                print("SELFTEST FAILED: disconnects_1h > 10 missed by _audit_health_url", file=sys.stderr)
                return 1
            if _mock_health_call(base_health, summary_payload={"bloat": "z" * (ANALYTICS_SUMMARY_MAX_BYTES + 1024)})[0] != 1:
                print("SELFTEST FAILED: >60KB /api/analytics/summary missed by _audit_health_url", file=sys.stderr)
                return 1
            rot_health = {
                **base_health,
                "account_pool": {"rotating_proxy_accounts": [{"id": 1, "telegram_username": "sender_self"}]},
            }
            rc_rot_default, err_rot_default = _mock_health_call(rot_health, strict=False)
            rc_rot_strict, _ = _mock_health_call(rot_health, strict=True)
            if rc_rot_default != 0 or "WARNING:" not in err_rot_default or rc_rot_strict != 1:
                print(
                    f"SELFTEST FAILED: health rotating proxy warning vs strict split failed: "
                    f"default={rc_rot_default} err={err_rot_default!r} strict={rc_rot_strict}",
                    file=sys.stderr,
                )
                return 1

            # --- v2 checks (j) ru_expert isolation, (k) RU language templates/CreatorOS, (l) enrichment_ledger bounds ---
            v2_db_path = tmp_root / "v2_selftest.db"
            v2_state_dir = tmp_root / "v2_leadgen_state"
            v2_state_dir.mkdir(parents=True, exist_ok=True)
            v2_cfg_path = tmp_root / "v2_leadgen_config.json"
            v2_cfg_path.write_text(
                json.dumps({"enabled": True, "enrichment": {"daily_budget_cap_usd": 5.0}}),
                encoding="utf-8",
            )

            def _run_v2_db_audit() -> tuple[int, str]:
                v_out = io.StringIO()
                v_err = io.StringIO()
                with (
                    patch.dict(os.environ, {"LEADGEN_CONFIG_PATH": str(v2_cfg_path)}),
                    patch(__name__ + ".is_vps_runtime", return_value=False),
                    contextlib.redirect_stdout(v_out),
                    contextlib.redirect_stderr(v_err),
                ):
                    rc_v2 = _audit_local_db(
                        v2_db_path,
                        heal=False,
                        since_hours=None,
                        enforce_vps_readiness=False,
                        leadgen_state_dir=v2_state_dir,
                    )
                return rc_v2, v_err.getvalue()

            v2_conn = connect(v2_db_path)
            try:
                init_db(v2_conn)
                ensure_funnel_columns(v2_conn)

                # (j) ru_expert lead with exclude_from_airtable == 0 MUST fail exit != 0
                ru_lead = create_lead(
                    v2_conn,
                    name="Иван Эксперт",
                    company="Школа Эксперта",
                    niche="Образование",
                    lead_source="ru_experts",
                    username="ivan_expert_ru",
                    telegram_user_id=9101,
                    status="new",
                )
                v2_conn.execute(
                    "UPDATE leads SET segment = 'ru_expert', language = 'ru', exclude_from_airtable = 0 WHERE id = ?",
                    (int(ru_lead["id"]),),
                )
                v2_conn.commit()
                if _audit_ru_expert_airtable_isolation(v2_conn).get("ok") is not False:
                    print("SELFTEST FAILED: _audit_ru_expert_airtable_isolation missed exclude_from_airtable=0", file=sys.stderr)
                    return 1
                rc_ru_bad, err_ru_bad = _run_v2_db_audit()
                if rc_ru_bad == 0 or "ru_expert_isolation" not in err_ru_bad:
                    print(f"SELFTEST FAILED: _audit_local_db missed ru_expert exclude_from_airtable=0: rc={rc_ru_bad} err={err_ru_bad!r}", file=sys.stderr)
                    return 1
                v2_conn.execute("UPDATE leads SET exclude_from_airtable = 1 WHERE id = ?", (int(ru_lead["id"]),))
                v2_conn.commit()
                if _audit_ru_expert_airtable_isolation(v2_conn).get("ok") is not True:
                    print("SELFTEST FAILED: ru_expert isolation did not pass when exclude_from_airtable=1", file=sys.stderr)
                    return 1

                # (k) RU-language campaign with English-only Step 1 or CreatorOS MUST fail exit != 0
                ru_camp_id = int(
                    v2_conn.execute(
                        """
                        INSERT INTO campaigns (
                            name, daily_limit, status, segment, language, copy_profile,
                            exclude_from_airtable, created_at, updated_at
                        ) VALUES ('Client Reels RU SelfTest', 20, 'draft', 'vacancy', 'ru', 'vacancy_ru', 0, ?, ?)
                        """,
                        (now, now),
                    ).lastrowid
                )
                v2_conn.execute(
                    "INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at) "
                    "VALUES (?, 1, 0, 'Hey, we scale your SaaS with short-form video reels', ?)",
                    (ru_camp_id, now),
                )
                v2_conn.commit()
                if _audit_ru_language_campaigns(v2_conn).get("ok") is not False:
                    print("SELFTEST FAILED: _audit_ru_language_campaigns missed EN Step 1 in RU campaign", file=sys.stderr)
                    return 1
                rc_en_in_ru, err_en_in_ru = _run_v2_db_audit()
                if rc_en_in_ru == 0 or "ru_language_campaigns" not in err_en_in_ru:
                    print(f"SELFTEST FAILED: _audit_local_db missed EN Step 1 in RU campaign: rc={rc_en_in_ru} err={err_en_in_ru!r}", file=sys.stderr)
                    return 1

                v2_conn.execute(
                    "UPDATE campaign_steps SET template_text = 'Привет! Поможем через CreatorOS вырастить канал' "
                    "WHERE campaign_id = ? AND step_order = 1",
                    (ru_camp_id,),
                )
                v2_conn.commit()
                if _audit_ru_language_campaigns(v2_conn).get("ok") is not False:
                    print("SELFTEST FAILED: _audit_ru_language_campaigns missed CreatorOS in RU campaign", file=sys.stderr)
                    return 1
                rc_cos_in_ru, err_cos_in_ru = _run_v2_db_audit()
                if rc_cos_in_ru == 0 or "ru_language_campaigns" not in err_cos_in_ru:
                    print(f"SELFTEST FAILED: _audit_local_db missed CreatorOS in RU campaign: rc={rc_cos_in_ru} err={err_cos_in_ru!r}", file=sys.stderr)
                    return 1

                v2_conn.execute(
                    "UPDATE campaign_steps SET template_text = 'Добрый день! Выстраиваем систему лидгена через короткие ролики SoftUGC.' "
                    "WHERE campaign_id = ? AND step_order = 1",
                    (ru_camp_id,),
                )
                v2_conn.commit()
                if _audit_ru_language_campaigns(v2_conn).get("ok") is not True:
                    print("SELFTEST FAILED: valid RU Step 1 failed _audit_ru_language_campaigns", file=sys.stderr)
                    return 1

                # (k2) EN-language campaign with RU leak in steps/custom/recipients or CreatorOS MUST fail exit != 0
                en_camp_id = int(
                    v2_conn.execute(
                        """
                        INSERT INTO campaigns (
                            name, daily_limit, status, segment, language, copy_profile,
                            exclude_from_airtable, created_at, updated_at
                        ) VALUES ('Whales EN SelfTest', 30, 'draft', 'whale', 'en', 'whale_mass', 0, ?, ?)
                        """,
                        (now, now),
                    ).lastrowid
                )
                v2_conn.execute(
                    "INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at) "
                    "VALUES (?, 1, 0, 'Привет! Check out SoftUGC reels', ?)",
                    (en_camp_id, now),
                )
                v2_conn.commit()
                if _audit_ru_language_campaigns(v2_conn).get("ok") is not False:
                    print("SELFTEST FAILED: _audit_ru_language_campaigns missed RU leak in EN campaign step", file=sys.stderr)
                    return 1
                rc_ru_in_en, err_ru_in_en = _run_v2_db_audit()
                if rc_ru_in_en == 0 or "ru_language_campaigns" not in err_ru_in_en:
                    print(f"SELFTEST FAILED: _audit_local_db missed RU leak in EN campaign: rc={rc_ru_in_en} err={err_ru_in_en!r}", file=sys.stderr)
                    return 1

                v2_conn.execute(
                    "UPDATE campaign_steps SET template_text = 'Hey, saw your app. Want 3 demo reels for Acme?' "
                    "WHERE campaign_id = ? AND step_order = 1",
                    (en_camp_id,),
                )
                v2_conn.execute(
                    "INSERT INTO campaign_recipients (campaign_id, telegram_user_id, username, name, step1_text, status, created_at, updated_at) "
                    "VALUES (?, 9202, 'en_ru_leak', 'Acme', 'Привет! We scale your reels with SoftUGC', 'pending', ?, ?)",
                    (en_camp_id, now, now),
                )
                v2_conn.commit()
                if _audit_ru_language_campaigns(v2_conn).get("ok") is not False:
                    print("SELFTEST FAILED: _audit_ru_language_campaigns missed RU greeting in EN recipient", file=sys.stderr)
                    return 1
                v2_conn.execute(
                    "UPDATE campaign_recipients SET step1_text = 'Hey Иван, saw your app. Want 3 demo reels for Acme?' "
                    "WHERE campaign_id = ? AND username = 'en_ru_leak'",
                    (en_camp_id,),
                )
                v2_conn.commit()
                if _audit_ru_language_campaigns(v2_conn).get("ok") is not True:
                    print("SELFTEST FAILED: valid EN template/recipient failed _audit_ru_language_campaigns", file=sys.stderr)
                    return 1

                # (l) enrichment_ledger daily spend exceeding cap MUST fail exit != 0
                v2_conn.execute(
                    """
                    INSERT INTO enrichment_ledger (day, provider, endpoint, lead_key, cost_usd, hit, created_at)
                    VALUES ('2026-11-22', 'treg', '/people.phone.find', 'whale.io', 5.75, 1, ?)
                    """,
                    (now,),
                )
                v2_conn.commit()
                with patch.dict(os.environ, {"LEADGEN_CONFIG_PATH": str(v2_cfg_path)}):
                    if _audit_enrichment_ledger_bounds(v2_conn).get("ok") is not False:
                        print("SELFTEST FAILED: _audit_enrichment_ledger_bounds missed over-cap spend", file=sys.stderr)
                        return 1
                rc_cap_bad, err_cap_bad = _run_v2_db_audit()
                if rc_cap_bad == 0 or "enrichment_ledger_bounds" not in err_cap_bad:
                    print(f"SELFTEST FAILED: _audit_local_db missed enrichment_ledger over-cap spend: rc={rc_cap_bad} err={err_cap_bad!r}", file=sys.stderr)
                    return 1

                v2_conn.execute("UPDATE enrichment_ledger SET cost_usd = 4.25 WHERE provider = 'treg'")
                v2_conn.commit()
                rc_v2_clean, err_v2_clean = _run_v2_db_audit()
                if rc_v2_clean != 0:
                    print(f"SELFTEST FAILED: clean v2 DB failed audit: rc={rc_v2_clean} err={err_v2_clean!r}", file=sys.stderr)
                    return 1
            finally:
                v2_conn.close()

            print("proactive_audit --self-test: OK (negative violations failed as expected, healed clean)")
            return 0
        finally:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Proactive reply-alert, ICP hygiene, session/SSOT & server leadgen readiness auditor"
    )
    parser.add_argument("--db", type=Path, help="Path to SQLite crm.db")
    parser.add_argument(
        "--heal",
        action="store_true",
        help="Heal false alerts, bot usernames, non-ICP junk, drift & session parity in DB",
    )
    parser.add_argument(
        "--enforce-vps-readiness",
        action="store_true",
        help="Enforce VPS leadgen server readiness checks (also active when CRMTG_VPS_RUNTIME=1)",
    )
    parser.add_argument(
        "--strict-static-proxy",
        action="store_true",
        help="Fail instead of warning when any connected Telegram account uses rotating NodeMaven proxy",
    )
    parser.add_argument(
        "--check-leadgen-starvation",
        action="store_true",
        help="Check standalone per-campaign B2B/OFM leadgen starvation (pending<15 or ingested_72h==0)",
    )
    parser.add_argument("--scraper-sessions-dir", type=Path, help="Override scraper_sessions directory")
    parser.add_argument("--leadgen-state-dir", type=Path, help="Override leadgen_state directory")
    parser.add_argument("--api-key", type=str, help="Bearer token for protected /api/health?deep=1 checks")
    parser.add_argument(
        "--since-hours",
        type=int,
        default=None,
        help="Restrict notification window in hours",
    )
    parser.add_argument("--health-url", type=str, help="Live /api/health?deep=1 URL")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Verify audit gates fail on injected DB/health violations and pass after healing",
    )
    args = parser.parse_args(argv)
    load_env_file()

    if args.self_test:
        return _run_self_test()

    rc = 0
    if args.db is not None:
        rc = max(
            rc,
            _audit_local_db(
                args.db,
                heal=args.heal,
                since_hours=args.since_hours,
                enforce_vps_readiness=args.enforce_vps_readiness,
                strict_static_proxy=args.strict_static_proxy,
                check_leadgen_starvation=args.check_leadgen_starvation,
                scraper_sessions_dir=args.scraper_sessions_dir,
                leadgen_state_dir=args.leadgen_state_dir,
            ),
        )
    if args.health_url:
        rc = max(
            rc,
            _audit_health_url(
                args.health_url,
                strict_static_proxy=args.strict_static_proxy,
                check_leadgen_starvation=args.check_leadgen_starvation,
                api_key=args.api_key,
            ),
        )
    if args.db is None and not args.health_url:
        default_db = ROOT / "data" / "crm.db"
        if default_db.exists():
            rc = _audit_local_db(
                default_db,
                heal=args.heal,
                since_hours=args.since_hours,
                enforce_vps_readiness=args.enforce_vps_readiness,
                strict_static_proxy=args.strict_static_proxy,
                check_leadgen_starvation=args.check_leadgen_starvation,
                scraper_sessions_dir=args.scraper_sessions_dir,
                leadgen_state_dir=args.leadgen_state_dir,
            )
        else:
            print("proactive_audit: OK (no local data/crm.db present)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
