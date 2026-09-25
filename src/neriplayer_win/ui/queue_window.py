"""播放队列窗口(M3):非模态独立窗口,展示队列内容与当前曲,双击切跳。

由 MainWindow 持有(首次点击播放条「队列」按钮时创建);自身直接订阅
PlayQueue 信号,内容替换时重建列表,切歌时同步高亮并滚动到当前曲。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from ..i18n import tr
from ..player.queue import PlayQueue


class QueueWindow(QWidget):
    """播放队列窗口;双击某行发出 jump_requested(行号)。"""

    jump_requested = Signal(int)

    def __init__(self, queue: PlayQueue, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(tr("queue.title"))
        self.resize(440, 540)
        self._queue = queue
        self._current_row = -1

        self.header_label = QLabel()
        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.header_label)
        layout.addWidget(self.list_widget, stretch=1)

        queue.queue_changed.connect(self.rebuild)
        queue.current_changed.connect(self.set_current)
        queue.mode_changed.connect(self._update_header)
        self.rebuild()

    # -- 对外 ----------------------------------------------------------------

    def retranslate(self) -> None:
        """语言切换:窗口标题与表头(表头经 _update_header 重算)。"""
        self.setWindowTitle(tr("queue.title"))
        self._update_header()

    def rebuild(self) -> None:
        """按队列内容重建列表并恢复当前曲高亮。"""
        current = self._queue.current_index()
        self._current_row = -1
        self.list_widget.clear()
        for row, song in enumerate(self._queue.items()):
            item = QListWidgetItem(self._row_text(song, row == current))
            item.setData(Qt.ItemDataRole.UserRole, row)
            if row == current:
                font = QFont(item.font())
                font.setBold(True)
                item.setFont(font)
                self._current_row = row
            self.list_widget.addItem(item)
        if current >= 0:
            self.list_widget.setCurrentRow(current)
        self._update_header()

    def set_current(self, row: int) -> None:
        """当前曲变化:更新高亮行并滚动到可见。"""
        previous = self._current_row
        if previous == row:
            return
        if 0 <= previous < self.list_widget.count():
            old_item = self.list_widget.item(previous)
            if old_item is not None:
                old_item.setText(self._row_text_at(previous, current=False))
                font = QFont(old_item.font())
                font.setBold(False)
                old_item.setFont(font)
        self._current_row = row
        if 0 <= row < self.list_widget.count():
            item = self.list_widget.item(row)
            if item is not None:
                item.setText(self._row_text_at(row, current=True))
                font = QFont(item.font())
                font.setBold(True)
                item.setFont(font)
                self.list_widget.setCurrentRow(row)
                self.list_widget.scrollToItem(
                    item, QListWidget.ScrollHint.PositionAtCenter
                )
        elif row < 0:
            self.list_widget.setCurrentRow(-1)

    # -- 内部 ----------------------------------------------------------------

    @staticmethod
    def _row_text(song, current: bool) -> str:
        text = song.title if not song.artist else f"{song.title} - {song.artist}"
        marker = "▶ " if current else ""
        return f"{marker}{text}"

    def _row_text_at(self, row: int, current: bool) -> str:
        count = len(self._queue)
        song = self._queue.item_at(row) if 0 <= row < count else None
        if song is None:
            return ""
        return self._row_text(song, current)

    def _update_header(self, *_args) -> None:
        mode = self._queue.mode()
        self.header_label.setText(
            tr("queue.header", count=len(self._queue), mode=mode.display_name)
        )

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        row = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(row, int):
            self.jump_requested.emit(row)
