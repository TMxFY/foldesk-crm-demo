import asyncio
import hashlib
import os
import re
import shutil
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import unquote, urlparse

from datetime import timezone

from app.db import DEFAULT_DB, resolve_session, utc_now


class GatewayError(Exception):
    pass


class TelegramGateway(ABC):
    @abstractmethod
    async def request_code(self, phone: str, session_path: str) -> str:
        """Send a login code. Return phone_code_hash."""

    @abstractmethod
    async def confirm_code(
        self, phone: str, code: str, phone_code_hash: str, session_path: str
    ) -> dict:
        """Return status connected or pending_password, plus profile on success."""

    @abstractmethod
    async def confirm_password(self, password: str, session_path: str) -> dict:
        """Finish 2FA. Return status connected and profile."""

    @abstractmethod
    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        """User dialogs only. A query starting with @ resolves that username."""

    async def send_message(self, session_path: str, peer_id: int, text: str, reply_to=None) -> dict:
        raise GatewayError("Отправка не подключена")

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        """Last personal-dialog messages, oldest first. Not groups or channels."""
        raise GatewayError("История не подключена")

    async def watch_private(self, session_path: str, on_message) -> None:
        """Listen until cancelled. Deliver incoming private messages to on_message."""
        raise GatewayError("Живые входящие не подключены")

    async def load_inbox(self, session_path: str, limit: int = 30) -> list:
        """Private dialogs already on the account. Not search, not groups or channels."""
        raise GatewayError("Список входящих не подключён")

    async def session_health(self, session_path: str) -> dict:
        """Probe a saved session. Return health and last_error. Do not log in again."""
        raise GatewayError("Проверка сессии не подключена")

    async def list_folders(self, session_path: str) -> list:
        """Own Telegram folders. Not members of groups or channels."""
        raise GatewayError("Папки не подключены")

    async def folder_peers(self, session_path: str, telegram_folder_id: int) -> list:
        """Personal dialogs already listed in one own folder."""
        raise GatewayError("Папки не подключены")


CHAT_TYPE_REASON = (
    "В папке стоят типы чатов или ссылки-приглашения. Уберите их в Telegram."
)


def _filter_title(item) -> str:
    title = getattr(item, "title", "") or ""
    text = getattr(title, "text", None)
    if text is not None:
        title = text
    if isinstance(title, bytes):
        title = title.decode("utf-8", "replace")
    return str(title).strip()


def _folder_block_reason(item) -> str | None:
    # Contacts/Groups/Bots and chatlists with invite links are not an explicit people list.
    if item.__class__.__name__ == "DialogFilterChatlist":
        return CHAT_TYPE_REASON
    flags = ("contacts", "non_contacts", "groups", "broadcasts", "bots")
    if any(getattr(item, flag, None) for flag in flags):
        return CHAT_TYPE_REASON
    return None


SESSION_HEALTH = ("active", "offline", "unauthorized", "banned", "frozen")


def _clip_error(exc) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]


def _classify_session_error(exc: Exception) -> dict:
    text = _clip_error(exc)
    blob = f"{exc.__class__.__name__} {text}".casefold()
    if "frozen" in blob:
        return {"health": "frozen", "last_error": text}
    try:
        from telethon import errors
    except Exception:
        errors = None
    if errors is not None and isinstance(
        exc, (errors.UserDeactivatedBanError, errors.PhoneNumberBannedError)
    ):
        return {"health": "banned", "last_error": text}
    if errors is not None and isinstance(
        exc,
        (
            errors.UnauthorizedError,
            errors.AuthKeyError,
            errors.UserDeactivatedError,
            getattr(errors, "ConnectionLayerInvalidError", errors.UnauthorizedError),
        ),
    ):
        return {"health": "unauthorized", "last_error": text}
    if "auth_key_unregistered" in blob or "connectionlayerinvaliderror" in blob or "invokeWithLayer" in text:
        return {"health": "unauthorized", "last_error": "Сессия сброшена Telegram — войдите заново"}
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return {"health": "offline", "last_error": text or "Нет связи"}
    if errors is not None and isinstance(exc, errors.TimedOutError):
        return {"health": "offline", "last_error": text or "Нет связи"}
    return {"health": "offline", "last_error": text or "Не удалось проверить сессию"}


def _session_name(session_path: str) -> str:
    # Telethon appends .session itself. The stored path already has the suffix.
    path = resolve_session(session_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".session":
        return str(path.with_suffix(""))
    return str(path)


def _person_name(user) -> str:
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    return " ".join(part for part in (first, last) if part).strip()


def _stamp(sent) -> str:
    if sent is None:
        return utc_now()
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return sent.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def listening_event(gateway, session_path: str) -> asyncio.Event:
    events = getattr(gateway, "_listening", None)
    if events is None:
        events = {}
        gateway._listening = events
    event = events.get(session_path)
    if event is None:
        event = asyncio.Event()
        events[session_path] = event
    return event


def _private_incoming(message) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get("is_group"):
        return False
    if (message.get("peer_type") or "user") != "user":
        return False
    if (message.get("direction") or "in") != "in":
        return False
    return message.get("telegram_user_id") is not None


def _inbox_limit(limit) -> int:
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 30
    if cap < 1:
        cap = 1
    return min(cap, 30)


def _history_limit(limit) -> int:
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 50
    if cap < 1:
        cap = 1
    return min(cap, 50)


_PROXY_PRESET_COUNTRIES = {
    "eu_residential": "de",
    "us_dedicated": "us",
    "cis_mobile": "kz",
}

_PHONE_PREFIX_COUNTRIES = (
    ("380", "ua"),
    ("375", "by"),
    ("998", "uz"),
    ("374", "am"),
    ("995", "ge"),
    ("971", "ae"),
    ("77", "kz"),
    # Direct MTProto TCP to Telegram DCs is blocked/throttled by TSPU on Russian residential ISPs;
    # route +7 numbers via CIS Kazakhstan residential nodes by default.
    ("7", "kz"),
    ("44", "gb"),
    ("49", "de"),
    ("33", "fr"),
    ("31", "nl"),
    ("34", "es"),
    ("39", "it"),
    ("48", "pl"),
    ("90", "tr"),
    ("62", "id"),
    ("55", "br"),
    ("1", "us"),
)


def infer_proxy_country(
    phone: str | None = None,
    preset: str | None = None,
    default_country: str = "us",
) -> str:
    fallback = (default_country or "us").strip().lower() or "us"
    raw_preset = (preset or "").strip().lower()
    if raw_preset in _PROXY_PRESET_COUNTRIES:
        return _PROXY_PRESET_COUNTRIES[raw_preset]
    if raw_preset.startswith("country-") and len(raw_preset) == 10:
        return raw_preset.split("-", 1)[1]
    if raw_preset.startswith("nodemaven:") and len(raw_preset.split(":", 1)[1]) == 2:
        return raw_preset.split(":", 1)[1]
    if len(raw_preset) == 2 and raw_preset.isalpha():
        return raw_preset

    digits = re.sub(r"\D", "", str(phone or ""))
    if digits:
        for prefix, iso in _PHONE_PREFIX_COUNTRIES:
            if digits.startswith(prefix):
                return iso
    return fallback


def _normalize_nodemaven_ttl(ttl_val: str | int | None) -> str:
    raw = str(ttl_val or "").strip().lower()
    if not raw or raw == "86400":
        return "24h"
    if raw.isdigit():
        secs = int(raw)
        if secs >= 3600:
            return f"{max(1, min(24, secs // 3600))}h"
        return f"{max(1, secs // 60)}m"
    if re.fullmatch(r"\d+[smhd]", raw):
        return raw
    return "24h"


_NODEMAVEN_API_CACHE: dict[str, tuple[str, str]] = {}


def fetch_nodemaven_api_credentials(api_key: str) -> tuple[str, str] | None:
    key = (api_key or "").strip()
    if not key:
        return None
    if key in _NODEMAVEN_API_CACHE:
        return _NODEMAVEN_API_CACHE[key]
    try:
        import httpx

        headers = {"Authorization": f"x-api-key {key}", "Accept": "application/json"}
        with httpx.Client(timeout=10.0) as client:
            resp = client.get("https://api.nodemaven.com/api/v2/base/sub-users/", headers=headers)
            if resp.status_code == 200:
                payload = resp.json()
                items = []
                if isinstance(payload, dict):
                    items = payload.get("payload") or payload.get("results") or []
                elif isinstance(payload, list):
                    items = payload
                if isinstance(items, list):
                    sorted_items = sorted(
                        (i for i in items if isinstance(i, dict)),
                        key=lambda x: 0 if x.get("is_default_user") else 1,
                    )
                    for item in sorted_items:
                        u = str(item.get("proxy_username") or item.get("username") or "").strip()
                        p = str(item.get("proxy_password") or item.get("password") or "").strip()
                        if u and p:
                            _NODEMAVEN_API_CACHE[key] = (u, p)
                            return (u, p)
            # Also check /account/ in case proxy credentials are returned on main account
            acc_resp = client.get("https://api.nodemaven.com/api/v2/base/account/", headers=headers)
            if acc_resp.status_code == 200:
                acc_data = acc_resp.json()
                if isinstance(acc_data, dict):
                    u = str(acc_data.get("proxy_username") or acc_data.get("username") or "").strip()
                    p = str(acc_data.get("proxy_password") or acc_data.get("password") or "").strip()
                    if u and p:
                        _NODEMAVEN_API_CACHE[key] = (u, p)
                        return (u, p)
    except Exception:
        pass
    return None


def fetch_nodemaven_balance(api_key: str) -> dict | None:
    key = (api_key or "").strip()
    if not key:
        return None
    try:
        import httpx

        headers = {"Authorization": f"x-api-key {key}", "Accept": "application/json"}
        with httpx.Client(timeout=8.0) as client:
            resp = client.get("https://api.nodemaven.com/api/v2/base/account/balance/", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return None


def resolve_nodemaven_config(conn=None) -> dict | None:
    api_key = os.getenv("NODEMAVEN_API_KEY", "").strip()
    user = os.getenv("NODEMAVEN_USER", "").strip()
    password = os.getenv("NODEMAVEN_PASS", "").strip() or os.getenv("NODEMAVEN_PASSWORD", "").strip()
    host = os.getenv("NODEMAVEN_HOST", "").strip()
    port = os.getenv("NODEMAVEN_PORT", "").strip()
    ttl = os.getenv("NODEMAVEN_TTL", "").strip()
    flt = os.getenv("NODEMAVEN_FILTER", "").strip()
    default_country = os.getenv("NODEMAVEN_DEFAULT_COUNTRY", "").strip()

    def _read_kv(c) -> None:
        nonlocal api_key, user, password, host, port, ttl, flt, default_country
        try:
            rows = dict(
                c.execute(
                    """
                    SELECT key, value FROM kv_settings
                    WHERE key IN (
                        'nodemaven_api_key', 'nodemaven_user', 'nodemaven_pass',
                        'nodemaven_host', 'nodemaven_port', 'nodemaven_ttl',
                        'nodemaven_filter', 'nodemaven_default_country'
                    )
                    """
                ).fetchall()
            )
            api_key = api_key or str(rows.get("nodemaven_api_key") or "").strip()
            user = user or str(rows.get("nodemaven_user") or "").strip()
            password = password or str(rows.get("nodemaven_pass") or "").strip()
            host = host or str(rows.get("nodemaven_host") or "").strip()
            port = port or str(rows.get("nodemaven_port") or "").strip()
            ttl = ttl or str(rows.get("nodemaven_ttl") or "").strip()
            flt = flt or str(rows.get("nodemaven_filter") or "").strip()
            default_country = default_country or str(rows.get("nodemaven_default_country") or "").strip()
        except Exception:
            pass

    if conn is not None:
        _read_kv(conn)
    elif (not user or not password) and "PYTEST_CURRENT_TEST" not in os.environ and DEFAULT_DB.exists():
        try:
            with sqlite3.connect(DEFAULT_DB) as c:
                _read_kv(c)
        except Exception:
            pass

    if (not user or not password) and api_key:
        fetched = fetch_nodemaven_api_credentials(api_key)
        if fetched:
            user, password = fetched
            if conn is not None:
                try:
                    now = utc_now()
                    for k, v in (("nodemaven_user", user), ("nodemaven_pass", password)):
                        conn.execute(
                            """
                            INSERT INTO kv_settings (key, value, updated_at)
                            VALUES (?, ?, ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                            """,
                            (k, v, now),
                        )
                    conn.commit()
                except Exception:
                    pass

    if not user or not password:
        return None

    try:
        port_int = int(port or 1080)
    except (TypeError, ValueError):
        port_int = 1080
    ttl_str = _normalize_nodemaven_ttl(ttl)

    return {
        "api_key": api_key or None,
        "user": user,
        "password": password,
        "host": host or "gate.nodemaven.com",
        "port": port_int,
        "ttl": ttl_str,
        "filter": (flt or "medium").strip().lower(),
        "default_country": (default_country or "us").strip().lower() or "us",
    }


def make_nodemaven_sid(account_key: str | int) -> str:
    raw = str(account_key or "").strip()
    if raw.endswith(".session"):
        raw = Path(raw).stem
    clean = re.sub(r"[^a-zA-Z0-9]", "", raw).lower()
    if not clean:
        clean = hashlib.sha1(str(account_key).encode("utf-8")).hexdigest()[:10]
    sid = f"foldeskacc{clean}"
    return sid[:28]


def build_nodemaven_proxy_url(
    account_key: str | int,
    phone: str | None = None,
    preset: str | None = None,
    conn=None,
) -> str | None:
    cfg = resolve_nodemaven_config(conn)
    if not cfg:
        return None
    country = infer_proxy_country(phone=phone, preset=preset, default_country=cfg["default_country"])
    sid = make_nodemaven_sid(account_key)
    base_user = cfg["user"]
    # If the user pasted a full NodeMaven login string from dashboard, normalize sid and country
    if "-sid-" in base_user:
        base_user = re.sub(r"-sid-[a-zA-Z0-9_]+", "", base_user)
    if "-country-" in base_user:
        base_user = re.sub(r"-country-[a-zA-Z]{2}", "", base_user)
    if "-ttl-" in base_user:
        base_user = re.sub(r"-ttl-[a-zA-Z0-9]+", "", base_user)
    if "-filter-" in base_user:
        base_user = re.sub(r"-filter-[a-zA-Z0-9_]+", "", base_user)
    base_user = base_user.strip("-")

    full_user = (
        f"{base_user}-country-{country}-sid-{sid}"
        f"-ttl-{cfg['ttl']}-filter-{cfg['filter']}"
    )
    return f"socks5://{full_user}:{cfg['password']}@{cfg['host']}:{cfg['port']}"


def mask_proxy_url(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    raw = str(proxy_url).strip()
    if "://" not in raw:
        return raw
    try:
        parsed = urlparse(raw)
        if not parsed.hostname:
            return raw
        auth = ""
        if parsed.username:
            auth = f"{unquote(parsed.username)}:***@" if parsed.password else f"{unquote(parsed.username)}@"
        port_part = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{auth}{parsed.hostname}{port_part}"
    except Exception:
        return raw


def _normalize_raw_proxy_url(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    # Support raw NodeMaven generator format: host:port:username:password
    if "://" not in text and "@" not in text:
        parts = text.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            host, port, user, pwd = parts
            return f"socks5://{user}:{pwd}@{host}:{port}"
    if "://" not in text:
        return f"socks5://{text}"
    return text


async def probe_proxy_ip(proxy_url: str, timeout: float = 8.0) -> dict:
    norm = _normalize_raw_proxy_url(proxy_url)
    parsed_tuple = _parse_proxy_for_telethon(norm)
    if not parsed_tuple:
        return {"ok": False, "error": "Некорректный формат прокси"}
    try:
        from python_socks.async_.asyncio import Proxy
        import json as _json

        proxy = Proxy.from_url(norm, rdns=True)
        sock = await asyncio.wait_for(
            proxy.connect(dest_host="ip-api.com", dest_port=80),
            timeout=timeout,
        )
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(sock=sock),
            timeout=timeout,
        )
        try:
            req = (
                "GET /json/?fields=status,country,countryCode,city,isp,query HTTP/1.1\r\n"
                "Host: ip-api.com\r\n"
                "User-Agent: FoldeskCRM/1.0\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(req.encode("ascii"))
            await writer.drain()
            raw_resp = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        finally:
            writer.close()
        body = raw_resp.decode("utf-8", "replace").split("\r\n\r\n", 1)[-1].strip()
        start = body.find("{")
        end = body.rfind("}")
        if start != -1 and end != -1:
            data = _json.loads(body[start : end + 1])
            return {
                "ok": data.get("status") == "success",
                "ip": data.get("query"),
                "country": data.get("country"),
                "country_code": str(data.get("countryCode") or "").lower(),
                "city": data.get("city"),
                "isp": data.get("isp"),
                "proxy_masked": mask_proxy_url(norm),
            }
        return {"ok": False, "error": "Пустой ответ от гео-сервиса", "proxy_masked": mask_proxy_url(norm)}
    except Exception as exc:
        return {
            "ok": False,
            "error": _clip_error(exc),
            "proxy_masked": mask_proxy_url(norm),
        }


def _parse_proxy_for_telethon(proxy_url: str | None) -> tuple | None:
    if not proxy_url:
        return None
    raw = _normalize_raw_proxy_url(str(proxy_url))
    if not raw:
        return None
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "socks5").lower()
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None
    # PySocks constants: SOCKS4 = 1, SOCKS5 = 2, HTTP = 3
    if scheme.startswith("http"):
        proxy_type = 3
    elif scheme == "socks4":
        proxy_type = 1
    else:
        proxy_type = 2
    try:
        import socks  # type: ignore

        if scheme.startswith("http"):
            proxy_type = getattr(socks, "HTTP", 3)
        elif scheme == "socks4":
            proxy_type = getattr(socks, "SOCKS4", 1)
        else:
            proxy_type = getattr(socks, "SOCKS5", 2)
    except Exception:
        pass
    user = unquote(parsed.username) if parsed.username else None
    pwd = unquote(parsed.password) if parsed.password else None
    return (proxy_type, host, int(port), True, user, pwd)


class TelethonGateway(TelegramGateway):
    def __init__(self):
        self._clients = {}
        self._locks = {}
        self._proxies = {}
        self._app_credentials = {}

    def set_proxy(self, session_path: str, proxy_url: str | None) -> None:
        if not session_path:
            return
        clean = (proxy_url or "").strip() or None
        old_proxy = self._proxies.get(session_path)
        if clean:
            self._proxies[session_path] = clean
        else:
            self._proxies.pop(session_path, None)
        if clean != old_proxy:
            old_client = self._clients.pop(session_path, None)
            if old_client is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(old_client.disconnect())
                except Exception:
                    pass

    def set_app_credentials(
        self, session_path: str, app_id: int | str | None, app_hash: str | None
    ) -> None:
        if not session_path or not app_id or not app_hash:
            return
        try:
            self._app_credentials[session_path] = (int(app_id), str(app_hash).strip())
        except (TypeError, ValueError):
            pass

    def _resolve_proxy(self, session_path: str, conn=None) -> str | None:
        cached = self._proxies.get(session_path)
        if cached and _parse_proxy_for_telethon(cached) is not None:
            return cached

        acc_id = None
        phone = None
        db_proxy = cached
        try:
            if conn is not None:
                row = conn.execute(
                    "SELECT id, phone, proxy_url FROM accounts WHERE session_path = ? ORDER BY id DESC LIMIT 1",
                    (session_path,),
                ).fetchone()
                if row:
                    acc_id, phone, db_proxy = row[0], row[1], db_proxy or row[2]
            elif DEFAULT_DB.exists():
                with sqlite3.connect(DEFAULT_DB) as c:
                    row = c.execute(
                        "SELECT id, phone, proxy_url FROM accounts WHERE session_path = ? ORDER BY id DESC LIMIT 1",
                        (session_path,),
                    ).fetchone()
                    if row:
                        acc_id, phone, db_proxy = row[0], row[1], db_proxy or row[2]
        except Exception:
            pass

        proxy_val = str(db_proxy or "").strip()
        if proxy_val and _parse_proxy_for_telethon(proxy_val) is not None:
            self._proxies[session_path] = proxy_val
            return proxy_val

        # Preset or empty -> try building sticky NodeMaven URL for this account
        nm_url = build_nodemaven_proxy_url(
            account_key=acc_id or session_path,
            phone=phone,
            preset=proxy_val or "auto",
            conn=conn,
        )
        if nm_url:
            self._proxies[session_path] = nm_url
            return nm_url

        if proxy_val:
            self._proxies[session_path] = proxy_val
            return proxy_val
        return None

    def _session_lock(self, session_path: str) -> asyncio.Lock:
        lock = self._locks.get(session_path)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_path] = lock
        return lock

    def _credentials(self, session_path: str | None = None):
        if session_path and session_path in self._app_credentials:
            return self._app_credentials[session_path]
        api_id = os.getenv("TG_API_ID", "").strip()
        api_hash = os.getenv("TG_API_HASH", "").strip()
        if (not api_id or not api_hash) and "PYTEST_CURRENT_TEST" not in os.environ:
            try:
                if DEFAULT_DB.exists():
                    with sqlite3.connect(DEFAULT_DB) as conn:
                        rows = dict(
                            conn.execute(
                                "SELECT key, value FROM kv_settings WHERE key IN ('tg_api_id', 'tg_api_hash')"
                            ).fetchall()
                        )
                        api_id = api_id or str(rows.get("tg_api_id") or "").strip()
                        api_hash = api_hash or str(rows.get("tg_api_hash") or "").strip()
            except Exception:
                pass
            if not api_id or not api_hash:
                api_id = "2040"
                api_hash = "b18441a1ff607e10a989891a5462e627"
        if not api_id or not api_hash:
            raise GatewayError(
                "Нужны ключи TG_API_ID и TG_API_HASH с https://my.telegram.org/apps"
            )
        return int(api_id), api_hash

    def _fallback_nodemaven_urls(self, session_path: str, primary_url: str | None) -> list[str]:
        if not primary_url or "nodemaven.com" not in primary_url:
            return []
        m = re.search(r"-country-([a-z]{2})-", primary_url)
        primary_country = m.group(1) if m else "kz"
        first_country = "kz" if primary_country == "ru" else primary_country
        stem = Path(str(session_path or "")).stem or "acc"
        candidates: list[str] = []
        # Rotate sticky session IDs within the SAME country so Telegram never sees cross-country IP jumps
        for idx in (1, 2):
            fb_sid = make_nodemaven_sid(f"{stem}fb{idx}")
            if "-country-" in primary_url and "-sid-" in primary_url:
                fb = re.sub(r"-country-[a-z]{2}-", f"-country-{first_country}-", primary_url)
                fb = re.sub(r"-sid-[a-zA-Z0-9_]+", f"-sid-{fb_sid}", fb)
            else:
                fb = build_nodemaven_proxy_url(
                    account_key=f"{stem}fb{idx}",
                    preset=first_country,
                )
            if fb and fb != primary_url and fb not in candidates:
                candidates.append(fb)
        return candidates

    async def _open(self, session_path: str):
        client = self._clients.get(session_path)
        if client is not None:
            if client.is_connected():
                return client, False
            try:
                await client.connect()
                return client, False
            except Exception:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                self._clients.pop(session_path, None)
                self._proxies.pop(session_path, None)
        from telethon import TelegramClient

        api_id, api_hash = self._credentials(session_path)
        primary_url = self._resolve_proxy(session_path)
        candidate_urls = [primary_url] + self._fallback_nodemaven_urls(session_path, primary_url)
        last_exc: Exception | None = None

        for idx, candidate_url in enumerate(candidate_urls):
            proxy_tuple = _parse_proxy_for_telethon(candidate_url)
            kwargs: dict = {
                "device_model": "MacBookPro18,3",
                "system_version": "macOS 14.5",
                "app_version": "10.12",
                "lang_code": "en",
                "system_lang_code": "en-US",
            }
            if proxy_tuple:
                kwargs["proxy"] = proxy_tuple
                kwargs["timeout"] = 15
                kwargs["connection_retries"] = 2
                kwargs["retry_delay"] = 1
            client = TelegramClient(_session_name(session_path), api_id, api_hash, **kwargs)
            try:
                await client.connect()
                if candidate_url and idx > 0:
                    self._proxies[session_path] = candidate_url
                self._clients[session_path] = client
                return client, False
            except Exception as exc:
                last_exc = exc
                try:
                    await client.disconnect()
                except Exception:
                    pass
                # Never retry across fallback proxies when the session auth_key itself is rejected by Telegram
                if _classify_session_error(exc).get("health") in ("unauthorized", "banned"):
                    break

        if last_exc is not None:
            raise last_exc
        raise GatewayError("Не удалось подключиться к Telegram")

    async def _close(self, session_path: str) -> None:
        client = self._clients.pop(session_path, None)
        if client is not None and client.is_connected():
            await client.disconnect()

    async def close_all(self) -> None:
        for path in list(self._clients.keys()):
            try:
                await self._close(path)
            except Exception:
                pass

    def _reset_corrupted_session_file(self, session_path: str) -> None:
        try:
            abs_p = resolve_session(session_path)
            if abs_p.exists() and abs_p.is_file():
                backup_p = abs_p.parent / f"backup_{abs_p.name}"
                shutil.move(str(abs_p), str(backup_p))
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = abs_p.with_name(abs_p.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        except Exception:
            pass

    async def request_code(self, phone: str, session_path: str) -> str:
        async with self._session_lock(session_path):
            for attempt in range(2):
                try:
                    client, _owned = await self._open(session_path)
                    sent = await client.send_code_request(phone)
                    return sent.phone_code_hash
                except GatewayError:
                    raise
                except Exception as exc:
                    await self._close(session_path)
                    if attempt == 0 and _classify_session_error(exc).get("health") == "unauthorized":
                        self._reset_corrupted_session_file(session_path)
                        continue
                    raise GatewayError(str(exc) or "Не удалось отправить код") from exc

    async def confirm_code(
        self, phone: str, code: str, phone_code_hash: str, session_path: str
    ) -> dict:
        from telethon import errors

        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                user = await client.sign_in(
                    phone=phone, code=code, phone_code_hash=phone_code_hash
                )
                return {
                    "status": "connected",
                    "telegram_user_id": user.id,
                    "telegram_username": getattr(user, "username", None),
                    "telegram_name": _person_name(user),
                }
            except errors.SessionPasswordNeededError:
                return {"status": "pending_password"}
            except errors.PhoneCodeInvalidError as exc:
                raise GatewayError("Неверный код") from exc
            except errors.PhoneCodeExpiredError as exc:
                raise GatewayError("Код истёк, запросите новый") from exc
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(str(exc) or "Не удалось подтвердить код") from exc
            finally:
                if owned:
                    await self._close(session_path)

    async def confirm_password(self, password: str, session_path: str) -> dict:
        from telethon import errors

        async with self._session_lock(session_path):
            owned = False
            try:
                client, owned = await self._open(session_path)
                user = await client.sign_in(password=password)
                return {
                    "status": "connected",
                    "telegram_user_id": user.id,
                    "telegram_username": getattr(user, "username", None),
                    "telegram_name": _person_name(user),
                }
            except errors.PasswordHashInvalidError as exc:
                raise GatewayError("Неверный пароль") from exc
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(str(exc) or "Не удалось подтвердить пароль") from exc
            finally:
                if owned:
                    await self._close(session_path)

    async def session_health(self, session_path: str) -> dict:
        async with self._session_lock(session_path):
            try:
                client, owned = await self._open(session_path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return _classify_session_error(exc)
            try:
                if not await client.is_user_authorized():
                    return {"health": "unauthorized", "last_error": "Сессия не авторизована"}
                me = await client.get_me()
                if me is None:
                    return {"health": "unauthorized", "last_error": "Сессия не авторизована"}
                return {"health": "active", "last_error": None}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return _classify_session_error(exc)
            finally:
                if owned:
                    try:
                        await self._close(session_path)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                return await self._search(client, query, limit)
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(str(exc) or "Не удалось прочитать диалоги") from exc
            finally:
                if owned:
                    await self._close(session_path)

    async def load_inbox(self, session_path: str, limit: int = 30) -> list:
        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                return await self._load_private(client, limit)
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(str(exc) or "Не удалось прочитать входящие") from exc
            finally:
                if owned:
                    await self._close(session_path)

    async def _load_private(self, client, limit: int) -> list:
        from telethon.tl.types import User

        cap = _inbox_limit(limit)
        found = []
        async for dialog in client.iter_dialogs():
            if not getattr(dialog, "is_user", False):
                continue
            if getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False):
                continue
            entity = getattr(dialog, "entity", None)
            if not isinstance(entity, User):
                continue
            message = getattr(dialog, "message", None)
            preview = None
            sent_at = None
            last_from = None
            if message is not None:
                text = getattr(message, "message", None)
                preview = "" if text is None else str(text)[:120]
                sent_at = _stamp(getattr(message, "date", None))
                last_from = 0 if getattr(message, "out", False) else 1
            found.append(
                {
                    "telegram_user_id": int(entity.id),
                    "username": getattr(entity, "username", None),
                    "name": dialog.name or _person_name(entity),
                    "peer_type": "user",
                    "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
                    "last_message_preview": preview,
                    "last_message_at": sent_at,
                    "last_from_peer": last_from,
                }
            )
            if len(found) >= cap:
                break
        return found

    async def _search(self, client, query: str, limit: int) -> list:
        q = (query or "").strip()
        if q.startswith("@"):
            return await self._resolve_username(client, q)
        needle = q.casefold()
        found = []
        async for dialog in client.iter_dialogs():
            if not dialog.is_user:
                continue
            entity = dialog.entity
            username = getattr(entity, "username", None)
            name = dialog.name or ""
            if needle:
                blob = " ".join((name, username or "", str(entity.id))).casefold()
                if needle not in blob:
                    continue
            found.append(
                {
                    "telegram_user_id": entity.id,
                    "username": username,
                    "name": name,
                }
            )
            if len(found) >= limit:
                break
        return found

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to=None,
        image_path: str | None = None,
    ) -> dict:
        body = (text or "").strip()
        clean_img = (image_path or "").strip() or None
        if not body and not clean_img:
            raise GatewayError("Напишите сообщение")
        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                kwargs = {"reply_to": int(reply_to)} if reply_to else {}
                if isinstance(peer_id, int) or str(peer_id).lstrip("-").isdigit():
                    target = int(peer_id)
                else:
                    target = str(peer_id).strip()
                if clean_img:
                    from app.db import resolve_outreach_media

                    media_target = resolve_outreach_media(clean_img)
                    msg = await client.send_file(
                        target,
                        str(media_target),
                        caption=body or None,
                        **kwargs,
                    )
                else:
                    msg = await client.send_message(target, body, **kwargs)
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(str(exc) or "Не удалось отправить") from exc
            finally:
                if owned:
                    await self._close(session_path)
            return {
                "telegram_message_id": int(msg.id),
                "text": getattr(msg, "message", None) or body,
                "image_url": clean_img,
                "sent_at": _stamp(getattr(msg, "date", None)),
                "direction": "out",
            }

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                return await self._history(client, peer_id, limit)
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(str(exc) or "Не удалось прочитать историю") from exc
            finally:
                if owned:
                    await self._close(session_path)

    async def _history(self, client, peer_id: int, limit: int) -> list:
        # get_messages is newest-first. A channel or chat id is not a personal dialog.
        from telethon.tl.types import User

        cap = _history_limit(limit)
        try:
            target = (
                int(peer_id)
                if isinstance(peer_id, int) or str(peer_id).lstrip("-").isdigit()
                else str(peer_id).strip()
            )
            entity = await client.get_entity(target)
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(str(exc) or "Не удалось открыть диалог") from exc
        if not isinstance(entity, User):
            raise GatewayError("Это не личный диалог")
        try:
            raw = await client.get_messages(entity, limit=cap)
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(str(exc) or "Не удалось прочитать историю") from exc
        rows = []
        for msg in raw or []:
            mid = getattr(msg, "id", None)
            if mid is None:
                continue
            reply = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
            rows.append(
                {
                    "telegram_message_id": int(mid),
                    "text": getattr(msg, "message", None),
                    "sent_at": _stamp(getattr(msg, "date", None)),
                    "direction": "out" if getattr(msg, "out", False) else "in",
                    "reply_to_telegram_message_id": int(reply) if reply else None,
                }
            )
        rows.reverse()
        return rows[-cap:]

    async def _private_payload(self, event):
        # Groups and channels are not the inbox. Outgoing is stored by send_reply.
        if not getattr(event, "is_private", False):
            return None
        if getattr(event, "is_group", False) or getattr(event, "is_channel", False):
            return None
        getter = getattr(event, "get_sender", None)
        if getter is None:
            return None
        sender = await getter()
        from telethon.tl.types import User

        if not isinstance(sender, User):
            return None
        message = getattr(event, "message", None)
        if message is None or getattr(message, "id", None) is None:
            return None
        if getattr(message, "out", False):
            return None
        text = getattr(message, "message", None)
        return {
            "telegram_user_id": int(sender.id),
            "username": getattr(sender, "username", None),
            "name": _person_name(sender),
            "telegram_message_id": int(message.id),
            "text": text,
            "sent_at": _stamp(getattr(message, "date", None)),
            "direction": "in",
            "peer_type": "user",
        }

    async def watch_private(self, session_path: str, on_message) -> None:
        from telethon import events

        async with self._session_lock(session_path):
            client, _owned = await self._open(session_path)
            self._loop = asyncio.get_running_loop()

            async def handler(event):
                payload = await self._private_payload(event)
                if payload is None:
                    return
                await on_message(payload)

            client.add_event_handler(handler, events.NewMessage(incoming=True))
            try:
                # Difference only: a long outage can miss updates Telegram already dropped.
                try:
                    await client.catch_up()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                listening_event(self, session_path).set()
            except Exception:
                client.remove_event_handler(handler)
                self._clients.pop(session_path, None)
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception:
                    pass
                raise

        try:
            await client.run_until_disconnected()
        finally:
            client.remove_event_handler(handler)
            self._clients.pop(session_path, None)
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

    async def _resolve_username(self, client, query: str) -> list:
        from telethon.tl.types import User

        try:
            entity = await client.get_entity(query)
        except Exception:
            return []
        if not isinstance(entity, User):
            return []
        return [
            {
                "telegram_user_id": entity.id,
                "username": entity.username,
                "name": _person_name(entity) or (entity.username or ""),
            }
        ]

    async def list_folders(self, session_path: str) -> list:
        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                items = await self._dialog_filters(client)
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(str(exc) or "Не удалось прочитать папки") from exc
            finally:
                if owned:
                    await self._close(session_path)
            return [_folder_row(item) for item in items]

    async def folder_peers(self, session_path: str, telegram_folder_id: int) -> list:
        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                return await self._explicit_users(client, int(telegram_folder_id))
            except GatewayError:
                raise
            except Exception as exc:
                raise GatewayError(str(exc) or "Не удалось прочитать папку") from exc
            finally:
                if owned:
                    await self._close(session_path)

    async def _dialog_filters(self, client) -> list:
        from telethon.tl.functions.messages import GetDialogFiltersRequest
        from telethon.tl.types import DialogFilterDefault

        result = await client(GetDialogFiltersRequest())
        filters = getattr(result, "filters", None)
        if filters is None and isinstance(result, (list, tuple)):
            filters = result
        items = []
        for item in filters or []:
            if isinstance(item, DialogFilterDefault) or not hasattr(item, "id"):
                continue
            items.append(item)
        return items

    async def _explicit_users(self, client, telegram_folder_id: int) -> list:
        # Pinned and included users only. GetParticipants is not called.
        from telethon.tl.types import User

        match = None
        for item in await self._dialog_filters(client):
            if int(item.id) == int(telegram_folder_id):
                match = item
                break
        if match is None:
            raise GatewayError("Папка не найдена")
        reason = _folder_block_reason(match)
        if reason:
            raise GatewayError(reason)
        peers = list(getattr(match, "pinned_peers", None) or [])
        peers.extend(getattr(match, "include_peers", None) or [])
        seen = set()
        people = []
        for peer in peers:
            if peer.__class__.__name__ not in ("InputPeerUser", "PeerUser"):
                continue
            user_id = int(peer.user_id)
            if user_id in seen:
                continue
            seen.add(user_id)
            try:
                entity = await client.get_entity(peer)
            except Exception:
                continue
            if not isinstance(entity, User) or getattr(entity, "bot", False):
                continue
            people.append(
                {
                    "telegram_user_id": int(entity.id),
                    "username": getattr(entity, "username", None),
                    "name": _person_name(entity),
                    "peer_type": "user",
                }
            )
        return people

    async def resolve_phones(self, session_path: str, phones: list[str]) -> list[dict]:
        from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                contacts = [
                    InputPhoneContact(client_id=idx, phone=p, first_name=f"Contact{idx}", last_name="")
                    for idx, p in enumerate(phones, start=1)
                ]
                res = await client(ImportContactsRequest(contacts))
                users = getattr(res, "users", None) or []
                out = []
                for u in users:
                    phone_val = getattr(u, "phone", None)
                    norm_p = f"+{phone_val.lstrip('+')}" if phone_val else None
                    out.append(
                        {
                            "phone": norm_p,
                            "found": True,
                            "telegram_user_id": int(u.id),
                            "username": getattr(u, "username", None),
                            "name": _person_name(u) or getattr(u, "username", None) or (norm_p or ""),
                        }
                    )
                if users:
                    try:
                        await client(DeleteContactsRequest(id=users))
                    except Exception:
                        pass
                return out
            finally:
                if owned:
                    await self._close(session_path)

    async def get_participants(self, session_path: str, group_link: str, limit: int = 50) -> list[dict]:
        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                entity = await client.get_entity(group_link)
                group_title = getattr(entity, "title", None) or str(group_link)
                users = await client.get_participants(entity, limit=min(int(limit or 50), 200))
                out = []
                for u in users or []:
                    if getattr(u, "bot", False):
                        continue
                    out.append(
                        {
                            "telegram_user_id": int(u.id),
                            "username": getattr(u, "username", None),
                            "name": _person_name(u) or getattr(u, "username", None) or f"User #{u.id}",
                            "bio": "",
                            "role": "member",
                            "is_premium": bool(getattr(u, "premium", False)),
                            "group_title": group_title,
                        }
                    )
                return out
            finally:
                if owned:
                    await self._close(session_path)


def _folder_row(item) -> dict:
    title = _filter_title(item) or f"Папка {int(item.id)}"
    return {
        "telegram_folder_id": int(item.id),
        "title": title,
        "blocked_reason": _folder_block_reason(item),
    }


class FakeGateway(TelegramGateway):
    def __init__(
        self,
        code="12345",
        password="secret",
        needs_password=False,
        dialogs=None,
        history=None,
        health=None,
        folders=None,
    ):
        self.code = code
        self.password = password
        self.needs_password = needs_password
        self.health = health
        self.dialogs = [dict(item) for item in (dialogs or [])]
        self.folders = []
        for item in folders or []:
            self.folders.append(
                {
                    "telegram_folder_id": item.get("telegram_folder_id"),
                    "title": item.get("title") or "",
                    "blocked_reason": item.get("blocked_reason"),
                    "peers": [dict(peer) for peer in (item.get("peers") or [])],
                }
            )
        self.history = {}
        for peer, rows in (history or {}).items():
            self.history[int(peer)] = [dict(row) for row in rows]
        self._pending = {}
        self.sent = []
        self._message_seq = 0
        self._lock = threading.Lock()
        self._listeners = {}
        self._backlog = {}
        self._loop = None

    def push_private(self, session_path: str, message: dict) -> None:
        """Queue one incoming private message. Groups and channels are ignored."""
        if not _private_incoming(message):
            return
        item = dict(message)
        with self._lock:
            listener = self._listeners.get(session_path)
            loop = self._loop
            if listener is None or loop is None:
                self._backlog.setdefault(session_path, []).append(item)
                return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(listener(item))
            return
        asyncio.run_coroutine_threadsafe(listener(item), loop).result(timeout=5)

    async def watch_private(self, session_path: str, on_message) -> None:
        self._loop = asyncio.get_running_loop()
        with self._lock:
            self._listeners[session_path] = on_message
            batch = self._backlog.pop(session_path, [])
        try:
            for item in batch:
                await on_message(dict(item))
            listening_event(self, session_path).set()
            await asyncio.Future()
        finally:
            with self._lock:
                if self._listeners.get(session_path) is on_message:
                    self._listeners.pop(session_path, None)

    async def session_health(self, session_path: str) -> dict:
        forced = self.health
        if forced in SESSION_HEALTH:
            if forced == "active":
                return {"health": "active", "last_error": None}
            text = {
                "offline": "Нет связи",
                "unauthorized": "Сессия недействительна",
                "banned": "Аккаунт заблокирован",
                "frozen": "Аккаунт заморожен",
            }[forced]
            return {"health": forced, "last_error": text}
        pending = self._pending.get(session_path) or {}
        if pending.get("state") == "connected":
            return {"health": "active", "last_error": None}
        return {"health": "unauthorized", "last_error": "Сессия не авторизована"}

    async def request_code(self, phone: str, session_path: str) -> str:
        phone_code_hash = "hash-" + phone
        self._pending[session_path] = {
            "phone": phone,
            "hash": phone_code_hash,
            "state": "pending_code",
        }
        return phone_code_hash

    async def confirm_code(
        self, phone: str, code: str, phone_code_hash: str, session_path: str
    ) -> dict:
        pending = self._pending.get(session_path)
        if (
            pending is None
            or pending.get("phone") != phone
            or pending.get("hash") != phone_code_hash
            or code != self.code
        ):
            raise GatewayError("Неверный код")
        if self.needs_password:
            pending["state"] = "pending_password"
            return {"status": "pending_password"}
        pending["state"] = "connected"
        return {
            "status": "connected",
            "telegram_user_id": 1001,
            "telegram_username": "operator",
            "telegram_name": "Оператор",
        }

    async def confirm_password(self, password: str, session_path: str) -> dict:
        pending = self._pending.get(session_path)
        if pending is None or pending.get("state") != "pending_password":
            raise GatewayError("Пароль сейчас не ожидается")
        if password != self.password:
            raise GatewayError("Неверный пароль")
        pending["state"] = "connected"
        return {
            "status": "connected",
            "telegram_user_id": 1001,
            "telegram_username": "operator",
            "telegram_name": "Оператор",
        }

    async def load_inbox(self, session_path: str, limit: int = 30) -> list:
        cap = _inbox_limit(limit)
        found = []
        for item in self.dialogs:
            if not isinstance(item, dict) or item.get("is_group"):
                continue
            if (item.get("peer_type") or "user") != "user":
                continue
            peer_id = item.get("telegram_user_id")
            if peer_id is None:
                continue
            row = {
                "telegram_user_id": int(peer_id),
                "username": item.get("username"),
                "name": item.get("name") or "",
                "peer_type": "user",
            }
            if "unread_count" in item and item.get("unread_count") is not None:
                row["unread_count"] = max(0, int(item["unread_count"]))
            if item.get("last_message_preview") is not None:
                row["last_message_preview"] = str(item["last_message_preview"])[:120]
            if item.get("last_message_at") is not None:
                row["last_message_at"] = str(item["last_message_at"])
            if "last_from_peer" in item and item.get("last_from_peer") is not None:
                row["last_from_peer"] = 1 if item.get("last_from_peer") else 0
            found.append(row)
            if len(found) >= cap:
                break
        return found

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        q = (query or "").strip()
        if q.startswith("@"):
            username = q[1:].casefold()
            matched = [
                dict(item)
                for item in self.dialogs
                if (item.get("username") or "").casefold() == username
            ]
            return matched[:limit]
        needle = q.casefold()
        matched = []
        for item in self.dialogs:
            blob = " ".join(
                (
                    str(item.get("name") or ""),
                    str(item.get("username") or ""),
                    str(item.get("telegram_user_id") or ""),
                )
            ).casefold()
            if needle and needle not in blob:
                continue
            matched.append(dict(item))
            if len(matched) >= limit:
                break
        return matched

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to=None,
        image_path: str | None = None,
    ) -> dict:
        body = (text or "").strip()
        clean_img = (image_path or "").strip() or None
        if not body and not clean_img:
            raise GatewayError("Напишите сообщение")
        if isinstance(peer_id, int) or str(peer_id).lstrip("-").isdigit():
            peer_key = int(peer_id)
        else:
            peer_key = str(peer_id).strip()
        self._message_seq += 1
        used = {
            int(row.get("telegram_message_id") or 0)
            for row in self.history.get(peer_key, [])
        }
        while self._message_seq in used:
            self._message_seq += 1
        self.sent.append(
            {
                "peer_id": peer_key,
                "text": body,
                "image_url": clean_img,
                "image_path": clean_img,
                "reply_to": reply_to,
                "session_path": session_path,
            }
        )
        record = {
            "telegram_message_id": self._message_seq,
            "text": body,
            "image_url": clean_img,
            "sent_at": utc_now(),
            "direction": "out",
            "reply_to_telegram_message_id": int(reply_to) if reply_to else None,
        }
        self.history.setdefault(peer_key, []).append(dict(record))
        return record

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        cap = _history_limit(limit)
        rows = self.history.get(int(peer_id), [])
        return [dict(row) for row in rows[-cap:]]

    async def list_folders(self, session_path: str) -> list:
        rows = []
        for item in self.folders:
            rows.append(
                {
                    "telegram_folder_id": int(item["telegram_folder_id"]),
                    "title": item.get("title") or "",
                    "blocked_reason": item.get("blocked_reason"),
                }
            )
        return rows

    async def folder_peers(self, session_path: str, telegram_folder_id: int) -> list:
        wanted = int(telegram_folder_id)
        match = None
        for item in self.folders:
            if int(item["telegram_folder_id"]) == wanted:
                match = item
                break
        if match is None:
            raise GatewayError("Папка не найдена")
        if match.get("blocked_reason"):
            raise GatewayError(str(match["blocked_reason"]))
        people = []
        for peer in match.get("peers") or []:
            if not _folder_person(peer):
                continue
            people.append(
                {
                    "telegram_user_id": int(peer["telegram_user_id"]),
                    "username": peer.get("username"),
                    "name": peer.get("name") or "",
                    "peer_type": "user",
                }
            )
        return people


def _folder_person(peer) -> bool:
    if not isinstance(peer, dict) or peer.get("is_group"):
        return False
    if (peer.get("peer_type") or "user") != "user":
        return False
    return peer.get("telegram_user_id") is not None
