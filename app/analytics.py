from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
import sqlite3

from app.db import (
    AIRTABLE_STAGE_KEYS,
    KEY_TO_AIRTABLE_STATUS,
    STATUSES,
    as_dict,
    clean_tg_username_lookup,
    is_newer_dialog_snapshot_unbacked,
    is_recipient_contacted_outreach,
    is_vacancy_campaign_meta,
    normalize_status,
    utc_today,
)
from app.digest import is_closed_lead_status, is_ineligible_reply_notify_peer
from app.outreach import _parse_account_ids, _sync_campaign_replies

STAGE_LABELS = {
    "new": "Лид",
    "talking": "Разговор",
    "call_scheduled": "Созвон назначен",
    "waiting": "Ждём",
    "on_hold": "На паузе",
    "won": "Выигран",
    "lost": "Проигран",
}

STAGE_COLORS = {
    "new": "#94a3b8",
    "talking": "#38bdf8",
    "call_scheduled": "#a855f7",
    "waiting": "#fbbf24",
    "on_hold": "#64748b",
    "won": "#34d399",
    "lost": "#f87171",
}

HEALTH_KEYS = ("active", "offline", "unauthorized", "banned", "frozen")

VALID_PERIODS = ("7d", "30d", "90d", "all")

HEATMAP_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
HEATMAP_SLOTS = ["00–04", "04–08", "08–12", "12–16", "16–20", "20–24"]


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


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if len(s) >= 11 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except ValueError:
        return None


def _norm_ts(value: str | None) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    dt = _parse_iso_dt(s)
    if dt is not None:
        return (
            dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
            if dt.microsecond
            else dt.strftime("%Y-%m-%dT%H:%M:%S")
        )
    return s.replace(" ", "T")


def _load_stage_catalog(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    db_labels: dict[str, str] = {}
    extra_stages: list[tuple[str, str]] = []
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pipeline_stages'"
    ).fetchone()
    if has_table:
        try:
            rows = conn.execute(
                "SELECT key, label FROM pipeline_stages ORDER BY position ASC, rowid ASC"
            ).fetchall()
            for r in rows:
                k = str(r["key"] or "").strip()
                lbl = str(r["label"] or STAGE_LABELS.get(k, k)).strip()
                if k:
                    db_labels[k] = lbl
                    if k not in AIRTABLE_STAGE_KEYS and k not in STATUSES:
                        extra_stages.append((k, lbl))
        except Exception:
            pass

    out: list[tuple[str, str]] = []
    seen = set()
    for std_k in AIRTABLE_STAGE_KEYS:
        lbl = db_labels.get(std_k) or STAGE_LABELS.get(std_k, std_k)
        out.append((std_k, lbl))
        seen.add(std_k)
    for k, lbl in extra_stages:
        if k not in seen:
            seen.add(k)
            out.append((k, lbl))
    return out


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


def _resolve_lead_monthly(lead: dict) -> int:
    raw_val = lead.get("monthly_value")
    if raw_val is None:
        return 0
    try:
        val = int(raw_val)
        return max(0, val)
    except (TypeError, ValueError):
        parsed = _parse_money_text(str(raw_val))
        return max(0, parsed) if parsed is not None else 0


def _compute_funnel_analytics(conn: sqlite3.Connection) -> dict:
    stage_catalog = _load_stage_catalog(conn)
    prop_amounts = _load_lead_amounts_from_properties(conn)
    lead_rows = [as_dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY id").fetchall()]

    stage_counts: dict[str, int] = {k: 0 for k, _ in stage_catalog}
    stage_amounts: dict[str, int] = {k: 0 for k, _ in stage_catalog}
    stage_monthly: dict[str, int] = {k: 0 for k, _ in stage_catalog}
    stage_labels: dict[str, str] = {k: lbl for k, lbl in stage_catalog}

    for lead in lead_rows:
        st = normalize_status(lead.get("status"), conn) or "new"
        if st not in stage_counts:
            stage_counts[st] = 0
            stage_amounts[st] = 0
            stage_monthly[st] = 0
            stage_labels[st] = STAGE_LABELS.get(st, st)
        amt = _resolve_lead_amount(lead, prop_amounts)
        mrr = _resolve_lead_monthly(lead)
        stage_counts[st] += 1
        stage_amounts[st] += amt
        stage_monthly[st] += mrr

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

    active_monthly_value = sum(
        m for st, m in stage_monthly.items() if st not in ("won", "lost")
    )
    won_monthly_value = stage_monthly.get("won", 0)
    monthly_value = active_monthly_value + won_monthly_value

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
        mrr = stage_monthly.get(st_key, 0)
        share_pct = round((cnt / total_leads) * 100, 1) if total_leads > 0 else 0.0
        item = {
            "key": st_key,
            "status": st_key,
            "label": st_label,
            "airtable_label": KEY_TO_AIRTABLE_STATUS.get(st_key, st_label),
            "color": STAGE_COLORS.get(st_key, "#6366f1"),
            "count": cnt,
            "amount": amt,
            "monthly_value": mrr,
            "share_pct": share_pct,
        }
        stages_list.append(item)
        by_stage[st_key] = item

    # Cumulative funnel conversion: new -> talking (incl. call_scheduled) -> waiting (incl. on_hold) -> won
    reached_new = total_leads
    reached_talking = (
        stage_counts.get("talking", 0)
        + stage_counts.get("call_scheduled", 0)
        + stage_counts.get("waiting", 0)
        + stage_counts.get("on_hold", 0)
        + stage_counts.get("won", 0)
    )
    reached_call_scheduled = (
        stage_counts.get("call_scheduled", 0)
        + stage_counts.get("waiting", 0)
        + stage_counts.get("on_hold", 0)
        + stage_counts.get("won", 0)
    )
    reached_waiting = (
        stage_counts.get("waiting", 0)
        + stage_counts.get("on_hold", 0)
        + stage_counts.get("won", 0)
    )
    reached_won = stage_counts.get("won", 0)

    new_to_talking_pct = (
        round((reached_talking / reached_new) * 100, 1) if reached_new > 0 else 0.0
    )
    talking_to_call_scheduled_pct = (
        round((reached_call_scheduled / reached_talking) * 100, 1)
        if reached_talking > 0
        else 0.0
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
        "talking_to_call_scheduled_pct": talking_to_call_scheduled_pct,
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
        "monthly_value": monthly_value,
        "active_monthly_value": active_monthly_value,
        "won_monthly_value": won_monthly_value,
        "win_rate_pct": win_rate_pct,
        "closed_win_rate_pct": closed_win_rate_pct,
        "stages": stages_list,
        "by_stage": by_stage,
        "conversions": conversions,
        "conversion_rates": conversion_rates,
    }


_HYGIENE_V11_ERRORS = frozenset(
    {
        "uncontacted_personal_chat_hygiene_v11",
        "pre_softugc_personal_dm_hygiene_v11",
    }
)
_REFUSAL_INTENT_MARKERS = (
    "not interested",
    "no thanks",
    "no, thanks",
    "don't need",
    "dont need",
    "unsubscribe",
    "не интересно",
    "неинтересно",
    "не актуально",
    "неактуально",
    "не надо",
    "нет бюджета",
    "спасибо, нет",
    "нет, спасибо",
)
_REFUSAL_INTENT_WORDS_RE = re.compile(r"\b(?:stop|pass)\b", re.IGNORECASE)


def _norm_username(value: str | None) -> str:
    return clean_tg_username_lookup(value)


def _infer_reply_intent(reply_text: str | None) -> str:
    txt = str(reply_text or "").strip()
    if not txt:
        return "neutral_or_question"
    low = txt.lower()
    if any(m in low for m in _REFUSAL_INTENT_MARKERS) or _REFUSAL_INTENT_WORDS_RE.search(low):
        return "refusal_not_interested"
    from app.funnel_analytics import _has_positive_reply_signal

    if _has_positive_reply_signal(txt):
        return "positive_interested"
    return "neutral_or_question"


def _lead_preference_key(lead: dict) -> tuple[int, int]:
    norm_st = str(lead.get("_norm_status") or "").strip().lower()
    is_closed = bool(lead.get("_is_closed"))
    if not is_closed and norm_st not in ("won", "lost"):
        rank = 3
    elif norm_st == "won" or is_closed_lead_status(
        norm_st, lead.get("airtable_status"), won_only=True
    ):
        rank = 2
    else:
        rank = 1
    return (rank, int(lead.get("id") or 0))


def _prefer_lead(existing: dict | None, cand: dict) -> dict:
    if existing is None:
        return cand
    return cand if _lead_preference_key(cand) > _lead_preference_key(existing) else existing


def _resolve_dialog_lead(
    dlg: dict | None, ctx: dict, *, fallback_lead: dict | None = None
) -> dict | None:
    if dlg is None:
        return fallback_lead
    dlid = int(dlg["lead_id"]) if dlg.get("lead_id") is not None else None
    peer_id = int(dlg["peer_id"]) if dlg.get("peer_id") is not None else None
    uname = _norm_username(dlg.get("username"))
    ph15 = _norm_phone_15(dlg.get("phone"))
    best: dict | None = None
    for cand in (
        ctx["leads_by_id"].get(dlid) if dlid is not None else None,
        ctx.get("leads_by_peer", {}).get(peer_id) if peer_id is not None else None,
        ctx.get("leads_by_user", {}).get(uname) if uname else None,
        ctx.get("leads_by_phone", {}).get(ph15) if ph15 else None,
    ):
        if cand is not None:
            best = _prefer_lead(best, cand)
    return best if best is not None else fallback_lead


def _resolve_dialog_reply_text(
    dlg: dict | None,
    msum: dict | None,
    extra: dict | None = None,
    fallback: str = "",
) -> str:
    ex = extra if isinstance(extra, dict) else {}
    in_txt = str((msum or {}).get("last_in_text") or "").strip()
    if dlg is not None and int(dlg.get("last_from_peer") or 0) == 1:
        prev_txt = str(dlg.get("last_message_preview") or "").strip()
        dlg_ts = _norm_ts(dlg.get("last_message_at"))
        max_in_ts = _norm_ts((msum or {}).get("max_in_ts"))
        if prev_txt and (not in_txt or (dlg_ts and (not max_in_ts or dlg_ts > max_in_ts))):
            return prev_txt
        if in_txt:
            return in_txt
        if prev_txt:
            return prev_txt
    elif in_txt:
        return in_txt
    return str(ex.get("all_in_texts") or ex.get("first_in_text") or fallback or "").strip()


def _norm_phone_15(value: str | None) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-15:] if len(digits) >= 7 else ""


def _parse_extra_json(raw: object) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _account_display_handle(acc_row: dict | None, aid: int | None = None) -> str:
    if not acc_row:
        return f"#{aid}" if aid is not None else "unknown"
    uname = str(acc_row.get("telegram_username") or "").strip().lstrip("@")
    if uname:
        return f"@{uname}"
    tname = str(acc_row.get("telegram_name") or "").strip()
    if tname:
        return tname
    phone = str(acc_row.get("phone") or "").strip()
    if phone:
        return phone
    return f"#{acc_row.get('id') or aid or '?'}"


def _sync_running_and_all_campaign_replies(conn: sqlite3.Connection) -> None:
    try:
        camp_rows = [
            as_dict(r)
            for r in conn.execute(
                "SELECT id, name, account_id, account_ids_json, leadgen_sources_json FROM campaigns ORDER BY id DESC"
            ).fetchall()
        ]
    except Exception:
        return
    for camp in camp_rows:
        if is_vacancy_campaign_meta(camp.get("name"), camp.get("leadgen_sources_json")):
            continue
        cid = int(camp["id"])
        acc_ids = _parse_account_ids(camp.get("account_id"), camp.get("account_ids_json"))
        try:
            _sync_campaign_replies(conn, cid, acc_ids[0] if len(acc_ids) == 1 else None)
        except Exception:
            pass


def _load_ssot_crm_context(conn: sqlite3.Connection) -> dict:
    acc_rows = [as_dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()]
    accounts_by_id: dict[int, dict] = {}
    for ar in acc_rows:
        aid = int(ar["id"])
        st = str(ar.get("status") or "error").strip()
        health = str(ar.get("health") or ("active" if st == "connected" else "offline")).strip()
        is_dropped = not (st == "connected" and health == "active")
        accounts_by_id[aid] = {
            **ar,
            "status": st,
            "health": health,
            "is_dropped": is_dropped,
            "handle": _account_display_handle(ar, aid),
        }

    lead_rows = [as_dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY id").fetchall()]
    leads_by_id: dict[int, dict] = {}
    leads_by_peer: dict[int, dict] = {}
    leads_by_user: dict[str, dict] = {}
    leads_by_phone: dict[str, dict] = {}
    for lr in lead_rows:
        lid = int(lr["id"])
        norm_st = normalize_status(lr.get("status"), conn) or "new"
        closed = is_closed_lead_status(norm_st, lr.get("airtable_status"))
        enriched = {**lr, "_norm_status": norm_st, "_is_closed": closed}
        leads_by_id[lid] = enriched
        if lr.get("telegram_user_id") is not None:
            pid_key = int(lr["telegram_user_id"])
            leads_by_peer[pid_key] = _prefer_lead(leads_by_peer.get(pid_key), enriched)
        lu = _norm_username(lr.get("username"))
        if lu:
            leads_by_user[lu] = _prefer_lead(leads_by_user.get(lu), enriched)
        lph = _norm_phone_15(lr.get("phone"))
        if lph:
            leads_by_phone[lph] = _prefer_lead(leads_by_phone.get(lph), enriched)

    contacted_lead_ids: set[int] = set()
    contacted_peer_ids: set[int] = set()
    contacted_usernames: set[str] = set()
    contacted_phones: set[str] = set()
    rec_rows = [
        as_dict(r)
        for r in conn.execute(
            """
            SELECT cr.*, c.status AS campaign_status, c.name AS campaign_name,
                   c.leadgen_sources_json AS campaign_sources_json
            FROM campaign_recipients cr
            LEFT JOIN campaigns c ON c.id = cr.campaign_id
            ORDER BY cr.id
            """
        ).fetchall()
    ]
    rec_by_lead_id: dict[int, dict] = {}
    rec_by_peer: dict[int, dict] = {}
    rec_by_user: dict[str, dict] = {}
    rec_by_phone: dict[str, dict] = {}

    for cr in rec_rows:
        is_vacancy = is_vacancy_campaign_meta(
            cr.get("campaign_name"), cr.get("campaign_sources_json")
        )
        err_txt = str(cr.get("error_text") or "").strip()
        is_hygiene_v11 = err_txt in _HYGIENE_V11_ERRORS
        st = str(cr.get("status") or "").lower()
        step = int(cr.get("current_step") or 0)
        is_contacted = is_recipient_contacted_outreach(
            st, step, cr.get("last_sent_at"), err_txt
        )
        extra = _parse_extra_json(cr.get("extra_json"))
        enriched_cr = {
            **cr,
            "_extra": extra,
            "_is_vacancy": is_vacancy,
            "_is_hygiene_v11": is_hygiene_v11,
        }
        cu = _norm_username(cr.get("username"))
        cph = _norm_phone_15(cr.get("phone"))
        if not is_hygiene_v11:
            if cr.get("lead_id") is not None:
                klid = int(cr["lead_id"])
                if klid not in rec_by_lead_id or (rec_by_lead_id[klid]["_is_vacancy"] and not is_vacancy):
                    rec_by_lead_id[klid] = enriched_cr
            if cr.get("telegram_user_id") is not None:
                kpeer = int(cr["telegram_user_id"])
                if kpeer not in rec_by_peer or (rec_by_peer[kpeer]["_is_vacancy"] and not is_vacancy):
                    rec_by_peer[kpeer] = enriched_cr
            if cu and (cu not in rec_by_user or (rec_by_user[cu]["_is_vacancy"] and not is_vacancy)):
                rec_by_user[cu] = enriched_cr
            if cph and (cph not in rec_by_phone or (rec_by_phone[cph]["_is_vacancy"] and not is_vacancy)):
                rec_by_phone[cph] = enriched_cr

        if is_contacted and not is_vacancy and not is_hygiene_v11:
            if cr.get("lead_id") is not None:
                contacted_lead_ids.add(int(cr["lead_id"]))
            if cr.get("telegram_user_id") is not None:
                contacted_peer_ids.add(int(cr["telegram_user_id"]))
            if cu:
                contacted_usernames.add(cu)
            if cph:
                contacted_phones.add(cph)

    last_out_mid_by_dlg: dict[int, int] = {}
    msg_in_summary_by_dlg: dict[int, dict] = {}
    try:
        for mr in conn.execute(
            """
            SELECT id, dialog_id, direction, text, sent_at, telegram_message_id
            FROM messages
            ORDER BY REPLACE(COALESCE(sent_at, ''), ' ', 'T') ASC, id ASC
            """
        ).fetchall():
            did = int(mr["dialog_id"])
            direction = str(mr["direction"] or "")
            txt = str(mr["text"] or "").strip()
            ts = _norm_ts(mr["sent_at"])
            mid_pk = int(mr["id"])
            if direction == "out" and mr["telegram_message_id"] is not None:
                tg_mid = int(mr["telegram_message_id"])
                if did not in last_out_mid_by_dlg or tg_mid > last_out_mid_by_dlg[did]:
                    last_out_mid_by_dlg[did] = tg_mid
            msum = msg_in_summary_by_dlg.get(did)
            if msum is None:
                msum = {
                    "in_count": 0,
                    "max_in_ts": "",
                    "max_any_ts": "",
                    "last_in_text": "",
                    "last_any_text": "",
                    "last_any_dir": "",
                    "_in_key": ("", -1),
                    "_any_key": ("", -1),
                }
                msg_in_summary_by_dlg[did] = msum
            any_key = (ts, mid_pk)
            if any_key >= msum["_any_key"]:
                msum["_any_key"] = any_key
                msum["max_any_ts"] = ts
                msum["last_any_text"] = txt
                msum["last_any_dir"] = direction
            if direction == "in":
                msum["in_count"] += 1
                if any_key >= msum["_in_key"]:
                    msum["_in_key"] = any_key
                    msum["max_in_ts"] = ts
                    if txt or not msum["last_in_text"]:
                        msum["last_in_text"] = txt
        for msum in msg_in_summary_by_dlg.values():
            msum.pop("_in_key", None)
            msum.pop("_any_key", None)
    except Exception:
        pass

    dlg_rows = [as_dict(r) for r in conn.execute("SELECT * FROM dialogs ORDER BY id").fetchall()]
    dialogs_by_id: dict[int, dict] = {}
    dialogs_by_lead_id: dict[int, dict] = {}
    dialogs_by_acc_peer: dict[tuple[int, int], dict] = {}
    dialogs_by_peer: dict[int, dict] = {}
    dialogs_by_user: dict[str, dict] = {}
    dialogs_by_phone: dict[str, dict] = {}
    eligible_crm_dialog_ids: set[int] = set()
    live_waiting_dialogs: list[dict] = []

    for d in dlg_rows:
        did = int(d["id"])
        aid = int(d["account_id"]) if d.get("account_id") is not None else None
        peer_id = int(d["peer_id"]) if d.get("peer_id") is not None else None
        uname = _norm_username(d.get("username"))
        ph15 = _norm_phone_15(d.get("phone"))
        dlid = int(d["lead_id"]) if d.get("lead_id") is not None else None
        dialogs_by_id[did] = d
        if dlid is not None:
            dialogs_by_lead_id.setdefault(dlid, d)
        if aid is not None and peer_id is not None:
            dialogs_by_acc_peer[(aid, peer_id)] = d
        if peer_id is not None and (peer_id not in dialogs_by_peer or int(d.get("last_from_peer") or 0) == 1):
            dialogs_by_peer[peer_id] = d
        if uname and (uname not in dialogs_by_user or int(d.get("last_from_peer") or 0) == 1):
            dialogs_by_user[uname] = d
        if ph15 and (ph15 not in dialogs_by_phone or int(d.get("last_from_peer") or 0) == 1):
            dialogs_by_phone[ph15] = d

        if str(d.get("peer_type") or "user") != "user" or int(d.get("is_group") or 0) == 1:
            continue
        is_bot = bool(d.get("is_bot"))
        acc_meta = accounts_by_id.get(aid) if aid is not None else None
        inelig, _ = is_ineligible_reply_notify_peer(
            conn,
            peer_id=peer_id,
            username=uname or None,
            lead_handle=d.get("title"),
            phone=d.get("phone"),
            account_id=aid,
            account_handle=acc_meta["handle"] if acc_meta else None,
            is_bot=is_bot,
        )
        if inelig:
            continue

        matched_lead = None
        for cand_lead in (
            leads_by_id.get(dlid) if dlid is not None else None,
            leads_by_peer.get(peer_id) if peer_id is not None else None,
            leads_by_user.get(uname) if uname else None,
            leads_by_phone.get(ph15) if ph15 else None,
        ):
            if cand_lead is not None:
                matched_lead = _prefer_lead(matched_lead, cand_lead)
        matched_contacted = bool(
            (dlid is not None and dlid in contacted_lead_ids)
            or (peer_id is not None and peer_id in contacted_peer_ids)
            or (uname and uname in contacted_usernames)
            or (ph15 and ph15 in contacted_phones)
        )
        if matched_lead is None and not matched_contacted:
            continue

        eligible_crm_dialog_ids.add(did)
        is_closed = bool(matched_lead and matched_lead.get("_is_closed"))
        is_sniper_quarantined = str((matched_lead or {}).get("next_step") or "").startswith(
            "Авто-карантин Sniper ICP"
        )
        unread_cnt = int(d.get("unread_count") or 0)
        if (
            int(d.get("last_from_peer") or 0) == 1
            and not is_sniper_quarantined
            and (not is_closed or unread_cnt > 0)
        ):
            matched_rec = (
                (rec_by_lead_id.get(int(matched_lead["id"])) if matched_lead else None)
                or (rec_by_peer.get(peer_id) if peer_id is not None else None)
                or (rec_by_user.get(uname) if uname else None)
                or (rec_by_phone.get(ph15) if ph15 else None)
            )
            extra = matched_rec.get("_extra", {}) if matched_rec else {}
            msum = msg_in_summary_by_dlg.get(did)
            reply_txt = _resolve_dialog_reply_text(d, msum, extra)
            intent = str(extra.get("reply_intent") or "").strip() or _infer_reply_intent(reply_txt)
            live_waiting_dialogs.append(
                {
                    "dialog_id": did,
                    "lead_id": int(matched_lead["id"]) if matched_lead else dlid,
                    "account_id": aid,
                    "peer_id": peer_id,
                    "sender_account": acc_meta["handle"] if acc_meta else f"#{aid}",
                    "account_health": acc_meta["health"] if acc_meta else "unknown",
                    "account_is_dropped": bool(acc_meta["is_dropped"]) if acc_meta else False,
                    "username": uname or (matched_lead.get("username") if matched_lead else None),
                    "name": (
                        d.get("title")
                        or (matched_lead.get("name") if matched_lead else None)
                        or (f"@{uname}" if uname else None)
                        or d.get("phone")
                        or f"Dialog #{did}"
                    ),
                    "company": (matched_lead.get("company") if matched_lead else None)
                    or (matched_rec.get("company") if matched_rec else "")
                    or "",
                    "unread_count": unread_cnt,
                    "reply_intent": intent,
                    "last_touch_at": str(d.get("last_message_at") or ""),
                    "reply_text": reply_txt[:360],
                }
            )

    return {
        "accounts_by_id": accounts_by_id,
        "leads_by_id": leads_by_id,
        "leads_by_peer": leads_by_peer,
        "leads_by_user": leads_by_user,
        "leads_by_phone": leads_by_phone,
        "rec_rows": rec_rows,
        "rec_by_lead_id": rec_by_lead_id,
        "rec_by_peer": rec_by_peer,
        "rec_by_user": rec_by_user,
        "rec_by_phone": rec_by_phone,
        "dialogs_by_id": dialogs_by_id,
        "dialogs_by_lead_id": dialogs_by_lead_id,
        "dialogs_by_acc_peer": dialogs_by_acc_peer,
        "dialogs_by_peer": dialogs_by_peer,
        "dialogs_by_user": dialogs_by_user,
        "dialogs_by_phone": dialogs_by_phone,
        "last_out_mid_by_dlg": last_out_mid_by_dlg,
        "msg_in_summary_by_dlg": msg_in_summary_by_dlg,
        "eligible_crm_dialog_ids": eligible_crm_dialog_ids,
        "live_waiting_dialogs": live_waiting_dialogs,
    }


def _match_recipient_dialog(cr: dict, ctx: dict) -> dict | None:
    if cr.get("lead_id") is not None:
        dlg = ctx["dialogs_by_lead_id"].get(int(cr["lead_id"]))
        if dlg is not None:
            return dlg
    aid = cr.get("sent_account_id") or cr.get("assigned_account_id")
    pid = int(cr["telegram_user_id"]) if cr.get("telegram_user_id") is not None else None
    if aid is not None and pid is not None:
        dlg = ctx["dialogs_by_acc_peer"].get((int(aid), pid))
        if dlg is not None:
            return dlg
    if pid is not None and pid in ctx["dialogs_by_peer"]:
        return ctx["dialogs_by_peer"][pid]
    uname = _norm_username(cr.get("username"))
    if uname and uname in ctx["dialogs_by_user"]:
        return ctx["dialogs_by_user"][uname]
    ph15 = _norm_phone_15(cr.get("phone"))
    if ph15 and ph15 in ctx["dialogs_by_phone"]:
        return ctx["dialogs_by_phone"][ph15]
    return None


def _is_recipient_read_live(rec: dict, extra: dict, dlg: dict | None, ctx: dict) -> bool:
    if str(rec.get("status") or "").lower() == "replied":
        return True
    if rec.get("read_at"):
        return True
    if int(extra.get("in_count") or 0) > 0 or bool(extra.get("last_out_read")):
        return True
    if dlg is not None:
        if int(dlg.get("last_from_peer") or 0) == 1:
            return True
        msum = ctx.get("msg_in_summary_by_dlg", {}).get(int(dlg["id"]))
        if msum and int(msum.get("in_count") or 0) > 0:
            return True
        ro = int(dlg.get("read_outbox_max_id") or 0)
        if ro > 0:
            last_out_mid = ctx["last_out_mid_by_dlg"].get(int(dlg["id"]))
            if last_out_mid is None or ro >= last_out_mid:
                return True
    return False


def _compute_outreach_analytics(conn: sqlite3.Connection, ctx: dict | None = None) -> dict:
    if ctx is None:
        _sync_running_and_all_campaign_replies(conn)
        ctx = _load_ssot_crm_context(conn)

    today = utc_today()
    status_rank = {"running": 0, "draft": 1, "paused": 2, "completed": 3}
    camp_rows = [
        as_dict(r)
        for r in conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
    ]
    camp_rows.sort(key=lambda c: (status_rank.get(str(c.get("status") or "draft"), 9), -int(c["id"])))

    step_rows = [
        as_dict(r)
        for r in conn.execute(
            "SELECT campaign_id, step_order, delay_hours FROM campaign_steps ORDER BY campaign_id, step_order"
        ).fetchall()
    ]
    steps_by_camp: dict[int, list[dict]] = {}
    for sr in step_rows:
        steps_by_camp.setdefault(int(sr["campaign_id"]), []).append(sr)

    sent_today_events_by_camp: dict[int, int] = {}
    try:
        for evr in conn.execute(
            """
            SELECT campaign_id, COUNT(*) AS cnt
            FROM campaign_events
            WHERE event_type = 'sent' AND substr(created_at, 1, 10) = ?
            GROUP BY campaign_id
            """,
            (today,),
        ).fetchall():
            if evr["campaign_id"] is not None:
                sent_today_events_by_camp[int(evr["campaign_id"])] = int(evr["cnt"] or 0)
    except Exception:
        pass

    waiting_by_camp_ids: dict[tuple[int | None, int | None, str], dict] = {}
    for w in ctx["live_waiting_dialogs"]:
        waiting_by_camp_ids[(w.get("lead_id"), w.get("peer_id"), str(w.get("username") or "").lower())] = w

    by_camp: dict[int, list[dict]] = {int(c["id"]): [] for c in camp_rows}
    for rec in ctx["rec_rows"]:
        by_camp.setdefault(int(rec["campaign_id"]), []).append(rec)

    campaigns_breakdown = []
    total_recipients = 0
    total_pending = 0
    total_in_progress = 0
    total_completed = 0
    total_replied = 0
    total_failed = 0
    total_sent = 0
    total_sent_today = 0
    total_read = 0
    pending_running_total = 0
    pending_draft_total = 0
    total_converted_talking = 0
    total_converted_won = 0

    for camp in camp_rows:
        cid = int(camp["id"])
        camp_status = str(camp.get("status") or "draft")
        recs = by_camp.get(cid, [])
        c_total = len(recs)
        c_pending = sum(1 for r in recs if r.get("status") == "pending")
        c_in_prog = sum(1 for r in recs if r.get("status") == "in_progress")
        c_comp = sum(1 for r in recs if r.get("status") == "completed")
        c_rep = sum(1 for r in recs if r.get("status") == "replied")
        c_fail = sum(1 for r in recs if r.get("status") == "failed")
        c_sent = c_in_prog + c_comp + c_rep

        c_sent_today_recs = sum(
            1
            for r in recs
            if r.get("status") in ("in_progress", "completed", "replied")
            and str(r.get("last_sent_at") or "").startswith(today)
        )
        c_sent_today = max(c_sent_today_recs, sent_today_events_by_camp.get(cid, 0))

        c_read = 0
        c_pos = 0
        c_q = 0
        c_refusal = 0
        c_waiting_ids: set[int] = set()
        c_talking = 0
        c_won = 0
        step_buckets: dict[int, dict[str, int]] = {}

        for r in recs:
            st = str(r.get("status") or "")
            err_txt = str(r.get("error_text") or "").strip()
            is_hygiene_v11 = bool(r.get("_is_hygiene_v11")) or (err_txt in _HYGIENE_V11_ERRORS)
            lid = int(r["lead_id"]) if r.get("lead_id") is not None else None
            lead_row = ctx["leads_by_id"].get(lid) if lid is not None else None
            l_status = lead_row.get("_norm_status") if lead_row else None
            if l_status in ("talking", "call_scheduled", "waiting", "on_hold", "won") or (
                st == "replied" and not is_hygiene_v11
            ):
                c_talking += 1
            if l_status == "won":
                c_won += 1

            extra = r.get("_extra") if isinstance(r.get("_extra"), dict) else _parse_extra_json(r.get("extra_json"))
            dlg = _match_recipient_dialog(r, ctx)
            msum = ctx["msg_in_summary_by_dlg"].get(int(dlg["id"])) if dlg is not None else None
            is_contacted_rec = is_recipient_contacted_outreach(
                st, r.get("current_step"), r.get("last_sent_at"), err_txt
            )
            is_sent = (st in ("in_progress", "completed", "replied")) and is_contacted_rec
            is_read = is_sent and _is_recipient_read_live(r, extra, dlg, ctx)
            if is_read:
                c_read += 1

            cur_step = max(1, int(r.get("current_step") or 0))
            if is_sent:
                for s_no in range(1, cur_step + 1):
                    sb = step_buckets.setdefault(s_no, {"sent": 0, "read": 0, "replied": 0})
                    sb["sent"] += 1
                    if is_read:
                        sb["read"] += 1
                if st == "replied":
                    rep_step_str = str(extra.get("replied_to_step") or "")
                    rep_s = (
                        3
                        if rep_step_str.startswith("step3")
                        else (2 if rep_step_str.startswith("step2") else cur_step)
                    )
                    sb_rep = step_buckets.setdefault(rep_s, {"sent": 0, "read": 0, "replied": 0})
                    sb_rep["replied"] += 1

            has_reply_sig = (not is_hygiene_v11) and (
                st == "replied"
                or int(extra.get("in_count") or 0) > 0
                or (
                    is_contacted_rec
                    and (
                        int((msum or {}).get("in_count") or 0) > 0
                        or (dlg is not None and int(dlg.get("last_from_peer") or 0) == 1)
                    )
                )
            )
            if has_reply_sig:
                reply_txt = _resolve_dialog_reply_text(dlg, msum, extra)
                intent = str(extra.get("reply_intent") or "").strip() or _infer_reply_intent(reply_txt)
                if intent == "positive_interested":
                    c_pos += 1
                elif intent == "refusal_not_interested":
                    c_refusal += 1
                else:
                    c_q += 1

            if not is_hygiene_v11 and dlg is not None and int(dlg.get("last_from_peer") or 0) == 1:
                did = int(dlg["id"])
                if did in ctx["eligible_crm_dialog_ids"]:
                    eff_lead = _resolve_dialog_lead(dlg, ctx, fallback_lead=lead_row)
                    is_closed = bool(eff_lead and eff_lead.get("_is_closed"))
                    is_sniper_q = str((eff_lead or {}).get("next_step") or "").startswith(
                        "Авто-карантин Sniper ICP"
                    )
                    if not is_sniper_q and (not is_closed or int(dlg.get("unread_count") or 0) > 0):
                        c_waiting_ids.add(did)

        acc_ids = _parse_account_ids(camp.get("account_id"), camp.get("account_ids_json"))
        assigned_accounts = []
        healthy_acc_cnt = 0
        dropped_acc_cnt = 0
        inferred_provider = None
        for aid in acc_ids:
            am = ctx["accounts_by_id"].get(aid)
            if am is None:
                continue
            if inferred_provider is None:
                inferred_provider = str(am.get("provider") or "telegram").strip().lower()
            if am["is_dropped"]:
                dropped_acc_cnt += 1
            else:
                healthy_acc_cnt += 1
            assigned_accounts.append(
                {
                    "id": aid,
                    "handle": am["handle"],
                    "telegram_username": am.get("telegram_username"),
                    "name": am.get("telegram_name") or am.get("phone") or am["handle"],
                    "provider": am.get("provider") or "telegram",
                    "status": am["status"],
                    "health": am["health"],
                    "is_dropped": am["is_dropped"],
                }
            )

        c_unseen = max(0, c_sent - c_read)
        c_open_rate = round((c_read / c_sent) * 100, 1) if c_sent > 0 else 0.0
        c_reply_rate = round((c_rep / c_sent) * 100, 1) if c_sent > 0 else 0.0
        c_reply_of_read = round((c_rep / c_read) * 100, 1) if c_read > 0 else 0.0

        c_steps = [
            {
                "step": s_no,
                "sent": step_buckets[s_no]["sent"],
                "read": step_buckets[s_no]["read"],
                "replied": step_buckets[s_no]["replied"],
            }
            for s_no in sorted(step_buckets)
        ]

        total_recipients += c_total
        total_pending += c_pending
        total_in_progress += c_in_prog
        total_completed += c_comp
        total_replied += c_rep
        total_failed += c_fail
        total_sent += c_sent
        total_sent_today += c_sent_today
        total_read += c_read
        total_converted_talking += c_talking
        total_converted_won += c_won
        if camp_status == "running":
            pending_running_total += c_pending
        elif camp_status == "draft":
            pending_draft_total += c_pending

        campaigns_breakdown.append(
            {
                "id": cid,
                "name": camp.get("name") or f"Кампания #{cid}",
                "status": camp_status,
                "provider": inferred_provider or "telegram",
                "daily_limit": int(camp.get("daily_limit") or 20),
                "steps_count": max(len(steps_by_camp.get(cid, [])), len(step_buckets)),
                "created_at": camp.get("created_at"),
                "updated_at": camp.get("updated_at"),
                "total_recipients": c_total,
                "sent": c_sent,
                "sent_recipients": c_sent,
                "sent_today": c_sent_today,
                "pending": c_pending,
                "queued_new": c_pending,
                "in_progress": c_in_prog,
                "awaiting_followup": c_in_prog,
                "completed": c_comp,
                "replied": c_rep,
                "failed": c_fail,
                "read": c_read,
                "unseen": c_unseen,
                "open_rate_pct": c_open_rate,
                "reply_rate_pct": c_reply_rate,
                "reply_of_read_pct": c_reply_of_read,
                "positive": c_pos,
                "questions": c_q,
                "refusals": c_refusal,
                "waiting_our_answer": len(c_waiting_ids),
                "converted_to_talking": c_talking,
                "converted_to_won": c_won,
                "assigned_accounts": assigned_accounts,
                "healthy_accounts_count": healthy_acc_cnt,
                "dropped_accounts_count": dropped_acc_cnt,
                "steps_breakdown": c_steps,
            }
        )

    overall_reply_rate = (
        round((total_replied / total_sent) * 100, 1) if total_sent > 0 else 0.0
    )
    overall_open_rate = (
        round((total_read / total_sent) * 100, 1) if total_sent > 0 else 0.0
    )
    running_campaigns = sum(1 for c in campaigns_breakdown if c["status"] == "running")
    active_campaigns = [c for c in campaigns_breakdown if c["status"] == "running"]

    return {
        "total_campaigns": len(camp_rows),
        "running_campaigns": running_campaigns,
        "active_campaigns": active_campaigns,
        "total_recipients": total_recipients,
        "sent": total_sent,
        "sent_recipients": total_sent,
        "sent_today_total": total_sent_today,
        "read": total_read,
        "unseen": max(0, total_sent - total_read),
        "open_rate_pct": overall_open_rate,
        "pending": total_pending,
        "pending_recipients": total_pending,
        "pending_running_total": pending_running_total,
        "pending_draft_total": pending_draft_total,
        "queued_new": total_pending,
        "in_progress": total_in_progress,
        "in_progress_recipients": total_in_progress,
        "awaiting_followup": total_in_progress,
        "completed": total_completed,
        "completed_recipients": total_completed,
        "replied": total_replied,
        "replied_recipients": total_replied,
        "failed": total_failed,
        "failed_total": total_failed,
        "failed_recipients": total_failed,
        "reply_rate_pct": overall_reply_rate,
        "converted_to_talking": total_converted_talking,
        "converted_to_won": total_converted_won,
        "campaigns": campaigns_breakdown,
    }


def _compute_accounts_analytics(conn: sqlite3.Connection, ctx: dict | None = None) -> dict:
    if ctx is None:
        ctx = _load_ssot_crm_context(conn)
    today = utc_today()
    acc_rows = [
        as_dict(r)
        for r in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    ]

    by_health: dict[str, int] = {k: 0 for k in HEALTH_KEYS}
    by_health["unknown"] = 0

    waiting_cnt_by_acc: dict[int, int] = {}
    for w in ctx["live_waiting_dialogs"]:
        if w.get("account_id") is not None:
            waiting_cnt_by_acc[int(w["account_id"])] = waiting_cnt_by_acc.get(int(w["account_id"]), 0) + 1

    account_items = []
    dropped_accounts = []
    total_sent_today = 0
    total_daily_limit = 0
    connected_daily_limit = 0
    connected_sent_today = 0
    connected_count = 0
    healthy_daily_limit = 0
    healthy_sent_today = 0
    healthy_count = 0
    total_dialogs = 0
    total_replies = 0

    for row in acc_rows:
        aid = int(row["id"])
        status = str(row.get("status") or "error")
        raw_health = row.get("health")
        health = str(raw_health).strip() if raw_health else None
        eff_health = health or ("active" if status == "connected" else "offline")
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
        is_dropped = not (status == "connected" and eff_health == "active")

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

        replied_dialog_peer_ids = {
            int(d["peer_id"]) for d in replied_dialogs if d.get("peer_id") is not None
        }
        replied_dialog_lead_ids = {
            int(d["lead_id"]) for d in replied_dialogs if d.get("lead_id") is not None
        }
        replied_dialog_usernames = {
            _norm_username(d["username"])
            for d in replied_dialogs
            if _norm_username(d.get("username"))
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
            elif _norm_username(cr.get("username")) in replied_dialog_usernames:
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
            if eff_health == "active":
                healthy_count += 1
                healthy_daily_limit += daily_limit
                healthy_sent_today += sent_today
        total_dialogs += dialogs_count
        total_replies += replies_count

        item_obj = {
            "id": aid,
            "phone": row.get("phone"),
            "name": display_name,
            "telegram_name": row.get("telegram_name"),
            "telegram_username": row.get("telegram_username"),
            "handle": _account_display_handle(row, aid),
            "provider": provider,
            "status": status,
            "health": eff_health,
            "raw_health": health,
            "is_dropped": is_dropped,
            "last_error": row.get("last_error"),
            "updated_at": row.get("updated_at"),
            "dropped_at": row.get("updated_at") if is_dropped else None,
            "sent_today": sent_today,
            "daily_limit": daily_limit,
            "remaining_today": remaining_today,
            "utilization_pct": utilization_pct,
            "dialogs_count": dialogs_count,
            "replies_count": replies_count,
            "replied_dialogs_count": replied_dialogs_count,
            "campaign_sent_count": camp_sent_count,
            "campaign_replied_count": camp_replied_count,
            "waiting_our_answer": waiting_cnt_by_acc.get(aid, 0),
            "reply_rate_pct": reply_rate_pct,
        }
        account_items.append(item_obj)
        if is_dropped:
            dropped_accounts.append(item_obj)

    dropped_accounts.sort(key=lambda a: (str(a.get("dropped_at") or ""), int(a["id"])), reverse=True)
    if healthy_count > 0:
        effective_limit = healthy_daily_limit
        effective_sent = healthy_sent_today
    elif connected_count > 0:
        effective_limit = connected_daily_limit
        effective_sent = connected_sent_today
    else:
        effective_limit = total_daily_limit
        effective_sent = total_sent_today
    pool_utilization_pct = (
        round((effective_sent / effective_limit) * 100, 1)
        if effective_limit > 0
        else 0.0
    )

    return {
        "total_accounts": len(acc_rows),
        "connected_accounts": connected_count,
        "healthy_accounts": healthy_count,
        "dropped_accounts_count": len(dropped_accounts),
        "dropped_accounts": dropped_accounts,
        "sent_today": total_sent_today,
        "healthy_sent_today": healthy_sent_today,
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


def _resolve_date_list_and_cutoff(
    conn: sqlite3.Connection, period: str
) -> tuple[list[str], str | None]:
    today_dt = datetime.now(timezone.utc).date()
    clean_p = str(period or "30d").strip().lower()
    if clean_p == "7d":
        days_count = 7
        cutoff_dt = today_dt - timedelta(days=days_count - 1)
    elif clean_p == "90d":
        days_count = 90
        cutoff_dt = today_dt - timedelta(days=days_count - 1)
    elif clean_p == "all":
        earliest_str: str | None = None
        for sql in (
            "SELECT MIN(substr(created_at, 1, 10)) FROM leads WHERE created_at IS NOT NULL AND created_at != ''",
            "SELECT MIN(substr(sent_at, 1, 10)) FROM messages WHERE sent_at IS NOT NULL AND sent_at != ''",
            "SELECT MIN(substr(last_sent_at, 1, 10)) FROM campaign_recipients WHERE last_sent_at IS NOT NULL AND last_sent_at != ''",
        ):
            try:
                val = conn.execute(sql).fetchone()[0]
                if val and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(val)[:10]):
                    d_str = str(val)[:10]
                    if earliest_str is None or d_str < earliest_str:
                        earliest_str = d_str
            except Exception:
                pass
        if earliest_str:
            try:
                earliest_date = datetime.strptime(earliest_str, "%Y-%m-%d").date()
            except ValueError:
                earliest_date = today_dt - timedelta(days=13)
        else:
            earliest_date = today_dt - timedelta(days=13)
        span_days = (today_dt - earliest_date).days + 1
        days_count = max(14, min(span_days, 120))
        cutoff_dt = today_dt - timedelta(days=days_count - 1)
    else:
        days_count = 30
        cutoff_dt = today_dt - timedelta(days=days_count - 1)

    dates = [
        (cutoff_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days_count)
    ]
    cutoff_filter = None if clean_p == "all" else dates[0]
    return dates, cutoff_filter


def _compute_charts_analytics(
    conn: sqlite3.Connection,
    *,
    period: str,
    accounts_analytics: dict,
    ctx: dict | None = None,
) -> dict:
    if ctx is None:
        ctx = _load_ssot_crm_context(conn)
    dates, cutoff_date = _resolve_date_list_and_cutoff(conn, period)
    prop_amounts = _load_lead_amounts_from_properties(conn)
    stage_catalog = _load_stage_catalog(conn)

    lead_rows = [as_dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY id").fetchall()]
    eligible_ids = sorted(ctx["eligible_crm_dialog_ids"])
    msg_rows = []
    for start in range(0, len(eligible_ids), 400):
        chunk_ids = eligible_ids[start : start + 400]
        ph_elig = ",".join("?" for _ in chunk_ids)
        msg_rows.extend(
            as_dict(r)
            for r in conn.execute(
                f"SELECT id, dialog_id, direction, sent_at, created_at FROM messages WHERE dialog_id IN ({ph_elig}) ORDER BY id",
                chunk_ids,
            ).fetchall()
        )
    msg_rows.sort(key=lambda r: int(r.get("id") or 0))
    camp_rec_rows = ctx["rec_rows"]

    # 1. daily_flow (strictly scoped to CRM leads & outreach recipients; zero personal-chat pollution)
    new_leads_by_day: dict[str, int] = {d: 0 for d in dates}
    out_msgs_by_day: dict[str, int] = {d: 0 for d in dates}
    in_msgs_by_day: dict[str, int] = {d: 0 for d in dates}
    unique_reply_dlgs_by_day: dict[str, set[int]] = {d: set() for d in dates}
    camp_sent_by_day: dict[str, int] = {d: 0 for d in dates}

    for ld in lead_rows:
        c_day = str(ld.get("created_at") or "")[:10]
        if c_day in new_leads_by_day:
            new_leads_by_day[c_day] += 1

    for m in msg_rows:
        s_day = str(m.get("sent_at") or m.get("created_at") or "")[:10]
        if s_day not in out_msgs_by_day:
            continue
        if m.get("direction") == "out":
            out_msgs_by_day[s_day] += 1
        elif m.get("direction") == "in":
            in_msgs_by_day[s_day] += 1
            if m.get("dialog_id") is not None:
                unique_reply_dlgs_by_day[s_day].add(int(m["dialog_id"]))

    snapshot_reply_dts: list[datetime] = []
    msg_summary_map = ctx.get("msg_in_summary_by_dlg", {})
    for did in eligible_ids:
        dlg = ctx["dialogs_by_id"].get(did)
        if not dlg or int(dlg.get("last_from_peer") or 0) != 1:
            continue
        raw_last_at = dlg.get("last_message_at")
        if not raw_last_at:
            continue
        msum = msg_summary_map.get(did)
        dlg_prev = str(dlg.get("last_message_preview") or "").strip()
        if is_newer_dialog_snapshot_unbacked(
            dlg_prev,
            raw_last_at,
            dlg.get("last_from_peer"),
            (msum or {}).get("last_any_text"),
            (msum or {}).get("max_any_ts"),
            (msum or {}).get("last_any_dir"),
        ):
            dt = _parse_iso_dt(raw_last_at)
            if dt is not None:
                snap_day = dt.strftime("%Y-%m-%d")
                if snap_day in in_msgs_by_day:
                    in_msgs_by_day[snap_day] += 1
                    unique_reply_dlgs_by_day[snap_day].add(did)
                snapshot_reply_dts.append(dt)

    for cr in camp_rec_rows:
        ls_day = str(cr.get("last_sent_at") or "")[:10]
        if ls_day in camp_sent_by_day and cr.get("status") in (
            "in_progress",
            "completed",
            "replied",
        ):
            camp_sent_by_day[ls_day] += 1

    daily_flow = []
    for d in dates:
        lbl = f"{d[8:10]}.{d[5:7]}"
        camp_sent = camp_sent_by_day.get(d, 0)
        sent_cnt = max(out_msgs_by_day.get(d, 0), camp_sent)
        daily_flow.append(
            {
                "date": d,
                "label": lbl,
                "new_leads": new_leads_by_day.get(d, 0),
                "campaign_sent": camp_sent,
                "messages_sent": sent_cnt,
                "replies_received": in_msgs_by_day.get(d, 0),
                "lead_replies_unique": len(unique_reply_dlgs_by_day.get(d, ())),
            }
        )

    # 2. revenue_timeline
    enriched_leads = []
    for ld in lead_rows:
        st = normalize_status(ld.get("status"), conn) or "new"
        amt = _resolve_lead_amount(ld, prop_amounts)
        mrr = _resolve_lead_monthly(ld)
        c_day = str(ld.get("created_at") or "")[:10]
        u_day = str(ld.get("updated_at") or ld.get("created_at") or "")[:10]
        enriched_leads.append(
            {
                "raw": ld,
                "status": st,
                "amount": amt,
                "monthly_value": mrr,
                "created_day": c_day,
                "updated_day": u_day,
            }
        )

    won_before_window = sum(
        el["amount"]
        for el in enriched_leads
        if el["status"] == "won" and el["updated_day"] and el["updated_day"] < dates[0]
    )
    cumulative_won = won_before_window
    revenue_timeline = []
    for d in dates:
        lbl = f"{d[8:10]}.{d[5:7]}"
        won_on_day = sum(
            el["amount"]
            for el in enriched_leads
            if el["status"] == "won" and el["updated_day"] == d
        )
        cumulative_won += won_on_day
        pipeline_on_day = sum(
            el["amount"]
            for el in enriched_leads
            if el["status"] not in ("won", "lost")
            and (not el["created_day"] or el["created_day"] <= d)
        )
        mrr_on_day = sum(
            el["monthly_value"]
            for el in enriched_leads
            if el["status"] != "lost"
            and (not el["created_day"] or el["created_day"] <= d)
        )
        revenue_timeline.append(
            {
                "date": d,
                "label": lbl,
                "won_amount": won_on_day,
                "pipeline_amount": pipeline_on_day,
                "monthly_value": mrr_on_day,
                "cumulative_won": cumulative_won,
            }
        )

    # Filter leads by period cutoff if applicable
    if cutoff_date:
        period_leads = [
            el
            for el in enriched_leads
            if (el["created_day"] and el["created_day"] >= cutoff_date)
            or (el["updated_day"] and el["updated_day"] >= cutoff_date)
        ]
    else:
        period_leads = list(enriched_leads)

    # 3. funnel_dropoff
    stage_counts: dict[str, int] = {k: 0 for k, _ in stage_catalog}
    stage_amounts: dict[str, int] = {k: 0 for k, _ in stage_catalog}
    stage_mrr: dict[str, int] = {k: 0 for k, _ in stage_catalog}
    stage_labels: dict[str, str] = {k: lbl for k, lbl in stage_catalog}

    for el in period_leads:
        st = el["status"]
        if st not in stage_counts:
            stage_counts[st] = 0
            stage_amounts[st] = 0
            stage_mrr[st] = 0
            stage_labels[st] = STAGE_LABELS.get(st, st)
        stage_counts[st] += 1
        stage_amounts[st] += el["amount"]
        stage_mrr[st] += el["monthly_value"]

    total_period_leads = len(period_leads)
    total_period_amount = sum(el["amount"] for el in period_leads)

    progressive_keys = [
        k for k, _ in stage_catalog if k != "lost"
    ]
    funnel_dropoff = []
    prev_reached_count = total_period_leads
    prev_reached_amount = total_period_amount

    for idx, st_key in enumerate(progressive_keys):
        cnt = stage_counts.get(st_key, 0)
        amt = stage_amounts.get(st_key, 0)
        mrr = stage_mrr.get(st_key, 0)
        if idx == 0:
            reached_cnt = total_period_leads
            reached_amt = total_period_amount
            conv_pct = 100.0 if reached_cnt > 0 else 0.0
            dropoff_cnt = 0
            dropoff_amt = 0
        elif st_key == "on_hold":
            reached_cnt = cnt
            reached_amt = amt
            conv_pct = (
                round((reached_cnt / prev_reached_count) * 100, 1)
                if prev_reached_count > 0
                else 0.0
            )
            dropoff_cnt = max(0, prev_reached_count - reached_cnt)
            dropoff_amt = max(0, prev_reached_amount - reached_amt)
        else:
            tail_keys = progressive_keys[idx:]
            reached_cnt = sum(stage_counts.get(tk, 0) for tk in tail_keys)
            reached_amt = sum(stage_amounts.get(tk, 0) for tk in tail_keys)
            conv_pct = (
                round((reached_cnt / prev_reached_count) * 100, 1)
                if prev_reached_count > 0
                else 0.0
            )
            dropoff_cnt = max(0, prev_reached_count - reached_cnt)
            dropoff_amt = max(0, prev_reached_amount - reached_amt)

        if st_key != "on_hold":
            prev_reached_count = reached_cnt
            prev_reached_amount = reached_amt
        lbl = stage_labels.get(st_key, STAGE_LABELS.get(st_key, st_key))
        funnel_dropoff.append(
            {
                "key": st_key,
                "label": lbl,
                "airtable_label": KEY_TO_AIRTABLE_STATUS.get(st_key, lbl),
                "count": cnt,
                "amount": amt,
                "monthly_value": mrr,
                "reached_count": reached_cnt,
                "reached_amount": reached_amt,
                "conversion_from_prev_pct": conv_pct,
                "dropoff_count": dropoff_cnt,
                "dropoff_amount": dropoff_amt,
                "color": STAGE_COLORS.get(st_key, "#6366f1"),
            }
        )

    lost_cnt = stage_counts.get("lost", 0)
    lost_amt = stage_amounts.get("lost", 0)
    lost_mrr = stage_mrr.get("lost", 0)
    lost_lbl = stage_labels.get("lost", STAGE_LABELS["lost"])
    lost_pct = (
        round((lost_cnt / total_period_leads) * 100, 1)
        if total_period_leads > 0
        else 0.0
    )
    funnel_dropoff.append(
        {
            "key": "lost",
            "label": lost_lbl,
            "airtable_label": KEY_TO_AIRTABLE_STATUS.get("lost", "Cancelled"),
            "count": lost_cnt,
            "amount": lost_amt,
            "monthly_value": lost_mrr,
            "reached_count": lost_cnt,
            "reached_amount": lost_amt,
            "conversion_from_prev_pct": lost_pct,
            "dropoff_count": lost_cnt,
            "dropoff_amount": lost_amt,
            "color": STAGE_COLORS["lost"],
        }
    )

    # 4. channels_and_sources
    channel_stats: dict[str, dict] = {
        "telegram": {
            "provider": "telegram",
            "channel": "telegram",
            "label": "Telegram",
            "leads_count": 0,
            "won_count": 0,
            "pipeline_value": 0,
            "won_value": 0,
            "monthly_value": 0,
            "dialogs_count": 0,
            "replies_count": 0,
            "reply_rate_pct": 0.0,
        },
        "whatsapp": {
            "provider": "whatsapp",
            "channel": "whatsapp",
            "label": "WhatsApp",
            "leads_count": 0,
            "won_count": 0,
            "pipeline_value": 0,
            "won_value": 0,
            "monthly_value": 0,
            "dialogs_count": 0,
            "replies_count": 0,
            "reply_rate_pct": 0.0,
        },
    }

    for el in period_leads:
        ld = el["raw"]
        prov = str(ld.get("provider") or "telegram").strip().lower()
        if prov not in channel_stats:
            prov = "telegram"
        cs = channel_stats[prov]
        cs["leads_count"] += 1
        if el["status"] == "won":
            cs["won_count"] += 1
            cs["won_value"] += el["amount"]
            cs["monthly_value"] += el["monthly_value"]
        elif el["status"] != "lost":
            cs["pipeline_value"] += el["amount"]
            cs["monthly_value"] += el["monthly_value"]

    for acc in accounts_analytics.get("items", []):
        prov = str(acc.get("provider") or "telegram").strip().lower()
        if prov not in channel_stats:
            prov = "telegram"
        channel_stats[prov]["dialogs_count"] += int(acc.get("dialogs_count") or 0)
        channel_stats[prov]["replies_count"] += int(acc.get("replies_count") or 0)

    for prov, cs in channel_stats.items():
        if cs["dialogs_count"] > 0:
            cs["reply_rate_pct"] = round(
                min(100.0, (cs["replies_count"] / cs["dialogs_count"]) * 100), 1
            )
        else:
            cs["reply_rate_pct"] = 0.0

    by_channel = [channel_stats["telegram"], channel_stats["whatsapp"]]

    source_buckets: dict[str, dict] = {}
    for el in period_leads:
        ld = el["raw"]
        raw_src = (ld.get("lead_source") or "").strip()
        if not raw_src:
            prov = str(ld.get("provider") or "telegram").strip().lower()
            raw_src = "WhatsApp" if prov == "whatsapp" else "Telegram"
        bucket = source_buckets.setdefault(
            raw_src,
            {
                "source": raw_src,
                "label": raw_src,
                "count": 0,
                "won_count": 0,
                "amount": 0,
                "monthly_value": 0,
                "win_rate_pct": 0.0,
            },
        )
        bucket["count"] += 1
        bucket["amount"] += el["amount"]
        bucket["monthly_value"] += el["monthly_value"]
        if el["status"] == "won":
            bucket["won_count"] += 1

    by_source = []
    for b in source_buckets.values():
        b["win_rate_pct"] = (
            round((b["won_count"] / b["count"]) * 100, 1) if b["count"] > 0 else 0.0
        )
        by_source.append(b)
    by_source.sort(key=lambda x: (x["amount"], x["count"], x["won_count"]), reverse=True)

    accounts_ranking = sorted(
        list(accounts_analytics.get("items", [])),
        key=lambda a: (
            float(a.get("reply_rate_pct") or 0.0),
            int(a.get("sent_today") or 0),
            int(a.get("replies_count") or 0),
        ),
        reverse=True,
    )

    channels_and_sources = {
        "by_channel": by_channel,
        "by_source": by_source,
        "accounts_ranking": accounts_ranking,
    }

    # 5. reply_heatmap (7 days x 6 4-hour slots + 24 hours)
    matrix = [[0 for _ in range(6)] for _ in range(7)]
    hourly_matrix = [[0 for _ in range(24)] for _ in range(7)]
    by_hour = [0 for _ in range(24)]
    total_inbound = 0

    for m in msg_rows:
        if m.get("direction") != "in":
            continue
        raw_ts = m.get("sent_at") or m.get("created_at")
        dt = _parse_iso_dt(raw_ts)
        if dt is None:
            continue
        day_str = dt.strftime("%Y-%m-%d")
        if cutoff_date and day_str < cutoff_date:
            continue
        wday = dt.weekday()  # 0 = Mon .. 6 = Sun
        hr = dt.hour  # 0 .. 23
        slot_idx = min(5, hr // 4)
        matrix[wday][slot_idx] += 1
        hourly_matrix[wday][hr] += 1
        by_hour[hr] += 1
        total_inbound += 1

    for dt in snapshot_reply_dts:
        day_str = dt.strftime("%Y-%m-%d")
        if cutoff_date and day_str < cutoff_date:
            continue
        wday = dt.weekday()
        hr = dt.hour
        slot_idx = min(5, hr // 4)
        matrix[wday][slot_idx] += 1
        hourly_matrix[wday][hr] += 1
        by_hour[hr] += 1
        total_inbound += 1

    max_value = 0
    peak_day_idx = 0
    peak_slot_idx = 0
    for d_idx in range(7):
        for s_idx in range(6):
            val = matrix[d_idx][s_idx]
            if val > max_value:
                max_value = val
                peak_day_idx = d_idx
                peak_slot_idx = s_idx

    reply_heatmap = {
        "days": list(HEATMAP_DAYS),
        "slots": list(HEATMAP_SLOTS),
        "matrix": matrix,
        "hourly_matrix": hourly_matrix,
        "by_hour": by_hour,
        "max_value": max_value,
        "peak_day": HEATMAP_DAYS[peak_day_idx] if max_value > 0 else "—",
        "peak_slot": HEATMAP_SLOTS[peak_slot_idx] if max_value > 0 else "—",
        "total_inbound": total_inbound,
    }

    return {
        "daily_flow": daily_flow,
        "revenue_timeline": revenue_timeline,
        "funnel_dropoff": funnel_dropoff,
        "channels_and_sources": channels_and_sources,
        "reply_heatmap": reply_heatmap,
    }


def _compute_outreach_deep_breakdown(conn: sqlite3.Connection, ctx: dict | None = None) -> dict:
    from collections import defaultdict

    if ctx is None:
        ctx = _load_ssot_crm_context(conn)

    rec_rows = [r for r in ctx["rec_rows"] if not r.get("_is_vacancy")]
    pending_queue_real = sum(
        1
        for r in rec_rows
        if r.get("status") == "pending" and str(r.get("campaign_status") or "") == "running"
    )
    pending_draft_real = sum(
        1
        for r in rec_rows
        if r.get("status") == "pending" and str(r.get("campaign_status") or "") == "draft"
    )
    failed_real = sum(1 for r in rec_rows if r.get("status") == "failed")

    # Deduplicate by username (or phone digits / id when username missing), preferring non-hygiene rows with extra_json or non-pending status
    by_key: dict[str, dict] = {}
    for r in rec_rows:
        u = _norm_username(r.get("username"))
        raw_phone = str(r.get("phone") or "").strip()
        p_digits = "".join(ch for ch in raw_phone if ch.isdigit())[-15:]
        key = f"u:{u}" if u else (f"p:{p_digits}" if p_digits else f"id:{r.get('id')}")
        if key not in by_key:
            by_key[key] = r
        else:
            prev = by_key[key]
            r_hyg = str(r.get("error_text") or "").strip() in _HYGIENE_V11_ERRORS
            p_hyg = str(prev.get("error_text") or "").strip() in _HYGIENE_V11_ERRORS
            r_score = (0 if r_hyg else 4) + (2 if r.get("extra_json") else 0) + (1 if r.get("status") in ("in_progress", "completed", "replied") else 0)
            p_score = (0 if p_hyg else 4) + (2 if prev.get("extra_json") else 0) + (1 if prev.get("status") in ("in_progress", "completed", "replied") else 0)
            if r_score > p_score:
                by_key[key] = r

    unique_recs = list(by_key.values()) if by_key else rec_rows

    total_base = len(unique_recs)
    contacted_recs = []
    replied_recs = []

    fine_counts: dict[str, int] = defaultdict(int)
    intent_counts: dict[str, int] = defaultdict(int)
    waiting_by_account: dict[str, int] = defaultdict(int)
    waiting_items = []
    stalled_items = []
    positive_items = []

    STEP_LABELS_RU = {
        "step1_personal_pitch": "Шаг 1: Персонализированный питч",
        "inbound_first": "Шаг 1: Персонализированный питч",
        "empty": "Шаг 1: Персонализированный питч",
        "step2_xplabs_case": "Шаг 2: Кейс XPLabs (80M views + видео)",
        "step2_followup": "Шаг 2: Кейс XPLabs (80M views + видео)",
        "step3_revshare_cpa": "Шаг 3: Revshare / CPA ($600 пилот)",
        "step3_followup": "Шаг 3: Revshare / CPA ($600 пилот)",
    }
    INTENT_LABELS_RU = {
        "positive_interested": "+ Позитив / Целевой интерес",
        "neutral_or_question": "? Конструктивный вопрос",
        "defer_contact_later": "⏳ Написать позже",
        "refusal_not_interested": "- Отказ",
        "media_or_auto": "📎 Медиа / Автоответ",
    }

    cat_stats = defaultdict(
        lambda: {"contacted": 0, "read": 0, "replied": 0, "pos": 0, "constr": 0, "refusal": 0, "waiting": 0, "examples": []}
    )
    niche_stats = defaultdict(
        lambda: {"contacted": 0, "read": 0, "replied": 0, "pos": 0, "constr": 0, "refusal": 0, "waiting": 0, "examples": []}
    )
    hook_stats = defaultdict(
        lambda: {"contacted": 0, "read": 0, "replied": 0, "pos": 0, "constr": 0, "refusal": 0, "sample_text": ""}
    )

    live_waiting_by_dlg = {int(w["dialog_id"]): w for w in ctx["live_waiting_dialogs"]}
    matched_waiting_dlgs: set[int] = set()
    matched_rec_dlg_ids: set[int] = set()
    msg_summary_map = ctx.get("msg_in_summary_by_dlg", {})

    for r in unique_recs:
        extra = r.get("_extra") if isinstance(r.get("_extra"), dict) else _parse_extra_json(r.get("extra_json"))
        r["_extra"] = extra
        err_txt = str(r.get("error_text") or "").strip()
        is_hygiene_v11 = bool(r.get("_is_hygiene_v11")) or (err_txt in _HYGIENE_V11_ERRORS)
        dlg = _match_recipient_dialog(r, ctx)
        if dlg is not None and not is_hygiene_v11:
            matched_rec_dlg_ids.add(int(dlg["id"]))
        msum = msg_summary_map.get(int(dlg["id"])) if dlg is not None else None
        is_contacted = is_recipient_contacted_outreach(
            r.get("status"), r.get("current_step"), r.get("last_sent_at"), err_txt
        )
        in_cnt = (
            max(
                int(extra.get("in_count") or 0),
                int((msum or {}).get("in_count") or 0),
                1
                if (
                    r.get("status") == "replied"
                    or (dlg is not None and int(dlg.get("last_from_peer") or 0) == 1)
                )
                else 0,
            )
            if is_contacted
            else 0
        )

        fine = "" if is_hygiene_v11 else str(extra.get("fine_status") or "")
        if not fine:
            st = r.get("status")
            if is_hygiene_v11:
                fine = "invalid_or_deleted_username" if st == "failed" else "not_contacted_skipped"
            elif st == "replied" or (is_contacted and in_cnt > 0):
                if dlg is not None and int(dlg.get("last_from_peer") or 0) == 0:
                    fine = "replied_we_answered_seen_stalled"
                else:
                    fine = "replied_waiting_our_answer"
            elif st in ("in_progress", "completed"):
                fine = "unseen_after_all_3_steps"
            elif st == "failed":
                fine = "invalid_or_deleted_username"
            else:
                fine = "not_contacted_skipped"
        r["_fine"] = fine
        fine_counts[fine] += 1

        if not is_contacted:
            continue

        contacted_recs.append(r)
        is_read = (in_cnt > 0) or _is_recipient_read_live(r, extra, dlg, ctx)
        r["_is_read"] = is_read
        r["_in_cnt"] = in_cnt

        cat = str(r.get("category") or "SaaS").strip() or "SaaS"
        niche = str(extra.get("niche") or cat).strip()
        hook = str(extra.get("hook_format") or r.get("hook") or "Прямой продуктовый хук").strip()

        cat_stats[cat]["contacted"] += 1
        niche_stats[niche]["contacted"] += 1
        hook_stats[hook]["contacted"] += 1
        if not hook_stats[hook]["sample_text"] and r.get("step1_text") and len(str(r.get("step1_text"))) > 60:
            hook_stats[hook]["sample_text"] = str(r.get("step1_text")).strip()

        if is_read:
            cat_stats[cat]["read"] += 1
            niche_stats[niche]["read"] += 1
            hook_stats[hook]["read"] += 1

        if in_cnt > 0:
            replied_recs.append(r)
            reply_txt = _resolve_dialog_reply_text(dlg, msum, extra)
            intent = str(extra.get("reply_intent") or "").strip() or _infer_reply_intent(reply_txt)
            rep_step = str(extra.get("replied_to_step") or f"step{max(1, int(r.get('current_step') or 1))}")
            r["_intent"] = intent
            r["_rep_step"] = rep_step
            intent_counts[intent] += 1

            cat_stats[cat]["replied"] += 1
            niche_stats[niche]["replied"] += 1
            hook_stats[hook]["replied"] += 1

            if intent == "positive_interested":
                cat_stats[cat]["pos"] += 1
                niche_stats[niche]["pos"] += 1
                hook_stats[hook]["pos"] += 1
                ex_tag = f"@{r.get('username')} ({r.get('company') or r.get('name')})"
                if len(cat_stats[cat]["examples"]) < 3:
                    cat_stats[cat]["examples"].append(ex_tag)
                if len(niche_stats[niche]["examples"]) < 3:
                    niche_stats[niche]["examples"].append(ex_tag)
            if intent in ("positive_interested", "neutral_or_question"):
                cat_stats[cat]["constr"] += 1
                niche_stats[niche]["constr"] += 1
                hook_stats[hook]["constr"] += 1
            if intent == "refusal_not_interested":
                cat_stats[cat]["refusal"] += 1
                niche_stats[niche]["refusal"] += 1
                hook_stats[hook]["refusal"] += 1

            aid = (
                (int(dlg["account_id"]) if dlg and dlg.get("account_id") is not None else None)
                or r.get("sent_account_id")
                or r.get("assigned_account_id")
            )
            acc_meta = ctx["accounts_by_id"].get(int(aid)) if aid is not None else None
            sender_acc = (acc_meta["handle"] if acc_meta else None) or str(extra.get("sender_account") or "unknown")
            dlg_id = int(dlg["id"]) if dlg is not None else None
            item_obj = {
                "lead_id": r.get("lead_id"),
                "dialog_id": dlg_id,
                "account_id": aid,
                "sender_account": sender_acc,
                "account_is_dropped": bool(acc_meta["is_dropped"]) if acc_meta else False,
                "account_health": acc_meta["health"] if acc_meta else "unknown",
                "username": r.get("username"),
                "name": r.get("name") or r.get("company") or r.get("username"),
                "company": r.get("company") or "",
                "website": r.get("website") or "",
                "category": cat,
                "niche": niche,
                "hook_format": hook,
                "replied_to_step": rep_step,
                "replied_to_step_label": STEP_LABELS_RU.get(rep_step, f"Шаг {r.get('current_step') or 1}"),
                "reply_intent": intent,
                "reply_intent_label": INTENT_LABELS_RU.get(intent, intent),
                "reply_text": (reply_txt or str(r.get("note") or ""))[:360],
                "step1_text": str(r.get("step1_text") or "")[:360],
                "last_touch_at": str((dlg.get("last_message_at") if dlg else None) or r.get("last_sent_at") or ""),
                "fine_status": fine,
            }
            if dlg_id is not None and dlg_id in live_waiting_by_dlg:
                matched_waiting_dlgs.add(dlg_id)
                cat_stats[cat]["waiting"] += 1
                niche_stats[niche]["waiting"] += 1
                waiting_by_account[sender_acc] += 1
                waiting_items.append(item_obj)
            elif fine == "replied_we_answered_seen_stalled":
                stalled_items.append(item_obj)
            if intent == "positive_interested":
                positive_items.append(item_obj)

    for dlg_id, w in live_waiting_by_dlg.items():
        if dlg_id in matched_waiting_dlgs:
            continue
        sender_acc = w["sender_account"]
        waiting_by_account[sender_acc] += 1
        waiting_items.append(
            {
                "lead_id": w.get("lead_id"),
                "dialog_id": dlg_id,
                "account_id": w.get("account_id"),
                "sender_account": sender_acc,
                "account_is_dropped": bool(w.get("account_is_dropped")),
                "account_health": w.get("account_health") or "unknown",
                "username": w.get("username"),
                "name": w.get("name"),
                "company": w.get("company") or "",
                "website": "",
                "category": "CRM",
                "niche": "CRM",
                "hook_format": "Прямой ответ",
                "replied_to_step": "step1_personal_pitch",
                "replied_to_step_label": "Шаг 1",
                "reply_intent": w.get("reply_intent") or "neutral_or_question",
                "reply_intent_label": INTENT_LABELS_RU.get(
                    w.get("reply_intent") or "neutral_or_question",
                    "Входящее сообщение",
                ),
                "reply_text": w.get("reply_text") or "",
                "step1_text": "",
                "last_touch_at": w.get("last_touch_at") or "",
                "fine_status": "replied_waiting_our_answer",
            }
        )

    # Sort waiting_items so positive_interested comes first
    waiting_items.sort(key=lambda x: (0 if x["reply_intent"] == "positive_interested" else 1, x["sender_account"]))
    stalled_items.sort(key=lambda x: (0 if x["reply_intent"] == "positive_interested" else 1, x["sender_account"]))
    positive_items.sort(key=lambda x: (0 if x["fine_status"] == "replied_waiting_our_answer" else 1, x["sender_account"]))

    # Step conversion breakdown
    s1_reached = len(contacted_recs)
    s1_read = sum(1 for r in contacted_recs if r.get("_is_read"))
    s1_rep_list = [r for r in replied_recs if r.get("_rep_step", "").startswith("step1") or r.get("_rep_step") in ("inbound_first", "empty", "")]
    s1_pos = sum(1 for r in s1_rep_list if r.get("_intent") == "positive_interested")
    s1_constr = sum(1 for r in s1_rep_list if r.get("_intent") in ("positive_interested", "neutral_or_question"))

    s2_reached_list = [
        r for r in contacted_recs
        if (int(r.get("current_step") or 0) >= 2 and r not in s1_rep_list)
        or str(r.get("_rep_step") or "").startswith("step2")
        or str(r.get("_rep_step") or "").startswith("step3")
    ]
    s2_reached = len(s2_reached_list)
    s2_rep_list = [r for r in replied_recs if str(r.get("_rep_step") or "").startswith("step2")]
    s2_pos = sum(1 for r in s2_rep_list if r.get("_intent") == "positive_interested")
    s2_constr = sum(1 for r in s2_rep_list if r.get("_intent") in ("positive_interested", "neutral_or_question"))

    s3_reached_list = [
        r for r in contacted_recs
        if (int(r.get("current_step") or 0) >= 3 and r not in s1_rep_list and r not in s2_rep_list)
        or str(r.get("_rep_step") or "").startswith("step3")
    ]
    s3_reached = len(s3_reached_list)
    s3_rep_list = [r for r in replied_recs if str(r.get("_rep_step") or "").startswith("step3")]
    s3_pos = sum(1 for r in s3_rep_list if r.get("_intent") == "positive_interested")
    s3_constr = sum(1 for r in s3_rep_list if r.get("_intent") in ("positive_interested", "neutral_or_question"))

    steps_breakdown = [
        {
            "step_order": 1,
            "key": "step1_personal_pitch",
            "title": "Шаг 1: Персонализированный питч (SoftUGC)",
            "delay_label": "Сразу (0 ч)",
            "reached": s1_reached,
            "read": s1_read,
            "replied": len(s1_rep_list),
            "reply_rate_pct": round(len(s1_rep_list) / max(1, s1_reached) * 100, 1),
            "positive": s1_pos,
            "positive_of_reached_pct": round(s1_pos / max(1, s1_reached) * 100, 1),
            "positive_of_replied_pct": round(s1_pos / max(1, len(s1_rep_list)) * 100, 1),
            "constructive": s1_constr,
            "constructive_pct": round(s1_constr / max(1, len(s1_rep_list)) * 100, 1),
            "template_text": "Hey, Saw {company}. At SoftUGC we run AI UGC production and a multi-account short-form network — 183.5M organic views in 90 days without live shoots...",
        },
        {
            "step_order": 2,
            "key": "step2_xplabs_case",
            "title": "Шаг 2: Фоллоу-ап — Кейс XPLabs (1.2M кликов / 80M просмотров + видео MP4)",
            "delay_label": "Через 48 ч",
            "reached": s2_reached,
            "read": sum(1 for r in s2_reached_list if r.get("_is_read")),
            "replied": len(s2_rep_list),
            "reply_rate_pct": round(len(s2_rep_list) / max(1, s2_reached) * 100, 1),
            "positive": s2_pos,
            "positive_of_reached_pct": round(s2_pos / max(1, s2_reached) * 100, 1),
            "positive_of_replied_pct": round(s2_pos / max(1, len(s2_rep_list)) * 100, 1),
            "constructive": s2_constr,
            "constructive_pct": round(s2_constr / max(1, len(s2_rep_list)) * 100, 1),
            "template_text": "Hey, last month we drove 1.2M clicks through tracked links and 80M views on a web3 gaming campaign for XPlabs — here's one of the top-performing creatives we produced for them. [+ MP4 видео-креатив]",
        },
        {
            "step_order": 3,
            "key": "step3_revshare_cpa",
            "title": "Шаг 3: Фоллоу-ап — Оффер Revshare и CPA (Оплата за результат)",
            "delay_label": "Через 96 ч (4 дня)",
            "reached": s3_reached,
            "read": sum(1 for r in s3_reached_list if r.get("_is_read")),
            "replied": len(s3_rep_list),
            "reply_rate_pct": round(len(s3_rep_list) / max(1, s3_reached) * 100, 1),
            "positive": s3_pos,
            "positive_of_reached_pct": round(s3_pos / max(1, s3_reached) * 100, 1),
            "positive_of_replied_pct": round(s3_pos / max(1, len(s3_rep_list)) * 100, 1),
            "constructive": s3_constr,
            "constructive_pct": round(s3_constr / max(1, len(s3_rep_list)) * 100, 1),
            "template_text": "Hello! Are you interested in organic promotion of your product? We work almost exclusively on revshare and CPA, so our main income comes from what you earn.",
        },
    ]

    hooks_list = []
    for h_name, st in sorted(hook_stats.items(), key=lambda x: -x[1]["pos"] / max(1, x[1]["contacted"])):
        c = st["contacted"]
        rd = st["read"]
        rep = st["replied"]
        pos = st["pos"]
        constr = st["constr"]
        hooks_list.append(
            {
                "hook": h_name,
                "contacted": c,
                "read": rd,
                "open_rate_pct": round(rd / max(1, c) * 100, 1),
                "replied": rep,
                "reply_rate_pct": round(rep / max(1, c) * 100, 1),
                "positive": pos,
                "positive_of_contacted_pct": round(pos / max(1, c) * 100, 1),
                "positive_of_replied_pct": round(pos / max(1, rep) * 100, 1),
                "constructive": constr,
                "constructive_pct": round(constr / max(1, rep) * 100, 1),
                "refusal": st["refusal"],
                "sample_text": st["sample_text"][:320],
            }
        )

    categories_list = []
    for c_name, st in sorted(cat_stats.items(), key=lambda x: (-x[1]["pos"] / max(1, x[1]["contacted"]), -x[1]["contacted"])):
        c = st["contacted"]
        rd = st["read"]
        rep = st["replied"]
        pos = st["pos"]
        constr = st["constr"]
        categories_list.append(
            {
                "category": c_name,
                "contacted": c,
                "read": rd,
                "open_rate_pct": round(rd / max(1, c) * 100, 1),
                "replied": rep,
                "reply_rate_pct": round(rep / max(1, c) * 100, 1),
                "positive": pos,
                "positive_of_contacted_pct": round(pos / max(1, c) * 100, 1),
                "positive_of_replied_pct": round(pos / max(1, rep) * 100, 1),
                "constructive": constr,
                "constructive_pct": round(constr / max(1, rep) * 100, 1),
                "refusal": st["refusal"],
                "waiting": st["waiting"],
                "examples": st["examples"],
            }
        )

    niches_list = []
    for n_name, st in sorted(niche_stats.items(), key=lambda x: (-x[1]["pos"] / max(1, x[1]["contacted"]), -x[1]["contacted"])):
        c = st["contacted"]
        rd = st["read"]
        rep = st["replied"]
        pos = st["pos"]
        constr = st["constr"]
        niches_list.append(
            {
                "niche": n_name,
                "contacted": c,
                "read": rd,
                "open_rate_pct": round(rd / max(1, c) * 100, 1),
                "replied": rep,
                "reply_rate_pct": round(rep / max(1, c) * 100, 1),
                "positive": pos,
                "positive_of_contacted_pct": round(pos / max(1, c) * 100, 1),
                "positive_of_replied_pct": round(pos / max(1, rep) * 100, 1),
                "constructive": constr,
                "constructive_pct": round(constr / max(1, rep) * 100, 1),
                "refusal": st["refusal"],
                "waiting": st["waiting"],
                "examples": st["examples"],
            }
        )

    total_contacted = len(contacted_recs)
    total_read = sum(1 for r in contacted_recs if r.get("_is_read"))
    total_replied = len(replied_recs)
    pos_total = intent_counts.get("positive_interested", 0)
    q_total = intent_counts.get("neutral_or_question", 0)
    waiting_total = len(waiting_items)
    waiting_on_healthy = sum(1 for x in waiting_items if not x.get("account_is_dropped"))
    waiting_on_dropped = sum(1 for x in waiting_items if x.get("account_is_dropped"))
    unseen_total = max(0, total_contacted - total_read)

    rec_by_lead_id = ctx.get("rec_by_lead_id", {})
    rec_by_peer = ctx.get("rec_by_peer", {})
    rec_by_user = ctx.get("rec_by_user", {})
    rec_by_phone = ctx.get("rec_by_phone", {})
    direct_client_replies = 0
    for did in sorted(ctx["eligible_crm_dialog_ids"]):
        if did in matched_rec_dlg_ids:
            continue
        dlg = ctx["dialogs_by_id"].get(did)
        if dlg is None:
            continue
        matched_lead = _resolve_dialog_lead(dlg, ctx)
        if matched_lead is None:
            continue
        if str(matched_lead.get("next_step") or "").startswith("Авто-карантин Sniper ICP"):
            continue
        dlid = int(dlg["lead_id"]) if dlg.get("lead_id") is not None else None
        peer_id = int(dlg["peer_id"]) if dlg.get("peer_id") is not None else None
        uname = _norm_username(dlg.get("username"))
        ph15 = _norm_phone_15(dlg.get("phone"))
        l_peer = (
            int(matched_lead["telegram_user_id"])
            if matched_lead.get("telegram_user_id") is not None
            else None
        )
        l_uname = _norm_username(matched_lead.get("username"))
        l_ph15 = _norm_phone_15(matched_lead.get("phone"))
        if (
            int(matched_lead["id"]) in rec_by_lead_id
            or (dlid is not None and dlid in rec_by_lead_id)
            or (peer_id is not None and peer_id in rec_by_peer)
            or (l_peer is not None and l_peer in rec_by_peer)
            or (uname and uname in rec_by_user)
            or (l_uname and l_uname in rec_by_user)
            or (ph15 and ph15 in rec_by_phone)
            or (l_ph15 and l_ph15 in rec_by_phone)
        ):
            continue
        msum = msg_summary_map.get(did)
        if int((msum or {}).get("in_count") or 0) > 0 or int(dlg.get("last_from_peer") or 0) == 1:
            direct_client_replies += 1

    return {
        "summary": {
            "total_base": total_base,
            "contacted": total_contacted,
            "read": total_read,
            "open_rate_pct": round(total_read / max(1, total_contacted) * 100, 1),
            "replied": total_replied,
            "direct_client_replies": direct_client_replies,
            "reply_rate_pct": round(total_replied / max(1, total_contacted) * 100, 1),
            "reply_of_read_pct": round(total_replied / max(1, total_read) * 100, 1),
            "positive_count": pos_total,
            "positive_pct": round(pos_total / max(1, total_replied) * 100, 1),
            "question_count": q_total,
            "constructive_count": pos_total + q_total,
            "constructive_pct": round((pos_total + q_total) / max(1, total_replied) * 100, 1),
            "defer_count": intent_counts.get("defer_contact_later", 0),
            "refusal_count": intent_counts.get("refusal_not_interested", 0),
            "waiting_our_answer": waiting_total,
            "waiting_on_healthy_accounts": waiting_on_healthy,
            "waiting_on_dropped_accounts": waiting_on_dropped,
            "waiting_positive": sum(1 for x in waiting_items if x["reply_intent"] == "positive_interested"),
            "waiting_question": sum(1 for x in waiting_items if x["reply_intent"] != "positive_interested"),
            "stalled_after_answer": fine_counts.get("replied_we_answered_seen_stalled", 0),
            "unseen_our_answer": fine_counts.get("replied_we_answered_unseen", 0),
            "need_step2": fine_counts.get("seen_step1_no_reply_need_followup2", 0),
            "need_step3": fine_counts.get("seen_step2_no_reply_need_followup3", 0),
            "seen_all_3_ghosted": fine_counts.get("seen_all_3_steps_ghosted", 0),
            "unseen_total": unseen_total,
            "pending_queue": pending_queue_real,
            "pending_draft": pending_draft_real,
            "failed_total": failed_real,
            "in_progress_followup": sum(1 for r in unique_recs if r.get("status") == "in_progress"),
            "waiting_by_account": dict(sorted(waiting_by_account.items(), key=lambda x: -x[1])),
        },
        "steps_breakdown": steps_breakdown,
        "hooks_breakdown": hooks_list,
        "categories_breakdown": categories_list,
        "niches_breakdown": niches_list,
        "waiting_items": waiting_items,
        "stalled_items": stalled_items,
        "positive_items": positive_items,
    }


def get_analytics_summary(conn: sqlite3.Connection, period: str = "30d") -> dict:
    clean_period = str(period or "30d").strip().lower()
    if clean_period not in VALID_PERIODS:
        clean_period = "30d"

    _sync_running_and_all_campaign_replies(conn)
    ctx = _load_ssot_crm_context(conn)
    funnel = _compute_funnel_analytics(conn)
    outreach = _compute_outreach_analytics(conn, ctx=ctx)
    accounts = _compute_accounts_analytics(conn, ctx=ctx)
    charts = _compute_charts_analytics(
        conn,
        period=clean_period,
        accounts_analytics=accounts,
        ctx=ctx,
    )
    deep_outreach = _compute_outreach_deep_breakdown(conn, ctx=ctx)

    return {
        "period": clean_period,
        "funnel": funnel,
        "outreach": outreach,
        "accounts": accounts,
        "charts": charts,
        "deep_outreach": deep_outreach,
        "pipeline_value": funnel["pipeline_value"],
        "won_value": funnel["won_value"],
        "won_revenue": funnel["won_value"],
        "lost_value": funnel["lost_value"],
        "total_value": funnel["total_value"],
        "monthly_value": funnel["monthly_value"],
        "win_rate_pct": funnel["win_rate_pct"],
        "reply_rate_pct": outreach["reply_rate_pct"],
    }
