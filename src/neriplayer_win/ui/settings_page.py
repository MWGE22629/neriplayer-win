"""设置页(M3 最小实现 + M4 外观/关于 + M5 音质偏好/最近列表数量/语言):
关闭行为 / 播放 / 音质 / 外观 / 语言 / 关于。

控件组只做控件,改动即时发信号给 MainWindow(由其持久化到 settings.json
并应用);程序化回填(set_xxx)时屏蔽信号避免回环。M4 增加外观(暗色/
亮色,信号同样交 MainWindow 切主题)与关于(版本 / GPL-3.0 / 上游标注)。
M5 增加音质偏好(网易云档位键,B站侧解析时换算),默认无损保持既有行为;
最近列表数量(侧栏「最近」分区条数,默认 8);界面语言(中/英切换,
滑块控件见 LanguageSwitch,文案表在 neriplayer_win.i18n,重翻译经
retranslate 由 MainWindow 在语言切换时统一驱动)。
"""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, i18n
from ..data.store import DEFAULT_RECENT_MAX, MAX_RECENT_MAX, MIN_RECENT_MAX
from . import theme

_CLOSE_EXIT = "exit"
_CLOSE_TRAY = "tray"
# 播放模式 / 外观 / 音质下拉项:(文案键, 持久化值);回填与重翻译都按键取
_MODE_ITEMS = [
    ("mode.sequence", "sequence"),
    ("mode.shuffle", "shuffle"),
    ("mode.repeat_one", "repeat_one"),
]
_APPEARANCE_ITEMS = [
    ("settings.theme.dark", "dark"),
    ("settings.theme.light", "light"),
]
# 音质偏好取网易云档位键(与 data.store._VALID_PLAY_QUALITIES 对齐),
# 默认 lossless;B站播放时由 selector 换算成对应的 B站偏好键
_QUALITY_ITEMS = [
    ("settings.quality.lossless", "lossless"),
    ("settings.quality.exhigh", "exhigh"),
    ("settings.quality.standard", "standard"),
]

_UPSTREAM_URL = "https://github.com/cwuom/NeriPlayer"


class LanguageSwitch(QWidget):
    """语言切换滑块:「中文 | English」两段,白色滑块左右滑动(动画)。

    自绘控件(M3 风格:胶囊轨道 + 半宽滑块),两侧文字即各自语言的
    名字,永远不翻译——中文用户和英文用户都能认出自己的入口。
    点击左/右半区直接选中对应语言;程序化回填走 set_value(不发信号)。
    配色实时读当前主题色板,主题切换后由上层调 update() 重画即可。
    """

    language_changed = Signal(str)  # "zh" | "en",仅用户交互触发

    _TRACK_HEIGHT = 30
    _PADDING = 3  # 滑块与轨道的内边距

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 初始值跟随当前语言(MainWindow 在构造 SettingsPage 前已设好);
        # 滑块直接落位不播动画
        self._value = i18n.current_language()
        self.setFixedSize(150, self._TRACK_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._thumb_x = self._thumb_target()
        self._animation = QPropertyAnimation(self, b"thumbX", self)
        self._animation.setDuration(200)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    # -- Qt 属性(动画驱动滑块位置)--------------------------------------------

    def get_thumb_x(self) -> float:
        return self._thumb_x

    def set_thumb_x(self, x: float) -> None:
        self._thumb_x = x
        self.update()

    thumbX = Property(float, get_thumb_x, set_thumb_x)

    # -- 对外 -----------------------------------------------------------------

    def value(self) -> str:
        return self._value

    def set_value(self, value: str, animate: bool = True) -> None:
        """程序化设置(回填用);不发 language_changed。"""
        if value not in i18n.VALID_LANGUAGES:
            return
        self._apply(value, animate)

    def _apply(self, value: str, animate: bool) -> None:
        self._value = value
        target = self._thumb_target()
        if animate:
            self._animation.stop()
            self._animation.setStartValue(self._thumb_x)
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self._animation.stop()
            self._thumb_x = target
            self.update()

    # -- 内部 -----------------------------------------------------------------

    def _thumb_target(self) -> float:
        half = self.width() / 2
        return self._PADDING if self._value == "zh" else half + self._PADDING / 2

    def _thumb_width(self) -> float:
        return self.width() / 2 - 1.5 * self._PADDING

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            value = "zh" if event.position().x() < self.width() / 2 else "en"
            if value != self._value:
                self._apply(value, animate=True)
                self.language_changed.emit(value)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        palette = theme.current_palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 轨道:胶囊形 surfaceHighest(M3 分段按钮容器底色)
        track = QPainterPath()
        track.addRoundedRect(
            0.0, 0.0, float(self.width()), float(self.height()),
            self.height() / 2, self.height() / 2,
        )
        painter.fillPath(track, QColor(palette["surfaceHighest"]))
        # 滑块:半宽圆角矩形 secondaryContainer(暗/亮主题下都与轨道有
        # 明确对比;surface 在暗色下与 surfaceHighest 几乎不可分辨)
        thumb = QPainterPath()
        thumb.addRoundedRect(
            self._thumb_x, float(self._PADDING),
            self._thumb_width(), float(self.height() - 2 * self._PADDING),
            (self.height() - 2 * self._PADDING) / 2,
            (self.height() - 2 * self._PADDING) / 2,
        )
        painter.fillPath(thumb, QColor(palette["secondaryContainer"]))
        # 未选中侧:onSurfaceVariant 常规字重(画在滑块所在半区的对侧)
        painter.setPen(QColor(palette["onSurfaceVariant"]))
        painter.drawText(
            0, 0, self.width() // 2, self.height(),
            Qt.AlignmentFlag.AlignCenter, "中文" if self._value == "en" else "",
        )
        painter.drawText(
            self.width() // 2, 0, self.width() // 2, self.height(),
            Qt.AlignmentFlag.AlignCenter, "English" if self._value == "zh" else "",
        )
        # 选中侧(滑块上):onSecondaryContainer 加粗
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(palette["onSecondaryContainer"]))
        painter.drawText(
            0, 0, self.width() // 2, self.height(),
            Qt.AlignmentFlag.AlignCenter, "中文" if self._value == "zh" else "",
        )
        painter.drawText(
            self.width() // 2, 0, self.width() // 2, self.height(),
            Qt.AlignmentFlag.AlignCenter, "English" if self._value == "en" else "",
        )
        painter.end()


class SettingsPage(QWidget):
    """设置页;*_changed 信号均由用户交互触发。"""

    close_action_changed = Signal(str)
    play_mode_changed = Signal(str)
    appearance_changed = Signal(str)  # "dark" | "light"
    quality_changed = Signal(str)  # "lossless" | "exhigh" | "standard"
    recent_max_changed = Signal(int)  # 「最近」分区条数(1~50)
    language_changed = Signal(str)  # "zh" | "en"
    dynamic_color_changed = Signal(bool)  # 播放时跟随封面取色

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.close_group = QGroupBox()
        self.exit_radio = QRadioButton()
        self.tray_radio = QRadioButton()
        close_layout = QHBoxLayout(self.close_group)
        close_layout.addWidget(self.exit_radio)
        close_layout.addWidget(self.tray_radio)
        close_layout.addStretch(1)

        self.mode_group = QGroupBox()
        mode_layout_parent = QVBoxLayout(self.mode_group)
        mode_layout = QHBoxLayout()
        self.mode_combo = QComboBox()
        for label_key, value in _MODE_ITEMS:
            self.mode_combo.addItem(i18n.tr(label_key), userData=value)
        self._mode_label = QLabel()
        mode_layout.addWidget(self._mode_label)
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addStretch(1)
        mode_layout_parent.addLayout(mode_layout)

        self.recent_max_spin = QSpinBox()
        self.recent_max_spin.setRange(MIN_RECENT_MAX, MAX_RECENT_MAX)
        self.recent_max_spin.setValue(DEFAULT_RECENT_MAX)
        self._recent_label = QLabel()
        recent_layout = QHBoxLayout()
        recent_layout.addWidget(self._recent_label)
        recent_layout.addWidget(self.recent_max_spin)
        recent_layout.addStretch(1)
        mode_layout_parent.addLayout(recent_layout)

        self.quality_group = QGroupBox()
        self.quality_combo = QComboBox()
        for label_key, value in _QUALITY_ITEMS:
            self.quality_combo.addItem(i18n.tr(label_key), userData=value)
        self._quality_label = QLabel()
        quality_layout = QHBoxLayout(self.quality_group)
        quality_layout.addWidget(self._quality_label)
        quality_layout.addWidget(self.quality_combo)
        quality_layout.addStretch(1)

        self.appearance_group = QGroupBox()
        self.appearance_combo = QComboBox()
        for label_key, value in _APPEARANCE_ITEMS:
            self.appearance_combo.addItem(i18n.tr(label_key), userData=value)
        self._theme_label = QLabel()
        appearance_layout = QHBoxLayout(self.appearance_group)
        appearance_layout.addWidget(self._theme_label)
        appearance_layout.addWidget(self.appearance_combo)
        appearance_layout.addStretch(1)
        # 动态取色(M5):播放时从封面提取主色套用动态主题
        self.dynamic_color_check = QCheckBox()
        self.dynamic_color_check.setChecked(True)
        self.dynamic_color_check.toggled.connect(self.dynamic_color_changed.emit)
        appearance_layout.addWidget(self.dynamic_color_check)

        self.language_group = QGroupBox()
        self._language_label = QLabel()
        self.language_switch = LanguageSwitch()
        language_layout = QHBoxLayout(self.language_group)
        language_layout.addWidget(self._language_label)
        language_layout.addWidget(self.language_switch)
        language_layout.addStretch(1)

        self.about_group = QGroupBox()
        about_layout = QVBoxLayout(self.about_group)
        self.about_name = QLabel()
        self.about_license = QLabel()
        self.about_license.setWordWrap(True)
        self.about_link = QLabel()
        self.about_link.setOpenExternalLinks(True)
        self.about_link.setObjectName("aboutLink")
        about_layout.addWidget(self.about_name)
        about_layout.addWidget(self.about_license)
        about_layout.addWidget(self.about_link)

        # 内容装进 QScrollArea:设置项的最小高度不再顶高主窗口
        # (语言分组加入后整页 minHint 约 700px,超过 680 默认窗高,
        # 窗口一打开就被 QStackedWidget 的最小尺寸撑到 800+;
        # 包滚动区后页面 minHint 与窗口解耦,内容超高自己滚)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.addWidget(self.close_group)
        root.addWidget(self.mode_group)
        root.addWidget(self.quality_group)
        root.addWidget(self.appearance_group)
        root.addWidget(self.language_group)
        root.addWidget(self.about_group)
        root.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.exit_radio.toggled.connect(self._on_close_toggled)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_index_changed)
        self.appearance_combo.currentIndexChanged.connect(
            self._on_appearance_index_changed
        )
        self.quality_combo.currentIndexChanged.connect(
            self._on_quality_index_changed
        )
        self.recent_max_spin.valueChanged.connect(self.recent_max_changed.emit)
        self.language_switch.language_changed.connect(self.language_changed.emit)

        # 默认勾选与持久化默认一致(close_action 默认 tray)
        self.tray_radio.setChecked(True)

        self.retranslate()

    # -- 文案(语言切换时由 MainWindow 统一调用)-------------------------------

    def retranslate(self) -> None:
        """按当前语言重设全部静态文案;下拉项重设文字但保持选中值。"""
        self.close_group.setTitle(i18n.tr("settings.close_group"))
        self.close_group.setToolTip(i18n.tr("settings.close_tooltip"))
        self.exit_radio.setText(i18n.tr("settings.close_exit"))
        self.tray_radio.setText(i18n.tr("settings.close_tray"))
        self.mode_group.setTitle(i18n.tr("settings.play_group"))
        self._mode_label.setText(i18n.tr("settings.play_mode"))
        for index, (label_key, _value) in enumerate(_MODE_ITEMS):
            self.mode_combo.setItemText(index, i18n.tr(label_key))
        self._recent_label.setText(i18n.tr("settings.recent_max"))
        self.recent_max_spin.setToolTip(i18n.tr("settings.recent_max_tooltip"))
        self.quality_group.setTitle(i18n.tr("settings.quality_group"))
        self._quality_label.setText(i18n.tr("settings.quality"))
        for index, (label_key, _value) in enumerate(_QUALITY_ITEMS):
            self.quality_combo.setItemText(index, i18n.tr(label_key))
        self.appearance_group.setTitle(i18n.tr("settings.appearance_group"))
        self._theme_label.setText(i18n.tr("settings.theme"))
        for index, (label_key, _value) in enumerate(_APPEARANCE_ITEMS):
            self.appearance_combo.setItemText(index, i18n.tr(label_key))
        self.dynamic_color_check.setText(i18n.tr("settings.dynamic_color"))
        self.dynamic_color_check.setToolTip(i18n.tr("settings.dynamic_color_tooltip"))
        self.about_group.setTitle(i18n.tr("settings.about_group"))
        self.about_name.setText(
            i18n.tr("settings.about_version", version=__version__)
        )
        self.about_license.setText(i18n.tr("settings.about_license"))
        # 分组标题双语并列(两种语言的用户都要能找到入口),滑块自身文字也不翻译
        self.language_group.setTitle(i18n.tr("settings.language_group"))
        self._language_label.setText(i18n.tr("settings.language"))
        self.language_switch.setAccessibleName(i18n.tr("settings.language"))
        self.retheme_link()

    # 兼容旧名:MainWindow._on_theme_changed 调用的锚点重取色
    def retheme_link(self) -> None:
        """富文本锚点颜色既不吃 QSS 的 color 也不吃 QPalette.Link,
        唯一可靠路径是内联 HTML style;主题/语言切换时按主色重生成文本。"""
        primary = theme.current_palette()["primary"]
        label = i18n.tr("settings.about_upstream", url=_UPSTREAM_URL)
        self.about_link.setText(
            f'<a href="{_UPSTREAM_URL}" style="color:{primary};">{label}</a>'
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

    def set_language(self, language: str) -> None:
        self.language_switch.set_value(language)

    def set_dynamic_color(self, enabled: bool) -> None:
        self.dynamic_color_check.blockSignals(True)
        self.dynamic_color_check.setChecked(enabled)
        self.dynamic_color_check.blockSignals(False)
