"""本地持久化:登录 cookie 与账号信息(网易云 netease.json + B站 bili.json)。

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
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..i18n import DEFAULT_LANGUAGE, VALID_LANGUAGES

_DATA_DIR_ENV = "NERIPLAYER_WIN_DATA_DIR"
_NETEASE_FILE = "netease.json"
_BILI_FILE = "bili.json"
_SETTINGS_FILE = "settings.json"

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
SETTING_SIDEBAR_EXPANDED = "sidebar_expanded"
SETTING_NETEASE_PLAYLIST_ORDER = "netease_playlist_order"
SETTING_NETEASE_SUBSCRIBED_ORDER = "netease_subscribed_order"
SETTING_BILI_FOLDER_ORDER = "bili_folder_order"
# 「最近」列表(M5):recent_max 记录侧栏展示条数(默认 8);recent_lists
# 是最近播放过的列表栈(新→旧),元素 {"kind", "id", "title"},kind 取
# RECENT_KINDS 白名单。仅浏览不计,起播才压栈(由 MainWindow 负责)。
SETTING_RECENT_MAX = "recent_max"
SETTING_RECENT_LISTS = "recent_lists"
DEFAULT_CLOSE_ACTION = "tray"
DEFAULT_PLAY_MODE = "sequence"
DEFAULT_APPEARANCE = "dark"
DEFAULT_PLAY_QUALITY = "lossless"
DEFAULT_RECENT_MAX = 8
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
# 最近列表的来源种类:网易云自建/收藏歌单、B站收藏夹/稍后再看、每日推荐、搜索
RECENT_KINDS = (
    "netease-playlist",
    "netease-subscribed-playlist",
    "bili-folder",
    "bili-watchlater",
    "netease-daily",
    "search",
)
_SIDEBAR_SECTIONS = ("netease", "netease-subscribed", "bili", "recent")
_SETTING_ID_ORDERS = (
    SETTING_NETEASE_PLAYLIST_ORDER,
    SETTING_NETEASE_SUBSCRIBED_ORDER,
    SETTING_BILI_FOLDER_ORDER,
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
        SETTING_SIDEBAR_EXPANDED: {section: True for section in _SIDEBAR_SECTIONS},
        SETTING_NETEASE_PLAYLIST_ORDER: [],
        SETTING_NETEASE_SUBSCRIBED_ORDER: [],
        SETTING_BILI_FOLDER_ORDER: [],
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


def _validated_recent_max(value: Any) -> int:
    """recent_max 校验:非法/越界回落默认 8(钳在 1~50)。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return max(_MIN_RECENT_MAX, min(_MAX_RECENT_MAX, value))
    return DEFAULT_RECENT_MAX


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


class LocalStore:
    """登录态与账号信息的落盘存取。"""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self._netease_path = self._dir / _NETEASE_FILE
        self._bili_path = self._dir / _BILI_FILE
        self._settings_path = self._dir / _SETTINGS_FILE

    @property
    def netease_path(self) -> Path:
        return self._netease_path

    @property
    def bili_path(self) -> Path:
        return self._bili_path

    @property
    def settings_path(self) -> Path:
        return self._settings_path

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
        merged[SETTING_SIDEBAR_EXPANDED] = _validated_sidebar_expanded(
            data.get(SETTING_SIDEBAR_EXPANDED)
        )
        for key in _SETTING_ID_ORDERS:
            order = _validated_id_order(data.get(key))
            if order is not None:
                merged[key] = order
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
        merged[SETTING_SIDEBAR_EXPANDED] = _validated_sidebar_expanded(
            settings.get(SETTING_SIDEBAR_EXPANDED)
        )
        for key in _SETTING_ID_ORDERS:
            order = _validated_id_order(settings.get(key))
            if order is not None:
                merged[key] = order
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
