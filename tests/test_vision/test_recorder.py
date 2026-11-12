import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from vision.recorder import ContinuousRecorder
from vision.ring import Frame


class ContinuousRecorderTests(unittest.TestCase):
    @patch("vision.recorder.ClipWriter")
    def test_stop_drains_frames_and_closes_writer(self, writer_type):
        writer = Mock()
        writer_type.return_value = writer

        with tempfile.TemporaryDirectory() as data_dir:
            recorder = ContinuousRecorder(data_dir, (640, 360), 10)
            recorder.start([Frame(0.0, b"jpeg-frame", 640, 360)])
            recorder.stop()

        writer.open.assert_called_once()
        writer.write_jpeg.assert_called_once_with(b"jpeg-frame")
        writer.close.assert_called_once()
        self.assertFalse(recorder.active)

    @patch("vision.recorder.ClipWriter")
    def test_default_segments_rotate_at_one_minute_and_continue(self, writer_type):
        first, second = Mock(), Mock()
        writer_type.side_effect = [first, second]
        clock = SimpleNamespace(
            monotonic=Mock(side_effect=[100.0, 159.999, 160.0, 160.0, 161.0]),
            time=Mock(side_effect=[1_700_000_000.0, 1_700_000_060.0]),
        )

        with tempfile.TemporaryDirectory() as data_dir:
            recorder = ContinuousRecorder(data_dir, (640, 360), 10)
            for jpeg in (b"start", b"before", b"boundary", b"after"):
                self.assertTrue(recorder.feed(Frame(0.0, jpeg, 640, 360)))
            recorder._stopping.set()
            with patch("vision.recorder.time", clock):
                recorder._run()

        self.assertEqual(writer_type.call_count, 2)
        self.assertEqual(first.write_jpeg.call_args_list, [call(b"start"), call(b"before")])
        self.assertEqual(second.write_jpeg.call_args_list, [call(b"boundary"), call(b"after")])
        first.close.assert_called_once()
        second.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()