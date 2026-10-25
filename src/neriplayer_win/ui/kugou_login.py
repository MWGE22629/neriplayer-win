"""酷狗扫码登录对话框。

为什么需要扫码:歌单/收藏走客户端网关接口,校验的是扫码登录下发的 token
(30 天有效、可无感续期);网页登录的 KuGoo 串只覆盖播放地址。两者互不
替代,因此登录流程是「扫码拿 token + 网页登录拿 KuGoo 串」两步。

本模块只负责扫码这一半:显示二维码、轮询状态、把凭据交给上层落盘。
网页登录复用 ui/browser_login.py 的 BrowserLoginDialog(配置见
browser_login.kugou_web_login)。

与网易云扫码登录的差异:酷狗二维码由接口直接返回 PNG base64(网易云是
本地用 segno 生成),所以这里不做二维码绘制,只解码显示。

注意:QtWebEngine 与接口模块都不在这里 import(本模块只用 QPixmap 与
KugouClient),避免拖慢启动链(见 docs/PERF.md v0.8.1)。
"""

from __future__ import annotations

import base64

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..api.kugou import KugouApiError, KugouClient, KugouCredentials
from ..i18n import tr

# 轮询间隔:酷狗状态接口按 2.5 秒一次足够,太密会被限流
_POLL_INTERVAL_MS = 2500
# 二维码有效期(接口侧约 3 分钟),到点自动停轮询并提示刷新
_QR_TIMEOUT_MS = 180_000
_QR_DISPLAY_SIZE = 320


class KugouQrLoginDialog(QDialog):
    """扫码登录窗口;成功时 login_credentials_ready 携带凭据。

    调用方拿到凭据后负责落盘与后续网页登录(见 MainWindow 的登录流程)。
    """

    login_credentials_ready = Signal(object)

    def __init__(self, client: KugouClient, parent=None) -> None:
        super().__init__(parent)
        self._client = client
        self._session = None
        self._finished = False

        self.setWindowTitle(tr("kugou.qr.title"))
        self.resize(420, 520)

        self._status = QLabel(tr("kugou.qr.fetching"))
        self._status.setWordWrap(True)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._image = QLabel()
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setMinimumSize(_QR_DISPLAY_SIZE, _QR_DISPLAY_SIZE)

        self._refresh_btn = QPushButton(tr("kugou.qr.refresh"))
        self._refresh_btn.clicked.connect(self._load_qr)
        self._cancel_btn = QPushButton(tr("weblogin.cancel"))
        self._cancel_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._refresh_btn)
        buttons.addWidget(self._cancel_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(self._image, stretch=1)
        layout.addLayout(buttons)

        self._poll = QTimer(self)
        self._poll.setInterval(_POLL_INTERVAL_MS)
        self._poll.timeout.connect(self._check)

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)

        self._load_qr()

    # -- 内部 ---------------------------------------------------------------

    def _load_qr(self) -> None:
        """取一张新二维码并开始轮询。"""
        self._finished = False
        self._poll.stop()
        self._timeout.stop()
        self._status.setText(tr("kugou.qr.fetching"))
        self._image.clear()
        self._refresh_btn.setEnabled(False)
        try:
            session = self._client.create_qr_session()
        except KugouApiError as exc:
            self._status.setText(tr("kugou.qr.failed", message=str(exc)))
            self._refresh_btn.setEnabled(True)
            return
        self._session = session
        pixmap = QPixmap()
        if session.image_base64:
            try:
                pixmap.loadFromData(base64.b64decode(session.image_base64))
            except Exception:  # noqa: BLE001 - 图片解码失败按无图处理
                pixmap = QPixmap()
        if not pixmap.isNull():
            self._image.setPixmap(
                pixmap.scaled(
                    _QR_DISPLAY_SIZE, _QR_DISPLAY_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self._status.setText(tr("kugou.qr.hint"))
        self._refresh_btn.setEnabled(True)
        self._poll.start()
        self._timeout.start(_QR_TIMEOUT_MS)

    def _check(self) -> None:
        if self._session is None or self._finished:
            return
        try:
            result = self._client.check_qr_session(self._session)
        except KugouApiError as exc:
            # 网络抖动不中断轮询,只更新提示
            self._status.setText(tr("kugou.qr.poll_error", message=str(exc)))
            return
        if result.succeeded and result.credentials is not None:
            self._finished = True
            self._poll.stop()
            self._timeout.stop()
            self._status.setText(tr("kugou.qr.success"))
            self.login_credentials_ready.emit(result.credentials)
            self.accept()
        elif result.expired:
            self._finished = True
            self._poll.stop()
            self._timeout.stop()
            self._status.setText(tr("kugou.qr.expired"))
        elif result.waiting_confirm:
            self._status.setText(tr("kugou.qr.scanned"))
        else:
            self._status.setText(tr("kugou.qr.hint"))

    def _on_timeout(self) -> None:
        if self._finished:
            return
        self._poll.stop()
        self._status.setText(tr("kugou.qr.expired"))
