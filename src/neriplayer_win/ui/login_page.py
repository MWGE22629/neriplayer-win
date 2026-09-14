"""网易云扫码登录页:二维码 + 状态文字 + 轮询。

轮询节奏对照 reference 的 NeteaseQrLoginActivity:
- POLL_INTERVAL_MS = 1000ms
- 801 等待扫码 / 802 已扫待确认 / 803 成功 / 800 过期(停止轮询,可手动刷新)
"""

from __future__ import annotations

import io

import segno
from PySide6.QtCore import QByteArray, QTimer, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from ..api.netease import (
    NeteaseClient,
    QrLoginSession,
)
from .workers import run_async

_POLL_INTERVAL_MS = 1000  # 照抄 NeteaseQrLoginActivity.POLL_INTERVAL_MS


class LoginPage(QWidget):
    """未登录时主区域展示的扫码登录页。"""

    login_succeeded = Signal(dict)  # 登录成功,携带 cookie 字典

    def __init__(self, client: NeteaseClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._session: QrLoginSession | None = None

        self.qr_label = QLabel("正在获取二维码…")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setFixedSize(260, 260)

        self.status_label = QLabel("请使用网易云音乐 App 扫码登录")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.refresh_button = QPushButton("刷新二维码")
        self.refresh_button.setEnabled(False)
        self.refresh_button.clicked.connect(self.start)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self.qr_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.status_label)
        layout.addWidget(self.refresh_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_once)
        self._polling = False

    # -- 流程 ----------------------------------------------------------------

    def start(self) -> None:
        """重新申请 unikey 并展示新二维码。"""
        self._stop_polling()
        self._session = None
        self.qr_label.setPixmap(QPixmap())
        self.status_label.setText("正在获取二维码…")
        self.refresh_button.setEnabled(False)
        run_async(
            self._client.create_qr_session,
            on_done=self._on_session_ready,
            on_error=self._on_session_error,
        )

    def stop(self) -> None:
        self._stop_polling()

    def _on_session_ready(self, session: QrLoginSession) -> None:
        self._session = session
        self._show_qr(session.qr_content)
        self.status_label.setText("请使用网易云音乐 App 扫码登录")
        self.refresh_button.setEnabled(True)
        self._start_polling()

    def _on_session_error(self, message: str) -> None:
        self.status_label.setText(f"获取二维码失败:{message}")
        self.refresh_button.setEnabled(True)

    def _show_qr(self, content: str) -> None:
        buffer = io.BytesIO()
        segno.make(content, error="m").save(buffer, kind="png", scale=8, border=2)
        pixmap = QPixmap()
        pixmap.loadFromData(QByteArray(buffer.getvalue()))
        self.qr_label.setPixmap(
            pixmap.scaled(
                self.qr_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # -- 轮询 ----------------------------------------------------------------

    def _start_polling(self) -> None:
        self._polling = True
        self._poll_timer.start()

    def _stop_polling(self) -> None:
        self._polling = False
        self._poll_timer.stop()

    def _poll_once(self) -> None:
        session = self._session
        if session is None or not self._polling:
            return
        # 防止网络慢时重入:轮询期间先停表,回调后再恢复
        self._poll_timer.stop()

        def check() -> object:
            return self._client.check_qr_login(session)

        run_async(
            check,
            on_done=self._on_check_result,
            on_error=self._on_check_error,
        )

    def _on_check_result(self, result) -> None:
        if not self._polling and not result.is_confirmed:
            return
        if result.code == 801:
            self.status_label.setText("等待扫码…")
        elif result.code == 802:
            self.status_label.setText("已扫描,请在手机上确认登录")
        elif result.code == 803:
            self._stop_polling()
            if result.cookies.get("MUSIC_U"):
                self.status_label.setText("登录成功")
                self.login_succeeded.emit(dict(result.cookies))
            else:
                # 对应 finishWithCookies 中 cookie 不完整的分支
                self.status_label.setText("登录已确认但 Cookie 不完整,请刷新重试")
                self.refresh_button.setEnabled(True)
            return
        elif result.code == 800:
            self._stop_polling()
            self.status_label.setText("二维码已过期,请点击刷新")
            self.refresh_button.setEnabled(True)
            return
        else:
            message = result.message or f"code={result.code}"
            self._stop_polling()
            self.status_label.setText(f"登录异常:{message},请点击刷新")
            self.refresh_button.setEnabled(True)
            return
        if self._polling:
            self._poll_timer.start()

    def _on_check_error(self, message: str) -> None:
        if not self._polling:
            return
        self._stop_polling()
        self.status_label.setText(f"网络异常:{message}")
        self.refresh_button.setEnabled(True)
