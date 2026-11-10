from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from vision.detector import Detection, Detector, draw, scale_detections


class _FakeRKNN:
    def __init__(self, outputs: list[np.ndarray]) -> None:
        self.outputs = outputs
        self.input: np.ndarray | None = None

    def inference(self, inputs: list[np.ndarray]) -> list[np.ndarray]:
        self.input = inputs[0]
        return self.outputs


def _detector(outputs: list[np.ndarray], input_size: int = 640) -> Detector:
    detector = object.__new__(Detector)
    detector.input_size = input_size
    detector.conf = 0.35
    detector.nms = 0.45
    detector.backend = "rknn"
    detector.pad_color = None
    detector.names = ["person"]
    detector._rknn = _FakeRKNN(outputs)
    return detector


class DetectorTests(unittest.TestCase):
    def test_full_frame_zones_draw_on_all_edges_with_distinct_labels(self) -> None:
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        polygon = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        detection = Detection(0, "package", 0.9, (0, 0, 99, 99))
        with patch("vision.detector.cv2.putText") as put_text:
            draw(frame, [detection], 0.0, [("doorstep", polygon), ("ground", polygon)])
        self.assertEqual(put_text.call_args_list[1].args[2], (4, 18))
        self.assertEqual(put_text.call_args_list[2].args[2], (4, 36))
        for y, x in [(0, 50), (99, 50), (50, 0), (50, 99)]:
            self.assertEqual(tuple(frame[y, x]), (0, 200, 255))

    def test_scales_detection_boxes_for_preview_size(self) -> None:
        detections = [Detection(0, "person", 0.9, (100, 50, 500, 300))]

        scaled = scale_detections(detections, (1280, 720), (640, 360))

        self.assertEqual(scaled[0].box, (50, 25, 250, 150))

    def test_decodes_rknn_yolov5_anchor_heads(self) -> None:
        outputs = []
        for size in (80, 40, 20):
            output = np.zeros((1, 255, size, size), dtype=np.float32)
            if size == 80:
                anchor = 0
                offset = anchor * 85
                output[0, offset:offset + 4, size // 2, size // 2] = 0.5
                output[0, offset + 4, size // 2, size // 2] = 0.9
                output[0, offset + 5, size // 2, size // 2] = 0.9
            outputs.append(output)

        detector = _detector(outputs)
        detections = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].name, "person")
        self.assertAlmostEqual(detections[0].conf, 0.81, places=5)

    def test_decodes_rknn_model_zoo_dfl_heads(self) -> None:
        outputs = []
        for size in (80, 40, 20):
            box_head = np.zeros((1, 64, size, size), dtype=np.float32)
            box_head[:, 16:20] = 8.0
            class_head = np.zeros((1, 80, size, size), dtype=np.float32)
            if size == 80:
                class_head[:, 0, size // 2, size // 2] = 0.92
            sum_head = np.ones((1, 1, size, size), dtype=np.float32)
            outputs.extend((box_head, class_head, sum_head))

        detector = _detector(outputs)
        detections = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].name, "person")
        self.assertGreater(detections[0].conf, 0.9)

    def test_rknn_input_is_rgb_with_black_letterbox(self) -> None:
        raw = np.zeros((1, 1, 5), dtype=np.float32)
        detector = _detector([raw], input_size=8)
        image = np.full((2, 4, 3), (10, 20, 230), dtype=np.uint8)

        detector.detect(image)

        model_input = detector._rknn.input
        self.assertIsNotNone(model_input)
        self.assertEqual(tuple(model_input[0, 0, 0]), (0, 0, 0))
        self.assertEqual(tuple(model_input[0, 2, 0]), (230, 20, 10))

    def test_single_tensor_layouts_remain_supported(self) -> None:
        row_major = np.zeros((1, 1, 5), dtype=np.float32)
        channel_first = np.zeros((1, 84, 8400), dtype=np.float32)

        self.assertEqual(Detector._decode(row_major).shape, (1, 5))
        self.assertEqual(Detector._decode(channel_first).shape, (8400, 84))

    def test_decodes_two_class_rknn_single_tensor_with_letterbox(self) -> None:
        output = np.zeros((1, 6, 8400), dtype=np.float32)
        output[0, :, 0] = (320, 320, 200, 100, 0.1, 0.9)
        detector = _detector([output])
        detector.names = ["defect-parcel", "good-parcel"]
        detector.pad_color = 114
        image = np.zeros((320, 640, 3), dtype=np.uint8)
        detections = detector.detect(image)
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].name, "good-parcel")
        self.assertEqual(detections[0].box, (220, 110, 420, 210))
        self.assertEqual(tuple(detector._rknn.input[0, 0, 0]), (114, 114, 114))

    def test_single_tensor_class_mismatch_is_explicit(self) -> None:
        detector = _detector([np.zeros((1, 6, 8400), dtype=np.float32)])
        with self.assertRaisesRegex(ValueError, "类别数"):
            detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()