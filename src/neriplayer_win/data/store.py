"""本地持久化:登录 cookie 与账号信息。

存放于 %APPDATA%/neriplayer-win/netease.json(可用环境变量
NERIPLAYER_WIN_DATA_DIR 覆盖以便测试)。

格式对照 reference/NeriPlayer-Android 的
data/auth/netease/NeteaseCookieRepository.kt(NeteaseAuthBundle.toJson):
{"cookies": {name: value}, "savedAt": <epoch_ms>},本项目扩展 "profile" 节点
保存 userId / nickname,避免每次启动都要多打一次账号接口。
cookie 清洗规则照搬 validateAndSanitizeNeteaseCookies。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

_DATA_DIR_ENV = "NERIPLAYER_WIN_DATA_DIR"
_NETEASE_FILE = "netease.json"

_COOKIE_NAME_REGEX = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_LOGIN_COOKIE_KEYS = ("MUSIC_U",)
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

    @property
    def netease_path(self) -> Path:
        return self._netease_path

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
