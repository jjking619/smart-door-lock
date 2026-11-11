"""规则引擎：入侵检测 + 快递识别。

两条最容易搞错的地方，先说清楚：

1. **不要用"画面有变化"当告警。** 树影、雨、灯光、飘落的叶子都会触发。
   必须落到"人形 + ROI + 停留时长 + 时段"这种可解释的条件上。
2. **快递识别靠时序，不靠单帧。** 门口区域内的包裹须连续出现并保持稳定，
   每个包裹只上报一次，离开一段时间后才重新允许上报。

所有判定都输出统一的 RuleEvent，由 EventManager 决定录像与通知，
这样调参只改这里，不用动采集与推送。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .detector import Detection

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuleEvent:
    kind: str                     # person.detected / intrusion / package.detected
    ts: float
    severity: str = "info"        # info / warn / critical
    conf: float = 0.0
    zone: str = "doorstep"
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ts": datetime.fromtimestamp(self.ts).astimezone().isoformat(timespec="milliseconds"),
            "type": self.kind,
            "source": "visiond",
            "severity": self.severity,
            "conf": round(self.conf, 3),
            "zone": self.zone,
            **self.detail,
        }


def _in_poly(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    """点在归一化多边形内或边界上（射线法）。"""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if (
            abs(cross) <= 1e-12
            and min(x1, x2) - 1e-12 <= x <= max(x1, x2) + 1e-12
            and min(y1, y2) - 1e-12 <= y <= max(y1, y2) + 1e-12
        ):
            return True
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def _norm_center(det: Detection, w: int, h: int) -> tuple[float, float]:
    """用框底边中点，比几何中心更贴地，更适合判断"站在门口"。"""
    x1, _y1, x2, y2 = det.box
    return ((x1 + x2) / 2 / w, y2 / h)


# 包裹框中心高于人框此高度比例时视为"人身上的"（脸/躯干/手持），低于则可能是脚边的包裹
_PERSON_BODY_FRACTION = 0.75


def _covered_by(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> float:
    ix = max(0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    iy = max(0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    area = max(1, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return ix * iy / area


def is_false_package(det: Detection, persons: list[Detection], w: int, h: int, cfg: Config) -> bool:
    """包裹模型没见过人，近景人脸/躯干常被识别成包裹；按尺寸和人体重叠剔除。"""
    if det.area > cfg.package_max_area * w * h:
        return True
    cy = (det.box[1] + det.box[3]) / 2
    for p in persons:
        body_limit = p.box[1] + (p.box[3] - p.box[1]) * _PERSON_BODY_FRACTION
        if cy < body_limit and _covered_by(det.box, p.box) >= cfg.package_person_overlap:
            return True
    return False


def filter_package_detections(dets: list[Detection], w: int, h: int, cfg: Config) -> list[Detection]:
    persons = [d for d in dets if d.name in cfg.person_class_names]
    return [
        d for d in dets
        if d.name not in cfg.package_class_names or not is_false_package(d, persons, w, h, cfg)
    ]


@dataclass(slots=True)
class _Track:
    cls: str
    box: tuple[int, int, int, int]
    first_ts: float
    last_ts: float
    hits: int
    conf: float
    stable_center: tuple[float, float]
    visible: bool = True
    reported: bool = False

    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) / 2, (y1 + y2) / 2)

    def size(self) -> float:
        x1, y1, x2, y2 = self.box
        return math.hypot(x2 - x1, y2 - y1)

    def stable_for(self, now: float) -> float:
        return now - self.first_ts


class Rules:
    """有状态规则引擎。每帧调用一次 `update(dets, w, h)`，返回本次产生的事件。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._tracks: list[_Track] = []
        # 人物
        self._person_first_seen = 0.0
        self._person_last_seen = 0.0
        self._person_box: tuple[int, int, int, int] | None = None
        self._visit_reported = False

    # ------------------------------------------------------------------
    def update(self, dets: list[Detection], w: int, h: int, now: float | None = None) -> list[RuleEvent]:
        """`now` 必须是**单调时钟**（用于时长/去重）；事件落库的时间戳用墙上时钟另取。"""
        now = time.monotonic() if now is None else now
        wall = time.time()   # 事件对外展示的时间，绝不能用 monotonic
        events: list[RuleEvent] = []

        persons = [d for d in dets if d.name in self.cfg.person_class_names]
        objects = [
            d for d in dets
            if d.name in self.cfg.package_class_names
            and not is_false_package(d, persons, w, h, self.cfg)
            and _in_poly(_norm_center(d, w, h), self.cfg.ground_poly)
        ]
        package_tracks = self._update_tracks(objects, now)

        # ---------- 人形 ----------
        person_in_zone = None
        for d in persons:
            if _in_poly(_norm_center(d, w, h), self.cfg.doorstep_poly):
                person_in_zone = d
                break

        if person_in_zone is not None:
            if self._person_first_seen == 0.0:
                self._person_first_seen = now
                self._visit_reported = False
                events.append(RuleEvent(
                    "person.detected", wall, "info", person_in_zone.conf,
                    detail={"bbox": list(person_in_zone.box)},
                ))
            self._person_last_seen = now
            self._person_box = person_in_zone.box
        else:
            gap = now - self._person_last_seen if self._person_last_seen else 1e9
            if gap > self.cfg.person_leave_grace_s:
                self._person_first_seen = 0.0
                self._person_box = None
                self._visit_reported = False

        # ---------- 入侵：停留超时 ----------
        if self._person_first_seen and not self._visit_reported:
            dwell = now - self._person_first_seen
            night = self._is_night()
            threshold = self.cfg.dwell_seconds * (0.5 if night else 1.0)
            if dwell >= threshold and self._person_box is not None:
                self._visit_reported = True
                events.append(RuleEvent(
                    "intrusion", wall, "critical" if night else "warn",
                    person_in_zone.conf if person_in_zone else 0.0,
                    detail={
                        "dwell_s": round(dwell, 1),
                        "night": night,
                        "bbox": list(self._person_box),
                    },
                ))

        # ---------- 快递：区域内连续检测到稳定包裹 ----------
        for track in package_tracks:
            if track.reported or track.stable_for(now) < self.cfg.static_seconds:
                continue
            track.reported = True
            events.append(RuleEvent(
                "package.detected", wall, "info", track.conf,
                zone="ground",
                detail={
                    "object": track.cls,
                    "static_s": round(track.stable_for(now), 1),
                    "bbox": list(track.box),
                },
            ))

        return events

    def _update_tracks(self, dets: list[Detection], now: float) -> list[_Track]:
        """关联门口包裹；检测中断或明显移动时重新计稳定时间。"""
        self._tracks = [
            t for t in self._tracks
            if now - t.last_ts < (self.cfg.dedupe_seconds if t.reported else 2.0)
        ]
        used: set[int] = set()
        matched: list[_Track] = []
        for d in dets:
            best_i, best_dist = -1, 1e9
            cx, cy = d.center
            for i, t in enumerate(self._tracks):
                if i in used:
                    continue
                tx, ty = t.center()
                dist = math.hypot(cx - tx, cy - ty)
                if dist < best_dist:
                    best_i, best_dist = i, dist
            scale = max(24.0, d.area ** 0.5 * 1.2)
            if best_i >= 0 and best_dist < scale:
                t = self._tracks[best_i]
                stable_dist = math.hypot(cx - t.stable_center[0], cy - t.stable_center[1])
                if not t.visible or now - t.last_ts >= 2.0 or stable_dist > max(12.0, t.size() * 0.1):
                    t.first_ts = now
                    t.stable_center = (cx, cy)
                    t.hits = 0
                t.box, t.last_ts = d.box, now
                t.cls = d.name
                t.hits += 1
                t.conf = max(t.conf * 0.8 + d.conf * 0.2, d.conf * 0.6)
                used.add(best_i)
            else:
                t = _Track(d.name, d.box, now, now, 1, d.conf, (cx, cy))
                self._tracks.append(t)
                used.add(len(self._tracks) - 1)
            t.visible = True
            matched.append(t)
        for i, track in enumerate(self._tracks):
            if i not in used:
                track.visible = False
        return matched

    def _is_night(self) -> bool:
        start, end = self.cfg.night_hours
        hour = datetime.now().hour
        return hour >= start or hour < end
