import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from ui.page_home import HomePage


class FakeApi:
    def __init__(self):
        self.base_url = "http://localhost:8083"
        self.token = ""
        self.recording_on = False

    def recording(self, on, on_ok, on_err):
        self.recording_on = on
        on_ok({"recording": {"requested": on, "active": on}})

    def health(self, on_ok, on_err=None):
        on_ok(SimpleNamespace(
            ok=True, fps=10.0, ring=100, clips_active=0, detector="test",
            preview_clients=0, recording_requested=self.recording_on,
            recording_active=self.recording_on,
        ))


class RecordingSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_button_toggles_and_reports_backend_state(self):
        page = HomePage(FakeApi())
        self.assertFalse(page.btn_recording.isChecked())

        page.btn_recording.click()
        self.assertTrue(page.btn_recording.isChecked())
        self.assertEqual(page.recording_pill.text(), "录像中")

        page.btn_recording.click()
        self.assertFalse(page.btn_recording.isChecked())
        self.assertEqual(page.recording_pill.text(), "录像关闭")
        page.stop()
        page.close()
        page.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()