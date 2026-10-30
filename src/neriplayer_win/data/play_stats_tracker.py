"""播放统计的计时与计数逻辑(纯逻辑,无 Qt 依赖,可注入时钟)。

三条口径(见 docs/research/M9-overview.md):
1. 播放时长 = 累计实际收听时长。墙钟增量累加,暂停停表,seek 不计。
2. 播放次数 = 听满 30 秒 +1,或整曲播完 +1,每个播放周期最多 +1。
3. 暂停必须由 playing_changed(False) 立即停表,不能只靠位置判据:
   实测 mpv 在 set_paused(True) 之后、真正停下之前会漏出一个带
   +38ms 增量的 tick,只靠「位置在推进就计时」会把这段算进去。

与 Android 端(PlaybackStatsTracker)口径一致,便于数据对照。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .play_stats import MIN_LISTEN_MS_FOR_PLAY_COUNT

# 周期性落盘间隔:与 Android 的 PLAYBACK_STATS_PERIODIC_FLUSH_MS 一致
PERIODIC_FLUSH_MS = 15_000


@dataclass
class StatsDelta:
    """一次 flush 产生的增量。listened_ms 与 count_inc 至少一个非零。"""

    listened_ms: int = 0
    count_inc: int = 0


class PlayStatsTracker:
    """单曲播放周期的计时与计数状态机。

    使用方在以下时机调用:
    - on_song_changed(key):切到新歌(结算上一周期)
    - on_progress(position_s, duration_s):每次进度 tick
    - on_playing_changed(playing):播放/暂停变化
    - on_track_ended():整曲播完
    - on_seek(position_s):用户手动 seek(用于抑制「拖到结尾」误判播完)
    - take_delta():取走累计增量(周期性调用,返回 None 表示无增量)
    """

    def __init__(
        self,
        clock: Callable[[], float] = None,
    ) -> None:
        import time

        self._clock = clock if clock is not None else time.monotonic
        self._key = ""
        # 计时
        self._counting = False
        self._segment_start = 0.0
        self._accumulated_ms = 0  # 本周期已累计、待 flush 的毫秒
        self._session_listened_ms = 0  # 本播放周期累计收听(判 30 秒阈值)
        # 计数
        self._counted_this_period = False
        # 位置
        self._last_position = -1.0
        self._reached_end = False
        self._suppress_next_end = False
        self._last_flush_at = 0.0

    # -- 查询 ----------------------------------------------------------------

    @property
    def key(self) -> str:
        return self._key

    @property
    def counting(self) -> bool:
        return self._counting

    @property
    def session_listened_ms(self) -> int:
        return self._session_listened_ms

    # -- 输入事件 ------------------------------------------------------------

    def on_song_changed(self, key: str) -> StatsDelta | None:
        """切歌:先结算上一周期,再重置状态。返回上一周期的遗留增量。"""
        leftover = self._settle()
        self._key = key
        self._reset_period()
        self._last_flush_at = self._clock()
        return leftover

    def on_playing_changed(self, playing: bool) -> None:
        """播放/暂停变化:立即记/停表。

        停表必须在这里发生。mpv 在 pause 生效前会漏一个带增量的 tick,
        只靠位置判据会把那段算进收听时长。
        """
        if playing:
            if not self._counting:
                self._segment_start = self._clock()
                self._counting = True
        else:
            self._stop_counting()

    def on_progress(self, position_s: float, duration_s: float) -> None:
        """进度 tick:只有位置确实在推进时才累加墙钟增量。"""
        if self._counting:
            # 位置不动(缓冲/未开播/暂停过渡)不计时
            if position_s > 0 and position_s != self._last_position:
                self._accumulate_to_now()
            else:
                # 位置没动:把起点推到当前时刻,避免下次累加时把停顿算进去
                self._segment_start = self._clock()
        self._last_position = position_s
        if duration_s > 0 and position_s >= duration_s - 1.0:
            if not self._suppress_next_end:
                self._reached_end = True

    def on_track_ended(self) -> StatsDelta | None:
        """整曲播完:无条件计一次(即使只听了 5 秒就跳到最后)。

        拖到结尾的 seek 会触发 mpv 的 eof,那种情况不算「播完」,
        由 on_seek 设的抑制标记过滤。
        """
        self._stop_counting()
        if self._suppress_next_end:
            self._suppress_next_end = False
            self._reached_end = False
            return self.take_delta()
        self._reached_end = True
        return self.take_delta(force_count=True)

    def on_seek(self, position_s: float) -> None:
        """用户手动 seek:拖到结尾时抑制下一次「播完」计数。"""
        self._last_position = position_s
        self._suppress_next_end = True

    def take_delta(self, force_count: bool = False) -> StatsDelta | None:
        """取走累计增量(时长 + 本周期尚未取走的计数);无增量返回 None。

        force_count 为真表示无条件计一次(整曲播完),不再看 30 秒阈值。
        """
        count = self._settle_count_if_needed(force=force_count)
        if self._accumulated_ms <= 0 and count == 0:
            return None
        delta = StatsDelta(listened_ms=self._accumulated_ms, count_inc=count)
        self._accumulated_ms = 0
        self._last_flush_at = self._clock()
        return delta

    def should_flush(self) -> bool:
        """距上次 flush 是否已到周期(按真实经过时间,不看 tick 计数)。"""
        return (self._clock() - self._last_flush_at) * 1000 >= PERIODIC_FLUSH_MS

    # -- 内部 ----------------------------------------------------------------

    def _accumulate_to_now(self) -> None:
        now = self._clock()
        elapsed_ms = int(max(0.0, now - self._segment_start) * 1000)
        if elapsed_ms <= 0:
            return
        self._accumulated_ms += elapsed_ms
        self._session_listened_ms += elapsed_ms
        self._segment_start = now

    def _stop_counting(self) -> None:
        if self._counting:
            self._accumulate_to_now()
        self._counting = False

    def _settle_count_if_needed(self, force: bool = False) -> int:
        """按 30 秒阈值结算本周期计数;force 为真时无条件计一次。"""
        if self._counted_this_period:
            return 0
        if force or self._session_listened_ms >= MIN_LISTEN_MS_FOR_PLAY_COUNT:
            self._counted_this_period = True
            return 1
        return 0

    def _settle(self) -> StatsDelta | None:
        """结束当前周期:停表、按阈值结算计数、返回增量。"""
        self._stop_counting()
        before = self._accumulated_ms
        count = self._settle_count_if_needed()
        if self._accumulated_ms <= 0 and count == 0:
            return None
        delta = StatsDelta(listened_ms=self._accumulated_ms, count_inc=count)
        self._accumulated_ms = 0
        self._last_flush_at = self._clock()
        return delta if (delta.listened_ms > 0 or delta.count_inc > 0) else None

    def _reset_period(self) -> None:
        self._counting = False
        self._segment_start = 0.0
        self._session_listened_ms = 0
        self._counted_this_period = False
        self._last_position = -1.0
        self._reached_end = False
        self._suppress_next_end = False
