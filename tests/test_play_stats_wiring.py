"""播放统计的窗口接线(M9-B):起播后必须真的开始计时。

回归背景:v0.9.3 起 `_start_play` 里 `engine.play_url()`(同步发
`playing_changed(True)`,tracker 开表)先于 `_stats_begin_song`
(`on_song_changed` 结算并复位)执行,刚开的表随即被复位,整首歌不再
计时,每首只落十几毫秒。tracker / store / 统计页各有单测,但这条
接线顺序只有窗口层能覆盖。

第二处回归:暂停/打开统计页/退出时结算出的增量,在条目尚未建(听不足
15 秒)且没带歌曲信息时被 `_stats_apply_delta` 直接丢弃,表现为「播了
几秒就去看统计,永远是 0」。

用假引擎 + 注入时钟驱动,不碰 mpv 与网络;mpv 自身的 eof/idle 信号
行为不在本文件覆盖范围(engine.py 的用例负责)。
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, Signal

from neriplayer_win.data.play_stats_tracker import PlayStatsTracker
from neriplayer_win.player.queue import BackupUrlRotator, QueueSong
from neriplayer_win.ui.main_window import MainWindow

_TICK_S = 0.25


class _FakeEngine(QObject):
    """只保留统计关心的行为:play_url 同步发 playing_changed(True)。"""

    progress = Signal(float, float)
    playing_changed = Signal(bool)
    track_ended = Signal()
    load_failed = Signal(str)
    error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0  # 注入时钟(秒),同时充当播放位置

    def play_url(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.playing_changed.emit(True)

    def position(self) -> float:
        return self.now

    def duration(self) -> float:
        return 300.0


def _song(sid: int) -> QueueSong:
    return QueueSong(
        source="netease", id=sid, bvid="", title=f"song {sid}", artist="a",
        duration_ms=300_000,
    )


def _start(window: MainWindow, sid: int) -> None:
    window._start_play(_song(sid), BackupUrlRotator([f"http://x/{sid}.mp3"]), None)


def _elapse(window: MainWindow, seconds: float) -> None:
    """模拟播放 seconds 秒:每 250ms 一个统计 tick,播放位置同步推进。"""
    for _ in range(int(seconds / _TICK_S)):
        window.engine.now += _TICK_S
        window._stats_on_tick()


def _make_window(qapp, monkeypatch, tmp_path) -> MainWindow:
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    window.engine = _FakeEngine()
    window._wire_engine()
    window._stats_tracker = PlayStatsTracker(clock=lambda: window.engine.now)
    return window


def _close(window: MainWindow) -> None:
    window._force_exit = True
    window.close()


def test_listening_accumulates_after_start_play(qapp, monkeypatch, tmp_path):
    """症状:播放统计时长全部为 0(每首只有十几毫秒)。"""
    window = _make_window(qapp, monkeypatch, tmp_path)
    try:
        _start(window, 1)
        _elapse(window, 40)
        window._on_playing_changed(False)  # 暂停:结算
        stat = window._play_stats.get("netease:1")
        listened = stat.listen_ms if stat else 0
        assert listened >= 39_000, f"只记了 {listened} ms"
        assert stat.play_count == 1  # 听满 15 秒计一次
    finally:
        _close(window)


def test_listening_counts_while_hidden_to_tray(qapp, monkeypatch, tmp_path):
    """收进托盘只是隐藏窗口:进程仍在、音频仍在出声,必须继续计时。"""
    window = _make_window(qapp, monkeypatch, tmp_path)
    try:
        _start(window, 1)
        window.hide()
        _elapse(window, 40)
        window._on_playing_changed(False)
        stat = window._play_stats.get("netease:1")
        listened = stat.listen_ms if stat else 0
        assert listened >= 39_000, f"只记了 {listened} ms"
    finally:
        _close(window)


def test_unflushed_leftover_stays_with_previous_song(qapp, monkeypatch, tmp_path):
    """切歌时上一首尚未落盘的收听时长记回上一首,不串到新歌。"""
    window = _make_window(qapp, monkeypatch, tmp_path)
    try:
        _start(window, 1)
        _elapse(window, 4)  # 不足 15 秒:增量还挂在 tracker 里
        _start(window, 2)
        second = window._play_stats.get("netease:2")
        assert window._play_stats["netease:1"].listen_ms >= 3_900
        assert (second.listen_ms if second else 0) < 500
    finally:
        _close(window)


def test_periodic_flush_merges_delta_without_pausing(qapp, monkeypatch, tmp_path):
    """连续播放不暂停:每 15 秒把 tracker 增量并入内存表并落盘(中途被杀最多丢 15 秒)。"""
    window = _make_window(qapp, monkeypatch, tmp_path)
    try:
        _start(window, 1)
        _elapse(window, 40)
        stat = window._play_stats.get("netease:1")
        assert stat is not None and stat.listen_ms >= 30_000, (
            f"内存表只有 {stat.listen_ms if stat else 0} ms"
        )
        saved = json.loads((tmp_path / "play_stats.json").read_text(encoding="utf-8"))
        assert saved["stats"][0]["listen_ms"] >= 30_000
    finally:
        _close(window)


def test_pause_before_first_flush_keeps_time(qapp, monkeypatch, tmp_path):
    """不足 15 秒就暂停:增量被取走时条目还没建,不能因此丢弃。"""
    window = _make_window(qapp, monkeypatch, tmp_path)
    try:
        _start(window, 1)
        _elapse(window, 4)
        window._on_playing_changed(False)  # 暂停
        _start(window, 2)
        _elapse(window, 4)
        window._on_playing_changed(False)
        assert window._play_stats["netease:1"].listen_ms >= 3_900
        assert window._play_stats["netease:2"].listen_ms >= 3_900
    finally:
        _close(window)


def test_open_stats_page_before_first_flush_keeps_time(qapp, monkeypatch, tmp_path):
    """播几秒就打开统计页:当页必须能看到这首歌的时长。"""
    window = _make_window(qapp, monkeypatch, tmp_path)
    try:
        _start(window, 1)
        _elapse(window, 4)
        window._open_stats_page()
        assert window._play_stats["netease:1"].listen_ms >= 3_900
    finally:
        _close(window)
