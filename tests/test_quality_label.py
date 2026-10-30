"""音质标签(QualityLabel)两态切换与文案构造的回归测试。

背景:音质标签要求「宽窗口显示档位+码率,窄窗口只显示档位,不出现省略号」。
这里锁住三件事:
1. 两态文案构造(三源各自的键与码率组合);
2. 宽度驱动整态切换,全程不产生省略号;
3. minimumSizeHint 取短形态宽度,因此不抬高窗口最小宽。
"""

from __future__ import annotations

import pytest

from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout

from neriplayer_win import i18n
from neriplayer_win.ui.player_bar import PlayerBar, QualityLabel

_FULL = "无损 FLAC · 1411 kbps"
_SHORT = "无损 FLAC"


@pytest.fixture
def bar(qapp):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    player_bar = PlayerBar()
    layout.addWidget(player_bar)
    widget.resize(900, 120)
    widget.show()
    qapp.processEvents()
    yield player_bar
    widget.close()


def _label_parts(player_bar: PlayerBar) -> tuple[str, str]:
    label = player_bar.quality_label
    return label._full, label._short


# -- 文案构造 ----------------------------------------------------------------

def test_quality_parts_with_bitrate(bar):
    """档位 + 码率:完整形态带中点与 kbps,短形态只有档位。"""
    bar.set_quality_parts("quality.lossless", 1411)
    full, short = _label_parts(bar)
    assert full == _FULL
    assert short == _SHORT


def test_quality_parts_without_bitrate_collapses_to_single_state(bar):
    """码率未知时两态相同,只显示档位(网易云走这条路径)。"""
    bar.set_quality_parts("quality.lossless", 0)
    full, short = _label_parts(bar)
    assert full == _SHORT == "无损 FLAC"


def test_quality_parts_bitrate_only(bar):
    """没有档位标签时只显示码率(B站普通音轨)。"""
    bar.set_quality_parts("", 448)
    full, short = _label_parts(bar)
    assert full == short == "448 kbps"


def test_quality_parts_empty_hides_label(bar):
    """无音质信息时标签隐藏,不留空占位。"""
    bar.set_quality_parts("", 0)
    assert not bar.quality_label.isVisible()


def test_clear_quality_hides_label(bar):
    bar.set_quality_parts("quality.lossless", 1411)
    assert bar.quality_label.isVisible()
    bar.clear_quality()
    assert not bar.quality_label.isVisible()


@pytest.mark.parametrize("key, kbps, expected_full, expected_short", [
    ("quality.lossless", 1411, "无损 FLAC · 1411 kbps", "无损 FLAC"),
    ("quality.kugou_lossless", 937, "无损 · 937 kbps", "无损"),
    ("quality.kugou_320", 320, "320k · 320 kbps", "320k"),
    ("quality.hires", 192, "Hi-Res · 192 kbps", "Hi-Res"),
    ("quality.dolby", 448, "杜比 · 448 kbps", "杜比"),
])
def test_quality_parts_per_source(bar, key, kbps, expected_full, expected_short):
    """三源各自的档位名与码率组合。"""
    bar.set_quality_parts(key, kbps)
    full, short = _label_parts(bar)
    assert full == expected_full
    assert short == expected_short


def test_quality_parts_retranslates_on_language_switch(bar):
    """标签存的是 i18n 键,语言切换后 retranslate 应换成英文文案。"""
    bar.set_quality_parts("quality.lossless", 1411)
    assert bar.quality_label.text() == _FULL
    original = i18n.current_language()
    try:
        i18n.set_language("en", notify=False)
        bar.retranslate()
        assert bar.quality_label.text() == "Lossless FLAC · 1411 kbps"
    finally:
        i18n.set_language(original, notify=False)
        bar.retranslate()


# -- 两态切换 ----------------------------------------------------------------

def _set_width(bar: PlayerBar, width: int) -> None:
    host = bar.parentWidget()
    host.resize(width, 120)
    QApplication.processEvents()


def test_narrow_width_shows_short_form_without_ellipsis(bar):
    """窄窗口退成短形态,且不得出现省略号。"""
    bar.set_quality_parts("quality.lossless", 1411)
    _set_width(bar, 600)
    text = bar.quality_label.text()
    assert text == _SHORT
    assert "…" not in text and "..." not in text


def test_wide_width_shows_full_form(bar):
    bar.set_quality_parts("quality.lossless", 1411)
    _set_width(bar, 1100)
    assert bar.quality_label.text() == _FULL


def test_switching_is_reversible(bar):
    """从窄拉回宽,应恢复完整形态(不是单向退化)。"""
    bar.set_quality_parts("quality.lossless", 1411)
    _set_width(bar, 600)
    assert bar.quality_label.text() == _SHORT
    _set_width(bar, 1100)
    assert bar.quality_label.text() == _FULL


def test_no_ellipsis_at_any_width(bar):
    """扫过一段宽度区间,任何宽度下都不应出现省略号。"""
    bar.set_quality_parts("quality.lossless", 1411)
    for width in range(600, 1201, 50):
        _set_width(bar, width)
        text = bar.quality_label.text()
        assert "…" not in text and "..." not in text, f"宽度 {width} 出现省略号"
        assert text in (_FULL, _SHORT), f"宽度 {width} 出现第三态: {text!r}"


# -- 最小宽度 ----------------------------------------------------------------

def test_minimum_size_hint_uses_short_form(bar):
    """minimumSizeHint 取短形态,标签不会把窗口最小宽抬高到完整形态。"""
    bar.set_quality_parts("quality.lossless", 1411)
    label = bar.quality_label
    assert label.minimumSizeHint().width() < label.sizeHint().width()


def test_label_hidden_when_empty_does_not_reserve_width(qapp):
    """空标签不应占位(隐藏控件不参与布局)。"""
    label = QualityLabel()
    assert not label.isVisible()
    assert label.text() == ""
