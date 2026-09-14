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
from typing import Any, Mapping

_DATA_DIR_ENV = "NERIPLAYER_WIN_DATA_DIR"
_NETEASE_FILE = "netease.json"
_BILI_FILE = "bili.json"
_SETTINGS_FILE = "settings.json"

# M3 设置项:关闭行为与默认播放模式。
# close_action 默认 "tray"(最小化到托盘,符合播放器习惯);"exit" 直接退出。
# play_mode 对应 player.queue.PlayMode 的枚举值。
SETTING_CLOSE_ACTION = "close_action"
SETTING_PLAY_MODE = "play_mode"
DEFAULT_CLOSE_ACTION = "tray"
DEFAULT_PLAY_MODE = "sequence"
_VALID_CLOSE_ACTIONS = ("exit", "tray")
_VALID_PLAY_MODES = ("sequence", "shuffle", "repeat_one")


def default_settings() -> dict[str, Any]:
    """设置的出厂默认值(副本)。"""
    return {
        SETTING_CLOSE_ACTION: DEFAULT_CLOSE_ACTION,
        SETTING_PLAY_MODE: DEFAULT_PLAY_MODE,
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
        return merged

    def save_settings(self, settings: Mapping[str, Any]) -> bool:
        """写盘(已知键取合法值,未知键忽略);IO 失败返回 False。"""
        merged = default_settings()
        if settings.get(SETTING_CLOSE_ACTION) in _VALID_CLOSE_ACTIONS:
            merged[SETTING_CLOSE_ACTION] = settings[SETTING_CLOSE_ACTION]
        if settings.get(SETTING_PLAY_MODE) in _VALID_PLAY_MODES:
            merged[SETTING_PLAY_MODE] = settings[SETTING_PLAY_MODE]
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            return False
        return True
