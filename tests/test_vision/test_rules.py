from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from vision.app import VisionService
from vision.config import Config
from vision.detector import Detection
from vision.rules import Rules, _in_poly


class PackageRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()
        self.cfg.package_class_names = ("backpack",)
        self.cfg.static_seconds = 2
        self.cfg.dedupe_seconds = 5
        self.rules = Rules(self.cfg)
        self.rules._is_night = lambda: False
        self.person = Detection(0, "person", 0.9, (280, 100, 360, 300))
        self.parcel = Detection(24, "backpack", 0.9, (280, 230, 360, 340))

    def test_static_package_is_reported_without_person_and_only_once(self) -> None:
        self.rules.update([self.parcel], 640, 360, now=100)
        self.rules.update([self.parcel], 640, 360, now=101)
        events = self.rules.update([self.parcel], 640, 360, now=102)

        package_events = [event for event in events if event.kind == "package.detected"]
        self.assertEqual(len(package_events), 1)
        self.assertEqual(package_events[0].detail["object"], "backpack")
        self.assertEqual(package_events[0].detail["static_s"], 2)
        self.assertNotIn("package.detected", [
            event.kind for event in self.rules.update([self.parcel], 640, 360, now=103)
        ])

    def test_outside_ground_is_not_reported(self) -> None:
        self.cfg.ground_poly = [(0.05, 0.55), (0.95, 0.55), (0.95, 0.98), (0.05, 0.98)]
        parcel = Detection(24, "backpack", 0.9, (280, 20, 360, 80))
        for now in range(100, 105):
            events = self.rules.update([parcel], 640, 360, now=now)
            self.assertNotIn("package.detected", [event.kind for event in events])

    def test_default_regions_cover_full_frame_including_boundaries(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config()
        corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        for polygon in (cfg.doorstep_poly, cfg.ground_poly):
            self.assertEqual(polygon, corners)
            for point in corners + [(0.5, 0.0), (0.5, 1.0), (0.0, 0.5), (1.0, 0.5)]:
                with self.subTest(polygon=polygon, point=point):
                    self.assertTrue(_in_poly(point, polygon))
            self.assertFalse(_in_poly((-0.01, 0.5), polygon))
            self.assertFalse(_in_poly((1.01, 0.5), polygon))

    def test_person_and_package_events_near_each_frame_edge(self) -> None:
        boxes = [
            (0, 0, 20, 20), (620, 0, 640, 20),
            (0, 320, 20, 360), (620, 320, 640, 360),
        ]
        for box in boxes:
            with self.subTest(box=box):
                rules = Rules(self.cfg)
                person = Detection(0, "person", 0.9, box)
                parcel = Detection(24, "backpack", 0.9, box)
                first = rules.update([person, parcel], 640, 360, now=100)
                rules.update([person, parcel], 640, 360, now=101)
                last = rules.update([person, parcel], 640, 360, now=102)
                self.assertIn("person.detected", [event.kind for event in first])
                self.assertIn("package.detected", [event.kind for event in last])

    def test_region_overrides_remain_configurable(self) -> None:
        with patch.dict(os.environ, {
            "VISION_DOORSTEP_POLY": "0.1,0.2;0.9,0.2;0.9,0.8;0.1,0.8",
            "VISION_GROUND_POLY": "0,0.5;1,0.5;1,1;0,1",
        }):
            cfg = Config()
        self.assertEqual(cfg.doorstep_poly[0], (0.1, 0.2))
        self.assertEqual(cfg.ground_poly[0], (0.0, 0.5))
        self.assertFalse(_in_poly((0.5, 0.3), cfg.ground_poly))
        self.assertTrue(_in_poly((0.5, 0.5), cfg.ground_poly))

    def test_missing_detection_restarts_stability(self) -> None:
        self.rules.update([self.parcel], 640, 360, now=100)
        self.rules.update([], 640, 360, now=101)
        self.rules.update([self.parcel], 640, 360, now=102)
        self.assertFalse(self.rules.update([self.parcel], 640, 360, now=103))
        events = self.rules.update([self.parcel], 640, 360, now=104)
        self.assertEqual([event.kind for event in events], ["package.detected"])

    def test_movement_restarts_stability(self) -> None:
        moved = Detection(24, "backpack", 0.9, (310, 230, 390, 340))
        self.rules.update([self.parcel], 640, 360, now=100)
        self.rules.update([self.parcel], 640, 360, now=101)
        self.assertFalse(self.rules.update([moved], 640, 360, now=102))
        self.assertFalse(self.rules.update([moved], 640, 360, now=103))
        events = self.rules.update([moved], 640, 360, now=104)
        self.assertEqual([event.kind for event in events], ["package.detected"])

    def test_reported_package_rearms_after_absence(self) -> None:
        for now in range(100, 103):
            self.rules.update([self.parcel], 640, 360, now=now)
        self.rules.update([], 640, 360, now=103)
        self.assertFalse(self.rules.update([self.parcel], 640, 360, now=104))
        self.rules.update([], 640, 360, now=105)
        self.rules.update([self.parcel], 640, 360, now=109)
        self.rules.update([self.parcel], 640, 360, now=110)
        events = self.rules.update([self.parcel], 640, 360, now=111)
        self.assertEqual([event.kind for event in events], ["package.detected"])

    def test_packages_are_deduplicated_independently(self) -> None:
        second = Detection(24, "backpack", 0.9, (50, 230, 130, 340))
        for now in range(100, 103):
            self.rules.update([self.parcel], 640, 360, now=now)
        self.rules.update([self.parcel, second], 640, 360, now=103)
        self.rules.update([self.parcel, second], 640, 360, now=104)
        events = self.rules.update([self.parcel, second], 640, 360, now=105)
        self.assertEqual([event.kind for event in events], ["package.detected"])
        self.assertEqual(events[0].detail["bbox"], list(second.box))

    def test_class_change_does_not_duplicate_a_package(self) -> None:
        self.cfg.package_class_names = ("good-parcel", "defect-parcel")
        good = Detection(1, "good-parcel", 0.9, self.parcel.box)
        defect = Detection(0, "defect-parcel", 0.9, self.parcel.box)
        self.rules.update([good], 640, 360, now=100)
        self.rules.update([good], 640, 360, now=101)
        events = self.rules.update([good], 640, 360, now=102)
        self.assertEqual([event.kind for event in events], ["package.detected"])
        self.assertFalse(self.rules.update([defect], 640, 360, now=103))

    def test_package_event_is_written_to_event_bus(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            self.cfg.data_dir = data_dir
            self.cfg.model_path = os.path.join(data_dir, "missing.rknn")
            self.cfg.package_model_path = ""
            service = VisionService(self.cfg)
            service.rules.update([self.parcel], 640, 360, now=100)
            service.rules.update([self.parcel], 640, 360, now=101)
            events = service.rules.update([self.parcel], 640, 360, now=102)
            with patch("vision.app.ClipJob") as clip:
                service._handle_event(events[0])
            clip.return_value.start.assert_called_once()
            with open(os.path.join(data_dir, "events.jsonl"), encoding="utf-8") as file:
                payload = json.loads(file.readline())
            self.assertEqual(payload["type"], "package.detected")
            self.assertEqual(payload["object"], "backpack")
            self.assertEqual(payload["zone"], "ground")
            service.bus.close()

    def test_class_names_can_be_configured_for_custom_model(self) -> None:
        with patch.dict(os.environ, {
            "VISION_CLASS_NAMES": "person, parcel",
            "VISION_PACKAGE_CLASSES": "parcel",
        }):
            cfg = Config()

        self.assertEqual(cfg.model_class_names, ("person", "parcel"))
        self.assertEqual(cfg.package_class_names, ("parcel",))

    def test_person_box_touching_frame_bottom_is_inside_doorstep(self) -> None:
        edge_person = Detection(0, "person", 0.9, (280, 100, 360, 360))

        events = Rules(self.cfg).update([edge_person], 640, 360, now=100)

        self.assertIn("person.detected", [event.kind for event in events])

    def test_intrusion_detection_still_works(self) -> None:
        self.cfg.dwell_seconds = 2
        self.rules.update([self.person], 640, 360, now=100)
        self.rules.update([self.person], 640, 360, now=101)
        events = self.rules.update([self.person], 640, 360, now=102)
        self.assertEqual([event.kind for event in events], ["intrusion"])
        self.assertEqual(events[0].detail["dwell_s"], 2)


if __name__ == "__main__":
    unittest.main()