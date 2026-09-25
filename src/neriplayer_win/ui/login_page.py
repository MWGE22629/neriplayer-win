"""网易云登录页:引导打开内嵌浏览器完成网页登录。

历史:第一版做扫码二维码直连(unikey + 轮询),但桌面端无 WebView 指纹
(YD token),手机确认后服务端以"请切换其他登录方式或升级新版本"拒绝;
改为内嵌 Chromium 网页登录——登录发生在真实页面上下文,任意方式可选,
成功后收割 Cookie(见 browser_login.py)。二维码客户端保留在
api/netease/client.py 备用。静态文案走 i18n(语言切换时 retranslate)。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from ..api.netease import NeteaseClient
from ..i18n import tr
from .browser_login import BrowserLoginDialog, netease_web_login


class LoginPage(QWidget):
    """未登录时主区域展示的登录引导页。"""

    login_succeeded = Signal(dict)  # 登录成功,携带 cookie 字典

    def __init__(self, client: NeteaseClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._dialog: BrowserLoginDialog | None = None

        self.title = QLabel()
        self.title.setObjectName("pageTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.title.font()
        font.setPointSize(16)
        self.title.setFont(font)

        self.hint_label = QLabel()
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.login_button = QPushButton()
        self.login_button.setObjectName("primaryButton")
        self.login_button.clicked.connect(self._open_browser_login)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self.title)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.login_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.retranslate()

    # -- 文案(语言切换时由 MainWindow 统一调用)-------------------------------

    def retranslate(self) -> None:
        """按当前语言重设静态文案;状态行为动态瞬时信息,不在此处理。"""
        self.title.setText(tr("login.title"))
        self.hint_label.setText(tr("login.hint"))
        self.login_button.setText(tr("login.button"))

    # -- 对外接口(保持 MainWindow 既有调用契约) ---------------------------

    def start(self) -> None:
        """进入登录态前的展示复位(不自动弹窗,等用户点击)。"""
        self.status_label.setText("")

    def stop(self) -> None:
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None

    # -- 内部 ---------------------------------------------------------------

    def _open_browser_login(self) -> None:
        self.status_label.setText(tr("login.waiting"))
        dialog = BrowserLoginDialog(netease_web_login(), self)
        self._dialog = dialog
        dialog.login_cookie_ready.connect(self._on_cookies)
        dialog.exec()
        self._dialog = None

    def _on_cookies(self, cookies: dict) -> None:
        if cookies.get("MUSIC_U"):
            self.status_label.setText(tr("login.success"))
            self.login_succeeded.emit(dict(cookies))
        else:
            self.status_label.setText(tr("login.no_credentials"))
