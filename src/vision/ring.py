"""环形缓冲：保存最近 N 秒的 JPEG 帧，用于事件发生时补齐"事发前"画面。

为什么不存原始帧：1280×720×3 一帧 2.7 MB，10 s@10 fps 就是 276 MB，
在 4 GB 内存的板子上会直接把系统拖死。存 JPEG（缩放到 640 宽）后约 3 MB，
解码 100 张也只要几十毫秒，代价可以接受。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Frame:
    ts: float
    jpeg: bytes
    width: int
    height: int
    # 该帧对应的检测框（已缩放到本帧尺寸）；只在写事件录像/快照时画上，缓冲里保留原图
    dets: tuple = ()


class RingBuffer:
    def __init__(self, maxlen: int) -> None:
        self._buf: deque[Frame] = deque(maxlen=max(1, maxlen))
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._buf)

    def push(self, frame: Frame) -> None:
        with self._lock:
            self._buf.append(frame)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def snapshot(self) -> list[Frame]:
        with self._lock:
            return list(self._buf)

    def latest(self) -> Frame | None:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def since(self, ts: float) -> list[Frame]:
        with self._lock:
            return [f for f in self._buf if f.ts >= ts]

    def oldest_ts(self) -> float:
        with self._lock:
            return self._buf[0].ts if self._buf else 0.0


def new_path(data_dir: str, ts: float, kind: str, ext: str) -> str:
    """按天分目录，方便清理与回放。"""
    import os

    day = datetime.fromtimestamp(ts).strftime("%Y%m%d")
    folder = os.path.join(data_dir, kind, day)
    os.makedirs(folder, exist_ok=True)
    name = datetime.fromtimestamp(ts).strftime("%H%M%S_%f")[:-3]
    return os.path.join(folder, f"{name}.{ext}")
