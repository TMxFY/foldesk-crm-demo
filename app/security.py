from __future__ import annotations

import hmac
import os
import sqlite3
import threading
import time
import urllib.parse

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

API_RATE_WINDOW_SECONDS = 60.0
API_RATE_MAX_REQUESTS = 240
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
        and "PYTEST_CURRENT_TEST" not in os.environ
        and os.getenv("BOT_TOKEN") != "e2e-not-a-secret"
    ):
        return True
    return False


def extract_client_ip(request: Request) -> str:
    client_host = (request.client.host or "").strip() if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "testclient", ""):
        return client_host
    xri = (request.headers.get("x-real-ip") or "").strip()
    if xri:
        return xri
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        parts = [part.strip() for part in xff.split(",") if part.strip()]
        if parts:
            return parts[-1]
    return client_host or "127.0.0.1"


def apply_security_headers(response: Response, path: str) -> Response:
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    if path.startswith("/api/"):
        ct = (response.headers.get("content-type") or "").lower()
        if not ct.startswith("text/event-stream"):
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


def _check_api_rate_limit(request: Request, path: str) -> bool:
    if not path.startswith("/api/") or path in ("/api/health", "/api/auth/login"):
        return True
    client_host = (request.client.host or "").strip() if request.client else ""
    has_forwarded = bool(
        (request.headers.get("x-forwarded-for") or "").strip()
        or (request.headers.get("x-real-ip") or "").strip()
    )
    client_ip = extract_client_ip(request)
    if (
        not has_forwarded
        and client_host in ("127.0.0.1", "::1", "testclient", "")
        and client_ip in ("127.0.0.1", "::1", "testclient")
    ):
        return True

    buckets: dict[str, list[float]] | None = getattr(
        request.app.state, "_api_rate_buckets", None
    )
    if buckets is None:
        buckets = {}
        request.app.state._api_rate_buckets = buckets

    now = time.monotonic()
    cutoff = now - API_RATE_WINDOW_SECONDS
    if len(buckets) > MAX_TRACKED_IPS:
        _prune_rate_buckets(buckets, cutoff)

    recent = [ts for ts in buckets.get(client_ip, ()) if ts >= cutoff]
    if len(recent) >= API_RATE_MAX_REQUESTS:
        buckets[client_ip] = recent
        return False
    recent.append(now)
    buckets[client_ip] = recent
    return True


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

            if not _check_api_rate_limit(request, path):
                return apply_security_headers(
                    JSONResponse({"detail": "Rate limit exceeded"}, status_code=429),
                    path,
                )

            if not _is_public_path(path):
                from app.crm_auth import resolve_request_crm_user

                conn = getattr(request.app.state, "conn", None)
                user = None
                if conn is not None:
                    auth_lock = getattr(request.app.state, "_auth_db_lock", None)
                    if auth_lock is None:
                        auth_lock = threading.Lock()
                        request.app.state._auth_db_lock = auth_lock
                    with auth_lock:
                        auth_conn = getattr(request.app.state, "_auth_conn", None)
                        if auth_conn is None:
                            try:
                                db_file = conn.execute("PRAGMA database_list").fetchone()[2]
                            except Exception:
                                db_file = ""
                            if db_file and db_file not in (":memory:", ""):
                                auth_conn = sqlite3.connect(
                                    db_file, check_same_thread=False, timeout=30.0
                                )
                                auth_conn.row_factory = sqlite3.Row
                                auth_conn.execute("PRAGMA busy_timeout = 30000")
                                request.app.state._auth_conn = auth_conn
                            else:
                                auth_conn = conn
                        user = resolve_request_crm_user(auth_conn, request)
                api_mcp_ok = path.startswith("/api/") and has_valid_mcp_api_key(request)
                if not api_mcp_ok:
                    if user is None:
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
                        _requires_admin_role(path, request.method.upper())
                        and user.get("role") != "admin"
                    ):
                        return apply_security_headers(
                            JSONResponse(
                                {"detail": "Доступ только для администратора"},
                                status_code=403,
                            ),
                            path,
                        )

        response = await call_next(request)
        return apply_security_headers(response, path)
