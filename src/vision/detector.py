"""YOLO 检测器：优先 RK3576 NPU（rknnlite），退化到 OpenCV ONNX（CPU）。

RKNN 与 ONNX 的差别只在"预处理 + 后处理"，其余（letterbox、NMS、类别映射）
完全一致，所以把差异收在两个 `_infer_*` 方法里，上层拿到的是统一 Detection。

模型导出（可在 x86 开发机或安装了 ARM64 Toolkit2 的板子上做）：
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


def scale_detections(
    detections: list[Detection], source_size: tuple[int, int], target_size: tuple[int, int]
) -> list[Detection]:
    source_width, source_height = source_size
    target_width, target_height = target_size
    scale_x = target_width / source_width
    scale_y = target_height / source_height
    return [
        Detection(
            detection.cls_id,
            detection.name,
            detection.conf,
            (
                int(detection.box[0] * scale_x),
                int(detection.box[1] * scale_y),
                int(detection.box[2] * scale_x),
                int(detection.box[3] * scale_y),
            ),
        )
        for detection in detections
    ]


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
    def __init__(self, model_path: str, input_size: int = 640, conf: float = 0.35,
                 nms: float = 0.45, pad_color: int | None = None) -> None:
        self.input_size = input_size
        self.conf = conf
        self.nms = nms
        self.pad_color = pad_color
        self.backend = "none"
        self._sess = None
        self._rknn = None
        self._model_zoo_heads = False
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
            rknn.release()
            raise RuntimeError("load_rknn 失败")
        # core_mask 0 自动选核；多模型时可指定 1/2/4 分摊三核
        if rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_AUTO) != 0:
            rknn.release()
            raise RuntimeError("init_runtime 失败")
        self._rknn = rknn
        self.backend = "rknn"

    def _init_onnx(self, path: str) -> None:
        self._sess = cv2.dnn.readNetFromONNX(path)
        self._sess.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self._sess.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self._output_names = self._sess.getUnconnectedOutLayersNames()
        self._model_zoo_heads = len(self._output_names) >= 6
        self.backend = "onnx-cpu"

    def set_names(self, names: list[str]) -> None:
        self.names = names

    def _name(self, cid: int) -> str:
        if 0 <= cid < len(self.names):
            return self.names[cid]
        return f"cls{cid}"

    # ---------- 推理 ----------
    def detect(self, bgr: np.ndarray) -> list[Detection]:
        pad_color = self.pad_color
        if pad_color is None:
            pad_color = 0 if self.backend == "rknn" or self._model_zoo_heads else 114
        padded, r, (dx, dy) = letterbox(bgr, self.input_size, color=pad_color)
        if self.backend == "rknn":
            out = self._infer_rknn(padded)
        elif self.backend == "onnx-cpu":
            out = self._infer_onnx(padded)
        else:
            return []

        outputs = [np.asarray(value) for value in out]
        if len(outputs) == 1 and outputs[0].ndim in (2, 3):
            pred = self._decode(outputs[0])
            if pred is None or pred.shape[1] != 4 + len(self.names):
                raise ValueError(f"单输出检测模型形状与类别数不匹配: {outputs[0].shape}, 类别数={len(self.names)}")
            if pred.size == 0:
                return []

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
        elif self.backend == "rknn" and any(
            value.ndim == 4 and value.shape[1] == 255 for value in outputs
        ):
            decoded = self._decode_yolov5_heads(outputs)
            if decoded is None:
                return []
            xy, scores, cls_ids = decoded
        elif self.backend == "rknn" or self._model_zoo_heads:
            decoded = self._decode_rknn_heads(outputs)
            if decoded is None:
                return []
            xy, scores, cls_ids = decoded
        else:
            raise ValueError(f"不支持的检测模型输出: {[value.shape for value in outputs]}")

        if not len(scores):
            return []

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

    def _decode_yolov5_heads(
        self, outputs: list[np.ndarray]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """Decode Rockchip YOLOv5 heads shaped [1, 3 * (5 + classes), H, W]."""
        anchors = np.asarray((
            ((10, 13), (16, 30), (33, 23)),
            ((30, 61), (62, 45), (59, 119)),
            ((116, 90), (156, 198), (373, 326)),
        ), dtype=np.float32)
        heads = sorted(
            (value for value in outputs if value.ndim == 4 and value.shape[0] == 1 and value.shape[1] % 3 == 0),
            key=lambda value: value.shape[2],
            reverse=True,
        )
        all_boxes: list[np.ndarray] = []
        all_scores: list[np.ndarray] = []
        all_classes: list[np.ndarray] = []

        for scale_index, output in enumerate(heads[:len(anchors)]):
            _, channels, height, width = output.shape
            values_per_anchor = channels // 3
            if values_per_anchor <= 5:
                continue
            prediction = output[0].reshape(3, values_per_anchor, height, width)
            grid_x, grid_y = np.meshgrid(np.arange(width), np.arange(height))
            stride_x = self.input_size / width
            stride_y = self.input_size / height

            for anchor_index in range(3):
                head = prediction[anchor_index]
                center_x = (head[0] * 2.0 - 0.5 + grid_x) * stride_x
                center_y = (head[1] * 2.0 - 0.5 + grid_y) * stride_y
                box_width = (head[2] * 2.0) ** 2 * anchors[scale_index, anchor_index, 0]
                box_height = (head[3] * 2.0) ** 2 * anchors[scale_index, anchor_index, 1]
                boxes = np.stack((
                    center_x - box_width / 2,
                    center_y - box_height / 2,
                    center_x + box_width / 2,
                    center_y + box_height / 2,
                ), axis=-1).reshape(-1, 4)

                class_scores = head[5:].transpose(1, 2, 0).reshape(-1, values_per_anchor - 5)
                classes = class_scores.argmax(axis=1)
                objectness = head[4].reshape(-1)
                scores = objectness * class_scores[np.arange(len(classes)), classes]
                mask = scores >= self.conf
                if np.any(mask):
                    all_boxes.append(boxes[mask])
                    all_scores.append(scores[mask])
                    all_classes.append(classes[mask])

        if not all_boxes:
            return None
        return (
            np.concatenate(all_boxes).astype(np.float32),
            np.concatenate(all_scores).astype(np.float32),
            np.concatenate(all_classes).astype(np.int64),
        )

    def _decode_rknn_heads(
        self, outputs: list[np.ndarray]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """Decode RKNN Model Zoo YOLO heads (DFL box logits + class scores)."""
        box_heads: dict[tuple[int, int], np.ndarray] = {}
        class_heads: dict[tuple[int, int], np.ndarray] = {}
        for output in outputs:
            if output.ndim != 4 or output.shape[0] != 1:
                continue
            channels, height, width = output.shape[1:]
            key = (height, width)
            if channels == 64:
                box_heads[key] = output[0]
            elif channels != 1:
                class_heads[key] = output[0]

        all_boxes: list[np.ndarray] = []
        all_scores: list[np.ndarray] = []
        all_classes: list[np.ndarray] = []
        for height, width in sorted(box_heads, reverse=True):
            class_head = class_heads.get((height, width))
            if class_head is None:
                continue
            box_head = box_heads[(height, width)]
            if class_head.shape[1:] != (height, width):
                continue

            reg_max = box_head.shape[0] // 4
            if reg_max * 4 != box_head.shape[0] or reg_max == 0:
                continue
            position = box_head.reshape(4, reg_max, height, width)
            position = position - position.max(axis=1, keepdims=True)
            probabilities = np.exp(position)
            probabilities /= probabilities.sum(axis=1, keepdims=True) + 1e-9
            distances = (probabilities * np.arange(reg_max, dtype=np.float32)[None, :, None, None]).sum(axis=1)

            grid_x, grid_y = np.meshgrid(np.arange(width), np.arange(height))
            stride_x = self.input_size / width
            stride_y = self.input_size / height
            center_x = (grid_x + 0.5) * stride_x
            center_y = (grid_y + 0.5) * stride_y
            boxes = np.stack((
                center_x - distances[0] * stride_x,
                center_y - distances[1] * stride_y,
                center_x + distances[2] * stride_x,
                center_y + distances[3] * stride_y,
            ), axis=-1).reshape(-1, 4)

            scores_by_anchor = class_head.transpose(1, 2, 0).reshape(-1, class_head.shape[0])
            classes = scores_by_anchor.argmax(axis=1)
            scores = scores_by_anchor[np.arange(len(classes)), classes]
            mask = scores >= self.conf
            if np.any(mask):
                all_boxes.append(boxes[mask])
                all_scores.append(scores[mask])
                all_classes.append(classes[mask])

        if not all_boxes:
            return None
        return (
            np.concatenate(all_boxes).astype(np.float32),
            np.concatenate(all_scores).astype(np.float32),
            np.concatenate(all_classes).astype(np.int64),
        )

    def _infer_rknn(self, padded: np.ndarray) -> list[np.ndarray]:
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        blob = np.expand_dims(rgb, 0)  # NHWC uint8, RKNN Model Zoo expects RGB.
        outs = self._rknn.inference(inputs=[blob])
        if not outs:
            raise RuntimeError("RKNN 推理未返回输出")
        return [np.asarray(o) for o in outs]

    def _infer_onnx(self, padded: np.ndarray) -> list[np.ndarray]:
        blob = cv2.dnn.blobFromImage(
            padded, 1 / 255.0, (self.input_size, self.input_size),
            swapRB=True, crop=False,
        )
        self._sess.setInput(blob)
        if self._model_zoo_heads:
            return [np.asarray(value) for value in self._sess.forward(list(self._output_names))]
        return [self._sess.forward()]

    @staticmethod
    def _decode(raw: np.ndarray) -> np.ndarray | None:
        """统一成 (N, 4+classes) 的 float32。"""
        arr = np.asarray(raw, dtype=np.float32)
        if arr.ndim == 3:
            arr = arr[0]
        if arr.ndim != 2:
            return None
        # (84, 8400) → (8400, 84); small N x channels tensors stay unchanged.
        if arr.shape[0] < arr.shape[1] and arr.shape[0] <= 256 and arr.shape[1] > 256:
            arr = arr.T
        return arr


# 与界面主题一致（BGR）：人员 = INFO 蓝，包裹 = WARN 琥珀
PERSON_COLOR = (255, 158, 74)
PARCEL_COLOR = (61, 179, 242)
OTHER_COLOR = (200, 200, 200)


def _box_style(name: str) -> tuple[tuple[int, int, int], str]:
    if name == "person":
        return PERSON_COLOR, "Person"
    # 进入 detect_frame 结果的非人类别都是包裹类（含无包裹模型时的行李类）
    return (PARCEL_COLOR, "Parcel") if name else (OTHER_COLOR, name)


def draw_detections(bgr: np.ndarray, dets) -> np.ndarray:  # noqa: ANN001
    """半透明填充 + 细边框 + 加粗四角 + 实底标签，box 坐标须与 bgr 同尺度。"""
    h, w = bgr.shape[:2]
    s = max(0.6, w / 640)
    edge = max(1, round(1.2 * s))
    corner = max(2, round(3 * s))
    font, font_scale = cv2.FONT_HERSHEY_DUPLEX, 0.48 * s
    pad = max(3, round(4 * s))
    for d in dets:
        x1, y1, x2, y2 = (int(v) for v in d.box)
        x1, x2 = max(0, min(x1, w - 1)), max(0, min(x2, w - 1))
        y1, y2 = max(0, min(y1, h - 1)), max(0, min(y2, h - 1))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        color, label = _box_style(d.name)

        roi = bgr[y1:y2, x1:x2]
        cv2.addWeighted(np.full_like(roi, color), 0.14, roi, 0.86, 0, dst=roi)
        cv2.rectangle(bgr, (x1, y1), (x2, y2), color, edge, cv2.LINE_AA)
        arm = int(max(6, min(x2 - x1, y2 - y1) * 0.2))
        for cx, cy, dx, dy in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
            cv2.line(bgr, (cx, cy), (cx + dx * arm, cy), color, corner, cv2.LINE_AA)
            cv2.line(bgr, (cx, cy), (cx, cy + dy * arm), color, corner, cv2.LINE_AA)

        text = f"{label} {round(d.conf * 100)}%"
        (tw, th), _ = cv2.getTextSize(text, font, font_scale, 1)
        lw, lh = tw + 2 * pad, th + 2 * pad
        lx = min(x1, max(0, w - lw))
        # 框顶贴着画面上沿时，标签放进框内
        ly = y1 - lh - 1 if y1 - lh - 1 >= 0 else y1 + 1
        cv2.rectangle(bgr, (lx, ly), (lx + lw, ly + lh), color, -1)
        cv2.putText(bgr, text, (lx + pad, ly + pad + th), font, font_scale,
                    (24, 24, 24), 1, cv2.LINE_AA)
    return bgr


def draw(bgr: np.ndarray, dets: list[Detection], fps: float, zones: list[tuple[str, list[tuple[float, float]]]]) -> np.ndarray:
    h, w = bgr.shape[:2]
    draw_detections(bgr, dets)
    label_counts: dict[tuple[int, int], int] = {}
    for name, poly in zones:
        pts = np.array([[int(x * (w - 1)), int(y * (h - 1))] for x, y in poly], dtype=np.int32)
        cv2.polylines(bgr, [pts], True, (0, 200, 255), 2)
        origin = (int(pts[0][0]), int(pts[0][1]))
        count = label_counts.get(origin, 0)
        label_counts[origin] = count + 1
        cv2.putText(bgr, name, (origin[0] + 4, origin[1] + 18 + count * 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 1, cv2.LINE_AA)

    text = f"{fps:.1f} fps"
    for color, thickness in (((0, 0, 0), 3), ((255, 255, 255), 1)):
        cv2.putText(bgr, text, (8, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, thickness, cv2.LINE_AA)
    return bgr
