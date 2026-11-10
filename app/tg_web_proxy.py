import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import ROOT

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


def extract_all_telethon_web_sessions(conn) -> list[dict]:
    """Never expose server Telethon MTProto auth_keys to browser clients.

    Sharing the same auth_key between a browser IP (Telegram Web A) and the
    server's NodeMaven proxy IP triggers Telegram's 406 AUTH_KEY_DUPLICATED
    and permanently revokes the session.
    """
    return []


def extract_telethon_web_session(conn, account_id: int | None = None) -> dict | None:
    """Server Telethon sessions are strictly server-side (never shared with Web A)."""
    return None


def _guess_mime(path: str, upstream_ct: str = "") -> str:
    ext = Path(path).suffix.lower()
    if ext in MIME_BY_EXT:
        return MIME_BY_EXT[ext]
    if upstream_ct:
        return upstream_ct
    return "application/octet-stream"


def _fetch_upstream_asset(rel_path: str) -> tuple[bytes, str] | None:
    unquoted = urllib.parse.unquote(rel_path or "")
    clean = unquoted.lstrip("/")
    if "\x00" in unquoted or "\\" in unquoted or ".." in Path(clean).parts:
        return None

    cache_root = CACHE_DIR.resolve()
    target_rel = clean if clean else "index.html"
    try:
        disk_file = (CACHE_DIR / target_rel).resolve()
        disk_file.relative_to(cache_root)
    except (ValueError, OSError):
        return None

    if clean in _MEM_CACHE:
        return _MEM_CACHE[clean]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
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


def build_session_init_js(
    session_info: dict | None = None, all_sessions: list[dict] | None = None
) -> str:
    """Scrub any previously injected server auth_keys from browser localStorage."""
    return """(function() {
  try {
    if (!localStorage.getItem("foldesk_tg_scrub_v2") || localStorage.getItem("foldesk_tg_session_sig") || localStorage.getItem("foldesk_tg_dead_accs")) {
      localStorage.removeItem("foldesk_tg_session_sig");
      localStorage.removeItem("foldesk_tg_failed_sig");
      localStorage.removeItem("foldesk_tg_dead_accs");
      localStorage.removeItem("user_auth");
      localStorage.removeItem("tt-global-state");
      localStorage.removeItem("dc");
      localStorage.removeItem("auth_key_fingerprint");
      for (var sIdx = 1; sIdx <= 20; sIdx++) {
        localStorage.removeItem("account" + sIdx);
      }
      for (var i = 1; i <= 5; i++) {
        localStorage.removeItem("dc" + i + "_auth_key");
        localStorage.removeItem("dc" + i + "_server_salt");
        localStorage.removeItem("dc" + i + "_hash");
      }
      localStorage.setItem("foldesk_tg_scrub_v2", "1");
    }
  } catch (e) {}
})();
"""


def register_tg_web_routes(app) -> None:
    @app.get("/a")
    def tg_web_root_redirect(request: Request):
        qs = request.url.query
        target = f"/a/?{qs}" if qs else "/a/"
        return RedirectResponse(url=target, status_code=307)

    @app.get("/a/foldesk-session-init.js")
    def tg_web_session_init_js(request: Request, account_id: int | None = None):
        js_code = build_session_init_js(None, [])
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
        raw_req_path = request.scope.get("raw_path") or b""
        if b".." in raw_req_path or b"%2e%2e" in raw_req_path.lower():
            return Response(status_code=404, content=b"")
        clean = (asset_path or "").strip("/")
        if not clean or clean == "index.html":
            try:
                fetched_root = _fetch_upstream_asset("")
                if fetched_root is None:
                    return Response(status_code=404, content=b"")
                raw_html, _ = fetched_root
                html_text = raw_html.decode("utf-8", errors="replace")
            except Exception as exc:
                return HTMLResponse(
                    f"<!doctype html><html><body><p>Не удалось загрузить Telegram Web A: {exc}</p></body></html>",
                    status_code=502,
                )
            init_tag = '<script src="./foldesk-session-init.js"></script>\n  '
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
            resp.delete_cookie("foldesk_tg_account_id", path="/a")
            return resp

        try:
            fetched = _fetch_upstream_asset(clean)
        except urllib.error.HTTPError as exc:
            return Response(status_code=exc.code, content=b"")
        except Exception:
            return Response(status_code=502, content=b"")
        if fetched is None:
            return Response(status_code=404, content=b"")
        data, mime = fetched

        return Response(
            content=data,
            media_type=mime,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )
