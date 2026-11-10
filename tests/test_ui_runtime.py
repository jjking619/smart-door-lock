import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Requires GUI runtime")
class UiRuntimeTests(unittest.TestCase):
    def test_talk_start_stop_and_interpreter_shutdown(self):
        program = textwrap.dedent("""
            import sys
            from PySide6.QtCore import QObject, QTimer, Signal
            from PySide6.QtWidgets import QApplication
            from ui.app import MainWindow

            class Source(QObject):
                changed = Signal(bool)

            source = Source()
            source.changed.connect(lambda value: None)
            before = (sys.getrefcount(True), sys.getrefcount(False))
            for index in range(20):
                source.changed.emit(True)
                source.changed.emit(False)
            after = (sys.getrefcount(True), sys.getrefcount(False))
            assert before == after, (before, after)

            app = QApplication([])
            win = MainWindow('http://127.0.0.1:1', '')
            transitions = []
            win.api.talk = lambda on, **kwargs: transitions.append(on)
            win.api.audio_health = lambda on_ok, on_err=None: on_ok(
                {'busy': win.home._talking})
            win.home._update_conversation(
                [
                    {'role': 'visitor', 'text': '快递到了', 'partial': False},
                    {'role': 'assistant', 'text': '请放在门口。', 'partial': False},
                ],
                '请继续说话',
            )
            assert '你：快递到了' in win.home.conversation_text.toPlainText()
            assert '助手：请放在门口。' in win.home.conversation_text.toPlainText()
            win.home._show_activity('conversation')
            assert win.home.activity_stack.currentWidget() is win.home.conversation_view
            win.home._show_activity('events')
            assert win.home.activity_stack.currentWidget() is win.home.events_view
            win.show()
            timer = QTimer()
            count = 0

            def toggle():
                global count
                win.home.btn_talk.setEnabled(True)
                win.home.btn_talk.click()
                count += 1
                if count == 1:
                    assert win.home.activity_stack.currentWidget() is win.home.conversation_view
                    assert not win.home.conversation_text.toPlainText()
                if count == 200:
                    timer.stop()
                    assert not win.home._talking
                    assert not win.home._talk_poll.isActive()
                    win.close()

            timer.timeout.connect(toggle)
            timer.start(5)
            QTimer.singleShot(10000, app.quit)
            assert app.exec() == 0
            assert transitions == [True, False] * 100, len(transitions)
            print('talk start/stop regression passed', flush=True)
        """)
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", program],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("talk start/stop regression passed", result.stdout)


if __name__ == "__main__":
    unittest.main()