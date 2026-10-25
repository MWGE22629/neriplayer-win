"""酷狗网页播放器流量捕获工具(M6 调研用,非产品代码)。

目的:不猜接口。用隔离的 WebEngine 打开酷狗歌曲页,由页面自身发起真实
播放请求,拦截器把发往 kugou.com 的 XHR/fetch 全部录下来(URL、方法、
POST 体、请求头白名单),导出到 tools/kugou_capture.local.json。
之后用 probe_kugou_api.py 或手写脚本照抄参数复现,把契约锚定成实测结论。

复用 probe_kugou_login.py 的持久化 profile,登录态无需重登。

用法(仓库根):
    uv run python tools/probe_kugou_capture.py
    uv run python tools/probe_kugou_capture.py --url "https://www.kugou.com/song/#hash=...&album_id=..."
    uv run python tools/probe_kugou_capture.py --seconds 90

操作:窗口打开后点一下页面里的播放按钮(或让它自动播放),等日志里出现
play/getdata 一类请求,再点「导出」。
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

_OUT_FILE = Path(__file__).with_name("kugou_capture.local.json")
_PROFILE_DIR = Path(os.environ.get("TEMP", ".")) / "neriplayer-kugou-probe-profile"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 只留与契约相关的头,避免把 cookie 原值写进文件
_KEEP_HEADERS = ("referer", "origin", "content-type", "x-requested-with", "user-agent")


class _Recorder:
    """拦截器与窗口之间的共享状态。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._seen: set[tuple[str, str]] = set()

    def add(self, method: str, url: str, body: bytes, headers: dict[str, str]) -> None:
        if "kugou.com" not in url:
            return
        key = (method, url)
        if key in self._seen:
            return
        self._seen.add(key)
        self.entries.append(
            {
                "method": method,
                "url": url,
                "body": body.decode("utf-8", errors="replace")[:4000],
                "headers": {k: v for k, v in headers.items() if k in _KEEP_HEADERS},
            }
        )


def _make_interceptor(recorder: _Recorder):
    from PySide6.QtWebEngineCore import QWebEngineUrlRequestInterceptor

    class Interceptor(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info) -> None:  # noqa: N802 - Qt 命名
            method = bytes(info.requestMethod()).decode("utf-8", errors="replace")
            url = info.requestUrl().toString()
            body = b""
            device = info.requestBody()
            if device is not None:
                # requestBody() 返回 QIODevice,不是 bytes
                from PySide6.QtCore import QIODevice

                device.open(QIODevice.OpenModeFlag.ReadOnly)
                try:
                    body = bytes(device.readAll())
                finally:
                    device.close()
            headers = {
                bytes(k).decode("latin-1").lower(): bytes(v).decode("latin-1")
                for k, v in info.httpHeaders().items()
            }
            recorder.add(method, url, body, headers)

    return Interceptor()


class CaptureWindow(QWidget):
    def __init__(self, url: str, recorder: _Recorder) -> None:
        super().__init__()
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self.setWindowTitle("Kugou traffic capture (M6 research)")
        self.resize(1200, 840)
        self._recorder = recorder

        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._profile = QWebEngineProfile("kugou-probe", self)
        self._profile.setPersistentStoragePath(str(_PROFILE_DIR))
        self._profile.setCachePath(str(_PROFILE_DIR / "cache"))
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self._profile.setHttpUserAgent(_UA)
        self._interceptor = _make_interceptor(recorder)
        self._profile.setUrlRequestInterceptor(self._interceptor)

        self._status = QLabel("录制中…")
        self._status.setWordWrap(True)
        export_btn = QPushButton("导出")
        export_btn.clicked.connect(self._export)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)

        buttons = QHBoxLayout()
        buttons.addWidget(self._status, stretch=1)
        buttons.addWidget(export_btn)
        buttons.addWidget(close_btn)

        self._view = QWebEngineView(self)
        self._view.setPage(QWebEnginePage(self._profile, self._view))

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "页面加载后点播放,让歌曲真正出声;状态栏出现 play/getdata "
                "等请求后再点「导出」。"
            )
        )
        layout.addWidget(self._view, stretch=1)
        layout.addLayout(buttons)

        self._view.load(QUrl(url))

        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()

    def _refresh(self) -> None:
        entries = self._recorder.entries
        play = [e for e in entries if "play" in e["url"] and "getdata" in e["url"]]
        self._status.setText(
            f"已录 {len(entries)} 条 kugou 请求;其中 play/getdata "
            f"{len(play)} 条"
        )

    def _export(self) -> None:
        payload = {
            "exportedAt": datetime.now().isoformat(timespec="seconds"),
            "entries": self._recorder.entries,
        }
        _OUT_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写入 {_OUT_FILE},共 {len(self._recorder.entries)} 条")
        for entry in self._recorder.entries:
            print(entry["method"], entry["url"][:200])
        self._status.setText(f"已导出 {len(self._recorder.entries)} 条到本地文件")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="https://www.kugou.com/yy/html/search.html#searchType=song&searchKeyWord=%E6%99%B4%E5%A4%A9",
    )
    parser.add_argument("--seconds", type=int, default=0, help="到时自动导出并退出")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    recorder = _Recorder()
    win = CaptureWindow(args.url, recorder)
    win.show()
    if args.seconds > 0:
        QTimer.singleShot(args.seconds * 1000, win._export)
        QTimer.singleShot(args.seconds * 1000 + 800, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
