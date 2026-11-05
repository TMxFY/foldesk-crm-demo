from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import sqlite3

from app.db import STATUSES, as_dict, utc_today
from app.outreach import _parse_account_ids, _sync_campaign_replies

STAGE_LABELS = {
    "new": "Лид",
    "talking": "Разговор",
    "waiting": "Ждём",
    "won": "Выигран",
    "lost": "Проигран",
}

HEALTH_KEYS = ("active", "offline", "unauthorized", "banned", "frozen")


def _parse_money_text(raw_text: str | None) -> int | None:
    if not raw_text:
        return None
    s = str(raw_text).strip()
    if not s:
        return None
    cleaned = re.sub(r"[^\d.,\-]", "", s)
    if not cleaned:
        return None
    # Handle thousands separators like "$2,400" vs decimal "2400.50"
    if "," in cleaned and "." not in cleaned:
        parts = cleaned.split(",")
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            cleaned = "".join(parts)
        else:
            cleaned = cleaned.replace(",", ".")
    elif "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    try:
        val = Decimal(cleaned)
        return max(0, int(val.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    except (InvalidOperation, ValueError):
        return None


def _load_stage_catalog(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pipeline_stages'"
    ).fetchone()
    if has_table:
        try:
            rows = conn.execute(
                "SELECT key, label FROM pipeline_stages ORDER BY position ASC, rowid ASC"
            ).fetchall()
            if rows:
                seen = set()
                out = []
                for r in rows:
                    k = str(r["key"] or "").strip()
                    lbl = str(r["label"] or STAGE_LABELS.get(k, k)).strip()
                    if k and k not in seen:
                        seen.add(k)
                        out.append((k, lbl))
                for std_k in STATUSES:
                    if std_k not in seen:
                        out.append((std_k, STAGE_LABELS.get(std_k, std_k)))
                return out
        except Exception:
            pass
    return [(k, STAGE_LABELS.get(k, k)) for k in STATUSES]


def _load_lead_amounts_from_properties(conn: sqlite3.Connection) -> dict[int, int]:
    amounts: dict[int, int] = {}
    try:
        rows = conn.execute(
            """
            SELECT v.lead_id, v.value_text, v.value_int
            FROM property_values v
            JOIN property_defs d ON d.id = v.property_id
            WHERE d.object_type = 'lead' AND d.type = 'amount' AND v.lead_id IS NOT NULL
            ORDER BY d.position ASC, v.id ASC
            """
        ).fetchall()
    except Exception:
        return amounts

    for row in rows:
        lead_id = int(row["lead_id"])
        if lead_id in amounts and amounts[lead_id] > 0:
            continue
        parsed = _parse_money_text(row["value_text"])
        if parsed is not None and parsed > 0:
            amounts[lead_id] = parsed
        elif row["value_int"] is not None:
            try:
                val_int = int(row["value_int"])
                amounts[lead_id] = max(0, int(round(val_int / 100)))
            except (TypeError, ValueError):
                pass
    return amounts


def _resolve_lead_amount(lead: dict, prop_amounts: dict[int, int]) -> int:
    raw_col = lead.get("amount")
    if raw_col is not None:
        try:
            col_val = int(raw_col)
            if col_val > 0:
                return col_val
        except (TypeError, ValueError):
            parsed_col = _parse_money_text(str(raw_col))
            if parsed_col is not None and parsed_col > 0:
                return parsed_col
    lid = lead.get("id")
    if lid is not None and int(lid) in prop_amounts:
        return int(prop_amounts[int(lid)])
    return 0


def _compute_funnel_analytics(conn: sqlite3.Connection) -> dict:
    stage_catalog = _load_stage_catalog(conn)
    prop_amounts = _load_lead_amounts_from_properties(conn)
    lead_rows = [as_dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY id").fetchall()]

    stage_counts: dict[str, int] = {k: 0 for k, _ in stage_catalog}
    stage_amounts: dict[str, int] = {k: 0 for k, _ in stage_catalog}
    stage_labels: dict[str, str] = {k: lbl for k, lbl in stage_catalog}

    for lead in lead_rows:
        st = str(lead.get("status") or "new").strip() or "new"
        if st not in stage_counts:
            stage_counts[st] = 0
            stage_amounts[st] = 0
            stage_labels[st] = STAGE_LABELS.get(st, st)
        amt = _resolve_lead_amount(lead, prop_amounts)
        stage_counts[st] += 1
        stage_amounts[st] += amt

    total_leads = len(lead_rows)
    won_leads = stage_counts.get("won", 0)
    lost_leads = stage_counts.get("lost", 0)
    active_leads = sum(
        cnt for st, cnt in stage_counts.items() if st not in ("won", "lost")
    )

    won_value = stage_amounts.get("won", 0)
    lost_value = stage_amounts.get("lost", 0)
    pipeline_value = sum(
        amt for st, amt in stage_amounts.items() if st not in ("won", "lost")
    )
    total_value = pipeline_value + won_value + lost_value

    win_rate_pct = round((won_leads / total_leads) * 100, 1) if total_leads > 0 else 0.0
    closed_total = won_leads + lost_leads
    closed_win_rate_pct = (
        round((won_leads / closed_total) * 100, 1) if closed_total > 0 else 0.0
    )

    stages_list = []
    by_stage = {}
    for st_key, st_label in stage_labels.items():
        cnt = stage_counts.get(st_key, 0)
        amt = stage_amounts.get(st_key, 0)
        share_pct = round((cnt / total_leads) * 100, 1) if total_leads > 0 else 0.0
        item = {
            "key": st_key,
            "status": st_key,
            "label": st_label,
            "count": cnt,
            "amount": amt,
            "share_pct": share_pct,
        }
        stages_list.append(item)
        by_stage[st_key] = item

    # Cumulative funnel conversion: new -> talking -> waiting -> won
    reached_new = total_leads
    reached_talking = (
        stage_counts.get("talking", 0)
        + stage_counts.get("waiting", 0)
        + stage_counts.get("won", 0)
    )
    reached_waiting = stage_counts.get("waiting", 0) + stage_counts.get("won", 0)
    reached_won = stage_counts.get("won", 0)

    new_to_talking_pct = (
        round((reached_talking / reached_new) * 100, 1) if reached_new > 0 else 0.0
    )
    talking_to_waiting_pct = (
        round((reached_waiting / reached_talking) * 100, 1)
        if reached_talking > 0
        else 0.0
    )
    waiting_to_won_pct = (
        round((reached_won / reached_waiting) * 100, 1)
        if reached_waiting > 0
        else 0.0
    )

    conversions = [
        {
            "from_stage": "new",
            "to_stage": "talking",
            "label": f"{stage_labels.get('new', 'Лид')} → {stage_labels.get('talking', 'Разговор')}",
            "from_count": reached_new,
            "to_count": reached_talking,
            "stage_from_count": stage_counts.get("new", 0),
            "stage_to_count": stage_counts.get("talking", 0),
            "rate_pct": new_to_talking_pct,
            "conversion_pct": new_to_talking_pct,
        },
        {
            "from_stage": "talking",
            "to_stage": "waiting",
            "label": f"{stage_labels.get('talking', 'Разговор')} → {stage_labels.get('waiting', 'Ждём')}",
            "from_count": reached_talking,
            "to_count": reached_waiting,
            "stage_from_count": stage_counts.get("talking", 0),
            "stage_to_count": stage_counts.get("waiting", 0),
            "rate_pct": talking_to_waiting_pct,
            "conversion_pct": talking_to_waiting_pct,
        },
        {
            "from_stage": "waiting",
            "to_stage": "won",
            "label": f"{stage_labels.get('waiting', 'Ждём')} → {stage_labels.get('won', 'Выигран')}",
            "from_count": reached_waiting,
            "to_count": reached_won,
            "stage_from_count": stage_counts.get("waiting", 0),
            "stage_to_count": stage_counts.get("won", 0),
            "rate_pct": waiting_to_won_pct,
            "conversion_pct": waiting_to_won_pct,
        },
    ]

    conversion_rates = {
        "new_to_talking_pct": new_to_talking_pct,
        "talking_to_waiting_pct": talking_to_waiting_pct,
        "waiting_to_won_pct": waiting_to_won_pct,
        "new_to_won_pct": win_rate_pct,
    }

    return {
        "total_leads": total_leads,
        "active_leads": active_leads,
        "won_leads": won_leads,
        "lost_leads": lost_leads,
        "pipeline_value": pipeline_value,
        "won_value": won_value,
        "won_revenue": won_value,
        "lost_value": lost_value,
        "total_value": total_value,
        "win_rate_pct": win_rate_pct,
        "closed_win_rate_pct": closed_win_rate_pct,
        "stages": stages_list,
        "by_stage": by_stage,
        "conversions": conversions,
        "conversion_rates": conversion_rates,
    }


def _compute_outreach_analytics(conn: sqlite3.Connection) -> dict:
    camp_rows = [
        as_dict(r)
        for r in conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
    ]
    for camp in camp_rows:
        cid = int(camp["id"])
        acc_ids = _parse_account_ids(camp.get("account_id"), camp.get("account_ids_json"))
        try:
            if acc_ids:
                for aid in acc_ids:
                    _sync_campaign_replies(conn, cid, aid)
            else:
                _sync_campaign_replies(conn, cid, None)
        except Exception:
            pass

    rec_rows = [
        as_dict(r)
        for r in conn.execute(
            """
            SELECT cr.*, l.status AS lead_status
            FROM campaign_recipients cr
            LEFT JOIN leads l ON l.id = cr.lead_id
            ORDER BY cr.id
            """
        ).fetchall()
    ]

    by_camp: dict[int, list[dict]] = {int(c["id"]): [] for c in camp_rows}
    for rec in rec_rows:
        cid = int(rec["campaign_id"])
        by_camp.setdefault(cid, []).append(rec)

    campaigns_breakdown = []
    total_recipients = 0
    total_pending = 0
    total_in_progress = 0
    total_completed = 0
    total_replied = 0
    total_failed = 0
    total_sent = 0
    total_converted_talking = 0
    total_converted_won = 0

    for camp in camp_rows:
        cid = int(camp["id"])
        recs = by_camp.get(cid, [])
        c_total = len(recs)
        c_pending = sum(1 for r in recs if r.get("status") == "pending")
        c_in_prog = sum(1 for r in recs if r.get("status") == "in_progress")
        c_comp = sum(1 for r in recs if r.get("status") == "completed")
        c_rep = sum(1 for r in recs if r.get("status") == "replied")
        c_fail = sum(1 for r in recs if r.get("status") == "failed")
        c_sent = c_in_prog + c_comp + c_rep
        c_reply_rate = round((c_rep / c_sent) * 100, 1) if c_sent > 0 else 0.0
        c_talking = sum(
            1
            for r in recs
            if r.get("lead_status") in ("talking", "waiting", "won")
            or r.get("status") == "replied"
        )
        c_won = sum(1 for r in recs if r.get("lead_status") == "won")

        total_recipients += c_total
        total_pending += c_pending
        total_in_progress += c_in_prog
        total_completed += c_comp
        total_replied += c_rep
        total_failed += c_fail
        total_sent += c_sent
        total_converted_talking += c_talking
        total_converted_won += c_won

        campaigns_breakdown.append(
            {
                "id": cid,
                "name": camp.get("name") or f"Кампания #{cid}",
                "status": camp.get("status") or "draft",
                "daily_limit": int(camp.get("daily_limit") or 20),
                "total_recipients": c_total,
                "sent": c_sent,
                "sent_recipients": c_sent,
                "pending": c_pending,
                "in_progress": c_in_prog,
                "completed": c_comp,
                "replied": c_rep,
                "failed": c_fail,
                "reply_rate_pct": c_reply_rate,
                "converted_to_talking": c_talking,
                "converted_to_won": c_won,
            }
        )

    overall_reply_rate = (
        round((total_replied / total_sent) * 100, 1) if total_sent > 0 else 0.0
    )
    running_campaigns = sum(1 for c in camp_rows if c.get("status") == "running")

    return {
        "total_campaigns": len(camp_rows),
        "running_campaigns": running_campaigns,
        "total_recipients": total_recipients,
        "sent": total_sent,
        "sent_recipients": total_sent,
        "pending": total_pending,
        "pending_recipients": total_pending,
        "in_progress": total_in_progress,
        "in_progress_recipients": total_in_progress,
        "completed": total_completed,
        "completed_recipients": total_completed,
        "replied": total_replied,
        "replied_recipients": total_replied,
        "failed": total_failed,
        "failed_recipients": total_failed,
        "reply_rate_pct": overall_reply_rate,
        "converted_to_talking": total_converted_talking,
        "converted_to_won": total_converted_won,
        "campaigns": campaigns_breakdown,
    }


def _compute_accounts_analytics(conn: sqlite3.Connection) -> dict:
    today = utc_today()
    acc_rows = [
        as_dict(r)
        for r in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    ]

    by_health: dict[str, int] = {k: 0 for k in HEALTH_KEYS}
    by_health["unknown"] = 0

    account_items = []
    total_sent_today = 0
    total_daily_limit = 0
    connected_daily_limit = 0
    connected_sent_today = 0
    connected_count = 0
    total_dialogs = 0
    total_replies = 0

    for row in acc_rows:
        aid = int(row["id"])
        status = str(row.get("status") or "error")
        raw_health = row.get("health")
        health = str(raw_health).strip() if raw_health else None
        if health in by_health:
            by_health[health] += 1
        else:
            by_health["unknown"] += 1

        daily_limit = int(row.get("daily_limit") or 20)
        sent_today = (
            int(row.get("sent_today") or 0) if row.get("sent_date") == today else 0
        )
        remaining_today = max(0, daily_limit - sent_today)
        utilization_pct = (
            round((sent_today / daily_limit) * 100, 1) if daily_limit > 0 else 0.0
        )

        # Dialogs & replies for this account
        dlg_rows = [
            as_dict(d)
            for d in conn.execute(
                """
                SELECT d.id, d.peer_id, d.username, d.phone, d.lead_id, d.last_from_peer,
                       EXISTS(
                           SELECT 1 FROM messages m
                           WHERE m.dialog_id = d.id AND m.direction = 'in'
                       ) AS has_inbound
                FROM dialogs d
                WHERE d.account_id = ?
                """,
                (aid,),
            ).fetchall()
        ]
        dialogs_count = len(dlg_rows)
        replied_dialogs = [
            d
            for d in dlg_rows
            if int(d.get("has_inbound") or 0) == 1
            or int(d.get("last_from_peer") or 0) == 1
        ]
        replied_dialogs_count = len(replied_dialogs)

        # Campaign recipients assigned to this account (or via campaign.account_id)
        camp_recs = [
            as_dict(cr)
            for cr in conn.execute(
                """
                SELECT cr.id, cr.status, cr.telegram_user_id, cr.username, cr.phone, cr.lead_id
                FROM campaign_recipients cr
                JOIN campaigns c ON c.id = cr.campaign_id
                WHERE COALESCE(cr.assigned_account_id, c.account_id) = ?
                """,
                (aid,),
            ).fetchall()
        ]
        camp_sent_count = sum(
            1
            for cr in camp_recs
            if cr.get("status") in ("in_progress", "completed", "replied")
        )
        camp_replied_recs = [cr for cr in camp_recs if cr.get("status") == "replied"]
        camp_replied_count = len(camp_replied_recs)

        # Deduplicate replies across dialogs and campaign recipients
        replied_dialog_peer_ids = {
            int(d["peer_id"]) for d in replied_dialogs if d.get("peer_id") is not None
        }
        replied_dialog_lead_ids = {
            int(d["lead_id"]) for d in replied_dialogs if d.get("lead_id") is not None
        }
        replied_dialog_usernames = {
            str(d["username"]).strip().lstrip("@").lower()
            for d in replied_dialogs
            if d.get("username")
        }
        extra_camp_replies = 0
        for cr in camp_replied_recs:
            matched = False
            if (
                cr.get("telegram_user_id") is not None
                and int(cr["telegram_user_id"]) in replied_dialog_peer_ids
            ):
                matched = True
            elif (
                cr.get("lead_id") is not None
                and int(cr["lead_id"]) in replied_dialog_lead_ids
            ):
                matched = True
            elif (
                cr.get("username")
                and str(cr["username"]).strip().lstrip("@").lower()
                in replied_dialog_usernames
            ):
                matched = True
            if not matched:
                extra_camp_replies += 1

        replies_count = replied_dialogs_count + extra_camp_replies
        if camp_sent_count > 0:
            reply_rate_pct = round((camp_replied_count / camp_sent_count) * 100, 1)
        elif dialogs_count > 0:
            reply_rate_pct = round((replied_dialogs_count / dialogs_count) * 100, 1)
        else:
            reply_rate_pct = 0.0

        provider = str(row.get("provider") or "telegram").strip().lower()
        if provider not in ("telegram", "whatsapp"):
            provider = "telegram"
        display_name = (
            row.get("telegram_name")
            or row.get("telegram_username")
            or row.get("phone")
            or f"Аккаунт #{aid}"
        )

        total_sent_today += sent_today
        total_daily_limit += daily_limit
        if status == "connected":
            connected_count += 1
            connected_daily_limit += daily_limit
            connected_sent_today += sent_today
        total_dialogs += dialogs_count
        total_replies += replies_count

        account_items.append(
            {
                "id": aid,
                "phone": row.get("phone"),
                "name": display_name,
                "telegram_name": row.get("telegram_name"),
                "telegram_username": row.get("telegram_username"),
                "provider": provider,
                "status": status,
                "health": health or ("active" if status == "connected" else "offline"),
                "raw_health": health,
                "sent_today": sent_today,
                "daily_limit": daily_limit,
                "remaining_today": remaining_today,
                "utilization_pct": utilization_pct,
                "dialogs_count": dialogs_count,
                "replies_count": replies_count,
                "replied_dialogs_count": replied_dialogs_count,
                "campaign_sent_count": camp_sent_count,
                "campaign_replied_count": camp_replied_count,
                "reply_rate_pct": reply_rate_pct,
            }
        )

    effective_limit = connected_daily_limit if connected_count > 0 else total_daily_limit
    effective_sent = connected_sent_today if connected_count > 0 else total_sent_today
    pool_utilization_pct = (
        round((effective_sent / effective_limit) * 100, 1)
        if effective_limit > 0
        else 0.0
    )

    return {
        "total_accounts": len(acc_rows),
        "connected_accounts": connected_count,
        "sent_today": total_sent_today,
        "daily_limit": effective_limit,
        "total_daily_limit": total_daily_limit,
        "remaining_today": max(0, effective_limit - effective_sent),
        "utilization_pct": pool_utilization_pct,
        "pool_utilization_pct": pool_utilization_pct,
        "total_dialogs": total_dialogs,
        "total_replies": total_replies,
        "by_health": by_health,
        "health_summary": by_health,
        "items": account_items,
        "accounts": account_items,
    }


def get_analytics_summary(conn: sqlite3.Connection) -> dict:
    funnel = _compute_funnel_analytics(conn)
    outreach = _compute_outreach_analytics(conn)
    accounts = _compute_accounts_analytics(conn)
    return {
        "funnel": funnel,
        "outreach": outreach,
        "accounts": accounts,
        "pipeline_value": funnel["pipeline_value"],
        "won_value": funnel["won_value"],
        "won_revenue": funnel["won_value"],
        "lost_value": funnel["lost_value"],
        "total_value": funnel["total_value"],
        "win_rate_pct": funnel["win_rate_pct"],
        "reply_rate_pct": outreach["reply_rate_pct"],
    }
