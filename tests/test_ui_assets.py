"""UI 资产与图标加载测试(M4):SVG/ico/png 全部可加载、染色生效、
播放条按钮图标状态、设置页外观信号、主窗口主题切换不崩。"""

from __future__ import annotations

import json

import pytest

from neriplayer_win.ui import icons, theme
from neriplayer_win.ui.icons import (
    ICON_NAMES,
    app_icon,
    plain_icon,
    tinted_icon,
    tray_icon,
)
from neriplayer_win.ui.main_window import MainWindow
from neriplayer_win.ui.player_bar import PlayerBar
from neriplayer_win.ui.settings_page import SettingsPage


@pytest.fixture(autouse=True)
def _reset_theme(qapp):
    """图标缓存与主题状态隔离:进入前复位暗色,退出后同样复位。"""
    from neriplayer_win.ui.theme import ThemeManager

    ThemeManager().apply(theme.THEME_DARK)
    icons.clear_cache()
    yield
    ThemeManager().apply(theme.THEME_DARK)
    icons.clear_cache()


class TestIconAssets:
    @pytest.mark.parametrize("name", ICON_NAMES)
    def test_icon_file_exists_and_loads(self, name):
        assert icons.icon_path(name).exists(), f"缺图标文件:{name}.svg"
        assert not plain_icon(name).isNull(), f"SVG 无法加载:{name}"

    def test_app_and_tray_icons(self):
        assert not app_icon().isNull()
        assert not tray_icon("dark").isNull()
        assert not tray_icon("light").isNull()

    def test_total_asset_size_budget(self):
        """轻量红线:全部资产合计 < 500KB(docs/UI_ASSETS.md C 约束)。"""
        total = sum(
            p.stat().st_size
            for p in icons.ASSETS_DIR.rglob("*")
            if p.is_file()
        )
        assert total < 500 * 1024

    def test_tinted_icon_cached_and_clearable(self):
        first = tinted_icon("play", "primary")
        assert not first.isNull()
        assert tinted_icon("play", "primary") is first  # 缓存命中
        icons.clear_cache()
        assert tinted_icon("play", "primary") is not first

    def test_tint_follows_theme(self, qapp):
        from neriplayer_win.ui.theme import ThemeManager

        def image_bytes() -> bytes:
            data = tinted_icon("settings", "primary").pixmap(64, 64).toImage()
            return bytes(memoryview(data.constBits()))

        manager = ThemeManager()
        manager.apply(theme.THEME_DARK)
        icons.clear_cache()
        dark = image_bytes()
        manager.apply(theme.THEME_LIGHT)
        icons.clear_cache()
        light = image_bytes()
        assert dark != light


class TestPlayerBarIcons:
    def test_control_buttons_carry_icons(self, qapp):
        bar = PlayerBar()
        for button in (
            bar.prev_button,
            bar.play_button,
            bar.next_button,
            bar.mode_button,
            bar.queue_button,
        ):
            assert not button.icon().isNull()

    def test_playing_state_swaps_icon_and_tooltip(self, qapp):
        bar = PlayerBar()
        assert bar.play_button.toolTip() == "播放"
        bar.set_playing(True)
        assert not bar.play_button.icon().isNull()
        assert bar.play_button.toolTip() == "暂停"
        bar.set_playing(False)
        assert bar.play_button.toolTip() == "播放"

    def test_mode_updates_icon_and_tooltip(self, qapp):
        bar = PlayerBar()
        bar.set_mode("shuffle", "随机播放")
        assert not bar.mode_button.icon().isNull()
        assert bar.mode_button.toolTip() == "播放模式:随机播放"
        assert bar.mode_button.accessibleName() == "播放模式:随机播放"

    def test_retheme_keeps_state(self, qapp):
        bar = PlayerBar()
        bar.set_playing(True)
        bar.set_mode("repeat_one", "单曲循环")
        bar.retheme()
        assert not bar.play_button.icon().isNull()
        assert bar.play_button.toolTip() == "暂停"
        assert bar.mode_button.toolTip() == "播放模式:单曲循环"


class TestSettingsPageAppearance:
    def test_signal_and_backfill(self, qapp):
        page = SettingsPage()
        fired: list[str] = []
        page.appearance_changed.connect(fired.append)
        page.set_appearance("light")  # 程序化回填不发信号,但选中项已变
        assert fired == []
        combo = page.appearance_combo
        dark_index = next(
            i for i in range(combo.count()) if combo.itemData(i) == "dark"
        )
        combo.setCurrentIndex(dark_index)  # 模拟用户选择(与当前 light 不同)
        assert fired == ["dark"]


def _settings_page_text(page: SettingsPage) -> str:
    from PySide6.QtWidgets import QLabel

    return " ".join(label.text() for label in page.findChildren(QLabel))


class TestSettingsPageAbout:
    def test_about_content(self, qapp):
        from neriplayer_win import __version__

        page = SettingsPage()
        text = _settings_page_text(page)
        assert __version__ in text
        assert "GPL-3.0" in text
        assert "NeriPlayer" in text
        assert "https://github.com/cwuom/NeriPlayer" in text
        assert page.about_link.openExternalLinks()


class TestMainWindowThemeSwitch:
    def test_startup_with_saved_light_theme(self, qapp, tmp_path, monkeypatch):
        """存了亮色主题时冷启动:__init__ 内 apply 即触发全量刷新路径,不应崩。"""
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        (tmp_path / "settings.json").write_text(
            json.dumps({"close_action": "exit", "appearance": "light"}),
            encoding="utf-8",
        )
        window = MainWindow()
        try:
            assert window.themes.name == "light"
            assert theme.PALETTES["light"]["surface"] in qapp.styleSheet()
            assert not window.player_bar.play_button.icon().isNull()
            assert not window._empty_state.icon_label.pixmap().isNull()
        finally:
            window.close()

    def test_construct_and_dark_to_light(self, qapp, tmp_path, monkeypatch):
        """主窗口构造 + 暗→亮切换不崩,QSS/图标/空态全部就位。"""
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        # 预置 close_action=exit,使 window.close() 走真实退出清理路径
        (tmp_path / "settings.json").write_text(
            json.dumps({"close_action": "exit"}), encoding="utf-8"
        )
        window = MainWindow()
        try:
            assert window.themes.name == "dark"
            assert not window.windowIcon().isNull()
            # 未登录:空态视图就位
            assert window.table_stack.currentIndex() == 1
            assert window._empty_state.hint_label.text() != ""
            # 暗色 QSS
            assert theme.PALETTES["dark"]["surface"] in qapp.styleSheet()
            # 切亮色(全量刷新路径)
            window.themes.apply("light")
            assert window.themes.name == "light"
            assert theme.PALETTES["light"]["surface"] in qapp.styleSheet()
            assert not window.player_bar.play_button.icon().isNull()
            assert not window._empty_state.icon_label.pixmap().isNull()
            # 设置页信号路径持久化
            window._on_settings_appearance("light")
            saved = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert saved["appearance"] == "light"
        finally:
            window.themes.apply("dark")
            window.close()
