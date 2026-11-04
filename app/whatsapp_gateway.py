import asyncio
import hashlib
import json
import re
import threading
import urllib.parse
from pathlib import Path

from app.db import resolve_session, utc_now
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
    import neonize  # type: ignore  # pragma: no cover

    HAS_NEONIZE = True
except Exception:
    neonize = None
    HAS_NEONIZE = False


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
        self._lock = threading.Lock()
        self._listeners = {}
        self._backlog = {}
        self._loop = None

    @staticmethod
    def generate_pairing_code(phone: str) -> str:
        return generate_pairing_code(phone)

    @staticmethod
    def generate_qr_svg(session_path: str, phone: str = "") -> str:
        return generate_qr_svg(session_path, phone)

    async def request_code(
        self, phone: str, session_path: str, auth_mode: str = "code"
    ) -> str:
        clean_phone = (phone or "").strip() or "+79990000000"
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
        int_from_phone = int(digits[-12:]) if digits else 79990000000
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
            "telegram_user_id": int(digits[-12:]),
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
            peer_id = int(digits[-12:])
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
            peer_key = int(digits[-12:]) if digits else str(peer_id).strip()

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
            peer_id = int(digits[-12:])
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
