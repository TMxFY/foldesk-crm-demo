import os
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
        sent = getattr(msg, "date", None)
        if sent is None:
            sent_at = utc_now()
        else:
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
            sent_at = sent.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return {
            "telegram_message_id": int(msg.id),
            "text": getattr(msg, "message", None) or body,
            "sent_at": sent_at,
            "direction": "out",
        }

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
    def __init__(self, code="12345", password="secret", needs_password=False, dialogs=None):
        self.code = code
        self.password = password
        self.needs_password = needs_password
        self.dialogs = [dict(item) for item in (dialogs or [])]
        self._pending = {}
        self.sent = []
        self._message_seq = 0

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
        self.sent.append({"peer_id": int(peer_id), "text": body, "reply_to": reply_to})
        return {
            "telegram_message_id": self._message_seq,
            "text": body,
            "sent_at": utc_now(),
            "direction": "out",
        }
