import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import zipfile

from app.db import as_dict, resolve_session, utc_now, utc_today
from app.telegram_gateway import SESSION_HEALTH, GatewayError

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
)


def _public(row: dict) -> dict:
    today = utc_today()
    daily_limit = int(row.get("daily_limit") or 20)
    sent_today = int(row.get("sent_today") or 0) if row.get("sent_date") == today else 0
    session_path = str(row.get("session_path") or "")
    is_demo = session_path.startswith("data/sessions/demo")
    session_exists = bool(session_path and resolve_session(session_path).exists())

    status = row.get("status")
    health = row.get("health")
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
        connection_label = "Подключён · MTProto активен"
    else:
        connection_label = "Подключён · сессия сохранена"

    out = {key: row.get(key) for key in PUBLIC_FIELDS}
    out["daily_limit"] = daily_limit
    out["sent_today"] = sent_today
    out["sent_date"] = row.get("sent_date") or today
    out["proxy_url"] = row.get("proxy_url")
    out["access_scope"] = row.get("access_scope") or "workspace"
    out["health_status"] = health
    out["is_demo"] = is_demo
    out["session_exists"] = session_exists
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


def list_accounts(conn) -> list:
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [_public(as_dict(row)) for row in rows]


async def start_login(
    conn,
    phone: str,
    gateway,
    proxy_url: str | None = None,
    access_scope: str = "workspace",
) -> dict:
    phone = (phone or "").strip()
    if not phone:
        raise ValueError("Укажите телефон")
    clean_proxy = (proxy_url or "").strip() or None
    clean_scope = (access_scope or "workspace").strip() or "workspace"
    cur = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, proxy_url, access_scope)
        VALUES (?, '', 'pending_code', ?, ?, ?)
        """,
        (phone, utc_now(), clean_proxy, clean_scope),
    )
    account_id = cur.lastrowid
    session_path = f"data/sessions/{account_id}.session"
    resolve_session(session_path).parent.mkdir(parents=True, exist_ok=True)
    conn.execute(
        "UPDATE accounts SET session_path = ? WHERE id = ?",
        (session_path, account_id),
    )
    conn.commit()
    try:
        phone_code_hash = await gateway.request_code(phone, session_path)
    except Exception as exc:
        _mark_error(conn, account_id)
        if isinstance(exc, GatewayError):
            raise
        raise GatewayError(str(exc) or "Не удалось отправить код") from exc
    conn.execute(
        """
        UPDATE accounts
        SET phone_code_hash = ?, status = 'pending_code'
        WHERE id = ?
        """,
        (phone_code_hash, account_id),
    )
    conn.commit()
    return {
        "id": account_id,
        "status": "pending_code",
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

        raw_phone = (
            entry.get("phone")
            or jdata.get("phone")
            or jdata.get("phone_number")
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

        username_raw = (
            entry.get("username")
            or entry.get("telegram_username")
            or jdata.get("username")
            or jdata.get("telegram_username")
        )
        username = str(username_raw).strip().lstrip("@") if username_raw else None
        if not username:
            username = None

        first_name = str(entry.get("first_name") or jdata.get("first_name") or "").strip()
        last_name = str(entry.get("last_name") or jdata.get("last_name") or "").strip()
        combined_fl = " ".join(p for p in (first_name, last_name) if p).strip()
        telegram_name = (
            str(entry.get("telegram_name") or entry.get("name") or "").strip()
            or combined_fl
            or str(jdata.get("name") or jdata.get("telegram_name") or "").strip()
            or username
            or phone
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

        safe_stem = re.sub(r"[^A-Za-z0-9_+-]", "", stem or phone.lstrip("+")) or f"session_{idx}"
        session_path = f"data/sessions/{safe_stem}.session"
        abs_session_path = resolve_session(session_path)
        abs_session_path.parent.mkdir(parents=True, exist_ok=True)

        sess_bytes = entry.get("_session_bytes")
        if not sess_bytes and entry.get("session_base64"):
            try:
                sess_bytes = _decode_b64(str(entry["session_base64"]))
            except Exception:
                sess_bytes = None
        if not sess_bytes and entry.get("session_string"):
            sess_bytes = str(entry["session_string"]).encode("utf-8")
        if not sess_bytes:
            sess_bytes = f"SQLite format 3\x00{phone}:{tg_id}".encode("utf-8")

        abs_session_path.write_bytes(sess_bytes)

        existing = conn.execute(
            "SELECT id FROM accounts WHERE phone = ? ORDER BY id LIMIT 1",
            (phone,),
        ).fetchone()
        if existing:
            account_id = int(existing["id"])
            conn.execute(
                """
                UPDATE accounts
                SET session_path = ?, status = 'connected', telegram_user_id = ?,
                    telegram_username = ?, telegram_name = ?, health = 'active',
                    last_error = NULL, updated_at = ?, daily_limit = ?,
                    proxy_url = ?, access_scope = ?
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
                    sent_today, sent_date, proxy_url, access_scope
                ) VALUES (?, ?, 'connected', ?, ?, ?, ?, 'active', NULL, ?, ?, 0, ?, ?, ?)
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
                ),
            )
            account_id = int(cur.lastrowid)

        if gateway is not None and hasattr(gateway, "_pending") and isinstance(gateway._pending, dict):
            gateway._pending[session_path] = {
                "phone": phone,
                "state": "connected",
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
    try:
        result = await gateway.confirm_code(
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
    return await _finish_login(conn, account_id, result, gateway)


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


async def check_all_accounts(conn, gateway=None) -> dict:
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    for row in rows:
        d = as_dict(row)
        if d.get("status") != "connected":
            continue
        account_id = int(d["id"])
        session_path = str(d.get("session_path") or "")
        is_demo = session_path.startswith("data/sessions/demo")
        if is_demo and gateway is None:
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
        conn.execute(
            """
            UPDATE accounts
            SET status = ?, telegram_user_id = ?, telegram_username = ?, telegram_name = ?
            WHERE id = ?
            """,
            (
                status,
                result.get("telegram_user_id"),
                result.get("telegram_username"),
                result.get("telegram_name"),
                account_id,
            ),
        )
    else:
        conn.execute(
            "UPDATE accounts SET status = ? WHERE id = ?",
            (status, account_id),
        )
    conn.commit()
