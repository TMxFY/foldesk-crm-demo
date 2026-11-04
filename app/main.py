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
    import_session_accounts,
    list_accounts,
    reconnect_account,
    set_daily_limit,
    start_login,
    update_account_settings,
)
from app.accounts import _load as load_account
from app.db import ROOT, connect, init_db
from app.demo import DEMO_SESSION, seed_demo
from app.inbox import (
    apply_incoming,
    get_thread,
    list_inbox,
    list_messages,
    open_dialog_by_lead,
    remember_dialogs,
    send_reply,
    sync_inbox,
)
from app.folders import list_folders, pull_folders, sync_folder, update_folder
from app.leads import add_lead_from_dialog, create_lead, get_lead, lead_card, search_leads, update_lead
from app.notes import add_note
from app.outreach import (
    create_campaign,
    enroll_leads_from_crm,
    get_campaign,
    get_outreach_schedule,
    import_campaign_csv,
    list_campaigns,
    preview_campaign_csv,
    run_campaign_batch,
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
from app.whatsapp_gateway import WhatsAppGateway

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

KEYS_DETAIL = "Нужны ключи TG_API_ID и TG_API_HASH с https://my.telegram.org/apps"


def load_env_file() -> None:
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
    access_scope: str = "workspace"
    provider: str = "telegram"
    auth_mode: str = "code"


class AccountSettingsPatch(BaseModel):
    proxy_url: str | None = None
    access_scope: str | None = None
    daily_limit: int | None = None


class OutreachScheduleIn(BaseModel):
    timezone: str = "UTC"
    days: dict


class OpenByLeadIn(BaseModel):
    lead_id: int
    account_id: int | None = None


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


class LeadPatch(BaseModel):
    status: str | None = None
    note: str | None = None
    next_step: str | None = None
    name: str | None = None
    description: str | None = None
    email: str | None = None
    url: str | None = None
    folder_id: int | None = None


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
    steps: list[dict] | None = None


class CampaignUpdateIn(BaseModel):
    name: str | None = None
    account_id: int | None = None
    account_ids: list[int] | None = None
    daily_limit: int | None = None
    status: str | None = None
    steps: list[dict] | None = None


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


def _gateway(request: Request):
    gateway = request.app.state.gateway
    if gateway is not None:
        return gateway
    api_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise HTTPException(status_code=400, detail=KEYS_DETAIL)
    gateway = TelethonGateway()
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
        provider = str(account.get("provider") or "telegram").strip().lower()
        gateway = (
            getattr(app.state, "wa_gateway", None)
            if provider == "whatsapp"
            else app.state.gateway
        )
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
    provider = str(account.get("provider") or "telegram").strip().lower()
    gateway = (
        getattr(app.state, "wa_gateway", None)
        if provider == "whatsapp"
        else app.state.gateway
    )
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
        if provider != "whatsapp" and app.state.gateway is None:
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


def create_app(conn=None, gateway=None) -> FastAPI:
    load_env_file()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await start_connected_watches(app)
        try:
            yield
        finally:
            await stop_inbox_watches(app)

    app = FastAPI(title="CRM Telegram", lifespan=lifespan)
    if conn is None:
        conn = connect()
    init_db(conn)
    if os.getenv("DEMO_AUTOSEED", "").strip().lower() in ("1", "true", "yes"):
        existing_acc = conn.execute("SELECT COUNT(*) AS cnt FROM accounts").fetchone()
        if not existing_acc or int(existing_acc["cnt"] or 0) == 0:
            try:
                seed_demo(conn)
            except Exception:
                pass
    app.state.conn = conn
    app.state.gateway = gateway
    app.state.wa_gateway = WhatsAppGateway()
    app.state.inbox_hub = InboxHub()
    app.state.inbox_tasks = {}
    app.state.inbox_synced = set()
    app.state.inbox_sync_lock = asyncio.Lock()
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

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
        return await _call_async(
            start_login,
            request.app.state.conn,
            body.phone,
            gateway,
            proxy_url=body.proxy_url,
            access_scope=body.access_scope,
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
        gw = request.app.state.wa_gateway if provider == "whatsapp" else request.app.state.gateway
        imported = _call(
            import_session_accounts,
            request.app.state.conn,
            gw,
            payload,
        )
        return {"imported": imported, "count": len(imported)}

    @app.patch("/api/accounts/{account_id}/settings")
    def accounts_settings_patch(
        account_id: int, body: AccountSettingsPatch, request: Request
    ):
        fields = body.model_dump(exclude_unset=True)
        return _call(
            update_account_settings,
            request.app.state.conn,
            account_id,
            proxy_url=body.proxy_url,
            access_scope=body.access_scope,
            daily_limit=body.daily_limit,
            has_proxy=("proxy_url" in fields),
        )

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

    @app.post("/api/accounts/check-all")
    async def accounts_check_all(request: Request):
        return await _call_async(
            check_all_accounts,
            request.app.state.conn,
            request.app.state.gateway,
            request.app.state.wa_gateway,
        )

    @app.patch("/api/accounts/{account_id}/limit")
    def accounts_limit_patch(account_id: int, body: AccountLimitPatch, request: Request):
        return _call(
            set_daily_limit, request.app.state.conn, account_id, body.daily_limit
        )

    @app.post("/api/accounts/{account_id}/reconnect")
    async def accounts_reconnect(account_id: int, request: Request):
        gateway = _gateway_for_account(request, account_id)
        return await _call_async(
            reconnect_account, request.app.state.conn, account_id, gateway
        )

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
    def leads_create(body: LeadIn, request: Request):
        conn = request.app.state.conn
        if body.telegram_user_id is not None and body.account_id is not None:
            return _call(
                add_lead_from_dialog,
                conn,
                body.account_id,
                body.telegram_user_id,
                body.username,
                body.name,
                phone=body.phone,
                provider=body.provider,
            )
        return _call(
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
        )

    @app.get("/api/leads")
    def leads_search(
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

    @app.get("/api/leads/{lead_id}")
    def leads_get(lead_id: int, request: Request):
        return _call(get_lead, request.app.state.conn, lead_id)

    @app.get("/api/leads/{lead_id}/card")
    def leads_card(lead_id: int, request: Request):
        return _call(lead_card, request.app.state.conn, lead_id)

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
    def leads_patch(lead_id: int, body: LeadPatch, request: Request):
        fields = body.model_dump(exclude_unset=True)
        return _call(update_lead, request.app.state.conn, lead_id, **fields)

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
    async def inbox_list(request: Request, account_id: int | None = None, q: str = ""):
        if account_id is None or account_id == 0:
            rows = request.app.state.conn.execute(
                "SELECT id FROM accounts WHERE status = 'connected'"
            ).fetchall()
            for r in rows:
                await ensure_private_inbox(request.app, int(r["id"]))
        else:
            await ensure_private_inbox(request.app, account_id)
        return _call(list_inbox, request.app.state.conn, account_id, q)

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
        )

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
        has_tg_creds = bool(
            os.getenv("TG_API_ID", "").strip() and os.getenv("TG_API_HASH", "").strip()
        )
        if (
            demo_row
            and (
                str(demo_row["session_path"] or "").startswith("data/sessions/demo")
                or (
                    str(demo_row["provider"] or "telegram").strip().lower() != "whatsapp"
                    and request.app.state.gateway is None
                    and not has_tg_creds
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
        has_tg_creds = bool(
            os.getenv("TG_API_ID", "").strip() and os.getenv("TG_API_HASH", "").strip()
        )
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
        )

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
            SELECT a.session_path, a.provider
            FROM campaigns c
            LEFT JOIN accounts a ON a.id = c.account_id
            WHERE c.id = ?
            """,
            (campaign_id,),
        ).fetchone()
        has_tg_creds = bool(
            os.getenv("TG_API_ID", "").strip() and os.getenv("TG_API_HASH", "").strip()
        )
        wa_gateway = _gateway_for_provider(request, "whatsapp")
        if (
            camp_row
            and request.app.state.gateway is None
            and str(camp_row["session_path"] or "").startswith("data/sessions/demo")
        ):
            gateway = None
        elif camp_row and str(camp_row["provider"] or "").strip().lower() == "whatsapp":
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
        elif camp_row and camp_row["session_path"]:
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
