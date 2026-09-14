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


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NeriPlayer Win")
    app.setOrganizationName("NeriPlayer Win")
    app.setWindowIcon(app_icon())  # 全部窗口/托盘默认图标(M4)
    window = MainWindow()
    window.show()
    return app.exec()
