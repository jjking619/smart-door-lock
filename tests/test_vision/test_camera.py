import unittest
from unittest.mock import patch

from vision.app import find_usb_camera, resolve_camera_source


class CameraSourceTests(unittest.TestCase):
    def test_auto_uses_stable_by_id_path(self):
        with patch("vision.app.glob.glob", return_value=["/dev/v4l/by-id/usb-cam-video-index0"]):
            self.assertEqual(resolve_camera_source("auto"), "/dev/v4l/by-id/usb-cam-video-index0")

    def test_falls_back_to_usb_by_path(self):
        by_path = "/dev/v4l/by-path/platform-xhci-usb-0:1:1.0-video-index0"
        with patch("vision.app.glob.glob", side_effect=[[], [by_path]]):
            self.assertEqual(find_usb_camera(), by_path)

    def test_auto_reports_missing_camera(self):
        with patch("vision.app.glob.glob", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "未找到 USB 摄像头"):
                resolve_camera_source("auto")

    def test_explicit_source_is_kept(self):
        with patch("vision.app.glob.glob") as scan:
            self.assertEqual(resolve_camera_source("/dev/video73"), "/dev/video73")
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
