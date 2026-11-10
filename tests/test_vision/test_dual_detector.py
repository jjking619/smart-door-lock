import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from vision.app import VisionService
from vision.config import Config
from vision.detector import Detection


class DualDetectorTests(unittest.TestCase):
    def test_disabling_package_model_restores_legacy_package_classes(self):
        with patch.dict("os.environ", {"VISION_PACKAGE_MODEL": ""}, clear=False):
            cfg = Config()
        self.assertEqual(cfg.package_model_path, "")
        self.assertEqual(cfg.package_class_names, ("suitcase", "backpack", "handbag"))

    def test_mismatched_package_labels_report_error(self):
        cfg = Config()
        cfg.package_model_path = "parcel.rknn"
        cfg.package_class_names = ("not-a-model-class",)
        with tempfile.TemporaryDirectory() as path:
            cfg.data_dir = path
            with patch("vision.app.Detector") as factory:
                with self.assertLogs("visiond", level="ERROR"):
                    svc = VisionService(cfg)
            self.assertIsNone(svc.package_detector)
            self.assertIn("类别不匹配", svc.package_detector_error)
            self.assertEqual(factory.call_count, 1)
            svc.bus.close()

    def test_dual_detections_and_rules(self):
        cfg = Config()
        cfg.package_model_path = "parcel.rknn"
        cfg.package_class_names = ("defect-parcel", "good-parcel")
        cfg.static_seconds = 2
        person = Detection(0, "person", 0.9, (280, 100, 360, 300))
        parcel = Detection(0, "defect-parcel", 0.9, (280, 230, 360, 340))
        with tempfile.TemporaryDirectory() as path:
            cfg.data_dir = path
            with patch("vision.app.Detector") as factory:
                human, package = Mock(), Mock()
                factory.side_effect = [human, package]
                human.detect.return_value = [person]
                package.detect.return_value = [parcel]
                svc = VisionService(cfg)
            frame = np.zeros((360, 640, 3), dtype=np.uint8)
            first = svc.rules.update(svc.detect_frame(frame), 640, 360, 100)
            svc.rules.update(svc.detect_frame(frame), 640, 360, 101)
            last = svc.rules.update(svc.detect_frame(frame), 640, 360, 102)
            self.assertIn("person.detected", [event.kind for event in first])
            self.assertIn("package.detected", [event.kind for event in last])
            package.set_names.assert_called_once_with(["defect-parcel", "good-parcel"])
            svc.bus.close()

    def test_package_failure_does_not_hide_person_detection(self):
        cfg = Config()
        cfg.package_model_path = "parcel.rknn"
        person = Detection(0, "person", 0.9, (1, 1, 10, 10))
        with tempfile.TemporaryDirectory() as path:
            cfg.data_dir = path
            with patch("vision.app.Detector") as factory:
                human, package = Mock(), Mock()
                factory.side_effect = [human, package]
                human.detect.return_value = [person]
                package.detect.side_effect = RuntimeError("NPU failure")
                svc = VisionService(cfg)
            with self.assertLogs("visiond", level="ERROR"):
                detections = svc.detect_frame(np.zeros((10, 10, 3), dtype=np.uint8))
            self.assertEqual(detections, [person])
            self.assertEqual(svc.package_detector_error, "NPU failure")
            svc.bus.close()
