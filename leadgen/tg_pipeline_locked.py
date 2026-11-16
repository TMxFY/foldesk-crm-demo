#!/usr/bin/env python3
"""Run the TG pipeline only while holding atomic Telethon-session locks.

SoftUGC pipelines (`tg_pipeline.sh`, `job_monitor.py`) share authenticated
Telethon SQLite sessions (`tim2.session`, `tim3.session`). Holding kernel
`fcntl.flock` locks per canonical session file prevents concurrent SQLite
access and combined Telegram request bursts.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Iterable, Iterator


ROOT = Path(__file__).resolve().parents[1]
SESSION_OLD = os.environ.get("TG_SESSION_OLD", "/Users/timofei/.claude/skill-telegram/tim2.session")
SESSION_NEW = os.environ.get("TG_SESSION_NEW", "/Users/timofei/.claude/skill-telegram/tim3.session")
SESSION = SESSION_OLD


def canonical_session_path(session_path: str | Path) -> str:
    raw = os.path.abspath(os.path.expanduser(str(session_path).strip()))
    if not raw.endswith(".session"):
        raw = f"{raw}.session"
    return os.path.realpath(raw)


def session_lock_path(session_path: str | Path, lock_dir: str | Path = "/tmp") -> Path:
    canon = canonical_session_path(session_path)
    digest = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]
    stem = Path(canon).stem
    return Path(lock_dir) / f"softugc-tg-session-{stem}-{digest}.lock"


class HeldSessionLocks:
    """Keeps open lock file descriptors alive until explicitly closed."""

    def __init__(self, handles: list) -> None:
        self._handles = list(handles)

    def close(self) -> None:
        while self._handles:
            fh = self._handles.pop()
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass

    def __enter__(self) -> HeldSessionLocks:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def try_acquire_session_locks(
    session_paths: Iterable[str | Path],
    *,
    lock_dir: str | Path = "/tmp",
) -> HeldSessionLocks | None:
    lock_paths = sorted({session_lock_path(p, lock_dir=lock_dir) for p in session_paths if str(p).strip()})
    acquired_handles = []
    for lpath in lock_paths:
        lpath.parent.mkdir(parents=True, exist_ok=True)
        fh = lpath.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            HeldSessionLocks(acquired_handles).close()
            return None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        acquired_handles.append(fh)
    return HeldSessionLocks(acquired_handles)


@contextmanager
def acquire_session_locks(
    session_paths: Iterable[str | Path],
    *,
    lock_dir: str | Path = "/tmp",
) -> Iterator[bool]:
    held = try_acquire_session_locks(session_paths, lock_dir=lock_dir)
    if held is None:
        yield False
        return
    try:
        yield True
    finally:
        held.close()


def main() -> int:
    sessions = [SESSION_OLD, SESSION_NEW]
    with acquire_session_locks(sessions) as acquired:
        if not acquired:
            print("Telegram session is busy; skipping TG pipeline run", flush=True)
            return 0
        env = os.environ.copy()
        env.setdefault("TG_SESSION", SESSION_OLD)
        command = ["/bin/bash", str(ROOT / "leadgen" / "tg_pipeline.sh")]
        return subprocess.run(command, cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
