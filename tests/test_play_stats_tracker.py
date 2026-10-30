"""PlayStatsTracker 的计时与计数口径测试。

锁住三条最容易写错的规则:
1. 暂停停表(包括 mpv 暂停生效前漏出的那个 tick);
2. 听满 30 秒才算一次,整曲播完无条件算一次,每周期最多一次;
3. seek 拖到结尾不算「播完」。
"""

from __future__ import annotations

import pytest

from neriplayer_win.data.play_stats_tracker import (
    PERIODIC_FLUSH_MS,
    PlayStatsTracker,
)


class FakeClock:
    """可手动推进的单调时钟(秒)。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tracker(clock: FakeClock) -> PlayStatsTracker:
    t = PlayStatsTracker(clock=clock)
    t.on_song_changed("netease:1")
    return t


# -- 时长累加 ----------------------------------------------------------------

def test_listen_time_accumulates_only_while_playing(tracker, clock):
    """播放中推进 10 秒,累计约 10 秒。"""
    tracker.on_playing_changed(True)
    for i in range(10):
        clock.advance(1.0)
        tracker.on_progress(float(i + 1), 200.0)
    delta = tracker.take_delta()
    assert delta is not None
    assert 9500 <= delta.listened_ms <= 10_500


def test_pause_stops_the_clock(tracker, clock):
    """暂停期间时间流逝不计入收听时长。"""
    tracker.on_playing_changed(True)
    clock.advance(5.0)
    tracker.on_progress(5.0, 200.0)
    tracker.on_playing_changed(False)
    clock.advance(60.0)  # 暂停 60 秒
    tracker.on_progress(5.0, 200.0)
    tracker.on_playing_changed(True)
    clock.advance(5.0)
    tracker.on_progress(10.0, 200.0)
    delta = tracker.take_delta()
    assert delta is not None
    # 只算两段各 5 秒,不是 70 秒
    assert 9500 <= delta.listened_ms <= 10_500


def test_paused_ticks_do_not_add_time(tracker, clock):
    """暂停状态下反复收到的 tick 不累加(位置不变)。"""
    tracker.on_playing_changed(True)
    clock.advance(2.0)
    tracker.on_progress(2.0, 200.0)
    tracker.on_playing_changed(False)
    for _ in range(20):
        clock.advance(0.25)
        tracker.on_progress(2.0, 200.0)
    delta = tracker.take_delta()
    assert delta is not None
    assert delta.listened_ms <= 2_500  # 只有暂停前那 2 秒


def test_tick_after_pause_command_is_not_counted(tracker, clock):
    """mpv 暂停生效前漏出的那个带增量 tick 不能被计入。

    实测序列:tick(pos=1.694) -> PLAYING(False) -> tick(pos=1.732, +38ms)。
    第二个 tick 位置仍在推进,但此时已停表,不得累加。
    """
    tracker.on_playing_changed(True)
    clock.advance(1.7)
    tracker.on_progress(1.694, 200.0)
    tracker.on_playing_changed(False)  # 立即停表
    clock.advance(0.038)
    tracker.on_progress(1.732, 200.0)  # 残留推进
    delta = tracker.take_delta()
    assert delta is not None
    # 只算到暂停那一刻的 1.7 秒,不含残留的 38ms
    assert delta.listened_ms <= 1_800


def test_stalled_position_does_not_add_time(tracker, clock):
    """缓冲卡顿:位置不动时不计时,即使仍在「播放」态。"""
    tracker.on_playing_changed(True)
    clock.advance(1.0)
    tracker.on_progress(1.0, 200.0)
    for _ in range(10):
        clock.advance(1.0)
        tracker.on_progress(1.0, 200.0)  # 位置卡住
    clock.advance(1.0)
    tracker.on_progress(2.0, 200.0)
    delta = tracker.take_delta()
    assert delta is not None
    # 卡顿的 10 秒不算,只有两段各 1 秒
    assert delta.listened_ms <= 2_500


def test_seek_does_not_add_time(tracker, clock):
    """拖动进度条快速前进,不产生额外收听时长。"""
    tracker.on_playing_changed(True)
    clock.advance(1.0)
    tracker.on_progress(1.0, 200.0)
    tracker.on_seek(150.0)
    clock.advance(1.0)
    tracker.on_progress(151.0, 200.0)
    delta = tracker.take_delta()
    assert delta is not None
    assert delta.listened_ms <= 2_500  # 只算实际经过的 2 秒


# -- 计数 --------------------------------------------------------------------

def test_play_count_requires_30_seconds(tracker, clock):
    """未听满 30 秒不计次。"""
    tracker.on_playing_changed(True)
    clock.advance(20.0)
    tracker.on_progress(20.0, 200.0)
    delta = tracker.take_delta()
    assert delta is None or delta.count_inc == 0


def test_play_count_at_30_seconds(tracker, clock):
    """听满 30 秒计一次。"""
    tracker.on_playing_changed(True)
    for i in range(31):
        clock.advance(1.0)
        tracker.on_progress(float(i + 1), 200.0)
    delta = tracker.take_delta()
    assert delta is not None
    assert delta.count_inc == 1


def test_play_count_only_once_per_period(tracker, clock):
    """同一播放周期内多次取增量,只计一次。"""
    tracker.on_playing_changed(True)
    for i in range(40):
        clock.advance(1.0)
        tracker.on_progress(float(i + 1), 200.0)
    first = tracker.take_delta()
    assert first is not None and first.count_inc == 1
    for i in range(40, 60):
        clock.advance(1.0)
        tracker.on_progress(float(i + 1), 200.0)
    second = tracker.take_delta()
    assert second is None or second.count_inc == 0


def test_track_ended_counts_even_if_short(tracker, clock):
    """整曲播完无条件计一次(短曲不到 30 秒也算)。"""
    tracker.on_playing_changed(True)
    clock.advance(5.0)
    tracker.on_progress(5.0, 5.0)  # 已到结尾
    delta = tracker.on_track_ended()
    assert delta is not None
    assert delta.count_inc == 1


def test_track_ended_counts_once_only(tracker, clock):
    """重复收到 track_ended 不重复计数。"""
    tracker.on_playing_changed(True)
    clock.advance(5.0)
    tracker.on_progress(5.0, 5.0)
    tracker.on_track_ended()
    delta = tracker.on_track_ended()
    assert delta is None or delta.count_inc == 0


def test_seek_to_end_does_not_count_as_ended(tracker, clock):
    """拖到结尾触发的 eof 不算播放一次。"""
    tracker.on_playing_changed(True)
    clock.advance(2.0)
    tracker.on_progress(2.0, 200.0)
    tracker.on_seek(199.5)  # 用户拖到结尾
    delta = tracker.on_track_ended()
    assert delta is None or delta.count_inc == 0


def test_song_change_settles_previous_period(tracker, clock):
    """切歌时结算上一周期:听满 30 秒的应计数。"""
    tracker.on_playing_changed(True)
    for i in range(35):
        clock.advance(1.0)
        tracker.on_progress(float(i + 1), 200.0)
    leftover = tracker.on_song_changed("netease:2")
    assert leftover is not None
    assert leftover.count_inc == 1
    assert leftover.listened_ms >= 30_000


# -- flush 节奏 --------------------------------------------------------------

def test_should_flush_after_interval(tracker, clock):
    """距上次 flush 超过周期即建议落盘。"""
    tracker.on_playing_changed(True)
    clock.advance(PERIODIC_FLUSH_MS / 1000.0 + 1.0)
    assert tracker.should_flush()


def test_should_not_flush_immediately(tracker, clock):
    clock.advance(1.0)
    assert not tracker.should_flush()
