import json
import re
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


def extract_all_telethon_web_sessions(conn) -> list[dict]:
    """Extract all connected Telethon .session accounts with their 1-based Telegram Web A slot."""
    rows = conn.execute(
        """
        SELECT * FROM accounts
        WHERE provider = 'telegram' OR provider IS NULL
        ORDER BY CASE WHEN status = 'connected' THEN 0 ELSE 1 END, id ASC
        """
    ).fetchall()
    out: list[dict] = []
    seen_users: set[int] = set()
    for raw in rows:
        acc = as_dict(raw)
        if str(acc.get("status") or "") != "connected":
            continue
        if str(acc.get("health") or "") in ("unauthorized", "offline", "banned", "frozen"):
            continue
        session_path = str(acc.get("session_path") or "").strip()
        if not session_path or session_path.startswith("data/sessions/demo"):
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
                raw_phone = re.sub(r"\D", "", str(acc.get("phone") or ""))
                if not user_id and raw_phone:
                    erow = sconn.execute(
                        "SELECT id FROM entities WHERE (phone = ? OR CAST(phone AS TEXT) = ?) AND id > 0 LIMIT 1",
                        (int(raw_phone) if raw_phone.isdigit() else raw_phone, raw_phone),
                    ).fetchone()
                    if erow:
                        user_id = int(erow[0])
                if dc_id and auth_key_hex and user_id:
                    uid = int(user_id)
                    if uid in seen_users:
                        continue
                    seen_users.add(uid)
                    full_name = str(acc.get("telegram_name") or "").strip()
                    parts = full_name.split(" ", 1) if full_name else [""]
                    first_name = parts[0] or str(acc.get("telegram_username") or acc.get("phone") or "Telegram")
                    last_name = parts[1] if len(parts) > 1 else ""
                    raw_phone = str(acc.get("phone") or "").strip().lstrip("+")
                    slot = len(out) + 1
                    out.append(
                        {
                            "slot": slot,
                            "account_id": int(acc["id"]),
                            "dc_id": dc_id,
                            "auth_key_hex": auth_key_hex,
                            "user_id": uid,
                            "username": acc.get("telegram_username") or "",
                            "phone": acc.get("phone") or "",
                            "phone_digits": raw_phone,
                            "name": full_name,
                            "first_name": first_name,
                            "last_name": last_name,
                        }
                    )
        except Exception:
            continue
    return out


def extract_telethon_web_session(conn, account_id: int | None = None) -> dict | None:
    """Extract (account_id, dc_id, auth_key_hex, telegram_user_id) from a connected Telethon .session."""
    all_sessions = extract_all_telethon_web_sessions(conn)
    if not all_sessions:
        return None
    if account_id is not None and int(account_id) > 0:
        for item in all_sessions:
            if int(item["account_id"]) == int(account_id):
                return item
    return all_sessions[0]


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


def build_session_init_js(
    session_info: dict | None, all_sessions: list[dict] | None = None
) -> str:
    multitab_guard = """
    if (typeof window.BroadcastChannel === "function" && !window.__foldeskBcPatched) {
      var NativeBC = window.BroadcastChannel;
      var tabUid = Math.random().toString(36).slice(2, 10);
      window.BroadcastChannel = function(name) {
        var n = String(name || "");
        if (n.indexOf("tt-establish") === 0 || n.indexOf("tt-multitab") === 0) {
          return new NativeBC(n + "_" + tabUid);
        }
        return new NativeBC(n);
      };
      window.BroadcastChannel.prototype = NativeBC.prototype;
      window.__foldeskBcPatched = true;
    }
"""
    if not session_info:
        return f"""(function() {{
  try {{{multitab_guard}
    if (localStorage.getItem("foldesk_tg_session_sig")) {{
      localStorage.removeItem("foldesk_tg_session_sig");
      localStorage.removeItem("foldesk_tg_failed_sig");
      localStorage.removeItem("user_auth");
      localStorage.removeItem("account1");
      localStorage.removeItem("tt-global-state");
      for (var i = 1; i <= 5; i++) {{
        localStorage.removeItem("dc" + i + "_auth_key");
        localStorage.removeItem("dc" + i + "_hash");
      }}
    }}
  }} catch (e) {{}}
}})();
"""
    sessions_list = all_sessions if all_sessions else [session_info]
    dc_id = int(session_info["dc_id"])
    auth_key_hex = str(session_info["auth_key_hex"])
    user_id = str(session_info["user_id"])
    acc_id = int(session_info["account_id"])
    sessions_json = json.dumps(
        [
            {
                "slot": int(s.get("slot") or idx),
                "account_id": int(s["account_id"]),
                "dc_id": int(s["dc_id"]),
                "auth_key_hex": str(s["auth_key_hex"]),
                "user_id": str(s["user_id"]),
                "first_name": str(s.get("first_name") or s.get("name") or ""),
                "last_name": str(s.get("last_name") or ""),
                "phone": str(s.get("phone_digits") or str(s.get("phone") or "").lstrip("+")),
            }
            for idx, s in enumerate(sessions_list, start=1)
        ],
        ensure_ascii=False,
    )
    return f"""(function() {{
  try {{{multitab_guard}
    var dc = {dc_id};
    var authKey = "{auth_key_hex}";
    var userId = "{user_id}";
    var accId = "{acc_id}";
    var allSessions = {sessions_json};
    for (var k = 0; k < allSessions.length; k++) {{
      var s = allSessions[k];
      var slotKey = "account" + s.slot;
      var slotObj = {{
        userId: String(s.user_id),
        dcId: Number(s.dc_id)
      }};
      slotObj["dc" + s.dc_id + "_auth_key"] = s.auth_key_hex;
      if (s.first_name) slotObj.firstName = s.first_name;
      if (s.last_name) slotObj.lastName = s.last_name;
      if (s.phone) slotObj.phone = s.phone;
      localStorage.setItem(slotKey, JSON.stringify(slotObj));
    }}

    var urlParams = new URLSearchParams(window.location.search || "");
    var rawSlot = urlParams.get("account");
    var slotParam = rawSlot ? Number(rawSlot) : 0;
    var active = null;
    if (slotParam >= 1) {{
      for (var m = 0; m < allSessions.length; m++) {{
        if (Number(allSessions[m].slot) === slotParam) {{
          active = allSessions[m];
          break;
        }}
      }}
    }}
    if (!active) {{
      for (var n = 0; n < allSessions.length; n++) {{
        if (String(allSessions[n].account_id) === String(accId)) {{
          active = allSessions[n];
          break;
        }}
      }}
    }}
    if (active) {{
      dc = Number(active.dc_id);
      authKey = String(active.auth_key_hex);
      userId = String(active.user_id);
      accId = String(active.account_id);
    }}

    var sig = accId + ":" + dc + ":" + userId + ":" + authKey.slice(0, 24);
    var prevSig = localStorage.getItem("foldesk_tg_session_sig");
    var existingKey = localStorage.getItem("dc" + dc + "_auth_key");
    var existingUser = localStorage.getItem("user_auth");
    localStorage.removeItem("foldesk_tg_failed_sig");

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
      localStorage.removeItem("tt-global-state");
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
        all_sessions = extract_all_telethon_web_sessions(request.app.state.conn)
        info = extract_telethon_web_session(request.app.state.conn, resolved_acc_id)
        js_code = build_session_init_js(info, all_sessions)
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
