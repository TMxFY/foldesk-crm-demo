import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
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
    restore_disk_sessions,
    set_daily_limit,
    start_login,
    update_account_settings,
)
from app.accounts import _load as load_account
from app.tg_web_proxy import register_tg_web_routes
from app.db import (
    ROOT,
    connect,
    create_pipeline_stage,
    init_db,
    list_pipeline_stages,
    update_pipeline_stage,
    utc_now,
)
from app.demo import DEMO_SESSION, seed_demo
from app.inbox import (
    apply_incoming,
    get_inbox_counts,
    get_thread,
    list_inbox,
    list_messages,
    mark_dialog_read,
    mark_dialog_unread,
    open_dialog_by_lead,
    remember_dialogs,
    send_reply,
    start_direct_dialog,
    sync_inbox,
)
from app.folders import list_folders, pull_folders, sync_all_folders, sync_folder, update_folder
from app.leads import add_lead_from_dialog, create_lead, get_lead, lead_card, search_leads, update_lead
from app.notes import add_note
from app.outreach import (
    create_campaign,
    enroll_leads_from_crm,
    get_autopilot_status,
    get_campaign,
    get_outreach_schedule,
    import_campaign_csv,
    list_campaigns,
    preview_campaign_csv,
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
from app.bot_login import apply_bot_update, operator_status, poll_bot_login
from app.digest import apply_digest_callback, callback_data_from_update, send_morning_digest
from app.reminders import complete_reminder, create_reminder, list_reminders, postpone_reminder
from app.tasks import create_task, set_task_done
from app.telegram_gateway import GatewayError, TelethonGateway, listening_event
from app.views import list_views, save_view
from app.snippets import create_snippet, delete_snippet, list_snippets
from app.whatsapp_gateway import WhatsAppGateway
from app.analytics import get_analytics_summary

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

KEYS_DETAIL = "Нужны ключи TG_API_ID и TG_API_HASH с https://my.telegram.org/apps"


def load_env_file() -> None:
    import sys

    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
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
        if (not api_id or not api_hash) and "PYTEST_CURRENT_TEST" not in os.environ:
            api_id = "2040"
            api_hash = "b18441a1ff607e10a989891a5462e627"
    return api_id, api_hash


def should_enable_live_tg_runtime(conn=None, gateway=None) -> bool:
    if os.getenv("DISABLE_TG_WATCHES") == "1":
        return False
    if gateway is not None:
        return True
    if (
        "PYTEST_CURRENT_TEST" in os.environ
        or os.getenv("BOT_TOKEN") == "e2e-not-a-secret"
    ):
        return False
    if os.getenv("RENDER", "").strip().lower() in ("1", "true", "yes") or os.getenv(
        "ALLOW_LOCAL_TG_SESSIONS", ""
    ).strip().lower() in ("1", "true", "yes"):
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
    if app.state.gateway is not None:
        return app.state.gateway
    if not getattr(app.state, "tg_lease_ready", True):
        return None
    conn = getattr(app.state, "conn", None)
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
    gateway = request.app.state.gateway
    if gateway is not None:
        return gateway
    if not getattr(request.app.state, "tg_lease_ready", True):
        raise HTTPException(
            status_code=503,
            detail="Переключение релиза Telegram-сессий, повторите через 5 сек",
        )
    conn = getattr(request.app.state, "conn", None)
    if (
        not should_enable_live_tg_runtime(conn, None)
        and "PYTEST_CURRENT_TEST" not in os.environ
    ):
        raise HTTPException(
            status_code=409,
            detail="Локальное подключение Telegram отключено для защиты прод-сессий на Render от AUTH_KEY_DUPLICATED. Задайте ALLOW_LOCAL_TG_SESSIONS=1 для явного локального входа.",
        )
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
        account = load_account(request.app.state.conn, account_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Не найдено") from exc
    return _gateway_for_provider(request, account.get("provider") or "telegram")


def _call(func, *args, **kwargs):
    try:
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
        self._subs = {}

    def subscribe(self, account_id: int) -> asyncio.Queue:
        queue = asyncio.Queue()
        self._subs.setdefault(account_id, set()).add(queue)
        return queue

    def unsubscribe(self, account_id: int, queue) -> None:
        subs = self._subs.get(account_id)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subs.pop(account_id, None)

    def publish(self, account_id: int, dialog: dict) -> None:
        for queue in list(self._subs.get(account_id, ())):
            queue.put_nowait(dict(dialog))
        if account_id != 0:
            for queue in list(self._subs.get(0, ())):
                queue.put_nowait(dict(dialog))


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


async def ensure_private_inbox(app, account_id: int) -> None:
    # One successful pull per process. A later page open reads SQLite.
    synced = app.state.inbox_synced
    if account_id in synced:
        return
    async with app.state.inbox_sync_lock:
        if account_id in synced:
            return
        try:
            account = load_account(app.state.conn, account_id)
        except LookupError:
            return
        if account["status"] != "connected":
            return
        if str(account.get("session_path") or "").startswith("data/sessions/demo"):
            synced.add(account_id)
            return
        if "PYTEST_CURRENT_TEST" not in os.environ and isinstance(
            getattr(app.state, "gateway", None), (type(None), TelethonGateway)
        ):
            total_dlg_cnt = app.state.conn.execute(
                "SELECT COUNT(*) FROM dialogs"
            ).fetchone()[0]
            if int(total_dlg_cnt or 0) > 0:
                synced.add(account_id)
                return
        provider = str(account.get("provider") or "telegram").strip().lower()
        gateway = _resolve_app_gateway(app, provider)
        if gateway is None:
            return
        try:
            people = await gateway.load_inbox(account["session_path"], limit=30)
        except Exception:
            return
        if not isinstance(people, list):
            return
        try:
            sync_inbox(app.state.conn, account_id, people[:30])
        except (LookupError, ValueError):
            return
        synced.add(account_id)


async def start_account_watch(app, account_id: int) -> None:
    try:
        account = load_account(app.state.conn, account_id)
    except LookupError:
        return
    if account["status"] != "connected":
        return
    if str(account.get("session_path") or "").startswith("data/sessions/demo") and isinstance(
        getattr(app.state, "gateway", None), (type(None), TelethonGateway)
    ):
        return
    if (
        "PYTEST_CURRENT_TEST" not in os.environ
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
            dialog = apply_incoming(app.state.conn, account_id, message)
        except ValueError:
            return
        app.state.inbox_hub.publish(account_id, dialog)

    async def runner():
        try:
            await gateway.watch_private(session_path, on_message)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    task = asyncio.create_task(runner())
    app.state.inbox_tasks[account_id] = task
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
    except asyncio.TimeoutError:
        return


async def start_connected_watches(app) -> None:
    rows = app.state.conn.execute(
        "SELECT id, provider FROM accounts WHERE status = 'connected'"
    ).fetchall()
    for row in rows:
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
    gw = getattr(app.state, "gateway", None)
    if gw is not None and hasattr(gw, "close_all"):
        try:
            await gw.close_all()
        except Exception:
            pass


async def autopilot_loop(app: FastAPI, interval_seconds: float = 30.0) -> None:
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            tick_res = await run_autopilot_tick(
                app.state.conn,
                gateway=_resolve_app_gateway(app, "telegram"),
                wa_gateway=_resolve_app_gateway(app, "whatsapp"),
            )
            if isinstance(tick_res, dict) and int(tick_res.get("sent") or 0) > 0:
                from app.cloud_persistence import schedule_cloud_backup

                schedule_cloud_backup(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


async def start_autopilot_loop(app: FastAPI) -> None:
    if (
        not should_enable_live_tg_runtime(
            getattr(app.state, "conn", None), getattr(app.state, "gateway", None)
        )
        and "PYTEST_CURRENT_TEST" not in os.environ
    ):
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
    if (
        "PYTEST_CURRENT_TEST" in os.environ
        or os.environ.get("BOT_TOKEN") == "e2e-not-a-secret"
    ):
        return
    try:
        from app.airtable_sync import get_airtable_config, push_lead_to_airtable

        cfg = get_airtable_config(getattr(app.state, "conn", None))
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
    if (
        "PYTEST_CURRENT_TEST" in os.environ
        or os.environ.get("BOT_TOKEN") == "e2e-not-a-secret"
    ):
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
    if (
        "PYTEST_CURRENT_TEST" in os.environ
        or os.environ.get("BOT_TOKEN") == "e2e-not-a-secret"
    ):
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


def create_app(conn=None, gateway=None) -> FastAPI:
    load_env_file()

    async def _tg_lease_heartbeat_loop(app: FastAPI) -> None:
        from app.cloud_persistence import (
            check_tg_runtime_lease_owned,
            write_tg_runtime_lease,
        )

        while True:
            try:
                await asyncio.sleep(20.0)
                owned = await asyncio.to_thread(check_tg_runtime_lease_owned, None)
                if not owned:
                    app.state.tg_lease_ready = False
                    await stop_autopilot_loop(app)
                    await stop_inbox_watches(app)
                    return
                await asyncio.to_thread(
                    write_tg_runtime_lease, released=False, conn=None
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def _leased_tg_runtime_boot(app: FastAPI) -> None:
        from app.cloud_persistence import wait_and_acquire_tg_runtime_lease

        try:
            await asyncio.to_thread(wait_and_acquire_tg_runtime_lease, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        app.state.tg_lease_ready = True
        app.state.tg_lease_heartbeat_task = asyncio.create_task(
            _tg_lease_heartbeat_loop(app)
        )
        await start_connected_watches(app)
        await start_autopilot_loop(app)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "_needs_initial_session_backup", False):
            app.state._needs_initial_session_backup = False
            from app.cloud_persistence import schedule_cloud_backup

            schedule_cloud_backup(app, delay_seconds=3.0)
        live_tg_enabled = False
        if "PYTEST_CURRENT_TEST" in os.environ or gateway is not None:
            if os.getenv("DISABLE_TG_WATCHES") != "1":
                if "PYTEST_CURRENT_TEST" in os.environ:
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
            if (
                live_tg_enabled
                and "PYTEST_CURRENT_TEST" not in os.environ
                and os.getenv("BOT_TOKEN") != "e2e-not-a-secret"
            ):
                try:
                    from app.cloud_persistence import release_tg_runtime_lease

                    await asyncio.to_thread(release_tg_runtime_lease, None)
                except Exception:
                    pass

    app = FastAPI(title="CRM Telegram", lifespan=lifespan)
    if (
        conn is None
        and "PYTEST_CURRENT_TEST" not in os.environ
        and os.getenv("BOT_TOKEN") != "e2e-not-a-secret"
    ):
        try:
            from app.cloud_persistence import restore_from_cloud

            restore_from_cloud()
        except Exception:
            pass
    if conn is None:
        conn = connect()
    init_db(conn)
    needs_initial_session_backup = False
    if (
        "PYTEST_CURRENT_TEST" not in os.environ
        and os.getenv("BOT_TOKEN") != "e2e-not-a-secret"
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
    app.state.conn = conn
    app.state.gateway = gateway
    app.state.wa_gateway = WhatsAppGateway()
    app.state.inbox_hub = InboxHub()
    app.state.inbox_tasks = {}
    app.state.inbox_synced = set()
    app.state.inbox_sync_lock = asyncio.Lock()
    app.state.tg_lease_ready = True
    app.state.tg_lease_boot_task = None
    app.state.tg_lease_heartbeat_task = None
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
        ):
            from app.cloud_persistence import schedule_cloud_backup

            schedule_cloud_backup(request.app)
        return response
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    register_tg_web_routes(app)

    @app.get("/design/tokens.css")
    def design_tokens():
        return FileResponse(ROOT / "replica" / "design" / "tokens.css", media_type="text/css")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        default_view_mode = (
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
    def accounts_settings_patch(
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
        row = request.app.state.conn.execute(
            "SELECT session_path FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        gw = getattr(request.app.state, "gateway", None)
        if row and row["session_path"] and gw is not None and hasattr(gw, "set_proxy"):
            try:
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
        return list_accounts(request.app.state.conn)

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
    async def account_dialogs(account_id: int, request: Request, q: str = ""):
        try:
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
        return remember_dialogs(request.app.state.conn, account_id, found[:30])

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

    @app.get("/api/leads/{lead_id}/events")
    async def leads_events_list(lead_id: int, request: Request, limit: int = 200):
        from app.events import list_lead_events

        _call(get_lead, request.app.state.conn, lead_id)
        return _call(list_lead_events, request.app.state.conn, lead_id, limit=limit)

    @app.post("/api/leads/{lead_id}/notes")
    def lead_note_create(lead_id: int, body: NoteIn, request: Request):
        return _call(add_note, request.app.state.conn, lead_id, body.body)

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
    async def digest_webhook(request: Request):
        try:
            update = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Неизвестное действие") from exc
        data = _call(callback_data_from_update, update)
        return _call(apply_digest_callback, request.app.state.conn, data)

    @app.get("/api/operator")
    def operator_get(request: Request):
        return operator_status(request.app.state.conn)

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
    ):
        if account_id is None or account_id == 0:
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
        )

    @app.get("/api/inbox/counts")
    async def inbox_counts_get(
        request: Request,
        account_id: int | None = None,
        q: str = "",
        provider: str | None = None,
        status: str | None = None,
    ):
        counts = _call(
            get_inbox_counts,
            request.app.state.conn,
            account_id,
            q,
            provider,
            status,
        )
        return {"counts": counts, **counts}

    @app.post("/api/inbox/dialogs/{dialog_id}/unread")
    @app.post("/api/dialogs/{dialog_id}/unread")
    def inbox_dialog_mark_unread(dialog_id: int, request: Request):
        return _call(mark_dialog_unread, request.app.state.conn, dialog_id)

    @app.post("/api/inbox/dialogs/{dialog_id}/read")
    @app.post("/api/dialogs/{dialog_id}/read")
    def inbox_dialog_mark_read(dialog_id: int, request: Request):
        return _call(mark_dialog_read, request.app.state.conn, dialog_id)

    @app.get("/api/inbox/events")
    async def inbox_events(request: Request, account_id: int = 0):
        try:
            list_inbox(request.app.state.conn, account_id, "")
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Не найдено") from exc
        if account_id == 0:
            rows = request.app.state.conn.execute(
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
                yield _sse("snapshot", list_inbox(request.app.state.conn, account_id, ""))
                while True:
                    try:
                        dialog = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield _sse("live", {"ok": _watch_alive(request.app, account_id)})
                        yield ": ping\n\n"
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
        if target_id is None or int(target_id) == 0:
            rows = conn.execute(
                "SELECT id FROM accounts WHERE status = 'connected'"
            ).fetchall()
            for r in rows:
                aid = int(r["id"])
                request.app.state.inbox_synced.discard(aid)
                await start_account_watch(request.app, aid)
            return {"synced": len(rows), "dialogs": list_inbox(conn, 0, "")}
        aid = int(target_id)
        request.app.state.inbox_synced.discard(aid)
        await start_account_watch(request.app, aid)
        return {"synced": 1, "dialogs": list_inbox(conn, aid, "")}

    @app.post("/api/settings/tg-credentials")
    def settings_set_tg_credentials(body: TgCredentialsIn, request: Request):
        api_id = str(body.api_id or "").strip()
        api_hash = str(body.api_hash or "").strip()
        if not api_id.isdigit() or not api_hash:
            raise HTTPException(status_code=400, detail="Укажите числовой api_id и строку api_hash")
        now = utc_now()
        conn = request.app.state.conn
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

    @app.post("/api/dialogs/{dialog_id}/messages")
    async def dialog_send(dialog_id: int, body: MessageIn, request: Request):
        conn = request.app.state.conn
        demo_row = conn.execute(
            """
            SELECT a.session_path, a.provider
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.id = ?
            """,
            (dialog_id,),
        ).fetchone()
        tg_id, tg_hash = _resolve_tg_creds(conn)
        has_tg_creds = bool(tg_id and tg_hash)
        if demo_row and str(demo_row["provider"] or "telegram").strip().lower() == "whatsapp":
            gateway = _gateway_for_provider(request, "whatsapp")
        elif (
            demo_row
            and request.app.state.gateway is None
            and (
                str(demo_row["session_path"] or "").startswith("data/sessions/demo")
                or not has_tg_creds
            )
        ):
            gateway = None
        else:
            gateway = _gateway(request)
        return await _call_async(
            send_reply,
            conn,
            dialog_id,
            body.text,
            gateway,
            body.reply_to,
        )

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
        camp_row = request.app.state.conn.execute(
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
                    acc_row = request.app.state.conn.execute(
                        "SELECT session_path, provider FROM accounts WHERE id = ?",
                        (int(acc_ids[0]),),
                    ).fetchone()
                    if acc_row:
                        session_path = acc_row["session_path"]
                        provider = acc_row["provider"]
            except Exception:
                pass
        tg_id, tg_hash = _resolve_tg_creds(request.app.state.conn)
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
            request.app.state.conn,
            campaign_id,
            gateway=gateway,
            max_send=max_send,
            respect_delays=respect_delays,
            wa_gateway=wa_gateway,
        )

    @app.get("/api/analytics/summary")
    def analytics_summary(request: Request, period: str = "30d"):
        return _call(get_analytics_summary, request.app.state.conn, period=period)

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

        return _call(backup_to_cloud, request.app.state.conn)

    @app.post("/api/cloud/restore")
    async def cloud_restore_run(request: Request):
        from app.cloud_persistence import restore_from_cloud

        res = _call(
            lambda: restore_from_cloud(force=True, live_conn=request.app.state.conn)
        )
        if isinstance(res, dict) and res.get("restored"):
            init_db(request.app.state.conn)
            restore_disk_sessions(request.app.state.conn)
            gw = getattr(request.app.state, "gateway", None)
            if gw is not None and hasattr(gw, "_session_strings"):
                gw._session_strings.clear()
                _seed_gateway_session_strings(request.app.state.conn, gw)
        return res

    @app.post("/api/cloud/credentials")
    def cloud_credentials_set(body: CloudCredentialsIn, request: Request):
        from app.cloud_persistence import backup_to_cloud
        from app.db import utc_now

        now = utc_now()
        conn_obj = request.app.state.conn
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
        return _call(
            sync_airtable,
            request.app.state.conn,
            direction=direction,
            client=client,
        )

    @app.post("/api/airtable/push/{lead_id}")
    def airtable_push_lead(lead_id: int, request: Request):
        from app.airtable_sync import push_lead_to_airtable

        client = getattr(request.app.state, "airtable_client", None)
        return _call(
            push_lead_to_airtable,
            request.app.state.conn,
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
        if res.get("needs_airtable_push") and "PYTEST_CURRENT_TEST" not in os.environ:
            try:
                from app.airtable_sync import get_airtable_config, push_calendar_date_to_airtable

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
        get_leadgen_config,
        get_leadgen_status,
        push_discovered_now,
        save_leadgen_config,
        sync_leadgen_campaigns,
    )

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
        result = _call(seed_demo, request.app.state.conn)
        account_id = result.get("account_id")
        if account_id is not None:
            request.app.state.inbox_synced.add(account_id)
        return result

    return app


load_env_file()
app = create_app()
