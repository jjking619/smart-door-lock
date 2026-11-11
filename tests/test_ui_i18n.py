import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui import i18n, theme
from ui.api import parse_event
from ui.page_home import HomePage
from ui.page_records import RecordsPage
from ui.page_settings import SettingsPage


class I18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        i18n.install(cls.app)

    def tearDown(self):
        i18n.set_lang("zh")

    def test_chinese_mode_is_passthrough(self):
        i18n.set_lang("zh")
        self.assertEqual(i18n.tr("事件 120 条"), "事件 120 条")
        self.assertEqual(i18n.L("解锁", "Unlock"), "解锁")

    def test_english_translates_exact_and_composed_text(self):
        i18n.set_lang("en")
        self.assertEqual(i18n.tr("门已打开"), "Door opened")
        self.assertEqual(i18n.tr("事件 120 条"), "Events 120")
        self.assertEqual(i18n.tr("开锁请求失败：timeout"), "Unlock request failed: timeout")
        self.assertEqual(i18n.tr("张三"), "张三")

    def test_event_layout_translates_filters_groups_and_snapshot_errors(self):
        i18n.set_lang("en")
        for text, expected in {
            "安全 2": "Security 2",
            "查看详情": "Show details",
            "收起详情": "Hide details",
            "今天": "Today",
            "昨天": "Yesterday",
            "最近载入 4 条 · 出入 1 条": "Recently loaded 4 · Access 1",
            "快照读取失败：HTTP 404": "Snapshot load failed: HTTP 404",
            "此事件没有关联图片或录像": "No image or recording attached",
        }.items():
            with self.subTest(text=text):
                self.assertEqual(i18n.tr(text), expected)

    def test_home_page_renders_english(self):
        i18n.set_lang("en")
        page = HomePage(object())
        page.show()
        self.app.processEvents()
        self.assertEqual(page.btn_talk.text(), "Intercom")
        self.assertEqual(page.tile_package.title.text(), "Parcel")
        for status, expected in {
            "正在播放开场提示": "Playing greeting",
            "请说话": "Please speak",
            "没有听到清晰语音": "No clear speech detected",
        }.items():
            with self.subTest(status=status):
                page._update_conversation([], status)
                self.assertEqual(page.conversation_status.text(), expected)
        page._update_conversation([{"role": "visitor", "text": "你好"}], "请继续说话")
        self.assertIn("You: 你好", page.conversation_text.toPlainText())
        self.assertEqual(page.conversation_status.text(), "Please keep talking")
        page.close()
        page.deleteLater()

    def test_event_details_toggle_renders_english_on_small_screen(self):
        i18n.set_lang("en")
        with patch("ui.page_records.HAVE_MULTIMEDIA", False):
            page = RecordsPage(SimpleNamespace(media_url=lambda path: f"http://localhost/{path}"))
            self.addCleanup(page.close)
            page.setStyleSheet(theme.qss())
            page.resize(828, 496)
            page.show()
            page._on_event_selected(parse_event({
                "type": "intrusion", "clip": "clips/event.mp4", "dwell_s": 32,
            }))
            self.app.processEvents()
            self.assertEqual(page.play_title.text(), "Loitering")
            self.assertEqual(page.btn_event_details.text(), "Show details")
            self.assertGreaterEqual(page.video_stack.height(), 290)
            page.btn_event_details.click()
            self.app.processEvents()
            self.assertEqual(page.btn_event_details.text(), "Hide details")
            self.assertFalse(page.event_details.isHidden())
            self.assertIn("Dwell", page.event_fields.text())

    def test_settings_tabs_and_fingerprint_list_render_english(self):
        i18n.set_lang("en")
        page = SettingsPage(SimpleNamespace(base_url="http://127.0.0.1:8083", token=""))
        self.addCleanup(page.close)
        self.assertEqual(page.tabs.tabText(0), "Fingerprints")
        self.assertEqual(page.tabs.tabText(1), "Connection / display")
        self.assertEqual(page.tabs.tabText(2), "Diagnostics / parameters")
        page._on_fingerprint_templates([{"id": 9, "registered": False}])
        self.assertIn("Unregistered template", page.fingerprint_list.item(0).text())


if __name__ == "__main__":
    unittest.main()
