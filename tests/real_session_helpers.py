"""Cryptographically valid Telethon & WhatsApp (whatsmeow) session builders for real-structure tests."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession


def make_real_telethon_string_session() -> str:
    """Create a syntactically and cryptographically valid 256-byte Telethon StringSession."""
    sess = StringSession()
    sess.set_dc(2, "149.154.167.40", 443)
    sess.auth_key = AuthKey(os.urandom(256))
    return sess.save()


def make_real_telethon_sqlite_bytes(tmp_dir: Path, name: str = "real_tg") -> bytes:
    """Create a real Telethon SQLite .session file (>= 4096 bytes, 256-byte auth_key) and return its bytes."""
    p = tmp_dir / f"{name}.session"
    sess = SQLiteSession(str(p))
    sess.set_dc(2, "149.154.167.40", 443)
    sess.auth_key = AuthKey(os.urandom(256))
    sess.save()
    sess.close()
    raw = p.read_bytes()
    if len(raw) < 4096:
        with sqlite3.connect(p) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _pad (x TEXT)")
            conn.execute("INSERT INTO _pad (x) VALUES (?)", ("x" * 4096,))
            conn.commit()
        raw = p.read_bytes()
    return raw


def make_real_whatsmeow_db(
    target_path: Path,
    *,
    jid: str = "31612345678:1@s.whatsapp.net",
    push_name: str = "SoftUGC WA",
    business_name: str = "SoftUGC",
    contacts: tuple[tuple[str, str], ...] = (),
) -> Path:
    """Create a real >= 4096-byte whatsmeow SQLite session DB passing _inspect_whatsmeow_db."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()
    with sqlite3.connect(target_path) as conn:
        conn.execute(
            """
            CREATE TABLE whatsmeow_device (
                jid TEXT PRIMARY KEY,
                registration_id INTEGER,
                noise_key BLOB,
                identity_key BLOB,
                signed_pre_key BLOB,
                signed_pre_key_id INTEGER,
                signed_pre_key_sig BLOB,
                platform TEXT,
                business_name TEXT,
                push_name TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE whatsmeow_contacts (
                our_jid TEXT,
                their_jid TEXT,
                first_name TEXT,
                full_name TEXT,
                push_name TEXT,
                business_name TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO whatsmeow_device (
                jid, registration_id, noise_key, identity_key,
                signed_pre_key, signed_pre_key_id, signed_pre_key_sig,
                platform, business_name, push_name
            ) VALUES (?, 123456, ?, ?, ?, 1, ?, 'smba', ?, ?)
            """,
            (jid, os.urandom(32), os.urandom(32), os.urandom(32), os.urandom(64), business_name, push_name),
        )
        for their_jid, full_name in contacts:
            conn.execute(
                """
                INSERT INTO whatsmeow_contacts (our_jid, their_jid, first_name, full_name, push_name, business_name)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (jid, their_jid, full_name, full_name, full_name, ""),
            )
        # Ensure multi-page authentic SQLite DB >= 4096 bytes
        conn.execute("CREATE TABLE whatsmeow_app_state_sync_keys (jid TEXT, key_id BLOB, key_data BLOB)")
        conn.execute(
            "INSERT INTO whatsmeow_app_state_sync_keys VALUES (?, ?, ?)",
            (jid, os.urandom(16), os.urandom(4096)),
        )
        conn.commit()
    assert target_path.stat().st_size >= 4096
    return target_path


def make_protocol_wa_client_factory(
    *,
    registered_phones: set[str] | None = None,
    pairing_code: str = "REAL-9821",
    qr_payload: str = "2@RealWhatsAppHandshakePayloadTokenForProtocolTest1234567890",
    pair_on_connect: dict[str, str] | None = None,
):
    """Build a NewAClient-compatible factory tracking real ProxySettings, IsOnWhatsApp, ChatPresence, and SendMessage."""
    records: dict[str, list] = {
        "connect_proxies": [],
        "proxy_updates": [],
        "checked_numbers": [],
        "presence_calls": [],
        "sent_messages": [],
        "stopped": [],
    }
    allowed_set = (
        {"".join(ch for ch in p if ch.isdigit())[-15:] for p in registered_phones}
        if registered_phones is not None
        else None
    )

    class _ProtocolWaClient:
        def __init__(self, db_path: str):
            self.db_path = db_path
            self._qr_cb = None
            self._handlers = {}

        def qr(self, cb):
            self._qr_cb = cb

        def event(self, ev_cls):
            def _wrap(fn):
                self._handlers[ev_cls] = fn
                return fn

            return _wrap

        async def connect(self, proxy_settings=None):
            records["connect_proxies"].append(
                getattr(proxy_settings, "proxy_address", proxy_settings)
            )
            if self._qr_cb is not None and qr_payload:
                await self._qr_cb(self, qr_payload.encode("utf-8"))
            if pair_on_connect is not None:
                make_real_whatsmeow_db(
                    Path(self.db_path),
                    jid=pair_on_connect.get("jid", "31612345678:1@s.whatsapp.net"),
                    push_name=pair_on_connect.get("push_name", "Paired WA"),
                )

        async def set_proxy_address(self, proxy_address: str | None, **_kw):
            records["proxy_updates"].append(proxy_address)

        async def PairPhone(self, phone: str, show_push_notification: bool = True):
            del phone, show_push_notification
            return pairing_code

        async def is_on_whatsapp(self, *numbers: str):
            records["checked_numbers"].extend(numbers)
            out = []
            for num in numbers:
                digits = "".join(ch for ch in num if ch.isdigit())[-15:]
                is_in = True if allowed_set is None else (digits in allowed_set)
                out.append(
                    SimpleNamespace(
                        Query=num,
                        IsIn=is_in,
                        JID=SimpleNamespace(User=digits, Server="s.whatsapp.net"),
                    )
                )
            return out

        async def send_chat_presence(self, jid, state, media):
            records["presence_calls"].append((str(jid), state, media))

        async def send_message(self, to: str, message: str):
            records["sent_messages"].append((str(to), str(message)))
            return SimpleNamespace(ID=f"3EB0PROTO{len(records['sent_messages']):04d}")

        async def stop(self):
            records["stopped"].append(self.db_path)

    return _ProtocolWaClient, records
