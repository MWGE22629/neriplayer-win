"""进度条 SeekSlider 的点击跳转与拖动 seek 回归测试。

背景:PlayerBar 曾用 monkeypatch mousePressEvent 实现"点击任意位置跳转",
事件被整体吞掉导致 QSlider 永不进入拖拽态(只能点、不能拖,sliderPressed
也从不发出)。改为 SeekSlider 自管 press/move/release 后,用这里的事件
模拟锁住行为:点击即跳、按住可拖、拖动中播放进度回写不得拉回手柄。
"""

from __future__ import annotations

import pytest

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from neriplayer_win.ui.player_bar import PlayerBar

_LEFT = Qt.MouseButton.LeftButton
_NONE = Qt.MouseButton.NoButton


def _mouse(target, event_type: QEvent.Type, x: float, buttons) -> None:
    from PySide6.QtWidgets import QApplication

    event = QMouseEvent(
        event_type, QPointF(x, 10), QPointF(x, 10),
        _LEFT, buttons, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(target, event)


@pytest.fixture()
def bar(qapp):
    bar = PlayerBar()
    bar.position_slider.setRange(0, 100)
    bar.position_slider.resize(200, 20)
    yield bar


def test_click_seeks_to_position(qapp, bar):
    """纯点击(不拖)即跳到点击比例处,并发出一次 seek。"""
    seeks: list[float] = []
    bar.seek_requested.connect(seeks.append)

    _mouse(bar.position_slider, QEvent.Type.MouseButtonPress, 10, _LEFT)
    _mouse(bar.position_slider, QEvent.Type.MouseButtonRelease, 10, _NONE)

    assert len(seeks) == 1
    assert 0 <= seeks[0] <= 12  # 10/200 ≈ 5%,容忍手柄取整


def test_drag_updates_value_and_seeks_on_release(qapp, bar):
    """按下后拖动:值实时跟随,释放时发出最终位置的 seek。"""
    events = {"press": 0, "release": 0}
    seeks: list[float] = []
    bar.position_slider.sliderPressed.connect(
        lambda: events.__setitem__("press", events["press"] + 1))
    bar.position_slider.sliderReleased.connect(
        lambda: events.__setitem__("release", events["release"] + 1))
    bar.seek_requested.connect(seeks.append)

    _mouse(bar.position_slider, QEvent.Type.MouseButtonPress, 20, _LEFT)
    _mouse(bar.position_slider, QEvent.Type.MouseMove, 150, _LEFT)
    assert bar.position_slider.value() >= 60, "拖动未生效(只点不拖的老 bug)"
    _mouse(bar.position_slider, QEvent.Type.MouseButtonRelease, 150, _NONE)

    assert events["press"] == 1
    assert events["release"] == 1
    assert 60 <= seeks[-1] <= 100


def test_progress_write_ignored_while_dragging(qapp, bar):
    """拖动期间引擎的进度回写不得把手柄拉回去(旧实现 _dragging 永远
    为 False,sliderPressed 从不发出,该保护形同虚设)。"""
    _mouse(bar.position_slider, QEvent.Type.MouseButtonPress, 150, _LEFT)
    assert bar.position_slider.value() >= 60

    bar.set_progress(3.0, 100.0)
    assert bar.position_slider.value() >= 60, "拖动中被播放进度回写覆盖"

    _mouse(bar.position_slider, QEvent.Type.MouseButtonRelease, 150, _NONE)
    bar.set_progress(3.0, 100.0)
    assert bar.position_slider.value() == 3, "释放后应恢复正常回写"


def test_disabled_or_empty_range_falls_through(qapp, bar):
    """无曲目(range 0..0)时左键不接管,不抛错。"""
    bar.position_slider.setRange(0, 0)
    _mouse(bar.position_slider, QEvent.Type.MouseButtonPress, 100, _LEFT)
    assert not bar.position_slider.isSliderDown()
