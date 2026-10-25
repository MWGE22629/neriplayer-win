"""酷狗网页登录探测工具(M6 调研用,非产品代码)。

用途:在隔离的 WebEngine 配置里打开酷狗网页,由用户手动完成登录,
点「导出 Cookie」后把 *.kugou.com 域下的 cookie 写到
tools/kugou_cookies.local.json(被仓库 .gitignore 的 *.local.json 覆盖,
不进版本库),供后续 API 实测脚本复用。

与 ui/browser_login.py 的区别:本工具不判定登录成功、不做 cookie 过滤
白名单,导出全部酷狗域 cookie 并附带 domain/path/expiry,便于核对登录态
到底落在哪个键上。profile 走持久化目录,重复运行时登录态可复用。

用法(仓库根):
    uv run python tools/probe_kugou_login.py
    uv run python tools/probe_kugou_login.py --url https://www.kugou.com/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_COOKIE_FILE = Path(__file__).with_name("kugou_cookies.local.json")
_PROFILE_DIR = Path(os.environ.get("TEMP", ".")) / "neriplayer-kugou-probe-profile"
_LOGIN_HINT = (
    "在下方网页完成酷狗登录(扫码/手机号均可)。登录后点「导出 Cookie」;\n"
    "工具只保存 *.kugou.com 的 cookie 到 tools/kugou_cookies.local.json。"
)


class ProbeWindow(QWidget):
    def __init__(self, url: str) -> None:
        super().__init__()
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self.setWindowTitle("Kugou login probe (M6 research)")
        self.resize(1180, 820)

        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._profile = QWebEngineProfile("kugou-probe", self)
        self._profile.setPersistentStoragePath(str(_PROFILE_DIR))
        self._profile.setCachePath(str(_PROFILE_DIR / "cache"))
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self._profile.setHttpUserAgent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        self._store = self._profile.cookieStore()
        self._cookies: dict[str, dict[str, str]] = {}
        self._store.cookieAdded.connect(self._on_cookie_added)
        self._store.loadAllCookies()

        self._status = QLabel("等待酷狗域 cookie…")
        self._status.setWordWrap(True)

        export_btn = QPushButton("导出 Cookie")
        export_btn.clicked.connect(self._export)
        quit_btn = QPushButton("关闭")
        quit_btn.clicked.connect(self.close)

        buttons = QHBoxLayout()
        buttons.addWidget(self._status, stretch=1)
        buttons.addWidget(export_btn)
        buttons.addWidget(quit_btn)

        self._view = QWebEngineView(self)
        self._view.setPage(QWebEnginePage(self._profile, self._view))

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(_LOGIN_HINT))
        layout.addWidget(self._view, stretch=1)
        layout.addLayout(buttons)

        self._view.load(QUrl(url))

        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._refresh_status)
        self._poll.start()

    # -- cookie 收集 --------------------------------------------------------

    @staticmethod
    def _is_kugou(domain: str) -> bool:
        host = domain.lstrip(".")
        return host.endswith("kugou.com")

    def _on_cookie_added(self, cookie) -> None:
        domain = cookie.domain()
        if not self._is_kugou(domain):
            return
        name = bytes(cookie.name()).decode("utf-8", errors="replace")
        if not name:
            return
        self._cookies[name] = {
            "value": bytes(cookie.value()).decode("utf-8", errors="replace"),
            "domain": domain,
            "path": cookie.path(),
            "expiry": cookie.expirationDate().toString("yyyy-MM-ddTHH:mm:ss")
            if not cookie.isSessionCookie()
            else "",
        }

    def _refresh_status(self) -> None:
        names = sorted(self._cookies)
        login_keys = [n for n in names if n.lower() in _LOGIN_KEY_NAMES]
        self._status.setText(
            f"已收 {len(names)} 个酷狗 cookie;疑似登录键:"
            f"{', '.join(login_keys) if login_keys else '尚未出现'}"
        )

    def _export(self) -> None:
        # loadAllCookies 的结果经 cookieAdded 信号回流到 _cookies,
        # 留出回流时间再落盘,避免只写到当前会话新增的部分
        self._store.loadAllCookies()
        QTimer.singleShot(600, self._write_cookie_file)

    def _write_cookie_file(self) -> None:
        payload = {
            "exportedAt": datetime.now().isoformat(timespec="seconds"),
            "cookies": {name: item["value"] for name, item in self._cookies.items()},
            "meta": self._cookies,
        }
        _COOKIE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        names = sorted(self._cookies)
        print(f"已写入 {_COOKIE_FILE} ({len(names)} 个 cookie)")
        print("cookie 名:", ", ".join(names))
        self._status.setText(f"已导出 {len(names)} 个 cookie 到本地文件")


_LOGIN_KEY_NAMES = {
    "kugoo",
    "kg_mid",
    "kg_dfid",
    "kg_mid_temp",
    "userid",
    "token",
    "vip_type",
    "vip_token",
    "nickname",
    "kugouid",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://www.kugou.com/")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    win = ProbeWindow(args.url)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
