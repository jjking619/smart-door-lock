"""界面中英文切换。

源码里的文字仍然写中文（也是测试断言的语言）。英文模式下：

* 首页这类拼接出来的动态文字直接用 ``L(zh, en)`` 二选一；
* 其余文字由 ``install()`` 挂到 Qt 的 setText / 首次 polish / drawText 等入口，
  先整句查表，查不到再按短语替换，最后把全角标点换成半角。

切换语言时主窗口整体重建，因此不需要记住每个控件的原文。
"""

from __future__ import annotations

import re

_lang = "zh"


def lang() -> str:
    return _lang


def set_lang(value: str) -> None:
    global _lang
    _lang = "en" if value == "en" else "zh"


def is_en() -> bool:
    return _lang == "en"


def L(zh: str, en: str) -> str:  # noqa: N802
    return en if _lang == "en" else zh


EN: dict[str, str] = {
    # ---- 顶栏 / 导航 ----
    "门锁操作界面": "Smart Door Lock",
    "AI 智能门锁": "AI Smart Lock",
    "首页": "Home", "记录": "Records", "设置": "Settings",
    "连接中": "Connecting", "离线": "Offline", "在线": "Online",
    "无画面": "No video", "异常": "Error",
    "指纹识别": "Fingerprint", "视频录制": "Recording", "入侵检测": "Intrusion",
    "快递识别": "Parcel", "语音对讲": "Intercom",
    # ---- 事件 ----
    "有人接近": "Person approaching", "异常逗留": "Loitering",
    "疑似快递送达": "Parcel delivered", "包裹已取走": "Parcel picked up",
    "指纹识别成功": "Fingerprint matched", "指纹验证失败": "Fingerprint rejected",
    "录入指纹": "Fingerprint enrolled", "删除指纹": "Delete fingerprint",
    "修改指纹备注": "Fingerprint renamed", "门已打开": "Door opened",
    "门已关闭": "Door closed", "防拆告警": "Tamper alarm", "远程开锁": "Remote unlock",
    "访客请求开锁": "Visitor unlock request", "面板防拆开关触发": "Panel tamper switch triggered",
    "指纹未匹配": "Fingerprint not matched", "门前停留": "Loitering",
    "置信度": "Confidence", "静止": "still", "人离开": "person left",
    "停留": "Dwell", "夜间": "Night", "识别为": "Detected as", "位置": "Position",
    "方式": "Method", "用户": "User", "匹配度": "Score", "指纹 ID": "Finger ID",
    "手指": "Finger", "原因": "Reason", "区域": "Zone", "来源": "Source",
    "物体": "Object", "主人": "Owner", "无路径": "No path",
    "处理": "Decision", "访客原话": "Visitor said", "屋主允许": "Owner allowed",
    "屋主拒绝": "Owner denied", "超时未处理": "Timed out", "（未开锁）": " (not unlocked)",
    "访客语音请求": "Visitor voice request", "访客（屋主确认）": "Visitor (owner approved)",
    "全部": "All", "入侵": "Intrusion", "安全": "Security", "快递": "Parcel", "出入": "Access",
    "系统": "System", "指纹": "Fingerprint", "是": "Yes", "否": "No",
    # ---- 首页 ----
    "门口实时画面": "Live Doorstep View", "录像关闭": "Rec off", "抓拍": "Snapshot",
    "开始录像": "Record", "停止录像": "Stop rec", "解锁": "Unlock",
    "确认解锁": "Confirm unlock", "解锁（未接入）": "Unlock (N/A)",
    "解锁（锁定中）": "Unlock (locked out)", "录像中": "Recording",
    "启动中": "Starting", "停止中": "Stopping", "访客动态": "Visitor Activity",
    "最近事件": "Recent", "当前对话": "Conversation", "全部 ›": "All ›",
    "暂无记录": "No records", "对话内容会显示在这里": "Conversation appears here",
    "保存快照": "Save snapshot", "当前没有画面可抓拍。": "No frame to capture.",
    "已发送开锁请求，等待 lockd 校验…": "Unlock request sent, waiting for lockd…",
    "再次点击确认。真正开锁由 lockd 校验后交给锁体。": "Tap again to confirm. lockd verifies before unlocking.",
    "对讲需要 audiood 提供 /talk 接口；当前后端未接入。": "Intercom needs audiood /talk; backend not connected.",
    "visiond 未连接": "visiond disconnected",
    "开锁动作已完成": "Unlock completed", "开锁请求失败": "Unlock request failed",
    "对讲控制失败": "Intercom control failed", "录像控制失败": "Recording control failed",
    "对话服务不可用": "Conversation service unavailable", "未开锁": "Not unlocked",
    "本机面板": "Local panel", "被拒绝": "Rejected", "已保存到": "Saved to",
    "秒后已自动回锁": "s, then relocked automatically", "保持": "held",
    # ---- 首页磁贴 ----
    "智能门锁": "Smart Lock", "已上锁": "Locked", "已开锁": "Unlocked", "已锁定": "Locked out",
    "锁体故障": "Lock fault", "状态未知": "Unknown", "锁体": "Lock", "模拟": "mock",
    "未接入": "N/A",
    "请按压指纹": "Press finger", "指纹模块连接中": "Connecting module",
    "按压指纹即可识别开锁": "Press a finger to unlock", "模块离线": "Module offline",
    "等待指纹模块连接": "Waiting for module", "录入中": "Enrolling", "正在录入指纹": "Enrolling",
    "未匹配": "No match", "最近识别：未匹配的指纹": "Last: unknown fingerprint",
    "未登记指纹": "Unregistered", "服务不可用": "Unavailable", "不可用": "N/A",
    "实时预览": "Live preview", "未录像": "Not recording", "录像启动中": "Starting",
    "人员 / 包裹出现自动录制": "Auto clips on person / parcel",
    "分段在线录像": "Segmented recording", "分段在线录像 · 可在记录页回放": "Segmented · replay in Records",
    "守护中": "Guarding", "今日暂无异常逗留": "No loitering today", "发现异常逗留": "Loitering!",
    "暂无包裹": "No parcel", "今日暂无快递": "No parcels today", "门口有包裹": "Parcel at door",
    "待命": "Ready", "点击开始 · AI 代为应答": "Tap to start · AI answers",
    "需要 audiood 提供 /talk 接口": "Needs audiood /talk", "对话中": "Talking",
    "请对着门口说话，再次点击结束": "Speak to the door; tap again to end",
    "通话": "Live", "结束对讲": "End call", "AI 语音助手代为应答": "AI assistant answers",
    "检测异常": "detector error", "包裹模型未加载": "parcel model missing", "无模型": "no model",
    "AI 离线": "AI offline", "AI 人员 · 包裹": "AI Person · Parcel",
    "点击「语音对讲」后，这里实时显示识别文字和回复。": "Tap Intercom to see live transcripts and replies here.",
    # ---- 记录页 ----
    "回放片段": "Replay clip", "事件": "Events", "录像": "Recordings",
    "正在载入…": "Loading…", "刷新": "Refresh", "标记全部已读": "Mark all read",
    "暂无事件": "No events", "无录像": "No video", "读取失败": "Load failed",
    "暂无入侵事件": "No intrusion events", "暂无快递事件": "No parcel events",
    "暂无出入记录": "No access records", "暂无系统事件": "No system events",
    "未知日期": "Unknown date", "详情": "Details", "查看详情": "Show details",
    "收起详情": "Hide details", "暂无数据": "No data",
    "今天": "Today", "昨天": "Yesterday", "最近载入": "Recently loaded",
    "暂无安全事件": "No security events", "展开日期分组查看事件": "Expand a date group to view events",
    "此事件没有关联图片或录像": "No image or recording attached",
    "正在载入快照…": "Loading snapshot…", "快照读取失败": "Snapshot load failed",
    "用系统播放器打开": "Open in system player", "请选择记录": "Select a record",
    "播放": "Play", "暂停": "Pause", "正在载入记录…": "Loading records…",
    "暂无可选择的录像": "No recordings to select", "正在载入录像…": "Loading recordings…",
    "图片格式无效": "Invalid image", "暂无录像": "No recordings", "等待载入": "Waiting",
    "此事件没有录像": "No recording for this event",
    "播放失败，可用系统播放器打开": "Playback failed, try the system player",
    "可用系统播放器打开": "Try the system player",
    "无图": "No image", "事件片段": "Event clip", "在线录像": "Live recording",
    "录像地址不可用": "Recording URL unavailable",
    "未安装 QtMultimedia，点击右上角用系统播放器打开。": "QtMultimedia not installed; use the system player (top right).",
    "播放失败": "Playback failed", "（可用系统播放器打开）": " (try the system player)",
    "记录读取失败，请刷新重试": "Failed to load records, please refresh",
    "录像暂时无法播放，正在重试": "Recording not playable yet, retrying",
    "正在重试": "retrying", "下载失败": "Download failed", "回放": "Replay",
    "选择左侧片段": "Pick a clip on the left", "上一个": "Previous", "下一个": "Next",
    "录像片段": "Clips", "个片段": " clips", "条 · 最近": " · last", "次记录": " records",
    "事件读取失败": "Failed to load events", "连接失败": "connection failed",
    # ---- 设置页 ----
    "指纹管理": "Fingerprints", "连接与显示": "Connection / display",
    "诊断与参数": "Diagnostics / parameters", "指纹列表": "Fingerprint list",
    "姓名与手指备注": "Name & finger note", "未填写手指备注": "No finger note",
    "添加：填写信息后点击添加指纹。\n修改：先选中列表中的指纹，再保存备注。":
        "Add: enter the details, then add a fingerprint.\nEdit: select a fingerprint, then save the note.",
    "先选中列表中的指纹，再保存备注或删除；删除会移除模块中的指纹。":
        "Select a fingerprint to edit or delete. Deletion removes it from the sensor.",
    "正在连接指纹模块…": "Connecting fingerprint module…",
    "例如：张三": "e.g. Alice", "例如：右手食指": "e.g. right index finger",
    "姓名": "Name", "手指备注": "Finger note", "添加指纹": "Add fingerprint",
    "取消录入": "Cancel enrollment", "保存备注": "Save note", "删除选中": "Delete selected",
    "先选中下方指纹，再保存备注或删除；删除会移除模块中的指纹。":
        "Select a fingerprint below to edit or delete it. Deletion removes it from the sensor.",
    "正在刷新模块指纹列表…": "Refreshing sensor fingerprints…",
    "刷新请求已提交，正在读取模块…": "Refresh requested, reading sensor…",
    "正在读取模块指纹列表": "Reading sensor fingerprints",
    "刷新中…": "Refreshing…", "刷新完成": "Refresh completed",
    "读取指纹列表失败": "Failed to read fingerprints", "刷新失败": "Refresh failed",
    "请先选择已登记的指纹": "Select a registered fingerprint first",
    "请先选择要删除的指纹": "Select a fingerprint to delete first",
    "确定删除未登记模板": "Delete unregistered template",
    "吗？无法确认此指纹所属人员。": "? The owner of this fingerprint is unknown. ",
    "删除后该指纹将无法识别，且不可恢复。":
        "This fingerprint will no longer be recognized. This cannot be undone.",
    "正在提交删除请求…": "Submitting deletion…",
    "删除请求已提交，等待模块确认…": "Deletion requested, waiting for sensor confirmation…",
    "指纹已删除": "Fingerprint deleted",
    "visiond 地址": "visiond URL", "留空表示未启用鉴权": "Leave empty if auth is disabled",
    "访问令牌": "Access token", "应用并重连": "Apply and reconnect",
    "全屏 kiosk 模式（无标题栏，适合触摸屏）": "Full-screen kiosk mode (for touch screens)",
    "点下方按钮获取": "Tap the button below", "刷新诊断": "Run diagnostics",
    "打开事件目录": "Open events folder", "打开在线录像目录": "Open recordings folder",
    "录入需要按压同一手指两次。录入仅登记身份，不会开锁。": "Press the same finger twice. Enrollment registers identity only; it does not unlock.",
    "查询中…": "Querying…", "未找到 data 目录": "data folder not found",
    "AS608 指纹管理": "AS608 Fingerprints", "连接": "Connection", "显示": "Display",
    "预览清晰度": "Preview quality", "流畅（640 宽）": "Smooth (640 wide)",
    "标准": "Standard", "高清（1080p）": "HD (1080p)",
    "后端参数（只读）": "Backend parameters (read-only)",
    "采集源 VISION_SOURCE": "Source VISION_SOURCE", "推理 VISION_MODEL": "Model VISION_MODEL",
    "推理间隔": "Inference interval", "每 3 帧": "Every 3 frames",
    "刻画区域 doorstep_poly": "Doorstep zone doorstep_poly",
    "包裹区域 ground_poly": "Parcel zone ground_poly", "异常逗留阈值": "Loitering threshold",
    "10 s（夜间 5 s）": "10 s (night 5 s)", "包裹静止阈值": "Parcel still threshold",
    "录像保留": "Recording retention", "事件前 10 s + 后 20 s": "10 s before + 20 s after event",
    "手动启停；5 分钟分段，默认约 5 GB 上限": "Manual; 5-min segments, ~5 GB cap",
    "诊断": "Diagnostics", "指纹服务不可用": "Fingerprint service unavailable",
    "AS608 离线": "AS608 offline", "已占用": "used", "录入中…": "Enrolling…",
    "请先填写姓名": "Please enter a name first", "姓名不能为空": "Name is required",
    "确定删除": "Delete", "的模块指纹模板吗？此操作不可撤销。": "'s fingerprint template? This cannot be undone.",
    "已应用": "Applied", "包裹检测": "parcel detection", "未加载模型：只有录像，没有检测事件": "No model loaded: recording only, no detection events",
    "串口": "Serial", "未登记模板": "Unregistered template", "包裹检测错误": "Parcel detector error",
    "人员检测错误": "Person detector error", "visiond 不可达": "visiond unreachable",
    "检查服务": "Check service", "检查地址": "Check URL", "上次操作失败": "Last operation failed",
    "等待串口设备": "Waiting for serial device", "指纹操作进行中": "Fingerprint operation in progress",
    "录入未开始": "Enrollment not started", "取消失败": "Cancel failed", "保存失败": "Save failed",
    "删除失败": "Delete failed", "未加载": "Not loaded", "已禁用": "Disabled",
    "ROI、停留阈值、布防时段等属于 visiond / lockd 的配置，通过 systemd 环境变量下发，界面不做伪造写入。\n修改方式：编辑 /etc/doorlock/visiond.env 后 sudo systemctl restart visiond":
        "ROI, dwell thresholds and arming schedule are visiond / lockd settings delivered via systemd env files; the UI does not write them.\nTo change: edit /etc/doorlock/visiond.env then sudo systemctl restart visiond",
    # ---- 视频 ----
    "正在连接摄像头…": "Connecting to camera…",
    "画面已停止更新（超过 5 s）": "Video stalled (over 5 s)",
    "摄像头信号中断，正在重连…": "Camera signal lost, reconnecting…",
    # ---- 后端（指纹 / 锁 / 对讲）常见状态 ----
    "准备中": "Preparing", "正在保存指纹模板": "Saving template",
    "正在合并指纹特征": "Merging features", "录入成功，请移开手指": "Enrolled, lift your finger",
    "请按压手指": "Place your finger", "请移开手指": "Lift your finger",
    "请重新按压": "press again", "请再次按压同一手指": "Press the same finger again",
    "未找到匹配指纹": "No matching fingerprint", "该指纹未在界面登记，不能开门": "Fingerprint not registered in UI; door stays locked",
    "刚刚已经开过锁，请稍候": "Just unlocked, please wait", "未接入开锁服务": "Unlock service not connected",
    "请求开锁失败": "Unlock request failed", "锁体未响应": "Lock not responding",
    "两次按压不一致，请重新按压手指": "Presses differ, place your finger again",
    "两次按压差异较大，请移开手指": "Presses differ too much, lift your finger",
    "指纹录入成功": "Fingerprint enrolled", "超时，请重试": " timed out, retry",
    "云端对话失败，已结束": "Cloud conversation failed, ended", "助手正在回复": "Assistant replying",
    "对话出错": "Conversation error", "对话已结束": "Conversation ended", "对话结束": "Conversation ended",
    "正在连接": "Connecting", "正在播放开场提示": "Playing greeting",
    "请说话": "Please speak", "没有听到清晰语音": "No clear speech detected",
    "空闲": "Idle", "请继续说话": "Please keep talking",
    "Coze 余额不足（4028），请充值或等待额度刷新": "Coze credits exhausted (4028); top up or wait for quota refresh",
    "后自动重试": " then auto-retry", "通信中断": "communication lost",
    "指纹串口通信中断": "Fingerprint serial link lost",
    "USB 串口掉线或模块供电不足？": "USB serial dropped or module under-powered?",
    "正在重连": "reconnecting", "未找到串口": "Serial port not found",
}

_CJK = re.compile(r"[\u3400-\u9fff]")
_PUNCT = str.maketrans({"：": ": ", "，": ", ", "（": " (", "）": ")", "；": "; ",
                        "。": ". ", "、": ", ", "！": "! ", "？": "? ", "「": '"',
                        "」": '"', "\u3000": " "})
_phrase_re: re.Pattern | None = None


def _phrases() -> re.Pattern:
    global _phrase_re
    if _phrase_re is None:
        keys = [k for k in EN if len(_CJK.findall(k)) >= 2]
        keys.sort(key=len, reverse=True)
        _phrase_re = re.compile("|".join(re.escape(k) for k in keys))
    return _phrase_re


def tr(text):  # noqa: ANN001, ANN201
    if _lang != "en" or not isinstance(text, str) or not _CJK.search(text):
        return text
    hit = EN.get(text) or EN.get(text.strip())
    if hit:
        return hit
    out = _phrases().sub(lambda m: f" {EN[m.group(0)].strip()} ", text)
    out = re.sub(r"(\d)\s*[条件次个帧]", r"\1", out)
    out = re.sub(r"^共\s*", "Total ", out)
    out = out.translate(_PUNCT)
    out = re.sub(r"[ ]{2,}", " ", out)
    out = re.sub(r" ([)\]:,.;!?])", r"\1", out)
    out = re.sub(r"([(\[]) ", r"\1", out)
    out = out.replace(" 's", "'s")
    return out.strip(" ")


_installed = False


def install(app) -> None:  # noqa: ANN001
    """把翻译挂到 Qt 文本入口。中文模式下 tr 原样返回，挂着也无副作用。"""
    global _installed
    if _installed:
        return
    _installed = True
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import (
        QAbstractButton, QComboBox, QFileDialog, QLabel, QLineEdit,
        QListWidgetItem, QMessageBox, QPlainTextEdit, QWidget,
    )

    def wrap_first(cls, name: str) -> None:
        orig = getattr(cls, name)

        def wrapper(self, text, *args, **kw):  # noqa: ANN001
            return orig(self, tr(text), *args, **kw)
        setattr(cls, name, wrapper)

    for cls, name in ((QLabel, "setText"), (QAbstractButton, "setText"),
                      (QLineEdit, "setPlaceholderText"), (QPlainTextEdit, "setPlaceholderText"),
                      (QWidget, "setWindowTitle"), (QWidget, "setToolTip"),
                      (QListWidgetItem, "setText")):
        wrap_first(cls, name)

    orig_add_items = QComboBox.addItems
    QComboBox.addItems = lambda self, items: orig_add_items(self, [tr(x) for x in items])

    orig_draw = QPainter.drawText

    def draw_text(self, *args):  # noqa: ANN001
        return orig_draw(self, *[tr(a) if isinstance(a, str) else a for a in args])
    QPainter.drawText = draw_text

    for name in ("question", "information", "warning", "critical"):
        orig_box = getattr(QMessageBox, name)

        def box(parent, title, text, *args, _orig=orig_box, **kw):  # noqa: ANN001
            return _orig(parent, tr(title), tr(text), *args, **kw)
        setattr(QMessageBox, name, staticmethod(box))

    orig_save = QFileDialog.getSaveFileName

    def save(parent, caption="", *args, **kw):  # noqa: ANN001
        return orig_save(parent, tr(caption), *args, **kw)
    QFileDialog.getSaveFileName = staticmethod(save)

    class _PolishFilter(QObject):
        """构造函数里直接传入的文字不经过 setText，在控件首次 polish 时补翻译。"""

        def eventFilter(self, obj, ev):  # noqa: N802, ANN001
            if _lang == "en" and ev.type() == QEvent.Type.Polish:
                if isinstance(obj, (QLabel, QAbstractButton)):
                    text = obj.text()
                    if text and _CJK.search(text):
                        obj.setText(text)
                elif isinstance(obj, (QLineEdit, QPlainTextEdit)):
                    text = obj.placeholderText()
                    if text and _CJK.search(text):
                        obj.setPlaceholderText(text)
            return False

    app._i18n_filter = _PolishFilter(app)
    app.installEventFilter(app._i18n_filter)
