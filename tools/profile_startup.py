"""启动阶段剖析:按 MainWindow 实际构造顺序逐项计时,找启动劣化的根因。

用法:uv run python tools/profile_startup.py
窗口短暂显示后自动退出(os._exit 直退,不走 mpv/托盘收尾防挂),
打印各阶段耗时。历史上用它定位了 platform.platform() 的 _wmi 慢查询
(单次 300-500ms)与 WebEngine 模块级 import(195MB DLL),详见
docs/PERF.md v0.8.1 条目。配合 `python -X importtime -c "import ..."`
看 import 树一起用。
"""

from __future__ import annotations

import sys
import time

marks: list[tuple[str, float]] = []
t0 = time.perf_counter()


def mark(name: str) -> None:
    marks.append((name, time.perf_counter()))


mark("脚本开始")

from neriplayer_win.log import setup_logging

setup_logging()
mark("setup_logging")

from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)
app.setApplicationName("NeriPlayer Win")
mark("QApplication()")

from neriplayer_win.ui.icons import app_icon

app.setWindowIcon(app_icon())
mark("app_icon()")

# -- MainWindow 内部逐项(按 __init__ 顺序) -------------------------------
from neriplayer_win.data.store import LocalStore

store = LocalStore()
settings = store.load_settings()
mark("LocalStore+load_settings")

from neriplayer_win import i18n

i18n.set_language(settings.get("language", "zh"), notify=False)
mark("i18n.set_language")

from neriplayer_win.api.netease import NeteaseClient
from neriplayer_win.api.bili import BiliClient

NeteaseClient()
mark("NeteaseClient()")

BiliClient()
mark("BiliClient()")

from neriplayer_win.ui.covers import CoverLoader

CoverLoader()
mark("CoverLoader() x1")

from neriplayer_win.player.engine import PlayerEngine

engine = PlayerEngine()
mark("PlayerEngine()(mpv init)")

from neriplayer_win.ui.login_page import LoginPage

login_page = LoginPage(NeteaseClient())
mark("LoginPage()")

from neriplayer_win.ui.settings_page import SettingsPage

SettingsPage()
mark("SettingsPage()")

from neriplayer_win.ui.theme import ThemeManager

themes = ThemeManager()
themes.apply(settings.get("appearance", "dark"))
mark("ThemeManager+apply(含全量QSS)")

from neriplayer_win.ui.main_window import MainWindow

window = MainWindow()
mark("MainWindow() 全量(含以上重复项)")

window.show()
mark("window.show()")

from PySide6.QtCore import QTimer


def report() -> None:
    mark("展示后稳定(800ms)")
    print("\n== 启动阶段耗时 ==")
    previous = 0.0
    for name, ts in marks:
        delta = (ts - t0) * 1000
        step = delta - previous
        previous = delta
        print(f"  +{step:7.1f} ms  (累计 {delta:7.1f} ms)  {name}", flush=True)
    import os

    os._exit(0)  # 不走 mpv/托盘收尾,防事件线程挂住剖析脚本


QTimer.singleShot(800, report)
app.exec()
