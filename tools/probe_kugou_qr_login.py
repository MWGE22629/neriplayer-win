"""酷狗扫码登录获取客户端 token(M6 调研用,非产品代码)。

为什么需要它:酷狗的网页登录 cookie(KugooID + t)只能用于网页接口;
歌单等客户端接口用 android 签名,并且校验的是**客户端 token**,用网页
token 调 /v1/user_verify 会返回 35005 token invalid。扫码登录接口
(login-user.kugou.com/v2/get_userinfo_qrcode)在授权成功时直接下发
客户端可用的 token 与 userid,因此扫一次码就能打通歌单接口,无需安装
酷狗客户端。

流程:
1. GET /v2/qrcode 拿 qrcode key 与二维码 PNG
2. 轮询 GET /v2/get_userinfo_qrcode,status 4 即成功,token 在 data.token
3. 写入 tools/kugou_client_auth.local.json

用法(仓库根):
    uv run python tools/probe_kugou_qr_login.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import httpx
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QVBoxLayout,
    QWidget,
)

_OUT_FILE = Path(__file__).with_name("kugou_client_auth.local.json")
_WEB_SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
_H5_URL = "https://h5.kugou.com/apps/loginQRCode/html/index.html?appid=1005&"
_POLL_INTERVAL_MS = 2500


def web_signature(params: dict, data: str = "") -> str:
    """H5 签名:参数名排序拼 k=v,首尾包盐取 md5。"""
    joined = "".join(f"{k}={params[k]}" for k in sorted(params))
    raw = _WEB_SALT + joined + data + _WEB_SALT
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def create_qr() -> dict:
    params = {
        "appid": "1001",
        "type": "1",
        "plat": "4",
        "qrcode_txt": _H5_URL,
        "srcappid": "2919",
        "clienttime": str(int(time.time())),
        "dfid": "-",
        "mid": "-",
        "uuid": "-",
        "clientver": "20000",
    }
    params["signature"] = web_signature(params)
    with httpx.Client(timeout=20, verify=False) as client:
        response = client.get(
            "https://login-user.kugou.com/v2/qrcode", params=params,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    response.raise_for_status()
    return response.json()["data"]


def check_qr(key: str) -> dict:
    params = {
        "plat": "4",
        "appid": "1005",
        "srcappid": "2919",
        "qrcode": key,
        "clienttime": str(int(time.time())),
        "dfid": "-",
        "mid": "-",
        "uuid": "-",
        "clientver": "20000",
    }
    params["signature"] = web_signature(params)
    with httpx.Client(timeout=20, verify=False) as client:
        response = client.get(
            "https://login-user.kugou.com/v2/get_userinfo_qrcode", params=params,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    response.raise_for_status()
    return response.json()


class QrWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Kugou QR login (M6 research)")
        self.resize(420, 560)

        self._status = QLabel("正在索取二维码…")
        self._status.setWordWrap(True)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image = QLabel()
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(self._image, stretch=1)

        self._key = ""
        self._create()

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _create(self) -> None:
        try:
            data = create_qr()
        except Exception as exc:  # noqa: BLE001 - 探测工具,直接显示原因
            self._status.setText(f"索取二维码失败:{exc}")
            return
        self._key = data.get("qrcode", "")
        raw = data.get("qrcode_img", "")
        png_hex = ""
        if "," in raw:
            png_hex = raw.split(",", 1)[1]
        if png_hex:
            import base64

            pixmap = QPixmap()
            pixmap.loadFromData(base64.b64decode(png_hex))
            self._image.setPixmap(
                pixmap.scaledToWidth(360, Qt.TransformationMode.SmoothTransformation)
            )
        self._status.setText("请用酷狗 App 扫码(手机酷狗即可,无需安装 PC 客户端)")

    def _poll(self) -> None:
        if not self._key:
            return
        try:
            payload = check_qr(self._key)
        except Exception as exc:  # noqa: BLE001
            self._status.setText(f"轮询失败:{exc}")
            return
        data = payload.get("data") or {}
        status = data.get("status")
        if status == 4:
            self._timer.stop()
            token = str(data.get("token", ""))
            userid = str(data.get("userid", ""))
            _OUT_FILE.write_text(
                json.dumps(
                    {"userid": userid, "token": token,
                     "savedAt": int(time.time() * 1000)},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            print(f"登录成功:userid={userid} token 长度={len(token)}")
            print("已写入", _OUT_FILE)
            self._status.setText("登录成功,已保存客户端凭据,可关闭窗口")
            QTimer.singleShot(1200, QApplication.quit)
        elif status == 2:
            self._status.setText("已扫码,请在手机上确认登录")
        elif status == 0:
            self._timer.stop()
            self._status.setText("二维码已过期,请重新运行本工具")


def main() -> int:
    app = QApplication(sys.argv)
    window = QrWindow()
    window.show()
    window.raise_()
    window.activateWindow()
    # 给足扫码与确认时间(手机端还要点一次确认),超时自动退出避免进程悬挂
    QTimer.singleShot(300000, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
