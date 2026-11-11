import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QInputDevice, QInputMethod, QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QScroller

from ui.page_settings import SettingsPage
from ui.system_input import configure_system_input_method


ROOT = Path(__file__).resolve().parents[1]


class SystemInputSelectionTests(unittest.TestCase):
    def test_gnome_wayland_uses_compositor_managed_ibus(self):
        for desktop in ("GNOME", "ubuntu:GNOME", "GNOME-Classic:GNOME"):
            for platform in ("", "wayland", "wayland-egl", "wayland:socket=wayland-0"):
                with self.subTest(desktop=desktop, platform=platform):
                    env = {
                        "XDG_CURRENT_DESKTOP": desktop,
                        "XDG_SESSION_TYPE": "wayland",
                        "QT_QPA_PLATFORM": platform,
                        "QT_IM_MODULE": "ibus",
                    }
                    expected = {**env, "QT_IM_MODULE": "wayland"}
                    self.assertTrue(configure_system_input_method(env))
                    self.assertEqual(env, expected)
                    self.assertFalse(configure_system_input_method(env))

    def test_preserves_other_desktops_platforms_and_explicit_input_methods(self):
        defaults = {
            "XDG_CURRENT_DESKTOP": "GNOME",
            "XDG_SESSION_TYPE": "wayland",
            "QT_IM_MODULE": "ibus",
        }
        cases = (
            {"XDG_CURRENT_DESKTOP": "KDE"},
            {"XDG_CURRENT_DESKTOP": ""},
            {"XDG_SESSION_TYPE": "x11"},
            {"XDG_SESSION_TYPE": "tty"},
            {"QT_QPA_PLATFORM": "xcb"},
            {"QT_QPA_PLATFORM": "offscreen"},
            {"QT_QPA_PLATFORM": "minimal"},
            {"QT_QPA_PLATFORM": "wayland;xcb"},
            {"QT_IM_MODULE": "fcitx"},
            {"QT_IM_MODULE": "wayland"},
            {"QT_IM_MODULE": ""},
            {"QT_IM_MODULE": "ibus:custom"},
            {"QT_IM_MODULES": "ibus"},
            {"QT_IM_MODULES": "fcitx;ibus"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                env = {**defaults, **overrides}
                original = env.copy()
                self.assertFalse(configure_system_input_method(env))
                self.assertEqual(env, original)


class FingerprintApiStub:
    base_url = "http://127.0.0.1:8083"
    token = ""

    def fingerprint_health(self, on_ok, _on_err=None):
        on_ok({"connected": True, "library_size": 300, "template_count": 0})

    def fingerprint_templates(self, on_ok, _on_err=None):
        on_ok([])


class SystemInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.touch = QTest.createTouchDevice(QInputDevice.DeviceType.TouchScreen)

    def setUp(self):
        self.page = SettingsPage(FingerprintApiStub())
        self.addCleanup(self.page.close)
        self.page.resize(784, 444)
        self.page.show()
        self.page.activateWindow()
        QTest.qWait(50)

    def prepare_field(self, field, tab=0):
        self.page.tabs.setCurrentIndex(tab)
        scroll = self.page.tabs.currentWidget()
        scroll.ensureWidgetVisible(field)
        self.page.tabs.tabBar().setFocus()
        QTest.qWait(30)
        self.assertTrue(field.isVisible())
        return scroll

    def test_tapping_each_field_requests_the_system_panel(self):
        for field, tab in (
            (self.page.fingerprint_name, 0),
            (self.page.fingerprint_note, 0),
            (self.page.base_edit, 1),
            (self.page.token_edit, 1),
        ):
            with self.subTest(field=field, tab=tab):
                self.prepare_field(field, tab)
                with patch.object(QInputMethod, "show") as show:
                    QTest.mouseClick(field, Qt.MouseButton.LeftButton)
                    self.assertTrue(field.hasFocus())
                    show.assert_called_once()
                    show.reset_mock()
                    QTest.mouseClick(field, Qt.MouseButton.LeftButton)
                    show.assert_called_once()

        self.page.fingerprint_name.setReadOnly(True)
        self.prepare_field(self.page.fingerprint_name)
        with patch.object(QInputMethod, "show") as show:
            QTest.mouseClick(self.page.fingerprint_name, Qt.MouseButton.LeftButton)
            show.assert_not_called()

    def test_touch_tap_focuses_the_name_and_requests_the_system_panel(self):
        field = self.page.fingerprint_name
        self.prepare_field(field)
        point = field.rect().center()
        with patch.object(QInputMethod, "show") as show:
            sequence = QTest.touchEvent(field, self.touch)
            sequence.press(0, point, field).commit()
            QTest.qWait(30)
            sequence.release(0, point, field).commit()
            QTest.qWait(250)
            self.assertTrue(field.hasFocus())
            show.assert_called()

    def test_touch_scrolling_still_allows_button_taps(self):
        button = self.page.btn_fingerprint_refresh
        scroll = self.page.tabs.currentWidget()
        scroll.ensureWidgetVisible(button)
        self.page._api.fingerprint_refresh = Mock()
        clicked = Mock()
        button.clicked.connect(clicked)
        viewport = scroll.viewport()
        point = button.mapTo(viewport, button.rect().center())
        sequence = QTest.touchEvent(viewport, self.touch)
        sequence.press(0, point, viewport).commit()
        QTest.qWait(30)
        sequence.release(0, point, viewport).commit()
        QTest.qWait(250)
        clicked.assert_called_once()

    def test_fingerprint_fields_accept_ime_commits_and_physical_keys(self):
        for field, chinese in (
            (self.page.fingerprint_name, "张三"),
            (self.page.fingerprint_note, "右手食指"),
        ):
            with self.subTest(chinese=chinese):
                self.prepare_field(field)
                field.clear()
                field.setFocus()
                self.assertTrue(field.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled))
                QApplication.sendEvent(field, QInputMethodEvent("pinyin", []))
                self.assertEqual(field.text(), "")
                commit = QInputMethodEvent()
                commit.setCommitString(chinese)
                QApplication.sendEvent(field, commit)
                QTest.keyClicks(field, " 123")
                self.assertEqual(field.text(), f"{chinese} 123")

    def test_token_keeps_password_and_sensitive_input_hints(self):
        field = self.page.token_edit
        self.prepare_field(field, 1)
        self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
        for hint in (
            Qt.InputMethodHint.ImhHiddenText,
            Qt.InputMethodHint.ImhSensitiveData,
            Qt.InputMethodHint.ImhNoPredictiveText,
            Qt.InputMethodHint.ImhNoAutoUppercase,
        ):
            self.assertTrue(field.inputMethodHints() & hint, hint)
        field.clear()
        field.setFocus()
        QTest.keyClicks(field, "token-123")
        self.assertEqual(field.text(), "token-123")

    def test_all_settings_sections_support_touch_scrolling(self):
        for index in range(self.page.tabs.count()):
            scroll = self.page.tabs.widget(index)
            self.assertNotEqual(
                QScroller.grabbedGesture(scroll.viewport()), Qt.GestureType(0)
            )

        self.page.tabs.setCurrentIndex(0)
        scroll = self.page.tabs.currentWidget()
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        viewport = scroll.viewport()
        start = QPoint(viewport.width() - 15, viewport.height() - 40)
        end = QPoint(start.x(), 30)
        sequence = QTest.touchEvent(viewport, self.touch)
        sequence.press(0, start, viewport).commit()
        QTest.qWait(40)
        sequence.move(0, end, viewport).commit()
        QTest.qWait(100)
        sequence.release(0, end, viewport).commit()
        QTest.qWait(200)
        self.assertGreater(scroll.verticalScrollBar().value(), 0)


class SystemInputRuntimeTests(unittest.TestCase):
    def test_selects_gnome_input_before_creating_qapplication(self):
        from ui import app as ui_app

        application = Mock()
        application.exec.return_value = 0

        def create_application(_argv):
            self.assertEqual(os.environ["QT_IM_MODULE"], "wayland")
            return application

        factory = Mock(side_effect=create_application)
        env = {
            "XDG_CURRENT_DESKTOP": "GNOME",
            "XDG_SESSION_TYPE": "wayland",
            "QT_IM_MODULE": "ibus",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(ui_app, "QApplication", factory),
            patch.object(ui_app, "QSettings"),
            patch.object(ui_app, "MainWindow"),
            patch.object(ui_app.i18n, "install"),
            patch.object(ui_app.i18n, "set_lang"),
        ):
            self.assertEqual(ui_app.main(["--lang", "zh"]), 0)
            factory.assert_called_once()

    def test_startup_and_language_switch_preserve_input_environment(self):
        program = textwrap.dedent("""
            import json
            import os
            import sys
            from PySide6.QtCore import QObject, QRect, QTimer, Signal
            from PySide6.QtWidgets import QApplication
            from ui import app as ui_app

            keys = ('QT_IM_MODULE', 'QT_IM_MODULES', 'QT_VIRTUALKEYBOARD_DESKTOP_DISABLE',
                    'DOORLOCK_TOUCH_KEYBOARD', 'QT_QPA_PLATFORM')
            expected = json.loads(os.environ['EXPECTED_INPUT'])
            windows = []

            class Window(QObject):
                language_requested = Signal(str)

                def __init__(self, base, token):
                    super().__init__()
                    windows.append(self)
                    assert {key: os.environ[key] for key in keys if key in os.environ} == expected

                def resize(self, *_args):
                    pass

                def setGeometry(self, *_args):
                    pass

                def geometry(self):
                    return QRect(0, 0, 960, 600)

                def isFullScreen(self):
                    return False

                def close(self):
                    pass

                def show(self):
                    if len(windows) == 1:
                        QTimer.singleShot(0, lambda: self.language_requested.emit('en'))
                    else:
                        QTimer.singleShot(0, QApplication.instance().quit)

            ui_app.MainWindow = Window
            assert ui_app.main(['--lang', 'zh']) == 0
            assert len(windows) == 2
            assert {key: os.environ[key] for key in keys if key in os.environ} == expected
            assert 'ui.touch_keyboard' not in sys.modules
            print('system input configuration preserved')
        """)
        keys = (
            "QT_IM_MODULE", "QT_IM_MODULES", "QT_VIRTUALKEYBOARD_DESKTOP_DISABLE",
            "DOORLOCK_TOUCH_KEYBOARD", "QT_QPA_PLATFORM",
        )
        cases = (
            {},
            {"QT_IM_MODULE": "ibus"},
            {"QT_IM_MODULE": "fcitx"},
            {"QT_IM_MODULE": "ibus", "QT_IM_MODULES": "ibus;wayland",
             "DOORLOCK_TOUCH_KEYBOARD": "qt"},
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                env = os.environ.copy()
                for key in keys:
                    env.pop(key, None)
                env.update(case)
                env.update({
                    "QT_QPA_PLATFORM": "offscreen",
                    "XDG_CONFIG_HOME": str(Path(folder) / "config"),
                    "XDG_CACHE_HOME": str(Path(folder) / "cache"),
                    "PYTHONPATH": str(ROOT / "src"),
                })
                env["EXPECTED_INPUT"] = json.dumps({key: env[key] for key in keys if key in env})
                result = subprocess.run(
                    [sys.executable, "-X", "faulthandler", "-c", program],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("system input configuration preserved", result.stdout)


if __name__ == "__main__":
    unittest.main()
