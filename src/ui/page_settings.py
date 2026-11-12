"""设置页：本地参数、后端地址、只读的诊断信息。

明确区分两类设置
----------------
* **本地设置**（连接地址、主题、是否全屏、诊断）→ 存在 QSettings，立即生效。
* **后端设置**（ROI 多边形、停留阈值、布防时段）→ 属于 visiond/lockd 的
  环境变量，界面**只读展示 + 提示改哪里**，不在 UI 里伪造一个假的写入通道。
  等后端提供 /config 接口后再接。
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QDesktopServices, QKeySequence, QMouseEvent, QShortcut
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QScroller,
    QTabWidget, QVBoxLayout, QWidget,
)

from . import theme
from .api import ApiClient
from .i18n import L, tr
from .system_input import request_system_input_panel
from .widgets import IconBadge, card, card_title, hline


class SystemInputLineEdit(QLineEdit):
    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.hasFocus()
            and not self.isReadOnly()
            and self.rect().contains(event.position().toPoint())
        ):
            # Some desktop styles only request the panel after the first click.
            request_system_input_panel()


class FingerprintListRow(QFrame):
    def __init__(self, entry: dict, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("FingerprintRow")
        self.setProperty("selected", False)
        self.setAccessibleName(
            f"{entry.get('name') or '未登记模板'}，ID {entry.get('id', '--')}，"
            f"{entry.get('finger') or '未填写手指备注'}"
        )

        registered = bool(entry.get("registered"))
        color = theme.FEAT_FINGER if registered else theme.TEXT_MUTED
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)
        layout.addWidget(IconBadge("finger", color, 34))

        text = QVBoxLayout()
        text.setSpacing(3)
        heading = QHBoxLayout()
        heading.setSpacing(8)
        self.name_label = QLabel(entry.get("name") or "未登记模板")
        self.name_label.setObjectName("FingerprintName")
        self.name_label.setStyleSheet(
            f"color:{theme.TEXT};font-size:15px;font-weight:600;"
        )
        heading.addWidget(self.name_label, 1)

        self.id_label = QLabel(f"ID {entry.get('id', '--')}")
        self.id_label.setObjectName("FingerprintId")
        self.id_label.setStyleSheet(
            f"color:{theme.TEXT_DIM};background:{theme.SURFACE_2};"
            "border-radius:8px;padding:2px 7px;font-size:12px;"
        )
        heading.addWidget(self.id_label)
        text.addLayout(heading)

        self.note_label = QLabel(
            entry.get("finger") or ("已登记 · 未填写备注" if registered else "未登记模板")
        )
        self.note_label.setObjectName("FingerprintNote")
        self.note_label.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:13px;")
        text.addWidget(self.note_label)
        layout.addLayout(text, 1)

        if not registered:
            self.status_label = QLabel("未登记")
            self.status_label.setObjectName("FingerprintStatus")
            self.status_label.setStyleSheet(
                f"color:{theme.WARN};background:rgba(242,179,61,0.12);"
                "border-radius:8px;padding:3px 7px;font-size:12px;"
            )
            layout.addWidget(self.status_label)
        else:
            self.status_label = None

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class SettingsPage(QWidget):
    base_url_changed = Signal(str)
    kiosk_changed = Signal(bool)
    replay_quality_changed = Signal(str)

    def __init__(self, api: ApiClient, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._settings = QSettings("doorlock", "doorui")
        self._fingerprint_connected = False
        self._fingerprint_busy = False
        self._fingerprint_loading = False
        self._fingerprint_submitting = False
        self._delete_confirmation: QFrame | None = None
        self._delete_overlay: QFrame | None = None
        self._delete_location: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        title = QLabel("设置")
        title.setObjectName("PageTitle")
        outer.addWidget(title)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("SettingsTabs")
        outer.addWidget(self.tabs, 1)
        sections = []
        for label in ("指纹管理", "连接与显示", "诊断与参数"):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            QScroller.grabGesture(scroll.viewport(), QScroller.ScrollerGestureType.TouchGesture)
            body = QWidget()
            section = QVBoxLayout(body)
            section.setContentsMargins(0, 14, 8, 0)
            section.setSpacing(14)
            scroll.setWidget(body)
            self.tabs.addTab(scroll, tr(label))
            sections.append(section)
        fp_section, display_section, diagnostics_section = sections

        # ---------------- 指纹管理 ----------------
        fp_card, fp_lay = card()
        fp_lay.addWidget(card_title("AS608 指纹管理"))
        self.fingerprint_status = QLabel("正在连接指纹模块…")
        self.fingerprint_status.setWordWrap(True)
        self.fingerprint_status.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:15px;")
        fp_lay.addWidget(self.fingerprint_status)
        fp_section.addWidget(fp_card)

        fp_columns = QHBoxLayout()
        fp_columns.setSpacing(14)
        list_card, list_lay = card()
        list_header = QHBoxLayout()
        list_header.addWidget(card_title("指纹列表"), 1)
        self.btn_fingerprint_refresh = QPushButton("刷新")
        self.btn_fingerprint_refresh.setObjectName("Ghost")
        self.btn_fingerprint_refresh.setMinimumHeight(40)
        self.btn_fingerprint_refresh.clicked.connect(self._refresh_fingerprint_module)
        list_header.addWidget(self.btn_fingerprint_refresh)
        list_lay.addLayout(list_header)

        self.fingerprint_list = QListWidget()
        self.fingerprint_list.setObjectName("FingerprintList")
        self.fingerprint_list.setMinimumSize(240, 200)
        self.fingerprint_list.setSpacing(4)
        self.fingerprint_list.setWordWrap(True)
        self.fingerprint_list.currentItemChanged.connect(self._on_fingerprint_selected)
        list_lay.addWidget(self.fingerprint_list, 1)
        self.btn_fingerprint_delete = QPushButton("删除选中")
        self.btn_fingerprint_delete.setObjectName("Danger")
        self.btn_fingerprint_delete.setMinimumHeight(44)
        self.btn_fingerprint_delete.clicked.connect(self._delete_fingerprint)
        self.btn_fingerprint_delete.setEnabled(False)
        list_lay.addWidget(self.btn_fingerprint_delete)
        fp_columns.addWidget(list_card, 3)

        edit_card, edit_lay = card()
        edit_card.setMinimumWidth(280)
        edit_lay.addWidget(card_title("姓名与手指备注"))
        fp_form = QFormLayout()
        fp_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        fp_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        fp_form.setVerticalSpacing(10)
        self.fingerprint_name = SystemInputLineEdit()
        self.fingerprint_name.setMaxLength(40)
        self.fingerprint_name.setPlaceholderText("例如：张三")
        self.fingerprint_note = SystemInputLineEdit()
        self.fingerprint_note.setMaxLength(40)
        self.fingerprint_note.setPlaceholderText("例如：右手食指")
        fp_form.addRow("姓名", self.fingerprint_name)
        fp_form.addRow("手指备注", self.fingerprint_note)
        edit_lay.addLayout(fp_form)

        fp_actions = QVBoxLayout()
        fp_actions.setSpacing(10)
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
        for button in (self.btn_fingerprint_enroll, self.btn_fingerprint_cancel,
                       self.btn_fingerprint_rename):
            button.setMinimumHeight(44)
        edit_lay.addLayout(fp_actions)
        edit_tip = QLabel("添加：填写信息后点击添加指纹。\n修改：先选中列表中的指纹，再保存备注。")
        edit_tip.setWordWrap(True)
        edit_tip.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:15px;")
        edit_lay.addWidget(edit_tip)
        edit_lay.addStretch(1)
        fp_columns.addWidget(edit_card, 2)
        fp_section.addLayout(fp_columns, 1)
        self.fingerprint_feedback = QLabel("先选中列表中的指纹，再保存备注或删除；删除会移除模块中的指纹。")
        self.fingerprint_feedback.setWordWrap(True)
        self.fingerprint_feedback.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:15px;")
        fp_section.addWidget(self.fingerprint_feedback)

        # ---------------- 连接 ----------------
        conn_card, conn_lay = card()
        conn_lay.addWidget(card_title("连接"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(10)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.base_edit = SystemInputLineEdit(self._settings.value("base_url", api.base_url, str))
        self.base_edit.setPlaceholderText("http://127.0.0.1:8083")
        form.addRow("visiond 地址", self.base_edit)

        self.token_edit = SystemInputLineEdit(self._settings.value("token", api.token, str))
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setInputMethodHints(
            Qt.InputMethodHint.ImhHiddenText
            | Qt.InputMethodHint.ImhSensitiveData
            | Qt.InputMethodHint.ImhNoPredictiveText
            | Qt.InputMethodHint.ImhNoAutoUppercase
        )
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
        self.conn_hint.setWordWrap(True)
        self.conn_hint.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:14px;")
        conn_lay.addWidget(self.conn_hint)
        display_section.addWidget(conn_card)

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
        display_section.addWidget(disp_card)
        display_section.addStretch(1)

        # ---------------- 后端只读参数 ----------------
        be_card, be_lay = card()
        be_lay.addWidget(card_title("后端参数（只读）"))
        tip = QLabel(
            "ROI、停留阈值、布防时段等属于 visiond / lockd 的配置，"
            "通过 systemd 环境变量下发，界面不做伪造写入。\n"
            "修改方式：编辑 deploy/systemd/doorlock.env 后 "
            "重启对应的 systemd 服务（用户级用 systemctl --user）"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:14px;line-height:150%;")
        be_lay.addWidget(tip)

        grid = QFormLayout()
        grid.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        grid.setVerticalSpacing(8)
        backend_rows = [
            ("采集源 VISION_SOURCE", "/dev/video0"),
            ("推理 VISION_MODEL", "models/yolo11n.rknn"),
            ("推理间隔", "每 3 帧"),
            ("刻画区域 doorstep_poly", "0.05,0.10 → 0.95,1.02"),
            ("包裹检测范围", "默认全画面（VISION_GROUND_POLY 可调整）"),
            ("异常逗留阈值", "10 s（夜间 5 s）"),
            ("包裹静止阈值", "10 s"),
            ("录像保留", "事件前 10 s + 后 20 s"),
            ("在线录像", "手动启停；1 分钟分段，默认约 5 GB 上限"),
        ]
        for label, value in backend_rows:
            v = QLabel(value)
            v.setWordWrap(True)
            v.setStyleSheet(f"color:{theme.TEXT};font-size:15px;")
            grid.addRow(label, v)
        be_lay.addLayout(grid)

        # ---------------- 诊断 ----------------
        diag_card, diag_lay = card()
        diag_lay.addWidget(card_title("诊断"))
        self.diag = QLabel("点下方按钮获取")
        self.diag.setWordWrap(True)
        self.diag.setStyleSheet(
            "font-family:'DejaVu Sans Mono','WenQuanYi Zen Hei Mono',monospace;"
            f"color:{theme.TEXT_DIM};font-size:14px;"
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
        diagnostics_section.addWidget(diag_card)
        diagnostics_section.addWidget(be_card)
        diagnostics_section.addStretch(1)
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
        self._close_delete_confirmation()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._delete_confirmation is not None:
            self._delete_confirmation.setGeometry(self.rect())

    def refresh_fingerprint(self) -> None:
        if self._fingerprint_loading:
            return
        self._fingerprint_loading = True
        self._update_fingerprint_buttons()

        def finish() -> None:
            self._fingerprint_loading = False
            self._update_fingerprint_buttons()

        def templates_ok(items: list[dict]) -> None:
            self._on_fingerprint_templates(items)
            finish()

        def templates_error(message: str) -> None:
            self._fingerprint_message(f"读取指纹列表失败：{message}", error=True)
            finish()

        def health_ok(status: dict) -> None:
            self._on_fingerprint_health(status)
            if self._fingerprint_connected:
                self._api.fingerprint_templates(templates_ok, templates_error)
            else:
                finish()

        def health_error(message: str) -> None:
            self._on_fingerprint_error(message)
            finish()

        self._api.fingerprint_health(health_ok, health_error)

    def _fingerprint_message(self, text: str, *, error: bool = False) -> None:
        self.fingerprint_feedback.setText(text)
        self.fingerprint_feedback.setStyleSheet(
            f"color:{theme.CRIT if error else theme.TEXT_DIM};font-size:15px;"
        )

    def _refresh_fingerprint_module(self) -> None:
        self._fingerprint_submitting = True
        self._fingerprint_message("正在刷新模块指纹列表…")
        self._update_fingerprint_buttons()

        def accepted(_data) -> None:
            self._fingerprint_submitting = False
            self._fingerprint_message("刷新请求已提交，正在读取模块…")
            self.refresh_fingerprint()

        def failed(message: str) -> None:
            self._fingerprint_submitting = False
            self._fingerprint_message(f"刷新失败：{message}", error=True)
            self._update_fingerprint_buttons()

        self._api.fingerprint_refresh(on_ok=accepted, on_err=failed)

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
        self.fingerprint_status.setStyleSheet(f"color:{color};font-size:15px;")
        busy = bool(status.get("action"))
        self._fingerprint_busy = busy
        if status.get("error"):
            self._fingerprint_message(f"上次操作失败：{status['error']}", error=True)
        elif status.get("notice"):
            self._fingerprint_message(str(status["notice"]))
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
        self._fingerprint_busy = False
        self.fingerprint_status.setText(message)
        self.fingerprint_status.setStyleSheet(f"color:{theme.CRIT};font-size:15px;")
        self.btn_fingerprint_enroll.setEnabled(False)
        self.btn_fingerprint_cancel.hide()
        self._fingerprint_message(message, error=True)
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
                label = f"{entry.get('name', '')}  ·  ID {entry['id']}\n{entry.get('finger') or '未填写手指备注'}"
            else:
                label = f"ID {entry['id']}  ·  未登记模板"
            item = QListWidgetItem(tr(label))
            item.setData(Qt.ItemDataRole.UserRole, entry)
            row = FingerprintListRow(entry)
            item.setSizeHint(QSize(0, max(68, row.sizeHint().height())))
            self.fingerprint_list.addItem(item)
            self.fingerprint_list.setItemWidget(item, row)
            if entry["id"] == selected_id:
                selected_row = self.fingerprint_list.count() - 1
        if selected_row is not None:
            self.fingerprint_list.setCurrentRow(selected_row)
        elif selected_id is not None:
            self.fingerprint_name.clear()
            self.fingerprint_note.clear()
        self.fingerprint_list.blockSignals(False)
        self._sync_fingerprint_row_selection()
        self._update_fingerprint_buttons()

    def _on_fingerprint_selected(self, current, _previous) -> None:
        entry = current.data(Qt.ItemDataRole.UserRole) if current else None
        if entry:
            self.fingerprint_name.setText(entry.get("name", ""))
            self.fingerprint_note.setText(entry.get("finger", ""))
        self._sync_fingerprint_row_selection()
        self._update_fingerprint_buttons()

    def _sync_fingerprint_row_selection(self) -> None:
        current = self.fingerprint_list.currentItem()
        for index in range(self.fingerprint_list.count()):
            item = self.fingerprint_list.item(index)
            row = self.fingerprint_list.itemWidget(item)
            if isinstance(row, FingerprintListRow):
                row.set_selected(item is current)

    def _update_fingerprint_buttons(self) -> None:
        current = self.fingerprint_list.currentItem()
        entry = current.data(Qt.ItemDataRole.UserRole) if current else None
        busy = self._fingerprint_busy or self._fingerprint_submitting
        can_manage = bool(entry and entry.get("registered")) and self._fingerprint_connected and not busy
        self.btn_fingerprint_rename.setEnabled(can_manage)
        self.btn_fingerprint_delete.setEnabled(bool(entry) and self._fingerprint_connected and not busy)
        self.btn_fingerprint_enroll.setEnabled(self._fingerprint_connected and not busy)
        self.btn_fingerprint_refresh.setEnabled(not busy and not self._fingerprint_loading)
        self.btn_fingerprint_refresh.setText("刷新中…" if self._fingerprint_loading or busy else "刷新")

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
            self._fingerprint_message("请先选择已登记的指纹", error=True)
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
        if self._delete_confirmation is not None:
            self._delete_confirmation.raise_()
            return
        item = self.fingerprint_list.currentItem()
        entry = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not entry:
            self._fingerprint_message("请先选择要删除的指纹", error=True)
            return
        if entry.get("registered"):
            confirmation = L(
                f"确定删除 {entry.get('name')}（ID {entry['id']}）的模块指纹模板吗？此操作不可撤销。",
                f"Delete the sensor fingerprint for {entry.get('name')} (ID {entry['id']})? "
                "This cannot be undone.",
            )
        else:
            confirmation = L(
                f"确定删除未登记模板（ID {entry['id']}）吗？无法确认此指纹所属人员。"
                "删除后该指纹将无法识别，且不可恢复。",
                f"Delete unregistered template (ID {entry['id']})? Its owner is unknown. "
                "This fingerprint will no longer be recognized. This cannot be undone.",
            )
        if self._delete_overlay is None:
            self._delete_overlay = self._build_delete_confirmation()
        overlay = self._delete_overlay
        overlay.findChild(QLabel, "FingerprintDeleteMessage").setText(confirmation)
        self._delete_location = int(entry["id"])
        self._delete_confirmation = overlay
        overlay.setGeometry(self.rect())
        self.tabs.setEnabled(False)
        overlay.show()
        overlay.raise_()
        overlay.findChild(QPushButton, "Ghost").setFocus(Qt.FocusReason.OtherFocusReason)

    def _build_delete_confirmation(self) -> QFrame:
        # Child overlay avoids native dialog stacking problems on kiosk/embedded Qt platforms.
        overlay = QFrame(self)
        overlay.setObjectName("FingerprintDeleteOverlay")
        overlay.setStyleSheet(
            "QFrame#FingerprintDeleteOverlay { background: rgba(0,0,0,180); }"
        )
        overlay.setGeometry(self.rect())
        overlay_layout = QVBoxLayout(overlay)
        overlay_layout.setContentsMargins(18, 18, 18, 18)
        overlay_layout.addStretch(1)
        panel, panel_layout = card(spacing=18, margins=(24, 24, 24, 24))
        panel.setMaximumWidth(540)
        panel_layout.addWidget(card_title(L("删除指纹", "Delete fingerprint")))
        message = QLabel()
        message.setObjectName("FingerprintDeleteMessage")
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setWordWrap(True)
        message.setStyleSheet(f"font-size:18px;color:{theme.TEXT};")
        panel_layout.addWidget(message)
        actions = QHBoxLayout()
        cancel = QPushButton(L("取消", "Cancel"))
        cancel.setObjectName("Ghost")
        cancel.setProperty("deleteAction", "cancel")
        cancel.clicked.connect(self._close_delete_confirmation)
        confirm = QPushButton(L("确认删除", "Confirm delete"))
        confirm.setObjectName("Danger")
        confirm.setProperty("deleteAction", "confirm")
        confirm.clicked.connect(lambda: self._confirm_fingerprint_delete(self._delete_location))
        actions.addWidget(cancel, 1)
        actions.addWidget(confirm, 1)
        panel_layout.addLayout(actions)
        overlay_layout.addWidget(panel, 0, Qt.AlignmentFlag.AlignHCenter)
        overlay_layout.addStretch(1)
        escape = QShortcut(QKeySequence("Escape"), overlay)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self._close_delete_confirmation)
        return overlay

    def _close_delete_confirmation(self) -> None:
        overlay = self._delete_confirmation
        if overlay is None:
            return
        self._delete_confirmation = None
        self._delete_location = None
        overlay.hide()
        self.tabs.setEnabled(True)
        if self.isVisible():
            self.btn_fingerprint_delete.setFocus(Qt.FocusReason.OtherFocusReason)

    def _confirm_fingerprint_delete(self, location: int | None) -> None:
        if self._delete_confirmation is None or location is None:
            return
        self._close_delete_confirmation()
        self._fingerprint_submitting = True
        self._fingerprint_message("正在提交删除请求…")
        self._update_fingerprint_buttons()

        def accepted(_data) -> None:
            self._fingerprint_submitting = False
            self._fingerprint_message("删除请求已提交，等待模块确认…")
            self.refresh_fingerprint()

        def failed(message: str) -> None:
            self._fingerprint_submitting = False
            self._fingerprint_message(f"删除失败：{message}", error=True)
            self._update_fingerprint_buttons()

        self._api.fingerprint_delete(location, on_ok=accepted, on_err=failed)

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
