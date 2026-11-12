"""记录中心页：统一查看事件与录像。

方案二落地：把原来的“事件”和“回放”聚合到一个入口，
用页内标签切换，避免用户在导航里猜“录像在哪看”。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
import logging
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QEvent, QRect, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QScrollArea,
    QPushButton, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QSizePolicy, QVBoxLayout, QWidget,
)

from . import theme
from .api import CATEGORIES, Event, Recording
from .widgets import card, card_title

log = logging.getLogger(__name__)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget

    HAVE_MULTIMEDIA = True
except ImportError:
    HAVE_MULTIMEDIA = False


def _draw_text(painter, rect: QRect, text: str, color: str, size: int,
               *, bold: bool = False, align=Qt.AlignmentFlag.AlignLeft) -> None:
    font = QFont(painter.font())
    font.setPixelSize(size)
    font.setBold(bold)
    painter.setFont(font)
    painter.setPen(QColor(color))
    text = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(0, rect.width()))
    painter.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, text)


class _ListDelegate(QStyledItemDelegate):
    def __init__(self, page, recordings: bool, parent=None) -> None:
        super().__init__(parent)
        self._page = page
        self._recordings = recordings

    def paint(self, painter, option, index) -> None:
        payload = index.data(Qt.ItemDataRole.UserRole)
        if payload is None:
            return
        painter.save()
        painter.setClipRect(option.rect)
        background = QStyleOptionViewItem(option)
        background.text = ""
        option.widget.style().drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem,
                                            background, painter, option.widget)
        r = option.rect
        if isinstance(payload, dict) and payload["type"] == "group":
            day = payload["day"]
            collapsed = (self._page._collapsed_days if self._recordings
                         else self._page._collapsed_event_days)
            arrow = "▸" if day in collapsed else "▾"
            label = day if self._recordings else _event_day_label(day)
            _draw_text(painter, r.adjusted(8, 0, -55, 0), f"{arrow}  {label}", theme.TEXT_DIM, 14, bold=True)
            _draw_text(painter, QRect(r.right() - 52, r.top(), 48, r.height()),
                       f"{payload['count']} 条", theme.TEXT_MUTED, 13, align=Qt.AlignmentFlag.AlignRight)
        elif self._recordings:
            rec = payload["record"]
            if rec.kind == "continuous":
                x = r.left() + 14
            else:
                thumb = QRect(r.left() + 6, r.top() + 6, 78, 44)
                pixmap = self._page._thumb_cache.get(rec.snapshot)
                if pixmap:
                    painter.drawPixmap(thumb, pixmap)
                else:
                    painter.fillRect(thumb, QColor("#2A2438"))
                    _draw_text(painter, thumb, "无图", theme.TEXT_MUTED, 14,
                               align=Qt.AlignmentFlag.AlignHCenter)
                x = thumb.right() + 11
            width = max(0, r.right() - x - 5)
            _draw_text(painter, QRect(x, r.top() + 5, width, 24), rec.title, rec.color, 15, bold=True)
            meta = f"{rec.kind_text} · {rec.date_text} {rec.time_text} · {_human_size(rec.size)}"
            _draw_text(painter, QRect(x, r.top() + 30, width, 22), meta, theme.TEXT_MUTED, 13)
        else:
            event = payload
            timestamp = event.ts.astimezone() if event.ts else None
            x = r.left() + 14
            width = max(0, r.right() - x - 80)
            _draw_text(painter, QRect(x, r.top() + 10, 14, 22), "●", event.color, 11)
            _draw_text(painter, QRect(x + 16, r.top() + 10, width - 16, 24),
                       event.title, theme.TEXT, 16, bold=True)
            _draw_text(painter, QRect(x, r.top() + 38, width, 22), event.summary(), theme.TEXT_DIM, 14)
            _draw_text(painter, QRect(r.right() - 76, r.top() + 10, 70, 24),
                       timestamp.strftime("%H:%M:%S") if timestamp else "--:--:--",
                       theme.TEXT, 15, bold=True, align=Qt.AlignmentFlag.AlignRight)
            _draw_text(painter, QRect(r.right() - 76, r.top() + 38, 70, 22),
                       timestamp.strftime("%m-%d") if timestamp else "--",
                       theme.TEXT_MUTED, 13, align=Qt.AlignmentFlag.AlignRight)
        painter.restore()


class RecordsPage(QWidget):
    replay_requested = Signal(object)

    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._events: list[Event] = []
        self._records: list[Recording] = []
        self._tab = "events"
        self._recording_kind = "all"
        self._event_category = "all"
        self._collapsed_days: set[str] = set()
        self._collapsed_event_days: set[str] = set()
        self._current_event: Event | None = None
        self._selection_generation = 0
        self._snapshot_pixmap: QPixmap | None = None
        self._current_record: Recording | None = None
        self._current_clip_name = "recording.mp4"
        self._loading = {"events": False, "recordings": False}
        self._loaded = {"events": False, "recordings": False}
        self._errors: dict[str, str] = {}
        self._generation = {"events": 0, "recordings": 0}
        self._dirty = {"events": False, "recordings": False}
        self._active = True
        self._auto_select_pending = True
        self._has_selection = False
        self._awaiting_video = False
        self._playback_retries = 0
        self._selection_title = "详情"
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(2000)
        self._retry_timer.timeout.connect(self._retry_playback)
        self._thumb_cache: dict[str, QPixmap] = {}
        self._thumb_pending: set[str] = set()
        self._thumb_failed: set[str] = set()
        self._source_generation = 0
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(30)
        self._thumb_timer.timeout.connect(self._load_visible_thumbnails)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("记录")
        title.setObjectName("PageTitle")
        self.sub = QLabel("正在载入…")
        self.sub.setObjectName("PageSub")
        head.addWidget(title)
        head.addWidget(self.sub)
        head.addStretch(1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setObjectName("Small")
        self.btn_refresh.clicked.connect(self.refresh)
        head.addWidget(self.btn_refresh)
        root.addLayout(head)

        tab = QHBoxLayout()
        tab.setSpacing(8)
        self.btn_events = QPushButton("事件")
        self.btn_events.setObjectName("Small")
        self.btn_events.setCheckable(True)
        self.btn_events.setChecked(True)
        self.btn_events.clicked.connect(lambda: self._set_tab("events"))
        tab.addWidget(self.btn_events)
        self.btn_recordings = QPushButton("录像")
        self.btn_recordings.setObjectName("Small")
        self.btn_recordings.setCheckable(True)
        self.btn_recordings.clicked.connect(lambda: self._set_tab("recordings"))
        tab.addWidget(self.btn_recordings)
        tab.addStretch(1)
        root.addLayout(tab)

        self.event_filter_row = QWidget()
        event_filters = QHBoxLayout(self.event_filter_row)
        event_filters.setContentsMargins(0, 0, 0, 0)
        event_filters.setSpacing(8)
        self._event_filter_group = QButtonGroup(self)
        self._event_chips: dict[str, QPushButton] = {}
        for key, label in CATEGORIES:
            button = QPushButton(label)
            button.setObjectName("Small")
            button.setCheckable(True)
            button.setChecked(key == "all")
            button.setMinimumHeight(40)
            button.clicked.connect(lambda _checked=False, k=key: self._set_event_category(k))
            self._event_filter_group.addButton(button)
            self._event_chips[key] = button
            event_filters.addWidget(button)
        event_filters.addStretch(1)
        root.addWidget(self.event_filter_row)

        body = QHBoxLayout()
        body.setSpacing(16)

        left_card, left_lay = card(spacing=8)
        left_card.setFixedWidth(350)
        self.left_title = QLabel("事件")
        self.left_title.setObjectName("CardTitle")
        left_lay.addWidget(self.left_title)

        self.record_filter_row = QWidget()
        filter_lay = QHBoxLayout(self.record_filter_row)
        filter_lay.setContentsMargins(0, 0, 0, 0)
        filter_lay.setSpacing(8)

        self.btn_filter_all = QPushButton("全部")
        self.btn_filter_all.setObjectName("Small")
        self.btn_filter_all.setCheckable(True)
        self.btn_filter_all.setChecked(True)
        self.btn_filter_all.clicked.connect(lambda: self._set_recording_kind("all"))
        filter_lay.addWidget(self.btn_filter_all)

        self.btn_filter_event = QPushButton("事件片段")
        self.btn_filter_event.setObjectName("Small")
        self.btn_filter_event.setCheckable(True)
        self.btn_filter_event.clicked.connect(lambda: self._set_recording_kind("event"))
        filter_lay.addWidget(self.btn_filter_event)

        self.btn_filter_cont = QPushButton("在线录像")
        self.btn_filter_cont.setObjectName("Small")
        self.btn_filter_cont.setCheckable(True)
        self.btn_filter_cont.clicked.connect(lambda: self._set_recording_kind("continuous"))
        filter_lay.addWidget(self.btn_filter_cont)
        filter_lay.addStretch(1)
        self.record_filter_row.hide()
        left_lay.addWidget(self.record_filter_row)

        self.events_list = QListWidget()
        self.events_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.events_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.events_list.setItemDelegate(_ListDelegate(self, False, self.events_list))
        self.events_list.itemClicked.connect(self._on_events_item_clicked)
        left_lay.addWidget(self.events_list, 1)

        self.recordings_list = QListWidget()
        self.recordings_list.setItemDelegate(_ListDelegate(self, True, self.recordings_list))
        self.recordings_list.setSpacing(2)
        self.recordings_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.recordings_list.itemClicked.connect(self._on_recordings_item_clicked)
        self.recordings_list.verticalScrollBar().valueChanged.connect(self._schedule_thumbnails)
        self.recordings_list.verticalScrollBar().rangeChanged.connect(self._schedule_thumbnails)
        self.recordings_list.hide()
        left_lay.addWidget(self.recordings_list, 1)

        self.empty = QLabel("暂无数据")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:15px;")
        left_lay.addWidget(self.empty)
        body.addWidget(left_card)

        right = QVBoxLayout()
        right.setSpacing(12)

        op = QHBoxLayout()
        self.play_title = QLabel("详情")
        self.play_title.setObjectName("PageSub")
        self.play_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        op.addWidget(self.play_title, 1)

        self.btn_event_details = QPushButton("查看详情")
        self.btn_event_details.setObjectName("Small")
        self.btn_event_details.setCheckable(True)
        self.btn_event_details.setStyleSheet("min-height:40px;")
        self.btn_event_details.clicked.connect(self._toggle_event_details)
        self.btn_event_details.hide()
        op.addWidget(self.btn_event_details)

        self.btn_play = QPushButton("▶  播放")
        self.btn_play.setObjectName("Small")
        self.btn_play.setStyleSheet("min-height:40px;")
        self.btn_play.setEnabled(False)
        self.btn_play.clicked.connect(self._toggle)
        op.addWidget(self.btn_play)

        self.btn_open = QPushButton("用系统播放器打开")
        self.btn_open.setObjectName("Small")
        self.btn_open.setStyleSheet("min-height:40px;")
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._open_external)
        op.addWidget(self.btn_open)
        right.addLayout(op)

        self.media_error = QLabel()
        self.media_error.setTextFormat(Qt.TextFormat.PlainText)
        self.media_error.setWordWrap(True)
        self.media_error.setStyleSheet(f"color:{theme.CRIT};font-size:14px;")
        self.media_error.hide()
        right.addWidget(self.media_error)

        self._right_layout = right
        self.event_details, details_frame = card(spacing=0, margins=(14, 12, 14, 12))
        self._event_details_scroll = QScrollArea()
        self._event_details_scroll.setWidgetResizable(True)
        self._event_details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._event_details_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        details_content = QWidget()
        details_layout = QVBoxLayout(details_content)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(8)
        self.event_title = QLabel()
        self.event_title.setWordWrap(True)
        self.event_title.setTextFormat(Qt.TextFormat.PlainText)
        self.event_title.setStyleSheet("font-size:20px;font-weight:600;")
        self.event_meta = QLabel()
        self.event_summary = QLabel()
        self.event_fields = QLabel()
        self.event_media_hint = QLabel("此事件没有关联图片或录像")
        for label in (self.event_meta, self.event_summary, self.event_fields, self.event_media_hint):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:14px;")
        details_layout.addWidget(self.event_title)
        details_layout.addWidget(self.event_meta)
        details_layout.addWidget(self.event_summary)
        details_layout.addWidget(self.event_fields)
        details_layout.addWidget(self.event_media_hint)
        details_layout.addStretch(1)
        self._event_details_scroll.setWidget(details_content)
        details_frame.addWidget(self._event_details_scroll)
        self.event_details.hide()
        right.addWidget(self.event_details)

        self.video_stack = QStackedWidget()
        self.video_placeholder = QLabel("请选择记录")
        self.video_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_placeholder.setWordWrap(True)
        self.video_placeholder.setTextFormat(Qt.TextFormat.PlainText)
        self.video_placeholder.setStyleSheet(
            f"color:{theme.TEXT_DIM};background:#05070A;border-radius:10px;padding:24px;"
        )
        self.video_stack.addWidget(self.video_placeholder)
        self.event_snapshot = QLabel()
        self.event_snapshot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.event_snapshot.setMinimumSize(1, 1)
        self.event_snapshot.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.event_snapshot.installEventFilter(self)
        self.event_snapshot.setStyleSheet("background:#05070A;border-radius:10px;")
        self.video_stack.addWidget(self.event_snapshot)
        right.addWidget(self.video_stack, 1)

        if HAVE_MULTIMEDIA:
            self.video = QVideoWidget()
            self.video.setStyleSheet("background:#05070A;border-radius:10px;")
            self.video_stack.addWidget(self.video)
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.errorOccurred.connect(self._on_player_error)
            self.video.videoSink().videoFrameChanged.connect(self._on_video_frame)
        else:
            self.player = None

        body.addLayout(right, 1)
        root.addLayout(body, 1)

        self._external_url: str | None = None

    def refresh(self) -> None:
        tab = self._tab
        if self._loading[tab]:
            return
        if not self._has_selection:
            self._auto_select_pending = True
        if tab == "recordings":
            self._source_generation += 1
            self._thumb_cache.clear()
            self._thumb_pending.clear()
            self._thumb_failed.clear()
        self._loading[tab] = True
        self._errors.pop(tab, None)
        self._generation[tab] += 1
        generation = self._generation[tab]
        self._update_subtitle()
        self._update_empty()

        def success(items) -> None:
            if generation != self._generation[tab]:
                return
            self._loading[tab] = False
            self._loaded[tab] = True
            if tab == "events":
                self._on_events(items)
            else:
                self._on_recordings(items)

        def failure(message: str) -> None:
            if generation != self._generation[tab]:
                return
            self._loading[tab] = False
            self._errors[tab] = message
            if self._tab == tab:
                self._update_subtitle()
                self._update_empty()
                if self._auto_select_pending:
                    self._auto_select_pending = False
                    self._reset_player("记录读取失败，请刷新重试")

        if tab == "events":
            self._api.events(1000, on_ok=success, on_err=failure)
        else:
            self._api.recordings(300, "all", on_ok=success, on_err=failure)

    def invalidate(self) -> None:
        self._source_generation += 1
        for tab in self._generation:
            self._generation[tab] += 1
            self._loading[tab] = False
            self._loaded[tab] = False
        self._errors.clear()
        self._thumb_cache.clear()
        self._thumb_pending.clear()
        self._thumb_failed.clear()
        self._thumb_timer.stop()
        self._events = []
        self._records = []
        self._collapsed_event_days.clear()
        self._current_event = None
        self._dirty = {"events": False, "recordings": False}
        self.events_list.clear()
        self.recordings_list.clear()
        self._reset_player("请选择记录")
        self._current_record = None
        self.play_title.setText("详情")
        self._auto_select_pending = True
        self._has_selection = False
        self._sync_record_filter_labels()
        self._sync_event_filter_labels()
        self._update_subtitle()
        self._update_empty()

    def pause_loading(self) -> None:
        self._active = False
        self._thumb_timer.stop()
        self._retry_timer.stop()

    def resume_loading(self) -> None:
        self._active = True
        self._auto_select_pending = True
        self._has_selection = False
        self.play_title.setText("详情")
        self._reset_player("正在载入记录…")
        self._render_current()
        self._schedule_thumbnails()

    def _on_events(self, events: list[Event]) -> None:
        self._events = sorted(
            events, key=lambda event: event.ts.timestamp() if event.ts else float("-inf"),
            reverse=True,
        )
        self._sync_event_filter_labels()
        self._dirty["events"] = True
        if self._tab == "events":
            if self._current_event not in self._filtered_events():
                self._current_event = None
                self._has_selection = False
                self._auto_select_pending = True
            self._update_subtitle()
            self._update_empty()
            self._render_current()
            self._select_default()

    def _on_recordings(self, records: list[Recording]) -> None:
        self._records = records
        self._thumb_failed.clear()
        self._sync_record_filter_labels()
        self._dirty["recordings"] = True
        if self._tab == "recordings":
            self._update_subtitle()
            self._update_empty()
            self._render_current()
            self._select_default()

    def _set_tab(self, tab: str) -> None:
        if tab == self._tab:
            return
        self._tab = tab
        self._current_event = None
        self._auto_select_pending = True
        self._has_selection = False
        self.play_title.setText("详情")
        self._reset_player("正在载入记录…")
        self.btn_events.setChecked(tab == "events")
        self.btn_recordings.setChecked(tab == "recordings")
        self.events_list.setVisible(tab == "events")
        self.recordings_list.setVisible(tab == "recordings")
        self.record_filter_row.setVisible(tab == "recordings")
        self.event_filter_row.setVisible(tab == "events")
        self.left_title.setText("事件" if tab == "events" else "录像")
        self._update_subtitle()
        self._update_empty()
        if not self._loaded[tab] and not self._loading[tab]:
            self.refresh()
        else:
            self._render_current()
            self._schedule_thumbnails()
            if self._loaded[tab]:
                self._select_default()

    def _set_event_category(self, category: str) -> None:
        if category == self._event_category:
            return
        self._event_category = category
        self._event_chips[category].setChecked(True)
        self._current_event = None
        self._has_selection = False
        self._auto_select_pending = True
        self.play_title.setText("详情")
        self._reset_player("正在载入记录…" if not self._loaded["events"]
                           else self._event_empty_message())
        self._dirty["events"] = True
        self._render_current()
        self._update_subtitle()
        self._update_empty()
        self._select_default()

    def _sync_event_filter_labels(self) -> None:
        for key, label in CATEGORIES:
            count = len(self._events) if key == "all" else sum(
                event.category == key for event in self._events
            )
            self._event_chips[key].setText(f"{label} {count}")

    def _filtered_events(self) -> list[Event]:
        if self._event_category == "all":
            return self._events
        return [event for event in self._events if event.category == self._event_category]

    def _set_recording_kind(self, kind: str) -> None:
        if kind not in ("all", "event", "continuous"):
            return
        self._recording_kind = kind
        self.btn_filter_all.setChecked(kind == "all")
        self.btn_filter_event.setChecked(kind == "event")
        self.btn_filter_cont.setChecked(kind == "continuous")
        self._rebuild_recordings_list()
        if self._tab == "recordings":
            self._update_subtitle()
            self._update_empty()

    def _sync_record_filter_labels(self) -> None:
        event_count = sum(1 for item in self._records if item.kind == "event")
        cont_count = sum(1 for item in self._records if item.kind == "continuous")
        all_count = len(self._records)
        self.btn_filter_all.setText(f"全部 {all_count}" if all_count else "全部")
        self.btn_filter_event.setText(f"事件片段 {event_count}" if event_count else "事件片段")
        self.btn_filter_cont.setText(f"在线录像 {cont_count}" if cont_count else "在线录像")

    def _filtered_records(self) -> list[Recording]:
        if self._recording_kind == "all":
            return self._records
        return [item for item in self._records if item.kind == self._recording_kind]

    def _rebuild_recordings_list(self) -> None:
        self._dirty["recordings"] = True
        if self._tab == "recordings":
            self._render_current()

    def _render_current(self) -> None:
        if not self._active or not self._dirty[self._tab]:
            return
        self._dirty[self._tab] = False
        if self._tab == "events":
            self.events_list.clear()
            groups: OrderedDict[str, list[Event]] = OrderedDict()
            for event in self._filtered_events():
                day = _event_day(event)
                groups.setdefault(day, []).append(event)
            for day, events in groups.items():
                header = QListWidgetItem()
                header.setData(Qt.ItemDataRole.UserRole, {
                    "type": "group", "day": day, "count": len(events),
                })
                header.setSizeHint(QSize(0, 40))
                self.events_list.addItem(header)
                if day in self._collapsed_event_days:
                    continue
                for event in events:
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, event)
                    item.setSizeHint(QSize(0, 70))
                    self.events_list.addItem(item)
                    if event == self._current_event:
                        self.events_list.setCurrentItem(item)
            return
        records = self._filtered_records()
        groups: OrderedDict[str, list[Recording]] = OrderedDict()
        for rec in records:
            day = _group_day(rec)
            groups.setdefault(day, []).append(rec)

        self.recordings_list.clear()
        for day, items in groups.items():
            header = QListWidgetItem()
            header.setData(Qt.ItemDataRole.UserRole, {"type": "group", "day": day, "count": len(items)})
            header.setSizeHint(QSize(0, 34))
            self.recordings_list.addItem(header)
            if day in self._collapsed_days:
                continue
            for rec in items:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, {"type": "record", "day": day, "record": rec})
                item.setSizeHint(QSize(0, 56))
                self.recordings_list.addItem(item)
        self._schedule_thumbnails()

    def _schedule_thumbnails(self, *_args) -> None:
        if self._active and self._tab == "recordings" and not self._thumb_timer.isActive():
            self._thumb_timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._schedule_thumbnails()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._schedule_thumbnails()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.event_snapshot and event.type() == QEvent.Type.Resize:
            self._resize_event_snapshot()
        return super().eventFilter(watched, event)

    def _load_visible_thumbnails(self) -> None:
        if self._tab != "recordings" or not self.recordings_list.isVisible():
            return
        viewport = self.recordings_list.viewport().rect()
        for index in range(self.recordings_list.count()):
            item = self.recordings_list.item(index)
            if not self.recordings_list.visualItemRect(item).intersects(viewport):
                continue
            payload = item.data(Qt.ItemDataRole.UserRole)
            if payload["type"] != "record":
                continue
            record = payload["record"]
            if record.kind == "continuous":
                continue
            path = record.snapshot
            if not path:
                continue
            if path in self._thumb_cache:
                continue
            elif path not in self._thumb_pending and path not in self._thumb_failed:
                if len(self._thumb_pending) >= 6:
                    break
                self._thumb_pending.add(path)
                generation = self._source_generation
                self._api.get_bytes(
                    path,
                    on_ok=lambda data, p=path, g=generation: self._thumbnail_done(p, g, data),
                    on_err=lambda message, p=path, g=generation: self._thumbnail_failed(p, g, message),
                )

    def _thumbnail_done(self, path: str, generation: int, data: bytes) -> None:
        if generation != self._source_generation:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self._thumbnail_failed(path, generation, "图片格式无效")
            return
        self._thumb_pending.discard(path)
        self._thumb_cache[path] = pixmap.scaled(
            78, 44, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self.recordings_list.viewport().update()
        self._schedule_thumbnails()

    def _thumbnail_failed(self, path: str, generation: int, message: str) -> None:
        if generation != self._source_generation:
            return
        self._thumb_pending.discard(path)
        self._thumb_failed.add(path)
        log.warning("录像缩略图读取失败 %s: %s", path, message)
        self._schedule_thumbnails()

    def _update_empty(self) -> None:
        if self._tab == "events":
            shown = len(self._filtered_events())
            self.empty.setText(self._event_empty_message())
        else:
            shown = len(self._filtered_records())
            self.empty.setText("暂无录像")
        self.empty.setVisible(shown == 0 and not self._loading[self._tab]
                              and self._tab not in self._errors)

    def _update_subtitle(self) -> None:
        if self._loading[self._tab]:
            self.sub.setText("正在载入…")
            return
        if self._tab in self._errors:
            label = "事件" if self._tab == "events" else "录像"
            self.sub.setText(f"{label}读取失败：{self._errors[self._tab]}")
            return
        if not self._loaded[self._tab]:
            self.sub.setText("等待载入")
            return
        if self._tab == "events":
            label = dict(CATEGORIES)[self._event_category]
            self.sub.setText(f"最近载入 {len(self._events)} 条 · {label} {len(self._filtered_events())} 条")
            return
        shown = len(self._filtered_records())
        label = {
            "all": "全部",
            "event": "事件片段",
            "continuous": "在线录像",
        }[self._recording_kind]
        self.sub.setText(f"录像 {shown} 条 · {label}")

    def _event_empty_message(self) -> str:
        return {
            "all": "暂无事件",
            "intrusion": "暂无安全事件",
            "access": "暂无出入记录",
            "package": "暂无快递事件",
            "system": "暂无系统事件",
        }[self._event_category]

    def _select_default(self) -> None:
        if not self._active or not self._auto_select_pending or not self._loaded[self._tab]:
            return
        self._auto_select_pending = False
        if self._tab == "events":
            for index in range(self.events_list.count()):
                item = self.events_list.item(index)
                event = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(event, Event):
                    self.events_list.setCurrentItem(item)
                    self._on_event_selected(event)
                    return
            message = ("展开日期分组查看事件" if self._filtered_events()
                       else self._event_empty_message())
        else:
            for index in range(self.recordings_list.count()):
                item = self.recordings_list.item(index)
                payload = item.data(Qt.ItemDataRole.UserRole)
                if payload.get("type") == "record":
                    self.recordings_list.setCurrentItem(item)
                    self._on_recordings_item_clicked(item)
                    return
            message = "暂无可选择的录像"
        self.play_title.setText("详情")
        self._reset_player(message)

    def _on_events_item_clicked(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(payload, Event):
            self._on_event_selected(payload)
        elif isinstance(payload, dict) and payload.get("type") == "group":
            day = payload["day"]
            if day in self._collapsed_event_days:
                self._collapsed_event_days.remove(day)
            else:
                self._collapsed_event_days.add(day)
            self._dirty["events"] = True
            self._render_current()

    def _matching_fingerprint_snapshot(self, event: Event) -> str | None:
        finger_id = event.detail.get("finger_id")
        if finger_id is None or event.ts is None:
            return None
        matches = []
        for candidate in self._events:
            if (
                candidate.kind != "door.opened"
                or candidate.raw.get("source") != "finger"
                or not candidate.snapshot
            ):
                continue
            if str(candidate.detail.get("user_id")) != str(finger_id) or candidate.ts is None:
                continue
            try:
                delta = abs((candidate.ts - event.ts).total_seconds())
            except TypeError:
                continue
            if delta <= 10:
                matches.append((delta, candidate.snapshot))
        return min(matches, default=(None, None))[1]

    def _on_event_selected(self, event: Event) -> None:
        self._auto_select_pending = False
        self._has_selection = True
        self._current_record = None
        self._current_event = event
        self.play_title.setText(event.title)
        if event.clip:
            self._play_clip(event.clip)
        else:
            self._reset_player("此事件没有录像")
        self.event_title.setText(event.title)
        timestamp = (event.ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                     if event.ts else "未知日期")
        self.event_meta.setText(f"{dict(CATEGORIES)[event.category]} · {timestamp}")
        self.event_summary.setText(event.summary())
        self.event_fields.setText("\n".join(f"{key}：{value}" for key, value in event.detail_rows()))
        self._event_details_scroll.verticalScrollBar().setValue(0)
        fingerprint_preview = event.kind == "finger.match" and not event.snapshot
        fingerprint_snapshot = (
            self._matching_fingerprint_snapshot(event) if fingerprint_preview else None
        )
        has_media = bool(event.clip or event.snapshot or fingerprint_preview)
        self.event_media_hint.setVisible(not has_media)
        self.event_details.setMinimumHeight(120 if has_media else 145)
        self.event_details.setMaximumHeight(120 if has_media else 16777215)
        self._right_layout.setStretchFactor(self.event_details, 0 if has_media else 1)
        self.event_details.setVisible(not has_media)
        self.btn_event_details.setVisible(has_media)
        self.video_stack.setVisible(has_media)
        self.btn_open.setVisible(bool(event.clip))
        self.btn_play.setVisible(bool(event.clip))
        self.event_snapshot.setToolTip("")
        if event.snapshot and not event.clip:
            self.video_placeholder.setText("正在载入快照…")
            self._load_event_snapshot(event.snapshot)
        elif fingerprint_preview and not event.clip:
            if fingerprint_snapshot:
                self.video_placeholder.setText("正在载入识别抓拍…")
                self.event_snapshot.setToolTip("同次开锁抓拍")
                self._load_event_snapshot(fingerprint_snapshot)
            else:
                self.video_placeholder.setText("正在载入当前门口画面…")
                self.event_snapshot.setToolTip("当前门口画面；该识别事件未附历史抓拍")
                self._load_event_snapshot(self._api.snapshot_url(), is_url=True)

    def _toggle_event_details(self, checked: bool) -> None:
        self.event_details.setVisible(checked)
        self.btn_event_details.setText("收起详情" if checked else "查看详情")

    def _load_event_snapshot(self, path: str, *, is_url: bool = False) -> None:
        generation = self._selection_generation

        def failure(message: str) -> None:
            if generation != self._selection_generation:
                return
            log.warning("事件快照读取失败 %s: %s", path, message)
            self.video_placeholder.setText(f"快照读取失败：{message}")
            self.video_stack.setCurrentWidget(self.video_placeholder)

        def success(data: bytes) -> None:
            if generation != self._selection_generation:
                return
            pixmap = QPixmap()
            if not pixmap.loadFromData(data):
                failure("图片格式无效")
                return
            self._snapshot_pixmap = pixmap
            self.video_stack.setCurrentWidget(self.event_snapshot)
            self._resize_event_snapshot()

        fetch = self._api.get_bytes_url if is_url else self._api.get_bytes
        fetch(path, on_ok=success, on_err=failure)

    def _resize_event_snapshot(self) -> None:
        if self._snapshot_pixmap is not None:
            self.event_snapshot.setPixmap(self._snapshot_pixmap.scaled(
                self.event_snapshot.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))

    def _on_recordings_item_clicked(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "group":
            day = str(payload.get("day", ""))
            if day in self._collapsed_days:
                self._collapsed_days.remove(day)
            else:
                self._collapsed_days.add(day)
            self._rebuild_recordings_list()
            return
        rec = payload.get("record")
        if not isinstance(rec, Recording):
            return
        self._auto_select_pending = False
        self._has_selection = True
        self._current_event = None
        self._current_record = rec
        self.play_title.setText(f"{rec.title} · {rec.date_text} {rec.time_text}")
        self._play_clip(rec.path)

    def _play_clip(self, path: str) -> None:
        self._reset_player("正在载入录像…")
        self._selection_title = self.play_title.text()
        url = self._api.media_url(path)
        self._external_url = url
        self._current_clip_name = Path(path).name or "recording.mp4"
        self.btn_open.setEnabled(bool(url))
        self.btn_play.setEnabled(bool(url) and HAVE_MULTIMEDIA)
        if HAVE_MULTIMEDIA and url and self.player is not None:
            self._awaiting_video = True
            self.player.setSource(QUrl(url))
            self.player.play()
            self.btn_play.setText("⏸  暂停")
        elif not url:
            self.video_placeholder.setText("录像地址不可用")
        else:
            self.video_placeholder.setText("未安装 QtMultimedia，点击右上角用系统播放器打开。")
            self.btn_play.setText("▶  播放")

    def _reset_player(self, message: str) -> None:
        self._selection_generation += 1
        self._snapshot_pixmap = None
        self.event_snapshot.clear()
        self.event_details.hide()
        self.btn_event_details.setChecked(False)
        self.btn_event_details.setText("查看详情")
        self.btn_event_details.hide()
        self.media_error.hide()
        self.video_stack.show()
        self.btn_open.show()
        self.btn_play.show()
        self._retry_timer.stop()
        self._playback_retries = 0
        self._awaiting_video = False
        self.video_placeholder.setText(message)
        self.video_stack.setCurrentWidget(self.video_placeholder)
        self._external_url = None
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self.btn_open.setEnabled(False)
        self.btn_play.setEnabled(False)
        self.btn_play.setText("▶  播放")

    def _on_video_frame(self, frame) -> None:
        if self._awaiting_video and frame.isValid():
            self._awaiting_video = False
            self.play_title.setText(self._selection_title)
            self.video_stack.setCurrentWidget(self.video)

    def _retry_playback(self) -> None:
        if self._active and self.player is not None and self._external_url:
            self._awaiting_video = True
            self.player.setSource(QUrl())
            self.player.setSource(QUrl(self._external_url))
            self.player.play()
            self.btn_play.setText("⏸  暂停")

    def _toggle(self) -> None:
        if not HAVE_MULTIMEDIA or self.player is None:
            return
        if self.player.error() != QMediaPlayer.Error.NoError:
            self._retry_timer.stop()
            self._playback_retries = 0
            self.video_placeholder.setText("正在载入录像…")
            self._retry_playback()
            return
        state = self.player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.btn_play.setText("▶  播放")
        else:
            self.player.play()
            self.btn_play.setText("⏸  暂停")

    def _open_external(self) -> None:
        if not self._external_url:
            return
        target = Path(os.getenv("XDG_RUNTIME_DIR", "/tmp")) / self._current_clip_name
        self.media_error.hide()

        def save(data: bytes) -> None:
            target.write_bytes(data)
            subprocess.Popen(["xdg-open", str(target)])

        self._api.get_bytes_url(self._external_url, on_ok=save, on_err=self._on_download_error)

    def _on_download_error(self, message: str) -> None:
        self.media_error.setText(f"下载失败：{message}")
        self.media_error.show()

    def _on_player_error(self, *_args) -> None:
        if HAVE_MULTIMEDIA and self.player is not None and self._external_url:
            self._awaiting_video = False
            self.video_stack.setCurrentWidget(self.video_placeholder)
            self.btn_play.setText("▶  播放")
            if (self._active and self.player.error() == QMediaPlayer.Error.FormatError
                    and self._playback_retries < 15):
                self._playback_retries += 1
                self.video_placeholder.setText(
                    f"录像暂时无法播放，正在重试（{self._playback_retries}/15）\n"
                    f"{self.player.errorString()}"
                )
                self.play_title.setText(f"播放失败：{self.player.errorString()}，正在重试")
                self._retry_timer.start()
                return
            self.video_placeholder.setText(
                f"播放失败：{self.player.errorString()}\n可用系统播放器打开"
            )
            self.play_title.setText(f"播放失败：{self.player.errorString()}（可用系统播放器打开）")

    def play_event(self, event: Event) -> None:
        self._set_tab("events")
        if self._event_category not in ("all", event.category):
            self._set_event_category(event.category)
        self._collapsed_event_days.discard(_event_day(event))
        self._dirty["events"] = True
        self._render_current()
        for index in range(self.events_list.count()):
            item = self.events_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == event:
                self.events_list.setCurrentItem(item)
                break
        self._on_event_selected(event)

    def stop(self) -> None:
        self._retry_timer.stop()
        if HAVE_MULTIMEDIA and self.player is not None:
            self.player.stop()


def _group_day(record: Recording) -> str:
    if record.ts is not None:
        return record.ts.strftime("%Y-%m-%d")
    parts = [p for p in record.path.split("/") if p]
    if len(parts) >= 2:
        day = parts[-2]
        if len(day) == 8 and day.isdigit():
            return f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    return "未知日期"


def _event_day(event: Event) -> str:
    return event.ts.astimezone().strftime("%Y-%m-%d") if event.ts else "未知日期"


def _event_day_label(day: str) -> str:
    today = datetime.now().date()
    if day == today.isoformat():
        return "今天"
    if day == (today - timedelta(days=1)).isoformat():
        return "昨天"
    return day


def _human_size(size: int) -> str:
    val = float(max(0, size))
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if val < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(val)} {unit}"
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return "0 B"
