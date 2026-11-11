"""门锁操作界面主窗口。

布局（1280×800 触摸屏，展会演示优先）

    ┌────┬─────────────────────────────────────────────────────────┐
    │ 🔒 │ AI 智能门锁  [指纹][录制][入侵][快递][对讲]   ● 在线 11:38 │  顶栏 68 px
    │ 首页├─────────────────────────────────────────────────────────┤
    │ 记录│   页面区（首页 / 记录 / 设置）                            │
    │ 设置│                                                         │
    └────┴─────────────────────────────────────────────────────────┘

顶栏的五个彩色标签与首页功能磁贴同色，观众一眼对上"哪个功能在哪"。

为什么左侧竖排导航而不是顶部横排：1024×600 的竖屏/横屏触摸屏上，
竖排能保住 56 px 高度给画面，横排会白白吃掉一行。
"""

from __future__ import annotations

import os
import sys
import uuid

from PySide6.QtCore import QEventLoop, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel,
    QMainWindow, QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)
from . import i18n, theme
from .i18n import L
from .api import ApiClient, CapabilityProbe
from .page_home import HomePage
from .page_records import RecordsPage
from .page_settings import SettingsPage
from .system_input import configure_system_input_method
from .unlock_request import UnlockRequestDialog
from .widgets import IconBadge, StatusPill, make_icon

NAV = [
    ("home", "首页", "home"),
    ("records", "记录", "list"),
    ("settings", "设置", "gear"),
]


class MainWindow(QMainWindow):
    language_requested = Signal(str)

    def __init__(self, base_url: str | None = None, token: str = "") -> None:
        super().__init__()
        self._settings = QSettings("doorlock", "doorui")
        base = base_url or self._settings.value(
            "base_url", os.getenv("DOORLOCK_BASE", "http://127.0.0.1:8083"), str
        )
        token = token or self._settings.value("token", os.getenv("VISION_TOKEN", ""), str)

        self.api = ApiClient(base, token, self)
        # 每个窗口独立租约：切换语言时新窗口先续租，旧窗口关闭只释放自己的
        self._ui_client_id = f"ui-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._ui_released = False
        self.setWindowTitle("门锁操作界面")
        self.setMinimumSize(960, 600)

        root = QWidget()
        root.setObjectName("Root")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---------------- 左侧导航 ----------------
        rail = QWidget()
        rail.setObjectName("Rail")
        rail.setFixedWidth(92)
        rail_lay = QVBoxLayout(rail)
        rail_lay.setContentsMargins(10, 14, 10, 14)
        rail_lay.setSpacing(8)

        logo = IconBadge("lock", theme.ACCENT, 48)
        rail_lay.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        rail_lay.addSpacing(14)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QToolButton] = {}
        for idx, (key, label, icon) in enumerate(NAV):
            btn = QToolButton()
            btn.setText(label)
            btn.setIcon(make_icon(icon, theme.TEXT_DIM, theme.ACCENT, size=26))
            btn.setIconSize(QSize(26, 26))
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setObjectName("NavBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedSize(72, 72)
            btn.clicked.connect(lambda _=False, k=key: self.goto(k))
            self._nav_group.addButton(btn)
            self._nav_buttons[key] = btn
            rail_lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)
            if idx == 0:
                btn.setChecked(True)

        rail_lay.addStretch(1)
        ver = QLabel("v0.1")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:15px;")
        rail_lay.addWidget(ver)
        outer.addWidget(rail)

        # ---------------- 右侧：顶栏 + 页面 ----------------
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        header = QWidget()
        header.setObjectName("Header")
        header.setFixedHeight(68)
        head = QHBoxLayout(header)
        head.setContentsMargins(22, 0, 22, 0)
        head.setSpacing(14)

        brand = QLabel("AI 智能门锁")
        brand.setObjectName("Brand")
        head.addWidget(brand)
        head.addSpacing(18)

        for text, color in (("指纹识别", theme.FEAT_FINGER), ("视频录制", theme.FEAT_VIDEO),
                            ("入侵检测", theme.FEAT_INTRUSION), ("快递识别", theme.FEAT_PACKAGE),
                            ("语音对讲", theme.FEAT_TALK)):
            chip = QLabel(text)
            chip.setStyleSheet(
                f"color:{color};border:1px solid {color};border-radius:12px;"
                "padding:3px 10px;font-size:14px;font-weight:600;"
            )
            head.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)

        self.online = StatusPill("连接中", theme.WARN)
        head.addWidget(self.online, 0, Qt.AlignmentFlag.AlignVCenter)

        other = "zh" if i18n.is_en() else "en"
        self.btn_lang = QPushButton("中文" if other == "zh" else "EN")
        self.btn_lang.setObjectName("LangBtn")
        self.btn_lang.setToolTip("切换语言 / Switch language")
        self.btn_lang.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_lang.setFixedSize(64, 36)
        self.btn_lang.clicked.connect(lambda: self.language_requested.emit(other))
        head.addWidget(self.btn_lang, 0, Qt.AlignmentFlag.AlignVCenter)

        clock_col = QVBoxLayout()
        clock_col.setSpacing(0)
        self.clock = QLabel("--:--")
        self.clock.setObjectName("Clock")
        self.clock.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.date = QLabel("")
        self.date.setObjectName("ClockSub")
        self.date.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col.addWidget(self.clock)
        clock_col.addWidget(self.date)
        head.addLayout(clock_col)

        right.addWidget(header)

        page_host = QWidget()
        page_lay = QVBoxLayout(page_host)
        page_lay.setContentsMargins(20, 18, 20, 18)
        page_lay.setSpacing(0)

        self.stack = QStackedWidget()
        page_lay.addWidget(self.stack)
        right.addWidget(page_host, 1)

        outer.addLayout(right, 1)
        self.setCentralWidget(root)

        # ---------------- 页面 ----------------
        self.home = HomePage(self.api)
        self.records = RecordsPage(self.api)
        self.records.pause_loading()
        self.settings = SettingsPage(self.api)
        self._pages = {
            "home": self.home,
            "records": self.records,
            "settings": self.settings,
        }
        for page in self._pages.values():
            self.stack.addWidget(page)

        # ---------------- 连线 ----------------
        self.home.replay_requested.connect(self._play_event)
        self.home.goto_events.connect(lambda: self.goto("records"))
        self.home.goto_settings.connect(lambda: self.goto("settings"))
        self.home.unlock_requested.connect(self._request_unlock)
        self.home.talk_changed.connect(self._talk)
        self.settings.base_url_changed.connect(self._on_base_changed)
        self.settings.kiosk_changed.connect(lambda on: self.set_kiosk(on))

        # ---------------- 能力探测 ----------------
        self.probe = CapabilityProbe(self.api, self)
        self.probe.changed.connect(self._on_caps)
        self.probe.probe()

        # ---------------- 时钟 / 在线状态 ----------------
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._tick)
        self._clock_timer.start()
        self._tick()

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(5000)
        self._health_timer.timeout.connect(self._poll_health)
        self._health_timer.start()
        self._poll_health()

        # visiond 在心跳超时后会释放相机、暂停检测；租期默认 15 s
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(5000)
        self._heartbeat_timer.timeout.connect(self._send_heartbeat)
        self._heartbeat_timer.start()
        self._send_heartbeat()

        # 访客语音"请求开门"：audiood 挂起请求，界面弹窗由屋主决定
        self._unlock_dialog: UnlockRequestDialog | None = None
        self._handled_unlock_ids: set[str] = set()
        self._unlock_poll = QTimer(self)
        self._unlock_poll.setInterval(1500)
        self._unlock_poll.timeout.connect(self._poll_unlock_request)
        if hasattr(self.api, "audio_health"):
            self._unlock_poll.start()

        # 键盘快捷键：调试/远程桌面友好
        for i, (key, _label, _icon) in enumerate(NAV, start=1):
            QShortcut(QKeySequence(str(i)), self,
                      activated=lambda k=key: self.goto(k))
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)

        self.home.start()

        self._kiosk_shortcut = QShortcut(QKeySequence("F11"), self,
                                        activated=self._toggle_kiosk)

    # ---------------- 导航 ----------------
    def goto(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        was_current = self.stack.currentWidget() is page
        if self.stack.currentWidget() is self.records and key != "records":
            self.records.pause_loading()
        self.stack.setCurrentWidget(page)
        btn = self._nav_buttons.get(key)
        if btn is not None:
            btn.setChecked(True)
        if key == "records" and not was_current:
            self.records.resume_loading()
            self.records.refresh()
        elif key == "settings":
            self.settings.refresh_diagnostics()
        elif key == "home":
            self.home.refresh()

    # ---------------- 状态 ----------------
    def _tick(self) -> None:
        from datetime import datetime

        now = datetime.now()
        self.clock.setText(now.strftime("%H:%M"))
        if i18n.is_en():
            self.date.setText(now.strftime("%a %m-%d"))
        else:
            weekdays = "一二三四五六日"
            self.date.setText(f"{now.month:02d}-{now.day:02d} 周{weekdays[now.weekday()]}")

    def _poll_health(self) -> None:
        self.api.health(self._on_health, self._on_health_err)

    def _on_health(self, h) -> None:
        if h.ok and h.fps > 0.1:
            self.online.set_state("在线", theme.OK)
        elif h.ok:
            self.online.set_state("无画面", theme.WARN)
        else:
            self.online.set_state("异常", theme.CRIT)

    def _on_health_err(self, _msg: str) -> None:
        self.online.set_state("离线", theme.CRIT)

    def _on_caps(self, unlock_ok: bool, talk_ok: bool) -> None:
        self.home.set_capabilities(unlock_ok, talk_ok)

    # ---------------- 动作 ----------------
    def _play_event(self, event) -> None:
        self.goto("records")
        self.records.play_event(event)

    def _request_unlock(self) -> None:
        """界面开锁：只发请求，由 lockd 校验策略（本机来源、防重复、失败锁定）。"""

        def ok(data) -> None:
            result = data if isinstance(data, dict) else {}
            self.home.btn_unlock.setEnabled(True)
            if result.get("ok"):
                self.home.show_temporary_hint(
                    f"开锁动作已完成：{result.get('name') or '本机面板'}，"
                    f"保持 {result.get('seconds')} 秒后已自动回锁",
                    5000,
                )
            else:
                self.home.hint.setText(f"未开锁：{result.get('reason') or result.get('error') or '被拒绝'}")

        def err(message: str) -> None:
            self.home.btn_unlock.setEnabled(True)
            self.home.hint.setText(f"开锁请求失败：{message}")

        if hasattr(self.api, "lock_unlock"):
            self.api.lock_unlock(on_ok=ok, on_err=err)
        else:
            self.api.unlock(on_ok=ok, on_err=err)

    def _talk(self, on: bool) -> None:
        self.api.talk(on, on_err=lambda m: self.home.hint.setText(f"对讲控制失败：{m}"))

    def _on_base_changed(self, _base: str) -> None:
        self.records.invalidate()
        self.probe.probe()
        self.home.video.stop()
        self.home.video.start()
        self._poll_health()

    # ---------------- kiosk ----------------
    def set_kiosk(self, on: bool) -> None:
        if on:
            self.showFullScreen()
        else:
            self.showNormal()

    def _toggle_kiosk(self) -> None:
        self.set_kiosk(not self.isFullScreen())

    def _send_heartbeat(self) -> None:
        self.api.ui_heartbeat(self._ui_client_id)

    # ---------------- 访客请求开门 ----------------
    def _poll_unlock_request(self) -> None:
        self.api.audio_health(self._on_unlock_status, lambda _m: None)

    def _on_unlock_status(self, status) -> None:  # noqa: ANN001
        req = status.get("unlock_request") if isinstance(status, dict) else None
        dialog = self._unlock_dialog
        if not isinstance(req, dict) or not req.get("id"):
            if dialog is not None and dialog.isVisible():
                dialog.expire(L("请求已失效", "Request no longer pending"))
            return
        req_id = str(req["id"])
        if dialog is not None and dialog.isVisible() and dialog.request_id == req_id:
            dialog.set_text(str(req.get("text") or ""))
            return
        if req_id in self._handled_unlock_ids:
            return
        self._handled_unlock_ids.add(req_id)
        self._show_unlock_request(req)

    def _show_unlock_request(self, req: dict) -> None:
        if self._unlock_dialog is not None:
            self._unlock_dialog.dismiss()
        dialog = UnlockRequestDialog(req, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.decided.connect(lambda decision, d=dialog: self._on_unlock_decision(d, decision))
        dialog.destroyed.connect(lambda _o=None, d=dialog: self._forget_unlock_dialog(d))
        self._unlock_dialog = dialog
        self.goto("home")
        self.home.hint.setText(L("访客请求开门，等待您确认", "Visitor asks to open the door — waiting for you"))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._fetch_unlock_snapshot(dialog, attempts=4)

    def _fetch_unlock_snapshot(self, dialog: UnlockRequestDialog, attempts: int) -> None:
        # visiond 刚从暂停恢复时相机还没出帧，/snapshot.jpg 会先返回 503，稍后重试
        def failed(_message: str) -> None:
            if self._unlock_dialog is not dialog:
                return
            if attempts > 1:
                QTimer.singleShot(1500, lambda: self._fetch_unlock_snapshot(dialog, attempts - 1))
            else:
                dialog.set_snapshot(b"")

        def loaded(data: bytes) -> None:
            if self._unlock_dialog is dialog:
                dialog.set_snapshot(data)

        self.api.get_bytes_url(self.api.snapshot_url(), loaded, failed)

    def _forget_unlock_dialog(self, dialog: UnlockRequestDialog) -> None:
        if self._unlock_dialog is dialog:
            self._unlock_dialog = None

    def _on_unlock_decision(self, dialog: UnlockRequestDialog, decision: str) -> None:
        req_id = dialog.request_id

        def report(unlocked: bool | None, reason: str = "") -> None:
            payload = {"id": req_id, "decision": decision}
            if unlocked is not None:
                payload["unlocked"] = unlocked
            if reason:
                payload["reason"] = reason
            self.api.audio_unlock_decision(payload, lambda _r: None, lambda _m: None)

        if decision != "allow":
            report(None)
            self.home.hint.setText(L("已拒绝访客的开门请求", "Visitor unlock request denied"))
            dialog.finish(L("已拒绝，已告知访客", "Denied — the visitor has been told"), theme.CRIT, 1500)
            return

        def ok(data) -> None:  # noqa: ANN001
            result = data if isinstance(data, dict) else {}
            if result.get("ok"):
                report(True)
                text = L(f"已为访客开门，{result.get('seconds')} 秒后自动回锁",
                         f"Door opened for the visitor; relocks in {result.get('seconds')}s")
                self.home.hint.setText(text)
                dialog.finish(text, theme.ACCENT)
            else:
                reason = str(result.get("reason") or result.get("error") or "被拒绝")
                report(False, reason)
                self.home.hint.setText(f"未开锁：{reason}")
                dialog.finish(f"未开锁：{reason}", theme.CRIT, 4000)

        def err(message: str) -> None:
            report(False, message)
            self.home.hint.setText(f"开锁请求失败：{message}")
            dialog.finish(f"开锁请求失败：{message}", theme.CRIT, 4000)

        self.api.lock_unlock(
            {"name": "访客（屋主确认）", "method": "访客语音请求"}, on_ok=ok, on_err=err,
        )

    def _release_backend(self, timeout_ms: int = 800) -> None:
        """正常关闭时立即释放租约，让 visiond 不必等到超时才暂停。"""
        if self._ui_released:
            return
        self._ui_released = True
        self._heartbeat_timer.stop()
        loop = QEventLoop()
        QTimer.singleShot(timeout_ms, loop.quit)
        self.api.ui_heartbeat(self._ui_client_id, False,
                              on_ok=lambda _r: loop.quit(), on_err=lambda _m: loop.quit())
        loop.exec()

    def closeEvent(self, ev) -> None:  # noqa: N802
        self._unlock_poll.stop()
        if self._unlock_dialog is not None:
            self._unlock_dialog.dismiss()
        self.home.stop()
        self.records.stop()
        self._release_backend()
        super().closeEvent(ev)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="门锁桌面操作界面")
    parser.add_argument("--base", default="", help="visiond 地址，默认 http://127.0.0.1:8083")
    parser.add_argument("--token", default="", help="VISION_TOKEN")
    parser.add_argument("--kiosk", action="store_true", help="全屏无边框")

    parser.add_argument("--lang", choices=("en", "zh"), default="",
                        help="界面语言，默认沿用上次选择（首次为英文）")
    args = parser.parse_args(argv)

    if configure_system_input_method():
        print("Input method: using GNOME Wayland's system input channel", file=sys.stderr)

    QApplication.setApplicationName("doorui")
    QApplication.setOrganizationName("doorlock")
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.qss())
    settings = QSettings("doorlock", "doorui")
    i18n.set_lang(args.lang or settings.value("lang", "en", str))
    i18n.install(app)

    state: dict = {}

    def open_window(geometry=None, fullscreen: bool = args.kiosk) -> None:
        win = MainWindow(args.base or None, args.token)
        win.language_requested.connect(switch_language)
        if geometry is not None:
            win.setGeometry(geometry)
        else:
            win.resize(1180, 720)
        if fullscreen:
            win.showFullScreen()
        else:
            win.show()
        state["win"] = win

    def switch_language(value: str) -> None:
        """整窗重建：所有文字都在构造时按当前语言生成，不用逐个控件回填。"""
        old = state["win"]
        i18n.set_lang(value)
        settings.setValue("lang", i18n.lang())
        open_window(old.geometry(), old.isFullScreen())
        old.close()
        old.deleteLater()

    open_window()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
