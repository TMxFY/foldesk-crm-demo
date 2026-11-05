import asyncio
import os
import sqlite3
import threading
from abc import ABC, abstractmethod
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


def _parse_proxy_for_telethon(proxy_url: str | None) -> tuple | None:
    if not proxy_url:
        return None
    raw = str(proxy_url).strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"socks5://{raw}"
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

    def _resolve_proxy(self, session_path: str) -> str | None:
        if session_path in self._proxies:
            return self._proxies[session_path]
        try:
            if DEFAULT_DB.exists():
                with sqlite3.connect(DEFAULT_DB) as conn:
                    row = conn.execute(
                        "SELECT proxy_url FROM accounts WHERE session_path = ? ORDER BY id DESC LIMIT 1",
                        (session_path,),
                    ).fetchone()
                    if row and row[0]:
                        proxy_val = str(row[0]).strip()
                        if proxy_val:
                            self._proxies[session_path] = proxy_val
                            return proxy_val
        except Exception:
            pass
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

    async def _open(self, session_path: str):
        client = self._clients.get(session_path)
        if client is not None:
            if not client.is_connected():
                await client.connect()
            return client, False
        from telethon import TelegramClient

        api_id, api_hash = self._credentials(session_path)
        proxy_tuple = _parse_proxy_for_telethon(self._resolve_proxy(session_path))
        kwargs = {"proxy": proxy_tuple} if proxy_tuple else {}
        client = TelegramClient(_session_name(session_path), api_id, api_hash, **kwargs)
        try:
            await client.connect()
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        self._clients[session_path] = client
        return client, False

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

    async def request_code(self, phone: str, session_path: str) -> str:
        async with self._session_lock(session_path):
            client, _owned = await self._open(session_path)
            try:
                sent = await client.send_code_request(phone)
                return sent.phone_code_hash
            except GatewayError:
                raise
            except Exception as exc:
                await self._close(session_path)
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

    async def send_message(self, session_path: str, peer_id, text: str, reply_to=None) -> dict:
        body = (text or "").strip()
        if not body:
            raise GatewayError("Напишите сообщение")
        async with self._session_lock(session_path):
            client, owned = await self._open(session_path)
            try:
                kwargs = {"reply_to": int(reply_to)} if reply_to else {}
                if isinstance(peer_id, int) or str(peer_id).lstrip("-").isdigit():
                    target = int(peer_id)
                else:
                    target = str(peer_id).strip()
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

    async def send_message(self, session_path: str, peer_id, text: str, reply_to=None) -> dict:
        body = (text or "").strip()
        if not body:
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
        self.sent.append({"peer_id": peer_key, "text": body, "reply_to": reply_to, "session_path": session_path})
        record = {
            "telegram_message_id": self._message_seq,
            "text": body,
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
