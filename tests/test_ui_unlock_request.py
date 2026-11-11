import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.app import MainWindow

REQ = {"id": "abc123", "text": "请帮我开门", "expires_in": 60}


class UnlockRequestPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patches = [
            patch("ui.page_home.HomePage.start"),
            patch("ui.page_home.HomePage.refresh"),
            patch("ui.app.ApiClient.ui_heartbeat"),
            patch("ui.app.ApiClient.audio_health"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.window = MainWindow("http://127.0.0.1:1")
        self.window.api.lock_unlock = Mock()
        self.window.api.audio_unlock_decision = Mock()
        self.window.api.get_bytes_url = Mock()
        self.addCleanup(self.window.close)

    def test_pending_request_opens_popup_once_with_snapshot(self):
        self.window._on_unlock_status({"unlock_request": REQ})
        dialog = self.window._unlock_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.isVisible())
        self.assertIn("请帮我开门", dialog.said.text())
        self.assertTrue(self.window.api.get_bytes_url.call_args.args[0].endswith("/snapshot.jpg"))
        self.window._on_unlock_status({"unlock_request": REQ})
        self.assertIs(self.window._unlock_dialog, dialog)
        self.assertEqual(self.window.api.get_bytes_url.call_count, 1)

    def test_allow_unlocks_through_lockd_then_reports_decision(self):
        self.window._on_unlock_status({"unlock_request": REQ})
        dialog = self.window._unlock_dialog
        dialog.btn_allow.click()
        self.assertFalse(dialog.btn_allow.isEnabled())
        self.window.api.audio_unlock_decision.assert_not_called()
        kwargs = self.window.api.lock_unlock.call_args.kwargs
        self.assertEqual(self.window.api.lock_unlock.call_args.args[0]["method"], "访客语音请求")
        kwargs["on_ok"]({"ok": True, "seconds": 5})
        payload = self.window.api.audio_unlock_decision.call_args.args[0]
        self.assertEqual(payload, {"id": "abc123", "decision": "allow", "unlocked": True})

    def test_allow_but_lockd_refuses_reports_not_unlocked(self):
        self.window._on_unlock_status({"unlock_request": REQ})
        self.window._unlock_dialog.btn_allow.click()
        self.window.api.lock_unlock.call_args.kwargs["on_ok"]({"ok": False, "reason": "已锁定"})
        payload = self.window.api.audio_unlock_decision.call_args.args[0]
        self.assertEqual(payload["unlocked"], False)
        self.assertEqual(payload["reason"], "已锁定")

    def test_deny_never_calls_lockd(self):
        self.window._on_unlock_status({"unlock_request": REQ})
        self.window._unlock_dialog.btn_deny.click()
        self.window.api.lock_unlock.assert_not_called()
        payload = self.window.api.audio_unlock_decision.call_args.args[0]
        self.assertEqual(payload, {"id": "abc123", "decision": "deny"})

    def test_request_gone_disables_popup_without_deciding(self):
        self.window._on_unlock_status({"unlock_request": REQ})
        dialog = self.window._unlock_dialog
        self.window._on_unlock_status({"unlock_request": None})
        self.assertFalse(dialog.btn_allow.isEnabled())
        self.window.api.audio_unlock_decision.assert_not_called()
        self.window.api.lock_unlock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
