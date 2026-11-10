"""可复用的界面零件。

约定：所有"卡片"都用 QFrame#Card + 竖排布局，标题用 CardTitle（小号、灰、
带字距）。这样整屏看过去层级一致，不会出现哪里粗哪里细。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QSizePolicy, QVBoxLayout, QWidget,
)

from . import theme


def card(*, spacing: int = 10, margins: tuple[int, int, int, int] = (18, 16, 18, 16)) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return frame, lay


def card_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("CardTitle")
    return label


def hline() -> QFrame:
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background:{theme.BORDER};")
    return line


class StatusPill(QLabel):
    """带色点的状态标签。颜色语义与事件严重度一致。"""

    def __init__(self, text: str = "", color: str = theme.OK, parent=None) -> None:
        super().__init__(text, parent)
        self._color = color
        self._dot = True
        self._refresh()

    def set_state(self, text: str, color: str) -> None:
        self.setText(text)
        self._color = color
        self._refresh()

    def _refresh(self) -> None:
        self.setStyleSheet(
            f"color:{self._color};"
            f"background:rgba({_rgb(self._color)},0.12);"
            f"border:1px solid rgba({_rgb(self._color)},0.30);"
            "border-radius:11px;padding:2px 10px 2px 9px;font-size:12px;"
        )


class MetricTile(QWidget):
    """大数字 + 单位 + 说明。用于 FPS / 缓冲帧数这类运行指标。"""

    def __init__(self, label: str, unit: str = "", parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._label = QLabel(label)
        self._label.setObjectName("CardTitle")
        self._value = QLabel("—")
        self._value.setStyleSheet(f"font-size:26px;font-weight:600;color:{theme.TEXT};")
        self._unit = QLabel(unit)
        self._unit.setObjectName("Muted")
        self._unit.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:12px;")

        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self._value)
        row.addWidget(self._unit, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)

        lay.addWidget(self._label)
        lay.addLayout(row)

    def set_value(self, text: str, color: str | None = None) -> None:
        self._value.setText(text)
        self._value.setStyleSheet(
            f"font-size:26px;font-weight:600;color:{color or theme.TEXT};"
        )


class Thumb(QLabel):
    """事件缩略图。图片没到之前显示占位色块，不闪烁。"""

    FALLBACK = ["#243040", "#2A2438", "#1F3230", "#33292A"]

    def __init__(self, size: QSize = QSize(96, 54), parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(size)
        self.setScaledContents(True)
        self._pixmap: QPixmap | None = None
        self._seed = 0

    def set_image(self, data: bytes) -> None:
        pm = QPixmap()
        if pm.loadFromData(data, "JPG"):
            self._pixmap = pm
            self.setPixmap(pm)
            return
        self._placeholder()

    def _placeholder(self, seed: int = 0) -> None:
        self._seed = seed
        pm = QPixmap(self.size())
        pm.fill(QColor(self.FALLBACK[seed % len(self.FALLBACK)]))
        painter = QPainter(pm)
        painter.setPen(QColor(theme.TEXT_MUTED))
        painter.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "无图")
        painter.end()
        self.setPixmap(pm)

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        if self._pixmap is None and self.pixmap().isNull():
            self._placeholder()


class EventRow(QWidget):
    """事件列表的一行：缩略图 + 标题 + 副标题 + 时间。

    用 QListWidget + setItemWidget 而不是自定义 delegate —— 行数在几十条量级，
    不值得为省内存写 delegate，可读性更重要。
    """

    clicked = Signal(object)

    def __init__(self, event, parent=None) -> None:
        super().__init__(parent)
        # 绝对不能叫 self.event：那会覆盖 QWidget.event()，Qt 分发事件时
        # 会去调用这个 dict，直接段错误。
        self.event_data = event
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 12, 8)
        lay.setSpacing(12)

        self.thumb = Thumb()
        lay.addWidget(self.thumb)

        text = QVBoxLayout()
        text.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        dot = QLabel("●")
        dot.setStyleSheet(f"color:{event.color};font-size:10px;")
        title = QLabel(event.title)
        title.setStyleSheet(f"font-size:14px;font-weight:600;color:{theme.TEXT};")
        title_row.addWidget(dot)
        title_row.addWidget(title)
        title_row.addStretch(1)

        sub = QLabel(event.summary())
        sub.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:12px;")

        text.addLayout(title_row)
        text.addWidget(sub)

        lay.addLayout(text, 1)

        when = QVBoxLayout()
        when.setSpacing(1)
        t = QLabel(event.time_text)
        t.setAlignment(Qt.AlignmentFlag.AlignRight)
        t.setStyleSheet(f"color:{theme.TEXT};font-size:13px;font-weight:600;")
        d = QLabel(event.date_text)
        d.setAlignment(Qt.AlignmentFlag.AlignRight)
        d.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        when.addWidget(t)
        when.addWidget(d)
        lay.addLayout(when)


class EventList(QListWidget):
    selected = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSpacing(0)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.itemClicked.connect(self._emit)

    def set_events(self, events: list) -> None:
        self.clear()
        for ev in events:
            # 注意：QListWidgetItem(list) 会自动插入列表，不能再调 addItem，
            # 否则同一 item 被加入两次，Qt 渲染时会段错误。
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, ev)
            row = EventRow(ev)
            item.setSizeHint(QSize(0, row.sizeHint().height() + 6))
            self.addItem(item)
            self.setItemWidget(item, row)

    def _emit(self, item: QListWidgetItem) -> None:
        ev = item.data(Qt.ItemDataRole.UserRole)
        if ev is not None:
            self.selected.emit(ev)


class ToggleSwitch(QWidget):
    """布防开关。没有用 QCheckBox：这里要的是即时反馈的大控件。"""

    toggled = Signal(bool)

    def __init__(self, on: bool = True, parent=None) -> None:
        super().__init__(parent)
        self._on = on
        self.setFixedSize(52, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool) -> None:
        self._on = on
        self.update()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        self._on = not self._on
        self.update()
        self.toggled.emit(self._on)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = self.height() / 2
        fillcolor = QColor(theme.ACCENT if self._on else theme.BORDER)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fillcolor)
        p.drawRoundedRect(self.rect(), radius, radius)

        knob = self.height() - 6
        x = self.width() - knob - 3 if self._on else 3
        p.setBrush(QColor("#FFFFFF" if self._on else theme.TEXT_MUTED))
        p.drawEllipse(x, 3, knob, knob)
        p.end()


def _rgb(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return f"{r},{g},{b}"


def today_text() -> str:
    now = datetime.now()
    return now.strftime("%m 月 %d 日")
