from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QSplitter,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..api.bili import (
    BiliAccount,
    BiliApiError,
    BiliAuthRequiredError,
    BiliClient,
    BiliFavFolder,
    build_bili_stream_headers,
)
from ..api.netease import (
    NeteaseAccount,
    NeteaseAuthRequiredError,
    NeteaseClient,
    NeteasePlaylist,
    NeteaseSong,
)
from ..data.store import SETTING_APPEARANCE, LocalStore
from ..player.engine import PlayerEngine, PlayerEngineError
from ..player.queue import BackupUrlRotator, PlayMode, PlayQueue, QueueSong
from . import theme
from .browser_login import BILI_WEB_LOGIN, BrowserLoginDialog
from .icons import app_icon, tinted_icon, tinted_icon_with_color, tray_icon
from .login_page import LoginPage
from .media_keys import MediaKeyHandler
from .player_bar import PlayerBar, format_seconds
from .queue_window import QueueWindow
from .settings_page import SettingsPage
from .theme import ThemeManager
from .tray import TrayController
from .workers import run_async

_PAGE_LOGIN = 0
_PAGE_TABLE = 1
_PAGE_SETTINGS = 2

_HEADER_NETEASE = ["#", "标题", "歌手", "时长"]
_HEADER_BILI = ["#", "标题", "UP主", "时长"]

_MODE_CYCLE = (PlayMode.SEQUENCE, PlayMode.SHUFFLE, PlayMode.REPEAT_ONE)


def _netease_song_to_queue(song: NeteaseSong) -> QueueSong:
    return QueueSong(
        source="netease", id=song.id, bvid="", title=song.title,
        artist=song.artist, duration_ms=song.duration_ms,
    )


def _bili_item_to_queue(avid: int, bvid: str, title: str, upper: str, duration_sec: int) -> QueueSong:
    return QueueSong(
        source="bili", id=avid, bvid=bvid, title=title,
        artist=upper, duration_ms=duration_sec * 1000,
    )


@dataclass
class _ActivePlay:
    """正在播放(或候选轮换中)的一次播放上下文。"""

    song: QueueSong
    rotator: BackupUrlRotator
    headers: dict[str, str] | None


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


class MainWindow(QMainWindow):
    """主窗口:左侧导航(网易云歌单 + B站收藏夹)+ 歌曲列表 + 播放条。

    播放队列 self._queue 持有统一条目(来源标记 netease/bili),双击列表、
    队列窗口切跳与自动接播都经 _play_at 按条目来源分发解析,两平台共用
    播放条。播放模式(顺序/随机/单曲循环)由 PlayQueue 管理;B站播放地址
    加载失败时按 backupUrls 候选轮换重试(_on_load_failed)。
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NeriPlayer Win")
        self.resize(1080, 680)

        self._store = LocalStore()
        self._settings = self._store.load_settings()
        self._client = NeteaseClient()
        self._account: NeteaseAccount | None = None
        self._playlists: list[NeteasePlaylist] = []

        self._bili_client = BiliClient()
        self._bili_account: BiliAccount | None = None
        self._bili_folders: list[BiliFavFolder] = []

        self._queue = PlayQueue(mode=PlayMode(self._settings.get("play_mode", "sequence")))
        self._resolving = False
        self._active: _ActivePlay | None = None
        self._table_header = _HEADER_NETEASE

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

        self.song_table = QTableWidget(0, 4)
        self.song_table.setHorizontalHeaderLabels(_HEADER_NETEASE)
        self.song_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.song_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.song_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.song_table.verticalHeader().setVisible(False)
        self.song_table.setAlternatingRowColors(True)
        self.song_table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        # 空态视图与歌曲表同页切换:表空显示居中提示,有内容显示表
        self._empty_state = _EmptyStateView()
        self.table_stack = QStackedWidget()
        self.table_stack.addWidget(self.song_table)  # 0
        self.table_stack.addWidget(self._empty_state)  # 1

        self.settings_page = SettingsPage()
        self._wire_settings_page()

        self.central_stack = QStackedWidget()
        self.central_stack.addWidget(self.login_page)  # 0
        self.central_stack.addWidget(self.table_stack)  # 1
        self.central_stack.addWidget(self.settings_page)  # 2

        # -- 侧栏 -------------------------------------------------------------
        self.sidebar = QListWidget()
        # 宽度可拖动(QSplitter),硬边界兜底;运行期按窗口比例钳制见 _clamp
        self.sidebar.setMinimumWidth(140)
        self.sidebar.setMaximumWidth(480)
        self.sidebar.setIconSize(QSize(18, 18))
        self.sidebar.currentRowChanged.connect(self._on_sidebar_row_changed)

        # -- 底部播放条 -------------------------------------------------------
        self.player_bar = PlayerBar()
        self.player_bar.setObjectName("playerBar")
        self.player_bar.set_active(False)
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

        self.statusBar().showMessage("就绪")

        # -- 托盘 / 媒体键 / 队列窗口 ------------------------------------------
        self.setWindowIcon(app_icon())
        self._setup_tray(tray_icon(self.themes.name))
        self._setup_media_keys()

        self._rebuild_sidebar()
        self._update_table_empty_state()
        self.central_stack.setCurrentIndex(_PAGE_LOGIN)
        self.login_page.start()
        self._boot_from_store()
        self._boot_bili_from_store()

        if self.engine is None:
            self.statusBar().showMessage(self._engine_error, 10000)

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
                f"检查登录态失败:{message}"
            ),
        )

    def _on_boot_status(self, account) -> None:
        if account is None:
            self._handle_stale_login("登录已过期,请重新登录")
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
                f"检查B站登录态失败:{message}"
            ),
        )

    def _on_bili_boot_status(self, account) -> None:
        if account is None:
            self._handle_bili_stale_login("B站登录已过期,请点击侧栏「B站 · 收藏夹」重新登录")
            return
        self._enter_bili_logged_in(account)

    # -- 网易云登录流程 -------------------------------------------------------

    def _on_login_succeeded(self, cookies: dict) -> None:
        self._store.save_netease(cookies)
        self._client.set_persisted_cookies(cookies)
        self.statusBar().showMessage("登录成功,正在获取账号信息…")
        run_async(
            self._client.get_login_status,
            on_done=self._on_login_account,
            on_error=lambda message: self.statusBar().showMessage(
                f"获取账号信息失败:{message}"
            ),
        )

    def _on_login_account(self, account) -> None:
        if account is None:
            self._handle_stale_login("登录态无效,请重新登录")
            return
        self._enter_logged_in(account)

    def _enter_logged_in(self, account: NeteaseAccount) -> None:
        self._account = account
        self.statusBar().showMessage(f"已登录:{account.nickname or account.user_id}")
        self._rebuild_sidebar()
        self.central_stack.setCurrentIndex(_PAGE_TABLE)
        self.song_table.setRowCount(0)
        self._update_table_empty_state()
        self._queue.replace([])
        self._load_playlists()

    def _handle_stale_login(self, message: str) -> None:
        self._store.clear_netease()
        self._client.logout()
        self._account = None
        self._playlists = []
        self._queue.replace([])
        self._rebuild_sidebar()
        self.central_stack.setCurrentIndex(_PAGE_LOGIN)
        self.login_page.start()
        self.statusBar().showMessage(message)

    # -- B站登录流程 ----------------------------------------------------------

    def _open_bili_login_dialog(self) -> None:
        dialog = BrowserLoginDialog(BILI_WEB_LOGIN, self)
        dialog.login_cookie_ready.connect(self._on_bili_cookies)
        dialog.exec()

    def _on_bili_cookies(self, cookies: dict) -> None:
        if not cookies.get("SESSDATA"):
            self.statusBar().showMessage("未检测到B站登录凭据,请重试")
            return
        self._store.save_bili(cookies)
        self._bili_client.set_cookies(cookies)
        self.statusBar().showMessage("B站登录成功,正在获取账号信息…")
        run_async(
            self._bili_client.get_login_status,
            on_done=self._on_bili_account_loaded,
            on_error=lambda message: self.statusBar().showMessage(
                f"获取B站账号信息失败:{message}"
            ),
        )

    def _on_bili_account_loaded(self, account) -> None:
        if account is None:
            self._handle_bili_stale_login("B站登录态无效,请重新登录")
            return
        self._enter_bili_logged_in(account)

    def _enter_bili_logged_in(self, account: BiliAccount) -> None:
        self._bili_account = account
        # 补写 profile(mid/uname),下次启动无需等 nav 就知道账号
        cookies = self._bili_client.cookies_snapshot()
        if cookies:
            self._store.save_bili(cookies, profile={"mid": account.mid, "uname": account.uname})
        self.statusBar().showMessage(
            f"B站已登录:{account.uname or account.mid},正在读取收藏夹…"
        )
        self._rebuild_sidebar()
        self._load_bili_folders()

    def _handle_bili_stale_login(self, message: str) -> None:
        self._store.clear_bili()
        self._bili_client.logout()
        self._bili_account = None
        self._bili_folders = []
        self._rebuild_sidebar()
        self.statusBar().showMessage(message)

    def _handle_bili_section_click(self) -> None:
        """侧栏「B站 · 未登录」被点击:有残留 cookie 先验证,否则弹网页登录。"""
        if self._bili_client.has_login():
            self.statusBar().showMessage("正在检查B站登录态…")
            run_async(
                self._bili_client.get_login_status,
                on_done=self._on_bili_section_status,
                on_error=lambda message: self.statusBar().showMessage(
                    f"检查B站登录态失败:{message}"
                ),
            )
        else:
            self._open_bili_login_dialog()

    def _on_bili_section_status(self, account) -> None:
        if account is not None:
            self._enter_bili_logged_in(account)
            return
        self._handle_bili_stale_login("B站登录已过期,请重新登录")
        self._open_bili_login_dialog()

    # -- 网易云歌单 -----------------------------------------------------------

    def _load_playlists(self) -> None:
        account = self._account
        if account is None:
            return

        def fetch() -> object:
            return self._client.get_user_playlists(account.user_id)

        run_async(
            fetch,
            on_done=self._on_playlists_loaded,
            on_error=lambda message: self.statusBar().showMessage(
                f"获取歌单失败:{message}"
            ),
        )

    def _on_playlists_loaded(self, playlists) -> None:
        if not isinstance(playlists, list):
            return
        self._playlists = list(playlists)
        self._rebuild_sidebar()

    def _load_playlist_tracks(self, playlist_id: int, title: str) -> None:
        self.song_table.setRowCount(0)
        self._update_table_empty_state()
        self.statusBar().showMessage(f"正在加载:{title}")

        def fetch() -> object:
            return self._client.get_playlist_tracks(playlist_id)

        def on_done(songs) -> None:
            if not isinstance(songs, list):
                return
            self._table_header = _HEADER_NETEASE
            # 切换列表即重置队列(保留既有行为):当前曲/历史清空
            self._queue.replace([_netease_song_to_queue(s) for s in songs])
            self._fill_song_table()
            self.statusBar().showMessage(f"{title} · 共 {len(songs)} 首")

        def on_error(message: str) -> None:
            self.statusBar().showMessage(f"加载歌曲失败:{message}")

        run_async(fetch, on_done=on_done, on_error=on_error)

    # -- B站收藏夹 ------------------------------------------------------------

    def _load_bili_folders(self) -> None:
        account = self._bili_account
        if account is None:
            return

        def fetch() -> object:
            return self._bili_client.get_user_created_fav_folders(account.mid)

        run_async(
            fetch,
            on_done=self._on_bili_folders_loaded,
            on_error=lambda message: self.statusBar().showMessage(
                f"获取B站收藏夹失败:{message}"
            ),
        )

    def _on_bili_folders_loaded(self, folders) -> None:
        if not isinstance(folders, list):
            return
        self._bili_folders = list(folders)
        self._rebuild_sidebar()
        self.statusBar().showMessage(f"B站收藏夹 · 共 {len(folders)} 个")

    def _load_bili_folder_tracks(self, media_id: int, title: str) -> None:
        self.song_table.setRowCount(0)
        self._update_table_empty_state()
        self.statusBar().showMessage(f"正在加载:{title}")

        def fetch() -> object:
            return self._bili_client.get_all_fav_folder_items(media_id)

        def on_done(items) -> None:
            if not isinstance(items, list):
                return
            playable = [item for item in items if item.playable]
            self._table_header = _HEADER_BILI
            # 切换收藏夹即重置队列(保留既有行为)
            self._queue.replace(
                [
                    _bili_item_to_queue(
                        item.id, item.bvid or "", item.title or "", item.upper_name,
                        item.duration_sec,
                    )
                    for item in playable
                ]
            )
            self._fill_song_table()
            skipped = len(items) - len(playable)
            suffix = f"(跳过 {skipped} 条不可播内容)" if skipped else ""
            self.statusBar().showMessage(f"{title} · 共 {len(playable)} 首{suffix}")

        def on_error(message: str) -> None:
            self.statusBar().showMessage(f"加载收藏夹失败:{message}")

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

    def _add_header_item(self, text: str, icon=None, kind: str | None = None) -> None:
        item = QListWidgetItem(text, self.sidebar)
        item.setFlags(Qt.ItemFlag.NoItemFlags)  # 不可选中的分区标题
        if kind is not None:
            item.setData(Qt.ItemDataRole.UserRole, (kind, None))
        if icon is not None:
            item.setIcon(icon)

    def _rebuild_sidebar(self) -> None:
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        self._add_header_item(
            "网易云 · 歌单", self._brand_header_icon("netease"), "netease-header"
        )
        if self._account is None:
            item = QListWidgetItem("扫码登录", self.sidebar)
            item.setData(Qt.ItemDataRole.UserRole, ("netease-login", None))
        else:
            for playlist in self._playlists:
                title = playlist.name
                if playlist.track_count:
                    title = f"{title}({playlist.track_count})"
                item = QListWidgetItem(title, self.sidebar)
                item.setData(
                    Qt.ItemDataRole.UserRole, ("netease-playlist", playlist.id)
                )
            if not self._playlists:
                self._add_header_item(
                    "网易云 · 歌单加载中…", None, "netease-header"
                )

        self._add_header_item(
            "B站 · 收藏夹", self._brand_header_icon("bilibili"), "bili-header"
        )
        if self._bili_account is None:
            item = QListWidgetItem("未登录,点击登录", self.sidebar)
            item.setData(Qt.ItemDataRole.UserRole, ("bili-login", None))
        else:
            for folder in self._bili_folders:
                title = folder.title
                if folder.count:
                    title = f"{title}({folder.count})"
                item = QListWidgetItem(title, self.sidebar)
                item.setData(Qt.ItemDataRole.UserRole, ("bili-folder", folder.media_id))
            if not self._bili_folders:
                self._add_header_item(
                    "B站 · 收藏夹加载中…", None, "bili-header"
                )

        item = QListWidgetItem("设置", self.sidebar)
        item.setData(Qt.ItemDataRole.UserRole, ("settings", None))
        self.sidebar.blockSignals(False)
        self._apply_sidebar_icons()

    _SIDEBAR_KIND_ICON = {
        "settings": "settings",
        "netease-login": "search",
        "netease-playlist": "library_music",
        "bili-login": "person",
        "bili-folder": "queue_music",
    }

    _HEADER_BRAND = {"netease-header": "netease", "bili-header": "bilibili"}

    def _apply_sidebar_icons(self) -> None:
        """按条目类型补单色图标;分区头品牌标按登录态取色(随主题/登录刷新)。"""
        for row in range(self.sidebar.count()):
            item = self.sidebar.item(row)
            if item is None:
                continue
            role = item.data(Qt.ItemDataRole.UserRole)
            kind = role[0] if isinstance(role, tuple) else None
            brand_source = self._HEADER_BRAND.get(kind)
            if brand_source is not None:
                item.setIcon(self._brand_header_icon(brand_source))
                continue
            icon_name = self._SIDEBAR_KIND_ICON.get(kind)
            if icon_name:
                item.setIcon(tinted_icon(icon_name))

    def _on_sidebar_row_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.sidebar.item(row)
        if item is None:
            return
        role = item.data(Qt.ItemDataRole.UserRole)
        kind, payload = role if isinstance(role, tuple) else ("", None)
        if kind == "settings":
            self.central_stack.setCurrentIndex(_PAGE_SETTINGS)
            return
        if kind == "netease-login":
            self.central_stack.setCurrentIndex(_PAGE_LOGIN)
            return
        if kind == "netease-playlist":
            self.central_stack.setCurrentIndex(_PAGE_TABLE)
            self._load_playlist_tracks(int(payload), item.text())
            return
        if kind == "bili-folder":
            self.central_stack.setCurrentIndex(_PAGE_TABLE)
            self._load_bili_folder_tracks(int(payload), item.text())
            return
        if kind == "bili-login":
            self.central_stack.setCurrentIndex(_PAGE_TABLE)
            self.song_table.setRowCount(0)
            self._update_table_empty_state()
            self._handle_bili_section_click()
            return
        self.central_stack.setCurrentIndex(
            _PAGE_LOGIN if self._account is None else _PAGE_TABLE
        )

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
                self._empty_state.set_hint("登录网易云或B站后,这里会展示你的歌单与收藏夹")
            else:
                self._empty_state.set_hint("从左侧选择歌单或收藏夹,双击即可播放")

    def _fill_song_table(self) -> None:
        songs = self._queue.items()
        self.song_table.setHorizontalHeaderLabels(self._table_header)
        self.song_table.setRowCount(len(songs))
        for row, song in enumerate(songs):
            number = QTableWidgetItem(str(row + 1))
            number.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.song_table.setItem(row, 0, number)
            self.song_table.setItem(row, 1, QTableWidgetItem(song.title))
            self.song_table.setItem(row, 2, QTableWidgetItem(song.artist))
            duration = (
                format_seconds(song.duration_ms / 1000) if song.duration_ms else "--:--"
            )
            self.song_table.setItem(row, 3, QTableWidgetItem(duration))
        self._update_table_empty_state()

    # -- 播放 ----------------------------------------------------------------

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        self._play_at(row)

    def _play_at(self, index: int) -> None:
        song = self._queue.item_at(index)
        if song is None:
            return
        if self.engine is None:
            self.statusBar().showMessage(self._engine_error or "播放内核不可用")
            return
        if self._resolving:
            return
        self._queue.jump(index)
        self._resolving = True
        self.player_bar.set_track(
            f"{song.title} - {song.artist}" if song.artist else song.title
        )
        self.statusBar().showMessage(f"正在解析播放地址:{song.title}")

        if song.source == "bili":
            on_done = self._make_bili_play_done(song)
        else:
            on_done = self._make_netease_play_done(song)

        def resolve() -> tuple[str, object]:
            if song.source == "bili":
                try:
                    stream = self._bili_client.resolve_audio_stream(song.bvid)
                except BiliAuthRequiredError:
                    return ("auth", "")
                except BiliApiError as error:
                    return ("error", str(error) or error.__class__.__name__)
                except Exception as error:  # noqa: BLE001 - 边界处统一转消息
                    return ("error", str(error) or error.__class__.__name__)
                return ("ok", stream)
            try:
                playable = self._client.resolve_playable_url(
                    song.id, song.duration_ms
                )
            except NeteaseAuthRequiredError:
                return ("auth", "")
            except Exception as error:  # noqa: BLE001 - 边界处统一转消息
                return ("error", str(error) or error.__class__.__name__)
            return ("ok", playable)

        def on_error(message: str) -> None:
            self._resolving = False
            self.statusBar().showMessage(f"播放失败:{message}")

        run_async(resolve, on_done=on_done, on_error=on_error)

    def _make_netease_play_done(self, song: QueueSong):
        def on_done(result) -> None:
            self._resolving = False
            kind, payload = result
            if kind == "auth":
                self._handle_stale_login("登录态已失效,请重新登录")
                return
            if kind == "error":
                self.statusBar().showMessage(f"播放失败:{payload}")
                QMessageBox.information(
                    self, "播放失败", str(payload), QMessageBox.StandardButton.Ok
                )
                return
            status = (
                "当前为试听片段(完整播放需开通 VIP)"
                if payload.is_preview
                else f"正在播放:{song.title}"
            )
            # 网易云不参与 backupUrls 轮换:音质回退链已在解析层完成
            self._start_play(
                song, BackupUrlRotator([payload.url]), None, status=status
            )

        return on_done

    def _make_bili_play_done(self, song: QueueSong):
        def on_done(result) -> None:
            self._resolving = False
            kind, payload = result
            if kind == "auth":
                self._handle_bili_stale_login(
                    "B站登录态已失效,请点击侧栏「B站 · 收藏夹」重新登录"
                )
                return
            if kind == "error":
                self.statusBar().showMessage(f"播放失败:{payload}")
                return
            # B站 m4s 音频流要求 Referer + 浏览器 UA,否则 403;
            # candidate_urls 含主 URL 与 backupUrls,加载失败时轮换
            self._start_play(
                song,
                BackupUrlRotator(payload.candidate_urls),
                build_bili_stream_headers(),
            )

        return on_done

    def _start_play(
        self,
        song: QueueSong,
        rotator: BackupUrlRotator,
        headers: dict[str, str] | None,
        status: str | None = None,
    ) -> None:
        url = rotator.current
        if not url:
            self.statusBar().showMessage(f"播放失败:{song.title}(无可用播放地址)")
            return
        self._active = _ActivePlay(song=song, rotator=rotator, headers=headers)
        self.player_bar.set_active(True)
        self.statusBar().showMessage(status or f"正在播放:{song.title}")
        self.engine.play_url(url, headers)

    def _on_load_failed(self, message: str) -> None:
        """engine 加载失败类错误:B站按 backupUrls 候选轮换重试,耗尽才报失败。"""
        active = self._active
        if active is None:
            self.statusBar().showMessage(f"播放失败:{message}")
            return
        if active.rotator.has_next():
            next_url = active.rotator.advance()
            self.statusBar().showMessage(
                f"播放失败,切换备用线路重试"
                f"({active.rotator.position}/{len(active.rotator)}):{active.song.title}"
            )
            self.engine.play_url(next_url, active.headers)
            return
        self.statusBar().showMessage(f"播放失败:{message}")

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
        if self._queue.mode() is PlayMode.REPEAT_ONE and self._active is not None:
            url = self._active.rotator.current
            if url:
                self.statusBar().showMessage(f"单曲循环:{self._active.song.title}")
                self.engine.play_url(url, self._active.headers)
                return
        index = self._queue.advance_ended()
        if index is None:
            self.player_bar.set_playing(False)
            self.statusBar().showMessage("播放完毕")
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
        self._settings["play_mode"] = mode.value
        self._store.save_settings(self._settings)
        self.settings_page.set_play_mode(mode.value)
        self.statusBar().showMessage(f"播放模式:{mode.display_name}")

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

    def _on_settings_appearance(self, value: str) -> None:
        """设置页外观选择:持久化并即时切换主题。"""
        if value not in theme.VALID_THEMES:
            return
        self._settings[SETTING_APPEARANCE] = value
        self._store.save_settings(self._settings)
        self.themes.apply(value)

    def _on_theme_changed(self, name: str) -> None:
        """主题切换的全量资源刷新(ThemeManager 已换全局 QSS)。"""
        self.player_bar.retheme()
        self._empty_state.retheme()
        self._apply_sidebar_icons()
        if self._tray is not None:
            self._tray.set_icon(tray_icon(name))

    def _wire_settings_page(self) -> None:
        page = self.settings_page
        page.close_action_changed.connect(self._on_settings_close_action)
        page.play_mode_changed.connect(self._on_settings_play_mode)
        page.appearance_changed.connect(self._on_settings_appearance)
        page.set_close_action(self._settings.get("close_action", "tray"))
        page.set_play_mode(self._settings.get("play_mode", "sequence"))
        page.set_appearance(self._settings.get(SETTING_APPEARANCE, "dark"))

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

    # -- 信号接线 ------------------------------------------------------------

    def _wire_engine(self) -> None:
        self.engine.progress.connect(self.player_bar.set_progress)
        self.engine.playing_changed.connect(self.player_bar.set_playing)
        self.engine.track_ended.connect(self._on_track_ended)
        self.engine.load_failed.connect(self._on_load_failed)
        self.engine.error.connect(
            lambda message: self.statusBar().showMessage(f"播放错误:{message}")
        )

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

    def _on_media_stop(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        self.player_bar.set_playing(False)
        self.statusBar().showMessage("已停止")

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
                    "已最小化到托盘:双击托盘图标恢复窗口,右键菜单可退出。",
                )
                self._tray_hint_shown = True
            return
        self.login_page.stop()
        if self._queue_window is not None:
            self._queue_window.close()
        if self._media_keys is not None:
            try:
                self._media_keys.stop()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass
        if self.engine is not None:
            # 只停播,不 terminate:libmpv 销毁在 Windows 上与事件线程存在
            # 平台级竞争(随机崩溃),交给进程退出回收,音频已停无副作用
            try:
                self.engine.stop()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass
        self._client.close()
        self._bili_client.close()
        super().closeEvent(event)
