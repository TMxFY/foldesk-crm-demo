"""Cloudflare R2 (S3-compatible) automatic persistence for SQLite CRM DB and sessions."""

from __future__ import annotations

import asyncio
import base64
import datetime
import fcntl
import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.db import (
    DEFAULT_DB,
    OUTREACH_MEDIA_DIR,
    ROOT,
    locked_raw_connection,
    utc_now,
)
from app.runtime_config import is_ephemeral_runtime, is_vps_runtime, should_skip_dotenv

logger = logging.getLogger(__name__)

R2_OBJECT_KEY = "foldesk-crm/crm_state.bundle.json.gz"
R2_LEASE_KEY = "foldesk-crm/tg_runtime_lease.json"
INSTANCE_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
DEFAULT_TG_LOCK_PATH = Path("/run/crmtg-tg.lock")
FALLBACK_TG_LOCK_PATH = Path("/tmp/crmtg-tg.lock")

_TG_LOCAL_LOCK_MUTEX = threading.Lock()
_TG_LOCAL_LOCK_FILE: Any | None = None
_TG_LOCAL_LOCK_ACTIVE_PATH: Path | None = None
_CACHED_GIT_SHA: str | None = None
_CACHED_PUBLIC_IP: str | None = None
_LAST_SEEN_LEASE_OWNER: str | None = None


def _resolve_git_sha() -> str:
    global _CACHED_GIT_SHA
    env_sha = os.getenv("CRMTG_GIT_SHA", "").strip()
    if env_sha:
        return env_sha[:12]
    if _CACHED_GIT_SHA is not None:
        return _CACHED_GIT_SHA
    sha = "unknown"
    try:
        git_entry = ROOT / ".git"
        git_dir: Path | None = None
        if git_entry.is_dir():
            git_dir = git_entry
        elif git_entry.is_file():
            raw_gitdir = git_entry.read_text(encoding="utf-8", errors="ignore").strip()
            if raw_gitdir.startswith("gitdir:"):
                rel = raw_gitdir.split(":", 1)[1].strip()
                cand = Path(rel)
                git_dir = cand if cand.is_absolute() else (ROOT / cand).resolve()
        if git_dir is not None:
            head_file = git_dir / "HEAD"
            if head_file.is_file():
                head_txt = head_file.read_text(encoding="utf-8", errors="ignore").strip()
                if head_txt.startswith("ref:"):
                    ref_name = head_txt.split(":", 1)[1].strip()
                    ref_path = git_dir / ref_name
                    if not ref_path.is_file() and (git_dir / "commondir").is_file():
                        common_rel = (git_dir / "commondir").read_text(encoding="utf-8", errors="ignore").strip()
                        common_dir = (git_dir / common_rel).resolve()
                        ref_path = common_dir / ref_name
                    if ref_path.is_file():
                        sha = ref_path.read_text(encoding="utf-8", errors="ignore").strip()[:12] or "unknown"
                    else:
                        packed = git_dir / "packed-refs"
                        if not packed.is_file() and (git_dir / "commondir").is_file():
                            common_rel = (git_dir / "commondir").read_text(encoding="utf-8", errors="ignore").strip()
                            packed = (git_dir / common_rel).resolve() / "packed-refs"
                        if packed.is_file():
                            for line in packed.read_text(encoding="utf-8", errors="ignore").splitlines():
                                if line and not line.startswith("#") and line.endswith(f" {ref_name}"):
                                    sha = line.split()[0][:12]
                                    break
                elif len(head_txt) >= 7:
                    sha = head_txt[:12]
    except Exception:
        sha = "unknown"
    _CACHED_GIT_SHA = sha
    return sha


def _resolve_public_ip() -> str:
    global _CACHED_PUBLIC_IP
    env_ip = os.getenv("CRMTG_PUBLIC_IP", "").strip()
    if env_ip:
        return env_ip
    if _CACHED_PUBLIC_IP is not None:
        return _CACHED_PUBLIC_IP
    ip_val = ""
    if is_vps_runtime() and not is_ephemeral_runtime():
        try:
            import urllib.request

            with urllib.request.urlopen("https://api.ipify.org", timeout=1.5) as resp:
                raw_ip = resp.read(64).decode("ascii", errors="ignore").strip()
                if raw_ip:
                    ip_val = raw_ip
        except Exception:
            ip_val = ""
    if not ip_val:
        try:
            ip_val = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip_val = "unknown"
    _CACHED_PUBLIC_IP = ip_val or "unknown"
    return _CACHED_PUBLIC_IP


def acquire_tg_local_lock(lock_path: Path | str | None = None) -> bool:
    """Acquire non-blocking OS flock preventing two local processes from opening TG sessions."""
    global _TG_LOCAL_LOCK_FILE, _TG_LOCAL_LOCK_ACTIVE_PATH
    with _TG_LOCAL_LOCK_MUTEX:
        if _TG_LOCAL_LOCK_FILE is not None:
            if lock_path is None or _TG_LOCAL_LOCK_ACTIVE_PATH == Path(lock_path):
                return True
        candidates: list[Path] = []
        if lock_path is not None:
            candidates.append(Path(lock_path))
        else:
            env_p = os.getenv("CRMTG_TG_LOCK_PATH", "").strip()
            if env_p:
                candidates.append(Path(env_p))
            else:
                candidates.extend([DEFAULT_TG_LOCK_PATH, FALLBACK_TG_LOCK_PATH])

        last_err: Exception | None = None
        for cand in candidates:
            fh = None
            try:
                cand.parent.mkdir(parents=True, exist_ok=True)
                fh = open(cand, "a+", encoding="utf-8")
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError) as exc:
                    fh.close()
                    fh = None
                    if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in (11, 35):
                        logger.warning("Local TG OS lock already held at %s", cand)
                        return False
                    last_err = exc
                    continue
                try:
                    fh.seek(0)
                    fh.truncate()
                    fh.write(f"{os.getpid()} {INSTANCE_ID}\n")
                    fh.flush()
                except Exception:
                    pass
                prev_fh = _TG_LOCAL_LOCK_FILE
                _TG_LOCAL_LOCK_FILE = fh
                _TG_LOCAL_LOCK_ACTIVE_PATH = cand
                if prev_fh is not None:
                    try:
                        fcntl.flock(prev_fh.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
                    try:
                        prev_fh.close()
                    except Exception:
                        pass
                return True
            except PermissionError as exc:
                last_err = exc
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
                continue
            except OSError as exc:
                last_err = exc
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
                continue
        logger.warning("Failed to acquire local TG OS lock (%s): %s", candidates, last_err)
        return False


def release_tg_local_lock() -> None:
    global _TG_LOCAL_LOCK_FILE, _TG_LOCAL_LOCK_ACTIVE_PATH
    with _TG_LOCAL_LOCK_MUTEX:
        fh = _TG_LOCAL_LOCK_FILE
        _TG_LOCAL_LOCK_FILE = None
        _TG_LOCAL_LOCK_ACTIVE_PATH = None
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def is_tg_local_lock_held() -> bool:
    with _TG_LOCAL_LOCK_MUTEX:
        return _TG_LOCAL_LOCK_FILE is not None


def get_last_seen_tg_lease_owner() -> str | None:
    return _LAST_SEEN_LEASE_OWNER


def _read_dotenv() -> dict[str, str]:
    if should_skip_dotenv():
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
        is_ephemeral_runtime()
        or os.environ.get("CLOUD_BACKUP_ENABLED", "1").strip().lower() in ("0", "false", "no", "off")
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


def _extract_conn_db_path(conn: sqlite3.Connection | None) -> Path | None:
    if conn is None:
        return None
    try:
        with locked_raw_connection(conn) as raw_c:
            rows = raw_c.execute("PRAGMA database_list").fetchall()
        for r in rows:
            name = r[1] if not isinstance(r, dict) else r.get("name")
            file_p = r[2] if not isinstance(r, dict) else r.get("file")
            if name == "main" and file_p and str(file_p) != ":memory:":
                return Path(str(file_p))
    except Exception:
        return None
    return None


def backup_to_cloud(
    conn: sqlite3.Connection | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    effective_db_path = db_path
    if effective_db_path is None:
        effective_db_path = _extract_conn_db_path(conn) or DEFAULT_DB
    in_bg_thread = threading.current_thread() is not threading.main_thread()
    use_separate_conn = (conn is None or in_bg_thread) and effective_db_path.exists()
    cfg = _resolve_r2_config(conn if conn is not None else None)
    if not cfg and use_separate_conn:
        try:
            tmp_cfg_conn = sqlite3.connect(effective_db_path, timeout=15.0)
            try:
                cfg = _resolve_r2_config(tmp_cfg_conn)
            finally:
                tmp_cfg_conn.close()
        except Exception:
            cfg = None
    if not cfg:
        return {"ok": False, "configured": False, "reason": "R2 credentials not set"}

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    sessions_map: dict[str, str] = {}
    active_session_names: set[str] = set()
    try:
        src_conn = (
            sqlite3.connect(effective_db_path, timeout=30.0)
            if use_separate_conn
            else conn
        )
        dst_conn = sqlite3.connect(tmp_path, timeout=30.0)
        try:
            with locked_raw_connection(src_conn) as raw_src:
                raw_src.backup(dst_conn)
                try:
                    rows = src_conn.execute(
                        "SELECT session_path, session_blob FROM accounts WHERE session_path IS NOT NULL"
                    ).fetchall()
                    for s_path, s_blob in rows:
                        fname = Path(str(s_path)).name
                        if fname.endswith(".session"):
                            active_session_names.add(fname)
                            if s_blob and len(bytes(s_blob)) > 100:
                                sessions_map[fname] = base64.b64encode(bytes(s_blob)).decode("ascii")
                except Exception:
                    pass
        finally:
            dst_conn.close()
            if use_separate_conn:
                src_conn.close()
        db_bytes = tmp_path.read_bytes()
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    sessions_dir = ROOT / "data" / "sessions"
    if sessions_dir.exists():
        for sf in sorted(sessions_dir.glob("*.session")):
            if sf.name in active_session_names and sf.is_file() and sf.stat().st_size > 100:
                sessions_map[sf.name] = base64.b64encode(sf.read_bytes()).decode("ascii")

    media_map: dict[str, str] = {}
    if OUTREACH_MEDIA_DIR.exists():
        for mf in sorted(OUTREACH_MEDIA_DIR.iterdir()):
            if (
                mf.is_file()
                and mf.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                and 0 < mf.stat().st_size <= 2 * 1024 * 1024
            ):
                media_map[mf.name] = base64.b64encode(mf.read_bytes()).decode("ascii")

    saved_at = utc_now()
    bundle = {
        "version": 1,
        "saved_at": saved_at,
        "db_b64": base64.b64encode(db_bytes).decode("ascii"),
        "sessions": sessions_map,
        "outreach_media": media_map,
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


def restore_from_cloud(
    db_path: Path = DEFAULT_DB,
    *,
    force: bool = False,
    live_conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    cfg = _resolve_r2_config(live_conn)
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

    raw_db_bytes = base64.b64decode(db_b64)
    if len(raw_db_bytes) < 4096 or raw_db_bytes[:16] != b"SQLite format 3\x00":
        return {
            "ok": False,
            "restored": False,
            "configured": True,
            "reason": "corrupt_cloud_db",
        }

    saved_at = str(bundle.get("saved_at") or "").strip()
    saved_at_cmp = saved_at.rstrip("Z")
    has_valid_saved_at = bool(
        saved_at_cmp and re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", saved_at_cmp)
    )

    cloud_step_sum = 0
    cloud_camp_progress: dict[int, tuple[int, str]] = {}
    cloud_max = ""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as ctmp:
        ctmp_path = Path(ctmp.name)
    try:
        ctmp_path.write_bytes(raw_db_bytes)
        cconn = sqlite3.connect(ctmp_path, timeout=5.0)
        try:
            qc = cconn.execute("PRAGMA quick_check").fetchone()
            if not qc or str(qc[0] or "").strip().lower() != "ok":
                return {
                    "ok": False,
                    "restored": False,
                    "configured": True,
                    "reason": "corrupt_cloud_db",
                }
            for tbl, col in (
                ("leads", "updated_at"),
                ("dialogs", "updated_at"),
                ("messages", "sent_at"),
                ("campaign_recipients", "last_sent_at"),
                ("campaign_recipients", "updated_at"),
                ("kv_settings", "updated_at"),
                ("accounts", "created_at"),
                ("accounts", "updated_at"),
            ):
                try:
                    crow_ts = cconn.execute(f"SELECT MAX({col}) FROM {tbl}").fetchone()
                    if crow_ts and crow_ts[0] and str(crow_ts[0]).rstrip("Z") > cloud_max:
                        cloud_max = str(crow_ts[0]).rstrip("Z")
                except sqlite3.OperationalError:
                    pass
            try:
                crow = cconn.execute(
                    "SELECT COALESCE(SUM(COALESCE(current_step, 0)), 0) FROM campaign_recipients"
                ).fetchone()
                if crow and crow[0] is not None:
                    cloud_step_sum = int(crow[0])
                for c_id, c_sum, c_last in cconn.execute(
                    """
                    SELECT campaign_id,
                           COALESCE(SUM(COALESCE(current_step, 0)), 0),
                           COALESCE(MAX(COALESCE(last_sent_at, '')), '')
                    FROM campaign_recipients
                    GROUP BY campaign_id
                    """
                ).fetchall():
                    if c_id is not None:
                        cloud_camp_progress[int(c_id)] = (
                            int(c_sum or 0),
                            str(c_last or "").rstrip("Z"),
                        )
            except sqlite3.OperationalError:
                cloud_step_sum = 0
        finally:
            cconn.close()
    except sqlite3.DatabaseError:
        return {
            "ok": False,
            "restored": False,
            "configured": True,
            "reason": "corrupt_cloud_db",
        }
    finally:
        if ctmp_path.exists():
            ctmp_path.unlink()

    if not force and db_path.exists() and db_path.stat().st_size > 4096:
        try:
            local_conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                local_max = ""
                local_rows_total = 0
                for tbl in ("leads", "dialogs", "messages", "accounts", "campaign_recipients"):
                    try:
                        cnt_r = local_conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                        if cnt_r and cnt_r[0]:
                            local_rows_total += int(cnt_r[0])
                    except sqlite3.OperationalError:
                        pass
                for tbl, col in (
                    ("leads", "updated_at"),
                    ("dialogs", "updated_at"),
                    ("messages", "sent_at"),
                    ("campaign_recipients", "last_sent_at"),
                    ("campaign_recipients", "updated_at"),
                    ("kv_settings", "updated_at"),
                    ("accounts", "created_at"),
                    ("accounts", "updated_at"),
                ):
                    try:
                        row = local_conn.execute(f"SELECT MAX({col}) FROM {tbl}").fetchone()
                        if row and row[0] and str(row[0]).rstrip("Z") > local_max:
                            local_max = str(row[0]).rstrip("Z")
                    except sqlite3.OperationalError:
                        pass
                local_step_sum = 0
                local_camp_progress: dict[int, tuple[int, str]] = {}
                try:
                    s_row = local_conn.execute(
                        "SELECT COALESCE(SUM(COALESCE(current_step, 0)), 0) FROM campaign_recipients"
                    ).fetchone()
                    if s_row and s_row[0] is not None:
                        local_step_sum = int(s_row[0])
                    for c_id, c_sum, c_last in local_conn.execute(
                        """
                        SELECT campaign_id,
                               COALESCE(SUM(COALESCE(current_step, 0)), 0),
                               COALESCE(MAX(COALESCE(last_sent_at, '')), '')
                        FROM campaign_recipients
                        GROUP BY campaign_id
                        """
                    ).fetchall():
                        if c_id is not None:
                            local_camp_progress[int(c_id)] = (
                                int(c_sum or 0),
                                str(c_last or "").rstrip("Z"),
                            )
                except sqlite3.OperationalError:
                    local_step_sum = 0
            finally:
                local_conn.close()
        except sqlite3.Error as exc:
            return {
                "ok": False,
                "restored": False,
                "configured": True,
                "reason": "local_db_inspect_error",
                "error": str(exc),
            }

        local_non_empty = local_rows_total > 0 or bool(local_max)
        if local_non_empty and not has_valid_saved_at:
            return {
                "ok": False,
                "restored": False,
                "configured": True,
                "reason": "invalid_cloud_metadata",
            }

        if local_step_sum > 0 or local_camp_progress:
            ahead_campaign_id: int | None = None
            for c_id, (l_sum, l_last) in local_camp_progress.items():
                c_sum, c_last = cloud_camp_progress.get(c_id, (0, ""))
                if l_sum > c_sum or (l_last and l_last > c_last):
                    ahead_campaign_id = c_id
                    break
            if local_step_sum > cloud_step_sum or ahead_campaign_id is not None:
                return {
                    "ok": True,
                    "restored": False,
                    "configured": True,
                    "reason": "local_outreach_progress_ahead",
                    "local_step_sum": local_step_sum,
                    "cloud_step_sum": cloud_step_sum,
                    "ahead_campaign_id": ahead_campaign_id,
                }

        if local_max and (
            (has_valid_saved_at and local_max > saved_at_cmp)
            or (cloud_max and local_max > cloud_max)
        ):
            return {
                "ok": True,
                "restored": False,
                "configured": True,
                "reason": "local_db_newer",
                "local_updated_at": local_max,
                "cloud_saved_at": saved_at,
            }

    existing_auth_sessions: list[tuple] = []
    try:
        chk_conn = live_conn if live_conn is not None else (sqlite3.connect(db_path, timeout=5.0) if db_path.exists() else None)
        if chk_conn is not None:
            try:
                existing_auth_sessions = chk_conn.execute(
                    "SELECT token, user_id, created_at, expires_at FROM crm_auth_sessions"
                ).fetchall()
            except Exception:
                pass
            finally:
                if live_conn is None:
                    chk_conn.close()
    except Exception:
        pass

    if live_conn is not None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            tmp_path.write_bytes(raw_db_bytes)
            src_conn = sqlite3.connect(tmp_path, timeout=30.0)
            try:
                with locked_raw_connection(live_conn) as raw_live:
                    src_conn.backup(raw_live)
                    if existing_auth_sessions:
                        try:
                            for srow in existing_auth_sessions:
                                live_conn.execute(
                                    "INSERT OR IGNORE INTO crm_auth_sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                                    (srow[0], srow[1], srow[2], srow[3]),
                                )
                            live_conn.commit()
                        except Exception:
                            pass
            finally:
                src_conn.close()
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(raw_db_bytes)
        if existing_auth_sessions:
            try:
                with sqlite3.connect(db_path, timeout=5.0) as dconn:
                    for srow in existing_auth_sessions:
                        dconn.execute(
                            "INSERT OR IGNORE INTO crm_auth_sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                            (srow[0], srow[1], srow[2], srow[3]),
                        )
                    dconn.commit()
            except Exception:
                pass

    sessions_map = bundle.get("sessions") or {}
    sessions_dir = ROOT / "data" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    restored_sessions = 0
    allowed_session_names: set[str] | None = None
    try:
        chk_c = live_conn if live_conn is not None else (sqlite3.connect(db_path, timeout=5.0) if db_path.exists() else None)
        if chk_c is not None:
            try:
                rows = chk_c.execute("SELECT session_path FROM accounts WHERE session_path IS NOT NULL").fetchall()
                allowed_session_names = {Path(str(r[0])).name for r in rows if r[0]}
            finally:
                if live_conn is None:
                    chk_c.close()
    except Exception:
        allowed_session_names = None

    if isinstance(sessions_map, dict):
        for fname, b64_data in sessions_map.items():
            clean_name = Path(str(fname)).name
            if not clean_name.endswith(".session") or not b64_data:
                continue
            if allowed_session_names is not None and clean_name not in allowed_session_names:
                continue
            (sessions_dir / clean_name).write_bytes(base64.b64decode(b64_data))
            restored_sessions += 1

    media_map = bundle.get("outreach_media") or {}
    restored_media = 0
    if isinstance(media_map, dict) and media_map:
        OUTREACH_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        for fname, b64_data in media_map.items():
            clean_name = Path(str(fname)).name
            if Path(clean_name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"} or not b64_data:
                continue
            (OUTREACH_MEDIA_DIR / clean_name).write_bytes(base64.b64decode(b64_data))
            restored_media += 1

    return {
        "ok": True,
        "restored": True,
        "configured": True,
        "saved_at": bundle.get("saved_at"),
        "restored_sessions": restored_sessions,
        "restored_media": restored_media,
    }


def get_cloud_status(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    cfg = _resolve_r2_config(conn)
    return {
        "configured": bool(cfg),
        "bucket": cfg["bucket"] if cfg else None,
        "object_key": R2_OBJECT_KEY,
    }


def read_tg_runtime_lease(
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    global _LAST_SEEN_LEASE_OWNER
    cfg = _resolve_r2_config(conn)
    if not cfg:
        return None
    url, headers = _sigv4_headers("GET", cfg, R2_LEASE_KEY, b"")
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 404:
                return None
            if resp.status_code == 200:
                if not resp.content:
                    return None
                try:
                    parsed = json.loads(resp.content.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None
                if isinstance(parsed, dict):
                    owner = parsed.get("instance_id")
                    if owner:
                        _LAST_SEEN_LEASE_OWNER = str(owner)
                    return parsed
                return None
            return {"_read_error": True, "status_code": resp.status_code}
    except Exception:
        return {"_read_error": True}


def _is_tg_runtime_lease_expired(
    lease: dict[str, Any],
    *,
    now_epoch: float,
    lease_ttl_seconds: float,
) -> bool:
    raw_exp = lease.get("expires_at")
    if raw_exp is not None and str(raw_exp).strip() != "":
        try:
            return now_epoch >= float(raw_exp)
        except (TypeError, ValueError):
            try:
                exp_str = str(raw_exp).strip()
                if exp_str.endswith("Z"):
                    exp_str = exp_str[:-1] + "+00:00"
                exp_dt = datetime.datetime.fromisoformat(exp_str)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
                return now_epoch >= exp_dt.timestamp()
            except ValueError:
                return False
    try:
        hb_epoch = float(lease.get("heartbeat_epoch") or 0)
    except (TypeError, ValueError):
        return False
    if hb_epoch <= 0:
        return False
    return (now_epoch - hb_epoch) >= lease_ttl_seconds


def check_tg_runtime_lease_owned(
    conn: sqlite3.Connection | None = None,
) -> bool:
    cfg = _resolve_r2_config(conn)
    if not cfg:
        return True
    lease = read_tg_runtime_lease(conn)
    if lease is None:
        return True
    if lease.get("_read_error"):
        return False
    owner = lease.get("instance_id")
    if owner and owner != INSTANCE_ID and bool(lease.get("released")) is False:
        return False
    return True


def write_tg_runtime_lease(
    *,
    released: bool = False,
    conn: sqlite3.Connection | None = None,
    lease_ttl_seconds: float = 45.0,
) -> bool:
    global _LAST_SEEN_LEASE_OWNER
    cfg = _resolve_r2_config(conn)
    if not cfg:
        return False
    now_epoch = time.time()
    payload = json.dumps(
        {
            "instance_id": INSTANCE_ID,
            "host": socket.gethostname(),
            "public_ip": _resolve_public_ip(),
            "pid": os.getpid(),
            "git_sha": _resolve_git_sha(),
            "heartbeat_epoch": now_epoch,
            "expires_at": now_epoch + max(1.0, float(lease_ttl_seconds)),
            "updated_at": utc_now(),
            "released": bool(released),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    url, headers = _sigv4_headers("PUT", cfg, R2_LEASE_KEY, payload)
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.put(url, content=payload, headers=headers)
            ok = resp.status_code in (200, 201, 204)
            if ok and not released:
                _LAST_SEEN_LEASE_OWNER = INSTANCE_ID
            return ok
    except Exception:
        return False


def wait_and_acquire_tg_runtime_lease(
    conn: sqlite3.Connection | None = None,
    *,
    max_wait_seconds: float = 45.0,
    poll_interval: float = 2.5,
    lease_ttl_seconds: float = 45.0,
    verify_delay_seconds: float = 1.5,
    lock_path: Path | str | None = None,
    acquire_local_lock: bool = True,
) -> bool:
    if acquire_local_lock and not acquire_tg_local_lock(lock_path=lock_path):
        return False
    cfg = _resolve_r2_config(conn)
    if not cfg:
        return True
    started = time.monotonic()
    while True:
        lease = read_tg_runtime_lease(conn)
        free = False
        if lease is None:
            free = True
        elif lease.get("_read_error"):
            free = False
        elif lease.get("instance_id") == INSTANCE_ID:
            free = True
        elif bool(lease.get("released")) is True:
            free = True
        elif _is_tg_runtime_lease_expired(
            lease, now_epoch=time.time(), lease_ttl_seconds=lease_ttl_seconds
        ):
            free = True
        if free:
            wrote = write_tg_runtime_lease(
                released=False, conn=conn, lease_ttl_seconds=lease_ttl_seconds
            )
            if wrote:
                delay = max(0.0, float(verify_delay_seconds))
                if delay > 0:
                    time.sleep(delay)
                confirmed = read_tg_runtime_lease(conn)
                if (
                    confirmed is not None
                    and not confirmed.get("_read_error")
                    and confirmed.get("instance_id") == INSTANCE_ID
                    and bool(confirmed.get("released")) is False
                ):
                    return True
                logger.warning(
                    "R2 TG lease read-after-write verification lost race (observed owner=%s)",
                    confirmed.get("instance_id") if isinstance(confirmed, dict) else None,
                )
        remaining = max_wait_seconds - (time.monotonic() - started)
        if remaining <= 0:
            if acquire_local_lock:
                release_tg_local_lock()
            return False
        time.sleep(min(poll_interval, max(0.01, remaining)))


def release_tg_runtime_lease(conn: sqlite3.Connection | None = None) -> bool:
    try:
        cfg = _resolve_r2_config(conn)
        if not cfg:
            return True
        cur = read_tg_runtime_lease(conn)
        if cur and cur.get("_read_error"):
            return False
        if cur and cur.get("instance_id") not in (None, INSTANCE_ID):
            return False
        return write_tg_runtime_lease(released=True, conn=conn)
    finally:
        release_tg_local_lock()


def schedule_cloud_backup(app: Any, delay_seconds: float = 1.5) -> None:
    if is_ephemeral_runtime():
        return
    if not getattr(app.state, "tg_lease_ready", True):
        return
    try:
        from app.main import should_enable_live_tg_runtime

        if not should_enable_live_tg_runtime(
            getattr(app.state, "conn", None), getattr(app.state, "gateway", None)
        ):
            return
    except Exception:
        pass
    existing_task = getattr(app.state, "cloud_backup_task", None)
    if existing_task is not None and not existing_task.done():
        app.state._cloud_backup_rerun = True
        return

    async def _runner():
        while True:
            app.state._cloud_backup_rerun = False
            await asyncio.sleep(delay_seconds)
            try:
                if getattr(app.state, "tg_lease_ready", True) and await asyncio.to_thread(
                    check_tg_runtime_lease_owned, None
                ):
                    await asyncio.to_thread(backup_to_cloud, None, DEFAULT_DB)
            except Exception:
                pass
            if not getattr(app.state, "_cloud_backup_rerun", False):
                break

    try:
        loop = asyncio.get_running_loop()
        app.state.cloud_backup_task = loop.create_task(_runner())
    except RuntimeError:
        pass
