import asyncio
import base64
import importlib
import inspect
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

_runtime_logger = logging.getLogger("crmtg.runtime")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict

from app.accounts import (
    check_all_accounts,
    confirm_login,
    confirm_password,
    delete_account,
    import_session_accounts,
    list_accounts,
    purge_dead_accounts,
    reconnect_account,
    record_account_health_transition,
    restore_disk_sessions,
    set_daily_limit,
    start_login,
    update_account_settings,
)
from app.accounts import _load as load_account
from app.tg_web_proxy import register_tg_web_routes
import app.inbox as _inbox_mod
import app.outreach as _outreach_mod
from app.runtime_config import (
    is_ephemeral_runtime,
    is_vps_runtime,
    should_skip_dotenv,
    should_start_airtable_bg_sync,
)
from app.db import (
    INBOX_MEDIA_DIR,
    ROOT,
    ThreadSafeConnection,
    connect,
    create_pipeline_stage,
    init_db,
    list_pipeline_stages,
    locked_conn,
    update_pipeline_stage,
    utc_now,
)
from app.demo import seed_demo
from app.funnel_analytics import (
    apply_read_outbox_update,
    ensure_funnel_columns,
    get_outreach_funnel,
    sync_dialog_read_receipts,
)
from app.inbox import (
    _filter_crm_only_rows,
    _is_vacancy_campaign_meta,
    _load_dialog_public,
    _person_is_crm_relevant,
    _query_inbox_rows,
    apply_incoming,
    clean_tg_username_lookup,
    edit_dialog_message,
    ensure_playable_voice_wav,
    get_dialog_avatar_with_cache,
    get_inbox_counts,
    get_thread,
    list_inbox,
    list_messages,
    mark_dialog_read,
    mark_dialog_unread,
    open_dialog_by_lead,
    remember_dialogs,
    send_dialog_media,
    send_reply,
    start_direct_dialog,
    sync_inbox,
)
from app.folders import list_folders, pull_folders, sync_all_folders, sync_folder, update_folder
from app.leads import (
    add_lead_from_dialog,
    create_lead,
    get_client_full_profile,
    get_lead,
    lead_card,
    search_leads,
    update_lead,
    upsert_peer_profile,
)
from app.notes import add_note
from app.outreach import (
    create_campaign,
    delete_campaign,
    enroll_leads_from_crm,
    get_autopilot_status,
    get_campaign,
    get_outreach_schedule,
    import_campaign_csv,
    list_campaigns,
    preview_campaign_csv,
    preview_outreach_batch,
    run_autopilot_tick,
    run_campaign_batch,
    set_autopilot_enabled,
    update_campaign,
    update_outreach_schedule,
)
from app.properties import (
    create_property_def,
    delete_property_def,
    list_property_defs,
    set_lead_property,
)
from app.tools import convert_phones_to_tg, parse_telegram_group
from app.bot_login import apply_bot_update, handle_bot_reply_to_lead, operator_status, poll_bot_login
from app.digest import (
    _norm_ts,
    apply_digest_callback,
    callback_data_from_update,
    is_ineligible_reply_notify_peer,
    send_morning_digest,
)
from app.reminders import complete_reminder, create_reminder, list_reminders, postpone_reminder
from app.tasks import create_task, set_task_done
from app.telegram_gateway import GatewayError, TelethonGateway, listening_event
from app.views import list_views, save_view
from app.snippets import create_snippet, delete_snippet, list_snippets
from app.whatsapp_gateway import WhatsAppGateway
from app.analytics import get_analytics_summary

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _load_hygiene_module():
    return importlib.import_module("app.hygiene")


def _heal_crm_hygiene_safe(conn) -> dict:
    if conn is None:
        return {
            "quarantined_recipients": 0,
            "quarantined_leads": 0,
            "sanitized_leads": 0,
        }
    return _load_hygiene_module().heal_crm_hygiene(conn)


def _audit_crm_hygiene_safe(conn) -> dict:
    return _load_hygiene_module().audit_crm_hygiene(conn)


def _audit_server_leadgen_readiness_safe(
    conn, *, enforce_vps_checks: bool | None = None
) -> dict:
    hygiene_mod = _load_hygiene_module()
    if enforce_vps_checks is None:
        return hygiene_mod.audit_server_leadgen_readiness(conn)
    return hygiene_mod.audit_server_leadgen_readiness(
        conn, enforce_vps_checks=enforce_vps_checks
    )

KEYS_DETAIL = "Нужны ключи TG_API_ID и TG_API_HASH с https://my.telegram.org/apps"


def load_env_file() -> None:
    if should_skip_dotenv():
        return
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class StartIn(BaseModel):
    phone: str = ""
    proxy_url: str | None = None
    proxy: str | None = None
    access_scope: str | None = None
    access: str | None = None
    provider: str = "telegram"
    auth_mode: str = "code"


class AccountSettingsPatch(BaseModel):
    proxy_url: str | None = None
    access_scope: str | None = None
    daily_limit: int | None = None


class OutreachScheduleIn(BaseModel):
    timezone: str = "UTC"
    days: dict
    autopilot_enabled: bool | None = None


class OutreachAutopilotIn(BaseModel):
    enabled: bool
    activate_drafts: bool = False


class OutreachAutopilotTickIn(BaseModel):
    max_send_per_campaign: int = 1
    respect_delays: bool = True
    force: bool = False



class OpenByLeadIn(BaseModel):
    lead_id: int | None = None
    account_id: int | None = None
    telegram_user_id: int | None = None
    username: str | None = None
    phone: str | None = None
    name: str | None = None


class StartDirectDialogIn(BaseModel):
    handle: str
    account_id: int | None = None
    name: str | None = None


class InboxSyncIn(BaseModel):
    account_id: int | None = None
    crm_only: bool = False


class TgCredentialsIn(BaseModel):
    api_id: str | int
    api_hash: str


class CodeIn(BaseModel):
    code: str


class PasswordIn(BaseModel):
    password: str


class LeadIn(BaseModel):
    account_id: int | None = None
    telegram_user_id: int | None = None
    username: str | None = None
    name: str | None = None
    phone: str | None = None
    status: str = "new"
    note: str | None = None
    next_step: str | None = None
    description: str | None = None
    email: str | None = None
    url: str | None = None
    provider: str | None = None
    amount: int | float | str | None = 0
    currency: str | None = "USD"
    company: str | None = None
    niche: str | None = None
    offer_type: list[str] | str | None = None
    offer_details: str | None = None
    monthly_value: int | float | str | None = 0
    lead_source: str | None = None
    next_follow_up: str | None = None
    last_contacted: str | None = None
    airtable_record_id: str | None = None


class LeadPatch(BaseModel):
    status: str | None = None
    note: str | None = None
    next_step: str | None = None
    name: str | None = None
    username: str | None = None
    phone: str | None = None
    description: str | None = None
    email: str | None = None
    url: str | None = None
    folder_id: int | None = None
    provider: str | None = None
    amount: int | float | str | None = None
    currency: str | None = None
    company: str | None = None
    niche: str | None = None
    offer_type: list[str] | str | None = None
    offer_details: str | None = None
    monthly_value: int | float | str | None = None
    lead_source: str | None = None
    next_follow_up: str | None = None
    last_contacted: str | None = None
    airtable_record_id: str | None = None


class PipelineStageIn(BaseModel):
    key: str | None = None
    label: str
    position: int | None = None
    color: str | None = None


class PipelineStagePatch(BaseModel):
    label: str | None = None
    position: int | None = None
    color: str | None = None


class FolderPullIn(BaseModel):
    account_id: int


class FolderPatch(BaseModel):
    sync_enabled: bool


class ReminderIn(BaseModel):
    lead_id: int
    due_at: str
    text: str


class PostponeIn(BaseModel):
    due_at: str


class DigestCallbackIn(BaseModel):
    data: str


class MessageIn(BaseModel):
    text: str
    reply_to: int | None = None
    reply_to_msg_id: int | None = None


class MessageEditIn(BaseModel):
    text: str


class MediaSendIn(BaseModel):
    media_type: str = "document"
    filename: str = "attachment.bin"
    mime_type: str = ""
    data_base64: str = ""
    caption: str = ""
    duration: int | None = None
    reply_to: int | None = None
    reply_to_msg_id: int | None = None


class NoteIn(BaseModel):
    body: str


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


class TaskDoneIn(BaseModel):
    done: bool = True


class PropertyDefIn(BaseModel):
    name: str
    type: str
    object_type: str = "lead"


class PropertyValueIn(BaseModel):
    value: str | None = None


class ViewIn(BaseModel):
    name: str
    status: str | None = None
    folder_id: int | None = None


class AccountLimitPatch(BaseModel):
    daily_limit: int


class CampaignStepIn(BaseModel):
    step_order: int = 1
    delay_hours: int = 0
    template_text: str


class CampaignIn(BaseModel):
    name: str
    account_id: int | None = None
    account_ids: list[int] | None = None
    daily_limit: int = 20
    status: str | None = None
    steps: list[dict] | None = None
    leadgen_enabled: bool | None = None
    leadgen_daily_quota: int | None = None
    leadgen_sources: list[str] | None = None


CampaignCreateIn = CampaignIn


class CampaignUpdateIn(BaseModel):
    name: str | None = None
    account_id: int | None = None
    account_ids: list[int] | None = None
    daily_limit: int | None = None
    status: str | None = None
    steps: list[dict] | None = None
    leadgen_enabled: bool | None = None
    leadgen_daily_quota: int | None = None
    leadgen_sources: list[str] | None = None


class CampaignCsvIn(BaseModel):
    csv_text: str
    column_mapping: dict[str, str] | None = None


class CampaignFromLeadsIn(BaseModel):
    lead_ids: list[int] | None = None
    status: str | None = None
    folder_id: int | None = None


class CampaignRunIn(BaseModel):
    max_send: int | None = None
    respect_delays: bool = False
    dry_run: bool = False


def _resolve_tg_creds(conn=None) -> tuple[str, str]:
    api_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    if not api_id or not api_hash:
        if conn is not None:
            try:
                rows = dict(
                    conn.execute(
                        "SELECT key, value FROM kv_settings WHERE key IN ('tg_api_id', 'tg_api_hash')"
                    ).fetchall()
                )
                api_id = api_id or str(rows.get("tg_api_id") or "").strip()
                api_hash = api_hash or str(rows.get("tg_api_hash") or "").strip()
            except Exception:
                pass
        if (not api_id or not api_hash) and not should_skip_dotenv():
            api_id = "2040"
            api_hash = "b18441a1ff607e10a989891a5462e627"
    return api_id, api_hash


def should_enable_live_tg_runtime(conn=None, gateway=None) -> bool:
    if os.getenv("DISABLE_TG_WATCHES") == "1":
        return False
    if gateway is not None:
        return True
    if is_ephemeral_runtime():
        return False
    if is_vps_runtime():
        return True
    # Render containers must never auto-connect MTProto sessions (VPS is the sole canonical runtime).
    if os.getenv("RENDER", "").strip().lower() in ("1", "true", "yes"):
        return False
    if os.getenv("ALLOW_LOCAL_TG_SESSIONS", "").strip().lower() in ("1", "true", "yes"):
        return True
    from app.cloud_persistence import _resolve_r2_config

    if _resolve_r2_config(conn) is not None:
        return False
    return True


def _seed_gateway_session_strings(conn, gateway) -> None:
    if conn is None or gateway is None or not hasattr(gateway, "set_session_string"):
        return
    try:
        rows = conn.execute(
            "SELECT session_path, session_string FROM accounts WHERE session_path IS NOT NULL AND session_string IS NOT NULL"
        ).fetchall()
        for s_path, s_str in rows:
            if s_path and s_str:
                gateway.set_session_string(str(s_path), str(s_str))
    except Exception:
        pass


def _resolve_app_gateway(app, provider: str = "telegram"):
    norm = str(provider or "telegram").strip().lower()
    if norm == "whatsapp":
        wa_gw = getattr(app.state, "wa_gateway", None)
        if wa_gw is None:
            wa_gw = WhatsAppGateway()
            app.state.wa_gateway = wa_gw
        return wa_gw
    if not getattr(app.state, "tg_lease_ready", True):
        return None
    if app.state.gateway is not None:
        return app.state.gateway
    conn = getattr(app.state, "conn", None)
    with locked_conn(conn):
        if not should_enable_live_tg_runtime(conn, None):
            return None
        api_id, api_hash = _resolve_tg_creds(conn)
        if not api_id or not api_hash:
            return None
        gateway = TelethonGateway()
        _seed_gateway_session_strings(conn, gateway)
    app.state.gateway = gateway
    return gateway


def _gateway(request: Request):
    if not getattr(request.app.state, "tg_lease_ready", True):
        raise HTTPException(
            status_code=503,
            detail="Переключение релиза Telegram-сессий, повторите через 5 сек",
        )
    gateway = request.app.state.gateway
    if gateway is not None:
        return gateway
    conn = getattr(request.app.state, "conn", None)
    with locked_conn(conn):
        enable_live = should_enable_live_tg_runtime(conn, None)
    if not enable_live and not is_ephemeral_runtime():
        raise HTTPException(
            status_code=409,
            detail="Локальное подключение Telegram отключено для защиты прод-сессий на VPS от AUTH_KEY_DUPLICATED. Задайте ALLOW_LOCAL_TG_SESSIONS=1 для явного локального входа.",
        )
    with locked_conn(conn):
        api_id, api_hash = _resolve_tg_creds(conn)
        if not api_id or not api_hash:
            raise HTTPException(status_code=400, detail=KEYS_DETAIL)
        gateway = TelethonGateway()
        _seed_gateway_session_strings(conn, gateway)
    request.app.state.gateway = gateway
    return gateway


def _gateway_for_provider(request: Request, provider: str = "telegram"):
    norm = str(provider or "telegram").strip().lower()
    if norm == "whatsapp":
        wa_gw = getattr(request.app.state, "wa_gateway", None)
        if wa_gw is None:
            wa_gw = WhatsAppGateway()
            request.app.state.wa_gateway = wa_gw
        return wa_gw
    return _gateway(request)


def _gateway_for_account(request: Request, account_id: int):
    try:
        with locked_conn(request.app.state.conn):
            account = load_account(request.app.state.conn, account_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Не найдено") from exc
    return _gateway_for_provider(request, account.get("provider") or "telegram")


def _extract_conn(args, kwargs):
    for val in args:
        if isinstance(val, sqlite3.Connection):
            return val
    for val in kwargs.values():
        if isinstance(val, sqlite3.Connection):
            return val
    return None


def _call(func, *args, **kwargs):
    conn = _extract_conn(args, kwargs)
    try:
        if conn is not None:
            with locked_conn(conn):
                return func(*args, **kwargs)
        return func(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Не найдено") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _call_async(func, *args, **kwargs):
    try:
        return await func(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Не найдено") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GatewayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class InboxHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = {}
        self._loops = {}

    def subscribe(self, account_id: int) -> asyncio.Queue:
        queue = asyncio.Queue()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._lock:
            if loop is not None:
                self._loops[queue] = loop
            self._subs.setdefault(account_id, set()).add(queue)
        return queue

    def unsubscribe(self, account_id: int, queue) -> None:
        with self._lock:
            self._loops.pop(queue, None)
            subs = self._subs.get(account_id)
            if not subs:
                return
            subs.discard(queue)
            if not subs:
                self._subs.pop(account_id, None)

    def publish(self, account_id: int, dialog: dict) -> None:
        if not isinstance(dialog, dict):
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        with self._lock:
            targets = [
                (queue, self._loops.get(queue))
                for queue in list(self._subs.get(account_id, ()))
            ]
            if account_id != 0:
                targets.extend(
                    (queue, self._loops.get(queue))
                    for queue in list(self._subs.get(0, ()))
                )

        for queue, loop in targets:
            item = dict(dialog)
            if loop is not None and loop is not running_loop and not loop.is_closed():
                loop.call_soon_threadsafe(queue.put_nowait, item)
            else:
                queue.put_nowait(item)


def _watch_alive(app, account_id: int) -> bool:
    if account_id == 0:
        return any(
            t is not None and not t.done()
            for t in app.state.inbox_tasks.values()
        )
    task = app.state.inbox_tasks.get(account_id)
    return task is not None and not task.done()


def _sse(event: str, data) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _dialog_sync_fingerprint(dialog: dict) -> tuple:
    return (
        int(dialog.get("unread_count") or 0),
        int(dialog.get("last_from_peer") or 0),
        str(dialog.get("last_message_at") or ""),
        str(dialog.get("last_message_preview") or ""),
        str(dialog.get("last_message_dir") or ""),
        int(dialog.get("read_outbox_max_id") or 0),
    )


async def _run_db_sync(conn, func, *args, **kwargs):
    if isinstance(conn, ThreadSafeConnection):
        def _locked_call():
            with locked_conn(conn):
                return func(*args, **kwargs)

        return await asyncio.to_thread(_locked_call)
    with locked_conn(conn):
        return func(*args, **kwargs)


def _filter_people_to_existing_or_crm(
    conn, account_id: int, people: list[dict]
) -> list[dict]:
    import re as _re

    if not people:
        return []
    existing_peer_ids = {
        int(r[0])
        for r in conn.execute(
            "SELECT peer_id FROM dialogs WHERE account_id = ? AND peer_id IS NOT NULL",
            (int(account_id),),
        ).fetchall()
    }
    crm_tg_ids = {
        int(r[0])
        for r in conn.execute(
            "SELECT telegram_user_id FROM leads WHERE telegram_user_id IS NOT NULL"
        ).fetchall()
    }
    crm_usernames: set[str] = set()
    for r in conn.execute(
        "SELECT username FROM leads WHERE username IS NOT NULL AND username != ''"
    ).fetchall():
        u = clean_tg_username_lookup(r[0])
        if u:
            crm_usernames.add(u)
    crm_phones: set[str] = set()
    for pr in conn.execute("SELECT phone FROM leads WHERE phone IS NOT NULL AND phone != ''").fetchall():
        dig = _re.sub(r"\D", "", str(pr[0] or ""))
        if len(dig) >= 7:
            crm_phones.add(dig[-15:])
    from app.db import is_recipient_contacted_outreach, is_vacancy_campaign_meta

    for rr in conn.execute(
        """
        SELECT cr.telegram_user_id, cr.username, cr.phone,
               cr.status, cr.current_step, cr.last_sent_at, cr.error_text,
               c.name AS camp_name, c.leadgen_sources_json AS camp_sources
        FROM campaign_recipients cr
        LEFT JOIN campaigns c ON c.id = cr.campaign_id
        WHERE cr.status IN ('in_progress', 'completed', 'replied', 'failed')
        """
    ).fetchall():
        if is_vacancy_campaign_meta(rr["camp_name"], rr["camp_sources"]):
            continue
        if not is_recipient_contacted_outreach(
            rr["status"], rr["current_step"], rr["last_sent_at"], rr["error_text"]
        ):
            continue
        if rr["telegram_user_id"] is not None:
            crm_tg_ids.add(int(rr["telegram_user_id"]))
        if rr["username"]:
            u = clean_tg_username_lookup(rr["username"])
            if u:
                crm_usernames.add(u)
        if rr["phone"]:
            dig = _re.sub(r"\D", "", str(rr["phone"] or ""))
            if len(dig) >= 7:
                crm_phones.add(dig[-15:])
    filtered = []
    for p in people:
        if not isinstance(p, dict):
            continue
        pid = p.get("telegram_user_id") if p.get("telegram_user_id") is not None else p.get("peer_id")
        try:
            pid_int = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid_int = None
        uname = clean_tg_username_lookup(p.get("username"))
        p_dig = _re.sub(r"\D", "", str(p.get("phone") or ""))
        p_norm = p_dig[-15:] if len(p_dig) >= 7 else ""
        inelig, _ = is_ineligible_reply_notify_peer(
            conn,
            peer_id=pid_int,
            username=uname or None,
            phone=p.get("phone"),
            account_id=account_id,
            is_bot=bool(p.get("is_bot")),
        )
        if inelig:
            continue
        if (
            (pid_int is not None and (pid_int in existing_peer_ids or pid_int in crm_tg_ids))
            or (uname and uname in crm_usernames)
            or (p_norm and p_norm in crm_phones)
        ):
            filtered.append(p)
    return filtered


def _sync_and_publish_dialogs_sync(
    conn,
    account_id: int,
    people: list[dict],
    *,
    crm_only: bool = False,
    notify: bool = True,
):
    load_all = getattr(_inbox_mod, "_query_inbox_rows", None)
    if load_all is not None:
        before_rows = load_all(conn, account_id=account_id)
    else:
        before_rows = list_inbox(conn, account_id, "")
    before_map = {
        int(row["id"]): _dialog_sync_fingerprint(row)
        for row in before_rows
        if isinstance(row, dict) and row.get("id") is not None
    }
    effective_people = (
        _filter_people_to_existing_or_crm(conn, account_id, people)
        if crm_only
        else people
    )
    sync_inbox(conn, account_id, effective_people, notify=notify)
    res = sync_dialog_read_receipts(conn, account_id, people)
    if load_all is not None:
        after_rows = load_all(conn, account_id=account_id)
    else:
        after_rows = list_inbox(conn, account_id, "")
    changed = []
    for row in after_rows:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        did = int(row["id"])
        if did not in before_map or before_map[did] != _dialog_sync_fingerprint(row):
            changed.append(row)
    return res, changed


async def _sync_and_publish_dialogs(
    app,
    conn,
    account_id: int,
    people: list[dict],
    *,
    crm_only: bool = False,
    notify: bool = True,
):
    res, changed = await _run_db_sync(
        conn,
        lambda c: _sync_and_publish_dialogs_sync(
            c, account_id, people, crm_only=crm_only, notify=notify
        ),
        conn,
    )
    hub = getattr(app.state, "inbox_hub", None)
    if hub is not None:
        for row in changed:
            hub.publish(account_id, row)
    return res


def _is_dead_account_health(health: object) -> bool:
    return str(health or "active").strip().lower() in ("unauthorized", "banned")


async def ensure_private_inbox(app, account_id: int) -> None:
    # One successful pull per process. A later page open reads SQLite.
    synced = app.state.inbox_synced
    if account_id in synced:
        return
    async with app.state.inbox_sync_lock:
        if account_id in synced:
            return
        try:
            with locked_conn(app.state.conn):
                account = load_account(app.state.conn, account_id)
        except LookupError:
            return
        if account["status"] != "connected":
            return
        if _is_dead_account_health(account.get("health")):
            return
        if str(account.get("session_path") or "").startswith("data/sessions/demo"):
            synced.add(account_id)
            return
        is_live_tg = (
            not is_ephemeral_runtime()
            and isinstance(getattr(app.state, "gateway", None), (type(None), TelethonGateway))
        )
        existing_only_refresh = False
        if is_live_tg:
            with locked_conn(app.state.conn):
                total_dlg_cnt = app.state.conn.execute(
                    "SELECT COUNT(*) FROM dialogs WHERE account_id = ?",
                    (int(account_id),),
                ).fetchone()[0]
            if int(total_dlg_cnt or 0) > 0:
                # Refresh read watermarks quietly before watch catch_up() without ingesting non-CRM DMs or notifying.
                existing_only_refresh = True
        provider = str(account.get("provider") or "telegram").strip().lower()
        gateway = _resolve_app_gateway(app, provider)
        if gateway is None:
            return
        pull_limit = 250 if (existing_only_refresh or is_live_tg) else 30
        try:
            people = await gateway.load_inbox(
                account["session_path"], limit=pull_limit
            )
        except Exception:
            if existing_only_refresh:
                synced.add(account_id)
            return
        if not isinstance(people, list):
            if existing_only_refresh:
                synced.add(account_id)
            return
        try:
            await _sync_and_publish_dialogs(
                app,
                app.state.conn,
                account_id,
                people[:pull_limit],
                crm_only=bool(existing_only_refresh or is_live_tg),
                notify=not (existing_only_refresh or is_live_tg),
            )
        except (LookupError, ValueError):
            return
        synced.add(account_id)


async def start_account_watch(app, account_id: int) -> None:
    try:
        with locked_conn(app.state.conn):
            account = load_account(app.state.conn, account_id)
    except LookupError:
        return
    if account["status"] != "connected":
        return
    if _is_dead_account_health(account.get("health")):
        return
    if str(account.get("session_path") or "").startswith("data/sessions/demo") and isinstance(
        getattr(app.state, "gateway", None), (type(None), TelethonGateway)
    ):
        return
    if (
        not is_ephemeral_runtime()
        and os.getenv("ENABLE_TG_PERSISTENT_WATCHES") != "1"
        and isinstance(getattr(app.state, "gateway", None), (type(None), TelethonGateway))
    ):
        return
    provider = str(account.get("provider") or "telegram").strip().lower()
    gateway = _resolve_app_gateway(app, provider)
    if gateway is None:
        return
    await ensure_private_inbox(app, account_id)
    existing = app.state.inbox_tasks.get(account_id)
    if existing is not None and not existing.done():
        return
    session_path = account["session_path"]
    ready = listening_event(gateway, session_path)
    ready.clear()

    async def on_message(message):
        try:
            dialog = await _run_db_sync(
                app.state.conn, apply_incoming, app.state.conn, account_id, message
            )
        except ValueError:
            return
        app.state.inbox_hub.publish(account_id, dialog)

    async def on_outgoing(message):
        if not isinstance(message, dict):
            return
        apply_live_outgoing_fn = getattr(_inbox_mod, "apply_live_outgoing", None)
        if apply_live_outgoing_fn is None:
            return
        try:
            dialog = await _run_db_sync(
                app.state.conn, apply_live_outgoing_fn, app.state.conn, account_id, message
            )
        except Exception:
            return
        if isinstance(dialog, dict):
            app.state.inbox_hub.publish(account_id, dialog)

    async def on_read(payload):
        if not isinstance(payload, dict):
            return
        peer_id = payload.get("telegram_user_id")
        if peer_id is None:
            peer_id = payload.get("peer_id")
        if peer_id is None:
            return
        outbox_max = payload.get("read_outbox_max_id")
        if outbox_max is not None:
            try:
                await _run_db_sync(
                    app.state.conn,
                    apply_read_outbox_update,
                    app.state.conn,
                    account_id,
                    int(peer_id),
                    int(outbox_max),
                )
            except Exception:
                pass
        inbox_max = payload.get("read_inbox_max_id")
        is_inbox_read = inbox_max is not None or (
            "unread_count" in payload and int(payload.get("unread_count") or 0) == 0
        )
        if is_inbox_read:
            apply_inbox_read_fn = getattr(_inbox_mod, "apply_inbox_read_update", None)
            if apply_inbox_read_fn is not None:
                try:
                    max_arg = int(inbox_max) if inbox_max is not None else None
                    updated = await _run_db_sync(
                        app.state.conn,
                        apply_inbox_read_fn,
                        app.state.conn,
                        account_id,
                        int(peer_id),
                        max_arg,
                    )
                except Exception:
                    updated = None
                if isinstance(updated, dict):
                    app.state.inbox_hub.publish(account_id, updated)

    async def runner():
        watch_kwargs = {}
        try:
            sig_params = inspect.signature(gateway.watch_private).parameters
            accepts_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values()
            )
            if accepts_var_kw or "on_read" in sig_params:
                watch_kwargs["on_read"] = on_read
            if accepts_var_kw or "on_outgoing" in sig_params:
                watch_kwargs["on_outgoing"] = on_outgoing
        except (TypeError, ValueError):
            watch_kwargs = {"on_read": on_read, "on_outgoing": on_outgoing}
        while True:
            try:
                try:
                    await gateway.watch_private(session_path, on_message, **watch_kwargs)
                except TypeError as exc:
                    if "unexpected keyword argument" in str(exc):
                        await gateway.watch_private(session_path, on_message)
                    else:
                        raise
                _runtime_logger.warning(
                    "Watch disconnected normally for account_id=%s, scheduling reconnect...",
                    account_id,
                )
                if hasattr(gateway, "record_disconnect"):
                    gateway.record_disconnect(account_id, reason="watch_eof")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err_low = f"{type(exc).__name__} {exc}".lower()
                if any(
                    fatal in err_low
                    for fatal in (
                        "unauthorized",
                        "auth_key_duplicated",
                        "authkeyduplicated",
                        "auth_key_unregistered",
                        "authkeyunregistered",
                        "authkeyinvalid",
                        "key is not registered",
                        "two different ip",
                        "sessionrevoked",
                        "deactivated",
                        "userdeactivated",
                    )
                ):
                    if hasattr(gateway, "record_disconnect"):
                        gateway.record_disconnect(account_id, reason="watch_fatal")
                    _runtime_logger.error(
                        "Permanent watch failure for account_id=%s: %s", account_id, exc
                    )
                    try:
                        await _run_db_sync(
                            app.state.conn,
                            record_account_health_transition,
                            app.state.conn,
                            account_id,
                            "unauthorized",
                            str(exc),
                        )
                    except Exception:
                        _runtime_logger.exception(
                            "Failed to persist unauthorized health for account_id=%s",
                            account_id,
                        )
                    return
                if hasattr(gateway, "record_disconnect"):
                    gateway.record_disconnect(account_id, reason=type(exc).__name__)
                _runtime_logger.warning(
                    "Transient watch disconnect for account_id=%s (%s), retrying...",
                    account_id,
                    exc,
                )
            retry_delay = float(getattr(app.state, "watch_retry_delay", 5.0) or 5.0)
            await asyncio.sleep(retry_delay)
            try:
                with locked_conn(app.state.conn):
                    refreshed = load_account(app.state.conn, account_id)
                if refreshed.get("status") != "connected" or _is_dead_account_health(
                    refreshed.get("health")
                ):
                    return
            except Exception:
                return

    task = asyncio.create_task(runner())
    app.state.inbox_tasks[account_id] = task
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
    except asyncio.TimeoutError:
        return


async def start_connected_watches(app) -> None:
    with locked_conn(app.state.conn):
        rows = app.state.conn.execute(
            "SELECT id, provider, health FROM accounts WHERE status = 'connected'"
        ).fetchall()
    for row in rows:
        if _is_dead_account_health(row["health"]):
            continue
        provider = str(row["provider"] or "telegram").strip().lower()
        if provider != "whatsapp" and _resolve_app_gateway(app, "telegram") is None:
            continue
        await start_account_watch(app, row["id"])


async def stop_inbox_watches(app) -> None:
    tasks = list(app.state.inbox_tasks.values())
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    app.state.inbox_tasks.clear()
    for gw in (
        getattr(app.state, "gateway", None),
        getattr(app.state, "wa_gateway", None),
    ):
        if gw is not None and hasattr(gw, "close_all"):
            try:
                await gw.close_all()
            except Exception:
                pass
    app.state.gateway = None


async def sync_live_tg_read_receipts_once(
    app: FastAPI, *, account_id: int | None = None, limit: int = 60
) -> dict:
    gateway = _resolve_app_gateway(app, "telegram")
    if gateway is None or not hasattr(gateway, "load_inbox"):
        return {"ok": False, "synced_accounts": 0, "updated_recipients": 0}
    conn = app.state.conn
    with locked_conn(conn):
        if account_id is not None:
            acc_rows = conn.execute(
                "SELECT id, session_path, provider, status, health FROM accounts WHERE id = ? AND status = 'connected'",
                (int(account_id),),
            ).fetchall()
        else:
            acc_rows = conn.execute(
                "SELECT id, session_path, provider, status, health FROM accounts WHERE status = 'connected' ORDER BY id"
            ).fetchall()
    tg_accs = [
        r
        for r in acc_rows
        if str(r["provider"] or "telegram").strip().lower() == "telegram"
        and not _is_dead_account_health(r["health"])
        and not str(r["session_path"] or "").startswith("data/sessions/demo")
    ]
    synced_accs = 0
    updated_recs = 0
    for acc in tg_accs:
        aid = int(acc["id"])
        try:
            people = await gateway.load_inbox(acc["session_path"], limit=limit)
        except Exception:
            continue
        if not isinstance(people, list):
            continue
        try:
            res = await _sync_and_publish_dialogs(
                app, conn, aid, people[:limit], crm_only=True
            )
            synced_accs += 1
            if isinstance(res, dict):
                updated_recs += int(res.get("updated_recipients") or 0)
            else:
                updated_recs += int(res or 0)
        except Exception:
            continue
    return {
        "ok": True,
        "synced_accounts": synced_accs,
        "updated_recipients": updated_recs,
    }


async def sync_crm_dialog_messages_backfill_once(
    app: FastAPI, *, account_id: int | None = None, max_dialogs: int = 25
) -> dict:
    gateway = _resolve_app_gateway(app, "telegram")
    if gateway is None or not hasattr(gateway, "fetch_messages"):
        return {"ok": False, "checked": 0, "backfilled": 0}
    conn = getattr(app.state, "conn", None)
    if conn is None:
        return {"ok": False, "checked": 0, "backfilled": 0}
    cap_dialogs = max(0, int(max_dialogs or 0))
    if cap_dialogs <= 0:
        return {"ok": True, "checked": 0, "backfilled": 0}

    attempted = getattr(app.state, "backfill_attempted_at", None)
    if not isinstance(attempted, dict):
        attempted = {}
        app.state.backfill_attempted_at = attempted

    now_mono = time.monotonic()
    if len(attempted) > 4096:
        expired = [k for k, ts in attempted.items() if (now_mono - ts) >= 600.0]
        for k in expired:
            attempted.pop(k, None)
        if len(attempted) > 4096:
            attempted.clear()

    is_real_or_none_gw = isinstance(gateway, (type(None), TelethonGateway))
    target_aid = int(account_id) if (account_id is not None and int(account_id) != 0) else None

    def _select_backfill_candidates(c) -> list[dict]:
        if target_aid is not None:
            acc_rows = c.execute(
                "SELECT id, session_path, provider, status, health FROM accounts WHERE id = ? AND status = 'connected'",
                (target_aid,),
            ).fetchall()
        else:
            acc_rows = c.execute(
                "SELECT id, session_path, provider, status, health FROM accounts WHERE status = 'connected' ORDER BY id"
            ).fetchall()
        readable_acc_ids = {
            int(r["id"])
            for r in acc_rows
            if str(r["provider"] or "telegram").strip().lower() == "telegram"
            and not _is_dead_account_health(r["health"])
            and not (
                is_real_or_none_gw
                and str(r["session_path"] or "").startswith("data/sessions/demo")
            )
        }
        if not readable_acc_ids:
            return []

        all_rows = _query_inbox_rows(c, account_id=target_aid or 0, crm_only=False)
        crm_rows = _filter_crm_only_rows(c, all_rows)
        eligible_rows = [
            r
            for r in crm_rows
            if r.get("id") is not None and int(r.get("account_id") or 0) in readable_acc_ids
        ]
        if not eligible_rows:
            return []

        msg_stats: dict[int, tuple[int, str]] = {}
        cand_dids = [int(r["id"]) for r in eligible_rows]
        for start in range(0, len(cand_dids), 400):
            chunk = cand_dids[start : start + 400]
            ph = ",".join("?" for _ in chunk)
            for mr in c.execute(
                f"""
                SELECT dialog_id,
                       COUNT(*) AS msg_cnt,
                       MAX(REPLACE(COALESCE(sent_at, ''), ' ', 'T')) AS max_msg_at
                FROM messages
                WHERE dialog_id IN ({ph})
                GROUP BY dialog_id
                """,
                chunk,
            ).fetchall():
                msg_stats[int(mr["dialog_id"])] = (
                    int(mr["msg_cnt"] or 0),
                    str(mr["max_msg_at"] or ""),
                )

        picked: list[dict] = []
        for r in eligible_rows:
            did = int(r["id"])
            aid = int(r["account_id"])
            msg_cnt, raw_max_msg_at = msg_stats.get(did, (0, ""))
            norm_max_msg_at = _norm_ts(raw_max_msg_at)
            raw_last_at = str(r.get("last_message_at") or "").strip()
            norm_last_at = _norm_ts(raw_last_at)
            has_snap = bool(
                str(r.get("last_message_preview") or "").strip() or norm_last_at
            )
            needs_backfill = (msg_cnt == 0 and has_snap) or (
                bool(norm_last_at) and norm_last_at > norm_max_msg_at
            )
            if not needs_backfill:
                continue
            prev_attempt = attempted.get((did, norm_last_at))
            if prev_attempt is not None and (now_mono - prev_attempt) < 600.0:
                continue
            picked.append(
                {
                    "id": did,
                    "account_id": aid,
                    "msg_cnt": msg_cnt,
                    "norm_max_msg_at": norm_max_msg_at,
                    "norm_last_at": norm_last_at,
                }
            )
        picked.sort(key=lambda item: (item["norm_last_at"], item["id"]), reverse=True)
        return picked[:cap_dialogs]

    try:
        candidates = await _run_db_sync(conn, _select_backfill_candidates, conn)
    except LookupError:
        return {"ok": True, "checked": 0, "backfilled": 0}

    checked = 0
    backfilled = 0
    hub = getattr(app.state, "inbox_hub", None)
    for item in candidates:
        did = int(item["id"])
        aid = int(item["account_id"])
        norm_last_at = item["norm_last_at"]
        checked += 1
        attempted[(did, norm_last_at)] = time.monotonic()
        before_cnt = int(item["msg_cnt"])
        before_max = item["norm_max_msg_at"]
        try:
            await get_thread(conn, did, gateway, 50)
        except Exception:
            continue

        def _reload_dialog_state(c):
            row_stats = c.execute(
                "SELECT COUNT(*) AS cnt, MAX(REPLACE(COALESCE(sent_at, ''), ' ', 'T')) AS max_at FROM messages WHERE dialog_id = ?",
                (did,),
            ).fetchone()
            after_cnt = int(row_stats["cnt"] or 0) if row_stats is not None else 0
            after_max = _norm_ts(row_stats["max_at"]) if row_stats is not None else ""
            dlg_pub = _load_dialog_public(c, did)
            return after_cnt, after_max, dlg_pub

        try:
            after_cnt, after_max, dlg_pub = await _run_db_sync(
                conn, _reload_dialog_state, conn
            )
        except Exception:
            continue
        if after_cnt > before_cnt or (after_max and after_max > before_max):
            backfilled += 1
            if hub is not None and isinstance(dlg_pub, dict):
                hub.publish(aid, dlg_pub)

    return {"ok": True, "checked": checked, "backfilled": backfilled}


async def autopilot_loop(app: FastAPI, interval_seconds: float = 30.0) -> None:
    tick_counter = 0
    read_cursor = 0
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            tick_counter += 1
            with locked_conn(app.state.conn):
                hygiene_healed = _heal_crm_hygiene_safe(app.state.conn)
            healed_dirty = isinstance(hygiene_healed, dict) and (
                int(hygiene_healed.get("quarantined_recipients") or 0)
                + int(hygiene_healed.get("quarantined_leads") or 0)
                + int(hygiene_healed.get("sanitized_leads") or 0)
                > 0
            )
            maybe_coro = run_autopilot_tick(
                app.state.conn,
                gateway=_resolve_app_gateway(app, "telegram"),
                wa_gateway=_resolve_app_gateway(app, "whatsapp"),
            )
            tick_res = (
                await maybe_coro if inspect.isawaitable(maybe_coro) else maybe_coro
            )
            app.state.autopilot_last_tick_at = utc_now()
            dirty = healed_dirty or (
                isinstance(tick_res, dict) and int(tick_res.get("sent") or 0) > 0
            )
            if tick_counter % 10 == 0:
                slo_watchdog_fn = getattr(_outreach_mod, "check_outreach_slo_watchdog", None)
                if callable(slo_watchdog_fn):
                    try:
                        maybe_wd = slo_watchdog_fn(app.state.conn)
                        if inspect.isawaitable(maybe_wd):
                            await maybe_wd
                    except Exception:
                        _runtime_logger.exception("check_outreach_slo_watchdog failed")
            if tick_counter % 2 == 0 and not is_ephemeral_runtime():
                with locked_conn(app.state.conn):
                    tg_rows = app.state.conn.execute(
                        "SELECT id, session_path, provider, health FROM accounts WHERE status = 'connected' ORDER BY id"
                    ).fetchall()
                tg_ids = [
                    int(r["id"])
                    for r in tg_rows
                    if str(r["provider"] or "telegram").strip().lower() == "telegram"
                    and not _is_dead_account_health(r["health"])
                    and not str(r["session_path"] or "").startswith("data/sessions/demo")
                ]
                if tg_ids:
                    aid = tg_ids[read_cursor % len(tg_ids)]
                    read_cursor += 1
                    rr = await sync_live_tg_read_receipts_once(app, account_id=aid, limit=200)
                    if int(rr.get("updated_recipients") or 0) > 0:
                        dirty = True
                    bf = await sync_crm_dialog_messages_backfill_once(
                        app, account_id=aid, max_dialogs=15
                    )
                    if int(bf.get("backfilled") or 0) > 0:
                        dirty = True
            if dirty:
                from app.cloud_persistence import schedule_cloud_backup

                schedule_cloud_backup(app)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.autopilot_error_count = (
                int(getattr(app.state, "autopilot_error_count", 0) or 0) + 1
            )
            app.state.autopilot_last_error = f"{type(exc).__name__}: {exc}"
            _runtime_logger.exception("autopilot_loop tick failed")
            continue


async def start_autopilot_loop(app: FastAPI) -> None:
    with locked_conn(getattr(app.state, "conn", None)):
        enable = should_enable_live_tg_runtime(
            getattr(app.state, "conn", None), getattr(app.state, "gateway", None)
        )
    if not enable and not is_ephemeral_runtime():
        return
    existing = getattr(app.state, "autopilot_task", None)
    if existing is not None and not existing.done():
        return
    interval = float(getattr(app.state, "autopilot_interval", 30.0) or 30.0)
    app.state.autopilot_task = asyncio.create_task(autopilot_loop(app, interval))


async def stop_autopilot_loop(app: FastAPI) -> None:
    task = getattr(app.state, "autopilot_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        app.state.autopilot_task = None


def _schedule_airtable_push_lead(app: FastAPI, lead_id: int) -> None:
    if not should_start_airtable_bg_sync():
        return
    try:
        from app.airtable_sync import get_airtable_config, push_lead_to_airtable

        conn = getattr(app.state, "conn", None)
        with locked_conn(conn):
            cfg = get_airtable_config(conn)
        if not cfg.get("configured"):
            return
    except Exception:
        return

    def _worker() -> None:
        bg_conn = connect()
        try:
            client = getattr(app.state, "airtable_client", None)
            push_lead_to_airtable(bg_conn, int(lead_id), client=client)
        except Exception:
            pass
        finally:
            try:
                bg_conn.close()
            except Exception:
                pass

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_worker))
    except RuntimeError:
        import threading

        threading.Thread(target=_worker, daemon=True).start()


def _run_airtable_sync_once(app: FastAPI) -> None:
    if not should_start_airtable_bg_sync():
        return
    from app.airtable_sync import get_airtable_config, sync_airtable

    bg_conn = connect()
    try:
        cfg = get_airtable_config(bg_conn)
        if not cfg.get("configured"):
            return
        client = getattr(app.state, "airtable_client", None)
        sync_airtable(bg_conn, direction="both", client=client)
    except Exception:
        pass
    finally:
        try:
            bg_conn.close()
        except Exception:
            pass


async def airtable_sync_loop(app: FastAPI, interval_seconds: float = 180.0) -> None:
    try:
        await asyncio.to_thread(_run_airtable_sync_once, app)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await asyncio.to_thread(_run_airtable_sync_once, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


async def start_airtable_sync_loop(app: FastAPI) -> None:
    if not should_start_airtable_bg_sync():
        return
    existing = getattr(app.state, "airtable_sync_task", None)
    if existing is not None and not existing.done():
        return
    interval = float(getattr(app.state, "airtable_sync_interval", 180.0) or 180.0)
    app.state.airtable_sync_task = asyncio.create_task(
        airtable_sync_loop(app, interval)
    )


async def stop_airtable_sync_loop(app: FastAPI) -> None:
    task = getattr(app.state, "airtable_sync_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        app.state.airtable_sync_task = None


async def _tg_lease_heartbeat_loop(app: FastAPI) -> None:
    from app.cloud_persistence import (
        check_tg_runtime_lease_owned,
        release_tg_local_lock,
        wait_and_acquire_tg_runtime_lease,
        write_tg_runtime_lease,
    )

    is_vps_canonical = os.getenv("CRMTG_VPS_RUNTIME", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    while True:
        try:
            await asyncio.sleep(20.0)
            owned = await asyncio.to_thread(check_tg_runtime_lease_owned, None)
            if not owned:
                # Strictly close all local MTProto sessions BEFORE attempting any re-acquisition
                # so split-brain window is 0s even on canonical VPS host.
                app.state.tg_lease_ready = False
                await stop_autopilot_loop(app)
                await stop_inbox_watches(app)
                app.state.gateway = None
                await asyncio.to_thread(release_tg_local_lock)
                if not is_vps_canonical:
                    return
                reacquired = bool(
                    await asyncio.to_thread(
                        wait_and_acquire_tg_runtime_lease,
                        None,
                        max_wait_seconds=10.0,
                    )
                )
                if reacquired:
                    app.state.tg_lease_ready = True
                    await start_connected_watches(app)
                    await start_autopilot_loop(app)
                continue
            await asyncio.to_thread(
                write_tg_runtime_lease, released=False, conn=None
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _runtime_logger.warning("tg_lease_heartbeat_loop error: %s", exc)
            continue


def create_app(conn=None, gateway=None, wa_gateway=None) -> FastAPI:
    load_env_file()

    async def _leased_tg_runtime_boot(app: FastAPI) -> None:
        from app.cloud_persistence import (
            _resolve_r2_config,
            acquire_tg_local_lock,
            schedule_cloud_backup,
            wait_and_acquire_tg_runtime_lease,
        )

        is_vps_canonical = os.getenv("CRMTG_VPS_RUNTIME", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        has_r2 = _resolve_r2_config(getattr(app.state, "conn", None)) is not None
        acquired = True
        if has_r2:
            try:
                acquired = bool(
                    await asyncio.to_thread(wait_and_acquire_tg_runtime_lease, None)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _runtime_logger.error("Failed acquiring R2 TG lease on boot: %s", exc)
                acquired = False
        else:
            try:
                acquired = bool(await asyncio.to_thread(acquire_tg_local_lock))
            except Exception as exc:
                _runtime_logger.error("Failed acquiring local TG OS lock on boot: %s", exc)
                acquired = False
        if not acquired:
            app.state.tg_lease_ready = False
            app.state.gateway = None
            _runtime_logger.warning(
                "Another host or local process holds active TG runtime lease/lock; refusing to start local TG sessions to prevent AUTH_KEY_DUPLICATED."
            )
            if is_vps_canonical:
                app.state.tg_lease_heartbeat_task = asyncio.create_task(
                    _tg_lease_heartbeat_loop(app)
                )
            return
        app.state.tg_lease_ready = True
        if getattr(app.state, "_needs_initial_session_backup", False):
            app.state._needs_initial_session_backup = False
            schedule_cloud_backup(app, delay_seconds=3.0)
        app.state.tg_lease_heartbeat_task = asyncio.create_task(
            _tg_lease_heartbeat_loop(app)
        )
        await start_connected_watches(app)
        await start_autopilot_loop(app)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        live_tg_enabled = False
        ephemeral = is_ephemeral_runtime()
        if ephemeral or gateway is not None:
            if os.getenv("DISABLE_TG_WATCHES") != "1":
                if ephemeral or not isinstance(gateway, (type(None), TelethonGateway)):
                    await start_connected_watches(app)
                else:
                    asyncio.create_task(start_connected_watches(app))
                await start_autopilot_loop(app)
        elif should_enable_live_tg_runtime(app.state.conn, gateway):
            from app.cloud_persistence import _resolve_r2_config

            live_tg_enabled = True
            if _resolve_r2_config(app.state.conn) is not None:
                app.state.tg_lease_ready = False
            app.state.tg_lease_boot_task = asyncio.create_task(
                _leased_tg_runtime_boot(app)
            )
        if not live_tg_enabled and getattr(app.state, "_needs_initial_session_backup", False):
            app.state._needs_initial_session_backup = False
            from app.cloud_persistence import schedule_cloud_backup

            schedule_cloud_backup(app, delay_seconds=3.0)
        await start_airtable_sync_loop(app)
        try:
            yield
        finally:
            boot_t = getattr(app.state, "tg_lease_boot_task", None)
            if boot_t is not None:
                boot_t.cancel()
                try:
                    await boot_t
                except (asyncio.CancelledError, Exception):
                    pass
                app.state.tg_lease_boot_task = None
            await stop_airtable_sync_loop(app)
            await stop_autopilot_loop(app)
            await stop_inbox_watches(app)
            hb_t = getattr(app.state, "tg_lease_heartbeat_task", None)
            if hb_t is not None:
                hb_t.cancel()
                try:
                    await hb_t
                except (asyncio.CancelledError, Exception):
                    pass
                app.state.tg_lease_heartbeat_task = None
            for b_attr in ("_cloud_backup_task", "cloud_backup_task"):
                bt = getattr(app.state, b_attr, None)
                if bt is not None and not bt.done():
                    try:
                        await asyncio.wait_for(bt, timeout=10.0)
                    except (asyncio.CancelledError, Exception):
                        pass
            try:
                obt = getattr(_outreach_mod, "_CLOUD_BACKUP_BG_TASK", None)
                if obt is not None and not obt.done():
                    await asyncio.wait_for(obt, timeout=10.0)
            except (asyncio.CancelledError, Exception):
                pass
            try:
                from app.security import close_auth_connection

                close_auth_connection(app)
            except Exception:
                pass
            if live_tg_enabled and not is_ephemeral_runtime():
                if (
                    os.getenv("CLOUD_BACKUP_ENABLED", "1").strip().lower()
                    not in ("0", "false", "no", "off")
                ):
                    try:
                        from app.cloud_persistence import release_tg_runtime_lease

                        await asyncio.to_thread(release_tg_runtime_lease, None)
                    except Exception:
                        pass
                else:
                    try:
                        from app.cloud_persistence import release_tg_local_lock

                        await asyncio.to_thread(release_tg_local_lock)
                    except Exception:
                        pass

    app = FastAPI(title="CRM Telegram", lifespan=lifespan)
    cloud_backup_enabled = (
        os.getenv("CLOUD_BACKUP_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    if (
        conn is None
        and not is_ephemeral_runtime()
        and cloud_backup_enabled
    ):
        try:
            from app.cloud_persistence import restore_from_cloud

            restore_from_cloud()
        except Exception:
            pass
    if conn is None:
        conn = connect()
    init_db(conn)
    ensure_funnel_columns(conn)
    needs_initial_session_backup = False
    if (
        not is_ephemeral_runtime()
        and cloud_backup_enabled
    ):
        restore_disk_sessions(conn)
        if os.getenv("BOOTSTRAP_PROD_SNAPSHOT", "").strip().lower() in ("1", "true", "yes"):
            from app.db import bootstrap_prod_snapshot

            bootstrap_prod_snapshot(conn)
        try:
            from app.cloud_persistence import _resolve_r2_config

            if _resolve_r2_config(conn) is not None:
                has_str = conn.execute(
                    "SELECT 1 FROM accounts WHERE session_string IS NOT NULL LIMIT 1"
                ).fetchone()
                has_mig = conn.execute(
                    "SELECT 1 FROM kv_settings WHERE key IN ('freeze_legacy_1300_and_scrub_creatoros_v1', 'repair_truncated_step1_and_freeze_yesterday_v2', 'purge_offline_non_icp_contacts_v3') LIMIT 1"
                ).fetchone()
                if has_str is not None or has_mig is not None:
                    needs_initial_session_backup = True
        except Exception:
            pass
    with locked_conn(conn):
        _heal_crm_hygiene_safe(conn)
    app.state.conn = conn
    app.state.gateway = gateway
    if wa_gateway is not None:
        app.state.wa_gateway = wa_gateway
    else:
        gw_db_path = None
        try:
            for db_row in conn.execute("PRAGMA database_list").fetchall():
                if str(db_row[1] or "") == "main" and str(db_row[2] or "").strip():
                    gw_db_path = str(db_row[2]).strip()
                    break
        except Exception:
            gw_db_path = None
        app.state.wa_gateway = WhatsAppGateway(db_path=gw_db_path)
    app.state.inbox_hub = InboxHub()
    app.state.inbox_tasks = {}
    app.state.inbox_synced = set()
    app.state.inbox_sync_lock = asyncio.Lock()
    app.state.backfill_attempted_at = {}
    app.state.tg_lease_ready = True
    app.state.tg_lease_boot_task = None
    app.state.tg_lease_heartbeat_task = None
    app.state.autopilot_error_count = 0
    app.state.autopilot_last_error = None
    app.state.autopilot_last_tick_at = None
    app.state._needs_initial_session_backup = needs_initial_session_backup
    if needs_initial_session_backup:
        from app.cloud_persistence import schedule_cloud_backup

        schedule_cloud_backup(app, delay_seconds=3.0)

    @app.middleware("http")
    async def _cloud_persistence_middleware(request: Request, call_next):
        response = await call_next(request)
        if (
            request.method in ("POST", "PATCH", "PUT", "DELETE")
            and response.status_code < 400
            and not request.url.path.startswith("/api/test")
            and not request.url.path.startswith("/api/auth/")
        ):
            from app.cloud_persistence import schedule_cloud_backup

            schedule_cloud_backup(request.app)
        return response

    from app.security import (
        has_valid_bot_webhook_secret,
        has_valid_mcp_api_key,
        is_auth_enforced,
        register_security_middleware,
    )

    register_security_middleware(app)

    def _resolve_db_file_path(conn_obj) -> str | None:
        if conn_obj is None:
            return None
        try:
            with locked_conn(conn_obj):
                rows = conn_obj.execute("PRAGMA database_list").fetchall()
            for r in rows:
                name = r[1] if not isinstance(r, dict) else r.get("name")
                file_p = r[2] if not isinstance(r, dict) else r.get("file")
                if name == "main" and file_p and str(file_p) != ":memory:":
                    return str(file_p)
        except Exception:
            return None
        return None

    def _call_isolated_db(conn_obj, func, *args, **kwargs):
        db_file = _resolve_db_file_path(conn_obj)
        if not db_file:
            return _call(func, conn_obj, *args, **kwargs)
        iso_conn = connect(db_file)
        try:
            return _call(func, iso_conn, *args, **kwargs)
        finally:
            try:
                iso_conn.close()
            except Exception:
                pass

    @app.get("/api/health")
    def api_health(request: Request, deep: int = 0):
        conn_obj = getattr(request.app.state, "conn", None)
        db_ok = False
        active_accounts = 0
        healthy_accounts = 0
        account_pool: dict = {
            "connected_accounts": 0,
            "active_accounts": 0,
            "daily_pool": 0,
            "sent_today": 0,
            "remaining_today": 0,
            "frozen_accounts": 0,
            "cooldown_accounts": 0,
            "offline_accounts": 0,
            "dead_accounts": 0,
        }
        running_campaigns = 0
        pending_step1_total = 0
        reply_alert_guard_ok = True
        false_reply_alerts_24h = 0
        hygiene_audit: dict = {"ok": True}
        leadgen_readiness: dict = {"ok": True, "missing_components": []}
        try:
            if conn_obj is not None:
                with locked_conn(conn_obj):
                    conn_obj.execute("SELECT 1").fetchone()
                    db_ok = True
                    if deep:
                        from app.accounts import compute_account_pool_summary

                        account_pool = compute_account_pool_summary(conn_obj)
                        active_accounts = int(account_pool["connected_accounts"])
                        healthy_accounts = int(account_pool["active_accounts"])
                        rc_row = conn_obj.execute(
                            "SELECT COUNT(*) AS cnt FROM campaigns WHERE status = 'running'"
                        ).fetchone()
                        if rc_row is not None:
                            running_campaigns = int(rc_row["cnt"] or 0)
                        p1_row = conn_obj.execute(
                            """
                            SELECT COUNT(*) AS cnt
                            FROM campaign_recipients cr
                            JOIN campaigns c ON c.id = cr.campaign_id
                            WHERE c.status = 'running'
                              AND cr.status = 'pending'
                              AND COALESCE(cr.current_step, 0) = 0
                            """
                        ).fetchone()
                        if p1_row is not None:
                            pending_step1_total = int(p1_row["cnt"] or 0)
                        from app.digest import audit_reply_notifications

                        notif_audit = audit_reply_notifications(conn_obj, since_hours=24)
                        false_reply_alerts_24h = int(notif_audit["false_notification_count"])
                        reply_alert_guard_ok = false_reply_alerts_24h == 0
                        hygiene_audit = _audit_crm_hygiene_safe(conn_obj)
                        leadgen_readiness = _audit_server_leadgen_readiness_safe(conn_obj)
        except Exception as exc:
            return Response(
                content=json.dumps(
                    {"ok": False, "status": "unhealthy", "db_ok": False, "error": str(exc)}
                ),
                status_code=503,
                media_type="application/json",
            )
        payload = {"ok": True, "status": "healthy", "db_ok": db_ok}
        if deep:
            from app.cloud_persistence import INSTANCE_ID, get_last_seen_tg_lease_owner

            ap_task = getattr(request.app.state, "autopilot_task", None)
            inbox_tasks = getattr(request.app.state, "inbox_tasks", None) or {}
            active_watches = sum(
                1 for t in inbox_tasks.values() if t is not None and not t.done()
            )
            gw_obj = getattr(request.app.state, "gateway", None)
            if gw_obj is not None and hasattr(gw_obj, "get_telemetry"):
                gw_tel = gw_obj.get_telemetry()
            else:
                gw_tel = {
                    "disconnects_1h_by_account": {},
                    "disconnects_1h_total": 0,
                    "leaked_tasks_1h": 0,
                    "connected_clients": 0,
                }
            tg_lease_ready = bool(getattr(request.app.state, "tg_lease_ready", True))
            tg_lease_owner = get_last_seen_tg_lease_owner() or (
                INSTANCE_ID if tg_lease_ready else None
            )
            payload.update(
                {
                    "tg_lease_ready": tg_lease_ready,
                    "tg_lease_owner": tg_lease_owner,
                    "active_watches": active_watches,
                    "disconnects_1h_by_account": gw_tel["disconnects_1h_by_account"],
                    "disconnects_1h_total": int(gw_tel["disconnects_1h_total"]),
                    "leaked_tasks_1h": int(gw_tel["leaked_tasks_1h"]),
                    "autopilot_running": bool(ap_task is not None and not ap_task.done()),
                    "autopilot_error_count": int(
                        getattr(request.app.state, "autopilot_error_count", 0) or 0
                    ),
                    "autopilot_last_error": getattr(
                        request.app.state, "autopilot_last_error", None
                    ),
                    "autopilot_last_tick_at": getattr(
                        request.app.state, "autopilot_last_tick_at", None
                    ),
                    "active_accounts": active_accounts,
                    "healthy_accounts": healthy_accounts,
                    "send_eligible_accounts": int(account_pool["active_accounts"]),
                    "daily_pool": int(account_pool["daily_pool"]),
                    "account_pool": account_pool,
                    "running_campaigns": running_campaigns,
                    "pending_step1_total": pending_step1_total,
                    "reply_alert_guard_ok": reply_alert_guard_ok,
                    "false_reply_alerts_24h": false_reply_alerts_24h,
                    "icp_hygiene_ok": bool(hygiene_audit.get("ok", True)),
                    "active_junk_recipients": int(
                        hygiene_audit.get("active_junk_recipient_count", 0)
                    ),
                    "active_junk_leads": int(
                        hygiene_audit.get("active_junk_lead_count", 0)
                    ),
                    "contactless_leads": int(
                        hygiene_audit.get("contactless_lead_count", 0)
                    ),
                    "leadgen_ready": bool(leadgen_readiness.get("ok", True)),
                    "leadgen_readiness": leadgen_readiness,
                }
            )
        return payload

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots_txt():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    register_tg_web_routes(app)

    @app.get("/design/tokens.css")
    def design_tokens():
        return FileResponse(ROOT / "replica" / "design" / "tokens.css", media_type="text/css")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        default_view_mode = os.getenv("CRM_VIEW_MODE") or (
            "all" if os.getenv("BOT_TOKEN") == "e2e-not-a-secret" else "sheet"
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {"request": request, "default_view_mode": default_view_mode},
        )

    @app.get("/landing", response_class=HTMLResponse)
    def landing(request: Request):
        return templates.TemplateResponse(request, "landing.html", {"request": request})

    @app.post("/api/accounts/start")
    async def accounts_start(body: StartIn, request: Request):
        gateway = _gateway_for_provider(request, body.provider)
        proxy_url = body.proxy_url if body.proxy_url is not None else body.proxy
        access_scope = body.access_scope or body.access or "workspace"
        return await _call_async(
            start_login,
            request.app.state.conn,
            body.phone,
            gateway,
            proxy_url=proxy_url,
            access_scope=access_scope,
            provider=body.provider,
            auth_mode=body.auth_mode,
        )

    @app.post("/api/accounts/import-session")
    async def accounts_import_session(request: Request):
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Некорректный JSON") from exc
        provider = str((payload if isinstance(payload, dict) else {}).get("provider") or "telegram").strip().lower()
        gw = _resolve_app_gateway(request.app, provider)
        imported = _call(
            import_session_accounts,
            request.app.state.conn,
            gw,
            payload,
        )
        for acc in imported:
            if isinstance(acc, dict) and acc.get("id") and acc.get("status") == "connected":
                await start_account_watch(request.app, int(acc["id"]))
        return {"imported": imported, "count": len(imported)}

    @app.patch("/api/accounts/{account_id}/settings")
    async def accounts_settings_patch(
        account_id: int, body: AccountSettingsPatch, request: Request
    ):
        fields = body.model_dump(exclude_unset=True)
        updated = _call(
            update_account_settings,
            request.app.state.conn,
            account_id,
            proxy_url=body.proxy_url,
            access_scope=body.access_scope,
            daily_limit=body.daily_limit,
            has_proxy=("proxy_url" in fields),
        )
        with locked_conn(request.app.state.conn):
            row = request.app.state.conn.execute(
                "SELECT provider, session_path FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if row and row["session_path"]:
            prov = str(row["provider"] or "telegram").strip().lower()
            if prov == "whatsapp":
                wa_gw = _resolve_app_gateway(request.app, "whatsapp")
                if wa_gw is not None and hasattr(wa_gw, "apply_proxy"):
                    try:
                        await wa_gw.apply_proxy(row["session_path"], updated.get("proxy_url"))
                    except GatewayError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
            else:
                gw = getattr(request.app.state, "gateway", None)
                if gw is not None:
                    try:
                        if hasattr(gw, "apply_proxy"):
                            await gw.apply_proxy(row["session_path"], updated.get("proxy_url"))
                        elif hasattr(gw, "set_proxy"):
                            gw.set_proxy(row["session_path"], updated.get("proxy_url"))
                    except Exception:
                        pass
        return updated

    @app.post("/api/accounts/{account_id}/code")
    async def accounts_code(account_id: int, body: CodeIn, request: Request):
        gateway = _gateway_for_account(request, account_id)
        result = await _call_async(
            confirm_login, request.app.state.conn, account_id, body.code, gateway
        )
        if result.get("status") == "connected":
            await start_account_watch(request.app, account_id)
        return result

    @app.post("/api/accounts/{account_id}/password")
    async def accounts_password(account_id: int, body: PasswordIn, request: Request):
        gateway = _gateway_for_account(request, account_id)
        result = await _call_async(
            confirm_password, request.app.state.conn, account_id, body.password, gateway
        )
        if result.get("status") == "connected":
            await start_account_watch(request.app, account_id)
        return result

    @app.get("/api/accounts")
    def accounts_list(request: Request):
        with locked_conn(request.app.state.conn):
            items = list_accounts(request.app.state.conn)
            caller = None
            check_mask = is_auth_enforced() and not has_valid_mcp_api_key(request)
            if check_mask:
                from app.crm_auth import resolve_request_crm_user

                caller = resolve_request_crm_user(request.app.state.conn, request)
        if check_mask:
            from app.accounts import mask_proxy_url

            if caller is None or caller.get("role") != "admin":
                items = [
                    {**acc, "proxy_url": mask_proxy_url(acc.get("proxy_url"))}
                    for acc in items
                ]
        return items

    async def _cleanup_deleted_account_runtime(app, account_id: int, session_path: str | None) -> None:
        task = app.state.inbox_tasks.pop(account_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        app.state.inbox_synced.discard(account_id)
        if session_path:
            for gw in (getattr(app.state, "gateway", None), getattr(app.state, "wa_gateway", None)):
                if gw is not None and hasattr(gw, "_close"):
                    try:
                        await gw._close(session_path)
                    except Exception:
                        pass

    @app.delete("/api/accounts/{account_id}")
    async def accounts_delete(account_id: int, request: Request):
        with locked_conn(request.app.state.conn):
            row = request.app.state.conn.execute(
                "SELECT session_path FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        pre_path = row["session_path"] if row else None
        await _cleanup_deleted_account_runtime(request.app, account_id, pre_path)
        res = _call(
            delete_account,
            request.app.state.conn,
            account_id,
            request.app.state.gateway,
        )
        return res

    @app.post("/api/accounts/purge-dead")
    async def accounts_purge_dead(request: Request):
        with locked_conn(request.app.state.conn):
            dead_rows = request.app.state.conn.execute(
                """
                SELECT id, session_path FROM accounts
                WHERE status != 'connected'
                   OR COALESCE(health, 'active') IN ('unauthorized', 'banned', 'frozen')
                """
            ).fetchall()
        for r in dead_rows:
            await _cleanup_deleted_account_runtime(
                request.app, int(r["id"]), r["session_path"]
            )
        res = _call(
            purge_dead_accounts,
            request.app.state.conn,
            request.app.state.gateway,
        )
        return res

    @app.post("/api/accounts/check-all")
    async def accounts_check_all(request: Request):
        return await _call_async(
            check_all_accounts,
            request.app.state.conn,
            _resolve_app_gateway(request.app, "telegram"),
            _resolve_app_gateway(request.app, "whatsapp"),
        )

    @app.patch("/api/accounts/{account_id}/limit")
    def accounts_limit_patch(account_id: int, body: AccountLimitPatch, request: Request):
        return _call(
            set_daily_limit, request.app.state.conn, account_id, body.daily_limit
        )

    @app.post("/api/accounts/{account_id}/reconnect")
    async def accounts_reconnect(account_id: int, request: Request):
        gateway = _gateway_for_account(request, account_id)
        result = await _call_async(
            reconnect_account, request.app.state.conn, account_id, gateway
        )
        if isinstance(result, dict) and result.get("status") == "connected":
            request.app.state.inbox_synced.discard(account_id)
            await start_account_watch(request.app, account_id)
        return result

    @app.get("/api/folders")
    def folders_list(request: Request, account_id: int):
        return _call(list_folders, request.app.state.conn, account_id)

    @app.post("/api/folders/pull")
    async def folders_pull(body: FolderPullIn, request: Request):
        gateway = _gateway(request)
        return await _call_async(
            pull_folders, request.app.state.conn, body.account_id, gateway
        )

    @app.patch("/api/folders/{folder_id}")
    def folders_patch(folder_id: int, body: FolderPatch, request: Request):
        return _call(
            update_folder, request.app.state.conn, folder_id, body.sync_enabled
        )

    @app.post("/api/folders/{folder_id}/sync")
    async def folders_sync(folder_id: int, request: Request):
        gateway = _gateway(request)
        return await _call_async(
            sync_folder, request.app.state.conn, folder_id, gateway
        )

    class FolderSyncBatchIn(BaseModel):
        account_id: int
        folder_ids: list[int] | None = None

    @app.post("/api/folders/sync-all")
    async def folders_sync_all(body: FolderSyncBatchIn, request: Request):
        gateway = _gateway(request)
        return await _call_async(
            sync_all_folders,
            request.app.state.conn,
            body.account_id,
            gateway,
            body.folder_ids,
        )

    @app.get("/api/accounts/{account_id}/dialogs")
    async def account_dialogs(
        account_id: int,
        request: Request,
        q: str = "",
        crm_only: bool = False,
    ):
        try:
            with locked_conn(request.app.state.conn):
                account = load_account(request.app.state.conn, account_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Не найдено") from exc
        if account["status"] != "connected":
            raise HTTPException(status_code=400, detail="Аккаунт не подключён")
        gateway = _gateway_for_account(request, account_id)
        try:
            found = await gateway.search_dialogs(account["session_path"], q, limit=30)
        except GatewayError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with locked_conn(request.app.state.conn):
            eligible_found = []
            for item in (found or [])[:30]:
                if not isinstance(item, dict):
                    continue
                pid = item.get("telegram_user_id") if item.get("telegram_user_id") is not None else item.get("peer_id")
                try:
                    pid_int = int(pid) if pid is not None else None
                except (TypeError, ValueError):
                    pid_int = None
                inelig, _ = is_ineligible_reply_notify_peer(
                    request.app.state.conn,
                    peer_id=pid_int,
                    username=item.get("username"),
                    phone=item.get("phone"),
                    account_id=account_id,
                    is_bot=bool(item.get("is_bot")),
                )
                if inelig:
                    continue
                if crm_only and pid_int is not None and not _person_is_crm_relevant(
                    request.app.state.conn,
                    account_id,
                    pid_int,
                    item.get("username"),
                    item.get("phone"),
                    is_bot=bool(item.get("is_bot")),
                ):
                    continue
                eligible_found.append(item)
            saved = remember_dialogs(request.app.state.conn, account_id, eligible_found)
            sync_dialog_read_receipts(request.app.state.conn, account_id, eligible_found)
            if crm_only:
                saved = _filter_crm_only_rows(request.app.state.conn, saved)
        return saved

    @app.post("/api/leads")
    async def leads_create(body: LeadIn, request: Request):
        conn = request.app.state.conn
        if body.telegram_user_id is not None and body.account_id is not None:
            created = _call(
                add_lead_from_dialog,
                conn,
                body.account_id,
                body.telegram_user_id,
                body.username,
                body.name,
                phone=body.phone,
                provider=body.provider,
                amount=body.amount,
                currency=body.currency,
                status=body.status,
                note=body.note,
                next_step=body.next_step,
                description=body.description,
                email=body.email,
                url=body.url,
                company=body.company,
                niche=body.niche,
                offer_type=body.offer_type,
                offer_details=body.offer_details,
                monthly_value=body.monthly_value,
                lead_source=body.lead_source,
                next_follow_up=body.next_follow_up,
                last_contacted=body.last_contacted,
                airtable_record_id=body.airtable_record_id,
            )
        else:
            created = _call(
                create_lead,
                conn,
                account_id=body.account_id,
                telegram_user_id=body.telegram_user_id,
                username=body.username,
                name=body.name,
                phone=body.phone,
                status=body.status,
                note=body.note,
                next_step=body.next_step,
                description=body.description,
                email=body.email,
                url=body.url,
                provider=body.provider,
                amount=body.amount,
                currency=body.currency,
                company=body.company,
                niche=body.niche,
                offer_type=body.offer_type,
                offer_details=body.offer_details,
                monthly_value=body.monthly_value,
                lead_source=body.lead_source,
                next_follow_up=body.next_follow_up,
                last_contacted=body.last_contacted,
                airtable_record_id=body.airtable_record_id,
            )
        if isinstance(created, dict) and created.get("id"):
            _schedule_airtable_push_lead(request.app, int(created["id"]))
        return created

    @app.get("/api/leads")
    async def leads_search(
        request: Request,
        q: str = "",
        status: str = "",
        folder_id: int | None = None,
        provider: str = "",
    ):
        return _call(
            search_leads,
            request.app.state.conn,
            q,
            status or None,
            folder_id,
            provider=provider or None,
        )

    class LeadBulkIn(BaseModel):
        lead_ids: list[int]
        action: str
        status: str | None = None
        folder_id: int | str | None = None
        amount: int | float | str | None = None
        currency: str | None = None
        campaign_id: int | None = None
        value: str | int | float | None = None

    class LeadMergeIn(BaseModel):
        source_lead_id: int
        target_lead_id: int

    @app.post("/api/leads/bulk")
    def leads_bulk(body: LeadBulkIn, request: Request):
        from app.leads import bulk_leads_action

        return _call(
            bulk_leads_action,
            request.app.state.conn,
            body.lead_ids,
            body.action,
            status=body.status,
            folder_id=body.folder_id,
            amount=body.amount,
            currency=body.currency,
            campaign_id=body.campaign_id,
            value=body.value,
        )

    @app.get("/api/leads/duplicates")
    def leads_duplicates(request: Request):
        from app.leads import find_duplicate_leads

        return _call(find_duplicate_leads, request.app.state.conn)

    @app.post("/api/leads/merge")
    def leads_merge(body: LeadMergeIn, request: Request):
        from app.leads import merge_leads

        return _call(
            merge_leads,
            request.app.state.conn,
            body.source_lead_id,
            body.target_lead_id,
        )

    @app.get("/api/leads/export.csv")
    def leads_export_csv(
        request: Request,
        q: str = "",
        status: str = "",
        folder_id: int | None = None,
        provider: str = "",
    ):
        from fastapi.responses import Response
        from app.leads import export_leads_csv

        csv_text = _call(
            export_leads_csv,
            request.app.state.conn,
            q=q,
            status=status or None,
            folder_id=folder_id,
            provider=provider or None,
        )
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="leads_export.csv"'
            },
        )

    @app.get("/api/views")
    def views_list(request: Request):
        return _call(list_views, request.app.state.conn)

    @app.post("/api/views")
    def views_save(body: ViewIn, request: Request):
        return _call(
            save_view,
            request.app.state.conn,
            body.name,
            status=body.status,
            folder_id=body.folder_id,
        )

    class SnippetIn(BaseModel):
        shortcut: str
        title: str
        body: str

    @app.get("/api/snippets")
    def snippets_list(request: Request):
        return _call(list_snippets, request.app.state.conn)

    @app.post("/api/snippets")
    def snippets_create(body: SnippetIn, request: Request):
        return _call(
            create_snippet,
            request.app.state.conn,
            body.shortcut,
            body.title,
            body.body,
        )

    @app.delete("/api/snippets/{snippet_id}")
    def snippets_delete(snippet_id: int, request: Request):
        return _call(delete_snippet, request.app.state.conn, snippet_id)

    @app.get("/api/leads/{lead_id}")
    async def leads_get(lead_id: int, request: Request):
        return _call(get_lead, request.app.state.conn, lead_id)

    @app.get("/api/leads/{lead_id}/card")
    async def leads_card(lead_id: int, request: Request):
        return _call(lead_card, request.app.state.conn, lead_id)

    async def _resolve_and_enrich_client_profile(
        request: Request,
        *,
        lead_id: int | None = None,
        dialog_id: int | None = None,
        refresh: bool = False,
    ) -> dict:
        conn = request.app.state.conn
        try:
            profile = await _run_db_sync(
                conn,
                get_client_full_profile,
                conn,
                lead_id=lead_id,
                dialog_id=dialog_id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Не найдено") from exc
        tg_prof = profile.get("telegram_profile") or {}
        need_live = bool(refresh) or tg_prof.get("source") == "crm_only"

        dialogs_pool = profile.get("dialogs") or []
        gateway = _resolve_app_gateway(request.app, "telegram")
        did_backfill = False
        primary_did = profile.get("primary_dialog_id")
        primary_dlg = (
            next(
                (
                    d
                    for d in dialogs_pool
                    if d.get("id") is not None and int(d["id"]) == int(primary_did)
                ),
                None,
            )
            if primary_did is not None
            else None
        )
        if (
            primary_dlg is not None
            and gateway is not None
            and hasattr(gateway, "fetch_messages")
            and str(primary_dlg.get("provider") or "telegram").strip().lower() == "telegram"
            and str(primary_dlg.get("account_status") or "").strip().lower() == "connected"
            and not _is_dead_account_health(primary_dlg.get("account_health"))
            and not (
                isinstance(gateway, (type(None), TelethonGateway))
                and str(primary_dlg.get("account_session_path") or "").startswith("data/sessions/demo")
            )
        ):
            msg_cnt = int(primary_dlg.get("msg_count") or 0)
            last_at_norm = _norm_ts(primary_dlg.get("last_message_at"))
            has_snap = bool(
                str(primary_dlg.get("last_message_preview") or "").strip() or last_at_norm
            )

            def _load_primary_max_msg_at(c) -> str:
                mr = c.execute(
                    "SELECT MAX(REPLACE(COALESCE(sent_at, ''), ' ', 'T')) AS max_at FROM messages WHERE dialog_id = ?",
                    (int(primary_did),),
                ).fetchone()
                return _norm_ts(mr["max_at"]) if mr is not None else ""

            max_msg_norm = await _run_db_sync(conn, _load_primary_max_msg_at, conn)
            if (msg_cnt == 0 and has_snap) or (
                bool(last_at_norm) and last_at_norm > max_msg_norm
            ):
                try:
                    await get_thread(conn, int(primary_did), gateway, 50)
                    did_backfill = True
                except Exception:
                    did_backfill = False

        if not need_live:
            if did_backfill:
                return await _run_db_sync(
                    conn,
                    get_client_full_profile,
                    conn,
                    lead_id=lead_id,
                    dialog_id=dialog_id,
                )
            return profile

        candidate_acc_ids: list[int] = []
        if tg_prof.get("account_id") is not None:
            candidate_acc_ids.append(int(tg_prof["account_id"]))
        for d in dialogs_pool:
            if d.get("account_id") is not None and int(d["account_id"]) not in candidate_acc_ids:
                candidate_acc_ids.append(int(d["account_id"]))
        if profile.get("lead") and profile["lead"].get("account_id") is not None:
            laid = int(profile["lead"]["account_id"])
            if laid not in candidate_acc_ids:
                candidate_acc_ids.append(laid)

        peer_target = tg_prof.get("peer_id") or tg_prof.get("username")
        if not peer_target:
            if did_backfill:
                return await _run_db_sync(
                    conn,
                    get_client_full_profile,
                    conn,
                    lead_id=lead_id,
                    dialog_id=dialog_id,
                )
            return profile

        def _pick_tg_account(pref_ids: list[int]) -> dict | None:
            checked = list(pref_ids)
            for ar in conn.execute(
                "SELECT id FROM accounts WHERE status = 'connected' ORDER BY id ASC"
            ).fetchall():
                aid_val = int(ar[0])
                if aid_val not in checked:
                    checked.append(aid_val)
            for aid in checked:
                try:
                    acc = load_account(conn, aid)
                except LookupError:
                    continue
                prov = str(acc.get("provider") or "telegram").strip().lower()
                if prov != "telegram" or acc.get("status") != "connected":
                    continue
                if str(acc.get("health") or "active").strip().lower() in ("unauthorized", "banned"):
                    continue
                sess_path = str(acc.get("session_path") or "")
                if sess_path.startswith("data/sessions/demo") and isinstance(
                    getattr(request.app.state, "gateway", None), (type(None), TelethonGateway)
                ):
                    continue
                return acc
            return None

        chosen_acc = await _run_db_sync(conn, _pick_tg_account, candidate_acc_ids)
        fetch_fn = getattr(gateway, "fetch_peer_profile", None) if gateway is not None else None
        if chosen_acc is None or not callable(fetch_fn):
            if did_backfill:
                return await _run_db_sync(
                    conn,
                    get_client_full_profile,
                    conn,
                    lead_id=lead_id,
                    dialog_id=dialog_id,
                )
            return profile

        try:
            fetched = await fetch_fn(
                chosen_acc["session_path"],
                tg_prof.get("peer_id") or tg_prof.get("username"),
                username=tg_prof.get("username"),
            )
        except Exception:
            fetched = None

        if isinstance(fetched, dict):
            stored_peer = fetched.get("peer_id") or tg_prof.get("peer_id")
            if stored_peer is not None:
                await _run_db_sync(
                    conn,
                    upsert_peer_profile,
                    conn,
                    int(chosen_acc["id"]),
                    int(stored_peer),
                    fetched,
                )
            refreshed = await _run_db_sync(
                conn,
                get_client_full_profile,
                conn,
                lead_id=lead_id,
                dialog_id=dialog_id,
            )
            r_tg = refreshed.get("telegram_profile")
            if isinstance(r_tg, dict):
                if r_tg.get("source") == "crm_only":
                    for k in (
                        "bio",
                        "status_text",
                        "is_premium",
                        "is_verified",
                        "is_scam",
                        "is_fake",
                        "common_chats_count",
                        "personal_channel",
                    ):
                        if fetched.get(k) is not None and not r_tg.get(k):
                            r_tg[k] = fetched[k]
                r_tg["source"] = "telegram_live"
            return refreshed
        if did_backfill:
            return await _run_db_sync(
                conn,
                get_client_full_profile,
                conn,
                lead_id=lead_id,
                dialog_id=dialog_id,
            )
        return profile

    @app.get("/api/leads/{lead_id}/profile")
    async def leads_profile(lead_id: int, request: Request, refresh: bool = False):
        return await _resolve_and_enrich_client_profile(
            request, lead_id=lead_id, refresh=bool(refresh)
        )

    @app.get("/api/dialogs/{dialog_id}/profile")
    async def dialogs_profile(dialog_id: int, request: Request, refresh: bool = False):
        return await _resolve_and_enrich_client_profile(
            request, dialog_id=dialog_id, refresh=bool(refresh)
        )

    @app.get("/api/leads/{lead_id}/events")
    async def leads_events_list(lead_id: int, request: Request, limit: int = 200):
        from app.events import list_lead_events

        _call(get_lead, request.app.state.conn, lead_id)
        return _call(list_lead_events, request.app.state.conn, lead_id, limit=limit)

    @app.post("/api/leads/{lead_id}/notes")
    def lead_note_create(lead_id: int, body: NoteIn, request: Request):
        created = _call(add_note, request.app.state.conn, lead_id, body.body)
        _schedule_airtable_push_lead(request.app, int(lead_id))
        return created

    @app.post("/api/leads/{lead_id}/tasks")
    def lead_task_create(lead_id: int, body: TaskIn, request: Request):
        return _call(create_task, request.app.state.conn, lead_id, body.title)

    @app.post("/api/tasks/{task_id}/done")
    def task_done(task_id: int, body: TaskDoneIn, request: Request):
        return _call(set_task_done, request.app.state.conn, task_id, body.done)

    @app.get("/api/property-defs")
    def property_def_list(request: Request, object_type: str | None = None):
        return _call(list_property_defs, request.app.state.conn, object_type)

    @app.post("/api/property-defs")
    def property_def_create(body: PropertyDefIn, request: Request):
        return _call(
            create_property_def,
            request.app.state.conn,
            name=body.name,
            type_=body.type,
            object_type=body.object_type,
        )

    @app.delete("/api/property-defs/{prop_id}")
    def property_def_delete(prop_id: int, request: Request):
        return _call(delete_property_def, request.app.state.conn, prop_id)

    @app.put("/api/leads/{lead_id}/properties/{property_id}")
    def lead_property_set(
        lead_id: int, property_id: int, body: PropertyValueIn, request: Request
    ):
        return _call(
            set_lead_property,
            request.app.state.conn,
            lead_id,
            property_id,
            body.value,
        )

    @app.patch("/api/leads/{lead_id}")
    async def leads_patch(lead_id: int, body: LeadPatch, request: Request):
        fields = body.model_dump(exclude_unset=True)
        updated = _call(update_lead, request.app.state.conn, lead_id, **fields)
        if isinstance(updated, dict) and updated.get("id"):
            _schedule_airtable_push_lead(request.app, int(updated["id"]))
        return updated

    @app.get("/api/pipeline/stages")
    def pipeline_stages_list(request: Request):
        return _call(list_pipeline_stages, request.app.state.conn)

    @app.post("/api/pipeline/stages")
    def pipeline_stages_create(body: PipelineStageIn, request: Request):
        return _call(
            create_pipeline_stage,
            request.app.state.conn,
            key=body.key,
            label=body.label,
            position=body.position,
            color=body.color,
        )

    @app.patch("/api/pipeline/stages/{stage_key}")
    def pipeline_stages_patch(
        stage_key: str, body: PipelineStagePatch, request: Request
    ):
        return _call(
            update_pipeline_stage,
            request.app.state.conn,
            stage_key,
            label=body.label,
            position=body.position,
            color=body.color,
        )

    @app.post("/api/reminders")
    def reminders_create(body: ReminderIn, request: Request):
        return _call(
            create_reminder,
            request.app.state.conn,
            body.lead_id,
            body.due_at,
            body.text,
        )

    @app.get("/api/reminders")
    def reminders_list(request: Request, scope: str = "open"):
        return _call(list_reminders, request.app.state.conn, scope)

    @app.post("/api/reminders/{reminder_id}/done")
    def reminders_done(reminder_id: int, request: Request):
        return _call(complete_reminder, request.app.state.conn, reminder_id)

    @app.post("/api/reminders/{reminder_id}/postpone")
    def reminders_postpone(reminder_id: int, body: PostponeIn, request: Request):
        return _call(
            postpone_reminder, request.app.state.conn, reminder_id, body.due_at
        )

    @app.get("/api/focus/today")
    def focus_today(
        request: Request,
        stale_days: int = 3,
        now: str | None = None,
    ):
        from app.events import get_today_focus

        return _call(
            get_today_focus,
            request.app.state.conn,
            now=now,
            stale_days=stale_days,
        )

    @app.post("/api/digest/send")
    def digest_send(request: Request):
        opener = getattr(request.app.state, "digest_opener", None)
        return _call(send_morning_digest, request.app.state.conn, opener=opener)

    @app.post("/api/digest/callback")
    def digest_callback(body: DigestCallbackIn, request: Request):
        return _call(apply_digest_callback, request.app.state.conn, body.data)

    @app.post("/api/digest/webhook")
    @app.post("/api/bot/webhook")
    async def digest_webhook(request: Request):
        if not has_valid_bot_webhook_secret(request):
            raise HTTPException(status_code=401, detail="Неверный секрет webhook")
        try:
            update = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Неизвестное действие") from exc
        if isinstance(update, dict) and isinstance(update.get("message"), dict) and not update.get("callback_query"):
            opener = getattr(request.app.state, "digest_opener", None) or getattr(request.app.state, "bot_opener", None)
            gateway = getattr(request.app.state, "gateway", None)
            if gateway is None:
                with locked_conn(request.app.state.conn):
                    tg_id, tg_hash = _resolve_tg_creds(request.app.state.conn)
                if tg_id and tg_hash:
                    gateway = _resolve_app_gateway(request.app, "telegram")
            res = await _call_async(
                handle_bot_reply_to_lead,
                request.app.state.conn,
                update,
                gateway=gateway,
                wa_gateway=_resolve_app_gateway(request.app, "whatsapp"),
                opener=opener,
            )
            if res.get("replied"):
                return res
        data = _call(callback_data_from_update, update)
        cb_q = update.get("callback_query") if isinstance(update, dict) else None
        chat_id = None
        if isinstance(cb_q, dict):
            msg = cb_q.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("chat"), dict) and msg["chat"].get("id") is not None:
                chat_id = str(msg["chat"]["id"])
            elif isinstance(cb_q.get("from"), dict) and cb_q["from"].get("id") is not None:
                chat_id = str(cb_q["from"]["id"])
        return _call(apply_digest_callback, request.app.state.conn, data, chat_id=chat_id)

    @app.get("/api/operator")
    def operator_get(request: Request):
        return _call(operator_status, request.app.state.conn)

    @app.post("/api/operator/bot-login")
    def operator_bot_login(request: Request):
        opener = getattr(request.app.state, "bot_opener", None)
        return _call(poll_bot_login, request.app.state.conn, opener)

    @app.post("/api/operator/update")
    async def operator_update(request: Request):
        try:
            update = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Не апдейт") from exc
        return _call(apply_bot_update, request.app.state.conn, update)

    @app.get("/api/inbox")
    async def inbox_list(
        request: Request,
        account_id: int | None = None,
        q: str = "",
        chip: str | None = None,
        provider: str | None = None,
        status: str | None = None,
        with_counts: bool = False,
        crm_only: bool = False,
    ):
        if account_id is None or account_id == 0:
            with locked_conn(request.app.state.conn):
                rows = request.app.state.conn.execute(
                    "SELECT id FROM accounts WHERE status = 'connected'"
                ).fetchall()
            for r in rows:
                await ensure_private_inbox(request.app, int(r["id"]))
        else:
            await ensure_private_inbox(request.app, account_id)
        return _call(
            list_inbox,
            request.app.state.conn,
            account_id,
            q,
            chip,
            provider,
            status,
            with_counts,
            crm_only=bool(crm_only),
        )

    @app.get("/api/inbox/counts")
    async def inbox_counts_get(
        request: Request,
        account_id: int | None = None,
        q: str = "",
        provider: str | None = None,
        status: str | None = None,
        crm_only: bool = False,
    ):
        counts = _call(
            get_inbox_counts,
            request.app.state.conn,
            account_id,
            q,
            provider,
            status,
            crm_only=bool(crm_only),
        )
        return {"counts": counts, **counts}

    @app.post("/api/inbox/dialogs/{dialog_id}/unread")
    @app.post("/api/dialogs/{dialog_id}/unread")
    def inbox_dialog_mark_unread(
        dialog_id: int, request: Request, crm_only: bool = False
    ):
        conn = request.app.state.conn
        out = _call(mark_dialog_unread, conn, dialog_id)
        if isinstance(out, dict):
            aid = out.get("account_id")
            if crm_only:
                with locked_conn(conn):
                    out = {
                        **out,
                        "counts": get_inbox_counts(conn, aid, crm_only=True),
                        "all_counts": get_inbox_counts(conn, None, crm_only=True),
                    }
            if aid is not None:
                request.app.state.inbox_hub.publish(int(aid), out)
        return out

    def _mark_dialog_read_tx(conn, dialog_id: int, crm_only: bool) -> tuple[dict, int | None]:
        row_max = conn.execute(
            "SELECT MAX(telegram_message_id) FROM messages WHERE dialog_id = ? AND direction = 'in'",
            (int(dialog_id),),
        ).fetchone()
        max_in_id = int(row_max[0]) if row_max and row_max[0] is not None else None

        can_fast_crm = (
            crm_only
            and mark_dialog_read is getattr(_inbox_mod, "mark_dialog_read", None)
            and hasattr(_inbox_mod, "ensure_dialog_notify_columns")
            and hasattr(_inbox_mod, "_advance_dialog_read_state")
            and hasattr(_inbox_mod, "_load_dialog_public")
            and hasattr(_inbox_mod, "_query_inbox_rows")
            and hasattr(_inbox_mod, "compute_inbox_counts")
        )
        if can_fast_crm:
            _inbox_mod.ensure_dialog_notify_columns(conn)
            existing = conn.execute("SELECT * FROM dialogs WHERE id = ?", (int(dialog_id),)).fetchone()
            if existing is None:
                raise LookupError("dialog")
            _inbox_mod._advance_dialog_read_state(
                conn, existing, utc_now(), max_id=None, force_bump_turn=True
            )
            conn.commit()
            out = _inbox_mod._load_dialog_public(conn, int(dialog_id))
            all_crm = _inbox_mod._query_inbox_rows(conn, crm_only=True)
            aid_val = int(out["account_id"])
            out["counts"] = _inbox_mod.compute_inbox_counts(
                [r for r in all_crm if int(r.get("account_id") or 0) == aid_val]
            )
            out["all_counts"] = _inbox_mod.compute_inbox_counts(all_crm)
            return out, max_in_id

        out = mark_dialog_read(conn, dialog_id)
        if isinstance(out, dict) and crm_only:
            aid_val = out.get("account_id")
            if hasattr(_inbox_mod, "_query_inbox_rows") and hasattr(_inbox_mod, "compute_inbox_counts"):
                all_crm = _inbox_mod._query_inbox_rows(conn, crm_only=True)
                counts = _inbox_mod.compute_inbox_counts(
                    [r for r in all_crm if aid_val is None or int(r.get("account_id") or 0) == int(aid_val)]
                )
                all_counts = _inbox_mod.compute_inbox_counts(all_crm)
            else:
                counts = get_inbox_counts(conn, aid_val, crm_only=True)
                all_counts = get_inbox_counts(conn, None, crm_only=True)
            out = {**out, "counts": counts, "all_counts": all_counts}
        return out, max_in_id

    @app.post("/api/inbox/dialogs/{dialog_id}/read")
    @app.post("/api/dialogs/{dialog_id}/read")
    async def inbox_dialog_mark_read(
        dialog_id: int, request: Request, crm_only: bool = False
    ):
        conn = request.app.state.conn
        out, max_in_id = _call(_mark_dialog_read_tx, conn, dialog_id, bool(crm_only))
        if not isinstance(out, dict):
            return out
        aid = out.get("account_id")
        if aid is not None:
            request.app.state.inbox_hub.publish(int(aid), out)
        peer_id = (
            out.get("peer_id")
            if out.get("peer_id") is not None
            else out.get("telegram_user_id")
        )
        if aid is not None and peer_id is not None:
            try:
                with locked_conn(conn):
                    acc = load_account(conn, int(aid))
            except LookupError:
                acc = None
            if isinstance(acc, dict) and acc.get("status") == "connected":
                sess_path = str(acc.get("session_path") or "")
                is_demo = sess_path.startswith("data/sessions/demo")
                app_gw = getattr(request.app.state, "gateway", None)
                if not (
                    is_demo and isinstance(app_gw, (type(None), TelethonGateway))
                ):
                    provider = str(acc.get("provider") or "telegram").strip().lower()
                    gw = _resolve_app_gateway(request.app, provider)
                    if gw is not None and hasattr(gw, "mark_dialog_read"):
                        try:
                            params = inspect.signature(gw.mark_dialog_read).parameters
                            accepts_var_kw = any(
                                p.kind == inspect.Parameter.VAR_KEYWORD
                                for p in params.values()
                            )
                            if accepts_var_kw or "max_id" in params:
                                res = gw.mark_dialog_read(
                                    sess_path, int(peer_id), max_id=max_in_id
                                )
                            else:
                                res = gw.mark_dialog_read(sess_path, int(peer_id))
                            if inspect.isawaitable(res):
                                await asyncio.wait_for(res, timeout=3.0)
                        except Exception:
                            pass
        return out

    @app.get("/api/inbox/events")
    async def inbox_events(request: Request, account_id: int = 0, crm_only: bool = False):
        conn = request.app.state.conn
        try:
            with locked_conn(conn):
                list_inbox(conn, account_id, "", crm_only=bool(crm_only))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Не найдено") from exc
        if account_id == 0:
            with locked_conn(conn):
                rows = conn.execute(
                    "SELECT id FROM accounts WHERE status = 'connected'"
                ).fetchall()
            for r in rows:
                await ensure_private_inbox(request.app, int(r["id"]))
        else:
            await ensure_private_inbox(request.app, account_id)

        async def gen():
            hub = request.app.state.inbox_hub
            queue = hub.subscribe(account_id)
            try:
                yield _sse("live", {"ok": _watch_alive(request.app, account_id)})
                with locked_conn(conn):
                    snap = list_inbox(conn, account_id, "", crm_only=bool(crm_only))
                yield _sse("snapshot", snap)
                while True:
                    try:
                        dialog = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield _sse("live", {"ok": _watch_alive(request.app, account_id)})
                        yield ": ping\n\n"
                        continue
                    if crm_only and isinstance(dialog, dict):
                        with locked_conn(conn):
                            allowed = _filter_crm_only_rows(conn, [dialog])
                        if not allowed:
                            continue
                    yield _sse("dialog", dialog)
            finally:
                hub.unsubscribe(account_id, queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/dialogs/open-by-lead")
    def dialogs_open_by_lead(body: OpenByLeadIn, request: Request):
        return _call(
            open_dialog_by_lead,
            request.app.state.conn,
            body.lead_id,
            body.account_id,
            telegram_user_id=body.telegram_user_id,
            username=body.username,
            phone=body.phone,
            name=body.name,
        )

    @app.post("/api/dialogs/start-direct")
    async def dialogs_start_direct(body: StartDirectDialogIn, request: Request):
        conn = request.app.state.conn
        gateway = None
        if body.account_id:
            try:
                with locked_conn(conn):
                    acc = load_account(conn, body.account_id)
                prov = str(acc.get("provider") or "telegram").strip().lower()
                if not str(acc.get("session_path") or "").startswith("data/sessions/demo"):
                    gateway = _resolve_app_gateway(request.app, prov)
            except Exception:
                gateway = None
        else:
            gateway = _resolve_app_gateway(request.app, "telegram")
        return await _call_async(
            start_direct_dialog,
            conn,
            body.handle,
            body.account_id,
            body.name,
            gateway=gateway,
        )

    @app.post("/api/inbox/sync")
    async def inbox_sync_now(request: Request, body: InboxSyncIn | None = None):
        conn = request.app.state.conn
        target_id = body.account_id if body is not None else None
        crm_only = bool(body.crm_only) if body is not None else False
        if target_id is None or int(target_id) == 0:
            with locked_conn(conn):
                rows = conn.execute(
                    "SELECT id FROM accounts WHERE status = 'connected'"
                ).fetchall()
            for r in rows:
                aid = int(r["id"])
                request.app.state.inbox_synced.discard(aid)
                await ensure_private_inbox(request.app, aid)
                await start_account_watch(request.app, aid)
            await sync_crm_dialog_messages_backfill_once(
                request.app, account_id=None, max_dialogs=80
            )
            with locked_conn(conn):
                dialogs = list_inbox(conn, 0, "", crm_only=crm_only)
            return {"synced": len(rows), "dialogs": dialogs}
        aid = int(target_id)
        request.app.state.inbox_synced.discard(aid)
        await ensure_private_inbox(request.app, aid)
        await start_account_watch(request.app, aid)
        await sync_crm_dialog_messages_backfill_once(
            request.app, account_id=aid, max_dialogs=80
        )
        with locked_conn(conn):
            dialogs = list_inbox(conn, aid, "", crm_only=crm_only)
        return {"synced": 1, "dialogs": dialogs}

    @app.post("/api/settings/tg-credentials")
    def settings_set_tg_credentials(body: TgCredentialsIn, request: Request):
        api_id = str(body.api_id or "").strip()
        api_hash = str(body.api_hash or "").strip()
        if not api_id.isdigit() or not api_hash:
            raise HTTPException(status_code=400, detail="Укажите числовой api_id и строку api_hash")
        now = utc_now()
        conn = request.app.state.conn
        with locked_conn(conn):
            conn.execute(
                """
                INSERT INTO kv_settings (key, value, updated_at) VALUES ('tg_api_id', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (api_id, now),
            )
            conn.execute(
                """
                INSERT INTO kv_settings (key, value, updated_at) VALUES ('tg_api_hash', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (api_hash, now),
            )
            conn.commit()
        os.environ["TG_API_ID"] = api_id
        os.environ["TG_API_HASH"] = api_hash
        return {"ok": True, "api_id": int(api_id)}

    @app.get("/api/dialogs/{dialog_id}/messages")
    async def dialog_messages(dialog_id: int, request: Request):
        conn = request.app.state.conn
        with locked_conn(conn):
            demo_row = conn.execute(
                """
                SELECT d.peer_type, d.is_group, a.status, a.session_path, a.provider, a.id AS account_id
                FROM dialogs d
                JOIN accounts a ON a.id = d.account_id
                WHERE d.id = ?
                """,
                (dialog_id,),
            ).fetchone()
            tg_id, tg_hash = _resolve_tg_creds(conn)
        has_tg_creds = bool(tg_id and tg_hash)
        if (
            demo_row
            and (
                str(demo_row["session_path"] or "").startswith("data/sessions/demo")
                or (
                    str(demo_row["provider"] or "telegram").strip().lower() != "whatsapp"
                    and request.app.state.gateway is None
                    and (not has_tg_creds or os.environ.get("DISABLE_TG_WATCHES") == "1")
                )
            )
            and demo_row["status"] == "connected"
            and demo_row["peer_type"] == "user"
            and not int(demo_row["is_group"] or 0)
        ):
            return _call(list_messages, conn, dialog_id, 50)
        if demo_row and str(demo_row["provider"] or "telegram").strip().lower() == "whatsapp":
            gateway = _gateway_for_provider(request, "whatsapp")
            return await _call_async(get_thread, conn, dialog_id, gateway)
        gateway = _gateway(request)
        return await _call_async(
            get_thread, conn, dialog_id, gateway
        )

    def _resolve_dialog_gateway(dialog_id: int, request: Request):
        conn = request.app.state.conn
        with locked_conn(conn):
            demo_row = conn.execute(
                """
                SELECT a.session_path, a.provider
                FROM dialogs d
                JOIN accounts a ON a.id = d.account_id
                WHERE d.id = ?
                """,
                (dialog_id,),
            ).fetchone()
        if demo_row and str(demo_row["provider"] or "telegram").strip().lower() == "whatsapp":
            return _gateway_for_provider(request, "whatsapp")
        if (
            demo_row
            and request.app.state.gateway is None
            and str(demo_row["session_path"] or "").startswith("data/sessions/demo")
        ):
            return None
        return _gateway(request)

    @app.post("/api/dialogs/{dialog_id}/messages")
    async def dialog_send(dialog_id: int, body: MessageIn, request: Request):
        conn = request.app.state.conn
        gateway = _resolve_dialog_gateway(dialog_id, request)
        reply_target = body.reply_to if body.reply_to is not None else body.reply_to_msg_id
        return await _call_async(
            send_reply,
            conn,
            dialog_id,
            body.text,
            gateway,
            reply_target,
        )

    @app.patch("/api/dialogs/{dialog_id}/messages/{telegram_message_id}")
    async def dialog_edit_message(
        dialog_id: int,
        telegram_message_id: int,
        body: MessageEditIn,
        request: Request,
    ):
        conn = request.app.state.conn
        gateway = _resolve_dialog_gateway(dialog_id, request)
        return await _call_async(
            edit_dialog_message,
            conn,
            dialog_id,
            telegram_message_id,
            body.text,
            gateway,
        )

    @app.post("/api/dialogs/{dialog_id}/media")
    async def dialog_send_media(dialog_id: int, body: MediaSendIn, request: Request):
        conn = request.app.state.conn
        gateway = _resolve_dialog_gateway(dialog_id, request)
        raw_b64 = (body.data_base64 or "").strip()
        if "," in raw_b64 and raw_b64.startswith("data:"):
            raw_b64 = raw_b64.split(",", 1)[1]
        try:
            file_bytes = base64.b64decode(raw_b64) if raw_b64 else b""
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Некорректный base64 файл") from exc
        reply_target = body.reply_to if body.reply_to is not None else body.reply_to_msg_id
        return await _call_async(
            send_dialog_media,
            conn,
            dialog_id,
            file_bytes,
            body.filename,
            mime_type=body.mime_type,
            media_type=body.media_type,
            caption=body.caption,
            duration=body.duration,
            reply_to=reply_target,
            gateway=gateway,
        )

    @app.get("/media/inbox/{filename}")
    def serve_inbox_media_file(filename: str, request: Request):
        safe_name = Path(filename).name
        target = INBOX_MEDIA_DIR / safe_name
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Медиафайл не найден")
        playable_flag = str(request.query_params.get("playable") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if playable_flag:
            wav_bytes = ensure_playable_voice_wav(
                target.read_bytes(),
                cache_key=Path(safe_name).stem,
            )
            if wav_bytes:
                return Response(
                    content=wav_bytes,
                    media_type="audio/wav",
                    headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"},
                )
        return FileResponse(target, headers={"Content-Security-Policy": "sandbox"})

    @app.get("/api/dialogs/{dialog_id}/avatar")
    async def dialog_avatar_get(dialog_id: int, request: Request):
        conn = request.app.state.conn
        try:
            gateway = _resolve_dialog_gateway(dialog_id, request)
        except Exception:
            gateway = None
        data, mime, cache_control = await _call_async(
            get_dialog_avatar_with_cache,
            conn,
            dialog_id,
            gateway=gateway,
        )
        return Response(
            content=data,
            media_type=mime,
            headers={"Cache-Control": cache_control},
        )

    @app.get("/api/dialogs/{dialog_id}/messages/{telegram_message_id}/media")
    async def dialog_get_message_media(
        dialog_id: int,
        telegram_message_id: int,
        request: Request,
    ):
        conn = request.app.state.conn
        with locked_conn(conn):
            msg_row = conn.execute(
                """
                SELECT m.*, d.peer_id, d.account_id, a.session_path
                FROM messages m
                JOIN dialogs d ON d.id = m.dialog_id
                JOIN accounts a ON a.id = d.account_id
                WHERE m.dialog_id = ? AND m.telegram_message_id = ?
                LIMIT 1
                """,
                (dialog_id, telegram_message_id),
            ).fetchone()
        if msg_row is None:
            raise HTTPException(status_code=404, detail="Сообщение не найдено")

        playable_flag = str(request.query_params.get("playable") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        mtype = str(msg_row["media_type"] or "document").lower()
        media_url = str(msg_row["media_url"] or "").strip()
        resolved_local: Path | None = None
        resolved_mime: str | None = msg_row["media_mime"]
        resolved_filename: str | None = msg_row["media_filename"]

        if media_url.startswith("/media/inbox/"):
            local_file = INBOX_MEDIA_DIR / Path(media_url).name
            if local_file.exists() and local_file.is_file():
                resolved_local = local_file

        if resolved_local is None:
            INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            ext = ".ogg" if mtype == "voice" else (".mp4" if mtype in ("video", "video_note") else (".jpg" if mtype == "photo" else ".bin"))
            dest_path = INBOX_MEDIA_DIR / f"dlg{dialog_id}_msg{telegram_message_id}{ext}"
            wav_cand = INBOX_MEDIA_DIR / f"dlg{dialog_id}_msg{telegram_message_id}.wav"
            if wav_cand.exists() and wav_cand.is_file():
                resolved_local = wav_cand
                resolved_mime = resolved_mime or "audio/wav"
            elif dest_path.exists() and dest_path.is_file():
                resolved_local = dest_path
            else:
                try:
                    gateway = _resolve_dialog_gateway(dialog_id, request)
                except Exception:
                    gateway = None
                if gateway is not None and hasattr(gateway, "download_message_media"):
                    dl_res = await gateway.download_message_media(
                        msg_row["session_path"],
                        int(msg_row["peer_id"]),
                        int(telegram_message_id),
                        str(dest_path),
                    )
                    if isinstance(dl_res, dict) and dl_res.get("file_path"):
                        actual_path = Path(dl_res["file_path"])
                        if actual_path.exists():
                            rel_url = f"/media/inbox/{actual_path.name}"
                            with locked_conn(conn):
                                conn.execute(
                                    "UPDATE messages SET media_url = ? WHERE id = ?",
                                    (rel_url, int(msg_row["id"])),
                                )
                                conn.commit()
                            resolved_local = actual_path
                            resolved_mime = dl_res.get("media_mime") or resolved_mime
                            resolved_filename = dl_res.get("media_filename") or resolved_filename

        if playable_flag and mtype in ("voice", "audio"):
            raw_bytes = resolved_local.read_bytes() if (resolved_local and resolved_local.exists()) else b""
            wav_bytes = ensure_playable_voice_wav(
                raw_bytes,
                cache_key=f"dlg{dialog_id}_msg{telegram_message_id}",
                text_hint=str(msg_row["text"] or ""),
            )
            if wav_bytes:
                return Response(
                    content=wav_bytes,
                    media_type="audio/wav",
                    headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"},
                )

        if resolved_local is not None and resolved_local.exists():
            return FileResponse(
                resolved_local,
                media_type=resolved_mime or "application/octet-stream",
                filename=resolved_filename or resolved_local.name,
            )

        raise HTTPException(status_code=404, detail="Медиафайл недоступен")

    @app.get("/api/outreach/schedule")
    def outreach_schedule_get(request: Request):
        return _call(get_outreach_schedule, request.app.state.conn)

    @app.put("/api/outreach/schedule")
    def outreach_schedule_put(body: OutreachScheduleIn, request: Request):
        return _call(
            update_outreach_schedule,
            request.app.state.conn,
            body.timezone,
            body.days,
            autopilot_enabled=body.autopilot_enabled,
        )

    @app.get("/api/outreach/autopilot")
    def outreach_autopilot_get(request: Request):
        return _call(get_autopilot_status, request.app.state.conn)

    @app.post("/api/outreach/autopilot")
    @app.put("/api/outreach/autopilot")
    @app.patch("/api/outreach/autopilot")
    def outreach_autopilot_set(body: OutreachAutopilotIn, request: Request):
        return _call(
            set_autopilot_enabled,
            request.app.state.conn,
            body.enabled,
            activate_drafts=body.activate_drafts,
        )

    @app.post("/api/outreach/autopilot/tick")
    async def outreach_autopilot_tick(
        request: Request, body: OutreachAutopilotTickIn | None = None
    ):
        max_send = body.max_send_per_campaign if body is not None else 1
        respect_delays = body.respect_delays if body is not None else True
        force = body.force if body is not None else False
        return await _call_async(
            run_autopilot_tick,
            request.app.state.conn,
            gateway=_resolve_app_gateway(request.app, "telegram"),
            wa_gateway=_resolve_app_gateway(request.app, "whatsapp"),
            max_send_per_campaign=max_send,
            respect_delays=respect_delays,
            force=force,
        )

    @app.post("/api/outreach/upload-image")
    @app.post("/api/outreach/media")
    async def outreach_upload_image(request: Request):
        import base64
        import hashlib
        import re
        from app.db import OUTREACH_MEDIA_DIR

        content_type = str(request.headers.get("content-type") or "").lower()
        raw_bytes: bytes = b""
        filename_in = "image.png"
        mime_ext = ""

        if "multipart/form-data" in content_type:
            raw_body = await request.body()
            # Simple RFC2046 boundary parser so python-multipart is optional
            boundary = ""
            for part_kv in content_type.split(";"):
                part_kv = part_kv.strip()
                if part_kv.startswith("boundary="):
                    boundary = part_kv.split("=", 1)[1].strip('"')
                    break
            if boundary:
                delim = f"--{boundary}".encode("latin-1")
                for chunk in raw_body.split(delim):
                    if b"Content-Disposition:" not in chunk:
                        continue
                    head_bytes, sep, body_bytes = chunk.partition(b"\r\n\r\n")
                    if not sep:
                        continue
                    if body_bytes.endswith(b"\r\n"):
                        body_bytes = body_bytes[:-2]
                    head_text = head_bytes.decode("utf-8", errors="ignore")
                    m_fn = re.search(r'filename="([^"]+)"', head_text)
                    if m_fn:
                        filename_in = m_fn.group(1).strip() or "image.png"
                    raw_bytes = body_bytes
                    break
        else:
            try:
                payload = await request.json()
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Некорректный JSON") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="Ожидается объект JSON")
            raw_data = str(payload.get("data_url") or payload.get("base64") or "").strip()
            filename_in = str(payload.get("filename") or "image.png").strip()
            if not raw_data:
                raise HTTPException(status_code=400, detail="Передайте файл картинки")
            if raw_data.startswith("data:"):
                header, _, b64_part = raw_data.partition(",")
                raw_data = b64_part.strip()
                if "image/jpeg" in header or "image/jpg" in header:
                    mime_ext = ".jpg"
                elif "image/png" in header:
                    mime_ext = ".png"
                elif "image/webp" in header:
                    mime_ext = ".webp"
                elif "image/gif" in header:
                    mime_ext = ".gif"
            try:
                raw_bytes = base64.b64decode(raw_data, validate=False)
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Некорректные данные изображения") from exc

        ext = Path(filename_in).suffix.lower() or mime_ext or ".png"
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in {".png", ".jpg", ".webp", ".gif"}:
            raise HTTPException(
                status_code=400,
                detail="Поддерживаются форматы PNG, JPG, WEBP и GIF",
            )
        if not raw_bytes:
            raise HTTPException(status_code=400, detail="Пустой файл изображения")
        if len(raw_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Размер картинки не должен превышать 10 МБ")
        digest = hashlib.sha1(raw_bytes).hexdigest()[:16]
        out_name = f"{digest}{ext}"
        OUTREACH_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTREACH_MEDIA_DIR / out_name
        out_path.write_bytes(raw_bytes)
        media_url = f"/media/outreach/{out_name}"
        return {
            "ok": True,
            "image_url": media_url,
            "url": media_url,
            "filename": filename_in,
            "size": len(raw_bytes),
        }

    @app.get("/media/outreach/{filename}")
    def outreach_media_get(filename: str):
        from app.db import resolve_outreach_media

        safe_name = Path(filename).name
        resolved = resolve_outreach_media(f"/media/outreach/{safe_name}")
        target = resolved if isinstance(resolved, Path) else None
        if target is None or not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Картинка не найдена")
        return FileResponse(target)

    @app.post("/api/campaigns/preview-csv")
    def campaigns_preview_csv(body: CampaignCsvIn):
        return _call(preview_campaign_csv, body.csv_text)

    @app.post("/api/tools/phone-converter")
    async def tools_phone_converter(request: Request):
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Некорректный JSON") from exc
        return await _call_async(
            convert_phones_to_tg,
            request.app.state.conn,
            request.app.state.gateway,
            payload,
        )

    @app.post("/api/tools/group-parser")
    async def tools_group_parser(request: Request):
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Некорректный JSON") from exc
        return await _call_async(
            parse_telegram_group,
            request.app.state.conn,
            request.app.state.gateway,
            payload,
        )

    @app.get("/api/campaigns")
    def campaigns_list(request: Request):
        return _call(list_campaigns, request.app.state.conn)

    @app.get("/api/campaigns/sample-csv")
    def campaigns_sample_csv():
        sample_path = BASE / "softugc_sample.csv"
        if not sample_path.exists():
            raise HTTPException(status_code=404, detail="Файл примера CSV не найден")
        return {"csv_text": sample_path.read_text(encoding="utf-8")}

    @app.post("/api/campaigns")
    def campaigns_create(body: CampaignIn, request: Request):
        return _call(
            create_campaign,
            request.app.state.conn,
            name=body.name,
            account_id=body.account_id,
            daily_limit=body.daily_limit,
            steps=body.steps,
            account_ids=body.account_ids,
            status=body.status,
            leadgen_enabled=body.leadgen_enabled,
            leadgen_daily_quota=body.leadgen_daily_quota,
            leadgen_sources=body.leadgen_sources,
        )

    @app.get("/api/campaigns/{campaign_id}")
    def campaigns_get(campaign_id: int, request: Request):
        return _call(get_campaign, request.app.state.conn, campaign_id)

    @app.delete("/api/campaigns/{campaign_id}")
    def campaigns_delete(campaign_id: int, request: Request):
        _require_admin_or_mcp_auth(request)
        return _call(delete_campaign, request.app.state.conn, campaign_id)

    @app.put("/api/campaigns/{campaign_id}")
    def campaigns_update(campaign_id: int, body: CampaignUpdateIn, request: Request):
        return _call(
            update_campaign,
            request.app.state.conn,
            campaign_id,
            name=body.name,
            account_id=body.account_id,
            daily_limit=body.daily_limit,
            status=body.status,
            steps=body.steps,
            account_ids=body.account_ids,
            leadgen_enabled=body.leadgen_enabled,
            leadgen_daily_quota=body.leadgen_daily_quota,
            leadgen_sources=body.leadgen_sources,
        )

    @app.post("/api/campaigns/{campaign_id}/csv")
    def campaigns_csv(campaign_id: int, body: CampaignCsvIn, request: Request):
        return _call(
            import_campaign_csv,
            request.app.state.conn,
            campaign_id,
            body.csv_text,
            column_mapping=body.column_mapping,
        )

    @app.post("/api/campaigns/{campaign_id}/from-leads")
    def campaigns_from_leads(
        campaign_id: int, body: CampaignFromLeadsIn, request: Request
    ):
        return _call(
            enroll_leads_from_crm,
            request.app.state.conn,
            campaign_id,
            lead_ids=body.lead_ids,
            status=body.status,
            folder_id=body.folder_id,
        )

    @app.post("/api/campaigns/{campaign_id}/run")
    async def campaigns_run(
        campaign_id: int, request: Request, body: CampaignRunIn | None = None
    ):
        max_send = body.max_send if body is not None else None
        respect_delays = bool(body.respect_delays) if body is not None else False
        dry_run = bool(body.dry_run) if body is not None else False
        conn = request.app.state.conn
        if dry_run:
            return _call(
                preview_outreach_batch,
                conn,
                campaign_id,
                max_send=max_send,
                respect_delays=respect_delays,
                strict=True,
            )
        with locked_conn(conn):
            camp_row = conn.execute(
                """
                SELECT a.session_path, a.provider, c.account_ids_json
                FROM campaigns c
                LEFT JOIN accounts a ON a.id = c.account_id
                WHERE c.id = ?
                """,
                (campaign_id,),
            ).fetchone()
            session_path = camp_row["session_path"] if camp_row else None
            provider = camp_row["provider"] if camp_row else None
            if camp_row and not session_path and camp_row["account_ids_json"]:
                try:
                    acc_ids = json.loads(camp_row["account_ids_json"])
                    if isinstance(acc_ids, list) and acc_ids:
                        acc_row = conn.execute(
                            "SELECT session_path, provider FROM accounts WHERE id = ?",
                            (int(acc_ids[0]),),
                        ).fetchone()
                        if acc_row:
                            session_path = acc_row["session_path"]
                            provider = acc_row["provider"]
                except Exception:
                    pass
            tg_id, tg_hash = _resolve_tg_creds(conn)
        has_tg_creds = bool(tg_id and tg_hash)
        wa_gateway = _gateway_for_provider(request, "whatsapp")
        if (
            camp_row
            and request.app.state.gateway is None
            and str(session_path or "").startswith("data/sessions/demo")
        ):
            gateway = None
        elif camp_row and str(provider or "").strip().lower() == "whatsapp":
            gateway = (
                request.app.state.gateway
                if request.app.state.gateway is not None
                else (_gateway(request) if has_tg_creds else None)
            )
        elif (
            camp_row
            and request.app.state.gateway is None
            and not has_tg_creds
        ):
            gateway = None
        elif camp_row and session_path:
            gateway = _gateway(request)
        else:
            gateway = request.app.state.gateway
        return await _call_async(
            run_campaign_batch,
            conn,
            campaign_id,
            gateway=gateway,
            max_send=max_send,
            respect_delays=respect_delays,
            wa_gateway=wa_gateway,
        )

    @app.get("/api/analytics/summary")
    def analytics_summary(request: Request, period: str = "30d"):
        return _call(get_analytics_summary, request.app.state.conn, period=period)

    @app.get("/api/analytics/outreach-funnel")
    def analytics_outreach_funnel(
        request: Request,
        campaign_id: int | None = None,
        days: int = 14,
        stage: str = "all",
        variant: str = "all",
        q: str = "",
        limit: int = 150,
    ):
        return _call(
            get_outreach_funnel,
            request.app.state.conn,
            campaign_id=campaign_id,
            days=days,
            stage=stage,
            variant=variant,
            q=q,
            limit=limit,
        )

    def _require_admin_or_mcp_auth(request: Request) -> None:
        from app.crm_auth import resolve_request_crm_user

        if has_valid_mcp_api_key(request):
            return
        with locked_conn(request.app.state.conn):
            user = resolve_request_crm_user(request.app.state.conn, request)
        if user is None:
            raise HTTPException(
                status_code=401, detail="Требуется авторизация CRM или MCP_API_KEY"
            )
        if user.get("role") != "admin":
            raise HTTPException(
                status_code=403, detail="Доступ только для администратора"
            )

    @app.post("/api/analytics/sync-read-receipts")
    async def analytics_sync_read_receipts(request: Request, account_id: int | None = None):
        _require_admin_or_mcp_auth(request)
        res = await sync_live_tg_read_receipts_once(
            request.app, account_id=account_id, limit=80
        )
        if int(res.get("updated_recipients") or 0) > 0:
            from app.cloud_persistence import schedule_cloud_backup

            schedule_cloud_backup(request.app)
        return res

    @app.post("/api/campaigns/sync-recipient-drafts")
    async def campaigns_sync_recipient_drafts(request: Request):
        from app.funnel_analytics import sync_untouched_recipient_drafts

        _require_admin_or_mcp_auth(request)
        payload = await request.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="items list is required")
        res = _call(sync_untouched_recipient_drafts, request.app.state.conn, items)
        if int(res.get("updated") or 0) > 0 and not is_ephemeral_runtime():
            from app.cloud_persistence import schedule_cloud_backup

            schedule_cloud_backup(request.app)
        return res

    class AirtableSyncIn(BaseModel):
        direction: str = "both"

    class AirtableCredentialsIn(BaseModel):
        api_key: str | None = None
        base_id: str | None = None
        table_name: str | None = None

    @app.get("/api/airtable/status")
    def airtable_status_get(request: Request):
        from app.airtable_sync import get_airtable_config

        return _call(get_airtable_config, request.app.state.conn)

    @app.post("/api/airtable/credentials")
    def airtable_credentials_set(body: AirtableCredentialsIn, request: Request):
        from app.airtable_sync import get_airtable_config
        from app.db import utc_now

        now = utc_now()
        conn_obj = request.app.state.conn
        with locked_conn(conn_obj):
            for kv_key, raw in (
                ("airtable_api_key", body.api_key),
                ("airtable_base_id", body.base_id),
                ("airtable_table_name", body.table_name),
            ):
                if raw is not None and str(raw).strip():
                    conn_obj.execute(
                        """
                        INSERT INTO kv_settings (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (kv_key, str(raw).strip(), now),
                    )
            conn_obj.commit()
        return _call(get_airtable_config, conn_obj)

    class CloudCredentialsIn(BaseModel):
        account_id: str | None = None
        access_key: str | None = None
        secret_key: str | None = None
        bucket: str | None = None

    @app.get("/api/cloud/status")
    def cloud_status_get(request: Request):
        from app.cloud_persistence import get_cloud_status

        return _call(get_cloud_status, request.app.state.conn)

    @app.post("/api/cloud/backup")
    def cloud_backup_run(request: Request):
        from app.cloud_persistence import backup_to_cloud

        return _call_isolated_db(request.app.state.conn, backup_to_cloud)

    @app.post("/api/cloud/restore")
    async def cloud_restore_run(request: Request):
        from app.cloud_persistence import restore_from_cloud

        conn_obj = request.app.state.conn
        with locked_conn(conn_obj):
            res = _call(restore_from_cloud, force=True, live_conn=conn_obj)
            if isinstance(res, dict) and res.get("restored"):
                init_db(conn_obj)
                ensure_funnel_columns(conn_obj)
                _heal_crm_hygiene_safe(conn_obj)
                restore_disk_sessions(conn_obj)
                gw = getattr(request.app.state, "gateway", None)
                if gw is not None and hasattr(gw, "_session_strings"):
                    gw._session_strings.clear()
                    _seed_gateway_session_strings(conn_obj, gw)
        return res

    @app.post("/api/cloud/credentials")
    def cloud_credentials_set(body: CloudCredentialsIn, request: Request):
        from app.cloud_persistence import backup_to_cloud
        from app.db import utc_now

        now = utc_now()
        conn_obj = request.app.state.conn
        with locked_conn(conn_obj):
            for kv_key, env_key, raw in (
                ("r2_account_id", "R2_ACCOUNT_ID", body.account_id),
                ("r2_access_key", "R2_ACCESS_KEY", body.access_key),
                ("r2_secret_key", "R2_SECRET_KEY", body.secret_key),
                ("r2_bucket", "R2_BUCKET", body.bucket),
            ):
                if raw is not None and str(raw).strip():
                    clean_v = str(raw).strip()
                    os.environ[env_key] = clean_v
                    conn_obj.execute(
                        """
                        INSERT INTO kv_settings (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (kv_key, clean_v, now),
                    )
            conn_obj.commit()
        return _call(backup_to_cloud, conn_obj)

    @app.post("/api/airtable/sync")
    def airtable_sync_run(request: Request, body: AirtableSyncIn | None = None):
        from app.airtable_sync import sync_airtable

        direction = body.direction if body is not None else "both"
        client = getattr(request.app.state, "airtable_client", None)
        return _call_isolated_db(
            request.app.state.conn,
            sync_airtable,
            direction=direction,
            client=client,
        )

    @app.post("/api/airtable/push/{lead_id}")
    def airtable_push_lead(lead_id: int, request: Request):
        from app.airtable_sync import push_lead_to_airtable

        client = getattr(request.app.state, "airtable_client", None)
        return _call_isolated_db(
            request.app.state.conn,
            push_lead_to_airtable,
            lead_id,
            client=client,
        )

    @app.post("/api/pipeline/stages/airtable")
    def pipeline_stages_ensure_airtable(request: Request):
        from app.db import ensure_airtable_pipeline_stages

        return _call(ensure_airtable_pipeline_stages, request.app.state.conn)

    class CalendarEventIn(BaseModel):
        title: str
        start_at: str
        end_at: str | None = None
        event_type: str = "call"
        all_day: bool = False
        lead_id: int | None = None
        meet_link: str | None = None
        notes: str | None = None
        status: str = "scheduled"

    class CalendarEventPatch(BaseModel):
        title: str | None = None
        start_at: str | None = None
        end_at: str | None = None
        event_type: str | None = None
        all_day: bool | None = None
        lead_id: int | None = None
        meet_link: str | None = None
        notes: str | None = None
        status: str | None = None

    class CalendarRescheduleIn(BaseModel):
        uid: str | None = None
        item_kind: str | None = None
        item_id: int | None = None
        new_start_at: str
        new_end_at: str | None = None

    @app.get("/api/calendar")
    def calendar_list(
        request: Request,
        start_date: str | None = None,
        end_date: str | None = None,
        event_type: str | None = None,
        lead_id: int | None = None,
        include_completed: bool = True,
    ):
        from app.calendar_events import list_calendar_items

        return _call(
            list_calendar_items,
            request.app.state.conn,
            start_date=start_date,
            end_date=end_date,
            event_type=event_type,
            lead_id=lead_id,
            include_completed=include_completed,
        )

    @app.post("/api/calendar/events")
    def calendar_event_create(body: CalendarEventIn, request: Request):
        from app.calendar_events import create_calendar_event

        return _call(
            create_calendar_event,
            request.app.state.conn,
            title=body.title,
            start_at=body.start_at,
            end_at=body.end_at,
            event_type=body.event_type,
            all_day=body.all_day,
            lead_id=body.lead_id,
            meet_link=body.meet_link,
            notes=body.notes,
            status=body.status,
        )

    @app.patch("/api/calendar/events/{event_id}")
    def calendar_event_patch(
        event_id: int, body: CalendarEventPatch, request: Request
    ):
        from app.calendar_events import update_calendar_event

        fields = body.model_dump(exclude_unset=True)
        return _call(
            update_calendar_event,
            request.app.state.conn,
            event_id,
            **fields,
        )

    @app.delete("/api/calendar/events/{event_id}")
    def calendar_event_delete(event_id: int, request: Request):
        from app.calendar_events import delete_calendar_event

        return _call(delete_calendar_event, request.app.state.conn, event_id)

    @app.post("/api/calendar/reschedule")
    def calendar_item_reschedule(body: CalendarRescheduleIn, request: Request):
        from app.calendar_events import reschedule_calendar_item

        res = _call(
            reschedule_calendar_item,
            request.app.state.conn,
            uid=body.uid,
            item_kind=body.item_kind,
            item_id=body.item_id,
            new_start_at=body.new_start_at,
            new_end_at=body.new_end_at,
        )
        if res.get("needs_airtable_push") and not is_ephemeral_runtime():
            try:
                from app.airtable_sync import get_airtable_config, push_calendar_date_to_airtable

                with locked_conn(request.app.state.conn):
                    cfg = get_airtable_config(request.app.state.conn)
                    if cfg.get("configured"):
                        client = getattr(request.app.state, "airtable_client", None)
                        push_calendar_date_to_airtable(
                            request.app.state.conn,
                            item_kind=res["item_kind"],
                            item_id=int(res["item_id"]),
                            new_date=res["new_date"],
                            client=client,
                        )
            except Exception:
                pass
        return res

    class AiBotConfigIn(BaseModel):
        enabled: bool = True
        copilot_enabled: bool = True
        auto_qualify: bool = True
        goal: str = ""
        classifier_prompt: str = ""
        strategist_prompt: str = ""
        generator_prompt: str = ""
        selector_prompt: str = ""

    @app.get("/api/ai-bot/config")
    def ai_bot_config_get(request: Request):
        with locked_conn(request.app.state.conn):
            row = request.app.state.conn.execute(
                "SELECT value FROM kv_settings WHERE key = 'ai_bot_config'"
            ).fetchone()
        default_cfg = {
            "enabled": True,
            "copilot_enabled": True,
            "auto_qualify": True,
            "goal": "",
            "classifier_prompt": "",
            "strategist_prompt": "",
            "generator_prompt": "",
            "selector_prompt": "",
        }
        if row and row["value"]:
            try:
                parsed = json.loads(row["value"])
                if isinstance(parsed, dict):
                    default_cfg.update(parsed)
            except Exception:
                pass
        return {"ok": True, "config": default_cfg}

    @app.post("/api/ai-bot/config")
    def ai_bot_config_save(body: AiBotConfigIn, request: Request):
        cfg = body.model_dump()
        now = utc_now()
        with locked_conn(request.app.state.conn):
            request.app.state.conn.execute(
                """
                INSERT INTO kv_settings (key, value, updated_at)
                VALUES ('ai_bot_config', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (json.dumps(cfg, ensure_ascii=False), now),
            )
            request.app.state.conn.commit()
        return {"ok": True, "config": cfg}

    class NodeMavenConfigIn(BaseModel):
        api_key: str = ""
        user: str = ""
        password: str = ""
        host: str = "gate.nodemaven.com"
        port: int = 1080
        ttl: str | int = "24h"
        filter: str = "medium"
        default_country: str = "us"

    class ProxyProbeIn(BaseModel):
        account_id: int | None = None
        phone: str | None = None
        preset: str | None = None
        proxy_url: str | None = None

    @app.get("/api/proxy/nodemaven/config")
    def nodemaven_config_get(request: Request):
        from app.telegram_gateway import (
            build_nodemaven_proxy_url,
            infer_proxy_country,
            make_nodemaven_sid,
            mask_proxy_url,
            resolve_nodemaven_config,
        )

        conn = request.app.state.conn
        with locked_conn(conn):
            cfg = resolve_nodemaven_config(conn)
            rows = conn.execute(
                "SELECT id, phone, telegram_username, telegram_name, proxy_url, provider FROM accounts ORDER BY id"
            ).fetchall()
            accounts_preview = []
            for r in rows:
                aid = r["id"]
                phone = r["phone"]
                raw_proxy = str(r["proxy_url"] or "").strip()
                nm_url = build_nodemaven_proxy_url(
                    account_key=aid,
                    phone=phone,
                    preset=raw_proxy or "auto",
                    conn=conn,
                )
                effective = nm_url or raw_proxy or None
                accounts_preview.append(
                    {
                        "account_id": aid,
                        "phone": phone,
                        "username": r["telegram_username"],
                        "title": r["telegram_name"],
                        "sid": make_nodemaven_sid(aid),
                        "country": infer_proxy_country(
                            phone=phone,
                            preset=raw_proxy or "auto",
                            default_country=(cfg or {}).get("default_country", "us"),
                        ),
                        "proxy_masked": mask_proxy_url(effective),
                    }
                )
        if not cfg:
            return {
                "ok": True,
                "configured": False,
                "host": "gate.nodemaven.com",
                "port": 1080,
                "ttl": "24h",
                "filter": "medium",
                "default_country": "us",
                "user_masked": None,
                "accounts": accounts_preview,
            }
        u = cfg["user"]
        user_masked = f"{u[:3]}***{u[-2:]}" if len(u) > 5 else "***"
        return {
            "ok": True,
            "configured": True,
            "host": cfg["host"],
            "port": cfg["port"],
            "ttl": cfg["ttl"],
            "filter": cfg["filter"],
            "default_country": cfg["default_country"],
            "user_masked": user_masked,
            "accounts": accounts_preview,
        }

    @app.post("/api/proxy/nodemaven/config")
    def nodemaven_config_save(body: NodeMavenConfigIn, request: Request):
        conn = request.app.state.conn
        now = utc_now()
        from app.telegram_gateway import _normalize_nodemaven_ttl

        pairs = {
            "nodemaven_user": body.user.strip(),
            "nodemaven_host": (body.host or "gate.nodemaven.com").strip(),
            "nodemaven_port": str(int(body.port or 1080)),
            "nodemaven_ttl": _normalize_nodemaven_ttl(body.ttl),
            "nodemaven_filter": (body.filter or "medium").strip().lower(),
            "nodemaven_default_country": (body.default_country or "us").strip().lower(),
        }
        if body.password.strip():
            pairs["nodemaven_pass"] = body.password.strip()
        if body.api_key.strip():
            pairs["nodemaven_api_key"] = body.api_key.strip()
        with locked_conn(conn):
            for k, v in pairs.items():
                conn.execute(
                    """
                    INSERT INTO kv_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (k, v, now),
                )
            conn.commit()
        gw = _resolve_app_gateway(request.app, "telegram")
        if gw is not None and hasattr(gw, "_proxies"):
            gw._proxies.clear()
        return nodemaven_config_get(request)

    @app.post("/api/proxy/nodemaven/test")
    async def nodemaven_proxy_test(body: ProxyProbeIn, request: Request):
        from app.telegram_gateway import (
            build_nodemaven_proxy_url,
            mask_proxy_url,
            probe_proxy_ip,
        )

        conn = request.app.state.conn
        target_url = (body.proxy_url or "").strip()
        if not target_url:
            acc_key: int | str = body.account_id or 1
            phone = body.phone
            preset = body.preset or "auto"
            with locked_conn(conn):
                if body.account_id:
                    row = conn.execute(
                        "SELECT id, phone, proxy_url FROM accounts WHERE id = ?",
                        (int(body.account_id),),
                    ).fetchone()
                    if row:
                        phone = phone or row["phone"]
                        preset = body.preset or row["proxy_url"] or "auto"
                target_url = (
                    build_nodemaven_proxy_url(
                        account_key=acc_key,
                        phone=phone,
                        preset=preset,
                        conn=conn,
                    )
                    or ""
                )
        if not target_url:
            return {
                "ok": False,
                "configured": False,
                "error": "Ключи NodeMaven ещё не заданы (NODEMAVEN_USER / NODEMAVEN_PASS)",
            }
        result = await probe_proxy_ip(target_url)
        result["configured"] = True
        result.setdefault("proxy_masked", mask_proxy_url(target_url))
        return result

    from app.leadgen_settings import (
        _resolve_state_dir,
        get_leadgen_config,
        get_leadgen_status,
        push_discovered_now,
        save_leadgen_config,
        sync_leadgen_campaigns,
    )

    @app.get("/api/leadgen/direct-leads")
    def leadgen_direct_leads_get(request: Request):
        jsonl_path = Path(_resolve_state_dir()) / "direct_x_leads.jsonl"
        items: list[dict] = []
        if jsonl_path.is_file():
            try:
                raw_text = jsonl_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw_text = ""
            for raw_line in raw_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    items.append(parsed)
        return {"ok": True, "count": len(items), "items": items[-200:]}

    @app.get("/api/leadgen/ofm-review")
    def leadgen_ofm_review_get(request: Request, limit: int = 100, query: str = ""):
        from app.hygiene import read_ofm_review_leads

        return read_ofm_review_leads(_resolve_state_dir(), limit=limit, query=query)

    @app.get("/api/leadgen/config")
    def leadgen_config_get(request: Request):
        conn = request.app.state.conn
        return {
            "ok": True,
            "config": _call(get_leadgen_config, conn),
            "status": _call(get_leadgen_status, conn),
        }

    @app.put("/api/leadgen/config")
    @app.post("/api/leadgen/config")
    async def leadgen_config_save(request: Request):
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Некорректный JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Ожидается объект JSON")
        conn = request.app.state.conn
        saved = _call(save_leadgen_config, conn, payload)
        if bool(payload.get("sync_campaigns")):
            _call(sync_leadgen_campaigns, conn, saved)
        return {
            "ok": True,
            "config": saved,
            "status": _call(get_leadgen_status, conn),
        }

    @app.post("/api/leadgen/sync-campaigns")
    def leadgen_sync_campaigns(request: Request):
        return _call(sync_leadgen_campaigns, request.app.state.conn)

    @app.post("/api/leadgen/push-now")
    def leadgen_push_now(request: Request):
        return _call(push_discovered_now, request.app.state.conn)

    from app.crm_auth import register_auth_routes

    register_auth_routes(app, templates)

    from app.mcp_server import register_mcp_routes

    register_mcp_routes(app)

    @app.post("/api/demo/seed")
    def demo_seed(request: Request):
        if is_vps_runtime() or (
            is_auth_enforced() and os.getenv("ALLOW_PROD_DEMO_SEED") != "1"
        ):
            raise HTTPException(
                status_code=403, detail="Demo seed is disabled on production"
            )
        result = _call(seed_demo, request.app.state.conn)
        account_id = result.get("account_id")
        if account_id is not None:
            request.app.state.inbox_synced.add(account_id)
        return result

    return app


_DEFAULT_APP: FastAPI | None = None


def get_app() -> FastAPI:
    """Create the default FastAPI instance lazily so importing app.main has no side effects."""
    global _DEFAULT_APP
    if _DEFAULT_APP is None:
        load_env_file()
        _DEFAULT_APP = create_app()
        globals()["app"] = _DEFAULT_APP
    return _DEFAULT_APP


def __getattr__(name: str):
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
