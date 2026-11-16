from __future__ import annotations

import asyncio
import hmac
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

API_RATE_WINDOW_SECONDS = 60.0
API_RATE_MAX_REQUESTS = 240
AUTH_API_RATE_MAX_REQUESTS = 1200
MAX_TRACKED_IPS = 2048

_SCANNER_SEGMENT_PREFIXES = (
    ".env",
    ".git",
    ".svn",
    ".ssh",
    ".aws",
    ".vscode",
)
_SCANNER_EXACT_SEGMENTS = {
    ".ds_store",
}
_SCANNER_EXTENSIONS = (
    ".php",
    ".sql",
    ".bak",
    ".old",
    ".swp",
    ".tar.gz",
    ".zip",
    ".pem",
    ".key",
)
_SCANNER_PATH_PREFIXES = (
    "/wp-admin",
    "/wp-login",
    "/wp-content",
    "/wp-includes",
    "/wp-config",
    "/xmlrpc",
    "/phpmyadmin",
    "/actuator",
    "/vendor/phpunit",
    "/cgi-bin",
    "/server-status",
)

_BLOCKED_UA_SUBSTRINGS = (
    "python-requests",
    "python-httpx",
    "python-urllib",
    "aiohttp",
    "go-http-client",
    "java/",
    "curl/",
    "wget/",
    "libwww-perl",
    "scrapy",
    "ahrefsbot",
    "semrushbot",
    "mj12bot",
    "dotbot",
    "petalbot",
    "bytespider",
    "gptbot",
    "ccbot",
    "claude-web",
    "anthropic-ai",
    "perplexitybot",
    "sqlmap",
    "nikto",
    "nmap",
    "masscan",
    "zgrab",
    "gobuster",
    "nuclei",
    "dirbuster",
    "ffuf",
    "httpx",
)

_PUBLIC_EXACT_PATHS = {
    "/api/health",
    "/robots.txt",
    "/login",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/design/tokens.css",
    "/favicon.ico",
    "/mcp",
    "/mcp/sse",
}

_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def is_auth_enforced() -> bool:
    if os.getenv("CRM_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes"):
        return True
    if (
        os.getenv("CRMTG_VPS_RUNTIME", "").strip().lower() in ("1", "true", "yes")
        and os.getenv("BOT_TOKEN") != "e2e-not-a-secret"
    ):
        return True
    return False


def extract_client_ip(request: Request) -> str:
    client_host = (request.client.host or "").strip() if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "testclient"):
        return client_host or "0.0.0.0"
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        parts = [part.strip() for part in xff.split(",") if part.strip()]
        if parts:
            return parts[-1]
    xri = (request.headers.get("x-real-ip") or "").strip()
    if xri:
        return xri
    return client_host


def _is_dialog_avatar_path(path: str) -> bool:
    if not path.startswith("/api/dialogs/") or not path.endswith("/avatar"):
        return False
    parts = path.strip("/").split("/")
    return (
        len(parts) == 4
        and parts[0] == "api"
        and parts[1] == "dialogs"
        and parts[2].isdigit()
        and parts[3] == "avatar"
    )


def apply_security_headers(response: Response, path: str) -> Response:
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    if path.startswith("/api/"):
        ct = (response.headers.get("content-type") or "").lower()
        existing_cc = (response.headers.get("cache-control") or "").strip()
        if ct.startswith("text/event-stream"):
            pass
        elif (
            response.status_code < 400
            and _is_dialog_avatar_path(path)
            and existing_cc
        ):
            pass
        else:
            response.headers["Cache-Control"] = "no-store"
    return response


def is_scanner_path(path: str) -> bool:
    decoded = urllib.parse.unquote(path or "/")
    lower_path = decoded.lower()
    if any(lower_path.startswith(prefix) for prefix in _SCANNER_PATH_PREFIXES):
        return True
    if lower_path.endswith(_SCANNER_EXTENSIONS):
        return True
    for seg in lower_path.split("/"):
        if not seg:
            continue
        if seg in _SCANNER_EXACT_SEGMENTS:
            return True
        if any(seg.startswith(pref) for pref in _SCANNER_SEGMENT_PREFIXES):
            return True
    return False


def has_valid_mcp_api_key(request: Request) -> bool:
    mcp_key = os.getenv("MCP_API_KEY", "").strip()
    if not mcp_key:
        return False
    auth_hdr = (request.headers.get("authorization") or "").strip()
    provided = ""
    if auth_hdr.lower().startswith("bearer "):
        provided = auth_hdr[7:].strip()
    if not provided:
        provided = (
            request.headers.get("x-mcp-api-key")
            or request.headers.get("x-mcp-key")
            or ""
        ).strip()
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), mcp_key.encode("utf-8"))


def is_blocked_user_agent(request: Request, path: str) -> bool:
    if path in ("/api/health", "/robots.txt", "/mcp", "/mcp/sse"):
        return False
    if path.startswith("/api/") and (
        (request.headers.get("authorization") or "").strip()
        or (request.headers.get("x-foldesk-token") or "").strip()
        or (request.headers.get("x-mcp-api-key") or "").strip()
        or (request.headers.get("x-mcp-key") or "").strip()
    ):
        return False
    ua = (request.headers.get("user-agent") or "").strip()
    if not ua:
        return True
    ua_lower = ua.lower()
    if ua_lower == "testclient" or ua_lower.startswith("testclient/"):
        return False
    return any(sub in ua_lower for sub in _BLOCKED_UA_SUBSTRINGS)


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT_PATHS:
        return True
    if path == "/static" or path.startswith("/static/"):
        return True
    return False


def _requires_admin_role(path: str, method: str) -> bool:
    if (
        path == "/api/cloud"
        or path.startswith("/api/cloud/")
        or path == "/api/settings"
        or path.startswith("/api/settings/")
        or path == "/api/airtable/credentials"
        or path.startswith("/api/airtable/credentials/")
        or path.startswith("/api/proxy/nodemaven")
        or path == "/api/ai-bot/config"
        or path.startswith("/api/ai-bot/config/")
        or path == "/api/demo"
        or path.startswith("/api/demo/")
    ):
        return True
    if method in _MUTATION_METHODS and (
        path == "/api/leadgen"
        or path.startswith("/api/leadgen/")
        or path == "/api/accounts"
        or path.startswith("/api/accounts/")
        or path == "/api/leads/bulk"
        or path.startswith("/api/leads/bulk/")
        or path == "/api/outreach/autopilot"
        or path.startswith("/api/outreach/autopilot/")
        or path == "/api/folders"
        or path.startswith("/api/folders/")
        or bool(re.fullmatch(r"/api/campaigns/[^/]+/run/?", path))
    ):
        return True
    return False


def _is_cross_origin_mutation(request: Request) -> bool:
    if request.method.upper() not in _MUTATION_METHODS:
        return False
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return False
    origin_netloc = (urllib.parse.urlparse(origin).netloc or "").strip().lower()
    host = (request.headers.get("host") or "").strip().lower()
    if not origin_netloc or origin_netloc != host:
        return True
    return False


def _prune_rate_buckets(buckets: dict[str, list[float]], cutoff: float) -> None:
    stale = [
        ip
        for ip, ts_list in buckets.items()
        if not ts_list or all(ts < cutoff for ts in ts_list)
    ]
    for ip in stale:
        buckets.pop(ip, None)
    while len(buckets) > MAX_TRACKED_IPS:
        buckets.pop(next(iter(buckets)), None)


def _is_rate_limit_exempt_for_authed(path: str, method: str) -> bool:
    if method in ("GET", "HEAD") and _is_dialog_avatar_path(path):
        return True
    if path == "/api/inbox/events":
        return True
    return False


def _has_auth_credentials(request: Request) -> bool:
    from app.crm_auth import AUTH_COOKIE_NAME

    if (request.headers.get("authorization") or "").strip():
        return True
    if (request.headers.get("x-foldesk-token") or "").strip():
        return True
    if (request.headers.get("x-mcp-api-key") or "").strip():
        return True
    if (request.headers.get("x-mcp-key") or "").strip():
        return True
    if (request.cookies.get(AUTH_COOKIE_NAME) or "").strip():
        return True
    return False


def _is_loopback_direct(request: Request) -> bool:
    client_host = (request.client.host or "").strip() if request.client else ""
    has_forwarded = bool(
        (request.headers.get("x-forwarded-for") or "").strip()
        or (request.headers.get("x-real-ip") or "").strip()
    )
    client_ip = extract_client_ip(request)
    return (
        not has_forwarded
        and client_host in ("127.0.0.1", "::1", "testclient")
        and client_ip in ("127.0.0.1", "::1", "testclient")
    )


def _get_rate_buckets(request: Request, attr_name: str) -> dict[str, list[float]]:
    buckets: dict[str, list[float]] | None = getattr(request.app.state, attr_name, None)
    if buckets is None:
        buckets = {}
        setattr(request.app.state, attr_name, buckets)
    return buckets


def _peek_rate_bucket_exceeded(
    buckets: dict[str, list[float]],
    key: str,
    limit: int,
    window_seconds: float = API_RATE_WINDOW_SECONDS,
) -> tuple[bool, int]:
    now = time.monotonic()
    cutoff = now - window_seconds
    recent = [ts for ts in buckets.get(key, ()) if ts >= cutoff]
    if key in buckets:
        if recent:
            buckets[key] = recent
        else:
            buckets.pop(key, None)
    if len(recent) >= limit:
        oldest = min(recent) if recent else now
        retry_after = max(1, math.ceil((oldest + window_seconds) - now))
        return True, retry_after
    return False, 0


def _consume_rate_bucket(
    buckets: dict[str, list[float]],
    key: str,
    limit: int,
    window_seconds: float = API_RATE_WINDOW_SECONDS,
) -> tuple[bool, int]:
    now = time.monotonic()
    cutoff = now - window_seconds
    if len(buckets) > MAX_TRACKED_IPS:
        _prune_rate_buckets(buckets, cutoff)

    recent = [ts for ts in buckets.get(key, ()) if ts >= cutoff]
    if len(recent) >= limit:
        buckets[key] = recent
        oldest = min(recent) if recent else now
        retry_after = max(1, math.ceil((oldest + window_seconds) - now))
        return False, retry_after
    recent.append(now)
    buckets[key] = recent
    return True, 0


def _rate_limited_response(path: str, retry_after: int) -> Response:
    return apply_security_headers(
        JSONResponse(
            {"detail": "Rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(max(1, int(retry_after)))},
        ),
        path,
    )


def _check_api_rate_limit(
    request: Request,
    path: str,
    *,
    bucket_attr: str = "_api_rate_buckets",
    key: str | None = None,
    limit: int = API_RATE_MAX_REQUESTS,
) -> tuple[bool, int]:
    if not path.startswith("/api/") or path in ("/api/health", "/api/auth/login"):
        return True, 0
    if _is_loopback_direct(request):
        return True, 0

    buckets = _get_rate_buckets(request, bucket_attr)
    bucket_key = key or extract_client_ip(request)
    return _consume_rate_bucket(buckets, bucket_key, limit)


def _resolve_request_user(request: Request):
    from app.crm_auth import resolve_request_crm_user
    from app.db import locked_conn

    conn = getattr(request.app.state, "conn", None)
    if conn is None:
        return None
    auth_lock = getattr(request.app.state, "_auth_db_lock", None)
    if auth_lock is None:
        auth_lock = threading.Lock()
        request.app.state._auth_db_lock = auth_lock
    with auth_lock:
        auth_conn = getattr(request.app.state, "_auth_conn", None)
        if auth_conn is None:
            try:
                with locked_conn(conn):
                    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
            except Exception:
                db_file = ""
            if db_file and db_file not in (":memory:", ""):
                auth_conn = sqlite3.connect(
                    db_file, check_same_thread=False, timeout=30.0
                )
                auth_conn.row_factory = sqlite3.Row
                auth_conn.execute("PRAGMA busy_timeout = 30000")
                try:
                    auth_conn.execute("PRAGMA journal_mode = WAL")
                    auth_conn.execute("PRAGMA synchronous = NORMAL")
                except sqlite3.OperationalError:
                    pass
                request.app.state._auth_conn = auth_conn
            else:
                auth_conn = conn
        with locked_conn(auth_conn):
            return resolve_request_crm_user(auth_conn, request)


def close_auth_connection(app: FastAPI) -> None:
    auth_lock = getattr(app.state, "_auth_db_lock", None)

    def _close() -> None:
        auth_conn = getattr(app.state, "_auth_conn", None)
        app.state._auth_conn = None
        if auth_conn is not None and auth_conn is not getattr(app.state, "conn", None):
            try:
                auth_conn.close()
            except Exception:
                pass

    if auth_lock is not None:
        with auth_lock:
            _close()
    else:
        _close()


def register_security_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _security_gate_middleware(request: Request, call_next):
        path = request.url.path or "/"

        enforced = is_auth_enforced()
        if enforced:
            if is_scanner_path(path):
                return apply_security_headers(
                    PlainTextResponse("Forbidden", status_code=403), path
                )

            if is_blocked_user_agent(request, path):
                return apply_security_headers(
                    PlainTextResponse("Forbidden", status_code=403), path
                )

            if _is_cross_origin_mutation(request):
                return apply_security_headers(
                    PlainTextResponse(
                        "Forbidden: cross-origin request", status_code=403
                    ),
                    path,
                )

            method = request.method.upper()
            is_rate_checked_api = path.startswith("/api/") and path not in (
                "/api/health",
                "/api/auth/login",
            )
            has_creds = _has_auth_credentials(request)
            user = None
            api_mcp_ok = False

            if not has_creds:
                if is_rate_checked_api:
                    allowed, retry_after = _check_api_rate_limit(request, path)
                    if not allowed:
                        return _rate_limited_response(path, retry_after)
            else:
                if is_rate_checked_api and not _is_loopback_direct(request):
                    invalid_buckets = _get_rate_buckets(
                        request, "_invalid_api_rate_buckets"
                    )
                    exceeded, retry_after = _peek_rate_bucket_exceeded(
                        invalid_buckets,
                        extract_client_ip(request),
                        API_RATE_MAX_REQUESTS,
                    )
                    if exceeded:
                        return _rate_limited_response(path, retry_after)

                api_mcp_ok = path.startswith("/api/") and has_valid_mcp_api_key(
                    request
                )
                if not api_mcp_ok:
                    user = await asyncio.to_thread(_resolve_request_user, request)
                if user is None and not api_mcp_ok:
                    if is_rate_checked_api:
                        allowed, retry_after = _check_api_rate_limit(
                            request,
                            path,
                            bucket_attr="_invalid_api_rate_buckets",
                        )
                        if not allowed:
                            return _rate_limited_response(path, retry_after)

            is_authenticated = user is not None or api_mcp_ok
            if not _is_public_path(path) and not is_authenticated:
                if path.startswith("/api/"):
                    return apply_security_headers(
                        JSONResponse(
                            {"detail": "Требуется авторизация в CRM"},
                            status_code=401,
                        ),
                        path,
                    )
                return apply_security_headers(
                    RedirectResponse(url="/login", status_code=302), path
                )

            if (
                user is not None
                and not api_mcp_ok
                and not _is_public_path(path)
                and _requires_admin_role(path, method)
                and user.get("role") != "admin"
            ):
                return apply_security_headers(
                    JSONResponse(
                        {"detail": "Доступ только для администратора"},
                        status_code=403,
                    ),
                    path,
                )

            if (
                is_authenticated
                and is_rate_checked_api
                and not _is_rate_limit_exempt_for_authed(path, method)
            ):
                auth_key = (
                    f"user:{user['id']}"
                    if user is not None
                    else f"mcp:{extract_client_ip(request)}"
                )
                allowed, retry_after = _check_api_rate_limit(
                    request,
                    path,
                    bucket_attr="_auth_api_rate_buckets",
                    key=auth_key,
                    limit=AUTH_API_RATE_MAX_REQUESTS,
                )
                if not allowed:
                    return _rate_limited_response(path, retry_after)

        response = await call_next(request)
        return apply_security_headers(response, path)
