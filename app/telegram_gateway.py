import asyncio
import os
import threading
from abc import ABC, abstractmethod

from datetime import timezone

from app.db import resolve_session, utc_now


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


class TelethonGateway(TelegramGateway):
    def __init__(self):
        self._clients = {}

    def _credentials(self):
        api_id = os.getenv("TG_API_ID", "").strip()
        api_hash = os.getenv("TG_API_HASH", "").strip()
        if not api_id or not api_hash:
            raise GatewayError(
                "Нужны ключи TG_API_ID и TG_API_HASH с https://my.telegram.org/apps"
            )
        return int(api_id), api_hash

    async def _open(self, session_path: str):
        client = self._clients.get(session_path)
        if client is not None:
            if not client.is_connected():
                await client.connect()
            return client, False
        from telethon import TelegramClient

        api_id, api_hash = self._credentials()
        client = TelegramClient(_session_name(session_path), api_id, api_hash)
        await client.connect()
        self._clients[session_path] = client
        return client, True

    async def _close(self, session_path: str) -> None:
        client = self._clients.pop(session_path, None)
        if client is not None and client.is_connected():
            await client.disconnect()

    async def request_code(self, phone: str, session_path: str) -> str:
        client, _owned = await self._open(session_path)
        try:
            sent = await client.send_code_request(phone)
            return sent.phone_code_hash
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(str(exc) or "Не удалось отправить код") from exc
        finally:
            await self._close(session_path)

    async def confirm_code(
        self, phone: str, code: str, phone_code_hash: str, session_path: str
    ) -> dict:
        from telethon import errors

        client, _owned = await self._open(session_path)
        try:
            user = await client.sign_in(
                phone=phone, code=code, phone_code_hash=phone_code_hash
            )
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
        profile = {
            "status": "connected",
            "telegram_user_id": user.id,
            "telegram_username": getattr(user, "username", None),
            "telegram_name": _person_name(user),
        }
        await self._close(session_path)
        return profile

    async def confirm_password(self, password: str, session_path: str) -> dict:
        from telethon import errors

        try:
            client, _owned = await self._open(session_path)
            user = await client.sign_in(password=password)
        except errors.PasswordHashInvalidError as exc:
            raise GatewayError("Неверный пароль") from exc
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(str(exc) or "Не удалось подтвердить пароль") from exc
        profile = {
            "status": "connected",
            "telegram_user_id": user.id,
            "telegram_username": getattr(user, "username", None),
            "telegram_name": _person_name(user),
        }
        await self._close(session_path)
        return profile

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
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

    async def send_message(self, session_path: str, peer_id: int, text: str, reply_to=None) -> dict:
        body = (text or "").strip()
        if not body:
            raise GatewayError("Напишите сообщение")
        client, owned = await self._open(session_path)
        try:
            kwargs = {"reply_to": int(reply_to)} if reply_to else {}
            msg = await client.send_message(int(peer_id), body, **kwargs)
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
            "sent_at": _stamp(getattr(msg, "date", None)),
            "direction": "out",
        }

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
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
            entity = await client.get_entity(int(peer_id))
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
            await client.run_until_disconnected()
        finally:
            client.remove_event_handler(handler)

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


class FakeGateway(TelegramGateway):
    def __init__(self, code="12345", password="secret", needs_password=False, dialogs=None, history=None):
        self.code = code
        self.password = password
        self.needs_password = needs_password
        self.dialogs = [dict(item) for item in (dialogs or [])]
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

    async def send_message(self, session_path: str, peer_id: int, text: str, reply_to=None) -> dict:
        body = (text or "").strip()
        if not body:
            raise GatewayError("Напишите сообщение")
        self._message_seq += 1
        used = {
            int(row.get("telegram_message_id") or 0)
            for row in self.history.get(int(peer_id), [])
        }
        while self._message_seq in used:
            self._message_seq += 1
        self.sent.append({"peer_id": int(peer_id), "text": body, "reply_to": reply_to})
        record = {
            "telegram_message_id": self._message_seq,
            "text": body,
            "sent_at": utc_now(),
            "direction": "out",
            "reply_to_telegram_message_id": int(reply_to) if reply_to else None,
        }
        self.history.setdefault(int(peer_id), []).append(dict(record))
        return record

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        cap = _history_limit(limit)
        rows = self.history.get(int(peer_id), [])
        return [dict(row) for row in rows[-cap:]]
