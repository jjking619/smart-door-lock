"""visiond 采集主循环。

数据流（单线程，避免相机句柄在多个线程间抢）：

    相机 → 取帧 → 缩略 JPEG → 入环形缓冲
                  ↘ 每 N 帧 → 推理 → 规则 → 事件 → 录像任务
                  ↘ 按需 → MJPEG 预览（只有有人看时才编码）

关键取舍：
* **推理与录像解耦**：推理每 N 帧跑一次，录像用环形缓冲补 pre-roll，
  所以推理慢不会丢画面，录像也不会拖慢检测。
* **预览只编码一次**：预览帧直接复用环形缓冲里已缩好的 JPEG，
  100 个客户端和 1 个客户端 CPU 一样。
* **相机异常自动重启**：USB 摄像头热插拔/掉线很常见，捕获 5 次失败就重开。

运行：
    python -m device.visiond.app            # 采集 + 检测 + 录像
    VISION_SOURCE=test python -m device.visiond.app   # 无相机时用合成画面自测
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import cv2
import numpy as np

from .config import Config
from .detector import Detector, draw
from .events import EventBus
from .recorder import ClipJob
from .ring import Frame, RingBuffer, new_path
from .rules import Rules

logger = logging.getLogger("visiond")


class SyntheticCamera:
    """无硬件自测：画渐变背景 + 一个来回移动的"人形"。

    用途：在没有摄像头/模型的环境下验证环形缓冲、录像、事件规则是否正常。
    """

    def __init__(self, w: int, h: int, fps: int) -> None:
        self.w, self.h, self.fps = w, h, fps
        self._i = 0

    def read(self) -> tuple[bool, np.ndarray]:
        frame = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        frame[:, :] = (40, 40, 45)
        x = int((np.sin(self._i / 20) * 0.35 + 0.5) * self.w)
        cv2.rectangle(frame, (x, int(self.h * 0.35)), (x + 60, int(self.h * 0.9)), (200, 180, 160), -1)
        cv2.putText(frame, f"SYNTH {self._i}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        self._i += 1
        time.sleep(1.0 / self.fps)
        return True, frame

    def release(self) -> None:
        return


def open_camera(cfg: Config):
    if cfg.source == "test":
        return SyntheticCamera(*cfg.capture_size, cfg.fps)

    src = int(cfg.source) if cfg.source.isdigit() else cfg.source
    cap = cv2.VideoCapture(src)
    # MJPG 比默认 YUYV 更省带宽，USB2 上能跑到 720p@30
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
    cap.set(cv2.CAP_PROP_FPS, cfg.fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # 关键：避免读到"旧帧"
    if not cap.isOpened():
        raise RuntimeError(f"打不开相机 {cfg.source}")
    return cap


class VisionService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.ring = RingBuffer(cfg.ring_len)
        self.bus = EventBus(
            cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_topic,
            cfg.mqtt_username, cfg.mqtt_password, cfg.data_dir,
        )
        self.rules = Rules(cfg)
        self.clips: list[ClipJob] = []
        self.preview_jpeg: bytes = b""
        self.preview_clients = 0
        self.fps = 0.0
        self._stop = asyncio.Event()

        self.detector: Detector | None = None
        try:
            self.detector = Detector(
                cfg.model_path, cfg.model_input, cfg.conf, cfg.nms
            )
            self.detector.set_names(_default_names())
            logger.info("检测器就绪 backend=%s", self.detector.backend)
        except FileNotFoundError:
            logger.warning("未找到模型 %s，仅录像不检测", cfg.model_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("检测器初始化失败（仅录像）: %s", exc)

    # ------------------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._loop)
        finally:
            self.bus.close()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        cfg = self.cfg
        cap = None
        fails = 0
        frame_i = 0
        t_last = time.monotonic()
        last_dets = []

        while not self._stop.is_set():
            if cap is None:
                try:
                    cap = open_camera(cfg)
                    fails = 0
                    logger.info("相机已打开 %s", cfg.source)
                except Exception as exc:  # noqa: BLE001
                    logger.error("打开相机失败，2 s 后重试: %s", exc)
                    time.sleep(2)
                    continue

            ok, frame = cap.read()
            if not ok or frame is None:
                fails += 1
                if fails >= 5:
                    logger.warning("相机连续失败，重新打开")
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001
                        pass
                    cap = None
                time.sleep(0.05)
                continue
            fails = 0
            frame_i += 1

            now = time.monotonic()
            if frame_i % 10 == 0:
                dt = now - t_last
                self.fps = 10.0 / dt if dt > 0 else 0.0
                t_last = now

            # ---- 推理 ----
            if self.detector and frame_i % cfg.infer_every_n == 0:
                try:
                    last_dets = self.detector.detect(frame)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("推理失败: %s", exc)
                    last_dets = []

                events = self.rules.update(last_dets, cfg.width, cfg.height, now)
                for ev in events:
                    self._handle_event(ev)

            # ---- 环形缓冲 ----
            small = cv2.resize(frame, (640, int(640 * cfg.height / cfg.width)))
            ok2, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, cfg.jpeg_quality])
            if ok2:
                self.ring.push(Frame(now, jpg.tobytes(), small.shape[1], small.shape[0]))

            # ---- 预览：只有有人看时才画框 ----
            if self.preview_clients > 0:
                preview = frame
                if self.detector:
                    preview = draw(frame.copy(), last_dets, self.fps, [
                        ("doorstep", cfg.doorstep_poly), ("ground", cfg.ground_poly),
                    ])
                ok3, pj = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok3:
                    self.preview_jpeg = pj.tobytes()

            # ---- 续写录像 ----
            if self.clips:
                current = self.ring.latest()
                alive = []
                for job in self.clips:
                    if job.done:
                        continue
                    if current is not None and not job.feed(current):
                        continue
                    alive.append(job)
                self.clips = alive

            # 合成源自带 sleep，真实相机由驱动限速；这里只对齐到目标帧率的 1/2，
            # 避免 USB 相机喂帧过慢时把 CPU 空转在 resize 上。
            if cap is not None and cfg.source != "test":
                elapsed = time.monotonic() - now
                budget = 1.0 / cfg.fps - elapsed
                if budget > 0:
                    time.sleep(min(budget, 1.0 / cfg.fps))

        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
        for job in self.clips:
            pass

    # ------------------------------------------------------------------
    def _handle_event(self, ev) -> None:  # noqa: ANN001
        logger.info("事件 %s %s", ev.kind, ev.as_dict())
        # 注意：ev.ts 是墙上时钟（用于对外展示），环形缓冲索引是单调时钟，
        # 两者不能混用，所以这里统一以"现在"为基准往前取 pre-roll。
        wall_now = time.time()
        mono_now = time.monotonic()
        pre = self.ring.since(mono_now - self.cfg.ring_seconds)
        clip_path = new_path(self.cfg.data_dir, wall_now, "clips", "mp4")
        snap_path = new_path(self.cfg.data_dir, wall_now, "snapshots", "jpg")

        latest = self.ring.latest()
        if latest is not None:
            with open(snap_path, "wb") as f:
                f.write(latest.jpeg)

        job = ClipJob(
            clip_path, self.cfg.clip_size, self.cfg.fps,
            pre, self.cfg.clip_after_s,
        )
        job.start()
        self.clips.append(job)

        payload = ev.as_dict()
        payload["snapshot"] = snap_path
        payload["clip"] = clip_path
        self.bus.emit(payload)

    # ---- MJPEG 预览 ----
    async def mjpeg_frames(self):
        """异步生成器：给 aiohttp 的 MJPEG 响应使用。

        必须是异步的：同步生成器里的 time.sleep(50ms) 会阻塞整个事件循环，
        导致一个预览客户端就把 /health、/events 全部拖慢。
        """
        self.preview_clients += 1
        interval = 1.0 / max(1.0, self.cfg.fps * 2)
        try:
            while not self._stop.is_set():
                data = self.preview_jpeg
                if data:
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                        + data + b"\r\n"
                    )
                await asyncio.sleep(interval)
        finally:
            self.preview_clients -= 1


def _default_names() -> list[str]:
    """COCO 80 类，顺序不能改（与 YOLO 训练时一致）。"""
    return [
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
        "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
        "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
        "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
        "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
        "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
        "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
        "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
        "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
        "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
        "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
        "hair drier", "toothbrush",
    ]


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = Config()
    svc = VisionService(cfg)
    logger.info(
        "visiond 启动 source=%s ring=%ds fps=%d 模型=%s",
        cfg.source, cfg.ring_seconds, cfg.fps,
        "enabled" if svc.detector else "disabled",
    )

    from .http import serve  # noqa: PLC0415 - 避免循环导入

    await asyncio.gather(svc.run(), serve(svc))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
