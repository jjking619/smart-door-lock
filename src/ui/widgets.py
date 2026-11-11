"""可复用的界面零件。

约定：所有"卡片"都用 QFrame#Card + 竖排布局，标题用 CardTitle（小号、灰、
带字距）。这样整屏看过去层级一致，不会出现哪里粗哪里细。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
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
            "border-radius:11px;padding:2px 10px 2px 9px;font-size:14px;"
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
        self._unit.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:14px;")

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

    def __init__(self, event, parent=None, thumbs: bool = True) -> None:
        super().__init__(parent)
        # 绝对不能叫 self.event：那会覆盖 QWidget.event()，Qt 分发事件时
        # 会去调用这个 dict，直接段错误。
        self.event_data = event
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 12, 8)
        lay.setSpacing(12)

        self.thumb = Thumb() if thumbs else None
        if self.thumb is not None:
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
        sub.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:14px;")

        text.addLayout(title_row)
        text.addWidget(sub)

        lay.addLayout(text, 1)

        when = QVBoxLayout()
        when.setSpacing(1)
        t = QLabel(event.time_text)
        t.setAlignment(Qt.AlignmentFlag.AlignRight)
        t.setStyleSheet(f"color:{theme.TEXT};font-size:15px;font-weight:600;")
        d = QLabel(event.date_text)
        d.setAlignment(Qt.AlignmentFlag.AlignRight)
        d.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:15px;")
        when.addWidget(t)
        when.addWidget(d)
        lay.addLayout(when)


class EventList(QListWidget):
    selected = Signal(object)

    def __init__(self, parent=None, thumbs: bool = True) -> None:
        super().__init__(parent)
        self._thumbs = thumbs
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
            row = EventRow(ev, thumbs=self._thumbs)
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


# ---------------------------------------------------------------------------
# 矢量图标：板子上没有彩色 emoji 字体，统一用 QPainter 画线性图标，
# 在 24×24 的坐标系里定义，按需缩放。
# ---------------------------------------------------------------------------

def paint_icon(p: QPainter, name: str, rect: QRectF, color: str, width: float = 1.8) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = min(rect.width(), rect.height()) / 24.0
    p.translate(rect.center().x() - 12 * s, rect.center().y() - 12 * s)
    p.scale(s, s)
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if name in ("lock", "unlock"):
        p.drawRoundedRect(QRectF(5, 11, 14, 10), 2.5, 2.5)
        path = QPainterPath()
        if name == "lock":
            path.moveTo(8, 11)
            path.lineTo(8, 7.5)
            path.arcTo(QRectF(8, 3.5, 8, 8), 180, -180)
            path.lineTo(16, 11)
        else:
            path.moveTo(8, 11)
            path.lineTo(8, 7.5)
            path.arcTo(QRectF(8, 3.5, 8, 8), 180, -150)
        p.drawPath(path)
        p.drawLine(QPointF(12, 15), QPointF(12, 17))
    elif name == "finger":
        for r in (3.0, 6.0, 9.0):
            p.drawArc(QRectF(12 - r, 13 - r, 2 * r, 2 * r), 20 * 16, 140 * 16)
        p.drawArc(QRectF(9, 10, 6, 6), 200 * 16, 140 * 16)
        p.drawLine(QPointF(12, 13), QPointF(12, 20))
        p.drawArc(QRectF(6, 7, 12, 12), 200 * 16, 60 * 16)
        p.drawArc(QRectF(6, 7, 12, 12), 280 * 16, 60 * 16)
    elif name == "video":
        p.drawRoundedRect(QRectF(2.5, 6, 13, 12), 2.5, 2.5)
        tri = QPainterPath()
        tri.moveTo(15.5, 10.5)
        tri.lineTo(21.5, 7.5)
        tri.lineTo(21.5, 16.5)
        tri.lineTo(15.5, 13.5)
        p.drawPath(tri)
    elif name == "shield":
        path = QPainterPath()
        path.moveTo(12, 2.5)
        path.lineTo(20, 5.5)
        path.lineTo(20, 11)
        path.cubicTo(20, 16, 16.5, 19.5, 12, 21.5)
        path.cubicTo(7.5, 19.5, 4, 16, 4, 11)
        path.lineTo(4, 5.5)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(12, 8), QPointF(12, 13))
        p.drawPoint(QPointF(12, 16.3))
    elif name == "box":
        path = QPainterPath()
        path.moveTo(12, 2.8)
        path.lineTo(20.5, 7)
        path.lineTo(20.5, 17)
        path.lineTo(12, 21.2)
        path.lineTo(3.5, 17)
        path.lineTo(3.5, 7)
        path.closeSubpath()
        p.drawPath(path)
        p.drawPolyline([QPointF(3.5, 7), QPointF(12, 11.2), QPointF(20.5, 7)])
        p.drawLine(QPointF(12, 11.2), QPointF(12, 21.2))
        p.drawLine(QPointF(7.8, 4.9), QPointF(16.2, 9.1))
    elif name == "mic":
        p.drawRoundedRect(QRectF(9, 2.5, 6, 11.5), 3, 3)
        p.drawArc(QRectF(5.5, 5, 13, 13), 180 * 16, 180 * 16)
        p.drawLine(QPointF(12, 18), QPointF(12, 21.5))
        p.drawLine(QPointF(8.5, 21.5), QPointF(15.5, 21.5))
    elif name == "rec":
        p.drawEllipse(QRectF(3, 3, 18, 18))
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(8, 8, 8, 8))
    elif name == "stop":
        p.drawEllipse(QRectF(3, 3, 18, 18))
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(8.5, 8.5, 7, 7), 1.2, 1.2)
    elif name == "camera":
        p.drawRoundedRect(QRectF(2.5, 7, 19, 13), 2.5, 2.5)
        p.drawPolyline([QPointF(8, 7), QPointF(9.5, 4), QPointF(14.5, 4), QPointF(16, 7)])
        p.drawEllipse(QRectF(8.5, 9.5, 7, 7))
    elif name == "home":
        p.drawPolyline([QPointF(3, 11), QPointF(12, 3.5), QPointF(21, 11)])
        p.drawPolyline([QPointF(5.5, 9.5), QPointF(5.5, 20.5), QPointF(18.5, 20.5), QPointF(18.5, 9.5)])
        p.drawRoundedRect(QRectF(10, 14, 4, 6.5), 1, 1)
    elif name == "list":
        for y in (6, 12, 18):
            p.drawPoint(QPointF(4.5, y))
            p.drawLine(QPointF(8.5, y), QPointF(20, y))
    elif name == "gear":
        p.drawEllipse(QRectF(9, 9, 6, 6))
        import math
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(QPointF(12 + 6.2 * math.cos(a), 12 + 6.2 * math.sin(a)),
                       QPointF(12 + 9 * math.cos(a), 12 + 9 * math.sin(a)))
        p.drawEllipse(QRectF(5.8, 5.8, 12.4, 12.4))
    p.restore()


def icon_pixmap(name: str, size: int, color: str, width: float = 1.8) -> QPixmap:
    ratio = 2.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(ratio)
    p = QPainter(pm)
    paint_icon(p, name, QRectF(0, 0, size, size), color, width)
    p.end()
    return pm


def make_icon(name: str, color: str, checked_color: str | None = None, size: int = 28) -> QIcon:
    icon = QIcon()
    icon.addPixmap(icon_pixmap(name, size, color), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(icon_pixmap(name, size, color), QIcon.Mode.Disabled, QIcon.State.Off)
    icon.addPixmap(icon_pixmap(name, size, checked_color or color), QIcon.Mode.Normal, QIcon.State.On)
    return icon


class IconBadge(QWidget):
    """圆角底色 + 图标：功能磁贴的"脸"。"""

    def __init__(self, name: str, color: str, size: int = 44, parent=None) -> None:
        super().__init__(parent)
        self._name = name
        self._color = color
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(self._color)
        bg.setAlpha(38)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        r = QRectF(self.rect())
        p.drawRoundedRect(r, r.width() * 0.3, r.height() * 0.3)
        inset = r.width() * 0.22
        paint_icon(p, self._name, r.adjusted(inset, inset, -inset, -inset), self._color, 2.0)
        p.end()


class FeatureTile(QFrame):
    """功能磁贴：图标 + 功能名 + 一行醒目状态 + 一行说明。

    新事件到达时调用 flash()：边框变成功能色并持续几秒，展会现场
    观众一眼就能看到"刚才识别到了什么"。
    """

    clicked = Signal()

    def __init__(self, icon: str, title: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color = color
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._unflash)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.badge = IconBadge(icon, color, 38)
        top.addWidget(self.badge)
        self.title = QLabel(title)
        self.title.setObjectName("TileTitle")
        top.addWidget(self.title, 1)
        self.tag = QLabel("")
        self.tag.hide()
        top.addWidget(self.tag, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(top)

        self.headline = QLabel("—")
        self.headline.setStyleSheet(f"font-size:19px;font-weight:700;color:{theme.TEXT};")
        lay.addWidget(self.headline)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:14px;")
        lay.addWidget(self.detail)
        lay.addStretch(1)

    def set_state(self, headline: str, color: str | None = None, detail: str | None = None) -> None:
        self.headline.setText(headline)
        self.headline.setStyleSheet(
            f"font-size:19px;font-weight:700;color:{color or theme.TEXT};"
        )
        if detail is not None:
            self.detail.setText(detail)

    def set_tag(self, text: str, color: str | None = None) -> None:
        if not text:
            self.tag.hide()
            return
        c = color or self._color
        self.tag.setText(text)
        self.tag.setStyleSheet(
            f"color:{c};background:rgba({_rgb(c)},0.14);border-radius:9px;"
            "padding:1px 8px;font-size:15px;font-weight:600;"
        )
        self.tag.show()

    def flash(self, ms: int = 8000) -> None:
        self.setStyleSheet(
            f"QFrame#Tile {{ border:2px solid {self._color}; "
            f"background:rgba({_rgb(self._color)},0.10); }}"
        )
        self._flash_timer.start(ms)

    def is_flashing(self) -> bool:
        return self._flash_timer.isActive()

    def _unflash(self) -> None:
        self.setStyleSheet("")

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        super().mouseReleaseEvent(ev)
        if self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()


def _rgb(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return f"{r},{g},{b}"


def today_text() -> str:
    now = datetime.now()
    return now.strftime("%m 月 %d 日")
