"""i18n(中英双语)回归:文案表 / 语言状态与监听 / settings 持久化 /
设置页滑块 / MainWindow 全量重翻译 / 「最近」搜索条目的双语展示。

约定:语言是模块级全局,凡切到 en 的用例必须在结束时切回 zh
(restore_zh fixture 兜底),避免污染同进程的其他 UI 测试。
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from neriplayer_win import i18n
from neriplayer_win.data.store import SETTING_LANGUAGE, default_settings
from neriplayer_win.player.queue import PlayMode
from neriplayer_win.ui.main_window import _ListRef, MainWindow
from neriplayer_win.ui.player_bar import PlayerBar
from neriplayer_win.ui.settings_page import LanguageSwitch, SettingsPage


@pytest.fixture(autouse=True)
def restore_zh():
    """用例结束后恢复默认中文(全局状态,不恢复会污染后续测试)。"""
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE, notify=False)


def _make_window(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window


def _close(window) -> None:
    window._force_exit = True
    window.close()


def _sidebar_texts(window) -> list[str]:
    from PySide6.QtWidgets import QTreeWidget

    tree = window.sidebar
    assert isinstance(tree, QTreeWidget)
    return [tree.topLevelItem(row).text(0) for row in range(tree.topLevelItemCount())]


# -- i18n 模块 ---------------------------------------------------------------


class TestI18nModule:
    def test_default_language_is_zh(self):
        assert i18n.current_language() == "zh"
        assert i18n.tr("sidebar.settings") == "设置"

    def test_set_language_switches_tr(self):
        assert i18n.set_language("en") is True
        assert i18n.tr("sidebar.settings") == "Settings"
        assert i18n.set_language("zh") is True
        assert i18n.tr("sidebar.settings") == "设置"

    def test_invalid_language_rejected(self):
        assert i18n.set_language("fr") is False
        assert i18n.current_language() == "zh"

    def test_named_format_kwargs(self):
        i18n.set_language("en")
        assert i18n.tr("list.loaded", title="Favs", count=3) == "Favs · 3 songs"
        i18n.set_language("zh")
        assert i18n.tr("list.loaded", title="收藏", count=3) == "收藏 · 共 3 首"

    def test_missing_key_falls_back_to_key(self):
        assert i18n.tr("no.such.key") == "no.such.key"
        assert i18n.tr("no.such.key", x=1) == "no.such.key"

    def test_listener_notified_on_change_only(self):
        calls: list[str] = []
        i18n.add_listener(calls.append)
        try:
            i18n.set_language("en", notify=True)
            i18n.set_language("en", notify=True)  # 值未变:不通知
            assert calls == ["en"]
            i18n.set_language("zh")
            assert calls == ["en", "zh"]
        finally:
            i18n.remove_listener(calls.append)
        i18n.set_language("en")
        assert calls == ["en", "zh"]  # 已注销:不再通知

    def test_every_key_has_both_languages(self):
        missing = [
            key for key, (zh, en) in i18n._STRINGS.items() if not zh or not en
        ]
        assert missing == []


# -- 持久化 ------------------------------------------------------------------


class TestLanguagePersistence:
    def test_default_settings_contains_zh(self):
        assert default_settings()[SETTING_LANGUAGE] == "zh"

    def test_language_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        from neriplayer_win.data.store import LocalStore

        store = LocalStore()
        store.save_settings({SETTING_LANGUAGE: "en"})
        assert store.load_settings()[SETTING_LANGUAGE] == "en"

    def test_invalid_language_falls_back_to_zh(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        from neriplayer_win.data.store import LocalStore

        store = LocalStore()
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text(
            json.dumps({SETTING_LANGUAGE: "jp"}), encoding="utf-8"
        )
        assert store.load_settings()[SETTING_LANGUAGE] == "zh"


# -- 设置页滑块 ---------------------------------------------------------------


class TestLanguageSwitch:
    def test_initial_value_zh(self, qapp):
        switch = LanguageSwitch()
        assert switch.value() == "zh"

    def test_programmatic_set_does_not_emit(self, qapp):
        switch = LanguageSwitch()
        emitted: list[str] = []
        switch.language_changed.connect(emitted.append)
        switch.set_value("en", animate=False)
        assert switch.value() == "en"
        assert emitted == []

    def test_click_right_half_selects_english(self, qapp):
        switch = LanguageSwitch()
        emitted: list[str] = []
        switch.language_changed.connect(emitted.append)
        QTest.mouseClick(
            switch, Qt.MouseButton.LeftButton, pos=QPoint(switch.width() - 10, 15)
        )
        assert emitted == ["en"]
        assert switch.value() == "en"

    def test_click_left_half_back_to_chinese(self, qapp):
        switch = LanguageSwitch()
        switch.set_value("en", animate=False)
        emitted: list[str] = []
        switch.language_changed.connect(emitted.append)
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(10, 15))
        assert emitted == ["zh"]
        assert switch.value() == "zh"


class TestSettingsPageRetranslate:
    def test_retranslate_updates_groups_and_backfill(self, qapp):
        page = SettingsPage()
        assert page.close_group.title() == "关闭行为"
        assert page.mode_combo.itemText(0) == "顺序播放"
        i18n.set_language("en", notify=False)
        page.retranslate()
        assert page.close_group.title() == "On Close"
        assert page.mode_combo.itemText(0) == "Sequential"
        assert page.about_name.text().startswith("<b>NeriPlayer Win</b> Version")
        # 语言分组标题双语并列:两种语言下都一样
        assert page.language_group.title() == "语言 · Language"
        # 回填:滑块跟随但不发信号
        page.set_language("zh")
        assert page.language_switch.value() == "zh"

    def test_switch_signal_propagates_to_page(self, qapp):
        page = SettingsPage()
        emitted: list[str] = []
        page.language_changed.connect(emitted.append)
        QTest.mouseClick(
            page.language_switch,
            Qt.MouseButton.LeftButton,
            pos=QPoint(page.language_switch.width() - 10, 15),
        )
        assert emitted == ["en"]

    def test_settings_page_does_not_force_window_height(self, qapp, monkeypatch, tmp_path):
        """语言分组加入后设置页 minHint 约 700px,曾把主窗口最小高度顶到
        800+(默认 680 被撑高);内容包 QScrollArea 后与窗口高度解耦。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window.settings_page.minimumSizeHint().height() <= 120
            assert window.minimumSizeHint().height() <= 680
            assert (window.width(), window.height()) == (1080, 680)
        finally:
            _close(window)


# -- MainWindow 全量重翻译 -------------------------------------------------------


class TestMainWindowLanguageSwitch:
    def test_startup_defaults_to_chinese(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert "设置" in _sidebar_texts(window)
            assert window._search_input.placeholderText() == "搜索关键词"
            assert window.statusBar().currentMessage() == "就绪"
        finally:
            _close(window)

    def test_startup_uses_saved_english(self, qapp, monkeypatch, tmp_path):
        (tmp_path / "settings.json").write_text(
            json.dumps({SETTING_LANGUAGE: "en"}), encoding="utf-8"
        )
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert "Settings" in _sidebar_texts(window)
            assert window._search_input.placeholderText() == "Search keywords"
            assert window.statusBar().currentMessage() == "Ready"
        finally:
            _close(window)

    def test_toggle_switch_retranslates_and_persists(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            # 模拟用户把滑块拨到右侧(English):走完整信号链(持久化+重翻译)
            QTest.mouseClick(
                window.settings_page.language_switch,
                Qt.MouseButton.LeftButton,
                pos=QPoint(window.settings_page.language_switch.width() - 10, 15),
            )
            qapp.processEvents()
            assert i18n.current_language() == "en"
            texts = _sidebar_texts(window)
            assert "Settings" in texts and "Search" in texts
            assert "设置" not in texts
            assert window._search_input.placeholderText() == "Search keywords"
            assert window._search_netease_btn.text() == "NetEase"
            assert window.statusBar().currentMessage() == "Ready"
            # 播放条:占位文案与 tooltip 跟随
            assert window.player_bar.track_label._full_text == "Not playing"
            assert window.player_bar.prev_button.toolTip() == "Previous"
            # 表头跟随(空表也重设表头标签)
            labels = [
                window.song_table.horizontalHeaderItem(col).text()
                for col in range(window.song_table.columnCount())
            ]
            assert labels == ["#", "", "Title", "Artist", "Duration"]
            # 持久化:settings.json 落了 language=en
            saved = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert saved[SETTING_LANGUAGE] == "en"
            # 拨回左侧(中文):全部回到中文
            QTest.mouseClick(
                window.settings_page.language_switch,
                Qt.MouseButton.LeftButton,
                pos=QPoint(10, 15),
            )
            qapp.processEvents()
            assert i18n.current_language() == "zh"
            assert "设置" in _sidebar_texts(window)
            saved = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert saved[SETTING_LANGUAGE] == "zh"
        finally:
            _close(window)

    def test_header_source_switches_table_header(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._set_table_songs([], "bili", None)
            labels = [
                window.song_table.horizontalHeaderItem(col).text()
                for col in range(window.song_table.columnCount())
            ]
            assert labels == ["#", "", "标题", "UP主", "时长"]
            i18n.set_language("en")  # 直接走监听器(全量重翻译入口)
            qapp.processEvents()
            assert window._table_header_source == "bili"
            labels = [
                window.song_table.horizontalHeaderItem(col).text()
                for col in range(window.song_table.columnCount())
            ]
            assert labels == ["#", "", "Title", "Uploader", "Duration"]
        finally:
            _close(window)

    def test_recent_search_entry_display_translated(self, qapp, monkeypatch, tmp_path):
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            # 存量数据:规范前缀「搜索:」是持久化格式,不随语言变
            window._recent = [_ListRef("search", 0, "搜索:周")]
            window._rebuild_sidebar()
            tree = window.sidebar
            header = tree.find_header("recent")
            assert header is not None
            assert header.child(0).text(0) == "搜索:周"
            i18n.set_language("en")
            qapp.processEvents()
            # 侧栏整树重建过,重新查分区头再断言
            header = tree.find_header("recent")
            assert header is not None
            assert header.child(0).text(0) == "Search: 周"
            # 回放:剥规范前缀还原关键词(语言无关)
            window._open_list_ref(_ListRef("search", 0, "搜索:周"))
            assert window._search_input.text() == "周"
        finally:
            _close(window)


# -- 其他组件 ---------------------------------------------------------------


class TestComponentRetranslate:
    def test_play_mode_display_name(self):
        assert PlayMode.SHUFFLE.display_name == "随机播放"
        i18n.set_language("en")
        assert PlayMode.SHUFFLE.display_name == "Shuffle"
        assert PlayMode.REPEAT_ONE.button_label == "One"

    def test_player_bar_retranslate(self, qapp):
        bar = PlayerBar()
        assert bar.track_label._full_text == "未在播放"
        bar.set_mode("shuffle")
        assert bar.mode_button.toolTip() == "播放模式:随机播放"
        i18n.set_language("en")
        bar.retranslate()
        assert bar.track_label._full_text == "Not playing"
        assert bar.mode_button.toolTip() == "Play mode: Shuffle"
        assert bar.volume_slider.toolTip() == "Volume"

    def test_player_bar_keeps_track_after_retranslate(self, qapp):
        bar = PlayerBar()
        bar.set_track("晴天", "周杰伦")
        i18n.set_language("en")
        bar.retranslate()
        assert bar.track_label._full_text == "晴天"  # 歌曲信息不被占位文案覆盖

    def test_weblogin_config_follows_language(self):
        from neriplayer_win.ui.browser_login import bili_web_login, netease_web_login

        assert netease_web_login().title == "网易云音乐 · 网页登录"
        i18n.set_language("en")
        assert netease_web_login().title == "NetEase Cloud Music · Web Login"
        assert bili_web_login().title == "Bilibili · Web Login"
