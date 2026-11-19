import base64
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import zipfile

from app.db import DEFAULT_DB, ROOT, as_dict, resolve_session, utc_now, utc_today

logger = logging.getLogger(__name__)
ORPHAN_DEAD_ACCOUNT_ERROR = "Цепочка остановлена: исходный аккаунт деавторизован (unauthorized)"
from app.telegram_gateway import (
    SESSION_HEALTH,
    GatewayError,
    classify_proxy_mode,
    deterministic_device_profile,
    extract_auth_key_sha256_from_sqlite,
    extract_auth_key_sha256_from_string,
    extract_string_session_from_sqlite,
    mask_proxy_url,
    resolve_nodemaven_config,
    write_sqlite_session_from_string,
)
from app.whatsapp_gateway import (
    DEFAULT_WA_DAILY_LIMIT,
    WhatsAppGateway,
    _inspect_whatsmeow_db,
)


def _is_valid_string_session(val: str | None) -> bool:
    if not val or not str(val).strip():
        return False
    try:
        from telethon.sessions import StringSession

        sess = StringSession(str(val).strip())
        ak = getattr(sess, "auth_key", None)
        key = getattr(ak, "key", None) if ak is not None else None
        return bool(key and len(bytes(key)) == 256)
    except Exception:
        return False


def _inspect_telethon_session(session_path: str) -> tuple[int, bytes] | None:
    if not session_path or session_path.startswith("data/sessions/demo"):
        return None
    path = resolve_session(session_path)
    if not path.exists() or not path.is_file():
        return None
    try:
        if path.stat().st_size < 4096:
            return None
        with sqlite3.connect(path) as sconn:
            row = sconn.execute(
                "SELECT dc_id, auth_key FROM sessions WHERE auth_key IS NOT NULL AND length(auth_key) = 256 LIMIT 1"
            ).fetchone()
            if row and row[0] and row[1] and len(bytes(row[1])) == 256:
                return int(row[0]), bytes(row[1])
    except Exception:
        return None
    return None

PUBLIC_FIELDS = (
    "id",
    "phone",
    "status",
    "telegram_user_id",
    "telegram_username",
    "telegram_name",
    "created_at",
    "health",
    "last_error",
    "daily_limit",
    "sent_today",
    "sent_date",
    "proxy_url",
    "access_scope",
    "provider",
    "wa_jid",
    "pairing_code",
    "qr_code",
)


def _is_nodemaven_configured(conn=None) -> bool:
    try:
        if conn is not None:
            return resolve_nodemaven_config(conn) is not None
    except Exception:
        pass
    user = os.getenv("NODEMAVEN_USER", "").strip()
    pwd = os.getenv("NODEMAVEN_PASS", "").strip() or os.getenv("NODEMAVEN_PASSWORD", "").strip()
    return bool(user and pwd)


def _public(row: dict, *, nodemaven_configured: bool | None = None) -> dict:
    today = utc_today()
    daily_limit = int(row.get("daily_limit") or 20)
    sent_today = int(row.get("sent_today") or 0) if row.get("sent_date") == today else 0
    session_path = str(row.get("session_path") or "")
    is_demo = session_path.startswith("data/sessions/demo")
    has_str_sess = bool(row.get("session_string") and str(row.get("session_string")).strip())
    session_exists = bool(has_str_sess or (session_path and resolve_session(session_path).exists()))
    provider = str(row.get("provider") or "telegram").strip().lower()
    if provider not in ("telegram", "whatsapp"):
        provider = "telegram"

    status = row.get("status")
    health = row.get("health")
    if provider == "whatsapp":
        if status == "pending_code":
            connection_label = "Не подключён · ждёт Pairing-код / QR"
        elif status != "connected":
            connection_label = "Не подключён · ошибка входа"
        elif health == "frozen" and _is_spambot_restriction_error(row.get("last_error")):
            connection_label = "Ограничен @SpamBot · только тёплые чаты"
        elif health in ("unauthorized", "banned", "frozen"):
            connection_label = "Сессия слетела · нужен вход"
        elif health == "offline":
            connection_label = "Нет связи с WhatsApp"
        elif is_demo:
            connection_label = "Демо WhatsApp (локально)"
        elif session_exists and health == "active":
            connection_label = "Подключён · WhatsApp Web активен"
        else:
            connection_label = "Подключён · WhatsApp сессия"
    else:
        if status == "pending_code":
            connection_label = "Не подключён · ждёт код"
        elif status == "pending_password":
            connection_label = "Не подключён · ждёт 2FA пароль"
        elif status != "connected":
            connection_label = "Не подключён · ошибка входа"
        elif health == "frozen" and _is_spambot_restriction_error(row.get("last_error")):
            connection_label = "Ограничен @SpamBot · только тёплые чаты"
        elif health in ("unauthorized", "banned", "frozen"):
            connection_label = "Сессия слетела · нужен вход"
        elif health == "offline":
            connection_label = "Нет связи с Telegram"
        elif is_demo:
            connection_label = "Демо-профиль (локально)"
        elif session_exists and health == "active":
            connection_label = "Подключён · MTProto онлайн"
        else:
            connection_label = "Подключён · сессия сохранена"

    eff_nm = (
        False
        if provider == "whatsapp"
        else (
            bool(nodemaven_configured)
            if nodemaven_configured is not None
            else _is_nodemaven_configured(None)
        )
    )
    proxy_mode = classify_proxy_mode(row.get("proxy_url"), nodemaven_configured=eff_nm)

    out = {key: row.get(key) for key in PUBLIC_FIELDS}
    out["provider"] = provider
    out["daily_limit"] = daily_limit
    out["sent_today"] = sent_today
    out["sent_date"] = row.get("sent_date") or today
    out["proxy_url"] = row.get("proxy_url")
    out["proxy_mode"] = proxy_mode
    out["access_scope"] = row.get("access_scope") or "workspace"
    out["health_status"] = health
    out["is_demo"] = is_demo
    out["session_exists"] = session_exists
    out["tg_web_ready"] = False
    out["connection_label"] = connection_label
    return out


def set_daily_limit(conn, account_id: int, daily_limit: int) -> dict:
    _load(conn, account_id)
    if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or not (1 <= int(daily_limit) <= 500):
        raise ValueError("Лимит должен быть от 1 до 500 сообщений в сутки")
    conn.execute(
        "UPDATE accounts SET daily_limit = ?, updated_at = ? WHERE id = ?",
        (int(daily_limit), utc_now(), account_id),
    )
    conn.commit()
    return _public(_load(conn, account_id), nodemaven_configured=_is_nodemaven_configured(conn))


def update_account_settings(
    conn,
    account_id: int,
    *,
    proxy_url: str | None = None,
    access_scope: str | None = None,
    daily_limit: int | None = None,
    has_proxy: bool = False,
) -> dict:
    current = _load(conn, account_id)
    new_limit = int(current.get("daily_limit") or 20)
    if daily_limit is not None:
        if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or not (1 <= int(daily_limit) <= 500):
            raise ValueError("Лимит должен быть от 1 до 500 сообщений в сутки")
        new_limit = int(daily_limit)
    new_proxy = current.get("proxy_url")
    if has_proxy or proxy_url is not None:
        new_proxy = (proxy_url or "").strip() or None
        if str(current.get("provider") or "telegram").strip().lower() == "whatsapp":
            from app.whatsapp_gateway import _validate_wa_proxy_url

            try:
                new_proxy = _validate_wa_proxy_url(new_proxy)
            except GatewayError as exc:
                raise ValueError(str(exc)) from exc
    new_scope = current.get("access_scope") or "workspace"
    if access_scope is not None:
        new_scope = (access_scope or "workspace").strip() or "workspace"
    conn.execute(
        """
        UPDATE accounts
        SET daily_limit = ?, proxy_url = ?, access_scope = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_limit, new_proxy, new_scope, utc_now(), account_id),
    )
    conn.commit()
    return _public(_load(conn, account_id), nodemaven_configured=_is_nodemaven_configured(conn))


def _unlink_account_refs(conn, account_ids: list[int]) -> None:
    if not account_ids:
        return
    for aid in account_ids:
        try:
            conn.execute("UPDATE leads SET account_id = NULL WHERE account_id = ?", (aid,))
        except sqlite3.IntegrityError:
            lead_rows = conn.execute(
                "SELECT id FROM leads WHERE account_id = ?", (aid,)
            ).fetchall()
            for lrow in lead_rows:
                lid = lrow[0]
                try:
                    conn.execute("UPDATE leads SET account_id = NULL WHERE id = ?", (lid,))
                except sqlite3.IntegrityError:
                    conn.execute(
                        "UPDATE leads SET account_id = NULL, telegram_user_id = NULL WHERE id = ?",
                        (lid,),
                    )
        try:
            conn.execute("UPDATE campaigns SET account_id = NULL WHERE account_id = ?", (aid,))
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "UPDATE campaign_recipients SET assigned_account_id = NULL WHERE assigned_account_id = ?",
                (aid,),
            )
        except sqlite3.OperationalError:
            pass


def _get_deleted_sessions(conn) -> set[str]:
    try:
        row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'deleted_sessions_v1'"
        ).fetchone()
        if row and row[0]:
            items = json.loads(str(row[0]))
            if isinstance(items, list):
                return {str(x).strip() for x in items if x}
    except Exception:
        pass
    return set()


def _record_deleted_session(conn, session_path: str | None = None, phone: str | None = None) -> None:
    try:
        items = _get_deleted_sessions(conn)
        if session_path:
            items.add(str(session_path).strip())
            items.add(Path(str(session_path).strip()).name)
        digits = _phone_digits(phone)
        if digits and len(digits) >= 7:
            items.add(f"phone:{digits}")
        conn.execute(
            "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('deleted_sessions_v1', ?, ?)",
            (json.dumps(sorted(items), ensure_ascii=False), utc_now()),
        )
        conn.commit()
    except Exception:
        pass


def _clear_deleted_session(conn, session_path: str | None = None, phone: str | None = None) -> None:
    try:
        items = _get_deleted_sessions(conn)
        if not items:
            return
        if session_path:
            items.discard(str(session_path).strip())
            items.discard(Path(str(session_path).strip()).name)
        digits = _phone_digits(phone)
        if digits:
            items.discard(f"phone:{digits}")
        conn.execute(
            "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('deleted_sessions_v1', ?, ?)",
            (json.dumps(sorted(items), ensure_ascii=False), utc_now()),
        )
        conn.commit()
    except Exception:
        pass


def _can_mutate_session_file_on_disk(conn, abs_p: Path) -> bool:
    if os.environ.get("CRMTG_SKIP_GLOBAL_DB_FALLBACK") == "1":
        return False
    if os.getenv("BOT_TOKEN") == "e2e-not-a-secret":
        return False
    try:
        db_file = None
        if conn is not None:
            for r in conn.execute("PRAGMA database_list").fetchall():
                if str(r[1] or "") == "main" and str(r[2] or "").strip():
                    db_file = Path(str(r[2])).resolve()
                    break
        default_resolved = DEFAULT_DB.resolve()
        if db_file is not None and db_file != default_resolved:
            global_data_dir = (ROOT / "data").resolve()
            resolved_sess = abs_p.resolve()
            if resolved_sess.is_relative_to(global_data_dir) and not resolved_sess.is_relative_to(db_file.parent):
                return False
    except Exception:
        pass
    return True


def delete_account(conn, account_id: int, gateway=None) -> dict:
    current = _load(conn, account_id)
    session_path = str(current.get("session_path") or "").strip()
    phone = str(current.get("phone") or "").strip()
    conn.execute(
        "UPDATE accounts SET session_blob = NULL, session_string = NULL WHERE id = ?",
        (account_id,),
    )
    if gateway is not None and session_path and hasattr(gateway, "clear_session"):
        gateway.clear_session(session_path)
    try:
        conn.execute(
            """
            UPDATE campaign_recipients
            SET status = 'completed',
                error_text = ?,
                updated_at = ?
            WHERE status = 'in_progress'
              AND (sent_account_id = ? OR (sent_account_id IS NULL AND assigned_account_id = ?))
            """,
            (ORPHAN_DEAD_ACCOUNT_ERROR, utc_now(), int(account_id), int(account_id)),
        )
    except sqlite3.OperationalError:
        pass
    _unlink_account_refs(conn, [int(account_id)])
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    _record_deleted_session(conn, session_path=session_path, phone=phone)
    if (
        session_path
        and not session_path.startswith("data/sessions/demo")
    ):
        try:
            abs_p = resolve_session(session_path)
            if _can_mutate_session_file_on_disk(conn, abs_p) and abs_p.exists() and abs_p.is_file():
                if _inspect_telethon_session(session_path) is not None:
                    backup_p = abs_p.parent / f"backup_{abs_p.name}"
                    shutil.move(str(abs_p), str(backup_p))
                else:
                    abs_p.unlink()
            if _can_mutate_session_file_on_disk(conn, abs_p) and abs_p.name == "1.session":
                tim_backup = abs_p.parent / "backup_tim_cros.session"
                if tim_backup.exists():
                    shutil.move(str(tim_backup), str(abs_p.parent / "backup_deleted_tim_cros.session"))
            if _can_mutate_session_file_on_disk(conn, abs_p):
                for suffix in ("-journal", "-wal", "-shm"):
                    sidecar = abs_p.with_name(abs_p.name + suffix)
                    if sidecar.exists():
                        sidecar.unlink()
        except Exception:
            pass
    return {"deleted": True, "id": int(account_id), "session_path": session_path}


_SPAMBOT_TERMINAL_DEAD_MARKERS = (
    "user_deactivated",
    "userdeactivated",
    "phonenumberbanned",
    "phone_number_banned",
    "auth_key_unregistered",
    "session_revoked",
    "frozen_method_invalid",
)

_SPAMBOT_RESTRICTION_MARKERS = (
    "spambot",
    "cannot message non-contacts",
    "peerspam",
    "spam-reported",
    "limited until",
    "account is now limited",
    "while the account is limited",
    "currently limited",
    "only send messages to mutual contacts",
    "moderators have confirmed",
    "account was blocked for violations",
    "ограничен до",
    "временно ограничен",
    "аккаунт ограничен",
    "наложены ограничения",
    "пока действуют ограничения",
    "писать первым вы пока не можете",
    "ограничения будут сняты",
    "обліковий запис обмежено",
    "hesabınız şu anda sınırlandırılmıştır",
    "peer_flood",
    "peerflood",
    "too many requests",
)


def _is_spambot_restriction_error(last_error: str | None) -> bool:
    raw = str(last_error or "").strip()
    if not raw:
        return False
    blob = raw.casefold()
    if any(dead in blob for dead in _SPAMBOT_TERMINAL_DEAD_MARKERS):
        return False
    if any(m in blob for m in _SPAMBOT_RESTRICTION_MARKERS):
        return True
    from app.telegram_gateway import parse_spambot_details

    details = parse_spambot_details(raw)
    return bool(details.get("limited") is True or details.get("is_expired_limit"))


def purge_dead_accounts(conn, gateway=None) -> dict:
    rows = conn.execute(
        """
        SELECT id, health, last_error FROM accounts
        WHERE status != 'connected'
           OR health IN ('unauthorized', 'banned', 'frozen')
        ORDER BY id ASC
        """
    ).fetchall()
    deleted_ids: list[int] = []
    deleted_paths: list[str] = []
    for row in rows:
        health_val = str(row[1] or "").strip().lower()
        if health_val == "frozen" and _is_spambot_restriction_error(row[2]):
            continue
        aid = int(row[0])
        try:
            res = delete_account(conn, aid, gateway=gateway)
            deleted_ids.append(aid)
            if res.get("session_path"):
                deleted_paths.append(str(res["session_path"]))
        except Exception:
            continue
    return {
        "deleted_ids": deleted_ids,
        "deleted_paths": deleted_paths,
        "count": len(deleted_ids),
        "accounts": list_accounts(conn),
    }


def check_daily_limit(conn, account_id: int) -> tuple[int, int]:
    row = _load(conn, account_id)
    today = utc_today()
    daily_limit = int(row.get("daily_limit") or 20)
    if row.get("sent_date") != today:
        conn.execute(
            "UPDATE accounts SET sent_today = 0, sent_date = ?, updated_at = ? WHERE id = ?",
            (today, utc_now(), account_id),
        )
        conn.commit()
        sent_today = 0
    else:
        sent_today = int(row.get("sent_today") or 0)
    if sent_today >= daily_limit:
        raise ValueError(f"Достигнут суточный лимит аккаунта ({daily_limit} сообщ./сутки)")
    return sent_today, daily_limit


def increment_daily_sent(conn, account_id: int) -> dict:
    _load(conn, account_id)
    today = utc_today()
    conn.execute(
        """
        UPDATE accounts
        SET sent_today = CASE WHEN sent_date = ? THEN sent_today + 1 ELSE 1 END,
            sent_date = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (today, today, utc_now(), account_id),
    )
    conn.commit()
    return _public(_load(conn, account_id), nodemaven_configured=_is_nodemaven_configured(conn))


def _load(conn, account_id: int) -> dict:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise LookupError("account")
    return as_dict(row)


def _mark_error(conn, account_id: int) -> None:
    conn.execute("UPDATE accounts SET status = 'error' WHERE id = ?", (account_id,))
    conn.commit()


def _phone_digits(phone: str | None) -> str:
    return re.sub(r"\D", "", str(phone or ""))


def _find_duplicate_account(
    conn,
    provider: str = "telegram",
    phone: str | None = None,
    telegram_user_id: int | None = None,
    exclude_id: int | None = None,
    connected_only: bool = True,
) -> dict | None:
    norm_prov = "whatsapp" if str(provider or "").strip().lower() == "whatsapp" else "telegram"
    target_digits = _phone_digits(phone)
    rows = conn.execute(
        "SELECT * FROM accounts WHERE COALESCE(provider, 'telegram') = ? ORDER BY CASE WHEN status = 'connected' THEN 0 ELSE 1 END, id ASC",
        (norm_prov,),
    ).fetchall()
    for raw in rows:
        row = as_dict(raw)
        rid = int(row["id"])
        if exclude_id is not None and rid == int(exclude_id):
            continue
        if connected_only and row.get("status") != "connected":
            continue
        row_digits = _phone_digits(row.get("phone"))
        if target_digits and len(target_digits) >= 3 and row_digits == target_digits:
            return row
        if (
            telegram_user_id is not None
            and row.get("telegram_user_id") is not None
            and int(row["telegram_user_id"]) == int(telegram_user_id)
        ):
            return row
    return None


def deduplicate_accounts(conn) -> int:
    rows = conn.execute(
        "SELECT * FROM accounts ORDER BY CASE WHEN status = 'connected' THEN 0 ELSE 1 END, id ASC"
    ).fetchall()
    seen_phones: dict[tuple[str, str], int] = {}
    seen_uids: dict[tuple[str, int], int] = {}
    dup_ids: list[int] = []

    for raw in rows:
        row = as_dict(raw)
        aid = int(row["id"])
        prov = "whatsapp" if str(row.get("provider") or "").strip().lower() == "whatsapp" else "telegram"
        pdigits = _phone_digits(row.get("phone"))
        uid = row.get("telegram_user_id")

        is_dup = False
        if pdigits and len(pdigits) >= 3:
            if (prov, pdigits) in seen_phones:
                is_dup = True
            else:
                seen_phones[(prov, pdigits)] = aid
        if uid is not None:
            try:
                uid_int = int(uid)
                if (prov, uid_int) in seen_uids:
                    is_dup = True
                else:
                    seen_uids[(prov, uid_int)] = aid
            except (TypeError, ValueError):
                pass
        if is_dup:
            dup_ids.append(aid)

    if dup_ids:
        _unlink_account_refs(conn, dup_ids)
        for did in dup_ids:
            conn.execute("DELETE FROM accounts WHERE id = ?", (did,))
        conn.commit()
    return len(dup_ids)


def list_accounts(conn) -> list:
    deduplicate_accounts(conn)
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    nm_configured = _is_nodemaven_configured(conn)
    return [_public(as_dict(row), nodemaven_configured=nm_configured) for row in rows]


def compute_account_pool_summary(
    conn, *, now_epoch: float | None = None
) -> dict:
    """Compute send-eligible account pool excluding frozen/dead/offline and active cooldowns."""
    today = utc_today()
    now_ts = float(now_epoch) if now_epoch is not None else time.time()
    cooldown_ids: set[int] = set()
    try:
        kv_rows = conn.execute(
            "SELECT key, value FROM kv_settings WHERE key LIKE 'account_cooldown_until:%'"
        ).fetchall()
        for k, v in kv_rows:
            try:
                acc_id = int(str(k).split(":", 1)[1])
                until_ts = float(v)
                if until_ts > now_ts:
                    cooldown_ids.add(acc_id)
            except (IndexError, TypeError, ValueError):
                continue
    except sqlite3.OperationalError:
        pass

    rows = conn.execute(
        """
        SELECT id, health, daily_limit, sent_today, sent_date
        FROM accounts
        WHERE status = 'connected'
        ORDER BY id ASC
        """
    ).fetchall()

    connected_accounts = 0
    active_accounts = 0
    daily_pool = 0
    sent_today_total = 0
    remaining_today = 0
    frozen_accounts = 0
    cooldown_accounts = 0
    offline_accounts = 0
    dead_accounts = 0

    for raw in rows:
        row = as_dict(raw)
        acc_id = int(row["id"])
        health_norm = str(row.get("health") or "active").strip().lower()
        limit_val = max(0, int(row.get("daily_limit") or 20))
        sent_val = int(row.get("sent_today") or 0) if row.get("sent_date") == today else 0
        in_cd = acc_id in cooldown_ids

        connected_accounts += 1
        if health_norm == "frozen":
            frozen_accounts += 1
        elif health_norm in ("unauthorized", "banned"):
            dead_accounts += 1
        elif health_norm == "offline":
            offline_accounts += 1
        if in_cd:
            cooldown_accounts += 1

        if health_norm == "active" and not in_cd:
            active_accounts += 1
            daily_pool += limit_val
            sent_today_total += sent_val
            remaining_today += max(0, limit_val - sent_val)

    return {
        "connected_accounts": connected_accounts,
        "active_accounts": active_accounts,
        "daily_pool": daily_pool,
        "sent_today": sent_today_total,
        "remaining_today": remaining_today,
        "frozen_accounts": frozen_accounts,
        "cooldown_accounts": cooldown_accounts,
        "offline_accounts": offline_accounts,
        "dead_accounts": dead_accounts,
    }


def backfill_account_device_profiles(conn, *, account_id: int | None = None) -> int:
    """Assign deterministic per-account device profiles only for rows missing explicit fields."""
    if account_id is not None:
        rows = conn.execute(
            """
            SELECT id, device_model, system_version, app_version, lang_code, system_lang_code
            FROM accounts
            WHERE id = ? AND COALESCE(provider, 'telegram') != 'whatsapp'
            """,
            (int(account_id),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, device_model, system_version, app_version, lang_code, system_lang_code
            FROM accounts
            WHERE COALESCE(provider, 'telegram') != 'whatsapp'
            ORDER BY id ASC
            """
        ).fetchall()
    updated = 0
    for r in rows:
        aid = int(r[0])
        cur_vals = {
            "device_model": str(r[1] or "").strip(),
            "system_version": str(r[2] or "").strip(),
            "app_version": str(r[3] or "").strip(),
            "lang_code": str(r[4] or "").strip(),
            "system_lang_code": str(r[5] or "").strip(),
        }
        if all(cur_vals.values()):
            continue
        prof = deterministic_device_profile(aid)
        merged = {k: (cur_vals[k] or prof[k]) for k in cur_vals}
        conn.execute(
            """
            UPDATE accounts
            SET device_model = ?, system_version = ?, app_version = ?, lang_code = ?, system_lang_code = ?
            WHERE id = ?
            """,
            (
                merged["device_model"],
                merged["system_version"],
                merged["app_version"],
                merged["lang_code"],
                merged["system_lang_code"],
                aid,
            ),
        )
        updated += 1
    if updated > 0:
        conn.commit()
    return updated


def reconcile_disk_sessions_with_db(conn) -> dict:
    """Enforce accounts.session_string as canonical SSOT; repair mismatched disk .session & session_blob."""
    rows = conn.execute(
        """
        SELECT id, session_path, session_string, session_blob, provider
        FROM accounts
        WHERE COALESCE(provider, 'telegram') != 'whatsapp'
          AND session_path IS NOT NULL
          AND session_path != ''
        ORDER BY id ASC
        """
    ).fetchall()
    repaired_ids: list[int] = []
    backfilled_ids: list[int] = []
    skipped_protected = 0

    for raw in rows:
        row = as_dict(raw)
        aid = int(row["id"])
        session_path = str(row.get("session_path") or "").strip()
        if not session_path or session_path.startswith("data/sessions/demo"):
            continue
        abs_p = resolve_session(session_path)
        str_sess = str(row.get("session_string") or "").strip()

        if _is_valid_string_session(str_sess):
            db_sha = extract_auth_key_sha256_from_string(str_sess)
            disk_sha = extract_auth_key_sha256_from_sqlite(abs_p) if abs_p.exists() else None
            if db_sha and disk_sha != db_sha:
                if _can_mutate_session_file_on_disk(conn, abs_p):
                    new_bytes = write_sqlite_session_from_string(str_sess, abs_p)
                    if new_bytes:
                        conn.execute(
                            "UPDATE accounts SET session_blob = ? WHERE id = ?",
                            (new_bytes, aid),
                        )
                        repaired_ids.append(aid)
                        logger.warning(
                            "Reconciled mismatched disk .session for account_id=%s (%s) from canonical session_string",
                            aid,
                            session_path,
                        )
                else:
                    skipped_protected += 1
            elif db_sha and disk_sha == db_sha and abs_p.exists():
                try:
                    cur_bytes = abs_p.read_bytes()
                    blob = row.get("session_blob")
                    if not blob or bytes(blob) != cur_bytes:
                        conn.execute(
                            "UPDATE accounts SET session_blob = ? WHERE id = ?",
                            (cur_bytes, aid),
                        )
                except Exception:
                    pass
        elif abs_p.exists():
            extracted = extract_string_session_from_sqlite(abs_p)
            if extracted and _is_valid_string_session(extracted):
                conn.execute(
                    "UPDATE accounts SET session_string = ? WHERE id = ?",
                    (extracted, aid),
                )
                backfilled_ids.append(aid)

    if repaired_ids or backfilled_ids:
        conn.commit()
    return {
        "repaired": len(repaired_ids),
        "repaired_ids": repaired_ids,
        "backfilled": len(backfilled_ids),
        "backfilled_ids": backfilled_ids,
        "skipped_protected": skipped_protected,
    }


def _sync_session_blob(conn, account_id: int) -> None:
    try:
        row = conn.execute(
            "SELECT session_path, session_string, provider FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if not row or not row[0]:
            return
        session_path = str(row[0]).strip()
        existing_str = str(row[1] or "").strip()
        prov = str(row[2] or "telegram").strip().lower()
        abs_p = resolve_session(session_path)
        if prov != "whatsapp":
            if _is_valid_string_session(existing_str):
                db_sha = extract_auth_key_sha256_from_string(existing_str)
                disk_sha = extract_auth_key_sha256_from_sqlite(abs_p) if abs_p.exists() else None
                if db_sha and disk_sha != db_sha and _can_mutate_session_file_on_disk(conn, abs_p):
                    new_bytes = write_sqlite_session_from_string(existing_str, abs_p)
                    if new_bytes:
                        conn.execute(
                            "UPDATE accounts SET session_blob = ? WHERE id = ?",
                            (new_bytes, account_id),
                        )
                        conn.commit()
                        return
            elif not existing_str:
                extracted = extract_string_session_from_sqlite(abs_p)
                if extracted:
                    conn.execute(
                        "UPDATE accounts SET session_string = ? WHERE id = ?",
                        (extracted, account_id),
                    )
                    conn.commit()
        if (
            _inspect_telethon_session(session_path) is None
            and _inspect_whatsmeow_db(session_path) is None
        ):
            return
        blob = abs_p.read_bytes()
        conn.execute(
            "UPDATE accounts SET session_blob = ? WHERE id = ?",
            (blob, account_id),
        )
        conn.commit()
    except Exception:
        pass


def restore_disk_sessions(conn) -> int:
    """Ensure authorized Telethon .session files and DB rows stay synchronized across restarts."""
    sessions_dir = resolve_session("data/sessions")
    sessions_dir.mkdir(parents=True, exist_ok=True)
    deleted_registry = _get_deleted_sessions(conn)
    primary_sess = sessions_dir / "1.session"
    backup_sess = sessions_dir / "backup_tim_cros.session"

    tim_deleted = (
        "data/sessions/1.session" in deleted_registry
        or "1.session" in deleted_registry
        or "phone:905346380943" in deleted_registry
    )
    if not tim_deleted:
        if not primary_sess.exists() and backup_sess.exists():
            try:
                shutil.copy2(backup_sess, primary_sess)
            except Exception:
                pass
        elif primary_sess.exists() and not backup_sess.exists():
            if _inspect_telethon_session("data/sessions/1.session") is not None:
                try:
                    shutil.copy2(primary_sess, backup_sess)
                except Exception:
                    pass

    # Clean up phantom accounts created from numeric ID session files (e.g. "+2", "+3")
    phantom_rows = conn.execute(
        """
        SELECT id, phone, session_path, telegram_username
        FROM accounts
        WHERE status != 'pending_code'
        """
    ).fetchall()
    phantom_ids = [
        int(r[0])
        for r in phantom_rows
        if len(_phone_digits(r[1])) < 7
        and not str(r[2] or "").startswith("data/sessions/demo")
        and not r[3]
    ]
    if phantom_ids:
        _unlink_account_refs(conn, phantom_ids)
        for pid in phantom_ids:
            conn.execute("DELETE FROM accounts WHERE id = ?", (pid,))
        conn.commit()

    restored = 0
    now = utc_now()
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    known_paths: set[str] = set()
    connected_phones: set[str] = set()

    for raw in rows:
        row = as_dict(raw)
        account_id = int(row["id"])
        session_path = str(row.get("session_path") or "").strip()
        if not session_path:
            continue
        p_digits = _phone_digits(str(row.get("phone") or ""))
        if (
            session_path in deleted_registry
            or Path(session_path).name in deleted_registry
            or (p_digits and f"phone:{p_digits}" in deleted_registry)
        ):
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            continue
        known_paths.add(session_path)
        abs_p = resolve_session(session_path)
        blob = row.get("session_blob")
        if not abs_p.exists() and blob and len(bytes(blob)) >= 4096:
            try:
                abs_p.parent.mkdir(parents=True, exist_ok=True)
                abs_p.write_bytes(bytes(blob))
                restored += 1
            except Exception:
                pass
        prov = str(row.get("provider") or "telegram").strip().lower()
        if prov != "whatsapp" and not row.get("session_string"):
            extracted = extract_string_session_from_sqlite(abs_p)
            if extracted:
                conn.execute(
                    "UPDATE accounts SET session_string = ? WHERE id = ?",
                    (extracted, account_id),
                )
                row["session_string"] = extracted
        has_disk_sess = (
            _inspect_telethon_session(session_path) is not None
            or _inspect_whatsmeow_db(session_path) is not None
        )
        has_str_sess = prov != "whatsapp" and _is_valid_string_session(row.get("session_string"))
        if has_disk_sess or has_str_sess:
            if has_disk_sess and abs_p.exists():
                try:
                    current_bytes = abs_p.read_bytes()
                    if not blob or bytes(blob) != current_bytes:
                        conn.execute(
                            "UPDATE accounts SET session_blob = ? WHERE id = ?",
                            (current_bytes, account_id),
                        )
                except Exception:
                    pass
            if row.get("status") != "connected" and row.get("health") not in (
                "unauthorized",
                "banned",
                "frozen",
            ):
                conn.execute(
                    """
                    UPDATE accounts
                    SET status = 'connected', health = COALESCE(health, 'active'),
                        last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, account_id),
                )
                restored += 1
            connected_phones.add(str(row.get("phone") or "").strip())

    # Register any authorized phone-named .session files on disk that are missing from accounts table
    for sess_file in sorted(sessions_dir.glob("*.session")):
        name = sess_file.name
        if (
            name.startswith("demo")
            or name.startswith("wa_")
            or name.startswith("backup_")
            or name.startswith("fake_")
        ):
            continue
        rel_path = f"data/sessions/{name}"
        if rel_path in known_paths:
            continue
        if rel_path in deleted_registry or name in deleted_registry:
            continue
        is_tim = name == "1.session"
        stem_digits = _phone_digits(sess_file.stem)
        # Never auto-register numeric row-ID files like 2.session, 3.session, 11.session
        if not is_tim and len(stem_digits) < 7:
            continue
        phone = "+905346380943" if is_tim else f"+{stem_digits}"
        if f"phone:{_phone_digits(phone)}" in deleted_registry:
            continue
        if _inspect_telethon_session(rel_path) is None:
            continue
        tg_uid = 5680938518 if is_tim else None
        tg_user = "tim_cros" if is_tim else None
        tg_name = "Tim Korznikov" if is_tim else f"Telegram ({sess_file.stem})"
        if _find_duplicate_account(conn, "telegram", phone=phone, telegram_user_id=tg_uid, connected_only=False) is not None:
            continue
        try:
            blob = sess_file.read_bytes()
        except Exception:
            blob = None
        extracted_str = extract_string_session_from_sqlite(sess_file)
        conn.execute(
            """
            INSERT INTO accounts (
                phone, session_path, status, telegram_user_id,
                telegram_username, telegram_name, created_at,
                health, last_error, updated_at, daily_limit,
                sent_today, sent_date, access_scope, provider, session_blob, session_string
            ) VALUES (?, ?, 'connected', ?, ?, ?, ?, 'active', NULL, ?, 25, 0, ?, 'workspace', 'telegram', ?, ?)
            """,
            (phone, rel_path, tg_uid, tg_user, tg_name, now, now, utc_today(), blob, extracted_str),
        )
        connected_phones.add(phone)
        restored += 1

    # Remove stale duplicate pending_code/error rows for already connected phones
    for c_phone in connected_phones:
        if c_phone:
            stale_rows = conn.execute(
                "SELECT id FROM accounts WHERE phone = ? AND status IN ('pending_code', 'error')",
                (c_phone,),
            ).fetchall()
            stale_ids = [int(r[0]) for r in stale_rows]
            if stale_ids:
                _unlink_account_refs(conn, stale_ids)
                conn.execute(
                    "DELETE FROM accounts WHERE phone = ? AND status IN ('pending_code', 'error')",
                    (c_phone,),
                )

    deduplicate_accounts(conn)
    reconcile_disk_sessions_with_db(conn)
    backfill_account_device_profiles(conn)
    conn.commit()
    return restored


async def start_login(
    conn,
    phone: str,
    gateway,
    proxy_url: str | None = None,
    access_scope: str = "workspace",
    provider: str = "telegram",
    auth_mode: str = "code",
) -> dict:
    norm_provider = "whatsapp" if str(provider or "").strip().lower() == "whatsapp" else "telegram"
    norm_mode = "qr" if str(auth_mode or "").strip().lower() == "qr" else "code"
    phone = (phone or "").strip()
    if norm_provider == "whatsapp" and norm_mode == "qr" and not phone:
        phone = f"wa-qr-pending-{os.urandom(4).hex()}"
    elif not phone:
        raise ValueError("Укажите телефон")
    if norm_provider == "whatsapp" and norm_mode == "code" and len(_phone_digits(phone)) < 8:
        raise ValueError("Укажите номер телефона в международном формате (+...)")
    clean_proxy = (proxy_url or "").strip() or None
    clean_scope = (access_scope or "workspace").strip() or "workspace"

    existing_connected = _find_duplicate_account(conn, norm_provider, phone=phone, connected_only=True)
    if existing_connected and str(existing_connected.get("health") or "active") not in (
        "unauthorized",
        "banned",
        "frozen",
    ):
        raise ValueError(f"Аккаунт {existing_connected['phone']} уже добавлен")

    # Remove previous unfinished/error attempts for the same phone + provider
    target_digits = _phone_digits(phone)
    all_pending = conn.execute(
        """
        SELECT id, phone, session_path FROM accounts
        WHERE COALESCE(provider, 'telegram') = ? AND status IN ('pending_code', 'error')
        """,
        (norm_provider,),
    ).fetchall()
    stale_rows = [
        r
        for r in all_pending
        if str(r[1] or "").strip() == phone or (target_digits and _phone_digits(r[1]) == target_digits)
    ]
    stale_ids = [int(r[0]) for r in stale_rows]
    if stale_ids:
        if gateway is not None:
            for r in stale_rows:
                old_path = str(r[2] or "").strip() or f"data/sessions/{int(r[0])}.session"
                if hasattr(gateway, "_close"):
                    try:
                        await gateway._close(old_path)
                    except Exception:
                        pass
                if hasattr(gateway, "clear_session"):
                    gateway.clear_session(old_path)
        _unlink_account_refs(conn, stale_ids)
        for sid in stale_ids:
            conn.execute("DELETE FROM accounts WHERE id = ?", (sid,))
    conn.commit()
    if existing_connected:
        account_id = int(existing_connected["id"])
        old_sess_rel = existing_connected["session_path"] or f"data/sessions/{account_id}.session"
        old_sess = resolve_session(old_sess_rel)
        is_dead_health = str(existing_connected.get("health") or "") in (
            "unauthorized",
            "banned",
            "frozen",
        )
        has_live_sess = not is_dead_health and (
            _is_valid_string_session(existing_connected.get("session_string"))
            or _inspect_telethon_session(old_sess_rel) is not None
            or _inspect_whatsmeow_db(old_sess_rel) is not None
        )
        if gateway is not None and hasattr(gateway, "_close"):
            try:
                await gateway._close(old_sess_rel)
            except Exception:
                pass
        if not has_live_sess and gateway is not None and hasattr(gateway, "clear_session"):
            gateway.clear_session(old_sess_rel)
        if old_sess.exists() and not has_live_sess:
            try:
                if _inspect_telethon_session(old_sess_rel) is not None:
                    backup_p = old_sess.parent / f"backup_{old_sess.name}"
                    shutil.move(str(old_sess), str(backup_p))
                else:
                    old_sess.unlink()
                for suffix in ("-journal", "-wal", "-shm"):
                    sidecar = old_sess.with_name(old_sess.name + suffix)
                    if sidecar.exists():
                        sidecar.unlink()
            except Exception:
                pass
        if not has_live_sess:
            conn.execute(
                "UPDATE accounts SET session_blob = NULL, session_string = NULL, health = NULL, last_error = NULL WHERE id = ?",
                (account_id,),
            )
    else:
        cur = conn.execute(
            """
            INSERT INTO accounts (phone, session_path, status, created_at, proxy_url, access_scope, provider)
            VALUES (?, '', 'pending_code', ?, ?, ?, ?)
            """,
            (phone, utc_now(), clean_proxy, clean_scope, norm_provider),
        )
        account_id = cur.lastrowid
    db_main_file = None
    try:
        for r_db in conn.execute("PRAGMA database_list").fetchall():
            if str(r_db[1] or "") == "main" and str(r_db[2] or "").strip():
                db_main_file = Path(str(r_db[2])).resolve()
                break
    except Exception:
        db_main_file = None
    is_non_default_db = db_main_file is not None and db_main_file != DEFAULT_DB.resolve()

    if norm_provider == "whatsapp":
        if is_non_default_db:
            session_path = str(db_main_file.parent / "sessions" / f"wa_{account_id}.session")
        else:
            session_path = f"data/sessions/wa_{account_id}.session"
    else:
        session_path = f"data/sessions/{account_id}.session"
    abs_target_sess = resolve_session(session_path)
    abs_target_sess.parent.mkdir(parents=True, exist_ok=True)
    if norm_provider == "whatsapp" and not existing_connected and abs_target_sess.exists():
        for orphan_p in (
            abs_target_sess,
            abs_target_sess.with_name(abs_target_sess.name + "-wal"),
            abs_target_sess.with_name(abs_target_sess.name + "-shm"),
            abs_target_sess.with_name(abs_target_sess.name + "-journal"),
        ):
            try:
                if orphan_p.exists():
                    orphan_p.unlink()
            except Exception:
                pass
    _clear_deleted_session(conn, session_path=session_path, phone=phone)
    conn.execute(
        "UPDATE accounts SET session_path = ? WHERE id = ?",
        (session_path, account_id),
    )
    conn.commit()

    pairing_code = None
    qr_code = None

    req_sess_str: str | None = None
    try:
        if gateway is not None and hasattr(gateway, "set_proxy"):
            gateway.set_proxy(session_path, clean_proxy)
        if norm_provider == "whatsapp":
            if gateway is None:
                gateway = WhatsAppGateway()
                gateway.set_proxy(session_path, clean_proxy)
            sent_res = await gateway.request_code(
                phone, session_path, auth_mode=norm_mode
            )
            p_info = (getattr(gateway, "_pending", None) or {}).get(session_path) or {}
            pairing_code = p_info.get("pairing_code") or None
            qr_code = p_info.get("qr_code") or None
        else:
            sent_res = await gateway.request_code(phone, session_path)
        if isinstance(sent_res, dict):
            phone_code_hash = str(sent_res.get("phone_code_hash") or sent_res.get("code_hash") or "")
            req_sess_str = (sent_res.get("session_string") or "").strip() or None
        else:
            phone_code_hash = str(sent_res or "")
            if gateway is not None and hasattr(gateway, "get_session_string"):
                req_sess_str = (gateway.get_session_string(session_path) or "").strip() or None
    except Exception as exc:
        _mark_error(conn, account_id)
        if isinstance(exc, GatewayError):
            raise
        raise GatewayError(str(exc) or "Не удалось отправить код") from exc
    if req_sess_str and gateway is not None and hasattr(gateway, "set_session_string"):
        gateway.set_session_string(session_path, req_sess_str)
    if norm_provider == "whatsapp" and str(phone_code_hash).startswith("wa:connected:"):
        wa_info = _inspect_whatsmeow_db(session_path) or {}
        conn.execute(
            """
            UPDATE accounts
            SET phone_code_hash = ?, status = 'connected', health = 'active', last_error = NULL,
                phone = COALESCE(?, phone), telegram_user_id = COALESCE(?, telegram_user_id),
                telegram_name = COALESCE(?, telegram_name), wa_jid = COALESCE(?, wa_jid),
                pairing_code = NULL, qr_code = NULL, provider = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                phone_code_hash,
                wa_info.get("phone"),
                wa_info.get("telegram_user_id"),
                wa_info.get("name"),
                wa_info.get("wa_jid"),
                norm_provider,
                utc_now(),
                account_id,
            ),
        )
        conn.commit()
        _sync_session_blob(conn, account_id)
        return {
            "id": account_id,
            "status": "connected",
            "provider": norm_provider,
            "pairing_code": None,
            "qr_code": None,
            "proxy_url": clean_proxy,
            "access_scope": clean_scope,
        }
    conn.execute(
        """
        UPDATE accounts
        SET phone_code_hash = ?, status = 'pending_code', pairing_code = ?, qr_code = ?,
            provider = ?, session_string = COALESCE(?, session_string)
        WHERE id = ?
        """,
        (phone_code_hash, pairing_code, qr_code, norm_provider, req_sess_str, account_id),
    )
    conn.commit()
    return {
        "id": account_id,
        "status": "pending_code",
        "provider": norm_provider,
        "pairing_code": pairing_code,
        "qr_code": qr_code,
        "proxy_url": clean_proxy,
        "access_scope": clean_scope,
    }


def _format_proxy(raw_proxy) -> str | None:
    if not raw_proxy:
        return None
    if isinstance(raw_proxy, str):
        return raw_proxy.strip() or None
    if isinstance(raw_proxy, (list, tuple)) and len(raw_proxy) >= 3:
        host = raw_proxy[1]
        port = raw_proxy[2]
        user = raw_proxy[4] if len(raw_proxy) > 4 and raw_proxy[4] else None
        pwd = raw_proxy[5] if len(raw_proxy) > 5 and raw_proxy[5] else None
        if user and pwd:
            return f"socks5://{user}:{pwd}@{host}:{port}"
        return f"socks5://{host}:{port}"
    if isinstance(raw_proxy, dict):
        host = raw_proxy.get("host") or raw_proxy.get("addr")
        port = raw_proxy.get("port")
        if host and port:
            return f"socks5://{host}:{port}"
    return None


def _decode_b64(raw_b64: str) -> bytes:
    cleaned = str(raw_b64 or "").strip()
    if "," in cleaned and cleaned.lower().startswith("data:"):
        cleaned = cleaned.split(",", 1)[1].strip()
    return base64.b64decode(cleaned)


def import_session_accounts(conn, gateway, payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("Некорректные данные для импорта сессий")

    default_proxy = _format_proxy(payload.get("proxy_url"))
    default_scope = (payload.get("access_scope") or "workspace").strip() or "workspace"
    raw_limit = payload.get("daily_limit")
    default_limit = 20
    if raw_limit is not None:
        try:
            default_limit = max(1, min(500, int(raw_limit)))
        except (TypeError, ValueError):
            default_limit = 20

    entries: list[dict] = []

    zip_b64 = payload.get("zip_base64")
    if zip_b64 and str(zip_b64).strip():
        try:
            zip_bytes = _decode_b64(str(zip_b64))
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except Exception as exc:
            raise ValueError("Не удалось распаковать ZIP-архив с сессиями") from exc

        json_by_stem: dict[str, dict] = {}
        sess_by_stem: dict[str, bytes] = {}
        ordered_stems: list[str] = []

        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            base_name = Path(name).name
            if "__MACOSX" in name or base_name.startswith(".") or base_name.startswith("._"):
                continue
            suffix = Path(base_name).suffix.lower()
            stem = Path(base_name).stem
            if suffix == ".json":
                try:
                    parsed_json = json.loads(zf.read(name).decode("utf-8", "replace"))
                    if isinstance(parsed_json, dict):
                        json_by_stem[stem] = parsed_json
                        if stem not in ordered_stems:
                            ordered_stems.append(stem)
                except Exception:
                    continue
            elif suffix == ".session":
                sess_by_stem[stem] = zf.read(name)
                if stem not in ordered_stems:
                    ordered_stems.append(stem)

        for stem in ordered_stems:
            entries.append(
                {
                    "filename": f"{stem}.session",
                    "json_data": json_by_stem.get(stem, {}),
                    "_session_bytes": sess_by_stem.get(stem),
                }
            )

    raw_sessions = payload.get("sessions")
    if isinstance(raw_sessions, list):
        for s in raw_sessions:
            if isinstance(s, dict):
                entries.append(dict(s))

    if not entries and any(
        payload.get(k) for k in ("phone", "session_string", "json_data", "filename", "session_base64")
    ):
        entries.append(dict(payload))

    if not entries:
        raise ValueError("Не найдено ни одной сессии или JSON-профиля для импорта")

    now = utc_now()
    today = utc_today()
    imported: list[dict] = []

    for idx, entry in enumerate(entries, start=1):
        jdata = entry.get("json_data")
        if isinstance(jdata, str) and jdata.strip():
            try:
                jdata = json.loads(jdata)
            except Exception:
                jdata = {}
        if not isinstance(jdata, dict):
            jdata = {}

        filename = str(entry.get("filename") or "").strip()
        stem = Path(filename).stem if filename else ""

        raw_provider = (
            entry.get("provider")
            or jdata.get("provider")
            or payload.get("provider")
            or ""
        )
        raw_provider_str = str(raw_provider).strip().lower()
        raw_wa_jid = entry.get("wa_jid") or jdata.get("wa_jid") or jdata.get("jid")
        if (
            raw_provider_str == "whatsapp"
            or stem.lower().startswith("wa_")
            or ".wa." in filename.lower()
            or bool(raw_wa_jid)
        ):
            provider = "whatsapp"
        else:
            provider = "telegram"

        raw_phone = (
            entry.get("phone")
            or jdata.get("phone")
            or jdata.get("phone_number")
            or (str(raw_wa_jid).split("@")[0] if raw_wa_jid else "")
            or stem
            or ""
        )
        phone_str = str(raw_phone).strip()
        digits = re.sub(r"\D", "", phone_str)
        if digits and len(digits) >= 7:
            phone = f"+{digits}"
        elif provider == "whatsapp":
            phone = ""
        else:
            raise ValueError(
                "Укажите корректный номер телефона для импортируемой сессии Telegram"
            )

        phone_digits = re.sub(r"\D", "", phone)
        wa_jid = (
            str(raw_wa_jid).strip()
            if raw_wa_jid
            else (
                f"{phone_digits}@s.whatsapp.net"
                if (provider == "whatsapp" and phone_digits)
                else None
            )
        )

        username_raw = (
            entry.get("username")
            or entry.get("telegram_username")
            or jdata.get("username")
            or jdata.get("telegram_username")
        )
        username = str(username_raw).strip().lstrip("@") if username_raw else None
        if not username or provider == "whatsapp":
            username = None if provider == "whatsapp" else username

        first_name = str(entry.get("first_name") or jdata.get("first_name") or "").strip()
        last_name = str(entry.get("last_name") or jdata.get("last_name") or "").strip()
        combined_fl = " ".join(p for p in (first_name, last_name) if p).strip()
        default_name = (
            f"WhatsApp {phone}".strip()
            if provider == "whatsapp"
            else (username or phone)
        )
        telegram_name = (
            str(entry.get("telegram_name") or entry.get("name") or "").strip()
            or combined_fl
            or str(jdata.get("name") or jdata.get("telegram_name") or "").strip()
            or default_name
        )

        tg_id_raw = (
            entry.get("telegram_user_id")
            or entry.get("id")
            or entry.get("user_id")
            or jdata.get("id")
            or jdata.get("user_id")
            or jdata.get("telegram_user_id")
        )
        tg_id = None
        if tg_id_raw is not None and str(tg_id_raw).strip():
            try:
                tg_id = int(str(tg_id_raw).strip())
            except ValueError:
                tg_id = None
        if tg_id is None and provider == "whatsapp" and phone_digits.isdigit():
            tg_id = int(phone_digits[-15:])

        proxy_url = (
            _format_proxy(entry.get("proxy_url"))
            or _format_proxy(entry.get("proxy"))
            or _format_proxy(jdata.get("proxy_url"))
            or _format_proxy(jdata.get("proxy"))
            or default_proxy
        )
        if provider == "whatsapp" and proxy_url:
            from app.whatsapp_gateway import _validate_wa_proxy_url

            try:
                proxy_url = _validate_wa_proxy_url(proxy_url)
            except GatewayError as exc:
                raise ValueError(str(exc)) from exc
        access_scope = (
            str(entry.get("access_scope") or default_scope or "workspace").strip()
            or "workspace"
        )
        try:
            daily_limit = max(1, min(500, int(entry.get("daily_limit") or default_limit)))
        except (TypeError, ValueError):
            daily_limit = default_limit

        raw_stem = stem or (f"wa_{phone_digits}" if provider == "whatsapp" else phone.lstrip("+"))
        safe_stem = re.sub(r"[^A-Za-z0-9_+-]", "", raw_stem) or f"session_{idx}"
        if provider == "whatsapp" and not safe_stem.lower().startswith("wa_"):
            safe_stem = f"wa_{safe_stem}"
        imp_db_file = None
        try:
            for r_db in conn.execute("PRAGMA database_list").fetchall():
                if str(r_db[1] or "") == "main" and str(r_db[2] or "").strip():
                    imp_db_file = Path(str(r_db[2])).resolve()
                    break
        except Exception:
            imp_db_file = None
        if imp_db_file is not None and imp_db_file != DEFAULT_DB.resolve():
            session_path = str(imp_db_file.parent / "sessions" / f"{safe_stem}.session")
        else:
            session_path = f"data/sessions/{safe_stem}.session"
        abs_session_path = resolve_session(session_path)
        abs_session_path.parent.mkdir(parents=True, exist_ok=True)

        sess_bytes = entry.get("_session_bytes")
        if not sess_bytes and entry.get("session_base64"):
            try:
                sess_bytes = _decode_b64(str(entry["session_base64"]))
            except Exception:
                sess_bytes = None

        wrote_session = False
        saved_session_str: str | None = None
        if sess_bytes:
            abs_session_path.write_bytes(sess_bytes)
            wrote_session = True
        else:
            raw_session_str = (
                entry.get("session_string")
                or jdata.get("session_string")
                or entry.get("string_session")
                or jdata.get("string_session")
            )
            if raw_session_str and str(raw_session_str).strip():
                s_val = str(raw_session_str).strip()
                if provider == "telegram":
                    saved_session_str = s_val

        if not wrote_session and not saved_session_str:
            raise ValueError(
                "Загрузите реальный .session / WhatsApp session файл или укажите валидную session_string (синтетические заглушки запрещены)"
            )

        if provider == "whatsapp":
            wm_info = _inspect_whatsmeow_db(session_path) if wrote_session else None
            if wm_info is None:
                if wrote_session and abs_session_path.exists():
                    try:
                        abs_session_path.unlink()
                    except Exception:
                        pass
                raise ValueError(
                    "Невалидный файл сессии WhatsApp: требуется реальная авторизованная БД whatsmeow_device"
                )
            phone = wm_info["phone"]
            wa_jid = wm_info["wa_jid"]
            tg_id = wm_info["telegram_user_id"]
            if telegram_name.startswith("WhatsApp +") and wm_info.get("name"):
                telegram_name = wm_info["name"]
            if raw_limit is None and "daily_limit" not in entry:
                daily_limit = DEFAULT_WA_DAILY_LIMIT
        else:
            if not saved_session_str and abs_session_path.exists():
                saved_session_str = extract_string_session_from_sqlite(abs_session_path)
            has_real_tg_sqlite = _inspect_telethon_session(session_path) is not None
            has_real_tg_str = _is_valid_string_session(saved_session_str)
            if not has_real_tg_sqlite and not has_real_tg_str:
                if wrote_session and abs_session_path.exists():
                    try:
                        abs_session_path.unlink()
                    except Exception:
                        pass
                raise ValueError(
                    "Невалидная сессия Telegram: требуется реальный .session файл (auth_key 256 байт) или валидная StringSession"
                )

        app_id_val = (
            entry.get("app_id")
            or entry.get("api_id")
            or jdata.get("app_id")
            or jdata.get("api_id")
            or payload.get("app_id")
            or payload.get("api_id")
        )
        app_hash_val = (
            entry.get("app_hash")
            or entry.get("api_hash")
            or jdata.get("app_hash")
            or jdata.get("api_hash")
            or payload.get("app_hash")
            or payload.get("api_hash")
        )
        device_model = str(
            entry.get("device")
            or entry.get("device_model")
            or jdata.get("device")
            or jdata.get("device_model")
            or ""
        ).strip() or None
        system_version = str(
            entry.get("sdk")
            or entry.get("system_version")
            or jdata.get("sdk")
            or jdata.get("system_version")
            or ""
        ).strip() or None
        app_version = str(
            entry.get("app_version")
            or jdata.get("app_version")
            or ""
        ).strip() or None
        lang_code = str(
            entry.get("lang_code")
            or entry.get("lang_pack")
            or jdata.get("lang_code")
            or jdata.get("lang_pack")
            or ""
        ).strip() or None
        system_lang_code = str(
            entry.get("system_lang_code")
            or entry.get("system_lang_pack")
            or jdata.get("system_lang_code")
            or jdata.get("system_lang_pack")
            or ""
        ).strip() or None
        parsed_api_id = None
        if app_id_val is not None and str(app_id_val).strip():
            try:
                parsed_api_id = int(str(app_id_val).strip())
            except ValueError:
                parsed_api_id = None
        parsed_api_hash = str(app_hash_val).strip() if app_hash_val else None

        if gateway is not None:
            if hasattr(gateway, "set_proxy"):
                gateway.set_proxy(session_path, proxy_url)
            if hasattr(gateway, "set_session_string") and _is_valid_string_session(saved_session_str):
                gateway.set_session_string(session_path, saved_session_str)
            if hasattr(gateway, "set_app_credentials") and parsed_api_id and parsed_api_hash:
                gateway.set_app_credentials(session_path, parsed_api_id, parsed_api_hash)
            if hasattr(gateway, "set_device_profile"):
                gateway.set_device_profile(
                    session_path,
                    device_model=device_model,
                    system_version=system_version,
                    app_version=app_version,
                    lang_code=lang_code,
                    system_lang_code=system_lang_code,
                )

        _clear_deleted_session(conn, session_path=session_path, phone=phone)

        dup_connected = _find_duplicate_account(
            conn,
            provider,
            phone=phone,
            telegram_user_id=tg_id,
            connected_only=True,
        )
        if dup_connected and str(dup_connected.get("health") or "active") not in (
            "unauthorized",
            "banned",
            "frozen",
        ):
            raise ValueError(f"Аккаунт {dup_connected['phone']} уже добавлен")

        existing = _find_duplicate_account(
            conn,
            provider,
            phone=phone,
            telegram_user_id=tg_id,
            connected_only=False,
        )
        if existing:
            account_id = int(existing["id"])
            updated_sess_str = saved_session_str if saved_session_str else existing.get("session_string")
            conn.execute(
                """
                UPDATE accounts
                SET session_path = ?, status = 'connected', telegram_user_id = ?,
                    telegram_username = ?, telegram_name = ?, health = 'active',
                    last_error = NULL, updated_at = ?, daily_limit = ?,
                    proxy_url = ?, access_scope = ?, provider = ?, wa_jid = ?,
                    api_id = COALESCE(?, api_id), api_hash = COALESCE(?, api_hash),
                    device_model = COALESCE(?, device_model), system_version = COALESCE(?, system_version),
                    app_version = COALESCE(?, app_version), lang_code = COALESCE(?, lang_code),
                    system_lang_code = COALESCE(?, system_lang_code),
                    session_string = ?
                WHERE id = ?
                """,
                (
                    session_path,
                    tg_id,
                    username,
                    telegram_name,
                    now,
                    daily_limit,
                    proxy_url,
                    access_scope,
                    provider,
                    wa_jid,
                    parsed_api_id,
                    parsed_api_hash,
                    device_model,
                    system_version,
                    app_version,
                    lang_code,
                    system_lang_code,
                    updated_sess_str,
                    account_id,
                ),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO accounts (
                    phone, session_path, status, telegram_user_id,
                    telegram_username, telegram_name, created_at,
                    health, last_error, updated_at, daily_limit,
                    sent_today, sent_date, proxy_url, access_scope,
                    provider, wa_jid, api_id, api_hash,
                    device_model, system_version, app_version, lang_code, system_lang_code,
                    session_string
                ) VALUES (?, ?, 'connected', ?, ?, ?, ?, 'active', NULL, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    phone,
                    session_path,
                    tg_id,
                    username,
                    telegram_name,
                    now,
                    now,
                    daily_limit,
                    today,
                    proxy_url,
                    access_scope,
                    provider,
                    wa_jid,
                    parsed_api_id,
                    parsed_api_hash,
                    device_model,
                    system_version,
                    app_version,
                    lang_code,
                    system_lang_code,
                    saved_session_str,
                ),
            )
            account_id = int(cur.lastrowid)

        backfill_account_device_profiles(conn, account_id=account_id)
        if (
            provider != "whatsapp"
            and gateway is not None
            and hasattr(gateway, "set_device_profile")
            and not any((device_model, system_version, app_version, lang_code, system_lang_code))
        ):
            gateway.set_device_profile(session_path, **deterministic_device_profile(account_id))
        _sync_session_blob(conn, account_id)

        imported.append(
            _public(_load(conn, account_id), nodemaven_configured=_is_nodemaven_configured(conn))
        )

    conn.commit()
    return imported


async def confirm_login(conn, account_id: int, code: str, gateway) -> dict:
    code = (code or "").strip()
    if not code:
        raise ValueError("Укажите код")
    account = _load(conn, account_id)
    if account["status"] == "connected":
        return {"status": "connected"}
    provider = str(account.get("provider") or "telegram").strip().lower()
    active_gateway = gateway
    if provider == "whatsapp" and not isinstance(active_gateway, WhatsAppGateway):
        active_gateway = WhatsAppGateway()
    try:
        result = await active_gateway.confirm_code(
            account["phone"],
            code,
            account.get("phone_code_hash") or "",
            account["session_path"],
        )
    except GatewayError:
        raise
    except Exception as exc:
        _mark_error(conn, account_id)
        raise GatewayError(str(exc) or "Не удалось подтвердить код") from exc
    return await _finish_login(conn, account_id, result, active_gateway)


async def confirm_password(conn, account_id: int, password: str, gateway) -> dict:
    password = password or ""
    if not password:
        raise ValueError("Укажите пароль")
    account = _load(conn, account_id)
    if account["status"] == "connected":
        return {"status": "connected"}
    if account["status"] != "pending_password":
        raise ValueError("Аккаунт не ждёт пароль")
    try:
        result = await gateway.confirm_password(password, account["session_path"])
    except GatewayError:
        raise
    except (ConnectionError, TimeoutError, OSError) as exc:
        # Обрыв connect не стирает pending_password: пароль можно ввести снова.
        raise GatewayError(str(exc) or "Не удалось подтвердить пароль") from exc
    except Exception as exc:
        _mark_error(conn, account_id)
        raise GatewayError(str(exc) or "Не удалось подтвердить пароль") from exc
    return await _finish_login(conn, account_id, result, gateway)


def _clip_error(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:500]


def record_account_health_transition(
    conn,
    account_id: int,
    health: str,
    last_error,
    *,
    opener=None,
) -> bool:
    if health not in SESSION_HEALTH:
        raise ValueError("Неизвестный статус сессии")
    now = utc_now()
    clipped_err = _clip_error(last_error)
    prev_row = conn.execute(
        "SELECT id, phone, telegram_username, telegram_name, health FROM accounts WHERE id = ?",
        (int(account_id),),
    ).fetchone()
    prev_dict = as_dict(prev_row) if prev_row is not None else {}
    prev_health = str(prev_dict.get("health") or "")

    conn.execute(
        """
        UPDATE accounts
        SET health = ?, last_error = ?, updated_at = ?
        WHERE id = ?
        """,
        (health, clipped_err, now, int(account_id)),
    )

    alert_state_key = f"account_health_alert_state:{int(account_id)}"
    if health == "active":
        try:
            conn.execute(
                """
                INSERT INTO kv_settings (key, value, updated_at)
                VALUES (?, 'active', ?)
                ON CONFLICT(key) DO UPDATE SET value = 'active', updated_at = excluded.updated_at
                """,
                (alert_state_key, now),
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
        return False

    if health not in ("unauthorized", "frozen", "banned"):
        conn.commit()
        return False

    last_alerted = ""
    try:
        st_row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = ?",
            (alert_state_key,),
        ).fetchone()
        if st_row is not None and st_row[0] is not None:
            last_alerted = str(st_row[0]).strip()
    except sqlite3.OperationalError:
        last_alerted = prev_health

    if last_alerted == health:
        conn.commit()
        return False

    event = {
        "account_id": int(account_id),
        "phone": prev_dict.get("phone"),
        "telegram_username": prev_dict.get("telegram_username"),
        "telegram_name": prev_dict.get("telegram_name"),
        "prev_health": prev_health or None,
        "health": health,
        "last_error": clipped_err,
        "created_at": now,
    }
    try:
        conn.execute(
            """
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (alert_state_key, health, now),
        )
        log_row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'account_health_alerts_log'"
        ).fetchone()
        history: list[dict] = []
        if log_row is not None and log_row[0]:
            try:
                parsed_hist = json.loads(str(log_row[0]))
                if isinstance(parsed_hist, list):
                    history = [x for x in parsed_hist if isinstance(x, dict)]
            except Exception:
                history = []
        history.append(event)
        history = history[-100:]
        conn.execute(
            """
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES ('account_health_alerts_log', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (json.dumps(history, ensure_ascii=False), now),
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()

    acc_label = (
        (f"@{prev_dict['telegram_username']}" if prev_dict.get("telegram_username") else None)
        or prev_dict.get("telegram_name")
        or prev_dict.get("phone")
        or f"#{account_id}"
    )
    logger.warning(
        "Account health alert: account %s (id=%s) transitioned %s -> %s (%s)",
        acc_label,
        account_id,
        prev_health or "none",
        health,
        clipped_err or "no error detail",
    )

    try:
        import app.digest as digest_mod

        token, chat_id = digest_mod.resolve_colleague_bot_credentials(conn, default_chat_id=True)
        if token and chat_id and os.environ.get("DISABLE_BOT_ALERTS") != "1":
            alert_text = (
                f"⚠️ Аккаунт рассылки {acc_label} (ID {account_id}) перешёл в статус {health}.\n"
                f"Причина: {clipped_err or 'не указана'}"
            )
            digest_mod._post(
                digest_mod.SEND_URL.format(token=token),
                {"chat_id": str(chat_id), "text": alert_text},
                opener,
            )
    except Exception as exc:
        logger.warning("Account health digest alert failed for id=%s: %s", account_id, exc)
    return True


def _write_health(conn, account_id: int, health: str, last_error) -> None:
    # health is the live session. accounts.status stays the login step.
    record_account_health_transition(conn, account_id, health, last_error)


async def _probe_health(gateway, session_path: str) -> dict:
    try:
        probe = await gateway.session_health(session_path)
    except (ConnectionError, TimeoutError, OSError) as exc:
        return {"health": "offline", "last_error": _clip_error(exc) or "Нет связи"}
    except Exception as exc:
        return {
            "health": "offline",
            "last_error": _clip_error(exc) or "Не удалось проверить сессию",
        }
    if not isinstance(probe, dict) or probe.get("health") not in SESSION_HEALTH:
        return {
            "health": "offline",
            "last_error": _clip_error(probe.get("last_error") if isinstance(probe, dict) else None)
            or "Не удалось проверить сессию",
        }
    health = probe["health"]
    if health == "active":
        return {"health": "active", "last_error": None}
    return {
        "health": health,
        "last_error": _clip_error(probe.get("last_error")) or "Сессия не активна",
    }


async def _finish_login(conn, account_id: int, result: dict, gateway) -> dict:
    _apply_result(conn, account_id, result)
    if result.get("status") == "connected":
        account = _load(conn, account_id)
        sess_str = (result.get("session_string") or "").strip() or (account.get("session_string") or "").strip() or None
        if not sess_str and account.get("session_path"):
            sess_str = extract_string_session_from_sqlite(resolve_session(account["session_path"]))
        if sess_str:
            conn.execute(
                "UPDATE accounts SET session_string = COALESCE(?, session_string) WHERE id = ?",
                (sess_str, account_id),
            )
            conn.commit()
            if gateway is not None and hasattr(gateway, "set_session_string"):
                gateway.set_session_string(account["session_path"], sess_str)
        backfill_account_device_profiles(conn, account_id=account_id)
        _sync_session_blob(conn, account_id)
        probe = await _probe_health(gateway, account["session_path"])
        _write_health(conn, account_id, probe["health"], probe["last_error"])
    return {"status": result["status"]}


async def probe_account(conn, account_id: int, gateway) -> dict:
    account = _load(conn, account_id)
    if account["status"] != "connected":
        raise ValueError("Аккаунт не подключён")
    probe = await _probe_health(gateway, account["session_path"])
    if (
        probe["health"] == "active"
        and str(account.get("health") or "") == "frozen"
        and _is_spambot_restriction_error(account.get("last_error"))
    ):
        if gateway is not None and hasattr(gateway, "probe_spambot"):
            sb = await gateway.probe_spambot(account["session_path"])
            try:
                from app.outreach import (
                    _SPAMBOT_PROBE_CACHE,
                    _spambot_cache_key,
                    clear_account_cooldown,
                    set_account_cooldown_until,
                )

                _SPAMBOT_PROBE_CACHE.pop(
                    _spambot_cache_key(conn, account_id, account["session_path"]),
                    None,
                )
                if sb.get("limited") is False:
                    clear_account_cooldown(conn, account_id)
                elif sb.get("limited") is True and sb.get("limited_until_utc"):
                    set_account_cooldown_until(conn, account_id, sb["limited_until_utc"])
            except Exception:
                pass
            if sb.get("limited") is False:
                probe = {"health": "active", "last_error": None}
            elif sb.get("limited") is True:
                probe = {
                    "health": "frozen",
                    "last_error": f"@SpamBot: {sb.get('reply_text') or ''}".strip()[:500],
                }
            else:
                probe = {"health": "frozen", "last_error": account.get("last_error")}
        else:
            probe = {"health": "frozen", "last_error": account.get("last_error")}
    _write_health(conn, account_id, probe["health"], probe["last_error"])
    return {
        "status": account["status"],
        "health": probe["health"],
        "last_error": probe["last_error"],
    }


reconnect_account = probe_account


async def check_all_accounts(
    conn, gateway=None, wa_gateway=None, *, recheck_dead: bool = False
) -> dict:
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    wa_gw = wa_gateway or WhatsAppGateway()
    for row in rows:
        d = as_dict(row)
        if d.get("status") != "connected":
            continue
        health_val = str(d.get("health") or "active").strip().lower()
        if not recheck_dead and health_val in ("unauthorized", "banned"):
            continue
        account_id = int(d["id"])
        session_path = str(d.get("session_path") or "")
        provider = str(d.get("provider") or "telegram").strip().lower()
        is_demo = session_path.startswith("data/sessions/demo")
        if provider == "whatsapp":
            await probe_account(conn, account_id, wa_gw)
        elif is_demo and gateway is None:
            _write_health(conn, account_id, "active", None)
        elif gateway is not None:
            await probe_account(conn, account_id, gateway)
    mtproto_ready = bool(
        os.getenv("TG_API_ID", "").strip() and os.getenv("TG_API_HASH", "").strip()
    )
    return {
        "accounts": list_accounts(conn),
        "mtproto_ready": mtproto_ready,
    }


def _apply_result(conn, account_id: int, result: dict) -> None:
    status = result["status"]
    if status == "connected":
        current = _load(conn, account_id)
        dup = _find_duplicate_account(
            conn,
            str(current.get("provider") or "telegram"),
            phone=result.get("phone") or current.get("phone"),
            telegram_user_id=result.get("telegram_user_id"),
            exclude_id=account_id,
            connected_only=True,
        )
        if dup and str(dup.get("health") or "active") not in (
            "unauthorized",
            "banned",
            "frozen",
        ):
            _unlink_account_refs(conn, [int(account_id)])
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            conn.commit()
            raise ValueError(f"Аккаунт {dup['phone']} уже добавлен")
        sess_str = (result.get("session_string") or "").strip() or None
        conn.execute(
            """
            UPDATE accounts
            SET status = ?, phone = COALESCE(?, phone), telegram_user_id = ?, telegram_username = ?, telegram_name = ?,
                wa_jid = COALESCE(?, wa_jid), session_string = COALESCE(?, session_string),
                pairing_code = NULL, qr_code = NULL
            WHERE id = ?
            """,
            (
                status,
                result.get("phone"),
                result.get("telegram_user_id"),
                result.get("telegram_username"),
                result.get("telegram_name"),
                result.get("wa_jid"),
                sess_str,
                account_id,
            ),
        )
        conn.commit()
        _sync_session_blob(conn, account_id)
    else:
        sess_str = (result.get("session_string") or "").strip() or None
        conn.execute(
            "UPDATE accounts SET status = ?, session_string = COALESCE(?, session_string) WHERE id = ?",
            (status, sess_str, account_id),
        )
        conn.commit()
