from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .icons import tinted_icon

# 播放模式 -> 图标名(顺序=按列表播 / 随机 / 单曲循环)
_MODE_ICON = {
    "sequence": "playlist_play",
    "shuffle": "shuffle",
    "repeat_one": "repeat_one",
}


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class PlayerBar(QWidget):
    """底部播放条:两行布局。

    第一行:当前时间 + 进度条(可拖 seek,独占整行)+ 总时长;
    第二行:歌曲信息 + 播放控制 + 音量。M4 起控制键均为 SVG 图标
    (单色随主题染色,见 ui/icons.py),状态经 tooltip / accessibleName
    暴露给无障碍与测试。
    """

    play_pause_clicked = Signal()
    prev_clicked = Signal()
    next_clicked = Signal()
    seek_requested = Signal(float)  # 秒
    volume_changed = Signal(int)  # 0-100
    mode_clicked = Signal()  # 循环播放模式按钮
    queue_clicked = Signal()  # 打开播放队列窗口

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dragging = False
        self._mode_value = "sequence"
        self._playing = False

        self.track_label = QLabel("未在播放")
        self.track_label.setObjectName("trackLabel")

        self.current_time_label = QLabel("00:00")
        self.total_time_label = QLabel("00:00")

        self.mode_button = QPushButton()
        self.mode_button.setToolTip("播放模式:顺序播放")
        self.mode_button.setAccessibleName("播放模式")
        self.prev_button = QPushButton()
        self.prev_button.setToolTip("上一首")
        self.prev_button.setAccessibleName("上一首")
        self.play_button = QPushButton()
        self.play_button.setObjectName("playButton")
        self.play_button.setToolTip("播放")
        self.play_button.setAccessibleName("播放/暂停")
        self.next_button = QPushButton()
        self.next_button.setToolTip("下一首")
        self.next_button.setAccessibleName("下一首")
        self.queue_button = QPushButton()
        self.queue_button.setToolTip("打开播放队列")
        self.queue_button.setAccessibleName("播放队列")
        for button in (self.mode_button, self.prev_button, self.next_button,
                       self.queue_button):
            button.setIconSize(QSize(20, 20))
            button.setFixedSize(36, 36)
        self.play_button.setIconSize(QSize(22, 22))
        self.play_button.setFixedSize(44, 44)

        self.volume_icon_label = QLabel()
        self.volume_icon_label.setFixedSize(20, 20)
        self.volume_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setToolTip("音量")

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.addWidget(self.current_time_label)
        progress_row.addWidget(self.position_slider, stretch=1)
        progress_row.addWidget(self.total_time_label)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(8)
        controls_row.addWidget(self.track_label)
        controls_row.addStretch(1)
        controls_row.addWidget(self.mode_button)
        controls_row.addWidget(self.prev_button)
        controls_row.addWidget(self.play_button)
        controls_row.addWidget(self.next_button)
        controls_row.addStretch(1)
        controls_row.addWidget(self.queue_button)
        controls_row.addWidget(self.volume_icon_label)
        controls_row.addWidget(self.volume_slider)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 6)
        layout.addLayout(progress_row)
        layout.addLayout(controls_row)

        self.play_button.clicked.connect(self.play_pause_clicked.emit)
        self.prev_button.clicked.connect(self.prev_clicked.emit)
        self.next_button.clicked.connect(self.next_clicked.emit)
        self.mode_button.clicked.connect(self.mode_clicked.emit)
        self.queue_button.clicked.connect(self.queue_clicked.emit)
        self.volume_slider.valueChanged.connect(self.volume_changed.emit)

        self.position_slider.sliderPressed.connect(self._on_slider_pressed)
        self.position_slider.sliderReleased.connect(self._on_slider_released)
        # 点击滑条任意位置直接跳转(QSlider 默认是按格移动,补鼠标事件)
        self.position_slider.mousePressEvent = self._wrap_slider_mouse_press(
            self.position_slider.mousePressEvent
        )

        self._apply_icons()

    # -- 对外状态 ------------------------------------------------------------

    def set_track(self, title: str) -> None:
        self.track_label.setText(title)

    def set_mode(self, mode_value: str, display_name: str = "") -> None:
        """更新播放模式按钮(由 MainWindow 在模式变化时调用)。

        mode_value 为 PlayMode 的枚举值;按钮显示对应图标,
        display_name 进 tooltip 与 accessibleName。
        """
        self._mode_value = mode_value
        name = display_name or mode_value
        self.mode_button.setToolTip(f"播放模式:{name}")
        self.mode_button.setAccessibleName(f"播放模式:{name}")
        self.mode_button.setIcon(tinted_icon(_MODE_ICON.get(mode_value, "playlist_play"), "primary"))

    def set_playing(self, playing: bool) -> None:
        """播放状态切换:按钮图标 播放三角 <-> 暂停双竖线。"""
        self._playing = playing
        self.play_button.setIcon(
            tinted_icon("pause" if playing else "play", "onPrimaryContainer")
        )
        self.play_button.setToolTip("暂停" if playing else "播放")

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

    def retheme(self) -> None:
        """主题切换后重取染色图标(icons 缓存已由 ThemeManager 清空)。"""
        self._apply_icons()

    # -- 图标 ----------------------------------------------------------------

    def _apply_icons(self) -> None:
        self.prev_button.setIcon(tinted_icon("skip_previous"))
        self.next_button.setIcon(tinted_icon("skip_next"))
        self.queue_button.setIcon(tinted_icon("queue_music"))
        self.mode_button.setIcon(
            tinted_icon(_MODE_ICON.get(self._mode_value, "playlist_play"), "primary")
        )
        self.set_playing(self._playing)  # 复用:按当前状态取播放/暂停图标
        self.volume_icon_label.setPixmap(
            tinted_icon("volume_up").pixmap(QSize(18, 18))
        )

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
