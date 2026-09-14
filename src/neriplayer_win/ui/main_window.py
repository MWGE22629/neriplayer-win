from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..api.netease import (
    NeteaseAccount,
    NeteaseAuthRequiredError,
    NeteaseClient,
    NeteasePlaylist,
    NeteaseSong,
)
from ..data.store import LocalStore
from ..player.engine import PlayerEngine, PlayerEngineError
from .login_page import LoginPage
from .player_bar import PlayerBar, format_seconds
from .workers import run_async

_PAGE_LOGIN = 0
_PAGE_TABLE = 1
_PAGE_SETTINGS = 2


class MainWindow(QMainWindow):
    """主窗口:左侧导航 + 中部(登录页/歌曲列表/设置占位)+ 底部播放条。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NeriPlayer Win")
        self.resize(1080, 680)

        self._store = LocalStore()
        self._client = NeteaseClient()
        self._account: NeteaseAccount | None = None
        self._playlists: list[NeteasePlaylist] = []
        self._songs: list[NeteaseSong] = []
        self._current_index = -1
        self._resolving = False

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
        self.song_table.setHorizontalHeaderLabels(["#", "标题", "歌手", "时长"])
        self.song_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.song_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.song_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.song_table.verticalHeader().setVisible(False)
        self.song_table.setAlternatingRowColors(True)
        self.song_table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        settings_page = QLabel("设置(M2+ 再补)")
        settings_page.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.central_stack = QStackedWidget()
        self.central_stack.addWidget(self.login_page)  # 0
        self.central_stack.addWidget(self.song_table)  # 1
        self.central_stack.addWidget(settings_page)  # 2

        # -- 侧栏 -------------------------------------------------------------
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(220)
        self.sidebar.currentRowChanged.connect(self._on_sidebar_row_changed)

        # -- 底部播放条 -------------------------------------------------------
        self.player_bar = PlayerBar()
        self.player_bar.set_active(False)
        if self.engine is not None:
            self._wire_engine()
        self._wire_player_bar()

        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sidebar)
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.central_stack, stretch=1)
        root.addWidget(self.player_bar)
        layout.addLayout(root, stretch=1)
        self.setCentralWidget(body)

        self.statusBar().showMessage("就绪")

        self._rebuild_sidebar()
        self.central_stack.setCurrentIndex(_PAGE_LOGIN)
        self.login_page.start()
        self._boot_from_store()

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
            self._handle_stale_login("登录已过期,请重新扫码")
            return
        self._enter_logged_in(account)

    # -- 登录流程 ------------------------------------------------------------

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
            self._handle_stale_login("登录态无效,请重新扫码")
            return
        self._enter_logged_in(account)

    def _enter_logged_in(self, account: NeteaseAccount) -> None:
        self._account = account
        self.statusBar().showMessage(f"已登录:{account.nickname or account.user_id}")
        self._rebuild_sidebar()
        self.central_stack.setCurrentIndex(_PAGE_TABLE)
        self.song_table.setRowCount(0)
        self._load_playlists()

    def _handle_stale_login(self, message: str) -> None:
        self._store.clear_netease()
        self._client.logout()
        self._account = None
        self._playlists = []
        self._songs = []
        self._current_index = -1
        self._rebuild_sidebar()
        self.central_stack.setCurrentIndex(_PAGE_LOGIN)
        self.login_page.start()
        self.statusBar().showMessage(message)

    # -- 歌单 ----------------------------------------------------------------

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

    def _rebuild_sidebar(self) -> None:
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        if self._account is None:
            QListWidgetItem("扫码登录", self.sidebar)
        else:
            for playlist in self._playlists:
                title = playlist.name
                if playlist.track_count:
                    title = f"{title}({playlist.track_count})"
                item = QListWidgetItem(title, self.sidebar)
                item.setData(Qt.ItemDataRole.UserRole, playlist.id)
            if not self._playlists:
                QListWidgetItem("网易云 · 歌单加载中…", self.sidebar)
        QListWidgetItem("设置", self.sidebar)
        self.sidebar.blockSignals(False)
        self.sidebar.setCurrentRow(0)

    def _on_sidebar_row_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.sidebar.item(row)
        if item is None:
            return
        if item.text() == "设置":
            self.central_stack.setCurrentIndex(_PAGE_SETTINGS)
            return
        playlist_id = item.data(Qt.ItemDataRole.UserRole)
        if playlist_id is None:
            self.central_stack.setCurrentIndex(
                _PAGE_LOGIN if self._account is None else _PAGE_TABLE
            )
            return
        self.central_stack.setCurrentIndex(_PAGE_TABLE)
        self._load_playlist_tracks(int(playlist_id), item.text())

    def _load_playlist_tracks(self, playlist_id: int, title: str) -> None:
        self.song_table.setRowCount(0)
        self.statusBar().showMessage(f"正在加载:{title}")

        def fetch() -> object:
            return self._client.get_playlist_tracks(playlist_id)

        def on_done(songs) -> None:
            if not isinstance(songs, list):
                return
            self._songs = list(songs)
            self._fill_song_table()
            self.statusBar().showMessage(f"{title} · 共 {len(songs)} 首")

        def on_error(message: str) -> None:
            self.statusBar().showMessage(f"加载歌曲失败:{message}")

        run_async(fetch, on_done=on_done, on_error=on_error)

    def _fill_song_table(self) -> None:
        self.song_table.setRowCount(len(self._songs))
        for row, song in enumerate(self._songs):
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

    # -- 播放 ----------------------------------------------------------------

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        self._play_index(row)

    def _play_index(self, index: int) -> None:
        if not (0 <= index < len(self._songs)):
            return
        if self.engine is None:
            self.statusBar().showMessage(self._engine_error or "播放内核不可用")
            return
        if self._resolving:
            return
        song = self._songs[index]
        self._current_index = index
        self._resolving = True
        self.player_bar.set_track(
            f"{song.title} - {song.artist}" if song.artist else song.title
        )
        self.statusBar().showMessage(f"正在解析播放地址:{song.title}")

        def resolve() -> tuple[str, object]:
            try:
                playable = self._client.resolve_playable_url(song.id, song.duration_ms)
            except NeteaseAuthRequiredError:
                return ("auth", "")
            except Exception as error:  # noqa: BLE001 - 边界处统一转消息
                return ("error", str(error) or error.__class__.__name__)
            return ("ok", playable)

        def on_done(result) -> None:
            self._resolving = False
            kind, payload = result
            if kind == "auth":
                self._handle_stale_login("登录态已失效,请重新扫码")
                return
            if kind == "error":
                self.statusBar().showMessage(f"播放失败:{payload}")
                QMessageBox.information(
                    self, "播放失败", str(payload), QMessageBox.StandardButton.Ok
                )
                return
            if payload.is_preview:
                self.statusBar().showMessage("当前为试听片段(完整播放需开通 VIP)")
            else:
                self.statusBar().showMessage(f"正在播放:{song.title}")
            self.player_bar.set_active(True)
            self.engine.play_url(payload.url)

        def on_error(message: str) -> None:
            self._resolving = False
            self.statusBar().showMessage(f"播放失败:{message}")

        run_async(resolve, on_done=on_done, on_error=on_error)

    def _play_next(self) -> None:
        if 0 <= self._current_index < len(self._songs) - 1:
            self._play_index(self._current_index + 1)
        else:
            if self.engine is not None:
                self.engine.stop()
            self.player_bar.set_playing(False)
            self.statusBar().showMessage("播放完毕")

    def _play_prev(self) -> None:
        if self._current_index > 0:
            self._play_index(self._current_index - 1)

    # -- 信号接线 ------------------------------------------------------------

    def _wire_engine(self) -> None:
        self.engine.progress.connect(self.player_bar.set_progress)
        self.engine.playing_changed.connect(self.player_bar.set_playing)
        self.engine.track_ended.connect(self._play_next)
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

    def _toggle_pause(self) -> None:
        if self.engine is not None:
            self.engine.toggle_pause()

    def _on_seek_requested(self, position_s: float) -> None:
        if self.engine is not None:
            self.engine.seek(position_s)

    def _on_volume_changed(self, volume: int) -> None:
        if self.engine is not None:
            self.engine.set_volume(volume)

    # -- 退出清理 ------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.login_page.stop()
        if self.engine is not None:
            # 只停播,不 terminate:libmpv 销毁在 Windows 上与事件线程存在
            # 平台级竞争(随机崩溃),交给进程退出回收,音频已停无副作用
            try:
                self.engine.stop()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass
        self._client.close()
        super().closeEvent(event)
