"""主题系统(M4):冻结 M3 色板 + 全局 QSS 渲染 + 亮暗切换。

设计数值取自 docs/UI_ASSETS.md(源头:NeriPlayer-Android 的
materialkolor 运行时配色,种子色 #0061A4;Android 侧没有静态色板,
此处一次性冻结为 Python dict,上线前用 material-theme-builder 复核):

- B1 色板:亮暗两套完整 M3 角色 + surface 五级层级;
- B2 设计数值:卡片圆角 20、缩略图 14、小件 8、胶囊全圆;
  间距节奏 4/8/12/16/24;字号四级 18/15/13/11(px,对应 Android dp)。

QSS 模板用 ``@role`` 记号引用色板,re.sub 替换(避开 str.format 与
QSS 花括号选择器的冲突);ThemeManager.apply() 负责写全局样式表并广播
theme_changed,各持图控件自行重取染色图标(见 ui/icons.py)。
"""

from __future__ import annotations

import re
from typing import Mapping

from PySide6.QtCore import QObject, Signal

THEME_DARK = "dark"
THEME_LIGHT = "light"
VALID_THEMES = (THEME_DARK, THEME_LIGHT)
DEFAULT_THEME = THEME_DARK  # 播放器惯例:默认暗色

# ---------------------------------------------------------------------------
# 色板(冻结值;docs/UI_ASSETS.md B1)
# ---------------------------------------------------------------------------

_LIGHT: dict[str, str] = {
    "primary": "#0061A4",
    "onPrimary": "#FFFFFF",
    "primaryContainer": "#D1E4FF",
    "onPrimaryContainer": "#001D36",
    "secondary": "#535F70",
    "secondaryContainer": "#D7E3F7",
    "onSecondaryContainer": "#101C2B",
    "tertiary": "#6B5778",
    "tertiaryContainer": "#F2DAFF",
    "onTertiaryContainer": "#251431",
    "background": "#F8F9FF",
    "onBackground": "#191C20",
    "surface": "#F8F9FF",
    "onSurface": "#191C20",
    "surfaceVariant": "#DFE2EB",
    "onSurfaceVariant": "#43474E",
    "outline": "#73777F",
    "outlineVariant": "#C3C7CF",
    "error": "#BA1A1A",
    "errorContainer": "#FFDAD6",
    # surface 层级(Light):lowest / low / container / high / highest
    "surfaceLowest": "#FFFFFF",
    "surfaceLow": "#F3F4F9",
    "surfaceContainer": "#EEF0F4",
    "surfaceHigh": "#E8E8EE",
    "surfaceHighest": "#E2E2E9",
}

_DARK: dict[str, str] = {
    "primary": "#9ECAFF",
    "onPrimary": "#003258",
    "primaryContainer": "#00497D",
    "onPrimaryContainer": "#D1E4FF",
    "secondary": "#BBC7DB",
    "secondaryContainer": "#3B4858",
    "onSecondaryContainer": "#D7E3F7",
    "tertiary": "#D7BEE4",
    "tertiaryContainer": "#523F5F",
    "onTertiaryContainer": "#F2DAFF",
    "background": "#111418",
    "onBackground": "#E1E2E8",
    "surface": "#111418",
    "onSurface": "#E1E2E8",
    "surfaceVariant": "#43474E",
    "onSurfaceVariant": "#C3C7CF",
    "outline": "#8D9199",
    "outlineVariant": "#43474E",
    "error": "#FFB4AB",
    "errorContainer": "#93000A",
    # surface 层级(Dark)
    "surfaceLowest": "#0C0E12",
    "surfaceLow": "#191C20",
    "surfaceContainer": "#1E2125",
    "surfaceHigh": "#282A2F",
    "surfaceHighest": "#33353A",
}

PALETTES: dict[str, dict[str, str]] = {THEME_DARK: _DARK, THEME_LIGHT: _LIGHT}

# QSS 允许引用的全部记号(防模板里写错色板键名)
_ROLES = tuple(sorted(_DARK))

# ---------------------------------------------------------------------------
# 全局 QSS 模板(@role 记号;设计数值:圆角 20/14/8、间距 4/8/12/16/24、
# 字号 18/15/13/11 —— docs/UI_ASSETS.md B2)
# ---------------------------------------------------------------------------

_QSS_TEMPLATE = """
/* ---- 基底 -------------------------------------------------------------- */
QWidget {
    background: @surface;
    color: @onSurface;
    font-size: 13px;
}
QDialog, QMessageBox {
    background: @surfaceContainer;
}
QLabel {
    background: transparent;
}
QToolTip {
    background: @surfaceHighest;
    color: @onSurface;
    border: 1px solid @outlineVariant;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 11px;
}

/* 页面大标题(登录页等)与小标签 */
QLabel#pageTitle {
    font-size: 18px;
    font-weight: 600;
    color: @onSurface;
}
QLabel#emptyHint {
    color: @onSurfaceVariant;
    font-size: 13px;
}
QLabel#trackLabel {
    font-size: 15px;
    color: @onSurface;
}
QLabel#artistLabel {
    font-size: 13px;
    color: @onSurfaceVariant;
}
/* 关于页链接:跟随主题主色,亮色下依旧可读 */
QLabel#aboutLink {
    color: @primary;
    font-size: 13px;
}

/* 侧栏拖动把手(QSplitter):常显细线,悬停加粗提示可拖 */
QSplitter::handle {
    background: @outlineVariant;
}
QSplitter::handle:hover {
    background: @primary;
}

/* ---- 侧栏 --------------------------------------------------------------- */
/* 树(M5 平台分区):条目样式与列表等价;加载占位为 NoItemFlags,走 :disabled */
/* 分支区保持透明:rootIsDecorated 已关,此条兜底防止个别样式在子节点
   缩进区绘制背景,把条目圆角色块左侧补出一截异色 */
QTreeWidget {
    background: transparent;
    border: none;
    outline: none;
    font-size: 13px;
}
QTreeWidget::branch {
    background: transparent;
}
QTreeWidget::item {
    height: 36px;
    border-radius: 18px;
    margin: 1px 8px;
    padding: 0 12px;
    color: @onSurfaceVariant;
}
QTreeWidget::item:hover {
    background: @surfaceContainer;
    color: @onSurface;
}
QTreeWidget::item:selected {
    background: @secondaryContainer;
    color: @onSecondaryContainer;
}
QTreeWidget::item:disabled {
    color: @outline;
    font-size: 11px;
}

/* 队列窗口列表沿用 QListWidget */
QListWidget {
    background: transparent;
    border: none;
    outline: none;
    font-size: 13px;
}
QListWidget::item {
    height: 36px;
    border-radius: 18px;
    margin: 1px 8px;
    padding: 0 12px;
    color: @onSurfaceVariant;
}
QListWidget::item:hover {
    background: @surfaceContainer;
    color: @onSurface;
}
QListWidget::item:selected {
    background: @secondaryContainer;
    color: @onSecondaryContainer;
}
QListWidget::item:disabled {
    color: @outline;
    font-size: 11px;
}

/* ---- 歌曲表 ------------------------------------------------------------- */
QTableWidget {
    background: @surface;
    alternate-background-color: @surfaceContainer;
    gridline-color: @surfaceHigh;
    border: none;
    selection-background-color: @secondaryContainer;
    selection-color: @onSecondaryContainer;
    font-size: 13px;
}
QTableWidget::item {
    padding: 4px 8px;
}
QTableWidget::item:selected {
    background: @secondaryContainer;
    color: @onSecondaryContainer;
}
QHeaderView {
    background: @surface;
}
QHeaderView::section {
    background: @surface;
    color: @onSurfaceVariant;
    border: none;
    border-bottom: 1px solid @outlineVariant;
    padding: 6px 10px;
    font-size: 11px;
}
QTableCornerButton::section {
    background: @surface;
    border: none;
}

/* ---- 播放条(Mini Player:顶角 20 的浮层卡片) ---------------------------- */
#playerBar {
    background: @surfaceContainer;
    border-top-left-radius: 20px;
    border-top-right-radius: 20px;
}

/* ---- 按钮 --------------------------------------------------------------- */
QPushButton {
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 6px 10px;
    color: @onSurfaceVariant;
    font-size: 13px;
}
QPushButton:hover {
    background: @surfaceHigh;
    color: @onSurface;
}
QPushButton:pressed {
    background: @surfaceHighest;
}
QPushButton:disabled {
    color: @outline;
    background: transparent;
}

/* 主行动按钮(登录页等) */
QPushButton#primaryButton {
    background: @primary;
    color: @onPrimary;
    border-radius: 20px;
    padding: 8px 24px;
    font-size: 15px;
}
QPushButton#primaryButton:hover {
    background: @primary;
    color: @onPrimary;
}
QPushButton#primaryButton:pressed {
    background: @primaryContainer;
    color: @onPrimaryContainer;
}

/* 播放键:M3 tonal 圆钮(图标色 onPrimaryContainer 由 icons.py 染) */
QPushButton#playButton {
    background: @primaryContainer;
    border-radius: 22px;
}
QPushButton#playButton:hover {
    background: @primaryContainer;
}
QPushButton#playButton:pressed {
    background: @primary;
}
QPushButton#playButton:disabled {
    background: @surfaceHighest;
}

/* 搜索来源栏:M3 主标签行风格(借鉴移动端 PrimaryScrollableTabRow)——
   底色透明,选中不铺色,只靠顶部指示线与 primary 文字色 */
QPushButton#searchSourceBtn {
    background: transparent;
    color: @onSurfaceVariant;
    border: none;
    border-radius: 0;
    font-size: 15px;
}
QPushButton#searchSourceBtn:hover {
    background: @surfaceHigh;
    color: @onSurface;
}
QPushButton#searchSourceBtn:checked {
    background: transparent;
    color: @primary;
    font-weight: 600;
}
QPushButton#searchSourceBtn:checked:hover {
    background: @surfaceHigh;
}
#searchSourceIndicator {
    background: @primary;
    border-radius: 1px;
}
QPushButton#searchMoreBtn {
    background: transparent;
    color: @primary;
    border: none;
    border-radius: 0;
    font-size: 13px;
}
QPushButton#searchMoreBtn:hover {
    background: @surfaceContainer;
}

/* ---- 滑条 --------------------------------------------------------------- */
QSlider {
    background: transparent;
    min-height: 20px;
}
QSlider::groove:horizontal {
    height: 4px;
    border-radius: 2px;
    background: @surfaceHighest;
}
QSlider::sub-page:horizontal {
    background: @primary;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 6px;
    background: @primary;
}
QSlider::handle:horizontal:hover {
    background: @primary;
}
QSlider::sub-page:horizontal:disabled {
    background: @outline;
}
QSlider::handle:horizontal:disabled {
    background: @outline;
}

/* ---- 输入/选择件(圆角 8) ----------------------------------------------- */
QLineEdit {
    background: @surfaceContainer;
    border: 1px solid @outlineVariant;
    border-radius: 8px;
    padding: 6px 10px;
    color: @onSurface;
    selection-background-color: @secondaryContainer;
    selection-color: @onSecondaryContainer;
}
QLineEdit:focus {
    border: 1px solid @primary;
}
/* 搜索框(两行高,半宽居中):胶囊造型,字号随高度放大 */
QLineEdit#searchInput {
    background: @surfaceContainer;
    border: 1px solid @outlineVariant;
    border-radius: 14px;
    padding: 8px 16px;
    font-size: 15px;
    color: @onSurface;
    selection-background-color: @secondaryContainer;
    selection-color: @onSecondaryContainer;
}
QLineEdit#searchInput:focus {
    border: 1px solid @primary;
}
QComboBox {
    background: @surfaceContainer;
    border: 1px solid @outlineVariant;
    border-radius: 8px;
    padding: 6px 12px;
    color: @onSurface;
    min-width: 100px;
}
QComboBox:hover {
    border: 1px solid @outline;
}
QComboBox:focus {
    border: 1px solid @primary;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background: @surfaceContainer;
    color: @onSurface;
    border: 1px solid @outlineVariant;
    border-radius: 8px;
    selection-background-color: @secondaryContainer;
    selection-color: @onSecondaryContainer;
}

/* ---- 单选钮 ------------------------------------------------------------- */
QRadioButton {
    spacing: 8px;
    color: @onSurface;
    background: transparent;
}
QRadioButton::indicator {
    width: 18px;
    height: 18px;
    border-radius: 9px;
    border: 2px solid @outline;
    background: transparent;
}
QRadioButton::indicator:checked {
    border: 2px solid @primary;
    background: @primary;
}
QRadioButton::indicator:disabled {
    border: 2px solid @outlineVariant;
}

/* ---- 设置页卡片(QGroupBox,圆角 20) ------------------------------------ */
QGroupBox {
    background: @surfaceContainer;
    border: 1px solid @outlineVariant;
    border-radius: 20px;
    margin-top: 16px;
    padding: 16px 12px 12px 12px;
    font-size: 15px;
    color: @onSurface;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 16px;
    padding: 0 4px;
    color: @onSurfaceVariant;
    font-size: 13px;
}

/* ---- 菜单(托盘右键 / 组合框弹层) ---------------------------------------- */
QMenu {
    background: @surfaceContainer;
    color: @onSurface;
    border: 1px solid @outlineVariant;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item {
    padding: 6px 24px 6px 12px;
    border-radius: 6px;
}
QMenu::item:selected {
    background: @secondaryContainer;
    color: @onSecondaryContainer;
}
QMenu::separator {
    height: 1px;
    background: @outlineVariant;
    margin: 4px 8px;
}

/* ---- 状态栏 / 滚动条 ------------------------------------------------------ */
QStatusBar {
    background: @surfaceLow;
    color: @onSurfaceVariant;
    border-top: 1px solid @outlineVariant;
    font-size: 11px;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: @surfaceHighest;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: @outline;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: transparent;
    height: 8px;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background: @surfaceHighest;
    border-radius: 4px;
    min-width: 24px;
}
QScrollBar::handle:horizontal:hover {
    background: @outline;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
}
"""

_TOKEN_REGEX = re.compile(r"@([A-Za-z]+)")


def build_qss(theme: Mapping[str, str]) -> str:
    """把 @role 记号替换成色板值;未知记号原样保留(便于测试发现拼写错)。"""

    def substitute(match: re.Match[str]) -> str:
        role = match.group(1)
        value = theme.get(role)
        return value if value is not None else match.group(0)

    return _TOKEN_REGEX.sub(substitute, _QSS_TEMPLATE)


# ---------------------------------------------------------------------------
# 当前主题与切换
# ---------------------------------------------------------------------------

_current_name: str = DEFAULT_THEME
# 已实际写入 QApplication 的主题;None = 进程还没写过——冷启动即使
# 就是默认主题(名字未变)也必须写一次 QSS,不能按"未变化"跳过
_applied_name: str | None = None


def current_theme_name() -> str:
    return _current_name


def current_palette() -> dict[str, str]:
    return PALETTES[_current_name]


class ThemeManager(QObject):
    """持当前主题;apply() 设全局 QSS 并广播,由各控件重取染色资源。"""

    theme_changed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name: str = DEFAULT_THEME

    @property
    def name(self) -> str:
        return self._name

    def palette(self) -> dict[str, str]:
        return PALETTES[self._name]

    def qss(self) -> str:
        return build_qss(PALETTES[self._name])

    def apply(self, name: str) -> None:
        """切换主题并即时重渲;主题未变且 QSS 已写过时跳过重设。

        setStyleSheet 会 re-polish 进程内全部存活控件,成本随控件数
        线性增长——同主题重复调用(每新建一个窗口都会 apply 一次)
        直接跳过,避免同进程多窗口构造耗时超线性劣化;但进程首次
        应用(哪怕主题就是默认值)必须真正写一次,否则整套样式缺失。
        """
        if name not in VALID_THEMES:
            raise ValueError(f"未知主题:{name!r}(可选:{VALID_THEMES})")
        global _current_name, _applied_name
        changed = name != _current_name
        self._name = name
        _current_name = name
        if not changed and _applied_name == name:
            return
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(self.qss())
            _applied_name = name
        if changed:
            self.theme_changed.emit(name)
