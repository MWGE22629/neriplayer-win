"""播放统计的持久化层:play_stats.json 的读写与校验。

口径(与 docs/research/M9-overview.md 一致):
- 「播放时长」是这首歌累计被收听的时长,不是最近播放时间,也不是歌曲时长;
  暂停、缓冲、加载中、切歌空档一律不计,seek 跳过的部分不计。
- 「播放次数」按「听满 30 秒 +1,或整曲播完 +1,每个播放周期最多 +1」计。
  计数与计时规则本身在 ui/play_stats_tracker.py,本模块只管存取。

文件与 local_playlists.json 并列,生命周期独立于 settings.json:
统计是用户产出的不可再生数据,损坏只能清零,不能像界面偏好那样
静默回落默认值。校验策略因此是「非法条目丢弃、合法条目保留」。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger("neriplayer.play_stats")

PLAY_STATS_FILE = "play_stats.json"
PLAY_STATS_VERSION = 1

# 一次播放的最短收听时长(毫秒):不足这个时长不计次
MIN_LISTEN_MS_FOR_PLAY_COUNT = 30_000


@dataclass
class PlayStat:
    """单首歌的累计统计。key 与 QueueSong.key 同构,用于跨表匹配。"""

    key: str  # 序列化键:source:id:bvid 形式的字符串
    source: str
    song_id: int
    bvid: str = ""
    title: str = ""
    artist: str = ""
    cover_url: str = ""
    duration_ms: int = 0
    listen_ms: int = 0  # 累计实际收听时长(不含暂停)
    play_count: int = 0  # 累计播放次数
    first_played_at: int = 0  # 首次播放的 Unix 毫秒时间戳
    last_played_at: int = 0  # 最近一次播放的 Unix 毫秒时间戳(仅排序用,不上界面)

    def to_dict(self) -> dict[str, Any]:
        # 字段顺序固定:落盘 diff 稳定,便于人工比对
        return {
            "key": self.key,
            "source": self.source,
            "id": self.song_id,
            "bvid": self.bvid,
            "title": self.title,
            "artist": self.artist,
            "cover_url": self.cover_url,
            "duration_ms": self.duration_ms,
            "listen_ms": self.listen_ms,
            "play_count": self.play_count,
            "first_played_at": self.first_played_at,
            "last_played_at": self.last_played_at,
        }


def make_key(source: str, song_id: int, bvid: str = "") -> str:
    """与 QueueSong.key 同构的字符串键(那边是元组,这边落盘用字符串)。"""
    if bvid:
        return f"{source}:{song_id}:{bvid}"
    return f"{source}:{song_id}"


def format_play_duration(listen_ms: int) -> str:
    """累计收听时长 → 「d天 hh:mm:ss」(不足一天省略天段)。

    与播放条的 format_seconds(mm:ss) 不同:这里跨天,且要满足
    「日d时:分:秒」的展示要求。天数不加前导零,时/分/秒各两位。
    """
    total_s = max(0, int(listen_ms) // 1000)
    days, rest = divmod(total_s, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, seconds = divmod(rest, 60)
    hms = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if days:
        return f"{days}天 {hms}"
    return hms


def _as_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float):
        return int(value) if value >= 0 else 0
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return 0
        return parsed if parsed >= 0 else 0
    return 0


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _validated_stat(raw: Any) -> PlayStat | None:
    """单条校验:key 与 source 必须有,数值字段负值归零,缺字段补默认。"""
    if not isinstance(raw, Mapping):
        return None
    key = _as_text(raw.get("key"))
    source = _as_text(raw.get("source"))
    if not key or not source:
        return None
    return PlayStat(
        key=key,
        source=source,
        song_id=_as_non_negative_int(raw.get("id")),
        bvid=_as_text(raw.get("bvid")),
        title=_as_text(raw.get("title")),
        artist=_as_text(raw.get("artist")),
        cover_url=_as_text(raw.get("cover_url")),
        duration_ms=_as_non_negative_int(raw.get("duration_ms")),
        listen_ms=_as_non_negative_int(raw.get("listen_ms")),
        play_count=_as_non_negative_int(raw.get("play_count")),
        first_played_at=_as_non_negative_int(raw.get("first_played_at")),
        last_played_at=_as_non_negative_int(raw.get("last_played_at")),
    )


def validated_stats(raw: Any) -> list[PlayStat]:
    """整份校验:非法条目丢弃,同一 key 只保留第一条(防手改后重复)。"""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    result: list[PlayStat] = []
    for item in raw:
        stat = _validated_stat(item)
        if stat is None or stat.key in seen:
            continue
        seen.add(stat.key)
        result.append(stat)
    return result


class PlayStatsStore:
    """play_stats.json 的读写。所有写入走原子替换,避免半截文件。"""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._dir = Path(data_dir) if data_dir is not None else _default_data_dir()
        self._path = self._dir / PLAY_STATS_FILE

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[PlayStat]:
        """读整份统计;文件缺失或损坏返回空列表(不抛,不阻断启动)。"""
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("播放统计读取失败,按空数据启动: %s", exc)
            return []
        if not isinstance(payload, Mapping):
            logger.warning("播放统计格式非法(顶层非对象),按空数据启动")
            return []
        version = payload.get("version")
        if version != PLAY_STATS_VERSION:
            logger.warning(
                "播放统计版本不符(读到 %r,期望 %r),按空数据启动",
                version, PLAY_STATS_VERSION,
            )
            return []
        return validated_stats(payload.get("stats"))

    def save(self, stats: Sequence[PlayStat]) -> bool:
        """整份落盘(原子替换);失败返回 False,由调用方决定是否重试。"""
        payload = {
            "version": PLAY_STATS_VERSION,
            "stats": [stat.to_dict() for stat in stats],
        }
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".play_stats-", suffix=".tmp", dir=str(self._dir)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=1)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, self._path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning("播放统计落盘失败: %s", exc)
            return False
        return True

    def clear(self) -> bool:
        """清空统计(删除文件)。缺文件视为成功。"""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning("播放统计清空失败: %s", exc)
            return False
        return True


def _default_data_dir() -> Path:
    # 与 data/store.py 同源:复用其目录解析,避免两处规则漂移
    from .store import default_data_dir

    return default_data_dir()
