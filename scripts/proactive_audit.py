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
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402
from app.digest import audit_reply_notifications, heal_reply_notifications  # noqa: E402
from app.main import load_env_file  # noqa: E402
from app.runtime_config import is_vps_runtime  # noqa: E402


from app.hygiene import (  # noqa: E402
    audit_crm_hygiene as _audit_crm_hygiene,
    audit_server_leadgen_readiness as _audit_server_leadgen_readiness,
    heal_crm_hygiene as _raw_heal_crm_hygiene,
)


def _heal_crm_hygiene(conn) -> dict:
    return _raw_heal_crm_hygiene(conn, clear_stale_stage_errors=True)


def _audit_local_db(
    db_path: Path,
    *,
    heal: bool,
    since_hours: int | None,
    enforce_vps_readiness: bool = False,
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
            healed_hygiene = _heal_crm_hygiene(conn)
            print(
                "proactive_audit: healed "
                + json.dumps(
                    {
                        "reply_notifications": healed_replies,
                        "crm_hygiene": healed_hygiene,
                    },
                    ensure_ascii=False,
                )
            )
            reply_report = audit_reply_notifications(conn, since_hours=since_hours)
            hygiene_report = _audit_crm_hygiene(conn)
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
            if check_readiness:
                readiness_report = _audit_server_leadgen_readiness(
                    conn, enforce_vps_checks=True
                )
        finally:
            conn.close()

    overall_ok = bool(reply_report.get("ok")) and bool(hygiene_report.get("ok"))
    if readiness_report is not None and not bool(readiness_report.get("ok")):
        overall_ok = False

    combined = dict(reply_report)
    combined["ok"] = overall_ok
    combined["icp_hygiene_ok"] = bool(hygiene_report.get("ok"))
    combined["crm_hygiene"] = hygiene_report
    if readiness_report is not None:
        combined["leadgen_ready"] = bool(readiness_report.get("ok"))
        combined["leadgen_readiness"] = readiness_report

    print(json.dumps(combined, ensure_ascii=False, indent=2))

    reasons: list[str] = []
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


def _audit_health_url(url: str) -> int:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 crmtg-proactive-audit/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    print(json.dumps(data, ensure_ascii=False, indent=2))

    failed_checks: list[str] = []
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Proactive reply-alert, ICP hygiene & server leadgen readiness auditor"
    )
    parser.add_argument("--db", type=Path, help="Path to SQLite crm.db")
    parser.add_argument(
        "--heal",
        action="store_true",
        help="Heal false alerts, bot usernames & non-ICP junk leads/recipients in DB",
    )
    parser.add_argument(
        "--enforce-vps-readiness",
        action="store_true",
        help="Enforce VPS leadgen server readiness checks (also active when CRMTG_VPS_RUNTIME=1)",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=None,
        help="Restrict notification window in hours",
    )
    parser.add_argument("--health-url", type=str, help="Live /api/health?deep=1 URL")
    args = parser.parse_args(argv)
    load_env_file()

    rc = 0
    if args.db is not None:
        rc = max(
            rc,
            _audit_local_db(
                args.db,
                heal=args.heal,
                since_hours=args.since_hours,
                enforce_vps_readiness=args.enforce_vps_readiness,
            ),
        )
    if args.health_url:
        rc = max(rc, _audit_health_url(args.health_url))
    if args.db is None and not args.health_url:
        default_db = ROOT / "data" / "crm.db"
        if default_db.exists():
            rc = _audit_local_db(
                default_db,
                heal=args.heal,
                since_hours=args.since_hours,
                enforce_vps_readiness=args.enforce_vps_readiness,
            )
        else:
            print("proactive_audit: OK (no local data/crm.db present)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
