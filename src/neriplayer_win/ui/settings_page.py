"""设置页(M3 最小实现):关闭行为 + 播放模式。

只做两组控件,改动即时发信号给 MainWindow(由其持久化到 settings.json
并应用);程序化回填(set_xxx)时屏蔽信号避免回环。
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

_CLOSE_EXIT = "exit"
_CLOSE_TRAY = "tray"
_MODE_ITEMS = [
    ("顺序播放", "sequence"),
    ("随机播放", "shuffle"),
    ("单曲循环", "repeat_one"),
]


class SettingsPage(QWidget):
    """设置页;close_action_changed / play_mode_changed 由用户交互触发。"""

    close_action_changed = Signal(str)
    play_mode_changed = Signal(str)

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

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.addWidget(close_group)
        root.addWidget(mode_group)
        root.addStretch(1)

        self.exit_radio.toggled.connect(self._on_close_toggled)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_index_changed)

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
