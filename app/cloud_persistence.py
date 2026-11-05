"""Cloudflare R2 (S3-compatible) automatic persistence for SQLite CRM DB and sessions."""

from __future__ import annotations

import asyncio
import base64
import datetime
import gzip
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app.db import DEFAULT_DB, ROOT, utc_now

R2_OBJECT_KEY = "foldesk-crm/crm_state.bundle.json.gz"


def _read_dotenv() -> dict[str, str]:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return {}
    return out


def _resolve_r2_config(conn: sqlite3.Connection | None = None) -> dict[str, str] | None:
    if (
        "PYTEST_CURRENT_TEST" in os.environ
        or os.environ.get("BOT_TOKEN") == "e2e-not-a-secret"
    ):
        return None
    dotenv_vals = _read_dotenv()
    kv_vals: dict[str, str] = {}
    try:
        if conn is not None:
            rows = conn.execute(
                "SELECT key, value FROM kv_settings WHERE key IN ('r2_account_id', 'r2_access_key', 'r2_secret_key', 'r2_bucket')"
            ).fetchall()
            kv_vals = {str(r[0]): str(r[1] or "") for r in rows}
        elif DEFAULT_DB.exists():
            with sqlite3.connect(DEFAULT_DB) as c:
                rows = c.execute(
                    "SELECT key, value FROM kv_settings WHERE key IN ('r2_account_id', 'r2_access_key', 'r2_secret_key', 'r2_bucket')"
                ).fetchall()
                kv_vals = {str(r[0]): str(r[1] or "") for r in rows}
    except Exception:
        pass

    account_id = (
        os.environ.get("R2_ACCOUNT_ID")
        or dotenv_vals.get("R2_ACCOUNT_ID")
        or kv_vals.get("r2_account_id")
        or ""
    ).strip()
    access_key = (
        os.environ.get("R2_ACCESS_KEY")
        or dotenv_vals.get("R2_ACCESS_KEY")
        or kv_vals.get("r2_access_key")
        or ""
    ).strip()
    secret_key = (
        os.environ.get("R2_SECRET_KEY")
        or dotenv_vals.get("R2_SECRET_KEY")
        or kv_vals.get("r2_secret_key")
        or ""
    ).strip()
    bucket = (
        os.environ.get("R2_BUCKET")
        or dotenv_vals.get("R2_BUCKET")
        or kv_vals.get("r2_bucket")
        or ""
    ).strip()

    if not (account_id and access_key and secret_key and bucket):
        return None
    return {
        "account_id": account_id,
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket": bucket,
    }


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_headers(
    method: str,
    cfg: dict[str, str],
    object_key: str,
    payload: bytes = b"",
) -> tuple[str, dict[str, str]]:
    host = f"{cfg['account_id']}.r2.cloudflarestorage.com"
    region = "auto"
    service = "s3"
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    uri_path = f"/{cfg['bucket']}/{object_key.lstrip('/')}"
    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = (
        f"{method}\n{uri_path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    )
    k_date = _sign(("AWS4" + cfg["secret_key"]).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    auth_header = (
        f"{algorithm} Credential={cfg['access_key']}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": auth_header,
    }
    return f"https://{host}{uri_path}", headers


def backup_to_cloud(
    conn: sqlite3.Connection | None = None,
    db_path: Path = DEFAULT_DB,
) -> dict[str, Any]:
    cfg = _resolve_r2_config(conn)
    if not cfg:
        return {"ok": False, "configured": False, "reason": "R2 credentials not set"}

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        src_conn = conn if conn is not None else sqlite3.connect(db_path)
        dst_conn = sqlite3.connect(tmp_path)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            if conn is None:
                src_conn.close()
        db_bytes = tmp_path.read_bytes()
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    sessions_dir = ROOT / "data" / "sessions"
    sessions_map: dict[str, str] = {}
    if sessions_dir.exists():
        for sf in sorted(sessions_dir.glob("*.session")):
            if sf.is_file() and sf.stat().st_size > 100:
                sessions_map[sf.name] = base64.b64encode(sf.read_bytes()).decode("ascii")

    saved_at = utc_now()
    bundle = {
        "version": 1,
        "saved_at": saved_at,
        "db_b64": base64.b64encode(db_bytes).decode("ascii"),
        "sessions": sessions_map,
    }
    compressed = gzip.compress(
        json.dumps(bundle, ensure_ascii=False).encode("utf-8"), compresslevel=6
    )
    url, headers = _sigv4_headers("PUT", cfg, R2_OBJECT_KEY, compressed)
    with httpx.Client(timeout=25.0) as client:
        resp = client.put(url, content=compressed, headers=headers)
        if resp.status_code not in (200, 201, 204):
            return {
                "ok": False,
                "configured": True,
                "status_code": resp.status_code,
                "error": resp.text[:200],
            }
    return {
        "ok": True,
        "configured": True,
        "saved_at": saved_at,
        "size_bytes": len(compressed),
        "sessions_count": len(sessions_map),
    }


def restore_from_cloud(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    cfg = _resolve_r2_config(None)
    if not cfg:
        return {"ok": False, "restored": False, "configured": False}
    url, headers = _sigv4_headers("GET", cfg, R2_OBJECT_KEY, b"")
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200 or not resp.content:
                return {
                    "ok": False,
                    "restored": False,
                    "configured": True,
                    "status_code": resp.status_code,
                }
            raw_json = gzip.decompress(resp.content).decode("utf-8")
            bundle = json.loads(raw_json)
    except Exception as exc:
        return {"ok": False, "restored": False, "configured": True, "error": str(exc)}

    db_b64 = bundle.get("db_b64")
    if not db_b64:
        return {"ok": False, "restored": False, "configured": True}

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(base64.b64decode(db_b64))

    sessions_map = bundle.get("sessions") or {}
    sessions_dir = ROOT / "data" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    restored_sessions = 0
    if isinstance(sessions_map, dict):
        for fname, b64_data in sessions_map.items():
            clean_name = Path(str(fname)).name
            if not clean_name.endswith(".session") or not b64_data:
                continue
            (sessions_dir / clean_name).write_bytes(base64.b64decode(b64_data))
            restored_sessions += 1

    return {
        "ok": True,
        "restored": True,
        "configured": True,
        "saved_at": bundle.get("saved_at"),
        "restored_sessions": restored_sessions,
    }


def get_cloud_status(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    cfg = _resolve_r2_config(conn)
    return {
        "configured": bool(cfg),
        "bucket": cfg["bucket"] if cfg else None,
        "object_key": R2_OBJECT_KEY,
    }


def schedule_cloud_backup(app: Any, delay_seconds: float = 1.5) -> None:
    if (
        "PYTEST_CURRENT_TEST" in os.environ
        or os.environ.get("BOT_TOKEN") == "e2e-not-a-secret"
    ):
        return
    existing_task = getattr(app.state, "cloud_backup_task", None)
    if existing_task is not None and not existing_task.done():
        return

    async def _runner():
        await asyncio.sleep(delay_seconds)
        try:
            await asyncio.to_thread(backup_to_cloud, getattr(app.state, "conn", None))
        except Exception:
            pass

    try:
        loop = asyncio.get_running_loop()
        app.state.cloud_backup_task = loop.create_task(_runner())
    except RuntimeError:
        pass
