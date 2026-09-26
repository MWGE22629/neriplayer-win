"""动效回归(借鉴 A 端参数体系的桌面移植):

- 播放/暂停图标缩放淡入过渡(AnimatedIconButton)
- 封面切歌交叉淡入 + 清空防闪延迟(_CoverLabel / PlayerBar)
- 播放条橡皮筋横滑切歌(swipe_resisted_offset / _SwipeHost)
- 中央页面切换快出慢进(_SlideStack)
- 歌曲表正在播放均衡器条(MainWindow._eq_* 状态机)
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QAbstractAnimation, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QWidget

from neriplayer_win.player.queue import BackupUrlRotator, QueueSong
from neriplayer_win.ui import main_window as main_window_module
from neriplayer_win.ui.main_window import MainWindow
from neriplayer_win.ui.player_bar import (
    _CoverLabel,
    _SwipeHost,
    AnimatedIconButton,
    swipe_resisted_offset,
)
from neriplayer_win.ui.icons import tinted_icon_with_color


def _make_window(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window


def _close(window) -> None:
    window._force_exit = True
    window.close()


def _song(n: int) -> QueueSong:
    return QueueSong(
        source="netease", id=n, bvid="", title=f"歌{n}",
        artist="测试", duration_ms=60000,
    )


class _StubEngine:
    """只记录调用不真播(与 test_main_window_layout 同款,本文件自持一份)。"""

    def __init__(self) -> None:
        self.played: list[str] = []

    def play_url(self, url: str, headers=None) -> None:
        self.played.append(url)

    def stop(self) -> None:
        pass


def _solid_pixmap(color: str, size: int = 40) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(color))
    return pixmap


# ---------------------------------------------------------------------------
# 橡皮筋阻尼数学
# ---------------------------------------------------------------------------


class TestSwipeResistance:
    def test_zero_and_sign(self):
        assert swipe_resisted_offset(0.0) == 0.0
        assert swipe_resisted_offset(30.0) > 0
        assert swipe_resisted_offset(-30.0) < 0

    def test_capped_below_peak(self):
        """阻尼位移单调递增且永远封顶在 52px 以内(拖得越远越费力)。"""
        previous = 0.0
        for delta in (10, 30, 72, 200, 1000):
            offset = swipe_resisted_offset(float(delta))
            assert previous < offset < 52.0
            previous = offset

    def test_small_drag_undamped_approx(self):
        """小位移近似线性(1-exp(-x)≈x):阻尼感从行程中段才明显。"""
        assert abs(swipe_resisted_offset(1.0) - 1.0) < 0.01


class TestSwipeHost:
    def _make_host(self, qapp) -> _SwipeHost:
        container = QWidget()
        host = _SwipeHost(container)
        host.setGeometry(10, 10, 200, 40)
        container.resize(300, 60)
        container.show()
        host._keepalive = container  # 防容器被 GC 连带删掉宿主
        qapp.processEvents()
        return host

    def test_swipe_past_threshold_emits_next_and_springs_back(self, qapp):
        host = self._make_host(qapp)
        fired: list[str] = []
        host.prev_swiped.connect(lambda: fired.append("prev"))
        host.next_swiped.connect(lambda: fired.append("next"))
        origin = host.pos()
        host._begin_swipe(500.0)
        assert host._dragging is True
        host._update_swipe(500.0 + 80.0)  # 超过 72px 阈值
        assert host.pos().x() > origin.x()  # 随拖动平移(阻尼后)
        host._end_swipe(500.0 + 80.0)
        assert fired == ["next"]
        assert host._dragging is False
        QTest.qWait(260)  # 弹回动画 200ms
        assert host.pos() == origin

    def test_swipe_left_emits_prev(self, qapp):
        host = self._make_host(qapp)
        fired: list[str] = []
        host.prev_swiped.connect(lambda: fired.append("prev"))
        origin = host.pos()
        host._begin_swipe(500.0)
        host._update_swipe(500.0 - 90.0)
        assert host.pos().x() < origin.x()
        host._end_swipe(500.0 - 90.0)
        assert fired == ["prev"]

    def test_short_swipe_no_emit(self, qapp):
        host = self._make_host(qapp)
        fired: list[str] = []
        host.prev_swiped.connect(lambda: fired.append("prev"))
        host.next_swiped.connect(lambda: fired.append("next"))
        host._begin_swipe(500.0)
        host._update_swipe(520.0)
        host._end_swipe(520.0)  # 仅 20px:未达阈值
        assert fired == []
        QTest.qWait(260)
        assert host.pos() == host._origin

    def test_end_without_begin_ignored(self, qapp):
        host = self._make_host(qapp)
        host._end_swipe(600.0)  # 未按下:无害
        assert host._dragging is False


# ---------------------------------------------------------------------------
# 播放/暂停图标过渡
# ---------------------------------------------------------------------------


class TestAnimatedIconButton:
    def _button(self) -> AnimatedIconButton:
        button = AnimatedIconButton()
        button.setFixedSize(44, 44)
        button.setIconSize(QSize(22, 22))
        button.show()
        return button

    def test_new_icon_crossfades(self, qapp):
        button = self._button()
        play = QIcon(_solid_pixmap("#336699", 22))
        pause = QIcon(_solid_pixmap("#993366", 22))
        button.setIcon(play)
        assert button._fade.state() != QAbstractAnimation.State.Running  # 首设不动画
        button.setIcon(pause)
        assert button._fade.state() == QAbstractAnimation.State.Running
        QTest.qWait(260)
        assert button._fade.state() != QAbstractAnimation.State.Running
        assert button.icon().isNull() is False

    def test_same_icon_instance_skips_animation(self, qapp):
        button = self._button()
        icon = QIcon(_solid_pixmap("#336699", 22))
        button.setIcon(icon)
        other = QIcon(_solid_pixmap("#993366", 22))
        button.setIcon(other)
        QTest.qWait(260)
        button.setIcon(other)  # 同一实例(缓存命中路径):不重放
        assert button._fade.state() != QAbstractAnimation.State.Running


# ---------------------------------------------------------------------------
# 封面交叉淡入与防闪清理
# ---------------------------------------------------------------------------


class TestCoverLabel:
    def test_cover_change_crossfades(self, qapp):
        label = _CoverLabel()
        label.setFixedSize(40, 40)
        label.show()
        label.set_placeholder(_solid_pixmap("#111111", 28))
        label.set_cover_pixmap(_solid_pixmap("#222222", 40))
        assert label._fade.state() == QAbstractAnimation.State.Running
        QTest.qWait(260)
        assert label._fade.state() != QAbstractAnimation.State.Running
        assert label._fading_from is None

    def test_clear_delayed_and_cancelled_by_new_cover(self, qapp):
        label = _CoverLabel()
        label.setFixedSize(40, 40)
        label.show()
        cover = _solid_pixmap("#222222", 40)
        placeholder = _solid_pixmap("#111111", 28)
        label.set_cover_pixmap(cover)
        label.schedule_clear(placeholder)
        QTest.qWait(300)  # < 900ms:仍是旧封面,占位未上
        assert label.pixmap().toImage() == cover.toImage()
        # 新封面到达:挂起的清空取消
        fresh = _solid_pixmap("#333333", 40)
        label.cancel_pending_clear()
        label.set_cover_pixmap(fresh)
        QTest.qWait(950)
        assert label.pixmap().toImage() == fresh.toImage()

    def test_clear_fires_after_delay(self, qapp):
        label = _CoverLabel()
        label.setFixedSize(40, 40)
        label.show()
        cover = _solid_pixmap("#222222", 40)
        placeholder = _solid_pixmap("#111111", 28)
        label.set_cover_pixmap(cover)
        label.schedule_clear(placeholder)
        QTest.qWait(950)
        assert label.pixmap().toImage() == placeholder.toImage()


class TestPlayerBarCover:
    def test_set_cover_empty_delays_placeholder(self, qapp, monkeypatch):
        from neriplayer_win.ui.player_bar import PlayerBar

        bar = PlayerBar()
        bar.show()
        monkeypatch.setattr(bar.cover_loader, "request", lambda *_: None)
        try:
            before = bar.cover_label.pixmap().toImage()
            bar.set_cover("")  # 不应立刻闪占位图
            bar.set_cover("http://example/x")  # 连续切歌:取消挂起清理
            QTest.qWait(950)
            after = bar.cover_label.pixmap().toImage()
            assert after == before  # 无网络回调:保持占位,且未被重复置换
        finally:
            bar.close()


# ---------------------------------------------------------------------------
# 中央页面切换过渡
# ---------------------------------------------------------------------------


class TestSlideStack:
    def test_visible_switch_runs_transition_and_settles(self, qapp):
        from neriplayer_win.ui.main_window import _SlideStack

        stack = _SlideStack()
        page0 = QWidget()
        page0.setObjectName("p0")
        page1 = QLabel("page1")
        stack.addWidget(page0)
        stack.addWidget(page1)
        stack.resize(400, 300)
        stack.show()
        qapp.processEvents()

        stack.setCurrentIndex(1)
        assert stack.currentIndex() == 1  # 索引立即生效
        assert stack._overlay is not None  # 旧页截图在场(快出)
        QTest.qWait(180)  # > 120ms:overlay 已删
        assert stack._overlay is None
        assert stack._in_widget is page1
        QTest.qWait(350)  # > 300ms:新页归位、效果移除
        assert stack._in_widget is None
        assert page1.pos() == QPoint(0, 0)
        assert page1.graphicsEffect() is None

        # 反向切回同样收敛
        stack.setCurrentIndex(0)
        assert stack.currentIndex() == 0
        QTest.qWait(500)
        assert stack._overlay is None and stack._in_widget is None
        assert page0.pos() == QPoint(0, 0)

    def test_same_index_no_transition(self, qapp):
        from neriplayer_win.ui.main_window import _SlideStack

        stack = _SlideStack()
        stack.addWidget(QWidget())
        stack.addWidget(QWidget())
        stack.show()
        qapp.processEvents()
        stack.setCurrentIndex(1)
        QTest.qWait(500)
        stack.setCurrentIndex(1)  # 同索引
        assert stack._overlay is None

    def test_hidden_stack_switches_directly(self, qapp):
        from neriplayer_win.ui.main_window import _SlideStack

        stack = _SlideStack()
        stack.addWidget(QWidget())
        stack.addWidget(QWidget())
        stack.resize(400, 300)
        stack.show()
        qapp.processEvents()
        stack.hide()
        stack.setCurrentIndex(1)  # 不可见:直接切换(启动初始化路径)
        assert stack.currentIndex() == 1
        assert stack._overlay is None

    def test_rapid_reswitch_settles_to_latest(self, qapp):
        from neriplayer_win.ui.main_window import _SlideStack

        stack = _SlideStack()
        pages = [QWidget() for _ in range(3)]
        for page in pages:
            stack.addWidget(page)
        stack.resize(400, 300)
        stack.show()
        qapp.processEvents()
        stack.setCurrentIndex(1)
        QTest.qWait(50)  # 过渡中再次切换
        stack.setCurrentIndex(2)
        assert stack.currentIndex() == 2
        QTest.qWait(500)
        assert stack._overlay is None and stack._in_widget is None
        assert pages[2].pos() == QPoint(0, 0)


# ---------------------------------------------------------------------------
# 歌曲表正在播放均衡器条
# ---------------------------------------------------------------------------


class TestNowPlayingBars:
    def test_row_tracks_active_song(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            songs = [_song(i) for i in range(3)]
            window._set_table_songs(songs, "netease", None)
            assert window._eq_state.row is None  # 未起播
            window._active = main_window_module._ActivePlay(
                song=songs[1], rotator=BackupUrlRotator(["http://x"]), headers=None
            )
            window._update_playing_row()
            assert window._eq_state.row == 1
            # 换歌:行号跟随
            window._active.song = songs[2]
            window._update_playing_row()
            assert window._eq_state.row == 2
            # 清表:复位
            window._clear_table()
            assert window._eq_state.row is None
        finally:
            _close(window)

    def test_playing_state_drives_level_and_timer(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            songs = [_song(i) for i in range(2)]
            window._set_table_songs(songs, "netease", None)
            window._active = main_window_module._ActivePlay(
                song=songs[0], rotator=BackupUrlRotator(["http://x"]), headers=None
            )
            window._update_playing_row()
            window._eq_set_active(True)
            QTest.qWait(220)  # level 180ms 过渡
            assert window._eq_state.level == pytest.approx(1.0)
            assert window._eq_timer.isActive()
            phase_before = window._eq_state.phase_ms
            QTest.qWait(100)
            assert window._eq_state.phase_ms > phase_before  # 相位在走
            window._eq_set_active(False)
            QTest.qWait(220)
            assert window._eq_state.level == pytest.approx(0.0)
            assert not window._eq_timer.isActive()
        finally:
            _close(window)

    def test_song_not_in_table_keeps_row_none(self, qapp, monkeypatch, tmp_path):
        """播放中的歌不在当前浏览列表:无均衡器条(全部行正常序号)。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._set_table_songs([_song(i) for i in range(2)], "netease", None)
            window._active = main_window_module._ActivePlay(
                song=_song(99), rotator=BackupUrlRotator(["http://x"]), headers=None
            )
            window._update_playing_row()
            assert window._eq_state.row is None
        finally:
            _close(window)


# ---------------------------------------------------------------------------
# 任务栏工具栏与主窗口接线(离屏下降级为空操作,不崩即可)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 任务栏歌名标题:等宽规范(截断/补齐)与生命周期
# ---------------------------------------------------------------------------


class TestTaskbarTitle:
    def _metrics(self, qapp):
        from PySide6.QtGui import QFontMetrics

        return QFontMetrics(qapp.font())

    def test_empty_falls_back_to_base(self, qapp):
        from neriplayer_win.ui.main_window import _TITLE_BASE, taskbar_title

        assert taskbar_title("", self._metrics(qapp)) == _TITLE_BASE

    def test_base_text_unchanged(self, qapp):
        from neriplayer_win.ui.main_window import _TITLE_BASE, taskbar_title

        metrics = self._metrics(qapp)
        assert taskbar_title(_TITLE_BASE, metrics) == _TITLE_BASE

    def test_long_title_elided_within_target(self, qapp):
        from neriplayer_win.ui.main_window import _TITLE_BASE, taskbar_title

        metrics = self._metrics(qapp)
        target = metrics.horizontalAdvance(_TITLE_BASE)
        long_name = "超级无敌长的歌名" * 10
        result = taskbar_title(long_name, metrics)
        assert result.endswith("…")
        assert metrics.horizontalAdvance(result) <= target
        assert long_name not in result  # 确实截掉了内容

    def test_short_title_padded_near_target(self, qapp):
        """短歌名补 NBSP(普通空格会被 Win11 任务栏文本层折叠):补后宽度
        不超过基准,且距基准不足一个空白宽。"""
        from neriplayer_win.ui.main_window import _TITLE_BASE, taskbar_title

        metrics = self._metrics(qapp)
        target = metrics.horizontalAdvance(_TITLE_BASE)
        blank = metrics.horizontalAdvance("\u00a0") or metrics.horizontalAdvance(" ")
        for short in ("Hi", "歌", "A B C"):
            result = taskbar_title(short, metrics)
            assert result.rstrip(" \u00a0") == short  # 只补空白不改内容
            assert "\u00a0" in result  # 补齐字符必须存在(空格会被折叠)
            width = metrics.horizontalAdvance(result)
            assert width <= target
            assert target - width < blank

    def test_ascii_and_cjk_same_family_consistent(self, qapp):
        """同类歌名(纯 ASCII / 纯 CJK)规范后宽度互相接近(≤ 一个空格差
        外加一个字符宽度容差),任务栏按钮肉眼不抖。"""
        from neriplayer_win.ui.main_window import _TITLE_BASE, taskbar_title

        metrics = self._metrics(qapp)
        target = metrics.horizontalAdvance(_TITLE_BASE)
        for name in ("Song A", "歌曲甲", "Track 01 演唱会版本"):
            width = metrics.horizontalAdvance(taskbar_title(name, metrics))
            assert abs(width - target) <= max(
                metrics.horizontalAdvance(" "), metrics.horizontalAdvance("字")
            )

    def test_start_play_sets_title_and_stop_resets(self, qapp, monkeypatch, tmp_path):
        from neriplayer_win.ui.main_window import _TITLE_BASE

        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window.windowTitle() == _TITLE_BASE
            song = QueueSong(
                source="netease", id=1, bvid="", title="晴天",
                artist="测试", duration_ms=60000,
            )
            window._queue.replace([song])
            window._queue.jump(0)
            window._start_play(song, BackupUrlRotator(["http://example/a"]), None)
            title = window.windowTitle()
            assert title.rstrip(" \u00a0") == "晴天"  # 歌名进标题(可能补空白)
            window._on_media_stop()
            assert window.windowTitle() == _TITLE_BASE
        finally:
            _close(window)

    def test_sequence_finished_resets_title(self, qapp, monkeypatch, tmp_path):
        """顺序模式播完队尾:任务栏恢复基础标题。"""
        from neriplayer_win.ui.main_window import _TITLE_BASE

        window = _make_window(qapp, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "engine", _StubEngine())
        try:
            song = QueueSong(
                source="netease", id=1, bvid="", title="孤曲",
                artist="测试", duration_ms=60000,
            )
            window._queue.replace([song])
            window._queue.jump(0)
            window._start_play(song, BackupUrlRotator(["http://example/a"]), None)
            assert window.windowTitle().rstrip(" \u00a0") == "孤曲"
            window._on_track_ended()  # 顺序模式队尾:播完即停
            assert window.windowTitle() == _TITLE_BASE
        finally:
            _close(window)


class TestTaskbarWiring:
    def test_playing_changed_reaches_taskbar_state(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window._taskbar is not None
            assert window._taskbar._playing is False
            window._on_playing_changed(True)
            assert window._taskbar._playing is True
            assert window.player_bar._playing is True
            window._on_playing_changed(False)
            assert window._taskbar._playing is False
        finally:
            _close(window)

    def test_show_event_attach_attempt_harmless(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window.show()  # 二次 show:attach 重试(离屏失败)不抛
            qapp.processEvents()
        finally:
            _close(window)

    def test_native_event_routes_thumbbar_commands(self, qapp, monkeypatch, tmp_path):
        """窗口过程层分发:MainWindow.nativeEvent 必须把缩略图按钮的
        WM_COMMAND 转成任务栏按钮信号(跨进程 SendMessage 直发路径,
        应用级过滤器接不到,回归根因见 2026-09-26 修复)。"""
        import ctypes

        from neriplayer_win.ui.media_keys import _MSG
        from neriplayer_win.ui.taskbar import _THBN_CLICKED, WM_COMMAND

        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            bar = window._taskbar
            assert bar is not None
            bar._hwnd = int(window.winId())
            fired: list[str] = []
            bar.play_pause_requested.connect(lambda: fired.append("pp"))
            bar.prev_requested.connect(lambda: fired.append("prev"))
            msg = _MSG()
            msg.hwnd = bar._hwnd
            msg.message = WM_COMMAND
            msg.wParam = (_THBN_CLICKED << 16) | 0x8002
            msg.lParam = 0
            consumed, _result = window.nativeEvent(
                b"windows_generic_MSG", ctypes.addressof(msg)
            )
            assert consumed is True
            assert fired == ["pp"]
            # 非目标消息原样放行,不影响 Qt 默认处理
            other = _MSG()
            other.hwnd = bar._hwnd
            other.message = 0x0005  # WM_SIZE
            other.wParam = 0
            other.lParam = 0
            consumed2, _ = window.nativeEvent(
                b"windows_generic_MSG", ctypes.addressof(other)
            )
            assert consumed2 is False
        finally:
            _close(window)
