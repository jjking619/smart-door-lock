"""visiond 配置：全部来自环境变量，便于 systemd 管理与现场调参。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _poly(name: str, default: str) -> list[tuple[float, float]]:
    """归一化多边形 ROI，格式 "x1,y1;x2,y2;..."，取值 0~1。"""
    raw = os.getenv(name, default)
    pts: list[tuple[float, float]] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        x, _, y = part.partition(",")
        pts.append((float(x), float(y)))
    return pts or [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def _names(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, ",".join(default)).split(",") if value.strip())


DEFAULT_PACKAGE_MODEL = "models/parcel-yolov5n-rk3576-fp.rknn"
FULL_FRAME_POLYGON = "0,0;1,0;1,1;0,1"


@dataclass(slots=True)
class Config:
    # ---- 采集 ----
    source: str = os.getenv("VISION_SOURCE", "auto")   # auto / 设备号 / 设备路径 / "test"
    width: int = _i("VISION_WIDTH", 1280)
    height: int = _i("VISION_HEIGHT", 720)
    fps: int = _i("VISION_FPS", 10)
    ring_seconds: int = _i("VISION_RING_SECONDS", 10)
    jpeg_quality: int = _i("VISION_JPEG_QUALITY", 75)   # 环形缓冲与预览质量
    clip_scale: float = _f("VISION_CLIP_SCALE", 0.5)    # 片段降采样，省空间

    # ---- 推理 ----
    model_path: str = os.getenv("VISION_MODEL", "models/yolov5s-rk3576.rknn")
    package_model_path: str = field(
        default_factory=lambda: os.getenv("VISION_PACKAGE_MODEL", DEFAULT_PACKAGE_MODEL)
    )
    model_input: int = _i("VISION_MODEL_INPUT", 640)
    conf: float = _f("VISION_CONF", 0.35)
    package_conf: float = _f("VISION_PACKAGE_CONF", 0.35)
    package_person_conf: float = _f("VISION_PACKAGE_PERSON_CONF", 0.2)
    package_tiles: bool = os.getenv("VISION_PACKAGE_TILES", "0") == "1"
    nms: float = _f("VISION_NMS", 0.45)
    infer_every_n: int = _i("VISION_INFER_EVERY_N", 3)  # 每 N 帧跑一次推理
    model_class_names: tuple[str, ...] = field(default_factory=lambda: _names("VISION_CLASS_NAMES"))
    package_model_class_names: tuple[str, ...] = field(
        default_factory=lambda: _names("VISION_PACKAGE_MODEL_CLASS_NAMES", ("defect-parcel", "good-parcel"))
    )
    person_class_names: tuple[str, ...] = ("person",)
    package_class_names: tuple[str, ...] = field(
        default_factory=lambda: _names(
            "VISION_PACKAGE_CLASSES",
            ("defect-parcel", "good-parcel") if os.getenv("VISION_PACKAGE_MODEL", DEFAULT_PACKAGE_MODEL)
            else ("suitcase", "backpack", "handbag"),
        )
    )

    # ---- 规则 ----
    doorstep_poly: list[tuple[float, float]] = field(
        default_factory=lambda: _poly(
            "VISION_DOORSTEP_POLY", FULL_FRAME_POLYGON
        )
    )
    ground_poly: list[tuple[float, float]] = field(
        default_factory=lambda: _poly(
            "VISION_GROUND_POLY", FULL_FRAME_POLYGON
        )
    )
    preview_show_zones: bool = os.getenv("VISION_PREVIEW_ZONES", "0") == "1"  # 预览是否画 ROI 黄框
    dwell_seconds: float = _f("VISION_DWELL_S", 10.0)   # 闯入：停留多久算异常
    static_seconds: float = _f("VISION_STATIC_S", 5.0)   # 快递：静止多久算包裹
    person_leave_grace_s: float = _f("VISION_PERSON_LEAVE_S", 20.0)
    dedupe_seconds: float = _f("VISION_DEDUPE_S", 10.0)  # 已上报包裹消失多久后重新允许上报
    # 包裹模型会把近景人脸/躯干误判为包裹：过大框、与人体中上部大面积重叠的框都丢弃
    package_max_area: float = _f("VISION_PACKAGE_MAX_AREA", 0.3)          # 占画面比例上限
    package_person_overlap: float = _f("VISION_PACKAGE_PERSON_OVERLAP", 0.5)  # 落在人框内的比例上限
    night_hours: tuple[int, int] = (22, 7)             # 夜间更敏感

    # ---- 录像 ----
    clip_after_s: float = _f("VISION_CLIP_AFTER_S", 20.0)
    continuous_max_gb: float = _f("VISION_CONTINUOUS_MAX_GB", 5.0)
    data_dir: str = os.getenv("VISION_DATA_DIR", "data")

    # ---- 无界面暂停 ----
    # 没有界面心跳、预览、录像时释放相机并停止推理；代价是入侵/快递检测也随之停止
    pause_without_ui: bool = os.getenv("VISION_PAUSE_WITHOUT_UI", "1") == "1"
    ui_lease_s: float = _f("VISION_UI_LEASE_S", 15.0)   # 心跳超时多久算"界面已关闭"

    # ---- 事件总线 ----
    mqtt_host: str = os.getenv("MQTT_HOST", "")
    mqtt_port: int = _i("MQTT_PORT", 1883)
    mqtt_topic: str = os.getenv("MQTT_TOPIC", "lock/event")
    mqtt_username: str = os.getenv("MQTT_USERNAME", "")
    mqtt_password: str = os.getenv("MQTT_PASSWORD", "")

    @property
    def capture_size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def clip_size(self) -> tuple[int, int]:
        w = max(2, int(self.width * self.clip_scale) // 2 * 2)
        h = max(2, int(self.height * self.clip_scale) // 2 * 2)
        return w, h

    @property
    def ring_len(self) -> int:
        return max(1, int(self.fps * self.ring_seconds))
