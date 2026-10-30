"""音量记忆(M10):settings.json 的 volume 键与播放条/引擎的联动。

覆盖:
- store 侧的 volume 校验已在 test_store.py;这里只测窗口接线
- 启动回填:文件里的 volume 落到滑块与引擎
- 拖动滑块:引擎即时跟随,防抖窗口内合并落盘
- 改动后立刻退出:closeEvent 补存,不丢最后一次音量
"""

from __future__ import annotations

import json

from PySide6.QtTest import QTest

from neriplayer_win.data.store import SETTING_VOLUME
from neriplayer_win.ui.main_window import _VOLUME_SAVE_DELAY_MS, MainWindow


def _make_window(qapp, monkeypatch, tmp_path) -> MainWindow:
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window


def _close(window) -> None:
    window._force_exit = True
    window.close()


def _stored_volume(tmp_path) -> int:
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    return data["volume"]


class TestVolumeRestore:
    def test_default_is_70(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window.player_bar.volume_slider.value() == 70
            if window.engine is not None:
                assert window.engine.volume() == 70
        finally:
            _close(window)

    def test_stored_volume_applied_on_startup(self, qapp, monkeypatch, tmp_path):
        (tmp_path / "settings.json").write_text(
            json.dumps({SETTING_VOLUME: 23}), encoding="utf-8"
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window.player_bar.volume_slider.value() == 23
            if window.engine is not None:
                assert window.engine.volume() == 23
        finally:
            _close(window)

    def test_illegal_stored_volume_falls_back(self, qapp, monkeypatch, tmp_path):
        (tmp_path / "settings.json").write_text(
            json.dumps({SETTING_VOLUME: 101}), encoding="utf-8"
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window.player_bar.volume_slider.value() == 70
        finally:
            _close(window)


class TestVolumePersistence:
    def test_slider_change_reaches_engine(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window.player_bar.volume_slider.setValue(38)
            qapp.processEvents()
            assert window._settings[SETTING_VOLUME] == 38
            if window.engine is not None:
                assert window.engine.volume() == 38
        finally:
            _close(window)

    def test_change_persisted_after_debounce(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window.player_bar.volume_slider.setValue(44)
            QTest.qWait(_VOLUME_SAVE_DELAY_MS + 200)
            assert _stored_volume(tmp_path) == 44
        finally:
            _close(window)

    def test_rapid_changes_write_once(self, qapp, monkeypatch, tmp_path):
        """拖动过程中的中间值不必逐次落盘,停手后只留最终值。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            for value in (60, 50, 40, 30, 25):
                window.player_bar.volume_slider.setValue(value)
            assert window._volume_save_timer.isActive()
            # 防抖窗口内文件里还没出现最终值
            assert not (tmp_path / "settings.json").exists() or (
                _stored_volume(tmp_path) != 25
            )
            QTest.qWait(_VOLUME_SAVE_DELAY_MS + 200)
            assert _stored_volume(tmp_path) == 25
        finally:
            _close(window)

    def test_exit_flushes_pending_change(self, qapp, monkeypatch, tmp_path):
        """改动后立刻退出:防抖还没到点,closeEvent 必须补存。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        window.player_bar.volume_slider.setValue(12)
        assert window._volume_save_timer.isActive()
        _close(window)
        assert _stored_volume(tmp_path) == 12

    def test_restored_volume_survives_restart(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        window.player_bar.volume_slider.setValue(67)
        _close(window)
        again = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert again.player_bar.volume_slider.value() == 67
        finally:
            _close(again)
