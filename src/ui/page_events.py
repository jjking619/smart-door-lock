"""事件页：筛选 + 列表 + 详情预览。

设计取舍
--------
* **筛选做成一条横向 pill**，不用下拉框：触摸屏上少一次点击。
* **详情不放右侧第三栏**，而是"选中即在列表下方展开" —— 1080p 竖屏
  (比如 1024×600) 上三栏会把视频区挤成邮票。
* 详情里直接给"回放"按钮，跳转到录像页播放该片段，而不是弹新窗口。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from . import theme
from .api import CATEGORIES, Event
from .widgets import EventList, Thumb, card, card_title, hline

PAGE_SIZE = 60


class DetailPanel(QFrame):
    """选中事件后的详情条；默认隐藏，避免无事件时空占位置。"""

    replay = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._event: Event | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(14)

        self.thumb = Thumb(size=QSize_(160, 90))
        lay.addWidget(self.thumb)

        info = QVBoxLayout()
        info.setSpacing(4)
        self.title = QLabel("")
        self.title.setStyleSheet(f"font-size:16px;font-weight:600;color:{theme.TEXT};")
        self.meta = QLabel("")
        self.meta.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:12px;")
        self.meta.setWordWrap(True)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.title)
        row.addStretch(1)
        info.addLayout(row)
        info.addWidget(self.meta)
        info.addStretch(1)
        lay.addLayout(info, 1)

        self.btn_replay = QPushButton("回放片段")
        self.btn_replay.setObjectName("Ghost")
        self.btn_replay.setFixedHeight(40)
        self.btn_replay.setMinimumWidth(110)
        self.btn_replay.clicked.connect(self._on_replay)
        lay.addWidget(self.btn_replay, 0, Qt.AlignmentFlag.AlignVCenter)

        self.hide()

    def show_event(self, event: Event) -> None:
        self._event = event
        self.title.setText(event.title)
        self.title.setStyleSheet(
            f"font-size:16px;font-weight:600;color:{event.color};"
        )
        rows = "　·　".join(f"{k} {v}" for k, v in event.detail_rows())
        self.meta.setText(f"{event.ts.strftime('%Y-%m-%d %H:%M:%S') if event.ts else '--'}"
                          f"　·　{rows}")
        self.thumb._placeholder(0)
        self.btn_replay.setEnabled(bool(event.clip))
        self.btn_replay.setText("回放片段" if event.clip else "无录像")
        self.show()

    def set_thumb(self, data: bytes) -> None:
        self.thumb.set_image(data)

    def _on_replay(self) -> None:
        if self._event is not None:
            self.replay.emit(self._event)

    def current(self) -> Event | None:
        return self._event


class EventsPage(QWidget):
    replay_requested = Signal(object)

    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._all: list[Event] = []
        self._filter = "all"
        self._unread = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(12)
        title = QLabel("事件")
        title.setObjectName("PageTitle")
        self.sub = QLabel("正在载入…")
        self.sub.setObjectName("PageSub")
        head.addWidget(title)
        head.addWidget(self.sub)
        head.addStretch(1)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setObjectName("Small")
        self.btn_refresh.clicked.connect(self.refresh)
        head.addWidget(self.btn_refresh, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(head)

        # ---- 筛选 pill ----
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._chips: dict[str, QPushButton] = {}
        for key, label in CATEGORIES:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setObjectName("Small")
            btn.setMinimumHeight(34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setChecked(key == "all")
            btn.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self._group.addButton(btn)
            self._chips[key] = btn
            chips.addWidget(btn)

        chips.addStretch(1)
        self.btn_clear_unread = QPushButton("标记全部已读")
        self.btn_clear_unread.setObjectName("Small")
        self.btn_clear_unread.clicked.connect(self._clear_unread)
        chips.addWidget(self.btn_clear_unread)
        root.addLayout(chips)

        # ---- 列表 ----
        self.list = EventList()
        self.list.selected.connect(self._on_select)
        root.addWidget(self.list, 1)

        # ---- 详情 ----
        self.detail = DetailPanel()
        self.detail.replay.connect(self.replay_requested.emit)
        root.addWidget(self.detail)

        self._empty = QLabel("暂无事件")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:14px;")
        self._empty.hide()
        root.addWidget(self._empty)

    # ---- 数据 ----
    def refresh(self) -> None:
        self.sub.setText("正在载入…")
        self._api.events(
            PAGE_SIZE,
            on_ok=self._on_events,
            on_err=lambda msg: self.sub.setText(f"读取失败：{msg}"),
        )

    def _on_events(self, events: list[Event]) -> None:
        self._all = events
        self.sub.setText(f"共 {len(events)} 条 · 最近 {len(events)} 次记录")
        self._apply_filter()

    def _set_filter(self, key: str) -> None:
        self._filter = key
        self._apply_filter()

    def _apply_filter(self) -> None:
        if self._filter == "all":
            shown = self._all
        else:
            shown = [e for e in self._all if e.category == self._filter]

        self.list.set_events(shown)
        self._empty.setVisible(not shown)
        self.list.setVisible(bool(shown))
        if not shown:
            self._empty.setText({
                "intrusion": "暂无入侵事件",
                "package": "暂无快递事件",
                "access": "暂无出入记录",
                "system": "暂无系统事件",
            }.get(self._filter, "暂无事件"))
        for key, btn in self._chips.items():
            count = len(self._all) if key == "all" else sum(
                1 for e in self._all if e.category == key
            )
            label = dict(CATEGORIES)[key]
            btn.setText(f"{label} {count}" if count else label)

    def _on_select(self, event: Event) -> None:
        self.detail.show_event(event)
        if event.snapshot:
            self._api.get_bytes(
                event.snapshot,
                on_ok=self.detail.set_thumb,
                on_err=lambda _msg: None,
            )
        else:
            self.detail.thumb._placeholder(0)

    def _clear_unread(self) -> None:
        self._unread = 0
        self._update_unread_hint()

    def _update_unread_hint(self) -> None:
        self.btn_clear_unread.setVisible(self._unread > 0)
        if self._unread:
            self.btn_clear_unread.setText(f"标记全部已读 ({self._unread})")

    def new_event(self, event: Event) -> None:
        """收到新事件：插到列表头部，不重建整个列表（避免滚动位置丢失）。"""
        self._all.insert(0, event)
        self._unread += 1
        self._apply_filter()
        self._update_unread_hint()


def QSize_(w: int, h: int):  # noqa: N802
    from PySide6.QtCore import QSize

    return QSize(w, h)
