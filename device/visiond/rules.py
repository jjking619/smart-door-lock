"""规则引擎：入侵检测 + 快递识别。

两条最容易搞错的地方，先说清楚：

1. **不要用"画面有变化"当告警。** 树影、雨、灯光、飘落的叶子都会触发。
   必须落到"人形 + ROI + 停留时长 + 时段"这种可解释的条件上。
2. **快递识别靠时序，不靠单帧。** 单帧看到"地上有个箱子"根本不是证据——
   可能是你自己的鞋柜。真正的证据链是：有人来 → 有人在门口停留 →
   人离开 → 地面上多了一个**此前不存在、且持续静止**的物体。

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
    """点在归一化多边形内（射线法）。"""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1
            if x < xin:
                inside = not inside
    return inside


def _norm_center(det: Detection, w: int, h: int) -> tuple[float, float]:
    """用框底边中点，比几何中心更贴地，更适合判断"站在门口"。"""
    x1, _y1, x2, y2 = det.box
    return ((x1 + x2) / 2 / w, y2 / h)


@dataclass(slots=True)
class _Track:
    cls: str
    box: tuple[int, int, int, int]
    first_ts: float
    last_ts: float
    hits: int
    conf: float

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
        # 事件去重
        self._last_emit: dict[str, float] = {}
        # 快递：记录"已被判定为包裹"的轨迹首帧时间，防止重复上报
        self._package_reported: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------
    def update(self, dets: list[Detection], w: int, h: int, now: float | None = None) -> list[RuleEvent]:
        """`now` 必须是**单调时钟**（用于时长/去重）；事件落库的时间戳用墙上时钟另取。"""
        now = now or time.monotonic()
        wall = time.time()   # 事件对外展示的时间，绝不能用 monotonic
        events: list[RuleEvent] = []

        persons = [d for d in dets if d.name in self.cfg.person_class_names]
        objects = [d for d in dets if d.name in self.cfg.package_class_names]

        self._update_tracks(dets, now)

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

        # ---------- 快递：人离开 + 地面出现静止新物体 ----------
        person_gone_for = (
            now - self._person_last_seen if self._person_last_seen else 1e9
        )
        if person_gone_for >= self.cfg.person_leave_grace_s and self._person_last_seen:
            for obj in objects:
                c = _norm_center(obj, w, h)
                if not _in_poly(c, self.cfg.ground_poly):
                    continue
                track = self._match_track(obj, now)
                if track is None or track.stable_for(now) < self.cfg.static_seconds:
                    continue
                key = (int(track.center()[0]) // 24, int(track.center()[1]) // 24)
                if key in self._package_reported:
                    continue
                if self._throttled("package.detected", now):
                    continue
                self._package_reported.add(key)
                events.append(RuleEvent(
                    "package.detected", wall, "info", track.conf,
                    zone="ground",
                    detail={
                        "object": track.cls,
                        "static_s": round(track.stable_for(now), 1),
                        "bbox": list(track.box),
                        "person_left_s": round(person_gone_for, 1),
                    },
                ))

        return events

    # ------------------------------------------------------------------
    def _update_tracks(self, dets: list[Detection], now: float) -> None:
        """极简关联：中心距最近且小于阈值即视为同一目标。"""
        used: set[int] = set()
        for d in dets:
            best_i, best_dist = -1, 1e9
            cx, cy = d.center
            for i, t in enumerate(self._tracks):
                if i in used or t.cls != d.name:
                    continue
                tx, ty = t.center()
                dist = math.hypot(cx - tx, cy - ty)
                if dist < best_dist:
                    best_i, best_dist = i, dist
            scale = max(24.0, d.area ** 0.5 * 1.2)
            if best_i >= 0 and best_dist < scale:
                t = self._tracks[best_i]
                t.box, t.last_ts = d.box, now
                t.hits += 1
                t.conf = max(t.conf * 0.8 + d.conf * 0.2, d.conf * 0.6)
                used.add(best_i)
            else:
                self._tracks.append(
                    _Track(d.name, d.box, now, now, 1, d.conf)
                )
        # 连续 2 s 未出现则丢弃
        self._tracks = [t for t in self._tracks if now - t.last_ts < 2.0]
        if len(self._tracks) > 32:
            self._tracks = self._tracks[-32:]

    def _match_track(self, det: Detection, now: float) -> _Track | None:
        cx, cy = det.center
        best, best_dist = None, 1e9
        for t in self._tracks:
            if t.cls != det.name:
                continue
            tx, ty = t.center()
            dist = math.hypot(cx - tx, cy - ty)
            if dist < best_dist:
                best, best_dist = t, dist
        if best is not None and best_dist < max(24.0, det.area ** 0.5 * 1.5):
            return best
        return None

    def _throttled(self, kind: str, now: float) -> bool:
        last = self._last_emit.get(kind, 0.0)
        if now - last < self.cfg.dedupe_seconds:
            return True
        self._last_emit[kind] = now
        return False

    def _is_night(self) -> bool:
        start, end = self.cfg.night_hours
        hour = datetime.now().hour
        return hour >= start or hour < end
