import json
import re
import sqlite3
from datetime import date, timedelta

from app.db import as_dict, utc_now, utc_today

STAGE_LABELS = {
    "queued": "В очереди / Не отправлено",
    "sent_unread": "Отправлено · Не прочитано",
    "read_no_reply": "Прочитано · Без ответа",
    "replied_no_call": "Ответил · Без созвона",
    "positive_or_call": "Интерес / Созвон / Сделка",
}

STAGE_ORDER = (
    "queued",
    "sent_unread",
    "read_no_reply",
    "replied_no_call",
    "positive_or_call",
)

VARIANT_LABELS = {
    "sonnet5_vlad_v1": "SoftUGC · Sonnet 5 (новый)",
    "vlad_softugc_v1": "SoftUGC · Шаблон Влада",
    "legacy_creatoros": "CreatorOS (старый архив)",
    "legacy_ceiling_hook": "Growth Ceiling (старый архив)",
    "custom_other": "Прочие шаблоны",
}

_POSITIVE_LEAD_STAGES = frozenset(
    {
        "qualified",
        "interested",
        "call_scheduled",
        "proposal",
        "won",
        "signed",
        "offer sent",
        "call scheduled",
    }
)

_TARGET_CATEGORY_PATTERNS = re.compile(
    r"(?:\bsaas\b|\bai\b|\bweb3\b|crypto|крипт|\bdefi\b|\bcex\b|\bdex\b|"
    r"agency|агентств|e-?com|ecommerce|маркетплейс|edtech|онлайн-школ|"
    r"\bapp\b|mobile\s*app|fintech|финтех|game|gaming|gamedev|igaming|"
    r"\bb2b\b|devtools|\bпроп\b|\bprop\b)",
    re.IGNORECASE,
)

_VALID_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")

_NEGATIVE_REPLY_MARKERS = (
    "no thanks",
    "no, thanks",
    "not interested",
    "don't need",
    "dont need",
    "unsubscribe",
    "не актуально",
    "неактуально",
    "не интересно",
    "неинтересно",
    "не надо",
    "спасибо, нет",
    "нет, спасибо",
)

_NEGATIVE_REPLY_WORDS_RE = re.compile(r"\b(?:stop|pass)\b", re.IGNORECASE)

_PRICING_MARKERS = (
    "how much",
    "сколько стоит",
    "прайс",
    "стоимост",
)
_PRICING_WORDS_RE = re.compile(r"\b(?:price|pricing|cost|budget|rate|цена|условия)\b", re.IGNORECASE)

_DEMO_REQUEST_MARKERS = (
    "материал",
    "пример",
    "кейс",
    "портфолио",
    "подробнее",
    "презентац",
)
_DEMO_REQUEST_WORDS_RE = re.compile(
    r"\b(?:materials?|examples?|cases?|portfolio|deck|details)\b",
    re.IGNORECASE,
)

_POSITIVE_SIGNAL_PHRASES = (
    "let's talk",
    "lets talk",
    "calendly",
    "cal.com",
    "скинь",
    "давай",
    "интересно",
    "покажи",
)
_POSITIVE_SIGNAL_WORDS_RE = re.compile(
    r"\b(?:send|demo|reels|sure|yes|interested|call)\b",
    re.IGNORECASE,
)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def ensure_funnel_columns(conn: sqlite3.Connection) -> None:
    changed = False
    dialog_cols = _table_columns(conn, "dialogs")
    if dialog_cols and "read_outbox_max_id" not in dialog_cols:
        conn.execute("ALTER TABLE dialogs ADD COLUMN read_outbox_max_id INTEGER DEFAULT 0")
        changed = True

    rec_cols = _table_columns(conn, "campaign_recipients")
    if rec_cols:
        wanted_rec_cols = (
            ("read_at", "TEXT"),
            ("variant_key", "TEXT"),
            ("pain_angle", "TEXT"),
            ("quality_score", "INTEGER"),
            ("added_date", "TEXT"),
        )
        for col_name, col_type in wanted_rec_cols:
            if col_name not in rec_cols:
                conn.execute(f"ALTER TABLE campaign_recipients ADD COLUMN {col_name} {col_type}")
                changed = True

    if changed:
        conn.commit()


def is_vacancy_campaign_name(name: str | None) -> bool:
    low = str(name or "").lower()
    return "ваканс" in low or "vacanc" in low


def _clean_username(raw: str | None) -> str:
    val = str(raw or "").strip()
    if not val:
        return ""
    val = re.sub(r"^https?://(?:www\.)?t\.me/", "", val, flags=re.IGNORECASE)
    val = val.lstrip("@").strip().strip("/")
    return val


def _parse_extra(raw_json: str | None) -> dict:
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _first_nonempty(*values) -> str:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("-", "none", "null", "n/a", "nan", "нет"):
            return s
    return ""


def _has_valid_website(raw_site: str) -> bool:
    if not raw_site or "." not in raw_site:
        return False
    low = raw_site.lower()
    if low.startswith(("t.me/", "https://t.me/", "http://t.me/", "@")):
        return False
    return True


def apply_read_outbox_update(
    conn: sqlite3.Connection,
    account_id: int | None,
    peer_id: int,
    max_id: int,
    read_at: str | None = None,
    commit: bool = True,
) -> int:
    ensure_funnel_columns(conn)
    try:
        peer_int = int(peer_id)
        max_int = int(max_id)
    except (TypeError, ValueError):
        return 0
    if peer_int <= 0 or max_int <= 0:
        return 0

    stamp = read_at or utc_now()
    if account_id is not None:
        conn.execute(
            """
            UPDATE dialogs
            SET read_outbox_max_id = CASE
                    WHEN COALESCE(read_outbox_max_id, 0) < ? THEN ?
                    ELSE COALESCE(read_outbox_max_id, 0)
                END,
                updated_at = ?
            WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
            """,
            (max_int, max_int, stamp, int(account_id), peer_int),
        )
        dlg_rows = conn.execute(
            """
            SELECT id, account_id, peer_id, username, lead_id, COALESCE(read_outbox_max_id, 0) AS read_outbox_max_id
            FROM dialogs
            WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
            """,
            (int(account_id), peer_int),
        ).fetchall()
    else:
        conn.execute(
            """
            UPDATE dialogs
            SET read_outbox_max_id = CASE
                    WHEN COALESCE(read_outbox_max_id, 0) < ? THEN ?
                    ELSE COALESCE(read_outbox_max_id, 0)
                END,
                updated_at = ?
            WHERE peer_type = 'user' AND peer_id = ?
            """,
            (max_int, max_int, stamp, peer_int),
        )
        dlg_rows = conn.execute(
            """
            SELECT id, account_id, peer_id, username, lead_id, COALESCE(read_outbox_max_id, 0) AS read_outbox_max_id
            FROM dialogs
            WHERE peer_type = 'user' AND peer_id = ?
            """,
            (peer_int,),
        ).fetchall()

    updated_recipients = 0
    for dlg in dlg_rows:
        dlg_id = int(dlg["id"])
        eff_max = max(max_int, int(dlg["read_outbox_max_id"] or 0))
        out_row = conn.execute(
            """
            SELECT COALESCE(MAX(telegram_message_id), 0) AS max_out_id
            FROM messages
            WHERE dialog_id = ? AND direction = 'out' AND telegram_message_id > 0
            """,
            (dlg_id,),
        ).fetchone()
        last_out_id = int(out_row["max_out_id"] or 0) if out_row else 0
        if last_out_id <= 0 or eff_max < last_out_id:
            continue

        clauses = ["telegram_user_id = ?"]
        params: list = [stamp, stamp, peer_int]
        if dlg["lead_id"] is not None:
            clauses.append("lead_id = ?")
            params.append(int(dlg["lead_id"]))
        clean_un = _clean_username(dlg["username"])
        if clean_un:
            clauses.append("lower(username) = lower(?)")
            params.append(clean_un)

        match_sql = " OR ".join(clauses)
        cur = conn.execute(
            f"""
            UPDATE campaign_recipients
            SET read_at = COALESCE(read_at, ?),
                updated_at = ?
            WHERE read_at IS NULL
              AND (current_step >= 1 OR last_sent_at IS NOT NULL)
              AND ({match_sql})
            """,
            params,
        )
        updated_recipients += int(cur.rowcount or 0)

    if commit:
        conn.commit()
    return updated_recipients


def sync_dialog_read_receipts(
    conn: sqlite3.Connection,
    account_id: int | None = None,
    people: list[dict] | None = None,
) -> int:
    ensure_funnel_columns(conn)
    updated = 0
    if people is not None:
        for item in people:
            if not isinstance(item, dict):
                continue
            peer_id = item.get("telegram_user_id") or item.get("peer_id")
            max_id = int(item.get("read_outbox_max_id") or 0)
            if peer_id is None or max_id <= 0:
                continue
            updated += apply_read_outbox_update(
                conn,
                account_id,
                int(peer_id),
                max_id,
                commit=False,
            )
        conn.commit()
        return updated

    sql = """
        SELECT account_id, peer_id, COALESCE(read_outbox_max_id, 0) AS read_outbox_max_id
        FROM dialogs
        WHERE peer_type = 'user' AND COALESCE(read_outbox_max_id, 0) > 0
    """
    params: tuple = ()
    if account_id is not None:
        sql += " AND account_id = ?"
        params = (int(account_id),)
    for row in conn.execute(sql, params).fetchall():
        updated += apply_read_outbox_update(
            conn,
            int(row["account_id"]),
            int(row["peer_id"]),
            int(row["read_outbox_max_id"]),
            commit=False,
        )
    conn.commit()
    return updated


def compute_icp_quality(
    *,
    website: str,
    their_angle: str,
    username: str,
    category: str,
    step1_text: str,
    explicit_score: int | None = None,
) -> tuple[int, str]:
    if explicit_score is not None:
        score = max(0, min(100, int(explicit_score)))
    else:
        score = 0
        if _has_valid_website(website):
            score += 25
        if len(their_angle.strip()) >= 25:
            score += 25
        clean_un = _clean_username(username)
        if clean_un and _VALID_HANDLE_RE.fullmatch(clean_un):
            score += 20
        if category and _TARGET_CATEGORY_PATTERNS.search(category):
            score += 15
        s1_low = (step1_text or "").lower()
        if "softugc" in s1_low and "183.5m" in s1_low and "revshare or cpa" in s1_low:
            score += 15

    if score >= 80:
        tier = "A"
    elif score >= 60:
        tier = "B"
    else:
        tier = "C"
    return score, tier


def resolve_variant_and_pain(
    *,
    explicit_variant_key: str | None,
    explicit_pain_angle: str | None,
    step1_text: str,
    category: str,
    hook: str,
    extra: dict,
) -> tuple[str, str, str]:
    raw_vk = str(explicit_variant_key or extra.get("variant_key") or "").strip()
    s1 = step1_text or ""
    s1_low = s1.lower()

    if raw_vk:
        v_key = raw_vk
        v_label = VARIANT_LABELS.get(v_key, v_key)
    elif "softugc" in s1_low and "revshare or cpa" in s1_low:
        v_key = "vlad_softugc_v1"
        v_label = VARIANT_LABELS["vlad_softugc_v1"]
    elif "creatoros" in s1_low:
        v_key = "legacy_creatoros"
        v_label = VARIANT_LABELS["legacy_creatoros"]
    elif "if your user growth has hit a ceiling" in s1_low:
        v_key = "legacy_ceiling_hook"
        v_label = VARIANT_LABELS["legacy_ceiling_hook"]
    else:
        v_key = "custom_other"
        v_label = VARIANT_LABELS["custom_other"]

    raw_pain = _first_nonempty(explicit_pain_angle, extra.get("pain_angle"), extra.get("hook_format"))
    if raw_pain:
        pain_angle = raw_pain[:90]
    elif "13-40x" in s1_low or "expensive initial reach" in s1_low:
        pain_angle = "Дорогой охват → AI UGC сеть (RevShare/CPA)"
    elif "if your user growth has hit a ceiling" in s1_low or "cac" in s1_low:
        pain_angle = "Потолок роста / высокий CAC"
    elif "organic ig/tiktok reels" in s1_low:
        pain_angle = "Органика Reels / TikTok под продукт"
    elif "revshare" in s1_low or "cpa" in s1_low:
        pain_angle = "Performance органика (RevShare / CPA)"
    elif hook and len(hook.strip()) <= 80:
        pain_angle = hook.strip()
    elif category:
        pain_angle = f"Аутрич · {category[:45]}"
    else:
        pain_angle = "Прямой B2B-аутрич"

    return v_key, v_label, pain_angle


def _is_negative_reply(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False
    if any(m in low for m in _NEGATIVE_REPLY_MARKERS):
        return True
    return bool(_NEGATIVE_REPLY_WORDS_RE.search(low))


def _is_pricing_reply(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False
    if any(m in low for m in _PRICING_MARKERS):
        return True
    return bool(_PRICING_WORDS_RE.search(low))


def _is_demo_request_reply(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False
    if any(m in low for m in _DEMO_REQUEST_MARKERS):
        return True
    return bool(_DEMO_REQUEST_WORDS_RE.search(low))


def _has_positive_reply_signal(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False
    if _is_negative_reply(low):
        return False
    if any(
        neg in low
        for neg in (
            "don't send",
            "dont send",
            "do not send",
            "no call",
            "not sure",
            "не скидывай",
            "не присылай",
        )
    ):
        return False
    # Pure pricing inquiry without explicit call/yes signal stays in replied_no_call (pricing_question)
    has_explicit_call_or_yes = (
        any(p in low for p in _POSITIVE_SIGNAL_PHRASES)
        or bool(_POSITIVE_SIGNAL_WORDS_RE.search(low))
    )
    if not has_explicit_call_or_yes:
        return False
    if _is_pricing_reply(low) and not any(
        k in low
        for k in ("calendly", "cal.com", "let's talk", "lets talk", "sure", "yes", "давай", "интересно", "reels", "demo")
    ):
        return False
    return True


def _diagnose_lead(
    *,
    stage: str,
    status: str,
    error_text: str | None,
    current_step: int,
    dialog_read_outbox_max_id: int,
    last_inbound_text: str,
) -> tuple[str, str]:
    step_num = max(1, int(current_step or 1))
    if stage == "queued":
        if status == "failed":
            err = (error_text or "").strip() or "Ошибка отправки"
            return "send_failed", err
        return "queued_ready", "Готов к запуску (персонализированный аутрич Step 1 в очереди)"

    if stage == "sent_unread":
        if int(dialog_read_outbox_max_id or 0) <= 0:
            return (
                "unread_mtproto_pending",
                f"Отправлен Step {step_num} · нет ответа (ждёт прочтения / синхронизации MTProto)",
            )
        return "sent_not_opened", f"Отправлен Step {step_num}, лид ещё не открыл сообщение"

    if stage == "read_no_reply":
        if int(current_step or 1) < 3:
            return (
                "read_awaiting_followup",
                f"Прочитал Step {step_num}, не ответил — по воронке дожимается следующим фоллоу-апом",
            )
        return (
            "read_sequence_exhausted",
            "Прочитал всю цепочку (3 касания) без ответа — угол боли не зацепил",
        )

    if stage == "replied_no_call":
        if _is_negative_reply(last_inbound_text):
            return "declined", "Отказ: написал, что сейчас неактуально"
        if _is_pricing_reply(last_inbound_text):
            return "pricing_question", "Спросил про стоимость/условия, но не закрыт на созвон"
        if _is_demo_request_reply(last_inbound_text):
            return (
                "awaiting_demo",
                "Запросил 3 демо-рилса / примеры — отправить рилсы и предложить слот",
            )
        return (
            "stalled_after_reply",
            "Ответил в диалоге, но диалог остановился до назначения созвона — нужен ручной фоллоу-ап",
        )

    return "positive_or_call", "Целевой лид: проявил интерес / обсуждение демо или созвона"


def _pct(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return round((num / den) * 100.0, 1)


def get_outreach_funnel(
    conn: sqlite3.Connection,
    campaign_id: int | None = None,
    days: int = 14,
    stage: str = "all",
    variant: str = "all",
    q: str = "",
    limit: int = 150,
) -> dict:
    ensure_funnel_columns(conn)

    all_camp_rows = conn.execute(
        "SELECT id, name, status, account_id FROM campaigns ORDER BY id DESC"
    ).fetchall()
    campaigns_meta = []
    camp_by_id: dict[int, dict] = {}
    for row in all_camp_rows:
        cd = as_dict(row)
        cid = int(cd["id"])
        is_b2b = not is_vacancy_campaign_name(cd.get("name"))
        item = {
            "id": cid,
            "name": cd.get("name") or f"Кампания #{cid}",
            "status": cd.get("status") or "draft",
            "account_id": cd.get("account_id"),
            "is_b2b": is_b2b,
        }
        campaigns_meta.append(item)
        camp_by_id[cid] = item

    if campaign_id is not None:
        selected_cids = [int(campaign_id)] if int(campaign_id) in camp_by_id else []
    else:
        selected_cids = [c["id"] for c in campaigns_meta if c["is_b2b"]]

    step1_tmpl_by_camp: dict[int, str] = {}
    if selected_cids:
        ph = ",".join("?" for _ in selected_cids)
        for srow in conn.execute(
            f"SELECT campaign_id, step_order, template_text FROM campaign_steps WHERE campaign_id IN ({ph}) AND step_order = 1",
            selected_cids,
        ).fetchall():
            step1_tmpl_by_camp[int(srow["campaign_id"])] = str(srow["template_text"] or "")

    # Preload calendar events linked to leads
    calendar_lead_ids: set[int] = set()
    try:
        for crow in conn.execute(
            "SELECT DISTINCT lead_id FROM calendar_events WHERE lead_id IS NOT NULL AND COALESCE(status, 'scheduled') != 'cancelled'"
        ).fetchall():
            if crow["lead_id"] is not None:
                calendar_lead_ids.add(int(crow["lead_id"]))
    except sqlite3.OperationalError:
        pass

    # Preload user dialogs & message summaries in two linear passes (no N+1 queries)
    dialogs_by_id: dict[int, dict] = {}
    dialogs_by_lead_id: dict[int, list[dict]] = {}
    dialogs_by_peer_id: dict[int, list[dict]] = {}
    dialogs_by_uname: dict[str, list[dict]] = {}
    dialogs_by_phone: dict[str, list[dict]] = {}

    for drow in conn.execute(
        """
        SELECT id, account_id, peer_id, username, phone, lead_id,
               last_from_peer, last_message_at, last_message_preview,
               COALESCE(read_outbox_max_id, 0) AS read_outbox_max_id
        FROM dialogs
        WHERE peer_type = 'user'
        ORDER BY last_message_at DESC, id DESC
        """
    ).fetchall():
        dd = as_dict(drow)
        did = int(dd["id"])
        dialogs_by_id[did] = dd
        if dd.get("lead_id") is not None:
            dialogs_by_lead_id.setdefault(int(dd["lead_id"]), []).append(dd)
        if dd.get("peer_id") is not None:
            dialogs_by_peer_id.setdefault(int(dd["peer_id"]), []).append(dd)
        un = _clean_username(dd.get("username")).lower()
        if un:
            dialogs_by_uname.setdefault(un, []).append(dd)
        ph_str = str(dd.get("phone") or "").strip()
        if ph_str:
            dialogs_by_phone.setdefault(ph_str, []).append(dd)

    msg_stats_by_dlg: dict[int, dict] = {}
    if dialogs_by_id:
        for mrow in conn.execute(
            """
            SELECT dialog_id, direction, telegram_message_id, text, sent_at
            FROM messages
            ORDER BY sent_at ASC, id ASC
            """
        ).fetchall():
            did = int(mrow["dialog_id"])
            if did not in dialogs_by_id:
                continue
            st = msg_stats_by_dlg.get(did)
            if st is None:
                st = {
                    "first_out_at": None,
                    "last_out_at": None,
                    "last_out_msg_id": 0,
                    "last_out_text": None,
                    "last_in_text": None,
                    "last_in_after_out_text": None,
                    "has_in": False,
                    "has_in_after_out": False,
                }
                msg_stats_by_dlg[did] = st
            direction = mrow["direction"]
            sent_at = str(mrow["sent_at"] or "")
            txt = str(mrow["text"] or "").strip()
            tg_mid = int(mrow["telegram_message_id"] or 0)
            if direction == "out":
                if st["first_out_at"] is None:
                    st["first_out_at"] = sent_at
                st["last_out_at"] = sent_at
                if tg_mid > st["last_out_msg_id"]:
                    st["last_out_msg_id"] = tg_mid
                if txt:
                    st["last_out_text"] = txt
            elif direction == "in":
                st["has_in"] = True
                if txt:
                    st["last_in_text"] = txt
                if st["first_out_at"] is not None and sent_at >= st["first_out_at"]:
                    st["has_in_after_out"] = True
                    if txt:
                        st["last_in_after_out_text"] = txt

    rec_rows = []
    if selected_cids:
        ph = ",".join("?" for _ in selected_cids)
        rec_rows = conn.execute(
            f"""
            SELECT cr.*,
                   l.status AS lead_status,
                   l.url AS lead_url,
                   l.niche AS lead_niche,
                   l.lead_source AS lead_source_col,
                   l.company AS lead_company,
                   l.dialog_id AS lead_dialog_id
            FROM campaign_recipients cr
            LEFT JOIN leads l ON l.id = cr.lead_id
            WHERE cr.campaign_id IN ({ph})
            ORDER BY cr.id DESC
            """,
            selected_cids,
        ).fetchall()

    today_str = utc_today()
    try:
        today_dt = date.fromisoformat(today_str)
    except ValueError:
        today_dt = date.today()
        today_str = today_dt.isoformat()
    date_7d_cutoff = (today_dt - timedelta(days=6)).isoformat()
    safe_days = max(1, min(int(days or 14), 365))
    intake_cutoff = (today_dt - timedelta(days=safe_days - 1)).isoformat()

    evaluated: list[dict] = []

    for r in rec_rows:
        rd = as_dict(r)
        cid = int(rd["campaign_id"])
        camp_info = camp_by_id.get(cid, {})
        extra = _parse_extra(rd.get("extra_json"))

        username = _clean_username(rd.get("username"))
        company = _first_nonempty(
            rd.get("company"),
            rd.get("lead_company"),
            extra.get("company"),
            extra.get("Компания"),
            rd.get("name"),
        )
        website = _first_nonempty(
            rd.get("website"),
            extra.get("website"),
            extra.get("Сайт"),
            rd.get("lead_url"),
        )
        category = _first_nonempty(
            rd.get("category"),
            rd.get("lead_niche"),
            extra.get("category"),
            extra.get("Категория"),
            extra.get("niche"),
        )
        their_angle = _first_nonempty(
            rd.get("their_angle"),
            extra.get("their_angle"),
            extra.get("Их угол"),
            extra.get("what_they_do"),
        )
        source = _first_nonempty(
            rd.get("lead_source_col"),
            extra.get("source"),
            extra.get("lead_source"),
            extra.get("Источник"),
            camp_info.get("name"),
        )
        added_date = _first_nonempty(
            rd.get("added_date"),
            str(rd.get("created_at") or "")[:10],
        ) or today_str

        # Match linked dialogs (deduplicated, prioritizing assigned/campaign account)
        pref_acc = rd.get("assigned_account_id") or camp_info.get("account_id")
        candidate_dlgs: list[dict] = []
        seen_dids: set[int] = set()

        def _add_dlgs(items: list[dict] | None) -> None:
            if not items:
                return
            for item in items:
                did = int(item["id"])
                if did not in seen_dids:
                    seen_dids.add(did)
                    candidate_dlgs.append(item)

        if rd.get("lead_dialog_id") and int(rd["lead_dialog_id"]) in dialogs_by_id:
            _add_dlgs([dialogs_by_id[int(rd["lead_dialog_id"])]])
        if rd.get("lead_id") is not None:
            _add_dlgs(dialogs_by_lead_id.get(int(rd["lead_id"])))
        if rd.get("telegram_user_id") is not None:
            _add_dlgs(dialogs_by_peer_id.get(int(rd["telegram_user_id"])))
        if username:
            _add_dlgs(dialogs_by_uname.get(username.lower()))
        if rd.get("phone"):
            _add_dlgs(dialogs_by_phone.get(str(rd["phone"]).strip()))

        if pref_acc is not None and len(candidate_dlgs) > 1:
            pref_int = int(pref_acc)
            candidate_dlgs.sort(key=lambda d: 0 if int(d["account_id"]) == pref_int else 1)

        primary_dialog_id = int(candidate_dlgs[0]["id"]) if candidate_dlgs else None
        max_read_outbox_id = 0
        dialog_read_confirmed = False
        has_msg_reply = False
        last_inbound_text = ""
        last_out_text_from_dlg = ""

        current_step = int(rd.get("current_step") or 0)
        rec_status = str(rd.get("status") or "pending")
        last_sent_at = rd.get("last_sent_at")
        has_sent = (current_step >= 1) or bool(last_sent_at)

        for dlg in candidate_dlgs:
            did = int(dlg["id"])
            r_max = int(dlg.get("read_outbox_max_id") or 0)
            if r_max > max_read_outbox_id:
                max_read_outbox_id = r_max
            mst = msg_stats_by_dlg.get(did)
            if mst:
                if mst["last_out_text"] and not last_out_text_from_dlg:
                    last_out_text_from_dlg = mst["last_out_text"]
                last_out_mid = int(mst["last_out_msg_id"] or 0)
                if r_max > 0 and last_out_mid > 0 and r_max >= last_out_mid:
                    dialog_read_confirmed = True
                if mst["has_in_after_out"]:
                    has_msg_reply = True
                    if mst["last_in_after_out_text"] and not last_inbound_text:
                        last_inbound_text = mst["last_in_after_out_text"]
                elif has_sent and mst["has_in"]:
                    ref_sent = str(last_sent_at or "")
                    if not ref_sent or (dlg.get("last_message_at") or "") >= ref_sent:
                        has_msg_reply = True
                        if mst["last_in_text"] and not last_inbound_text:
                            last_inbound_text = mst["last_in_text"]
            if (
                not has_msg_reply
                and has_sent
                and int(dlg.get("last_from_peer") or 0) == 1
                and (not last_sent_at or str(dlg.get("last_message_at") or "") >= str(last_sent_at))
            ):
                has_msg_reply = True
                if dlg.get("last_message_preview") and not last_inbound_text:
                    last_inbound_text = str(dlg["last_message_preview"]).strip()

        if not last_inbound_text:
            last_inbound_text = _first_nonempty(
                extra.get("all_in_texts"),
                extra.get("first_in_text"),
            )

        step1_effective = _first_nonempty(
            rd.get("step1_text"),
            last_out_text_from_dlg if current_step == 1 else "",
            step1_tmpl_by_camp.get(cid, ""),
        )

        quality_score, quality_tier = compute_icp_quality(
            website=website,
            their_angle=their_angle,
            username=username,
            category=category,
            step1_text=step1_effective,
            explicit_score=rd.get("quality_score"),
        )

        variant_key, variant_label, pain_angle = resolve_variant_and_pain(
            explicit_variant_key=rd.get("variant_key"),
            explicit_pain_angle=rd.get("pain_angle"),
            step1_text=step1_effective,
            category=category,
            hook=str(rd.get("hook") or ""),
            extra=extra,
        )

        has_reply = (rec_status == "replied") or has_msg_reply
        read_at_val = rd.get("read_at")
        is_read = (
            bool(read_at_val)
            or has_reply
            or dialog_read_confirmed
            or (extra.get("last_out_read") is True)
        )

        lead_stage_low = str(rd.get("lead_status") or "").strip().lower()
        lead_id_val = int(rd["lead_id"]) if rd.get("lead_id") is not None else None
        is_positive_or_call = (
            (lead_stage_low in _POSITIVE_LEAD_STAGES)
            or (lead_id_val is not None and lead_id_val in calendar_lead_ids)
            or (has_reply and _has_positive_reply_signal(last_inbound_text))
        )

        if is_positive_or_call:
            stage_key = "positive_or_call"
        elif has_reply:
            stage_key = "replied_no_call"
        elif not has_sent:
            stage_key = "queued"
        elif is_read:
            stage_key = "read_no_reply"
        else:
            stage_key = "sent_unread"

        reason_code, reason_label = _diagnose_lead(
            stage=stage_key,
            status=rec_status,
            error_text=rd.get("error_text"),
            current_step=current_step,
            dialog_read_outbox_max_id=max_read_outbox_id,
            last_inbound_text=last_inbound_text,
        )

        evaluated.append(
            {
                "id": int(rd["id"]),
                "lead_id": lead_id_val,
                "campaign_id": cid,
                "campaign_name": camp_info.get("name") or f"Кампания #{cid}",
                "username": username,
                "name": rd.get("name") or company or username,
                "company": company,
                "category": category,
                "website": website,
                "source": source,
                "added_date": added_date,
                "quality_score": quality_score,
                "quality_tier": quality_tier,
                "variant_key": variant_key,
                "variant_label": variant_label,
                "pain_angle": pain_angle,
                "current_step": current_step,
                "status": rec_status,
                "stage": stage_key,
                "stage_label": STAGE_LABELS[stage_key],
                "reason_code": reason_code,
                "reason_label": reason_label,
                "last_sent_at": last_sent_at,
                "read_at": read_at_val,
                "is_read": is_read,
                "has_reply": has_reply,
                "last_inbound_text": last_inbound_text[:300] if last_inbound_text else "",
                "step1_preview": step1_effective[:500] if step1_effective else "",
                "dialog_id": primary_dialog_id,
            }
        )

    # 1. Summary (SSOT across selected campaign scope)
    stages_counts = {k: 0 for k in STAGE_ORDER}
    tiers_counts = {"A": 0, "B": 0, "C": 0}
    added_today = 0
    added_7d = 0
    quality_sum = 0

    for item in evaluated:
        stages_counts[item["stage"]] += 1
        tiers_counts[item["quality_tier"]] += 1
        quality_sum += int(item["quality_score"])
        ad = item["added_date"]
        if ad == today_str:
            added_today += 1
        if ad >= date_7d_cutoff:
            added_7d += 1

    total_leads = len(evaluated)
    contacted_total = total_leads - stages_counts["queued"]
    read_total = (
        stages_counts["read_no_reply"]
        + stages_counts["replied_no_call"]
        + stages_counts["positive_or_call"]
    )
    replied_total = stages_counts["replied_no_call"] + stages_counts["positive_or_call"]
    positive_total = stages_counts["positive_or_call"]

    summary = {
        "total_leads": total_leads,
        "added_today": added_today,
        "added_7d": added_7d,
        "avg_quality_score": round(quality_sum / total_leads, 1) if total_leads > 0 else 0.0,
        "quality_tiers": tiers_counts,
        "contacted_total": contacted_total,
        "read_total": read_total,
        "replied_total": replied_total,
        "positive_or_call_total": positive_total,
        "read_rate_pct": _pct(read_total, contacted_total),
        "reply_rate_pct": _pct(replied_total, contacted_total),
        "positive_rate_pct": _pct(positive_total, contacted_total),
        "stages": stages_counts,
    }

    # 2. Daily Intake
    intake_buckets: dict[str, dict] = {}
    for item in evaluated:
        day_key = item["added_date"]
        b = intake_buckets.get(day_key)
        if b is None:
            b = {"date": day_key, "added_count": 0, "q_sum": 0, "tier_a_count": 0, "sources": {}}
            intake_buckets[day_key] = b
        b["added_count"] += 1
        b["q_sum"] += int(item["quality_score"])
        if item["quality_tier"] == "A":
            b["tier_a_count"] += 1
        src = item["source"] or "Не указан"
        b["sources"][src] = b["sources"].get(src, 0) + 1

    all_days_sorted = sorted(intake_buckets.values(), key=lambda x: x["date"], reverse=True)
    window_days = [b for b in all_days_sorted if b["date"] >= intake_cutoff]
    chosen_days = window_days if window_days else all_days_sorted[:safe_days]
    daily_intake = [
        {
            "date": b["date"],
            "added_count": b["added_count"],
            "avg_quality": round(b["q_sum"] / b["added_count"], 1) if b["added_count"] else 0.0,
            "tier_a_count": b["tier_a_count"],
            "sources": b["sources"],
        }
        for b in chosen_days
    ]

    # 3. Variant Stats
    var_buckets: dict[str, dict] = {}
    for item in evaluated:
        vk = item["variant_key"]
        vb = var_buckets.get(vk)
        if vb is None:
            vb = {
                "variant_key": vk,
                "label": item["variant_label"],
                "total_leads": 0,
                "queued": 0,
                "sent": 0,
                "read": 0,
                "replied": 0,
                "positive_or_call": 0,
            }
            var_buckets[vk] = vb
        vb["total_leads"] += 1
        st_key = item["stage"]
        if st_key == "queued":
            vb["queued"] += 1
        else:
            vb["sent"] += 1
            if st_key in ("read_no_reply", "replied_no_call", "positive_or_call"):
                vb["read"] += 1
            if st_key in ("replied_no_call", "positive_or_call"):
                vb["replied"] += 1
            if st_key == "positive_or_call":
                vb["positive_or_call"] += 1

    variant_stats = []
    for vb in sorted(var_buckets.values(), key=lambda x: (-x["total_leads"], x["variant_key"])):
        sent_n = vb["sent"]
        variant_stats.append(
            {
                "variant_key": vb["variant_key"],
                "label": vb["label"],
                "total_leads": vb["total_leads"],
                "queued": vb["queued"],
                "sent": sent_n,
                "read": vb["read"],
                "read_rate_pct": _pct(vb["read"], sent_n),
                "replied": vb["replied"],
                "reply_rate_pct": _pct(vb["replied"], sent_n),
                "positive_or_call": vb["positive_or_call"],
                "positive_rate_pct": _pct(vb["positive_or_call"], sent_n),
            }
        )

    # 4. Step Stats & Drop-off Reasons
    step_defs = (
        (1, "Step 1 — Первое касание"),
        (2, "Step 2 — Фоллоу-ап 1"),
        (3, "Step 3 — Фоллоу-ап 2"),
    )
    steps_summary = []
    for step_no, step_title in step_defs:
        if step_no == 1:
            reached_items = [x for x in evaluated if x["stage"] != "queued" or x["current_step"] >= 1]
            at_step_items = [x for x in reached_items if max(1, int(x["current_step"] or 1)) == 1]
        elif step_no == 2:
            reached_items = [x for x in evaluated if int(x["current_step"] or 0) >= 2]
            at_step_items = [x for x in reached_items if int(x["current_step"] or 0) == 2]
        else:
            reached_items = [x for x in evaluated if int(x["current_step"] or 0) >= 3]
            at_step_items = [x for x in reached_items if int(x["current_step"] or 0) >= 3]

        reached_n = len(reached_items)
        read_n = sum(
            1
            for x in reached_items
            if x["stage"] in ("read_no_reply", "replied_no_call", "positive_or_call")
        )
        replied_n = sum(
            1 for x in at_step_items if x["stage"] in ("replied_no_call", "positive_or_call")
        )
        pos_n = sum(1 for x in at_step_items if x["stage"] == "positive_or_call")
        stopped_n = sum(
            1 for x in at_step_items if x["stage"] in ("sent_unread", "read_no_reply", "replied_no_call")
        )
        steps_summary.append(
            {
                "step": step_no,
                "label": step_title,
                "reached": reached_n,
                "at_step": len(at_step_items),
                "stopped_here": stopped_n,
                "read": read_n,
                "replied": replied_n,
                "positive_or_call": pos_n,
                "reply_rate_pct": _pct(replied_n, reached_n),
            }
        )

    dropoff_map: dict[tuple[str, str], dict] = {}
    dropoff_pool_total = 0
    for item in evaluated:
        st_key = item["stage"]
        if st_key in ("sent_unread", "read_no_reply", "replied_no_call") or (
            st_key == "queued" and item["reason_code"] == "send_failed"
        ):
            dropoff_pool_total += 1
            k = (item["reason_code"], item["reason_label"])
            entry = dropoff_map.get(k)
            if entry is None:
                entry = {
                    "reason_code": item["reason_code"],
                    "reason_label": item["reason_label"],
                    "stage": st_key,
                    "count": 0,
                }
                dropoff_map[k] = entry
            entry["count"] += 1

    dropoff_reasons = [
        {
            "reason_code": e["reason_code"],
            "reason_label": e["reason_label"],
            "stage": e["stage"],
            "count": e["count"],
            "share_pct": _pct(e["count"], dropoff_pool_total),
        }
        for e in sorted(dropoff_map.values(), key=lambda x: (-x["count"], x["reason_code"]))
    ]

    step_stats = {
        "steps": steps_summary,
        "dropoff_reasons": dropoff_reasons,
    }

    # 5. Filtered Leads List
    wanted_stage = str(stage or "all").strip()
    wanted_variant = str(variant or "all").strip()
    needle = str(q or "").strip().casefold()
    safe_limit = max(1, min(int(limit or 150), 1000))

    filtered = []
    for item in evaluated:
        if wanted_stage and wanted_stage != "all" and item["stage"] != wanted_stage:
            continue
        if wanted_variant and wanted_variant != "all" and item["variant_key"] != wanted_variant:
            continue
        if needle:
            blob = " ".join(
                (
                    item["username"],
                    item["company"],
                    str(item.get("name") or ""),
                    item["website"],
                    item["category"],
                    item["source"],
                    item["variant_label"],
                    item["reason_label"],
                    item["last_inbound_text"],
                )
            ).casefold()
            if needle not in blob:
                continue
        filtered.append(item)

    filtered.sort(key=lambda x: (x["added_date"], x["id"]), reverse=True)
    leads_out = [
        {
            "id": x["id"],
            "lead_id": x["lead_id"],
            "campaign_id": x["campaign_id"],
            "campaign_name": x["campaign_name"],
            "username": x["username"],
            "name": x["name"],
            "company": x["company"],
            "category": x["category"],
            "website": x["website"],
            "source": x["source"],
            "added_date": x["added_date"],
            "quality_score": x["quality_score"],
            "quality_tier": x["quality_tier"],
            "variant_key": x["variant_key"],
            "variant_label": x["variant_label"],
            "pain_angle": x["pain_angle"],
            "current_step": x["current_step"],
            "stage": x["stage"],
            "stage_label": x["stage_label"],
            "reason_code": x["reason_code"],
            "reason_label": x["reason_label"],
            "last_sent_at": x["last_sent_at"],
            "read_at": x["read_at"],
            "last_inbound_text": x["last_inbound_text"],
            "step1_preview": x["step1_preview"],
            "dialog_id": x["dialog_id"],
        }
        for x in filtered[:safe_limit]
    ]

    return {
        "summary": summary,
        "daily_intake": daily_intake,
        "variant_stats": variant_stats,
        "step_stats": step_stats,
        "leads": leads_out,
        "filtered_total": len(filtered),
        "campaigns": campaigns_meta,
    }
