from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class PlayerBar(QWidget):
    """底部播放条:两行布局。

    第一行:当前时间 + 进度条(可拖 seek,独占整行)+ 总时长;
    第二行:歌曲信息 + 播放控制 + 音量。
    """

    play_pause_clicked = Signal()
    prev_clicked = Signal()
    next_clicked = Signal()
    seek_requested = Signal(float)  # 秒
    volume_changed = Signal(int)  # 0-100

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dragging = False

        self.track_label = QLabel("未在播放")

        self.current_time_label = QLabel("00:00")
        self.total_time_label = QLabel("00:00")

        self.prev_button = QPushButton("⏮")
        self.play_button = QPushButton("▶")
        self.next_button = QPushButton("⏭")

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_slider.setFixedWidth(90)

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.addWidget(self.current_time_label)
        progress_row.addWidget(self.position_slider, stretch=1)
        progress_row.addWidget(self.total_time_label)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.addWidget(self.track_label)
        controls_row.addStretch(1)
        controls_row.addWidget(self.prev_button)
        controls_row.addWidget(self.play_button)
        controls_row.addWidget(self.next_button)
        controls_row.addStretch(1)
        controls_row.addWidget(self.volume_slider)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 6)
        layout.addLayout(progress_row)
        layout.addLayout(controls_row)

        self.play_button.clicked.connect(self.play_pause_clicked.emit)
        self.prev_button.clicked.connect(self.prev_clicked.emit)
        self.next_button.clicked.connect(self.next_clicked.emit)
        self.volume_slider.valueChanged.connect(self.volume_changed.emit)

        self.position_slider.sliderPressed.connect(self._on_slider_pressed)
        self.position_slider.sliderReleased.connect(self._on_slider_released)
        # 点击滑条任意位置直接跳转(QSlider 默认是按格移动,补鼠标事件)
        self.position_slider.mousePressEvent = self._wrap_slider_mouse_press(
            self.position_slider.mousePressEvent
        )

    # -- 对外状态 ------------------------------------------------------------

    def set_track(self, title: str) -> None:
        self.track_label.setText(title)

    def set_playing(self, playing: bool) -> None:
        self.play_button.setText("⏸" if playing else "▶")

    def set_progress(self, position_s: float, duration_s: float) -> None:
        self.current_time_label.setText(format_seconds(position_s))
        self.total_time_label.setText(format_seconds(duration_s))
        if self._dragging or duration_s <= 0:
            return
        self.position_slider.setRange(0, int(duration_s))
        self.position_slider.setValue(int(position_s))

    def set_volume(self, volume: int) -> None:
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(max(0, min(100, volume)))
        self.volume_slider.blockSignals(False)

    def set_active(self, active: bool) -> None:
        for control in (
            self.prev_button,
            self.play_button,
            self.next_button,
            self.position_slider,
        ):
            control.setEnabled(active)
        self.volume_slider.setEnabled(True)

    # -- 进度拖动 ------------------------------------------------------------

    def _on_slider_pressed(self) -> None:
        self._dragging = True

    def _on_slider_released(self) -> None:
        self._dragging = False
        self.seek_requested.emit(float(self.position_slider.value()))

    def _wrap_slider_mouse_press(self, original_handler):
        def handler(event):
            if event.button() == Qt.MouseButton.LeftButton:
                slider = self.position_slider
                if slider.maximum() > slider.minimum():
                    span = slider.maximum() - slider.minimum()
                    ratio = event.position().x() / max(slider.width(), 1)
                    value = slider.minimum() + int(span * min(max(ratio, 0.0), 1.0))
                    slider.setValue(value)
                    self.seek_requested.emit(float(value))
                    event.accept()
                    return
            original_handler(event)

        return handler
