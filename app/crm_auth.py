from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.db import as_dict, utc_now
from app.security import (
    MAX_TRACKED_IPS,
    extract_client_ip,
    has_valid_mcp_api_key,
    is_auth_enforced,
)

AUTH_COOKIE_NAME = "foldesk_auth"
SESSION_TTL_DAYS = 30
SESSION_MAX_AGE = 86400 * SESSION_TTL_DAYS
LOGIN_WINDOW_SECONDS = 600.0
LOGIN_MAX_FAILURES = 8
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,64}$")
_TOKEN_SIGN_SECRET = b"foldesk_crm_persistent_auth_v1_secret"
_PROCESS_FALLBACK_SECRET = secrets.token_bytes(32)
_REVOKED_TOKENS: set[str] = set()
_VERIFIED_SAFE_HASHES: set[tuple[int, str]] = set()
_ENV_PASSWORD_KEYS: dict[str, str] = {
    "tim": "CRM_ADMIN_PASSWORD",
    "vlad": "CRM_VLAD_PASSWORD",
    "operator": "CRM_OPERATOR_PASSWORD",
}
DEFAULT_CRM_USERS: tuple[tuple[str, str, str, str], ...] = (
    ("tim", "TimFoldesk2026!", "Тимофей (Owner)", "admin"),
    ("vlad", "VladSales2026!", "Влад (Sales)", "sales"),
    ("operator", "Operator2026!", "Оператор (Outreach)", "operator"),
)
_LEAKED_DEFAULT_PASSWORDS = frozenset(pwd for _, pwd, _, _ in DEFAULT_CRM_USERS)


def _get_signing_secret(conn: sqlite3.Connection | None = None) -> bytes:
    env_secret = os.getenv("CRM_AUTH_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    if "PYTEST_CURRENT_TEST" in os.environ and not is_auth_enforced():
        return _TOKEN_SIGN_SECRET
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT value FROM kv_settings WHERE key = 'crm_auth_secret_v2'"
            ).fetchone()
            if row and str(row[0] or "").strip():
                return str(row[0]).strip().encode("utf-8")
            generated = secrets.token_hex(32)
            conn.execute(
                """
                INSERT INTO kv_settings (key, value, updated_at)
                VALUES ('crm_auth_secret_v2', ?, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (generated, utc_now()),
            )
            conn.commit()
            row2 = conn.execute(
                "SELECT value FROM kv_settings WHERE key = 'crm_auth_secret_v2'"
            ).fetchone()
            if row2 and str(row2[0] or "").strip():
                return str(row2[0]).strip().encode("utf-8")
            return generated.encode("utf-8")
        except sqlite3.Error:
            pass
    return _PROCESS_FALLBACK_SECRET


def _sign_stateless_token(
    user_id: int,
    exp_unix: int,
    nonce: str,
    conn: sqlite3.Connection | None = None,
    pwd_hash: str = "",
) -> str:
    secret = _get_signing_secret(conn)
    pwd_tag = hashlib.sha256(str(pwd_hash or "").encode("utf-8")).hexdigest()[:12]
    msg = f"u{int(user_id)}.{int(exp_unix)}.{nonce}.{pwd_tag}".encode("utf-8")
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"u{int(user_id)}.{int(exp_unix)}.{nonce}.{sig}"


def _parse_stateless_token(token: str) -> tuple[int, int, str, str] | None:
    if not token or not token.startswith("u") or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    uid_part, exp_part, nonce, sig = parts
    if not nonce or len(sig) != 64:
        return None
    try:
        uid = int(uid_part[1:])
        exp_unix = int(exp_part)
    except ValueError:
        return None
    if int(time.time()) >= exp_unix:
        return None
    return uid, exp_unix, nonce, sig


def _verify_stateless_token_uid(
    token: str,
    conn: sqlite3.Connection | None = None,
    pwd_hash: str = "",
) -> tuple[int, int] | None:
    parsed = _parse_stateless_token(token)
    if parsed is None:
        return None
    uid, exp_unix, nonce, _ = parsed
    expected = _sign_stateless_token(
        uid, exp_unix, nonce, conn=conn, pwd_hash=pwd_hash
    )
    if hmac.compare_digest(token, expected):
        return uid, exp_unix
    return None


def _ensure_revoked_tokens_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crm_revoked_tokens (
            token_sha256 TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL
        )
        """
    )


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_token_revoked(conn: sqlite3.Connection, token: str) -> bool:
    if token in _REVOKED_TOKENS:
        return True
    try:
        row = conn.execute(
            "SELECT 1 FROM crm_revoked_tokens WHERE token_sha256 = ? AND expires_at >= ? LIMIT 1",
            (_token_sha256(token), int(time.time())),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def hash_password(password: str, salt: str | None = None) -> str:
    clean_salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        clean_salt.encode("utf-8"),
        120_000,
    )
    return f"pbkdf2_sha256${clean_salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash or "$" not in stored_hash:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
        return False
    _, salt, _ = parts
    candidate = hash_password(password, salt=salt)
    return hmac.compare_digest(candidate, stored_hash)


def _public_user(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = as_dict(row) if not isinstance(row, dict) else dict(row)
    return {
        "id": int(d["id"]),
        "username": str(d["username"]),
        "display_name": str(d["display_name"]),
        "role": str(d.get("role") or "operator"),
        "created_at": str(d.get("created_at") or ""),
    }


def ensure_default_crm_users(conn: sqlite3.Connection) -> None:
    _ensure_revoked_tokens_table(conn)
    conn.execute(
        "DELETE FROM crm_revoked_tokens WHERE expires_at < ?",
        (int(time.time()),),
    )
    now = utc_now()
    enforced = is_auth_enforced()
    for idx, (username, default_password, display_name, role) in enumerate(
        DEFAULT_CRM_USERS, start=1
    ):
        env_key = _ENV_PASSWORD_KEYS.get(username, "")
        env_pwd = os.getenv(env_key, "").strip() if env_key else ""
        row = conn.execute(
            "SELECT id, password_hash FROM crm_users WHERE lower(username) = lower(?)",
            (username,),
        ).fetchone()
        if row is not None:
            uid = int(row["id"])
            stored_hash = str(row["password_hash"] or "")
            if env_pwd:
                if not verify_password(env_pwd, stored_hash):
                    new_hash = hash_password(env_pwd)
                    conn.execute(
                        "UPDATE crm_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                        (new_hash, now, uid),
                    )
                    conn.execute(
                        "DELETE FROM crm_auth_sessions WHERE user_id = ?",
                        (uid,),
                    )
                    _VERIFIED_SAFE_HASHES.add((uid, new_hash))
            elif enforced and (uid, stored_hash) not in _VERIFIED_SAFE_HASHES:
                if any(
                    verify_password(leaked, stored_hash)
                    for leaked in _LEAKED_DEFAULT_PASSWORDS
                ):
                    new_hash = hash_password(secrets.token_urlsafe(24))
                    conn.execute(
                        "UPDATE crm_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                        (new_hash, now, uid),
                    )
                    conn.execute(
                        "DELETE FROM crm_auth_sessions WHERE user_id = ?",
                        (uid,),
                    )
                    _VERIFIED_SAFE_HASHES.add((uid, new_hash))
                else:
                    _VERIFIED_SAFE_HASHES.add((uid, stored_hash))
            continue

        if env_pwd:
            pwd_hash = hash_password(env_pwd)
        elif enforced:
            pwd_hash = hash_password(secrets.token_urlsafe(24))
        else:
            # Deterministic salt for local/test defaults keeps DB initialization fast and reproducible
            seed_salt = hashlib.sha256(
                f"foldesk_seed_{username}_{idx}".encode("utf-8")
            ).hexdigest()[:32]
            pwd_hash = hash_password(default_password, salt=seed_salt)
        cur = conn.execute(
            """
            INSERT INTO crm_users (username, password_hash, display_name, role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username, pwd_hash, display_name, role, now, now),
        )
        if enforced and not env_pwd and cur.lastrowid:
            _VERIFIED_SAFE_HASHES.add((int(cur.lastrowid), pwd_hash))
    conn.commit()


def list_crm_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, username, display_name, role, created_at FROM crm_users ORDER BY id ASC"
    ).fetchall()
    return [_public_user(r) for r in rows if r is not None]


def _create_session(
    conn: sqlite3.Connection, user_id: int, pwd_hash: str = ""
) -> str:
    if not pwd_hash:
        prow = conn.execute(
            "SELECT password_hash FROM crm_users WHERE id = ?", (int(user_id),)
        ).fetchone()
        pwd_hash = str(prow["password_hash"] or "") if prow else ""
    now_dt = datetime.now(timezone.utc)
    exp_dt = now_dt + timedelta(days=SESSION_TTL_DAYS)
    exp_unix = int(exp_dt.timestamp())
    nonce = secrets.token_urlsafe(24)
    token = _sign_stateless_token(
        int(user_id), exp_unix, nonce, conn=conn, pwd_hash=pwd_hash
    )
    _REVOKED_TOKENS.discard(token)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    exp_iso = exp_dt.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        INSERT OR REPLACE INTO crm_auth_sessions (token, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (token, int(user_id), now_iso, exp_iso),
    )
    conn.commit()
    return token


def _extract_candidate_tokens(request: Request) -> list[str]:
    candidates: list[str] = []
    auth_header = (request.headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:].strip()
        if bearer:
            candidates.append(bearer)
    header_tok = (request.headers.get("x-foldesk-token") or "").strip()
    if header_tok and header_tok not in candidates:
        candidates.append(header_tok)
    cookie_tok = (request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    if cookie_tok and cookie_tok not in candidates:
        candidates.append(cookie_tok)
    return candidates


def extract_request_token(request: Request) -> str | None:
    candidates = _extract_candidate_tokens(request)
    return candidates[0] if candidates else None


def resolve_request_crm_user(
    conn: sqlite3.Connection, request: Request
) -> dict[str, Any] | None:
    for token in _extract_candidate_tokens(request):
        user = get_user_by_token(conn, token)
        if user is not None:
            return user
    return None


def get_user_by_token(conn: sqlite3.Connection, token: str | None) -> dict[str, Any] | None:
    if not token or _is_token_revoked(conn, token):
        return None
    now_iso = utc_now()
    row = conn.execute(
        """
        SELECT u.id, u.username, u.display_name, u.role, u.created_at, s.expires_at
        FROM crm_auth_sessions s
        JOIN crm_users u ON u.id = s.user_id
        WHERE s.token = ?
        """,
        (token,),
    ).fetchone()
    if row is not None:
        if str(row["expires_at"] or "") < now_iso:
            conn.execute("DELETE FROM crm_auth_sessions WHERE token = ?", (token,))
            conn.commit()
            return None
        return _public_user(row)

    # Stateless HMAC token recovery across container restarts / R2 restores
    parsed = _parse_stateless_token(token)
    if parsed is not None:
        candidate_uid, exp_unix, _, _ = parsed
        urow = conn.execute(
            "SELECT id, username, display_name, role, password_hash, created_at FROM crm_users WHERE id = ? ORDER BY id ASC LIMIT 1",
            (int(candidate_uid),),
        ).fetchone()
        if urow is None:
            ensure_default_crm_users(conn)
            urow = conn.execute(
                "SELECT id, username, display_name, role, password_hash, created_at FROM crm_users WHERE id = ? ORDER BY id ASC LIMIT 1",
                (int(candidate_uid),),
            ).fetchone()
        if urow is not None:
            verified = _verify_stateless_token_uid(
                token, conn=conn, pwd_hash=str(urow["password_hash"] or "")
            )
            if verified is not None and verified[0] == int(urow["id"]):
                exp_iso = datetime.fromtimestamp(
                    exp_unix, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%S")
                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO crm_auth_sessions (token, user_id, created_at, expires_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (token, int(urow["id"]), now_iso, exp_iso),
                    )
                    conn.commit()
                except Exception:
                    pass
                return _public_user(urow)
    return None


def authenticate_crm_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
) -> tuple[dict[str, Any], str]:
    clean_u = str(username or "").strip()
    clean_p = str(password or "")
    if not clean_u or not clean_p:
        raise ValueError("Неверный логин или пароль")
    if is_auth_enforced():
        ensure_default_crm_users(conn)
        if clean_p in _LEAKED_DEFAULT_PASSWORDS:
            raise PermissionError("Неверный логин или пароль")
    row = conn.execute(
        "SELECT * FROM crm_users WHERE lower(username) = lower(?)",
        (clean_u,),
    ).fetchone()
    if row is None or not verify_password(clean_p, str(row["password_hash"])):
        raise PermissionError("Неверный логин или пароль")
    user_pub = _public_user(row)
    assert user_pub is not None
    token = _create_session(conn, user_pub["id"], pwd_hash=str(row["password_hash"]))
    return user_pub, token


def register_crm_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    display_name: str = "",
    role: str = "operator",
) -> tuple[dict[str, Any], str]:
    clean_u = str(username or "").strip()
    clean_p = str(password or "")
    if len(clean_u) < 3 or not USERNAME_RE.match(clean_u):
        raise ValueError("Логин должен содержать минимум 3 символа (латиница, цифры, _, ., -)")
    if len(clean_p) < 4:
        raise ValueError("Пароль должен быть не короче 4 символов")
    existing = conn.execute(
        "SELECT id FROM crm_users WHERE lower(username) = lower(?)",
        (clean_u,),
    ).fetchone()
    if existing is not None:
        raise FileExistsError("Пользователь с таким логином уже существует")

    clean_display = str(display_name or "").strip() or clean_u
    clean_role = str(role or "operator").strip().lower()
    if clean_role not in ("admin", "sales", "operator"):
        clean_role = "operator"

    now = utc_now()
    pwd_hash = hash_password(clean_p)
    cur = conn.execute(
        """
        INSERT INTO crm_users (username, password_hash, display_name, role, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (clean_u, pwd_hash, clean_display, clean_role, now, now),
    )
    user_id = int(cur.lastrowid)
    conn.commit()
    row = conn.execute("SELECT * FROM crm_users WHERE id = ?", (user_id,)).fetchone()
    user_pub = _public_user(row)
    assert user_pub is not None
    token = _create_session(conn, user_id, pwd_hash=pwd_hash)
    return user_pub, token


def logout_crm_token(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        _REVOKED_TOKENS.add(token)
        exp_unix = int(time.time()) + SESSION_MAX_AGE
        parts = token.split(".")
        if len(parts) == 4:
            try:
                exp_unix = max(int(parts[1]), int(time.time()) + 60)
            except ValueError:
                pass
        try:
            _ensure_revoked_tokens_table(conn)
            conn.execute(
                "DELETE FROM crm_revoked_tokens WHERE expires_at < ?",
                (int(time.time()),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO crm_revoked_tokens (token_sha256, expires_at) VALUES (?, ?)",
                (_token_sha256(token), exp_unix),
            )
        except sqlite3.Error:
            pass
        conn.execute("DELETE FROM crm_auth_sessions WHERE token = ?", (token,))
        conn.commit()


def _is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded_proto = (
        (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    )
    return forwarded_proto == "https"


def _set_auth_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        path="/",
        samesite="lax",
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=_is_https_request(request),
    )


def _get_login_failure_bucket(
    request: Request,
) -> tuple[dict[str, list[float]], str, list[float], float]:
    failures: dict[str, list[float]] | None = getattr(
        request.app.state, "_login_failures", None
    )
    if failures is None:
        failures = {}
        request.app.state._login_failures = failures
    now = time.time()
    cutoff = now - LOGIN_WINDOW_SECONDS
    if len(failures) > MAX_TRACKED_IPS:
        stale = [
            ip
            for ip, ts_list in failures.items()
            if not ts_list or all(ts < cutoff for ts in ts_list)
        ]
        for ip in stale:
            failures.pop(ip, None)
        while len(failures) > MAX_TRACKED_IPS:
            failures.pop(next(iter(failures)), None)
    client_ip = extract_client_ip(request)
    recent = [ts for ts in failures.get(client_ip, ()) if ts >= cutoff]
    failures[client_ip] = recent
    return failures, client_ip, recent, now


class AuthLoginIn(BaseModel):
    username: str
    password: str


class AuthRegisterIn(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "operator"


def register_auth_routes(app: FastAPI, templates: Any) -> None:
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        login_tpl = templates.env.loader.searchpath[0] if templates.env.loader else ""
        from pathlib import Path

        if login_tpl and (Path(login_tpl) / "login.html").exists():
            return templates.TemplateResponse(request, "login.html", {"request": request})
        return templates.TemplateResponse(
            request,
            "index.html",
            {"request": request, "default_view_mode": "sheet"},
        )

    @app.get("/api/auth/me")
    def auth_me(request: Request):
        conn = request.app.state.conn
        user = resolve_request_crm_user(conn, request)
        if is_auth_enforced() and user is None:
            return {
                "ok": True,
                "authenticated": False,
                "user": None,
                "users": [],
            }
        return {
            "ok": True,
            "authenticated": user is not None,
            "user": user,
            "users": list_crm_users(conn),
        }

    @app.get("/api/auth/users")
    def auth_users_list(request: Request):
        conn = request.app.state.conn
        if is_auth_enforced():
            user = resolve_request_crm_user(conn, request)
            if user is None and not has_valid_mcp_api_key(request):
                raise HTTPException(status_code=401, detail="Требуется авторизация в CRM")
        return {"ok": True, "users": list_crm_users(conn)}

    @app.post("/api/auth/login")
    def auth_login(body: AuthLoginIn, request: Request, response: Response):
        conn = request.app.state.conn
        failures, client_ip, recent, now = _get_login_failure_bucket(request)
        if len(recent) >= LOGIN_MAX_FAILURES:
            raise HTTPException(
                status_code=429,
                detail="Слишком много неудачных попыток входа. Подождите 10 минут.",
            )
        try:
            user, token = authenticate_crm_user(conn, body.username, body.password)
        except (PermissionError, ValueError) as exc:
            recent.append(now)
            failures[client_ip] = recent
            raise HTTPException(status_code=401, detail="Неверный логин или пароль") from exc

        failures.pop(client_ip, None)
        _set_auth_cookie(response, request, token)
        return {
            "ok": True,
            "authenticated": True,
            "token": token,
            "user": user,
        }

    @app.post("/api/auth/register")
    def auth_register(body: AuthRegisterIn, request: Request, response: Response):
        conn = request.app.state.conn
        caller = resolve_request_crm_user(conn, request)
        if is_auth_enforced():
            if caller is None:
                raise HTTPException(
                    status_code=401, detail="Требуется авторизация администратора"
                )
            if caller.get("role") != "admin":
                raise HTTPException(
                    status_code=403,
                    detail="Только администратор может создавать пользователей",
                )
        try:
            user, token = register_crm_user(
                conn,
                username=body.username,
                password=body.password,
                display_name=body.display_name,
                role=body.role,
            )
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if caller is None:
            _set_auth_cookie(response, request, token)
        return {
            "ok": True,
            "authenticated": True,
            "token": token,
            "user": user,
        }

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        conn = request.app.state.conn
        for tok in _extract_candidate_tokens(request):
            logout_crm_token(conn, tok)
        response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")
        return {"ok": True, "authenticated": False}
