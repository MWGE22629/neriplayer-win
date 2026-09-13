from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)


class PlayerBar(QWidget):
    """底部播放条骨架:歌曲信息 + 进度 + 播放控制 + 音量。

    骨架阶段控件均为占位、不连信号;M1 接入播放内核后启用。
    """

    def __init__(self) -> None:
        super().__init__()
        self.track_label = QLabel("未在播放")

        self.prev_button = QPushButton("⏮")
        self.play_button = QPushButton("▶")
        self.next_button = QPushButton("⏭")

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedWidth(90)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.addWidget(self.track_label)
        layout.addStretch(1)
        layout.addWidget(self.prev_button)
        layout.addWidget(self.play_button)
        layout.addWidget(self.next_button)
        layout.addStretch(1)
        layout.addWidget(self.position_slider, stretch=1)
        layout.addWidget(self.volume_slider)

        for control in (
            self.prev_button,
            self.play_button,
            self.next_button,
            self.position_slider,
            self.volume_slider,
        ):
            control.setEnabled(False)
