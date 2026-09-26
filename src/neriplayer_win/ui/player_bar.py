from __future__ import annotations

import math

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QRect,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from ..player.queue import PlayMode
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


class SeekSlider(QSlider):
    """进度滑条:点击任意位置即跳到该处,且可从该处直接继续拖动。

    原生 QSlider 点击槽区是按页步进,且能否拖动取决于 style 的
    hit-test(手柄按下才 setSliderDown)。这里左键按下/移动/释放全部
    自管:按下即按点击位置取值并显式进入拖拽态,移动持续取值,释放时
    setSliderDown(False) 照常发出 sliderReleased。信号语义与原生一致
    (sliderPressed/sliderMoved/sliderReleased),上层 _dragging/seek
    逻辑无需改动。
    """

    def __init__(self) -> None:
        super().__init__(Qt.Orientation.Horizontal)

    def _set_value_at(self, x: float) -> None:
        self.setValue(QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(),
            int(x), max(self.width() - 1, 1),
        ))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.isEnabled()
            and self.maximum() > self.minimum()
        ):
            self.setSliderDown(True)
            self._set_value_at(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self.isSliderDown():
            self._set_value_at(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.isSliderDown()
        ):
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class AnimatedIconButton(QPushButton):
    """setIcon 自动带缩放+淡入过渡(借鉴 A 端 PlaybackControlIndicator 的
    AnimatedContent:新图标 scaleIn+fadeIn,旧图标 scaleOut+fadeOut)。

    同一 QIcon 实例视为未变化直接跳过(icons.py 染色缓存命中时语言
    重翻译重设同图不重放动画)。过渡期间自绘:按钮底板照样式原样
    (仅清掉 icon 字段),图标层按进度画新旧的缩放交叉。
    """

    _DURATION_MS = 200  # A 端图标切换量级,快而有感
    _SCALE_FROM = 0.72  # 换入起点 / 换出终点缩放

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_icon: QIcon | None = None
        self._prev_icon: QIcon | None = None
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(self._DURATION_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(lambda _: self.update())
        self._fade.finished.connect(self._on_fade_finished)

    def setIcon(self, icon: QIcon) -> None:  # noqa: N802 - Qt 命名
        if icon is self._current_icon:
            return  # 缓存命中:同图不动画
        self._prev_icon = self._current_icon
        self._current_icon = icon
        super().setIcon(icon)
        if (
            self._prev_icon is None
            or self._prev_icon.isNull()
            or icon is None
            or icon.isNull()
        ):
            return  # 首次设置 / 空图标:直接显示
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _on_fade_finished(self) -> None:
        self._prev_icon = None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        running = (
            self._fade.state() == QAbstractAnimation.State.Running
            and self._prev_icon is not None
        )
        if not running:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.icon = QIcon()  # 底板不含图标,图标层自画
        self.style().drawControl(
            QStyle.ControlElement.CE_PushButton, option, painter, self
        )
        progress = float(self._fade.currentValue() or 0.0)
        contents = self.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, option, self
        )
        icon_size = self.iconSize()
        center = contents.center()
        for icon, alpha, scale in (
            (self._prev_icon, 1.0 - progress, 1.0 - (1.0 - self._SCALE_FROM) * progress),
            (self._current_icon, progress, self._SCALE_FROM + (1.0 - self._SCALE_FROM) * progress),
        ):
            side = max(1, int(icon_size.width() * scale)), max(1, int(icon_size.height() * scale))
            rect = QRect(center.x() - side[0] // 2, center.y() - side[1] // 2, side[0], side[1])
            painter.setOpacity(alpha)
            icon.paint(painter, rect)


class _CoverLabel(QLabel):
    """封面标签:新封面到达时旧封面在上层淡出(200ms 交叉淡化,新图
    已在下层就位);清空回占位图延迟 900ms(A 端 MiniPlayer 防闪思路:
    快速连续切歌时旧封面始终在场,不闪灰底)。

    crossfade 期间 QLabel 先照常画新 pixmap,再按进度叠画旧 pixmap,
    旧图淡出即显出新图,无额外合成层。
    """

    _FADE_MS = 200
    _CLEAR_DELAY_MS = 900

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fading_from: QPixmap | None = None  # 淡出中的旧封面
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(self._FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(lambda _: self.update())
        self._fade.finished.connect(self._on_fade_finished)
        self._clear_timer = QTimer(self)
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(self._on_clear_timeout)
        self._pending_clear: QPixmap | None = None

    def set_cover_pixmap(self, pixmap: QPixmap) -> None:
        """展示新封面(与旧封面交叉淡化)。"""
        self._cancel_clear()
        old = self.pixmap()
        self._fading_from = QPixmap(old) if old is not None and not old.isNull() else None
        self.setPixmap(pixmap)
        if self._fading_from is None:
            return
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _on_fade_finished(self) -> None:
        self._fading_from = None
        self.update()

    def set_placeholder(self, pixmap: QPixmap) -> None:
        """直接展示占位图(初始化/主题切换,不动画)。"""
        self._cancel_clear()
        self._fade.stop()
        self.setPixmap(pixmap)

    def cancel_pending_clear(self) -> None:
        """取消挂起的延迟清空(新封面请求已发出,旧封面继续在场)。"""
        self._cancel_clear()

    def schedule_clear(self, placeholder: QPixmap) -> None:
        """安排延迟清空回占位图;期间再来新封面即取消。"""
        self._pending_clear = placeholder
        self._clear_timer.start(self._CLEAR_DELAY_MS)

    def _cancel_clear(self) -> None:
        self._clear_timer.stop()
        self._pending_clear = None

    def _on_clear_timeout(self) -> None:
        placeholder = self._pending_clear
        self._pending_clear = None
        if placeholder is not None:
            self.set_placeholder(placeholder)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().paintEvent(event)  # 当前(新)图由 QLabel 画出
        old = self._fading_from
        if (
            old is None
            or self._fade.state() != QAbstractAnimation.State.Running
        ):
            return
        painter = QPainter(self)
        painter.setOpacity(1.0 - float(self._fade.currentValue() or 0.0))
        # QLabel(AlignCenter): pixmap 居中于 contentsRect
        rect = self.contentsRect()
        x = rect.x() + (rect.width() - old.width()) // 2
        y = rect.y() + (rect.height() - old.height()) // 2
        painter.drawPixmap(x, y, old)


# -- 橡皮筋横滑切歌(A 端 MiniPlayer 手势的桌面移植) -------------------------
# 峰值 52px / 阈值 72px:超过阈值松手切歌,未超过弹回;阻尼为指数衰减,
# 拖得越远越费力,视觉位移永远封顶在峰值附近
_SWIPE_PEAK_PX = 52.0
_SWIPE_THRESHOLD_PX = 72.0
_SWIPE_SPRING_MS = 200


def swipe_resisted_offset(delta_px: float) -> float:
    """原位移 -> 阻尼位移:sign(d)·PEAK·(1-exp(-|d|/PEAK))。"""
    if delta_px == 0:
        return 0.0
    magnitude = _SWIPE_PEAK_PX * (1.0 - math.exp(-abs(delta_px) / _SWIPE_PEAK_PX))
    return math.copysign(magnitude, delta_px)


class _SwipeHost(QWidget):
    """封面+歌曲信息块的宿主:按住横向拖动,松手过阈值切上一首/下一首。

    子控件均为 QLabel(无自身鼠标语义),按下事件自然冒泡到宿主;
    press 后 grabMouse 保证拖出宿主边界仍持续跟踪,松手弹回原位
    (200ms OutCubic)。手势期间宿主整体平移(阻尼位移),切歌信号
    在松手瞬间发出——与 A 端一致:确认感来自回弹,而非先动画再切。
    """

    prev_swiped = Signal()
    next_swiped = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dragging = False
        self._origin = self.pos()
        self._press_x = 0.0
        self._spring = QVariantAnimation(self)
        self._spring.setDuration(_SWIPE_SPRING_MS)
        self._spring.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._spring.valueChanged.connect(self._on_spring)

    # -- 拖动手势(供测试直接驱动的三个入口) --------------------------------

    def _begin_swipe(self, global_x: float) -> None:
        self._dragging = True
        self._origin = self.pos()
        self._press_x = global_x
        self._spring.stop()
        self.grabMouse()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _update_swipe(self, global_x: float) -> None:
        if not self._dragging:
            return
        offset = swipe_resisted_offset(global_x - self._press_x)
        self.move(self._origin.x() + int(round(offset)), self._origin.y())

    def _end_swipe(self, global_x: float) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.releaseMouse()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        raw_delta = global_x - self._press_x
        if raw_delta <= -_SWIPE_THRESHOLD_PX:
            self.prev_swiped.emit()
        elif raw_delta >= _SWIPE_THRESHOLD_PX:
            self.next_swiped.emit()
        # 弹回布局原位:从当前位移渐变归零
        current = self.pos().x() - self._origin.x()
        if current != 0:
            self._spring.stop()
            self._spring.setStartValue(float(current))
            self._spring.setEndValue(0.0)
            self._spring.start()

    def _on_spring(self, value) -> None:
        self.move(self._origin.x() + int(round(float(value))), self._origin.y())

    # -- Qt 鼠标事件 ----------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self._begin_swipe(event.globalPosition().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._dragging:
            self._update_swipe(event.globalPosition().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._end_swipe(event.globalPosition().x())
            event.accept()
            return
        super().mouseReleaseEvent(event)


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
        self._track_set = False  # set_track 后占位文案不再随语言重翻译覆盖
        self._cover_url = ""

        # 封面小图(40x40 圆角;加载中/缺失显示淡色音符占位;
        # 切歌交叉淡化见 _CoverLabel)
        self.cover_label = _CoverLabel()
        self.cover_label.setFixedSize(_COVER_SIZE, _COVER_SIZE)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_loader = CoverLoader(self)
        self.cover_loader.cover_ready.connect(self._on_cover_ready)
        self._show_cover_placeholder()

        self.track_label = ElidedLabel(tr("player.not_playing"))
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
        self.mode_button.setToolTip(tr("player.mode_tooltip", name=PlayMode.SEQUENCE.display_name))
        self.mode_button.setAccessibleName(tr("player.mode_accessible"))
        self.prev_button = QPushButton()
        self.prev_button.setToolTip(tr("player.prev"))
        self.prev_button.setAccessibleName(tr("player.prev_accessible"))
        self.play_button = AnimatedIconButton()
        self.play_button.setObjectName("playButton")
        self.play_button.setToolTip(tr("player.play"))
        self.play_button.setAccessibleName(tr("player.play_accessible"))
        self.next_button = QPushButton()
        self.next_button.setToolTip(tr("player.next"))
        self.next_button.setAccessibleName(tr("player.next_accessible"))
        self.queue_button = QPushButton()
        self.queue_button.setToolTip(tr("player.queue"))
        self.queue_button.setAccessibleName(tr("player.queue_accessible"))
        for button in (self.mode_button, self.prev_button, self.next_button,
                       self.queue_button):
            button.setIconSize(QSize(20, 20))
            button.setFixedSize(36, 36)
        self.play_button.setIconSize(QSize(22, 22))
        self.play_button.setFixedSize(44, 44)

        self.volume_icon_label = QLabel()
        self.volume_icon_label.setFixedSize(20, 20)
        self.volume_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.position_slider = SeekSlider()
        self.position_slider.setRange(0, 0)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setToolTip(tr("player.volume"))

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.addWidget(self.current_time_label)
        progress_row.addWidget(self.position_slider, stretch=1)
        progress_row.addWidget(self.total_time_label)

        # 封面+信息块放进步滑宿主:按住横滑切上一首/下一首(A 端手势)
        self._swipe_host = _SwipeHost()
        host_layout = QHBoxLayout(self._swipe_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(8)
        host_layout.addWidget(self.cover_label)
        host_layout.addWidget(info_container)
        self._swipe_host.prev_swiped.connect(self.prev_clicked.emit)
        self._swipe_host.next_swiped.connect(self.next_clicked.emit)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(8)
        controls_row.addWidget(self._swipe_host)
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
        # 拖动中时间标签跟随(实际 seek 仍在释放时发)
        self.position_slider.sliderMoved.connect(
            lambda value: self.current_time_label.setText(format_seconds(value))
        )

        self._apply_icons()

    # -- 对外状态 ------------------------------------------------------------

    def set_track(self, title: str, artist: str = "") -> None:
        self._track_set = True
        self.track_label.set_text_elided(title)
        self.artist_label.set_text_elided(artist)

    def set_cover(self, url: str) -> None:
        """请求封面;空 URL 延迟 900ms 回占位图(连续切歌不闪灰底)。
        回调按 url 比对丢弃过期结果。"""
        self._cover_url = url
        if not url:
            self.cover_label.schedule_clear(self._placeholder_pixmap())
            return
        self.cover_label.cancel_pending_clear()  # 新封面到达前取消挂起的清理
        self.cover_loader.request(url)

    def _on_cover_ready(self, url: str, data: bytes) -> None:
        if url != self._cover_url:
            return  # 已切歌,过期结果丢弃
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.cover_label.set_cover_pixmap(
                rounded_pixmap(pixmap, _COVER_SIZE, _COVER_RADIUS)
            )

    def _placeholder_pixmap(self) -> QPixmap:
        color = QColor(theme.current_palette().get("onSurfaceVariant", "#888888"))
        icon = tinted_icon_with_color("playlist_play", color)
        return icon.pixmap(QSize(28, 28))

    def _show_cover_placeholder(self) -> None:
        self.cover_label.set_placeholder(self._placeholder_pixmap())

    def set_mode(self, mode_value: str, display_name: str = "") -> None:
        """更新播放模式按钮(由 MainWindow 在模式变化时调用)。

        mode_value 为 PlayMode 的枚举值;按钮显示对应图标,
        display_name 进 tooltip 与 accessibleName(缺省按值现取,
        覆盖语言切换后 display_name 未随行的问题)。
        """
        self._mode_value = mode_value
        name = display_name or PlayMode(mode_value).display_name
        self.mode_button.setToolTip(tr("player.mode_tooltip", name=name))
        self.mode_button.setAccessibleName(tr("player.mode_tooltip", name=name))
        self.mode_button.setIcon(tinted_icon(_MODE_ICON.get(mode_value, "playlist_play"), "primary"))

    def set_playing(self, playing: bool) -> None:
        """播放状态切换:按钮图标 播放三角 <-> 暂停双竖线。"""
        self._playing = playing
        self.play_button.setIcon(
            tinted_icon("pause" if playing else "play", "onPrimaryContainer")
        )
        self.play_button.setToolTip(tr("player.pause" if playing else "player.play"))

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

    def retranslate(self) -> None:
        """语言切换后重设全部静态文案(歌曲标题等动态内容不动)。"""
        self.prev_button.setToolTip(tr("player.prev"))
        self.prev_button.setAccessibleName(tr("player.prev_accessible"))
        self.play_button.setAccessibleName(tr("player.play_accessible"))
        self.next_button.setToolTip(tr("player.next"))
        self.next_button.setAccessibleName(tr("player.next_accessible"))
        self.queue_button.setToolTip(tr("player.queue"))
        self.queue_button.setAccessibleName(tr("player.queue_accessible"))
        self.volume_slider.setToolTip(tr("player.volume"))
        self.set_mode(self._mode_value)  # 现取当前模式的译名进 tooltip/accessibleName
        self.set_playing(self._playing)  # 现取播放/暂停 tooltip
        if not self._track_set:
            self.track_label.set_text_elided(tr("player.not_playing"))

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
