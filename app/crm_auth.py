from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.db import as_dict, utc_now

AUTH_COOKIE_NAME = "foldesk_auth"
SESSION_TTL_DAYS = 30
SESSION_MAX_AGE = 86400 * SESSION_TTL_DAYS
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,64}$")

DEFAULT_CRM_USERS: tuple[tuple[str, str, str, str], ...] = (
    ("tim", "TimFoldesk2026!", "Тимофей (Owner)", "admin"),
    ("vlad", "VladSales2026!", "Влад (Sales)", "sales"),
    ("operator", "Operator2026!", "Оператор (Outreach)", "operator"),
)


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
    now = utc_now()
    for idx, (username, password, display_name, role) in enumerate(DEFAULT_CRM_USERS, start=1):
        row = conn.execute(
            "SELECT id FROM crm_users WHERE lower(username) = lower(?)",
            (username,),
        ).fetchone()
        if row is not None:
            continue
        # Deterministic salt for seeded defaults keeps DB initialization fast and reproducible
        seed_salt = hashlib.sha256(f"foldesk_seed_{username}_{idx}".encode("utf-8")).hexdigest()[:32]
        pwd_hash = hash_password(password, salt=seed_salt)
        conn.execute(
            """
            INSERT INTO crm_users (username, password_hash, display_name, role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username, pwd_hash, display_name, role, now, now),
        )
    conn.commit()


def list_crm_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, username, display_name, role, created_at FROM crm_users ORDER BY id ASC"
    ).fetchall()
    return [_public_user(r) for r in rows if r is not None]


def _create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    exp_iso = (now_dt + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        INSERT INTO crm_auth_sessions (token, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (token, int(user_id), now_iso, exp_iso),
    )
    conn.commit()
    return token


def extract_request_token(request: Request) -> str | None:
    cookie_tok = (request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    if cookie_tok:
        return cookie_tok
    auth_header = (request.headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:].strip()
        if bearer:
            return bearer
    header_tok = (request.headers.get("x-foldesk-token") or "").strip()
    return header_tok or None


def get_user_by_token(conn: sqlite3.Connection, token: str | None) -> dict[str, Any] | None:
    if not token:
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
    if row is None:
        return None
    if str(row["expires_at"] or "") < now_iso:
        conn.execute("DELETE FROM crm_auth_sessions WHERE token = ?", (token,))
        conn.commit()
        return None
    return _public_user(row)


def authenticate_crm_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
) -> tuple[dict[str, Any], str]:
    clean_u = str(username or "").strip()
    clean_p = str(password or "")
    if not clean_u or not clean_p:
        raise ValueError("Неверный логин или пароль")
    row = conn.execute(
        "SELECT * FROM crm_users WHERE lower(username) = lower(?)",
        (clean_u,),
    ).fetchone()
    if row is None or not verify_password(clean_p, str(row["password_hash"])):
        raise PermissionError("Неверный логин или пароль")
    user_pub = _public_user(row)
    assert user_pub is not None
    token = _create_session(conn, user_pub["id"])
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
    token = _create_session(conn, user_id)
    return user_pub, token


def logout_crm_token(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM crm_auth_sessions WHERE token = ?", (token,))
        conn.commit()


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
        token = extract_request_token(request)
        user = get_user_by_token(conn, token)
        return {
            "ok": True,
            "authenticated": user is not None,
            "user": user,
            "users": list_crm_users(conn),
        }

    @app.get("/api/auth/users")
    def auth_users_list(request: Request):
        conn = request.app.state.conn
        return {"ok": True, "users": list_crm_users(conn)}

    @app.post("/api/auth/login")
    def auth_login(body: AuthLoginIn, request: Request, response: Response):
        conn = request.app.state.conn
        try:
            user, token = authenticate_crm_user(conn, body.username, body.password)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Неверный логин или пароль") from exc

        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            path="/",
            samesite="lax",
            max_age=SESSION_MAX_AGE,
            httponly=False,
        )
        return {
            "ok": True,
            "authenticated": True,
            "token": token,
            "user": user,
        }

    @app.post("/api/auth/register")
    def auth_register(body: AuthRegisterIn, request: Request, response: Response):
        conn = request.app.state.conn
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

        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            path="/",
            samesite="lax",
            max_age=SESSION_MAX_AGE,
            httponly=False,
        )
        return {
            "ok": True,
            "authenticated": True,
            "token": token,
            "user": user,
        }

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        conn = request.app.state.conn
        token = extract_request_token(request)
        logout_crm_token(conn, token)
        response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")
        return {"ok": True, "authenticated": False}
