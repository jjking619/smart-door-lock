import unittest
from unittest.mock import Mock, patch

from vision.recorder import ContinuousRecorder
from vision.ring import Frame


class ContinuousRecorderTests(unittest.TestCase):
    @patch("vision.recorder.ClipWriter")
    def test_stop_drains_frames_and_closes_writer(self, writer_type):
        writer = Mock()
        writer_type.return_value = writer
        recorder = ContinuousRecorder("data", (640, 360), 10)

        recorder.start([Frame(0.0, b"jpeg-frame", 640, 360)])
        recorder.stop()

        writer.open.assert_called_once()
        writer.write_jpeg.assert_called_once_with(b"jpeg-frame")
        writer.close.assert_called_once()
        self.assertFalse(recorder.active)


if __name__ == "__main__":
    unittest.main()