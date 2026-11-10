"""配色与字号。

深色主题：门内侧夜间弱光环境不刺眼，也让摄像头画面成为视觉焦点。
所有可点元素 ≥ 44 px，主操作按钮 56 px —— 站着、戴手套也要能一次点中。
"""

from __future__ import annotations

# ---- 颜色 ----
BG = "#0F1216"          # 窗口底色
SURFACE = "#171C23"     # 卡片
SURFACE_2 = "#1F262F"   # 卡片内的次级块 / hover
BORDER = "#2A3340"      # 1 px 分隔线
TEXT = "#E8EEF4"
TEXT_DIM = "#8C9AAB"
TEXT_MUTED = "#5C6A7A"

OK = "#2FD3A8"
ACCENT = "#2FD3A8"
WARN = "#F2B33D"
CRIT = "#F0555A"
INFO = "#4A9EFF"

SEVERITY_COLOR = {
    "info": INFO,
    "warn": WARN,
    "critical": CRIT,
}

EVENT_TITLE = {
    "person.detected": "有人接近",
    "intrusion": "异常逗留",
    "package.detected": "疑似快递送达",
    "package.picked": "包裹已取走",
    "finger.match": "指纹识别成功",
    "finger.reject": "指纹验证失败",
    "finger.enrolled": "录入指纹",
    "finger.deleted": "删除指纹",
    "finger.renamed": "修改指纹备注",
    "door.opened": "门已打开",
    "door.closed": "门已关闭",
    "tamper": "防拆告警",
    "unlock.remote": "远程开锁",
    "unlock.request": "访客请求开锁",
}

# ---- 字体 ----
# 顺序很重要：板子上通常只有文泉驿，PC 上有 Noto CJK。
FONT_STACK = '"Noto Sans CJK SC", "WenQuanYi Zen Hei", "Noto Sans", "DejaVu Sans"'

FONT_SIZE_BASE = 14
FONT_SIZE_TITLE = 20
FONT_SIZE_H1 = 26
FONT_SIZE_HUGE = 40


def qss() -> str:
    return f"""
* {{
    font-family: {FONT_STACK};
    font-size: {FONT_SIZE_BASE}px;
    color: {TEXT};
}}
QWidget#Root {{
    background: {BG};
}}

/* ---------- 顶栏 ---------- */
QWidget#Header {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}
QLabel#Brand {{
    font-size: 18px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel#BrandSub {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}
QLabel#Clock {{
    font-size: 20px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel#ClockSub {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}

/* ---------- 左侧导航 ---------- */
QWidget#Rail {{
    background: {SURFACE};
    border-right: 1px solid {BORDER};
}}
QPushButton#NavBtn {{
    background: transparent;
    border: none;
    border-left: 3px solid transparent;
    color: {TEXT_DIM};
    padding: 12px 0;
    text-align: center;
    font-size: 13px;
}}
QPushButton#NavBtn:hover {{
    background: {SURFACE_2};
    color: {TEXT};
}}
QPushButton#NavBtn:checked {{
    background: {SURFACE_2};
    border-left: 3px solid {ACCENT};
    color: {ACCENT};
    font-weight: 600;
}}

/* ---------- 卡片 ---------- */
QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QLabel#CardTitle {{
    color: {TEXT_DIM};
    font-size: 12px;
    letter-spacing: 1px;
}}
QLabel#CardHeadline {{
    font-size: {FONT_SIZE_H1}px;
    font-weight: 600;
}}
QLabel#PageTitle {{
    font-size: {FONT_SIZE_H1}px;
    font-weight: 600;
}}
QLabel#PageSub {{
    color: {TEXT_MUTED};
    font-size: 13px;
}}
QLabel#Muted {{
    color: {TEXT_DIM};
}}

/* ---------- 视频 ---------- */
QWidget#Video {{
    background: #05070A;
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

/* ---------- 按钮 ---------- */
QPushButton#Primary {{
    background: {ACCENT};
    color: #07231C;
    border: none;
    border-radius: 8px;
    min-height: 56px;
    font-size: 16px;
    font-weight: 600;
    padding: 0 24px;
}}
QPushButton#Primary:hover {{ background: #3FE4B8; }}
QPushButton#Primary:pressed {{ background: #25B891; }}
QPushButton#Primary:disabled {{
    background: {SURFACE_2};
    color: {TEXT_MUTED};
}}

QPushButton#Danger {{
    background: {CRIT};
    color: #2A0708;
    border: none;
    border-radius: 8px;
    min-height: 56px;
    font-size: 16px;
    font-weight: 600;
    padding: 0 24px;
}}
QPushButton#Danger:pressed {{ background: #C44449; }}

QPushButton#Ghost {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 8px;
    min-height: 56px;
    color: {TEXT};
    padding: 0 24px;
}}
QPushButton#Ghost:hover {{ background: {SURFACE_2}; }}
QPushButton#Ghost:pressed {{ background: {BORDER}; }}
QPushButton#Ghost:checked {{
    border: 1px solid {ACCENT};
    color: {ACCENT};
}}

QPushButton#Small {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 6px;
    min-height: 32px;
    color: {TEXT_DIM};
    padding: 0 12px;
}}
QPushButton#Small:hover {{ background: {SURFACE_2}; color: {TEXT}; }}

/* ---------- 列表 ---------- */
QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    border-radius: 8px;
    margin: 0 0 6px 0;
}}
QListWidget::item:selected {{ background: {SURFACE_2}; }}
QListWidget::item:hover {{ background: {SURFACE_2}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 4px;
    min-height: 32px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

/* ---------- 其它 ---------- */
QComboBox {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 6px;
    min-height: 34px;
    padding: 0 10px;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    selection-background-color: {BORDER};
}}
QLineEdit {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 6px;
    min-height: 34px;
    padding: 0 10px;
}}
QCheckBox {{ color: {TEXT_DIM}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {SURFACE_2};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
}}
QSlider::groove:horizontal {{
    height: 4px; background: {BORDER}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 16px; height: 16px; margin: -6px 0;
    background: {ACCENT}; border-radius: 8px;
}}
"""
