"""本地持久化:登录 cookie 与账号信息(网易云 netease.json + B站 bili.json)、
应用设置(settings.json)、本地歌单(local_playlists.json)。

存放于 %APPDATA%/neriplayer-win/(可用环境变量 NERIPLAYER_WIN_DATA_DIR
覆盖以便测试)。

格式对照 reference/NeriPlayer-Android:
- data/auth/netease/NeteaseCookieRepository.kt(NeteaseAuthBundle.toJson):
  {"cookies": {name: value}, "savedAt": <epoch_ms>},本项目扩展 "profile"
  节点保存 userId / nickname,避免每次启动都要多打一次账号接口。
- data/auth/bili/BiliCookieRepository.kt(BiliAuthBundle.toJson):同一
  {"cookies", "savedAt"} 结构,登录判定键为 SESSDATA(BILI_LOGIN_COOKIE_KEYS
  = SESSDATA / DedeUserID / bili_jct);profile 存 mid / uname。
cookie 清洗规则照搬 validateAndSanitizeNeteaseCookies(两平台通用)。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..i18n import DEFAULT_LANGUAGE, VALID_LANGUAGES

# 不能 from ..log import get_logger:log.py 模块级 import 本模块的
# default_data_dir,会成环。这里直接取 neriplayer 根下的同名 logger,
# 与 get_logger("store") 等价(handler 挂在 "neriplayer" 根上)。
logger = logging.getLogger("neriplayer.store")

_DATA_DIR_ENV = "NERIPLAYER_WIN_DATA_DIR"
_NETEASE_FILE = "netease.json"
_BILI_FILE = "bili.json"
_SETTINGS_FILE = "settings.json"
# M6 酷狗登录凭据:扫码 token(歌单侧)与网页 KuGoo 串(播放侧)同存一份
_KUGOU_FILE = "kugou.json"
# M7 本地歌单:用户产出的变长数据,生命周期独立于界面偏好,单独成文件
_LOCAL_PLAYLISTS_FILE = "local_playlists.json"

# M3 设置项:关闭行为与默认播放模式。
# close_action 默认 "tray"(最小化到托盘,符合播放器习惯);"exit" 直接退出。
# play_mode 对应 player.queue.PlayMode 的枚举值。
# M4 增加 appearance:外观主题("dark" 暗色 / "light" 亮色),默认 dark。
# M5 增加 play_quality:播放音质偏好,取网易云档位键(standard/exhigh/
# lossless,B站侧由 selector 的映射函数换算);默认 lossless 保持既有行为。
# M5 增加侧栏树状态:sidebar_expanded 记录平台分区折叠状态
# ({"netease": bool, "netease-subscribed": bool, "bili": bool},默认全展开);
# netease_playlist_order / netease_subscribed_order / bili_folder_order
# 记录分区内拖拽排序后的条目 id 顺序(加载时按存储序重排,新条目追加尾部,
# 存储里已失效的 id 在重排时静默清掉)。
# 界面语言 language("zh" 中文 / "en" 英文,默认 zh;文案表见 neriplayer_win.i18n)。
SETTING_CLOSE_ACTION = "close_action"
SETTING_PLAY_MODE = "play_mode"
SETTING_APPEARANCE = "appearance"
SETTING_PLAY_QUALITY = "play_quality"
SETTING_LANGUAGE = "language"
SETTING_DYNAMIC_COLOR = "dynamic_color"
# M9 流光背景:关闭 / 动态纯色 / 流光。默认关闭,保持既有观感不变。
SETTING_AMBIENT_EFFECT = "ambient_effect"
AMBIENT_OFF = "off"
AMBIENT_SOLID = "solid"
AMBIENT_FLOW = "flow"
DEFAULT_AMBIENT_EFFECT = AMBIENT_OFF
_VALID_AMBIENT_EFFECTS = (AMBIENT_OFF, AMBIENT_SOLID, AMBIENT_FLOW)
# 播放音量(0-100 整数):记住上次退出时的值,启动时回填到播放条与 mpv。
# 默认 70,与 PlayerEngine / PlayerBar 的出厂值一致(M10)。
SETTING_VOLUME = "volume"
SETTING_SIDEBAR_EXPANDED = "sidebar_expanded"
SETTING_NETEASE_PLAYLIST_ORDER = "netease_playlist_order"
SETTING_NETEASE_SUBSCRIBED_ORDER = "netease_subscribed_order"
SETTING_BILI_FOLDER_ORDER = "bili_folder_order"
# 「最近」列表(M5):recent_max 记录侧栏展示条数(默认 8);recent_lists
# 是最近播放过的列表栈(新→旧),元素 {"kind", "id", "title"},kind 取
# RECENT_KINDS 白名单。仅浏览不计,起播才压栈(由 MainWindow 负责)。
SETTING_RECENT_MAX = "recent_max"
SETTING_RECENT_LISTS = "recent_lists"
# M7 本地歌单分区内的拖拽排序键(与上面三个平台分区排序键同构)
SETTING_LOCAL_PLAYLIST_ORDER = "local_playlist_order"
# M6 酷狗歌单分区内的拖拽排序键(与其余平台分区同构)
SETTING_KUGOU_PLAYLIST_ORDER = "kugou_playlist_order"
# M8 侧栏分区(顶层大类)的显示顺序:存分区键序列,由分区头「⋮」拖动改变。
# 「搜索」固定置顶、「设置」固定置底,不参与排序,所以只存中间那些。
SETTING_SECTION_ORDER = "section_order"
DEFAULT_CLOSE_ACTION = "tray"
DEFAULT_PLAY_MODE = "sequence"
DEFAULT_APPEARANCE = "dark"
DEFAULT_PLAY_QUALITY = "lossless"
DEFAULT_RECENT_MAX = 8
# 音量默认值:与 PlayerEngine._player.volume = 70.0 / PlayerBar 滑块的
# 出厂值同源,旧配置无 volume 键时行为不变
DEFAULT_VOLUME = 70
# 动态取色(M5):播放时从封面提取主色套用动态主题;对齐 Android 端默认开
DEFAULT_DYNAMIC_COLOR = True
# 语言代码以 neriplayer_win.i18n 为单一来源,此处沿用本文件命名习惯
_VALID_CLOSE_ACTIONS = ("exit", "tray")
_VALID_PLAY_MODES = ("sequence", "shuffle", "repeat_one")
_VALID_APPEARANCES = ("dark", "light")
_VALID_PLAY_QUALITIES = ("standard", "exhigh", "lossless")
_VALID_LANGUAGES = VALID_LANGUAGES
_MIN_RECENT_MAX = 1
_MAX_RECENT_MAX = 50
# 公开别名:设置页 SpinBox 的范围与文档共用
MIN_RECENT_MAX = _MIN_RECENT_MAX
MAX_RECENT_MAX = _MAX_RECENT_MAX
# 最近列表的来源种类:网易云自建/收藏歌单、B站收藏夹/稍后再看、每日推荐、
# 搜索、本地歌单。本地歌单必须在此白名单里:起播后 _push_recent 会把
# ("local-playlist", id, 标题) 写进 settings,缺失会被载入侧过滤掉。
RECENT_KINDS = (
    "netease-playlist",
    "netease-subscribed-playlist",
    "bili-folder",
    "bili-watchlater",
    "netease-daily",
    "local-playlist",
    "search",
)
# 本地歌单可收录的歌曲来源白名单。M6 接酷狗后已加 "kugou"
# (与 MainWindow._resolve_song_context 的分发配对维护)。
LOCAL_SONG_SOURCES = ("netease", "bili", "kugou")
# 本地歌单文件版本号(首个需要迁移位的文件;M7 之后条目结构还要扩字段)
LOCAL_PLAYLISTS_VERSION = 1
# 单歌单条目上限与歌单名长度上限:超限静默截断,避免坏文件撑破侧栏
LOCAL_PLAYLIST_MAX_ITEMS = 5000
LOCAL_PLAYLIST_NAME_MAX = 100
_SIDEBAR_SECTIONS = (
    "netease", "netease-subscribed", "bili", "kugou", "local", "recent",
)
# 参与「⋮」拖动排序的分区键(搜索/设置固定位置,不在其中)
SECTION_ORDER_KEYS = ("netease", "netease-subscribed", "bili", "kugou", "local", "recent")
_SETTING_ID_ORDERS = (
    SETTING_NETEASE_PLAYLIST_ORDER,
    SETTING_NETEASE_SUBSCRIBED_ORDER,
    SETTING_BILI_FOLDER_ORDER,
    SETTING_KUGOU_PLAYLIST_ORDER,
    SETTING_LOCAL_PLAYLIST_ORDER,
)


def default_settings() -> dict[str, Any]:
    """设置的出厂默认值(副本)。"""
    return {
        SETTING_CLOSE_ACTION: DEFAULT_CLOSE_ACTION,
        SETTING_PLAY_MODE: DEFAULT_PLAY_MODE,
        SETTING_APPEARANCE: DEFAULT_APPEARANCE,
        SETTING_PLAY_QUALITY: DEFAULT_PLAY_QUALITY,
        SETTING_LANGUAGE: DEFAULT_LANGUAGE,
        SETTING_DYNAMIC_COLOR: DEFAULT_DYNAMIC_COLOR,
        SETTING_AMBIENT_EFFECT: DEFAULT_AMBIENT_EFFECT,
        SETTING_VOLUME: DEFAULT_VOLUME,
        SETTING_SIDEBAR_EXPANDED: {section: True for section in _SIDEBAR_SECTIONS},
        SETTING_NETEASE_PLAYLIST_ORDER: [],
        SETTING_NETEASE_SUBSCRIBED_ORDER: [],
        SETTING_BILI_FOLDER_ORDER: [],
        SETTING_KUGOU_PLAYLIST_ORDER: [],
        SETTING_LOCAL_PLAYLIST_ORDER: [],
        SETTING_SECTION_ORDER: [],
        SETTING_RECENT_MAX: DEFAULT_RECENT_MAX,
        SETTING_RECENT_LISTS: [],
    }

_COOKIE_NAME_REGEX = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_LOGIN_COOKIE_KEYS = ("MUSIC_U",)
# BiliCookieRepository.kt 的 BILI_LOGIN_COOKIE_KEYS
_BILI_LOGIN_COOKIE_KEYS = ("SESSDATA", "DedeUserID", "bili_jct")
_FALLBACK_OS = "pc"
_FALLBACK_APPVER = "8.10.35"


def default_data_dir() -> Path:
    override = os.environ.get(_DATA_DIR_ENV)
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "neriplayer-win"
    return Path.home() / ".local" / "share" / "neriplayer-win"


def validate_and_sanitize_netease_cookies(
    cookies: dict[str, str], include_fallback_cookies: bool = True
) -> tuple[dict[str, str], list[str]]:
    """对应 validateAndSanitizeNeteaseCookies;返回 (干净 cookie, 被拒键)。"""
    sanitized: dict[str, str] = {}
    rejected: list[str] = []
    for raw_key, raw_value in cookies.items():
        key = raw_key.strip()
        value = raw_value.strip()
        rejected_key = key or "<blank>"
        if (
            not key
            or not _COOKIE_NAME_REGEX.match(key)
            or not value
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
            or ";" in value
        ):
            rejected.append(rejected_key)
        else:
            sanitized[key] = value
    if include_fallback_cookies and sanitized:
        sanitized.setdefault("os", _FALLBACK_OS)
        sanitized.setdefault("appver", _FALLBACK_APPVER)
    return sanitized, rejected


def _validated_sidebar_expanded(value: Any) -> dict[str, bool]:
    """sidebar_expanded 白名单校验:非 dict 或已知键值非 bool 时逐键回落默认。"""
    merged = {section: True for section in _SIDEBAR_SECTIONS}
    if isinstance(value, dict):
        for section in _SIDEBAR_SECTIONS:
            flag = value.get(section)
            if isinstance(flag, bool):
                merged[section] = flag
    return merged


def _validated_id_order(value: Any) -> list[int] | None:
    """排序键校验:list 且元素全为 int(bool 不算——Python 的 bool 是 int 子类)。"""
    if isinstance(value, list) and all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        return value
    return None


def validate_section_order(value: Any) -> list[str]:
    """侧栏分区顺序校验:只留白名单键、去重、保序。

    与 _validated_id_order 同风格:非法输入一律回落空列表(调用方按
    默认顺序渲染),绝不抛异常。未知键静默丢弃,便于将来删分区时
    旧配置自动收敛。
    """
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        if item not in SECTION_ORDER_KEYS:
            continue
        if item in result:
            continue
        result.append(item)
    return result


def apply_section_order(
    current: Sequence[str], stored: Sequence[str]
) -> list[str]:
    """按存储顺序重排当前分区键:已知的按存储序在前,新的追加到尾部。

    与 apply_stored_order 同一语义(它处理的是 int id,这里是 str 键),
    这样新增分区(M6 的酷狗)不会因为旧配置里没有它而被漏掉。
    """
    ordered = [key for key in stored if key in current]
    ordered.extend(key for key in current if key not in ordered)
    return ordered


def _validated_recent_max(value: Any) -> int:
    """recent_max 校验:非法/越界回落默认 8(钳在 1~50)。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return max(_MIN_RECENT_MAX, min(_MAX_RECENT_MAX, value))
    return DEFAULT_RECENT_MAX


def _validated_volume(value: Any) -> int:
    """volume 校验:只收 0~100 的 int(bool 不算),其余回落默认 70。

    不做钳位而是回落默认:音量是一个连续可调量,越界值只可能来自
    坏配置,静默截成 100 与静默截成 70 相比,后者更容易被用户察觉
    并纠正,也不会突然把音量推到极值。
    """
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 100
    ):
        return value
    return DEFAULT_VOLUME


def _validated_int(value: Any, default: int) -> int:
    """宽松整数校验:非 int(含 bool)或不可转换时回落 default。

    用于酷狗凭据里的时间戳/等级等字段:这类值损坏不该让整份凭据作废,
    丢一个字段比丢整份登录态代价小。
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _validated_recent_lists(value: Any, max_entries: int) -> list[dict[str, Any]]:
    """recent_lists 校验:逐条过滤(kind 白名单 / id int / title str),
    保序去重(同 kind+id 只留首个),截断到 max_entries。"""
    if not isinstance(value, list):
        return []
    seen: set[tuple[str, int]] = set()
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or len(result) >= max_entries:
            continue
        kind = item.get("kind")
        entry_id = item.get("id")
        title = item.get("title")
        if (
            kind not in RECENT_KINDS
            or not isinstance(entry_id, int)
            or isinstance(entry_id, bool)
            or entry_id < 0
            or not isinstance(title, str)
        ):
            continue
        if (kind, entry_id) in seen:
            continue
        seen.add((kind, entry_id))
        result.append({"kind": kind, "id": entry_id, "title": title})
    return result


def apply_stored_order(stored: Sequence[int], current: Sequence[int]) -> list[int]:
    """分区内显示顺序(M5 纯函数):按存储顺序重排 current 的 id 列表。

    - 存储里仍存在的 id 按存储序排前(存储里的重复项只保留首个);
    - current 新出现的 id 追加尾部(保持其相对顺序);
    - 存储里已失效的 id 自然丢弃——调用方比较结果与存储即可静默清理。
    """
    current_ids = set(current)
    seen: set[int] = set()
    ordered: list[int] = []
    for item_id in stored:
        if item_id in current_ids and item_id not in seen:
            seen.add(item_id)
            ordered.append(item_id)
    ordered.extend(item_id for item_id in current if item_id not in seen)
    return ordered


# -- 本地歌单(M7)数据模型 ----------------------------------------------------

@dataclass
class LocalPlaylistItem:
    """本地歌单项:标识 (source, id) + 音源私有解析参数 extra + 展示快照。

    存完整快照(而非只存标识)的决定性理由:启动时无需回源即可拿到
    cover_url 交给封面预取,断网时也能列出曲目名。播放仍按 source+id
    重新解析,快照只影响展示。
    """

    source: str
    id: int
    extra: dict[str, str] = field(default_factory=dict)
    title: str = ""
    artist: str = ""
    duration_ms: int = 0
    cover_url: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """歌单内去重键。

        默认是 (source, id)。酷狗歌曲的 id 恒为 0(真实标识是字符串
        hash,存在 extra["bvid"]),若只用 (source, id) 会把所有酷狗歌
        判成同一首,所以这里把 extra 里的标识并入键。
        B站 id 本身就是 avid,不受影响。
        """
        marker = self.extra.get("bvid") if self.extra else ""
        if marker:
            return (self.source, f"{self.id}:{marker}")
        return (self.source, str(self.id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "id": self.id,
            "extra": dict(self.extra),
            "title": self.title,
            "artist": self.artist,
            "duration_ms": self.duration_ms,
            "cover_url": self.cover_url,
        }


@dataclass
class LocalPlaylist:
    """本地歌单。cover_url 只承载手动指定的封面 URL,自动封面永不落盘。"""

    id: int
    name: str
    created_at: int = 0
    cover_url: str = ""
    items: list[LocalPlaylistItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "cover_url": self.cover_url,
            "items": [item.to_dict() for item in self.items],
        }


def local_playlist_cover_url(playlist: LocalPlaylist) -> str:
    """歌单封面的唯一判定点:手动值优先,否则取第一首歌的封面。

    自动值(首曲 cover_url)只在展示时现算、永不回写 playlist.cover_url,
    因此「清空 cover_url」即恢复自动封面,不存在不可逆的手动覆盖。
    """
    if playlist.cover_url:
        return playlist.cover_url
    return playlist.items[0].cover_url if playlist.items else ""


def next_local_playlist_id(playlists: Sequence[LocalPlaylist]) -> int:
    """分配新歌单 id:max(已有 id) + 1;空列表从 1 开始。"""
    return max((playlist.id for playlist in playlists), default=0) + 1


def rename_local_playlist(
    playlists: Sequence[LocalPlaylist], playlist_id: int, name: str
) -> bool:
    """重命名本地歌单;返回是否真的改动了。

    - trim 后为空 → False
    - 超过 LOCAL_PLAYLIST_NAME_MAX 截断到上限(不是拒绝)
    - 与另一歌单重名(trim 后完全相同)→ False
    - 与自身原名相同 → False(无变化,调用方不必落盘)
    - 歌单 id 不存在 → False
    - 成功 → 原地改 playlist.name,返回 True
    """
    target = next(
        (playlist for playlist in playlists if playlist.id == playlist_id), None
    )
    if target is None:
        return False
    trimmed = name.strip()[:LOCAL_PLAYLIST_NAME_MAX]
    if not trimmed:
        return False
    if any(
        playlist is not target and playlist.name.strip() == trimmed
        for playlist in playlists
    ):
        return False
    if target.name.strip() == trimmed:
        return False
    target.name = trimmed
    return True


def delete_local_playlist(
    playlists: list[LocalPlaylist], playlist_id: int
) -> LocalPlaylist | None:
    """原地从列表移除该歌单并返回被删对象;id 不存在返回 None。"""
    for index, playlist in enumerate(playlists):
        if playlist.id == playlist_id:
            return playlists.pop(index)
    return None


def move_local_playlist_in_order(
    ids: Sequence[int], playlist_id: int, delta: int
) -> list[int] | None:
    """返回把 playlist_id 在 id 序列里移动 delta 位后的新序列。

    - 目标下标越界(<0 或 >= len)→ None
    - playlist_id 不在 ids 里 → None
    - delta == 0 或移动后位置与原位置相同 → None
    - 成功 → 返回新列表(不改动入参)
    """
    order = list(ids)
    try:
        index = order.index(playlist_id)
    except ValueError:
        return None
    target = index + delta
    if target < 0 or target >= len(order) or target == index:
        return None
    order.insert(target, order.pop(index))
    return order


def move_local_playlist_item(
    playlist: LocalPlaylist, from_index: int, to_index: int
) -> bool:
    """把 playlist.items[from_index] 移到 to_index(移动后该元素位于该下标)。

    - 任一越界或 from_index == to_index → False
    - 成功 → 原地移动并返回 True
    """
    items = playlist.items
    if from_index == to_index:
        return False
    if not (0 <= from_index < len(items)) or not (0 <= to_index < len(items)):
        return False
    items.insert(to_index, items.pop(from_index))
    return True


def remove_local_playlist_item(
    playlist: LocalPlaylist, key: tuple[str, str]
) -> LocalPlaylistItem | None:
    """按 LocalPlaylistItem.key 移除并返回该条目;不存在返回 None。

    必须用 item.key 比较,不能直接写 (source, id):酷狗歌曲 id 恒为 0,
    真实标识在 extra["bvid"] 里,LocalPlaylistItem.key 负责这点。
    """
    for index, item in enumerate(playlist.items):
        if item.key == key:
            return playlist.items.pop(index)
    return None


def _validated_local_extra(value: Any) -> dict[str, str] | None:
    """extra 校验:dict[str, str];键值超长截断而不是丢弃条目。"""
    if not isinstance(value, dict):
        return None
    extra: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            return None
        extra[raw_key[:32]] = raw_value[:2048]
    return extra


def _validated_local_playlist_item(value: Any) -> LocalPlaylistItem | None:
    """单个条目校验:任一字段不合格返回 None(由调用方丢弃该条目)。"""
    if not isinstance(value, dict):
        return None
    source = value.get("source")
    entry_id = value.get("id")
    extra = _validated_local_extra(value.get("extra"))
    title = value.get("title")
    artist = value.get("artist")
    duration_ms = value.get("duration_ms")
    cover_url = value.get("cover_url")
    # id 下限按音源区分:网易云/B站用正数 id,酷狗 id 恒为 0(真实标识
    # 是字符串 hash,存在 extra["bvid"]),所以酷狗允许 0 但必须有 bvid。
    if source == "kugou":
        id_ok = (
            isinstance(entry_id, int)
            and not isinstance(entry_id, bool)
            and entry_id == 0
            and bool(extra and extra.get("bvid"))
        )
    else:
        id_ok = (
            isinstance(entry_id, int)
            and not isinstance(entry_id, bool)
            and entry_id > 0
        )
    if (
        source not in LOCAL_SONG_SOURCES
        or not id_ok
        or extra is None
        or not isinstance(title, str)
        or not isinstance(artist, str)
        or not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or duration_ms < 0
        or not isinstance(cover_url, str)
    ):
        return None
    return LocalPlaylistItem(
        source=source,
        id=entry_id,
        extra=extra,
        title=title,
        artist=artist,
        duration_ms=duration_ms,
        cover_url=cover_url,
    )


def _validated_local_playlists(value: Any) -> list[LocalPlaylist]:
    """local_playlists.json 的 version+playlists 校验。

    规则对照 _validated_recent_lists:顶层不合格返回空列表,歌单/条目
    逐条过滤,歌单 id 与条目 (source, id) 保序去重,条目超上限截断,
    歌单名超长截断。任何异常输入都静默丢弃,绝不抛异常。
    """
    if not isinstance(value, dict):
        return []
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return []
    raw_playlists = value.get("playlists")
    if not isinstance(raw_playlists, list):
        return []
    playlists: list[LocalPlaylist] = []
    seen_ids: set[int] = set()
    for raw in raw_playlists:
        if not isinstance(raw, dict):
            continue
        playlist_id = raw.get("id")
        name = raw.get("name")
        created_at = raw.get("created_at")
        cover_url = raw.get("cover_url")
        raw_items = raw.get("items")
        if (
            not isinstance(playlist_id, int)
            or isinstance(playlist_id, bool)
            or playlist_id <= 0
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(created_at, int)
            or isinstance(created_at, bool)
            or not isinstance(cover_url, str)
            or not isinstance(raw_items, list)
        ):
            continue
        if playlist_id in seen_ids:
            continue  # 同 id 只留首个
        seen_ids.add(playlist_id)
        items: list[LocalPlaylistItem] = []
        seen_items: set[tuple[str, int]] = set()
        for raw_item in raw_items:
            if len(items) >= LOCAL_PLAYLIST_MAX_ITEMS:
                break
            item = _validated_local_playlist_item(raw_item)
            if item is None or item.key in seen_items:
                continue
            seen_items.add(item.key)
            items.append(item)
        playlists.append(
            LocalPlaylist(
                id=playlist_id,
                name=name[:LOCAL_PLAYLIST_NAME_MAX],
                created_at=created_at,
                cover_url=cover_url,
                items=items,
            )
        )
    return playlists


class LocalStore:
    """登录态与账号信息的落盘存取。"""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self._netease_path = self._dir / _NETEASE_FILE
        self._bili_path = self._dir / _BILI_FILE
        self._settings_path = self._dir / _SETTINGS_FILE
        self._local_playlists_path = self._dir / _LOCAL_PLAYLISTS_FILE
        self._kugou_path = self._dir / _KUGOU_FILE

    @property
    def netease_path(self) -> Path:
        return self._netease_path

    @property
    def bili_path(self) -> Path:
        return self._bili_path

    @property
    def settings_path(self) -> Path:
        return self._settings_path

    @property
    def local_playlists_path(self) -> Path:
        return self._local_playlists_path

    @property
    def kugou_path(self) -> Path:
        return self._kugou_path

    # -- 网易云登录态 ---------------------------------------------------------

    def load_netease(self) -> dict[str, Any] | None:
        """读取 {"cookies": ..., "savedAt": ..., "profile": ...};无或损坏返回 None。"""
        try:
            raw = self._netease_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            bundle = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(bundle, dict):
            return None
        cookies = bundle.get("cookies")
        if not isinstance(cookies, dict) or not cookies:
            return None
        sanitized, _ = validate_and_sanitize_netease_cookies(cookies)
        if not any(sanitized.get(k, "").strip() for k in _LOGIN_COOKIE_KEYS):
            return None
        profile = bundle.get("profile") if isinstance(bundle.get("profile"), dict) else {}
        return {
            "cookies": sanitized,
            "savedAt": bundle.get("savedAt", 0),
            "profile": profile,
        }

    def save_netease(
        self,
        cookies: dict[str, str],
        profile: dict[str, Any] | None = None,
    ) -> bool:
        """登录成功后写盘;cookie 无 MUSIC_U 时拒绝(对齐 saveCookiesLocked)。"""
        sanitized, _ = validate_and_sanitize_netease_cookies(cookies)
        if not any(sanitized.get(k, "").strip() for k in _LOGIN_COOKIE_KEYS):
            return False
        bundle = {
            "cookies": sanitized,
            "savedAt": int(time.time() * 1000),
            "profile": profile or {},
        }
        self._dir.mkdir(parents=True, exist_ok=True)
        self._netease_path.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True

    def clear_netease(self) -> None:
        """登录失效时清除。"""
        try:
            self._netease_path.unlink()
        except OSError:
            pass

    # -- B站登录态 -------------------------------------------------------------

    def load_bili(self) -> dict[str, Any] | None:
        """读取 B站登录包 {"cookies", "savedAt", "profile"};无/损坏/未登录返回 None。

        对应 BiliAuthBundle.fromJson + hasLoginCookies(无 SESSDATA 视为 Missing)。
        """
        try:
            raw = self._bili_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            bundle = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(bundle, dict):
            return None
        cookies = bundle.get("cookies")
        if not isinstance(cookies, dict) or not cookies:
            return None
        sanitized, _ = validate_and_sanitize_netease_cookies(
            cookies, include_fallback_cookies=False
        )
        if not sanitized.get("SESSDATA", "").strip():
            return None
        profile = bundle.get("profile") if isinstance(bundle.get("profile"), dict) else {}
        return {
            "cookies": sanitized,
            "savedAt": bundle.get("savedAt", 0),
            "profile": profile,
        }

    def save_bili(
        self,
        cookies: dict[str, str],
        profile: dict[str, Any] | None = None,
    ) -> bool:
        """B站登录成功后写盘;cookie 无 SESSDATA 时拒绝。"""
        sanitized, _ = validate_and_sanitize_netease_cookies(
            cookies, include_fallback_cookies=False
        )
        if not sanitized.get("SESSDATA", "").strip():
            return False
        bundle = {
            "cookies": sanitized,
            "savedAt": int(time.time() * 1000),
            "profile": profile or {},
        }
        self._dir.mkdir(parents=True, exist_ok=True)
        self._bili_path.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True

    def clear_bili(self) -> None:
        """B站登录失效时清除。"""
        try:
            self._bili_path.unlink()
        except OSError:
            pass

    # -- 应用设置(M3) ----------------------------------------------------------

    def load_settings(self) -> dict[str, Any]:
        """读取 settings.json;无文件/损坏/非法值时回落默认,结果恒为合法。"""
        merged = default_settings()
        try:
            raw = self._settings_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return merged
        if not isinstance(data, dict):
            return merged
        if data.get(SETTING_CLOSE_ACTION) in _VALID_CLOSE_ACTIONS:
            merged[SETTING_CLOSE_ACTION] = data[SETTING_CLOSE_ACTION]
        if data.get(SETTING_PLAY_MODE) in _VALID_PLAY_MODES:
            merged[SETTING_PLAY_MODE] = data[SETTING_PLAY_MODE]
        if data.get(SETTING_APPEARANCE) in _VALID_APPEARANCES:
            merged[SETTING_APPEARANCE] = data[SETTING_APPEARANCE]
        if data.get(SETTING_PLAY_QUALITY) in _VALID_PLAY_QUALITIES:
            merged[SETTING_PLAY_QUALITY] = data[SETTING_PLAY_QUALITY]
        if data.get(SETTING_LANGUAGE) in _VALID_LANGUAGES:
            merged[SETTING_LANGUAGE] = data[SETTING_LANGUAGE]
        if isinstance(data.get(SETTING_DYNAMIC_COLOR), bool):
            merged[SETTING_DYNAMIC_COLOR] = data[SETTING_DYNAMIC_COLOR]
        ambient = data.get(SETTING_AMBIENT_EFFECT)
        if ambient in _VALID_AMBIENT_EFFECTS:
            merged[SETTING_AMBIENT_EFFECT] = ambient
        merged[SETTING_VOLUME] = _validated_volume(data.get(SETTING_VOLUME))
        merged[SETTING_SIDEBAR_EXPANDED] = _validated_sidebar_expanded(
            data.get(SETTING_SIDEBAR_EXPANDED)
        )
        for key in _SETTING_ID_ORDERS:
            order = _validated_id_order(data.get(key))
            if order is not None:
                merged[key] = order
        merged[SETTING_SECTION_ORDER] = validate_section_order(
            data.get(SETTING_SECTION_ORDER)
        )
        # 先取 max 再按它截断最近列表(存量超限时静默裁掉)
        merged[SETTING_RECENT_MAX] = _validated_recent_max(data.get(SETTING_RECENT_MAX))
        merged[SETTING_RECENT_LISTS] = _validated_recent_lists(
            data.get(SETTING_RECENT_LISTS), merged[SETTING_RECENT_MAX]
        )
        return merged

    def save_settings(self, settings: Mapping[str, Any]) -> bool:
        """写盘(已知键取合法值,未知键忽略);IO 失败返回 False。"""
        merged = default_settings()
        if settings.get(SETTING_CLOSE_ACTION) in _VALID_CLOSE_ACTIONS:
            merged[SETTING_CLOSE_ACTION] = settings[SETTING_CLOSE_ACTION]
        if settings.get(SETTING_PLAY_MODE) in _VALID_PLAY_MODES:
            merged[SETTING_PLAY_MODE] = settings[SETTING_PLAY_MODE]
        if settings.get(SETTING_APPEARANCE) in _VALID_APPEARANCES:
            merged[SETTING_APPEARANCE] = settings[SETTING_APPEARANCE]
        if settings.get(SETTING_PLAY_QUALITY) in _VALID_PLAY_QUALITIES:
            merged[SETTING_PLAY_QUALITY] = settings[SETTING_PLAY_QUALITY]
        if settings.get(SETTING_LANGUAGE) in _VALID_LANGUAGES:
            merged[SETTING_LANGUAGE] = settings[SETTING_LANGUAGE]
        if isinstance(settings.get(SETTING_DYNAMIC_COLOR), bool):
            merged[SETTING_DYNAMIC_COLOR] = settings[SETTING_DYNAMIC_COLOR]
        merged[SETTING_VOLUME] = _validated_volume(settings.get(SETTING_VOLUME))
        merged[SETTING_SIDEBAR_EXPANDED] = _validated_sidebar_expanded(
            settings.get(SETTING_SIDEBAR_EXPANDED)
        )
        for key in _SETTING_ID_ORDERS:
            order = _validated_id_order(settings.get(key))
            if order is not None:
                merged[key] = order
        merged[SETTING_SECTION_ORDER] = validate_section_order(
            settings.get(SETTING_SECTION_ORDER)
        )
        merged[SETTING_RECENT_MAX] = _validated_recent_max(settings.get(SETTING_RECENT_MAX))
        merged[SETTING_RECENT_LISTS] = _validated_recent_lists(
            settings.get(SETTING_RECENT_LISTS), merged[SETTING_RECENT_MAX]
        )
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            return False
        return True

    # -- 本地歌单(M7) ---------------------------------------------------------

    def load_local_playlists(self) -> list[LocalPlaylist]:
        """读取 local_playlists.json;无文件/损坏/非法结构一律回落空列表。

        兜底结构与 load_netease 同构(OSError / JSONDecodeError / 非 dict
        三段式),损坏时不备份原文件:与 load_settings 的一致性优先,
        只在日志里留一行 warning。
        """
        try:
            raw = self._local_playlists_path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("local_playlists.json 损坏,按空列表处理")
            return []
        playlists = _validated_local_playlists(data)
        if not playlists and isinstance(data, dict) and data.get("playlists"):
            logger.warning("local_playlists.json 无可解析歌单,按空列表处理")
        return playlists

    def save_local_playlists(self, playlists: Sequence[LocalPlaylist]) -> bool:
        """写盘(整份覆盖,写临时文件后 os.replace);IO 失败返回 False。

        写频率是用户显式动作(新建/加歌/拖拽)的量级,不做防抖;原子替换
        防的是写到一半崩溃留下半截 JSON,与封面缓存的写盘写法一致。
        """
        payload = {
            "version": LOCAL_PLAYLISTS_VERSION,
            "playlists": [playlist.to_dict() for playlist in playlists],
        }
        tmp_path = self._local_playlists_path.with_name(
            f"{self._local_playlists_path.name}.{os.getpid()}.tmp"
        )
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp_path, self._local_playlists_path)
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return False
        return True

    # -- 酷狗登录态(M6) -------------------------------------------------------

    def load_kugou(self) -> dict[str, Any] | None:
        """读取酷狗登录包;无文件/损坏/未登录返回 None。

        结构对齐 netease/bili 的 {"cookies", "savedAt", "profile"} 习惯,
        但酷狗有两条独立凭据链(见 docs/research/M6-kugou-api.md §2.6):
        - token 系列:扫码登录下发,歌单/收藏用,可续期
        - kugoo_cookie:网页登录下发的 KuGoo 复合串,播放地址用,不可推导
        两组分别落在 "client" 与 "web" 两个节点,任一缺失只影响对应功能。
        """
        try:
            raw = self._kugou_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            bundle = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("kugou.json 损坏,按未登录处理")
            return None
        if not isinstance(bundle, dict):
            return None
        client = bundle.get("client")
        web = bundle.get("web")
        if not isinstance(client, dict) and not isinstance(web, dict):
            return None
        client = client if isinstance(client, dict) else {}
        web = web if isinstance(web, dict) else {}
        userid = str(client.get("userid") or web.get("userid") or "").strip()
        token = str(client.get("token") or "").strip()
        kugoo_cookie = str(web.get("kugoo_cookie") or "").strip()
        # 两条链都没有可用凭据才算未登录
        if not token and not kugoo_cookie:
            return None
        return {
            "userid": userid,
            "token": token,
            "t1": str(client.get("t1") or ""),
            "vip_token": str(client.get("vip_token") or ""),
            "vip_type": _validated_int(client.get("vip_type"), 0),
            "t_expire_time": _validated_int(client.get("t_expire_time"), 0),
            "nickname": str(client.get("nickname") or web.get("nickname") or ""),
            "mid": str(client.get("mid") or web.get("mid") or ""),
            "dfid": str(client.get("dfid") or web.get("dfid") or ""),
            "kugoo_cookie": kugoo_cookie,
            "kugoo_cookie_at": _validated_int(web.get("savedAt"), 0),
            "savedAt": _validated_int(bundle.get("savedAt"), 0),
        }

    def save_kugou(self, credentials: Mapping[str, Any]) -> bool:
        """写盘酷狗凭据;IO 失败返回 False。

        与 save_settings 同风格:整份覆盖 + 临时文件原子替换。
        凭据是用户显式登录动作的产物,不做防抖。
        """
        token = str(credentials.get("token") or "").strip()
        kugoo_cookie = str(credentials.get("kugoo_cookie") or "").strip()
        if not token and not kugoo_cookie:
            return False
        payload = {
            "savedAt": int(time.time() * 1000),
            "client": {
                "userid": str(credentials.get("userid") or ""),
                "token": token,
                "t1": str(credentials.get("t1") or ""),
                "vip_token": str(credentials.get("vip_token") or ""),
                "vip_type": _validated_int(credentials.get("vip_type"), 0),
                "t_expire_time": _validated_int(credentials.get("t_expire_time"), 0),
                "nickname": str(credentials.get("nickname") or ""),
                "mid": str(credentials.get("mid") or ""),
                "dfid": str(credentials.get("dfid") or ""),
            },
            "web": {
                "userid": str(credentials.get("userid") or ""),
                "kugoo_cookie": kugoo_cookie,
                "nickname": str(credentials.get("nickname") or ""),
                "mid": str(credentials.get("mid") or ""),
                "dfid": str(credentials.get("dfid") or ""),
                "savedAt": _validated_int(credentials.get("kugoo_cookie_at"), 0)
                or int(time.time() * 1000),
            },
        }
        tmp_path = self._kugou_path.with_name(
            f"{self._kugou_path.name}.{os.getpid()}.tmp"
        )
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp_path, self._kugou_path)
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return False
        return True

    def clear_kugou(self) -> None:
        """退出登录:删除 kugou.json(含两条凭据链)。"""
        try:
            self._kugou_path.unlink()
        except OSError:
            pass
