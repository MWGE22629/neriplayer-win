"""M7 本地歌单 UI:侧栏分区 / 新建 / 点开 / 右键添加 / 封面。

conftest 只提供 session 级 qapp(无 pytest-qt),点击与菜单用 QTest 与
猴补;helper 形态与 tests/test_main_window_layout.py 保持一致(各测试
文件各自定义,不共享)。
"""

from __future__ import annotations

import json

from PySide6.QtCore import QPoint, QPointF, QMimeData, Qt
from PySide6.QtGui import QAction, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu

from neriplayer_win.data.store import (
    LocalPlaylist,
    LocalPlaylistItem,
    LocalStore,
)
from neriplayer_win.player.queue import QueueSong
from neriplayer_win.ui import main_window as main_window_module
from neriplayer_win.ui.main_window import MainWindow, _ListRef, _SidebarTree

_ROLE = Qt.ItemDataRole.UserRole


def _make_window(qapp, monkeypatch, tmp_path) -> MainWindow:
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window


def _close(window) -> None:
    # 与 test_main_window_layout 一致:强制真退出,避免托盘分支跳过清理
    window._force_exit = True
    window.close()


def _capture_async(monkeypatch) -> list[tuple]:
    """猴补 run_async:只记录调用,不启线程不触网。"""
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        main_window_module,
        "run_async",
        lambda fn, on_done=None, on_error=None: scheduled.append(
            (fn, on_done, on_error)
        ),
    )
    return scheduled


def _song(n: int = 1, source: str = "netease", bvid: str = "") -> QueueSong:
    return QueueSong(
        source=source, id=n, bvid=bvid, title=f"歌{n}", artist="歌手",
        duration_ms=60000, cover_url=f"http://cover/{n}.jpg",
    )


def _item(n: int = 1, source: str = "netease", cover_url: str = "") -> LocalPlaylistItem:
    return LocalPlaylistItem(
        source=source, id=n, extra={}, title=f"歌{n}", artist="歌手",
        duration_ms=60000, cover_url=cover_url or f"http://cover/{n}.jpg",
    )


def _write_playlists(tmp_path, playlists: list[LocalPlaylist]) -> None:
    store = LocalStore(tmp_path)
    assert store.save_local_playlists(playlists) is True


def _local_header(window):
    return window.sidebar.find_header("local")


def _header_child_roles(window) -> list[tuple]:
    header = _local_header(window)
    assert header is not None
    return [
        header.child(i).data(0, _ROLE) for i in range(header.childCount())
    ]


# ---------------------------------------------------------------------------
# 侧栏分区:存在 / 折叠持久化 / 拖拽排序 / 图标
# ---------------------------------------------------------------------------

class TestLocalSection:
    def test_section_present_even_when_empty(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            header = _local_header(window)
            assert header is not None
            assert header.text(0) == "本地歌单"
            assert not header.icon(0).isNull()
            # 分区头启用且可选中/可拖(与其余可排序分区一致)
            assert header.flags() & Qt.ItemFlag.ItemIsEnabled
            assert header.flags() & Qt.ItemFlag.ItemIsSelectable
            assert header.flags() & Qt.ItemFlag.ItemIsDragEnabled
            # 空分区给一行不可选提示
            assert _header_child_roles(window) == [("local-empty", None)]
            assert not (
                header.child(0).flags() & Qt.ItemFlag.ItemIsSelectable
            )
            assert window.sidebar.section_child_ids("local") == []
        finally:
            _close(window)

    def test_collapse_persists(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            header = _local_header(window)
            assert header.isExpanded()
            QTest.mouseClick(
                window.sidebar.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                window.sidebar.visualItemRect(header).center(),
            )
            qapp.processEvents()
            assert header.isExpanded() is False
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["sidebar_expanded"]["local"] is False
            window._rebuild_sidebar()
            assert _local_header(window).isExpanded() is False
        finally:
            _close(window)

    def test_stored_collapse_applied_on_startup(self, qapp, monkeypatch, tmp_path):
        (tmp_path / "settings.json").write_text(
            json.dumps({"sidebar_expanded": {"local": False}}), encoding="utf-8"
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert _local_header(window).isExpanded() is False
        finally:
            _close(window)

    def test_rendered_from_file_with_stored_order(self, qapp, monkeypatch, tmp_path):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(id=1, name="通勤", items=[_item()]),
                LocalPlaylist(id=2, name="深夜", items=[]),
                LocalPlaylist(id=3, name="跑步", items=[]),
            ],
        )
        (tmp_path / "settings.json").write_text(
            json.dumps({"local_playlist_order": [3, 1]}), encoding="utf-8"
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window.sidebar.section_child_ids("local") == [3, 1, 2]
            header = _local_header(window)
            titles = [header.child(i).text(0) for i in range(header.childCount())]
            assert titles == ["跑步", "通勤(1)", "深夜"]
        finally:
            _close(window)

    def test_drag_reorder_persists_and_syncs_memory(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(id=1, name="A"),
                LocalPlaylist(id=2, name="B"),
                LocalPlaylist(id=3, name="C"),
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            tree = window.sidebar
            header = _local_header(window)
            first = header.child(0)
            last = header.child(2)
            tree.setCurrentItem(first)
            center = tree.visualItemRect(last).center()
            event = QDropEvent(
                QPointF(center),
                Qt.DropAction.MoveAction,
                QMimeData(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            tree.dropEvent(event)
            assert event.isAccepted()
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["local_playlist_order"] == [2, 3, 1]
            window._rebuild_sidebar()
            assert tree.section_child_ids("local") == [2, 3, 1]
            assert [p.id for p in window._local_playlists] == [2, 3, 1]
            # 内存顺序同步后落盘的歌单文件顺序不变(歌单文件只存内容)
            assert [p.id for p in window._store.load_local_playlists()] == [1, 2, 3]
        finally:
            _close(window)

    def test_header_icon_rotates_on_toggle(self, qapp, monkeypatch, tmp_path):
        """分区头不能拿 "local" 当品牌名(会 KeyError,角标冻结)。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            header = _local_header(window)
            expanded_image = header.icon(0).pixmap(30, 18).toImage()
            header.setExpanded(False)
            collapsed_image = header.icon(0).pixmap(30, 18).toImage()
            assert collapsed_image != expanded_image
        finally:
            _close(window)

    def test_local_children_excluded_from_other_sections(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(tmp_path, [LocalPlaylist(id=1, name="A")])
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            # 本地歌单 id 与平台歌单 id 数值相同也不串区
            assert window.sidebar.section_child_ids("netease") == []
            assert window.sidebar.section_child_ids("bili") == []
            assert window.sidebar.section_child_ids("local") == [1]
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# 分区头「+」:命中区 / 新建
# ---------------------------------------------------------------------------

class TestCreatePlaylist:
    def test_plus_hit_creates_playlist(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            rect = window.sidebar.local_header_plus_rect()
            assert rect is not None and rect.isValid()
            monkeypatch.setattr(window, "_ask_local_playlist_name", lambda *a: "通勤")
            QTest.mouseClick(
                window.sidebar.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                rect.center(),
            )
            qapp.processEvents()
            assert _header_child_roles(window) == [("local-playlist", 1)]
            assert window._local_playlists[0].name == "通勤"
            assert window._local_playlists[0].created_at > 0
            # 落盘
            stored = window._store.load_local_playlists()
            assert [(p.id, p.name) for p in stored] == [(1, "通勤")]
            assert "已创建歌单" in window.statusBar().currentMessage()
            # 命中「+」不应触发分区折叠
            assert _local_header(window).isExpanded() is True
        finally:
            _close(window)

    def test_click_outside_plus_toggles_collapse(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            header = _local_header(window)
            rect = window.sidebar.visualItemRect(header)
            QTest.mouseClick(
                window.sidebar.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                QPoint(rect.left() + 20, rect.center().y()),
            )
            qapp.processEvents()
            assert header.isExpanded() is False
            assert window._local_playlists == []
        finally:
            _close(window)

    def test_cancel_creates_nothing(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(window, "_ask_local_playlist_name", lambda *a: None)
            window._on_create_local_playlist_requested()
            assert window._local_playlists == []
            assert _header_child_roles(window) == [("local-empty", None)]
        finally:
            _close(window)

    def test_empty_name_rejected(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window._create_local_playlist("   ") is None
            assert window._local_playlists == []
            assert "不能为空" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_ids_increment_and_survive_restart(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._create_local_playlist("一")
            window._create_local_playlist("二")
            assert [p.id for p in window._local_playlists] == [1, 2]
        finally:
            _close(window)
        window2 = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert [p.id for p in window2._local_playlists] == [1, 2]
            assert window2._create_local_playlist("三").id == 3
        finally:
            _close(window2)

    def test_name_truncated_to_limit(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            playlist = window._create_local_playlist("名" * 300)
            assert len(playlist.name) == 100
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# 点开歌单 / 空态 / 最近联动
# ---------------------------------------------------------------------------

class TestOpenPlaylist:
    def test_click_fills_table_without_network(
        self, qapp, monkeypatch, tmp_path
    ):
        scheduled = _capture_async(monkeypatch)
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="混合",
                    items=[
                        _item(1, "netease"),
                        LocalPlaylistItem(
                            source="bili", id=9, extra={"bvid": "BV1xyz"},
                            title="视频", artist="UP", duration_ms=30000,
                            cover_url="http://cover/b.jpg",
                        ),
                    ],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            item = _local_header(window).child(0)
            window._on_sidebar_item_clicked(item, 0)
            assert window.central_stack.currentIndex() == 1
            assert scheduled == []  # 快照直出,零后台任务
            assert window.song_table.rowCount() == 2
            assert window._table_context == _ListRef("local-playlist", 1, "混合")
            # B站条目必须还原 bvid,否则播放解析拿不到 bvid
            assert window._table_songs[1].bvid == "BV1xyz"
            assert window._table_songs[1].source == "bili"
            assert window._table_songs[0].source == "netease"
            # 表头固定「歌手」版(混收两源)
            assert (
                window.song_table.horizontalHeaderItem(3).text() == "歌手"
            )
            assert "共 2 首" in window.statusBar().currentMessage()
            # 浏览不动队列
            assert window._queue.items() == []
        finally:
            _close(window)

    def test_empty_playlist_shows_dedicated_hint(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(tmp_path, [LocalPlaylist(id=1, name="空歌单")])
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._on_sidebar_item_clicked(_local_header(window).child(0), 0)
            assert window.song_table.rowCount() == 0
            assert window.table_stack.currentIndex() == 1
            assert "添加到" in window._empty_state.hint_label.text()
        finally:
            _close(window)

    def test_click_missing_playlist_shows_status(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._open_list_ref(_ListRef("local-playlist", 99, "不存在"))
            assert "歌单已不存在" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_recent_gets_local_playlist_and_survives_restart(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path, [LocalPlaylist(id=1, name="通勤", items=[_item()])]
        )
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._on_sidebar_item_clicked(_local_header(window).child(0), 0)
            # 双击表格起播(解析被 run_async 拦掉,不真播)
            window._play_from_table(0)
            assert window._queue_context == _ListRef("local-playlist", 1, "通勤")
            window._start_play(
                _song(1),
                main_window_module.BackupUrlRotator(["http://example/a"]),
                None,
            )
            assert window._recent == [_ListRef("local-playlist", 1, "通勤")]
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["recent_lists"] == [
                {"kind": "local-playlist", "id": 1, "title": "通勤"}
            ]
        finally:
            _close(window)
        window2 = _make_window(qapp, monkeypatch, tmp_path)
        try:
            # RECENT_KINDS 白名单必须含 local-playlist,否则重启被过滤
            assert window2._recent == [_ListRef("local-playlist", 1, "通勤")]
            recent_header = window2.sidebar.find_header("recent")
            assert recent_header.child(0).data(0, _ROLE) == (
                "recent-item", ("local-playlist", 1, "通勤"),
            )
        finally:
            _close(window2)


# ---------------------------------------------------------------------------
# 右键「添加到…」
# ---------------------------------------------------------------------------

def _patch_menu_exec(monkeypatch, target_getter) -> None:
    """把菜单弹出猴补成返回指定 action(不弹真菜单)。

    QMenu.exec 是 PySide6 的静态内建方法,无法在类上猴补;MainWindow 的
    两个右键菜单都经模块级 _exec_menu 薄封装调用,这里替换那一层。
    target_getter(menu) 在菜单已构建好之后调用,可从中挑出目标 action。
    """
    def fake_exec(menu, global_pos):
        return target_getter(menu)

    monkeypatch.setattr(main_window_module, "_exec_menu", fake_exec)


def _find_action(menu: QMenu, predicate) -> QAction | None:
    for action in menu.actions():
        if predicate(action):
            return action
        submenu = action.menu()
        if submenu is not None:
            found = _find_action(submenu, predicate)
            if found is not None:
                return found
    return None


def _open_table_with_one_song(window) -> None:
    window._set_table_songs([_song(7)], "netease", _ListRef("search", 0, "搜索:x"))


class TestAddToPlaylist:
    def test_add_to_existing_playlist(self, qapp, monkeypatch, tmp_path):
        _write_playlists(tmp_path, [LocalPlaylist(id=1, name="通勤")])
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_table_with_one_song(window)
            _patch_menu_exec(
                monkeypatch,
                lambda menu: _find_action(
                    menu,
                    lambda a: a.data() == 1 and a.isEnabled(),
                ),
            )
            window._on_table_context_menu(
                window.song_table.visualItemRect(
                    window.song_table.item(0, 2)
                ).center()
            )
            stored = window._store.load_local_playlists()
            assert [i.id for i in stored[0].items] == [7]
            assert stored[0].items[0].title == "歌7"
            assert "已添加到" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_duplicate_add_is_rejected(self, qapp, monkeypatch, tmp_path):
        _write_playlists(
            tmp_path, [LocalPlaylist(id=1, name="通勤", items=[_item(7)])]
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            song = _song(7)
            assert window._add_song_to_local_playlist(1, song) is False
            assert len(window._local_playlists[0].items) == 1
            assert "已有这首歌" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_duplicate_action_disabled_and_tagged(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(id=1, name="有", items=[_item(7)]),
                LocalPlaylist(id=2, name="无"),
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            menu = QMenu()
            actions, new_action = window._menu_local_playlist_actions(
                menu, _song(7)
            )
            assert [a.data() for a in actions] == [1, 2]
            assert actions[0].isEnabled() is False
            assert actions[0].text().endswith("(已在)")
            assert actions[1].isEnabled() is True
            assert actions[1].text() == "无"
            assert new_action is not None
        finally:
            _close(window)

    def test_new_playlist_and_add(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_table_with_one_song(window)
            monkeypatch.setattr(window, "_ask_local_playlist_name", lambda *a: "新单")
            _patch_menu_exec(
                monkeypatch,
                lambda menu: _find_action(
                    menu, lambda a: a.data() is None and "新建" in a.text()
                ),
            )
            window._on_table_context_menu(
                window.song_table.visualItemRect(
                    window.song_table.item(0, 2)
                ).center()
            )
            assert [(p.id, p.name) for p in window._local_playlists] == [
                (1, "新单")
            ]
            assert [i.id for i in window._local_playlists[0].items] == [7]
            assert _header_child_roles(window) == [("local-playlist", 1)]
        finally:
            _close(window)

    def test_play_next_still_works(self, qapp, monkeypatch, tmp_path):
        """新增二级菜单不能破坏既有「下一首播放」。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_table_with_one_song(window)
            window._queue.replace([_song(1), _song(2)])
            _patch_menu_exec(
                monkeypatch,
                lambda menu: _find_action(
                    menu, lambda a: a.text() == "下一首播放"
                ),
            )
            window._on_table_context_menu(
                window.song_table.visualItemRect(
                    window.song_table.item(0, 2)
                ).center()
            )
            assert window._queue_dirty is True
        finally:
            _close(window)

    def test_table_refreshes_when_viewing_that_playlist(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(tmp_path, [LocalPlaylist(id=1, name="通勤")])
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._on_sidebar_item_clicked(_local_header(window).child(0), 0)
            assert window.song_table.rowCount() == 0
            window._add_song_to_local_playlist(1, _song(7))
            assert window.song_table.rowCount() == 1
            assert window.table_stack.currentIndex() == 0
            assert window._table_context == _ListRef("local-playlist", 1, "通勤")
        finally:
            _close(window)

    def test_table_untouched_when_viewing_elsewhere(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(tmp_path, [LocalPlaylist(id=1, name="通勤")])
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_table_with_one_song(window)
            before = window._table_songs
            window._add_song_to_local_playlist(1, _song(8))
            assert window._table_songs is before
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# 封面:自动取首曲 / 预取 / 回调点亮 / 手动优先
# ---------------------------------------------------------------------------

class TestPlaylistCovers:
    def test_auto_cover_uses_first_item(self, qapp, monkeypatch, tmp_path):
        _write_playlists(
            tmp_path,
            [LocalPlaylist(id=1, name="A", items=[_item(1), _item(2)])],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            playlist = window._find_local_playlist(1)
            assert (
                main_window_module.local_playlist_cover_url(playlist)
                == "http://cover/1.jpg"
            )
        finally:
            _close(window)

    def test_preload_called_with_all_cover_urls(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(id=1, name="A", items=[_item(1)]),
                LocalPlaylist(
                    id=2, name="B", cover_url="http://manual/b.jpg",
                    items=[_item(2)],
                ),
                LocalPlaylist(id=3, name="空"),
            ],
        )
        calls: list[list[str]] = []
        original_preload = main_window_module.CoverLoader.preload

        def spy(self, urls):
            calls.append(list(urls))
            return original_preload(self, urls)

        monkeypatch.setattr(main_window_module.CoverLoader, "preload", spy)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            # 启动加载那一次(此前 _rebuild_sidebar 的预取尚未涉及歌单)
            assert calls, "启动时应发起歌单封面预取"
            assert calls[-1] == ["http://cover/1.jpg", "http://manual/b.jpg"]
            assert window._sidebar_cover_urls == calls[-1]
        finally:
            _close(window)

    def test_cover_ready_lights_up_sidebar_item(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [LocalPlaylist(id=1, name="A", items=[_item(1)])],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            item = _local_header(window).child(0)
            fallback = item.icon(0).pixmap(18, 18).toImage()
            data = _png_bytes()
            window._on_sidebar_cover_ready("http://cover/1.jpg", data)
            lit = item.icon(0).pixmap(18, 18).toImage()
            assert lit != fallback
            assert "http://cover/1.jpg" in window._sidebar_cover_icons
            # 重建后封面仍在(图标缓存复用,不重新解码)
            window._rebuild_sidebar()
            rebuilt = _local_header(window).child(0).icon(0).pixmap(18, 18).toImage()
            assert rebuilt == lit
        finally:
            _close(window)

    def test_stale_cover_callback_ignored(self, qapp, monkeypatch, tmp_path):
        _write_playlists(
            tmp_path, [LocalPlaylist(id=1, name="A", items=[_item(1)])]
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._on_sidebar_cover_ready("http://not-in-playlists.jpg", _png_bytes())
            assert window._sidebar_cover_icons == {}
        finally:
            _close(window)

    def test_manual_cover_persists_and_wins(self, qapp, monkeypatch, tmp_path):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="A",
                    items=[_item(1), _item(2)],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._set_local_playlist_cover(1, "http://cover/2.jpg")
            stored = window._store.load_local_playlists()[0]
            assert stored.cover_url == "http://cover/2.jpg"
            # 手动值不因首曲变化而变
            stored.items.insert(0, _item(9))
            assert (
                main_window_module.local_playlist_cover_url(stored)
                == "http://cover/2.jpg"
            )
        finally:
            _close(window)
        window2 = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window2._find_local_playlist(1).cover_url == "http://cover/2.jpg"
        finally:
            _close(window2)

    def test_clear_manual_cover_restores_auto(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="A", cover_url="http://manual/x.jpg",
                    items=[_item(1)],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._set_local_playlist_cover(1, "")
            stored = window._store.load_local_playlists()[0]
            assert stored.cover_url == ""
            assert (
                main_window_module.local_playlist_cover_url(stored)
                == "http://cover/1.jpg"
            )
            assert "恢复自动封面" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_cover_picker_dialog_lists_items_with_covers(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="A",
                    items=[
                        _item(1),
                        _item(2),
                        LocalPlaylistItem(
                            source="netease", id=3, extra={}, title="无封面",
                            artist="", duration_ms=0, cover_url="",
                        ),
                    ],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            playlist = window._find_local_playlist(1)
            dialog = main_window_module._CoverPickerDialog(
                playlist, window._sidebar_cover_loader, window,
            )
            try:
                # 无封面的曲目不出现在选择列表里
                assert dialog.list_widget.count() == 2
                dialog.list_widget.setCurrentRow(1)
                assert dialog.chosen_url() == "http://cover/2.jpg"
                dialog.list_widget.setCurrentRow(-1)
                assert dialog.chosen_url() == ""
            finally:
                dialog.done(0)
                dialog.deleteLater()
        finally:
            _close(window)

    def test_sidebar_context_menu_sets_cover(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path, [LocalPlaylist(id=1, name="A", items=[_item(1)])]
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            item = _local_header(window).child(0)
            pos = window.sidebar.visualItemRect(item).center()
            _patch_menu_exec(
                monkeypatch,
                lambda menu: _find_action(
                    menu, lambda a: a.text() == "设置封面…"
                ),
            )
            picked: list[LocalPlaylist] = []
            monkeypatch.setattr(
                window,
                "_pick_local_playlist_cover",
                lambda playlist: picked.append(playlist),
            )
            window._on_sidebar_context_menu(pos)
            assert [p.id for p in picked] == [1]
        finally:
            _close(window)

    def test_sidebar_context_menu_restores_auto(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="A", cover_url="http://manual/x.jpg",
                    items=[_item(1)],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            item = _local_header(window).child(0)
            pos = window.sidebar.visualItemRect(item).center()
            _patch_menu_exec(
                monkeypatch,
                lambda menu: _find_action(
                    menu, lambda a: a.text() == "使用自动封面"
                ),
            )
            window._on_sidebar_context_menu(pos)
            assert window._find_local_playlist(1).cover_url == ""
        finally:
            _close(window)

    def test_sidebar_context_menu_ignores_other_kinds(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            calls: list = []
            monkeypatch.setattr(
                main_window_module, "_exec_menu",
                lambda menu, pos: calls.append(menu) or None,
            )
            # 分区头与「设置」都不该弹菜单
            header = _local_header(window)
            window._on_sidebar_context_menu(
                window.sidebar.visualItemRect(header).center()
            )
            settings_item = window.sidebar.topLevelItem(
                window.sidebar.topLevelItemCount() - 1
            )
            window._on_sidebar_context_menu(
                window.sidebar.visualItemRect(settings_item).center()
            )
            assert calls == []
        finally:
            _close(window)


def _png_bytes() -> bytes:
    """一张 4x4 纯色 PNG(测试封面解码用)。"""
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtGui import QColor, QImage

    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(QColor("#3366CC"))
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class TestSidebarTreeLocalSection:
    """独立树:_SECTION_CHILD_KIND 含 local,拖拽自动生效。"""

    def test_section_child_kind_mapping(self):
        assert _SidebarTree._SECTION_CHILD_KIND["local"] == "local-playlist"

    def test_section_of_local_header(self, qapp):
        tree = _SidebarTree()
        header = main_window_module.QTreeWidgetItem(["本地歌单"])
        header.setFlags(Qt.ItemFlag.ItemIsEnabled)
        header.setData(0, _ROLE, ("local-header", None))
        tree.addTopLevelItem(header)
        child = main_window_module.QTreeWidgetItem(header, ["歌单"])
        child.setData(0, _ROLE, ("local-playlist", 3))
        assert _SidebarTree.section_of(header) == "local"
        assert _SidebarTree.section_of(child) == "local"
        assert tree.section_child_ids("local") == [3]
        tree.deleteLater()


# ---------------------------------------------------------------------------
# 歌单管理:重命名 / 删除 / 上移下移 / 歌曲调序与移除
# ---------------------------------------------------------------------------

def _menu_labels(monkeypatch, window, item) -> list[str]:
    """捕获侧栏右键菜单项文案,不真正弹出模态菜单。"""
    captured: dict[str, list[str]] = {}

    def fake_exec(menu, _global_pos):
        captured["labels"] = [a.text() for a in menu.actions()]
        captured["actions"] = list(menu.actions())
        return None

    monkeypatch.setattr(main_window_module, "_exec_menu", fake_exec)
    window._on_sidebar_context_menu(window.sidebar.visualItemRect(item).center())
    return captured["labels"]


class TestPlaylistManagement:
    def test_context_menu_has_management_items(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [LocalPlaylist(id=1, name="A"), LocalPlaylist(id=2, name="B")],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            labels = _menu_labels(monkeypatch, window, _local_header(window).child(0))
            assert "重命名…" in labels
            assert "删除歌单…" in labels
            assert "上移" in labels
            assert "下移" in labels
        finally:
            _close(window)

    def test_move_up_down_persists(self, qapp, monkeypatch, tmp_path):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(id=1, name="A"),
                LocalPlaylist(id=2, name="B"),
                LocalPlaylist(id=3, name="C"),
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._move_local_playlist(3, -1)
            assert [p.id for p in window._local_playlists] == [1, 3, 2]
            stored = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert stored["local_playlist_order"] == [1, 3, 2]
            assert window.sidebar.section_child_ids("local") == [1, 3, 2]
            # 首项再上移是空操作(不写坏顺序)
            window._move_local_playlist(1, -1)
            assert [p.id for p in window._local_playlists] == [1, 3, 2]
        finally:
            _close(window)

    def test_rename_persists_and_rejects_duplicate(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [LocalPlaylist(id=1, name="A"), LocalPlaylist(id=2, name="B")],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(
                main_window_module.QInputDialog,
                "getText",
                staticmethod(lambda *a, **k: ("夜跑", True)),
            )
            window._rename_local_playlist(window._find_local_playlist(1))
            assert window._find_local_playlist(1).name == "夜跑"
            stored = window._store.load_local_playlists()
            assert [p.name for p in stored] == ["夜跑", "B"]
            # 与另一歌单重名被拒
            monkeypatch.setattr(
                main_window_module.QInputDialog,
                "getText",
                staticmethod(lambda *a, **k: ("B", True)),
            )
            window._rename_local_playlist(window._find_local_playlist(1))
            assert window._find_local_playlist(1).name == "夜跑"
            assert "同名" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_rename_retitles_recent_and_table_context(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(tmp_path, [LocalPlaylist(id=1, name="A")])
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._show_local_playlist(_ListRef("local-playlist", 1, "A"))
            window._queue_context = _ListRef("local-playlist", 1, "A")
            window._recent = [_ListRef("local-playlist", 1, "A")]
            monkeypatch.setattr(
                main_window_module.QInputDialog,
                "getText",
                staticmethod(lambda *a, **k: ("新名", True)),
            )
            window._rename_local_playlist(window._find_local_playlist(1))
            assert window._table_context.title == "新名"
            assert window._queue_context.title == "新名"
            assert [r.title for r in window._recent] == ["新名"]
            stored = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert stored["recent_lists"][0]["title"] == "新名"
        finally:
            _close(window)

    def test_delete_cleans_all_references(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="A",
                    items=[
                        LocalPlaylistItem(
                            source="netease", id=11, title="x",
                            cover_url="http://cover/1.jpg",
                        )
                    ],
                ),
                LocalPlaylist(id=2, name="B"),
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._show_local_playlist(_ListRef("local-playlist", 1, "A"))
            window._queue_context = _ListRef("local-playlist", 1, "A")
            window._recent = [
                _ListRef("local-playlist", 1, "A"),
                _ListRef("netease-playlist", 9, "远端"),
            ]
            monkeypatch.setattr(
                main_window_module.QMessageBox,
                "question",
                staticmethod(
                    lambda *a, **k: main_window_module.QMessageBox.StandardButton.Yes
                ),
            )
            window._delete_local_playlist(window._find_local_playlist(1))

            assert [p.id for p in window._local_playlists] == [2]
            # 歌单文件
            assert [p.id for p in window._store.load_local_playlists()] == [2]
            # 排序键
            stored = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert stored["local_playlist_order"] == [2]
            # 最近栈
            assert [(r.kind, r.id) for r in window._recent] == [
                ("netease-playlist", 9)
            ]
            # 表格上下文清空
            assert window._table_context is None
            assert window.song_table.rowCount() == 0
            # 队列只解绑,不清空(不打断播放)
            assert window._queue_context is None
            assert window._queue_dirty is True
            assert "已删除歌单" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_delete_cancelled_keeps_playlist(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(tmp_path, [LocalPlaylist(id=1, name="A")])
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(
                main_window_module.QMessageBox,
                "question",
                staticmethod(
                    lambda *a, **k: main_window_module.QMessageBox.StandardButton.No
                ),
            )
            window._delete_local_playlist(window._find_local_playlist(1))
            assert [p.id for p in window._local_playlists] == [1]
        finally:
            _close(window)
class TestLocalSongEditing:
    def _open(self, window, playlist_id: int = 1) -> None:
        window._show_local_playlist(
            _ListRef("local-playlist", playlist_id, "P")
        )

    def test_drag_enabled_only_for_local_playlist(
        self, qapp, monkeypatch, tmp_path
    ):
        from PySide6.QtWidgets import QAbstractItemView

        _write_playlists(tmp_path, [LocalPlaylist(id=1, name="P")])
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            self._open(window)
            assert (
                window.song_table.dragDropMode()
                == QAbstractItemView.DragDropMode.InternalMove
            )
            assert window.song_table.dragEnabled()
            # 远端列表不开拖放
            window._set_table_songs(
                [], "netease", _ListRef("netease-playlist", 5, "远端")
            )
            assert (
                window.song_table.dragDropMode()
                == QAbstractItemView.DragDropMode.NoDragDrop
            )
            assert not window.song_table.dragEnabled()
        finally:
            _close(window)

    def test_reorder_persists_and_marks_queue_dirty(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="P",
                    items=[
                        LocalPlaylistItem(source="netease", id=n, title=t)
                        for n, t in ((11, "A"), (12, "B"), (13, "C"))
                    ],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            self._open(window)
            # 第 3 行拖到第 1 行上半 → 插到最前
            window.song_table.setCurrentCell(2, 0)
            rect = window.song_table.visualRect(
                window.song_table.model().index(0, 0)
            )
            event = QDropEvent(
                QPointF(rect.center().x(), rect.top() + 2),
                Qt.DropAction.MoveAction,
                QMimeData(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            window.song_table.dropEvent(event)
            assert event.isAccepted()
            assert [i.title for i in window._find_local_playlist(1).items] == [
                "C", "A", "B",
            ]
            assert [
                window.song_table.item(r, 2).text()
                for r in range(window.song_table.rowCount())
            ] == ["C", "A", "B"]
            assert window._queue_dirty is True
            stored = window._store.load_local_playlists()
            assert [i.title for i in stored[0].items] == ["C", "A", "B"]
        finally:
            _close(window)

    def test_reorder_to_same_position_is_noop(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="P",
                    items=[
                        LocalPlaylistItem(source="netease", id=n, title=t)
                        for n, t in ((11, "A"), (12, "B"))
                    ],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            self._open(window)
            window._queue_dirty = False
            window.song_table.setCurrentCell(0, 0)
            rect = window.song_table.visualRect(
                window.song_table.model().index(0, 0)
            )
            event = QDropEvent(
                QPointF(rect.center().x(), rect.top() + 2),
                Qt.DropAction.MoveAction,
                QMimeData(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            window.song_table.dropEvent(event)
            assert [i.title for i in window._find_local_playlist(1).items] == [
                "A", "B",
            ]
            # 顺序没变就不该标脏
            assert window._queue_dirty is False
        finally:
            _close(window)

    def test_remove_song_updates_table_and_file(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="P",
                    items=[
                        LocalPlaylistItem(source="netease", id=n, title=t)
                        for n, t in ((11, "A"), (12, "B"))
                    ],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            self._open(window)
            window._remove_song_from_local_playlist(1, window._table_songs[0])
            assert [i.title for i in window._find_local_playlist(1).items] == ["B"]
            assert window.song_table.rowCount() == 1
            assert window._queue_dirty is True
            stored = window._store.load_local_playlists()
            assert [i.title for i in stored[0].items] == ["B"]
        finally:
            _close(window)

    def test_remove_uses_bvid_for_kugou(self, qapp, monkeypatch, tmp_path):
        """酷狗两首 id 都是 0,必须按 extra["bvid"] 区分,不能删错。"""
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="P",
                    items=[
                        LocalPlaylistItem(
                            source="kugou", id=0, extra={"bvid": "hash-a"},
                            title="甲",
                        ),
                        LocalPlaylistItem(
                            source="kugou", id=0, extra={"bvid": "hash-b"},
                            title="乙",
                        ),
                    ],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            self._open(window)
            window._remove_song_from_local_playlist(1, window._table_songs[1])
            assert [i.title for i in window._find_local_playlist(1).items] == [
                "甲"
            ]
        finally:
            _close(window)

    def test_remove_menu_only_in_local_context(
        self, qapp, monkeypatch, tmp_path
    ):
        _write_playlists(
            tmp_path,
            [
                LocalPlaylist(
                    id=1, name="P",
                    items=[LocalPlaylistItem(source="netease", id=11, title="A")],
                )
            ],
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            captured: dict[str, list[str]] = {}

            def fake_exec(menu, _pos):
                captured["labels"] = [a.text() for a in menu.actions()]
                return None

            monkeypatch.setattr(main_window_module, "_exec_menu", fake_exec)
            self._open(window)
            rect = window.song_table.visualRect(
                window.song_table.model().index(0, 0)
            )
            window._on_table_context_menu(rect.center())
            assert "从歌单移除" in captured["labels"]
            # 远端上下文:不该出现
            window._set_table_songs(
                [window._table_songs[0]], "netease",
                _ListRef("netease-playlist", 5, "远端"),
            )
            window._on_table_context_menu(rect.center())
            assert "从歌单移除" not in captured["labels"]
        finally:
            _close(window)
