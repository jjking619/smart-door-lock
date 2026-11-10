import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.page_home import HomePage


class HomeConversationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_conversation_view_renders_and_persists_until_next_session(self):
        page = HomePage(api=object())
        page._update_conversation(
            [
                {"role": "visitor", "text": "快递到了", "partial": False},
                {"role": "assistant", "text": "请放在门口。", "partial": False},
            ],
            "请继续说话",
        )

        text = page.conversation_text.toPlainText()
        self.assertIn("你：快递到了", text)
        self.assertIn("助手：请放在门口。", text)

        page._show_activity("conversation")
        self.assertIs(page.activity_stack.currentWidget(), page.conversation_view)
        page._talk(False)
        page._talk(True)
        self.assertEqual(page.conversation_text.toPlainText(), "")
        self.assertIs(page.activity_stack.currentWidget(), page.conversation_view)

        page._update_conversation(
            [{"role": "visitor", "text": "你好", "partial": False}],
            "对话结束",
        )
        page._talk(False)
        self.assertIn("你：你好", page.conversation_text.toPlainText())

        page.close()
        page.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
