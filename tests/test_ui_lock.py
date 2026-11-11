import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.api import ApiClient
from ui.app import MainWindow
from ui.page_home import HomePage


class LockStub:
    """只回答 /health，用来验证首页把 lockd 的状态翻译成人话。"""

    base_url = "http://127.0.0.1:8083"
    token = ""

    def __init__(self, status):
        self.status = status

    def lock_health(self, on_ok, _on_err=None):
        on_ok(self.status)

    def fingerprint_health(self, _on_ok, on_err=None):
        if on_err:
            on_err("指纹服务未连接")


class HomeLockStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _page(self, status) -> HomePage:
        page = HomePage(LockStub(status))
        page._on_lock_health(status)
        return page

    def test_locked_state_enables_the_unlock_button(self):
        page = self._page({"service": "lockd", "state": "locked", "driver": {"driver": "gpio"}})

        self.assertEqual(page.lock_state.text(), "已上锁")
        self.assertTrue(page.btn_unlock.isEnabled())
        self.assertIn("gpio", page.arm_state.text())
        page.close()

    def test_unlocked_state_is_shown_as_open(self):
        page = self._page({"service": "lockd", "state": "unlocked", "driver": {"driver": "gpio"}})

        self.assertEqual(page.lock_state.text(), "已开锁")
        page.close()

    def test_servo_driver_is_shown_and_panel_requires_confirmation(self):
        page = self._page({"service": "lockd", "state": "locked", "driver": {"driver": "servo"}})
        requested = []
        page.unlock_requested.connect(lambda: requested.append(True))
        page.btn_unlock.click()
        self.assertEqual(requested, [])
        self.assertEqual(page.btn_unlock.text(), "确认解锁")
        page.btn_unlock.click()
        self.assertEqual(requested, [True])
        self.assertIn("servo", page.arm_state.text())
        page.close()

    def test_servo_return_failure_does_not_display_locked(self):
        page = self._page({
            "service": "lockd", "state": "fault",
            "driver": {"driver": "servo", "error": "PWM 回锁失败"},
        })
        self.assertEqual(page.lock_state.text(), "锁体故障")
        self.assertIn("PWM 回锁失败", page.arm_state.text())
        page.close()

    def test_panel_request_uses_lockd_and_waits_for_servo_cycle(self):
        api = ApiClient("http://127.0.0.1:8083")
        with patch.object(api._nam, "post", return_value=Mock()) as post:
            api.lock_unlock()
        request, body = post.call_args.args
        self.assertEqual(request.url().path(), "/unlock")
        self.assertEqual(json.loads(bytes(body)), {"source": "ui"})
        self.assertEqual(request.transferTimeout(), 15000)

    def test_panel_completion_message_does_not_claim_servo_is_still_open(self):
        page = self._page({"service": "lockd", "state": "locked", "driver": {"driver": "servo"}})
        api = Mock()
        window = SimpleNamespace(api=api, home=page)
        with patch("ui.page_home.QTimer.singleShot") as single_shot:
            MainWindow._request_unlock(window)
            api.lock_unlock.call_args.kwargs["on_ok"]({"ok": True, "seconds": 5})
            self.assertIn("已自动回锁", page.hint.text())
            self.assertNotIn("秒后自动回锁", page.hint.text())
            self.assertEqual(single_shot.call_args.args[0], 5000)
            single_shot.call_args.args[1]()
            self.assertEqual(page.hint.text(), "")
        page.close()

    def test_expiring_unlock_message_does_not_clear_a_later_hint(self):
        page = self._page({"service": "lockd", "state": "locked", "driver": {"driver": "servo"}})
        api = Mock()
        window = SimpleNamespace(api=api, home=page)
        with patch("ui.page_home.QTimer.singleShot") as single_shot:
            MainWindow._request_unlock(window)
            api.lock_unlock.call_args.kwargs["on_ok"]({"ok": True, "seconds": 5})
            expire = single_shot.call_args.args[1]
            page.hint.setText("新的错误提示")
            expire()
        self.assertEqual(page.hint.text(), "新的错误提示")
        page.close()

    def test_home_hides_normal_vision_counters_but_keeps_problem_status(self):
        page = HomePage(LockStub({}))
        health = SimpleNamespace(
            detector_error=None, package_detector_error=None,
            package_detector_enabled=False, package_detector=None,
            fps=8.9, ring=100, preview_clients=1, clips_active=0,
            recording_requested=False, recording_active=False,
        )
        page._on_health(health)
        self.assertEqual(page.conn_label.text(), "")

        health.detector_error = "推理失败"
        page._on_health(health)
        self.assertIn("检测异常", page.conn_label.text())
        self.assertNotIn("fps", page.conn_label.text())
        page.close()

    def test_lockout_disables_the_button_and_explains_why(self):
        page = self._page({
            "service": "lockd", "state": "locked", "locked_out": True,
            "driver": {"driver": "mock"},
        })

        self.assertEqual(page.lock_state.text(), "已锁定")
        self.assertFalse(page.btn_unlock.isEnabled())
        self.assertIn("锁定中", page.btn_unlock.text())
        page.close()

    def test_missing_lockd_shows_not_wired_instead_of_a_dead_button(self):
        page = HomePage(LockStub({}))
        page._on_lock_health_error("连接被拒绝")

        self.assertEqual(page.lock_state.text(), "状态未知")
        self.assertFalse(page.btn_unlock.isEnabled())
        self.assertIn("未接入", page.btn_unlock.text())
        self.assertFalse(page.lock_available)
        page.close()

    def test_capabilities_no_longer_switch_the_unlock_button(self):
        page = self._page({"service": "lockd", "state": "locked", "driver": {"driver": "gpio"}})

        page.set_capabilities(False, True)

        self.assertTrue(page.btn_unlock.isEnabled())
        self.assertEqual(page.btn_unlock.text(), "解锁")
        page.close()

    def test_fingerprint_result_reports_refusal_reason(self):
        page = HomePage(LockStub({}))
        page._on_fingerprint_health({
            "service": "fingerprintd",
            "connected": True,
            "device": "/dev/serial/by-id/as608",
            "last_match": {"id": 3, "name": "张三", "finger": "食指"},
            "last_unlock": {"ok": False, "reason": "该指纹未在界面登记，不能开门"},
        })

        text = page.fingerprint_result.text()
        self.assertIn("张三", text)
        self.assertIn("未开锁", text)
        self.assertIn("未在界面登记", text)
        page.close()

    def test_fingerprint_result_reports_successful_unlock(self):
        page = HomePage(LockStub({}))
        page._on_fingerprint_health({
            "service": "fingerprintd",
            "connected": True,
            "device": "/dev/serial/by-id/as608",
            "last_match": {"id": 0, "name": "kane", "finger": "拇指"},
            "last_unlock": {"ok": True},
        })

        self.assertIn("已开锁", page.fingerprint_result.text())
        page.close()


if __name__ == "__main__":
    unittest.main()
