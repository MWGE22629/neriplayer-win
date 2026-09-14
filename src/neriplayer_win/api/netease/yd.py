"""网易指纹(YD)设备令牌抓取:隐藏 QWebEnginePage 复刻 Android WebView 方案。

逐项对照 reference 的 NeteaseYdDeviceTokenProvider.kt:
- 加载 https://music.163.com/(桌面 UA),等待页面脚本就绪
- 调用页面自带的 createNEFingerprint({appId, timeout: 6000}).getToken()
- 收集页面下发的 Cookie(重点 sDeviceId)
- 任一环节失败都软回退:返回只含 Cookie(或全空)的快照,不阻断登录

差异:Kotlin 用 WebView 的 JavascriptInterface 桥接,这里用
QWebEnginePage.runJavaScript(Qt 6 会解析脚本返回的 Promise);桥接脚本
相应改为返回 JSON 字符串。QWebEngine 与 Android WebView 同为 Chromium
内核,指纹采集环境一致,令牌质量与真实浏览器登录相同。

必须在 Qt UI 线程使用(依赖 Qt 事件循环)。
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)

from .models import NeteaseYdSnapshot

_PAGE_URL = "https://music.163.com/"


class NeteaseCdnFallbackInterceptor(QWebEngineUrlRequestInterceptor):
    """把当前网络不可达的网易静态节点改投到可用镜像。

    实测部分网络对 CDN 边缘 IP 120.226.20.41 路由不通,s3.music.126.net /
    st.music.163.com 等域名恰好全部解析到该 IP,导致首页与网页登录组件
    永远加载不完、createNEFingerprint 不可用。镜像验证:
    - s3.music.126.net/web/s/* 在 s4.music.126.net 逐字节镜像
    - st.music.163.com/g/*(ct-web-login 组件)在 music.163.com/g/* 直接可取
    仅作用于指纹抓取使用的 off-the-record profile,不影响其他请求。
    """

    _REWRITES = {
        "https://s3.music.126.net/": "https://s4.music.126.net/",
        "https://st.music.163.com/": "https://music.163.com/",
    }

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:
        url = info.requestUrl().toString()
        for source, target in self._REWRITES.items():
            if url.startswith(source):
                info.redirect(QUrl(target + url[len(source):]))
                return
_APP_ID = "9d0ef7e0905d422cba1ecf7e73d77e67"
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
)
_PAGE_TIMEOUT_MS = 15_000
_READY_TIMEOUT_MS = 12_000
_TOKEN_TIMEOUT_MS = 12_000
_READY_POLL_MS = 400
_FIRST_CHECK_DELAY_MS = 1_000

_READY_CHECK_SCRIPT = """
(function() {
  try {
    return JSON.stringify({
      ready: typeof createNEFingerprint === 'function',
      href: location.href
    });
  } catch (error) {
    return JSON.stringify({ready: false, error: String(error)});
  }
})();
"""

_TOKEN_SCRIPT = f"""
(async function() {{
  try {{
    if (typeof createNEFingerprint !== 'function') {{
      return JSON.stringify({{error: 'createNEFingerprint missing'}});
    }}
    var instance = createNEFingerprint({{appId: '{_APP_ID}', timeout: 6000}});
    var result = await instance.getToken();
    return JSON.stringify({{
      token: (result && result.token) || '',
      keys: result ? Object.keys(result) : []
    }});
  }} catch (error) {{
    return JSON.stringify({{error: String(error)}});
  }}
}})();
"""


class YdTokenFetcher(QObject):
    """抓取一次指纹快照;done 恰好触发一次(failed 仅作诊断提示,先于 done)。"""

    done = Signal(object)  # NeteaseYdSnapshot
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._profile: QWebEngineProfile | None = None
        self._page: QWebEnginePage | None = None
        self._cookies: dict[str, str] = {}
        self._finished = False

    # -- 流程 ---------------------------------------------------------------

    def start(self) -> None:
        self._profile = QWebEngineProfile(self)  # 无存储名 = off-the-record
        self._profile.setHttpUserAgent(_DESKTOP_UA)
        self._profile.cookieStore().cookieAdded.connect(self._on_cookie_added)
        self._interceptor = NeteaseCdnFallbackInterceptor(self)
        self._profile.setUrlRequestInterceptor(self._interceptor)

        self._page = QWebEnginePage(self._profile, self)
        self._page.loadFinished.connect(self._on_load_finished)

        # 首页挂有第三方统计脚本,实测 loadFinished 可能永不触发
        # (卡在 80% 等 onload),因此不依赖它:加载启动 1 秒后直接轮询
        # 指纹 API 是否就绪。loadFinished 仅作日志参考。
        QTimer.singleShot(_PAGE_TIMEOUT_MS, self, self._on_page_timeout)
        QTimer.singleShot(_FIRST_CHECK_DELAY_MS, self, lambda: self._wait_fingerprint_ready(_READY_TIMEOUT_MS))
        self._page.load(QUrl(_PAGE_URL))

    # -- 内部 ---------------------------------------------------------------

    def _on_cookie_added(self, cookie) -> None:
        host = cookie.domain().lstrip(".")
        if host not in ("music.163.com", "163.com"):
            return
        name = bytes(cookie.name()).decode("utf-8", errors="replace")
        value = bytes(cookie.value()).decode("utf-8", errors="replace")
        if name:
            self._cookies[name] = value

    def _on_load_finished(self, ok: bool) -> None:
        # 仅日志参考:真正驱动流程的是 start() 里安排的就绪轮询
        if self._finished:
            return

    def _on_page_timeout(self) -> None:
        # 页面在期限内未加载完:软回退返回已收到的 Cookie(Kotlin 同语义)
        self._finish(
            NeteaseYdSnapshot(cookies=dict(self._cookies)), note="指纹页加载超时"
        )

    def _wait_fingerprint_ready(self, deadline_left_ms: int) -> None:
        if self._finished:
            return
        if deadline_left_ms <= 0:
            self._finish(
                NeteaseYdSnapshot(
                    s_device_id=self._cookies.get("sDeviceId", ""),
                    cookies=dict(self._cookies),
                ),
                note="指纹 API 未就绪",
            )
            return
        self._page.runJavaScript(
            _READY_CHECK_SCRIPT,
            lambda raw: self._on_ready_check(raw, deadline_left_ms),
        )

    def _on_ready_check(self, raw: object, deadline_left_ms: int) -> None:
        if self._finished:
            return
        try:
            payload = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            payload = {}
        if payload.get("ready"):
            self._request_token()
        else:
            QTimer.singleShot(
                _READY_POLL_MS,
                self,
                lambda: self._wait_fingerprint_ready(deadline_left_ms - _READY_POLL_MS),
            )

    def _request_token(self) -> None:
        QTimer.singleShot(_TOKEN_TIMEOUT_MS, self, self._on_token_timeout)
        self._page.runJavaScript(_TOKEN_SCRIPT, self._on_token_result)

    def _on_token_timeout(self) -> None:
        self._finish(
            NeteaseYdSnapshot(
                s_device_id=self._cookies.get("sDeviceId", ""),
                cookies=dict(self._cookies),
            ),
            note="指纹令牌获取超时",
        )

    def _on_token_result(self, raw: object) -> None:
        if self._finished:
            return
        try:
            payload = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            payload = {}
        token = str(payload.get("token") or "")
        if token:
            self._finish(
                NeteaseYdSnapshot(
                    token=token,
                    s_device_id=self._cookies.get("sDeviceId", ""),
                    cookies=dict(self._cookies),
                )
            )
        else:
            error = str(payload.get("error") or "空令牌")
            self._finish(
                NeteaseYdSnapshot(
                    s_device_id=self._cookies.get("sDeviceId", ""),
                    cookies=dict(self._cookies),
                ),
                note=f"指纹令牌失败:{error}",
            )

    def _finish(self, snapshot: NeteaseYdSnapshot, note: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        page, profile = self._page, self._profile
        self._page = None
        self._profile = None
        if page is not None:
            page.deleteLater()
        if profile is not None:
            profile.deleteLater()
        if note:
            self.failed.emit(f"{note},已回退继续")
        self.done.emit(snapshot)
