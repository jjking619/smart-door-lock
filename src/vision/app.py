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
* **无界面时暂停**（VISION_PAUSE_WITHOUT_UI=1）：界面心跳超时、无预览、无录像时
  释放相机、停止推理；进程、HTTP 与已加载的模型保留，界面回来后秒级恢复。

运行：
    python -m vision.app            # 采集 + 检测 + 录像
    VISION_SOURCE=test python -m vision.app   # 无相机时用合成画面自测
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import glob
import logging
import os
import signal
import threading
import time

import cv2
import numpy as np

from .config import Config
from .detector import Detection, Detector, draw, draw_detections, scale_detections
from .events import EventBus
from .recorder import ClipJob, ContinuousRecorder
from .ring import Frame, RingBuffer, new_path
from .rules import Rules, filter_package_detections

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


CAMERA_GLOBS = (
    "/dev/v4l/by-id/*-video-index0",
    "/dev/v4l/by-path/*usb*-video-index0",
)


def find_usb_camera() -> str | None:
    """返回稳定的 USB 摄像头路径；不依赖会变化的 /dev/videoN 编号。"""
    for pattern in CAMERA_GLOBS:
        devices = sorted(glob.glob(pattern))
        if devices:
            return devices[0]
    return None


def resolve_camera_source(source: str) -> str:
    if source != "auto":
        return source
    device = find_usb_camera()
    if device is None:
        raise RuntimeError("未找到 USB 摄像头（/dev/v4l/by-id/*-video-index0）")
    return device


def open_camera(cfg: Config, source: str):
    if source == "test":
        return SyntheticCamera(*cfg.capture_size, cfg.fps)

    src = int(source) if source.isdigit() else source
    cap = cv2.VideoCapture(src)
    # MJPG 比默认 YUYV 更省带宽，USB2 上能跑到 720p@30
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
    cap.set(cv2.CAP_PROP_FPS, cfg.fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # 关键：避免读到"旧帧"
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"打不开相机 {source}")
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
        self._recording_lock = threading.Lock()
        self._recording_requested = False
        self._continuous_recorder: ContinuousRecorder | None = None
        self.preview_jpeg: bytes = b""
        self.preview_clients = 0
        self.fps = 0.0
        self.camera_device: str | None = None
        self.paused = False
        self._ui_lock = threading.Lock()
        self._ui_clients: dict[str, float] = {}
        # 启动后先给界面一个租期的时间连上来，避免刚启动就暂停
        self._ui_grace_until = time.monotonic() + cfg.ui_lease_s
        self._stop = asyncio.Event()

        self.detector: Detector | None = None
        self.detector_error: str | None = None
        try:
            self.detector = Detector(
                cfg.model_path, cfg.model_input, cfg.conf, cfg.nms, core_mask=1
            )
            self.detector.set_names(cfg.model_class_names or _default_names())
            logger.info("检测器就绪 backend=%s", self.detector.backend)
        except FileNotFoundError as exc:
            self.detector_error = str(exc)
            logger.warning("未找到模型 %s，仅录像不检测", cfg.model_path)
        except Exception as exc:  # noqa: BLE001
            self.detector_error = str(exc)
            logger.warning("检测器初始化失败（仅录像）: %s", exc)

        self.package_detector: Detector | None = None
        self.package_detector_error: str | None = None
        if cfg.package_model_path:
            try:
                if not cfg.package_model_class_names or not set(cfg.package_model_class_names).intersection(
                    cfg.package_class_names
                ):
                    raise ValueError("包裹检测类别与包裹模型类别不匹配")
                self.package_detector = Detector(
                    cfg.package_model_path, cfg.model_input, cfg.conf, cfg.nms,
                    pad_color=114, core_mask=2,
                )
                self.package_detector.set_names(list(cfg.package_model_class_names))
                logger.info("包裹检测器就绪 backend=%s", self.package_detector.backend)
            except Exception as exc:  # noqa: BLE001
                self.package_detector_error = str(exc)
                logger.error("包裹检测器初始化失败 %s: %s", cfg.package_model_path, exc)

        self._inference_executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="parcel-inference")
            if self.detector is not None and self.package_detector is not None else None
        )

    def detect_frame(self, frame: np.ndarray) -> list[Detection]:
        detections: list[Detection] = []
        # Submit the parcel model first so both cores process the same frame concurrently.
        package_future = (
            self._inference_executor.submit(self.package_detector.detect, frame)
            if self._inference_executor is not None and self.package_detector is not None else None
        )
        for label, detector in (("人员", self.detector), ("包裹", self.package_detector)):
            if detector is None:
                continue
            try:
                if label == "包裹" and package_future is not None:
                    found = package_future.result()
                else:
                    found = detector.detect(frame)
                if label == "包裹":
                    allowed_names = self.cfg.package_class_names
                    self.package_detector_error = None
                else:
                    allowed_names = self.cfg.person_class_names
                    if not self.cfg.package_model_path:
                        allowed_names += self.cfg.package_class_names
                    self.detector_error = None
                detections.extend(d for d in found if d.name in allowed_names)
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s检测推理失败: %s", label, exc)
                if label == "包裹":
                    self.package_detector_error = str(exc)
                else:
                    self.detector_error = str(exc)
        return filter_package_detections(detections, frame.shape[1], frame.shape[0], self.cfg)

    # ------------------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._loop)
        finally:
            self.close()

    def close(self) -> None:
        if self._inference_executor is not None:
            self._inference_executor.shutdown(wait=True)
        self.bus.close()

    def stop(self) -> None:
        self._stop.set()

    def request_recording(self, on: bool) -> None:
        with self._recording_lock:
            self._recording_requested = on

    def recording_state(self) -> dict[str, bool]:
        with self._recording_lock:
            requested = self._recording_requested
        recorder = self._continuous_recorder
        return {"requested": requested, "active": bool(recorder and recorder.active)}

    # ---- 界面心跳 ----
    def ui_heartbeat(self, client: str, active: bool = True) -> None:
        with self._ui_lock:
            if active:
                self._ui_clients[client] = time.monotonic()
            else:
                self._ui_clients.pop(client, None)

    def ui_clients(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._ui_lock:
            expired = [c for c, seen in self._ui_clients.items() if now - seen > self.cfg.ui_lease_s]
            for client in expired:
                del self._ui_clients[client]
            return len(self._ui_clients)

    def should_pause(self, now: float | None = None) -> bool:
        if not self.cfg.pause_without_ui:
            return False
        now = time.monotonic() if now is None else now
        if now < self._ui_grace_until or self.preview_clients > 0 or self.clips:
            return False
        with self._recording_lock:
            if self._recording_requested or self._continuous_recorder is not None:
                return False
        return self.ui_clients(now) == 0

    def _enter_pause(self, cap) -> None:  # noqa: ANN001
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
        self.camera_device = None
        self.fps = 0.0
        # 旧画面不能再当作"当前"快照或下次事件的 pre-roll
        self.ring.clear()
        self.preview_jpeg = b""
        self.rules = Rules(self.cfg)
        self.paused = True
        logger.info("无界面连接，释放相机并暂停检测")

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        cfg = self.cfg
        cap = None
        fails = 0
        frame_i = 0
        t_last = time.monotonic()
        last_dets = []

        while not self._stop.is_set():
            if self.should_pause():
                if not self.paused:
                    self._enter_pause(cap)
                    cap = None
                    last_dets = []
                time.sleep(0.2)
                continue
            if self.paused:
                self.paused = False
                frame_i = 0
                t_last = time.monotonic()
                logger.info("界面已连接，恢复检测")

            if cap is None:
                try:
                    source = resolve_camera_source(cfg.source)
                    cap = open_camera(cfg, source)
                    self.camera_device = source
                    fails = 0
                    logger.info("相机已打开 %s", source)
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
                    self.camera_device = None
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
            if (self.detector or self.package_detector) and frame_i % cfg.infer_every_n == 0:
                last_dets = self.detect_frame(frame)
                events = self.rules.update(last_dets, frame.shape[1], frame.shape[0], now)
                for ev in events:
                    self._handle_event(ev)

            # ---- 环形缓冲 ----
            small = cv2.resize(frame, (640, int(640 * cfg.height / cfg.width)))
            current_frame: Frame | None = None
            ok2, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, cfg.jpeg_quality])
            if ok2:
                frame_dets = tuple(scale_detections(
                    last_dets, (frame.shape[1], frame.shape[0]), (small.shape[1], small.shape[0]),
                )) if last_dets else ()
                current_frame = Frame(now, jpg.tobytes(), small.shape[1], small.shape[0], frame_dets)
                self.ring.push(current_frame)

            self._sync_continuous_recording(current=current_frame, current_bgr=small)

            # ---- 预览：只有有人看时才画框 ----
            if self.preview_clients > 0:
                preview = small.copy()
                if self.detector or self.package_detector:
                    preview_dets = scale_detections(
                        last_dets,
                        (frame.shape[1], frame.shape[0]),
                        (small.shape[1], small.shape[0]),
                    )
                    zones = [
                        ("doorstep", cfg.doorstep_poly), ("ground", cfg.ground_poly),
                    ] if cfg.preview_show_zones else []
                    preview = draw(preview, preview_dets, self.fps, zones)
                ok3, pj = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok3:
                    self.preview_jpeg = pj.tobytes()

            # ---- 续写录像 ----
            if self.clips:
                alive = []
                for job in self.clips:
                    if job.done:
                        continue
                    if current_frame is not None and not job.feed(current_frame):
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
        self.camera_device = None
        for job in self.clips:
            pass

        self._stop_continuous_recording()

    def _stop_continuous_recording(self) -> None:
        recorder = self._continuous_recorder
        self._continuous_recorder = None
        with self._recording_lock:
            self._recording_requested = False
        if recorder is not None:
            recorder.stop()

    def _sync_continuous_recording(
        self,
        current: Frame | None = None,
        current_bgr: np.ndarray | None = None,
    ) -> None:
        with self._recording_lock:
            requested = self._recording_requested
        recorder = self._continuous_recorder
        if requested and recorder is None:
            recorder = ContinuousRecorder(
                self.cfg.data_dir, self.cfg.clip_size, self.cfg.fps,
                max_bytes=int(self.cfg.continuous_max_gb * 1024**3),
            )
            recorder.start(self.ring.snapshot())
            self._continuous_recorder = recorder
        elif not requested and recorder is not None:
            recorder.stop()
            self._continuous_recorder = None
        elif requested and recorder is not None and not recorder.active:
            logger.error("持续录像线程意外退出")
            with self._recording_lock:
                self._recording_requested = False
            self._continuous_recorder = None
        elif recorder is not None:
            if current_bgr is not None:
                recorder.feed_bgr(current_bgr)
            elif current is not None:
                recorder.feed(current)
            else:
                latest = self.ring.latest()
                if latest is not None:
                    recorder.feed(latest)

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
                f.write(_annotated_jpeg(latest, self.cfg.jpeg_quality))

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
        interval = 1.0 / max(1.0, self.cfg.fps)
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


def _annotated_jpeg(frame: Frame, quality: int) -> bytes:
    if not frame.dets:
        return frame.jpeg
    img = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return frame.jpeg
    ok, jpg = cv2.imencode(".jpg", draw_detections(img, frame.dets), [cv2.IMWRITE_JPEG_QUALITY, quality])
    return jpg.tobytes() if ok else frame.jpeg


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

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def request_stop() -> None:
        svc.stop()
        stopping.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, request_stop)
    capture = asyncio.create_task(svc.run())
    http = asyncio.create_task(serve(svc))
    shutdown = asyncio.create_task(stopping.wait())
    try:
        completed, _ = await asyncio.wait(
            (capture, http, shutdown), return_when=asyncio.FIRST_COMPLETED
        )
        for task in completed:
            task.result()
    finally:
        svc.stop()
        http.cancel()
        shutdown.cancel()
        try:
            await capture
        finally:
            await asyncio.gather(http, shutdown, return_exceptions=True)
            for detector in (svc.detector, svc.package_detector):
                if detector is not None and detector._rknn is not None:
                    detector._rknn.release()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signum)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
