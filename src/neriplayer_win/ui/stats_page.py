"""播放统计页:累计收听时长与播放次数的表格视图。

表格沿用歌曲表的形态(序号 / 封面 / 标题),把「作者」「歌曲时长」两列
换成「播放时长」「播放次数」。默认按播放次数降序。

口径见 docs/research/M9-overview.md:播放时长是累计实际收听时长,
不含暂停;不显示「最近播放」列。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..data.play_stats import PlayStat, format_play_duration
from ..i18n import tr
from .covers import CoverLoader
from .player_bar import rounded_pixmap

# 列定义(与歌曲表同源:序号 50 / 封面 50 / 标题伸缩)
_COL_NUMBER = 0
_COL_COVER = 1
_COL_TITLE = 2
_COL_DURATION = 3
_COL_COUNT = 4
_COLUMN_COUNT = 5

_NUMBER_WIDTH = 50
_COVER_WIDTH = 50
_COVER_SIZE = 20
_DURATION_WIDTH = 130
_COUNT_WIDTH = 90
_ROW_HEIGHT = 32

# 排序键
_SORT_COUNT = "count"
_SORT_DURATION = "duration"
_SORT_TITLE = "title"


@dataclass
class _SortState:
    key: str = _SORT_COUNT
    descending: bool = True


class _CoverDelegate(QStyledItemDelegate):
    """封面列:居中画 20px 圆角缩略图(与歌曲表同款)。"""

    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt 命名
        view_option = QStyleOptionViewItem(option)
        self.initStyleOption(view_option, index)
        size = view_option.decorationSize
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        view_option.icon = QIcon()
        view_option.text = ""
        widget = view_option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, view_option, painter, widget
        )
        if not isinstance(icon, QIcon) or icon.isNull():
            return
        device = painter.device()
        dpr = device.devicePixelRatioF() if device is not None else 1.0
        pixmap = icon.pixmap(size, dpr)
        painter.drawPixmap(
            QRect(
                option.rect.x() + (option.rect.width() - size.width()) // 2,
                option.rect.y() + (option.rect.height() - size.height()) // 2,
                size.width(),
                size.height(),
            ),
            pixmap,
        )


class StatsPage(QWidget):
    """播放统计页:表格 + 空态 + 返回按钮。"""

    back_requested = Signal()
    clear_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stats: list[PlayStat] = []
        self._sort = _SortState()

        self.title_label = QLabel()
        self.title_label.setObjectName("statsTitle")
        self.back_button = QPushButton()
        self.clear_button = QPushButton()
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.clear_button)
        header.addWidget(self.back_button)

        self._cover_urls: list[str] = []
        # 懒创建:统计页在多数会话里不会被打开,CoverLoader 构造会建缓存
        # 目录(文件系统操作),不必在每次构造 StatsPage 时都付这个成本
        self._cover_loader: CoverLoader | None = None

        self.table = QTableWidget(0, _COLUMN_COUNT)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        self.table.setColumnWidth(_COL_NUMBER, _NUMBER_WIDTH)
        self.table.setColumnWidth(_COL_COVER, _COVER_WIDTH)
        self.table.setColumnWidth(_COL_DURATION, _DURATION_WIDTH)
        self.table.setColumnWidth(_COL_COUNT, _COUNT_WIDTH)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(_COL_TITLE, QHeaderView.ResizeMode.Stretch)
        header_view.setStretchLastSection(False)
        header_view.setSortIndicatorShown(False)
        header_view.sectionClicked.connect(self._on_header_clicked)
        self.table.setIconSize(QSize(_COVER_SIZE, _COVER_SIZE))
        self.table.setItemDelegateForColumn(_COL_COVER, _CoverDelegate(self.table))

        self.empty_label = QLabel()
        self.empty_label.setObjectName("emptyHint")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.table)  # 0
        self.stack.addWidget(self.empty_label)  # 1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addLayout(header)
        layout.addWidget(self.stack, stretch=1)

        self.back_button.clicked.connect(self.back_requested.emit)
        self.clear_button.clicked.connect(self.clear_requested.emit)

        self.retranslate()

    # -- 对外 ----------------------------------------------------------------

    def set_stats(self, stats: Sequence[PlayStat]) -> None:
        """喂入整份统计(已按需要排序前的原始数据)。"""
        self._stats = list(stats)
        self._refresh()

    def retranslate(self) -> None:
        self.title_label.setText(tr("stats.title"))
        self.back_button.setText(tr("stats.back"))
        self.clear_button.setText(tr("stats.clear"))
        self.empty_label.setText(tr("stats.empty"))
        self._apply_header_labels()
        # 内容重填:表头与时长格式都依赖语言
        self._refresh()

    # -- 排序与填充 -----------------------------------------------------------

    def _apply_header_labels(self) -> None:
        labels = [
            tr("stats.col_number"),
            tr("stats.col_cover"),
            self._title_header_text(),
            self._duration_header_text(),
            self._count_header_text(),
        ]
        self.table.setHorizontalHeaderLabels(labels)
        # 列头对齐与数据对齐一致,setHorizontalHeaderLabels 每次重建 item,
        # 所以对齐必须在这里重设
        alignments = {
            _COL_NUMBER: Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            _COL_COVER: Qt.AlignmentFlag.AlignCenter,
            _COL_TITLE: Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            _COL_DURATION: Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            _COL_COUNT: Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        }
        for col, align in alignments.items():
            item = self.table.horizontalHeaderItem(col)
            if item is not None:
                item.setTextAlignment(align)

    def _title_header_text(self) -> str:
        return self._with_arrow(tr("stats.col_title"), _SORT_TITLE)

    def _duration_header_text(self) -> str:
        return self._with_arrow(tr("stats.col_duration"), _SORT_DURATION)

    def _count_header_text(self) -> str:
        return self._with_arrow(tr("stats.col_count"), _SORT_COUNT)

    def _with_arrow(self, text: str, key: str) -> str:
        if self._sort.key != key:
            return text
        return f"{text} {'▼' if self._sort.descending else '▲'}"

    def _on_header_clicked(self, column: int) -> None:
        mapping = {
            _COL_TITLE: _SORT_TITLE,
            _COL_DURATION: _SORT_DURATION,
            _COL_COUNT: _SORT_COUNT,
        }
        key = mapping.get(column)
        if key is None:
            return
        if self._sort.key == key:
            self._sort.descending = not self._sort.descending
        else:
            self._sort.key = key
            # 数字列默认降序(最常听/最久的在前),标题列默认升序
            self._sort.descending = key != _SORT_TITLE
        self._refresh()

    def _sorted_stats(self) -> list[PlayStat]:
        key = self._sort.key
        if key == _SORT_TITLE:
            return sorted(self._stats, key=lambda s: s.title.lower(),
                          reverse=self._sort.descending)
        if key == _SORT_DURATION:
            primary = lambda s: s.listen_ms  # noqa: E731
        else:
            primary = lambda s: s.play_count  # noqa: E731
        return sorted(
            self._stats,
            key=lambda s: (primary(s), s.last_played_at, s.key),
            reverse=self._sort.descending,
        )

    def _refresh(self) -> None:
        rows = self._sorted_stats()
        self._apply_header_labels()
        self._cover_urls = []
        if not rows:
            self.stack.setCurrentIndex(1)
            self.table.setRowCount(0)
            return
        self.stack.setCurrentIndex(0)
        self.table.setRowCount(len(rows))
        for row, stat in enumerate(rows):
            self._fill_row(row, stat)
        self._request_covers(rows)

    def _fill_row(self, row: int, stat: PlayStat) -> None:
        number = QTableWidgetItem(str(row + 1))
        number.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, _COL_NUMBER, number)

        cover = QTableWidgetItem("")
        cover.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, _COL_COVER, cover)

        title = QTableWidgetItem(stat.title or tr("stats.unknown_title"))
        title.setToolTip(stat.title)
        self.table.setItem(row, _COL_TITLE, title)

        duration = QTableWidgetItem(format_play_duration(stat.listen_ms))
        duration.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, _COL_DURATION, duration)

        count = QTableWidgetItem(str(stat.play_count))
        count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, _COL_COUNT, count)

    def _ensure_cover_loader(self) -> CoverLoader:
        if self._cover_loader is None:
            self._cover_loader = CoverLoader(self)
            self._cover_loader.cover_ready.connect(self._on_cover_ready)
        return self._cover_loader

    def _request_covers(self, rows: Sequence[PlayStat]) -> None:
        """按当前行序请求封面;结果回来按 URL 回填,行序变化不影响对应关系。"""
        self._cover_urls = [stat.cover_url for stat in rows]
        urls = [url for url in self._cover_urls if url]
        if not urls:
            return
        loader = self._ensure_cover_loader()
        for url in urls:
            loader.request(url)

    def _on_cover_ready(self, url: str, data: bytes) -> None:
        """封面到达:按 URL 找到对应行回填(行序可能已因排序变化)。"""
        if url not in self._cover_urls:
            return  # 已换页/换排序,过期结果丢弃
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        icon = QIcon(rounded_pixmap(pixmap, _COVER_SIZE, 4))
        for row, row_url in enumerate(self._cover_urls):
            if row_url == url and row < self.table.rowCount():
                item = self.table.item(row, _COL_COVER)
                if item is not None:
                    item.setIcon(icon)

    # -- 测试辅助 ------------------------------------------------------------

    def visible_rows(self) -> list[tuple[str, str, str]]:
        """(标题, 播放时长, 播放次数) 三元组,供测试断言。"""
        result: list[tuple[str, str, str]] = []
        for row in range(self.table.rowCount()):
            result.append((
                self.table.item(row, _COL_TITLE).text(),
                self.table.item(row, _COL_DURATION).text(),
                self.table.item(row, _COL_COUNT).text(),
            ))
        return result
