"""play_stats.json 的读写、校验与时长格式化测试。"""

from __future__ import annotations

import json

import pytest

from neriplayer_win.data.play_stats import (
    PLAY_STATS_VERSION,
    PlayStat,
    PlayStatsStore,
    format_play_duration,
    make_key,
    validated_stats,
)


@pytest.fixture
def store(tmp_path):
    return PlayStatsStore(tmp_path)


def _stat(**kwargs) -> PlayStat:
    base = dict(
        key="netease:185811", source="netease", song_id=185811,
        title="富士山下", artist="陈奕迅", listen_ms=60_000, play_count=3,
    )
    base.update(kwargs)
    return PlayStat(**base)


# -- 时长格式化 --------------------------------------------------------------

@pytest.mark.parametrize("ms, expected", [
    (0, "00:00:00"),
    (47_000, "00:00:47"),
    (723_000, "00:12:03"),
    (11_229_000, "03:07:09"),
    (86_399_999, "23:59:59"),
    (86_400_000, "1天 00:00:00"),
    (187_340_000, "2天 04:02:20"),
    (8_640_000_000, "100天 00:00:00"),
])
def test_format_play_duration(ms, expected):
    """日d时:分:秒;不足一天省略天段,毫秒向下取整到秒。"""
    assert format_play_duration(ms) == expected


def test_format_play_duration_negative_clamps_to_zero():
    assert format_play_duration(-5000) == "00:00:00"


# -- 键构造 ------------------------------------------------------------------

def test_make_key_matches_queue_song_key():
    """统计键与 QueueSong.key 同构(那边是元组,这边拼成字符串)。"""
    from neriplayer_win.player.queue import QueueSong

    song = QueueSong(
        source="netease", id=185811, bvid="", title="t", artist="a", duration_ms=1000,
    )
    assert make_key("netease", 185811) == ":".join(song.key)


def test_make_key_with_bvid():
    """酷狗 id 恒 0,必须靠 bvid 区分不同歌。"""
    assert make_key("kugou", 0, "abc") == "kugou:0:abc"
    assert make_key("kugou", 0, "def") == "kugou:0:def"


# -- 读写往返 ----------------------------------------------------------------

def test_save_and_load_roundtrip(store):
    stats = [_stat(), _stat(key="bili:123", source="bili", song_id=123, play_count=1)]
    assert store.save(stats) is True
    loaded = store.load()
    assert len(loaded) == 2
    assert loaded[0].key == "netease:185811"
    assert loaded[0].listen_ms == 60_000
    assert loaded[0].title == "富士山下"


def test_load_missing_file_returns_empty(store):
    assert store.load() == []


def test_save_is_atomic_no_partial_file(store):
    """落盘后目录里不应残留临时文件。"""
    store.save([_stat()])
    leftovers = list(store.path.parent.glob(".play_stats-*"))
    assert leftovers == []


def test_load_corrupt_json_returns_empty(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() == []


def test_load_wrong_version_returns_empty(store):
    """版本不符按空数据启动,不尝试猜测旧格式。"""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"version": 99, "stats": [_stat().to_dict()]}), encoding="utf-8"
    )
    assert store.load() == []


def test_saved_file_has_version(store):
    store.save([_stat()])
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == PLAY_STATS_VERSION


# -- 校验 --------------------------------------------------------------------

def test_validated_stats_drops_invalid_entries():
    raw = [
        {"key": "netease:1", "source": "netease"},
        {"key": "", "source": "netease"},  # 缺 key
        {"source": "netease"},  # 缺 key
        "not a dict",
        None,
        {"key": "bili:2", "source": "bili"},
    ]
    result = validated_stats(raw)
    assert [s.key for s in result] == ["netease:1", "bili:2"]


def test_validated_stats_drops_duplicate_keys():
    raw = [
        {"key": "netease:1", "source": "netease", "play_count": 5},
        {"key": "netease:1", "source": "netease", "play_count": 9},
    ]
    result = validated_stats(raw)
    assert len(result) == 1
    assert result[0].play_count == 5  # 保留第一条


def test_validated_stats_clamps_negative_numbers():
    raw = [{
        "key": "netease:1", "source": "netease",
        "listen_ms": -100, "play_count": -5, "duration_ms": -1,
    }]
    result = validated_stats(raw)
    assert result[0].listen_ms == 0
    assert result[0].play_count == 0
    assert result[0].duration_ms == 0


def test_validated_stats_tolerates_bad_types():
    raw = [{
        "key": "netease:1", "source": "netease",
        "listen_ms": "abc", "title": 123, "cover_url": None,
    }]
    result = validated_stats(raw)
    assert result[0].listen_ms == 0
    assert result[0].title == ""
    assert result[0].cover_url == ""


def test_validated_stats_accepts_numeric_strings():
    raw = [{"key": "netease:1", "source": "netease", "listen_ms": "60000"}]
    result = validated_stats(raw)
    assert result[0].listen_ms == 60_000


# -- 清空 --------------------------------------------------------------------

def test_clear_removes_file(store):
    store.save([_stat()])
    assert store.clear() is True
    assert store.load() == []


def test_clear_missing_file_is_success(store):
    assert store.clear() is True
