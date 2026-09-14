from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .covers import CoverLoader
from .icons import tinted_icon, tinted_icon_with_color

# 播放模式 -> 图标名(顺序=按列表播 / 随机 / 单曲循环)
_MODE_ICON = {
    "sequence": "playlist_play",
    "shuffle": "shuffle",
    "repeat_one": "repeat_one",
}

_COVER_SIZE = 40
_COVER_RADIUS = 8


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class ElidedLabel(QLabel):
    """超长文本以 … 截断的自适应标签(宽度变化时重新截断)。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setMinimumWidth(0)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(),
                           self.sizePolicy().verticalPolicy())

    def set_text_elided(self, text: str) -> None:
        self._full_text = text
        self._reapply()

    def _reapply(self) -> None:
        metrics = QFontMetrics(self.font())
        available = max(self.width() - 8, 10)
        self.setText(metrics.elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, available
        ))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._reapply()


def rounded_pixmap(source: QPixmap, size: int, radius: int) -> QPixmap:
    """等比裁成正方形并画圆角。"""
    square = source.scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    cropped = square.copy(
        (square.width() - size) // 2, (square.height() - size) // 2, size, size
    )
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    try:
        path = QPainterPath()
        path.addRoundedRect(0, 0, size, size, radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, cropped)
    finally:
        painter.end()
    return result


class PlayerBar(QWidget):
    """底部播放条:两行布局。

    第一行:当前时间 + 进度条(可拖 seek,独占整行)+ 总时长;
    第二行:封面小图 + 歌名/作者(两行,超长 … 截断)+ 播放控制 + 音量。
    M4 起控制键均为 SVG 图标(单色随主题染色,见 ui/icons.py)。
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
        self._cover_url = ""

        # 封面小图(40x40 圆角;加载中/缺失显示淡色音符占位)
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(_COVER_SIZE, _COVER_SIZE)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_loader = CoverLoader(self)
        self.cover_loader.cover_ready.connect(self._on_cover_ready)
        self._show_cover_placeholder()

        self.track_label = ElidedLabel("未在播放")
        self.track_label.setObjectName("trackLabel")
        self.artist_label = ElidedLabel("")
        self.artist_label.setObjectName("artistLabel")
        info_block = QVBoxLayout()
        info_block.setContentsMargins(0, 0, 0, 0)
        info_block.setSpacing(0)
        info_block.addWidget(self.track_label)
        info_block.addWidget(self.artist_label)
        # 文本块封顶宽度:长标题截断为 …,不把窗口/控制键挤走
        info_container = QWidget()
        info_container.setLayout(info_block)
        info_container.setMaximumWidth(420)
        info_container.setMinimumWidth(120)

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
        controls_row.addWidget(self.cover_label)
        controls_row.addWidget(info_container)
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

    def set_track(self, title: str, artist: str = "") -> None:
        self.track_label.set_text_elided(title)
        self.artist_label.set_text_elided(artist)

    def set_cover(self, url: str) -> None:
        """请求封面;空 URL 直接回到占位图。回调按 url 比对丢弃过期结果。"""
        self._cover_url = url
        if not url:
            self._show_cover_placeholder()
            return
        self.cover_loader.request(url)

    def _on_cover_ready(self, url: str, data: bytes) -> None:
        if url != self._cover_url:
            return  # 已切歌,过期结果丢弃
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.cover_label.setPixmap(
                rounded_pixmap(pixmap, _COVER_SIZE, _COVER_RADIUS)
            )

    def _show_cover_placeholder(self) -> None:
        color = QColor(theme.current_palette().get("onSurfaceVariant", "#888888"))
        icon = tinted_icon_with_color("playlist_play", color)
        self.cover_label.setPixmap(icon.pixmap(QSize(28, 28)))

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
        if not self._cover_url:
            self._show_cover_placeholder()

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
