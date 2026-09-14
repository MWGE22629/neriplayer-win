"""M3 播放队列单测:PlayQueue 全导航语义 + BackupUrlRotator 轮换 + settings.json。

PlayQueue / BackupUrlRotator 为纯逻辑(QObject 信号直连,无需事件循环),
随机模式用 random.seed 保证可复现。
"""

from __future__ import annotations

import json
import random

import pytest
from PySide6.QtWidgets import QApplication

from neriplayer_win.data.store import (
    DEFAULT_CLOSE_ACTION,
    DEFAULT_PLAY_MODE,
    LocalStore,
    default_settings,
)
from neriplayer_win.player.queue import (
    BackupUrlRotator,
    PlayMode,
    PlayQueue,
    QueueSong,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def song(n: int) -> QueueSong:
    return QueueSong(
        source="netease", id=n, bvid="", title=f"歌曲{n}",
        artist=f"歌手{n}", duration_ms=1000 * n,
    )


class TestPlayQueueSequence:
    """顺序模式:手动 next/prev 越界回绕;播完自动接播到队尾即停。"""

    def test_empty_queue(self, qapp):
        queue = PlayQueue()
        assert len(queue) == 0
        assert queue.current_index() == -1
        assert queue.current_item() is None
        assert queue.next() is None
        assert queue.prev() is None
        assert queue.advance_ended() is None

    def test_next_from_scratch_starts_first(self, qapp):
        queue = PlayQueue()
        queue.replace([song(i) for i in range(3)])
        assert queue.next() == 0

    def test_next_wraps_at_end(self, qapp):
        queue = PlayQueue()
        queue.replace([song(i) for i in range(3)])
        queue.jump(2)
        assert queue.next() == 0

    def test_prev_wraps_at_start(self, qapp):
        queue = PlayQueue()
        queue.replace([song(i) for i in range(3)])
        queue.jump(0)
        assert queue.prev() == 2

    def test_linear_prev_next(self, qapp):
        queue = PlayQueue()
        queue.replace([song(i) for i in range(3)])
        queue.jump(1)
        assert queue.next() == 2
        assert queue.prev() == 1

    def test_advance_ended_stops_at_queue_end(self, qapp):
        queue = PlayQueue()
        queue.replace([song(i) for i in range(3)])
        queue.jump(2)
        assert queue.advance_ended() is None
        assert queue.current_index() == 2  # 停在最后一首

    def test_advance_ended_linear(self, qapp):
        queue = PlayQueue()
        queue.replace([song(i) for i in range(3)])
        queue.jump(1)
        assert queue.advance_ended() == 2

    def test_advance_ended_from_scratch(self, qapp):
        queue = PlayQueue()
        queue.replace([song(i) for i in range(3)])
        assert queue.advance_ended() == 0

    def test_current_changed_signal(self, qapp):
        queue = PlayQueue()
        received: list[int] = []
        queue.current_changed.connect(received.append)
        queue.replace([song(0), song(1)])
        queue.jump(1)
        assert received == [-1, 1]  # replace 清空 + jump


class TestPlayQueueShuffle:
    """随机模式:不立即重复当前曲;prev 沿已播历史回退。"""

    def test_next_never_repeats_current(self, qapp):
        random.seed(20260914)
        queue = PlayQueue(mode=PlayMode.SHUFFLE)
        queue.replace([song(i) for i in range(5)])
        queue.jump(0)
        previous = queue.current_index()
        for _ in range(50):
            index = queue.next()
            assert index is not None
            assert index != previous
            previous = index

    def test_advance_ended_never_repeats_current(self, qapp):
        random.seed(42)
        queue = PlayQueue(mode=PlayMode.SHUFFLE)
        queue.replace([song(i) for i in range(4)])
        queue.jump(0)
        previous = queue.current_index()
        for _ in range(30):
            index = queue.advance_ended()
            assert index is not None and index != previous
            previous = index

    def test_prev_walks_play_history_backwards(self, qapp):
        random.seed(7)
        queue = PlayQueue(mode=PlayMode.SHUFFLE)
        queue.replace([song(i) for i in range(6)])
        queue.jump(0)
        visited = [queue.next() for _ in range(4)]  # 0 之后播了 4 首
        # prev 依次回退:倒数第 2、第 3 …
        assert queue.prev() == visited[-2]
        assert queue.prev() == visited[-3]
        assert queue.current_index() == visited[-3]

    def test_prev_at_history_start_returns_none(self, qapp):
        queue = PlayQueue(mode=PlayMode.SHUFFLE)
        queue.replace([song(i) for i in range(3)])
        queue.jump(0)
        assert queue.prev() is None
        assert queue.current_index() == 0  # 保持不动

    def test_prev_after_replace_resets_history(self, qapp):
        queue = PlayQueue(mode=PlayMode.SHUFFLE)
        queue.replace([song(i) for i in range(3)])
        queue.jump(1)
        queue.replace([song(i) for i in range(3)])
        assert queue.prev() is None  # 历史已随 replace 清空

    def test_single_track_shuffle_does_not_deadlock(self, qapp):
        queue = PlayQueue(mode=PlayMode.SHUFFLE)
        queue.replace([song(0)])
        queue.jump(0)
        assert queue.next() == 0
        assert queue.advance_ended() == 0


class TestPlayQueueRepeatOne:
    """单曲循环:播完重播当前;手动 next/prev 仍线性切歌(含回绕)。"""

    def test_advance_ended_replays_current(self, qapp):
        queue = PlayQueue(mode=PlayMode.REPEAT_ONE)
        queue.replace([song(i) for i in range(3)])
        queue.jump(1)
        assert queue.advance_ended() == 1
        assert queue.current_index() == 1  # 当前曲不变,由调用方重播

    def test_manual_next_advances_and_wraps(self, qapp):
        queue = PlayQueue(mode=PlayMode.REPEAT_ONE)
        queue.replace([song(i) for i in range(3)])
        queue.jump(1)
        assert queue.next() == 2
        assert queue.next() == 0  # 回绕

    def test_manual_prev(self, qapp):
        queue = PlayQueue(mode=PlayMode.REPEAT_ONE)
        queue.replace([song(i) for i in range(3)])
        queue.jump(1)
        assert queue.prev() == 0


class TestPlayQueueReplaceResetAndSignals:
    """replace/reset/jump/set_mode 的状态与信号语义。"""

    def test_replace_resets_current_and_history(self, qapp):
        queue = PlayQueue()
        first = [song(i) for i in range(3)]
        second = [song(10 + i) for i in range(2)]
        queue.replace(first)
        queue.jump(2)
        queue.replace(second)
        assert len(queue) == 2
        assert queue.current_index() == -1
        assert queue.item_at(0) == second[0]

    def test_replace_emits_signals(self, qapp):
        queue = PlayQueue()
        queue_events: list[bool] = []
        current_events: list[int] = []
        queue.queue_changed.connect(lambda: queue_events.append(True))
        queue.current_changed.connect(current_events.append)
        queue.replace([song(0)])
        queue.jump(0)
        assert queue_events == [True]
        assert current_events == [-1, 0]

    def test_reset_keeps_items_clears_current(self, qapp):
        queue = PlayQueue()
        queue.replace([song(i) for i in range(3)])
        queue.jump(1)
        queue.reset()
        assert len(queue) == 3
        assert queue.current_index() == -1
        assert queue.current_item() is None

    def test_jump_bounds_and_same_index(self, qapp):
        queue = PlayQueue()
        queue.replace([song(i) for i in range(3)])
        assert queue.jump(3) is False
        assert queue.jump(-1) is False
        received: list[int] = []
        queue.current_changed.connect(received.append)
        assert queue.jump(0) is True
        assert queue.jump(0) is True  # 同曲幂等
        assert received == [0]

    def test_set_mode_emits_only_on_change(self, qapp):
        queue = PlayQueue()
        modes: list[PlayMode] = []
        queue.mode_changed.connect(modes.append)
        queue.set_mode(PlayMode.SEQUENCE)  # 与默认相同,不发光
        assert modes == []
        queue.set_mode(PlayMode.SHUFFLE)
        queue.set_mode(PlayMode.REPEAT_ONE)
        assert modes == [PlayMode.SHUFFLE, PlayMode.REPEAT_ONE]
        assert queue.mode() is PlayMode.REPEAT_ONE

    def test_play_mode_enum_persist_values(self, qapp):
        """枚举字符串值与 settings.json 持久化约定一致。"""
        assert PlayMode.SEQUENCE.value == DEFAULT_PLAY_MODE
        for mode in PlayMode:
            PlayMode(mode.value)  # 可从持久化字符串还原
            assert mode.display_name
            assert mode.button_label


class TestBackupUrlRotator:
    """B站 backupUrls 候选轮换(加载失败按序换候选,耗尽才判失败)。"""

    def test_dedup_and_strip(self):
        rotator = BackupUrlRotator(["u1", "u1", "  ", "u2", "", "u1"])
        assert len(rotator) == 2
        assert rotator.current == "u1"

    def test_advance_through_all_candidates(self):
        rotator = BackupUrlRotator(["u1", "u2", "u3"])
        assert rotator.position == 1
        assert rotator.advance() == "u2"
        assert rotator.position == 2
        assert rotator.advance() == "u3"
        assert rotator.has_next() is False
        assert rotator.advance() is None
        assert rotator.current == "u3"  # 耗尽后状态不变

    def test_single_candidate_exhausted_immediately(self):
        rotator = BackupUrlRotator(["only"])
        assert rotator.has_next() is False
        assert rotator.advance() is None
        assert rotator.current == "only"

    def test_empty_candidates(self):
        rotator = BackupUrlRotator([])
        assert len(rotator) == 0
        assert rotator.current == ""
        assert rotator.position == 0
        assert rotator.has_next() is False

    def test_failure_sequence_exhausts_then_reports(self):
        """模拟 engine 连发加载失败:逐候选重试,全部失败才最终报错。"""
        rotator = BackupUrlRotator(["main", "backup1", "backup2"])
        tried: list[str] = [rotator.current]
        retried = 0
        while True:
            if rotator.has_next():
                retried += 1
                tried.append(rotator.advance())
            else:
                break  # 到此才向上层报「播放失败」
        assert tried == ["main", "backup1", "backup2"]
        assert retried == 2


class TestSettingsStore:
    """settings.json:关闭行为(close_action,默认 tray)+ 默认播放模式。"""

    def make_store(self, tmp_path, monkeypatch) -> LocalStore:
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        return LocalStore()

    def test_defaults_without_file(self, tmp_path, monkeypatch):
        store = self.make_store(tmp_path, monkeypatch)
        assert store.load_settings() == default_settings()
        assert default_settings()["close_action"] == DEFAULT_CLOSE_ACTION == "tray"
        assert default_settings()["play_mode"] == DEFAULT_PLAY_MODE == "sequence"

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        store = self.make_store(tmp_path, monkeypatch)
        assert store.save_settings({"close_action": "exit", "play_mode": "shuffle"})
        loaded = store.load_settings()
        assert loaded["close_action"] == "exit"
        assert loaded["play_mode"] == "shuffle"
        # 文件内容可读(与登录包同风格 JSON)
        raw = json.loads(store.settings_path.read_text(encoding="utf-8"))
        assert raw["close_action"] == "exit"

    def test_invalid_values_fall_back_to_defaults(self, tmp_path, monkeypatch):
        store = self.make_store(tmp_path, monkeypatch)
        assert store.save_settings({"close_action": "explode", "play_mode": "repeat"})
        loaded = store.load_settings()
        assert loaded == default_settings()

    def test_partial_file_keeps_defaults_for_missing_keys(self, tmp_path, monkeypatch):
        store = self.make_store(tmp_path, monkeypatch)
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text(
            json.dumps({"play_mode": "repeat_one"}), encoding="utf-8"
        )
        loaded = store.load_settings()
        assert loaded["close_action"] == "tray"
        assert loaded["play_mode"] == "repeat_one"

    def test_corrupted_file_returns_defaults(self, tmp_path, monkeypatch):
        store = self.make_store(tmp_path, monkeypatch)
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text("{broken", encoding="utf-8")
        assert store.load_settings() == default_settings()

    def test_unknown_keys_ignored_on_save(self, tmp_path, monkeypatch):
        store = self.make_store(tmp_path, monkeypatch)
        store.save_settings({"close_action": "exit", "play_mode": "sequence", "junk": 1})
        raw = json.loads(store.settings_path.read_text(encoding="utf-8"))
        assert set(raw) == {"close_action", "play_mode"}
