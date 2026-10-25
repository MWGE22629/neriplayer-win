"""M7 本地歌单数据层:local_playlists.json 的读写、校验与封面优先级。

纯数据层,无 Qt。风格对照 tests/test_store.py 的 make_store 模式。
"""

from __future__ import annotations

import json
import os

from neriplayer_win.data.store import (
    LOCAL_PLAYLIST_MAX_ITEMS,
    LOCAL_PLAYLIST_NAME_MAX,
    LocalPlaylist,
    LocalPlaylistItem,
    LocalStore,
    local_playlist_cover_url,
    next_local_playlist_id,
)


def make_store(tmp_path, monkeypatch) -> LocalStore:
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    return LocalStore()


def _item(
    source: str = "netease",
    entry_id: int = 1,
    title: str = "歌",
    cover_url: str = "",
    extra: dict[str, str] | None = None,
) -> LocalPlaylistItem:
    return LocalPlaylistItem(
        source=source,
        id=entry_id,
        extra=extra or {},
        title=title,
        artist="歌手",
        duration_ms=60000,
        cover_url=cover_url,
    )


def _write(tmp_path, payload) -> None:
    (tmp_path / "local_playlists.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


class TestLoadDefaults:
    def test_no_file_returns_empty(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        assert store.load_local_playlists() == []
        assert store.local_playlists_path == tmp_path / "local_playlists.json"

    def test_corrupted_json_returns_empty(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.local_playlists_path.write_text("{broken", encoding="utf-8")
        assert store.load_local_playlists() == []

    def test_top_level_not_dict_returns_empty(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        for payload in ([], "text", 3, None):
            _write(tmp_path, payload)
            assert store.load_local_playlists() == []

    def test_missing_or_bad_version_returns_empty(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        playlist = LocalPlaylist(id=1, name="A", items=[]).to_dict()
        for version in (None, "1", True):
            payload = {"playlists": [playlist]}
            if version is not None:
                payload["version"] = version
            _write(tmp_path, payload)
            assert store.load_local_playlists() == [], version

    def test_zero_version_treated_as_one(self, tmp_path, monkeypatch):
        """version <= 0 视为 1(文档 2.2 表);只有非 int/bool 才判非法。"""
        store = make_store(tmp_path, monkeypatch)
        _write(
            tmp_path,
            {
                "version": 0,
                "playlists": [
                    {
                        "id": 1, "name": "A", "items": [],
                        "created_at": 0, "cover_url": "",
                    }
                ],
            },
        )
        assert [p.id for p in store.load_local_playlists()] == [1]

    def test_playlists_not_list_returns_empty(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        _write(tmp_path, {"version": 1, "playlists": {"1": {}}})
        assert store.load_local_playlists() == []


class TestRoundTrip:
    def test_two_playlists_all_fields(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        playlists = [
            LocalPlaylist(
                id=1,
                name="通勤",
                created_at=1761177600000,
                items=[
                    _item("netease", 101, "歌A", "http://a/1.jpg"),
                    _item(
                        "bili", 202, "视频B", "http://b/2.jpg",
                        extra={"bvid": "BV1xx"},
                    ),
                ],
            ),
            LocalPlaylist(
                id=3,
                name="深夜",
                created_at=1761177601000,
                cover_url="http://manual/cover.jpg",
                items=[],
            ),
        ]
        assert store.save_local_playlists(playlists) is True
        loaded = store.load_local_playlists()
        assert loaded == playlists

    def test_saved_file_carries_version_and_no_auto_cover(
        self, tmp_path, monkeypatch
    ):
        store = make_store(tmp_path, monkeypatch)
        playlist = LocalPlaylist(id=1, name="A", items=[_item(cover_url="http://x")])
        assert store.save_local_playlists([playlist]) is True
        data = json.loads(
            store.local_playlists_path.read_text(encoding="utf-8")
        )
        assert data["version"] == 1
        # 自动封面(首曲 cover_url)不写进歌单自身的 cover_url 字段
        assert data["playlists"][0]["cover_url"] == ""
        assert data["playlists"][0]["items"][0]["cover_url"] == "http://x"

    def test_separate_from_settings_file(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.save_settings({"appearance": "light"})
        store.save_local_playlists([LocalPlaylist(id=1, name="A")])
        settings = json.loads(store.settings_path.read_text(encoding="utf-8"))
        assert "playlists" not in settings
        assert store.local_playlists_path != store.settings_path
        assert store.load_settings()["appearance"] == "light"
        assert store.load_local_playlists()[0].name == "A"


class TestPlaylistValidation:
    def test_invalid_playlists_dropped(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        _write(
            tmp_path,
            {
                "version": 1,
                "playlists": [
                    {"name": "缺 id", "items": [], "created_at": 0, "cover_url": ""},
                    {
                        "id": True, "name": "bool id", "items": [],
                        "created_at": 0, "cover_url": "",
                    },
                    {
                        "id": 0, "name": "零 id", "items": [],
                        "created_at": 0, "cover_url": "",
                    },
                    {
                        "id": 5, "name": "   ", "items": [],
                        "created_at": 0, "cover_url": "",
                    },
                    {
                        "id": 6, "name": "items 非 list", "items": {},
                        "created_at": 0, "cover_url": "",
                    },
                    {
                        "id": 7, "name": "缺 created_at", "items": [],
                        "cover_url": "",
                    },
                    {
                        "id": 8, "name": "cover_url 非 str", "items": [],
                        "created_at": 0, "cover_url": 1,
                    },
                    "not-a-dict",
                    {
                        "id": 9, "name": "合法", "items": [],
                        "created_at": 123, "cover_url": "",
                    },
                ],
            },
        )
        loaded = store.load_local_playlists()
        assert [playlist.id for playlist in loaded] == [9]
        assert loaded[0].name == "合法"
        assert loaded[0].created_at == 123

    def test_duplicate_playlist_ids_keep_first(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        _write(
            tmp_path,
            {
                "version": 1,
                "playlists": [
                    {
                        "id": 2, "name": "首个", "items": [],
                        "created_at": 0, "cover_url": "",
                    },
                    {
                        "id": 2, "name": "重复", "items": [],
                        "created_at": 0, "cover_url": "",
                    },
                    {
                        "id": 3, "name": "别的", "items": [],
                        "created_at": 0, "cover_url": "",
                    },
                ],
            },
        )
        loaded = store.load_local_playlists()
        assert [(p.id, p.name) for p in loaded] == [(2, "首个"), (3, "别的")]

    def test_overlong_name_truncated(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        _write(
            tmp_path,
            {
                "version": 1,
                "playlists": [
                    {
                        "id": 1, "name": "名" * 300, "items": [],
                        "created_at": 0, "cover_url": "",
                    }
                ],
            },
        )
        loaded = store.load_local_playlists()
        assert len(loaded[0].name) == LOCAL_PLAYLIST_NAME_MAX

    def test_unknown_future_version_still_read(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        _write(
            tmp_path,
            {
                "version": 2,
                "future_key": {"x": 1},
                "playlists": [
                    {
                        "id": 4, "name": "未来版本", "items": [],
                        "created_at": 0, "cover_url": "", "unknown": 1,
                    }
                ],
            },
        )
        loaded = store.load_local_playlists()
        assert [(p.id, p.name) for p in loaded] == [(4, "未来版本")]


class TestItemValidation:
    def test_invalid_items_dropped(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        good = _item("netease", 1, "好歌").to_dict()
        bad_source = dict(good, source="youtube")
        bad_id_bool = dict(good, id=True)
        bad_id_neg = dict(good, id=-5)
        bad_extra = dict(good, extra={"bvid": 1})
        bad_extra_type = dict(good, extra=["x"])
        bad_title = dict(good, title=None)
        bad_duration = dict(good, duration_ms=-1)
        bad_duration_type = dict(good, duration_ms="60")
        bad_cover = dict(good, cover_url=None)
        _write(
            tmp_path,
            {
                "version": 1,
                "playlists": [
                    {
                        "id": 1, "name": "混合", "created_at": 0, "cover_url": "",
                        "items": [
                            good,
                            bad_source, bad_id_bool, bad_id_neg, bad_extra,
                            bad_extra_type, bad_title, bad_duration,
                            bad_duration_type, bad_cover, "not-a-dict",
                        ],
                    }
                ],
            },
        )
        loaded = store.load_local_playlists()
        assert [item.id for item in loaded[0].items] == [1]

    def test_duplicate_items_keep_first(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        _write(
            tmp_path,
            {
                "version": 1,
                "playlists": [
                    {
                        "id": 1, "name": "去重", "created_at": 0, "cover_url": "",
                        "items": [
                            _item("netease", 7, "首个").to_dict(),
                            _item("netease", 7, "重复").to_dict(),
                            _item("bili", 7, "同 id 异源").to_dict(),
                        ],
                    }
                ],
            },
        )
        items = store.load_local_playlists()[0].items
        assert [(i.source, i.id, i.title) for i in items] == [
            ("netease", 7, "首个"),
            ("bili", 7, "同 id 异源"),
        ]

    def test_item_cap_truncates(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        items = [
            _item("netease", i + 1, f"歌{i}").to_dict()
            for i in range(LOCAL_PLAYLIST_MAX_ITEMS + 5)
        ]
        _write(
            tmp_path,
            {
                "version": 1,
                "playlists": [
                    {
                        "id": 1, "name": "超长", "created_at": 0, "cover_url": "",
                        "items": items,
                    }
                ],
            },
        )
        assert len(store.load_local_playlists()[0].items) == LOCAL_PLAYLIST_MAX_ITEMS

    def test_bili_bvid_survives_in_extra(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        playlist = LocalPlaylist(
            id=1, name="B站",
            items=[_item("bili", 42, "视频", extra={"bvid": "BV1abc"})],
        )
        assert store.save_local_playlists([playlist]) is True
        loaded = store.load_local_playlists()[0].items[0]
        assert loaded.extra == {"bvid": "BV1abc"}


class TestAtomicWrite:
    def test_replace_failure_keeps_old_file_intact(
        self, tmp_path, monkeypatch
    ):
        store = make_store(tmp_path, monkeypatch)
        original = LocalPlaylist(id=1, name="原件", items=[_item()])
        assert store.save_local_playlists([original]) is True

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        assert store.save_local_playlists([LocalPlaylist(id=2, name="新件")]) is False
        loaded = store.load_local_playlists()
        assert [(p.id, p.name) for p in loaded] == [(1, "原件")]
        # 临时文件不留在目录里
        assert not list(tmp_path.glob("*.tmp"))

    def test_write_failure_returns_false(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        # 数据目录被一个同名文件占住:mkdir 必然失败
        blocker = tmp_path / "blocked"
        blocker.write_text("x", encoding="utf-8")
        blocked_store = LocalStore(blocker / "sub")
        assert blocked_store.save_local_playlists([]) is False


class TestCoverPrecedence:
    """手动封面优先于自动封面;自动值永不落盘。"""

    def test_empty_playlist_has_no_cover(self):
        assert local_playlist_cover_url(LocalPlaylist(id=1, name="空")) == ""

    def test_auto_cover_from_first_item(self):
        playlist = LocalPlaylist(
            id=1, name="A",
            items=[
                _item(cover_url="http://first"),
                _item(entry_id=2, cover_url="http://second"),
            ],
        )
        assert local_playlist_cover_url(playlist) == "http://first"

    def test_manual_cover_wins(self):
        playlist = LocalPlaylist(
            id=1, name="A",
            cover_url="http://manual",
            items=[_item(cover_url="http://first")],
        )
        assert local_playlist_cover_url(playlist) == "http://manual"

    def test_clearing_manual_falls_back_to_auto(self):
        playlist = LocalPlaylist(
            id=1, name="A",
            cover_url="http://manual",
            items=[_item(cover_url="http://first")],
        )
        playlist.cover_url = ""
        assert local_playlist_cover_url(playlist) == "http://first"

    def test_manual_survives_roundtrip(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        playlist = LocalPlaylist(
            id=1, name="A",
            cover_url="http://manual",
            items=[_item(cover_url="http://first")],
        )
        assert store.save_local_playlists([playlist]) is True
        loaded = store.load_local_playlists()[0]
        assert loaded.cover_url == "http://manual"
        assert local_playlist_cover_url(loaded) == "http://manual"


class TestNextId:
    def test_empty_starts_at_one(self):
        assert next_local_playlist_id([]) == 1

    def test_max_plus_one(self):
        playlists = [
            LocalPlaylist(id=7, name="A"),
            LocalPlaylist(id=2, name="B"),
        ]
        assert next_local_playlist_id(playlists) == 8
