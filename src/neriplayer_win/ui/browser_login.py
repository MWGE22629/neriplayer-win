"""网页登录对话框:内嵌 Chromium 打开网易云音乐网页,用户手动完成登录。

比扫码二维码直连方案更稳:登录发生在真实网页上下文里,指纹/风控由
页面自身的 SDK 完成,支持网页提供的任意登录方式(扫码/手机号等)。
本对话框只负责:加载页面 → 盯 Cookie → 出现 MUSIC_U 即视为登录成功,
收集全部域 Cookie 交给上层。"""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Signal
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

_LOGIN_URL = "https://music.163.com/#/login"
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
)
_COOKIE_SETTLE_MS = 800  # 检测到 MUSIC_U 后稍等,收齐其他登录 Cookie
_POLL_INTERVAL_MS = 400


class BrowserLoginDialog(QDialog):
    """模态网页登录窗口;成功时 login_cookie_ready 携带 Cookie 字典。"""

    login_cookie_ready = Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("网易云音乐 · 网页登录")
        self.resize(1120, 780)
        self._cookies: dict[str, str] = {}
        self._detected = False

        self._profile = QWebEngineProfile(self)  # off-the-record,会话隔离
        self._profile.setHttpUserAgent(_DESKTOP_UA)
        self._profile.setUrlRequestInterceptor(NeteaseCdnFallbackInterceptor(self))
        cookie_store = self._profile.cookieStore()
        cookie_store.cookieAdded.connect(self._on_cookie_added)

        self._view = QWebEngineView(self)
        self._view.setPage(QWebEnginePage(self._profile, self._view))

        hint = QLabel(
            "在下方页面中使用任意方式登录(扫码 / 手机号)。检测到登录成功后会自动完成;"
            "若页面加载缓慢请稍候。"
        )
        hint.setWordWrap(True)

        done_button = QPushButton("我已完成登录")
        done_button.clicked.connect(self._finish)
        cancel_button = QPushButton("取消")
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

        self._view.load(QUrl(_LOGIN_URL))

    # -- 内部 ---------------------------------------------------------------

    def _on_cookie_added(self, cookie) -> None:
        host = cookie.domain().lstrip(".")
        if not (host == "163.com" or host.endswith(".163.com")):
            return
        name = bytes(cookie.name()).decode("utf-8", errors="replace")
        value = bytes(cookie.value()).decode("utf-8", errors="replace")
        if name:
            self._cookies[name] = value

    def _check_login(self) -> None:
        if self._detected:
            return
        if self._cookies.get("MUSIC_U"):
            self._detected = True
            # MUSIC_U 到手后其他登录 Cookie 可能还在路上,稍等收齐
            QTimer.singleShot(_COOKIE_SETTLE_MS, self._finish)

    def _finish(self) -> None:
        # 只在已检测到登录凭据时收尾;"我已完成登录"是兜底触发器
        if not self._detected:
            return
        self._poll.stop()
        cookies = dict(self._cookies)
        self.login_cookie_ready.emit(cookies)
        self.accept()
