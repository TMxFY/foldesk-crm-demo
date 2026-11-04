from app.db import as_dict, resolve_session, utc_now
from app.telegram_gateway import GatewayError

PUBLIC_FIELDS = (
    "id",
    "phone",
    "status",
    "telegram_user_id",
    "telegram_username",
    "telegram_name",
    "created_at",
)


def _public(row: dict) -> dict:
    return {key: row[key] for key in PUBLIC_FIELDS}


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


async def start_login(conn, phone: str, gateway) -> dict:
    phone = (phone or "").strip()
    if not phone:
        raise ValueError("Укажите телефон")
    cur = conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at)
        VALUES (?, '', 'pending_code', ?)
        """,
        (phone, utc_now()),
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
    return {"id": account_id, "status": "pending_code"}


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
    _apply_result(conn, account_id, result)
    return {"status": result["status"]}


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
    _apply_result(conn, account_id, result)
    return {"status": result["status"]}


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
