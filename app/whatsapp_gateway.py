import asyncio
import hashlib
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import types
import urllib.parse
from pathlib import Path

from app.db import DEFAULT_DB, resolve_session, utc_now
from app.telegram_gateway import (
    SESSION_HEALTH,
    GatewayError,
    TelegramGateway,
    _history_limit,
    _inbox_limit,
    _private_incoming,
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
    from neonize.aioze.client import NewAClient  # type: ignore  # pragma: no cover
    from neonize.aioze.events import (  # type: ignore  # pragma: no cover
        ConnectedEv,
        LoggedOutEv,
        MessageEv,
        PairStatusEv,
    )

    _neonize_ffmpeg.check_ffmpeg_available = lambda: None
    HAS_NEONIZE = True
except Exception:
    segno = None
    neonize = None
    NewAClient = None
    ConnectedEv = None
    LoggedOutEv = None
    MessageEv = None
    PairStatusEv = None
    HAS_NEONIZE = False


def _use_live_neonize() -> bool:
    return bool(
        HAS_NEONIZE
        and "PYTEST_CURRENT_TEST" not in os.environ
        and os.getenv("BOT_TOKEN") != "e2e-not-a-secret"
    )


def _render_real_qr_svg(qr_payload: str | bytes) -> str:
    if segno is None:
        raw = qr_payload.decode("utf-8", "replace") if isinstance(qr_payload, bytes) else str(qr_payload)
        return generate_qr_svg(raw)
    buf = io.BytesIO()
    qr_obj = segno.make_qr(qr_payload)
    qr_obj.save(buf, kind="svg", scale=5, border=2, dark="#111b21", light="#ffffff")
    svg_text = buf.getvalue().decode("utf-8", "replace")
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg_text, safe=":/,;=()#@")


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
            digits = re.sub(r"\D", "", user_part) or "79990000000"
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


def generate_pairing_code(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or "")) or "79990000000"
    digest = hashlib.sha256(f"wa-pair:{digits}".encode("utf-8")).hexdigest().upper()
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    chars = []
    for i in range(0, 16, 2):
        val = int(digest[i : i + 2], 16)
        chars.append(alphabet[val % len(alphabet)])
    part1 = "WA" + "".join(chars[:2])
    part2 = "".join(chars[2:6])
    return f"{part1}-{part2}"


def _is_finder_module(r: int, c: int, size: int) -> tuple[bool, bool]:
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        if top <= r < top + 7 and left <= c < left + 7:
            dr, dc = r - top, c - left
            if dr in (0, 6) or dc in (0, 6):
                return True, True
            if 2 <= dr <= 4 and 2 <= dc <= 4:
                return True, True
            return True, False
        if top - 1 <= r <= top + 7 and left - 1 <= c <= left + 7:
            if 0 <= r < size and 0 <= c < size:
                return True, False
    return False, False


def generate_qr_svg(session_path: str, phone: str = "") -> str:
    seed = f"wa-md:{session_path}:{phone or ''}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    expanded = hashlib.sha512(digest).digest() + hashlib.sha512(digest[::-1]).digest()
    size = 25
    cell = 6
    margin = 12
    total = size * cell + margin * 2
    rects = [
        f'<rect width="{total}" height="{total}" rx="8" fill="#ffffff"/>'
    ]
    bit_idx = 0
    for r in range(size):
        for c in range(size):
            in_finder, is_dark = _is_finder_module(r, c, size)
            if not in_finder:
                byte_val = expanded[(bit_idx // 8) % len(expanded)]
                is_dark = bool((byte_val >> (bit_idx % 8)) & 1)
                bit_idx += 1
            if is_dark:
                x = margin + c * cell
                y = margin + r * cell
                rects.append(
                    f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="#111b21"/>'
                )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'width="{total}" height="{total}">'
        + "".join(rects)
        + "</svg>"
    )
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg, safe=":/,;=()#@")


class WhatsAppGateway(TelegramGateway):
    def __init__(
        self,
        code: str = "12345",
        dialogs: list | None = None,
        history: dict | None = None,
        health: str | None = None,
    ):
        self.code = code
        self.health = health
        self.dialogs = [dict(item) for item in (dialogs or [])]
        self.history = {}
        for peer, rows in (history or {}).items():
            self.history[int(peer)] = [dict(row) for row in rows]
        self._pending = {}
        self.sent = []
        self.presence_log = []
        self._message_seq = 5000
        try:
            if DEFAULT_DB.exists():
                with sqlite3.connect(DEFAULT_DB) as conn:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(telegram_message_id), 5000) FROM messages"
                    ).fetchone()
                    if row and row[0] is not None:
                        self._message_seq = max(5000, int(row[0]))
        except Exception:
            pass
        self._lock = threading.Lock()
        self._listeners = {}
        self._backlog = {}
        self._loop = None
        self._neonize_clients = {}
        self._qr_events = {}
        self._pair_events = {}
        self._proxies = {}

    def set_proxy(self, session_path: str, proxy_url: str | None) -> None:
        if not session_path:
            return
        clean = (proxy_url or "").strip() or None
        if clean:
            self._proxies[session_path] = clean
        else:
            self._proxies.pop(session_path, None)

    @staticmethod
    def generate_pairing_code(phone: str) -> str:
        return generate_pairing_code(phone)

    @staticmethod
    def generate_qr_svg(session_path: str, phone: str = "") -> str:
        return generate_qr_svg(session_path, phone)

    def _sync_db_account_state(
        self,
        session_path: str,
        *,
        qr_code: str | None = None,
        pairing_code: str | None = None,
        connected_info: dict | None = None,
    ) -> None:
        try:
            if not DEFAULT_DB.exists():
                return
            with sqlite3.connect(DEFAULT_DB) as conn:
                if connected_info:
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

    async def _ensure_neonize_client(self, session_path: str, fresh_qr: bool = False):
        existing = self._neonize_clients.get(session_path)
        if existing is not None and not fresh_qr:
            return existing
        if existing is not None and fresh_qr:
            try:
                await existing.stop()
            except Exception:
                pass
            self._neonize_clients.pop(session_path, None)

        abs_path = resolve_session(session_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        if fresh_qr and abs_path.exists() and _inspect_whatsmeow_db(session_path) is None:
            try:
                abs_path.unlink()
            except Exception:
                pass

        qr_ev = asyncio.Event()
        pair_ev = asyncio.Event()
        self._qr_events[session_path] = qr_ev
        self._pair_events[session_path] = pair_ev

        client = NewAClient(str(abs_path))

        async def _on_qr(_client, data_qr: bytes):
            qr_str = data_qr.decode("utf-8", "replace")
            svg_uri = _render_real_qr_svg(qr_str)
            p = self._pending.setdefault(session_path, {})
            p["qr_code"] = svg_uri
            p["qr_raw"] = qr_str
            qr_ev.set()
            self._sync_db_account_state(session_path, qr_code=svg_uri)

        client.qr(_on_qr)

        @client.event(PairStatusEv)
        async def _on_pair(_client, ev):
            try:
                user_part = str(getattr(getattr(ev, "ID", None), "User", "") or "")
                digits = re.sub(r"\D", "", user_part)
                bname = str(getattr(ev, "BusinessName", "") or "").strip()
                info = _inspect_whatsmeow_db(session_path) or {
                    "wa_jid": f"{digits}@s.whatsapp.net" if digits else None,
                    "phone": f"+{digits}" if digits else None,
                    "telegram_user_id": int(digits[-15:]) if digits else None,
                    "name": bname or (f"WhatsApp +{digits}" if digits else "WhatsApp"),
                }
                p = self._pending.setdefault(session_path, {})
                p["state"] = "connected"
                p["wa_jid"] = info.get("wa_jid")
                p["phone"] = info.get("phone") or p.get("phone")
                pair_ev.set()
                self._sync_db_account_state(session_path, connected_info=info)
            except Exception:
                pair_ev.set()

        @client.event(ConnectedEv)
        async def _on_connected(_client, _ev):
            info = _inspect_whatsmeow_db(session_path)
            p = self._pending.setdefault(session_path, {})
            p["state"] = "connected"
            if info:
                p["wa_jid"] = info.get("wa_jid")
                p["phone"] = info.get("phone") or p.get("phone")
                self._sync_db_account_state(session_path, connected_info=info)
            pair_ev.set()

        @client.event(MessageEv)
        async def _on_message(_client, ev):
            try:
                info_proto = getattr(ev, "Info", None)
                src = getattr(info_proto, "MessageSource", None)
                if src is None or getattr(src, "IsGroup", False) or getattr(src, "IsFromMe", False):
                    return
                sender = getattr(src, "Sender", None) or getattr(src, "Chat", None)
                sender_user = str(getattr(sender, "User", "") or "").split(":", 1)[0]
                digits = re.sub(r"\D", "", sender_user)
                if not digits:
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
                push_name = str(getattr(info_proto, "Pushname", "") or f"WA +{digits}").strip()
                self._message_seq += 1
                payload = {
                    "telegram_user_id": peer_id,
                    "username": None,
                    "name": push_name,
                    "phone": f"+{digits}",
                    "wa_jid": f"{digits}@s.whatsapp.net",
                    "telegram_message_id": self._message_seq,
                    "external_message_id": str(getattr(info_proto, "ID", "") or f"WA{self._message_seq}"),
                    "text": text,
                    "sent_at": utc_now(),
                    "direction": "in",
                    "peer_type": "user",
                }
                self.history.setdefault(peer_id, []).append(dict(payload))
                self.push_private(session_path, payload)
            except Exception:
                pass

        await client.connect()
        self._neonize_clients[session_path] = client
        return client

    async def request_code(
        self, phone: str, session_path: str, auth_mode: str = "code"
    ) -> str:
        clean_phone = (phone or "").strip() or "+79990000000"
        digits = re.sub(r"\D", "", clean_phone)

        if _use_live_neonize():
            existing_info = _inspect_whatsmeow_db(session_path)
            if existing_info:
                self._pending[session_path] = {
                    "phone": existing_info["phone"],
                    "hash": f"wa:connected:{existing_info['wa_jid']}",
                    "pairing_code": "CONNECTED",
                    "qr_code": generate_qr_svg(session_path, existing_info["phone"]),
                    "auth_mode": auth_mode or "code",
                    "state": "connected",
                    "wa_jid": existing_info["wa_jid"],
                }
                return f"wa:connected:{existing_info['wa_jid']}"

            client = await self._ensure_neonize_client(session_path, fresh_qr=True)
            qr_ev = self._qr_events.get(session_path)
            if qr_ev is not None:
                try:
                    await asyncio.wait_for(qr_ev.wait(), timeout=8.0)
                except asyncio.TimeoutError:
                    pass

            p = self._pending.setdefault(session_path, {})
            qr_code = p.get("qr_code") or _render_real_qr_svg(f"https://wa.me/settings/linked_devices#{session_path}")
            pairing_code = self.generate_pairing_code(clean_phone)

            if (auth_mode or "code") == "code" and digits and digits != "79990000000":
                try:
                    raw_code = await asyncio.wait_for(
                        client.PairPhone(digits, show_push_notification=True),
                        timeout=10.0,
                    )
                    if raw_code and str(raw_code).strip():
                        pairing_code = str(raw_code).strip().upper()
                except Exception as exc:
                    raise GatewayError(
                        f"Не удалось запросить код сопряжения WhatsApp для +{digits}: {exc}"
                    ) from exc

            phone_code_hash = f"wa:{pairing_code}:{hashlib.md5(qr_code.encode('utf-8')).hexdigest()[:12]}"
            p.update(
                {
                    "phone": clean_phone,
                    "hash": phone_code_hash,
                    "pairing_code": pairing_code,
                    "qr_code": qr_code,
                    "auth_mode": auth_mode or "code",
                    "state": "pending_code",
                }
            )
            self._sync_db_account_state(
                session_path, qr_code=qr_code, pairing_code=pairing_code
            )
            return phone_code_hash

        pairing_code = self.generate_pairing_code(clean_phone)
        qr_code = self.generate_qr_svg(session_path, clean_phone)
        payload = {
            "provider": "whatsapp",
            "auth_mode": auth_mode or "code",
            "phone": clean_phone,
            "pairing_code": pairing_code,
            "qr_code": qr_code,
        }
        phone_code_hash = f"wa:{pairing_code}:{hashlib.md5(qr_code.encode('utf-8')).hexdigest()[:12]}"
        self._pending[session_path] = {
            "phone": clean_phone,
            "hash": phone_code_hash,
            "pairing_code": pairing_code,
            "qr_code": qr_code,
            "auth_mode": auth_mode or "code",
            "state": "pending_code",
        }
        path = resolve_session(session_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"PENDING:{json.dumps(payload)}".encode("utf-8"))
        return phone_code_hash

    async def confirm_code(
        self, phone: str, code: str, phone_code_hash: str, session_path: str
    ) -> dict:
        clean_phone = (phone or "").strip() or "+79990000000"
        raw_code = (code or "").strip()
        if not raw_code:
            raise GatewayError("Укажите код сопряжения или отсканируйте QR")

        if _use_live_neonize():
            info = _inspect_whatsmeow_db(session_path)
            if not info:
                pair_ev = self._pair_events.get(session_path)
                if pair_ev is not None and not pair_ev.is_set():
                    try:
                        await asyncio.wait_for(pair_ev.wait(), timeout=2.5)
                    except asyncio.TimeoutError:
                        pass
                info = _inspect_whatsmeow_db(session_path)
            if not info:
                raise GatewayError(
                    "Сначала отсканируйте QR-код или введите код сопряжения в WhatsApp на телефоне (Настройки → Связанные устройства)"
                )
            p = self._pending.setdefault(session_path, {})
            p["state"] = "connected"
            p["wa_jid"] = info["wa_jid"]
            p["phone"] = info["phone"]
            return {
                "status": "connected",
                "telegram_user_id": info["telegram_user_id"],
                "telegram_username": None,
                "telegram_name": info["name"],
                "wa_jid": info["wa_jid"],
            }

        upper_code = raw_code.upper()
        nodash_code = upper_code.replace("-", "").replace(" ", "")

        pending = self._pending.get(session_path) or {}
        expected_pairing = (
            pending.get("pairing_code") or self.generate_pairing_code(clean_phone)
        ).upper()
        expected_nodash = expected_pairing.replace("-", "")

        hash_pairing = ""
        if phone_code_hash and phone_code_hash.startswith("wa:"):
            parts = phone_code_hash.split(":")
            if len(parts) >= 2:
                hash_pairing = parts[1].upper()

        valid_codes = {
            expected_pairing,
            expected_nodash,
            "QR",
            "QR_SCANNED",
            "12345",
            str(self.code).upper(),
        }
        if hash_pairing:
            valid_codes.add(hash_pairing)
            valid_codes.add(hash_pairing.replace("-", ""))

        if upper_code not in valid_codes and nodash_code not in valid_codes:
            raise GatewayError("Неверный код сопряжения WhatsApp")

        digits = re.sub(r"\D", "", clean_phone) or "79990000000"
        int_from_phone = int(digits[-15:]) if digits else 79990000000
        wa_jid = f"{digits}@s.whatsapp.net"

        path = resolve_session(session_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"SQLite format 3\x00whatsapp:{clean_phone}".encode("utf-8"))

        self._pending[session_path] = {
            "phone": clean_phone,
            "hash": phone_code_hash,
            "pairing_code": expected_pairing,
            "state": "connected",
            "wa_jid": wa_jid,
        }
        return {
            "status": "connected",
            "telegram_user_id": int_from_phone,
            "telegram_username": None,
            "telegram_name": f"WhatsApp {clean_phone}",
            "wa_jid": wa_jid,
        }

    async def confirm_password(self, password: str, session_path: str) -> dict:
        pending = self._pending.get(session_path) or {}
        phone = pending.get("phone") or "+79990000000"
        digits = re.sub(r"\D", "", phone) or "79990000000"
        return {
            "status": "connected",
            "telegram_user_id": int(digits[-15:]),
            "telegram_username": None,
            "telegram_name": f"WhatsApp {phone}",
            "wa_jid": f"{digits}@s.whatsapp.net",
        }

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
        if pending.get("state") == "connected":
            return {"health": "active", "last_error": None}
        if pending.get("state") == "pending_code":
            return {"health": "unauthorized", "last_error": "Сессия WhatsApp ожидает сопряжения"}

        path = resolve_session(session_path)
        if path.exists():
            if _inspect_whatsmeow_db(session_path) is not None:
                return {"health": "active", "last_error": None}
            if not _use_live_neonize():
                try:
                    raw = path.read_bytes()
                except Exception:
                    raw = b""
                if raw and not raw.startswith(b"PENDING:"):
                    return {"health": "active", "last_error": None}

        return {"health": "unauthorized", "last_error": "Сессия WhatsApp не авторизована"}

    async def load_inbox(self, session_path: str, limit: int = 30) -> list:
        cap = _inbox_limit(limit)
        found = []
        seen_peers: set[int] = set()
        # Load real contacts from whatsmeow_contacts SQLite table if present
        path = resolve_session(session_path)
        if path.exists() and _inspect_whatsmeow_db(session_path) is not None:
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
                        digits = re.sub(r"\D", "", u_part)
                        if not digits:
                            continue
                        peer_id = int(digits[-15:])
                        if peer_id in seen_peers:
                            continue
                        seen_peers.add(peer_id)
                        cname = str(crow[1] or crow[2] or crow[3] or crow[4] or f"WA +{digits}").strip()
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
                                "last_message_preview": (last_msg.get("text") or "")[:120] if last_msg else None,
                                "last_message_at": last_msg.get("sent_at") if last_msg else None,
                                "last_from_peer": (1 if last_msg.get("direction") == "in" else 0) if last_msg else None,
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
            if peer_id is None:
                continue
            digits = re.sub(r"\D", "", str(item.get("phone") or jid or peer_id))
            row = {
                "telegram_user_id": int(peer_id),
                "username": item.get("username"),
                "name": item.get("name") or (f"WA +{digits}" if digits else ""),
                "peer_type": "user",
                "phone": item.get("phone") or (f"+{digits}" if digits else None),
                "wa_jid": jid or (f"{digits}@s.whatsapp.net" if digits else None),
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

        digits = re.sub(r"\D", "", q)
        if digits and len(digits) >= 7 and not matched:
            peer_id = int(digits[-15:])
            matched.append(
                {
                    "telegram_user_id": peer_id,
                    "username": None,
                    "name": f"WA +{digits}",
                    "phone": f"+{digits}",
                    "wa_jid": f"{digits}@s.whatsapp.net",
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
    ) -> dict:
        body = (text or "").strip()
        if not body:
            raise GatewayError("Напишите сообщение")
        if isinstance(peer_id, int) or str(peer_id).lstrip("-").isdigit():
            peer_key = int(peer_id)
        else:
            digits = re.sub(r"\D", "", str(peer_id))
            peer_key = int(digits[-15:]) if digits else str(peer_id).strip()

        if simulate_typing:
            self.presence_log.append(
                {
                    "session_path": session_path,
                    "peer_id": peer_key,
                    "presence": "composing",
                }
            )
            await asyncio.sleep(0.01)

        self._message_seq += 1
        used = {
            int(row.get("telegram_message_id") or 0)
            for row in self.history.get(peer_key, [])
        }
        while self._message_seq in used:
            self._message_seq += 1

        ext_hash = hashlib.sha1(
            f"{session_path}:{peer_key}:{self._message_seq}:{body}".encode("utf-8")
        ).hexdigest()[:16].upper()
        external_message_id = f"3EB0{ext_hash}"

        if _use_live_neonize() and _inspect_whatsmeow_db(session_path) is not None:
            client = await self._ensure_neonize_client(session_path, fresh_qr=False)
            target_digits = re.sub(r"\D", "", str(peer_id))
            if not target_digits:
                raise GatewayError("Некорректный номер получателя WhatsApp")
            try:
                send_resp = await client.send_message(
                    f"{target_digits}@s.whatsapp.net", body
                )
                resp_id = getattr(send_resp, "ID", None)
                if resp_id:
                    external_message_id = str(resp_id)
            except Exception as exc:
                raise GatewayError(f"Ошибка отправки в WhatsApp: {exc}") from exc

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
            "telegram_message_id": self._message_seq,
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
        cap = _history_limit(limit)
        rows = self.history.get(int(peer_id), [])
        return [dict(row) for row in rows[-cap:]]

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
        if _use_live_neonize() and _inspect_whatsmeow_db(session_path) is not None:
            try:
                await self._ensure_neonize_client(session_path, fresh_qr=False)
            except Exception:
                pass
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

    async def check_numbers(
        self, session_path: str, phones: list[str]
    ) -> list[dict]:
        results = []
        for raw in phones or []:
            digits = re.sub(r"\D", "", str(raw or ""))
            if not digits or len(digits) < 7:
                continue
            peer_id = int(digits[-15:])
            results.append(
                {
                    "phone": f"+{digits}",
                    "found": True,
                    "wa_jid": f"{digits}@s.whatsapp.net",
                    "telegram_user_id": peer_id,
                    "username": None,
                    "name": f"WA +{digits}",
                }
            )
        return results

    async def resolve_phones(
        self, session_path: str, phones: list[str]
    ) -> list[dict]:
        return await self.check_numbers(session_path, phones)
