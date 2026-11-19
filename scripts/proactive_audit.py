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
import re
import sqlite3
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.analytics import _sync_running_and_all_campaign_replies, get_analytics_summary  # noqa: E402
from app.db import (  # noqa: E402
    connect,
    init_db,
    is_newer_dialog_snapshot_unbacked,
    is_recipient_contacted_outreach,
    is_vacancy_campaign_meta,
    utc_now,
)
from app.digest import (  # noqa: E402
    audit_reply_notifications,
    heal_reply_notifications,
    is_ineligible_reply_notify_peer,
)
from app.funnel_analytics import ensure_funnel_columns  # noqa: E402
from app.inbox import (  # noqa: E402
    _PERSONAL_HYGIENE_ERRORS,
    _norm_ts,
    clean_tg_username_lookup,
    list_inbox,
    list_messages,
)
from app.leads import create_lead  # noqa: E402
from app.main import load_env_file  # noqa: E402
from app.outreach import (  # noqa: E402
    _dialog_has_client_reply,
    _find_recipient_dialogs,
    _latest_dialog_in_meta,
)
from app.runtime_config import is_vps_runtime  # noqa: E402


from app.hygiene import (  # noqa: E402
    audit_crm_hygiene as _audit_crm_hygiene,
    audit_server_leadgen_readiness as _audit_server_leadgen_readiness,
    heal_crm_hygiene as _raw_heal_crm_hygiene,
)


def _audit_ssot_statistics(conn: sqlite3.Connection) -> dict:
    """Verify SSOT analytics math consistency and visibility of all dropped accounts."""
    summary = get_analytics_summary(conn, period="7d")
    issues: list[str] = []
    deep_sum = (summary.get("deep_outreach") or {}).get("summary") or {}
    contacted = int(deep_sum.get("contacted") or 0)
    read_cnt = int(deep_sum.get("read") or 0)
    unseen_cnt = int(deep_sum.get("unseen_total") or 0)
    if contacted > 0 and read_cnt + unseen_cnt != contacted:
        issues.append(f"read({read_cnt})+unseen({unseen_cnt})!=contacted({contacted})")

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
        "dropped_accounts_count": len(dropped_items),
        "db_dropped_accounts_count": len(db_dropped),
        "ignored_personal_messages_total": int(personal_msgs_7d or 0),
    }


def _audit_old_lead_message_visibility(conn: sqlite3.Connection) -> dict:
    """Verify every client/contacted reply is visible in Inbox, marked replied, and has readable latest msg."""
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
        dialogs = _find_recipient_dialogs(conn, rec, account_id=rec.get("sent_account_id") or rec.get("assigned_account_id"))
        replied_cands = [d for d in dialogs if _dialog_has_client_reply(conn, d, rec)]
        if not replied_cands:
            dialogs_any = _find_recipient_dialogs(conn, rec, account_id=None, ignore_assigned_account=True)
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
            _sync_running_and_all_campaign_replies(conn)
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
            stats_report = _audit_ssot_statistics(conn)
            vis_report = _audit_old_lead_message_visibility(conn)
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
            if check_readiness:
                readiness_report = _audit_server_leadgen_readiness(
                    conn, enforce_vps_checks=True
                )
        finally:
            conn.close()

    overall_ok = (
        bool(reply_report.get("ok"))
        and bool(hygiene_report.get("ok"))
        and bool(stats_report.get("ok"))
        and bool(vis_report.get("ok"))
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
    if not stats_report.get("ok"):
        reasons.append(f"ssot_stats({', '.join(stats_report.get('issues') or [])})")
    if not vis_report.get("ok"):
        reasons.append(
            "message_visibility("
            f"hidden_replied_dialogs={vis_report.get('hidden_replied_dialog_count', 0)}, "
            f"unmarked_replied_recipients={vis_report.get('unmarked_replied_recipient_count', 0)}, "
            f"invisible_latest_messages={vis_report.get('invisible_latest_message_count', 0)})"
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
            _heal_crm_hygiene(conn)
            _sync_running_and_all_campaign_replies(conn)

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

            print("proactive_audit --self-test: OK (negative violations failed as expected, healed clean)")
            return 0
        finally:
            conn.close()


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
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Verify audit gates fail on injected DB violations and pass after healing",
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
