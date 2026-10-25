"""侧栏分区(顶层大类)拖动换序的测试(M8)。

覆盖:
- store 侧的 section_order 校验与重排(纯函数)
- 分区头「⋮」把手命中区
- 拖动换序后的信号、落库、重建后顺序保持
- 搜索/设置固定位置不被拖动影响
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt

from neriplayer_win.data.store import (
    SECTION_ORDER_KEYS,
    SETTING_SECTION_ORDER,
    apply_section_order,
    validate_section_order,
)
from neriplayer_win.ui.main_window import MainWindow

_ROLE = Qt.ItemDataRole.UserRole


def _make_window(qapp, monkeypatch, tmp_path) -> MainWindow:
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window


def _close(window) -> None:
    window._force_exit = True
    window.close()


def _top_names(window) -> list[str]:
    tree = window.sidebar
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


class TestSectionOrderStore:
    def test_validate_keeps_only_whitelisted_keys(self) -> None:
        result = validate_section_order(["bili", "bogus", "netease", 42, None])
        assert result == ["bili", "netease"]

    def test_validate_deduplicates_preserving_order(self) -> None:
        assert validate_section_order(["bili", "netease", "bili"]) == [
            "bili", "netease",
        ]

    def test_validate_rejects_non_list(self) -> None:
        assert validate_section_order("bili") == []
        assert validate_section_order(None) == []
        assert validate_section_order({"bili": 1}) == []

    def test_apply_puts_new_sections_at_tail(self) -> None:
        """旧配置里没有的新分区(如后来加的 kugou)追加到尾部,不能丢。"""
        current = ["netease", "bili", "kugou", "local"]
        stored = ["bili", "netease"]
        assert apply_section_order(current, stored) == [
            "bili", "netease", "kugou", "local",
        ]

    def test_apply_ignores_stale_keys(self) -> None:
        """存储里已不存在的分区键要静默跳过。"""
        assert apply_section_order(["netease", "bili"], ["gone", "bili", "netease"]) == [
            "bili", "netease",
        ]

    def test_empty_stored_keeps_default_order(self) -> None:
        current = ["netease", "bili", "kugou"]
        assert apply_section_order(current, []) == current

    def test_settings_roundtrip(self, tmp_path) -> None:
        from neriplayer_win.data.store import LocalStore

        store = LocalStore(tmp_path)
        store.save_settings({SETTING_SECTION_ORDER: ["local", "bili"]})
        loaded = store.load_settings()
        assert loaded[SETTING_SECTION_ORDER] == ["local", "bili"]

    def test_settings_sanitize_on_save(self, tmp_path) -> None:
        from neriplayer_win.data.store import LocalStore

        store = LocalStore(tmp_path)
        store.save_settings({SETTING_SECTION_ORDER: ["bad", "bili", "bili"]})
        assert store.load_settings()[SETTING_SECTION_ORDER] == ["bili"]

    def test_search_and_settings_not_sortable(self) -> None:
        """「搜索」「设置」是固定位置,不能出现在可排序白名单里。"""
        assert "search" not in SECTION_ORDER_KEYS
        assert "settings" not in SECTION_ORDER_KEYS


class TestSectionReorderUi:
    def test_all_sections_have_grip(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            tree = window.sidebar
            headers = tree._sortable_headers()
            assert headers, "至少应有网易云/B站/酷狗/本地四个分区"
            for section, header in headers:
                assert tree.header_grip_rect(header) is not None, section
        finally:
            _close(window)

    def test_search_and_settings_have_no_grip(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            tree = window.sidebar
            for row in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(row)
                kind = tree.item_kind(item)
                if kind in ("search", "settings"):
                    assert tree.header_grip_rect(item) is None
        finally:
            _close(window)

    def test_grip_hit_maps_to_section(self, qapp, monkeypatch, tmp_path) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            tree = window.sidebar
            for section, header in tree._sortable_headers():
                rect = tree.header_grip_rect(header)
                assert rect is not None
                assert tree._grip_hit_section(rect.center()) == section
        finally:
            _close(window)

    def test_reorder_persists_and_survives_rebuild(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            tree = window.sidebar
            before = _top_names(window)
            assert before[0] == "搜索"
            assert before[-1] == "设置"

            # 把 B站 拖到网易云之前(搜索之后)
            bili = tree.find_header("bili")
            assert bili is not None
            old_index = tree.indexOfTopLevelItem(bili)
            tree.takeTopLevelItem(old_index)
            tree.insertTopLevelItem(1, bili)
            tree.section_order_changed.emit(
                [section for section, _item in tree._sortable_headers()]
            )

            stored = window._settings.get(SETTING_SECTION_ORDER)
            assert stored is not None
            assert stored[0] == "bili"
            assert stored[1] == "netease"

            window._rebuild_sidebar()
            after = _top_names(window)
            assert after[0] == "搜索", "搜索必须仍置顶"
            assert after[-1] == "设置", "设置必须仍置底"
            assert after[1] == "B站 · 收藏夹"
            assert after[2] == "网易云 · 歌单"
        finally:
            _close(window)

    def test_reorder_written_to_settings_file(
        self, qapp, monkeypatch, tmp_path
    ) -> None:
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._on_section_order_changed(["local", "bili", "netease", "kugou"])
            data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
            assert data["section_order"] == ["local", "bili", "netease", "kugou"]
        finally:
            _close(window)

    def test_invalid_signal_payload_ignored(self, qapp, monkeypatch, tmp_path) -> None:
        """信号参数异常时不能崩,也不能写坏配置。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            before = window._settings.get(SETTING_SECTION_ORDER)
            window._on_section_order_changed("not-a-list")
            assert window._settings.get(SETTING_SECTION_ORDER) == before
        finally:
            _close(window)

    def test_stored_order_applied_on_boot(self, qapp, monkeypatch, tmp_path) -> None:
        """落盘的分区顺序要在下次启动时生效。"""
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            window._on_section_order_changed(["local", "kugou", "netease", "bili"])
            window._rebuild_sidebar()
            names = _top_names(window)
            assert names[1] == "本地歌单"
            assert names[2] == "酷狗 · 歌单"
        finally:
            _close(window)
