"""酷狗音源接入的接线测试(第三音源,不联网)。

重点覆盖 M6 调研里标出的三个高风险点
(docs/research/M6-integration-points.md §2.1):

1. `_resolve_song_context` 的 else 兜底会把酷狗歌当网易云处理
2. `_resolve_and_start` 的 else 兜底同上
3. 鉴权异常只捕获两种,酷狗登录失效会被当成普通播放失败

以及侧栏分区、三源搜索、凭据两链落盘等接线。
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from neriplayer_win.api.kugou import (
    KugouAuthRequiredError,
    KugouClient,
    KugouCredentials,
    KugouPlaylist,
    KugouSong,
)
from neriplayer_win.player.queue import QueueSong
from neriplayer_win.ui import main_window as main_window_module
from neriplayer_win.ui.main_window import (
    MainWindow,
    _kugou_song_from_queue,
    _kugou_song_to_queue,
)

_ROLE = Qt.ItemDataRole.UserRole

def _make_window(qapp, monkeypatch, tmp_path) -> MainWindow:
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window

def _close(window) -> None:
    window._force_exit = True
    window.close()

def _kugou_playlist(listid: int, name: str, count: int = 10) -> KugouPlaylist:
    return KugouPlaylist(
        listid=listid,
        global_collection_id=f"collection_3_1_{listid}_0",
        name=name,
        count=count,
    )

def _kugou_song() -> KugouSong:
    return KugouSong(
        hash="HASH123",
        title="酷狗歌",
        artist="酷狗歌手",
        duration_ms=200_000,
        encode_album_audio_id="emix456",
        album_id="789",
        cover_url="http://imge.kugou.com/x.jpg",
    )

class TestSongConversion:
    """酷狗歌曲标识是字符串,必须能无损地往返 QueueSong。"""

    def test_roundtrip_preserves_identifiers(self) -> None:
        original = _kugou_song()
        restored = _kugou_song_from_queue(_kugou_song_to_queue(original))
        assert restored.hash == original.hash
        assert restored.encode_album_audio_id == original.encode_album_audio_id
        assert restored.album_id == original.album_id
        assert restored.title == original.title
        assert restored.duration_ms == original.duration_ms

    def test_queue_song_uses_zero_id_and_encodes_bvid(self) -> None:
        """id 字段是 int,酷狗字符串标识全部编码进 bvid。"""
        queued = _kugou_song_to_queue(_kugou_song())
        assert queued.source == "kugou"
        assert queued.id == 0
        assert queued.bvid == "HASH123|emix456|789"

    def test_missing_parts_tolerated(self) -> None:
        """标识缺项(如只有 hash)反解时不能崩。"""
        queued = QueueSong(
            source="kugou", id=0, bvid="ONLYHASH",
            title="t", artist="a", duration_ms=1000,
        )
        restored = _kugou_song_from_queue(queued)
        assert restored.hash == "ONLYHASH"
        assert restored.encode_album_audio_id == ""
        assert restored.album_id == ""

class TestSidebarSection:
    def test_kugou_section_logged_out(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            tree = window.sidebar
            kinds = [
                tree.topLevelItem(i).data(0, _ROLE)[0]
                for i in range(tree.topLevelItemCount())
            ]
            assert "kugou-header" in kinds
            header = next(
                tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).data(0, _ROLE)[0] == "kugou-header"
            )
            assert header.childCount() == 1
            assert header.child(0).data(0, _ROLE) == ("kugou-login", None)
        finally:
            _close(window)

    def test_kugou_section_lists_playlists(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._kugou_account = object()  # 只要有账号即视为已登录
            window._kugou_playlists = [
                _kugou_playlist(1, "我喜欢", 525),
                _kugou_playlist(2, "睡", 45),
            ]
            window._rebuild_sidebar()
            header = next(
                window.sidebar.topLevelItem(i)
                for i in range(window.sidebar.topLevelItemCount())
                if window.sidebar.topLevelItem(i).data(0, _ROLE)[0] == "kugou-header"
            )
            assert header.childCount() == 2
            # 侧栏展示带计数
            assert header.child(0).text(0) == "我喜欢(525)"
            assert header.child(0).data(0, _ROLE) == ("kugou-playlist", 1)
        finally:
            _close(window)

    def test_header_icon_does_not_raise(self, qapp, monkeypatch, tmp_path) -> None:
        """分区头图标不能走品牌色查表(会 KeyError),必须用单色标。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            header = next(
                window.sidebar.topLevelItem(i)
                for i in range(window.sidebar.topLevelItemCount())
                if window.sidebar.topLevelItem(i).data(0, _ROLE)[0] == "kugou-header"
            )
            assert not header.icon(0).isNull()
        finally:
            _close(window)

class TestResolveDispatch:
    """最高风险区:酷狗歌绝不能被当成网易云处理。"""

    def test_kugou_does_not_call_netease(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            called = {"netease": 0}

            def boom(*_a, **_k):
                called["netease"] += 1
                raise AssertionError("酷狗歌被当成网易云处理了")

            monkeypatch.setattr(window._client, "resolve_playable_url", boom)
            sentinel = object()
            monkeypatch.setattr(
                window, "_resolve_kugou_context", lambda song, quality: sentinel
            )
            queued = _kugou_song_to_queue(_kugou_song())
            assert window._resolve_song_context(queued) is sentinel
            assert called["netease"] == 0
        finally:
            _close(window)

    def test_unknown_source_raises(self, qapp, monkeypatch, tmp_path) -> None:
        """未知 source 要显式报错,不能静默走网易云兜底。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            song = QueueSong(
                source="unknown", id=1, bvid="",
                title="t", artist="a", duration_ms=1000,
            )
            with pytest.raises(ValueError, match="未知音源"):
                window._resolve_song_context(song)
        finally:
            _close(window)

    def test_kugou_context_builds_rotator(self, qapp, monkeypatch, tmp_path) -> None:
        """解析成功要带上主 URL 与备用 URL(酷狗返回两个 CDN 域名)。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            from neriplayer_win.api.kugou import KugouPlayableUrl

            monkeypatch.setattr(
                window._kugou_client,
                "resolve_playable_url",
                lambda song, preferred_quality="lossless": KugouPlayableUrl(
                    url="https://webfs.kugou.com/a.mp3",
                    backup_url="https://webfs.tx.kugou.com/a.mp3",
                    bitrate_kbps=320,
                    quality_key="HQFileHash",
                ),
            )
            queued = _kugou_song_to_queue(_kugou_song())
            context = window._resolve_song_context(queued)
            assert context.rotator.current == "https://webfs.kugou.com/a.mp3"
            assert len(context.rotator) == 2
            assert "320k" in context.status
        finally:
            _close(window)

class TestAuthErrorHandling:
    def test_kugou_auth_error_maps_to_auth_kind(self, qapp, monkeypatch, tmp_path) -> None:
        """酷狗鉴权异常必须归到 "auth",否则会走自动跳歌而不是提示重登。"""
        scheduled: list[tuple] = []
        monkeypatch.setattr(
            main_window_module, "run_async",
            lambda fn, on_done=None, on_error=None: scheduled.append((fn, on_done, on_error)),
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            def raise_auth(_song):
                raise KugouAuthRequiredError("expired")

            monkeypatch.setattr(window, "_resolve_song_context", raise_auth)
            queued = _kugou_song_to_queue(_kugou_song())
            window._resolve_and_start(queued, window._resolve_generation)
            assert scheduled
            fn, _on_done, _on_error = scheduled[-1]
            kind, payload = fn()
            assert kind == "auth"
            assert payload == ""
        finally:
            _close(window)

    def test_kugou_play_done_handles_auth(self, qapp, monkeypatch, tmp_path) -> None:
        """auth 结果走酷狗自己的 stale 处理,不误用网易云/B站那套。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            handled: list[str] = []
            monkeypatch.setattr(
                window, "_handle_kugou_stale_login", lambda msg: handled.append(msg)
            )
            queued = _kugou_song_to_queue(_kugou_song())
            on_done = window._make_kugou_play_done(queued, window._resolve_generation)
            on_done(("auth", ""))
            assert len(handled) == 1
        finally:
            _close(window)

class TestThreeSourceSearch:
    def test_search_dispatches_to_kugou(self, qapp, monkeypatch, tmp_path) -> None:
        scheduled: list[tuple] = []
        monkeypatch.setattr(
            main_window_module, "run_async",
            lambda fn, on_done=None, on_error=None: scheduled.append((fn, on_done, on_error)),
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            called = {"kugou": 0, "netease": 0}

            def kugou_search(keyword, limit=30, offset=0):
                called["kugou"] += 1
                return ([_kugou_song()], 1)

            def netease_search(*_a, **_k):
                called["netease"] += 1
                return ([], 0)

            monkeypatch.setattr(window._kugou_client, "search_songs", kugou_search)
            monkeypatch.setattr(window._client, "search_songs", netease_search)
            window._search_source = "kugou"
            window._run_search("关键词", reset=True)
            assert scheduled
            fn, on_done, _on_error = scheduled[-1]
            on_done(fn())
            assert called["kugou"] == 1
            assert called["netease"] == 0
            assert [song.source for song in window._table_songs] == ["kugou"]
        finally:
            _close(window)

    def test_source_buttons_are_exclusive(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._on_search_source_changed("kugou")
            assert window._search_kugou_btn.isChecked()
            assert not window._search_bili_btn.isChecked()
            assert not window._search_netease_btn.isChecked()
        finally:
            _close(window)

class TestCredentialPersistence:
    """两条凭据链必须都能落盘与恢复(缺任一条只影响对应功能)。"""

    def test_both_chains_roundtrip(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._store.save_kugou({
                "userid": "1", "token": "tok", "t1": "t1v",
                "t_expire_time": 1_800_000_000, "nickname": "n",
                "mid": "m" * 32, "dfid": "d" * 24,
                "kugoo_cookie": "KugooID=1&KugooPwd=AB&t=old",
            })
            bundle = window._store.load_kugou()
            assert bundle is not None
            assert bundle["token"] == "tok"
            assert bundle["kugoo_cookie"] == "KugooID=1&KugooPwd=AB&t=old"
            assert bundle["t_expire_time"] == 1_800_000_000
        finally:
            _close(window)

    def test_web_only_credentials_are_enough_for_playback(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        """只有网页凭据(KuGoo 串)也算已登录,播放仍可用。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._store.save_kugou({
                "userid": "1", "kugoo_cookie": "KugooID=1&KugooPwd=AB&t=old",
            })
            bundle = window._store.load_kugou()
            assert bundle is not None
            assert bundle["token"] == ""
            assert bundle["kugoo_cookie"]
        finally:
            _close(window)

    def test_boot_restores_credentials(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._store.save_kugou({
                "userid": "42", "token": "tok", "nickname": "牛马",
                "mid": "m" * 32, "dfid": "d" * 24,
                "kugoo_cookie": "KugooID=42&KugooPwd=AB&t=old",
                "t_expire_time": 1_800_000_000,
            })
            # 换一个干净 client 再走启动恢复
            window._kugou_client = KugouClient()
            monkeypatch.setattr(main_window_module, "run_async", lambda *a, **k: None)
            window._boot_kugou_from_store()
            credentials = window._kugou_client.credentials()
            assert credentials.userid == "42"
            assert credentials.token == "tok"
            assert credentials.has_web_login
        finally:
            _close(window)

class TestLocalPlaylistWithKugou:
    """酷狗歌 id 恒为 0,本地歌单去重必须靠 bvid 区分。"""

    def test_two_kugou_songs_are_not_duplicates(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            playlist = window._create_local_playlist("测试")
            assert playlist is not None
            first = QueueSong(
                source="kugou", id=0, bvid="HASH_A||",
                title="歌A", artist="人", duration_ms=1000,
            )
            second = QueueSong(
                source="kugou", id=0, bvid="HASH_B||",
                title="歌B", artist="人", duration_ms=1000,
            )
            assert window._add_song_to_local_playlist(playlist.id, first) is True
            # 第二首 id 也是 0,但 hash 不同,必须能加进去
            assert window._add_song_to_local_playlist(playlist.id, second) is True
            stored = window._find_local_playlist(playlist.id)
            assert stored is not None
            assert len(stored.items) == 2
        finally:
            _close(window)

    def test_same_kugou_song_rejected(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            playlist = window._create_local_playlist("测试")
            assert playlist is not None
            song = QueueSong(
                source="kugou", id=0, bvid="HASH_A||",
                title="歌A", artist="人", duration_ms=1000,
            )
            assert window._add_song_to_local_playlist(playlist.id, song) is True
            assert window._add_song_to_local_playlist(playlist.id, song) is False
            stored = window._find_local_playlist(playlist.id)
            assert stored is not None and len(stored.items) == 1
        finally:
            _close(window)

    def test_kugou_item_roundtrips_through_disk(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        """落盘再读回,bvid 标识必须还在(否则重启后去重失效)。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            playlist = window._create_local_playlist("测试")
            assert playlist is not None
            song = QueueSong(
                source="kugou", id=0, bvid="HASH_A|emix|album",
                title="歌A", artist="人", duration_ms=1000,
                cover_url="http://x/y.jpg",
            )
            window._add_song_to_local_playlist(playlist.id, song)
            loaded = window._store.load_local_playlists()
            assert len(loaded) == 1
            item = loaded[0].items[0]
            assert item.source == "kugou"
            assert item.extra["bvid"] == "HASH_A|emix|album"
            # 反解回 QueueSong 能还原播放标识
            restored = _kugou_song_from_queue(
                QueueSong(source=item.source, id=item.id, bvid=item.extra["bvid"],
                          title=item.title, artist=item.artist,
                          duration_ms=item.duration_ms)
            )
            assert restored.hash == "HASH_A"
            assert restored.encode_album_audio_id == "emix"
        finally:
            _close(window)
