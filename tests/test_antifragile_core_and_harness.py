from __future__ import annotations

import asyncio
import base64
import gzip
import json
import sqlite3
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Any

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if not sys.path or sys.path[0] != _WORKTREE_ROOT:
    sys.path.insert(0, _WORKTREE_ROOT)

import pytest
from fastapi.testclient import TestClient

import app.cloud_persistence as cp
import app.crm_auth as crm_auth
import app.digest as digest_mod
import app.inbox as inbox_mod
import app.tg_web_proxy as twp
from app.airtable_sync import pull_from_airtable, sync_airtable
from app.db import ThreadSafeConnection, connect, init_db, locked_conn, utc_now
from app.folders import list_folders, pull_folders, sync_all_folders, update_folder
from app.inbox import (
    _upsert_dialog_message_row,
    apply_incoming,
    apply_live_outgoing,
    ensure_playable_voice_wav,
    open_dialog_by_lead,
    send_dialog_media,
    send_reply,
    stop_outreach_on_reply,
)
from app.leads import create_lead, get_lead
from app.main import create_app
from app.outreach import create_campaign
from app.security import _requires_admin_role, extract_client_ip, is_auth_enforced
from app.telegram_gateway import FakeGateway
from app.whatsapp_gateway import WhatsAppGateway


def _seed_connected_account(
    conn: sqlite3.Connection,
    *,
    phone: str = "+79990001122",
    provider: str = "telegram",
    username: str = "mgr_acc",
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, provider, health, created_at, updated_at
        ) VALUES (?, ?, 'connected', 100500, ?, 'Manager', ?, 'active', ?, ?)
        """,
        (phone, f"data/sessions/{provider}_{phone[-4:]}.session", username, provider, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


# 1. Privacy-by-Default синхронизации папок (Sacred Rule 4)
def test_folders_privacy_by_default_explicit_allowlist(tmp_path: Path):
    conn = connect(tmp_path / "folders_privacy.db")
    init_db(conn)
    acc_id = _seed_connected_account(conn)
    gw = FakeGateway(
        folders=[
            {
                "telegram_folder_id": 10,
                "title": "Личное и Семья",
                "sync_enabled": 1,
                "peers": [
                    {"telegram_user_id": 501, "username": "mom_private", "name": "Мама", "peer_type": "user"}
                ],
            },
            {
                "telegram_folder_id": 11,
                "title": "Клиенты SoftUGC",
                "sync_enabled": 1,
                "peers": [
                    {"telegram_user_id": 502, "username": "target_founder", "name": "Фаундер", "peer_type": "user"}
                ],
            },
        ]
    )

    pulled = asyncio.run(pull_folders(conn, acc_id, gw))
    assert {f["sync_enabled"] for f in pulled} == {0}

    # sync_all_folders without explicit folder_ids MUST ONLY sync allowlisted (sync_enabled=1) folders
    batch_res = asyncio.run(sync_all_folders(conn, acc_id, gw, folder_ids=None))
    assert batch_res["synced_folders"] == 0
    assert batch_res["added"] == 0
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert {f["sync_enabled"] for f in list_folders(conn, acc_id)} == {0}

    # Explicitly allowlisting only "Клиенты SoftUGC" imports strictly that folder
    clients_folder = next(f for f in pulled if f["title"] == "Клиенты SoftUGC")
    update_folder(conn, int(clients_folder["id"]), True)
    batch_allowed = asyncio.run(sync_all_folders(conn, acc_id, gw, folder_ids=None))
    assert batch_allowed["synced_folders"] == 1
    leads = conn.execute("SELECT username FROM leads").fetchall()
    assert [r["username"] for r in leads] == ["target_founder"]


# 2a & 2b. Устранение гонки исходящих и сохранение preview при mid is None
def test_outgoing_race_send_reply_and_media_idempotent_and_preview_without_mid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(inbox_mod, "INBOX_MEDIA_DIR", tmp_path / "inbox_media")
    conn = connect(tmp_path / "inbox_race.db")
    init_db(conn)
    acc_id = _seed_connected_account(conn)
    now = utc_now()
    dlg_id = int(
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                muted, created_at, updated_at
            ) VALUES (?, 'user', 77001, 'race_peer', 'Race Peer', 0, 1, ?, 'Жду ответ', 1, 0, ?, ?)
            """,
            (acc_id, now, now, now),
        ).lastrowid
    )
    conn.commit()

    class LiveWatcherRaceGateway(FakeGateway):
        async def send_message(self, session_path, peer_id, text, reply_to=None, **kw):
            sent_ts = utc_now()
            # Live watcher inserts the outgoing message 1ms BEFORE send_message returns
            apply_live_outgoing(
                conn,
                acc_id,
                {
                    "telegram_user_id": 77001,
                    "telegram_message_id": 9001,
                    "username": "race_peer",
                    "name": "Race Peer",
                    "text": text,
                    "sent_at": sent_ts,
                    "direction": "out",
                },
            )
            return {"telegram_message_id": 9001, "text": text, "sent_at": sent_ts}

        async def send_media_message(
            self, session_path, peer_id, file_path, media_type="document", caption="", reply_to=None, duration=None
        ):
            sent_ts = utc_now()
            apply_live_outgoing(
                conn,
                acc_id,
                {
                    "telegram_user_id": 77001,
                    "telegram_message_id": 9002,
                    "username": "race_peer",
                    "name": "Race Peer",
                    "text": caption,
                    "media_type": media_type,
                    "sent_at": sent_ts,
                    "direction": "out",
                },
            )
            return {
                "telegram_message_id": 9002,
                "text": caption,
                "media_type": media_type,
                "sent_at": sent_ts,
            }

    gw = LiveWatcherRaceGateway()
    reply_row = asyncio.run(send_reply(conn, dlg_id, "Отправил КП без ошибки 400!", gw))
    assert reply_row["telegram_message_id"] == 9001
    assert reply_row["text"] == "Отправил КП без ошибки 400!"

    media_row = asyncio.run(
        send_dialog_media(
            conn,
            dlg_id,
            file_bytes=b"%PDF-1.4 sample offer",
            filename="offer.pdf",
            mime_type="application/pdf",
            media_type="document",
            caption="Презентация SoftUGC",
            gateway=gw,
        )
    )
    assert media_row["telegram_message_id"] == 9002
    assert media_row["media_url"].startswith("/media/inbox/")

    # _upsert_dialog_message_row must not lose last_message_preview when mid is None
    dlg2_id = int(
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                muted, created_at, updated_at
            ) VALUES (?, 'user', 77002, 'nomid_peer', 'NoMid Peer', 0, 0, NULL, NULL, 1, 0, ?, ?)
            """,
            (acc_id, now, now),
        ).lastrowid
    )
    conn.commit()
    _upsert_dialog_message_row(
        conn,
        dlg2_id,
        "in",
        {
            "mid": None,
            "text": "Сообщение без message_id",
            "preview": "Сообщение без message_id",
            "sent_at": now,
        },
        now,
    )
    d2 = conn.execute("SELECT last_message_preview FROM dialogs WHERE id = ?", (dlg2_id,)).fetchone()
    assert d2["last_message_preview"] == "Сообщение без message_id"

    # Empty preview with mid is None must never clobber existing last_message_preview
    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 77002,
            "telegram_message_id": None,
            "username": "nomid_peer",
            "text": "",
            "direction": "in",
        },
    )
    d2_after = conn.execute("SELECT last_message_preview FROM dialogs WHERE id = ?", (dlg2_id,)).fetchone()
    assert d2_after["last_message_preview"] == "Сообщение без message_id"


# 2c. Запрет подмены голосовых клиента синтезированной речью при ошибке ffmpeg
def test_voice_conversion_never_fakes_client_audio_on_ffmpeg_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(inbox_mod, "INBOX_MEDIA_DIR", tmp_path / "voice_media")
    corrupt_ogg = b"OggS\x00\x02corrupt-client-voice-payload-" * 15
    res_corrupt = ensure_playable_voice_wav(
        corrupt_ogg,
        cache_key="client_voice_corrupt",
        text_hint="Голосовое по запуску пилота",
    )
    assert res_corrupt is None
    assert ensure_playable_voice_wav(b"", cache_key="empty_voice") is None
    assert not hasattr(inbox_mod, "_synthesize_harmonic_speech_wav")


# 3a & 3b. Push строго до Pull и запрет удаления локальных лидов без airtable_record_id (БЕЗ PYTEST_CURRENT_TEST!)
def test_airtable_push_before_pull_and_no_local_lead_deletion_without_pytest_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "prod-sim-bot-token")
    monkeypatch.delenv("BOT_CHAT_ID", raising=False)

    conn = connect(tmp_path / "airtable_safe.db")
    init_db(conn)
    acc_id = _seed_connected_account(conn)
    now_ts = utc_now()
    folder_id = int(
        conn.execute(
            """
            INSERT INTO folders (account_id, telegram_folder_id, title, sync_enabled, created_at, updated_at)
            VALUES (?, 55, 'Warm Folder', 1, ?, ?)
            """,
            (acc_id, now_ts, now_ts),
        ).lastrowid
    )

    # 1) Older local CRM lead without airtable_record_id and folder_id IS NULL (the exact target of old 5m delete!)
    local_unlinked = create_lead(
        conn,
        name="Local Unlinked Founder",
        username="local_unlinked_founder",
        status="talking",
        amount=2500,
    )
    conn.execute(
        "UPDATE leads SET created_at = '2026-01-01T00:00:00' WHERE id = ?",
        (local_unlinked["id"],),
    )

    # 2) Folder-linked local lead without airtable_record_id
    local_folder_lead = create_lead(
        conn,
        name="Local Folder Founder",
        username="local_folder_founder",
        status="talking",
        amount=1800,
    )
    conn.execute(
        "UPDATE leads SET created_at = '2026-01-01T00:00:00', folder_id = ? WHERE id = ?",
        (folder_id, local_folder_lead["id"]),
    )

    # 3) Lead whose specific airtable_record_id WAS deleted in Airtable, with an active dialog + message
    deleted_remote = create_lead(
        conn,
        name="Deleted In AT",
        username="deleted_in_at",
        status="new",
        airtable_record_id="recConfirmedDeleted77",
    )
    dlg_keep_id = int(
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                muted, lead_id, created_at, updated_at
            ) VALUES (?, 'user', 88077, 'deleted_in_at', 'Deleted In AT', 0, 0, ?, 'История чата', 1, 0, ?, ?, ?)
            """,
            (acc_id, now_ts, deleted_remote["id"], now_ts, now_ts),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 1, 'in', 'История чата сохранена', ?, ?, ?)
        """,
        (dlg_keep_id, now_ts, now_ts, now_ts),
    )
    conn.execute("DELETE FROM kv_settings WHERE key = 'strict_airtable_leads_only_v1'")
    conn.commit()

    # Re-running init_db WITHOUT PYTEST_CURRENT_TEST must NOT wipe local leads!
    init_db(conn)
    assert conn.execute("SELECT id FROM leads WHERE id = ?", (local_unlinked["id"],)).fetchone() is not None
    assert conn.execute("SELECT id FROM leads WHERE id = ?", (local_folder_lead["id"],)).fetchone() is not None

    class OrderTrackingAirtableClient:
        def __init__(self) -> None:
            self.table_name = "Leads"
            self.ops: list[str] = []
            self.records: list[dict[str, Any]] = [
                {
                    "id": "recExistingAT1",
                    "fields": {
                        "Lead Name": "Existing AT",
                        "Status": "In conversation",
                        "Contact": "@existing_at_user",
                    },
                }
            ]

        def list_records(self, table: str | None = None, **_kw) -> list[dict[str, Any]]:
            t = table or self.table_name
            self.ops.append(f"list:{t}")
            if t == "Leads":
                return [dict(r, fields=dict(r["fields"])) for r in self.records]
            return []

        def create_record(self, table: str, fields: dict[str, Any], *, typecast: bool = True) -> dict[str, Any]:
            self.ops.append(f"create:{table}")
            rec = {"id": f"recCreatedFirst{len(self.records) + 1}", "fields": dict(fields)}
            self.records.append(rec)
            return rec

        def update_record(self, table: str, record_id: str, fields: dict[str, Any], *, typecast: bool = True) -> dict[str, Any]:
            self.ops.append(f"update:{table}:{record_id}")
            for r in self.records:
                if r["id"] == record_id:
                    r["fields"].update(fields)
                    return r
            return {"id": record_id, "fields": dict(fields)}

    at_client = OrderTrackingAirtableClient()

    # Direct pull_from_airtable WITHOUT PYTEST_CURRENT_TEST must NEVER delete local leads with airtable_record_id IS NULL!
    pull_from_airtable(conn, client=at_client)
    assert conn.execute("SELECT id FROM leads WHERE id = ?", (local_unlinked["id"],)).fetchone() is not None
    assert conn.execute("SELECT id FROM leads WHERE id = ?", (local_folder_lead["id"],)).fetchone() is not None
    # Confirmed-deleted airtable_record_id is removed from leads WITHOUT losing dialog or messages
    assert conn.execute("SELECT id FROM leads WHERE id = ?", (deleted_remote["id"],)).fetchone() is None
    assert conn.execute("SELECT id FROM dialogs WHERE id = ?", (dlg_keep_id,)).fetchone() is not None
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE dialog_id = ?", (dlg_keep_id,)).fetchone()[0] == 1

    # Now sync_airtable(direction="both") pushes local_unlinked STRICTLY BEFORE pull!
    at_client.ops.clear()
    res = sync_airtable(conn, direction="both", client=at_client)
    assert res["pushed_to_airtable"] == 1
    first_create_idx = at_client.ops.index("create:Leads")
    first_tasks_pull_idx = at_client.ops.index("list:Tasks")
    assert first_create_idx < first_tasks_pull_idx, f"Expected push before pull, got ops={at_client.ops}"

    assert conn.execute("SELECT id FROM leads WHERE id = ?", (local_folder_lead["id"],)).fetchone() is not None
    pushed_row = get_lead(conn, local_unlinked["id"])
    assert pushed_row["airtable_record_id"].startswith("recCreatedFirst")


# 3c. Запрет перезаписи чужого airtable_record_id, запрет матча по голому имени и устранение O(N^2)
def test_airtable_no_foreign_record_overwrite_no_bare_name_match_and_linear_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    conn = connect(tmp_path / "airtable_match.db")
    init_db(conn)

    deal_one = create_lead(
        conn,
        name="Alex",
        username="alex_multi_deal",
        status="won",
        amount=9000,
        airtable_record_id="recDealOne",
    )
    local_alex = create_lead(
        conn,
        name="Alex",
        company="Some Co",
        status="talking",
        amount=3000,
    )

    class BatchSpyAirtableClient:
        def __init__(self, leads: list[dict[str, Any]]) -> None:
            self.table_name = "Leads"
            self.leads = leads
            self.list_leads_calls = 0
            self._seq = 0

        def list_records(self, table: str | None = None, **_kw) -> list[dict[str, Any]]:
            t = table or self.table_name
            if t == "Leads":
                self.list_leads_calls += 1
                return [dict(r, fields=dict(r["fields"])) for r in self.leads]
            return []

        def create_record(self, table: str, fields: dict[str, Any], *, typecast: bool = True) -> dict[str, Any]:
            self._seq += 1
            rec = {"id": f"recBatch{self._seq}", "fields": dict(fields)}
            self.leads.append(rec)
            return rec

        def update_record(self, table: str, record_id: str, fields: dict[str, Any], *, typecast: bool = True) -> dict[str, Any]:
            for r in self.leads:
                if r["id"] == record_id:
                    r["fields"].update(fields)
                    return r
            return {"id": record_id, "fields": dict(fields)}

    spy = BatchSpyAirtableClient(
        leads=[
            {
                "id": "recDealOne",
                "fields": {"Lead Name": "Alex Deal 1", "Status": "Signed", "Value": 9000, "Contact": "@alex_multi_deal"},
            },
            {
                "id": "recDealTwo",
                "fields": {"Lead Name": "Alex Deal 2", "Status": "New", "Value": 1000, "Contact": "@alex_multi_deal"},
            },
            {
                "id": "recStrangerAlex",
                "fields": {"Lead Name": "Alex", "Company": "Some Co", "Status": "Cancelled", "Value": 0},
            },
        ]
    )

    pull_from_airtable(conn, client=spy)
    # 1) deal_one must NOT have its airtable_record_id overwritten by recDealTwo
    d1_after = get_lead(conn, deal_one["id"])
    assert d1_after["airtable_record_id"] == "recDealOne"
    assert d1_after["status"] == "won"
    assert d1_after["amount"] == 9000

    # 2) local_alex (no username/phone) must NOT match recStrangerAlex by bare name/company!
    local_alex_after = get_lead(conn, local_alex["id"])
    assert local_alex_after["airtable_record_id"] is None
    assert local_alex_after["status"] == "talking"
    assert local_alex_after["amount"] == 3000

    # 3) Batch push of 12 new leads must NOT do O(N^2) list_records scans
    for idx in range(12):
        create_lead(conn, name=f"Batch Lead {idx}", username=f"batch_u_{idx}", status="new")

    spy.list_leads_calls = 0
    sync_airtable(conn, direction="push", client=spy)
    assert spy.list_leads_calls <= 1, f"Expected <= 1 list_records('Leads') in batch push, got {spy.list_leads_calls}"


# 4. Защита R2 lease от AUTH_KEY_DUPLICATED и защита локальной БД в restore_from_cloud
def test_r2_lease_never_steals_live_or_errored_lease_and_restore_protects_local_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(
        cp,
        "_resolve_r2_config",
        lambda conn=None: {"account_id": "acc", "access_key": "ak", "secret_key": "sk", "bucket": "bkt"},
    )

    put_calls: list[bytes] = []
    live_lease = json.dumps(
        {
            "instance_id": "vps-contabo-prod",
            "heartbeat_epoch": time.time(),
            "expires_at": time.time() + 60.0,
            "released": False,
        }
    ).encode("utf-8")

    class FakeResp:
        def __init__(self, status_code: int, content: bytes = b"") -> None:
            self.status_code = status_code
            self.content = content
            self.text = ""

    mode = {"state": "live_foreign"}

    class LeaseAndRestoreClient:
        def __init__(self, *a, **kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            if mode["state"] == "live_foreign":
                return FakeResp(200, live_lease)
            if mode["state"] == "net_error":
                return FakeResp(503, b"Service Unavailable")
            if mode["state"] == "missing_saved_at":
                b = {"version": 1, "saved_at": "", "db_b64": base64.b64encode(stale_bytes).decode("ascii")}
                return FakeResp(200, gzip.compress(json.dumps(b).encode("utf-8")))
            if mode["state"] == "corrupt_db":
                b = {
                    "version": 1,
                    "saved_at": "2099-01-01T00:00:00Z",
                    "db_b64": base64.b64encode(b"NOT_A_VALID_SQLITE_FILE" * 300).decode("ascii"),
                }
                return FakeResp(200, gzip.compress(json.dumps(b).encode("utf-8")))
            return FakeResp(404, b"")

        def put(self, url, content=b"", headers=None):
            put_calls.append(bytes(content))
            return FakeResp(200, b"")

    monkeypatch.setattr(cp.httpx, "Client", LeaseAndRestoreClient)

    # A: Foreign VPS instance actively holding lease -> MUST return False and NEVER overwrite lease!
    acquired_live = cp.wait_and_acquire_tg_runtime_lease(
        max_wait_seconds=0.12, poll_interval=0.04, lease_ttl_seconds=45.0
    )
    assert acquired_live is False
    assert put_calls == []

    # B: R2 HTTP 503 / network error -> fail-closed False!
    mode["state"] = "net_error"
    acquired_err = cp.wait_and_acquire_tg_runtime_lease(
        max_wait_seconds=0.08, poll_interval=0.03, lease_ttl_seconds=45.0
    )
    assert acquired_err is False
    assert cp.check_tg_runtime_lease_owned() is False
    assert put_calls == []

    # C: restore_from_cloud must NEVER overwrite non-empty local DB on missing saved_at or corrupt cloud DB
    stale_db_path = tmp_path / "stale_cloud.db"
    sconn = connect(stale_db_path)
    init_db(sconn)
    sconn.close()
    stale_bytes = stale_db_path.read_bytes()

    local_db_path = tmp_path / "live_local.db"
    lconn = connect(local_db_path)
    init_db(lconn)
    precious = create_lead(lconn, name="Precious Local Lead", username="precious_vip", status="won", amount=15000)
    lconn.close()

    mode["state"] = "missing_saved_at"
    r_missing_meta = cp.restore_from_cloud(local_db_path, force=False)
    assert r_missing_meta["restored"] is False

    mode["state"] = "corrupt_db"
    r_corrupt = cp.restore_from_cloud(local_db_path, force=False)
    assert r_corrupt["restored"] is False

    verify_conn = connect(local_db_path)
    still_there = verify_conn.execute("SELECT name, amount FROM leads WHERE id = ?", (precious["id"],)).fetchone()
    verify_conn.close()
    assert still_there is not None and still_there["amount"] == 15000


# 5. Единая нормализация WhatsApp номеров 13–15 цифр
def test_whatsapp_13_to_15_digit_e164_unified_peer_id_and_stop_outreach(tmp_path: Path):
    from real_session_helpers import make_protocol_wa_client_factory, make_real_whatsmeow_db

    conn = connect(tmp_path / "wa_e164.db")
    init_db(conn)
    wa_acc_id = _seed_connected_account(conn, phone="+4915100000001", provider="whatsapp", username="wa_mgr")
    wa_db = make_real_whatsmeow_db(tmp_path / "wa_e164.session", jid="4915100000001:1@s.whatsapp.net")
    factory, _ = make_protocol_wa_client_factory(registered_phones={"+4915123456789"})
    wa_gw = WhatsAppGateway(client_factory=factory, db_path=tmp_path / "wa_e164.db")

    checked = asyncio.run(wa_gw.check_numbers(str(wa_db), ["+49 (151) 2345-6789"]))
    assert checked[0]["telegram_user_id"] == 4915123456789

    lead = create_lead(
        conn,
        account_id=wa_acc_id,
        name="Hans Berlin",
        phone="+49 151 2345 6789",
        provider="whatsapp",
        status="waiting",
    )
    assert lead["telegram_user_id"] == 4915123456789

    # Opening dialog from lead AND receiving incoming WA message must resolve to the EXACT SAME single dialog
    opened = open_dialog_by_lead(conn, lead["id"], wa_acc_id)
    incoming = apply_incoming(
        conn,
        wa_acc_id,
        {
            "telegram_user_id": 4915123456789,
            "telegram_message_id": 1001,
            "name": "Hans Berlin",
            "phone": "+4915123456789",
            "provider": "whatsapp",
            "text": "Hallo, let's do a pilot!",
            "direction": "in",
        },
    )
    assert opened["id"] == incoming["id"]
    dlg_count = conn.execute("SELECT COUNT(*) FROM dialogs WHERE account_id = ?", (wa_acc_id,)).fetchone()[0]
    assert dlg_count == 1

    # stop_outreach_on_reply by 13-digit phone (even with different formatting on recipient) MUST stop outreach
    camp = create_campaign(conn, name="WA Intl", account_id=wa_acc_id, steps=[{"template_text": "Hi"}])
    now = utc_now()
    rec_id = int(
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, phone, telegram_user_id, name, status, current_step, created_at, updated_at
            ) VALUES (?, '+86 (138) 0013-8000-1', 86138001380001, 'Chen', 'in_progress', 1, ?, ?)
            """,
            (camp["id"], now, now),
        ).lastrowid
    )
    conn.commit()

    stopped = stop_outreach_on_reply(conn, account_id=wa_acc_id, phone="+86138001380001", notify=False)
    assert stopped == 1
    rec_row = conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec_id,)).fetchone()
    assert rec_row["status"] == "replied"


# 6a & 6b. extract_client_ip (XFF правый IP приоритетнее X-Real-IP, только loopback) и admin RBAC
def test_security_xff_over_x_real_ip_direct_peer_and_admin_rbac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    monkeypatch.setenv("BOT_TOKEN", "live-prod-token")
    assert is_auth_enforced() is True

    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "StrongTimPass#2026")
    monkeypatch.setenv("CRM_VLAD_PASSWORD", "StrongVladPass#2026")
    monkeypatch.setenv("CRM_OPERATOR_PASSWORD", "StrongOpPass#2026")

    def _req(client_tuple, headers: dict[str, str]):
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        from starlette.requests import Request as StarletteRequest

        return StarletteRequest(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/login",
                "headers": raw_headers,
                "client": client_tuple,
            }
        )

    # Loopback proxy: rightmost XFF appended by Caddy MUST beat spoofed X-Real-IP
    r_loopback = _req(
        ("127.0.0.1", 54321),
        {"x-forwarded-for": "10.0.0.1, 198.51.100.77", "x-real-ip": "9.9.9.9"},
    )
    assert extract_client_ip(r_loopback) == "198.51.100.77"

    # Direct non-loopback peer or missing client: NEVER trust XFF or X-Real-IP
    r_direct = _req(
        ("203.0.113.50", 54321),
        {"x-forwarded-for": "1.1.1.1", "x-real-ip": "2.2.2.2"},
    )
    assert extract_client_ip(r_direct) == "203.0.113.50"
    r_none_client = _req(None, {"x-forwarded-for": "1.1.1.1", "x-real-ip": "2.2.2.2"})
    assert extract_client_ip(r_none_client) not in ("1.1.1.1", "2.2.2.2")

    # RBAC check on dangerous mutations
    for path in (
        "/api/leads/bulk",
        "/api/campaigns/42/run",
        "/api/outreach/autopilot",
        "/api/folders/sync-all",
    ):
        assert _requires_admin_role(path, "POST") is True

    conn = connect(tmp_path / "sec_rbac.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=FakeGateway())
    client = TestClient(app, follow_redirects=False)

    # Attacker rotating spoofed X-Real-IP behind Caddy XFF must still get 429 on 9th attempt
    for i in range(8):
        resp = client.post(
            "/api/auth/login",
            json={"username": "tim", "password": "wrong"},
            headers={
                "X-Forwarded-For": "198.51.100.88",
                "X-Real-IP": f"45.67.89.{i + 1}",
                "User-Agent": "Mozilla/5.0",
            },
        )
        assert resp.status_code == 401
    r_blocked = client.post(
        "/api/auth/login",
        json={"username": "tim", "password": "wrong"},
        headers={
            "X-Forwarded-For": "198.51.100.88",
            "X-Real-IP": "45.67.89.250",
            "User-Agent": "Mozilla/5.0",
        },
    )
    assert r_blocked.status_code == 429

    # Sales user ('vlad') MUST get 403 on all 4 dangerous mutations
    r_vlad = client.post(
        "/api/auth/login",
        json={"username": "vlad", "password": "StrongVladPass#2026"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    assert r_vlad.status_code == 200
    vlad_hdrs = {"Authorization": f"Bearer {r_vlad.json()['token']}", "User-Agent": "Mozilla/5.0"}
    for mut_path, payload in (
        ("/api/leads/bulk", {"lead_ids": [1], "action": "delete"}),
        ("/api/campaigns/1/run", {}),
        ("/api/outreach/autopilot", {"enabled": True}),
        ("/api/folders/sync-all", {"account_id": 1}),
    ):
        r_mut = client.post(mut_path, json=payload, headers=vlad_hdrs)
        assert r_mut.status_code == 403, f"Expected 403 for sales role on {mut_path}, got {r_mut.status_code}"


# 6c. crm_auth обязан оборачивать работу с conn в locked_conn
def test_crm_auth_uses_locked_conn(tmp_path: Path):
    raw_conn = sqlite3.connect(tmp_path / "auth_lock.db", check_same_thread=False)
    raw_conn.row_factory = sqlite3.Row
    init_db(raw_conn)
    crm_auth.ensure_default_crm_users(raw_conn)

    holder_active = threading.Event()
    holder_entered = threading.Event()
    unlocked_collision = {"detected": False}

    def _trace(_sql: str) -> None:
        if holder_active.is_set():
            unlocked_collision["detected"] = True

    raw_conn.set_trace_callback(_trace)

    def _hold_lock() -> None:
        with locked_conn(raw_conn):
            holder_active.set()
            holder_entered.set()
            time.sleep(0.08)
            holder_active.clear()

    t = threading.Thread(target=_hold_lock)
    t.start()
    holder_entered.wait(timeout=1.0)

    user, tok = crm_auth.authenticate_crm_user(raw_conn, "tim", "TimFoldesk2026!")
    t.join(timeout=2.0)
    raw_conn.set_trace_callback(None)
    raw_conn.close()

    assert user["username"] == "tim" and tok
    assert unlocked_collision["detected"] is False, "crm_auth accessed conn without acquiring locked_conn!"


# 6d. tg_web_proxy обязан проверять TLS-сертификат (verify=True)
def test_tg_web_proxy_enforces_tls_verification():
    assert twp._SSL_CTX.verify_mode == ssl.CERT_REQUIRED
    assert twp._SSL_CTX.check_hostname is True


# 6e. ThreadSafeConnection.transaction() удерживает lock на весь BEGIN..COMMIT/ROLLBACK
def test_thread_safe_connection_atomic_transaction_no_interleaving(tmp_path: Path):
    conn = connect(tmp_path / "atomic_tx.db")
    assert isinstance(conn, ThreadSafeConnection)
    assert hasattr(conn, "transaction")
    conn.execute("CREATE TABLE tx_items (id INTEGER PRIMARY KEY AUTOINCREMENT, tag TEXT NOT NULL)")
    conn.commit()

    t1_midway = threading.Event()
    errors: list[Exception] = []

    def _failing_tx() -> None:
        try:
            with conn.transaction():
                conn.execute("INSERT INTO tx_items (tag) VALUES ('from_failing_t1')")
                t1_midway.set()
                time.sleep(0.06)
                raise RuntimeError("abort t1")
        except RuntimeError:
            pass
        except Exception as exc:
            errors.append(exc)

    def _committing_tx() -> None:
        t1_midway.wait(timeout=1.0)
        try:
            with conn.transaction():
                conn.execute("INSERT INTO tx_items (tag) VALUES ('from_committed_t2')")
        except Exception as exc:
            errors.append(exc)

    th1 = threading.Thread(target=_failing_tx)
    th2 = threading.Thread(target=_committing_tx)
    th1.start()
    th2.start()
    th1.join(timeout=2.0)
    th2.join(timeout=2.0)

    assert errors == []
    tags = [r[0] for r in conn.execute("SELECT tag FROM tx_items ORDER BY id").fetchall()]
    conn.close()
    # t1 rolled back cleanly without rolling back t2, and t2 did not prematurely commit t1!
    assert tags == ["from_committed_t2"]


# 6f. digest не читает чужой /opt/softugcbot/.env и безопасен без BOT_CHAT_ID
def test_digest_never_reads_softugcbot_env_and_safe_without_chat_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    fake_foreign_env = tmp_path / "foreign_softugcbot.env"
    fake_foreign_env.write_text(
        "BOT_TOKEN=stolen_foreign_token\nBOT_CHAT_ID=777888999\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(digest_mod, "VPS_SOFTUGCBOT_ENV", fake_foreign_env, raising=False)
    monkeypatch.setattr(digest_mod, "SOFTUGCBOT_ENV", fake_foreign_env, raising=False)
    monkeypatch.setattr(digest_mod, "ROOT", tmp_path / "isolated_root", raising=False)

    conn = connect(tmp_path / "digest_safe.db")
    # Even when BOT_TOKEN is set and init_db runs WITHOUT PYTEST_CURRENT_TEST, BOT_CHAT_ID must NOT default to 978069541!
    tok0, cid0 = digest_mod.resolve_colleague_bot_credentials(conn)
    assert tok0 == "" and cid0 == ""

    monkeypatch.setenv("BOT_TOKEN", "crm_own_bot_token")
    init_db(conn)

    http_calls: list[Any] = []

    def _forbid_opener(req, timeout=15):
        http_calls.append(req)
        raise AssertionError("Must never send HTTP request when BOT_CHAT_ID is not configured!")

    with pytest.raises(ValueError):
        digest_mod.send_morning_digest(conn, opener=_forbid_opener)
    assert http_calls == []
    conn.close()
