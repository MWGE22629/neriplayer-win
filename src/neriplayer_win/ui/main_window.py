from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDropEvent,
    QFontMetrics,
    QIcon,
    QPainter,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QStackedWidget,
    QSplitter,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..api.bili import (
    BiliAccount,
    BiliApiError,
    BiliAudioStreamInfo,
    BiliAuthRequiredError,
    BiliClient,
    BiliFavFolder,
    bili_quality_key_from_netease_level,
    build_bili_stream_headers,
)
from ..api.netease import (
    NeteaseAccount,
    NeteaseAuthRequiredError,
    NeteaseClient,
    NeteasePlaylist,
    NeteaseSong,
    NeteaseUserPlaylists,
)
from ..data.store import (
    DEFAULT_RECENT_MAX,
    SETTING_APPEARANCE,
    SETTING_BILI_FOLDER_ORDER,
    SETTING_DYNAMIC_COLOR,
    SETTING_LANGUAGE,
    SETTING_NETEASE_PLAYLIST_ORDER,
    SETTING_NETEASE_SUBSCRIBED_ORDER,
    SETTING_PLAY_QUALITY,
    SETTING_RECENT_LISTS,
    SETTING_RECENT_MAX,
    SETTING_SIDEBAR_EXPANDED,
    LocalStore,
    apply_stored_order,
)
from ..i18n import tr
from ..player.engine import PlayerEngine, PlayerEngineError
from ..player.queue import BackupUrlRotator, PlayMode, PlayQueue, QueueSong
from . import theme
from . import cover_seed
from .browser_login import BrowserLoginDialog, bili_web_login
from .covers import CoverLoader
from .icons import app_icon, tinted_icon, tinted_icon_with_color, tray_icon
from .login_page import LoginPage
from .material_color import seed_from_hex
from .media_keys import MediaKeyHandler
from .player_bar import PlayerBar, format_seconds, rounded_pixmap
from .queue_window import QueueWindow
from .settings_page import SettingsPage
from .taskbar import TaskbarThumbBar
from .theme import ThemeManager
from .textsafe import sanitize_ui_text
from .tray import TrayController
from .workers import run_async

_PAGE_LOGIN = 0
_PAGE_TABLE = 1
_PAGE_SETTINGS = 2

# 基础窗口标题(品牌名,不随语言变):任务栏歌名显示的宽度基准
_TITLE_BASE = "NeriPlayer Win"

# 补齐用空白字符:Win11 任务栏文本层(XAML/DirectWrite)会折叠尾随
# 普通空格(补了看不见),NBSP 不折叠且渲染为等宽空白
_TITLE_PAD = "\u00a0"


def taskbar_title(text: str, metrics: QFontMetrics) -> str:
    """歌名 -> 与基础标题等宽的任务栏文案。

    过长按 … 右截断;过短补 NBSP,补完宽度距基准不超过一个空白宽
    (普通空格会被任务栏文本层折叠,见 _TITLE_PAD)。目标:任务栏/
    预览页文案在切歌间保持等长,不随歌名长短抖动。空歌名回落基础标题。
    """
    target = metrics.horizontalAdvance(_TITLE_BASE)
    if not text:
        return _TITLE_BASE
    width = metrics.horizontalAdvance(text)
    if width > target:
        return metrics.elidedText(text, Qt.TextElideMode.ElideRight, target)
    blank = metrics.horizontalAdvance(_TITLE_PAD)
    if blank <= 0:
        blank = metrics.horizontalAdvance(" ") or 1
    padded = text + _TITLE_PAD * int((target - width) // blank)
    return padded


def _header_netease() -> list[str]:
    """网易云歌曲表表头(语言相关,现算现用)。"""
    return ["#", "", tr("table.title"), tr("table.artist"), tr("table.duration")]


def _header_bili() -> list[str]:
    """B站条目表表头(语言相关,现算现用)。"""
    return ["#", "", tr("table.title"), tr("table.upper"), tr("table.duration")]

# 序号列(默认 100px)减半腾出的宽度给封面缩略图列,总占位不变。
_TABLE_COL_NUMBER = 0
_TABLE_COL_COVER = 1
_TABLE_NUMBER_WIDTH = 50
_TABLE_COVER_WIDTH = 50
# 缩略图为播放条封面(40px)的 50% 缩放;圆角随尺寸等比(播放条 8/40)。
_TABLE_COVER_SIZE = 20
_TABLE_COVER_RADIUS = 4
# 固定行高:缩略图到达前后行高一致,加载过程中列表不跳动
_TABLE_ROW_HEIGHT = 32
# 成品 QIcon 缓存条数上限(FIFO 淘汰):item.setIcon 存的是自己的副本,
# 淘汰只影响"下次进表要不要重解码",不动正在显示的行
_TABLE_ICON_CACHE_MAX = 1024

# 搜索页:输入框(半宽居中)与来源栏(网易云|B站 各半宽)均为两行高;
# 结果落现有歌曲表,双击播放/右键插播/封面缩略图全复用
_SEARCH_BAR_HEIGHT = _TABLE_ROW_HEIGHT * 2
_SEARCH_PAGE_SIZE = 30  # 对齐参考实现 NETEASE_SEARCH_PAGE_SIZE
# 「最近」栈里搜索条目的持久化标题前缀(规范数据格式,恒中文,不随界面
# 语言变——老数据回放剥前缀才不失效);展示前缀走 tr("search.recent_prefix")
_SEARCH_TITLE_PREFIX = "搜索:"

_MODE_CYCLE = (PlayMode.SEQUENCE, PlayMode.SHUFFLE, PlayMode.REPEAT_ONE)

# 连续播放失败熔断上限。低于 Android 参考实现的 MAX_CONSECUTIVE_FAILURES=10:
# 桌面端没有 watchdog 做逐级判定(冻结/恢复/重建管线),弱网下逐曲盲跳只会
# 连环失败刷屏+打 API,3 次即熔断停跳并交还手动控制。
_MAX_PLAY_FAILS = 3

# 失败后自动跳下一首的延迟(ms):既给用户留出看清提示的时间,也拉开与下一次
# 解析请求的间隔,避免弱网下解析失败连环触发形成 API 风暴。
_FAIL_SKIP_DELAY_MS = 1000

# 下一首预取结果的有效期(秒)。CDN 播放链接的有效期通常远长于一首歌的
# 时长,但预取毕竟是投机行为,保守取 10 分钟:过期宁可重新解析也不吃 403。
_PREFETCH_TTL_S = 10 * 60.0

# 侧栏分区头 kind → 分区键(折叠状态与排序持久化共用)
_HEADER_SECTION = {
    "netease-header": "netease",
    "netease-subscribed-header": "netease-subscribed",
    "bili-header": "bili",
    "recent-header": "recent",
}

# M5 实际生效音质展示:网易云 level → 文案键(覆盖 QUALITY_FALLBACK_ORDER
# 全集;未识别的新档位原样显示,避免把官方新增档位吞成「未知」)
_NETEASE_LEVEL_KEYS = {
    "lossless": "quality.lossless",
    "hires": "quality.hires",
    "exhigh": "quality.exhigh",
    "higher": "quality.higher",
    "standard": "quality.standard",
    "jymaster": "quality.jymaster",
    "jyeffect": "quality.jyeffect",
    "sky": "quality.sky",
}

# B站音轨标签(BiliAudioStreamInfo.quality_tag)→ 文案键;None 为普通音轨不显示
_BILI_QUALITY_TAG_KEYS = {
    "dolby": "quality.dolby",
    "hires": "quality.hires",
}


def _netease_quality_suffix(level: str | None) -> str:
    """网易云实际生效音质 → 状态栏后缀;响应缺 level 时无信息可展示,不追加。"""
    if not level:
        return ""
    key = _NETEASE_LEVEL_KEYS.get(level)
    label = tr(key) if key is not None else level
    return f"({label})"


def _bili_quality_suffix(stream: BiliAudioStreamInfo) -> str:
    """B站实际生效音质 → 状态栏后缀;比特率未知(0)时无信息可展示,不追加。"""
    if stream.bitrate_kbps <= 0:
        return ""
    key = _BILI_QUALITY_TAG_KEYS.get(stream.quality_tag or "")
    tag = tr(key) if key is not None else ""
    parts = [tag, f"{stream.bitrate_kbps}kbps"]
    return f"({' '.join(part for part in parts if part)})"


def _netease_song_to_queue(song: NeteaseSong) -> QueueSong:
    # 标题/歌手净化:第三方文本可能含主字体不覆盖的字符,任一命中都会
    # 触发 DirectWrite 回退字体(+45~50MB,见 docs/PERF.md);队列标题
    # 会流向歌曲表/播放条/状态栏/托盘,在这一处净化即全覆盖
    return QueueSong(
        source="netease", id=song.id, bvid="", title=sanitize_ui_text(song.title),
        artist=sanitize_ui_text(song.artist), duration_ms=song.duration_ms,
        cover_url=song.cover_url,
    )


def _bili_item_to_queue(avid: int, bvid: str, title: str, upper: str, duration_sec: int,
                        cover_url: str = "") -> QueueSong:
    return QueueSong(
        source="bili", id=avid, bvid=bvid, title=sanitize_ui_text(title),
        artist=sanitize_ui_text(upper), duration_ms=duration_sec * 1000,
        cover_url=cover_url,
    )


@dataclass(frozen=True)
class _ListRef:
    """一次「可浏览可播放的列表」的标识:侧栏条目、表格上下文、最近栈共用。

    kind 取 data.store.RECENT_KINDS 白名单;固定虚拟列表(稍后再看/
    每日推荐)id 恒 0,以 kind 区分。title 为不带计数的干净名字
    (最近栈展示与状态栏提示用,计数会过期故不存)。
    """

    kind: str
    id: int
    title: str


# 每日推荐:固定虚拟列表(歌曲由推荐接口直接返回,无歌单 id);
# 标题随界面语言现算,故用工厂函数而非模块级常量
def _daily_ref() -> _ListRef:
    return _ListRef(kind="netease-daily", id=0, title=tr("sidebar.daily"))


@dataclass
class _ActivePlay:
    """正在播放(或候选轮换中)的一次播放上下文。"""

    song: QueueSong
    rotator: BackupUrlRotator
    headers: dict[str, str] | None
    # 本次起播是否来自预取缓存:是则 _on_load_failed 候选耗尽时先静默
    # 重解析一次(链接过期兜底),再失败才进失败路径
    from_prefetch: bool = False


@dataclass
class _SongContext:
    """一首歌解析成功的播放上下文:起播(_start_play)与预取共用一份逻辑。"""

    rotator: BackupUrlRotator
    headers: dict[str, str] | None
    status: str  # 状态栏文案(含实际生效音质 / 试听提示)


@dataclass
class _PrefetchedPlay:
    """下一曲预取结果(单槽):命中校验信息 + 播放上下文。"""

    queue_index: int  # 预取时的队列索引,消费时与目标 index 比对
    song: QueueSong  # 标识比对用(source+id);手动跳歌未命中即作废
    rotator: BackupUrlRotator
    headers: dict[str, str] | None
    status: str
    created_at: float  # time.monotonic() 时间戳,TTL 校验用


class _EmptyStateView(QWidget):
    """歌曲表空态:居中淡色图标 + 一行提示(docs/UI_ASSETS.md C5,不引插画)。"""

    def __init__(self, icon_name: str = "library_music", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(48, 48)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("emptyHint")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self.icon_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.hint_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self.retheme()

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(text)

    def retheme(self) -> None:
        color = QColor(theme.current_palette()["onSurfaceVariant"])
        color.setAlpha(120)  # 淡色:约半透明
        self.icon_label.setPixmap(
            tinted_icon_with_color(self._icon_name, color).pixmap(QSize(48, 48))
        )


class _CoverColumnDelegate(QStyledItemDelegate):
    """歌曲表封面列:背景(选中/交替行)照默认绘制,icon 画在格子正中。

    空文本 item 的 decoration 走通用布局,实测固定偏左约 4px,
    20px 缩略图在 50px 列里左右留白不对称;该列只有 icon,居中自画最稳。
    """

    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt 命名
        view_option = QStyleOptionViewItem(option)
        self.initStyleOption(view_option, index)
        size = view_option.decorationSize
        # icon 必须从 model 取:PySide6 中 view_option.icon 取出的引用与
        # 成员共享,下面把成员置空画纯背景时会连带清掉它
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        view_option.icon = QIcon()
        view_option.text = ""
        widget = view_option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, view_option, painter, widget
        )
        if not isinstance(icon, QIcon) or icon.isNull():
            return
        device = painter.device()
        dpr = device.devicePixelRatioF() if device is not None else 1.0
        pixmap = icon.pixmap(size, dpr)
        painter.drawPixmap(
            QRect(
                option.rect.x() + (option.rect.width() - size.width()) // 2,
                option.rect.y() + (option.rect.height() - size.height()) // 2,
                size.width(),
                size.height(),
            ),
            pixmap,
        )


class _SlideStack(QStackedWidget):
    """中央页面切换过渡(借鉴 A 端 Tab 转场「快出慢进」)。

    旧页先 grab 截图盖在最上层,120ms 淡出+顺向滑移让位(快出);
    新页同时从逆向 28px 滑入+淡入,300ms OutCubic 归位(慢进)。
    不可见(启动初始化/最小化)或索引未变时直接切换;过渡中再次
    切换:立即清理在途动画,以最新目标重放,不排队。
    """

    _OUT_MS = 120
    _IN_MS = 300
    _OUT_SHIFT = 40
    _IN_SHIFT = 28

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._overlay: QLabel | None = None
        self._out_group: QParallelAnimationGroup | None = None
        self._in_group: QParallelAnimationGroup | None = None
        self._in_widget: QWidget | None = None

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt 命名
        current = self.currentIndex()
        if (
            index == current
            or index < 0
            or index >= self.count()
            or not self.isVisible()
        ):
            super().setCurrentIndex(index)
            return
        self._cleanup_transition()
        snapshot = self.currentWidget().grab()
        direction = 1 if index > current else -1
        super().setCurrentIndex(index)
        self._start_transition(snapshot, direction)

    def _start_transition(self, snapshot: QPixmap, direction: int) -> None:
        curve = QEasingCurve.Type.OutCubic
        # 旧页:截图 overlay 顺向滑出+淡出(快出)
        self._overlay = QLabel(self)
        self._overlay.setPixmap(snapshot)
        self._overlay.setGeometry(self.rect())
        self._overlay.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._overlay.show()
        self._overlay.raise_()
        overlay_effect = QGraphicsOpacityEffect(self._overlay)
        overlay_effect.setOpacity(1.0)
        self._overlay.setGraphicsEffect(overlay_effect)
        overlay_slide = QPropertyAnimation(self._overlay, b"pos", self)
        overlay_slide.setDuration(self._OUT_MS)
        overlay_slide.setEasingCurve(curve)
        overlay_slide.setEndValue(
            QPoint(direction * self._OUT_SHIFT, self._overlay.y())
        )
        overlay_fade = QPropertyAnimation(overlay_effect, b"opacity", self)
        overlay_fade.setDuration(self._OUT_MS)
        overlay_fade.setEasingCurve(curve)
        overlay_fade.setEndValue(0.0)
        self._out_group = QParallelAnimationGroup(self)
        self._out_group.addAnimation(overlay_slide)
        self._out_group.addAnimation(overlay_fade)
        self._out_group.finished.connect(self._on_out_finished)
        self._out_group.start()

        # 新页:逆向滑入+淡入(慢进)
        new_widget = self.currentWidget()
        self._in_widget = new_widget
        new_widget.move(new_widget.x() - direction * self._IN_SHIFT, new_widget.y())
        in_effect = QGraphicsOpacityEffect(new_widget)
        in_effect.setOpacity(0.0)
        new_widget.setGraphicsEffect(in_effect)
        in_slide = QPropertyAnimation(new_widget, b"pos", self)
        in_slide.setDuration(self._IN_MS)
        in_slide.setEasingCurve(curve)
        in_slide.setEndValue(QPoint(0, 0))
        in_fade = QPropertyAnimation(in_effect, b"opacity", self)
        in_fade.setDuration(self._IN_MS)
        in_fade.setEasingCurve(curve)
        in_fade.setEndValue(1.0)
        self._in_group = QParallelAnimationGroup(self)
        self._in_group.addAnimation(in_slide)
        self._in_group.addAnimation(in_fade)
        self._in_group.finished.connect(self._on_in_finished)
        self._in_group.start()

    def _on_out_finished(self) -> None:
        if self._overlay is not None:
            self._overlay.hide()
            self._overlay.deleteLater()
            self._overlay = None
        if self._out_group is not None:
            self._out_group.deleteLater()
            self._out_group = None

    def _on_in_finished(self) -> None:
        if self._in_widget is not None:
            self._in_widget.setGraphicsEffect(None)
            self._in_widget.move(0, 0)
            self._in_widget = None
        if self._in_group is not None:
            self._in_group.deleteLater()
            self._in_group = None

    def _cleanup_transition(self) -> None:
        """切换中又有新切换:停掉在途动画并复位,不排队。"""
        self._on_out_finished()
        self._on_in_finished()
        if self._out_group is not None:
            self._out_group.stop()
        if self._in_group is not None:
            self._in_group.stop()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        if self._overlay is not None:
            self._overlay.setGeometry(self.rect())


# -- 正在播放均衡器条(A 端 RecentScreen 三根条,暂停回落) --------------------
_EQ_PERIODS_MS = (520.0, 680.0, 600.0)  # 三根条周期互异,相位天然错开
_EQ_PHASES = (0.0, 0.33, 0.66)
_EQ_BAR_MIN_MAX = ((5.0, 12.0), (7.0, 15.0), (5.0, 11.0))  # 各条高度区间 px
_EQ_BAR_WIDTH = 3
_EQ_BAR_GAP = 2
_EQ_TICK_MS = 33  # 相位驱动 ~30fps(条幅变化平缓,无需 60fps)
_EQ_LEVEL_MS = 180  # 播放<->暂停 整体高度过渡(A 端暂停回落时长)


@dataclass
class _EqState:
    """均衡器条的共享状态:MainWindow 驱动,委托只读。

    row 为当前播放行(不在当前表则为 None);level 1=跳动 0=平齐
    (播放态过渡);phase_ms 为累计相位,驱动条的正弦起伏。
    """

    row: int | None = None
    level: float = 0.0
    phase_ms: float = 0.0


class _NowPlayingBarsDelegate(QStyledItemDelegate):
    """歌曲表序号列:当前播放行以三根均衡器条替代序号。

    行不匹配时照默认绘制序号;匹配时背景(选中/交替行)仍走默认
    绘制,仅替换内容层。暂停时条随 level 回落到最低高度平齐。
    """

    def __init__(self, state: _EqState, parent=None) -> None:
        super().__init__(parent)
        self._state = state

    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt 命名
        if index.row() == self._state.row:
            view_option = QStyleOptionViewItem(option)
            self.initStyleOption(view_option, index)
            view_option.text = ""
            view_option.icon = QIcon()
            widget = view_option.widget
            style = widget.style() if widget is not None else QApplication.style()
            style.drawControl(
                QStyle.ControlElement.CE_ItemViewItem, view_option, painter, widget
            )
            self._paint_bars(painter, option.rect)
            return
        super().paint(painter, option, index)

    def _paint_bars(self, painter: QPainter, rect: QRect) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.current_palette()["primary"]))
        center_x = rect.center().x()
        center_y = rect.center().y()
        total_width = 3 * _EQ_BAR_WIDTH + 2 * _EQ_BAR_GAP
        x = center_x - total_width // 2
        for i in range(3):
            minimum, maximum = _EQ_BAR_MIN_MAX[i]
            wave = 0.5 + 0.5 * math.sin(
                2 * math.pi
                * (self._state.phase_ms / _EQ_PERIODS_MS[i] + _EQ_PHASES[i])
            )
            height = minimum + (maximum - minimum) * wave * self._state.level
            height = max(minimum, height)
            painter.drawRoundedRect(
                QRectF(
                    x,
                    center_y - height / 2,
                    _EQ_BAR_WIDTH,
                    height,
                ),
                1.5,
                1.5,
            )
            x += _EQ_BAR_WIDTH + _EQ_BAR_GAP
        painter.restore()


class _SourceBar(QWidget):
    """搜索来源栏:网易云|B站 平铺两半,顶部一条滑动指示线代表选中。

    借鉴移动端 PrimaryScrollableTabRow(M3 主标签行):底色透明,
    选中不铺满颜色,只有 primary 色指示线贴着选中页签文字宽居中,
    切换来源时线滑动过去(250ms OutCubic)。
    """

    def __init__(
        self,
        netease_button: QPushButton,
        bili_button: QPushButton,
        on_layout_changed,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(netease_button, 1)
        layout.addWidget(bili_button, 1)
        self.indicator = QWidget(self)
        self.indicator.setObjectName("searchSourceIndicator")
        self.indicator.setFixedHeight(3)
        self.indicator.setGeometry(0, 0, 0, 3)
        self._on_layout_changed = on_layout_changed
        self._animation = QPropertyAnimation(self.indicator, b"geometry", self)
        self._animation.setDuration(250)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def move_indicator(self, geometry: QRect, animate: bool) -> None:
        self._animation.stop()
        if animate:
            self._animation.setEndValue(geometry)
            self._animation.start()
        else:
            self.indicator.setGeometry(geometry)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._on_layout_changed()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        self._on_layout_changed()


class _SidebarTree(QTreeWidget):
    """侧栏树(M5):四分区顶层项(网易云歌单/网易云收藏/B站/设置)+ 分区内子节点。

    拖拽排序做硬约束:只允许「同一分区内」的子节点(歌单/收藏夹)移动。
    dropEvent 不交给 Qt 默认实现(默认会把子节点挂成目标节点的子节点,
    产生意外嵌套),而是手动搬移;合法移动后发 order_changed(分区键),
    由 MainWindow 读子节点顺序落库。拖到顶层项(含分区头与「设置」)、
    跨分区、空白处,或试图拖动顶层项/登录占位/固定项(稍后再看)时直接
    ignore,不落库。
    """

    order_changed = Signal(str)  # 分区键 "netease" | "netease-subscribed" | "bili"

    # 可参与排序的子节点 kind(分区键 → 子节点 kind)
    _SECTION_CHILD_KIND = {
        "netease": "netease-playlist",
        "netease-subscribed": "netease-subscribed-playlist",
        "bili": "bili-folder",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setColumnCount(1)
        self.setHeaderHidden(True)
        self.setExpandsOnDoubleClick(False)  # 折叠只走「单击分区头」
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        # 不用系统折叠箭头列:它按层级占一整列宽度,且分支区与条目背景
        # 分开绘制,选中/悬停的圆角色块会被切成两截。可收起的可视化提示
        # 改由分区头图标右下角的三角徽标承担(QPainter 绘制,见
        # _section_header_icon)。刻意不用「▾/▸」等文字符号:这些几何
        # 字形会触发 DirectWrite 回退字体加载,实测仅两个字符就给进程
        # 常驻内存增加约 50MB WorkingSet。
        self.setRootIsDecorated(False)
        self.setIndentation(14)  # 子节点仅保留小幅层级缩进,无分支列

    @staticmethod
    def item_kind(item: QTreeWidgetItem | None) -> str:
        """条目 UserRole 元组 ("kind", payload) 的 kind;无数据返回空串。"""
        if item is None:
            return ""
        role = item.data(0, Qt.ItemDataRole.UserRole)
        return role[0] if isinstance(role, tuple) else ""

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt 命名
        selected = self.selectedItems()
        dragged = selected[0] if len(selected) == 1 else None
        if (
            dragged is None
            or dragged.parent() is None
            or self.item_kind(dragged) not in self._SECTION_CHILD_KIND.values()
        ):
            # 顶层项(分区头/设置)与登录占位都不可移动
            event.ignore()
            return
        target = self.itemAt(event.position().toPoint())
        if (
            target is None
            or target.parent() is None
            or target.parent() is not dragged.parent()
        ):
            # 顶层项上、空白处、跨分区一律拒绝
            event.ignore()
            return
        if target is dragged:
            # 原地放置:接受事件但顺序无变化,不通知落库
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        indicator = self.dropIndicatorPosition()
        below = indicator != QAbstractItemView.DropIndicatorPosition.AboveItem
        self._move_child(dragged, target, below)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        section = _HEADER_SECTION.get(self.item_kind(dragged.parent()))
        if section is not None:
            self.order_changed.emit(section)

    def _move_child(
        self, dragged: QTreeWidgetItem, target: QTreeWidgetItem, below: bool
    ) -> None:
        """把 dragged 移动到同分区 target 的前/后(手动搬移,不经 Qt 默认拖放)。"""
        parent = dragged.parent()
        old_index = parent.indexOfChild(dragged)
        new_index = parent.indexOfChild(target) + (1 if below else 0)
        if old_index < new_index:
            new_index -= 1  # 摘除自身后,后方条目索引整体前移一位
        parent.takeChild(old_index)
        parent.insertChild(new_index, dragged)
        self.setCurrentItem(dragged)

    @classmethod
    def section_of(cls, item: QTreeWidgetItem | None) -> str | None:
        """条目所属分区键:分区头返回自身,子节点返回其父分区头,其余 None。"""
        if item is None:
            return None
        header = item if item.parent() is None else item.parent()
        return _HEADER_SECTION.get(cls.item_kind(header))

    def section_child_ids(self, section: str) -> list[int]:
        """读分区子节点的 id 顺序(跳过加载占位等非排序条目)。"""
        kind = self._SECTION_CHILD_KIND.get(section)
        if kind is None:
            return []
        header = self.find_header(section)
        if header is None:
            return []
        ids: list[int] = []
        for row in range(header.childCount()):
            child = header.child(row)
            role = child.data(0, Qt.ItemDataRole.UserRole)
            child_kind, payload = role if isinstance(role, tuple) else ("", None)
            if child_kind == kind:
                ids.append(int(payload))
        return ids

    def find_header(self, section: str) -> QTreeWidgetItem | None:
        """按分区键找顶层分区头(重建后对象会换,每次现查)。"""
        kind = f"{section}-header"
        for row in range(self.topLevelItemCount()):
            item = self.topLevelItem(row)
            if self.item_kind(item) == kind:
                return item
        return None


class MainWindow(QMainWindow):
    """主窗口:左侧导航(最近 + 网易云自建/收藏歌单 + B站收藏夹/稍后再看)
    + 歌曲列表 + 播放条。

    表格(浏览的列表)与播放队列解耦:侧栏点击只把列表装进表格,双击表格
    才把该列表装入 self._queue 起播;右键「下一首播放」把表格里点中的歌
    插到队列当前曲之后(insert_next,按点击顺序累积),不替换队列其余
    部分。成功起播时把队列来源列表压入「最近」栈(仅浏览不计)。

    播放队列持有统一条目(来源标记 netease/bili),双击列表、队列窗口
    切跳与自动接播都经 _play_at 按条目来源分发解析,两平台共用播放条。
    播放模式(顺序/随机/单曲循环)由 PlayQueue 管理;B站播放地址加载
    失败时按 backupUrls 候选轮换重试(_on_load_failed);各类播放失败统一
    走 _handle_play_failure:非阻塞提示(状态栏+托盘气泡)+ 延时自动跳
    下一首 + 连续失败熔断;解析请求用代际 token 作废过期回调(新点歌
    可覆盖旧请求,弱网下不卡死)。起播成功后顺手后台预取下一首的播放
    地址(peek_next 语义,顺序/单曲模式),切歌命中预取槽即零网络起播。
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(_TITLE_BASE)
        self.resize(1080, 680)

        self._store = LocalStore()
        self._settings = self._store.load_settings()
        # 界面语言先于任何控件构造(静态文案在构建时就取当前语言);
        # 语言变更监听在 UI 全部建好后再注册(_on_language_changed 依赖全套控件)
        i18n.set_language(
            self._settings.get(SETTING_LANGUAGE, i18n.DEFAULT_LANGUAGE), notify=False
        )
        self._client = NeteaseClient()
        self._account: NeteaseAccount | None = None
        self._playlists: list[NeteasePlaylist] = []
        # 收藏的他人歌单(user/playlist 响应按 subscribed 分流的另一份视图)
        self._subscribed_playlists: list[NeteasePlaylist] = []

        self._bili_client = BiliClient()
        self._bili_account: BiliAccount | None = None
        self._bili_folders: list[BiliFavFolder] = []
        # 稍后再看条数(None=未加载,侧栏条目不显示计数)
        self._bili_watchlater_count: int | None = None

        self._queue = PlayQueue(mode=PlayMode(self._settings.get("play_mode", "sequence")))
        # 表格(浏览的列表)与播放队列解耦:侧栏点击只浏览,双击表格才把
        # 该列表装入队列起播;右键「下一首播放」在队列当前曲后插播。
        # _table_context/_queue_context 记录两边各自的列表来源(_ListRef),
        # _queue_dirty 标记队列因插播偏离其来源列表(此时双击同列表也需重装)。
        self._table_songs: list[QueueSong] = []
        self._table_context: _ListRef | None = None
        self._queue_context: _ListRef | None = None
        self._queue_dirty = False
        # 「最近」栈(新→旧):起播成功时压栈,仅浏览不计
        self._recent: list[_ListRef] = [
            _ListRef(kind=entry["kind"], id=entry["id"], title=entry["title"])
            for entry in self._settings.get(SETTING_RECENT_LISTS, [])
        ]
        # 解析代际:每次进入 _play_at 自增,旧代际的解析回调一律作废。
        # 弱网下一次解析可挂 40s+(httpx 超时 × 音质回退链串行请求),
        # 必须允许新点歌覆盖旧请求,故不再用单一 _resolving 标志拦截。
        self._resolve_generation = 0
        self._play_fail_count = 0  # 连续播放失败计数(成功发起播放/正常播完复位)
        self._fail_skip_token = 0  # 失败自动跳过守卫 token(新点歌/新调度即作废旧回调)
        self._active: _ActivePlay | None = None
        # 下一曲预取单槽(顺序/单曲模式;随机不可预知不预取):起播成功后
        # 后台解析下一首,切歌命中即零网络等待起播;切歌单/换模式即作废。
        self._prefetched: _PrefetchedPlay | None = None
        self._table_header_source = "netease"
        # 搜索状态:来源/关键词/已加载页数/是否还有下一页;generation 防串号
        # (切来源/新搜索/退出搜索模式都自增,旧回调一律作废)
        self._search_source = "netease"
        self._search_keyword = ""
        self._search_page = 0
        self._search_has_more = False
        self._search_generation = 0
        # 歌曲表封面缩略图:预取与播放条各用各的 CoverLoader 实例,
        # 磁盘缓存按 URL 共享(列表预取过的歌播放秒出图,反之亦然)
        self._cover_loader = CoverLoader(self)
        self._cover_loader.cover_ready.connect(self._on_table_cover_ready)
        self._cover_icons: dict[str, QIcon] = {}
        # 动态取色(M5):第三实例只喂种子提取,磁盘缓存同源,零额外网络
        self._seed_loader = CoverLoader(self)
        self._seed_loader.cover_ready.connect(self._on_seed_cover_ready)
        self._dynamic_seed: int | None = None  # 当前生效的动态种子(ARGB)
        self._seed_url = ""  # 提取请求对应的封面 URL(过期回调丢弃用)
        self._seed_generation = 0  # 切歌/清歌作废在途提取

        try:
            self.engine = PlayerEngine()
        except PlayerEngineError as error:
            self.engine = None
            self._engine_error = str(error)
        else:
            self._engine_error = ""

        # -- 中部页面 ---------------------------------------------------------
        self.login_page = LoginPage(self._client)
        self.login_page.login_succeeded.connect(self._on_login_succeeded)

        self.song_table = QTableWidget(0, 5)
        self.song_table.setHorizontalHeaderLabels(_header_netease())
        header = self.song_table.horizontalHeader()
        # 序号列与封面列固定 50px(原序号列默认 100px 的一半),标题列 Stretch
        header.setSectionResizeMode(
            _TABLE_COL_NUMBER, QHeaderView.ResizeMode.Fixed
        )
        header.setSectionResizeMode(
            _TABLE_COL_COVER, QHeaderView.ResizeMode.Fixed
        )
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.song_table.setColumnWidth(_TABLE_COL_NUMBER, _TABLE_NUMBER_WIDTH)
        self.song_table.setColumnWidth(_TABLE_COL_COVER, _TABLE_COVER_WIDTH)
        self.song_table.setIconSize(QSize(_TABLE_COVER_SIZE, _TABLE_COVER_SIZE))
        self.song_table.setItemDelegateForColumn(
            _TABLE_COL_COVER, _CoverColumnDelegate(self.song_table)
        )
        # 缩略图未到时行高即取最终值,渐进显示不引起行高跳动
        self.song_table.verticalHeader().setDefaultSectionSize(_TABLE_ROW_HEIGHT)
        self.song_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.song_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.song_table.verticalHeader().setVisible(False)
        self.song_table.setAlternatingRowColors(True)
        self.song_table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        # 右键「下一首播放」(插播不换队列)
        self.song_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.song_table.customContextMenuRequested.connect(self._on_table_context_menu)

        # 空态视图与歌曲表同页切换:表空显示居中提示,有内容显示表
        self._empty_state = _EmptyStateView()
        self.table_stack = QStackedWidget()
        self.table_stack.addWidget(self.song_table)  # 0
        self.table_stack.addWidget(self._empty_state)  # 1

        # 正在播放均衡器条:状态共享给序号列委托,timer 驱动相位(~30fps,
        # 只重绘当前行),播放态经 _eq_level 过渡(A 端暂停回落 180ms)
        self._eq_state = _EqState()
        self.song_table.setItemDelegateForColumn(
            _TABLE_COL_NUMBER, _NowPlayingBarsDelegate(self._eq_state, self.song_table)
        )
        self._playing = False
        self._eq_last_tick = time.monotonic()
        self._eq_timer = QTimer(self)
        self._eq_timer.setInterval(_EQ_TICK_MS)
        self._eq_timer.timeout.connect(self._eq_tick)
        self._eq_level = QVariantAnimation(self)
        self._eq_level.setDuration(_EQ_LEVEL_MS)
        self._eq_level.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._eq_level.valueChanged.connect(self._on_eq_level)

        # 搜索页头:输入框(半宽居中,两行高)+ 来源栏(网易云|B站 各半宽,
        # 两行高);只在搜索模式显示,结果直接落下方现有歌曲表
        self._search_input = QLineEdit()
        self._search_input.setObjectName("searchInput")
        self._search_input.setPlaceholderText(tr("search.placeholder"))
        self._search_input.setFixedHeight(_SEARCH_BAR_HEIGHT)
        self._search_input.setClearButtonEnabled(True)
        self._search_input.returnPressed.connect(self._on_search_return_pressed)
        input_row = QHBoxLayout()
        input_row.addStretch(1)
        # stretch 1:2:1 → 输入框恰占半宽且居中
        input_row.addWidget(self._search_input, 2)
        input_row.addStretch(1)

        self._search_netease_btn = QPushButton(tr("search.source_netease"))
        self._search_bili_btn = QPushButton(tr("search.source_bili"))
        for button in (self._search_netease_btn, self._search_bili_btn):
            button.setObjectName("searchSourceBtn")
            button.setCheckable(True)
            button.setFixedHeight(_SEARCH_BAR_HEIGHT)
        self._search_netease_btn.setChecked(True)
        self._search_group = QButtonGroup(self)
        self._search_group.addButton(self._search_netease_btn)
        self._search_group.addButton(self._search_bili_btn)
        self._search_netease_btn.clicked.connect(
            lambda: self._on_search_source_changed("netease")
        )
        self._search_bili_btn.clicked.connect(
            lambda: self._on_search_source_changed("bili")
        )
        # 来源栏:M3 主标签行风格,顶部滑动指示线代表选中
        self._source_bar = _SourceBar(
            self._search_netease_btn,
            self._search_bili_btn,
            on_layout_changed=lambda: self._update_source_indicator(animate=False),
        )
        self._source_indicator = self._source_bar.indicator

        self._search_header = QWidget()
        header_layout = QVBoxLayout(self._search_header)
        header_layout.setContentsMargins(16, 16, 16, 8)
        header_layout.setSpacing(8)
        header_layout.addLayout(input_row)
        header_layout.addWidget(self._source_bar)
        self._search_header.setVisible(False)

        self._search_more_btn = QPushButton(tr("search.more"))
        self._search_more_btn.setObjectName("searchMoreBtn")
        self._search_more_btn.setFixedHeight(_TABLE_ROW_HEIGHT + 8)
        self._search_more_btn.clicked.connect(self._on_search_more_clicked)
        self._search_more_btn.setVisible(False)

        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(0)
        table_layout.addWidget(self._search_header)
        table_layout.addWidget(self.table_stack, 1)
        table_layout.addWidget(self._search_more_btn)

        self.settings_page = SettingsPage()
        self._wire_settings_page()

        self.central_stack = _SlideStack()
        self.central_stack.addWidget(self.login_page)  # 0
        self.central_stack.addWidget(table_page)  # 1
        self.central_stack.addWidget(self.settings_page)  # 2

        # -- 侧栏 -------------------------------------------------------------
        # QTreeWidget(M5):平台分区可折叠 + 分区内歌单/收藏夹拖拽排序
        self.sidebar = _SidebarTree()
        # 宽度可拖动(QSplitter),硬边界兜底;运行期按窗口比例钳制见 _clamp
        self.sidebar.setMinimumWidth(140)
        self.sidebar.setMaximumWidth(480)
        # 分区头图标是「品牌标 18px + 折叠三角徽标」的 30x18 合成画布;
        # 子节点图标 18px 会在 30x18 格内居中,视觉尺寸不变
        self.sidebar.setIconSize(QSize(30, 18))
        # 选择分发走 itemClicked(currentItemChanged 对鼠标点击不可靠);
        # 分区头折叠走同一个槽(见 _on_sidebar_item_clicked)
        self.sidebar.itemClicked.connect(self._on_sidebar_item_clicked)
        self.sidebar.order_changed.connect(self._on_sidebar_order_changed)
        # 折叠状态持久化挂在 itemExpanded/itemCollapsed 上(单击分区头
        # 经 _toggle_sidebar_section 程序化切换也走这两个信号);
        # _rebuild_sidebar 重建时被 blockSignals 抑制
        self.sidebar.itemExpanded.connect(self._on_sidebar_section_toggled)
        self.sidebar.itemCollapsed.connect(self._on_sidebar_section_toggled)

        # -- 底部播放条 -------------------------------------------------------
        # 任务栏缩略图工具栏在 _setup_taskbar 才创建;先置 None,
        # _set_play_controls_active(_on_playing_changed 等)全程可安全判空
        self._taskbar: TaskbarThumbBar | None = None
        self.player_bar = PlayerBar()
        self.player_bar.setObjectName("playerBar")
        self._set_play_controls_active(False)
        if self.engine is not None:
            self._wire_engine()
        self._wire_player_bar()
        self._wire_queue()

        # -- 主题(M4):应用持久化主题;此后切换经 theme_changed 全量刷新 -----
        # 注意:_on_theme_changed 会触碰 _tray 等属性,须先完成初始化
        self._queue_window: QueueWindow | None = None
        self._tray: TrayController | None = None
        self._tray_hint_shown = False
        self._force_exit = False
        self._media_keys: MediaKeyHandler | None = None
        self.themes = ThemeManager()
        self.themes.theme_changed.connect(self._on_theme_changed)
        self.themes.apply(self._settings.get(SETTING_APPEARANCE, "dark"))

        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.central_stack, stretch=1)
        root.addWidget(self.player_bar)
        layout.addLayout(root, stretch=1)  # 右列挂进 body(遗漏此行=中央区/播放条消失)
        # 侧栏宽度可拖动调整,比例钳制在窗口宽度的 1/10 ~ 1/2(_clamp_sidebar_width)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setContentsMargins(0, 0, 0, 0)
        self._splitter.setHandleWidth(6)
        self._splitter.addWidget(self.sidebar)
        self._splitter.addWidget(body)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([220, 860])
        # 拖动把手时同步钳制(隐藏窗口的 resizeEvent 可能延迟到 show 才送达)
        self._splitter.splitterMoved.connect(
            lambda *_: self._clamp_sidebar_width()
        )
        self.setCentralWidget(self._splitter)
        self._clamp_sidebar_width()

        self.statusBar().showMessage(tr("common.ready"))

        # -- 托盘 / 媒体键 / 队列窗口 ------------------------------------------
        self.setWindowIcon(app_icon())
        self._setup_tray(tray_icon(self.themes.name))
        self._setup_media_keys()
        self._setup_taskbar()

        self._rebuild_sidebar()
        self._update_table_empty_state()
        self.central_stack.setCurrentIndex(_PAGE_LOGIN)
        self.login_page.start()
        self._boot_from_store()
        self._boot_bili_from_store()

        if self.engine is None:
            self.statusBar().showMessage(self._engine_error, 10000)

        # UI 全部就绪:注册语言变更监听(设置页滑块切换 → 全量重翻译)
        i18n.add_listener(self._on_language_changed)

    # -- 启动恢复 ------------------------------------------------------------

    def _boot_from_store(self) -> None:
        bundle = self._store.load_netease()
        if not bundle:
            return
        self._client.set_persisted_cookies(bundle["cookies"])
        run_async(
            self._client.get_login_status,
            on_done=self._on_boot_status,
            on_error=lambda message: self.statusBar().showMessage(
                tr("login.status.check_failed", message=message)
            ),
        )

    def _on_boot_status(self, account) -> None:
        if account is None:
            self._handle_stale_login(tr("login.status.expired"))
            return
        self._enter_logged_in(account)

    def _boot_bili_from_store(self) -> None:
        bundle = self._store.load_bili()
        if not bundle:
            return
        self._bili_client.set_cookies(bundle["cookies"])
        run_async(
            self._bili_client.get_login_status,
            on_done=self._on_bili_boot_status,
            on_error=lambda message: self.statusBar().showMessage(
                tr("bili.check_failed", message=message)
            ),
        )

    def _on_bili_boot_status(self, account) -> None:
        if account is None:
            self._handle_bili_stale_login(tr("bili.expired_sidebar"))
            return
        self._enter_bili_logged_in(account)

    # -- 网易云登录流程 -------------------------------------------------------

    def _on_login_succeeded(self, cookies: dict) -> None:
        self._store.save_netease(cookies)
        self._client.set_persisted_cookies(cookies)
        self.statusBar().showMessage(tr("login.status.success_fetching"))
        run_async(
            self._client.get_login_status,
            on_done=self._on_login_account,
            on_error=lambda message: self.statusBar().showMessage(
                tr("login.status.account_failed", message=message)
            ),
        )

    def _on_login_account(self, account) -> None:
        if account is None:
            self._handle_stale_login(tr("login.status.invalid"))
            return
        self._enter_logged_in(account)

    def _enter_logged_in(self, account: NeteaseAccount) -> None:
        self._account = account
        self.statusBar().showMessage(
            tr("login.status.logged_in", name=account.nickname or account.user_id)
        )
        self._rebuild_sidebar()
        self.central_stack.setCurrentIndex(_PAGE_TABLE)
        self._clear_table()
        self._queue.replace([])
        self._queue_context = None
        self._queue_dirty = False
        self._load_playlists()

    def _handle_stale_login(self, message: str) -> None:
        self._store.clear_netease()
        self._client.logout()
        self._account = None
        self._playlists = []
        self._subscribed_playlists = []
        self._queue.replace([])
        self._queue_context = None
        self._queue_dirty = False
        self._clear_table()
        self._rebuild_sidebar()
        self.central_stack.setCurrentIndex(_PAGE_LOGIN)
        self.login_page.start()
        self.statusBar().showMessage(message)

    # -- B站登录流程 ----------------------------------------------------------

    def _open_bili_login_dialog(self) -> None:
        dialog = BrowserLoginDialog(bili_web_login(), self)
        dialog.login_cookie_ready.connect(self._on_bili_cookies)
        dialog.exec()

    def _on_bili_cookies(self, cookies: dict) -> None:
        if not cookies.get("SESSDATA"):
            self.statusBar().showMessage(tr("bili.no_credentials"))
            return
        self._store.save_bili(cookies)
        self._bili_client.set_cookies(cookies)
        self.statusBar().showMessage(tr("bili.login_success"))
        run_async(
            self._bili_client.get_login_status,
            on_done=self._on_bili_account_loaded,
            on_error=lambda message: self.statusBar().showMessage(
                tr("bili.account_failed", message=message)
            ),
        )

    def _on_bili_account_loaded(self, account) -> None:
        if account is None:
            self._handle_bili_stale_login(tr("bili.login_invalid"))
            return
        self._enter_bili_logged_in(account)

    def _enter_bili_logged_in(self, account: BiliAccount) -> None:
        self._bili_account = account
        # 补写 profile(mid/uname),下次启动无需等 nav 就知道账号
        cookies = self._bili_client.cookies_snapshot()
        if cookies:
            self._store.save_bili(cookies, profile={"mid": account.mid, "uname": account.uname})
        self.statusBar().showMessage(
            tr("bili.logged_in", name=account.uname or account.mid)
        )
        self._rebuild_sidebar()
        self._load_bili_folders()

    def _handle_bili_stale_login(self, message: str) -> None:
        self._store.clear_bili()
        self._bili_client.logout()
        self._bili_account = None
        self._bili_folders = []
        self._bili_watchlater_count = None
        self._rebuild_sidebar()
        self.statusBar().showMessage(message)

    def _handle_bili_section_click(self) -> None:
        """侧栏「B站 · 未登录」被点击:有残留 cookie 先验证,否则弹网页登录。"""
        if self._bili_client.has_login():
            self.statusBar().showMessage(tr("bili.checking"))
            run_async(
                self._bili_client.get_login_status,
                on_done=self._on_bili_section_status,
                on_error=lambda message: self.statusBar().showMessage(
                    tr("bili.check_failed", message=message)
                ),
            )
        else:
            self._open_bili_login_dialog()

    def _on_bili_section_status(self, account) -> None:
        if account is not None:
            self._enter_bili_logged_in(account)
            return
        self._handle_bili_stale_login(tr("bili.expired"))
        self._open_bili_login_dialog()

    # -- 网易云歌单 -----------------------------------------------------------

    def _load_playlists(self) -> None:
        account = self._account
        if account is None:
            return

        def fetch() -> object:
            return self._client.get_user_playlists_grouped(account.user_id)

        run_async(
            fetch,
            on_done=self._on_playlists_loaded,
            on_error=lambda message: self.statusBar().showMessage(
                tr("list.playlists_failed", message=message)
            ),
        )

    def _on_playlists_loaded(self, groups) -> None:
        if not isinstance(groups, NeteaseUserPlaylists):
            return
        # 套用拖拽排序的存储顺序:已知歌单按存储序排前,新歌单追加尾部
        self._playlists = self._apply_stored_order(
            groups.created, SETTING_NETEASE_PLAYLIST_ORDER, lambda p: p.id
        )
        self._subscribed_playlists = self._apply_stored_order(
            groups.subscribed, SETTING_NETEASE_SUBSCRIBED_ORDER, lambda p: p.id
        )
        self._rebuild_sidebar()

    def _load_playlist_tracks(self, list_ref: _ListRef) -> None:
        """加载网易云歌单(自建/收藏)到表格;只浏览不换队列。"""
        self._clear_table()
        self.statusBar().showMessage(tr("list.loading", title=list_ref.title))

        def fetch() -> object:
            return self._client.get_playlist_tracks(list_ref.id)

        def on_done(songs) -> None:
            if not isinstance(songs, list):
                return
            self._set_table_songs(
                [_netease_song_to_queue(s) for s in songs], "netease", list_ref
            )
            self.statusBar().showMessage(
                tr("list.loaded", title=list_ref.title, count=len(songs))
            )

        def on_error(message: str) -> None:
            self.statusBar().showMessage(tr("list.songs_failed", message=message))

        run_async(fetch, on_done=on_done, on_error=on_error)

    # -- 网易云每日推荐 --------------------------------------------------------

    def _load_netease_daily(self) -> None:
        """每日推荐歌曲(参考实现 getDailyRecommendedSongs);只浏览不换队列。"""
        daily_ref = _daily_ref()
        self._clear_table()
        self.statusBar().showMessage(tr("list.loading", title=daily_ref.title))

        def fetch() -> object:
            return self._client.get_daily_recommended_songs()

        def on_done(songs) -> None:
            if not isinstance(songs, list):
                return
            self._set_table_songs(
                [_netease_song_to_queue(s) for s in songs], "netease", daily_ref
            )
            self.statusBar().showMessage(
                tr("list.loaded", title=daily_ref.title, count=len(songs))
            )

        def on_error(message: str) -> None:
            self.statusBar().showMessage(tr("list.daily_failed", message=message))

        run_async(fetch, on_done=on_done, on_error=on_error)

    # -- B站收藏夹 ------------------------------------------------------------

    def _load_bili_folders(self) -> None:
        account = self._bili_account
        if account is None:
            return

        def fetch() -> object:
            folders = self._bili_client.get_user_created_fav_folders(account.mid)
            # 顺手拉一次稍后再看条数(接口全量返回,count 顺带可得);
            # 失败只影响侧栏计数显示,不影响收藏夹本体
            try:
                watchlater_count: int | None = len(
                    self._bili_client.get_watch_later_items()
                )
            except BiliApiError:
                watchlater_count = None
            return (folders, watchlater_count)

        run_async(
            fetch,
            on_done=self._on_bili_folders_loaded,
            on_error=lambda message: self.statusBar().showMessage(
                tr("list.bili_folders_failed", message=message)
            ),
        )

    def _on_bili_folders_loaded(self, result) -> None:
        folders, watchlater_count = result
        if not isinstance(folders, list):
            return
        self._bili_watchlater_count = watchlater_count
        # 套用拖拽排序的存储顺序:已知收藏夹按存储序排前,新收藏夹追加尾部
        self._bili_folders = self._apply_stored_order(
            folders, SETTING_BILI_FOLDER_ORDER, lambda f: f.media_id
        )
        self._rebuild_sidebar()
        self.statusBar().showMessage(
            tr("list.bili_folders_loaded", count=len(folders))
        )

    def _show_bili_items(self, items: list, list_ref: _ListRef) -> None:
        """B站条目列表(收藏夹/稍后再看共用)填表;只浏览不换队列。"""
        playable = [item for item in items if item.playable]
        self._set_table_songs(
            [
                _bili_item_to_queue(
                    item.id, item.bvid or "", item.title or "", item.upper_name,
                    item.duration_sec, item.cover_url,
                )
                for item in playable
            ],
            "bili",
            list_ref,
        )
        skipped = len(items) - len(playable)
        suffix = tr("list.bili_skipped", count=skipped) if skipped else ""
        self.statusBar().showMessage(
            tr("list.loaded", title=list_ref.title, count=len(playable)) + suffix
        )

    def _load_bili_folder_tracks(self, list_ref: _ListRef) -> None:
        self._clear_table()
        self.statusBar().showMessage(tr("list.loading", title=list_ref.title))

        def fetch() -> object:
            return self._bili_client.get_all_fav_folder_items(list_ref.id)

        def on_done(items) -> None:
            if not isinstance(items, list):
                return
            self._show_bili_items(items, list_ref)

        def on_error(message: str) -> None:
            self.statusBar().showMessage(tr("list.folder_failed", message=message))

        run_async(fetch, on_done=on_done, on_error=on_error)

    def _load_bili_watch_later(self, list_ref: _ListRef) -> None:
        self._clear_table()
        self.statusBar().showMessage(tr("list.loading", title=list_ref.title))

        def fetch() -> object:
            return self._bili_client.get_watch_later_items()

        def on_done(items) -> None:
            if not isinstance(items, list):
                return
            # 稍后再看变化频繁:每次打开都刷新侧栏计数
            self._bili_watchlater_count = len(items)
            self._show_bili_items(items, list_ref)
            self._rebuild_sidebar()

        def on_error(message: str) -> None:
            self.statusBar().showMessage(tr("list.watchlater_failed", message=message))

        run_async(fetch, on_done=on_done, on_error=on_error)

    # -- 侧栏 ----------------------------------------------------------------

    # 分区头品牌标:未登录跟随主题灰,已登录用柔和品牌色
    _SOFT_BRAND_COLORS = {"netease": "#E05A5A", "bilibili": "#5AA9E0"}

    def _brand_header_icon(self, source: str):
        logged_in = (
            self._account is not None
            if source == "netease"
            else self._bili_account is not None
        )
        if logged_in:
            return tinted_icon_with_color(
                source, QColor(self._SOFT_BRAND_COLORS[source])
            )
        return tinted_icon(source)

    def _add_header_item(
        self, text: str, icon=None, kind: str | None = None
    ) -> QTreeWidgetItem:
        """顶层分区头:启用但不可选中(注意不能用 NoItemFlags——禁用的父项
        会让子节点无法成为 current/selected,折叠与拖拽都会失效)。"""
        item = QTreeWidgetItem([text])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        if kind is not None:
            item.setData(0, Qt.ItemDataRole.UserRole, (kind, None))
        if icon is not None:
            item.setIcon(0, icon)
        self.sidebar.addTopLevelItem(item)
        return item

    @staticmethod
    def _add_child_item(
        parent: QTreeWidgetItem, text: str, kind: str, payload, selectable: bool = True
    ) -> QTreeWidgetItem:
        """分区内子节点;不可选条目(加载占位)用 selectable=False。"""
        item = QTreeWidgetItem(parent, [text])
        item.setData(0, Qt.ItemDataRole.UserRole, (kind, payload))
        if not selectable:
            item.setFlags(Qt.ItemFlag.NoItemFlags)
        elif kind in ("netease-login", "bili-login"):
            # 登录占位可选但不参与拖拽排序
            item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        return item

    def _rebuild_sidebar(self) -> None:
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        # 「搜索」入口:固定最顶(「最近」之上),图标复用移动端放大镜
        search_item = QTreeWidgetItem([tr("sidebar.search")])
        search_item.setFlags(
            Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        )
        search_item.setData(0, Qt.ItemDataRole.UserRole, ("search", None))
        self.sidebar.addTopLevelItem(search_item)

        # 「最近」分区:跨平台聚合最近播放过的列表,非空才出现
        # (空分区只有占位噪音,首次起播后自然浮现)
        if self._recent:
            recent_header = self._add_header_item(
                tr("sidebar.recent"), None, "recent-header"
            )
            for ref in self._recent:
                title = ref.title
                if ref.kind == "search":
                    # 搜索条目:存储标题用规范前缀,展示前缀随界面语言
                    keyword = ref.title.removeprefix(_SEARCH_TITLE_PREFIX)
                    title = f"{tr('search.recent_prefix')}{keyword}"
                self._add_child_item(
                    recent_header, sanitize_ui_text(title), "recent-item",
                    (ref.kind, ref.id, ref.title),
                )

        netease_header = self._add_header_item(
            tr("sidebar.netease_playlists"),
            self._brand_header_icon("netease"), "netease-header",
        )
        if self._account is None:
            self._add_child_item(
                netease_header, tr("sidebar.netease_login"), "netease-login", None
            )
        else:
            for playlist in self._playlists:
                title = sanitize_ui_text(playlist.name)
                if playlist.track_count:
                    title = f"{title}({playlist.track_count})"
                self._add_child_item(
                    netease_header, title, "netease-playlist", playlist.id
                )
            if not self._playlists:
                self._add_child_item(
                    netease_header, tr("sidebar.loading_playlists"),
                    "netease-loading", None, selectable=False,
                )

        # 收藏的他人歌单:仅网易云已登录时展示(未登录整个分区不出现)
        if self._account is not None:
            subscribed_header = self._add_header_item(
                tr("sidebar.netease_subscribed"),
                self._brand_header_icon("netease"), "netease-subscribed-header",
            )
            # 每日推荐:固定首位(与 B站稍后再看同策略,不参与拖拽排序)
            self._add_child_item(
                subscribed_header, _daily_ref().title, "netease-daily", None
            )
            for playlist in self._subscribed_playlists:
                title = sanitize_ui_text(playlist.name)
                if playlist.track_count:
                    title = f"{title}({playlist.track_count})"
                self._add_child_item(
                    subscribed_header, title, "netease-subscribed-playlist", playlist.id
                )
            if not self._subscribed_playlists:
                self._add_child_item(
                    subscribed_header, tr("sidebar.loading_subscribed"),
                    "netease-subscribed-loading", None, selectable=False,
                )

        bili_header = self._add_header_item(
            tr("sidebar.bili_folders"),
            self._brand_header_icon("bilibili"), "bili-header",
        )
        if self._bili_account is None:
            self._add_child_item(
                bili_header, tr("sidebar.bili_login"), "bili-login", None
            )
        else:
            # 稍后再看:固定首位,kind 不在可排序集合里(不参与拖拽排序)
            watchlater_title = tr("sidebar.watchlater")
            if self._bili_watchlater_count is not None:
                watchlater_title += f"({self._bili_watchlater_count})"
            self._add_child_item(
                bili_header, watchlater_title, "bili-watchlater", None
            )
            for folder in self._bili_folders:
                title = sanitize_ui_text(folder.title)
                if folder.count:
                    title = f"{title}({folder.count})"
                self._add_child_item(bili_header, title, "bili-folder", folder.media_id)
            if not self._bili_folders:
                self._add_child_item(
                    bili_header, tr("sidebar.loading_folders"),
                    "bili-loading", None, selectable=False,
                )

        settings_item = QTreeWidgetItem([tr("sidebar.settings")])
        settings_item.setFlags(
            Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        )
        settings_item.setData(0, Qt.ItemDataRole.UserRole, ("settings", None))
        self.sidebar.addTopLevelItem(settings_item)

        # 全量重建后重新应用折叠状态(新挂载的顶层项默认收起);
        # 折叠三角徽标随 _apply_sidebar_icons 按展开态现算,无需另刷
        expanded = self._settings.get(SETTING_SIDEBAR_EXPANDED, {})
        netease_header.setExpanded(expanded.get("netease", True))
        for row in range(self.sidebar.topLevelItemCount()):
            item = self.sidebar.topLevelItem(row)
            section = _HEADER_SECTION.get(self.sidebar.item_kind(item))
            if section is not None:
                item.setExpanded(expanded.get(section, True))
        self.sidebar.blockSignals(False)
        self._apply_sidebar_icons()

    _SIDEBAR_KIND_ICON = {
        "search": "search",
        "settings": "settings",
        "netease-login": "search",
        "netease-playlist": "library_music",
        "netease-subscribed-playlist": "favorite",
        "netease-daily": "refresh",
        "bili-login": "person",
        "bili-watchlater": "playlist_play",
        "bili-folder": "queue_music",
    }

    _HEADER_BRAND = {
        "netease-header": "netease",
        "netease-subscribed-header": "netease",
        "bili-header": "bilibili",
    }

    def _apply_sidebar_icons(self) -> None:
        """按条目类型补单色图标;分区头品牌标按登录态取色(随主题/登录刷新)。"""
        for row in range(self.sidebar.topLevelItemCount()):
            self._apply_item_icons(self.sidebar.topLevelItem(row))

    def _apply_item_icons(self, item: QTreeWidgetItem) -> None:
        role = item.data(0, Qt.ItemDataRole.UserRole)
        kind = role[0] if isinstance(role, tuple) else None
        brand_source = self._HEADER_BRAND.get(kind)
        if brand_source is not None:
            item.setIcon(0, self._section_header_icon(brand_source, item.isExpanded()))
        elif kind == "recent-header":
            # 「最近」分区头:history 单色标 + 折叠三角(无品牌标可挂)
            item.setIcon(
                0, self._monochrome_header_icon("history", item.isExpanded())
            )
        else:
            # 「最近」子节点按来源类型取图标(payload=(来源 kind, id, title))
            if kind == "recent-item" and isinstance(role[1], tuple):
                icon_name = self._SIDEBAR_KIND_ICON.get(role[1][0])
            else:
                icon_name = self._SIDEBAR_KIND_ICON.get(kind)
            if icon_name:
                item.setIcon(0, tinted_icon(icon_name))
        for row in range(item.childCount()):
            self._apply_item_icons(item.child(row))

    def _sidebar_list_ref(self, kind: str, payload, item: QTreeWidgetItem) -> _ListRef:
        """侧栏条目 → _ListRef;标题取内存里的干净名字(侧栏文本带计数,
        计数会过期,最近栈与状态栏提示都不想带它),查不到回落条目文本。"""
        entry_id = int(payload) if isinstance(payload, int) else 0
        if kind == "netease-playlist":
            for playlist in self._playlists:
                if playlist.id == entry_id:
                    return _ListRef(kind, entry_id, playlist.name)
        elif kind == "netease-subscribed-playlist":
            for playlist in self._subscribed_playlists:
                if playlist.id == entry_id:
                    return _ListRef(kind, entry_id, playlist.name)
        elif kind == "bili-folder":
            for folder in self._bili_folders:
                if folder.media_id == entry_id:
                    return _ListRef(kind, entry_id, folder.title)
        elif kind == "bili-watchlater":
            return _ListRef("bili-watchlater", 0, tr("sidebar.watchlater"))
        return _ListRef(kind, entry_id, item.text(0))

    def _open_list_ref(self, list_ref: _ListRef) -> None:
        """按列表来源分发加载(侧栏原生条目与「最近」条目共用)。"""
        # 浏览实体列表即退出搜索模式(搜索分支会在下方重新进入)
        self._set_search_mode(False)
        self.central_stack.setCurrentIndex(_PAGE_TABLE)
        if list_ref.kind in ("netease-playlist", "netease-subscribed-playlist"):
            self._load_playlist_tracks(list_ref)
        elif list_ref.kind == "netease-daily":
            self._load_netease_daily()
        elif list_ref.kind == "bili-folder":
            self._load_bili_folder_tracks(list_ref)
        elif list_ref.kind == "bili-watchlater":
            self._load_bili_watch_later(list_ref)
        elif list_ref.kind == "search":
            # 「最近」里的搜索条目回放:标题剥前缀还原关键词重搜
            self._open_search_page(
                list_ref.title.removeprefix(_SEARCH_TITLE_PREFIX)
            )

    def _on_sidebar_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        role = item.data(0, Qt.ItemDataRole.UserRole)
        kind, payload = role if isinstance(role, tuple) else ("", None)
        # 分区头:单击切换折叠/展开并持久化
        if item.parent() is None and kind in _HEADER_SECTION:
            self._toggle_sidebar_section(item)
            return
        if kind == "settings":
            self.central_stack.setCurrentIndex(_PAGE_SETTINGS)
            return
        if kind == "search":
            self._open_search_page()
            return
        if kind == "netease-login":
            self.central_stack.setCurrentIndex(_PAGE_LOGIN)
            return
        if kind in ("netease-playlist", "netease-subscribed-playlist"):
            # 自建与收藏歌单走同一展开/播放路径(歌单详情接口对两者一致)
            self._open_list_ref(self._sidebar_list_ref(kind, payload, item))
            return
        if kind == "netease-daily":
            self._open_list_ref(_daily_ref())
            return
        if kind == "bili-folder":
            self._open_list_ref(self._sidebar_list_ref(kind, payload, item))
            return
        if kind == "bili-watchlater":
            self._open_list_ref(self._sidebar_list_ref(kind, payload, item))
            return
        if kind == "recent-item":
            # payload = (来源 kind, id, 干净标题):按来源分发,加载失败
            # (远端已删/未登录)由各加载器的错误路径提示
            if isinstance(payload, tuple) and len(payload) == 3:
                origin_kind, entry_id, title = payload
                self._open_list_ref(_ListRef(origin_kind, int(entry_id), str(title)))
            return
        if kind == "bili-login":
            self.central_stack.setCurrentIndex(_PAGE_TABLE)
            self._set_search_mode(False)
            self._clear_table()
            self._handle_bili_section_click()
            return
        # 加载占位等不可选条目不动作(等价旧行为:不可选中即不触发分发)
        if not (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            return
        self.central_stack.setCurrentIndex(
            _PAGE_LOGIN if self._account is None else _PAGE_TABLE
        )

    # -- 侧栏折叠与排序持久化 ---------------------------------------------------

    def _toggle_sidebar_section(self, header: QTreeWidgetItem) -> None:
        """单击分区头:切换折叠/展开(持久化经 itemExpanded/itemCollapsed 统一处理)。"""
        header.setExpanded(not header.isExpanded())

    # -- 分区头折叠三角(QPainter 绘制,不落字体系统) --------------------------

    _ARROW_POINTS = {
        # 12x12 逻辑坐标里的实心三角:展开朝下 / 收起朝右
        True: [(2.0, 4.5), (10.0, 4.5), (6.0, 8.5)],
        False: [(4.5, 2.0), (4.5, 10.0), (8.5, 6.0)],
    }

    @staticmethod
    def _arrow_pixmap(expanded: bool) -> QPixmap:
        """折叠指示三角:主题灰、2x DPR 抗锯齿;不走字体(DirectWrite 回退
        字体会带来 ~50MB 常驻内存,见 _SidebarTree 注释)。"""
        dpr = 2.0
        pix = QPixmap(int(12 * dpr), int(12 * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(theme.current_palette()["onSurfaceVariant"])
        color.setAlpha(200)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(
            QPolygonF([QPointF(x, y) for x, y in MainWindow._ARROW_POINTS[expanded]])
        )
        painter.end()
        return pix

    def _section_header_icon(self, source: str, expanded: bool) -> QIcon:
        """分区头图标 = 品牌标(18px)+ 右侧折叠三角,合成在 30x18 画布。"""
        return self._composite_header_icon(self._brand_header_icon(source), expanded)

    def _monochrome_header_icon(self, icon_name: str, expanded: bool) -> QIcon:
        """无品牌标的分区头(「最近」):主题色单色标 + 折叠三角。"""
        return self._composite_header_icon(tinted_icon(icon_name), expanded)

    # 分区头合成画布的设备像素比:2x 栅格化。高分屏(125%~200% 缩放)下
    # DPR=1 的 30x18 画布会被拉伸 1.25~2 倍渲染,品牌标/三角发糊;
    # 2x 覆盖到 200% 缩放仍为原生物理像素(更高缩放下也只轻微上采样)。
    _HEADER_ICON_DPR = 2.0

    @classmethod
    def _composite_header_icon(cls, base_icon: QIcon, expanded: bool) -> QIcon:
        dpr = cls._HEADER_ICON_DPR
        canvas = QPixmap(int(30 * dpr), int(18 * dpr))
        canvas.setDevicePixelRatio(dpr)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        # 品牌标经 icon.paint 按画布物理尺寸(18*dpr)栅格化:SVG 引擎矢量
        # 直出该尺寸,染色标取最近底图缩放,都不会再被 DPI 拉伸
        base_icon.paint(painter, 0, 0, 18, 18)
        painter.drawPixmap(18, 3, cls._arrow_pixmap(expanded))  # 自带 2x DPR
        painter.end()
        return QIcon(canvas)

    def _on_sidebar_section_toggled(self, item: QTreeWidgetItem) -> None:
        """分区头展开状态变化(单击分区头切换;重建期被 blockSignals 抑制)
        → 写盘(键见 store.SETTING_SIDEBAR_EXPANDED)并刷新三角指示。

        刷新走 _apply_item_icons 按条目 kind 分发:平台分区头是品牌标合成,
        「最近」分区头是单色 history 合成——不能拿分区键直接当品牌名调
        _section_header_icon(netease-subscribed / bili / recent 会 KeyError
        在槽内被吞,角标冻结不转向,v0.2.0 起的老 bug)。
        """
        section = _HEADER_SECTION.get(_SidebarTree.item_kind(item))
        if section is None:
            return  # 非分区头不涉及
        flags = self._settings.setdefault(
            SETTING_SIDEBAR_EXPANDED,
            {key: True for key in _HEADER_SECTION.values()},
        )
        flags[section] = item.isExpanded()
        self._store.save_settings(self._settings)
        self._apply_item_icons(item)

    def _on_sidebar_order_changed(self, section: str) -> None:
        """分区内拖拽排序落库:读子节点顺序写设置键,并同步内存列表。"""
        ids = self.sidebar.section_child_ids(section)
        if section == "netease":
            setting = SETTING_NETEASE_PLAYLIST_ORDER
            by_id = {p.id: p for p in self._playlists}
        elif section == "netease-subscribed":
            setting = SETTING_NETEASE_SUBSCRIBED_ORDER
            by_id = {p.id: p for p in self._subscribed_playlists}
        else:
            setting = SETTING_BILI_FOLDER_ORDER
            by_id = {f.media_id: f for f in self._bili_folders}
        self._settings[setting] = ids
        self._store.save_settings(self._settings)
        # 内存列表同步成相同顺序,后续 _rebuild_sidebar 不会回退到旧顺序
        if len(by_id) == len(ids):
            ordered = [by_id[i] for i in ids]
            if section == "netease":
                self._playlists = ordered
            elif section == "netease-subscribed":
                self._subscribed_playlists = ordered
            else:
                self._bili_folders = ordered

    def _apply_stored_order(self, items: list, setting: str, id_of) -> list:
        """加载后套用存储顺序:已知 id 按存储序排前、新条目追加尾部;
        顺序有变化(含存储里失效 id 的静默清理)才写盘。"""
        stored = list(self._settings.get(setting, []))
        ordered = apply_stored_order(stored, [id_of(i) for i in items])
        by_id = {id_of(i): i for i in items}
        result = [by_id[i] for i in ordered]
        if ordered != stored:
            self._settings[setting] = ordered
            self._store.save_settings(self._settings)
        return result

    # -- 侧栏宽度钳制 ----------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._clamp_sidebar_width()

    def _clamp_sidebar_width(self) -> None:
        """把侧栏宽度限制在窗口宽度的 1/10 ~ 1/2(拖动与窗口缩放都生效)。"""
        window_width = max(self.width(), 1)
        lower = max(140, window_width // 10)
        upper = max(lower + 1, window_width // 2)
        self.sidebar.setMinimumWidth(lower)
        self.sidebar.setMaximumWidth(upper)
        current = self.sidebar.width()
        if current and (current < lower or current > upper):
            self._splitter.setSizes([min(max(current, lower), upper), 1])

    # -- 歌曲表 ---------------------------------------------------------------

    def _update_table_empty_state(self) -> None:
        """表空时切到空态视图;文案按登录状态区分。"""
        empty = self.song_table.rowCount() == 0
        self.table_stack.setCurrentIndex(1 if empty else 0)
        if empty:
            if self._account is None and self._bili_account is None:
                self._empty_state.set_hint(tr("empty.not_logged_in"))
            else:
                self._empty_state.set_hint(tr("empty.pick_from_sidebar"))

    def _clear_table(self) -> None:
        """清空表格与浏览上下文(切列表/登出时的加载中状态)。"""
        self._table_songs = []
        self._table_context = None
        self.song_table.setRowCount(0)
        self._cover_loader.preload([])  # 作废仍在途的缩略图预取
        self._update_playing_row()  # 均衡器条随清表复位
        self._update_table_empty_state()

    def _set_table_songs(
        self, songs: list[QueueSong], header_source: str, context: _ListRef | None
    ) -> None:
        """把一个列表的歌曲填进表格(仅浏览;装入播放队列走双击)。

        header_source 为 "netease" | "bili"(表头文案随语言现算,语言切换
        时按它重取)。
        """
        self._table_header_source = header_source
        self._table_songs = songs
        self._table_context = context
        self._fill_song_table()

    def _fill_song_table(self) -> None:
        songs = self._table_songs
        header = (
            _header_bili()
            if self._table_header_source == "bili"
            else _header_netease()
        )
        self.song_table.setHorizontalHeaderLabels(header)
        self.song_table.setRowCount(len(songs))
        for row, song in enumerate(songs):
            number = QTableWidgetItem(str(row + 1))
            number.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.song_table.setItem(row, _TABLE_COL_NUMBER, number)
            # 封面缩略图:无 icon 的空 item 即透明占位,预取回来后 setIcon
            self.song_table.setItem(row, _TABLE_COL_COVER, QTableWidgetItem())
            self.song_table.setItem(row, 2, QTableWidgetItem(song.title))
            self.song_table.setItem(row, 3, QTableWidgetItem(song.artist))
            duration = (
                format_seconds(song.duration_ms / 1000) if song.duration_ms else "--:--"
            )
            self.song_table.setItem(row, 4, QTableWidgetItem(duration))
        self._preload_table_covers()
        self._update_playing_row()  # 重填后当前曲行号重算(均衡器条跟行)
        self._update_table_empty_state()

    # -- 表格封面缩略图 -----------------------------------------------------

    def _preload_table_covers(self) -> None:
        """按行序把整表封面交给预取池;切表再调即作废上一批(空表亦然)。"""
        self._cover_loader.preload([song.cover_url for song in self._table_songs])

    def _on_table_cover_ready(self, url: str, data: bytes) -> None:
        """缩略图到达:当前表没有该 URL(切表后迟到的回调)直接丢弃。"""
        rows = [
            row for row, song in enumerate(self._table_songs)
            if song.cover_url == url
        ]
        if not rows:
            return
        icon = self._cover_icons.get(url)
        if icon is None:
            pixmap = QPixmap()
            if not pixmap.loadFromData(data):
                return
            dpr = self.song_table.devicePixelRatioF()
            physical = max(1, round(_TABLE_COVER_SIZE * dpr))
            pixmap = rounded_pixmap(
                pixmap, physical, max(1, round(_TABLE_COVER_RADIUS * dpr))
            )
            pixmap.setDevicePixelRatio(dpr)
            icon = QIcon(pixmap)
            if len(self._cover_icons) >= _TABLE_ICON_CACHE_MAX:
                self._cover_icons.pop(next(iter(self._cover_icons)), None)
            self._cover_icons[url] = icon
        for row in rows:
            item = self.song_table.item(row, _TABLE_COL_COVER)
            if item is not None:
                item.setIcon(icon)

    # -- 正在播放均衡器指示 ---------------------------------------------------

    def _update_playing_row(self) -> None:
        """当前播放曲在表中的行号(source+id 匹配;不在当前表为 None)。"""
        active = self._active
        row = None
        if active is not None:
            song = active.song
            row = next(
                (
                    r
                    for r, item in enumerate(self._table_songs)
                    if item.source == song.source and item.id == song.id
                ),
                None,
            )
        if row == self._eq_state.row:
            return
        old_row = self._eq_state.row
        self._eq_state.row = row
        for candidate in (old_row, row):
            self._repaint_table_row(candidate)
        if self._playing and row is not None:
            self._eq_timer.start()

    def _repaint_table_row(self, row: int | None) -> None:
        if row is None or row >= self.song_table.rowCount():
            return
        item = self.song_table.item(row, _TABLE_COL_NUMBER)
        if item is not None:
            self.song_table.viewport().update(
                self.song_table.visualItemRect(item)
            )

    def _eq_set_active(self, playing: bool) -> None:
        """播放态:条的跳动幅度 level 在 1<->0 间过渡;仅播放且当前行
        在表内时跑相位 timer(暂停时条平齐,无需驱动)。"""
        self._playing = playing
        self._eq_level.stop()
        self._eq_level.setStartValue(float(self._eq_state.level))
        self._eq_level.setEndValue(1.0 if playing else 0.0)
        self._eq_level.start()
        if playing:
            if self._eq_state.row is not None:
                self._eq_last_tick = time.monotonic()
                self._eq_timer.start()
        else:
            self._eq_timer.stop()
            # 回落动画本身需要重绘进度条上的行
            self._repaint_table_row(self._eq_state.row)

    def _on_eq_level(self, value) -> None:
        self._eq_state.level = float(value)
        self._repaint_table_row(self._eq_state.row)

    def _eq_tick(self) -> None:
        now = time.monotonic()
        self._eq_state.phase_ms = (
            self._eq_state.phase_ms + (now - self._eq_last_tick) * 1000.0
        ) % 3_600_000.0  # 取模防累计溢出(不影响三周期相位)
        self._eq_last_tick = now
        self._repaint_table_row(self._eq_state.row)

    # -- 任务栏歌名标题 -------------------------------------------------------

    def _apply_song_window_title(self) -> None:
        """起播:窗口标题(=任务栏文案)换成歌名,宽度规范到与基础标题
        一致(taskbar_title);暂停/失败保持歌名不回退,停止/播完才恢复。"""
        if self._active is None:
            return
        self.setWindowTitle(
            taskbar_title(self._active.song.title, QFontMetrics(self.font()))
        )

    def _reset_window_title(self) -> None:
        self.setWindowTitle(_TITLE_BASE)

    # -- 搜索 ----------------------------------------------------------------

    def _open_search_page(self, keyword: str = "") -> None:
        """进入搜索模式(显示页头);带关键词时直接执行一次搜索。"""
        self.central_stack.setCurrentIndex(_PAGE_TABLE)
        self._set_search_mode(True)
        if keyword and keyword != self._search_keyword:
            self._search_input.setText(keyword)
        if keyword:
            self._run_search(keyword, reset=True)
        elif not self._table_songs:
            self._empty_state.set_hint(tr("search.empty_hint"))
        self._search_input.setFocus()
        self._search_input.selectAll()

    def _set_search_mode(self, active: bool) -> None:
        """页头与「加载更多」只在搜索模式出现;退出即作废在途搜索。"""
        if not active:
            self._search_generation += 1
        self._search_header.setVisible(active)
        self._search_more_btn.setVisible(active and self._search_has_more)

    def _on_search_return_pressed(self) -> None:
        keyword = self._search_input.text().strip()
        if not keyword:
            self.statusBar().showMessage(tr("search.keyword_empty"))
            return
        self._run_search(keyword, reset=True)

    def _on_search_source_changed(self, source: str) -> None:
        """切换来源:同步按钮态与指示线;已有关键词时立即按新来源重搜。"""
        if source == self._search_source:
            return
        self._search_source = source
        self._search_netease_btn.setChecked(source == "netease")
        self._search_bili_btn.setChecked(source == "bili")
        self._update_source_indicator(animate=True)
        if self._search_keyword:
            self._run_search(self._search_keyword, reset=True)

    def _update_source_indicator(self, animate: bool = True) -> None:
        """指示线贴选中按钮、按文字宽居中(布局变化即时对位,切换时滑动)。"""
        button = (
            self._search_bili_btn
            if self._search_source == "bili"
            else self._search_netease_btn
        )
        text_width = button.fontMetrics().horizontalAdvance(button.text())
        line_width = max(24, min(button.width(), text_width + 28))
        self._source_bar.move_indicator(
            QRect(
                button.x() + (button.width() - line_width) // 2,
                0,
                line_width,
                3,
            ),
            animate,
        )

    def _on_search_more_clicked(self) -> None:
        self._run_search(self._search_keyword, reset=False)

    def _run_search(self, keyword: str, reset: bool) -> None:
        """执行/续拉一页搜索;reset=True 重置页码与表格,否则追加。"""
        self._search_keyword = keyword
        page = 1 if reset else self._search_page + 1
        self._search_page = page
        self._search_generation += 1
        generation = self._search_generation
        source = self._search_source
        if reset:
            self._clear_table()
            self._empty_state.set_hint(tr("search.searching_hint"))
        self._search_more_btn.setVisible(False)
        self.statusBar().showMessage(tr("search.searching", keyword=keyword))
        if source == "bili":
            def fetch() -> object:
                return self._bili_client.search_videos(keyword, page)

            def on_done(result) -> None:
                if generation != self._search_generation:
                    return
                items, has_more = result
                self._apply_search_page(
                    [
                        _bili_item_to_queue(
                            item.id, item.bvid or "", item.title or "",
                            item.upper_name, item.duration_sec, item.cover_url,
                        )
                        for item in items if item.playable
                    ],
                    "bili",
                    has_more,
                )
        else:
            def fetch() -> object:
                return self._client.search_songs(
                    keyword,
                    limit=_SEARCH_PAGE_SIZE,
                    offset=(page - 1) * _SEARCH_PAGE_SIZE,
                )

            def on_done(result) -> None:
                if generation != self._search_generation:
                    return
                songs, total = result
                self._apply_search_page(
                    [_netease_song_to_queue(song) for song in songs],
                    "netease",
                    page * _SEARCH_PAGE_SIZE < total,
                )

        def on_error(message) -> None:
            if generation != self._search_generation:
                return
            self.statusBar().showMessage(tr("search.failed", message=message))
            if not self._table_songs:
                self._empty_state.set_hint(tr("search.failed_hint"))

        run_async(fetch, on_done, on_error)

    def _apply_search_page(
        self, songs: list[QueueSong], header_source: str, has_more: bool
    ) -> None:
        """一页搜索结果落表(续拉时与已加载结果合并整表重填,缩略图走缓存)。"""
        merged = self._table_songs + songs if self._search_page > 1 else songs
        self._set_table_songs(
            merged,
            header_source,
            _ListRef("search", 0, f"{_SEARCH_TITLE_PREFIX}{self._search_keyword}"),
        )
        self._search_has_more = has_more
        self._search_more_btn.setVisible(has_more)
        self.statusBar().showMessage(
            tr(
                "search.result_loaded",
                keyword=self._search_keyword, count=len(merged),
            )
        )

    # -- 播放 ----------------------------------------------------------------

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        self._play_from_table(row)

    def _play_from_table(self, row: int) -> None:
        """双击表格行起播:浏览的列表 ≠ 队列来源(或队列已被插播改写)时,
        先把表格列表整体装入队列(即「切到该列表播放」),再跳到所点行。

        同列表且未插播时等价旧行为的纯 jump(保留随机模式已播历史)。
        """
        if not (0 <= row < len(self._table_songs)):
            return
        if self._table_context != self._queue_context or self._queue_dirty:
            self._queue.replace(list(self._table_songs))
            self._queue_context = self._table_context
            self._queue_dirty = False
        self._play_at(row)

    def _on_table_context_menu(self, pos) -> None:
        """歌曲表右键菜单:「下一首播放」(插到当前曲后,不动队列其余部分)。"""
        row = self.song_table.indexAt(pos).row()
        if not (0 <= row < len(self._table_songs)):
            return
        menu = QMenu(self.song_table)
        play_next_action = menu.addAction(tr("play.play_next_menu"))
        chosen = menu.exec(self.song_table.viewport().mapToGlobal(pos))
        if chosen is play_next_action:
            self._insert_next_from_table(row)

    def _insert_next_from_table(self, row: int) -> None:
        song = self._table_songs[row]
        position = self._queue.insert_next(song)
        # 队列偏离其来源列表:再双击同列表也需重装(replace 回净表内容)
        self._queue_dirty = True
        # queue_changed 订阅方会作废下一首预取(peek 目标已变成插播曲)
        self.statusBar().showMessage(
            tr("play.play_next_done", title=song.title, position=position + 1)
        )

    def _play_at(self, index: int) -> None:
        # 代际与跳歌 token 自增放在最前:任何新的点歌都作废仍在途的旧解析
        # 回调与未触发的失败自动跳过(允许新请求覆盖旧请求,防解析卡死)。
        self._resolve_generation += 1
        self._fail_skip_token += 1
        song = self._queue.item_at(index)
        if song is None:
            return
        if self.engine is None:
            self.statusBar().showMessage(
                self._engine_error or tr("play.engine_unavailable")
            )
            return
        generation = self._resolve_generation
        self._queue.jump(index)
        self.player_bar.set_track(song.title, song.artist)
        self.player_bar.set_cover(song.cover_url)
        self._request_dynamic_seed(song)

        prefetched = self._take_prefetched(index, song)
        if prefetched is not None:
            # 预取命中:直接用缓存的播放上下文起播,零网络等待;
            # from_prefetch 标记供 _on_load_failed 兜底(链接过期重解析一次)
            self._start_play(
                song,
                prefetched.rotator,
                prefetched.headers,
                status=prefetched.status,
                from_prefetch=True,
            )
            return

        self.statusBar().showMessage(tr("play.resolving", title=song.title))
        self._resolve_and_start(song, generation)

    def _resolve_song_context(self, song: QueueSong) -> _SongContext:
        """解析一首歌的播放上下文(网络部分):点歌起播与预取共用同一逻辑。

        网易云走 resolve_playable_url(含音质设置回退链),B站走
        resolve_audio_stream + 流请求头,构建 _start_play 所需的
        rotator/headers/状态文案。登录失效与解析失败以异常抛出,
        由调用方按各自策略处理(起播要提示,预取则静默吞掉)。
        """
        # 音质偏好取读快照即可:设置只在 UI 线程经设置页写入,dict 读原子
        quality = self._settings.get(SETTING_PLAY_QUALITY, "lossless")
        if song.source == "bili":
            # B站用各自的偏好键体系:网易云档位先换算,不满足时
            # selector 内部自带降级,无需在这里重试
            stream = self._bili_client.resolve_audio_stream(
                song.bvid,
                preferred_quality=bili_quality_key_from_netease_level(quality),
            )
            # B站 m4s 音频流要求 Referer + 浏览器 UA,否则 403;
            # candidate_urls 含主 URL 与 backupUrls,加载失败时轮换
            return _SongContext(
                rotator=BackupUrlRotator(stream.candidate_urls),
                headers=build_bili_stream_headers(),
                status=tr(
                    "play.playing",
                    title=song.title, quality=_bili_quality_suffix(stream),
                ),
            )
        playable = self._client.resolve_playable_url(
            song.id, song.duration_ms, preferred_quality=quality
        )
        # 试听片段提示保持原样不追加音质;完整播放展示实际生效档位
        status = (
            tr("play.preview")
            if playable.is_preview
            else tr(
                "play.playing",
                title=song.title,
                quality=_netease_quality_suffix(playable.level),
            )
        )
        # 网易云不参与 backupUrls 轮换:音质回退链已在解析层完成
        return _SongContext(
            rotator=BackupUrlRotator([playable.url]), headers=None, status=status
        )

    def _resolve_and_start(self, song: QueueSong, generation: int) -> None:
        """后台解析播放地址并起播(点歌与预取过期重解析共用)。

        回调带代际 token:已被更新的点歌覆盖的迟到结果一律静默作废
        (不计失败、不跳歌、不重登)。
        """
        if song.source == "bili":
            on_done = self._make_bili_play_done(song, generation)
        else:
            on_done = self._make_netease_play_done(song, generation)

        def resolve() -> tuple[str, object]:
            try:
                return ("ok", self._resolve_song_context(song))
            except (NeteaseAuthRequiredError, BiliAuthRequiredError):
                return ("auth", "")
            except Exception as error:  # noqa: BLE001 - 边界处统一转消息
                return ("error", str(error) or error.__class__.__name__)

        def on_error(message: str) -> None:
            if generation != self._resolve_generation:
                # 过期请求:已被更新的点歌覆盖,静默作废(不计失败、不跳歌)
                return
            self._handle_play_failure(song, message)

        run_async(resolve, on_done=on_done, on_error=on_error)

    def _make_netease_play_done(self, song: QueueSong, generation: int):
        def on_done(result) -> None:
            if generation != self._resolve_generation:
                return  # 过期请求:已被更新的点歌覆盖,结果作废
            kind, payload = result
            if kind == "auth":
                self._handle_stale_login(tr("login.status.stale"))
                return
            if kind == "error":
                self._handle_play_failure(song, payload)
                return
            self._start_play(song, payload.rotator, payload.headers, status=payload.status)

        return on_done

    def _make_bili_play_done(self, song: QueueSong, generation: int):
        def on_done(result) -> None:
            if generation != self._resolve_generation:
                return  # 过期请求:已被更新的点歌覆盖,结果作废
            kind, payload = result
            if kind == "auth":
                self._handle_bili_stale_login(tr("bili.auth_expired_sidebar"))
                return
            if kind == "error":
                self._handle_play_failure(song, payload)
                return
            self._start_play(song, payload.rotator, payload.headers, status=payload.status)

        return on_done

    def _start_play(
        self,
        song: QueueSong,
        rotator: BackupUrlRotator,
        headers: dict[str, str] | None,
        status: str | None = None,
        from_prefetch: bool = False,
    ) -> None:
        url = rotator.current
        if not url:
            # 解析成功但没有任何可用地址:同样是播放失败,走统一出口
            self._handle_play_failure(song, tr("play.no_url"))
            return
        self._active = _ActivePlay(
            song=song, rotator=rotator, headers=headers, from_prefetch=from_prefetch
        )
        self._update_playing_row()  # 新起播:均衡器条移到新行
        self._play_fail_count = 0  # 成功发起播放:连续失败计数复位
        self._set_play_controls_active(True)
        self._apply_song_window_title()  # 任务栏文案换为歌名(等宽规范)
        self.statusBar().showMessage(
            status or tr("play.playing", title=song.title, quality="")
        )
        self.engine.play_url(url, headers)
        self._push_recent()  # 成功起播才算「听过」(仅浏览不压栈)
        self._schedule_prefetch_next()  # 起播成功:顺手后台预取下一首

    # -- 「最近」列表 ------------------------------------------------------------

    def _push_recent(self) -> None:
        """把队列来源列表压入最近栈(新→旧):去重置顶、标题刷新、按上限截断。

        仅当栈内容实际变化才落盘+重建侧栏(同一列表连播时栈顶不变,
        不刷侧栏避免每首歌重建一次)。
        """
        context = self._queue_context
        if context is None:
            return
        entry = _ListRef(kind=context.kind, id=context.id, title=context.title)
        remaining = [
            ref for ref in self._recent if (ref.kind, ref.id) != (entry.kind, entry.id)
        ]
        max_entries = int(
            self._settings.get(SETTING_RECENT_MAX, DEFAULT_RECENT_MAX)
        )
        updated = [entry, *remaining][:max_entries]
        if updated == self._recent:
            return
        self._recent = updated
        self._settings[SETTING_RECENT_LISTS] = [
            {"kind": ref.kind, "id": ref.id, "title": ref.title} for ref in updated
        ]
        self._store.save_settings(self._settings)
        self._rebuild_sidebar()

    # -- 动态取色(M5) --------------------------------------------------------

    def _request_dynamic_seed(self, song: QueueSong) -> None:
        """点歌时请求封面种子色:缓存命中直接套用,否则交 seed loader。

        无封面的歌回退静态冻结色板(对齐 Android:activeCoverSeedHex
        为空即用默认种子)。代际自增作废在途回调(快速切歌不打架)。
        """
        self._seed_generation += 1
        url = song.cover_url or ""
        self._seed_url = url
        if not url:
            self._revert_static_theme()
            return
        cached = cover_seed.cache_get(url)
        if cached is not None:
            self._apply_dynamic_seed(cached)
            return
        self._seed_loader.request(url)

    def _on_seed_cover_ready(self, url: str, data: bytes) -> None:
        """封面字节到手:后台线程提取种子(QImage 解码+直方图),回 UI 应用。"""
        if url != self._seed_url:
            return  # 已切歌,过期结果丢弃
        generation = self._seed_generation

        def extract() -> object:
            return cover_seed.extract_seed_hex(data)

        def on_done(hex_color) -> None:
            if generation != self._seed_generation or url != self._seed_url:
                return
            if not isinstance(hex_color, str):
                return  # 坏图:维持现状
            cover_seed.cache_put(url, hex_color)
            self._apply_dynamic_seed(hex_color)

        run_async(extract, on_done=on_done)

    def _apply_dynamic_seed(self, hex_color: str) -> None:
        """把种子色套成动态主题(开关关闭时只记住不应用)。"""
        seed = seed_from_hex(hex_color)
        if seed == self._dynamic_seed:
            return
        self._dynamic_seed = seed
        if self._settings.get(SETTING_DYNAMIC_COLOR, True):
            self.themes.apply_dynamic(seed)

    def _revert_static_theme(self) -> None:
        """回退静态冻结色板(歌无封面 / 关闭开关)。"""
        self._dynamic_seed = None
        self.themes.apply(self.themes.name)

    def _schedule_prefetch_next(self) -> None:
        """预取下一首的播放地址(run_async 后台,UI 线程零网络铁律)。

        - peek_next 返回 None(随机模式 / 已到队尾)或等于当前索引
          (单曲循环,URL 已在手)时不预取。
        - 解析成功 → 存入 self._prefetched 单槽;失败静默吞掉——
          预取是投机行为,不给用户任何提示。
        - 结果归来时按代际作废:期间用户已切歌,预取大概率无用,不占槽。
        """
        index = self._queue.peek_next()
        if index is None or index == self._queue.current_index():
            return
        song = self._queue.item_at(index)
        if song is None:
            return
        generation = self._resolve_generation

        def prefetch() -> _PrefetchedPlay | None:
            try:
                context = self._resolve_song_context(song)
            except Exception:  # noqa: BLE001 - 预取失败静默,不打扰用户
                return None
            return _PrefetchedPlay(
                queue_index=index,
                song=song,
                rotator=context.rotator,
                headers=context.headers,
                status=context.status,
                created_at=time.monotonic(),
            )

        def on_prefetched(result) -> None:
            if generation != self._resolve_generation:
                return  # 期间已切歌:预取结果作废,不占槽
            if result is not None:
                self._prefetched = result

        run_async(prefetch, on_done=on_prefetched)

    def _take_prefetched(self, index: int, song: QueueSong) -> _PrefetchedPlay | None:
        """取预取槽(无论命中与否都清槽):命中需索引、歌曲标识、TTL 三重校验。

        手动跳歌自然未命中即作废,无需特判;过期宁可重新解析也不吃 403。
        """
        slot = self._prefetched
        self._prefetched = None
        if slot is None:
            return None
        if slot.queue_index != index or (slot.song.source, slot.song.id) != (
            song.source,
            song.id,
        ):
            return None
        if time.monotonic() - slot.created_at > _PREFETCH_TTL_S:
            return None
        return slot

    def _clear_prefetched(self) -> None:
        """作废预取槽(切歌单 replace / 换播放模式等 peek 依据变化的场景)。"""
        self._prefetched = None

    def _on_load_failed(self, message: str) -> None:
        """engine 加载失败类错误:B站按 backupUrls 候选轮换重试,耗尽才报失败。"""
        active = self._active
        if active is None:
            # 无进行中的播放上下文:没有候选可轮换、没有歌曲可跳,维持仅提示
            self.statusBar().showMessage(tr("play.failed", message=message))
            return
        if active.rotator.has_next():
            next_url = active.rotator.advance()
            self.statusBar().showMessage(
                tr(
                    "play.failed_retry",
                    position=active.rotator.position,
                    total=len(active.rotator),
                    title=active.song.title,
                )
            )
            self.engine.play_url(next_url, active.headers)
            return
        if active.from_prefetch:
            # 预取缓存的地址连备用候选也耗尽:大概率是链接过期(TTL 内的
            # 极端情况)。先清标记(重解析起播的 _ActivePlay 不再带
            # from_prefetch,防循环),后台重新解析同一首歌一次走正常
            # 起播流程;再失败才按既有失败路径走。
            active.from_prefetch = False
            self.statusBar().showMessage(
                tr("play.prefetch_expired", title=active.song.title)
            )
            self._resolve_and_start(active.song, self._resolve_generation)
            return
        self._handle_play_failure(active.song, message)

    def _handle_play_failure(self, song: QueueSong, message: str) -> None:
        """统一播放失败出口:非阻塞提示(状态栏+托盘气泡)+ 延时自动跳下一首。

        - 状态栏与托盘气泡只提示不打断(窗口在后台/托盘时气泡仍可见)。
        - 未达熔断上限:延时 _FAIL_SKIP_DELAY_MS 后自动接播下一首。
        - 连续失败达 _MAX_PLAY_FAILS 即熔断:只提示、不再自动接播。
        - 登录失效(kind=="auth")不经此处,仍走原有重登流程,绝不跳歌。
        """
        self.statusBar().showMessage(
            tr("play.failed_song", title=song.title, message=message)
        )
        if self._tray is not None:
            self._tray.show_message(
                tr("play.failed_title"), f"{song.title}\n{message}"
            )
        self._play_fail_count += 1
        if self._play_fail_count >= _MAX_PLAY_FAILS:
            self.statusBar().showMessage(
                tr("play.circuit_breaker", count=_MAX_PLAY_FAILS)
            )
            if self._tray is not None:
                self._tray.show_message(
                    tr("play.circuit_breaker_title"),
                    tr("play.circuit_breaker_tray", count=_MAX_PLAY_FAILS),
                )
            return
        self.statusBar().showMessage(
            tr(
                "play.failed_auto_skip", title=song.title, message=message,
            )
        )
        # 自增 token:作废可能仍在等待的旧自动跳过;期间用户手动点歌同样作废
        self._fail_skip_token += 1
        token = self._fail_skip_token

        def skip() -> None:
            if token != self._fail_skip_token:
                return  # 已被新点歌/新失败调度作废,本次自动跳过取消
            self._advance_after_failure()

        QTimer.singleShot(_FAIL_SKIP_DELAY_MS, skip)

    def _advance_after_failure(self) -> None:
        """失败后的自动接播(由 _handle_play_failure 的延时回调触发)。

        推进语义见 PlayQueue.advance_after_failure:单曲循环也切下一首、
        队列到尾不回绕;None 表示队列已到尾,停止并提示。
        """
        index = self._queue.advance_after_failure()
        if index is None:
            self.statusBar().showMessage(tr("play.failed_end"))
            return
        self._play_at(index)

    def _play_next(self) -> None:
        index = self._queue.next()
        if index is None:
            return
        self._play_at(index)

    def _play_prev(self) -> None:
        index = self._queue.prev()
        if index is None:
            return
        self._play_at(index)

    def _on_track_ended(self) -> None:
        """一首播完:按模式自动接播(单曲循环重播当前,顺序到尾即停)。"""
        self._play_fail_count = 0  # 正常播完一首:连续失败计数复位
        if self._queue.mode() is PlayMode.REPEAT_ONE and self._active is not None:
            url = self._active.rotator.current
            if url:
                self.statusBar().showMessage(
                    tr("play.repeat_one", title=self._active.song.title)
                )
                self.engine.play_url(url, self._active.headers)
                return
        index = self._queue.advance_ended()
        if index is None:
            self.player_bar.set_playing(False)
            self._reset_window_title()  # 顺序播完队尾:任务栏恢复基础标题
            self.statusBar().showMessage(tr("play.finished"))
            return
        self._play_at(index)

    # -- 播放模式 --------------------------------------------------------------

    def _cycle_mode(self) -> None:
        current = self._queue.mode()
        try:
            position = _MODE_CYCLE.index(current)
        except ValueError:
            position = 0
        self._set_play_mode(_MODE_CYCLE[(position + 1) % len(_MODE_CYCLE)])

    def _set_play_mode(self, mode: PlayMode) -> None:
        self._queue.set_mode(mode)
        self._clear_prefetched()  # peek 依据模式而变:换模式即作废预取槽
        self._settings["play_mode"] = mode.value
        self._store.save_settings(self._settings)
        self.settings_page.set_play_mode(mode.value)
        self.statusBar().showMessage(tr("play.mode", name=mode.display_name))

    def _on_queue_mode_changed(self, mode) -> None:
        self.player_bar.set_mode(mode.value, mode.display_name)

    def _on_settings_play_mode(self, value: str) -> None:
        try:
            mode = PlayMode(value)
        except ValueError:
            return
        self._set_play_mode(mode)

    def _on_settings_close_action(self, action: str) -> None:
        self._settings["close_action"] = action
        self._store.save_settings(self._settings)

    def _on_settings_play_quality(self, value: str) -> None:
        """设置页音质偏好:只持久化,下一首解析地址时按新偏好取档。"""
        self._settings[SETTING_PLAY_QUALITY] = value
        self._store.save_settings(self._settings)

    def _on_settings_recent_max(self, value: int) -> None:
        """设置页「最近列表数量」:持久化并即刻裁剪存量栈(缩小即时生效,
        扩大不回填——被裁掉的条目已经丢了)。"""
        self._settings[SETTING_RECENT_MAX] = value
        if len(self._recent) > value:
            self._recent = self._recent[:value]
            self._settings[SETTING_RECENT_LISTS] = [
                {"kind": ref.kind, "id": ref.id, "title": ref.title}
                for ref in self._recent
            ]
        self._store.save_settings(self._settings)
        self._rebuild_sidebar()

    def _on_settings_appearance(self, value: str) -> None:
        """设置页外观选择:持久化并即时切换主题(动态取色在新明暗下重算)。"""
        if value not in theme.VALID_THEMES:
            return
        self._settings[SETTING_APPEARANCE] = value
        self._store.save_settings(self._settings)
        self.themes.apply(value)
        if (
            self._dynamic_seed is not None
            and self._settings.get(SETTING_DYNAMIC_COLOR, True)
        ):
            self.themes.apply_dynamic(self._dynamic_seed)

    def _on_settings_dynamic_color(self, enabled: bool) -> None:
        """设置页动态取色开关:持久化;开启即恢复记住的种子,关闭回静态。

        关闭期间点歌仍会记住(不应用)种子,故重开可直接恢复,无需
        等下一次点歌;无种子(还没播过歌)时维持静态。
        """
        self._settings[SETTING_DYNAMIC_COLOR] = enabled
        self._store.save_settings(self._settings)
        if enabled:
            if self._dynamic_seed is not None:
                self.themes.apply_dynamic(self._dynamic_seed)
        else:
            self._revert_static_theme()

    def _on_settings_language(self, value: str) -> None:
        """设置页语言滑块:持久化并切换当前语言(经 i18n 通知重翻译)。"""
        if value not in i18n.VALID_LANGUAGES:
            return
        self._settings[SETTING_LANGUAGE] = value
        self._store.save_settings(self._settings)
        i18n.set_language(value)

    def _on_language_changed(self, language: str) -> None:
        """语言切换的全量文案刷新(与主题切换的 theme_changed 同套路)。

        静态文案逐一重设;动态文案(状态栏消息)本就现算现显,无需处理。
        侧栏整树重建(条目文字都在树里);表格只重设表头(单元格内容是
        歌曲数据,不随语言变)。
        """
        self._fill_song_table()
        self._search_input.setPlaceholderText(tr("search.placeholder"))
        self._search_netease_btn.setText(tr("search.source_netease"))
        self._search_bili_btn.setText(tr("search.source_bili"))
        self._search_more_btn.setText(tr("search.more"))
        self._rebuild_sidebar()
        self._update_table_empty_state()
        self._update_source_indicator(animate=False)  # 文字变宽,指示线重新对位
        self.player_bar.retranslate()
        self.settings_page.retranslate()
        self.login_page.retranslate()
        if self._queue_window is not None:
            self._queue_window.retranslate()
        if self._tray is not None:
            self._tray.retranslate()
        if self._taskbar is not None:
            self._taskbar.retranslate()
        self.statusBar().showMessage(tr("common.ready"))

    def _on_theme_changed(self, name: str) -> None:
        """主题切换的全量资源刷新(ThemeManager 已换全局 QSS)。"""
        self.player_bar.retheme()
        self._empty_state.retheme()
        self.settings_page.retheme_link()
        self.settings_page.language_switch.update()  # 自绘滑块按新色板重画
        self._apply_sidebar_icons()
        if self._tray is not None:
            self._tray.set_icon(tray_icon(name))

    def _wire_settings_page(self) -> None:
        page = self.settings_page
        page.close_action_changed.connect(self._on_settings_close_action)
        page.play_mode_changed.connect(self._on_settings_play_mode)
        page.appearance_changed.connect(self._on_settings_appearance)
        page.quality_changed.connect(self._on_settings_play_quality)
        page.recent_max_changed.connect(self._on_settings_recent_max)
        page.language_changed.connect(self._on_settings_language)
        page.dynamic_color_changed.connect(self._on_settings_dynamic_color)
        page.set_close_action(self._settings.get("close_action", "tray"))
        page.set_play_mode(self._settings.get("play_mode", "sequence"))
        page.set_appearance(self._settings.get(SETTING_APPEARANCE, "dark"))
        page.set_quality(self._settings.get(SETTING_PLAY_QUALITY, "lossless"))
        page.set_recent_max(
            self._settings.get(SETTING_RECENT_MAX, DEFAULT_RECENT_MAX)
        )
        page.set_language(
            self._settings.get(SETTING_LANGUAGE, i18n.DEFAULT_LANGUAGE)
        )
        page.set_dynamic_color(
            self._settings.get(SETTING_DYNAMIC_COLOR, True)
        )

    # -- 队列窗口 --------------------------------------------------------------

    def _open_queue_window(self) -> None:
        if self._queue_window is None:
            self._queue_window = QueueWindow(self._queue, self)
            self._queue_window.jump_requested.connect(self._play_at)
        self._queue_window.show()
        self._queue_window.raise_()
        self._queue_window.activateWindow()

    def _wire_queue(self) -> None:
        mode = self._queue.mode()
        self.player_bar.set_mode(mode.value, mode.display_name)
        self._queue.mode_changed.connect(self._on_queue_mode_changed)
        # 切歌单(replace)后队列索引全变:预取槽一并作废
        self._queue.queue_changed.connect(self._clear_prefetched)

    # -- 信号接线 ------------------------------------------------------------

    def _wire_engine(self) -> None:
        self.engine.progress.connect(self.player_bar.set_progress)
        self.engine.playing_changed.connect(self._on_playing_changed)
        self.engine.track_ended.connect(self._on_track_ended)
        self.engine.load_failed.connect(self._on_load_failed)
        self.engine.error.connect(
            lambda message: self.statusBar().showMessage(
                tr("play.error", message=message)
            )
        )

    def _on_playing_changed(self, playing: bool) -> None:
        """播放态变化:播放条按钮 / 任务栏缩略图按钮 / 歌曲表均衡器条同步。"""
        self.player_bar.set_playing(playing)
        if self._taskbar is not None:
            self._taskbar.set_playing(playing)
        self._eq_set_active(playing)

    def _wire_player_bar(self) -> None:
        bar = self.player_bar
        bar.play_pause_clicked.connect(self._toggle_pause)
        bar.prev_clicked.connect(self._play_prev)
        bar.next_clicked.connect(self._play_next)
        bar.seek_requested.connect(self._on_seek_requested)
        bar.volume_changed.connect(self._on_volume_changed)
        bar.mode_clicked.connect(self._cycle_mode)
        bar.queue_clicked.connect(self._open_queue_window)

    def _toggle_pause(self) -> None:
        if self.engine is not None:
            self.engine.toggle_pause()

    def _set_play_controls_active(self, active: bool) -> None:
        """播放控制可用态:播放条与任务栏缩略图按钮同步启用/禁用。"""
        self.player_bar.set_active(active)
        if self._taskbar is not None:
            self._taskbar.set_enabled(active)

    def _on_seek_requested(self, position_s: float) -> None:
        if self.engine is not None:
            self.engine.seek(position_s)

    def _on_volume_changed(self, volume: int) -> None:
        if self.engine is not None:
            self.engine.set_volume(volume)

    # -- 托盘与媒体键 ------------------------------------------------------------

    def _setup_tray(self, icon) -> None:
        """托盘不可用(如离屏环境)时静默跳过,不影响主流程。"""
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            self._tray = TrayController(icon, self)
        except Exception:  # noqa: BLE001 - 托盘属增强能力,绝不因此崩启动
            self._tray = None
            return
        tray = self._tray
        tray.toggle_play_requested.connect(self._toggle_pause)
        tray.prev_requested.connect(self._play_prev)
        tray.next_requested.connect(self._play_next)
        tray.show_main_requested.connect(self._show_main_window)
        tray.exit_requested.connect(self._exit_app)

    def _setup_media_keys(self) -> None:
        if sys.platform != "win32":
            return
        try:
            # handler 必须持引用,防止 native filter 被 GC
            self._media_keys = MediaKeyHandler(self)
        except Exception:  # noqa: BLE001 - 媒体键属增强能力,失败不崩启动
            self._media_keys = None
            return
        keys = self._media_keys
        keys.play_pause_requested.connect(self._toggle_pause)
        keys.stop_requested.connect(self._on_media_stop)
        keys.next_requested.connect(self._play_next)
        keys.prev_requested.connect(self._play_prev)

    def _setup_taskbar(self) -> None:
        """任务栏缩略图工具栏(增强能力,失败静默降级为空操作)。

        附加(ThumbBarAddButtons)在 showEvent 里做:窗口得先有
        任务栏按钮;这里只建对象与接信号。
        """
        try:
            self._taskbar = TaskbarThumbBar(self)
        except Exception:  # noqa: BLE001 - 任务栏属增强能力,绝不崩启动
            self._taskbar = None
            return
        bar = self._taskbar
        bar.prev_requested.connect(self._play_prev)
        bar.play_pause_requested.connect(self._toggle_pause)
        bar.next_requested.connect(self._play_next)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        # 缩略图工具栏需窗口已有任务栏按钮:每次显示时尝试附加,
        # 成功一次即固定(离屏/COM 不可用时 attach 恒失败,静默重试无害)
        if self._taskbar is not None:
            self._taskbar.attach(int(self.winId()))

    def _on_media_stop(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        self.player_bar.set_playing(False)
        self._reset_window_title()  # 停止播放:任务栏恢复基础标题
        self.statusBar().showMessage(tr("common.stopped"))

    def _show_main_window(self) -> None:
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _exit_app(self) -> None:
        """托盘菜单「退出」:绕过「最小化到托盘」逻辑,真正退出。"""
        self._force_exit = True
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # -- 退出清理 ------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if (
            not self._force_exit
            and self._settings.get("close_action") == "tray"
            and self._tray is not None
        ):
            # 收进托盘:隐藏窗口继续播放;首次给气泡提示
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray.show_message(
                    "NeriPlayer Win",
                    tr("tray.minimized_body"),
                )
                self._tray_hint_shown = True
            return
        self.login_page.stop()
        i18n.remove_listener(self._on_language_changed)
        if self._queue_window is not None:
            self._queue_window.close()
        if self._media_keys is not None:
            try:
                self._media_keys.stop()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass
        if self._taskbar is not None:
            try:
                self._taskbar.shutdown()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass
        if self.engine is not None:
            # 真退出走 shutdown 安全序列(quit→等事件线程退出→terminate):
            # 只 stop 会泄漏一个 mpv 事件线程/窗口,同进程多窗口(测试)
            # 构造耗时超线性劣化(实测 20 窗口 132ms→694ms)
            try:
                self.engine.shutdown()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass
        self._client.close()
        self._bili_client.close()
        super().closeEvent(event)
