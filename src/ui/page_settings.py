"""设置页：本地参数、后端地址、只读的诊断信息。

明确区分两类设置
----------------
* **本地设置**（连接地址、主题、是否全屏、诊断）→ 存在 QSettings，立即生效。
* **后端设置**（ROI 多边形、停留阈值、布防时段）→ 属于 visiond/lockd 的
  环境变量，界面**只读展示 + 提示改哪里**，不在 UI 里伪造一个假的写入通道。
  等后端提供 /config 接口后再接。
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, Signal, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from . import theme
from .api import ApiClient
from .widgets import card, card_title, hline


class SettingsPage(QWidget):
    base_url_changed = Signal(str)
    kiosk_changed = Signal(bool)
    replay_quality_changed = Signal(str)

    def __init__(self, api: ApiClient, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._settings = QSettings("doorlock", "doorui")
        self._fingerprint_connected = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(14)

        title = QLabel("设置")
        title.setObjectName("PageTitle")
        lay.addWidget(title)

        # ---------------- 指纹管理 ----------------
        fp_card, fp_lay = card()
        fp_lay.addWidget(card_title("AS608 指纹管理"))
        self.fingerprint_status = QLabel("正在连接指纹模块…")
        self.fingerprint_status.setWordWrap(True)
        self.fingerprint_status.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:13px;")
        fp_lay.addWidget(self.fingerprint_status)

        self.fingerprint_list = QListWidget()
        self.fingerprint_list.setMinimumHeight(120)
        self.fingerprint_list.currentItemChanged.connect(self._on_fingerprint_selected)

        fp_form = QFormLayout()
        fp_form.setHorizontalSpacing(20)
        fp_form.setVerticalSpacing(8)
        self.fingerprint_name = QLineEdit()
        self.fingerprint_name.setMaxLength(40)
        self.fingerprint_name.setPlaceholderText("例如：张三")
        self.fingerprint_note = QLineEdit()
        self.fingerprint_note.setMaxLength(40)
        self.fingerprint_note.setPlaceholderText("例如：右手食指")
        fp_form.addRow("姓名", self.fingerprint_name)
        fp_form.addRow("手指备注", self.fingerprint_note)
        fp_lay.addLayout(fp_form)

        fp_actions = QHBoxLayout()
        self.btn_fingerprint_enroll = QPushButton("添加指纹")
        self.btn_fingerprint_enroll.setObjectName("Primary")
        self.btn_fingerprint_enroll.clicked.connect(self._enroll_fingerprint)
        self.btn_fingerprint_enroll.setEnabled(False)
        fp_actions.addWidget(self.btn_fingerprint_enroll)
        self.btn_fingerprint_cancel = QPushButton("取消录入")
        self.btn_fingerprint_cancel.setObjectName("Ghost")
        self.btn_fingerprint_cancel.clicked.connect(self._cancel_fingerprint_enroll)
        self.btn_fingerprint_cancel.hide()
        fp_actions.addWidget(self.btn_fingerprint_cancel)
        self.btn_fingerprint_rename = QPushButton("保存备注")
        self.btn_fingerprint_rename.setObjectName("Ghost")
        self.btn_fingerprint_rename.clicked.connect(self._rename_fingerprint)
        self.btn_fingerprint_rename.setEnabled(False)
        fp_actions.addWidget(self.btn_fingerprint_rename)
        self.btn_fingerprint_delete = QPushButton("删除选中")
        self.btn_fingerprint_delete.setObjectName("Small")
        self.btn_fingerprint_delete.clicked.connect(self._delete_fingerprint)
        self.btn_fingerprint_delete.setEnabled(False)
        fp_actions.addWidget(self.btn_fingerprint_delete)
        self.btn_fingerprint_refresh = QPushButton("刷新")
        self.btn_fingerprint_refresh.setObjectName("Small")
        self.btn_fingerprint_refresh.clicked.connect(self.refresh_fingerprint)
        fp_actions.addWidget(self.btn_fingerprint_refresh)
        fp_lay.addLayout(fp_actions)
        fp_lay.addWidget(self.fingerprint_list)
        lay.addWidget(fp_card)

        # ---------------- 连接 ----------------
        conn_card, conn_lay = card()
        conn_lay.addWidget(card_title("连接"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(10)

        self.base_edit = QLineEdit(self._settings.value("base_url", api.base_url, str))
        self.base_edit.setPlaceholderText("http://127.0.0.1:8083")
        form.addRow("visiond 地址", self.base_edit)

        self.token_edit = QLineEdit(self._settings.value("token", api.token, str))
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("留空表示未启用鉴权")
        form.addRow("访问令牌", self.token_edit)

        conn_lay.addLayout(form)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_apply = QPushButton("应用并重连")
        self.btn_apply.setObjectName("Primary")
        self.btn_apply.setFixedHeight(40)
        self.btn_apply.setMinimumWidth(130)
        self.btn_apply.clicked.connect(self._apply_connection)
        row.addWidget(self.btn_apply)
        conn_lay.addLayout(row)

        self.conn_hint = QLabel("")
        self.conn_hint.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:12px;")
        conn_lay.addWidget(self.conn_hint)
        lay.addWidget(conn_card)

        # ---------------- 显示 ----------------
        disp_card, disp_lay = card()
        disp_lay.addWidget(card_title("显示"))
        self.kiosk_box = QCheckBox("全屏 kiosk 模式（无标题栏，适合触摸屏）")
        self.kiosk_box.setChecked(self._settings.value("kiosk", False, bool))
        self.kiosk_box.toggled.connect(self._on_kiosk)
        disp_lay.addWidget(self.kiosk_box)

        q_row = QHBoxLayout()
        q_row.addWidget(QLabel("预览清晰度"))
        self.quality = QComboBox()
        self.quality.addItems(["流畅（640 宽）", "标准", "高清（1080p）"])
        self.quality.setCurrentIndex(
            {"smooth": 0, "std": 1, "hd": 2}.get(
                self._settings.value("quality", "smooth", str), 0
            )
        )
        self.quality.currentIndexChanged.connect(self._on_quality)
        q_row.addWidget(self.quality)
        q_row.addStretch(1)
        disp_lay.addLayout(q_row)
        lay.addWidget(disp_card)

        # ---------------- 后端只读参数 ----------------
        be_card, be_lay = card()
        be_lay.addWidget(card_title("后端参数（只读）"))
        tip = QLabel(
            "ROI、停留阈值、布防时段等属于 visiond / lockd 的配置，"
            "通过 systemd 环境变量下发，界面不做伪造写入。\n"
            "修改方式：编辑 /etc/doorlock/visiond.env 后 "
            "sudo systemctl restart visiond"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:12px;line-height:150%;")
        be_lay.addWidget(tip)

        grid = QFormLayout()
        grid.setVerticalSpacing(8)
        backend_rows = [
            ("采集源 VISION_SOURCE", "/dev/video0"),
            ("推理 VISION_MODEL", "models/yolo11n.rknn"),
            ("推理间隔", "每 3 帧"),
            ("刻画区域 doorstep_poly", "0.05,0.10 → 0.95,1.02"),
            ("包裹区域 ground_poly", "0.05,0.55 → 0.98,0.98"),
            ("异常逗留阈值", "10 s（夜间 5 s）"),
            ("包裹静止阈值", "10 s"),
            ("录像保留", "事件前 10 s + 后 20 s"),
            ("在线录像", "手动启停；5 分钟分段，默认约 5 GB 上限"),
        ]
        for label, value in backend_rows:
            v = QLabel(value)
            v.setStyleSheet(f"color:{theme.TEXT};font-size:13px;")
            grid.addRow(label, v)
        be_lay.addLayout(grid)
        lay.addWidget(be_card)

        # ---------------- 诊断 ----------------
        diag_card, diag_lay = card()
        diag_lay.addWidget(card_title("诊断"))
        self.diag = QLabel("点下方按钮获取")
        self.diag.setWordWrap(True)
        self.diag.setStyleSheet(
            "font-family:'DejaVu Sans Mono','WenQuanYi Zen Hei Mono',monospace;"
            f"color:{theme.TEXT_DIM};font-size:12px;"
        )
        diag_lay.addWidget(self.diag)

        d_row = QHBoxLayout()
        btn_diag = QPushButton("刷新诊断")
        btn_diag.setObjectName("Small")
        btn_diag.clicked.connect(self.refresh_diagnostics)
        d_row.addWidget(btn_diag)

        btn_open = QPushButton("打开事件目录")
        btn_open.setObjectName("Small")
        btn_open.clicked.connect(self._open_data_dir)
        d_row.addWidget(btn_open)
        btn_recordings = QPushButton("打开在线录像目录")
        btn_recordings.setObjectName("Small")
        btn_recordings.clicked.connect(self._open_recordings_dir)
        d_row.addWidget(btn_recordings)
        d_row.addStretch(1)
        diag_lay.addLayout(d_row)
        lay.addWidget(diag_card)

        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self._fingerprint_timer = QTimer(self)
        self._fingerprint_timer.setInterval(3000)
        self._fingerprint_timer.timeout.connect(self.refresh_fingerprint)

    # ---------------- 交互 ----------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._fingerprint_timer.start()
        self.refresh_fingerprint()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._fingerprint_timer.stop()
        super().hideEvent(event)

    def refresh_fingerprint(self) -> None:
        self._api.fingerprint_health(self._on_fingerprint_health, self._on_fingerprint_error)
        self._api.fingerprint_templates(
            self._on_fingerprint_templates, lambda _message: None
        )

    def _on_fingerprint_health(self, status: dict) -> None:
        if not isinstance(status, dict) or status.get("service") != "fingerprintd":
            self._fingerprint_unavailable("指纹服务不可用")
            return
        self._fingerprint_connected = bool(status.get("connected"))
        if status.get("connected"):
            capacity = int(status.get("library_size") or 0)
            count = int(status.get("template_count") or 0)
            device = str(status.get("device") or "串口")
            text = f"AS608 在线 · {device} · 已占用 {count}/{capacity}"
            color = theme.OK
            if status.get("action"):
                text += f"\n{status.get('step') or '指纹操作进行中'}"
            elif status.get("error"):
                text += f"\n上次操作失败：{status.get('error')}"
                color = theme.WARN
            elif status.get("notice"):
                text += f"\n{status.get('notice')}"
        else:
            text = f"AS608 离线：{status.get('error') or '等待串口设备'}"
            color = theme.WARN
        self.fingerprint_status.setText(text)
        self.fingerprint_status.setStyleSheet(f"color:{color};font-size:13px;")
        busy = bool(status.get("action"))
        self._fingerprint_timer.setInterval(800 if busy else 3000)
        self.btn_fingerprint_enroll.setEnabled(self._fingerprint_connected and not busy)
        self.btn_fingerprint_cancel.setVisible(status.get("action") == "enroll")
        if status.get("action") == "enroll":
            self.btn_fingerprint_enroll.setText("录入中…")
        else:
            self.btn_fingerprint_enroll.setText("添加指纹")
        self._update_fingerprint_buttons()

    def _on_fingerprint_error(self, message: str) -> None:
        self._fingerprint_unavailable(f"指纹服务不可用：{message}")

    def _fingerprint_unavailable(self, message: str) -> None:
        self._fingerprint_connected = False
        self.fingerprint_status.setText(message)
        self.fingerprint_status.setStyleSheet(f"color:{theme.CRIT};font-size:13px;")
        self.btn_fingerprint_enroll.setEnabled(False)
        self.btn_fingerprint_cancel.hide()
        self._update_fingerprint_buttons()

    def _on_fingerprint_templates(self, items: list[dict]) -> None:
        selected = self.fingerprint_list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole)["id"] if selected else None
        # Periodic refresh must not overwrite what the user is typing.
        self.fingerprint_list.blockSignals(True)
        self.fingerprint_list.clear()
        selected_row = None
        for entry in items:
            if entry.get("registered"):
                label = f"ID {entry['id']}  ·  {entry.get('name', '')}  ·  {entry.get('finger', '')}"
            else:
                label = f"ID {entry['id']}  ·  未登记模板"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.fingerprint_list.addItem(item)
            if entry["id"] == selected_id:
                selected_row = self.fingerprint_list.count() - 1
        if selected_row is not None:
            self.fingerprint_list.setCurrentRow(selected_row)
        elif selected_id is not None:
            self.fingerprint_name.clear()
            self.fingerprint_note.clear()
        self.fingerprint_list.blockSignals(False)
        self._update_fingerprint_buttons()

    def _on_fingerprint_selected(self, current, _previous) -> None:
        entry = current.data(Qt.ItemDataRole.UserRole) if current else None
        if entry:
            self.fingerprint_name.setText(entry.get("name", ""))
            self.fingerprint_note.setText(entry.get("finger", ""))
        self._update_fingerprint_buttons()

    def _update_fingerprint_buttons(self) -> None:
        current = self.fingerprint_list.currentItem()
        entry = current.data(Qt.ItemDataRole.UserRole) if current else None
        can_manage = bool(entry and entry.get("registered")) and self._fingerprint_connected
        self.btn_fingerprint_rename.setEnabled(can_manage)
        self.btn_fingerprint_delete.setEnabled(can_manage)

    def _enroll_fingerprint(self) -> None:
        name = self.fingerprint_name.text().strip()
        if not name:
            self.fingerprint_status.setText("请先填写姓名")
            return
        answer = QMessageBox.question(
            self,
            "添加指纹",
            "录入需要按压同一手指两次。录入仅登记身份，不会开锁。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        self._api.fingerprint_enroll(
            name,
            self.fingerprint_note.text().strip(),
            on_ok=lambda _data: self.refresh_fingerprint(),
            on_err=lambda message: self.fingerprint_status.setText(f"录入未开始：{message}"),
        )

    def _cancel_fingerprint_enroll(self) -> None:
        self._api.fingerprint_cancel(
            on_err=lambda message: self.fingerprint_status.setText(f"取消失败：{message}")
        )

    def _rename_fingerprint(self) -> None:
        item = self.fingerprint_list.currentItem()
        entry = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not entry or not entry.get("registered"):
            return
        name = self.fingerprint_name.text().strip()
        if not name:
            self.fingerprint_status.setText("姓名不能为空")
            return
        self._api.fingerprint_rename(
            entry["id"],
            name,
            self.fingerprint_note.text().strip(),
            on_ok=lambda _data: self.refresh_fingerprint(),
            on_err=lambda message: self.fingerprint_status.setText(f"保存失败：{message}"),
        )

    def _delete_fingerprint(self) -> None:
        item = self.fingerprint_list.currentItem()
        entry = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not entry or not entry.get("registered"):
            return
        answer = QMessageBox.question(
            self,
            "删除指纹",
            f"确定删除 {entry.get('name')}（ID {entry['id']}）的模块指纹模板吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._api.fingerprint_delete(
            entry["id"],
            on_ok=lambda _data: self.refresh_fingerprint(),
            on_err=lambda message: self.fingerprint_status.setText(f"删除失败：{message}"),
        )

    def _apply_connection(self) -> None:
        base = self.base_edit.text().strip() or "http://127.0.0.1:8083"
        token = self.token_edit.text().strip()
        self._settings.setValue("base_url", base)
        self._settings.setValue("token", token)
        self._api.base_url = base.rstrip("/")
        self._api.token = token
        self.conn_hint.setText(f"已应用：{self._api.base_url}")
        self.base_url_changed.emit(self._api.base_url)

    def _on_kiosk(self, on: bool) -> None:
        self._settings.setValue("kiosk", on)
        self.kiosk_changed.emit(on)

    def _on_quality(self, index: int) -> None:
        key = {0: "smooth", 1: "std", 2: "hd"}.get(index, "smooth")
        self._settings.setValue("quality", key)
        self.replay_quality_changed.emit(key)

    def refresh_diagnostics(self) -> None:
        def ok(h) -> None:
            text = (
                f"visiond  {self._api.base_url}\n"
                f"  ok={h.ok}  fps={h.fps:.1f}  ring={h.ring}  clips={h.clips_active}"
                f"  recording={'on' if h.recording_active else 'off'}\n"
                f"  detector={h.detector or 'none'}  preview_clients={h.preview_clients}\n"
                f"  包裹检测={h.package_detector or ('未加载' if h.package_detector_enabled else '已禁用')}\n"
            )
            if h.package_detector_error:
                text += f"  ⚠ 包裹检测错误：{h.package_detector_error}\n"
            if not h.detector:
                text += "  ⚠ 未加载模型：只有录像，没有检测事件\n"
            if h.detector_error:
                text += f"  ⚠ 人员检测错误：{h.detector_error}\n"
            self.diag.setText(text)

        def err(msg: str) -> None:
            self.diag.setText(f"visiond 不可达：{msg}\n"
                              f"  检查服务：systemctl status visiond\n"
                              f"  检查地址：{self._api.base_url}")

        self.diag.setText("查询中…")
        self._api.health(ok, err)

    def _open_data_dir(self) -> None:
        from pathlib import Path

        for cand in (Path("data"), Path.home() / "smart-door-lock" / "data"):
            if cand.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(cand.resolve())))
                return
        self.diag.setText("未找到 data 目录")

    def _open_recordings_dir(self) -> None:
        from pathlib import Path

        path = Path("data/recordings/continuous")
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
