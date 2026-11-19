import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import threading
import time
import types
import urllib.parse

from app.db import DEFAULT_DB, resolve_session, utc_now
from app.telegram_gateway import (
    SESSION_HEALTH,
    GatewayError,
    TelegramGateway,
    _history_limit,
    _inbox_limit,
    _private_incoming,
    _private_outgoing,
    listening_event,
)

try:
    import magic as _real_magic  # type: ignore  # pragma: no cover
except Exception:
    _magic_shim = types.ModuleType("magic")
    _magic_shim.from_buffer = lambda buf, mime=True: "application/octet-stream"  # type: ignore[attr-defined]
    _magic_shim.from_file = (  # type: ignore[attr-defined]
        lambda path, mime=True: mimetypes.guess_type(str(path))[0]
        or "application/octet-stream"
    )
    sys.modules["magic"] = _magic_shim

try:
    import segno  # type: ignore
    import neonize  # type: ignore  # pragma: no cover
    import neonize.utils.ffmpeg as _neonize_ffmpeg  # type: ignore  # pragma: no cover
    from neonize._binder import ProxySettings  # type: ignore  # pragma: no cover
    from neonize.aioze.client import NewAClient, _ensure_jid  # type: ignore  # pragma: no cover
    from neonize.aioze.events import (  # type: ignore  # pragma: no cover
        ConnectedEv,
        LoggedOutEv,
        MessageEv,
        PairStatusEv,
    )
    from neonize.utils.enum import ChatPresence, ChatPresenceMedia  # type: ignore  # pragma: no cover

    _neonize_ffmpeg.check_ffmpeg_available = lambda: None
    HAS_NEONIZE = True
except Exception:
    segno = None
    neonize = None
    ProxySettings = None
    NewAClient = None
    _ensure_jid = None
    ConnectedEv = None
    LoggedOutEv = None
    MessageEv = None
    PairStatusEv = None
    ChatPresence = None
    ChatPresenceMedia = None
    HAS_NEONIZE = False

DEFAULT_WA_DAILY_LIMIT = 6
WA_MIN_COLD_INTERVAL_SECONDS = 480
_ALLOWED_PROXY_SCHEMES = ("socks5://", "socks5h://", "http://", "https://")


def _wa_monotonic_mid(raw_wa_id: str, ts_sec: float | None = None) -> int:
    """Build a deterministic 52-bit integer ID that stays monotonic with Unix timestamps."""
    sec = int(ts_sec) if (ts_sec is not None and ts_sec > 1_500_000_000) else int(time.time())
    h20 = int(hashlib.sha256(str(raw_wa_id).encode("utf-8")).hexdigest()[:5], 16)
    mid = ((sec & 0xFFFFFFFF) << 20) | (h20 & 0xFFFFF)
    return mid if mid > 0 else 1


def _render_real_qr_svg(qr_payload: str | bytes) -> str:
    raw = (
        qr_payload.decode("utf-8", "replace")
        if isinstance(qr_payload, bytes)
        else str(qr_payload or "")
    ).strip()
    if segno is None or not raw:
        raise GatewayError("Генератор QR (segno) недоступен или пустой QR-токен")
    buf = io.BytesIO()
    qr_obj = segno.make_qr(raw)
    qr_obj.save(buf, kind="svg", scale=5, border=2, dark="#111b21", light="#ffffff")
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _validate_wa_proxy_url(proxy_url: str | None) -> str | None:
    clean = str(proxy_url or "").strip()
    if not clean:
        return None
    low = clean.lower()
    if low == "auto" or not low.startswith(_ALLOWED_PROXY_SCHEMES):
        raise GatewayError(
            "Для WhatsApp укажите валидный SOCKS5/HTTP прокси вида socks5://user:pass@host:port"
        )
    parsed = urllib.parse.urlparse(clean)
    if not parsed.hostname or parsed.port is None:
        raise GatewayError(
            "Для WhatsApp укажите прокси с хостом и портом вида socks5://user:pass@host:port"
        )
    return clean


def _inspect_whatsmeow_db(session_path: str) -> dict | None:
    if not session_path or session_path.startswith("data/sessions/demo"):
        return None
    path = resolve_session(session_path)
    if not path.exists() or not path.is_file():
        return None
    try:
        if path.stat().st_size < 4096:
            return None
        with sqlite3.connect(path) as sconn:
            row = sconn.execute(
                "SELECT jid, push_name, business_name FROM whatsmeow_device WHERE jid IS NOT NULL AND jid != '' LIMIT 1"
            ).fetchone()
            if not row or not row[0]:
                return None
            raw_jid = str(row[0]).strip()
            user_part = raw_jid.split("@", 1)[0].split(":", 1)[0]
            digits = re.sub(r"\D", "", user_part)[-15:]
            if len(digits) < 7:
                return None
            push_name = str(row[1] or row[2] or "").strip()
            return {
                "raw_jid": raw_jid,
                "wa_jid": f"{digits}@s.whatsapp.net",
                "phone": f"+{digits}",
                "telegram_user_id": int(digits[-15:]),
                "name": push_name or f"WhatsApp +{digits}",
            }
    except Exception:
        return None


def _jid_to_str(jid_obj) -> str:
    if jid_obj is None:
        return ""
    if isinstance(jid_obj, str):
        return jid_obj.strip()
    user = str(getattr(jid_obj, "User", "") or "").split(":", 1)[0].strip()
    server = str(getattr(jid_obj, "Server", "") or "s.whatsapp.net").strip()
    if not user:
        return ""
    return f"{user}@{server}"


class WhatsAppGateway(TelegramGateway):
    _seq_lock = threading.Lock()
    _global_last_seq = 0

    def __init__(
        self,
        dialogs: list | None = None,
        history: dict | None = None,
        health: str | None = None,
        client_factory=None,
        db_path: Path | str | None = None,
    ):
        self.health = health
        self.dialogs = [dict(item) for item in (dialogs or [])]
        self.history = {}
        for peer, rows in (history or {}).items():
            self.history[int(peer)] = [dict(row) for row in rows]
        self._pending = {}
        self.sent = []
        self.presence_log = []
        self._db_path = Path(db_path) if db_path else DEFAULT_DB
        self._client_factory = client_factory
        now_ms = int(time.time() * 1000)
        db_max = 0
        try:
            if self._db_path.exists():
                with sqlite3.connect(self._db_path) as conn:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(telegram_message_id), 0) FROM messages"
                    ).fetchone()
                    if row and row[0] is not None:
                        db_max = int(row[0])
        except Exception:
            pass
        with WhatsAppGateway._seq_lock:
            floor = max(
                1_000_000_000, db_max, now_ms, WhatsAppGateway._global_last_seq
            )
            WhatsAppGateway._global_last_seq = floor
            self._message_seq = floor
        self._lock = threading.Lock()
        self._listeners = {}
        self._outgoing_listeners = {}
        self._backlog = {}
        self._outgoing_backlog = {}
        self._loop = None
        self._neonize_clients = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._qr_events = {}
        self._pair_events = {}
        self._proxies = {}
        self._active_proxies = {}
        self._verified_wa_numbers = {}
        self._wa_id_to_mid: dict[str, int] = {}

    def _get_session_lock(self, session_path: str) -> asyncio.Lock:
        with self._lock:
            lock = self._session_locks.get(session_path)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_path] = lock
            return lock

    def _next_message_seq(self, used: set[int] | None = None) -> int:
        now_ms = int(time.time() * 1000)
        with WhatsAppGateway._seq_lock:
            cand = max(
                self._message_seq + 1,
                now_ms,
                WhatsAppGateway._global_last_seq + 1,
                1_000_000_001,
            )
            while used and cand in used:
                cand += 1
            self._message_seq = cand
            WhatsAppGateway._global_last_seq = cand
            return cand

    def set_proxy(self, session_path: str, proxy_url: str | None) -> None:
        if not session_path:
            return
        validated = _validate_wa_proxy_url(proxy_url)
        if validated:
            self._proxies[session_path] = validated
        else:
            self._proxies.pop(session_path, None)

    def clear_session(self, session_path: str) -> None:
        if not session_path:
            return
        self._pending.pop(session_path, None)
        self._proxies.pop(session_path, None)
        self._active_proxies.pop(session_path, None)

    async def apply_proxy(self, session_path: str, proxy_url: str | None) -> None:
        if not session_path:
            return
        validated = _validate_wa_proxy_url(proxy_url)
        if validated:
            self._proxies[session_path] = validated
        else:
            self._proxies.pop(session_path, None)
        existing = self._neonize_clients.get(session_path)
        if existing is not None and hasattr(existing, "set_proxy_address"):
            try:
                await existing.set_proxy_address(validated)
                self._active_proxies[session_path] = validated
            except Exception as exc:
                raise GatewayError(
                    f"Не удалось применить прокси к сессии WhatsApp: {exc}"
                ) from exc

    def _resolve_proxy_url(self, session_path: str) -> str | None:
        try:
            if self._db_path.exists():
                with sqlite3.connect(self._db_path) as conn:
                    row = conn.execute(
                        "SELECT proxy_url FROM accounts WHERE session_path = ? LIMIT 1",
                        (session_path,),
                    ).fetchone()
                    if row is not None:
                        validated = _validate_wa_proxy_url(str(row[0] or ""))
                        if validated:
                            self._proxies[session_path] = validated
                        else:
                            self._proxies.pop(session_path, None)
                        return validated
        except GatewayError:
            raise
        except Exception:
            pass
        if session_path in self._proxies:
            return _validate_wa_proxy_url(self._proxies[session_path])
        return None

    def _sync_db_account_state(
        self,
        session_path: str,
        *,
        qr_code: str | None = None,
        pairing_code: str | None = None,
        connected_info: dict | None = None,
        unauthorized_reason: str | None = None,
    ) -> None:
        try:
            if not self._db_path.exists():
                return
            with sqlite3.connect(self._db_path) as conn:
                if unauthorized_reason:
                    conn.execute(
                        """
                        UPDATE accounts
                        SET health = 'unauthorized',
                            last_error = ?,
                            updated_at = ?
                        WHERE session_path = ?
                        """,
                        (unauthorized_reason, utc_now(), session_path),
                    )
                elif connected_info:
                    conn.execute(
                        """
                        UPDATE accounts
                        SET status = 'connected',
                            health = 'active',
                            last_error = NULL,
                            phone = COALESCE(?, phone),
                            telegram_user_id = COALESCE(?, telegram_user_id),
                            telegram_name = COALESCE(?, telegram_name),
                            wa_jid = COALESCE(?, wa_jid),
                            pairing_code = NULL,
                            qr_code = NULL,
                            updated_at = ?
                        WHERE session_path = ?
                        """,
                        (
                            connected_info.get("phone"),
                            connected_info.get("telegram_user_id"),
                            connected_info.get("name"),
                            connected_info.get("wa_jid"),
                            utc_now(),
                            session_path,
                        ),
                    )
                else:
                    if qr_code:
                        conn.execute(
                            "UPDATE accounts SET qr_code = ?, updated_at = ? WHERE session_path = ?",
                            (qr_code, utc_now(), session_path),
                        )
                    if pairing_code:
                        conn.execute(
                            "UPDATE accounts SET pairing_code = ?, updated_at = ? WHERE session_path = ?",
                            (pairing_code, utc_now(), session_path),
                        )
                conn.commit()
        except Exception:
            pass

    async def _close(self, session_path: str) -> None:
        client = self._neonize_clients.pop(session_path, None)
        self._pending.pop(session_path, None)
        self._proxies.pop(session_path, None)
        self._active_proxies.pop(session_path, None)
        self._qr_events.pop(session_path, None)
        self._pair_events.pop(session_path, None)
        if client is not None and hasattr(client, "stop"):
            try:
                await client.stop()
            except Exception:
                pass

    async def close_all(self) -> None:
        for sess in list(self._neonize_clients.keys()):
            await self._close(sess)

    async def _ensure_neonize_client(
        self, session_path: str, fresh_qr: bool = False
    ):
        async with self._get_session_lock(session_path):
            return await self._ensure_neonize_client_locked(
                session_path, fresh_qr=fresh_qr
            )

    async def _ensure_neonize_client_locked(
        self, session_path: str, fresh_qr: bool = False
    ):
        if self._client_factory is None and not HAS_NEONIZE:
            raise GatewayError("Движок WhatsApp (neonize) не установлен")

        proxy_url = self._resolve_proxy_url(session_path)
        existing = self._neonize_clients.get(session_path)
        if existing is not None and not fresh_qr:
            if self._active_proxies.get(session_path) != proxy_url and hasattr(
                existing, "set_proxy_address"
            ):
                try:
                    await existing.set_proxy_address(proxy_url)
                    self._active_proxies[session_path] = proxy_url
                except Exception as exc:
                    raise GatewayError(
                        f"Не удалось применить прокси к сессии WhatsApp: {exc}"
                    ) from exc
            return existing
        if existing is not None and fresh_qr:
            await self._close(session_path)

        abs_path = resolve_session(session_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        if fresh_qr and abs_path.exists() and _inspect_whatsmeow_db(session_path) is None:
            for target in (
                abs_path,
                abs_path.with_name(abs_path.name + "-wal"),
                abs_path.with_name(abs_path.name + "-shm"),
                abs_path.with_name(abs_path.name + "-journal"),
            ):
                try:
                    if target.exists():
                        target.unlink()
                except Exception:
                    pass

        qr_ev = asyncio.Event()
        pair_ev = asyncio.Event()
        self._qr_events[session_path] = qr_ev
        self._pair_events[session_path] = pair_ev

        factory = self._client_factory or NewAClient
        client = factory(str(abs_path))

        async def _on_qr(_client, data_qr: bytes):
            qr_str = (
                data_qr.decode("utf-8", "replace")
                if isinstance(data_qr, bytes)
                else str(data_qr or "")
            ).strip()
            if not qr_str:
                return
            try:
                svg_uri = _render_real_qr_svg(qr_str)
            except GatewayError:
                return
            p = self._pending.setdefault(session_path, {})
            p["qr_code"] = svg_uri
            p["qr_raw"] = qr_str
            qr_ev.set()
            self._sync_db_account_state(session_path, qr_code=svg_uri)

        if hasattr(client, "qr"):
            client.qr(_on_qr)

        if hasattr(client, "event") and PairStatusEv is not None:

            @client.event(PairStatusEv)
            async def _on_pair(_client, ev):
                try:
                    user_part = str(getattr(getattr(ev, "ID", None), "User", "") or "")
                    digits = re.sub(r"\D", "", user_part)[-15:]
                    bname = str(getattr(ev, "BusinessName", "") or "").strip()
                    info = _inspect_whatsmeow_db(session_path) or (
                        {
                            "wa_jid": f"{digits}@s.whatsapp.net",
                            "phone": f"+{digits}",
                            "telegram_user_id": int(digits[-15:]),
                            "name": bname or f"WhatsApp +{digits}",
                        }
                        if len(digits) >= 7
                        else None
                    )
                    if info:
                        p = self._pending.setdefault(session_path, {})
                        p["state"] = "paired_ready"
                        p["wa_jid"] = info.get("wa_jid")
                        p["phone"] = info.get("phone") or p.get("phone")
                        pair_ev.set()
                        if p.get("auth_mode") == "qr":
                            self._sync_db_account_state(
                                session_path, pairing_code="QR-READY"
                            )
                except Exception:
                    pair_ev.set()

        if hasattr(client, "event") and ConnectedEv is not None:

            @client.event(ConnectedEv)
            async def _on_connected(_client, _ev):
                info = _inspect_whatsmeow_db(session_path)
                p = self._pending.setdefault(session_path, {})
                if info:
                    p["state"] = "paired_ready"
                    p["wa_jid"] = info.get("wa_jid")
                    p["phone"] = info.get("phone") or p.get("phone")
                    if p.get("auth_mode") == "qr":
                        self._sync_db_account_state(
                            session_path, pairing_code="QR-READY"
                        )
                pair_ev.set()

        if hasattr(client, "event") and LoggedOutEv is not None:

            @client.event(LoggedOutEv)
            async def _on_logged_out(_client, _ev):
                p = self._pending.setdefault(session_path, {})
                p["state"] = "unauthorized"
                self._sync_db_account_state(
                    session_path,
                    unauthorized_reason="Сессия WhatsApp разлогинена с телефона",
                )

        if hasattr(client, "event") and MessageEv is not None:

            @client.event(MessageEv)
            async def _on_message(_client, ev):
                try:
                    info_proto = getattr(ev, "Info", None)
                    src = getattr(info_proto, "MessageSource", None)
                    if src is None or getattr(src, "IsGroup", False):
                        return
                    is_from_me = bool(getattr(src, "IsFromMe", False))
                    if is_from_me:
                        with self._lock:
                            has_out_listener = session_path in self._outgoing_listeners
                        if not has_out_listener:
                            return
                        peer_jid = getattr(src, "Chat", None) or getattr(src, "Sender", None)
                    else:
                        peer_jid = getattr(src, "Sender", None) or getattr(src, "Chat", None)
                    if str(getattr(peer_jid, "Server", "") or "").lower() in (
                        "g.us",
                        "newsletter",
                        "lid",
                    ):
                        return
                    raw_jid_str = str(peer_jid or "").lower()
                    if any(tag in raw_jid_str for tag in ("@g.us", "@newsletter", "@lid")):
                        return
                    sender_user = str(getattr(peer_jid, "User", "") or "").split(":", 1)[0]
                    digits = re.sub(r"\D", "", sender_user)[-15:]
                    if len(digits) < 7:
                        return
                    if not is_from_me:
                        own_user = str(
                            getattr(
                                getattr(getattr(_client, "me", None), "JID", None),
                                "User",
                                "",
                            )
                            or ""
                        ).split(":", 1)[0]
                        own_digits = re.sub(r"\D", "", own_user)[-15:]
                        if own_digits and digits == own_digits:
                            return
                    msg_proto = getattr(ev, "Message", None)
                    text = (
                        getattr(msg_proto, "conversation", "")
                        or getattr(getattr(msg_proto, "extendedTextMessage", None), "text", "")
                        or ""
                    ).strip()
                    if not text:
                        return
                    peer_id = int(digits[-15:])
                    if is_from_me:
                        push_name = f"WA +{digits}"
                    else:
                        push_name = str(
                            getattr(info_proto, "Pushname", "") or f"WA +{digits}"
                        ).strip()
                    raw_wa_id = str(getattr(info_proto, "ID", "") or "").strip()
                    raw_ts = getattr(info_proto, "Timestamp", None)
                    parsed_ts: float | None = None
                    sent_at_val = utc_now()
                    if raw_ts is not None:
                        try:
                            ts_num = float(raw_ts)
                            if ts_num > 1e12:
                                ts_num /= 1000.0
                            if ts_num > 0:
                                parsed_ts = ts_num
                                sent_at_val = datetime.fromtimestamp(
                                    ts_num, tz=timezone.utc
                                ).strftime("%Y-%m-%dT%H:%M:%S")
                        except (TypeError, ValueError, OSError, OverflowError):
                            pass
                    if raw_wa_id:
                        cached_mid = self._wa_id_to_mid.get(raw_wa_id)
                        if cached_mid is not None:
                            msg_mid = cached_mid
                        else:
                            msg_mid = _wa_monotonic_mid(raw_wa_id, ts_sec=parsed_ts)
                            self._wa_id_to_mid[raw_wa_id] = msg_mid
                    else:
                        msg_mid = self._next_message_seq()
                    payload = {
                        "telegram_user_id": peer_id,
                        "username": None,
                        "name": push_name,
                        "phone": f"+{digits}",
                        "wa_jid": f"{digits}@s.whatsapp.net",
                        "telegram_message_id": msg_mid,
                        "external_message_id": raw_wa_id or f"WA{msg_mid}",
                        "text": text,
                        "sent_at": sent_at_val,
                        "direction": "out" if is_from_me else "in",
                        "peer_type": "user",
                    }
                    self.history.setdefault(peer_id, []).append(dict(payload))
                    if is_from_me:
                        self.push_outgoing(session_path, payload)
                    else:
                        self.push_private(session_path, payload)
                except Exception:
                    pass

        proxy_settings = (
            ProxySettings(proxy_address=proxy_url)
            if (proxy_url and ProxySettings is not None)
            else None
        )
        try:
            await client.connect(proxy_settings)
        except Exception as exc:
            if hasattr(client, "stop"):
                try:
                    await client.stop()
                except Exception:
                    pass
            raise GatewayError(
                f"Не удалось подключиться к WhatsApp WebSocket: {exc}"
            ) from exc

        self._neonize_clients[session_path] = client
        self._active_proxies[session_path] = proxy_url
        return client

    async def request_code(
        self, phone: str, session_path: str, auth_mode: str = "code"
    ) -> str:
        mode = "qr" if str(auth_mode or "").strip().lower() == "qr" else "code"
        clean_phone = (phone or "").strip()
        digits = re.sub(r"\D", "", clean_phone)

        existing_info = _inspect_whatsmeow_db(session_path)
        if existing_info:
            self._pending[session_path] = {
                "phone": existing_info["phone"],
                "hash": f"wa:connected:{existing_info['wa_jid']}",
                "pairing_code": None,
                "qr_code": None,
                "auth_mode": mode,
                "state": "connected",
                "wa_jid": existing_info["wa_jid"],
            }
            return f"wa:connected:{existing_info['wa_jid']}"

        if mode == "code" and not (8 <= len(digits) <= 15):
            raise GatewayError(
                "Укажите номер WhatsApp в международном формате (+...)"
            )

        client = await self._ensure_neonize_client(session_path, fresh_qr=True)
        qr_ev = self._qr_events.get(session_path)
        p = self._pending.setdefault(session_path, {})

        pairing_code: str | None = None
        qr_code: str | None = p.get("qr_code")
        qr_raw: str = str(p.get("qr_raw") or "")

        if mode == "qr":
            if qr_ev is not None and not qr_ev.is_set():
                try:
                    await asyncio.wait_for(qr_ev.wait(), timeout=12.0)
                except asyncio.TimeoutError:
                    pass
            qr_code = p.get("qr_code")
            qr_raw = str(p.get("qr_raw") or "")
            if not qr_code or len(qr_raw) < 20:
                await self._close(session_path)
                raise GatewayError(
                    "Не удалось получить реальный QR-код от серверов WhatsApp"
                )
        else:
            if qr_ev is not None and not qr_ev.is_set():
                try:
                    await asyncio.wait_for(qr_ev.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass
            try:
                raw_code = await asyncio.wait_for(
                    client.PairPhone(digits, show_push_notification=True),
                    timeout=12.0,
                )
            except Exception as exc:
                await self._close(session_path)
                raise GatewayError(
                    f"Не удалось запросить реальный код сопряжения WhatsApp для +{digits}: {exc}"
                ) from exc
            alnum = re.sub(r"[^A-Za-z0-9]", "", str(raw_code or "")).upper()
            if len(alnum) < 6:
                await self._close(session_path)
                raise GatewayError(
                    f"WhatsApp не вернул код сопряжения для +{digits}"
                )
            pairing_code = (
                f"{alnum[:4]}-{alnum[4:]}" if len(alnum) == 8 else alnum
            )
            qr_code = p.get("qr_code")

        seed_token = pairing_code or qr_raw
        phone_code_hash = (
            f"wa:{mode}:{hashlib.sha256(seed_token.encode('utf-8')).hexdigest()[:16]}"
        )
        p.update(
            {
                "phone": f"+{digits}" if digits else clean_phone,
                "hash": phone_code_hash,
                "pairing_code": pairing_code,
                "qr_code": qr_code,
                "auth_mode": mode,
                "state": "pending_code",
            }
        )
        self._sync_db_account_state(
            session_path, qr_code=qr_code, pairing_code=pairing_code
        )
        return phone_code_hash

    async def confirm_code(
        self, phone: str, code: str, phone_code_hash: str, session_path: str
    ) -> dict:
        if not (code or "").strip():
            raise GatewayError("Укажите код сопряжения или отсканируйте QR")

        info = _inspect_whatsmeow_db(session_path)
        if not info:
            pair_ev = self._pair_events.get(session_path)
            if pair_ev is not None and not pair_ev.is_set():
                try:
                    await asyncio.wait_for(pair_ev.wait(), timeout=4.0)
                except asyncio.TimeoutError:
                    pass
            info = _inspect_whatsmeow_db(session_path)

        if not info:
            raise GatewayError(
                "Устройство ещё не привязано в WhatsApp на телефоне (откройте WhatsApp → Связанные устройства и завершите привязку)"
            )

        p = self._pending.setdefault(session_path, {})
        p["state"] = "connected"
        p["wa_jid"] = info["wa_jid"]
        p["phone"] = info["phone"]
        self._sync_db_account_state(session_path, connected_info=info)
        return {
            "status": "connected",
            "phone": info["phone"],
            "telegram_user_id": info["telegram_user_id"],
            "telegram_username": None,
            "telegram_name": info["name"],
            "wa_jid": info["wa_jid"],
        }

    async def confirm_password(self, password: str, session_path: str) -> dict:
        raise GatewayError(
            "WhatsApp Multi-Device использует привязку через QR или 8-значный код"
        )

    async def session_health(self, session_path: str) -> dict:
        forced = self.health
        if forced in SESSION_HEALTH:
            if forced == "active":
                return {"health": "active", "last_error": None}
            text = {
                "offline": "Нет связи с WhatsApp",
                "unauthorized": "Сессия WhatsApp недействительна",
                "banned": "Аккаунт WhatsApp заблокирован",
                "frozen": "Аккаунт WhatsApp ограничен",
            }[forced]
            return {"health": forced, "last_error": text}

        pending = self._pending.get(session_path) or {}
        if pending.get("state") == "unauthorized":
            return {
                "health": "unauthorized",
                "last_error": "Сессия WhatsApp разлогинена с телефона",
            }

        if _inspect_whatsmeow_db(session_path) is not None:
            return {"health": "active", "last_error": None}

        if pending.get("state") == "pending_code":
            return {
                "health": "unauthorized",
                "last_error": "Сессия WhatsApp ожидает сопряжения",
            }

        return {
            "health": "unauthorized",
            "last_error": "Сессия WhatsApp не авторизована",
        }

    async def load_inbox(self, session_path: str, limit: int = 30) -> list:
        cap = _inbox_limit(limit)
        found = []
        seen_peers: set[int] = set()
        if _inspect_whatsmeow_db(session_path) is not None:
            path = resolve_session(session_path)
            try:
                with sqlite3.connect(path) as sconn:
                    crows = sconn.execute(
                        """
                        SELECT their_jid, full_name, first_name, push_name, business_name
                        FROM whatsmeow_contacts
                        WHERE their_jid LIKE '%@s.whatsapp.net'
                        LIMIT ?
                        """,
                        (cap,),
                    ).fetchall()
                    for crow in crows:
                        t_jid = str(crow[0] or "").strip()
                        u_part = t_jid.split("@", 1)[0].split(":", 1)[0]
                        digits = re.sub(r"\D", "", u_part)[-15:]
                        if len(digits) < 7:
                            continue
                        peer_id = int(digits[-15:])
                        if peer_id in seen_peers:
                            continue
                        seen_peers.add(peer_id)
                        cname = str(
                            crow[1] or crow[2] or crow[3] or crow[4] or f"WA +{digits}"
                        ).strip()
                        hist = self.history.get(peer_id) or []
                        last_msg = hist[-1] if hist else None
                        found.append(
                            {
                                "telegram_user_id": peer_id,
                                "username": None,
                                "name": cname,
                                "peer_type": "user",
                                "phone": f"+{digits}",
                                "wa_jid": f"{digits}@s.whatsapp.net",
                                "last_message_preview": (
                                    (last_msg.get("text") or "")[:120]
                                    if last_msg
                                    else None
                                ),
                                "last_message_at": (
                                    last_msg.get("sent_at") if last_msg else None
                                ),
                                "last_from_peer": (
                                    (1 if last_msg.get("direction") == "in" else 0)
                                    if last_msg
                                    else None
                                ),
                            }
                        )
            except Exception:
                pass
        for item in self.dialogs:
            if not isinstance(item, dict) or item.get("is_group"):
                continue
            jid = str(item.get("wa_jid") or "")
            if jid.endswith("@g.us"):
                continue
            if (item.get("peer_type") or "user") != "user":
                continue
            peer_id = item.get("telegram_user_id")
            if peer_id is None and item.get("phone"):
                p_digits = re.sub(r"\D", "", str(item.get("phone")))[-15:]
                if len(p_digits) >= 7:
                    peer_id = int(p_digits)
            if peer_id is None:
                continue
            digits = re.sub(r"\D", "", str(item.get("phone") or jid or peer_id))[-15:]
            row = {
                "telegram_user_id": (
                    int(digits[-15:])
                    if len(digits) >= 7 and not (900000 <= int(peer_id) < 1000000)
                    else int(peer_id)
                ),
                "username": item.get("username"),
                "name": item.get("name") or (f"WA +{digits}" if digits else ""),
                "peer_type": "user",
                "phone": f"+{digits}" if len(digits) >= 7 else item.get("phone"),
                "wa_jid": jid or (f"{digits}@s.whatsapp.net" if digits else None),
            }
            if "unread_count" in item and item.get("unread_count") is not None:
                row["unread_count"] = max(0, int(item["unread_count"]))
            if item.get("last_message_preview") is not None:
                row["last_message_preview"] = str(item["last_message_preview"])[:120]
            if item.get("last_message_at") is not None:
                row["last_message_at"] = str(item["last_message_at"])
            if "last_from_peer" in item and item.get("last_from_peer") is not None:
                row["last_from_peer"] = 1 if item["last_from_peer"] else 0
            found.append(row)
            if len(found) >= cap:
                break
        return found

    async def check_numbers(
        self, session_path: str, phones: list[str]
    ) -> list[dict]:
        if _inspect_whatsmeow_db(session_path) is None:
            raise GatewayError("Сессия WhatsApp не авторизована для проверки номеров")

        clean_digits: list[str] = []
        for raw in phones or []:
            d = re.sub(r"\D", "", str(raw or ""))[-15:]
            if len(d) >= 7 and d not in clean_digits:
                clean_digits.append(d)
        if not clean_digits:
            return []

        client = await self._ensure_neonize_client(session_path, fresh_qr=False)
        try:
            resp_seq = await asyncio.wait_for(
                client.is_on_whatsapp(*[f"+{d}" for d in clean_digits]),
                timeout=12.0,
            )
        except Exception as exc:
            raise GatewayError(
                f"Не удалось проверить номера в WhatsApp (IsOnWhatsApp): {exc}"
            ) from exc

        by_digits: dict[str, dict] = {}
        for item in resp_seq or ():
            q_digits = re.sub(r"\D", "", str(getattr(item, "Query", "") or ""))[-15:]
            is_in = bool(getattr(item, "IsIn", False))
            jid_str = _jid_to_str(getattr(item, "JID", None))
            jid_digits = re.sub(r"\D", "", jid_str.split("@", 1)[0])[-15:]
            key_d = q_digits or jid_digits
            if not key_d:
                continue
            resolved_jid = jid_str or f"{key_d}@s.whatsapp.net"
            if is_in:
                self._verified_wa_numbers[key_d] = resolved_jid
            by_digits[key_d] = {
                "phone": f"+{key_d}",
                "found": is_in,
                "wa_jid": resolved_jid if is_in else None,
                "telegram_user_id": int(key_d[-15:]),
                "username": None,
                "name": f"WA +{key_d}",
            }

        results = []
        for d in clean_digits:
            if d in by_digits:
                results.append(by_digits[d])
            else:
                results.append(
                    {
                        "phone": f"+{d}",
                        "found": False,
                        "wa_jid": None,
                        "telegram_user_id": int(d[-15:]),
                        "username": None,
                        "name": f"WA +{d}",
                    }
                )
        return results

    async def resolve_phones(
        self, session_path: str, phones: list[str]
    ) -> list[dict]:
        return await self.check_numbers(session_path, phones)

    async def search_dialogs(
        self, session_path: str, query: str, limit: int = 30
    ) -> list:
        q = (query or "").strip()
        needle = q.casefold()
        matched = []
        for item in self.dialogs:
            if item.get("is_group") or str(item.get("wa_jid") or "").endswith("@g.us"):
                continue
            blob = " ".join(
                (
                    str(item.get("name") or ""),
                    str(item.get("username") or ""),
                    str(item.get("phone") or ""),
                    str(item.get("wa_jid") or ""),
                    str(item.get("telegram_user_id") or ""),
                )
            ).casefold()
            if needle and needle not in blob:
                continue
            matched.append(dict(item))
            if len(matched) >= limit:
                break

        digits = re.sub(r"\D", "", q)[-15:]
        if (
            len(digits) >= 7
            and not matched
            and _inspect_whatsmeow_db(session_path) is not None
        ):
            checked = await self.check_numbers(session_path, [f"+{digits}"])
            for item in checked:
                if item.get("found"):
                    matched.append(
                        {
                            "telegram_user_id": item["telegram_user_id"],
                            "username": None,
                            "name": item["name"],
                            "phone": item["phone"],
                            "wa_jid": item["wa_jid"],
                            "peer_type": "user",
                        }
                    )
        return matched[:limit]

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to=None,
        simulate_typing: bool = True,
        image_path: str | None = None,
    ) -> dict:
        del image_path
        body = (text or "").strip()
        if not body:
            raise GatewayError("Напишите сообщение")

        if _inspect_whatsmeow_db(session_path) is None:
            raise GatewayError(
                "Сессия WhatsApp не авторизована: отправка невозможна"
            )

        target_digits = re.sub(r"\D", "", str(peer_id or ""))[-15:]
        if len(target_digits) < 7:
            raise GatewayError("Некорректный номер получателя WhatsApp")

        peer_key = int(target_digits[-15:])
        client = await self._ensure_neonize_client(session_path, fresh_qr=False)

        if target_digits not in self._verified_wa_numbers:
            checked = await self.check_numbers(session_path, [f"+{target_digits}"])
            if not checked or not checked[0].get("found"):
                raise GatewayError(
                    f"Номер +{target_digits} не зарегистрирован в WhatsApp"
                )

        target_jid_str = (
            self._verified_wa_numbers.get(target_digits)
            or f"{target_digits}@s.whatsapp.net"
        )

        if simulate_typing:
            self.presence_log.append(
                {
                    "session_path": session_path,
                    "peer_id": peer_key,
                    "presence": "composing",
                }
            )
            jid_for_presence = (
                _ensure_jid(target_jid_str)
                if _ensure_jid is not None
                else target_jid_str
            )
            if (
                hasattr(client, "send_chat_presence")
                and ChatPresence is not None
                and ChatPresenceMedia is not None
            ):
                try:
                    await client.send_chat_presence(
                        jid_for_presence,
                        ChatPresence.CHAT_PRESENCE_COMPOSING,
                        ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
                    )
                except Exception:
                    pass
            delay_s = (
                0.01
                if os.environ.get("CRMTG_FORCE_NO_JITTER") == "1"
                else min(5.5, max(1.2, len(body) * random.uniform(0.032, 0.048)))
            )
            await asyncio.sleep(delay_s)
            if (
                hasattr(client, "send_chat_presence")
                and ChatPresence is not None
                and ChatPresenceMedia is not None
            ):
                try:
                    await client.send_chat_presence(
                        jid_for_presence,
                        ChatPresence.CHAT_PRESENCE_PAUSED,
                        ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
                    )
                except Exception:
                    pass

        try:
            send_resp = await client.send_message(target_jid_str, body)
        except Exception as exc:
            raise GatewayError(f"Ошибка отправки в WhatsApp: {exc}") from exc

        external_message_id = str(getattr(send_resp, "ID", "") or "").strip()
        if not external_message_id:
            raise GatewayError(
                "WhatsApp не подтвердил ID отправленного сообщения"
            )

        used = {
            int(row.get("telegram_message_id") or 0)
            for row in self.history.get(peer_key, [])
        }
        seq_id = _wa_monotonic_mid(external_message_id, ts_sec=time.time())
        while used and seq_id in used:
            seq_id += 1
        self._wa_id_to_mid[external_message_id] = seq_id

        sent_item = {
            "peer_id": peer_key,
            "text": body,
            "reply_to": reply_to,
            "session_path": session_path,
            "external_message_id": external_message_id,
            "presence": "composing" if simulate_typing else "available",
        }
        self.sent.append(sent_item)

        record = {
            "telegram_message_id": seq_id,
            "external_message_id": external_message_id,
            "text": body,
            "sent_at": utc_now(),
            "direction": "out",
            "reply_to_telegram_message_id": int(reply_to) if reply_to else None,
            "presence": "composing" if simulate_typing else "available",
        }
        self.history.setdefault(peer_key, []).append(dict(record))
        return record

    async def fetch_messages(
        self, session_path: str, peer_id: int, limit: int = 50
    ) -> list:
        del session_path
        cap = _history_limit(limit)
        rows = self.history.get(int(peer_id), [])
        return [dict(row) for row in rows[-cap:]]

    @staticmethod
    async def _invoke_cb(listener, item: dict) -> None:
        res = listener(item)
        if asyncio.iscoroutine(res):
            await res

    def _dispatch_cb(self, listener, loop, item: dict) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self._invoke_cb(listener, item))
            return
        asyncio.run_coroutine_threadsafe(
            self._invoke_cb(listener, item), loop
        ).result(timeout=5)

    def push_private(self, session_path: str, message: dict) -> None:
        if not _private_incoming(message):
            return
        jid = str(message.get("wa_jid") or "")
        if jid.endswith("@g.us"):
            return
        item = dict(message)
        with self._lock:
            listener = self._listeners.get(session_path)
            loop = self._loop
            if listener is None or loop is None:
                self._backlog.setdefault(session_path, []).append(item)
                return
        self._dispatch_cb(listener, loop, item)

    def push_outgoing(self, session_path: str, message: dict) -> None:
        if not _private_outgoing(message):
            return
        jid = str(message.get("wa_jid") or "")
        if jid.endswith("@g.us"):
            return
        item = dict(message)
        with self._lock:
            listener = self._outgoing_listeners.get(session_path)
            loop = self._loop
            if listener is None or loop is None:
                self._outgoing_backlog.setdefault(session_path, []).append(item)
                return
        self._dispatch_cb(listener, loop, item)

    async def watch_private(
        self, session_path: str, on_message, on_read=None, on_outgoing=None
    ) -> None:
        del on_read
        self._loop = asyncio.get_running_loop()
        # Register listeners BEFORE connecting neonize so early sync events are never dropped
        with self._lock:
            self._listeners[session_path] = on_message
            if on_outgoing is not None:
                self._outgoing_listeners[session_path] = on_outgoing
            batch = self._backlog.pop(session_path, [])
            out_batch = (
                self._outgoing_backlog.pop(session_path, [])
                if on_outgoing is not None
                else []
            )
        if _inspect_whatsmeow_db(session_path) is not None:
            try:
                await self._ensure_neonize_client(session_path, fresh_qr=False)
            except Exception:
                pass
        try:
            for item in batch:
                await self._invoke_cb(on_message, dict(item))
            if on_outgoing is not None:
                for o_item in out_batch:
                    await self._invoke_cb(on_outgoing, dict(o_item))
            listening_event(self, session_path).set()
            while True:
                client = self._neonize_clients.get(session_path)
                if client is not None and getattr(client, "stopped", False) is True:
                    return
                await asyncio.sleep(5.0)
        finally:
            with self._lock:
                if self._listeners.get(session_path) is on_message:
                    self._listeners.pop(session_path, None)
                if (
                    on_outgoing is not None
                    and self._outgoing_listeners.get(session_path) is on_outgoing
                ):
                    self._outgoing_listeners.pop(session_path, None)
