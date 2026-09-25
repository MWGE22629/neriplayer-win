from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    # Qt6Core 动态依赖 icuuc.dll;若 PATH 中存在 miniconda 等自带的 ICU
    # (如 conda 的 ICU 73),会被抢先加载并导致 WinError 127。
    # 先显式加载系统自带的 ICU,钉住进程内版本。
    import ctypes

    try:
        ctypes.CDLL(os.path.join(os.environ["SystemRoot"], "System32", "icuuc.dll"))
    except OSError:
        pass

from PySide6.QtCore import QLoggingCategory
from PySide6.QtWidgets import QApplication

from .ui.icons import app_icon
from .ui.main_window import MainWindow

# 静音 qt.qpa.fonts 的 Fixedsys 告警:QtWebEngine 请求等宽字体回退时会
# 点名 Windows 古老位图字体 Fixedsys,DirectWrite 无法为它生成字形,
# Qt 会自动换用其他等宽字体——功能无影响,仅刷屏。此规则的代价是
# 同时屏蔽该类别下其他字体告警(实际几乎只有这一种噪音)。
QLoggingCategory.setFilterRules("qt.qpa.fonts.warning = false")


def _boost_process_priority() -> None:
    """Windows 下把本进程提到 ABOVE_NORMAL 优先级(增强项,失败静默)。

    动机:游戏全屏时 Windows 会压制后台普通优先级进程的 CPU 配额,
    后台放歌的切歌解析与 UI 响应都会变得迟钝;ABOVE_NORMAL 足以改善,
    又刻意不用 HIGH_PRIORITY——反抢前台游戏的调度有违「后台播放」
    的初衷。只应在 main() 里调用一次;不放在 MainWindow.__init__,
    测试构造 MainWindow 不应改动进程优先级。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        above_normal_priority_class = 0x00008000
        process = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(process, above_normal_priority_class)
    except Exception:  # noqa: BLE001 - 优先级属增强能力,绝不因此崩启动
        pass


def main() -> int:
    from .log import setup_logging

    setup_logging()  # 尽早:后续启动阶段的问题也要能落盘
    app = QApplication(sys.argv)
    app.setApplicationName("NeriPlayer Win")
    app.setOrganizationName("NeriPlayer Win")
    app.setWindowIcon(app_icon())  # 全部窗口/托盘默认图标(M4)
    _boost_process_priority()  # M5:后台播放场景改善切歌与 UI 响应
    window = MainWindow()
    window.show()
    return app.exec()
