"""系统托盘(M3):占位图标 + 右键菜单 + 双击恢复主窗口。

图标是运行时画的简单 QPixmap(M4 再换正式应用图标,不引入资产转换);
菜单含 播放/暂停、上一首、下一首、显示主窗口、退出(始终真退出)。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


def build_placeholder_icon() -> QIcon:
    """画一个 32x32 占位图标:圆角底 + 白色播放三角。"""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2f6fdb"))
        painter.drawRoundedRect(2, 2, 28, 28, 8, 8)
        painter.setBrush(QColor("#ffffff"))
        painter.drawPolygon(
            QPolygonF([QPointF(12, 9), QPointF(12, 23), QPointF(24, 16)])
        )
    finally:
        painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    """托盘图标控制器;动作以信号交给 MainWindow 执行。"""

    toggle_play_requested = Signal()
    prev_requested = Signal()
    next_requested = Signal()
    show_main_requested = Signal()
    exit_requested = Signal()

    def __init__(self, icon: QIcon, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._menu = QMenu()
        action_toggle = self._menu.addAction("播放 / 暂停")
        action_toggle.triggered.connect(self.toggle_play_requested.emit)
        action_prev = self._menu.addAction("上一首")
        action_prev.triggered.connect(self.prev_requested.emit)
        action_next = self._menu.addAction("下一首")
        action_next.triggered.connect(self.next_requested.emit)
        self._menu.addSeparator()
        action_show = self._menu.addAction("显示主窗口")
        action_show.triggered.connect(self.show_main_requested.emit)
        action_exit = self._menu.addAction("退出")
        action_exit.triggered.connect(self.exit_requested.emit)

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setContextMenu(self._menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_main_requested.emit()

    def show_message(
        self, title: str, body: str, msecs: int = 4000
    ) -> None:
        """托盘气泡通知(首次最小化提示用)。"""
        self.tray.showMessage(
            title, body, QSystemTrayIcon.MessageIcon.Information, msecs
        )

    def shutdown(self) -> None:
        self.tray.hide()
        self._menu.clear()
