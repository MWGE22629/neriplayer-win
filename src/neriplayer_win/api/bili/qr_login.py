"""B站扫码登录客户端(纯 API,UI 主路径用网页登录,此处留 API + 测试)。

逐字对照 reference/NeriPlayer-Android 的
core/api/bili/BiliQrLoginClient.kt:申请 qrcode key、轮询确认、
Set-Cookie 手工解析(过期/Max-Age=0 判删除)、网络错误重试 3 次。

结论(与网易云同教训):桌面端直连扫码可能被风控,B站网页登录为主路径;
本客户端保持可用性,不接 UI。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from .models import BiliApiError, BiliQrLoginCheckResult, BiliQrLoginSession

BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILI_QR_REFERER = "https://passport.bilibili.com/login"
BILI_QR_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BILI_QR_NETWORK_RETRY_COUNT = 3
BILI_QR_NETWORK_RETRY_DELAY_MS = 0.1


def parse_set_cookie_header(header: str) -> tuple[str, str, bool] | None:
    """对应 parseSetCookieHeader;返回 (name, value, removed) 或 None。

    removed:值为空、Max-Age=0 或 Expires=1970 开头。
    """
    parts = [p.strip() for p in header.split(";")]
    name_value = parts[0] if parts else ""
    separator_index = name_value.find("=")
    if separator_index <= 0:
        return None
    name = name_value[:separator_index].strip()
    value = name_value[separator_index + 1 :].strip()
    if not name:
        return None
    removed = not value or any(
        part.lower() == "max-age=0"
        or part.lower().startswith("expires=thu, 01 jan 1970")
        for part in parts
    )
    return name, value, removed


def _is_retryable_network_error(error: httpx.HTTPError) -> bool:
    """对应 IOException.isRetryableNetworkError(DNS/连接/超时类)。"""
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout)):
        return True
    text = str(error).lower()
    return "dns" in text or "resolve" in text or "hostname" in text


class BiliQrLoginClient:
    """QR 登录客户端;自带 cookie 存储(成功后交由上层持久化)。"""

    def __init__(self, timeout: httpx.Timeout | None = None) -> None:
        self._timeout = timeout or httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=15.0)
        self._http = httpx.Client(timeout=self._timeout)
        self._cookie_store: dict[str, str] = {}
        self._cookie_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def reset(self) -> None:
        """对应 reset:清空本次登录会话的 cookie。"""
        with self._cookie_lock:
            self._cookie_store.clear()

    def current_cookies(self) -> dict[str, str]:
        with self._cookie_lock:
            return dict(self._cookie_store)

    # -- 对外 API -------------------------------------------------------------

    def create_session(self) -> BiliQrLoginSession:
        """申请二维码(对应 createSession)。"""
        root = self._execute_json(BILI_QR_GENERATE_URL)
        code = root.get("code", -1)
        code = code if isinstance(code, int) else -1
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        key = str(data.get("qrcode_key") or "").strip()
        url = str(data.get("url") or "").strip()
        if code != 0 or not key or not url:
            message = self._read_message(root)
            raise BiliApiError(
                f"创建B站扫码会话失败: {message or f'code={code}'}", code=code
            )
        return BiliQrLoginSession(key=key, qr_content=url)

    def check_login(self, session: BiliQrLoginSession) -> BiliQrLoginCheckResult:
        """轮询扫码状态(对应 checkLogin)。"""
        url = f"{BILI_QR_POLL_URL}?{urlencode({'qrcode_key': session.key})}"
        root = self._execute_json(url)
        root_code = root.get("code", -1)
        root_code = root_code if isinstance(root_code, int) else -1
        if root_code != 0:
            message = self._read_message(root)
            raise BiliApiError(
                f"B站扫码轮询失败: {message or f'code={root_code}'}", code=root_code
            )
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        code = data.get("code", root_code)
        code = code if isinstance(code, int) else root_code
        message = str(data.get("message") or "") or self._read_message(root)
        cookies = self.current_cookies() if code == 0 else {}
        return BiliQrLoginCheckResult(code=code, message=message, cookies=cookies)

    # -- 内部实现 -------------------------------------------------------------

    def _current_cookie_header(self) -> str:
        with self._cookie_lock:
            return "; ".join(f"{k}={v}" for k, v in self._cookie_store.items())

    def _store_set_cookie_headers(self, headers: list[str]) -> None:
        """对应 storeSetCookieHeaders。"""
        if not headers:
            return
        with self._cookie_lock:
            for header in headers:
                update = parse_set_cookie_header(header)
                if update is None:
                    continue
                name, value, removed = update
                if removed:
                    self._cookie_store.pop(name, None)
                else:
                    self._cookie_store[name] = value

    def _execute_json(self, url: str) -> dict[str, Any]:
        last_error: BiliApiError | None = None
        for attempt in range(BILI_QR_NETWORK_RETRY_COUNT):
            try:
                return self._execute_json_once(url)
            except BiliApiError as error:
                retryable = _is_retryable_network_error(error.__cause__) if error.__cause__ else False
                if not retryable or attempt == BILI_QR_NETWORK_RETRY_COUNT - 1:
                    raise
                last_error = error
                time.sleep(BILI_QR_NETWORK_RETRY_DELAY_MS)
        raise last_error or BiliApiError("B站扫码请求失败")

    def _execute_json_once(self, url: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": BILI_QR_REFERER,
            "User-Agent": BILI_QR_WEB_UA,
        }
        cookie_header = self._current_cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            response = self._http.get(url, headers=headers)
        except httpx.HTTPError as error:
            raise BiliApiError(
                f"网络请求失败: {url}({error.__class__.__name__}: {error})"
            ) from error
        self._store_set_cookie_headers(response.headers.get_list("set-cookie"))
        text = response.content.decode("utf-8", errors="replace")
        if response.status_code < 200 or response.status_code >= 300:
            raise BiliApiError(f"HTTP {response.status_code}: {text[:200]}")
        if not text.strip():
            raise BiliApiError("B站扫码登录响应为空")
        try:
            root = json.loads(text)
        except (json.JSONDecodeError, TypeError) as error:
            raise BiliApiError(f"B站扫码登录响应非 JSON: {text[:120]}") from error
        if not isinstance(root, dict):
            raise BiliApiError("B站扫码登录响应结构异常")
        return root

    @staticmethod
    def _read_message(root: Mapping[str, Any]) -> str:
        return str(root.get("message") or root.get("msg") or "")
