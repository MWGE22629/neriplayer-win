"""设置页(M3 最小实现 + M4 外观/关于):关闭行为 / 播放模式 / 外观 / 关于。

前两组只做控件,改动即时发信号给 MainWindow(由其持久化到 settings.json
并应用);程序化回填(set_xxx)时屏蔽信号避免回环。M4 增加外观(暗色/
亮色,信号同样交 MainWindow 切主题)与关于(版本 / GPL-3.0 / 上游标注)。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__

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

_UPSTREAM_URL = "https://github.com/cwuom/NeriPlayer"


class SettingsPage(QWidget):
    """设置页;*_changed 信号均由用户交互触发。"""

    close_action_changed = Signal(str)
    play_mode_changed = Signal(str)
    appearance_changed = Signal(str)  # "dark" | "light"

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
        self.mode_combo = QComboBox()
        for label, value in _MODE_ITEMS:
            self.mode_combo.addItem(label, userData=value)
        mode_layout = QHBoxLayout(mode_group)
        mode_layout.addWidget(QLabel("播放模式:"))
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addStretch(1)

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
        about_layout.addWidget(about_name)
        about_layout.addWidget(about_license)
        about_layout.addWidget(self.about_link)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.addWidget(close_group)
        root.addWidget(mode_group)
        root.addWidget(appearance_group)
        root.addWidget(about_group)
        root.addStretch(1)

        self.exit_radio.toggled.connect(self._on_close_toggled)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_index_changed)
        self.appearance_combo.currentIndexChanged.connect(
            self._on_appearance_index_changed
        )

        # 默认勾选与持久化默认一致(close_action 默认 tray)
        self.tray_radio.setChecked(True)

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
