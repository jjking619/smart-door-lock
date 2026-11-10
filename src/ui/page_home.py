"""首页：实时画面 + 状态 + 主操作 + 最近事件。

三个决定
--------
1. **开门是唯一的红色按钮，且不常驻在首页**：默认只显示"解锁"要求二次确认，
   避免误触，也符合"UI 不持有钥匙"的边界（请求交给 lockd）。
2. **对讲用点击切换**。单击开始，再次点击结束；半双工避免无参考信号时的啸叫。
3. **状态卡优先显示"能不能进"**，而不是一堆技术指标。技术指标放最下方小字。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from . import theme
from .api import Event, Health
from .stream import VideoView
from .widgets import EventList, MetricTile, StatusPill, card, card_title, hline


class HomePage(QWidget):
    replay_requested = Signal(object)
    unlock_requested = Signal()
    talk_changed = Signal(bool)
    goto_events = Signal()

    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._unlocking = False
        self._talking = False
        self.lock_available = False
        self._conversation_text = ""
        self._talk_poll = QTimer(self)
        self._talk_poll.setInterval(1000)
        self._talk_poll.timeout.connect(self._poll_talk_status)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # ================= 左：画面 =================
        left = QVBoxLayout()
        left.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("门口实时画面")
        title.setObjectName("PageTitle")
        self.cam_pill = StatusPill("连接中", theme.WARN)
        head.addWidget(title)
        head.addWidget(self.cam_pill)
        head.addStretch(1)

        self.recording_pill = StatusPill("录像关闭", theme.TEXT_MUTED)
        head.addWidget(self.recording_pill)
        self.btn_recording = QPushButton("开始录像")
        self.btn_recording.setObjectName("Ghost")
        self.btn_recording.setCheckable(True)
        self.btn_recording.clicked.connect(self._toggle_recording)
        head.addWidget(self.btn_recording)

        self.btn_snapshot = QPushButton("抓拍")
        self.btn_snapshot.setObjectName("Small")
        self.btn_snapshot.clicked.connect(self._save_snapshot)
        head.addWidget(self.btn_snapshot)
        left.addLayout(head)

        self.video = VideoView(api)
        left.addWidget(self.video, 1)

        # ---- 主操作条 ----
        actions = QHBoxLayout()
        actions.setSpacing(10)

        self.btn_talk = QPushButton("开始对话")
        self.btn_talk.setObjectName("Ghost")
        self.btn_talk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_talk.clicked.connect(lambda: self._talk(not self._talking))
        self.btn_talk.setEnabled(False)
        actions.addWidget(self.btn_talk, 1)

        self.btn_unlock = QPushButton("解锁")
        self.btn_unlock.setObjectName("Primary")
        self.btn_unlock.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_unlock.clicked.connect(self._on_unlock_clicked)
        actions.addWidget(self.btn_unlock, 1)

        left.addLayout(actions)
        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:12px;")
        self.hint.setWordWrap(True)
        left.addWidget(self.hint)

        root.addLayout(left, 3)

        # ================= 右：状态 =================
        side = QVBoxLayout()
        side.setSpacing(12)

        # ---- 门锁状态 ----
        lock_card, lock_lay = card()
        lock_lay.addWidget(card_title("门锁状态"))
        self.lock_state = QLabel("已上锁")
        self.lock_state.setObjectName("CardHeadline")
        lock_lay.addWidget(self.lock_state)

        lock_rows = QGridLayout()
        lock_rows.setHorizontalSpacing(10)
        lock_rows.setVerticalSpacing(6)
        self.door_sensor = QLabel("门磁　关闭")
        self.arm_state = QLabel("布防　夜间")
        for i, w in enumerate((self.door_sensor, self.arm_state)):
            w.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:13px;")
            lock_rows.addWidget(w, i // 2, i % 2)
        lock_lay.addLayout(lock_rows)
        self.fingerprint_state = QLabel("指纹模块：连接中")
        self.fingerprint_state.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:12px;")
        self.fingerprint_result = QLabel("请按压指纹")
        self.fingerprint_result.setWordWrap(True)
        self.fingerprint_result.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:13px;")
        lock_lay.addWidget(self.fingerprint_state)
        lock_lay.addWidget(self.fingerprint_result)
        lock_lay.addStretch(1)
        side.addWidget(lock_card)

        # ---- 访客动态：最近事件 / 当前对话 ----
        ev_card, ev_lay = card(spacing=8)
        ev_head = QHBoxLayout()
        ev_head.addWidget(card_title("访客动态"))
        ev_head.addStretch(1)
        self.btn_show_conversation = QPushButton("当前对话")
        self.btn_show_conversation.setObjectName("Small")
        self.btn_show_conversation.setCheckable(True)
        self.btn_show_conversation.clicked.connect(lambda: self._show_activity("conversation"))
        ev_head.addWidget(self.btn_show_conversation)
        self.btn_show_events = QPushButton("最近事件")
        self.btn_show_events.setObjectName("Small")
        self.btn_show_events.setCheckable(True)
        self.btn_show_events.setChecked(True)
        self.btn_show_events.clicked.connect(lambda: self._show_activity("events"))
        ev_head.addWidget(self.btn_show_events)
        more = QPushButton("全部 ›")
        more.setObjectName("Small")
        more.clicked.connect(self.goto_events.emit)
        ev_head.addWidget(more)
        ev_lay.addLayout(ev_head)

        self.activity_stack = QStackedWidget()
        self.recent = EventList()
        self.recent.setMinimumHeight(230)
        self.recent.selected.connect(self.replay_requested.emit)
        self.recent_empty = QLabel("暂无记录")
        self.recent_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recent_empty.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:13px;")
        self.events_view = QWidget()
        events_lay = QVBoxLayout(self.events_view)
        events_lay.setContentsMargins(0, 0, 0, 0)
        events_lay.setSpacing(0)
        events_lay.addWidget(self.recent, 1)
        events_lay.addWidget(self.recent_empty, 1)
        self.activity_stack.addWidget(self.events_view)

        self.conversation_view = QWidget()
        conversation_lay = QVBoxLayout(self.conversation_view)
        conversation_lay.setContentsMargins(0, 0, 0, 0)
        conversation_lay.setSpacing(6)
        self.conversation_status = QLabel("开始对话后，会在这里显示识别文字和回复。")
        self.conversation_status.setStyleSheet(
            f"color:{theme.TEXT_MUTED};font-size:12px;"
        )
        self.conversation_text = QPlainTextEdit()
        self.conversation_text.setReadOnly(True)
        self.conversation_text.setPlaceholderText("对话内容会显示在这里")
        self.conversation_text.setStyleSheet(
            f"QPlainTextEdit {{ background:transparent; border:none; "
            f"color:{theme.TEXT}; font-size:14px; }}"
        )
        conversation_lay.addWidget(self.conversation_status)
        conversation_lay.addWidget(self.conversation_text, 1)
        self.activity_stack.addWidget(self.conversation_view)
        ev_lay.addWidget(self.activity_stack, 1)
        self._show_activity("events")
        side.addWidget(ev_card, 1)

        # ---- 技术指标（小字，放最后）----
        tech_card, tech_lay = card(spacing=6)
        tech_lay.addWidget(card_title("运行状态"))
        row = QHBoxLayout()
        row.setSpacing(18)
        self.m_fps = MetricTile("采集帧率", "fps")
        self.m_ring = MetricTile("缓冲帧", "帧")
        self.m_det = MetricTile("推理后端")
        for m in (self.m_fps, self.m_ring, self.m_det):
            row.addWidget(m)
        tech_lay.addLayout(row)
        self.conn_label = QLabel("visiond 未连接")
        self.conn_label.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        tech_lay.addWidget(self.conn_label)
        side.addWidget(tech_card)

        root.addLayout(side, 2)

        # 采集状态轮询：3 s 一次，只用一个定时器
        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(self.refresh)
        self._poll.start()

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        self.video.start()
        self.refresh()

    def stop(self) -> None:
        self._poll.stop()
        self.video.stop()

    # ---------------- 轮询 ----------------
    def refresh(self) -> None:
        self._api.health(self._on_health, self._on_health_error)
        if hasattr(self._api, "fingerprint_health"):
            self._api.fingerprint_health(self._on_fingerprint_health, self._on_fingerprint_error)
        if hasattr(self._api, "lock_health"):
            self._api.lock_health(self._on_lock_health, self._on_lock_health_error)
        self._api.events(6, on_ok=self._on_recent, on_err=lambda _m: None)
        self.fit_recent_height()

    def _on_fingerprint_health(self, status: dict) -> None:
        if not isinstance(status, dict) or status.get("service") != "fingerprintd":
            self._on_fingerprint_error("服务不可用")
            return
        if not status.get("connected"):
            self.fingerprint_state.setText("指纹模块：离线")
            self.fingerprint_state.setStyleSheet(f"color:{theme.WARN};font-size:12px;")
            self.fingerprint_result.setText(status.get("error") or "等待指纹模块连接")
            return
        device = status.get("device") or "串口"
        self.fingerprint_state.setText(f"指纹模块：在线 · {device}")
        self.fingerprint_state.setStyleSheet(f"color:{theme.OK};font-size:12px;")
        match = status.get("last_match")
        unlock = status.get("last_unlock")
        if status.get("action") == "enroll":
            self.fingerprint_result.setText(str(status.get("step") or "正在录入指纹"))
        elif match and match.get("id") is None:
            self.fingerprint_result.setText("最近识别：未匹配")
        elif match:
            name = match.get("name") or "未登记指纹"
            finger = f" · {match['finger']}" if match.get("finger") else ""
            text = f"最近识别：{name}{finger}（ID {match['id']}）"
            if unlock:
                text += " · 已开锁" if unlock.get("ok") else f" · 未开锁：{unlock.get('reason') or '被拒绝'}"
            self.fingerprint_result.setText(text)
            self.fingerprint_result.setStyleSheet(
                f"color:{theme.OK if (unlock or {}).get('ok') else theme.WARN};font-size:13px;"
            )
        else:
            self.fingerprint_result.setText("请按压指纹")
            self.fingerprint_result.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:13px;")

    def _on_fingerprint_error(self, message: str) -> None:
        self.fingerprint_state.setText("指纹模块：服务不可用")
        self.fingerprint_state.setStyleSheet(f"color:{theme.CRIT};font-size:12px;")
        self.fingerprint_result.setText(str(message))

    def _on_lock_health(self, status: dict) -> None:
        if not isinstance(status, dict) or status.get("service") != "lockd":
            self._on_lock_health_error("")
            return
        self.lock_available = True
        if status.get("state") == "fault":
            self.lock_state.setText("锁体故障")
            self.lock_state.setStyleSheet(f"color:{theme.CRIT};")
        elif status.get("state") == "unlocked":
            self.lock_state.setText("已开锁")
            self.lock_state.setStyleSheet(f"color:{theme.OK};")
        elif status.get("locked_out"):
            self.lock_state.setText("已锁定")
            self.lock_state.setStyleSheet(f"color:{theme.CRIT};")
        else:
            self.lock_state.setText("已上锁")
            self.lock_state.setStyleSheet("")
        driver = (status.get("driver") or {}).get("driver")
        if driver == "mock":
            self.arm_state.setText("锁体　模拟")
        elif driver:
            self.arm_state.setText(f"锁体　{driver}")
        error = (status.get("driver") or {}).get("error")
        if error:
            self.arm_state.setText(f"锁体　{driver}：{error}")
            self.arm_state.setWordWrap(True)
        locked_out = bool(status.get("locked_out"))
        self.btn_unlock.setEnabled(not locked_out)
        self.btn_unlock.setText("解锁（锁定中）" if locked_out else "解锁")

    def _on_lock_health_error(self, _message: str = "") -> None:
        self.lock_available = False
        self.lock_state.setText("状态未知")
        self.lock_state.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        self.arm_state.setText("锁体　未接入")
        self.btn_unlock.setEnabled(False)
        self.btn_unlock.setText("解锁（未接入）")

    def _on_health(self, h: Health) -> None:
        self.m_fps.set_value(f"{h.fps:.1f}")
        self.m_ring.set_value(str(h.ring))
        if h.detector_error or h.package_detector_error:
            self.m_det.set_value("检测异常", theme.CRIT)
        elif h.package_detector_enabled and not h.package_detector:
            self.m_det.set_value("包裹模型未加载", theme.CRIT)
        else:
            self.m_det.set_value(h.detector or "无模型",
                                 theme.TEXT if h.detector else theme.TEXT_MUTED)
        self.conn_label.setText(
            f"visiond 已连接 · 预览客户端 {h.preview_clients} · 录制中 {h.clips_active}"
        )
        self._set_recording_state(h.recording_requested, h.recording_active)
        if h.fps <= 0.1:
            self.cam_pill.set_state("无画面", theme.CRIT)
        else:
            self.cam_pill.set_state("画面正常", theme.OK)

    def _on_health_error(self, msg: str) -> None:
        self.conn_label.setText(f"visiond 未连接：{msg}")
        self.cam_pill.set_state("离线", theme.CRIT)
        self.m_fps.set_value("—")
        self.m_ring.set_value("—")
        self.m_det.set_value("—", theme.TEXT_MUTED)

    def _on_recent(self, events: list[Event]) -> None:
        self.recent.set_events(events[:6])
        self.recent_empty.setVisible(not events)
        self.recent.setVisible(bool(events))
        self.fit_recent_height()

    def fit_recent_height(self) -> None:
        """最近事件区按内容收放，不留大片空白。"""
        count = self.recent.count()
        if count == 0:
            return
        row = self.recent.sizeHintForRow(0) or 62
        self.recent.setMinimumHeight(min(6, count) * row + 8)
        self.recent.setMaximumHeight(min(6, count) * row + 8)

    # ---------------- 操作 ----------------
    def _toggle_recording(self, on: bool) -> None:
        self.btn_recording.setEnabled(False)
        self.recording_pill.set_state("启动中" if on else "停止中", theme.WARN)

        def ok(data) -> None:
            state = (data or {}).get("recording") or {}
            self._set_recording_state(bool(state.get("requested")), bool(state.get("active")))

        def err(message: str) -> None:
            self._set_recording_state(not on, False)
            self.hint.setText(f"录像控制失败：{message}")

        self._api.recording(on, ok, err)

    def _set_recording_state(self, requested: bool, active: bool) -> None:
        self.btn_recording.blockSignals(True)
        self.btn_recording.setChecked(requested)
        self.btn_recording.setText("停止录像" if requested else "开始录像")
        self.btn_recording.setEnabled(True)
        self.btn_recording.blockSignals(False)
        if active:
            self.recording_pill.set_state("录像中", theme.CRIT)
        elif requested:
            self.recording_pill.set_state("启动中", theme.WARN)
        else:
            self.recording_pill.set_state("录像关闭", theme.TEXT_MUTED)

    def _on_unlock_clicked(self) -> None:
        """两段式：第一次点击变成"确认解锁"，3 s 内不点则复原。"""
        if self._unlocking:
            self._unlocking = False
            self.btn_unlock.setText("解锁")
            self._restyle(self.btn_unlock, "Primary")
            self.btn_unlock.setEnabled(False)
            self.unlock_requested.emit()
            self.hint.setText("已发送开锁请求，等待 lockd 校验…")
            return

        self._unlocking = True
        self.btn_unlock.setText("确认解锁")
        self._restyle(self.btn_unlock, "Danger")
        self.hint.setText("再次点击确认。真正开锁由 lockd 校验后交给锁体。")
        QTimer.singleShot(3000, self._reset_unlock)

    def _reset_unlock(self) -> None:
        if not self._unlocking:
            return
        self._unlocking = False
        self.btn_unlock.setText("解锁")
        self._restyle(self.btn_unlock, "Primary")
        self.hint.setText("")

    @staticmethod
    def _restyle(widget: QWidget, name: str) -> None:
        widget.setObjectName(name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _talk(self, on: bool) -> None:
        if on == self._talking:
            return
        self._talking = on
        if on:
            self._conversation_text = ""
            self.conversation_text.clear()
            self.conversation_status.setText("正在连接")
            self._show_activity("conversation")
        self.btn_talk.setText("结束对话" if on else "开始对话")
        self._restyle(self.btn_talk, "Primary" if on else "Ghost")
        self.talk_changed.emit(on)
        if on:
            self._talk_poll.start()
        else:
            self._talk_poll.stop()

    def _poll_talk_status(self) -> None:
        self._api.audio_health(self._on_audio_health, self._on_audio_health_error)

    def _on_audio_health(self, status) -> None:  # noqa: ANN001
        self._update_conversation(
            status.get("conversation", []),
            status.get("conversation_status", ""),
        )
        if self._talking and not status.get("busy", False):
            error = status.get("talk_error")
            self._talk(False)
            if error:
                self.hint.setText(str(error))

    def _on_audio_health_error(self, message: str) -> None:
        if self._talking:
            self._talk(False)
            self.hint.setText(f"对话服务不可用：{message}")

    def _show_activity(self, view: str) -> None:
        show_conversation = view == "conversation"
        self.activity_stack.setCurrentWidget(
            self.conversation_view if show_conversation else self.events_view
        )
        self.btn_show_conversation.setChecked(show_conversation)
        self.btn_show_events.setChecked(not show_conversation)

    def _update_conversation(self, messages: list, status: str) -> None:
        self.conversation_status.setText(status or "对话已结束")
        lines = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = "你" if item.get("role") == "visitor" else "助手"
            text = str(item.get("text") or "").strip()
            if item.get("partial"):
                text = f"{text}…" if text else "正在识别…"
            if text:
                lines.append(f"{role}：{text}")
        rendered = "\n\n".join(lines)
        if rendered == self._conversation_text:
            return
        self._conversation_text = rendered
        self.conversation_text.setPlainText(rendered)
        scrollbar = self.conversation_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def set_capabilities(self, unlock_ok: bool, talk_ok: bool) -> None:
        """只处理对讲能力；解锁按钮的可用性由 lockd 的 /health 决定。"""
        self.btn_talk.setEnabled(talk_ok)
        if not talk_ok:
            self.hint.setText("对讲需要 audiood 提供 /talk 接口；当前后端未接入。")
        elif "对讲需要 audiood 提供 /talk 接口" in self.hint.text():
            self.hint.setText("")

    def _save_snapshot(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        pm = self.video.pixmap()
        if pm is None or pm.isNull():
            self.hint.setText("当前没有画面可抓拍。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存快照", "snapshot.jpg", "JPEG (*.jpg)"
        )
        if path:
            self.video.pixmap().save(path, "JPG")
            self.hint.setText(f"已保存到 {path}")
