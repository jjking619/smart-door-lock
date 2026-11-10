"""门锁操作界面主窗口。

布局（对应 docs/ui-design.md 的线框图）

    ┌──────────────────────────────────────────────────────────────┐
    │ 门锁 · DOORLOCK            01-03 周三           20:15  ● 在线 │  顶栏 56 px
    ├────┬─────────────────────────────────────────────────────────┤
    │ 首 │                                                         │
    │ 页 │   页面区（首页 / 事件 / 回放 / 设置）                     │
    │ 事 │                                                         │
    │ 件 │                                                         │
    │ 回 │                                                         │
    │ 放 │                                                         │
    │ 设 │                                                         │
    │ 置 │                                                         │
    └────┴─────────────────────────────────────────────────────────┘

为什么左侧竖排导航而不是顶部横排：1024×600 的竖屏/横屏触摸屏上，
竖排能保住 56 px 高度给画面，横排会白白吃掉一行。
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel,
    QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)
from . import theme
from .api import ApiClient, CapabilityProbe
from .page_home import HomePage
from .page_records import RecordsPage
from .page_settings import SettingsPage
from .widgets import StatusPill

if TYPE_CHECKING:
    pass


NAV = [
    ("home", "首页"),
    ("records", "记录"),
    ("settings", "设置"),
]


class MainWindow(QMainWindow):
    def __init__(self, base_url: str | None = None, token: str = "") -> None:
        super().__init__()
        self._settings = QSettings("doorlock", "doorui")
        base = base_url or self._settings.value(
            "base_url", os.getenv("DOORLOCK_BASE", "http://127.0.0.1:8083"), str
        )
        token = token or self._settings.value("token", os.getenv("VISION_TOKEN", ""), str)

        self.api = ApiClient(base, token, self)
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
        rail.setFixedWidth(88)
        rail_lay = QVBoxLayout(rail)
        rail_lay.setContentsMargins(0, 14, 0, 14)
        rail_lay.setSpacing(2)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        for idx, (key, label) in enumerate(NAV):
            btn = QPushButton(label)
            btn.setObjectName("NavBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(64)
            btn.clicked.connect(lambda _=False, k=key: self.goto(k))
            self._nav_group.addButton(btn)
            self._nav_buttons[key] = btn
            rail_lay.addWidget(btn)
            if idx == 0:
                btn.setChecked(True)

        rail_lay.addStretch(1)
        ver = QLabel("v0.1")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        rail_lay.addWidget(ver)
        outer.addWidget(rail)

        # ---------------- 右侧：顶栏 + 页面 ----------------
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        header = QWidget()
        header.setObjectName("Header")
        header.setFixedHeight(60)
        head = QHBoxLayout(header)
        head.setContentsMargins(20, 0, 20, 0)
        head.setSpacing(12)

        brand = QLabel("门锁")
        brand.setObjectName("Brand")
        brand_sub = QLabel("DOORLOCK")
        brand_sub.setObjectName("BrandSub")
        head.addWidget(brand)
        head.addWidget(brand_sub)
        head.addStretch(1)

        self.online = StatusPill("连接中", theme.WARN)
        head.addWidget(self.online, 0, Qt.AlignmentFlag.AlignVCenter)

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

        # 键盘快捷键：调试/远程桌面友好
        for i, (key, _label) in enumerate(NAV, start=1):
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
                self.home.hint.setText(
                    f"开锁动作已完成：{result.get('name') or '本机面板'}，"
                    f"保持 {result.get('seconds')} 秒后已自动回锁"
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

    def closeEvent(self, ev) -> None:  # noqa: N802
        self.home.stop()
        self.records.stop()
        super().closeEvent(ev)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="门锁桌面操作界面")
    parser.add_argument("--base", default="", help="visiond 地址，默认 http://127.0.0.1:8083")
    parser.add_argument("--token", default="", help="VISION_TOKEN")
    parser.add_argument("--kiosk", action="store_true", help="全屏无边框")
    args = parser.parse_args(argv)

    QApplication.setApplicationName("doorui")
    QApplication.setOrganizationName("doorlock")
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.qss())

    win = MainWindow(args.base or None, args.token)
    if args.kiosk:
        win.showFullScreen()
    else:
        win.resize(1180, 720)
        win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
