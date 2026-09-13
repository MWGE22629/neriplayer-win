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

from PySide6.QtWidgets import QApplication

from .ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NeriPlayer Win")
    app.setOrganizationName("NeriPlayer Win")
    window = MainWindow()
    window.show()
    return app.exec()
