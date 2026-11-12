from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

MAX_EVENT_RECORDS = 1000
_COMPACT_CHUNK_SIZE = 64 * 1024


def _trim_locked(fd: int) -> None:
    size = os.lseek(fd, 0, os.SEEK_END)
    cursor = size
    newline_count = 0
    cutoff: int | None = None

    while cursor and newline_count <= MAX_EVENT_RECORDS:
        start = max(0, cursor - _COMPACT_CHUNK_SIZE)
        chunk = os.pread(fd, cursor - start, start)
        for index in range(len(chunk) - 1, -1, -1):
            if chunk[index] != 0x0A:
                continue
            newline_count += 1
            if newline_count > MAX_EVENT_RECORDS:
                cutoff = start + index + 1
                break
        if cutoff is not None:
            break
        cursor = start

    if cutoff is None:
        return

    remaining = size - cutoff
    copied = 0
    while copied < remaining:
        chunk = os.pread(fd, min(_COMPACT_CHUNK_SIZE, remaining - copied), cutoff + copied)
        if not chunk:
            raise OSError("event log changed while compacting")
        written = 0
        while written < len(chunk):
            written += os.pwrite(fd, chunk[written:], copied + written)
        copied += len(chunk)
    os.ftruncate(fd, remaining)


def trim_event_log(path: str | Path) -> None:
    """Keep only the newest MAX_EVENT_RECORDS complete event lines."""
    target = Path(path)
    if not target.exists():
        return
    fd = os.open(target, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _trim_locked(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def append_event(path: str | Path, event: dict) -> None:
    """Append an event and retain only the newest records across concurrent processes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_END)
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        _trim_locked(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
