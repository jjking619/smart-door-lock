"""记录中心页：统一查看事件与录像。

方案二落地：把原来的“事件”和“回放”聚合到一个入口，
用页内标签切换，避免用户在导航里猜“录像在哪看”。
"""

from __future__ import annotations

from collections import OrderedDict
import logging
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QRect, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QVBoxLayout, QWidget,
)

from . import theme
from .api import Event, Recording
from .widgets import EventList, card, card_title

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
        if self._recordings and payload["type"] == "group":
            day = payload["day"]
            arrow = "▸" if day in self._page._collapsed_days else "▾"
            _draw_text(painter, r.adjusted(8, 0, -55, 0), f"{arrow}  {day}", theme.TEXT_DIM, 12, bold=True)
            _draw_text(painter, QRect(r.right() - 52, r.top(), 48, r.height()),
                       f"{payload['count']} 条", theme.TEXT_MUTED, 11, align=Qt.AlignmentFlag.AlignRight)
        elif self._recordings:
            rec = payload["record"]
            thumb = QRect(r.left() + 6, r.top() + 5, 72, 40)
            pixmap = self._page._thumb_cache.get(rec.snapshot)
            if pixmap:
                painter.drawPixmap(thumb, pixmap)
            else:
                painter.fillRect(thumb, QColor("#2A2438" if rec.kind == "event" else "#1F3230"))
                _draw_text(painter, thumb, "无图", theme.TEXT_MUTED, 12, align=Qt.AlignmentFlag.AlignHCenter)
            x = thumb.right() + 11
            width = max(0, r.right() - x - 5)
            _draw_text(painter, QRect(x, r.top() + 4, width, 22), rec.title, rec.color, 13, bold=True)
            meta = f"{rec.kind_text} · {rec.date_text} {rec.time_text} · {_human_size(rec.size)}"
            _draw_text(painter, QRect(x, r.top() + 26, width, 20), meta, theme.TEXT_MUTED, 11)
        else:
            event = payload
            thumb = QRect(r.left() + 10, r.top() + 8, 96, 54)
            painter.fillRect(thumb, QColor("#243040"))
            _draw_text(painter, thumb, "无图", theme.TEXT_MUTED, 12, align=Qt.AlignmentFlag.AlignHCenter)
            x = thumb.right() + 12
            width = max(0, r.right() - x - 74)
            _draw_text(painter, QRect(x, r.top() + 10, 14, 22), "●", event.color, 10)
            _draw_text(painter, QRect(x + 16, r.top() + 10, width - 16, 22),
                       event.title, theme.TEXT, 14, bold=True)
            _draw_text(painter, QRect(x, r.top() + 35, width, 22), event.summary(), theme.TEXT_DIM, 12)
            _draw_text(painter, QRect(r.right() - 70, r.top() + 10, 64, 22),
                       event.time_text, theme.TEXT, 13, bold=True, align=Qt.AlignmentFlag.AlignRight)
            _draw_text(painter, QRect(r.right() - 70, r.top() + 35, 64, 22),
                       event.date_text, theme.TEXT_MUTED, 11, align=Qt.AlignmentFlag.AlignRight)
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
        self._collapsed_days: set[str] = set()
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

        self.events_list = EventList()
        self.events_list.setItemDelegate(_ListDelegate(self, False, self.events_list))
        self.events_list.selected.connect(self._on_event_selected)
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
        self.empty.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:13px;")
        left_lay.addWidget(self.empty)
        body.addWidget(left_card)

        right = QVBoxLayout()
        right.setSpacing(12)

        op = QHBoxLayout()
        self.play_title = QLabel("详情")
        self.play_title.setObjectName("PageSub")
        op.addWidget(self.play_title)
        op.addStretch(1)
        self.btn_open = QPushButton("用系统播放器打开")
        self.btn_open.setObjectName("Small")
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._open_external)
        op.addWidget(self.btn_open)
        right.addLayout(op)

        self.video_stack = QStackedWidget()
        self.video_placeholder = QLabel("请选择记录")
        self.video_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_placeholder.setStyleSheet(
            f"color:{theme.TEXT_DIM};background:#05070A;border-radius:10px;padding:24px;"
        )
        self.video_stack.addWidget(self.video_placeholder)
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

        self.btn_play = QPushButton("▶  播放")
        self.btn_play.setObjectName("Ghost")
        self.btn_play.setFixedHeight(52)
        self.btn_play.setEnabled(False)
        self.btn_play.clicked.connect(self._toggle)
        right.addWidget(self.btn_play)

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
            self._api.events(120, on_ok=success, on_err=failure)
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
        self._dirty = {"events": False, "recordings": False}
        self.events_list.clear()
        self.recordings_list.clear()
        self._reset_player("请选择记录")
        self._current_record = None
        self.play_title.setText("详情")
        self._auto_select_pending = True
        self._has_selection = False
        self._sync_record_filter_labels()
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
        self._events = events
        self._dirty["events"] = True
        if self._tab == "events":
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
        self._auto_select_pending = True
        self._has_selection = False
        self.play_title.setText("详情")
        self._reset_player("正在载入记录…")
        self.btn_events.setChecked(tab == "events")
        self.btn_recordings.setChecked(tab == "recordings")
        self.events_list.setVisible(tab == "events")
        self.recordings_list.setVisible(tab == "recordings")
        self.record_filter_row.setVisible(tab == "recordings")
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
            for event in self._events:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, event)
                item.setSizeHint(QSize(0, 76))
                self.events_list.addItem(item)
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
            header.setSizeHint(QSize(0, 30))
            self.recordings_list.addItem(header)
            if day in self._collapsed_days:
                continue
            for rec in items:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, {"type": "record", "day": day, "record": rec})
                item.setSizeHint(QSize(0, 50))
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
            path = payload["record"].snapshot
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
            72, 40, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
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
            shown = len(self._events)
            self.empty.setText("暂无事件")
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
            self.sub.setText(f"事件 {len(self._events)} 条")
            return
        shown = len(self._filtered_records())
        label = {
            "all": "全部",
            "event": "事件片段",
            "continuous": "在线录像",
        }[self._recording_kind]
        self.sub.setText(f"录像 {shown} 条 · {label}")

    def _select_default(self) -> None:
        if not self._active or not self._auto_select_pending:
            return
        self._auto_select_pending = False
        if self._tab == "events":
            item = self.events_list.item(0)
            if item is not None:
                self.events_list.setCurrentItem(item)
                self._on_event_selected(item.data(Qt.ItemDataRole.UserRole))
                return
            message = "暂无事件"
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

    def _on_event_selected(self, event: Event) -> None:
        self._auto_select_pending = False
        self._has_selection = True
        self._current_record = None
        self.play_title.setText(f"{event.title} · {event.date_text} {event.time_text}")
        if event.clip:
            self._play_clip(event.clip)
        else:
            self._reset_player("此事件没有录像")

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

        def save(data: bytes) -> None:
            target.write_bytes(data)
            subprocess.Popen(["xdg-open", str(target)])

        self._api.get_bytes_url(self._external_url, on_ok=save,
                                on_err=lambda m: self.play_title.setText(f"下载失败：{m}"))

    def _on_player_error(self, *_args) -> None:
        if HAVE_MULTIMEDIA and self.player is not None and self._external_url:
            self._awaiting_video = False
            self.video_stack.setCurrentWidget(self.video_placeholder)
            self.btn_play.setText("▶  播放")
            if (self._active and self.player.error() == QMediaPlayer.Error.FormatError
                    and self._playback_retries < 15):
                self._playback_retries += 1
                self.video_placeholder.setText(f"录像暂时无法播放，正在重试（{self._playback_retries}/15）")
                self.play_title.setText(f"播放失败：{self.player.errorString()}，正在重试")
                self._retry_timer.start()
                return
            self.video_placeholder.setText("播放失败，可用系统播放器打开")
            self.play_title.setText(f"播放失败：{self.player.errorString()}（可用系统播放器打开）")

    def play_event(self, event: Event) -> None:
        self._set_tab("events")
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
