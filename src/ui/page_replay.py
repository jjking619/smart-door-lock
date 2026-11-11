"""录像回放页。

为什么不用内嵌浏览器 / 强依赖 QVideoWidget：
RK3576 上 QtMultimedia 走 GStreamer，`mppvideodec` 已验证存在，但**如果发行版
没装 QtMultimedia 插件，模块导入就会失败**。所以这里做成分层：

  有 QtMultimedia → 真播放器（硬解）
  没有            → 退化成"下载 / 用系统播放器打开"，功能不假死

仓库里现有证据：`gst-inspect-1.0 mppvideodec` 存在，H.264 硬编已实测通过。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from . import theme
from .api import Event
from .widgets import Thumb, card, card_title, hline

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget

    HAVE_MULTIMEDIA = True
except ImportError:      # 板子上没装 qtmultimedia 时不要整个界面崩掉
    HAVE_MULTIMEDIA = False


class ReplayPage(QWidget):
    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._clips: list[Event] = []

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # ---------- 左：片段列表 ----------
        left_card, left_lay = card(spacing=8)
        left_card.setFixedWidth(280)
        left_lay.addWidget(card_title("录像片段"))
        self.list = QListWidget()
        self.list.setSpacing(2)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.itemClicked.connect(self._on_pick)
        left_lay.addWidget(self.list, 1)
        self.empty = QLabel("暂无录像")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:15px;")
        left_lay.addWidget(self.empty)
        root.addWidget(left_card)

        # ---------- 右：播放器 ----------
        right = QVBoxLayout()
        right.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("回放")
        title.setObjectName("PageTitle")
        self.sub = QLabel("选择左侧片段")
        self.sub.setObjectName("PageSub")
        head.addWidget(title)
        head.addWidget(self.sub)
        head.addStretch(1)
        self.btn_open = QPushButton("用系统播放器打开")
        self.btn_open.setObjectName("Small")
        self.btn_open.clicked.connect(self._open_external)
        self.btn_open.setEnabled(False)
        head.addWidget(self.btn_open)
        right.addLayout(head)

        self.stack = QStackedWidget()
        if HAVE_MULTIMEDIA:
            self.video = QVideoWidget()
            self.video.setStyleSheet(f"background:#05070A;border-radius:10px;")
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.errorOccurred.connect(self._on_player_error)
            self.stack.addWidget(self.video)
        else:
            fallback = QLabel(
                "未检测到 QtMultimedia。\n\n"
                "装上即可内嵌播放（板子已具备 mppvideodec 硬解）：\n"
                "  sudo apt install python3-pyside6.qtmultimedia\n\n"
                "现在仍可用右侧按钮交给系统播放器。"
            )
            fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            fallback.setStyleSheet(f"color:{theme.TEXT_DIM};background:#05070A;"
                                   "border-radius:10px;padding:24px;")
            self.player = None
            self.stack.addWidget(fallback)
        right.addWidget(self.stack, 1)

        # ---------- 传输条 ----------
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.btn_play = QPushButton("▶  播放")
        self.btn_play.setObjectName("Ghost")
        self.btn_play.setFixedHeight(52)
        self.btn_play.clicked.connect(self._toggle)
        self.btn_play.setEnabled(False)
        bar.addWidget(self.btn_play)

        self.position = QLabel("00:00 / 00:00")
        self.position.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:15px;")
        bar.addWidget(self.position)
        bar.addStretch(1)

        self.btn_prev = QPushButton("上一个")
        self.btn_prev.setObjectName("Small")
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next = QPushButton("下一个")
        self.btn_next.setObjectName("Small")
        self.btn_next.clicked.connect(lambda: self._step(1))
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_next)
        right.addLayout(bar)

        root.addLayout(right, 1)

        if HAVE_MULTIMEDIA:
            self.player.positionChanged.connect(self._on_position)
            self.player.durationChanged.connect(self._on_duration)
            self.player.playbackStateChanged.connect(self._on_state)

        self._current: Event | None = None
        self._external_path: str | None = None

    # ---------------- 数据 ----------------
    def set_events(self, events: list[Event]) -> None:
        self._clips = [e for e in events if e.clip]
        self.list.clear()
        for ev in self._clips:
            item = QListWidgetItem()          # 不要传 self，见 widgets.EventList 的注释
            row = self._row(ev)
            item.setSizeHint(row.sizeHint())
            item.setData(Qt.ItemDataRole.UserRole, ev)
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        self.empty.setVisible(not self._clips)
        self.list.setVisible(bool(self._clips))
        if self._clips:
            self.sub.setText(f"{len(self._clips)} 个片段")

    def _row(self, ev: Event) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(10)
        thumb = Thumb(size=self._thumb_size())
        if ev.snapshot:
            self._api.get_bytes(ev.snapshot, on_ok=thumb.set_image, on_err=lambda _m: None)
        else:
            thumb._placeholder(1)
        lay.addWidget(thumb)
        info = QVBoxLayout()
        info.setSpacing(1)
        t = QLabel(ev.title)
        t.setStyleSheet(f"font-size:15px;font-weight:600;color:{theme.TEXT};")
        d = QLabel(ev.time_text)
        d.setStyleSheet(f"font-size:15px;color:{theme.TEXT_MUTED};")
        info.addWidget(t)
        info.addWidget(d)
        lay.addLayout(info, 1)
        return w

    @staticmethod
    def _thumb_size():
        from PySide6.QtCore import QSize

        return QSize(72, 40)

    # ---------------- 交互 ----------------
    def _on_pick(self, item: QListWidgetItem) -> None:
        ev = item.data(Qt.ItemDataRole.UserRole)
        if ev is not None:
            self.play_event(ev)

    def play_event(self, ev: Event) -> None:
        self._current = ev
        url = self._api.media_url(ev.clip)
        self._external_path = url
        self.btn_open.setEnabled(bool(url))
        self.btn_play.setEnabled(bool(url) and HAVE_MULTIMEDIA)
        self.sub.setText(f"{ev.title} · {ev.date_text} {ev.time_text}")
        if HAVE_MULTIMEDIA and url:
            self.player.setSource(QUrl(url))
            self.player.play()
        else:
            self.btn_play.setText("▶  播放")

    def _toggle(self) -> None:
        if not HAVE_MULTIMEDIA or self._current is None:
            return
        state = self.player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _step(self, delta: int) -> None:
        if not self._clips or self._current is None:
            return
        try:
            idx = self._clips.index(self._current)
        except ValueError:
            idx = 0
        nxt = max(0, min(len(self._clips) - 1, idx + delta))
        self.play_event(self._clips[nxt])
        self.list.setCurrentRow(nxt)

    def _open_external(self) -> None:
        """兜底：把片段下到本地再用 xdg-open 交给系统播放器。"""
        if not self._external_path or self._current is None:
            return
        target = Path(os.getenv("XDG_RUNTIME_DIR", "/tmp")) / (
            (self._current.ts.strftime("%Y%m%d_%H%M%S") if self._current.ts else "clip") + ".mp4"
        )

        def save(data: bytes) -> None:
            target.write_bytes(data)
            subprocess.Popen(["xdg-open", str(target)])

        self._api.get_bytes_url(self._external_path, on_ok=save,
                                on_err=lambda m: self.sub.setText(f"下载失败：{m}"))

    # ---------------- 播放器回调 ----------------
    def _on_position(self, ms: int) -> None:
        self.position.setText(f"{_mmss(ms)} / {_mmss(self.player.duration())}")

    def _on_duration(self, _ms: int) -> None:
        self.position.setText(f"{_mmss(self.player.position())} / {_mmss(self.player.duration())}")

    def _on_state(self, state) -> None:
        if not HAVE_MULTIMEDIA:
            return
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.btn_play.setText("⏸  暂停" if playing else "▶  播放")

    def _on_player_error(self, *_args) -> None:
        self.sub.setText(f"播放失败：{self.player.errorString()}（可用系统播放器打开）")

    def stop(self) -> None:
        if HAVE_MULTIMEDIA and self.player is not None:
            self.player.stop()


def _mmss(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    return f"{total // 60:02d}:{total % 60:02d}"
