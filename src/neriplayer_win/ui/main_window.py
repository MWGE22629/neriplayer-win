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
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedLayout,
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
from ..api.kugou import (
    KugouAccount,
    KugouApiError,
    KugouAuthRequiredError,
    KugouClient,
    KugouCredentials,
    KugouPlaylist,
    KugouSong,
    kugou_quality_i18n_key,
    kugou_quality_label,
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
    DEFAULT_VOLUME,
    LOCAL_PLAYLIST_NAME_MAX,
    SETTING_APPEARANCE,
    SETTING_BILI_FOLDER_ORDER,
    SETTING_DYNAMIC_COLOR,
    SETTING_LANGUAGE,
    SETTING_KUGOU_PLAYLIST_ORDER,
    SETTING_LOCAL_PLAYLIST_ORDER,
    SETTING_NETEASE_PLAYLIST_ORDER,
    SETTING_NETEASE_SUBSCRIBED_ORDER,
    SETTING_PLAY_QUALITY,
    SETTING_RECENT_LISTS,
    SETTING_RECENT_MAX,
    SETTING_SECTION_ORDER,
    SETTING_SIDEBAR_EXPANDED,
    SETTING_VOLUME,
    LocalPlaylist,
    LocalPlaylistItem,
    LocalStore,
    apply_section_order,
    apply_stored_order,
    delete_local_playlist,
    local_playlist_cover_url,
    move_local_playlist_in_order,
    move_local_playlist_item,
    next_local_playlist_id,
    remove_local_playlist_item,
    rename_local_playlist,
)
from .. import bootstamp
from ..data.play_stats import PlayStat, PlayStatsStore, make_key
from ..data.play_stats_tracker import PlayStatsTracker
from ..i18n import tr
from ..log import get_logger
from ..player.engine import PlayerEngine, PlayerEngineError
from ..player.queue import BackupUrlRotator, PlayMode, PlayQueue, QueueSong
from . import theme
from . import cover_seed
from .audio_route import AudioRouteGuard
from .browser_login import (
    BrowserLoginDialog,
    bili_web_login,
    kugou_web_login,
)
from .kugou_login import KugouQrLoginDialog
from .covers import CoverLoader
from .icons import app_icon, tinted_icon, tinted_icon_with_color, tray_icon
from .login_page import LoginPage
from .material_color import seed_from_hex
from .media_keys import MediaKeyHandler
from .player_bar import PlayerBar, format_seconds, rounded_pixmap
from .queue_window import QueueWindow
from .settings_page import SettingsPage
from .stats_page import StatsPage
from .taskbar import TaskbarThumbBar
from .theme import ThemeManager
from .textsafe import sanitize_ui_text
from .tray import TrayController
from .workers import run_async

_PAGE_LOGIN = 0
_PAGE_TABLE = 1
_PAGE_SETTINGS = 2
_PAGE_STATS = 3

# 基础窗口标题(品牌名,不随语言变):任务栏歌名显示的宽度基准
logger = get_logger("main_window")

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

# 音量记忆(M10):滑块拖动时 valueChanged 逐像素触发,落盘走防抖,
# 避免拖动一次写几十遍 settings.json;退出时 closeEvent 再补存一次,
# 保证最后一次改动不丢。
_VOLUME_SAVE_DELAY_MS = 400

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
    "kugou-header": "kugou",
    "local-header": "local",
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


def _netease_quality_parts(level: str | None) -> tuple[str, int]:
    """网易云 → (档位名 i18n 键, 码率)。

    网易云播放接口不返回码率(解析层只取 type/level/size),所以码率恒 0,
    标签只显示档位名;而档位名本身已隐含码率(320K / 128K)。
    """
    if not level:
        return "", 0
    key = _NETEASE_LEVEL_KEYS.get(level)
    # 未识别的新档位原样展示:键为空时直接用 level 文本(与状态栏同策略)
    return (key if key is not None else level), 0


def _bili_quality_parts(stream: BiliAudioStreamInfo) -> tuple[str, int]:
    """B站 → (档位名 i18n 键, 码率)。

    有杜比/Hi-Res 标签时用标签名,否则只用码率(键为空)。
    """
    if stream.bitrate_kbps <= 0:
        return "", 0
    key = _BILI_QUALITY_TAG_KEYS.get(stream.quality_tag or "")
    return (key if key is not None else ""), stream.bitrate_kbps


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


def _kugou_song_to_queue(song: KugouSong) -> QueueSong:
    """酷狗歌曲 → 统一队列条目。

    酷狗的播放标识是 hash / encode_album_audio_id 这类字符串,塞不进
    QueueSong.id(int)。约定:id 恒为 0,真实标识全部放 bvid 字段
    (该字段本就只承载字符串标识,B站用它存 bvid),多值以
    `hash|encode_album_audio_id|album_id` 编码。反解见
    _kugou_song_from_queue,两处必须配对维护。
    """
    encoded = "|".join((song.hash, song.encode_album_audio_id, song.album_id))
    return QueueSong(
        source="kugou", id=0, bvid=encoded,
        title=sanitize_ui_text(song.title),
        artist=sanitize_ui_text(song.artist),
        duration_ms=song.duration_ms,
        cover_url=song.cover_url,
    )


def _kugou_song_from_queue(song: QueueSong) -> KugouSong:
    """统一队列条目 → 酷狗歌曲(解析播放地址用)。

    与 _kugou_song_to_queue 配对。hashes 留空:解析层会按需调
    get_song_info 补各档 hash,队列条目不必携带。
    """
    parts = (song.bvid or "").split("|")
    while len(parts) < 3:
        parts.append("")
    return KugouSong(
        hash=parts[0],
        title=song.title,
        artist=song.artist,
        duration_ms=song.duration_ms,
        encode_album_audio_id=parts[1],
        album_id=parts[2],
        cover_url=song.cover_url,
    )


def _exec_menu(menu: QMenu, global_pos) -> object | None:
    """弹出菜单并返回选中动作的薄封装。

    QMenu.exec 是 PySide6 的静态内建方法,无法在类上猴补;包一层模块级
    函数,测试才能把「弹菜单」换成「直接返回目标动作」。
    """
    return menu.exec(global_pos)


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
    # 音质标签原料:档位名 i18n 键 + 码率(kbps,0 表示未知)。
    # 存键而非译文,语言切换后播放条能自行重译。
    quality_key: str = ""
    quality_kbps: int = 0


@dataclass
class _PrefetchedPlay:
    """下一曲预取结果(单槽):命中校验信息 + 播放上下文。"""

    queue_index: int  # 预取时的队列索引,消费时与目标 index 比对
    song: QueueSong  # 标识比对用(source+id);手动跳歌未命中即作废
    rotator: BackupUrlRotator
    headers: dict[str, str] | None
    status: str
    created_at: float  # time.monotonic() 时间戳,TTL 校验用
    # 音质标签原料,随预取一起缓存(与 status 同源)
    quality_key: str = ""
    quality_kbps: int = 0


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


class _CoverPickerDialog(QDialog):
    """歌单封面选择:列出歌单内各曲封面,选一张作为手动封面(M7 步骤 6)。

    只从歌单内已有歌曲里选,不引入本地文件路径:零新增失效路径,
    数据全在已有快照里,与「自动封面取首曲」同一数据源。
    封面字节复用侧栏 loader 的磁盘/内存缓存,缺的现请求,到一张亮一张。
    """

    # 对话框最多为多少首歌现请求封面(防止超大歌单一次开几十个线程)
    _MAX_REQUESTS = 60

    def __init__(
        self,
        playlist: LocalPlaylist,
        loader: CoverLoader,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("local.cover_pick"))
        self.setMinimumSize(360, 420)
        self._loader = loader
        # 对话框自己的 64px 图标缓存:侧栏缓存是 18px,共用会把小图放大发糊
        self._icons: dict[str, QIcon] = {}
        self._urls_by_item: dict[int, str] = {}

        self.list_widget = QListWidget()
        self.list_widget.setIconSize(QSize(64, 64))
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setSpacing(6)
        self.list_widget.setWordWrap(True)

        for row, item in enumerate(playlist.items):
            if not item.cover_url:
                continue  # 无封面的曲目没有可选内容
            entry = QListWidgetItem(sanitize_ui_text(item.title))
            entry.setData(Qt.ItemDataRole.UserRole, item.cover_url)
            entry.setSizeHint(QSize(96, 108))
            icon = self._icons.get(item.cover_url)
            if icon is not None:
                entry.setIcon(icon)
            self.list_widget.addItem(entry)
            self._urls_by_item[row] = item.cover_url

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("local.cover_confirm"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            tr("weblogin.cancel")
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        hint = QLabel(tr("local.cover_hint"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(self.list_widget, 1)
        layout.addWidget(buttons)

        # 未选中任何封面时「设为封面」不可点
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.list_widget.currentItemChanged.connect(self._on_current_changed)
        self._loader.cover_ready.connect(self._on_cover_ready)
        self._request_missing()

    def _request_missing(self) -> None:
        """为尚未缓存封面的曲目发起请求(单曲一次,同 URL 自动去重)。"""
        pending = [
            url
            for url in dict.fromkeys(self._urls_by_item.values())
            if url not in self._icons
        ]
        for url in pending[: self._MAX_REQUESTS]:
            self._loader.request(url)

    def _on_current_changed(self, current: QListWidgetItem | None, _previous) -> None:
        # 只有选中项才允许确定:空选中时 OK 无意义
        ok = self.findChild(QDialogButtonBox)
        if ok is not None:
            ok.button(QDialogButtonBox.StandardButton.Ok).setEnabled(current is not None)

    def _on_cover_ready(self, url: str, data: bytes) -> None:
        if url not in self._urls_by_item.values():
            return
        icon = self._icons.get(url)
        if icon is None:
            pixmap = QPixmap()
            if not pixmap.loadFromData(data):
                return
            pixmap = rounded_pixmap(pixmap, 64, 8)
            icon = QIcon(pixmap)
            self._icons[url] = icon
        for row in range(self.list_widget.count()):
            entry = self.list_widget.item(row)
            if entry.data(Qt.ItemDataRole.UserRole) == url:
                entry.setIcon(icon)

    def chosen_url(self) -> str:
        """选中项的封面 URL;未选中返回空串。"""
        entry = self.list_widget.currentItem()
        if entry is None:
            return ""
        value = entry.data(Qt.ItemDataRole.UserRole)
        return value if isinstance(value, str) else ""

    def done(self, result: int) -> None:  # noqa: N802 - Qt 命名
        # 关闭即断线:loader 是长生命周期对象,不能留着指向已销毁对话框的槽
        try:
            self._loader.cover_ready.disconnect(self._on_cover_ready)
        except (RuntimeError, TypeError):
            pass
        super().done(result)


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


class _SongTable(QTableWidget):
    """歌曲表:本地歌单上下文下支持整行拖拽调序。

    真正的数据源是 LocalPlaylist.items(落盘顺序即列表顺序),表格只是
    它的投影。因此搬行不交给 QTableWidget 的默认实现(默认会自己搬行,
    搬完与内存顺序打架,还会在下次 _fill_song_table 时回退),这里只算出
    (from_row, to_row) 发信号,由 MainWindow 改 items 后重填。但 accept 后
    仍要调 super().dropEvent:与 _SidebarTree._accept_move 同理,基类对已
    接受的事件不会再搬行,只置 dropEventMoved 拦住 startDrag 在 MoveAction
    结束后对被拖行(仍选中)的 removeRows,否则拖完一行表格就少一行。
    拒绝路径不调基类,避免默认搬行。

    其余列表(网易云/B站/酷狗/搜索)是远端只读数据,不开拖放;
    开关由 MainWindow._set_table_songs 按上下文设置。
    """

    # (from_row, to_row):to_row 为移动后应处的新下标
    reorder_requested = Signal(int, int)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        from_row = self.currentRow()
        target = self.indexAt(event.position().toPoint())
        if from_row < 0 or not target.isValid():
            event.ignore()
            return
        if len(self.selectionModel().selectedRows()) > 1:
            # 多选拖拽语义不明确:拒绝,避免只搬了当前行造成困惑
            event.ignore()
            return
        to_row = target.row()
        # 落点在该行下半即插到其后
        rect = self.visualRect(target)
        if rect.isValid() and event.position().toPoint().y() >= rect.center().y():
            to_row += 1
        if to_row > from_row:
            to_row -= 1  # 摘除自身后,后方行索引整体前移一位
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        super().dropEvent(event)
        if to_row != from_row:
            self.reorder_requested.emit(from_row, to_row)


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
    """搜索来源栏:网易云|B站|酷狗 平铺,顶部一条滑动指示线代表选中。

    借鉴移动端 PrimaryScrollableTabRow(M3 主标签行):底色透明,
    选中不铺满颜色,只有 primary 色指示线贴着选中页签文字宽居中,
    切换来源时线滑动过去(250ms OutCubic)。
    """

    def __init__(
        self,
        netease_button: QPushButton,
        bili_button: QPushButton,
        kugou_button: QPushButton,
        on_layout_changed,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(netease_button, 1)
        layout.addWidget(bili_button, 1)
        layout.addWidget(kugou_button, 1)
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
    """侧栏树:顶层分区头(网易云歌单/网易云收藏/B站/酷狗/本地歌单/最近/
    设置)+ 分区内子节点。

    两层拖拽,语义分开:

    - 子节点(歌单/收藏夹):只允许「同一分区内」移动。搬移逻辑不交给 Qt
      默认实现(默认会把子节点挂成目标节点的子节点,产生意外嵌套),而是
      手动搬移;合法移动后发 order_changed(分区键),由 MainWindow 读子节点
      顺序落库。跨分区、空白处、登录占位、固定项(稍后再看)一律 ignore。
      accept 后的事件要交回基类收尾(_accept_move),否则 Qt 会在拖放结束
      后把被拖行从模型里删掉,详见 _accept_move 注释。
    - 分区头(大类):允许互相换序,拖到「搜索」或「设置」上则拒绝(这两个
      固定置顶/置底)。换序后发 section_order_changed(分区键序列)。

    M8 另加分区头右侧的竖排「⋮」把手:分区头是 QTreeWidgetItem 而非控件,
    挂 setItemWidget 会遮住 item 自绘并要在每次重建时管理 widget 生命周期,
    故与「+」同法在 paintEvent 里自绘、mouseMoveEvent 里做悬停反馈。
    把手只作视觉提示与光标反馈,实际拖拽由 QTreeWidget 内建 InternalMove
    承担(按住分区头任意处拖动都有效,不必精确按在三个点上)。
    """

    order_changed = Signal(str)  # 分区键 "netease" | "netease-subscribed" | "bili" | "kugou" | "local"
    # 分区头换序完成:参数是新的分区键序列(只含可排序分区,不含搜索/设置)
    section_order_changed = Signal(list)
    # 分区头「+」被点击:请求新建本地歌单(实际弹窗与落盘在 MainWindow)
    create_local_playlist_requested = Signal()

    # 可参与排序的子节点 kind(分区键 → 子节点 kind)
    _SECTION_CHILD_KIND = {
        "netease": "netease-playlist",
        "netease-subscribed": "netease-subscribed-playlist",
        "bili": "bili-folder",
        "kugou": "kugou-playlist",
        "local": "local-playlist",
    }

    # 「+」命中区宽度(逻辑像素)与距右边缘的留白
    _PLUS_HIT_WIDTH = 22
    _PLUS_RIGHT_MARGIN = 4
    # 「+」笔画长度与线宽(逻辑像素)
    _PLUS_STROKE = 9
    _PLUS_PEN_WIDTH = 2
    # 「⋮」把手:命中区宽度、点半径、点间距(逻辑像素)
    _GRIP_HIT_WIDTH = 14
    _GRIP_DOT_RADIUS = 1.4
    _GRIP_DOT_GAP = 4.0
    # 把手距条目左缘的留白:落在 QTreeWidget::item 的左侧内边距里
    # (theme.py 给左侧留了 26px),既不压品牌图标,也不破坏各行左对齐
    _GRIP_LEFT = 12

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
        # 「+」与「⋮」的 hover 反馈需要无按键的鼠标移动事件
        self.setMouseTracking(True)
        self._plus_hover = False
        # 当前悬停的「⋮」所属分区键(None=没悬停在任何把手上)
        self._grip_hover: str | None = None

    @staticmethod
    def item_kind(item: QTreeWidgetItem | None) -> str:
        """条目 UserRole 元组 ("kind", payload) 的 kind;无数据返回空串。"""
        if item is None:
            return ""
        role = item.data(0, Qt.ItemDataRole.UserRole)
        return role[0] if isinstance(role, tuple) else ""

    # -- 分区头「+」(自绘命中区) ---------------------------------------------

    def local_header_plus_rect(self) -> QRect | None:
        """「本地歌单」分区头右侧「+」的命中区(视口坐标);无该分区返回 None。

        绘制与命中判定共用这一个函数,避免两处各算一遍导致漂移;
        宽度按视口现算,侧栏拖动变宽/变窄时自动跟随。
        """
        header = self.find_header("local")
        if header is None:
            return None
        row = self.visualItemRect(header)
        if not row.isValid() or row.width() <= 0:
            return None
        # 分区头滚动到视口外/部分可见时,只保留可见的那段(竖向上裁剪)
        visible = row.intersected(self.viewport().rect())
        if visible.height() <= 0:
            return None
        width = min(self._PLUS_HIT_WIDTH, max(0, row.width() - 24))
        if width <= 0:
            return None
        return QRect(
            self.viewport().width() - self._PLUS_RIGHT_MARGIN - width,
            visible.top(),
            width,
            visible.height(),
        )

    def _draw_plus(self, painter: QPainter, rect: QRect) -> None:
        """画「+」:两条线段,主题色(悬停时 primary),2x 内不依赖字体。"""
        color = QColor(
            theme.current_palette()["primary" if self._plus_hover else "onSurfaceVariant"]
        )
        color.setAlpha(255 if self._plus_hover else 170)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(color)
        pen.setWidthF(self._PLUS_PEN_WIDTH)
        painter.setPen(pen)
        center = rect.center()
        half = self._PLUS_STROKE / 2
        painter.drawLine(
            QPointF(center.x() - half, center.y()), QPointF(center.x() + half, center.y())
        )
        painter.drawLine(
            QPointF(center.x(), center.y() - half), QPointF(center.x(), center.y() + half)
        )
        painter.restore()

    # -- 分区头「⋮」把手(自绘 + 拖动换序) -----------------------------------

    def _sortable_headers(self) -> list[tuple[str, QTreeWidgetItem]]:
        """当前可拖排序的分区头 [(分区键, item)];搜索/设置不在其中。"""
        headers: list[tuple[str, QTreeWidgetItem]] = []
        for row in range(self.topLevelItemCount()):
            item = self.topLevelItem(row)
            section = _HEADER_SECTION.get(self.item_kind(item))
            if section is not None:
                headers.append((section, item))
        return headers

    def header_grip_rect(self, header: QTreeWidgetItem) -> QRect | None:
        """某个分区头左侧「⋮」的命中区(视口坐标);不可见时返回 None。

        绘制与命中共用本函数,窗口缩放自动跟随。把手放在条目左缘的
        内边距里(主题左 padding 已让出槽位),与右侧「+」不再抢同一段
        空间:此前两者都按右边缘定位,本地歌单分区头上完全重叠,点把手
        会弹出新建歌单对话框。
        「搜索」「设置」固定位置,不画把手,这里直接返回 None(绘制与命中
        都走本函数,拒绝一次即两处一致)。
        """
        if _HEADER_SECTION.get(self.item_kind(header)) is None:
            return None
        row = self.visualItemRect(header)
        if not row.isValid() or row.width() <= 0:
            return None
        visible = row.intersected(self.viewport().rect())
        if visible.height() <= 0:
            return None
        width = min(self._GRIP_HIT_WIDTH, max(0, row.width() - 24))
        if width <= 0:
            return None
        return QRect(
            row.left() + self._GRIP_LEFT,
            visible.top(),
            width,
            visible.height(),
        )

    def _draw_grip(self, painter: QPainter, rect: QRect, hovered: bool) -> None:
        """画竖排三点:纯线段/圆点,不依赖字体(同「+」的理由)。"""
        color = QColor(
            theme.current_palette()["primary" if hovered else "onSurfaceVariant"]
        )
        color.setAlpha(255 if hovered else 140)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        center = rect.center()
        radius = self._GRIP_DOT_RADIUS
        gap = self._GRIP_DOT_GAP
        for offset in (-gap, 0.0, gap):
            painter.drawEllipse(
                QPointF(center.x(), center.y() + offset), radius, radius
            )
        painter.restore()

    def _grip_hit_section(self, point: QPoint) -> str | None:
        """点在哪个分区头的把手上,返回该分区键;未命中返回 None。"""
        for section, header in self._sortable_headers():
            rect = self.header_grip_rect(header)
            if rect is not None and rect.contains(point):
                return section
        return None

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        try:
            # 「+」只在本地歌单分区头上
            plus_rect = self.local_header_plus_rect()
            if plus_rect is not None:
                self._draw_plus(painter, plus_rect)
            # 「⋮」在每个可排序分区头上
            for section, header in self._sortable_headers():
                rect = self.header_grip_rect(header)
                if rect is not None:
                    self._draw_grip(
                        painter, rect, hovered=(self._grip_hover == section)
                    )
        finally:
            painter.end()

    def _plus_hit(self, point: QPoint) -> bool:
        rect = self.local_header_plus_rect()
        return rect is not None and rect.contains(point)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 命中「+」先于折叠语义处理:吞掉事件,不让分区头收到点击
        if event.button() == Qt.MouseButton.LeftButton and self._plus_hit(
            event.position().toPoint()
        ):
            event.accept()
            self.create_local_playlist_requested.emit()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        point = event.position().toPoint()
        plus_hover = self._plus_hit(point)
        grip_hover = self._grip_hit_section(point)
        if plus_hover != self._plus_hover or grip_hover != self._grip_hover:
            self._plus_hover = plus_hover
            self._grip_hover = grip_hover
            self.viewport().update()
        # 悬停在把手上时给出「可拖动」光标,提示这里能抓
        if grip_hover is not None:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.viewport().unsetCursor()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._plus_hover or self._grip_hover is not None:
            self._plus_hover = False
            self._grip_hover = None
            self.viewport().unsetCursor()
            self.viewport().update()
        super().leaveEvent(event)

    def _accept_move(self, event: QDropEvent) -> None:
        """接受一次内部移动并把事件交回基类收尾。

        必须调 super().dropEvent:Qt 的 startDrag 在 drag->exec() 以
        MoveAction 结束且 dropEventMoved 为 false 时,会对仍处于选中态
        的被拖行执行 removeRows(分区头被删等于整个分区连同子节点从侧栏
        消失)。而 dropEventMoved 只在 QTreeWidget 的 C++ 基类 dropEvent
        里、看到事件已被 accept 时置位(源码注释明说这是为「已自行搬好
        数据的子类」留的口子);基类对已接受的事件不会再搬数据,只置标志
        并做 setState(NoState) 等收尾。本测试直构 QDropEvent 走不到
        startDrag,offscreen 下 exec 也只返回 IgnoreAction,所以此坑
        自动化测不出,只能靠这里的 super() 挡住。
        拒绝路径不得调基类:未接受的事件会被基类按默认语义搬移(子节点
        被挂成目标节点的子节点,产生意外嵌套)。
        """
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        super().dropEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt 命名
        selected = self.selectedItems()
        dragged = selected[0] if len(selected) == 1 else None
        if dragged is None:
            # 分区头在旧版本不可选中,selectedItems 取不到;回退 currentItem
            # 保证拖拽起点始终有值(子节点单选时两者一致,不会取错)
            dragged = self.currentItem()
        if dragged is None:
            event.ignore()
            return
        # 分区头拖动:走顶层换序分支(与子节点拖动语义分开)
        if dragged.parent() is None:
            self._drop_header(event, dragged)
            return
        if self.item_kind(dragged) not in self._SECTION_CHILD_KIND.values():
            # 登录占位、加载占位、固定项(稍后再看/每日推荐)不可移动
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
            self._accept_move(event)
            return
        below = self._drop_inserts_below(event, target)
        self._move_child(dragged, target, below)
        self._accept_move(event)
        section = _HEADER_SECTION.get(self.item_kind(dragged.parent()))
        if section is not None:
            self.order_changed.emit(section)

    def _drop_inserts_below(
        self, event: QDropEvent, target: QTreeWidgetItem
    ) -> bool:
        """落点插在 target 之前(False)还是之后(True)。

        按光标落在该行的上半还是下半判定,不用 dropIndicatorPosition():
        鼠标停在行中间三分之一时 Qt 报 OnItem(整行视为落点),旧逻辑把
        OnItem 一律当成「之后」,于是「向上拖一格」是空操作。改为与行
        中心比 y 后上下各半语义明确,分区头与子节点两条路径共用。
        """
        row = self.visualItemRect(target)
        if not row.isValid():
            return True
        return event.position().toPoint().y() >= row.center().y()

    def _drop_header(self, event: QDropEvent, dragged: QTreeWidgetItem) -> None:
        """分区头换序:只允许在可排序分区之间移动。

        「搜索」固定置顶、「设置」固定置底,拖到它们上或拖它们本身一律拒绝;
        判定用被拖项与目标项的 item_kind,不看 payload。
        """
        dragged_section = _HEADER_SECTION.get(self.item_kind(dragged))
        if dragged_section is None:
            # 「搜索」/「设置」不参与排序
            event.ignore()
            return
        target = self.itemAt(event.position().toPoint())
        if target is None:
            event.ignore()
            return
        # 落点可能是分区头的子节点(拖到展开的分区内部):上溯到其顶层项
        while target.parent() is not None:
            target = target.parent()
        if target is dragged:
            self._accept_move(event)
            return
        target_section = _HEADER_SECTION.get(self.item_kind(target))
        if target_section is None:
            # 拖到「搜索」或「设置」上:拒绝(它们是固定位置)
            event.ignore()
            return
        below = self._drop_inserts_below(event, target)
        old_index = self.indexOfTopLevelItem(dragged)
        new_index = self.indexOfTopLevelItem(target) + (1 if below else 0)
        if old_index < new_index:
            new_index -= 1
        if old_index == new_index:
            self._accept_move(event)
            return
        self.takeTopLevelItem(old_index)
        self.insertTopLevelItem(new_index, dragged)
        self.setCurrentItem(dragged)
        self._accept_move(event)
        self.section_order_changed.emit(
            [section for section, _item in self._sortable_headers()]
        )

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
        # M9 播放统计:独立数据文件 + 计时状态机(口径见 data/play_stats*.py)
        self._play_stats_store = PlayStatsStore()
        self._play_stats: dict[str, PlayStat] = {
            stat.key: stat for stat in self._play_stats_store.load()
        }
        self._stats_tracker = PlayStatsTracker()
        self._stats_timer: QTimer | None = None
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

        # 本地歌单(M7):内存副本按侧栏显示顺序排列,落盘走 local_playlists.json
        self._local_playlists: list[LocalPlaylist] = []

        # 酷狗(M6):凭据两条链(扫码 token 管歌单、网页 KuGoo 串管播放),
        # 见 docs/research/M6-kugou-api.md §2.6
        self._kugou_client = KugouClient()
        self._kugou_account: KugouAccount | None = None
        self._kugou_playlists: list[KugouPlaylist] = []

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
        # 侧栏歌单封面(M7):第三实例,理由与 seed loader 同构——表格预取的
        # preload 批次会互相作废,歌单封面不能跟表格抢同一个实例;磁盘缓存
        # 按 URL 共享,同一首歌的封面不会重复下载。
        self._sidebar_cover_loader = CoverLoader(self)
        self._sidebar_cover_loader.cover_ready.connect(self._on_sidebar_cover_ready)
        # 已解码的歌单封面(URL → QIcon);侧栏重建时直接复用,不重新解码
        self._sidebar_cover_icons: dict[str, QIcon] = {}
        self._sidebar_cover_urls: list[str] = []

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

        self.song_table = _SongTable(0, 5)
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
        # 本地歌单内拖拽调序(其余列表不开拖放,见 _set_table_songs)
        self.song_table.reorder_requested.connect(
            self._on_table_reorder_requested
        )
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
        self._search_kugou_btn = QPushButton(tr("search.source_kugou"))
        for button in (
            self._search_netease_btn, self._search_bili_btn, self._search_kugou_btn
        ):
            button.setObjectName("searchSourceBtn")
            button.setCheckable(True)
            button.setFixedHeight(_SEARCH_BAR_HEIGHT)
        self._search_netease_btn.setChecked(True)
        self._search_group = QButtonGroup(self)
        self._search_group.addButton(self._search_netease_btn)
        self._search_group.addButton(self._search_bili_btn)
        self._search_group.addButton(self._search_kugou_btn)
        self._search_netease_btn.clicked.connect(
            lambda: self._on_search_source_changed("netease")
        )
        self._search_bili_btn.clicked.connect(
            lambda: self._on_search_source_changed("bili")
        )
        self._search_kugou_btn.clicked.connect(
            lambda: self._on_search_source_changed("kugou")
        )
        # 来源栏:M3 主标签行风格,顶部滑动指示线代表选中
        self._source_bar = _SourceBar(
            self._search_netease_btn,
            self._search_bili_btn,
            self._search_kugou_btn,
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

        # 统计页懒创建:QTableWidget 构造成本较高(实测每次约 100ms 级),
        # 而多数会话不会打开统计页,不必在每次启动时都付这个代价。
        self.stats_page: StatsPage | None = None
        self._stats_page_added = False

        self.central_stack = _SlideStack()
        self.central_stack.addWidget(self.login_page)  # 0
        self.central_stack.addWidget(table_page)  # 1
        self.central_stack.addWidget(self.settings_page)  # 2

        # -- 侧栏 -------------------------------------------------------------
        # QTreeWidget(M5):平台分区可折叠 + 分区内歌单/收藏夹拖拽排序
        self.sidebar = _SidebarTree()
        self.sidebar.setObjectName("mainSidebar")
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
        # M8:分区头「⋮」拖动换序(搜索/设置固定,不参与)
        self.sidebar.section_order_changed.connect(
            self._on_section_order_changed
        )
        # 分区头「+」(本地歌单)与歌单右键菜单(M7)
        self.sidebar.create_local_playlist_requested.connect(
            self._on_create_local_playlist_requested
        )
        self.sidebar.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.sidebar.customContextMenuRequested.connect(
            self._on_sidebar_context_menu
        )
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
        # 音量防抖落盘定时器(M10):须先于 _wire_player_bar 建好,
        # 保证 valueChanged 早到也不会碰到未初始化的属性
        self._volume_save_timer = QTimer(self)
        self._volume_save_timer.setSingleShot(True)
        self._volume_save_timer.setInterval(_VOLUME_SAVE_DELAY_MS)
        self._volume_save_timer.timeout.connect(self._flush_volume_setting)
        self._set_play_controls_active(False)
        if self.engine is not None:
            self._wire_engine()
        self._wire_player_bar()
        self._wire_queue()
        self._restore_volume()

        # -- 主题(M4):应用持久化主题;此后切换经 theme_changed 全量刷新 -----
        # 注意:_on_theme_changed 会触碰 _tray 等属性,须先完成初始化
        self._queue_window: QueueWindow | None = None
        self._tray: TrayController | None = None
        self._tray_hint_shown = False
        self._force_exit = False
        self._media_keys: MediaKeyHandler | None = None
        self._route_guard: AudioRouteGuard | None = None
        self.themes = ThemeManager()
        self.themes.theme_changed.connect(self._on_theme_changed)
        self.themes.apply(self._settings.get(SETTING_APPEARANCE, "dark"))

        body = QWidget()
        body.setObjectName("mainBody")
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
        self._central_host = QWidget()
        host_layout = QVBoxLayout(self._central_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)
        host_layout.addWidget(self._splitter)
        self.setCentralWidget(self._central_host)
        self._clamp_sidebar_width()

        self.statusBar().showMessage(tr("common.ready"))

        # -- 托盘 / 媒体键 / 队列窗口 ------------------------------------------
        self.setWindowIcon(app_icon())
        self._setup_tray(tray_icon(self.themes.name))
        self._setup_media_keys()
        self._setup_route_guard()
        self._setup_taskbar()

        self._rebuild_sidebar()
        self._update_table_empty_state()
        self.central_stack.setCurrentIndex(_PAGE_LOGIN)
        self.login_page.start()
        self._boot_from_store()
        self._boot_bili_from_store()
        self._boot_kugou_from_store()
        self._boot_local_playlists()

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

    # -- 酷狗(M6)登录流程 -----------------------------------------------------

    def _boot_kugou_from_store(self) -> None:
        """启动恢复酷狗登录态。

        凭据有两条独立链(见 docs/research/M6-kugou-api.md §2.6):
        - token 链:管歌单,启动时静默续期,只要不超 30 天用户无需重扫
        - KuGoo 链:管播放,网页登录产物,无法续期
        因此这里只要读到任一链就先进入登录态,再后台校验 token;
        token 校验失败只清 token 链,不清 KuGoo 链(播放仍可用)。
        """
        bundle = self._store.load_kugou()
        if not bundle:
            return
        credentials = KugouCredentials(
            userid=bundle.get("userid", ""),
            token=bundle.get("token", ""),
            t1=bundle.get("t1", ""),
            vip_token=bundle.get("vip_token", ""),
            vip_type=bundle.get("vip_type", 0),
            t_expire_time=bundle.get("t_expire_time", 0),
            nickname=bundle.get("nickname", ""),
            mid=bundle.get("mid", ""),
            dfid=bundle.get("dfid", ""),
            kugoo_cookie=bundle.get("kugoo_cookie", ""),
            kugoo_cookie_at=bundle.get("kugoo_cookie_at", 0),
        )
        self._kugou_client.set_credentials(credentials)
        # 续期成功后立即落盘,保证下次启动用的是新 token
        self._kugou_client.set_refresh_callback(self._on_kugou_credentials_refreshed)
        if credentials.has_login:
            self._kugou_account = KugouAccount(
                userid=int(credentials.userid or 0), nickname=credentials.nickname
            )
            self._rebuild_sidebar()
            run_async(
                self._kugou_boot_check,
                on_done=self._on_kugou_boot_status,
                on_error=lambda message: logger.info("酷狗启动校验失败:%s", message),
            )

    def _kugou_boot_check(self) -> object:
        """启动校验:先静默续期(距过期不足 3 天),再验登录态。"""
        self._kugou_client.ensure_fresh_credentials()
        return self._kugou_client.verify_login()

    def _on_kugou_boot_status(self, account) -> None:
        if account is None:
            # token 链失效:只清 token,保留 KuGoo 串(播放仍要靠它)
            self._kugou_account = None
            self._kugou_playlists = []
            self._rebuild_sidebar()
            self.statusBar().showMessage(tr("kugou.expired_sidebar"))
            return
        self._enter_kugou_logged_in(account)

    def _on_kugou_credentials_refreshed(self, credentials: KugouCredentials) -> None:
        """续期回调:把新凭据写回磁盘(在后台线程被调用,只碰 store)。"""
        self._store.save_kugou(
            {
                "userid": credentials.userid,
                "token": credentials.token,
                "t1": credentials.t1,
                "vip_token": credentials.vip_token,
                "vip_type": credentials.vip_type,
                "t_expire_time": credentials.t_expire_time,
                "nickname": credentials.nickname,
                "mid": credentials.mid,
                "dfid": credentials.dfid,
                "kugoo_cookie": credentials.kugoo_cookie,
                "kugoo_cookie_at": credentials.kugoo_cookie_at,
            }
        )

    def _enter_kugou_logged_in(self, account: KugouAccount) -> None:
        self._kugou_account = account
        # 补写 profile,下次启动无需等校验就知道账号
        credentials = self._kugou_client.credentials()
        self._store.save_kugou(
            {
                "userid": credentials.userid,
                "token": credentials.token,
                "t1": credentials.t1,
                "vip_token": credentials.vip_token,
                "vip_type": credentials.vip_type,
                "t_expire_time": credentials.t_expire_time,
                "nickname": account.nickname or credentials.nickname,
                "mid": credentials.mid,
                "dfid": credentials.dfid,
                "kugoo_cookie": credentials.kugoo_cookie,
                "kugoo_cookie_at": credentials.kugoo_cookie_at,
            }
        )
        self.statusBar().showMessage(
            tr("kugou.logged_in", name=account.nickname or account.userid)
        )
        self._rebuild_sidebar()
        self._load_kugou_playlists()

    def _handle_kugou_stale_login(self, message: str) -> None:
        """酷狗登录失效:清 token 链,保留 KuGoo 串(播放仍可用)。"""
        credentials = self._kugou_client.credentials()
        self._kugou_client.set_credentials(
            KugouCredentials(
                userid=credentials.userid,
                token="",
                t1=credentials.t1,
                vip_token=credentials.vip_token,
                vip_type=credentials.vip_type,
                nickname=credentials.nickname,
                mid=credentials.mid,
                dfid=credentials.dfid,
                kugoo_cookie=credentials.kugoo_cookie,
                kugoo_cookie_at=credentials.kugoo_cookie_at,
            )
        )
        self._kugou_account = None
        self._kugou_playlists = []
        self._on_kugou_credentials_refreshed(self._kugou_client.credentials())
        self._rebuild_sidebar()
        self.statusBar().showMessage(message)

    def _handle_kugou_section_click(self) -> None:
        """侧栏「酷狗 · 未登录」被点击:先验 token,再按需补网页登录。"""
        if self._kugou_client.has_login():
            self.statusBar().showMessage(tr("kugou.checking"))
            run_async(
                self._kugou_boot_check,
                on_done=self._on_kugou_section_status,
                on_error=lambda message: self.statusBar().showMessage(
                    tr("kugou.check_failed", message=message)
                ),
            )
        else:
            self._open_kugou_login_dialog()

    def _on_kugou_section_status(self, account) -> None:
        if account is not None:
            self._enter_kugou_logged_in(account)
            return
        self._handle_kugou_stale_login(tr("kugou.expired"))
        self._open_kugou_login_dialog()

    def _open_kugou_login_dialog(self) -> None:
        """酷狗登录:先扫码(token 链),再按需补网页登录(KuGoo 链)。

        两步的原因见 docs/research/M6-kugou-api.md §2.6:扫码 token 管歌单,
        网页 KuGoo 串管播放,两者互不替代。已有 KuGoo 串时跳过第二步,
        避免用户每次重扫都要重登一次网页。
        """
        dialog = KugouQrLoginDialog(self._kugou_client, self)
        dialog.login_credentials_ready.connect(self._on_kugou_qr_ready)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not self._kugou_client.credentials().has_web_login:
            self._open_kugou_web_login_dialog()

    def _on_kugou_qr_ready(self, credentials: KugouCredentials) -> None:
        self.statusBar().showMessage(tr("kugou.qr.success"))
        self._on_kugou_credentials_refreshed(credentials)
        self._enter_kugou_logged_in(
            KugouAccount(
                userid=int(credentials.userid or 0),
                nickname=credentials.nickname,
            )
        )

    def _open_kugou_web_login_dialog(self) -> None:
        """网页登录补 KuGoo 串(播放侧凭据)。已有则直接返回。"""
        credentials = self._kugou_client.credentials()
        if credentials.has_web_login:
            return
        dialog = BrowserLoginDialog(kugou_web_login(), self)
        dialog.login_cookie_ready.connect(self._on_kugou_web_cookies)
        dialog.exec()

    def _on_kugou_web_cookies(self, cookies: dict) -> None:
        """网页登录成功:保存 KuGoo 串,补齐播放侧凭据。

        token 链可能已有(扫码在先),这里合并后再落盘。
        """
        kugoo_cookie = str(cookies.get("KuGoo") or "").strip()
        if not kugoo_cookie:
            self.statusBar().showMessage(tr("kugou.web_login_missing"))
            return
        credentials = self._kugou_client.credentials()
        merged = KugouCredentials(
            userid=str(cookies.get("KugooID") or credentials.userid),
            token=credentials.token,
            t1=credentials.t1 or str(cookies.get("t1") or ""),
            vip_token=credentials.vip_token,
            vip_type=credentials.vip_type,
            t_expire_time=credentials.t_expire_time,
            nickname=credentials.nickname or str(cookies.get("UserName") or ""),
            mid=str(cookies.get("mid") or cookies.get("kg_mid") or credentials.mid),
            dfid=str(cookies.get("dfid") or cookies.get("kg_dfid") or credentials.dfid),
            kugoo_cookie=kugoo_cookie,
            kugoo_cookie_at=int(time.time()),
        )
        self._kugou_client.set_credentials(merged)
        self._on_kugou_credentials_refreshed(merged)
        self.statusBar().showMessage(tr("kugou.web_login_done"))
        self._rebuild_sidebar()
        if merged.has_login and self._kugou_account is None:
            # 只做了网页登录:补一次 token 校验,能用就进登录态
            run_async(
                self._kugou_client.verify_login,
                on_done=self._on_kugou_boot_status,
                on_error=lambda message: logger.info("酷狗网页登录后校验失败:%s", message),
            )

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

    # -- 本地歌单(M7) --------------------------------------------------------

    def _boot_local_playlists(self) -> None:
        """启动加载本地歌单:读文件 → 套用拖拽顺序 → 重建侧栏 → 预取封面。

        不依赖任何登录态,断网也能列出;封面 URL 直接来自快照,无需回源。
        """
        self._local_playlists = self._apply_stored_order(
            self._store.load_local_playlists(),
            SETTING_LOCAL_PLAYLIST_ORDER,
            lambda p: p.id,
        )
        self._rebuild_sidebar()
        self._preload_local_playlist_covers()

    def _save_local_playlists(self) -> bool:
        """整份落盘;失败在状态栏提示(与 save_settings 的契约一致)。"""
        if self._store.save_local_playlists(self._local_playlists):
            return True
        self.statusBar().showMessage(tr("local.save_failed"))
        return False

    def _find_local_playlist(self, playlist_id: int) -> LocalPlaylist | None:
        return next(
            (p for p in self._local_playlists if p.id == playlist_id), None
        )

    def _create_local_playlist(self, name: str) -> LocalPlaylist | None:
        """新建本地歌单:分配 id、追加内存、落盘、重建侧栏。

        空名拒绝(不做静默改名),返回 None 由调用方提示。
        """
        cleaned = name.strip()[:LOCAL_PLAYLIST_NAME_MAX]
        if not cleaned:
            self.statusBar().showMessage(tr("local.name_empty"))
            return None
        playlist = LocalPlaylist(
            id=next_local_playlist_id(self._local_playlists),
            name=cleaned,
            created_at=int(time.time() * 1000),
        )
        self._local_playlists.append(playlist)
        self._save_local_playlists()
        self._rebuild_sidebar()
        self.statusBar().showMessage(tr("local.created", name=cleaned))
        return playlist

    def _add_song_to_local_playlist(
        self, playlist_id: int, song: QueueSong
    ) -> bool:
        """把一首歌加进指定本地歌单;同 (source, id) 已在则不改动,返回 False。

        歌单是「集合」语义(不同于队列允许重复),故写入侧再查一次重,
        防重复点击/并发路径绕过菜单置灰。
        """
        playlist = self._find_local_playlist(playlist_id)
        if playlist is None:
            return False
        # 去重键要带上音源私有标识:酷狗 id 恒为 0,真实标识在 bvid 里,
        # 只按 (source, id) 会把不同酷狗歌判成同一首
        extra = (
            {"bvid": song.bvid}
            if song.source in ("bili", "kugou") and song.bvid
            else {}
        )
        candidate = LocalPlaylistItem(
            source=song.source, id=song.id, extra=extra,
            title=song.title, artist=song.artist,
            duration_ms=song.duration_ms, cover_url=song.cover_url,
        )
        if any(item.key == candidate.key for item in playlist.items):
            self.statusBar().showMessage(
                tr("local.already_added", title=song.title, playlist=playlist.name)
            )
            return False
        playlist.items.append(candidate)
        self._save_local_playlists()
        self._refresh_local_playlist_views(playlist)
        self.statusBar().showMessage(
            tr("local.added", title=song.title, playlist=playlist.name)
        )
        return True

    def _local_playlist_songs(self, playlist: LocalPlaylist) -> list[QueueSong]:
        """快照条目 → 统一队列条目;展示侧净化文本,存储侧保留原文。"""
        return [
            QueueSong(
                source=item.source,
                id=item.id,
                bvid=item.extra.get("bvid", ""),
                title=sanitize_ui_text(item.title),
                artist=sanitize_ui_text(item.artist),
                duration_ms=item.duration_ms,
                cover_url=item.cover_url,
            )
            for item in playlist.items
        ]

    def _refresh_local_playlist_views(self, playlist: LocalPlaylist) -> None:
        """歌单内容变化后的刷新:侧栏重建 + (正在浏览它时)表格重填。

        正在浏览时表格重填是为了让用户立刻看到新加的歌;浏览别处时
        不动表格,避免打断。
        """
        self._rebuild_sidebar()
        self._preload_local_playlist_covers()
        context = self._table_context
        if context is None or context.kind != "local-playlist":
            return
        if context.id != playlist.id:
            return
        self._set_table_songs(
            self._local_playlist_songs(playlist), "netease", context
        )

    def _show_local_playlist(self, list_ref: _ListRef) -> None:
        """点开本地歌单:直接用快照填表,零网络、零后台任务。"""
        playlist = self._find_local_playlist(list_ref.id)
        if playlist is None:
            self.statusBar().showMessage(tr("local.missing"))
            return
        songs = self._local_playlist_songs(playlist)
        # 表头固定用「歌手」版:本地歌单可混收两源,列语义是「作者」
        self._set_table_songs(songs, "netease", list_ref)
        self.statusBar().showMessage(
            tr("list.loaded", title=list_ref.title, count=len(songs))
        )

    def _on_create_local_playlist_requested(self) -> None:
        """分区头「+」与「新建歌单并添加」共用的一次取名流程。"""
        name = self._ask_local_playlist_name()
        if name is None:
            return
        self._create_local_playlist(name)

    def _ask_local_playlist_name(self, title: str | None = None) -> str | None:
        """弹一行输入取名;取消或空名返回 None(空名给状态栏提示)。"""
        text, accepted = QInputDialog.getText(
            self, title or tr("local.create_prompt"), tr("local.name_label")
        )
        if not accepted:
            return None
        cleaned = text.strip()
        if not cleaned:
            self.statusBar().showMessage(tr("local.name_empty"))
            return None
        return cleaned

    def _set_local_playlist_cover(
        self, playlist_id: int, cover_url: str
    ) -> None:
        """设置/清空歌单手动封面(空串即恢复自动封面)并落盘。"""
        playlist = self._find_local_playlist(playlist_id)
        if playlist is None:
            return
        playlist.cover_url = cover_url
        self._save_local_playlists()
        self._rebuild_sidebar()
        if cover_url:
            self.statusBar().showMessage(tr("local.cover_set", name=playlist.name))
        else:
            self.statusBar().showMessage(tr("local.cover_auto_set", name=playlist.name))
        self._preload_local_playlist_covers()

    # -- 侧栏歌单封面(M7) ----------------------------------------------------

    # 侧栏图标格是 30x18,封面做成方图按 18px 高显示;圆角与表格缩略图同比例
    _SIDEBAR_COVER_SIZE = 18
    _SIDEBAR_COVER_RADIUS = 4

    def _preload_local_playlist_covers(self) -> None:
        """把全部本地歌单封面 URL 交给侧栏专用 loader 批量预取。

        preload 语义是「再次调用即作废上一批」,所以每次都交全集;
        磁盘缓存命中时不会触网,「启动后逐个亮起」由 5 线程池自然实现。
        """
        urls = [
            local_playlist_cover_url(playlist)
            for playlist in self._local_playlists
        ]
        self._sidebar_cover_urls = [url for url in urls if url]
        self._sidebar_cover_loader.preload(self._sidebar_cover_urls)

    def _on_sidebar_cover_ready(self, url: str, data: bytes) -> None:
        """某张歌单封面字节到达:解码成圆角图标缓存,并刷新对应歌单项。"""
        if url not in self._sidebar_cover_urls:
            return  # 已换歌单封面/已无引用:过期回调丢弃
        icon = self._sidebar_cover_icons.get(url)
        if icon is None:
            pixmap = QPixmap()
            if not pixmap.loadFromData(data):
                return
            dpr = self.sidebar.devicePixelRatioF()
            physical = max(1, round(self._SIDEBAR_COVER_SIZE * dpr))
            pixmap = rounded_pixmap(
                pixmap, physical, max(1, round(self._SIDEBAR_COVER_RADIUS * dpr))
            )
            pixmap.setDevicePixelRatio(dpr)
            icon = QIcon(pixmap)
            self._sidebar_cover_icons[url] = icon
        self._apply_local_playlist_icons()

    def _apply_local_playlist_icons(self) -> None:
        """封面到达后只刷本地歌单子节点图标,不整树重建。"""
        header = self.sidebar.find_header("local")
        if header is None:
            return
        for row in range(header.childCount()):
            self._apply_item_icons(header.child(row))

    # -- 侧栏右键菜单与手动封面 ------------------------------------------------

    def _on_sidebar_context_menu(self, pos) -> None:
        """侧栏右键:本地歌单的管理菜单(封面 / 重命名 / 删除 / 上移下移)。

        其余条目不弹菜单。三组之间加分隔符:封面、歌单实体、排序。
        上移/下移给不想拖拽的用户一条确定可用的路径(把手不可拖时尤其
        需要),首末项置灰。
        """
        item = self.sidebar.itemAt(pos)
        if item is None:
            return
        role = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(role, tuple) or role[0] != "local-playlist":
            return
        playlist_id = role[1]
        if not isinstance(playlist_id, int):
            return
        playlist = self._find_local_playlist(playlist_id)
        if playlist is None:
            return
        menu = QMenu(self.sidebar)
        cover_action = menu.addAction(tr("local.cover_menu"))
        auto_action = menu.addAction(tr("local.cover_auto"))
        # 没有手动封面时「使用自动封面」无意义,置灰但仍展示(可发现性)
        auto_action.setEnabled(bool(playlist.cover_url))
        cover_action.setEnabled(bool(playlist.items))
        menu.addSeparator()
        rename_action = menu.addAction(tr("local.rename_menu"))
        delete_action = menu.addAction(tr("local.delete_menu"))
        menu.addSeparator()
        up_action = menu.addAction(tr("local.move_up"))
        down_action = menu.addAction(tr("local.move_down"))
        order = [p.id for p in self._local_playlists]
        position = order.index(playlist_id) if playlist_id in order else -1
        up_action.setEnabled(position > 0)
        down_action.setEnabled(0 <= position < len(order) - 1)
        chosen = _exec_menu(menu, self.sidebar.viewport().mapToGlobal(pos))
        if chosen is auto_action:
            self._set_local_playlist_cover(playlist_id, "")
        elif chosen is cover_action:
            self._pick_local_playlist_cover(playlist)
        elif chosen is rename_action:
            self._rename_local_playlist(playlist)
        elif chosen is delete_action:
            self._delete_local_playlist(playlist)
        elif chosen is up_action:
            self._move_local_playlist(playlist_id, -1)
        elif chosen is down_action:
            self._move_local_playlist(playlist_id, 1)

    def _rename_local_playlist(self, playlist: LocalPlaylist) -> None:
        """重命名:预填原名,空名/重名/同名由 store 侧判定后提示。"""
        text, accepted = QInputDialog.getText(
            self,
            tr("local.rename_prompt"),
            tr("local.name_label"),
            text=sanitize_ui_text(playlist.name),
        )
        if not accepted:
            return
        cleaned = text.strip()
        if not cleaned:
            self.statusBar().showMessage(tr("local.name_empty"))
            return
        old_name = playlist.name
        if not rename_local_playlist(self._local_playlists, playlist.id, cleaned):
            if cleaned[:LOCAL_PLAYLIST_NAME_MAX] != old_name.strip():
                # 没有改动的原因只可能是重名(空名/同名已在上方排除)
                self.statusBar().showMessage(tr("local.name_taken"))
            return
        new_name = playlist.name
        self._save_local_playlists()
        self._retitle_local_playlist_refs(playlist.id, new_name)
        self._rebuild_sidebar()
        self.statusBar().showMessage(tr("local.renamed", name=new_name))

    def _retitle_local_playlist_refs(self, playlist_id: int, title: str) -> None:
        """改名后同步所有引用该歌单的 _ListRef 标题。

        _ListRef 是 frozen dataclass,只能整体重建;表格/队列上下文与
        「最近」栈里的标题都是快照,不同步会让状态栏与最近条目显示旧名。
        """
        def retitled(ref: _ListRef) -> _ListRef:
            if ref.kind == "local-playlist" and ref.id == playlist_id:
                return _ListRef(kind=ref.kind, id=ref.id, title=title)
            return ref

        if self._table_context is not None:
            self._table_context = retitled(self._table_context)
        if self._queue_context is not None:
            self._queue_context = retitled(self._queue_context)
        updated = [retitled(ref) for ref in self._recent]
        if updated != self._recent:
            self._recent = updated
            self._settings[SETTING_RECENT_LISTS] = [
                {"kind": ref.kind, "id": ref.id, "title": ref.title}
                for ref in updated
            ]
            self._store.save_settings(self._settings)

    def _delete_local_playlist(self, playlist: LocalPlaylist) -> None:
        """删除本地歌单:二次确认后清掉歌单与全部引用它的状态。

        引用清理缺一不可:排序键与最近栈残留会在 id 被复用(新建按
        max(id)+1 分配)后指向新歌单;播放队列来源只解绑不停播,避免
        打断正在听的歌。
        """
        answer = QMessageBox.question(
            self,
            tr("local.delete_menu"),
            tr("local.delete_confirm", name=playlist.name, count=len(playlist.items)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        name = playlist.name
        if delete_local_playlist(self._local_playlists, playlist.id) is None:
            return
        self._save_local_playlists()
        # 排序键:去掉该 id 并落盘(不清理会在 id 复用后指向新歌单)
        self._settings[SETTING_LOCAL_PLAYLIST_ORDER] = [
            p.id for p in self._local_playlists
        ]
        # 最近栈:剔除该歌单条目(否则点开会提示「歌单已不存在」)
        remaining = [
            ref for ref in self._recent
            if not (ref.kind == "local-playlist" and ref.id == playlist.id)
        ]
        if remaining != self._recent:
            self._recent = remaining
            self._settings[SETTING_RECENT_LISTS] = [
                {"kind": ref.kind, "id": ref.id, "title": ref.title}
                for ref in remaining
            ]
        self._store.save_settings(self._settings)
        # 侧栏封面缓存:URL 若被新歌单复用,残留 key 会让新回调被丢弃
        for url in list(self._sidebar_cover_urls):
            if url and url not in [
                local_playlist_cover_url(p) for p in self._local_playlists
            ]:
                self._sidebar_cover_urls.remove(url)
                self._sidebar_cover_icons.pop(url, None)
        # 表格正在浏览它:清表回空态
        if (
            self._table_context is not None
            and self._table_context.kind == "local-playlist"
            and self._table_context.id == playlist.id
        ):
            self._clear_table()
        # 队列来源是它:只解绑并标脏,不清空队列(不打断正在播放的歌)
        if (
            self._queue_context is not None
            and self._queue_context.kind == "local-playlist"
            and self._queue_context.id == playlist.id
        ):
            self._queue_context = None
            self._queue_dirty = True
        self._rebuild_sidebar()
        self._preload_local_playlist_covers()
        self.statusBar().showMessage(tr("local.deleted", name=name))

    def _move_local_playlist(self, playlist_id: int, delta: int) -> None:
        """侧栏右键上移/下移:与拖拽共用同一份顺序设置键。"""
        order = move_local_playlist_in_order(
            [p.id for p in self._local_playlists], playlist_id, delta
        )
        if order is None:
            return
        by_id = {p.id: p for p in self._local_playlists}
        if len(by_id) != len(order):
            return  # 与拖拽路径同款守卫:顺序表与内存不一致时不落库
        self._local_playlists = [by_id[i] for i in order]
        self._settings[SETTING_LOCAL_PLAYLIST_ORDER] = order
        self._store.save_settings(self._settings)
        self._rebuild_sidebar()

    def _pick_local_playlist_cover(self, playlist: LocalPlaylist) -> None:
        """从歌单内已有歌曲的封面里选一张作为手动封面。"""
        dialog = _CoverPickerDialog(playlist, self._sidebar_cover_loader, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        url = dialog.chosen_url()
        if url:
            self._set_local_playlist_cover(playlist.id, url)

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

    # -- 酷狗歌单(M6) --------------------------------------------------------

    def _load_kugou_playlists(self) -> None:
        """拉取酷狗歌单列表(token 链)。未登录直接返回。"""
        if self._kugou_account is None:
            return

        def fetch() -> object:
            return self._kugou_client.get_user_playlists()

        run_async(
            fetch,
            on_done=self._on_kugou_playlists_loaded,
            on_error=self._on_kugou_playlists_error,
        )

    def _on_kugou_playlists_loaded(self, playlists) -> None:
        if not isinstance(playlists, list):
            return
        # 套用分区内拖拽排序:已知歌单按存储序排前,新歌单追加尾部
        self._kugou_playlists = self._apply_stored_order(
            playlists, SETTING_KUGOU_PLAYLIST_ORDER, lambda p: p.listid
        )
        self._rebuild_sidebar()
        self.statusBar().showMessage(
            tr("list.kugou_loaded", count=len(playlists))
        )

    def _on_kugou_playlists_error(self, message: str) -> None:
        # 登录失效走重登提示,其余按加载失败提示(不自动登出:
        # 可能是网络抖动,清凭据会让用户白重扫一次)
        if isinstance(message, str) and "登录" in message:
            self.statusBar().showMessage(message)
            return
        self.statusBar().showMessage(
            tr("list.kugou_failed", message=message)
        )

    def _load_kugou_playlist_tracks(self, list_ref: _ListRef) -> None:
        """打开酷狗歌单:拉曲目并填表(只浏览,双击才起播)。"""
        playlist = next(
            (p for p in self._kugou_playlists if p.listid == list_ref.id), None
        )
        if playlist is None:
            self.statusBar().showMessage(tr("kugou.playlist_missing"))
            return
        self._clear_table()
        self.statusBar().showMessage(tr("list.loading", title=list_ref.title))

        def fetch() -> object:
            return self._kugou_client.get_playlist_tracks(playlist)

        def on_done(songs) -> None:
            if not isinstance(songs, list):
                return
            self._set_table_songs(
                [_kugou_song_to_queue(song) for song in songs],
                "kugou",
                list_ref,
            )
            self.statusBar().showMessage(
                tr("list.loaded", title=list_ref.title, count=len(songs))
            )

        def on_error(message: str) -> None:
            self.statusBar().showMessage(
                tr("list.kugou_tracks_failed", message=message)
            )
            if not self._table_songs:
                self._empty_state.set_hint(tr("list.kugou_tracks_failed_hint"))

        run_async(fetch, on_done=on_done, on_error=on_error)

    def _show_bili_items(self, items: list, list_ref: _ListRef) -> None:
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

    @staticmethod
    def _make_header_item(
        text: str, icon=None, kind: str | None = None, sortable: bool = False
    ) -> QTreeWidgetItem:
        """构造顶层分区头但不挂载(M8 起顺序由 SETTING_SECTION_ORDER 决定)。

        必须启用(不能用 NoItemFlags——禁用的父项会让子节点无法成为
        current/selected,折叠与拖拽都会失效)。sortable 的分区头额外给
        Selectable + DragEnabled:Qt 只对可选中项启动拖拽,实测仅给
        DragEnabled 时 startDrag 从不触发。「搜索」「设置」固定位置,
        保持不可选中也不可拖。
        """
        item = QTreeWidgetItem([text])
        flags = Qt.ItemFlag.ItemIsEnabled
        if sortable:
            flags |= (
                Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
        item.setFlags(flags)
        if kind is not None:
            item.setData(0, Qt.ItemDataRole.UserRole, (kind, None))
        if icon is not None:
            item.setIcon(0, icon)
        return item

    def _add_header_item(
        self, text: str, icon=None, kind: str | None = None
    ) -> QTreeWidgetItem:
        """构造并立即挂载顶层分区头(保留给不需要排序的调用点)。"""
        item = self._make_header_item(text, icon, kind)
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

        # 分区头先全部建好,最后按用户拖拽顺序统一挂载(M8):_add_header_item
        # 内部会 addTopLevelItem,这里改用不挂载的构造,顺序由末尾决定。
        recent_header: QTreeWidgetItem | None = None
        # 「最近」分区:跨平台聚合最近播放过的列表,非空才出现
        # (空分区只有占位噪音,首次起播后自然浮现)
        if self._recent:
            recent_header = self._make_header_item(
                tr("sidebar.recent"), None, "recent-header", sortable=True
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

        netease_header = self._make_header_item(
            tr("sidebar.netease_playlists"),
            self._brand_header_icon("netease"), "netease-header", sortable=True,
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
        subscribed_header: QTreeWidgetItem | None = None
        if self._account is not None:
            subscribed_header = self._make_header_item(
                tr("sidebar.netease_subscribed"),
                self._brand_header_icon("netease"), "netease-subscribed-header",
                sortable=True,
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

        bili_header = self._make_header_item(
            tr("sidebar.bili_folders"),
            self._brand_header_icon("bilibili"), "bili-header", sortable=True,
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

        # 酷狗(M6):未登录时与网易云/B站一致,只放一个登录入口
        kugou_header = self._make_header_item(
            tr("sidebar.kugou_playlists"), None, "kugou-header", sortable=True,
        )
        if self._kugou_account is None:
            self._add_child_item(
                kugou_header, tr("sidebar.kugou_login"), "kugou-login", None
            )
        else:
            for playlist in self._kugou_playlists:
                title = sanitize_ui_text(playlist.name)
                if playlist.count:
                    title = f"{title}({playlist.count})"
                self._add_child_item(
                    kugou_header, title, "kugou-playlist", playlist.listid
                )
            if not self._kugou_playlists:
                self._add_child_item(
                    kugou_header, tr("sidebar.loading_kugou"),
                    "kugou-loading", None, selectable=False,
                )

        # 本地歌单(M7):始终出现(分区头右侧的「+」是新建入口,空分区
        # 也必须有入口);子节点 payload 是裸 int,与其他平台歌单一致
        local_header = self._make_header_item(
            tr("sidebar.local_playlists"), None, "local-header", sortable=True,
        )
        for playlist in self._local_playlists:
            title = sanitize_ui_text(playlist.name)
            if playlist.items:
                title = f"{title}({len(playlist.items)})"
            self._add_child_item(
                local_header, title, "local-playlist", playlist.id
            )
        if not self._local_playlists:
            self._add_child_item(
                local_header, tr("sidebar.local_empty_hint"),
                "local-empty", None, selectable=False,
            )

        # M8:按用户拖拽顺序挂载分区头。「搜索」固定置顶、「设置」固定置底,
        # 中间各分区顺序取自 section_order;配置里没有的新分区追加到尾部。
        headers: dict[str, QTreeWidgetItem] = {"netease": netease_header,
                                               "bili": bili_header,
                                               "kugou": kugou_header,
                                               "local": local_header}
        if self._account is not None and subscribed_header is not None:
            headers["netease-subscribed"] = subscribed_header
        if recent_header is not None:
            headers["recent"] = recent_header
        ordered_sections = apply_section_order(
            list(headers), self._settings.get(SETTING_SECTION_ORDER, [])
        )
        for section in ordered_sections:
            header = headers.get(section)
            if header is not None:
                self.sidebar.addTopLevelItem(header)

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
        "kugou-login": "person",
        "kugou-playlist": "library_music",
        # 本地歌单默认图标;有封面时由 _apply_item_icons 换成封面缩略图
        "local-playlist": "library_music",
        "local-empty": "library_music",
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
        elif kind == "kugou-header":
            # 「酷狗」分区头:暂无品牌标资产,单色标 + 折叠三角。
            # 不能拿 "kugou" 调 _section_header_icon(会 KeyError,同 local)。
            item.setIcon(
                0, self._monochrome_header_icon("library_music", item.isExpanded())
            )
        elif kind == "local-header":
            # 「本地歌单」分区头:同样无品牌标,单色标 + 折叠三角。
            # 不能拿分区键 "local" 调 _section_header_icon(会去
            # _SOFT_BRAND_COLORS 查品牌色并 KeyError)。
            item.setIcon(
                0, self._monochrome_header_icon("library_music", item.isExpanded())
            )
        elif kind == "local-playlist" and isinstance(role[1], int):
            # 本地歌单:有(手动或自动)封面就用封面缩略图,否则回落单色标
            item.setIcon(0, self._local_playlist_icon(role[1]))
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

    def _local_playlist_icon(self, playlist_id: int) -> QIcon:
        """歌单侧栏图标:封面缩略图(已解码则复用),无封面回落单色标。"""
        playlist = self._find_local_playlist(playlist_id)
        if playlist is not None:
            url = local_playlist_cover_url(playlist)
            icon = self._sidebar_cover_icons.get(url) if url else None
            if icon is not None:
                return icon
        return tinted_icon("library_music")


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
        elif kind == "kugou-playlist":
            for playlist in self._kugou_playlists:
                if playlist.listid == entry_id:
                    return _ListRef(kind, entry_id, playlist.name)
        elif kind == "local-playlist":
            playlist = self._find_local_playlist(entry_id)
            if playlist is not None:
                return _ListRef("local-playlist", entry_id, playlist.name)
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
        elif list_ref.kind == "kugou-playlist":
            self._load_kugou_playlist_tracks(list_ref)
        elif list_ref.kind == "local-playlist":
            # 本地快照直接填表,不发任何网络请求
            self._show_local_playlist(list_ref)
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
            # 分区头为可拖换序必须可选中(M8),但选中态对它是噪音:
            # 它只承载折叠,不像子节点那样表示「当前浏览的是谁」。
            # 拖拽由按下后移动触发,清选中不影响。
            self.sidebar.clearSelection()
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
        if kind == "local-playlist":
            self._open_list_ref(self._sidebar_list_ref(kind, payload, item))
            return
        if kind == "kugou-playlist":
            self._open_list_ref(self._sidebar_list_ref(kind, payload, item))
            return
        if kind == "kugou-login":
            self.central_stack.setCurrentIndex(_PAGE_TABLE)
            self._set_search_mode(False)
            self._clear_table()
            self._handle_kugou_section_click()
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

    def _on_section_order_changed(self, sections: list) -> None:
        """分区头换序落库:存分区键序列,重建后顺序保持不变。

        只存可排序分区(不含搜索/设置)。未知/非法值由 store 侧
        validate_section_order 清洗,这里不做二次过滤。
        """
        if not isinstance(sections, list):
            return
        self._settings[SETTING_SECTION_ORDER] = list(sections)
        self._store.save_settings(self._settings)

    def _on_sidebar_order_changed(self, section: str) -> None:
        """分区内拖拽排序落库:读子节点顺序写设置键,并同步内存列表。

        每个分区显式分支,不用 else 兜底:新增分区(如 M7 的 local)若
        落进 B站分支,会拿收藏夹字典按歌单 id 建映射、写错设置键且
        静默丢数据。
        """
        ids = self.sidebar.section_child_ids(section)
        if section == "netease":
            setting = SETTING_NETEASE_PLAYLIST_ORDER
            by_id = {p.id: p for p in self._playlists}
        elif section == "netease-subscribed":
            setting = SETTING_NETEASE_SUBSCRIBED_ORDER
            by_id = {p.id: p for p in self._subscribed_playlists}
        elif section == "bili":
            setting = SETTING_BILI_FOLDER_ORDER
            by_id = {f.media_id: f for f in self._bili_folders}
        elif section == "kugou":
            # M6:酷狗歌单同样参与分区内拖拽排序
            setting = SETTING_KUGOU_PLAYLIST_ORDER
            by_id = {p.listid: p for p in self._kugou_playlists}
        elif section == "local":
            setting = SETTING_LOCAL_PLAYLIST_ORDER
            by_id = {p.id: p for p in self._local_playlists}
        else:
            return  # 未知分区:不落库也不改内存
        self._settings[setting] = ids
        self._store.save_settings(self._settings)
        # 内存列表同步成相同顺序,后续 _rebuild_sidebar 不会回退到旧顺序
        if len(by_id) == len(ids):
            ordered = [by_id[i] for i in ids]
            if section == "netease":
                self._playlists = ordered
            elif section == "netease-subscribed":
                self._subscribed_playlists = ordered
            elif section == "bili":
                self._bili_folders = ordered
            elif section == "kugou":
                self._kugou_playlists = ordered
            else:
                self._local_playlists = ordered


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
        """表空时切到空态视图;文案按当前浏览上下文与登录状态区分。"""
        empty = self.song_table.rowCount() == 0
        self.table_stack.setCurrentIndex(1 if empty else 0)
        if not empty:
            return
        context = self._table_context
        if context is not None and context.kind == "local-playlist":
            # 空本地歌单:提示加歌入口,而不是通用的「从左侧选择歌单」
            self._empty_state.set_hint(tr("empty.local_playlist"))
        elif self._account is None and self._bili_account is None:
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
        self._apply_table_drag_mode(context)
        self._fill_song_table()

    def _apply_table_drag_mode(self, context: _ListRef | None) -> None:
        """只有本地歌单可拖拽调序。

        远端列表(网易云/B站/酷狗/搜索)的内容顺序由接口决定,不能拖;
        本地歌单的 items 顺序就是用户意图,拖完落盘。开关放在
        _set_table_songs 里,所有填表路径(点开/刷新/搜索)自动一致。
        """
        editable = context is not None and context.kind == "local-playlist"
        mode = (
            QAbstractItemView.DragDropMode.InternalMove
            if editable
            else QAbstractItemView.DragDropMode.NoDragDrop
        )
        self.song_table.setDragDropMode(mode)
        self.song_table.setDragEnabled(editable)
        self.song_table.setAcceptDrops(editable)
        self.song_table.setDropIndicatorShown(editable)
        if editable:
            self.song_table.setDefaultDropAction(Qt.DropAction.MoveAction)

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
        """当前播放曲在表中的行号(按 QueueSong.key 匹配;不在当前表为 None)。

        匹配必须走 key 而非 (source, id):酷狗歌曲 id 恒 0、真实标识在
        bvid,同表两首酷狗歌按 (source, id) 会同时命中,排序后高亮跳行。
        """
        active = self._active
        row = None
        if active is not None:
            song_key = active.song.key
            row = next(
                (
                    r
                    for r, item in enumerate(self._table_songs)
                    if item.key == song_key
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
        self._search_kugou_btn.setChecked(source == "kugou")
        self._update_source_indicator(animate=True)
        if self._search_keyword:
            self._run_search(self._search_keyword, reset=True)

    def _update_source_indicator(self, animate: bool = True) -> None:
        """指示线贴选中按钮、按文字宽居中(布局变化即时对位,切换时滑动)。"""
        # 三源查表:新增来源只改这一处,避免嵌套三元漏判
        button = {
            "netease": self._search_netease_btn,
            "bili": self._search_bili_btn,
            "kugou": self._search_kugou_btn,
        }.get(self._search_source, self._search_netease_btn)
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
        elif source == "kugou":
            def fetch() -> object:
                return self._kugou_client.search_songs(
                    keyword,
                    limit=_SEARCH_PAGE_SIZE,
                    offset=(page - 1) * _SEARCH_PAGE_SIZE,
                )

            def on_done(result) -> None:
                if generation != self._search_generation:
                    return
                songs, total = result
                self._apply_search_page(
                    [_kugou_song_to_queue(song) for song in songs],
                    "kugou",
                    page * _SEARCH_PAGE_SIZE < total,
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
        """歌曲表右键菜单:「下一首播放」+「添加到…」本地歌单二级菜单。

        本地歌单子项用 QAction.setData(歌单 id) 而不是闭包捕获:
        menu.exec 只返回选中的 QAction,data() 可反查目标,测试也能从
        action 直接读目标。

        浏览本地歌单时追加「从歌单移除」:该列表的内容是用户自己维护的,
        才有「移除」语义;远端歌单的内容不由本应用掌管,不出现此项。
        """
        row = self.song_table.indexAt(pos).row()
        if not (0 <= row < len(self._table_songs)):
            return
        song = self._table_songs[row]
        menu = QMenu(self.song_table)
        play_next_action = menu.addAction(tr("play.play_next_menu"))
        add_menu = menu.addMenu(tr("play.add_to_menu"))
        playlist_actions, new_action = self._menu_local_playlist_actions(add_menu, song)
        remove_action = None
        context = self._table_context
        if context is not None and context.kind == "local-playlist":
            menu.addSeparator()
            remove_action = menu.addAction(tr("local.remove_menu"))
        chosen = _exec_menu(menu, self.song_table.viewport().mapToGlobal(pos))
        if chosen is play_next_action:
            self._insert_next_from_table(row)
        elif chosen is new_action:
            self._create_local_playlist_and_add(song)
        elif chosen in playlist_actions:
            self._add_song_to_local_playlist(chosen.data(), song)
        elif remove_action is not None and chosen is remove_action:
            self._remove_song_from_local_playlist(context.id, song)

    def _on_table_reorder_requested(self, from_row: int, to_row: int) -> None:
        """本地歌单内拖拽调序:改 items 顺序 → 落盘 → 重填表格。

        _queue_dirty 必须置真:双击起播时只有「上下文不同或脏」才重装
        队列(_play_from_table),否则会按新行号去播旧顺序的歌。
        """
        context = self._table_context
        if context is None or context.kind != "local-playlist":
            return
        playlist = self._find_local_playlist(context.id)
        if playlist is None:
            return
        if not move_local_playlist_item(playlist, from_row, to_row):
            return
        self._save_local_playlists()
        self._queue_dirty = True
        # 走统一刷新:侧栏条数/自动封面与表格行序一起更新
        self._refresh_local_playlist_views(playlist)

    def _remove_song_from_local_playlist(
        self, playlist_id: int, song: QueueSong
    ) -> None:
        """从本地歌单移除一首歌:按 LocalPlaylistItem.key 定位后落盘。

        必须走 key(而非 (source, id)):酷狗歌曲 id 恒为 0,真实标识在
        extra["bvid"] 里,只比 id 会删错歌。
        """
        playlist = self._find_local_playlist(playlist_id)
        if playlist is None:
            return
        probe = LocalPlaylistItem(
            source=song.source,
            id=song.id,
            extra={"bvid": song.bvid} if song.bvid else {},
        )
        if remove_local_playlist_item(playlist, probe.key) is None:
            return
        self._save_local_playlists()
        self._queue_dirty = True
        self._refresh_local_playlist_views(playlist)
        self.statusBar().showMessage(
            tr("local.removed", playlist=playlist.name, title=song.title)
        )

    def _menu_local_playlist_actions(
        self, menu: QMenu, song: QueueSong
    ) -> tuple[list, object]:
        """填充「添加到…」二级菜单;返回 (已有歌单动作列表, 新建并添加动作)。

        已含该曲的歌单置灰并追加「(已在)」后缀,点之前就知道结果;
        写入侧 _add_song_to_local_playlist 仍会再查一次重。
        """
        # 复用 LocalPlaylistItem.key 的判定(含音源私有标识),不要在这里
        # 另写 (source, id) 比较:酷狗 id 恒为 0,那样会把不同歌判成重复
        probe = LocalPlaylistItem(
            source=song.source, id=song.id,
            extra={"bvid": song.bvid} if song.bvid else {},
        )
        actions: list = []
        for playlist in self._local_playlists:
            already = any(item.key == probe.key for item in playlist.items)
            label = sanitize_ui_text(playlist.name)
            if already:
                label = f"{label}{tr('local.already_tag')}"
            action = menu.addAction(label)
            action.setData(playlist.id)
            action.setEnabled(not already)
            actions.append(action)
        if self._local_playlists:
            menu.addSeparator()
        new_action = menu.addAction(tr("play.add_to_new"))
        return actions, new_action

    def _create_local_playlist_and_add(self, song: QueueSong) -> None:
        """「新建歌单并添加」:一次取名同时完成建单与加歌。"""
        name = self._ask_local_playlist_name()
        if name is None:
            return
        playlist = self._create_local_playlist(name)
        if playlist is not None:
            self._add_song_to_local_playlist(playlist.id, song)

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
                quality_key=prefetched.quality_key,
                quality_kbps=prefetched.quality_kbps,
            )
            return

        self.statusBar().showMessage(tr("play.resolving", title=song.title))
        self._resolve_and_start(song, generation)

    def _resolve_song_context(self, song: QueueSong) -> _SongContext:
        """解析一首歌的播放上下文(网络部分):点歌起播与预取共用同一逻辑。

        三源各自分派都在这里,新增音源必须加显式分支。尾部对未知 source
        抛错,不能靠「默认当网易云」兜底,否则酷狗歌会被拿 song.id 当
        网易云歌曲 id 去请求(见 docs/research/M6-integration-points.md §2.1)。
        登录失效与解析失败以异常抛出,由调用方按各自策略处理
        (起播要提示,预取则静默吞掉)。
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
            q_key, q_kbps = _bili_quality_parts(stream)
            return _SongContext(
                rotator=BackupUrlRotator(stream.candidate_urls),
                headers=build_bili_stream_headers(),
                status=tr(
                    "play.playing",
                    title=song.title, quality=_bili_quality_suffix(stream),
                ),
                quality_key=q_key,
                quality_kbps=q_kbps,
            )
        if song.source == "kugou":
            return self._resolve_kugou_context(song, quality)
        if song.source == "netease":
            return self._resolve_netease_context(song, quality)
        raise ValueError(f"未知音源:{song.source}")

    def _resolve_netease_context(self, song: QueueSong, quality: str) -> _SongContext:
        """网易云播放上下文(从 _resolve_song_context 拆出,行为不变)。"""
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
        # 试听片段不标音质(用户听到的不是完整音质,标档位会误导)
        q_key, q_kbps = (
            ("", 0) if playable.is_preview else _netease_quality_parts(playable.level)
        )
        return _SongContext(
            rotator=BackupUrlRotator([playable.url]), headers=None, status=status,
            quality_key=q_key, quality_kbps=q_kbps,
        )

    def _resolve_kugou_context(self, song: QueueSong, quality: str) -> _SongContext:
        """酷狗播放上下文。

        酷狗歌曲标识是 hash / encode_album_audio_id 这类字符串,放不进
        QueueSong.id(int),统一从 extra 取(见 _kugou_song_from_queue)。
        播放地址有时效,每次现解析,不做持久缓存。
        """
        if self._kugou_client is None:
            raise KugouApiError(tr("kugou.not_inited"))
        kugou_song = _kugou_song_from_queue(song)
        playable = self._kugou_client.resolve_playable_url(
            kugou_song, preferred_quality=quality
        )
        # 档位名走 i18n(原 selector 里是硬编码中文,英文界面会漏中文)
        q_key = kugou_quality_i18n_key(quality, playable.quality_key)
        q_kbps = playable.bitrate_kbps
        return _SongContext(
            rotator=BackupUrlRotator(playable.candidate_urls),
            headers=None,
            status=tr(
                "play.playing",
                title=song.title,
                quality=kugou_quality_label(quality, playable.quality_key),
            ),
            quality_key=q_key,
            quality_kbps=q_kbps,
        )

    def _resolve_and_start(self, song: QueueSong, generation: int) -> None:
        """后台解析播放地址并起播(点歌与预取过期重解析共用)。

        回调带代际 token:已被更新的点歌覆盖的迟到结果一律静默作废
        (不计失败、不跳歌、不重登)。
        """
        if song.source == "bili":
            on_done = self._make_bili_play_done(song, generation)
        elif song.source == "kugou":
            on_done = self._make_kugou_play_done(song, generation)
        else:
            on_done = self._make_netease_play_done(song, generation)

        def resolve() -> tuple[str, object]:
            try:
                return ("ok", self._resolve_song_context(song))
            except (NeteaseAuthRequiredError, BiliAuthRequiredError,
                    KugouAuthRequiredError):
                # 三源鉴权异常都要归到 "auth":否则酷狗的登录失效会被当成
                # 普通播放失败,走自动跳歌而不是提示重登(见
                # docs/research/M6-integration-points.md §2.1 的 A4)
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
            self._start_play(
                song, payload.rotator, payload.headers, status=payload.status,
                quality_key=payload.quality_key, quality_kbps=payload.quality_kbps,
            )

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
            self._start_play(
                song, payload.rotator, payload.headers, status=payload.status,
                quality_key=payload.quality_key, quality_kbps=payload.quality_kbps,
            )

        return on_done

    def _make_kugou_play_done(self, song: QueueSong, generation: int):
        """酷狗起播完成回调。

        与网易云/B站的差异:酷狗的凭据失效分两种(见 M6 调研 §2.6),
        提示语要能区分,否则用户不知道该重扫还是该重登网页:
        - 歌单侧 token 失效 -> 提示重新扫码
        - 播放侧 KuGoo 串缺失/失效 -> 提示重新做网页登录
        """

        def on_done(result) -> None:
            if generation != self._resolve_generation:
                return  # 过期请求:已被更新的点歌覆盖,结果作废
            kind, payload = result
            if kind == "auth":
                self._handle_kugou_stale_login(tr("kugou.auth_expired"))
                return
            if kind == "error":
                self._handle_play_failure(song, payload)
                return
            self._start_play(
                song, payload.rotator, payload.headers, status=payload.status,
                quality_key=payload.quality_key, quality_kbps=payload.quality_kbps,
            )

        return on_done

    def _start_play(
        self,
        song: QueueSong,
        rotator: BackupUrlRotator,
        headers: dict[str, str] | None,
        status: str | None = None,
        from_prefetch: bool = False,
        quality_key: str = "",
        quality_kbps: int = 0,
    ) -> None:
        url = rotator.current
        if not url:
            # 解析成功但没有任何可用地址:同样是播放失败,走统一出口
            self._handle_play_failure(song, tr("play.no_url"))
            return
        # 音质标签随起播一起刷新:解析层已给出最终生效档位(含降级回退)
        self.player_bar.set_quality_parts(quality_key, quality_kbps)
        self._active = _ActivePlay(
            song=song, rotator=rotator, headers=headers, from_prefetch=from_prefetch
        )
        self._update_playing_row()  # 新起播:均衡器条移到新行
        self._play_fail_count = 0  # 成功发起播放:连续失败计数复位
        self._set_play_controls_active(True)
        if self._route_guard is not None:
            # 用户选歌(含自动接播)即用户意图:清除路由暂停的自动恢复态
            self._route_guard.notify_user_action()
        self._apply_song_window_title()  # 任务栏文案换为歌名(等宽规范)
        self.statusBar().showMessage(
            status or tr("play.playing", title=song.title, quality="")
        )
        self.engine.play_url(url, headers)
        self._push_recent()  # 成功起播才算「听过」(仅浏览不压栈)
        self._schedule_prefetch_next()  # 起播成功:顺手后台预取下一首
        self._stats_begin_song(song)  # 新播放周期开始(结算上一首)

    # -- 播放统计 --------------------------------------------------------------

    def _stats_begin_song(self, song: QueueSong) -> None:
        """起播时开新统计周期;上一首的遗留增量在此结算落盘。"""
        key = make_key(song.source, song.id, song.bvid)
        leftover = self._stats_tracker.on_song_changed(key)
        self._stats_apply_delta(key, leftover, song)
        self._stats_ensure_timer()

    def _stats_ensure_timer(self) -> None:
        """惰性启动 250ms 统计定时器(与引擎进度轮询同频)。

        定时器独立于引擎:引擎在 stop 时会停掉自己的轮询,而统计需要在
        暂停、切歌等时刻仍能周期性落盘。
        """
        if self._stats_timer is not None:
            if not self._stats_timer.isActive():
                self._stats_timer.start()
            return
        timer = QTimer(self)
        timer.setInterval(250)
        timer.timeout.connect(self._stats_on_tick)
        timer.start()
        self._stats_timer = timer

    def _stats_on_tick(self) -> None:
        """250ms tick:喂给 tracker,并按周期落盘。

        窗口不可见(托盘/最小化)时不跑:统计靠墙钟增量,隐藏期间本来
        就不该计时,跳过也顺带避免后台空转。
        """
        if not self.isVisible():
            return
        engine = self.engine
        if engine is None:
            return
        position = engine.position()
        duration = engine.duration()
        self._stats_tracker.on_progress(position, duration)
        if self._stats_tracker.should_flush():
            self._stats_flush()

    def _stats_apply_delta(
        self,
        key: str,
        delta: object,
        song: QueueSong | None = None,
    ) -> None:
        """把增量写进内存表。song 为 None 时只在已有条目上累加。"""
        if delta is None:
            return
        listened = getattr(delta, "listened_ms", 0)
        count = getattr(delta, "count_inc", 0)
        if listened <= 0 and count <= 0:
            return
        stat = self._play_stats.get(key)
        if stat is None:
            if song is None:
                return
            now = int(time.time() * 1000)
            stat = PlayStat(
                key=key,
                source=song.source,
                song_id=song.id,
                bvid=song.bvid,
                title=song.title,
                artist=song.artist,
                cover_url=song.cover_url,
                duration_ms=song.duration_ms,
                first_played_at=now,
                last_played_at=now,
            )
            self._play_stats[key] = stat
        if listened > 0:
            stat.listen_ms += int(listened)
        if count > 0:
            stat.play_count += int(count)
        stat.last_played_at = int(time.time() * 1000)

    def _stats_flush(self) -> None:
        """把内存表整份落盘(增量已先写进内存,写失败不丢累计值)。"""
        if not self._play_stats:
            return
        self._play_stats_store.save(list(self._play_stats.values()))

    def _stats_settle_current(self) -> None:
        """结束当前播放周期并落盘(暂停/停止/切歌/退出时调用)。"""
        key = self._stats_tracker.key
        if not key:
            return
        delta = self._stats_tracker.take_delta()
        self._stats_apply_delta(key, delta)
        self._stats_flush()

    def _stats_on_track_ended(self) -> None:
        """整曲播完:无条件计一次并落盘。"""
        key = self._stats_tracker.key
        if not key:
            return
        delta = self._stats_tracker.on_track_ended()
        self._stats_apply_delta(key, delta)
        self._stats_flush()

    def _ensure_stats_page(self) -> StatsPage:
        """首次打开统计页时才构造并挂进页面栈。"""
        if self.stats_page is None:
            self.stats_page = StatsPage()
            self.stats_page.back_requested.connect(
                lambda: self.central_stack.setCurrentIndex(_PAGE_SETTINGS)
            )
            self.stats_page.clear_requested.connect(self._on_stats_clear_requested)
            self.central_stack.addWidget(self.stats_page)
            self._stats_page_added = True
        return self.stats_page

    def _open_stats_page(self) -> None:
        """从设置页打开统计页:重算并填充,再切页。"""
        self._stats_settle_current()
        page = self._ensure_stats_page()
        page.set_stats(self._play_stats.values())
        self.central_stack.setCurrentIndex(_PAGE_STATS)

    def _on_stats_clear_requested(self) -> None:
        """清空统计:二次确认后清内存、删文件、刷新页面。"""
        answer = QMessageBox.question(
            self,
            tr("stats.clear_confirm_title"),
            tr("stats.clear_confirm_text"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._play_stats.clear()
        self._play_stats_store.clear()
        if self.stats_page is not None:
            self.stats_page.set_stats([])
        self.statusBar().showMessage(tr("stats.empty"))

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
                quality_key=context.quality_key,
                quality_kbps=context.quality_kbps,
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
        # 统计:整曲播完无条件计一次(短曲不足 30 秒也算)
        self._stats_on_track_ended()
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
        page.stats_open_requested.connect(self._open_stats_page)
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
        if self._route_guard is not None:
            self._route_guard.set_playing(playing)
        # 统计:暂停必须立即停表(mpv 暂停生效前会漏一个带增量的 tick)
        self._stats_tracker.on_playing_changed(playing)
        if not playing:
            self._stats_settle_current()

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
        if self._route_guard is not None:
            # 手动播放/暂停:撤销「切回耳机自动恢复」意图,用户说了算
            self._route_guard.notify_user_action()
        if self.engine is not None:
            self.engine.toggle_pause()

    def _set_play_controls_active(self, active: bool) -> None:
        """播放控制可用态:播放条与任务栏缩略图按钮同步启用/禁用。"""
        self.player_bar.set_active(active)
        if self._taskbar is not None:
            self._taskbar.set_enabled(active)

    def _on_seek_requested(self, position_s: float) -> None:
        # 统计:拖到结尾会触发 mpv 的 eof,那种情况不算「播完」一次
        self._stats_tracker.on_seek(position_s)
        if self.engine is not None:
            self.engine.seek(position_s)

    def _on_volume_changed(self, volume: int) -> None:
        if self.engine is not None:
            self.engine.set_volume(volume)
        # 音量记忆(M10):内存值立即更新,落盘延后到拖动停下
        # (_VOLUME_SAVE_DELAY_MS),同一次拖动只写一次文件
        self._settings[SETTING_VOLUME] = max(0, min(100, int(volume)))
        self._volume_save_timer.start()

    def _flush_volume_setting(self) -> None:
        """把当前音量写进 settings.json;IO 失败静默(音量属轻量偏好,
        不值得为一次落盘失败弹状态栏,下次改动会再试)。"""
        if self._volume_save_timer.isActive():
            self._volume_save_timer.stop()
        self._store.save_settings(self._settings)

    def _restore_volume(self) -> None:
        """启动回填上次退出的音量(滑块 + mpv 双侧),默认 70。

        滑块回填会触发 valueChanged,但 PlayerBar.set_volume 内部屏蔽了
        信号,不会反过来再发一次音量变更;引擎侧直接 set 一次即可。
        """
        volume = self._settings.get(SETTING_VOLUME, DEFAULT_VOLUME)
        if not isinstance(volume, int) or isinstance(volume, bool):
            volume = DEFAULT_VOLUME
        volume = max(0, min(100, volume))
        self.player_bar.set_volume(volume)
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

    # -- 音频路由:耳机断开自动暂停/切回恢复 ------------------------------------

    def _setup_route_guard(self) -> None:
        """默认输出设备监视(增强能力,失败静默降级为无自动暂停)。

        对照 Android PlayerManager:路由离开耳机类 → 暂停(蓝牙短确认
        防抖),回到耳机类 → 恢复;用户手动操作撤销自动恢复意图。
        常开能力,不设开关。
        """
        if sys.platform != "win32":
            return
        try:
            self._route_guard = AudioRouteGuard(self)
            self._route_guard.start()
        except Exception:  # noqa: BLE001 - 路由监视属增强能力,绝不崩启动
            logger.exception("音频路由守卫初始化失败")
            self._route_guard = None
            return
        guard = self._route_guard
        guard.pause_requested.connect(self._on_route_pause)
        guard.resume_requested.connect(self._on_route_resume)

    def _on_route_pause(self, reason: str) -> None:
        """路由丢失(耳机断开/设备切离):暂停并提示。"""
        guard = self._route_guard
        if guard is None:
            return
        if self.engine is None or not self.engine.has_media:
            return
        if not self.engine.is_playing():
            return
        logger.info("音频路由丢失,自动暂停: %s", reason)
        self.engine.set_paused(True)
        self.statusBar().showMessage(tr("route.paused"))

    def _on_route_resume(self, reason: str) -> None:
        """路由回到耳机类设备:恢复因路由暂停的播放。"""
        guard = self._route_guard
        if guard is None:
            return
        if self.engine is None or not self.engine.is_paused():
            # 只解除「因路由而设的暂停」;播完转 idle/已停/用户已操作时
            # 不动 mpv(set_paused(False) 会发多余的 playing_changed)
            return
        logger.info("音频路由切回耳机,恢复播放: %s", reason)
        self.engine.set_paused(False)
        self.statusBar().showMessage(tr("route.resumed"))

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
        bootstamp.report_show_once()  # 启动桩收尾:窗口首显(仅首次)
        # 缩略图工具栏需窗口已有任务栏按钮:每次显示时尝试附加,
        # 成功一次即固定(离屏/COM 不可用时 attach 恒失败,静默重试无害)
        if self._taskbar is not None:
            self._taskbar.attach(int(self.winId()))

    def nativeEvent(self, eventType, message):  # noqa: N802 - Qt 命名
        """窗口过程层消息分发:任务栏缩略图按钮点击等原生消息。

        缩略图按钮的 WM_COMMAND 是 Explorer 跨进程 SendMessage 直发
        窗口过程的,应用级 native event filter 看不到(实测),必须在
        这里接;返回 True 短路 Qt 默认处理。未命中返回 (False, 0),
        与基类默认等价(PySide6 基类不支持从 Python 侧合成参数直调)。
        """
        if self._taskbar is not None:
            try:
                if self._taskbar.handle_native_event(eventType, message):
                    return True, 0
            except Exception:  # noqa: BLE001 - 窗口过程内绝不抛
                pass
        return False, 0

    def _on_media_stop(self) -> None:
        if self._route_guard is not None:
            self._route_guard.notify_user_action()
        if self.engine is not None:
            self.engine.stop()
        if self._stats_timer is not None:
            self._stats_timer.stop()  # 停止播放:统计定时器也停,不再空转
        self.player_bar.set_playing(False)
        self.player_bar.clear_quality()  # 停止:不留上一首的档位
        self._stats_settle_current()  # 停止:结算并落盘当前周期
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
        # 退出前结算统计并落盘:托盘隐藏路径不算退出(进程仍在,定时器继续)
        self._stats_settle_current()
        if self._stats_timer is not None:
            self._stats_timer.stop()
        # 音量补存:拖完滑块 400ms 内就退出时,防抖定时器可能还没触发
        if self._volume_save_timer.isActive():
            self._flush_volume_setting()
        self.login_page.stop()
        i18n.remove_listener(self._on_language_changed)
        if self._queue_window is not None:
            self._queue_window.close()
        if self._media_keys is not None:
            try:
                self._media_keys.stop()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass
        if self._route_guard is not None:
            try:
                self._route_guard.stop()
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
