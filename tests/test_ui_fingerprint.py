import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.page_home import HomePage
from ui.page_settings import SettingsPage


class FingerprintApiStub:
    base_url = "http://127.0.0.1:8083"
    token = ""

    def fingerprint_health(self, on_ok, _on_err=None):
        on_ok({
            "service": "fingerprintd",
            "connected": True,
            "device": "/dev/serial/by-id/as608",
            "library_size": 300,
            "template_count": 1,
        })

    def fingerprint_templates(self, on_ok, _on_err=None):
        on_ok([{
            "id": 7,
            "name": "张三",
            "finger": "右手食指",
            "registered": True,
        }])


class FingerprintUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_displays_status_and_registered_templates(self):
        page = SettingsPage(FingerprintApiStub())
        page.refresh_fingerprint()

        self.assertIn("AS608 在线", page.fingerprint_status.text())
        self.assertIn("1/300", page.fingerprint_status.text())
        self.assertEqual(page.fingerprint_list.count(), 1)
        self.assertTrue(page.btn_fingerprint_enroll.isEnabled())
        self.assertFalse(page.btn_fingerprint_delete.isEnabled())
        page.fingerprint_list.setCurrentRow(0)
        self.assertEqual(page.fingerprint_name.text(), "张三")
        self.assertEqual(page.fingerprint_note.text(), "右手食指")
        self.assertTrue(page.btn_fingerprint_delete.isEnabled())
        page._on_fingerprint_error("服务离线")
        self.assertFalse(page.btn_fingerprint_enroll.isEnabled())
        self.assertFalse(page.btn_fingerprint_delete.isEnabled())
        page.close()

    def test_refresh_keeps_typed_name_and_note(self):
        page = SettingsPage(FingerprintApiStub())
        page.refresh_fingerprint()
        page.fingerprint_name.setText("李四")
        page.fingerprint_note.setText("左手拇指")
        page.refresh_fingerprint()
        self.assertEqual(page.fingerprint_name.text(), "李四")
        self.assertEqual(page.fingerprint_note.text(), "左手拇指")

        page.fingerprint_list.setCurrentRow(0)
        page.fingerprint_name.setText("张三丰")
        page.refresh_fingerprint()
        self.assertEqual(page.fingerprint_name.text(), "张三丰")
        page.close()

    def test_finished_enrollment_shows_failure_or_success_instead_of_vanishing(self):
        page = SettingsPage(FingerprintApiStub())
        base = {"service": "fingerprintd", "connected": True, "device": "/dev/ttyUSB0",
                "library_size": 300, "template_count": 0}
        page._on_fingerprint_health({**base, "action": "enroll", "step": "请按压手指（第 1 次）"})
        self.assertIn("第 1 次", page.fingerprint_status.text())
        self.assertEqual(page._fingerprint_timer.interval(), 800)

        page._on_fingerprint_health({**base, "action": None, "error": "有效特征点太少或按压面积太小"})
        self.assertIn("上次操作失败：有效特征点太少", page.fingerprint_status.text())
        self.assertEqual(page._fingerprint_timer.interval(), 3000)

        page._on_fingerprint_health({**base, "action": None, "notice": "指纹录入成功：张三（编号 0）"})
        self.assertIn("指纹录入成功", page.fingerprint_status.text())
        page.close()

    def test_home_displays_match_without_claiming_unlock(self):
        page = HomePage(object())
        page._on_fingerprint_health({
            "service": "fingerprintd",
            "connected": True,
            "device": "/dev/serial/by-id/as608",
            "last_match": {"id": 7, "name": "张三", "finger": "右手食指"},
        })

        self.assertIn("在线", page.fingerprint_state.text())
        self.assertIn("张三", page.fingerprint_result.text())
        self.assertNotIn("开锁成功", page.fingerprint_result.text())
        page.close()


if __name__ == "__main__":
    unittest.main()
