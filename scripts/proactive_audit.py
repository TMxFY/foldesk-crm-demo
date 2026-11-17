#!/usr/bin/env python3
"""Proactive DB & runtime invariant auditor for crmtg reply-alert eligibility.

Exits with code 1 whenever `bot_reply_notifications` or `leads` contain:
  1. Colleague bot self-echo (`@softugccollegabot` / `bot_token` user_id)
  2. Telegram bots (`is_bot=1` or bot usernames `*_bot`)
  3. Saved Messages / operator chat ID / connected CRM accounts / internal team
  4. Already closed clients (`leads.status IN ('won', 'lost')` / `Signed` / `Cancelled`)
  5. Non-lead personal DMs (`lead_id IS NULL` with 0 `leads` / `campaign_recipients` match)

Usage:
  python scripts/proactive_audit.py --db data/crm.db
  python scripts/proactive_audit.py --db data/crm.db --heal
  python scripts/proactive_audit.py --health-url http://127.0.0.1:18084/api/health?deep=1
"""

from __future__ import annotations

import argparse
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


def _audit_local_db(db_path: Path, *, heal: bool, since_hours: int | None) -> int:
    if not db_path.exists():
        print(f"proactive_audit: ERROR db not found: {db_path}", file=sys.stderr)
        return 1

    if heal:
        conn = connect(db_path)
        try:
            healed = heal_reply_notifications(conn)
            print(f"proactive_audit: healed {json.dumps(healed, ensure_ascii=False)}")
            report = audit_reply_notifications(conn, since_hours=since_hours)
        finally:
            conn.close()
    else:
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            report = audit_reply_notifications(conn, since_hours=since_hours)
        finally:
            conn.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        print(
            f"PROACTIVE AUDIT FAILED: {report['false_notification_count']} false notification(s), "
            f"{report['bot_username_lead_count']} bot-username lead(s)",
            file=sys.stderr,
        )
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
    if not data.get("db_ok") or data.get("reply_alert_guard_ok") is not True or int(data.get("false_reply_alerts_24h", 1)) != 0:
        print("PROACTIVE HEALTH AUDIT FAILED", file=sys.stderr)
        return 1
    print("proactive_audit (health): OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Proactive reply-alert & CRM DB auditor")
    parser.add_argument("--db", type=Path, help="Path to SQLite crm.db")
    parser.add_argument("--heal", action="store_true", help="Heal false alerts & bot usernames in DB")
    parser.add_argument("--since-hours", type=int, default=None, help="Restrict notification window in hours")
    parser.add_argument("--health-url", type=str, help="Live /api/health?deep=1 URL")
    args = parser.parse_args(argv)
    load_env_file()

    rc = 0
    if args.db is not None:
        rc = max(rc, _audit_local_db(args.db, heal=args.heal, since_hours=args.since_hours))
    if args.health_url:
        rc = max(rc, _audit_health_url(args.health_url))
    if args.db is None and not args.health_url:
        default_db = ROOT / "data" / "crm.db"
        if default_db.exists():
            rc = _audit_local_db(default_db, heal=args.heal, since_hours=args.since_hours)
        else:
            print("proactive_audit: OK (no local data/crm.db present)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
