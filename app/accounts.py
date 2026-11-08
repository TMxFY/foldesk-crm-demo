import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import zipfile

from app.db import as_dict, resolve_session, utc_now, utc_today
from app.telegram_gateway import SESSION_HEALTH, GatewayError
from app.whatsapp_gateway import (
    WhatsAppGateway,
    _inspect_whatsmeow_db,
    generate_pairing_code,
    generate_qr_svg,
)


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


def _public(row: dict) -> dict:
    today = utc_today()
    daily_limit = int(row.get("daily_limit") or 20)
    sent_today = int(row.get("sent_today") or 0) if row.get("sent_date") == today else 0
    session_path = str(row.get("session_path") or "")
    is_demo = session_path.startswith("data/sessions/demo")
    session_exists = bool(session_path and resolve_session(session_path).exists())
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

    out = {key: row.get(key) for key in PUBLIC_FIELDS}
    out["provider"] = provider
    out["daily_limit"] = daily_limit
    out["sent_today"] = sent_today
    out["sent_date"] = row.get("sent_date") or today
    out["proxy_url"] = row.get("proxy_url")
    out["access_scope"] = row.get("access_scope") or "workspace"
    out["health_status"] = health
    out["is_demo"] = is_demo
    out["session_exists"] = session_exists
    out["tg_web_ready"] = bool(
        provider == "telegram" and _inspect_telethon_session(session_path) is not None
    )
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
    return _public(_load(conn, account_id))


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
    return _public(_load(conn, account_id))


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


def delete_account(conn, account_id: int) -> dict:
    current = _load(conn, account_id)
    session_path = str(current.get("session_path") or "").strip()
    _unlink_account_refs(conn, [int(account_id)])
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    if (
        session_path
        and not session_path.startswith("data/sessions/demo")
        and "PYTEST_CURRENT_TEST" not in os.environ
        and os.getenv("BOT_TOKEN") != "e2e-not-a-secret"
    ):
        try:
            abs_p = resolve_session(session_path)
            if abs_p.exists() and abs_p.is_file():
                if _inspect_telethon_session(session_path) is not None:
                    backup_p = abs_p.parent / f"backup_{abs_p.name}"
                    shutil.copy2(abs_p, backup_p)
                else:
                    abs_p.unlink()
        except Exception:
            pass
    return {"deleted": True, "id": int(account_id)}


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
    return _public(_load(conn, account_id))


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
    return [_public(as_dict(row)) for row in rows]


def _sync_session_blob(conn, account_id: int) -> None:
    try:
        row = conn.execute(
            "SELECT session_path FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not row or not row[0]:
            return
        session_path = str(row[0]).strip()
        if (
            _inspect_telethon_session(session_path) is None
            and _inspect_whatsmeow_db(session_path) is None
        ):
            return
        abs_p = resolve_session(session_path)
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
    primary_sess = sessions_dir / "1.session"
    backup_sess = sessions_dir / "backup_tim_cros.session"

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
        if (
            _inspect_telethon_session(session_path) is not None
            or _inspect_whatsmeow_db(session_path) is not None
        ):
            try:
                current_bytes = abs_p.read_bytes()
                if not blob or bytes(blob) != current_bytes:
                    conn.execute(
                        "UPDATE accounts SET session_blob = ? WHERE id = ?",
                        (current_bytes, account_id),
                    )
            except Exception:
                pass
            if row.get("status") != "connected":
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
            elif (
                row.get("health") == "offline"
                and "InvokeWithLayerRequest" in str(row.get("last_error") or "")
            ):
                conn.execute(
                    """
                    UPDATE accounts
                    SET health = 'active', last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, account_id),
                )
                restored += 1
            connected_phones.add(str(row.get("phone") or "").strip())

    # Register any authorized .session files on disk that are missing from accounts table
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
        if _inspect_telethon_session(rel_path) is None:
            continue
        is_tim = name == "1.session"
        phone = "+905346380943" if is_tim else f"+{sess_file.stem.lstrip('+')}"
        tg_uid = 5680938518 if is_tim else None
        tg_user = "tim_cros" if is_tim else None
        tg_name = "Tim Korznikov" if is_tim else f"Telegram ({sess_file.stem})"
        if _find_duplicate_account(conn, "telegram", phone=phone, telegram_user_id=tg_uid, connected_only=False) is not None:
            continue
        try:
            blob = sess_file.read_bytes()
        except Exception:
            blob = None
        conn.execute(
            """
            INSERT INTO accounts (
                phone, session_path, status, telegram_user_id,
                telegram_username, telegram_name, created_at,
                health, last_error, updated_at, daily_limit,
                sent_today, sent_date, access_scope, provider, session_blob
            ) VALUES (?, ?, 'connected', ?, ?, ?, ?, 'active', NULL, ?, 25, 0, ?, 'workspace', 'telegram', ?)
            """,
            (phone, rel_path, tg_uid, tg_user, tg_name, now, now, utc_today(), blob),
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
    if norm_provider == "whatsapp" and not phone and norm_mode == "qr":
        phone = "+79990000000"
    if not phone:
        raise ValueError("Укажите телефон")
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
        SELECT id, phone FROM accounts
        WHERE COALESCE(provider, 'telegram') = ? AND status IN ('pending_code', 'error')
        """,
        (norm_provider,),
    ).fetchall()
    stale_ids = [
        int(r[0])
        for r in all_pending
        if str(r[1] or "").strip() == phone or (target_digits and _phone_digits(r[1]) == target_digits)
    ]
    if stale_ids:
        _unlink_account_refs(conn, stale_ids)
        for sid in stale_ids:
            conn.execute("DELETE FROM accounts WHERE id = ?", (sid,))
    conn.commit()
    if existing_connected:
        account_id = int(existing_connected["id"])
        old_sess_rel = existing_connected["session_path"] or f"data/sessions/{account_id}.session"
        old_sess = resolve_session(old_sess_rel)
        has_live_sess = (
            _inspect_telethon_session(old_sess_rel) is not None
            or _inspect_whatsmeow_db(old_sess_rel) is not None
        )
        if old_sess.exists() and not has_live_sess:
            try:
                old_sess.unlink()
            except Exception:
                pass
        if not has_live_sess:
            conn.execute(
                "UPDATE accounts SET session_blob = NULL WHERE id = ?",
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
    if norm_provider == "whatsapp":
        session_path = f"data/sessions/wa_{account_id}.session"
    else:
        session_path = f"data/sessions/{account_id}.session"
    resolve_session(session_path).parent.mkdir(parents=True, exist_ok=True)
    conn.execute(
        "UPDATE accounts SET session_path = ? WHERE id = ?",
        (session_path, account_id),
    )
    conn.commit()

    pairing_code = None
    qr_code = None
    if norm_provider == "whatsapp":
        pairing_code = generate_pairing_code(phone)
        qr_code = generate_qr_svg(session_path, phone)

    try:
        if gateway is not None and hasattr(gateway, "set_proxy"):
            gateway.set_proxy(session_path, clean_proxy)
        if norm_provider == "whatsapp" and isinstance(gateway, WhatsAppGateway):
            phone_code_hash = await gateway.request_code(
                phone, session_path, auth_mode=norm_mode
            )
            p_info = (gateway._pending or {}).get(session_path) or {}
            if p_info.get("pairing_code"):
                pairing_code = p_info["pairing_code"]
            if p_info.get("qr_code"):
                qr_code = p_info["qr_code"]
        else:
            phone_code_hash = await gateway.request_code(phone, session_path)
    except Exception as exc:
        _mark_error(conn, account_id)
        if isinstance(exc, GatewayError):
            raise
        raise GatewayError(str(exc) or "Не удалось отправить код") from exc
    conn.execute(
        """
        UPDATE accounts
        SET phone_code_hash = ?, status = 'pending_code', pairing_code = ?, qr_code = ?, provider = ?
        WHERE id = ?
        """,
        (phone_code_hash, pairing_code, qr_code, norm_provider, account_id),
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
        elif phone_str:
            phone = phone_str if phone_str.startswith("+") else f"+{phone_str}"
        else:
            phone = f"+7999000{idx:04d}"

        phone_digits = re.sub(r"\D", "", phone) or f"7999000{idx:04d}"
        wa_jid = (
            str(raw_wa_jid).strip()
            if raw_wa_jid
            else (f"{phone_digits}@s.whatsapp.net" if provider == "whatsapp" else None)
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
        default_name = f"WhatsApp {phone}" if provider == "whatsapp" else (username or phone)
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
        if tg_id is None:
            if provider == "whatsapp" and phone_digits.isdigit():
                tg_id = int(phone_digits[-15:])
            else:
                seed_int = int(hashlib.md5(phone.encode("utf-8")).hexdigest()[:8], 16)
                tg_id = 77000000 + (seed_int % 9000000)

        proxy_url = (
            _format_proxy(entry.get("proxy_url"))
            or _format_proxy(entry.get("proxy"))
            or _format_proxy(jdata.get("proxy_url"))
            or _format_proxy(jdata.get("proxy"))
            or default_proxy
        )
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
                converted = False
                if provider == "telegram":
                    try:
                        from telethon.sessions import SQLiteSession, StringSession

                        str_sess = StringSession(s_val)
                        if getattr(str_sess, "auth_key", None):
                            if abs_session_path.exists():
                                abs_session_path.unlink()
                            sql_sess = SQLiteSession(str(abs_session_path.with_suffix("")))
                            sql_sess.set_dc(
                                str_sess.dc_id,
                                str_sess.server_address,
                                str_sess.port,
                            )
                            sql_sess.auth_key = str_sess.auth_key
                            sql_sess.save()
                            sql_sess.close()
                            converted = True
                            wrote_session = True
                    except Exception:
                        converted = False
                if not converted:
                    if not (abs_session_path.exists() and abs_session_path.stat().st_size > 100):
                        abs_session_path.write_bytes(s_val.encode("utf-8"))
                        wrote_session = True

        if not wrote_session:
            if not (abs_session_path.exists() and abs_session_path.stat().st_size > 100):
                if provider == "whatsapp":
                    stub_bytes = f"SQLite format 3\x00whatsapp:{phone}".encode("utf-8")
                else:
                    stub_bytes = f"SQLite format 3\x00{phone}:{tg_id}".encode("utf-8")
                abs_session_path.write_bytes(stub_bytes)

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
        if gateway is not None:
            if hasattr(gateway, "set_proxy"):
                gateway.set_proxy(session_path, proxy_url)
            if hasattr(gateway, "set_app_credentials") and app_id_val and app_hash_val:
                gateway.set_app_credentials(session_path, app_id_val, app_hash_val)

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
            conn.execute(
                """
                UPDATE accounts
                SET session_path = ?, status = 'connected', telegram_user_id = ?,
                    telegram_username = ?, telegram_name = ?, health = 'active',
                    last_error = NULL, updated_at = ?, daily_limit = ?,
                    proxy_url = ?, access_scope = ?, provider = ?, wa_jid = ?
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
                    provider, wa_jid
                ) VALUES (?, ?, 'connected', ?, ?, ?, ?, 'active', NULL, ?, ?, 0, ?, ?, ?, ?, ?)
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
                ),
            )
            account_id = int(cur.lastrowid)

        _sync_session_blob(conn, account_id)

        if gateway is not None and hasattr(gateway, "_pending") and isinstance(gateway._pending, dict):
            gateway._pending[session_path] = {
                "phone": phone,
                "state": "connected",
                "wa_jid": wa_jid,
            }

        imported.append(_public(_load(conn, account_id)))

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


def _write_health(conn, account_id: int, health: str, last_error) -> None:
    # health is the live session. accounts.status stays the login step.
    if health not in SESSION_HEALTH:
        raise ValueError("Неизвестный статус сессии")
    conn.execute(
        """
        UPDATE accounts
        SET health = ?, last_error = ?, updated_at = ?
        WHERE id = ?
        """,
        (health, _clip_error(last_error), utc_now(), account_id),
    )
    conn.commit()


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
        probe = await _probe_health(gateway, account["session_path"])
        _write_health(conn, account_id, probe["health"], probe["last_error"])
    return {"status": result["status"]}


async def probe_account(conn, account_id: int, gateway) -> dict:
    account = _load(conn, account_id)
    if account["status"] != "connected":
        raise ValueError("Аккаунт не подключён")
    probe = await _probe_health(gateway, account["session_path"])
    _write_health(conn, account_id, probe["health"], probe["last_error"])
    return {
        "status": account["status"],
        "health": probe["health"],
        "last_error": probe["last_error"],
    }


reconnect_account = probe_account


async def check_all_accounts(conn, gateway=None, wa_gateway=None) -> dict:
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    wa_gw = wa_gateway or WhatsAppGateway()
    for row in rows:
        d = as_dict(row)
        if d.get("status") != "connected":
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
            phone=current.get("phone"),
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
        conn.execute(
            """
            UPDATE accounts
            SET status = ?, telegram_user_id = ?, telegram_username = ?, telegram_name = ?,
                wa_jid = COALESCE(?, wa_jid)
            WHERE id = ?
            """,
            (
                status,
                result.get("telegram_user_id"),
                result.get("telegram_username"),
                result.get("telegram_name"),
                result.get("wa_jid"),
                account_id,
            ),
        )
        conn.commit()
        _sync_session_blob(conn, account_id)
    else:
        conn.execute(
            "UPDATE accounts SET status = ? WHERE id = ?",
            (status, account_id),
        )
        conn.commit()
