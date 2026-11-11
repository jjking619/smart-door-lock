"""首页：实时画面 + 五大功能磁贴 + 访客动态。

布局（1280×800 触摸屏）
-----------------------

    ┌─────────────── 实时画面 ───────────────┐ ┌ 门锁 ──┐ ┌ 指纹 ──┐
    │                                        │ ├ 录像 ──┤ ├ 入侵 ──┤
    │            摄像头（人 / 包裹框）        │ ├ 快递 ──┤ ├ 对讲 ──┤
    │                                        │ ┌──── 访客动态 ────┐
    └────────────────────────────────────────┘ │ 最近事件 / 对话   │
    [  语音对讲  ] [  开始录像  ] [   解锁   ]  └──────────────────┘

三个决定
--------
1. **开门要二次确认**：第一次点击变红色"确认解锁"，3 s 内再点才发请求，
   真正开锁由 lockd 校验，UI 不持有钥匙。
2. **对讲用点击切换**。单击开始，再次点击结束；半双工避免无参考信号时的啸叫。
3. **每个功能一块磁贴、一种颜色**：展会观众远看就能对上功能；新事件到达时
   对应磁贴高亮几秒，演示时不用解释"刚才发生了什么"。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import theme
from .api import Event, Health
from .i18n import L
from .stream import VideoView
from .widgets import EventList, FeatureTile, StatusPill, card, card_title, make_icon

RECENT_LIMIT = 6
FEED_LIMIT = 80


class HomePage(QWidget):
    replay_requested = Signal(object)
    unlock_requested = Signal()
    talk_changed = Signal(bool)
    goto_events = Signal()
    goto_settings = Signal()

    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._unlocking = False
        self._talking = False
        self._talk_available = False
        self._talk_hint = False
        self.lock_available = False
        self._conversation_text = ""
        self._seen: dict[str, datetime | None] = {}
        self._finger_key: str | None = None
        self._hint_generation = 0
        self._talk_poll = QTimer(self)
        self._talk_poll.setInterval(1000)
        self._talk_poll.timeout.connect(self._poll_talk_status)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # ================= 左：画面 + 主操作 =================
        left = QVBoxLayout()
        left.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("门口实时画面")
        title.setObjectName("PageTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.recording_pill = StatusPill("录像关闭", theme.TEXT_MUTED)
        head.addWidget(self.recording_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        self.btn_snapshot = QPushButton(" 抓拍")
        self.btn_snapshot.setObjectName("Small")
        self.btn_snapshot.setIcon(make_icon("camera", theme.TEXT_DIM, size=18))
        self.btn_snapshot.setIconSize(QSize(18, 18))
        self.btn_snapshot.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_snapshot.clicked.connect(self._save_snapshot)
        head.addWidget(self.btn_snapshot)
        left.addLayout(head)

        self.video = VideoView(api)
        left.addWidget(self.video, 1)

        # 画面按 16:9 封顶，多出来的高度给演示指引，避免上下大黑边。
        self.guide = QWidget()
        # 2×2：单行四个时每格只剩约 75 px 文字宽度，放大字号后必然截断
        guide_lay = QGridLayout(self.guide)
        guide_lay.setContentsMargins(0, 0, 0, 0)
        guide_lay.setHorizontalSpacing(10)
        guide_lay.setVerticalSpacing(8)
        for step, (icon, color, action, result) in enumerate((
            ("finger", theme.FEAT_FINGER, L("按压指纹", "Press finger"), L("识别开锁", "Unlock")),
            ("shield", theme.FEAT_INTRUSION, L("走近镜头", "Approach"), L("人员识别", "Person detect")),
            ("box", theme.FEAT_PACKAGE, L("放下纸箱", "Drop a box"), L("快递识别", "Parcel detect")),
            ("mic", theme.FEAT_TALK, L("点击对讲", "Tap intercom"), L("AI 应答", "AI replies")),
        ), start=1):
            guide_lay.addWidget(self._guide_step(step, icon, color, action, result),
                                (step - 1) // 2, (step - 1) % 2)
        left.addWidget(self.guide)

        actions = QHBoxLayout()
        actions.setSpacing(12)

        self.btn_talk = QPushButton("语音对讲")
        self.btn_talk.setObjectName("Action")
        self.btn_talk.setIconSize(QSize(28, 28))
        self.btn_talk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_talk.clicked.connect(lambda: self._talk(not self._talking))
        self.btn_talk.setEnabled(False)
        actions.addWidget(self.btn_talk, 1)

        self.btn_recording = QPushButton("开始录像")
        self.btn_recording.setObjectName("Action")
        self.btn_recording.setIconSize(QSize(28, 28))
        self.btn_recording.setCheckable(True)
        self.btn_recording.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_recording.clicked.connect(self._toggle_recording)
        actions.addWidget(self.btn_recording, 1)

        self.btn_unlock = QPushButton("解锁")
        self.btn_unlock.setObjectName("Primary")
        self.btn_unlock.setProperty("big", True)
        self.btn_unlock.setIcon(make_icon("unlock", "#07231C", size=28))
        self.btn_unlock.setIconSize(QSize(28, 28))
        self.btn_unlock.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_unlock.clicked.connect(self._on_unlock_clicked)
        actions.addWidget(self.btn_unlock, 1)
        left.addLayout(actions)

        foot = QHBoxLayout()
        foot.setSpacing(12)
        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:15px;")
        self.hint.setWordWrap(True)
        foot.addWidget(self.hint, 1)
        self.conn_label = QLabel("visiond 未连接")
        self.conn_label.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:15px;")
        self.conn_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        foot.addWidget(self.conn_label)
        left.addLayout(foot)

        root.addLayout(left, 16)

        # ================= 右：功能磁贴 + 访客动态 =================
        side = QVBoxLayout()
        side.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self.tile_lock = FeatureTile("lock", "智能门锁", theme.FEAT_LOCK)
        self.tile_finger = FeatureTile("finger", "指纹识别", theme.FEAT_FINGER)
        self.tile_video = FeatureTile("video", "视频录制", theme.FEAT_VIDEO)
        self.tile_intrusion = FeatureTile("shield", "入侵检测", theme.FEAT_INTRUSION)
        self.tile_package = FeatureTile("box", "快递识别", theme.FEAT_PACKAGE)
        self.tile_talk = FeatureTile("mic", "语音对讲", theme.FEAT_TALK)
        tiles = (self.tile_lock, self.tile_finger, self.tile_video,
                 self.tile_intrusion, self.tile_package, self.tile_talk)
        for i, tile in enumerate(tiles):
            grid.addWidget(tile, i // 2, i % 2)
        side.addLayout(grid)

        self.lock_state = self.tile_lock.headline
        self.arm_state = self.tile_lock.detail
        self.tile_lock.set_state("已上锁", theme.TEXT, "锁体　连接中")

        self.fingerprint_state = self.tile_finger.tag
        self.fingerprint_result = self.tile_finger.detail
        self.tile_finger.set_state("请按压指纹", theme.TEXT, "指纹模块连接中")
        self.tile_finger.set_tag("连接中", theme.WARN)
        self.tile_finger.clicked.connect(self.goto_settings.emit)

        self.tile_video.set_state("未录像", theme.TEXT, "人员 / 包裹出现自动录制")
        self.tile_video.set_tag("LIVE", theme.FEAT_VIDEO)
        self.tile_video.clicked.connect(self.goto_events.emit)

        self.tile_intrusion.set_state("守护中", theme.OK, "今日暂无异常逗留")
        self.tile_intrusion.clicked.connect(self.goto_events.emit)

        self.tile_package.set_state("暂无包裹", theme.TEXT, "今日暂无快递")
        self.tile_package.clicked.connect(self.goto_events.emit)

        self.tile_talk.set_state("未接入", theme.TEXT_MUTED, "AI 语音助手代为应答")
        self.tile_talk.clicked.connect(self._on_talk_tile)

        # ---- 访客动态：最近事件 / 当前对话 ----
        ev_card, ev_lay = card(spacing=8, margins=(16, 12, 16, 12))
        ev_head = QHBoxLayout()
        ev_head.setSpacing(6)
        ev_title = card_title("访客动态")
        ev_title.setStyleSheet(f"color:{theme.TEXT};font-size:15px;font-weight:600;")
        ev_head.addWidget(ev_title)
        ev_head.addStretch(1)
        self.btn_show_events = QPushButton("最近事件")
        self.btn_show_events.setObjectName("Small")
        self.btn_show_events.setCheckable(True)
        self.btn_show_events.setChecked(True)
        self.btn_show_events.clicked.connect(lambda: self._show_activity("events"))
        ev_head.addWidget(self.btn_show_events)
        self.btn_show_conversation = QPushButton("当前对话")
        self.btn_show_conversation.setObjectName("Small")
        self.btn_show_conversation.setCheckable(True)
        self.btn_show_conversation.clicked.connect(lambda: self._show_activity("conversation"))
        ev_head.addWidget(self.btn_show_conversation)
        more = QPushButton("全部 ›")
        more.setObjectName("Small")
        more.clicked.connect(self.goto_events.emit)
        ev_head.addWidget(more)
        ev_lay.addLayout(ev_head)

        self.activity_stack = QStackedWidget()
        self.recent = EventList(thumbs=False)
        self.recent.selected.connect(self.replay_requested.emit)
        self.recent_empty = QLabel("暂无记录")
        self.recent_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recent_empty.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:15px;")
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
        self.conversation_status = QLabel("点击「语音对讲」后，这里实时显示识别文字和回复。")
        self.conversation_status.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:14px;")
        self.conversation_text = QPlainTextEdit()
        self.conversation_text.setReadOnly(True)
        self.conversation_text.setPlaceholderText("对话内容会显示在这里")
        self.conversation_text.setStyleSheet(
            f"QPlainTextEdit {{ background:transparent; border:none; "
            f"color:{theme.TEXT}; font-size:15px; }}"
        )
        conversation_lay.addWidget(self.conversation_status)
        conversation_lay.addWidget(self.conversation_text, 1)
        self.activity_stack.addWidget(self.conversation_view)
        ev_lay.addWidget(self.activity_stack, 1)
        self._show_activity("events")
        side.addWidget(ev_card, 1)

        root.addLayout(side, 9)

        self._style_talk_button()
        self._set_recording_state(False, False)

        # 采集状态轮询：3 s 一次，只用一个定时器
        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(self.refresh)
        self._poll.start()

    @staticmethod
    def _guide_step(step: int, icon: str, color: str, action: str, result: str) -> QWidget:
        from .widgets import IconBadge, _rgb

        box = QFrame()
        box.setObjectName("GuideStep")
        box.setStyleSheet(
            f"QFrame#GuideStep {{ background:rgba({_rgb(color)},0.08); "
            f"border:1px dashed rgba({_rgb(color)},0.45); border-radius:12px; }}"
        )
        lay = QHBoxLayout(box)
        lay.setContentsMargins(10, 6, 12, 6)
        lay.setSpacing(10)
        lay.addWidget(IconBadge(icon, color, 30))
        top = QLabel(f"{step}. {action}")
        top.setStyleSheet(f"color:{theme.TEXT};font-size:15px;font-weight:600;background:transparent;border:none;")
        bottom = QLabel(f"→ {result}")
        bottom.setStyleSheet(f"color:{color};font-size:15px;background:transparent;border:none;")
        lay.addWidget(top)
        lay.addWidget(bottom)
        lay.addStretch(1)
        return box

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        width = self.video.width()
        if width > 0:
            self.video.setMaximumHeight(int(width * 9 / 16) + 2)

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
        self._api.events(FEED_LIMIT, on_ok=self._on_recent, on_err=lambda _m: None)

    def _on_fingerprint_health(self, status: dict) -> None:
        if not isinstance(status, dict) or status.get("service") != "fingerprintd":
            self._on_fingerprint_error("服务不可用")
            return
        tile = self.tile_finger
        if not status.get("connected"):
            tile.set_tag("离线", theme.WARN)
            tile.set_state("模块离线", theme.WARN, status.get("error") or "等待指纹模块连接")
            return
        tile.set_tag("在线", theme.OK)
        match = status.get("last_match")
        unlock = status.get("last_unlock")
        if status.get("action") == "enroll":
            tile.set_state("录入中", theme.FEAT_FINGER, str(status.get("step") or "正在录入指纹"))
        elif match and match.get("id") is None:
            tile.set_state("未匹配", theme.WARN, "最近识别：未匹配的指纹")
        elif match:
            name = match.get("name") or "未登记指纹"
            finger = f" · {match['finger']}" if match.get("finger") else ""
            text = L(f"最近识别：{name}{finger}（ID {match['id']}）",
                     f"Last match: {name}{finger} (ID {match['id']})")
            if unlock and unlock.get("ok"):
                text += L(" · 已开锁", " · unlocked")
                tile.set_state(f"{name} · {L('已开锁', 'Unlocked')}", theme.OK, text)
            elif unlock:
                text += f" · {L('未开锁', 'not unlocked')}：{unlock.get('reason') or L('被拒绝', 'rejected')}"
                tile.set_state(f"{name} · {L('未开锁', 'Denied')}", theme.WARN, text)
            else:
                tile.set_state(f"{name} · {L('已识别', 'Matched')}", theme.FEAT_FINGER, text)
        else:
            tile.set_state("请按压指纹", theme.TEXT, "按压指纹即可识别开锁")
        key = repr((match, unlock))
        if match and self._finger_key is not None and key != self._finger_key:
            tile.flash()
        self._finger_key = key

    def _on_fingerprint_error(self, message: str) -> None:
        self.tile_finger.set_tag("不可用", theme.CRIT)
        self.tile_finger.set_state("服务不可用", theme.CRIT, str(message))

    def _on_lock_health(self, status: dict) -> None:
        if not isinstance(status, dict) or status.get("service") != "lockd":
            self._on_lock_health_error("")
            return
        self.lock_available = True
        if status.get("state") == "fault":
            headline, color = "锁体故障", theme.CRIT
        elif status.get("state") == "unlocked":
            headline, color = "已开锁", theme.OK
        elif status.get("locked_out"):
            headline, color = "已锁定", theme.CRIT
        else:
            headline, color = "已上锁", theme.TEXT
        driver = (status.get("driver") or {}).get("driver")
        detail = "锁体　模拟" if driver == "mock" else (f"锁体　{driver}" if driver else "锁体　—")
        error = (status.get("driver") or {}).get("error")
        if error:
            detail = f"锁体　{driver}：{error}"
        self.tile_lock.set_state(headline, color, detail)
        self.tile_lock.set_tag("开" if headline == "已开锁" else "", theme.OK)
        locked_out = bool(status.get("locked_out"))
        if not self._unlocking:
            self.btn_unlock.setEnabled(not locked_out)
            self.btn_unlock.setText("解锁（锁定中）" if locked_out else "解锁")

    def _on_lock_health_error(self, _message: str = "") -> None:
        self.lock_available = False
        self.tile_lock.set_state("状态未知", theme.TEXT_MUTED, "锁体　未接入")
        self.btn_unlock.setEnabled(False)
        self.btn_unlock.setText("解锁（未接入）")

    def _on_health(self, h: Health) -> None:
        if h.detector_error or h.package_detector_error:
            problem = L("检测异常 · ", "Detector error · ")
        elif h.package_detector_enabled and not h.package_detector:
            problem = L("包裹模型未加载 · ", "Parcel model not loaded · ")
        else:
            problem = ""
        self.conn_label.setText(problem.rstrip(" ·"))
        self.conn_label.setStyleSheet(
            f"color:{theme.CRIT if problem else theme.TEXT_MUTED};font-size:15px;"
        )
        self._set_recording_state(h.recording_requested, h.recording_active)
        if h.fps <= 0.1:
            self.tile_video.set_tag("无画面", theme.CRIT)
        else:
            self.tile_video.set_tag("LIVE", theme.FEAT_VIDEO)

    def _on_health_error(self, msg: str) -> None:
        self.conn_label.setText(f"visiond 未连接：{msg}")
        self.conn_label.setStyleSheet(f"color:{theme.CRIT};font-size:15px;")
        self.tile_video.set_tag("离线", theme.CRIT)

    def show_temporary_hint(self, text: str, duration_ms: int = 5000) -> None:
        self._hint_generation += 1
        generation = self._hint_generation
        self.hint.setText(text)
        QTimer.singleShot(
            duration_ms,
            lambda: self.hint.clear()
            if generation == self._hint_generation and self.hint.text() == text else None,
        )

    def _on_recent(self, events: list[Event]) -> None:
        self.recent.set_events(events[:RECENT_LIMIT])
        self.recent_empty.setVisible(not events)
        self.recent.setVisible(bool(events))
        self._update_feature_tiles(events)

    def _update_feature_tiles(self, events: list[Event]) -> None:
        """从最近事件里汇总入侵 / 快递两块磁贴的状态，新事件到达时高亮。"""
        today = datetime.now().date()

        def latest(*kinds: str) -> Event | None:
            hits = [e for e in events if e.kind in kinds and e.ts is not None]
            return max(hits, key=lambda e: e.ts) if hits else None

        def today_count(kind: str) -> int:
            return sum(1 for e in events
                       if e.kind == kind and e.ts is not None and e.ts.date() == today)

        def is_new(kind: str, ev: Event | None) -> bool:
            ts = ev.ts if ev else None
            first = kind not in self._seen
            prev = self._seen.get(kind)
            self._seen[kind] = ts
            return not first and ts is not None and (prev is None or ts > prev)

        def ago(ts: datetime) -> str:
            secs = (datetime.now(ts.tzinfo) - ts).total_seconds()
            if secs < 60:
                return L("刚刚", "just now")
            if secs < 3600:
                return L(f"{int(secs // 60)} 分钟前", f"{int(secs // 60)} min ago")
            return ts.strftime("%H:%M") if ts.date() == today else ts.strftime("%m-%d %H:%M")

        intr = latest("intrusion")
        n = today_count("intrusion")
        if intr is not None and (datetime.now(intr.ts.tzinfo) - intr.ts).total_seconds() < 120:
            self.tile_intrusion.set_state("发现异常逗留", theme.CRIT,
                                          L(f"{ago(intr.ts)} · 今日 {n} 次", f"{ago(intr.ts)} · {n} today"))
        elif intr is not None:
            self.tile_intrusion.set_state("守护中", theme.OK,
                                          L(f"上次逗留 {ago(intr.ts)} · 今日 {n} 次",
                                            f"Last {ago(intr.ts)} · {n} today"))
        else:
            self.tile_intrusion.set_state("守护中", theme.OK, "今日暂无异常逗留")
        self.tile_intrusion.set_tag(L(f"今日 {n}", f"Today {n}") if n else "")
        if is_new("intrusion", intr):
            self.tile_intrusion.flash()

        person = latest("person.detected")
        if is_new("person.detected", person):
            self.tile_video.flash(4000)

        pkg = latest("package.detected")
        picked = latest("package.picked")
        n = today_count("package.detected")
        if pkg is not None and (picked is None or picked.ts < pkg.ts):
            self.tile_package.set_state("门口有包裹", theme.FEAT_PACKAGE,
                                        L(f"{ago(pkg.ts)}送达 · 今日 {n} 件",
                                          f"Delivered {ago(pkg.ts)} · {n} today"))
        elif pkg is not None:
            self.tile_package.set_state("暂无包裹", theme.TEXT,
                                        L(f"上次 {ago(pkg.ts)} · 已取走", f"Last {ago(pkg.ts)} · picked up"))
        else:
            self.tile_package.set_state("暂无包裹", theme.TEXT, "今日暂无快递")
        self.tile_package.set_tag(L(f"今日 {n}", f"Today {n}") if n else "")
        if is_new("package.detected", pkg):
            self.tile_package.flash()

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
        self._restyle(self.btn_recording, "ActionRecOn" if requested else "Action")
        self.btn_recording.setIcon(make_icon(
            "stop" if requested else "rec", theme.CRIT, size=28))
        if active:
            self.recording_pill.set_state("录像中", theme.CRIT)
            self.tile_video.set_state("录像中", theme.CRIT, "分段在线录像 · 可在记录页回放")
        elif requested:
            self.recording_pill.set_state("启动中", theme.WARN)
            self.tile_video.set_state("录像启动中", theme.WARN, "分段在线录像")
        else:
            self.recording_pill.set_state("录像关闭", theme.TEXT_MUTED)
            self.tile_video.set_state("实时预览", theme.TEXT, "人员 / 包裹出现自动录制")

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

    def _on_talk_tile(self) -> None:
        if self.btn_talk.isEnabled():
            self._talk(not self._talking)

    def _style_talk_button(self) -> None:
        on = self._talking
        self.btn_talk.setText("结束对讲" if on else "语音对讲")
        self._restyle(self.btn_talk, "ActionTalkOn" if on else "Action")
        self.btn_talk.setIcon(make_icon("mic", "#1B0B2E" if on else theme.FEAT_TALK, size=28))
        if on:
            self.tile_talk.set_state("对话中", theme.FEAT_TALK, "请对着门口说话，再次点击结束")
            self.tile_talk.set_tag("通话", theme.FEAT_TALK)
        elif self._talk_available:
            self.tile_talk.set_state("待命", theme.TEXT, "点击开始 · AI 代为应答")
            self.tile_talk.set_tag("")
        else:
            self.tile_talk.set_state("未接入", theme.TEXT_MUTED, "需要 audiood 提供 /talk 接口")
            self.tile_talk.set_tag("")

    def _talk(self, on: bool) -> None:
        if on == self._talking:
            return
        self._talking = on
        if on:
            self._conversation_text = ""
            self.conversation_text.clear()
            self.conversation_status.setText("正在连接")
            self._show_activity("conversation")
            self.tile_talk.flash(3000)
        self._style_talk_button()
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
            role = L("你", "You") if item.get("role") == "visitor" else L("助手", "Assistant")
            text = str(item.get("text") or "").strip()
            if item.get("partial"):
                text = f"{text}…" if text else L("正在识别…", "Recognizing…")
            if text:
                lines.append(f"{role}{L('：', ': ')}{text}")
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
        self._talk_available = talk_ok
        self._style_talk_button()
        if not talk_ok:
            self.hint.setText("对讲需要 audiood 提供 /talk 接口；当前后端未接入。")
            self._talk_hint = True
        elif self._talk_hint:
            self.hint.setText("")
            self._talk_hint = False

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
