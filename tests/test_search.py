"""双源搜索:cloudsearch / wbi search-type 解析器与主窗口搜索接线。

- 解析器:网易云 result.songs[](ar/al/dt 复用歌单解析,B 站 <em> 剥离、
  mm:ss 转秒、type 过滤、numPages 分页判定);
- 接线:侧栏「搜索」置顶入口 → 页头(输入框+来源栏) → 回车搜/切来源
  重搜/加载更多追加 → 结果落现有歌曲表(表格上下文 kind=search);
- 退出与回放:浏览列表即退出搜索模式;起播搜索结果压栈「最近」,
  「最近」里的搜索条目可剥前缀重搜。
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from neriplayer_win.api.bili import BiliApiError, BiliClient, BiliFavItem
from neriplayer_win.api.bili.client import parse_search_video_response
from neriplayer_win.api.netease import NeteaseApiError, NeteaseClient, NeteaseSong
from neriplayer_win.player.queue import BackupUrlRotator, QueueSong
from neriplayer_win.ui import main_window as main_window_module
from neriplayer_win.ui.main_window import (
    _ListRef,
    MainWindow,
)

_ROLE = Qt.ItemDataRole.UserRole


def _netease_song(n: int, cover: str = "") -> NeteaseSong:
    return NeteaseSong(
        id=n, title=f"歌{n}", artist=f"歌手{n}", duration_ms=60000, cover_url=cover
    )


# ---------------------------------------------------------------------------
# 解析器(纯函数 / mock 客户端,零网络)
# ---------------------------------------------------------------------------


def _netease_search_raw() -> str:
    return json.dumps(
        {
            "code": 200,
            "result": {
                "songCount": 41,
                "songs": [
                    {
                        "id": 1,
                        "name": "晴天",
                        "ar": [{"name": "周杰伦"}],
                        "al": {"id": 7, "picUrl": "http://p1.music.126.net/x.jpg"},
                        "dt": 269000,
                    },
                    {
                        "id": 2,
                        "name": "七里香",
                        # 老字段回退链:artists/album/duration
                        "artists": [{"name": "A"}, {"name": "B"}],
                        "album": {"picUrl": "http://p2.music.126.net/y.jpg"},
                        "duration": 299000,
                    },
                    {"id": 0, "name": "坏条目"},
                    {"id": 3, "name": ""},
                ],
            },
        }
    )


class TestNeteaseSearchParser:
    def test_parses_songs_and_total(self, monkeypatch):
        client = NeteaseClient()
        monkeypatch.setattr(client, "search_songs_raw", lambda *a, **k: _netease_search_raw())
        songs, total = client.search_songs("周")
        assert total == 41
        assert [song.id for song in songs] == [1, 2]  # id<=0/空名跳过
        assert songs[0].title == "晴天"
        assert songs[0].artist == "周杰伦"
        assert songs[0].cover_url == "http://p1.music.126.net/x.jpg"
        assert songs[0].duration_ms == 269000
        assert songs[1].artist == "A / B"
        assert songs[1].duration_ms == 299000

    def test_bad_code_raises(self, monkeypatch):
        client = NeteaseClient()
        monkeypatch.setattr(
            client, "search_songs_raw", lambda *a, **k: json.dumps({"code": 301})
        )
        with pytest.raises(NeteaseApiError):
            client.search_songs("周")

    def test_missing_result_raises(self, monkeypatch):
        client = NeteaseClient()
        monkeypatch.setattr(
            client, "search_songs_raw", lambda *a, **k: json.dumps({"code": 200})
        )
        with pytest.raises(NeteaseApiError):
            client.search_songs("周")


def _bili_search_root() -> dict:
    return {
        "code": 0,
        "data": {
            "numPages": 3,
            "result": [
                {
                    "type": "video",
                    "aid": 11,
                    "bvid": "BV1a",
                    "title": "『<em class=\"keyword\">周</em>杰伦》&amp;朋友",
                    "author": "UP主",
                    "pic": "//i2.hdslb.com/bfs/x.jpg",
                    "duration": "4:29",
                },
                {"type": "special", "aid": 12, "bvid": "BV1b", "title": "特殊卡"},
                {
                    "type": "video",
                    "aid": 13,
                    "bvid": "",
                    "title": "无bvid视频",
                    "duration": "1:01:02",
                },
            ],
        },
    }


class TestBiliSearchParser:
    def test_filters_strips_and_converts(self):
        items, has_more = parse_search_video_response(_bili_search_root(), page=1)
        assert has_more is True  # 1 < numPages 3
        assert [item.id for item in items] == [11, 13]  # 非 video 类型过滤
        first = items[0]
        assert first.title == "『周杰伦》&朋友"  # <em> 剥离 + 实体还原
        assert first.cover_url == "https://i2.hdslb.com/bfs/x.jpg"  # 强制 https
        assert first.duration_sec == 269
        assert first.upper_name == "UP主"
        assert items[1].bvid is None
        assert items[1].duration_sec == 3662  # h:mm:ss = 1 小时 1 分 2 秒

    def test_last_page_and_missing_numpages_fallback(self):
        root = _bili_search_root()
        _, has_more = parse_search_video_response(root, page=3)
        assert has_more is False  # 3 < 3 不成立
        del root["data"]["numPages"]
        items, has_more = parse_search_video_response(root, page=9)
        assert has_more is True  # 缺省回退:本页非空粗判

    def test_client_error_raises(self, monkeypatch):
        client = BiliClient()
        monkeypatch.setattr(
            client,
            "search_videos_raw",
            lambda *a, **k: json.dumps({"code": -400, "message": "请求错误"}),
        )
        with pytest.raises(BiliApiError):
            client.search_videos("周")


# ---------------------------------------------------------------------------
# 主窗口接线
# ---------------------------------------------------------------------------


def _make_window(qapp, monkeypatch, tmp_path) -> MainWindow:
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window


def _close(window) -> None:
    window._force_exit = True
    window.close()


def _capture_async(monkeypatch) -> list[tuple]:
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        main_window_module,
        "run_async",
        lambda fn, on_done=None, on_error=None: scheduled.append(
            (fn, on_done, on_error)
        ),
    )
    return scheduled


def _mock_netease_search(monkeypatch, window, songs, total):
    monkeypatch.setattr(
        window._client, "search_songs", lambda keyword, limit=30, offset=0: (songs, total)
    )


def _mock_bili_search(monkeypatch, window, items, has_more):
    monkeypatch.setattr(
        window._bili_client, "search_videos", lambda keyword, page=1: (items, has_more)
    )


class _StubEngine:
    """只记录调用不真播;closeEvent 的 stop 也一并兜住。"""

    def play_url(self, url, headers=None) -> None:
        pass

    def stop(self) -> None:
        pass


def _open_search(window) -> None:
    window._on_sidebar_item_clicked(window.sidebar.topLevelItem(0), 0)


class TestSearchEntry:
    def test_search_is_first_item_with_icon(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            first = window.sidebar.topLevelItem(0)
            assert first is not None
            assert first.text(0) == "搜索"
            assert first.data(0, _ROLE) == ("search", None)
            assert not first.icon(0).isNull()  # 放大镜图标
        finally:
            _close(window)


class TestSearchPageWiring:
    def test_open_shows_header_with_idle_hint(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_search(window)
            assert window.central_stack.currentIndex() == 1
            assert window._search_header.isVisibleTo(window)
            assert window._search_input.placeholderText() == "搜索关键词"
            assert window._search_input.height() == 64  # 两行高
            assert window._search_netease_btn.height() == 64
            assert window.table_stack.currentIndex() == 1  # 空态
            assert scheduled == []  # 进页面不自动搜
        finally:
            _close(window)

    def test_return_pressed_fills_table(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_search(window)
            _mock_netease_search(
                monkeypatch, window, [_netease_song(1, "http://img/a"), _netease_song(2)], 5
            )
            window._search_input.setText("周")
            window._on_search_return_pressed()
            assert len(scheduled) == 1
            fetch, on_done, _on_error = scheduled[0]
            on_done(fetch())
            assert window.song_table.rowCount() == 2
            assert window._table_context == _ListRef("search", 0, "搜索:周")
            assert window._table_header_source == "netease"
            assert window._table_songs[0].cover_url == "http://img/a"  # 缩略图可用
            assert not window._search_more_btn.isVisibleTo(window)  # 2*30 >= 5
        finally:
            _close(window)

    def test_more_button_visible_when_total_exceeds_page(
        self, qapp, monkeypatch, tmp_path
    ):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_search(window)
            _mock_netease_search(monkeypatch, window, [_netease_song(1)], 31)
            window._search_input.setText("周")
            window._on_search_return_pressed()
            fetch, on_done, _on_error = scheduled[0]
            on_done(fetch())
            assert window._search_more_btn.isVisibleTo(window)  # 30 < 31
        finally:
            _close(window)

    def test_source_switch_reruns_with_bili(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_search(window)
            _mock_netease_search(monkeypatch, window, [_netease_song(1)], 1)
            window._search_input.setText("周")
            window._on_search_return_pressed()
            on_done = scheduled[0][1]
            on_done(([_netease_song(1)], 1))
            # 切到 B站:同关键词重搜,结果换表头且过滤不可播条目
            item = BiliFavItem(
                type=2, id=21, bvid="BV1a", title="视频A",
                duration_sec=60, upper_name="UP",
            )
            unplayable = BiliFavItem(type=21, id=22, bvid=None, title="合集B")
            _mock_bili_search(monkeypatch, window, [item, unplayable], False)
            window._on_search_source_changed("bili")
            assert len(scheduled) == 2
            fetch, on_done, _on_error = scheduled[1]
            on_done(fetch())
            assert window.song_table.rowCount() == 1
            assert window._table_header_source == "bili"
            assert window._table_songs[0].bvid == "BV1a"
        finally:
            _close(window)

    def test_load_more_appends(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_search(window)
            pages = {
                1: ([_netease_song(1), _netease_song(2)], 90),
                2: ([_netease_song(3), _netease_song(4)], 90),
            }
            monkeypatch.setattr(
                window._client,
                "search_songs",
                lambda keyword, limit=30, offset=0: pages[offset // 30 + 1],
            )
            window._search_input.setText("周")
            window._on_search_return_pressed()
            scheduled[0][1](scheduled[0][0]())
            assert window.song_table.rowCount() == 2
            window._on_search_more_clicked()
            assert len(scheduled) == 2
            scheduled[1][1](scheduled[1][0]())
            assert window.song_table.rowCount() == 4  # 追加不清前页
            assert [song.id for song in window._table_songs] == [1, 2, 3, 4]
        finally:
            _close(window)

    def test_search_error_keeps_hint(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_search(window)
            window._search_input.setText("周")
            window._on_search_return_pressed()
            _fetch, _on_done, on_error = scheduled[0]
            on_error("网络超时")
            assert "搜索失败" in window.statusBar().currentMessage()
            assert window.table_stack.currentIndex() == 1  # 仍空态
        finally:
            _close(window)

    def test_source_indicator_slides_to_selection(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_search(window)
            QTest.qWait(50)
            indicator = window._source_indicator
            bar = window._source_bar
            # 初始:指示线在顶部、贴网易云(左半)
            assert indicator.y() == 0 and indicator.height() == 3
            center = indicator.x() + indicator.width() / 2
            assert center < bar.width() / 2
            window._on_search_source_changed("bili")
            QTest.qWait(400)  # 等滑动动画(250ms)结束
            center = indicator.x() + indicator.width() / 2
            assert center > bar.width() / 2  # 滑到 B站(右半)
            assert window._search_bili_btn.isChecked()
            assert not window._search_netease_btn.isChecked()
        finally:
            _close(window)

    def test_browse_exits_search_mode(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _open_search(window)
            assert window._search_header.isVisibleTo(window)
            window._open_list_ref(_ListRef("netease-playlist", 11, "列表B"))
            assert not window._search_header.isVisibleTo(window)
            assert not window._search_more_btn.isVisibleTo(window)
        finally:
            _close(window)


class TestSearchRecent:
    def _start_play(self, window, ref) -> None:
        window._queue_context = ref
        song = QueueSong(
            source="netease", id=1, bvid="", title="歌", artist="歌手", duration_ms=60000
        )
        window._start_play(song, BackupUrlRotator(["http://example/a"]), None)

    def test_playing_search_result_pushes_recent(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            self._start_play(window, _ListRef("search", 0, "搜索:周"))
            assert window._recent[0] == _ListRef("search", 0, "搜索:周")
            # 侧栏「最近」出现搜索条目
            recent_header = window.sidebar.find_header("recent")
            assert recent_header is not None
            assert recent_header.child(0).text(0) == "搜索:周"
        finally:
            _close(window)

    def test_recent_search_entry_reruns_keyword(self, qapp, monkeypatch, tmp_path):
        scheduled = _capture_async(monkeypatch)
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _mock_netease_search(monkeypatch, window, [_netease_song(7)], 1)
            window._open_list_ref(_ListRef("search", 0, "搜索:周"))
            assert window._search_header.isVisibleTo(window)
            assert window._search_input.text() == "周"  # 前缀剥掉还原关键词
            assert len(scheduled) == 1
            scheduled[0][1](scheduled[0][0]())
            assert window.song_table.rowCount() == 1
        finally:
            _close(window)
