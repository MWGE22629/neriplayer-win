"""设置页(M3 最小实现 + M4 外观/关于 + M5 音质偏好 + 最近列表数量):
关闭行为 / 播放 / 音质 / 外观 / 关于。

控件组只做控件,改动即时发信号给 MainWindow(由其持久化到 settings.json
并应用);程序化回填(set_xxx)时屏蔽信号避免回环。M4 增加外观(暗色/
亮色,信号同样交 MainWindow 切主题)与关于(版本 / GPL-3.0 / 上游标注)。
M5 增加音质偏好(网易云档位键,B站侧解析时换算),默认无损保持既有行为;
最近列表数量(侧栏「最近」分区条数,默认 8)。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..data.store import DEFAULT_RECENT_MAX, MAX_RECENT_MAX, MIN_RECENT_MAX
from . import theme

_CLOSE_EXIT = "exit"
_CLOSE_TRAY = "tray"
_MODE_ITEMS = [
    ("顺序播放", "sequence"),
    ("随机播放", "shuffle"),
    ("单曲循环", "repeat_one"),
]
_APPEARANCE_ITEMS = [
    ("暗色", "dark"),
    ("亮色", "light"),
]
# 音质偏好取网易云档位键(与 data.store._VALID_PLAY_QUALITIES 对齐),
# 默认 lossless;B站播放时由 selector 换算成对应的 B站偏好键
_QUALITY_ITEMS = [
    ("无损 FLAC", "lossless"),
    ("极高 320K", "exhigh"),
    ("标准 128K", "standard"),
]

_UPSTREAM_URL = "https://github.com/cwuom/NeriPlayer"


class SettingsPage(QWidget):
    """设置页;*_changed 信号均由用户交互触发。"""

    close_action_changed = Signal(str)
    play_mode_changed = Signal(str)
    appearance_changed = Signal(str)  # "dark" | "light"
    quality_changed = Signal(str)  # "lossless" | "exhigh" | "standard"
    recent_max_changed = Signal(int)  # 「最近」分区条数(1~50)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        close_group = QGroupBox("关闭行为")
        close_group.setToolTip("点窗口右上角 X 时的行为;托盘菜单「退出」始终真正退出")
        self.exit_radio = QRadioButton("直接退出")
        self.tray_radio = QRadioButton("最小化到托盘")
        close_layout = QHBoxLayout(close_group)
        close_layout.addWidget(self.exit_radio)
        close_layout.addWidget(self.tray_radio)
        close_layout.addStretch(1)

        mode_group = QGroupBox("播放")
        mode_layout_parent = QVBoxLayout(mode_group)
        mode_layout = QHBoxLayout()
        self.mode_combo = QComboBox()
        for label, value in _MODE_ITEMS:
            self.mode_combo.addItem(label, userData=value)
        mode_layout.addWidget(QLabel("播放模式:"))
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addStretch(1)
        mode_layout_parent.addLayout(mode_layout)

        self.recent_max_spin = QSpinBox()
        self.recent_max_spin.setRange(MIN_RECENT_MAX, MAX_RECENT_MAX)
        self.recent_max_spin.setValue(DEFAULT_RECENT_MAX)
        self.recent_max_spin.setToolTip(
            "侧栏「最近」分区记录的最近播放列表条数;\n缩小即时裁剪存量,扩大不回填已裁条目"
        )
        recent_layout = QHBoxLayout()
        recent_layout.addWidget(QLabel("最近列表数量:"))
        recent_layout.addWidget(self.recent_max_spin)
        recent_layout.addStretch(1)
        mode_layout_parent.addLayout(recent_layout)

        quality_group = QGroupBox("音质")
        self.quality_combo = QComboBox()
        for label, value in _QUALITY_ITEMS:
            self.quality_combo.addItem(label, userData=value)
        quality_layout = QHBoxLayout(quality_group)
        quality_layout.addWidget(QLabel("音质偏好:"))
        quality_layout.addWidget(self.quality_combo)
        quality_layout.addStretch(1)

        appearance_group = QGroupBox("外观")
        self.appearance_combo = QComboBox()
        for label, value in _APPEARANCE_ITEMS:
            self.appearance_combo.addItem(label, userData=value)
        appearance_layout = QHBoxLayout(appearance_group)
        appearance_layout.addWidget(QLabel("主题:"))
        appearance_layout.addWidget(self.appearance_combo)
        appearance_layout.addStretch(1)

        about_group = QGroupBox("关于")
        about_layout = QVBoxLayout(about_group)
        about_name = QLabel(f"<b>NeriPlayer Win</b> 版本 {__version__}")
        about_license = QLabel(
            "本程序为自由软件,依据 GNU GPL-3.0 及以后版本授权发布。\n"
            "衍生自 NeriPlayer(cwuom),感谢上游项目。"
        )
        about_license.setWordWrap(True)
        self.about_link = QLabel(f'<a href="{_UPSTREAM_URL}">上游仓库:{_UPSTREAM_URL}</a>')
        self.about_link.setOpenExternalLinks(True)
        self.about_link.setObjectName("aboutLink")
        self.retheme_link()
        about_layout.addWidget(about_name)
        about_layout.addWidget(about_license)
        about_layout.addWidget(self.about_link)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.addWidget(close_group)
        root.addWidget(mode_group)
        root.addWidget(quality_group)
        root.addWidget(appearance_group)
        root.addWidget(about_group)
        root.addStretch(1)

        self.exit_radio.toggled.connect(self._on_close_toggled)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_index_changed)
        self.appearance_combo.currentIndexChanged.connect(
            self._on_appearance_index_changed
        )
        self.quality_combo.currentIndexChanged.connect(
            self._on_quality_index_changed
        )
        self.recent_max_spin.valueChanged.connect(self.recent_max_changed.emit)

        # 默认勾选与持久化默认一致(close_action 默认 tray)
        self.tray_radio.setChecked(True)

    # -- 主题 ------------------------------------------------------------------

    def retheme_link(self) -> None:
        """富文本锚点颜色既不吃 QSS 的 color 也不吃 QPalette.Link,
        唯一可靠路径是内联 HTML style;主题切换时按主色重生成文本。"""
        primary = theme.current_palette()["primary"]
        self.about_link.setText(
            f'<a href="{_UPSTREAM_URL}" style="color:{primary};">'
            f"上游仓库:{_UPSTREAM_URL}</a>"
        )

    # -- 用户交互 --------------------------------------------------------------

    def _on_close_toggled(self, _checked: bool) -> None:
        if self.exit_radio.isChecked():
            self.close_action_changed.emit(_CLOSE_EXIT)
        elif self.tray_radio.isChecked():
            self.close_action_changed.emit(_CLOSE_TRAY)

    def _on_mode_index_changed(self, _index: int) -> None:
        value = self.mode_combo.currentData()
        if isinstance(value, str):
            self.play_mode_changed.emit(value)

    def _on_appearance_index_changed(self, _index: int) -> None:
        value = self.appearance_combo.currentData()
        if isinstance(value, str):
            self.appearance_changed.emit(value)

    def _on_quality_index_changed(self, _index: int) -> None:
        value = self.quality_combo.currentData()
        if isinstance(value, str):
            self.quality_changed.emit(value)

    # -- 程序化回填 ------------------------------------------------------------

    def set_close_action(self, action: str) -> None:
        radio = self.tray_radio if action == _CLOSE_TRAY else self.exit_radio
        radio.blockSignals(True)
        radio.setChecked(True)
        radio.blockSignals(False)

    def set_play_mode(self, mode_value: str) -> None:
        for index in range(self.mode_combo.count()):
            if self.mode_combo.itemData(index) == mode_value:
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(index)
                self.mode_combo.blockSignals(False)
                return

    def set_appearance(self, appearance: str) -> None:
        for index in range(self.appearance_combo.count()):
            if self.appearance_combo.itemData(index) == appearance:
                self.appearance_combo.blockSignals(True)
                self.appearance_combo.setCurrentIndex(index)
                self.appearance_combo.blockSignals(False)
                return

    def set_quality(self, quality: str) -> None:
        for index in range(self.quality_combo.count()):
            if self.quality_combo.itemData(index) == quality:
                self.quality_combo.blockSignals(True)
                self.quality_combo.setCurrentIndex(index)
                self.quality_combo.blockSignals(False)
                return

    def set_recent_max(self, value: int) -> None:
        self.recent_max_spin.blockSignals(True)
        self.recent_max_spin.setValue(value)
        self.recent_max_spin.blockSignals(False)
