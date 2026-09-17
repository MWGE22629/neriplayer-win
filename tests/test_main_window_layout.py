"""主窗口布局回归:中央区与播放条必须真正挂进渲染树。

背景:QSplitter 改造时曾丢失 layout.addLayout(root),导致 central_stack
与 player_bar 无父容器、界面空白——数据层测试全部通过但渲染全空。
此测试在渲染层面断言,防同类回归。

M5 增补侧栏树(_SidebarTree)行为:三分区结构、分区折叠持久化、
分区内拖拽排序约束与顺序落盘。
M5 另增播放失败统一出口回归:非阻塞提示、连续失败熔断、
过期解析结果作废与防卡死 token 自增。
M5 再增下一首预取回归:起播触发预取、消费命中零网络、
切歌单/换模式失效、预取链接过期重解析兜底。
"""

from __future__ import annotations

import json
import time

from PySide6.QtCore import QPointF, QMimeData, QSize, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from neriplayer_win.api.bili import BiliAccount, BiliFavFolder, BiliFavItem
from neriplayer_win.api.netease import (
    NeteaseAccount,
    NeteasePlaylist,
    NeteaseSong,
    NeteaseUserPlaylists,
)
from neriplayer_win.player.queue import BackupUrlRotator, PlayMode, QueueSong
from neriplayer_win.ui import main_window as main_window_module
from neriplayer_win.ui.main_window import (
    _ActivePlay,
    _ListRef,
    _PrefetchedPlay,
    _SidebarTree,
    MainWindow,
)

_ROLE = Qt.ItemDataRole.UserRole


def _make_window(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window


def _close(window) -> None:
    # 真实平台下托盘可用,close 默认走"收托盘"分支不触发清理
    # (媒体热键仍被占,污染后续测试),故强制真退出路径
    window._force_exit = True
    window.close()


def test_body_layout_renders_central_and_player_bar(qapp, monkeypatch, tmp_path):
    window = _make_window(qapp, monkeypatch, tmp_path)
    try:
        assert window.central_stack.parentWidget() is not None, (
            "central_stack 无父容器(布局挂载丢失)"
        )
        assert window.player_bar.parentWidget() is not None, (
            "player_bar 无父容器(布局挂载丢失)"
        )
        assert window.central_stack.isVisibleTo(window), "central_stack 不在渲染树"
        assert window.player_bar.isVisibleTo(window), "player_bar 不在渲染树"
        # 侧栏与右列并存于 splitter
        assert window._splitter.count() == 2
    finally:
        _close(window)


# ---------------------------------------------------------------------------
# M5 侧栏树:结构 / 折叠 / 排序
# ---------------------------------------------------------------------------


class TestSidebarTreeStructure:
    def test_three_sections_logged_out(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            tree = window.sidebar
            assert isinstance(tree, _SidebarTree)
            assert isinstance(tree, QTreeWidget)
            assert tree.topLevelItemCount() == 3
            netease, bili, settings = (tree.topLevelItem(i) for i in range(3))
            assert netease.data(0, _ROLE) == ("netease-header", None)
            assert bili.data(0, _ROLE) == ("bili-header", None)
            assert settings.data(0, _ROLE) == ("settings", None)
            assert settings.childCount() == 0, "「设置」必须是顶层无子节点项"
            # 未登录占位子节点
            assert netease.childCount() == 1
            assert netease.child(0).data(0, _ROLE) == ("netease-login", None)
            assert bili.childCount() == 1
            assert bili.child(0).data(0, _ROLE) == ("bili-login", None)
            # 分区头启用但不可选中(禁用父项会让子节点无法选中,故不能 NoItemFlags)
            assert not netease.flags() & Qt.ItemFlag.ItemIsSelectable
            assert netease.flags() & Qt.ItemFlag.ItemIsEnabled
            # 默认全展开
            assert netease.isExpanded() and bili.isExpanded()
        finally:
            _close(window)

    def test_loading_placeholders(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._rebuild_sidebar()
            netease = window.sidebar.topLevelItem(0)
            assert netease.childCount() == 1
            child = netease.child(0)
            assert child.text(0) == "歌单加载中…"
            assert child.data(0, _ROLE) == ("netease-loading", None)
            assert not child.flags() & Qt.ItemFlag.ItemIsSelectable

            window._bili_account = BiliAccount(mid=42, uname="测试")
            window._rebuild_sidebar()
            # 登录网易云后四分区:歌单 / 收藏 / B站 / 设置
            bili = window.sidebar.topLevelItem(2)
            # 稍后再看固定首位,收藏夹加载占位其后
            assert bili.child(0).data(0, _ROLE) == ("bili-watchlater", None)
            assert bili.child(1).text(0) == "收藏夹加载中…"
        finally:
            _close(window)

    def test_sidebar_text_has_no_exotic_glyph(self, qapp, monkeypatch, tmp_path):
        """侧栏文本必须避开生僻字形:折叠指示只允许画进图标(三角徽标)。

        「▾/▸」等几何字形会触发 DirectWrite 回退字体加载,实测给进程带来
        ~50MB 常驻 WorkingSet(2026-09-15 v0.2.0 打包验收时踩坑);
        此测试防止文字型指示符被重新引入。
        """
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._playlists = [NeteasePlaylist(id=7, name="歌单A", track_count=3)]
            window._rebuild_sidebar()
            for row in range(window.sidebar.topLevelItemCount()):
                top = window.sidebar.topLevelItem(row)
                for item in (top, *(top.child(i) for i in range(top.childCount()))):
                    for ch in item.text(0):
                        code = ord(ch)
                        # 允许:常用拉丁/标点(含 · 与 … U+2026)、CJK 标点、CJK 汉字;
                        # 拦截几何形状/箭头/杂项符号区(如 ▾ U+25BE)
                        allowed = (
                            code <= 0x2026
                            or 0x3000 <= code <= 0x303F
                            or 0x4E00 <= code <= 0x9FFF
                        )
                        assert allowed, (
                            f"侧栏文本「{item.text(0)}」含低频区字符 U+{code:04X},"
                            "可能触发 DirectWrite 回退字体(折叠指示应画进图标)"
                        )
        finally:
            _close(window)


class TestSidebarCollapse:
    def test_every_section_arrow_rotates_on_toggle(
        self, qapp, monkeypatch, tmp_path
    ):
        """回归(v0.2.0 起的老 bug):所有分区头折叠/展开后角标必须转向。

        旧实现把分区键(netease-subscribed / bili / recent)当品牌名传入
        _section_header_icon,查 _SOFT_BRAND_COLORS 时 KeyError 在 Qt 槽内
        被吞、setIcon 不执行——表现为只有「网易云 · 歌单」的角标会动,
        其余三个分区角标冻结;且已登录(bili)时必现。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            # 双平台登录 + 一条最近记录:四个分区头齐备
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._bili_account = BiliAccount(mid=42, uname="测试")
            window._recent = [main_window_module._ListRef("bili-folder", 9, "收藏夹")]
            window._rebuild_sidebar()
            for section in ("recent", "netease", "netease-subscribed", "bili"):
                header = window.sidebar.find_header(section)
                assert header is not None and header.isExpanded(), section
                expanded_image = (
                    header.icon(0).pixmap(QSize(30, 18)).toImage()
                )
                header.setExpanded(False)  # 经 itemCollapsed 刷新角标
                collapsed_image = (
                    header.icon(0).pixmap(QSize(30, 18)).toImage()
                )
                assert collapsed_image != expanded_image, (
                    f"{section} 分区头折叠后角标未转向"
                )
                header.setExpanded(True)
                reopened = header.icon(0).pixmap(QSize(30, 18)).toImage()
                assert reopened == expanded_image, (
                    f"{section} 分区头展开后角标未转回"
                )
        finally:
            _close(window)

    def test_header_icons_carry_hidpi_backing(self, qapp, monkeypatch, tmp_path):
        """防回归:分区头合成图标必须带 ≥2x 物理底图。

        DPR=1 的 30x18 画布在 125%~200% 缩放的屏幕上会被拉伸渲染,
        品牌标/折叠三角发糊(用户可感知);合成画布固定 2x 栅格化。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._bili_account = BiliAccount(mid=42, uname="测试")
            window._recent = [main_window_module._ListRef("bili-folder", 9, "收藏夹")]
            window._rebuild_sidebar()
            for section in ("recent", "netease", "netease-subscribed", "bili"):
                header = window.sidebar.find_header(section)
                hidpi = header.icon(0).pixmap(QSize(30, 18), 2.0)
                assert hidpi.devicePixelRatio() == 2.0, (
                    f"{section} 分区头图标没有 2x 物理底图"
                )
                assert (hidpi.width(), hidpi.height()) == (60, 36)
        finally:
            _close(window)

    def test_header_click_collapses_and_persists(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            tree = window.sidebar
            header = tree.topLevelItem(0)
            assert header.isExpanded()
            QTest.mouseClick(
                tree.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                tree.visualItemRect(header).center(),
            )
            qapp.processEvents()
            assert header.isExpanded() is False
            # 折叠状态落盘(未登录时「网易云·收藏」分区不出现,保持默认展开)
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["sidebar_expanded"] == {
                "netease": False,
                "netease-subscribed": True,
                "bili": True,
                "recent": True,
            }
            # 全量重建后重新应用收起状态(不回弹)
            window._rebuild_sidebar()
            assert tree.topLevelItem(0).isExpanded() is False
            assert tree.topLevelItem(1).isExpanded() is True
        finally:
            _close(window)

    def test_stored_collapsed_state_applied_on_startup(
        self, qapp, monkeypatch, tmp_path
    ):
        (tmp_path / "settings.json").write_text(
            json.dumps({"sidebar_expanded": {"netease": False, "bili": True}}),
            encoding="utf-8",
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window.sidebar.topLevelItem(0).isExpanded() is False
            assert window.sidebar.topLevelItem(1).isExpanded() is True
        finally:
            _close(window)


def _netease_playlists(*ids: int) -> list[NeteasePlaylist]:
    return [
        NeteasePlaylist(id=i, name=f"歌单{i}", track_count=i) for i in ids
    ]


class TestStoredOrderApplied:
    def test_playlists_sorted_and_pruned(self, qapp, monkeypatch, tmp_path):
        # 存储顺序 [30, 99, 10]:99 已失效应被静默清掉,新歌单 20 追加尾部
        (tmp_path / "settings.json").write_text(
            json.dumps({"netease_playlist_order": [30, 99, 10]}),
            encoding="utf-8",
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._on_playlists_loaded(
                NeteaseUserPlaylists(created=_netease_playlists(10, 20, 30))
            )
            header = window.sidebar.topLevelItem(0)
            roles = [
                header.child(i).data(0, _ROLE) for i in range(header.childCount())
            ]
            assert roles == [
                ("netease-playlist", 30),
                ("netease-playlist", 10),
                ("netease-playlist", 20),
            ]
            # 清理后的顺序落盘
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["netease_playlist_order"] == [30, 10, 20]
            # 内存列表同步:重建后顺序不回退
            window._rebuild_sidebar()
            assert window.sidebar.section_child_ids("netease") == [30, 10, 20]
        finally:
            _close(window)

    def test_subscribed_playlists_sorted_and_pruned(
        self, qapp, monkeypatch, tmp_path
    ):
        (tmp_path / "settings.json").write_text(
            json.dumps({"netease_subscribed_order": [30, 99, 10]}),
            encoding="utf-8",
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._on_playlists_loaded(
                NeteaseUserPlaylists(subscribed=_netease_playlists(10, 20, 30))
            )
            # 收藏分区是第二个顶层项(无最近条目时不出现「最近」分区)
            header = window.sidebar.topLevelItem(1)
            assert header.data(0, _ROLE) == ("netease-subscribed-header", None)
            roles = [
                header.child(i).data(0, _ROLE) for i in range(header.childCount())
            ]
            # 每日推荐固定首位,其后才是收藏歌单(按存储序)
            assert roles == [
                ("netease-daily", None),
                ("netease-subscribed-playlist", 30),
                ("netease-subscribed-playlist", 10),
                ("netease-subscribed-playlist", 20),
            ]
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["netease_subscribed_order"] == [30, 10, 20]
            window._rebuild_sidebar()
            assert window.sidebar.section_child_ids("netease-subscribed") == [30, 10, 20]
        finally:
            _close(window)

    def test_bili_folders_sorted(self, qapp, monkeypatch, tmp_path):
        (tmp_path / "settings.json").write_text(
            json.dumps({"bili_folder_order": [9, 1]}), encoding="utf-8"
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._bili_account = BiliAccount(mid=42, uname="测试")
            window._on_bili_folders_loaded(
                (
                    [
                        BiliFavFolder(media_id=1, fid=1, mid=42, title="默认收藏夹"),
                        BiliFavFolder(media_id=9, fid=9, mid=42, title="歌单收藏"),
                        BiliFavFolder(media_id=5, fid=5, mid=42, title="新收藏"),
                    ],
                    4,  # 稍后再看条数
                )
            )
            assert window.sidebar.section_child_ids("bili") == [9, 1, 5]
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# 网易云收藏歌单分区 + B站稍后再看
# ---------------------------------------------------------------------------


class TestSubscribedSection:
    """「网易云 · 收藏」:登录后与自建歌单并列;未登录整个分区不出现。"""

    def test_subscribed_rendered_parallel_to_created(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._on_playlists_loaded(
                NeteaseUserPlaylists(
                    created=_netease_playlists(1, 2),
                    subscribed=[
                        NeteasePlaylist(id=31, name="共享歌单", track_count=28),
                        NeteasePlaylist(id=32, name="空收藏", track_count=0),
                    ],
                )
            )
            tree = window.sidebar
            assert tree.topLevelItemCount() == 4
            created, subscribed, _bili, _settings = (
                tree.topLevelItem(i) for i in range(4)
            )
            assert created.data(0, _ROLE) == ("netease-header", None)
            assert subscribed.data(0, _ROLE) == ("netease-subscribed-header", None)
            # 每日推荐固定首位(不参与拖拽排序)
            assert subscribed.child(0).text(0) == "每日推荐"
            assert subscribed.child(0).data(0, _ROLE) == ("netease-daily", None)
            assert subscribed.child(1).text(0) == "共享歌单(28)"
            assert subscribed.child(1).data(0, _ROLE) == (
                "netease-subscribed-playlist", 31,
            )
            # track_count 为 0 不追加计数
            assert subscribed.child(2).text(0) == "空收藏"
            # 分区独立折叠持久化
            subscribed.setExpanded(False)
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["sidebar_expanded"]["netease-subscribed"] is False
        finally:
            _close(window)

    def test_empty_subscribed_shows_placeholder(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._on_playlists_loaded(
                NeteaseUserPlaylists(created=_netease_playlists(1))
            )
            subscribed = window.sidebar.topLevelItem(1)
            # 每日推荐固定首位,占位在其后
            assert subscribed.child(0).data(0, _ROLE) == ("netease-daily", None)
            child = subscribed.child(1)
            assert child.text(0) == "收藏加载中…"
            assert child.data(0, _ROLE) == ("netease-subscribed-loading", None)
            assert not child.flags() & Qt.ItemFlag.ItemIsSelectable
        finally:
            _close(window)

    def test_stale_login_clears_subscribed(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._on_playlists_loaded(
                NeteaseUserPlaylists(subscribed=_netease_playlists(5))
            )
            window._handle_stale_login("登录已过期")
            assert window._subscribed_playlists == []
            # 未登录:分区消失,顶层回到三项
            assert window.sidebar.topLevelItemCount() == 3
        finally:
            _close(window)

    def test_subscribed_playlist_click_starts_load(
        self, qapp, monkeypatch, tmp_path
    ):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._on_playlists_loaded(
                NeteaseUserPlaylists(subscribed=_netease_playlists(31))
            )
            item = window.sidebar.topLevelItem(1).child(0)
            window._on_sidebar_item_clicked(item, 0)
            assert window.central_stack.currentIndex() == 1  # 切到歌曲表页
            assert len(scheduled) == 1  # 后台加载歌曲,UI 线程零网络
            assert "正在加载" in window.statusBar().currentMessage()
        finally:
            _close(window)


class TestBiliWatchLater:
    """「稍后再看」:B站登录后固定在收藏夹分区首位,不参与拖拽排序。"""

    def test_watchlater_first_child_with_count(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._bili_account = BiliAccount(mid=42, uname="测试")
            window._on_bili_folders_loaded(
                (
                    [BiliFavFolder(media_id=9, fid=9, mid=42, title="默认收藏夹")],
                    7,
                )
            )
            header = window.sidebar.find_header("bili")
            first = header.child(0)
            assert first.text(0) == "稍后再看(7)"
            assert first.data(0, _ROLE) == ("bili-watchlater", None)
            # 不参与排序:顺序持久化只读收藏夹条目
            assert window.sidebar.section_child_ids("bili") == [9]
        finally:
            _close(window)

    def test_watchlater_count_pending_shows_plain_title(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._bili_account = BiliAccount(mid=42, uname="测试")
            window._rebuild_sidebar()
            first = window.sidebar.find_header("bili").child(0)
            assert first.text(0) == "稍后再看"
        finally:
            _close(window)

    def test_watchlater_click_loads_items(self, qapp, monkeypatch, tmp_path):
        """表格与队列解耦:点击加载只填表格,播放队列不被替换。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._bili_account = BiliAccount(mid=42, uname="测试")
            window._rebuild_sidebar()
            monkeypatch.setattr(
                window._bili_client,
                "get_watch_later_items",
                lambda: [
                    BiliFavItem(
                        type=2, id=1, bvid="BV1", title="视频A",
                        duration_sec=60, upper_name="UP",
                    ),
                    BiliFavItem(
                        type=21, id=2, bvid=None, title="合集B",
                        duration_sec=30, upper_name="UP",
                    ),
                ],
            )
            item = window.sidebar.find_header("bili").child(0)
            window._on_sidebar_item_clicked(item, 0)
            assert window.central_stack.currentIndex() == 1
            assert len(scheduled) == 1
            fetch, on_done, _on_error = scheduled[0]
            on_done(fetch())
            # 不可播条目(无 bvid)被跳过;表格填的是浏览列表
            assert window.song_table.rowCount() == 1
            assert window._table_songs[0].title == "视频A"
            assert window._table_songs[0].bvid == "BV1"
            assert window._table_context == main_window_module._ListRef(
                kind="bili-watchlater", id=0, title="稍后再看",
            )
            # 队列仍是空的:浏览不动队列,双击才装入
            assert window._queue.items() == []
            # 计数刷新并回写侧栏
            assert window._bili_watchlater_count == 2
            assert (
                window.sidebar.find_header("bili").child(0).text(0)
                == "稍后再看(2)"
            )
            assert "稍后再看" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_stale_login_resets_watchlater(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._bili_account = BiliAccount(mid=42, uname="测试")
            window._bili_watchlater_count = 7
            window._handle_bili_stale_login("B站登录已过期")
            assert window._bili_watchlater_count is None
            header = window.sidebar.find_header("bili")
            assert header.child(0).data(0, _ROLE) == ("bili-login", None)
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# _SidebarTree 拖拽约束(独立树,不牵 MainWindow 网络路径)
# ---------------------------------------------------------------------------


def _make_standalone_tree(qapp) -> _SidebarTree:
    tree = _SidebarTree()
    netease = QTreeWidgetItem(["网易云 · 歌单"])
    netease.setFlags(Qt.ItemFlag.ItemIsEnabled)
    netease.setData(0, _ROLE, ("netease-header", None))
    tree.addTopLevelItem(netease)
    for pid in (1, 2, 3):
        child = QTreeWidgetItem(netease, [f"歌单{pid}"])
        child.setData(0, _ROLE, ("netease-playlist", pid))
    bili = QTreeWidgetItem(["B站 · 收藏夹"])
    bili.setFlags(Qt.ItemFlag.ItemIsEnabled)
    bili.setData(0, _ROLE, ("bili-header", None))
    tree.addTopLevelItem(bili)
    b_child = QTreeWidgetItem(bili, ["收藏夹"])
    b_child.setData(0, _ROLE, ("bili-folder", 9))
    settings = QTreeWidgetItem(["设置"])
    settings.setFlags(
        Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
    )
    settings.setData(0, _ROLE, ("settings", None))
    tree.addTopLevelItem(settings)
    netease.setExpanded(True)
    tree.resize(240, 400)
    tree.show()
    qapp.processEvents()
    return tree


def _drop_on(tree: _SidebarTree, target: QTreeWidgetItem) -> QDropEvent:
    """在 target 中心构造并派发一次 InternalMove dropEvent(未先经 dragMove,
    dropIndicatorPosition 取默认 OnItem,即「插到目标之后」)。"""
    center = tree.visualItemRect(target).center()
    event = QDropEvent(
        QPointF(center),
        Qt.DropAction.MoveAction,
        QMimeData(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tree.dropEvent(event)
    return event


class TestSidebarDropConstraints:
    def test_cross_section_rejected(self, qapp):
        tree = _make_standalone_tree(qapp)
        netease_child = tree.topLevelItem(0).child(0)
        bili_child = tree.topLevelItem(1).child(0)
        tree.setCurrentItem(netease_child)
        event = _drop_on(tree, bili_child)
        assert not event.isAccepted()
        assert tree.section_child_ids("netease") == [1, 2, 3]
        assert tree.section_child_ids("bili") == [9]

    def test_drop_on_top_level_rejected(self, qapp):
        tree = _make_standalone_tree(qapp)
        netease_child = tree.topLevelItem(0).child(0)
        tree.setCurrentItem(netease_child)
        # 拖到「设置」顶层项上
        event = _drop_on(tree, tree.topLevelItem(2))
        assert not event.isAccepted()
        # 拖到「B站」分区头上(同索引顶层)
        event = _drop_on(tree, tree.topLevelItem(1))
        assert not event.isAccepted()
        assert tree.section_child_ids("netease") == [1, 2, 3]

    def test_moving_top_level_item_rejected(self, qapp):
        tree = _make_standalone_tree(qapp)
        settings_item = tree.topLevelItem(2)
        tree.setCurrentItem(settings_item)
        target = tree.topLevelItem(0).child(1)
        event = _drop_on(tree, target)
        assert not event.isAccepted()
        assert tree.topLevelItemCount() == 3
        assert tree.section_child_ids("netease") == [1, 2, 3]

    def test_same_section_move_persists_order_and_emits(self, qapp):
        tree = _make_standalone_tree(qapp)
        received: list[str] = []
        tree.order_changed.connect(received.append)
        p1 = tree.topLevelItem(0).child(0)
        p3 = tree.topLevelItem(0).child(2)
        tree.setCurrentItem(p1)
        event = _drop_on(tree, p3)
        assert event.isAccepted()
        assert tree.section_child_ids("netease") == [2, 3, 1]
        assert received == ["netease"]

    def test_drop_on_self_is_noop(self, qapp):
        tree = _make_standalone_tree(qapp)
        received: list[str] = []
        tree.order_changed.connect(received.append)
        p2 = tree.topLevelItem(0).child(1)
        tree.setCurrentItem(p2)
        event = _drop_on(tree, p2)
        assert event.isAccepted()
        assert tree.section_child_ids("netease") == [1, 2, 3]
        assert received == []


class TestSidebarMoveChild:
    def test_move_above_and_below(self, qapp):
        tree = _make_standalone_tree(qapp)
        header = tree.topLevelItem(0)
        p1, _p2, p3 = (header.child(i) for i in range(3))
        tree._move_child(p3, p1, below=False)  # p3 移到 p1 前
        assert tree.section_child_ids("netease") == [3, 1, 2]
        tree._move_child(p1, p3, below=True)  # p1 移到 p3 紧后方
        assert tree.section_child_ids("netease") == [3, 1, 2]
        tree._move_child(p3, p1, below=True)  # p3 移到 p1 紧后方
        assert tree.section_child_ids("netease") == [1, 3, 2]

    def test_section_of(self, qapp):
        tree = _make_standalone_tree(qapp)
        assert _SidebarTree.section_of(tree.topLevelItem(0)) == "netease"
        assert _SidebarTree.section_of(tree.topLevelItem(0).child(0)) == "netease"
        assert _SidebarTree.section_of(tree.topLevelItem(1)) == "bili"
        assert _SidebarTree.section_of(tree.topLevelItem(2)) is None
        assert _SidebarTree.section_of(None) is None


class TestSidebarDragPersists:
    def test_drag_reorder_persists_and_syncs_memory(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._account = NeteaseAccount(user_id=1, nickname="测试")
            window._on_playlists_loaded(
                NeteaseUserPlaylists(created=_netease_playlists(10, 20, 30))
            )
            tree = window.sidebar
            p10 = tree.topLevelItem(0).child(0)
            p30 = tree.topLevelItem(0).child(2)
            tree.setCurrentItem(p10)
            event = _drop_on(tree, p30)
            assert event.isAccepted()
            # 拖拽后的顺序落库
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["netease_playlist_order"] == [20, 30, 10]
            # 内存列表同步:重建不回退到旧顺序
            window._rebuild_sidebar()
            assert tree.section_child_ids("netease") == [20, 30, 10]
            assert [p.id for p in window._playlists] == [20, 30, 10]
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# M5 播放失败统一出口:非阻塞提示 / 熔断 / 过期解析作废
# ---------------------------------------------------------------------------


def _fail_song(n: int = 1) -> QueueSong:
    return QueueSong(
        source="netease", id=n, bvid="", title=f"失败曲{n}",
        artist="测试", duration_ms=60000,
    )


class _StubEngine:
    """只记录调用不真播;closeEvent 的 stop 也一并兜住。"""

    def __init__(self) -> None:
        self.played: list[str] = []

    def play_url(self, url: str, headers=None) -> None:
        self.played.append(url)

    def stop(self) -> None:
        pass


class TestPlayFailureHandling:
    def test_failure_below_limit_shows_skip_hint(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._handle_play_failure(_fail_song(), "网络超时")
            assert window._play_fail_count == 1
            assert "即将自动跳到下一首" in window.statusBar().currentMessage()
        finally:
            window._fail_skip_token += 1  # 作废待触发的 singleShot,不污染后续测试
            _close(window)

    def test_failure_breaker_trips_at_third(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            song = _fail_song()
            window._handle_play_failure(song, "网络超时")  # 内部自增 token 会作废旧调度
            window._handle_play_failure(song, "网络超时")
            token_before = window._fail_skip_token
            window._handle_play_failure(song, "网络超时")  # 第 3 次:熔断
            assert window._play_fail_count == 3
            assert "已暂停自动跳过" in window.statusBar().currentMessage()
            assert window._fail_skip_token == token_before  # 熔断不再调度自动跳歌
        finally:
            window._fail_skip_token += 1  # 作废第 2 次调用留下的待触发跳歌
            _close(window)

    def test_start_play_resets_fail_count(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        engine = _StubEngine()
        monkeypatch.setattr(window, "engine", engine)
        try:
            song = _fail_song()
            window._play_fail_count = 2
            window._start_play(song, BackupUrlRotator([f"http://example/{song.id}"]), None)
            assert window._play_fail_count == 0  # 成功发起播放即复位
            assert engine.played == [f"http://example/{song.id}"]
        finally:
            _close(window)

    def test_start_play_without_url_routes_to_handler(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            song = _fail_song()
            calls: list[tuple] = []
            monkeypatch.setattr(
                window, "_handle_play_failure", lambda *args: calls.append(args)
            )
            window._start_play(song, BackupUrlRotator([]), None)
            assert calls == [(song, "无可用播放地址")]
        finally:
            _close(window)

    def test_stale_resolve_result_ignored(self, qapp, monkeypatch, tmp_path):
        """过期代际的解析结果(被新点歌覆盖)不得触发失败处理或重登。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            song = _fail_song()
            calls: list[tuple] = []
            monkeypatch.setattr(
                window, "_handle_play_failure", lambda *args: calls.append(args)
            )
            monkeypatch.setattr(
                window, "_handle_stale_login", lambda *args: calls.append(("auth",))
            )
            stale = window._make_netease_play_done(song, window._resolve_generation - 1)
            stale(("error", "迟到结果"))
            stale(("auth", ""))
            assert calls == []  # 全部静默作废
            # 当前代际的错误结果仍要走统一失败出口
            current = window._make_netease_play_done(song, window._resolve_generation)
            current(("error", "最新结果"))
            assert calls == [(song, "最新结果")]
        finally:
            _close(window)

    def test_load_failed_exhausted_rotator_routes_to_handler(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            song = _fail_song()
            window._active = _ActivePlay(
                song=song, rotator=BackupUrlRotator(["only"]), headers=None
            )
            calls: list[tuple] = []
            monkeypatch.setattr(
                window, "_handle_play_failure", lambda *args: calls.append(args)
            )
            window._on_load_failed("加载播放地址失败: 403")
            assert calls == [(song, "加载播放地址失败: 403")]
        finally:
            _close(window)

    def test_play_at_bumps_generation_and_skip_token(
        self, qapp, monkeypatch, tmp_path
    ):
        """新点歌自增代际与跳歌 token:旧解析回调与待触发跳歌一并作废。"""
        # 拦掉后台解析(run_async 已猴补,engine 缺失时也不会真的播放)
        monkeypatch.setattr(main_window_module, "run_async", lambda *a, **k: None)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._queue.replace([_fail_song(i) for i in range(3)])
            generation = window._resolve_generation
            token = window._fail_skip_token
            window._play_at(1)
            assert window._resolve_generation == generation + 1
            assert window._fail_skip_token == token + 1
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# M5 下一首预取:触发 / 消费 / 失效 / 过期兜底
# ---------------------------------------------------------------------------


class _FakePlayable:
    """网易云解析结果替身:_resolve_song_context 只读这三个字段。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.level = "exhigh"
        self.is_preview = False


def _capture_async(monkeypatch) -> list[tuple]:
    """猴补 run_async:只记录 (fn, on_done, on_error),不启线程不触网。"""
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        main_window_module,
        "run_async",
        lambda fn, on_done=None, on_error=None: scheduled.append(
            (fn, on_done, on_error)
        ),
    )
    return scheduled


def _forbid_network(monkeypatch, window) -> None:
    """网络解析函数「调用即失败」:命中预取的路径绝不应触网。"""

    def _boom(*args, **kwargs):
        raise AssertionError("该路径不应触发网络解析")

    monkeypatch.setattr(window._client, "resolve_playable_url", _boom)
    monkeypatch.setattr(window._bili_client, "resolve_audio_stream", _boom)


def _make_slot(window, index: int, url: str = "http://example/prefetched",
               age_s: float = 0.0) -> _PrefetchedPlay:
    """构造一个指向 queue 中 index 的伪造预取槽。"""
    song = window._queue.item_at(index)
    assert song is not None
    return _PrefetchedPlay(
        queue_index=index,
        song=song,
        rotator=BackupUrlRotator([url]),
        headers=None,
        status=f"正在播放:{song.title}(320K)",
        created_at=time.monotonic() - age_s,
    )


class TestPrefetchScheduling:
    """触发:起播成功后预取下一首;随机/单曲循环/队尾不预取。"""

    def test_start_play_schedules_prefetch_and_stores_slot(
        self, qapp, monkeypatch, tmp_path
    ):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        engine = _StubEngine()
        monkeypatch.setattr(window, "engine", engine)
        try:
            songs = [_fail_song(i) for i in range(3)]
            window._queue.replace(songs)
            window._queue.jump(0)
            window._start_play(
                songs[0], BackupUrlRotator(["http://example/a"]), None
            )
            assert len(scheduled) == 1  # 顺序模式中段:预取 index 1
            # 执行预取工作体(解析已替换为假实现,不触网)并投递结果
            monkeypatch.setattr(
                window._client,
                "resolve_playable_url",
                lambda *a, **k: _FakePlayable("http://example/next"),
            )
            resolve, on_done, _on_error = scheduled[0]
            on_done(resolve())
            slot = window._prefetched
            assert slot is not None
            assert slot.queue_index == 1
            assert slot.song is songs[1]
            assert slot.rotator.current == "http://example/next"
            assert "正在播放" in slot.status
        finally:
            _close(window)

    def test_no_prefetch_without_predictable_next(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            songs = [_fail_song(i) for i in range(3)]
            window._queue.replace(songs)
            # 单曲循环:下一首就是当前曲,URL 已在手
            window._set_play_mode(PlayMode.REPEAT_ONE)
            window._queue.jump(1)
            window._start_play(
                songs[1], BackupUrlRotator(["http://example/b"]), None
            )
            # 随机模式:下一曲不可预知
            window._set_play_mode(PlayMode.SHUFFLE)
            window._start_play(
                songs[1], BackupUrlRotator(["http://example/b"]), None
            )
            # 顺序模式已在队尾:播完即停
            window._set_play_mode(PlayMode.SEQUENCE)
            window._queue.jump(2)
            window._start_play(
                songs[2], BackupUrlRotator(["http://example/c"]), None
            )
            assert scheduled == []
        finally:
            _close(window)

    def test_prefetch_failure_swallowed_silently(self, qapp, monkeypatch, tmp_path):
        """预取解析失败必须静默:不落槽、不提示、不进失败路径。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            songs = [_fail_song(i) for i in range(2)]
            window._queue.replace(songs)
            window._queue.jump(0)

            def _raise(*args, **kwargs):
                raise RuntimeError("弱网解析失败")

            monkeypatch.setattr(window._client, "resolve_playable_url", _raise)
            window._start_play(
                songs[0], BackupUrlRotator(["http://example/a"]), None
            )
            resolve, on_done, _on_error = scheduled[0]
            assert resolve() is None  # 异常被吞成 None
            on_done(None)
            assert window._prefetched is None
            assert window._play_fail_count == 0  # 投机失败不计入熔断
            assert "播放失败" not in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_stale_prefetch_result_not_stored(self, qapp, monkeypatch, tmp_path):
        """预取归来时代际已变(用户切了歌):结果作废不占槽。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            songs = [_fail_song(i) for i in range(3)]
            window._queue.replace(songs)
            window._queue.jump(0)
            monkeypatch.setattr(
                window._client,
                "resolve_playable_url",
                lambda *a, **k: _FakePlayable("http://example/next"),
            )
            window._start_play(
                songs[0], BackupUrlRotator(["http://example/a"]), None
            )
            resolve, on_done, _on_error = scheduled[0]
            result = resolve()
            window._resolve_generation += 1  # 预取在途时用户点了别的歌
            on_done(result)
            assert window._prefetched is None
        finally:
            _close(window)


class TestPrefetchConsume:
    """消费:_play_at 三重校验(索引 / 歌曲标识 / TTL)后零网络起播。"""

    def test_hit_starts_play_without_network(self, qapp, monkeypatch, tmp_path):
        """验收主路径:预置伪造预取槽,网络解析「调用即失败」,
        _play_at 直接用缓存上下文起播。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        engine = _StubEngine()
        monkeypatch.setattr(window, "engine", engine)
        try:
            window._queue.replace([_fail_song(i) for i in range(3)])
            window._prefetched = _make_slot(window, 1)
            _forbid_network(monkeypatch, window)
            window._play_at(1)
            assert engine.played == ["http://example/prefetched"]  # 直接起播
            assert window._active is not None
            assert window._active.from_prefetch is True  # 供过期兜底识别
            assert window._prefetched is None  # 用后清槽
            assert "正在播放" in window.statusBar().currentMessage()
            # 之后的 run_async 只有「下一首(index 2)」的预取,当前曲零解析
            assert len(scheduled) == 1
        finally:
            _close(window)

    def test_expired_slot_falls_back_to_resolve(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        engine = _StubEngine()
        monkeypatch.setattr(window, "engine", engine)
        try:
            window._queue.replace([_fail_song(i) for i in range(3)])
            window._prefetched = _make_slot(
                window, 1, age_s=main_window_module._PREFETCH_TTL_S + 1
            )
            window._play_at(1)
            assert engine.played == []  # 未用过期缓存直接起播
            assert window._prefetched is None
            assert len(scheduled) == 1  # 走正常后台解析
            assert "正在解析播放地址" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_index_mismatch_discards_slot(self, qapp, monkeypatch, tmp_path):
        """手动跳歌(槽指向 index 2、实际点 index 1):未命中即作废。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            window._queue.replace([_fail_song(i) for i in range(3)])
            window._prefetched = _make_slot(window, 2)
            window._play_at(1)
            assert window._prefetched is None  # 槽作废不留
            assert len(scheduled) == 1  # 正常解析
        finally:
            _close(window)

    def test_song_identity_mismatch_discards_slot(self, qapp, monkeypatch, tmp_path):
        """同索引但歌曲换了(队列内容变化):标识不匹配即作废。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            songs = [_fail_song(i) for i in range(3)]
            window._queue.replace(songs)
            slot = _make_slot(window, 1)
            window._queue.replace(songs)  # replace 已会清槽,重新预置
            window._prefetched = _PrefetchedPlay(
                queue_index=1,  # 索引相同
                song=QueueSong(
                    source="netease", id=99, bvid="", title="另一首",
                    artist="测试", duration_ms=60000,
                ),  # 但标识不同
                rotator=slot.rotator,
                headers=None,
                status=slot.status,
                created_at=slot.created_at,
            )
            window._play_at(1)
            assert window._prefetched is None
            assert len(scheduled) == 1
        finally:
            _close(window)


class TestPrefetchInvalidation:
    """失效:切歌单(replace)与换播放模式后清槽。"""

    def test_queue_replace_clears_slot(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._queue.replace([_fail_song(i) for i in range(3)])
            window._prefetched = _make_slot(window, 1)
            window._queue.replace([_fail_song(10)])  # 切歌单
            assert window._prefetched is None
        finally:
            _close(window)

    def test_mode_change_clears_slot(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._queue.replace([_fail_song(i) for i in range(3)])
            window._prefetched = _make_slot(window, 1)
            window._set_play_mode(PlayMode.SHUFFLE)  # peek 依据模式而变
            assert window._prefetched is None
        finally:
            _close(window)


class TestPrefetchStaleUrlFallback:
    """兜底:预取链接加载失败且候选耗尽时,先静默重解析一次再失败。"""

    def test_exhausted_prefetch_retries_resolve_instead_of_failing(
        self, qapp, monkeypatch, tmp_path
    ):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        engine = _StubEngine()
        monkeypatch.setattr(window, "engine", engine)
        failures: list[tuple] = []
        monkeypatch.setattr(
            window, "_handle_play_failure", lambda *args: failures.append(args)
        )
        try:
            song = _fail_song()
            window._active = _ActivePlay(
                song=song,
                rotator=BackupUrlRotator(["http://example/prefetch-stale"]),
                headers=None,
                from_prefetch=True,
            )
            window._on_load_failed("加载播放地址失败: 403")
            assert failures == []  # 不直接进失败路径
            assert window._active.from_prefetch is False  # 标记已清,防循环
            assert "重新解析" in window.statusBar().currentMessage()
            assert len(scheduled) == 1
            # 执行重解析(假实现不触网)→ 正常起播,且不再带 from_prefetch
            monkeypatch.setattr(
                window._client,
                "resolve_playable_url",
                lambda *a, **k: _FakePlayable("http://example/re-resolved"),
            )
            resolve, on_done, _on_error = scheduled[0]
            on_done(resolve())
            assert engine.played == ["http://example/re-resolved"]
            assert window._active.from_prefetch is False
            # 重解析起播后再失败:走既有失败路径(不再重解析,循环闭合)
            window._on_load_failed("加载播放地址失败: 403")
            assert failures == [(song, "加载播放地址失败: 403")]
        finally:
            _close(window)

    def test_prefetch_rotates_backup_candidates_first(
        self, qapp, monkeypatch, tmp_path
    ):
        """来自预取的播放仍有备用候选时,先轮换候选、不急着重解析。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        engine = _StubEngine()
        monkeypatch.setattr(window, "engine", engine)
        try:
            window._active = _ActivePlay(
                song=_fail_song(),
                rotator=BackupUrlRotator(["main", "backup"]),
                headers=None,
                from_prefetch=True,
            )
            window._on_load_failed("加载播放地址失败: 403")
            assert engine.played == ["backup"]
            assert scheduled == []
            assert window._active.from_prefetch is True  # 未到兜底分支
        finally:
            _close(window)




# ---------------------------------------------------------------------------
# 表格与播放队列解耦:浏览不换队列,双击才切换;右键「下一首播放」插播
# ---------------------------------------------------------------------------


def _load_table_playlist(
    window, monkeypatch, scheduled, playlist_id: int = 11, count: int = 2
) -> None:
    """点击侧栏歌单把歌曲装进表格(client 已猴补,不触网)。"""
    monkeypatch.setattr(
        window._client,
        "get_playlist_tracks",
        lambda _pid: [
            NeteaseSong(id=i, title=f"歌{i}", artist=f"歌手{i}", duration_ms=60000)
            for i in range(count)
        ],
    )
    window._account = NeteaseAccount(user_id=1, nickname="测试")
    window._playlists = [
        NeteasePlaylist(id=playlist_id, name="列表B", track_count=count)
    ]
    window._rebuild_sidebar()
    item = window.sidebar.topLevelItem(0).child(0)
    window._on_sidebar_item_clicked(item, 0)
    assert scheduled, "点击歌单应调度一次后台加载"
    fetch, on_done, _on_error = scheduled[-1]
    on_done(fetch())


class TestTableQueueDecoupling:
    """侧栏点击只填表格(浏览);双击表格才把该列表装入播放队列。"""

    def test_browse_keeps_queue_intact(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            songs_a = [_fail_song(i) for i in range(3)]
            window._queue.replace(songs_a)
            window._queue_context = _ListRef("netease-playlist", 1, "列表A")
            _load_table_playlist(window, monkeypatch, scheduled, playlist_id=11)
            assert window._table_context == _ListRef(
                "netease-playlist", 11, "列表B"
            )
            assert [s.id for s in window._queue.items()] == [0, 1, 2]  # 队列原样
            assert window.song_table.rowCount() == 2  # 表格是浏览的列表
        finally:
            _close(window)

    def test_double_click_other_list_switches_queue(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._queue.replace([_fail_song(i) for i in range(3)])
            window._queue_context = _ListRef("netease-playlist", 1, "列表A")
            _load_table_playlist(window, monkeypatch, scheduled, playlist_id=11)
            window._play_from_table(0)
            # 队列换成浏览的列表,从所点行起播
            assert [s.id for s in window._queue.items()] == [0, 1]
            assert window._queue.current_index() == 0
            assert window._queue_context == _ListRef("netease-playlist", 11, "列表B")
            assert window._queue_dirty is False
        finally:
            _close(window)

    def test_double_click_same_list_jumps_without_replace(
        self, qapp, monkeypatch, tmp_path
    ):
        """队列来源即当前表格且未被插播改写:双击等价纯 jump,不再整体替换。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _load_table_playlist(window, monkeypatch, scheduled, playlist_id=11)
            window._play_from_table(0)  # 装入队列起播
            changed: list[str] = []
            window._queue.queue_changed.connect(lambda: changed.append("x"))
            window._play_from_table(1)  # 同列表双击另一行
            assert changed == []  # 没有 replace
            assert window._queue.current_index() == 1
        finally:
            _close(window)

    def test_double_click_after_insert_rebuilds_from_table(
        self, qapp, monkeypatch, tmp_path
    ):
        """插播改写队列后(_queue_dirty),同列表双击也按表格内容重装。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _load_table_playlist(window, monkeypatch, scheduled, playlist_id=11)
            window._play_from_table(0)
            window._insert_next_from_table(0)  # 插播表格第 0 行
            assert window._queue_dirty is True
            window._play_from_table(1)  # 重装:插播条目让位给净表内容
            assert window._queue_dirty is False
            assert [s.id for s in window._queue.items()] == [0, 1]
            assert window._queue.current_index() == 1
        finally:
            _close(window)


class TestPlayNextInsertUI:
    """右键「下一首播放」:表格行插入队列当前曲之后,不动其余部分。"""

    def test_insert_next_puts_song_after_current(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._queue.replace([_fail_song(i) for i in range(3)])
            window._queue.jump(1)
            window._queue_context = _ListRef("netease-playlist", 1, "列表A")
            _load_table_playlist(window, monkeypatch, scheduled, playlist_id=11)
            window._insert_next_from_table(0)
            assert window._queue.item_at(2) is window._table_songs[0]
            assert [s.id for s in window._queue.items()] == [0, 1, 0, 2]
            assert window._queue.current_index() == 1  # 当前曲不动
            assert window._queue.peek_next() == 2  # 下一首是插播曲
            assert window._queue_dirty is True
            assert "下一首播放" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_insert_twice_keeps_click_order(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._queue.replace([_fail_song(i) for i in range(2)])
            window._queue.jump(0)
            _load_table_playlist(window, monkeypatch, scheduled, playlist_id=11)
            window._insert_next_from_table(0)
            window._insert_next_from_table(1)
            # 当前曲是索引 0:两首插播依次落在 1、2,按点击顺序
            second = window._queue.item_at(1)
            assert second is not None and second.title == "歌0"
            third = window._queue.item_at(2)
            assert third is not None and third.title == "歌1"
            assert window._queue.advance_ended() == 1  # 先接播第一首插播
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# 「最近」分区:起播压栈 / 去重置顶 / 上限 / 点击加载 / 持久化
# ---------------------------------------------------------------------------


class TestRecentSection:
    def _start(self, window, ref) -> None:
        window._queue_context = ref
        window._start_play(_fail_song(1), BackupUrlRotator(["http://example/a"]), None)

    def test_start_play_pushes_queue_list(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            ref = _ListRef("netease-playlist", 7, "歌单七")
            self._start(window, ref)
            assert window._recent == [ref]
            # 「最近」分区出现在侧栏最顶,子节点带来源 payload
            header = window.sidebar.topLevelItem(0)
            assert header.data(0, _ROLE) == ("recent-header", None)
            assert header.child(0).data(0, _ROLE) == (
                "recent-item", ("netease-playlist", 7, "歌单七"),
            )
            assert header.child(0).text(0) == "歌单七"
            # 落盘
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["recent_lists"] == [
                {"kind": "netease-playlist", "id": 7, "title": "歌单七"}
            ]
        finally:
            _close(window)

    def test_browse_only_never_pushes(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _load_table_playlist(window, monkeypatch, scheduled, playlist_id=11)
            assert window._recent == []  # 只是浏览,没播放
            assert window.sidebar.topLevelItem(0).data(0, _ROLE) == (
                "netease-header", None,
            )  # 「最近」分区不出现
        finally:
            _close(window)

    def test_dedupe_moves_to_top(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            self._start(window, _ListRef("netease-playlist", 1, "A"))
            self._start(window, _ListRef("bili-folder", 9, "B"))
            self._start(window, _ListRef("netease-playlist", 1, "A改名"))
            assert window._recent == [
                _ListRef("netease-playlist", 1, "A改名"),  # 置顶并刷新标题
                _ListRef("bili-folder", 9, "B"),
            ]
        finally:
            _close(window)

    def test_cap_trims_to_setting(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        (tmp_path / "settings.json").write_text(
            json.dumps({"recent_max": 3}), encoding="utf-8"
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            for i in range(5):
                self._start(window, _ListRef("netease-playlist", i, f"歌单{i}"))
            assert [(r.kind, r.id) for r in window._recent] == [
                ("netease-playlist", 4),
                ("netease-playlist", 3),
                ("netease-playlist", 2),
            ]
        finally:
            _close(window)

    def test_repeat_push_no_sidebar_rebuild(self, qapp, monkeypatch, tmp_path):
        """栈顶已是同一列表(连播场景):内容无变化不重建侧栏。"""
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            ref = _ListRef("netease-playlist", 1, "A")
            self._start(window, ref)
            rebuilds: list[None] = []
            monkeypatch.setattr(
                window, "_rebuild_sidebar", lambda: rebuilds.append(None)
            )
            self._start(window, ref)  # 同列表下一首
            assert rebuilds == []
            assert window._recent == [ref]
        finally:
            _close(window)

    def test_recent_item_click_loads_list(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            self._start(window, _ListRef("netease-playlist", 11, "列表B"))
            _load_table_playlist(window, monkeypatch, scheduled, playlist_id=11)
            # 最近分区的条目重新点击仍能加载(与原生条目同一分发)
            recent_child = window.sidebar.topLevelItem(0).child(0)
            assert recent_child.data(0, _ROLE) == (
                "recent-item", ("netease-playlist", 11, "列表B"),
            )
            scheduled.clear()
            window._on_sidebar_item_clicked(recent_child, 0)
            assert window.central_stack.currentIndex() == 1
            assert len(scheduled) == 1
            fetch, on_done, _on_error = scheduled[0]
            on_done(fetch())
            assert window.song_table.rowCount() == 2
            assert "列表B" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_recent_survives_restart(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            self._start(window, _ListRef("bili-watchlater", 0, "稍后再看"))
        finally:
            _close(window)
        window2 = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window2._recent == [_ListRef("bili-watchlater", 0, "稍后再看")]
            header = window2.sidebar.topLevelItem(0)
            assert header.data(0, _ROLE) == ("recent-header", None)
            assert header.child(0).text(0) == "稍后再看"
        finally:
            _close(window2)


# ---------------------------------------------------------------------------
# 网易云每日推荐:收藏分区固定首位 + 点击加载
# ---------------------------------------------------------------------------


class TestDailyRecommendSection:
    def _daily_item(self, window):
        window._account = NeteaseAccount(user_id=1, nickname="测试")
        window._rebuild_sidebar()
        subscribed = window.sidebar.topLevelItem(1)
        assert subscribed.data(0, _ROLE) == ("netease-subscribed-header", None)
        item = subscribed.child(0)
        assert item.data(0, _ROLE) == ("netease-daily", None)
        return item

    def test_daily_click_loads_table(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(
                window._client,
                "get_daily_recommended_songs",
                lambda: [
                    NeteaseSong(id=1, title="日推歌", artist="歌手", duration_ms=60000),
                    NeteaseSong(id=2, title="日推歌2", artist="歌手", duration_ms=60000),
                ],
            )
            item = self._daily_item(window)
            window._on_sidebar_item_clicked(item, 0)
            assert window.central_stack.currentIndex() == 1
            assert len(scheduled) == 1
            fetch, on_done, _on_error = scheduled[0]
            on_done(fetch())
            assert window.song_table.rowCount() == 2
            assert window._table_context == _ListRef(
                "netease-daily", 0, "每日推荐"
            )
            assert "每日推荐" in window.statusBar().currentMessage()
        finally:
            _close(window)

    def test_daily_error_shows_status(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            item = self._daily_item(window)
            window._on_sidebar_item_clicked(item, 0)
            _fetch, _on_done, on_error = scheduled[0]
            on_error("登录态已失效")
            assert "加载每日推荐失败" in window.statusBar().currentMessage()
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# 设置页:最近列表数量
# ---------------------------------------------------------------------------


class TestRecentMaxSetting:
    def test_spin_change_trims_and_persists(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            for i in range(5):
                window._queue_context = _ListRef("netease-playlist", i, f"歌单{i}")
                window._start_play(
                    _fail_song(1), BackupUrlRotator(["http://example/a"]), None
                )
            assert len(window._recent) == 5  # 默认上限 8 不截断
            # 经设置页控件驱动(信号 → MainWindow 落盘并裁剪)
            window.settings_page.recent_max_spin.setValue(2)
            assert len(window._recent) == 2
            assert window.settings_page.recent_max_spin.value() == 2
            data = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert data["recent_max"] == 2
            assert len(data["recent_lists"]) == 2
        finally:
            _close(window)

    def test_spin_defaults_to_8(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window.settings_page.recent_max_spin.value() == 8
        finally:
            _close(window)
