"""播放队列核心逻辑(M3):统一队列条目 + 播放模式导航 + 备用 URL 轮换。

纯逻辑实现(只用 Qt 信号机制,不碰网络与 UI),方便单测:

- QueueSong:网易云歌曲与B站视频的同构条目(自 MainWindow._QueueSong 迁入)。
- PlayMode / PlayQueue:顺序 / 随机 / 单曲循环三种模式的导航语义。
  - SEQUENCE:手动 next/prev 越界回绕;一首播完自动接下一首,到队尾即停
    (保持 M1/M2「播放完毕」语义,不无限循环)。
  - SHUFFLE:随机挑选但绝不立即重复当前曲;prev 沿已播历史回退。
  - REPEAT_ONE:播完自动重播当前曲;手动 next/prev 仍正常切歌。
- BackupUrlRotator:B站 backupUrls 候选轮换(播放加载失败按序换候选,
  全部失败才判「播放失败」;网易云不参与,其音质回退链在解析层已完成)。

随机性使用全局 random 模块,测试可用 random.seed(...) 复现。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from PySide6.QtCore import QObject, Signal


class PlayMode(str, Enum):
    """播放模式;值为 settings.json 里的持久化字符串。"""

    SEQUENCE = "sequence"
    SHUFFLE = "shuffle"
    REPEAT_ONE = "repeat_one"

    @property
    def display_name(self) -> str:
        return _MODE_DISPLAY_NAME[self]

    @property
    def button_label(self) -> str:
        return _MODE_BUTTON_LABEL[self]


_MODE_DISPLAY_NAME = {
    PlayMode.SEQUENCE: "顺序播放",
    PlayMode.SHUFFLE: "随机播放",
    PlayMode.REPEAT_ONE: "单曲循环",
}
_MODE_BUTTON_LABEL = {
    PlayMode.SEQUENCE: "顺序",
    PlayMode.SHUFFLE: "随机",
    PlayMode.REPEAT_ONE: "单曲",
}


@dataclass(frozen=True)
class QueueSong:
    """统一队列条目:网易云歌曲与B站视频(收藏夹条目)同构。"""

    source: str  # "netease" | "bili"
    id: int  # 网易云 song id / B站 avid
    bvid: str  # B站 only
    title: str
    artist: str  # 歌手 / UP主
    duration_ms: int


class PlayQueue(QObject):
    """带播放模式的播放队列;持有当前曲与已播历史,发信号通知 UI。

    信号:
    - queue_changed:队列内容被整体替换(replace)时发一次。
    - current_changed(int):当前曲变化(-1 表示无当前曲)。
    - mode_changed(PlayMode):播放模式变化。
    """

    queue_changed = Signal()
    current_changed = Signal(int)
    mode_changed = Signal(object)

    def __init__(
        self, parent: QObject | None = None, mode: PlayMode = PlayMode.SEQUENCE
    ) -> None:
        super().__init__(parent)
        self._items: list[QueueSong] = []
        self._index: int = -1
        self._mode: PlayMode = mode
        # 已播历史(索引按播报顺序记录)与历史游标;仅随机模式的 prev 使用
        self._history: list[int] = []
        self._history_pos: int = -1

    # -- 查询 ----------------------------------------------------------------

    def items(self) -> list[QueueSong]:
        """队列内容副本(调用方可安全遍历;不应原地修改)。"""
        return list(self._items)

    def item_at(self, index: int) -> QueueSong | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def __len__(self) -> int:
        return len(self._items)

    def current_index(self) -> int:
        return self._index

    def current_item(self) -> QueueSong | None:
        return self.item_at(self._index)

    def mode(self) -> PlayMode:
        return self._mode

    # -- 内容变更 --------------------------------------------------------------

    def replace(self, items: Iterable[QueueSong]) -> None:
        """整体替换队列(切换歌单/收藏夹):当前曲与历史一并重置。"""
        self._items = list(items)
        self._index = -1
        self._history = []
        self._history_pos = -1
        self.queue_changed.emit()
        self.current_changed.emit(-1)

    def reset(self) -> None:
        """清空当前曲与历史,保留队列内容(用于登出等场景)。"""
        self._index = -1
        self._history = []
        self._history_pos = -1
        self.current_changed.emit(-1)

    def set_mode(self, mode: PlayMode) -> None:
        if mode is self._mode:
            return
        self._mode = mode
        self.mode_changed.emit(mode)

    # -- 当前曲与导航 ------------------------------------------------------------

    def jump(self, index: int) -> bool:
        """直接指定当前曲(双击列表/队列窗口切跳);越界返回 False。"""
        if not (0 <= index < len(self._items)):
            return False
        if index == self._index:
            return True
        self._set_current(index)
        return True

    def next(self) -> int | None:
        """手动下一首;返回新当前索引,队列空返回 None。

        - SEQUENCE / REPEAT_ONE:线性前进,队尾回绕到队首。
        - SHUFFLE:随机挑选且不立即重复当前曲。
        """
        count = len(self._items)
        if count == 0:
            return None
        if self._index < 0:
            self._set_current(0)
            return 0
        if self._mode is PlayMode.SHUFFLE:
            chosen = self._pick_shuffle_next()
        else:
            chosen = (self._index + 1) % count
        self._set_current(chosen)
        return chosen

    def prev(self) -> int | None:
        """手动上一首;返回新当前索引,无可回退返回 None(保持不动)。

        - SEQUENCE / REPEAT_ONE:线性后退,队首回绕到队尾。
        - SHUFFLE:沿已播历史回退,不动历史游标之前的部分。
        """
        count = len(self._items)
        if count == 0 or self._index < 0:
            return None
        if self._mode is PlayMode.SHUFFLE:
            if self._history_pos <= 0:
                return None
            self._history_pos -= 1
            index = self._history[self._history_pos]
            if index != self._index:
                self._index = index
                self.current_changed.emit(index)
            return index
        chosen = (self._index - 1) % count
        self._set_current(chosen)
        return chosen

    def advance_ended(self) -> int | None:
        """一首播完后的自动接播;返回下一曲索引,None 表示播完停止。

        - REPEAT_ONE:返回当前索引(调用方直接重播,不变更当前曲)。
        - SHUFFLE:随机挑选且不立即重复当前曲。
        - SEQUENCE:有下一曲则前进;已在队尾返回 None(到尾即停,不回绕)。
        """
        count = len(self._items)
        if count == 0:
            return None
        if self._index < 0:
            self._set_current(0)
            return 0
        if self._mode is PlayMode.REPEAT_ONE:
            return self._index
        if self._mode is PlayMode.SHUFFLE:
            chosen = self._pick_shuffle_next()
            self._set_current(chosen)
            return chosen
        if self._index + 1 < count:
            chosen = self._index + 1
            self._set_current(chosen)
            return chosen
        return None

    # -- 内部 ----------------------------------------------------------------

    def _set_current(self, index: int) -> None:
        """设置当前曲并记录历史(截断游标之后的旧前进分支)。"""
        self._index = index
        self._history = self._history[: self._history_pos + 1]
        self._history.append(index)
        self._history_pos = len(self._history) - 1
        self.current_changed.emit(index)

    def _pick_shuffle_next(self) -> int:
        count = len(self._items)
        if count <= 1:
            return max(self._index, 0)
        chosen = self._index
        while chosen == self._index:
            chosen = random.randrange(count)
        return chosen


class BackupUrlRotator:
    """播放 URL 候选轮换(B站 backupUrls)。

    候选列表去重去空;首个候选即主 URL。加载失败时 advance() 取下一个,
    全部耗尽(has_next() 为 False)才判定播放失败。
    """

    def __init__(self, candidates: Iterable[str] | None) -> None:
        seen: set[str] = set()
        urls: list[str] = []
        for raw in candidates or []:
            url = (raw or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
        self._urls = urls
        self._pos = 0

    def __len__(self) -> int:
        return len(self._urls)

    @property
    def current(self) -> str:
        """当前候选 URL;空轮换返回空串。"""
        return self._urls[self._pos] if self._urls else ""

    @property
    def position(self) -> int:
        """当前候选序号(1 起;空轮换为 0)。"""
        return self._pos + 1 if self._urls else 0

    def has_next(self) -> bool:
        """是否还有下一个候选。"""
        return self._pos + 1 < len(self._urls)

    def advance(self) -> str | None:
        """前进到下一个候选并返回;耗尽返回 None(状态不变)。"""
        if not self.has_next():
            return None
        self._pos += 1
        return self._urls[self._pos]
