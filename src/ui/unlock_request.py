"""访客语音"请求开门"弹窗：给屋主看门口抓拍和访客原话，由屋主决定是否开门。

弹窗本身不开锁，只发出 ``decided("allow"|"deny")``；真正开锁仍走 lockd。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import theme
from .i18n import L

SNAP_W, SNAP_H = 480, 270


class UnlockRequestDialog(QDialog):
    decided = Signal(str)

    def __init__(self, request: dict, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.request_id = str(request.get("id") or "")
        self._remaining = max(1, int(request.get("expires_in") or 60))
        self._done = False
        self.setObjectName("UnlockRequest")
        self.setWindowTitle(L("访客请求开门", "Visitor unlock request"))
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setStyleSheet(
            f"QDialog#UnlockRequest {{ background: {theme.SURFACE}; border: 2px solid {theme.WARN};"
            " border-radius: 14px; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        title = QLabel(L("访客请求开门", "Visitor asks to open the door"))
        title.setStyleSheet(f"color: {theme.WARN}; font-size: 22px; font-weight: 700;")
        root.addWidget(title)

        self.snapshot = QLabel(L("正在获取门口画面…", "Loading doorstep snapshot…"))
        self.snapshot.setFixedSize(SNAP_W, SNAP_H)
        self.snapshot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.snapshot.setStyleSheet(
            f"background: {theme.BG}; color: {theme.TEXT_MUTED}; border: 1px solid {theme.BORDER};"
            " border-radius: 10px; font-size: 15px;"
        )
        root.addWidget(self.snapshot, 0, Qt.AlignmentFlag.AlignHCenter)

        self.said = QLabel()
        self.said.setWordWrap(True)
        self.said.setStyleSheet(f"color: {theme.TEXT}; font-size: 18px;")
        root.addWidget(self.said)
        self.set_text(str(request.get("text") or ""))

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 15px;")
        root.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.btn_deny = QPushButton(L("拒绝", "Deny"))
        self.btn_deny.setObjectName("Danger")
        self.btn_allow = QPushButton(L("允许开门", "Allow"))
        self.btn_allow.setObjectName("Primary")
        for btn in (self.btn_deny, self.btn_allow):
            btn.setMinimumHeight(52)
            btn.setStyleSheet("font-size: 18px;")
            buttons.addWidget(btn, 1)
        root.addLayout(buttons)

        self.btn_allow.clicked.connect(lambda: self._decide("allow"))
        self.btn_deny.clicked.connect(lambda: self._decide("deny"))

        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._tick)
        self._countdown.start()
        self._show_remaining()

    def set_text(self, text: str) -> None:
        text = text.strip()
        self.said.setText(
            L(f"访客说：“{text}”", f"Visitor said: \"{text}\"") if text
            else L("访客通过语音请求开门", "The visitor asked by voice to open the door")
        )

    def set_snapshot(self, data: bytes) -> None:
        pix = QPixmap()
        if not data or not pix.loadFromData(data):
            self.snapshot.setText(L("暂时拿不到门口画面", "Doorstep snapshot unavailable"))
            return
        self.snapshot.setPixmap(pix.scaled(
            SNAP_W, SNAP_H, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def set_status(self, text: str, color: str | None = None) -> None:
        self.status.setStyleSheet(f"color: {color or theme.TEXT_DIM}; font-size: 15px;")
        self.status.setText(text)

    def _show_remaining(self) -> None:
        self.set_status(L(f"{self._remaining} 秒内未处理将自动忽略",
                          f"Ignored automatically in {self._remaining}s"))

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self.expire(L("请求已超时", "Request timed out"))
            return
        self._show_remaining()

    def _decide(self, decision: str) -> None:
        if self._done:
            return
        self._done = True
        self._countdown.stop()
        self.btn_allow.setEnabled(False)
        self.btn_deny.setEnabled(False)
        if decision == "allow":
            self.set_status(L("正在开锁…", "Unlocking…"), theme.ACCENT)
        self.decided.emit(decision)

    def finish(self, text: str, color: str | None = None, close_after_ms: int = 2500) -> None:
        self._done = True
        self._countdown.stop()
        self.btn_allow.setEnabled(False)
        self.btn_deny.setEnabled(False)
        self.set_status(text, color)
        QTimer.singleShot(close_after_ms, self.close)

    def expire(self, text: str) -> None:
        if self._done:
            return
        self.finish(text, theme.TEXT_MUTED, 1500)

    def dismiss(self) -> None:
        """界面退出时关掉弹窗但不替屋主做决定，请求留给 audiood 自行超时。"""
        self._done = True
        self._countdown.stop()
        self.close()

    def reject(self) -> None:  # noqa: D401 - Esc / 关闭按钮视为"拒绝"
        if not self._done:
            self._decide("deny")
            return
        super().reject()

    def closeEvent(self, ev) -> None:  # noqa: N802, ANN001
        if not self._done:
            self._decide("deny")
        super().closeEvent(ev)
