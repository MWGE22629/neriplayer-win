"""播放统计页 UI 测试:列定义、排序、空态、时长格式。"""

from __future__ import annotations

import pytest

from neriplayer_win.data.play_stats import PlayStat, format_play_duration
from neriplayer_win.ui.stats_page import StatsPage


def _stat(key: str, title: str, listen_ms: int, count: int, last: int = 0) -> PlayStat:
    source, sid = key.split(":", 1)
    return PlayStat(
        key=key, source=source, song_id=int(sid), title=title,
        listen_ms=listen_ms, play_count=count, last_played_at=last,
    )


@pytest.fixture
def page(qapp):
    widget = StatsPage()
    widget.resize(900, 600)
    yield widget
    widget.deleteLater()


# -- 列定义 ----------------------------------------------------------------

def test_columns_replace_artist_and_duration(page):
    """列头应把「作者」「歌曲时长」换成「播放时长」「播放次数」。"""
    page.set_stats([_stat("netease:1", "富士山下", 60_000, 3)])
    labels = [
        page.table.horizontalHeaderItem(i).text()
        for i in range(page.table.columnCount())
    ]
    assert "播放时长" in labels[3]
    assert "播放次数" in labels[4]
    joined = "".join(labels)
    assert "作者" not in joined
    assert "时长" in joined  # 播放时长里有「时长」,但不能有单独的歌曲时长列


def test_no_last_played_column(page):
    """不显示「最近播放」列(播放时长不是最近播放时间)。"""
    page.set_stats([_stat("netease:1", "富士山下", 60_000, 3, last=1_798_760_000_000)])
    labels = [
        page.table.horizontalHeaderItem(i).text()
        for i in range(page.table.columnCount())
    ]
    assert not any("最近" in text for text in labels)
    assert page.table.columnCount() == 5


# -- 时长格式 --------------------------------------------------------------

def test_duration_formatted_as_day_hms(page):
    page.set_stats([_stat("netease:1", "富士山下", 187_340_000, 3)])
    rows = page.visible_rows()
    assert rows[0][1] == "2天 04:02:20"


def test_duration_under_one_day_has_no_day_part(page):
    page.set_stats([_stat("netease:1", "富士山下", 47_000, 1)])
    rows = page.visible_rows()
    assert rows[0][1] == "00:00:47"


# -- 排序 ------------------------------------------------------------------

def test_default_sort_by_play_count_desc(page):
    """默认按播放次数降序:最常听的在前。"""
    page.set_stats([
        _stat("netease:1", "A", 60_000, 2),
        _stat("netease:2", "B", 60_000, 9),
        _stat("netease:3", "C", 60_000, 5),
    ])
    rows = page.visible_rows()
    assert [r[0] for r in rows] == ["B", "C", "A"]


def test_click_count_header_toggles_direction(page):
    page.set_stats([
        _stat("netease:1", "A", 60_000, 2),
        _stat("netease:2", "B", 60_000, 9),
    ])
    assert [r[0] for r in page.visible_rows()] == ["B", "A"]
    page._on_header_clicked(4)  # 播放次数列
    assert [r[0] for r in page.visible_rows()] == ["A", "B"]


def test_click_duration_header_sorts_by_time(page):
    page.set_stats([
        _stat("netease:1", "A", 120_000, 1),
        _stat("netease:2", "B", 30_000, 5),
    ])
    page._on_header_clicked(3)  # 播放时长列
    assert [r[0] for r in page.visible_rows()] == ["A", "B"]


def test_sort_arrow_shown_on_active_column(page):
    page.set_stats([_stat("netease:1", "A", 60_000, 1)])
    header = page.table.horizontalHeaderItem(4).text()
    assert "▼" in header
    page._on_header_clicked(4)
    header = page.table.horizontalHeaderItem(4).text()
    assert "▲" in header


# -- 空态 ------------------------------------------------------------------

def test_empty_state_shown_when_no_stats(page):
    page.set_stats([])
    assert page.stack.currentIndex() == 1
    assert page.empty_label.text() == "还没有播放记录"


def test_table_shown_when_has_stats(page):
    page.set_stats([_stat("netease:1", "A", 60_000, 1)])
    assert page.stack.currentIndex() == 0


def test_unknown_title_fallback(page):
    page.set_stats([_stat("netease:1", "", 60_000, 1)])
    assert page.visible_rows()[0][0] == "(未知歌曲)"


# -- 语言切换 --------------------------------------------------------------

def test_retranslate_updates_headers(page):
    from neriplayer_win import i18n

    original = i18n.current_language()
    try:
        i18n.set_language("en", notify=False)
        page.retranslate()
        labels = [
            page.table.horizontalHeaderItem(i).text()
            for i in range(page.table.columnCount())
        ]
        assert "Listening time" in labels[3]
        assert "Play count" in labels[4]
    finally:
        i18n.set_language(original, notify=False)
        page.retranslate()
