"""通用网页登录对话框:内嵌 Chromium 打开登录页,用户手动完成登录。

比扫码二维码直连方案更稳:登录发生在真实网页上下文里,指纹/风控由
页面自身的 SDK 完成,支持网页提供的任意登录方式(扫码/手机号等)。
本对话框只负责:加载页面 → 盯 Cookie → 出现成功判据 Cookie 即视为
登录成功,收集全部域 Cookie 交给上层。

泛化说明:网易云与 B站共用此对话框,差异全部收敛在 WebLoginConfig
(登录 URL、成功判据 cookie 名、cookie 域过滤、标题/提示文案、UA、
可选的 CDN 重写拦截器工厂)。注意坑:setUrlRequestInterceptor 不接管
拦截器生命周期,必须持有引用防 GC(见过静默失效的事故)。
标题/提示为语言相关文案:配置经 netease_web_login()/bili_web_login()
工厂函数在弹窗时现造(总取当前语言),对话框本身无需重翻译。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..api.netease.yd import NeteaseCdnFallbackInterceptor
from ..i18n import tr

_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
)
_WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_COOKIE_SETTLE_MS = 800  # 检测到成功判据 Cookie 后稍等,收齐其他登录 Cookie
_POLL_INTERVAL_MS = 400


@dataclass(frozen=True)
class WebLoginConfig:
    """网页登录差异配置(网易云 / B站各一份)。"""

    title: str
    login_url: str
    success_cookie: str  # 出现该 cookie 即判定登录成功
    cookie_host_suffix: str  # 只收集 host == suffix / *.suffix 的 cookie
    hint: str
    user_agent: str
    interceptor_factory: (
        Callable[[QObject], object] | None
    ) = None  # 持引用防 GC;None 表示不需要 CDN 重写


def netease_web_login() -> WebLoginConfig:
    """网易云网页登录配置(弹窗时现造,标题/提示随当前语言)。"""
    return WebLoginConfig(
        title=tr("weblogin.netease.title"),
        login_url="https://music.163.com/#/login",
        success_cookie="MUSIC_U",
        cookie_host_suffix="163.com",
        hint=tr("weblogin.netease.hint"),
        user_agent=_DESKTOP_UA,
        interceptor_factory=lambda parent: NeteaseCdnFallbackInterceptor(parent),
    )


def bili_web_login() -> WebLoginConfig:
    """B站网页登录配置(弹窗时现造,标题/提示随当前语言)。"""
    return WebLoginConfig(
        title=tr("weblogin.bili.title"),
        login_url="https://passport.bilibili.com/login",
        success_cookie="SESSDATA",
        cookie_host_suffix="bilibili.com",
        hint=tr("weblogin.bili.hint"),
        user_agent=_WINDOWS_UA,
        interceptor_factory=None,  # B站域名本机全通(见 M2 连通性报告),无需重写
    )


class BrowserLoginDialog(QDialog):
    """模态网页登录窗口;成功时 login_cookie_ready 携带 Cookie 字典。"""

    login_cookie_ready = Signal(dict)

    def __init__(self, config: WebLoginConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle(config.title)
        self.resize(1120, 780)
        self._cookies: dict[str, str] = {}
        self._detected = False

        self._profile = QWebEngineProfile(self)  # off-the-record,会话隔离
        self._profile.setHttpUserAgent(config.user_agent)
        # 注意:setUrlRequestInterceptor 不接管对象生命周期,
        # 必须持有引用,否则被 GC 后拦截静默失效
        self._interceptor = None
        if config.interceptor_factory is not None:
            self._interceptor = config.interceptor_factory(self)
            self._profile.setUrlRequestInterceptor(self._interceptor)
        cookie_store = self._profile.cookieStore()
        cookie_store.cookieAdded.connect(self._on_cookie_added)

        self._view = QWebEngineView(self)
        self._view.setPage(QWebEnginePage(self._profile, self._view))

        hint = QLabel(config.hint)
        hint.setWordWrap(True)

        done_button = QPushButton(tr("weblogin.done"))
        done_button.clicked.connect(self._finish)
        cancel_button = QPushButton(tr("weblogin.cancel"))
        cancel_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(done_button)
        buttons.addWidget(cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self._view, stretch=1)
        layout.addLayout(buttons)

        self._poll = QTimer(self)
        self._poll.setInterval(_POLL_INTERVAL_MS)
        self._poll.timeout.connect(self._check_login)
        self._poll.start()

        self._view.load(QUrl(config.login_url))

    # -- 内部 ---------------------------------------------------------------

    def _host_allowed(self, host: str) -> bool:
        suffix = self._config.cookie_host_suffix
        return host == suffix or host.endswith(f".{suffix}")

    def _on_cookie_added(self, cookie) -> None:
        host = cookie.domain().lstrip(".")
        if not self._host_allowed(host):
            return
        name = bytes(cookie.name()).decode("utf-8", errors="replace")
        value = bytes(cookie.value()).decode("utf-8", errors="replace")
        if name:
            self._cookies[name] = value

    def _check_login(self) -> None:
        if self._detected:
            return
        if self._cookies.get(self._config.success_cookie):
            self._detected = True
            # 成功判据 Cookie 到手后其他登录 Cookie 可能还在路上,稍等收齐
            QTimer.singleShot(_COOKIE_SETTLE_MS, self._finish)

    def _finish(self) -> None:
        # 只在已检测到登录凭据时收尾;"我已完成登录"是兜底触发器
        if not self._detected:
            return
        self._poll.stop()
        cookies = dict(self._cookies)
        self.login_cookie_ready.emit(cookies)
        self.accept()
