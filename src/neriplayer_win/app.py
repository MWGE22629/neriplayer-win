from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NeriPlayer Win")
    app.setOrganizationName("NeriPlayer Win")
    window = MainWindow()
    window.show()
    return app.exec()
