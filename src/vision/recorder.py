"""事件录像：把环形缓冲里的"事发前"帧 + 之后的帧写成一个 mp4。

两个实现：
  * `opencv`：用 cv2.VideoWriter（mp4v），最简单，CPU 软编码。
    1280×720@10fps 时 RK3576 单核 ~15% —— 只有事件触发时才录，完全可接受。
  * `gstreamer`：`mpph264enc` 硬编码（板子上装了 gst-rkmpp 时优先），CPU 接近 0。

不要试图用 ffmpeg 子进程传 100 张 JPEG —— 启动开销比编码本身还大。
真正的硬编码路径应该是"GStreamer 直接 tee 到 appsink + mp4mux"，那是第三阶段的活。
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from .ring import Frame

logger = logging.getLogger(__name__)
_GST_MPP_AVAILABLE: bool | None = None
_GST_MPP_LOCK = threading.Lock()


def _has_gst_mpp() -> bool:
    global _GST_MPP_AVAILABLE

    cached = _GST_MPP_AVAILABLE
    if cached is not None:
        return cached

    with _GST_MPP_LOCK:
        cached = _GST_MPP_AVAILABLE
        if cached is not None:
            return cached

        available = False
        if shutil.which("gst-inspect-1.0"):
            try:
                out = subprocess.run(
                    ["gst-inspect-1.0", "mpph264enc"],
                    capture_output=True, timeout=5, check=False,
                )
                available = out.returncode == 0
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("检测 mpph264enc 失败: %s", exc)

        _GST_MPP_AVAILABLE = available
        return available


class ClipWriter:
    def __init__(self, path: str, size: tuple[int, int], fps: int) -> None:
        self.path = path
        self.size = size
        self.fps = fps
        self.backend = "none"
        self._proc: subprocess.Popen[bytes] | None = None
        self._writer: cv2.VideoWriter | None = None
        self._lock = threading.Lock()
        self._opened = False

    def __enter__(self) -> "ClipWriter":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        w, h = self.size
        if _has_gst_mpp():
            cmd = [
                "gst-launch-1.0", "-q",
                "fdsrc", "fd=0", "!", "rawvideoparse",
                f"width={w}", f"height={h}", "format=rgb", f"framerate={self.fps}/1", "!",
                "videoconvert", "!", "mpph264enc", "!", "h264parse", "!",
                "mp4mux", "!", "filesink", f"location={self.path}",
            ]
            try:
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                self.backend = "gstreamer-mpp"
                self._opened = True
                logger.info("使用硬编码录像 %s", self.path)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("硬编码启动失败，回退 OpenCV: %s", exc)

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(self.path, fourcc, float(self.fps), (w, h))
        if not writer.isOpened():
            raise RuntimeError(f"无法写入 {self.path}")
        self._writer = writer
        self.backend = "opencv"
        self._opened = True
        logger.info("使用 OpenCV 录像 %s", self.path)

    def write_bgr(self, bgr: np.ndarray) -> None:
        """所有输入统一为 BGR，内部按后端转换。"""
        if not self._opened:
            return
        with self._lock:
            if self._writer is not None:
                if (bgr.shape[1], bgr.shape[0]) != self.size:
                    bgr = cv2.resize(bgr, self.size)
                self._writer.write(bgr)
            elif self._proc is not None and self._proc.stdin is not None:
                if (bgr.shape[1], bgr.shape[0]) != self.size:
                    bgr = cv2.resize(bgr, self.size)
                try:
                    self._proc.stdin.write(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).tobytes())
                except (BrokenPipeError, ValueError):
                    pass

    def write_jpeg(self, jpeg: bytes) -> None:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            self.write_bgr(img)

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        with self._lock:
            if self._writer is not None:
                self._writer.release()
                self._writer = None
            if self._proc is not None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                    self._proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    self._proc.kill()
                self._proc = None


class ClipJob:
    """后台线程：先写缓冲的 pre-roll，再持续写 duration 秒新帧。"""

    def __init__(
        self,
        path: str,
        size: tuple[int, int],
        fps: int,
        pre_frames: list[Frame],
        duration_s: float,
    ) -> None:
        self.path = path
        self.size = size
        self.fps = fps
        self.pre_frames = pre_frames
        self.deadline = time.monotonic() + duration_s
        self._thread = threading.Thread(target=self._run, name=f"clip:{os.path.basename(path)}", daemon=True)
        self._queue: deque[Frame] = deque()
        self._queue_lock = threading.Lock()
        self._queue_ready = threading.Event()

    def start(self) -> None:
        self._queue_ready.set()
        self._thread.start()

    def feed(self, frame: Frame) -> bool:
        """返回 False 表示已录满，可以从活动列表里移除。"""
        if not self._queue_ready.is_set() or time.monotonic() >= self.deadline:
            return False
        with self._queue_lock:
            self._queue.append(frame)
        return True

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            with ClipWriter(self.path, self.size, self.fps) as cw:
                for f in self.pre_frames:
                    cw.write_jpeg(f.jpeg)
                while time.monotonic() < self.deadline:
                    with self._queue_lock:
                        if self._queue:
                            f = self._queue.popleft()
                        else:
                            f = None
                    if f is not None:
                        cw.write_jpeg(f.jpeg)
                    else:
                        time.sleep(1.0 / (self.fps * 2))
        except Exception as exc:  # noqa: BLE001
            logger.exception("录像失败 %s: %s", self.path, exc)
        finally:
            self._queue_ready.clear()
            with self._queue_lock:
                self._queue.clear()


class ContinuousRecorder:
    """独立线程写持续录像，并按固定时长切分 MP4。"""

    def __init__(
        self,
        data_dir: str,
        size: tuple[int, int],
        fps: int,
        segment_seconds: float = 300.0,
        max_bytes: int = 1 * 1024 * 1024 * 1024,
    ) -> None:
        self.data_dir = data_dir
        self.size = size
        self.fps = fps
        self.segment_seconds = max(1.0, segment_seconds)
        self.max_bytes = max(1, max_bytes)
        self._queue: queue.Queue[Frame | np.ndarray] = queue.Queue(maxsize=max(2, fps * 2))
        self._lock = threading.Lock()
        self._accepting = True
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="continuous-recorder", daemon=True)

    @property
    def active(self) -> bool:
        return self._thread.is_alive() and self._accepting

    def start(self, initial_frames: list[Frame] | None = None) -> None:
        self._thread.start()
        for frame in initial_frames or []:
            self.feed(frame)

    def feed(self, frame: Frame) -> bool:
        return self._enqueue(frame)

    def feed_bgr(self, bgr: np.ndarray) -> bool:
        return self._enqueue(bgr)

    def _enqueue(self, item: Frame | np.ndarray) -> bool:
        with self._lock:
            if not self._accepting:
                return False
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                return False
            return True

    def stop(self, timeout: float = 15.0) -> None:
        with self._lock:
            if not self._thread.is_alive():
                self._accepting = False
                return
            self._accepting = False
        self._stopping.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("持续录像停止超时")

    def _run(self) -> None:
        from .ring import new_path

        writer: ClipWriter | None = None
        segment_started = 0.0
        try:
            while True:
                try:
                    frame = self._queue.get(timeout=0.25)
                except queue.Empty:
                    if self._stopping.is_set():
                        break
                    continue
                if writer is None or time.monotonic() - segment_started >= self.segment_seconds:
                    if writer is not None:
                        writer.close()
                        self._prune_segments()
                    path = new_path(self.data_dir, time.time(), "recordings/continuous", "mp4")
                    writer = ClipWriter(path, self.size, self.fps)
                    writer.open()
                    segment_started = time.monotonic()
                if isinstance(frame, Frame):
                    writer.write_jpeg(frame.jpeg)
                else:
                    writer.write_bgr(frame)
        except Exception as exc:  # noqa: BLE001
            logger.exception("持续录像失败: %s", exc)
        finally:
            if writer is not None:
                writer.close()
                self._prune_segments()

    def _prune_segments(self) -> None:
        root = Path(self.data_dir) / "recordings" / "continuous"
        if not root.exists():
            return

        files: list[tuple[Path, float, int]] = []
        total = 0
        for path in root.rglob("*.mp4"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((path, stat.st_mtime, stat.st_size))
            total += stat.st_size
        files.sort(key=lambda item: item[1])

        while total > self.max_bytes and len(files) > 1:
            oldest, _, size = files.pop(0)
            try:
                oldest.unlink()
                total -= size
            except OSError as exc:
                logger.warning("无法清理旧录像 %s: %s", oldest, exc)
                break
