"""配色与字号。

深色主题：门内侧夜间弱光环境不刺眼，也让摄像头画面成为视觉焦点。
所有可点元素 ≥ 44 px，主操作按钮 56 px —— 站着、戴手套也要能一次点中。
"""

from __future__ import annotations

# ---- 颜色 ----
BG = "#0B0F15"          # 窗口底色
SURFACE = "#141A23"     # 卡片
SURFACE_2 = "#1C2430"   # 卡片内的次级块 / hover
BORDER = "#263140"      # 1 px 分隔线
TEXT = "#EEF3F8"
TEXT_DIM = "#93A1B2"
TEXT_MUTED = "#5F6D7E"

OK = "#2FD3A8"
ACCENT = "#2FD3A8"
WARN = "#F2B33D"
CRIT = "#F0555A"
INFO = "#4A9EFF"

# 五大功能各自的识别色：展会上远看也能一眼对上"哪块是哪个功能"。
FEAT_LOCK = ACCENT
FEAT_FINGER = "#7C8CFF"
FEAT_VIDEO = INFO
FEAT_INTRUSION = CRIT
FEAT_PACKAGE = WARN
FEAT_TALK = "#B57CFF"

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
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #12302A, stop:0.35 {SURFACE}, stop:1 {SURFACE});
    border-bottom: 1px solid {BORDER};
}}
QLabel#Brand {{
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 2px;
}}
QLabel#BrandSub {{
    color: {TEXT_DIM};
    font-size:14px;
    letter-spacing: 1px;
}}
QLabel#Clock {{
    font-size: 26px;
    font-weight: 700;
    color: {TEXT};
}}
QPushButton#LangBtn {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 18px;
    color: {TEXT};
    font-size: 14px;
    font-weight: 700;
}}
QPushButton#LangBtn:hover {{ border: 1px solid {ACCENT}; color: {ACCENT}; }}
QPushButton#LangBtn:pressed {{ background: {BORDER}; }}
QLabel#ClockSub {{
    color: {TEXT_MUTED};
    font-size:14px;
}}

/* ---------- 左侧导航 ---------- */
QWidget#Rail {{
    background: {SURFACE};
    border-right: 1px solid {BORDER};
}}
QToolButton#NavBtn {{
    background: transparent;
    border: none;
    border-radius: 12px;
    color: {TEXT_DIM};
    padding: 0;
    text-align: center;
    font-size:15px;
}}
QToolButton#NavBtn:hover {{
    background: {SURFACE_2};
    color: {TEXT};
}}
QToolButton#NavBtn:checked {{
    background: rgba(47,211,168,0.14);
    color: {ACCENT};
    font-weight: 600;
}}

/* ---------- 卡片 ---------- */
QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QLabel#CardTitle {{
    color: {TEXT_DIM};
    font-size:14px;
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
    font-size:15px;
}}
QLabel#Muted {{
    color: {TEXT_DIM};
}}

/* ---------- 视频 ---------- */
QWidget#Video {{
    background: #05070A;
    border: 1px solid {BORDER};
    border-radius: 14px;
}}

/* ---------- 首页大按钮 ---------- */
QPushButton#Action {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 14px;
    min-height: 68px;
    font-size: 18px;
    font-weight: 600;
    color: {TEXT};
    padding: 0 18px;
}}
QPushButton#Action:hover {{ background: {SURFACE_2}; }}
QPushButton#Action:pressed {{ background: {BORDER}; }}
QPushButton#Action:disabled {{ color: {TEXT_MUTED}; }}
QPushButton#ActionTalkOn {{
    background: {FEAT_TALK};
    border: none;
    border-radius: 14px;
    min-height: 68px;
    font-size: 18px;
    font-weight: 700;
    color: #1B0B2E;
    padding: 0 18px;
}}
QPushButton#ActionRecOn {{
    background: rgba(240,85,90,0.16);
    border: 1px solid {CRIT};
    border-radius: 14px;
    min-height: 68px;
    font-size: 18px;
    font-weight: 700;
    color: {CRIT};
    padding: 0 18px;
}}
QPushButton#Primary[big="true"], QPushButton#Danger[big="true"] {{
    border-radius: 14px;
    min-height: 68px;
    font-size: 19px;
    font-weight: 700;
}}

/* ---------- 功能磁贴 ---------- */
QFrame#Tile {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QFrame#Tile:hover {{ background: {SURFACE_2}; }}
QLabel#TileTitle {{
    color: {TEXT_DIM};
    font-size:15px;
    font-weight: 600;
    letter-spacing: 1px;
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
QPushButton#Small:checked {{
    background: rgba(47,211,168,0.14);
    border: 1px solid rgba(47,211,168,0.45);
    color: {ACCENT};
}}

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
QScrollArea > QWidget#qt_scrollarea_viewport,
QScrollArea > QWidget#qt_scrollarea_viewport > QWidget {{ background: transparent; }}
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
QTabWidget#SettingsTabs::pane {{
    border: none;
}}
QTabWidget#SettingsTabs QTabBar::tab {{
    background: {SURFACE};
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 10px 24px;
    margin-right: 10px;
    font-size: 15px;
    min-height: 22px;
}}
QTabWidget#SettingsTabs QTabBar::tab:selected {{
    background: {SURFACE_2};
    border-color: {ACCENT};
    color: {ACCENT};
}}
QTabWidget#SettingsTabs QTabBar::tab:hover {{
    color: {TEXT};
}}
QListWidget#FingerprintList {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 6px;
}}
QListWidget#FingerprintList::item {{
    background: transparent;
    border: none;
    padding: 0;
}}
QListWidget#FingerprintList::item:selected {{
    background: transparent;
}}
QFrame#FingerprintRow {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame#FingerprintRow:hover {{ border-color: #3B4D5C; }}
QFrame#FingerprintRow[selected="true"] {{
    background: #1A2A30;
    border-color: {ACCENT};
}}
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
QMessageBox {{
    background: {SURFACE};
    color: {TEXT};
}}
QMessageBox QLabel {{
    background: transparent;
    color: {TEXT};
}}
QMessageBox QPushButton {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: {TEXT};
    min-width: 76px;
    min-height: 36px;
    padding: 0 12px;
}}
QMessageBox QPushButton:hover {{ border-color: {ACCENT}; }}
QSlider::groove:horizontal {{
    height: 4px; background: {BORDER}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 16px; height: 16px; margin: -6px 0;
    background: {ACCENT}; border-radius: 8px;
}}
"""
