from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path


def append_event(path: str | Path, event: dict) -> None:
    """Append one complete JSON event while coordinating concurrent processes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
