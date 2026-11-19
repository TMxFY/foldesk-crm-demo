"""Deprecated Telegram Web A proxy stub.

Live proxying of https://web.telegram.org/a/ and server auth_key injection into
browser localStorage were permanently removed to eliminate AuthKeyDuplicatedError
and remove the unauthenticated CSP-stripping / unbounded memory cache surface.
"""

from __future__ import annotations

import os
import ssl


def _build_ssl_context(*, verify: bool = True) -> ssl.SSLContext:
    if not verify:
        return ssl._create_unverified_context()
    cafile: str | None = None
    try:
        import certifi

        cafile = certifi.where()
    except Exception:
        if os.path.exists("/etc/ssl/cert.pem"):
            cafile = "/etc/ssl/cert.pem"
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


_SSL_CTX = _build_ssl_context(verify=True)


def extract_all_telethon_web_sessions(conn) -> list[dict]:
    """Server Telethon MTProto auth_keys are never exposed to browser clients."""
    return []


def extract_telethon_web_session(conn, account_id: int | None = None) -> dict | None:
    """Server Telethon sessions are strictly server-side (never shared with Web A)."""
    return None


def register_tg_web_routes(app) -> None:
    """No-op: /a/* routes are permanently unmounted and return 404."""
    return None
