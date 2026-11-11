import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from ui.page_home import HomePage
from ui.page_settings import SettingsPage
from ui import i18n, theme


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

    def delete_action(self, page, action):
        overlay = page._delete_confirmation
        self.assertIsNotNone(overlay)
        button = next(b for b in overlay.findChildren(QPushButton)
                      if b.property("deleteAction") == action)
        button.click()

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

    def test_fingerprint_rows_distinguish_registered_and_unregistered_templates(self):
        page = SettingsPage(FingerprintApiStub())
        self.addCleanup(page.close)
        page._on_fingerprint_templates([
            {"id": 7, "name": "张三", "finger": "右手食指", "registered": True},
            {"id": 9, "registered": False},
        ])

        registered = page.fingerprint_list.itemWidget(page.fingerprint_list.item(0))
        unregistered = page.fingerprint_list.itemWidget(page.fingerprint_list.item(1))
        self.assertEqual(registered.name_label.text(), "张三")
        self.assertEqual(registered.note_label.text(), "右手食指")
        self.assertEqual(registered.id_label.text(), "ID 7")
        self.assertIsNone(registered.status_label)
        self.assertEqual(unregistered.status_label.text(), "未登记")

        page.fingerprint_list.setCurrentRow(0)
        self.assertTrue(registered.property("selected"))
        self.assertFalse(unregistered.property("selected"))

    def test_settings_sections_preserve_fingerprint_edits(self):
        page = SettingsPage(FingerprintApiStub())
        self.addCleanup(page.close)
        self.assertEqual(page.tabs.count(), 3)
        page.refresh_fingerprint()
        page.fingerprint_list.setCurrentRow(0)
        page.fingerprint_name.setText("张三丰")
        page.tabs.setCurrentIndex(1)
        self.assertTrue(page.tabs.currentWidget().isAncestorOf(page.base_edit))
        page.tabs.setCurrentIndex(2)
        self.assertTrue(page.tabs.currentWidget().isAncestorOf(page.diag))
        page.tabs.setCurrentIndex(0)
        self.assertEqual(page.fingerprint_name.text(), "张三丰")
        self.assertEqual(page.fingerprint_list.currentRow(), 0)

    def test_settings_layout_fits_width_and_keeps_actions_accessible(self):
        previous_lang = i18n.lang()
        self.addCleanup(i18n.set_lang, previous_lang)
        for language in ("zh", "en"):
            i18n.set_lang(language)
            for width, height in ((784, 444), (1104, 644)):
                with self.subTest(language=language, width=width):
                    page = SettingsPage(FingerprintApiStub())
                    self.addCleanup(page.close)
                    page.setStyleSheet(theme.qss())
                    page.resize(width, height)
                    page.show()
                    self.app.processEvents()
                    for index in range(page.tabs.count()):
                        page.tabs.setCurrentIndex(index)
                        self.app.processEvents()
                        scroll = page.tabs.widget(index)
                        self.assertLessEqual(scroll.widget().width(), scroll.viewport().width())
                        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                        if width == 1104:
                            self.assertEqual(scroll.verticalScrollBar().maximum(), 0)
                        for button in scroll.findChildren(QPushButton):
                            if not button.isVisible():
                                continue
                            scroll.ensureWidgetVisible(button)
                            self.app.processEvents()
                            visible_rect = button.rect()
                            visible_rect.moveTopLeft(button.mapTo(scroll.viewport(), button.rect().topLeft()))
                            self.assertTrue(scroll.viewport().rect().contains(visible_rect), button.text())
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

    def test_refresh_button_requests_hardware_refresh_and_shows_completion(self):
        api = FingerprintApiStub()
        api.fingerprint_refresh = Mock()
        page = SettingsPage(api)
        page.refresh_fingerprint()
        page.btn_fingerprint_refresh.click()
        self.assertFalse(page.btn_fingerprint_refresh.isEnabled())
        self.assertIn("正在刷新", page.fingerprint_feedback.text())
        api.fingerprint_refresh.call_args.kwargs["on_ok"]({"ok": True})
        page._on_fingerprint_health({
            "service": "fingerprintd", "connected": True, "notice": "刷新完成",
        })
        self.assertEqual(page.fingerprint_feedback.text(), "刷新完成")
        self.assertTrue(page.btn_fingerprint_refresh.isEnabled())
        page.close()

    def test_refresh_failures_are_visible_and_button_is_reenabled(self):
        api = FingerprintApiStub()
        api.fingerprint_refresh = Mock()
        page = SettingsPage(api)
        page.btn_fingerprint_refresh.click()
        api.fingerprint_refresh.call_args.kwargs["on_err"]("连接被拒绝")
        self.assertIn("刷新失败：连接被拒绝", page.fingerprint_feedback.text())
        self.assertTrue(page.btn_fingerprint_refresh.isEnabled())
        api.fingerprint_templates = lambda _ok, err: err("列表不可用")
        page.refresh_fingerprint()
        self.assertIn("读取指纹列表失败：列表不可用", page.fingerprint_feedback.text())
        self.assertTrue(page.btn_fingerprint_refresh.isEnabled())
        page.close()

    def test_delete_button_confirms_and_removes_selected_fingerprint_from_list(self):
        api = FingerprintApiStub()
        api.fingerprint_delete = Mock()
        page = SettingsPage(api)
        page.refresh_fingerprint()
        page.fingerprint_list.setCurrentRow(0)
        page.btn_fingerprint_delete.click()
        self.delete_action(page, "cancel")
        api.fingerprint_delete.assert_not_called()
        page.btn_fingerprint_delete.click()
        self.delete_action(page, "confirm")
        self.assertEqual(api.fingerprint_delete.call_args.args, (7,))
        self.assertFalse(page.btn_fingerprint_delete.isEnabled())
        api.fingerprint_templates = lambda ok, _err=None: ok([])
        api.fingerprint_delete.call_args.kwargs["on_ok"]({"ok": True})
        page._on_fingerprint_health({
            "service": "fingerprintd", "connected": True, "notice": "指纹已删除：张三（编号 7）",
        })
        self.assertEqual(page.fingerprint_list.count(), 0)
        self.assertEqual(page.fingerprint_name.text(), "")
        self.assertIn("指纹已删除", page.fingerprint_feedback.text())
        self.assertFalse(page.btn_fingerprint_delete.isEnabled())
        page.close()

    def test_delete_failure_is_visible_without_removing_selection(self):
        api = FingerprintApiStub()
        api.fingerprint_delete = Mock()
        page = SettingsPage(api)
        page.refresh_fingerprint()
        page.fingerprint_list.setCurrentRow(0)
        page.btn_fingerprint_delete.click()
        self.delete_action(page, "confirm")
        api.fingerprint_delete.call_args.kwargs["on_err"]("模块忙")
        self.assertIn("删除失败：模块忙", page.fingerprint_feedback.text())
        self.assertEqual(page.fingerprint_list.count(), 1)
        self.assertTrue(page.btn_fingerprint_delete.isEnabled())
        page.close()

    def test_unregistered_template_can_only_be_deleted_after_confirmation(self):
        api = FingerprintApiStub()
        api.fingerprint_delete = Mock()
        page = SettingsPage(api)
        page.refresh_fingerprint()
        page._on_fingerprint_templates([{"id": 9, "registered": False}])
        page.fingerprint_list.setCurrentRow(0)
        self.assertTrue(page.btn_fingerprint_delete.isEnabled())
        self.assertFalse(page.btn_fingerprint_rename.isEnabled())
        page.btn_fingerprint_delete.click()
        api.fingerprint_delete.assert_not_called()
        message = page._delete_confirmation.findChild(QLabel, "FingerprintDeleteMessage").text()
        self.assertIn("未登记模板（ID 9）", message)
        self.assertIn("不可恢复", message)
        self.delete_action(page, "cancel")
        page.btn_fingerprint_delete.click()
        self.delete_action(page, "confirm")
        self.assertEqual(api.fingerprint_delete.call_args.args, (9,))
        page.close()

    def test_delete_overlay_is_visible_in_normal_and_fullscreen_windows(self):
        previous_lang = i18n.lang()
        self.addCleanup(i18n.set_lang, previous_lang)
        for language in ("zh", "en"):
            i18n.set_lang(language)
            for fullscreen in (False, True):
                with self.subTest(language=language, fullscreen=fullscreen):
                    api = FingerprintApiStub()
                    api.fingerprint_delete = Mock()
                    page = SettingsPage(api)
                    self.addCleanup(page.close)
                    page.setStyleSheet(theme.qss())
                    page.resize(784, 444)
                    page.showFullScreen() if fullscreen else page.show()
                    self.app.processEvents()
                    page.fingerprint_list.setCurrentRow(0)
                    scroll = page.tabs.widget(0)
                    scroll.ensureWidgetVisible(page.btn_fingerprint_delete)
                    self.app.processEvents()
                    QTest.mouseClick(page.btn_fingerprint_delete, Qt.MouseButton.LeftButton)
                    self.app.processEvents()
                    overlay = page._delete_confirmation
                    self.assertTrue(overlay.isVisible())
                    self.assertFalse(overlay.isWindow())
                    self.assertEqual(overlay.geometry(), page.rect())
                    self.assertFalse(page.tabs.isEnabled())
                    for button in overlay.findChildren(QPushButton):
                        rect = button.rect()
                        rect.moveTopLeft(button.mapTo(page, button.rect().topLeft()))
                        self.assertTrue(page.rect().contains(rect))
                    self.assertEqual(self.app.focusWidget().property("deleteAction"), "cancel")
                    api.fingerprint_delete.assert_not_called()
                    QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Escape)
                    self.app.processEvents()
                    self.assertIsNone(page._delete_confirmation)
                    self.assertTrue(page.tabs.isEnabled())
                    api.fingerprint_delete.assert_not_called()
                    page.close()

    def test_delete_confirmation_keeps_original_id_across_refresh_and_submits_once(self):
        api = FingerprintApiStub()
        api.fingerprint_delete = Mock()
        page = SettingsPage(api)
        self.addCleanup(page.close)
        page.refresh_fingerprint()
        page.fingerprint_list.setCurrentRow(0)
        page.btn_fingerprint_delete.click()
        page._on_fingerprint_templates([{"id": 9, "registered": False}])
        page.fingerprint_list.setCurrentRow(0)
        self.delete_action(page, "confirm")
        page._confirm_fingerprint_delete(7)
        self.assertEqual(api.fingerprint_delete.call_count, 1)
        self.assertEqual(api.fingerprint_delete.call_args.args, (7,))

    def test_leaving_settings_cancels_visible_confirmation(self):
        api = FingerprintApiStub()
        api.fingerprint_delete = Mock()
        page = SettingsPage(api)
        self.addCleanup(page.close)
        page.show()
        self.app.processEvents()
        page.fingerprint_list.setCurrentRow(0)
        page.btn_fingerprint_delete.click()
        page.resize(900, 600)
        self.app.processEvents()
        self.assertEqual(page._delete_confirmation.geometry(), page.rect())
        page.hide()
        self.assertIsNone(page._delete_confirmation)
        api.fingerprint_delete.assert_not_called()

    def test_management_buttons_are_disabled_while_sensor_is_busy(self):
        page = SettingsPage(FingerprintApiStub())
        page.refresh_fingerprint()
        page.fingerprint_list.setCurrentRow(0)
        page._on_fingerprint_health({
            "service": "fingerprintd", "connected": True, "action": "delete",
        })
        self.assertFalse(page.btn_fingerprint_delete.isEnabled())
        self.assertFalse(page.btn_fingerprint_rename.isEnabled())
        self.assertFalse(page.btn_fingerprint_refresh.isEnabled())
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
