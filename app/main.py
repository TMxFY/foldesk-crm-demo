import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.accounts import confirm_login, confirm_password, list_accounts, start_login
from app.accounts import _load as load_account
from app.db import ROOT, connect, init_db
from app.inbox import get_thread, list_inbox, remember_dialogs, send_reply
from app.leads import add_lead_from_dialog, create_lead, get_lead, search_leads, update_lead
from app.reminders import complete_reminder, create_reminder, list_reminders, postpone_reminder
from app.telegram_gateway import GatewayError, TelethonGateway

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
    phone: str


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


class LeadPatch(BaseModel):
    status: str | None = None
    note: str | None = None
    next_step: str | None = None
    name: str | None = None
    description: str | None = None
    email: str | None = None
    url: str | None = None


class ReminderIn(BaseModel):
    lead_id: int
    due_at: str
    text: str


class PostponeIn(BaseModel):
    due_at: str


class MessageIn(BaseModel):
    text: str
    reply_to: int | None = None


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


def create_app(conn=None, gateway=None) -> FastAPI:
    load_env_file()
    app = FastAPI(title="CRM Telegram")
    if conn is None:
        conn = connect()
    init_db(conn)
    app.state.conn = conn
    app.state.gateway = gateway
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    @app.get("/design/tokens.css")
    def design_tokens():
        return FileResponse(ROOT / "replica" / "design" / "tokens.css", media_type="text/css")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {"request": request})

    @app.post("/api/accounts/start")
    async def accounts_start(body: StartIn, request: Request):
        gateway = _gateway(request)
        return await _call_async(start_login, request.app.state.conn, body.phone, gateway)

    @app.post("/api/accounts/{account_id}/code")
    async def accounts_code(account_id: int, body: CodeIn, request: Request):
        gateway = _gateway(request)
        return await _call_async(
            confirm_login, request.app.state.conn, account_id, body.code, gateway
        )

    @app.post("/api/accounts/{account_id}/password")
    async def accounts_password(account_id: int, body: PasswordIn, request: Request):
        gateway = _gateway(request)
        return await _call_async(
            confirm_password, request.app.state.conn, account_id, body.password, gateway
        )

    @app.get("/api/accounts")
    def accounts_list(request: Request):
        return list_accounts(request.app.state.conn)

    @app.get("/api/accounts/{account_id}/dialogs")
    async def account_dialogs(account_id: int, request: Request, q: str = ""):
        try:
            account = load_account(request.app.state.conn, account_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Не найдено") from exc
        if account["status"] != "connected":
            raise HTTPException(status_code=400, detail="Аккаунт не подключён")
        gateway = _gateway(request)
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
        )

    @app.get("/api/leads")
    def leads_search(request: Request, q: str = "", status: str = ""):
        return _call(
            search_leads,
            request.app.state.conn,
            q,
            status or None,
        )

    @app.get("/api/leads/{lead_id}")
    def leads_get(lead_id: int, request: Request):
        return _call(get_lead, request.app.state.conn, lead_id)

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

    @app.get("/api/inbox")
    def inbox_list(request: Request, account_id: int, q: str = ""):
        return _call(list_inbox, request.app.state.conn, account_id, q)

    @app.get("/api/dialogs/{dialog_id}/messages")
    async def dialog_messages(dialog_id: int, request: Request):
        gateway = _gateway(request)
        return await _call_async(
            get_thread, request.app.state.conn, dialog_id, gateway
        )

    @app.post("/api/dialogs/{dialog_id}/messages")
    async def dialog_send(dialog_id: int, body: MessageIn, request: Request):
        gateway = _gateway(request)
        return await _call_async(
            send_reply,
            request.app.state.conn,
            dialog_id,
            body.text,
            gateway,
            body.reply_to,
        )

    return app


load_env_file()
app = create_app()
