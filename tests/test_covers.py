"""封面缩略图:CoverLoader 预取池、缓存命中与表格接线。

- 并发上限:preload 池一次最多 5 个在途请求,且确实并发(非串行退化),
  全部 URL 最终投递;
- 批次作废:再次 preload 后,上一批在途/排队的任务全部静默丢弃;
- 缓存:磁盘命中(跨实例)与内存命中(同实例)均不再发网络请求;
- 表格接线:进表先全透明占位,cover_ready 只点亮当前表内的 URL
  (同 URL 多行一次点亮),切表后迟到的回调被丢弃。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from PySide6.QtCore import QBuffer, Qt
from PySide6.QtGui import QColor, QImage

from neriplayer_win.player.queue import QueueSong
from neriplayer_win.ui import covers as covers_module
from neriplayer_win.ui.covers import CoverLoader
from neriplayer_win.ui.main_window import MainWindow


def _png_bytes(color: str = "#ff0000") -> bytes:
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _collect(loader: CoverLoader) -> list[str]:
    """直连收集 cover_ready 的 URL(测试线程无事件循环,用 DirectConnection)。"""
    received: list[str] = []

    def on_ready(url: str, data: bytes) -> None:
        received.append(url)

    loader.cover_ready.connect(on_ready, Qt.ConnectionType.DirectConnection)
    return received


def _wait_until(condition, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "条件等待超时"
        time.sleep(0.02)


def _fake_response(png: bytes) -> SimpleNamespace:
    return SimpleNamespace(content=png, raise_for_status=lambda: None)


class TestPreloadPool:
    def test_bounded_concurrency_and_full_delivery(
        self, qapp, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        loader = CoverLoader()
        png = _png_bytes()
        lock = threading.Lock()
        state = {"active": 0, "peak": 0}
        done = threading.Semaphore(0)

        def fake_get(url, **kwargs):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            return _fake_response(png)

        monkeypatch.setattr(covers_module.httpx, "get", fake_get)
        received: list[str] = []

        def on_ready(url: str, data: bytes) -> None:
            received.append(url)
            done.release()

        loader.cover_ready.connect(on_ready, Qt.ConnectionType.DirectConnection)
        urls = [f"http://img/{i}" for i in range(20)]
        loader.preload(urls)
        for _ in urls:
            assert done.acquire(timeout=15), f"只收到 {len(received)}/20 个封面"
        assert sorted(received) == sorted(urls)
        assert state["peak"] <= 5, f"并发超上限:峰值 {state['peak']}"
        assert state["peak"] >= 2, "预取池没有真正并发(串行退化)"

    def test_next_batch_revokes_previous(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        loader = CoverLoader()
        png = _png_bytes()
        gate = threading.Event()
        slow_started = threading.Event()

        def fake_get(url, **kwargs):
            if "slow" in url:
                slow_started.set()
                gate.wait(timeout=15)
            return _fake_response(png)

        monkeypatch.setattr(covers_module.httpx, "get", fake_get)
        received: list[str] = []

        def on_ready(url: str, data: bytes) -> None:
            received.append(url)

        loader.cover_ready.connect(on_ready, Qt.ConnectionType.DirectConnection)
        # 第一批全部堵在 gate 上(占满全部 worker),一个都没投递
        loader.preload([f"http://img/slow-{i}" for i in range(4)])
        assert slow_started.wait(timeout=15)
        loader.preload(["http://img/next"])  # 版本自增:旧批作废
        gate.set()
        _wait_until(lambda: received == ["http://img/next"])
        loader._tasks.join()  # 旧任务全部被消费(作废路径)
        assert received == ["http://img/next"]

    def test_empty_urls_start_no_workers(self, qapp, monkeypatch, tmp_path):
        """空表(如 _clear_table)只作废,不为空批次拉起线程池。"""
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        loader = CoverLoader()
        loader.preload([])
        loader.preload(["", ""])
        time.sleep(0.1)
        assert not loader._workers_running

    def test_disk_cache_hit_skips_network(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        png = _png_bytes()
        calls: list[str] = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _fake_response(png)

        monkeypatch.setattr(covers_module.httpx, "get", fake_get)
        loader_a = CoverLoader()
        received_a = _collect(loader_a)
        loader_a.preload(["http://img/x"])
        _wait_until(lambda: len(received_a) == 1)
        # 新实例:内存缓存各自独立,磁盘缓存共享
        loader_b = CoverLoader()
        received_b = _collect(loader_b)
        loader_b.preload(["http://img/x"])
        _wait_until(lambda: len(received_b) == 1)
        assert calls == [covers_module.build_thumb_url("http://img/x")]


class TestMemoryCache:
    def test_memory_hit_skips_refetch_and_still_emits(
        self, qapp, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        loader = CoverLoader()
        png = _png_bytes()
        calls: list[str] = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _fake_response(png)

        monkeypatch.setattr(covers_module.httpx, "get", fake_get)
        received = _collect(loader)
        loader.preload(["http://img/y"])
        _wait_until(lambda: len(received) == 1)
        received.clear()
        loader.preload(["http://img/y"])  # 同实例二次预取:命中内存
        _wait_until(lambda: len(received) == 1)
        assert len(calls) == 1  # 只下载过一次
        assert received == ["http://img/y"]  # 内存命中仍正常投递


# ---------------------------------------------------------------------------
# 表格接线:透明占位 → 预取调度 → cover_ready 点亮/丢弃
# ---------------------------------------------------------------------------


def _make_window(qapp, monkeypatch, tmp_path) -> MainWindow:
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window


def _close(window) -> None:
    window._force_exit = True
    window.close()


def _song(n: int, cover: str = "") -> QueueSong:
    return QueueSong(
        source="netease", id=n, bvid="", title=f"歌{n}",
        artist="歌手", duration_ms=60000, cover_url=cover,
    )


def _silence_preload(monkeypatch, window) -> list[list[str]]:
    """记下(而非真发)整表预取请求:接线测试不触网。"""
    requested: list[list[str]] = []
    monkeypatch.setattr(
        window._cover_loader,
        "preload",
        lambda urls: requested.append(list(urls)),
    )
    return requested


class TestTableCoverWiring:
    def test_table_is_five_columns_with_fixed_cover_column(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            from PySide6.QtWidgets import QHeaderView

            table = window.song_table
            assert table.columnCount() == 5
            assert table.columnWidth(0) == 50  # 序号列减半
            assert table.columnWidth(1) == 50  # 封面列
            assert (
                table.horizontalHeader().sectionResizeMode(2)
                == QHeaderView.ResizeMode.Stretch
            )  # 标题列仍 Stretch
            assert table.verticalHeader().defaultSectionSize() == 32
        finally:
            _close(window)

    def test_fill_starts_transparent_and_preloads_in_row_order(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            requested = _silence_preload(monkeypatch, window)
            songs = [
                _song(1, "http://img/a"),
                _song(2, "http://img/a"),
                _song(3, "http://img/b"),
            ]
            window._set_table_songs(songs, "netease", None)
            # 按行序整表交给预取池(去重在池内做)
            assert requested == [
                ["http://img/a", "http://img/a", "http://img/b"]
            ]
            for row in range(3):
                item = window.song_table.item(row, 1)
                assert item is not None and item.icon().isNull(), "进表必须透明占位"
        finally:
            _close(window)

    def test_cover_ready_lights_matching_rows_and_drops_foreign(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _silence_preload(monkeypatch, window)
            songs = [
                _song(1, "http://img/a"),
                _song(2, "http://img/a"),
                _song(3, "http://img/b"),
            ]
            window._set_table_songs(songs, "netease", None)
            window._on_table_cover_ready("http://img/a", _png_bytes("#00ff00"))
            assert not window.song_table.item(0, 1).icon().isNull()
            assert not window.song_table.item(1, 1).icon().isNull()  # 同 URL 多行
            assert window.song_table.item(2, 1).icon().isNull()  # b 尚未到
            # 不属于当前表的 URL:静默丢弃,且不污染图标缓存
            window._on_table_cover_ready("http://img/elsewhere", _png_bytes())
            assert "http://img/elsewhere" not in window._cover_icons
        finally:
            _close(window)

    def test_switching_table_discards_stale_covers(
        self, qapp, monkeypatch, tmp_path
    ):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _silence_preload(monkeypatch, window)
            window._set_table_songs([_song(1, "http://img/a")], "netease", None)
            window._set_table_songs([_song(9, "http://img/z")], "bili", None)
            # 旧表迟到的回调:当前表没有该 URL,直接丢弃
            window._on_table_cover_ready("http://img/a", _png_bytes())
            assert window.song_table.item(0, 1).icon().isNull()
            assert "http://img/a" not in window._cover_icons
        finally:
            _close(window)

    def test_cover_icon_carries_hidpi_backing(self, qapp, monkeypatch, tmp_path):
        """缩略图必须带设备 DPR 底图:125%/150% 缩放屏上不糊(对齐侧栏图标先例)。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            _silence_preload(monkeypatch, window)
            window._set_table_songs([_song(1, "http://img/a")], "netease", None)
            window._on_table_cover_ready("http://img/a", _png_bytes("#0000ff"))
            icon = window._cover_icons["http://img/a"]
            dpr = window.song_table.devicePixelRatioF()
            pixmap = icon.pixmap(window.song_table.iconSize(), dpr)
            assert not pixmap.isNull()
            assert (
                abs(pixmap.devicePixelRatio() - dpr) < 0.01
                or pixmap.width() >= 20 * dpr - 0.5
            )
        finally:
            _close(window)
