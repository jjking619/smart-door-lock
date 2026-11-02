"""YOLO 检测器：优先 RK3576 NPU（rknnlite），退化到 OpenCV ONNX（CPU）。

RKNN 与 ONNX 的差别只在"预处理 + 后处理"，其余（letterbox、NMS、类别映射）
完全一致，所以把差异收在两个 `_infer_*` 方法里，上层拿到的是统一 Detection。

模型导出（在 x86 开发机上做，不要在板子上跑 rknn-toolkit2）：
    yolo export model=yolo11n.pt format=onnx opset=12 imgsz=640
    # 再按 RKNN-Toolkit2 的 YOLO 示例量化成 int8，注意 keep_ratio=True / letterbox
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(slots=True)
class Detection:
    cls_id: int
    name: str
    conf: float
    box: tuple[int, int, int, int]  # x1, y1, x2, y2（原图坐标）

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        return (x1 + x2) / 2, (y1 + y2) / 2

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.box
        return max(0, x2 - x1) * max(0, y2 - y1)


def letterbox(
    img: np.ndarray, new: int = 640, color: int = 114
) -> tuple[np.ndarray, float, tuple[int, int]]:
    h, w = img.shape[:2]
    r = min(new / h, new / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    out = np.full((new, new, 3), color, dtype=np.uint8)
    top, left = (new - nh) // 2, (new - nw) // 2
    out[top : top + nh, left : left + nw] = cv2.resize(img, (nw, nh))
    return out, r, (left, top)


def _nms(boxes: np.ndarray, scores: np.ndarray, thr: float) -> list[int]:
    if boxes.size == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= thr]
    return keep


class Detector:
    def __init__(self, model_path: str, input_size: int = 640, conf: float = 0.35, nms: float = 0.45) -> None:
        self.input_size = input_size
        self.conf = conf
        self.nms = nms
        self.backend = "none"
        self._sess = None
        self._rknn = None
        self.names: list[str] = []

        if not model_path or not os.path.exists(model_path):
            raise FileNotFoundError(f"模型不存在: {model_path}")

        if model_path.endswith(".rknn"):
            self._init_rknn(model_path)
        else:
            self._init_onnx(model_path)

    # ---------- 后端初始化 ----------
    def _init_rknn(self, path: str) -> None:
        from rknnlite.api import RKNNLite  # noqa: PLC0415

        rknn = RKNNLite()
        if rknn.load_rknn(path) != 0:
            raise RuntimeError("load_rknn 失败")
        # core_mask 0 自动选核；多模型时可指定 1/2/4 分摊三核
        if rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_AUTO) != 0:
            raise RuntimeError("init_runtime 失败")
        self._rknn = rknn
        self.backend = "rknn"

    def _init_onnx(self, path: str) -> None:
        self._sess = cv2.dnn.readNetFromONNX(path)
        self._sess.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self._sess.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.backend = "onnx-cpu"

    def set_names(self, names: list[str]) -> None:
        self.names = names

    def _name(self, cid: int) -> str:
        if 0 <= cid < len(self.names):
            return self.names[cid]
        return f"cls{cid}"

    # ---------- 推理 ----------
    def detect(self, bgr: np.ndarray) -> list[Detection]:
        padded, r, (dx, dy) = letterbox(bgr, self.input_size)
        if self.backend == "rknn":
            out = self._infer_rknn(padded)
        elif self.backend == "onnx-cpu":
            out = self._infer_onnx(padded)
        else:
            return []

        pred = self._decode(out[0] if isinstance(out, (list, tuple)) else out)
        if pred is None or pred.size == 0:
            return []

        # YOLOv8: (84, N) → (N, 84)；YOLOv5/v11 也可能是 (N, 85)
        boxes_xywh = pred[:, :4]
        scores_all = pred[:, 4:]
        cls_ids = scores_all.argmax(axis=1)
        scores = scores_all[np.arange(len(cls_ids)), cls_ids]

        mask = scores >= self.conf
        boxes_xywh, scores, cls_ids = boxes_xywh[mask], scores[mask], cls_ids[mask]
        if not len(scores):
            return []

        xy = np.empty_like(boxes_xywh)
        xy[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2
        xy[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2
        xy[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2
        xy[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2

        # letterbox 逆变换
        xy[:, [0, 2]] = (xy[:, [0, 2]] - dx) / r
        xy[:, [1, 3]] = (xy[:, [1, 3]] - dy) / r
        h, w = bgr.shape[:2]
        xy[:, [0, 2]] = xy[:, [0, 2]].clip(0, w - 1)
        xy[:, [1, 3]] = xy[:, [1, 3]].clip(0, h - 1)

        keep = _nms(xy, scores, self.nms)
        return [
            Detection(
                cls_id=int(cls_ids[i]),
                name=self._name(int(cls_ids[i])),
                conf=float(scores[i]),
                box=tuple(int(v) for v in xy[i]),  # type: ignore[arg-type]
            )
            for i in keep
        ]

    def _infer_rknn(self, padded: np.ndarray) -> list[np.ndarray]:
        blob = np.expand_dims(padded, 0)  # NHWC uint8
        outs = self._rknn.inference(inputs=[blob])
        return [np.asarray(o) for o in outs]

    def _infer_onnx(self, padded: np.ndarray) -> list[np.ndarray]:
        blob = cv2.dnn.blobFromImage(
            padded, 1 / 255.0, (self.input_size, self.input_size),
            swapRB=True, crop=False,
        )
        self._sess.setInput(blob)
        return [self._sess.forward()]

    @staticmethod
    def _decode(raw: np.ndarray) -> np.ndarray | None:
        """统一成 (N, 4+classes) 的 float32。"""
        arr = np.asarray(raw, dtype=np.float32)
        if arr.ndim == 3:
            arr = arr[0]
        if arr.ndim != 2:
            return None
        # (84, 8400) → (8400, 84)
        if arr.shape[0] < arr.shape[1] and arr.shape[0] <= 256:
            arr = arr.T
        return arr


def draw(bgr: np.ndarray, dets: list[Detection], fps: float, zones: list[tuple[str, list[tuple[float, float]]]]) -> np.ndarray:
    h, w = bgr.shape[:2]
    for name, poly in zones:
        pts = np.array([[int(x * w), int(y * h)] for x, y in poly], dtype=np.int32)
        cv2.polylines(bgr, [pts], True, (0, 200, 255), 2)
        cv2.putText(bgr, name, (pts[0][0] + 4, pts[0][1] + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 1, cv2.LINE_AA)

    for d in dets:
        x1, y1, x2, y2 = d.box
        color = (0, 255, 0) if d.name in ("person", "box", "package") else (200, 200, 200)
        cv2.rectangle(bgr, (x1, y1), (x2, y2), color, 2)
        cv2.putText(bgr, f"{d.name} {d.conf:.2f}", (x1 + 2, max(14, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    cv2.putText(bgr, f"{fps:.1f} fps", (8, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return bgr
