import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from vision.app import VisionService
from vision.config import Config
from vision.detector import Detection, draw


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

    def test_detect_frame_drops_package_boxes_on_people(self):
        cfg = Config()
        cfg.package_model_path = "parcel.rknn"
        cfg.package_class_names = ("defect-parcel", "good-parcel")
        person = Detection(0, "person", 0.9, (280, 100, 360, 300))
        face = Detection(0, "defect-parcel", 0.6, (290, 110, 350, 180))
        whole_frame = Detection(0, "defect-parcel", 0.6, (30, 0, 639, 359))
        parcel = Detection(1, "good-parcel", 0.5, (400, 280, 480, 340))
        with tempfile.TemporaryDirectory() as path:
            cfg.data_dir = path
            with patch("vision.app.Detector") as factory:
                human, package = Mock(), Mock()
                factory.side_effect = [human, package]
                human.detect.return_value = [person]
                package.detect.return_value = [face, whole_frame, parcel]
                svc = VisionService(cfg)
            detections = svc.detect_frame(np.zeros((360, 640, 3), dtype=np.uint8))
            self.assertEqual(detections, [person, parcel])
            svc.bus.close()

    def test_only_people_and_packages_reach_preview(self):
        cfg = Config()
        cfg.source = "test"
        cfg.width, cfg.height = 640, 360
        cfg.infer_every_n = 1
        cfg.package_model_path = "parcel.rknn"
        cfg.package_class_names = ("defect-parcel", "good-parcel")
        person = Detection(0, "person", 0.9, (20, 40, 100, 300))
        chair = Detection(56, "chair", 0.9, (150, 100, 220, 300))
        suitcase = Detection(28, "suitcase", 0.9, (250, 250, 300, 330))
        main_parcel = Detection(80, "good-parcel", 0.9, (310, 260, 360, 330))
        good = Detection(1, "good-parcel", 0.9, (400, 260, 450, 330))
        defect = Detection(0, "defect-parcel", 0.9, (500, 260, 550, 330))
        unrelated = Detection(2, "cat", 0.9, (570, 260, 620, 330))
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as path:
            cfg.data_dir = path
            with patch("vision.app.Detector") as factory:
                human, package = Mock(), Mock()
                factory.side_effect = [human, package]
                human.detect.return_value = [chair, person, suitcase, main_parcel]
                package.detect.return_value = [good, defect, unrelated]
                svc = VisionService(cfg)
            self.assertEqual(svc.detect_frame(frame), [person, good, defect])

            svc.preview_clients = 1
            camera = Mock()

            def read_once():
                svc.stop()
                return True, frame

            camera.read.side_effect = read_once
            with (
                patch("vision.app.open_camera", return_value=camera),
                patch("vision.app.draw", wraps=draw) as preview_draw,
                patch.object(svc, "_handle_event"),
            ):
                svc._loop()
            self.assertEqual(preview_draw.call_args.args[1], [person, good, defect])
            self.assertTrue(svc.preview_jpeg)
            camera.release.assert_called_once()
            svc.bus.close()

    def test_single_model_retains_only_people_and_configured_package_classes(self):
        cfg = Config()
        cfg.package_model_path = ""
        cfg.package_class_names = ("suitcase", "backpack", "handbag")
        person = Detection(0, "person", 0.9, (20, 40, 100, 300))
        suitcase = Detection(28, "suitcase", 0.9, (140, 260, 180, 330))
        backpack = Detection(24, "backpack", 0.9, (240, 260, 280, 330))
        handbag = Detection(26, "handbag", 0.9, (340, 260, 380, 330))
        chair = Detection(56, "chair", 0.9, (440, 260, 480, 330))
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as path:
            cfg.data_dir = path
            with patch("vision.app.Detector") as factory:
                factory.return_value.detect.return_value = [
                    person, suitcase, backpack, handbag, chair,
                ]
                svc = VisionService(cfg)
            self.assertEqual(svc.detect_frame(frame), [person, suitcase, backpack, handbag])
            svc.bus.close()

    def test_single_model_respects_custom_detection_classes(self):
        cfg = Config()
        cfg.package_model_path = ""
        cfg.person_class_names = ("human",)
        cfg.package_class_names = ("carton",)
        human = Detection(0, "human", 0.9, (20, 40, 100, 300))
        carton = Detection(1, "carton", 0.9, (400, 260, 450, 330))
        person = Detection(2, "person", 0.9, (200, 40, 280, 300))
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as path:
            cfg.data_dir = path
            with patch("vision.app.Detector") as factory:
                factory.return_value.detect.return_value = [human, carton, person]
                svc = VisionService(cfg)
            self.assertEqual(svc.detect_frame(frame), [human, carton])
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
