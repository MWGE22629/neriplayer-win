"""主窗口布局回归:中央区与播放条必须真正挂进渲染树。

背景:QSplitter 改造时曾丢失 layout.addLayout(root),导致 central_stack
与 player_bar 无父容器、界面空白——数据层测试全部通过但渲染全空。
此测试在渲染层面断言,防同类回归。
"""

from __future__ import annotations

from neriplayer_win.ui.main_window import MainWindow


def test_body_layout_renders_central_and_player_bar(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    try:
        assert window.central_stack.parentWidget() is not None, (
            "central_stack 无父容器(布局挂载丢失)"
        )
        assert window.player_bar.parentWidget() is not None, (
            "player_bar 无父容器(布局挂载丢失)"
        )
        assert window.central_stack.isVisibleTo(window), "central_stack 不在渲染树"
        assert window.player_bar.isVisibleTo(window), "player_bar 不在渲染树"
        # 侧栏与右列并存于 splitter
        assert window._splitter.count() == 2
    finally:
        window.close()
