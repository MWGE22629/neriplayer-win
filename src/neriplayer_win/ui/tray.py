"""系统托盘(M3):托盘菜单 + 双击恢复主窗口。

图标由 MainWindow 注入(M4 起为 assets/tray_*.png 深浅两版,随主题
切换;build_placeholder_icon 保留为资产缺失时的运行时回退)。
菜单含 播放/暂停、上一首、下一首、显示主窗口、退出(始终真退出)。
菜单文案走 i18n,语言切换时 retranslate 重设(动作对象保持不变)。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..i18n import tr


def build_placeholder_icon() -> QIcon:
    """运行时画的 32x32 占位图标:圆角底 + 白色播放三角(资产缺失时的回退)。"""
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
        self._action_toggle = QAction(tr("tray.toggle"), self._menu)
        self._action_toggle.triggered.connect(self.toggle_play_requested.emit)
        self._menu.addAction(self._action_toggle)
        self._action_prev = QAction(tr("tray.prev"), self._menu)
        self._action_prev.triggered.connect(self.prev_requested.emit)
        self._menu.addAction(self._action_prev)
        self._action_next = QAction(tr("tray.next"), self._menu)
        self._action_next.triggered.connect(self.next_requested.emit)
        self._menu.addAction(self._action_next)
        self._menu.addSeparator()
        self._action_show = QAction(tr("tray.show_main"), self._menu)
        self._action_show.triggered.connect(self.show_main_requested.emit)
        self._menu.addAction(self._action_show)
        self._action_exit = QAction(tr("tray.exit"), self._menu)
        self._action_exit.triggered.connect(self.exit_requested.emit)
        self._menu.addAction(self._action_exit)

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setContextMenu(self._menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

    def retranslate(self) -> None:
        """语言切换:菜单动作文字重设(动作与信号接线不动)。"""
        self._action_toggle.setText(tr("tray.toggle"))
        self._action_prev.setText(tr("tray.prev"))
        self._action_next.setText(tr("tray.next"))
        self._action_show.setText(tr("tray.show_main"))
        self._action_exit.setText(tr("tray.exit"))

    def set_icon(self, icon: QIcon) -> None:
        """更换托盘图标(主题切换深/浅版)。"""
        self.tray.setIcon(icon)

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
