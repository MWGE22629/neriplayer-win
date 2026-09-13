from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from .player_bar import PlayerBar

_SIDEBAR_SECTIONS = [
    "搜索",
    "网易云 · 我喜欢的音乐",
    "网易云 · 我的歌单",
    "B站 · 收藏夹",
    "设置",
]


class MainWindow(QMainWindow):
    """主窗口骨架:左侧导航 + 中部歌曲列表 + 底部播放条。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NeriPlayer Win")
        self.resize(1080, 680)

        sidebar = QListWidget()
        for title in _SIDEBAR_SECTIONS:
            QListWidgetItem(title, sidebar)
        sidebar.setFixedWidth(220)

        # 骨架阶段只有占位页;M1 起歌单/搜索内容落在歌曲列表页
        placeholder = QLabel("登录后这里展示歌单与歌曲(M1)")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.song_table = QTableWidget(0, 4)
        self.song_table.setHorizontalHeaderLabels(["#", "标题", "歌手", "时长"])
        self.song_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.song_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.song_table.verticalHeader().setVisible(False)
        self.song_table.setEnabled(False)

        self.central_stack = QStackedWidget()
        self.central_stack.addWidget(placeholder)
        self.central_stack.addWidget(self.song_table)

        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(sidebar)
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.central_stack, stretch=1)
        root.addWidget(PlayerBar())
        layout.addLayout(root, stretch=1)
        self.setCentralWidget(body)
