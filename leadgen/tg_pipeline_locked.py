#!/usr/bin/env python3
"""Run the TG pipeline only while holding its shared Telethon-session lock.

ForexSbor and CreatorOS deliberately use the same authenticated Telethon session.
Keeping an advisory lock for the whole child process prevents concurrent SQLite
access and avoids combining their Telegram request rates.
"""

import fcntl
import hashlib
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSION = "/Users/timofei/.claude/skill-telegram/tim2.session"
LOCK = Path("/tmp") / (
    "creatoros-telegram-"
    + hashlib.sha1(os.path.abspath(SESSION).encode()).hexdigest()[:16]
    + ".lock"
)


def main() -> int:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Telegram session is busy; skipping TG pipeline run", flush=True)
            return 0

        command = ["/bin/bash", str(ROOT / "leadgen" / "tg_pipeline.sh")]
        return subprocess.run(command, cwd=ROOT, env=os.environ.copy()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
