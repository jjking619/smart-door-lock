import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from vision.app import VisionService
from vision.ring import Frame


class RecordingServiceTests(unittest.TestCase):
    def setUp(self):
        self.frame = Frame(1.0, b"jpeg-frame", 640, 360)
        self.service = VisionService.__new__(VisionService)
        self.service.cfg = SimpleNamespace(
            data_dir="data", clip_size=(640, 360), fps=10, continuous_max_gb=5.0,
        )
        self.service._recording_lock = threading.Lock()
        self.service._recording_requested = False
        self.service._continuous_recorder = None
        self.service.ring = SimpleNamespace(
            snapshot=lambda: [self.frame], latest=lambda: self.frame,
        )

    @patch("vision.app.ContinuousRecorder")
    def test_enable_feed_disable_and_shutdown(self, recorder_type):
        recorder = Mock()
        recorder.active = True
        recorder_type.return_value = recorder

        self.service.request_recording(True)
        self.service._sync_continuous_recording()
        recorder.start.assert_called_once_with([self.frame])
        recorder.feed.assert_not_called()

        self.service._sync_continuous_recording()
        recorder.feed.assert_called_once_with(self.frame)

        self.service.request_recording(False)
        self.service._sync_continuous_recording()
        recorder.stop.assert_called_once()
        self.assertIsNone(self.service._continuous_recorder)

        recorder_type.return_value = recorder
        self.service.request_recording(True)
        self.service._sync_continuous_recording()
        self.service._stop_continuous_recording()
        self.assertFalse(self.service.recording_state()["requested"])

    @patch("vision.app.ContinuousRecorder")
    def test_sync_prefers_bgr_feed_when_available(self, recorder_type):
        recorder = Mock()
        recorder.active = True
        recorder_type.return_value = recorder

        self.service.request_recording(True)
        self.service._sync_continuous_recording()
        recorder.feed.reset_mock()

        current_bgr = np.zeros((360, 640, 3), dtype=np.uint8)
        self.service._sync_continuous_recording(current_bgr=current_bgr)

        recorder.feed_bgr.assert_called_once_with(current_bgr)
        recorder.feed.assert_not_called()


if __name__ == "__main__":
    unittest.main()