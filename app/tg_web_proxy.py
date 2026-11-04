import sqlite3
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import ROOT, as_dict, resolve_session

UPSTREAM_BASE = "https://web.telegram.org/a/"
CACHE_DIR = ROOT / "data" / "tg_web_cache"
_SSL_CTX = ssl._create_unverified_context()

_MEM_CACHE: dict[str, tuple[bytes, str]] = {}

MIME_BY_EXT = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".wasm": "application/wasm",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".xml": "application/xml; charset=utf-8",
    ".tgs": "application/gzip",
}


def extract_telethon_web_session(conn, account_id: int | None = None) -> dict | None:
    """Extract (account_id, dc_id, auth_key_hex, telegram_user_id) from a connected Telethon .session."""
    rows = conn.execute(
        """
        SELECT * FROM accounts
        WHERE provider = 'telegram' OR provider IS NULL
        ORDER BY CASE WHEN status = 'connected' THEN 0 ELSE 1 END, id ASC
        """
    ).fetchall()
    candidates = [as_dict(r) for r in rows]
    if account_id is not None and int(account_id) > 0:
        preferred = [c for c in candidates if int(c["id"]) == int(account_id)]
        others = [c for c in candidates if int(c["id"]) != int(account_id)]
        candidates = preferred + others

    for acc in candidates:
        session_path = str(acc.get("session_path") or "").strip()
        if not session_path:
            continue
        abs_path = resolve_session(session_path)
        if not abs_path.exists() and not str(abs_path).endswith(".session"):
            abs_path = Path(f"{abs_path}.session")
        if not abs_path.exists() and acc.get("session_blob"):
            try:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_bytes(bytes(acc["session_blob"]))
            except Exception:
                pass
        if not abs_path.exists():
            continue
        try:
            with sqlite3.connect(abs_path) as sconn:
                srow = sconn.execute(
                    "SELECT dc_id, auth_key FROM sessions WHERE auth_key IS NOT NULL AND length(auth_key) = 256 LIMIT 1"
                ).fetchone()
                if not srow:
                    continue
                dc_id = int(srow[0])
                auth_key_hex = bytes(srow[1]).hex()
                user_id = acc.get("telegram_user_id")
                if not user_id:
                    erow = sconn.execute(
                        "SELECT id FROM entities WHERE id > 0 AND id != 777000 ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if erow:
                        user_id = int(erow[0])
                if dc_id and auth_key_hex and user_id:
                    return {
                        "account_id": int(acc["id"]),
                        "dc_id": dc_id,
                        "auth_key_hex": auth_key_hex,
                        "user_id": int(user_id),
                        "username": acc.get("telegram_username") or "",
                        "phone": acc.get("phone") or "",
                        "name": acc.get("telegram_name") or "",
                    }
        except Exception:
            continue
    return None


def _guess_mime(path: str, upstream_ct: str = "") -> str:
    ext = Path(path).suffix.lower()
    if ext in MIME_BY_EXT:
        return MIME_BY_EXT[ext]
    if upstream_ct:
        return upstream_ct
    return "application/octet-stream"


def _fetch_upstream_asset(rel_path: str) -> tuple[bytes, str]:
    clean = rel_path.lstrip("/")
    if clean in _MEM_CACHE:
        return _MEM_CACHE[clean]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    disk_file = CACHE_DIR / (clean if clean else "index.html")
    if clean and disk_file.exists() and disk_file.is_file():
        data = disk_file.read_bytes()
        mime = _guess_mime(clean)
        _MEM_CACHE[clean] = (data, mime)
        return data, mime

    url = f"{UPSTREAM_BASE}{clean}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
        data = resp.read()
        ct = resp.headers.get("Content-Type") or ""
        mime = _guess_mime(clean or "index.html", ct)

    if clean:
        try:
            disk_file.parent.mkdir(parents=True, exist_ok=True)
            disk_file.write_bytes(data)
        except Exception:
            pass
    _MEM_CACHE[clean] = (data, mime)
    return data, mime


def build_session_init_js(session_info: dict | None) -> str:
    if not session_info:
        return "// No active Telegram session in CRM\n"
    dc_id = int(session_info["dc_id"])
    auth_key_hex = str(session_info["auth_key_hex"])
    user_id = str(session_info["user_id"])
    acc_id = int(session_info["account_id"])
    return f"""(function() {{
  try {{
    var dc = {dc_id};
    var authKey = "{auth_key_hex}";
    var userId = "{user_id}";
    var accId = "{acc_id}";
    var sig = accId + ":" + dc + ":" + userId + ":" + authKey.slice(0, 24);
    var prevSig = localStorage.getItem("foldesk_tg_session_sig");
    var existingKey = localStorage.getItem("dc" + dc + "_auth_key");
    var existingUser = localStorage.getItem("user_auth");
    if (prevSig !== sig || !existingKey || !existingUser) {{
      localStorage.setItem("dc", String(dc));
      localStorage.setItem("dc" + dc + "_auth_key", JSON.stringify(authKey));
      localStorage.setItem("user_auth", JSON.stringify({{
        dcID: dc,
        id: String(userId),
        date: Math.floor(Date.now() / 1000)
      }}));
      for (var i = 1; i <= 5; i++) {{
        localStorage.setItem("dc" + i + "_hash", JSON.stringify(""));
      }}
      if (prevSig && prevSig !== sig) {{
        localStorage.removeItem("tt-global-state");
      }}
      localStorage.setItem("foldesk_tg_session_sig", sig);
    }}
  }} catch (e) {{
    console.error("Foldesk TG Web session init error:", e);
  }}
}})();
"""


def register_tg_web_routes(app) -> None:
    @app.get("/a")
    def tg_web_root_redirect(request: Request):
        qs = request.url.query
        target = f"/a/?{qs}" if qs else "/a/"
        return RedirectResponse(url=target, status_code=307)

    @app.get("/a/foldesk-session-init.js")
    def tg_web_session_init_js(request: Request, account_id: int | None = None):
        cookie_acc = request.cookies.get("foldesk_tg_account_id")
        resolved_acc_id = account_id
        if resolved_acc_id is None and cookie_acc and cookie_acc.isdigit():
            resolved_acc_id = int(cookie_acc)
        info = extract_telethon_web_session(request.app.state.conn, resolved_acc_id)
        js_code = build_session_init_js(info)
        return Response(
            content=js_code,
            media_type="application/javascript; charset=utf-8",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
            },
        )

    @app.get("/a/")
    @app.get("/a/{asset_path:path}")
    def tg_web_proxy(request: Request, asset_path: str = "", account_id: int | None = None):
        clean = (asset_path or "").strip("/")
        if not clean or clean == "index.html":
            try:
                raw_html, _ = _fetch_upstream_asset("")
                html_text = raw_html.decode("utf-8", errors="replace")
            except Exception as exc:
                return HTMLResponse(
                    f"<!doctype html><html><body><p>Не удалось загрузить Telegram Web A: {exc}</p></body></html>",
                    status_code=502,
                )
            resolved_acc = account_id
            if resolved_acc is None:
                cookie_acc = request.cookies.get("foldesk_tg_account_id")
                if cookie_acc and cookie_acc.isdigit():
                    resolved_acc = int(cookie_acc)
            info = extract_telethon_web_session(request.app.state.conn, resolved_acc)
            init_tag = (
                f'<script src="./foldesk-session-init.js'
                f'{"?account_id=" + str(info["account_id"]) if info else ""}'
                f'"></script>\n  '
            )
            # Remove restrictive CSP meta tag so iframe and our session initializer work seamlessly
            import re

            html_text = re.sub(
                r'<meta\s+http-equiv=["\']Content-Security-Policy["\'][^>]*>',
                "",
                html_text,
                flags=re.IGNORECASE,
            )
            if '<script  src="./redirect.js"></script>' in html_text:
                html_text = html_text.replace(
                    '<script  src="./redirect.js"></script>',
                    init_tag + '<script  src="./redirect.js"></script>',
                    1,
                )
            elif "<head>" in html_text:
                html_text = html_text.replace("<head>", "<head>\n  " + init_tag, 1)

            resp = HTMLResponse(
                content=html_text,
                headers={"Cache-Control": "no-cache"},
            )
            if info:
                resp.set_cookie(
                    "foldesk_tg_account_id",
                    str(info["account_id"]),
                    path="/a",
                    samesite="lax",
                )
            return resp

        try:
            data, mime = _fetch_upstream_asset(clean)
        except urllib.error.HTTPError as exc:
            return Response(status_code=exc.code, content=b"")
        except Exception:
            return Response(status_code=502, content=b"")

        return Response(
            content=data,
            media_type=mime,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )
