"""流光背景层测试:开关、纯色档、流动、换歌过渡、停表。"""

from __future__ import annotations

import pytest

from neriplayer_win.ui.flow_background import FlowBackground
from neriplayer_win.ui.flow_palette import flow_colors_from_palette
from neriplayer_win.ui.theme import PALETTES, THEME_DARK, THEME_LIGHT

_COLORS = [
    (0.5, 0.6, 0.9),
    (0.9, 0.6, 0.5),
    (0.8, 0.5, 0.7),
    (0.5, 0.5, 0.8),
    (0.6, 0.7, 0.8),
]


@pytest.fixture
def bg(qapp):
    widget = FlowBackground()
    widget.resize(600, 400)
    yield widget
    widget.stop()
    widget.deleteLater()


# -- 开关与流动 -----------------------------------------------------------

def test_disabled_by_default_does_not_tick(bg):
    """默认未启用,不应有定时器在跑。"""
    assert bg.running is False


def test_set_enabled_true_does_not_auto_start(bg):
    """启用只是允许绘制;流动要显式 start。"""
    bg.set_enabled(True)
    assert bg._enabled is True
    assert bg.running is False


def test_start_and_stop(bg):
    bg.set_enabled(True)
    bg.start()
    assert bg.running is True
    bg.stop()
    assert bg.running is False


def test_hide_stops_timer(bg):
    """窗口隐藏到托盘时必须停表,不占后台 CPU。"""
    bg.set_enabled(True)
    bg.show()
    bg.start()
    assert bg.running is True
    bg.hide()
    assert bg.running is False


# -- 纯色档 --------------------------------------------------------------

def test_solid_mode_stops_timer(bg):
    """纯色档不流动:设定后定时器停。"""
    bg.set_enabled(True)
    bg.start()
    bg.set_solid(True)
    assert bg.running is False


def test_flow_mode_is_not_solid(bg):
    bg.set_solid(True)
    bg.set_solid(False)
    assert bg._solid is False


# -- 换歌过渡 ------------------------------------------------------------

def test_set_colors_animates_by_default(bg):
    """默认走过渡:目标色与当前色分离,transition 从 0 开始。"""
    bg.set_colors(_COLORS)
    other = [(1.0 - r, 1.0 - g, 1.0 - b) for r, g, b in _COLORS]
    bg.set_colors(other)
    assert bg._transition < 1.0
    assert bg._to_colors == other


def test_transition_completes_after_frames(bg):
    """推进足够帧后过渡完成,颜色收敛到目标值。"""
    bg.set_colors(_COLORS)
    target = [(0.1, 0.2, 0.3)] * 5
    bg.set_colors(target)
    for _ in range(40):  # 40 x 20ms = 800ms > 520ms
        bg._on_frame()
    assert bg._transition == 1.0
    for got, want in zip(bg._colors, target):
        assert got == pytest.approx(want, abs=1e-6)


def test_set_colors_without_animate_applies_immediately(bg):
    bg.set_colors(_COLORS, animate=False)
    assert bg._transition == 1.0
    assert bg._colors == _COLORS


def test_set_colors_rejects_wrong_count(bg):
    with pytest.raises(ValueError):
        bg.set_colors([(0.1, 0.2, 0.3)])


# -- 五色派生 ------------------------------------------------------------

def test_flow_colors_returns_five_distinct_values():
    colors = flow_colors_from_palette(PALETTES[THEME_DARK], dark=True)
    assert len(colors) == 5
    # 至少两种不同颜色(不能五色全同)
    assert len({tuple(round(v, 3) for v in c) for c in colors}) > 1


def test_dark_theme_colors_are_not_all_white():
    """暗色主题的 primary 本身已亮,派生后不能全糊成白。"""
    colors = flow_colors_from_palette(PALETTES[THEME_DARK], dark=True)
    for r, g, b in colors:
        # 不能三个通道都接近 1.0
        assert not (r > 0.97 and g > 0.97 and b > 0.97)


def test_light_theme_colors_are_bright_enough():
    """亮色主题的 primary 是深色,派生后要够亮才像光。"""
    colors = flow_colors_from_palette(PALETTES[THEME_LIGHT], dark=False)
    for r, g, b in colors:
        assert max(r, g, b) > 0.6


def test_flow_colors_handles_missing_palette_keys():
    colors = flow_colors_from_palette({}, dark=True)
    assert len(colors) == 5
    for c in colors:
        assert len(c) == 3
