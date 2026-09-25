"""动态取色(M5)回归:M3 算法移植 / 封面种子提取 / ThemeManager 动态色板 /
MainWindow 接线与持久化。

_REFERENCE_PALETTES 为烘干的参考值:由 materialyoucolor 3.x(上游
material-color-utilities 的 Python 移植)的 SchemeTonalSpot(SPEC_2021,
contrast 0)生成,与本项目 ui/material_color.py 的移植对拍 500/500 一致
(10 种子 × 亮暗 × 25 角色);固化进测试后不再依赖外部包。
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from neriplayer_win.data.store import (
    DEFAULT_DYNAMIC_COLOR,
    SETTING_DYNAMIC_COLOR,
    default_settings,
)
from neriplayer_win.ui import cover_seed, theme
from neriplayer_win.ui.main_window import MainWindow, QueueSong
from neriplayer_win.ui.material_color import (
    argb_from_hct,
    hct_from_int,
    seed_from_hex,
    tonal_spot_palette,
)

_REFERENCE_PALETTES: dict[str, dict[str, str]] = {
    "DEFAULT_BLUE_light": {"primary": "#36618E", "onPrimary": "#FFFFFF", "primaryContainer": "#D1E4FF", "onPrimaryContainer": "#1A4975", "secondary": "#535F70", "secondaryContainer": "#D7E3F8", "onSecondaryContainer": "#3B4858", "tertiary": "#6B5778", "tertiaryContainer": "#F3DAFF", "onTertiaryContainer": "#523F5F", "error": "#BA1A1A", "errorContainer": "#FFDAD6", "background": "#F8F9FF", "onBackground": "#191C20", "surface": "#F8F9FF", "onSurface": "#191C20", "surfaceVariant": "#DFE2EB", "onSurfaceVariant": "#43474E", "outline": "#73777F", "outlineVariant": "#C3C6CF", "surfaceLowest": "#FFFFFF", "surfaceLow": "#F2F3FA", "surfaceContainer": "#ECEEF4", "surfaceHigh": "#E6E8EE", "surfaceHighest": "#E1E2E8"},
    "DEFAULT_BLUE_dark": {"primary": "#A0CAFD", "onPrimary": "#003258", "primaryContainer": "#1A4975", "onPrimaryContainer": "#D1E4FF", "secondary": "#BBC7DB", "secondaryContainer": "#3B4858", "onSecondaryContainer": "#D7E3F8", "tertiary": "#D7BEE4", "tertiaryContainer": "#523F5F", "onTertiaryContainer": "#F3DAFF", "error": "#FFB4AB", "errorContainer": "#93000A", "background": "#111418", "onBackground": "#E1E2E8", "surface": "#111418", "onSurface": "#E1E2E8", "surfaceVariant": "#43474E", "onSurfaceVariant": "#C3C6CF", "outline": "#8D9199", "outlineVariant": "#43474E", "surfaceLowest": "#0B0E13", "surfaceLow": "#191C20", "surfaceContainer": "#1D2024", "surfaceHigh": "#272A2F", "surfaceHighest": "#32353A"},
    "MAGENTA_light": {"primary": "#844C73", "onPrimary": "#FFFFFF", "primaryContainer": "#FFD7EF", "onPrimaryContainer": "#69345B", "secondary": "#705766", "secondaryContainer": "#FADAEC", "onSecondaryContainer": "#56404E", "tertiary": "#815340", "tertiaryContainer": "#FFDBCD", "onTertiaryContainer": "#663C2A", "error": "#BA1A1A", "errorContainer": "#FFDAD6", "background": "#FFF8F9", "onBackground": "#201A1E", "surface": "#FFF8F9", "onSurface": "#201A1E", "surfaceVariant": "#EFDEE6", "onSurfaceVariant": "#4F444A", "outline": "#81737A", "outlineVariant": "#D2C2CA", "surfaceLowest": "#FFFFFF", "surfaceLow": "#FEF0F6", "surfaceContainer": "#F8EAF0", "surfaceHigh": "#F2E5EA", "surfaceHighest": "#EDDFE4"},
    "MAGENTA_dark": {"primary": "#F7B2DF", "onPrimary": "#4F1E43", "primaryContainer": "#69345B", "onPrimaryContainer": "#FFD7EF", "secondary": "#DDBECF", "secondaryContainer": "#56404E", "onSecondaryContainer": "#FADAEC", "tertiary": "#F5B9A1", "tertiaryContainer": "#663C2A", "onTertiaryContainer": "#FFDBCD", "error": "#FFB4AB", "errorContainer": "#93000A", "background": "#181215", "onBackground": "#EDDFE4", "surface": "#181215", "onSurface": "#EDDFE4", "surfaceVariant": "#4F444A", "onSurfaceVariant": "#D2C2CA", "outline": "#9B8D94", "outlineVariant": "#4F444A", "surfaceLowest": "#120C10", "surfaceLow": "#201A1E", "surfaceContainer": "#251E22", "surfaceHigh": "#2F282C", "surfaceHighest": "#3A3337"},
    "GREEN_light": {"primary": "#3B6939", "onPrimary": "#FFFFFF", "primaryContainer": "#BCF0B4", "onPrimaryContainer": "#245024", "secondary": "#52634F", "secondaryContainer": "#D5E8CE", "onSecondaryContainer": "#3B4B38", "tertiary": "#38656A", "tertiaryContainer": "#BCEBF0", "onTertiaryContainer": "#1F4D52", "error": "#BA1A1A", "errorContainer": "#FFDAD6", "background": "#F7FBF1", "onBackground": "#191D17", "surface": "#F7FBF1", "onSurface": "#191D17", "surfaceVariant": "#DEE5D8", "onSurfaceVariant": "#424940", "outline": "#72796F", "outlineVariant": "#C2C9BD", "surfaceLowest": "#FFFFFF", "surfaceLow": "#F1F5EB", "surfaceContainer": "#ECEFE6", "surfaceHigh": "#E6E9E0", "surfaceHighest": "#E0E4DB"},
    "GREEN_dark": {"primary": "#A1D39A", "onPrimary": "#0A390F", "primaryContainer": "#245024", "onPrimaryContainer": "#BCF0B4", "secondary": "#BACCB3", "secondaryContainer": "#3B4B38", "onSecondaryContainer": "#D5E8CE", "tertiary": "#A0CFD4", "tertiaryContainer": "#1F4D52", "onTertiaryContainer": "#BCEBF0", "error": "#FFB4AB", "errorContainer": "#93000A", "background": "#10140F", "onBackground": "#E0E4DB", "surface": "#10140F", "onSurface": "#E0E4DB", "surfaceVariant": "#424940", "onSurfaceVariant": "#C2C9BD", "outline": "#8C9388", "outlineVariant": "#424940", "surfaceLowest": "#0B0F0A", "surfaceLow": "#191D17", "surfaceContainer": "#1D211B", "surfaceHigh": "#272B25", "surfaceHighest": "#323630"},
}


# ---------------------------------------------------------------------------
# material_color.py:算法移植对照烘干参考值
# ---------------------------------------------------------------------------


class TestMaterialColor:
    @pytest.mark.parametrize("key", sorted(_REFERENCE_PALETTES))
    def test_tonal_spot_matches_reference(self, key):
        seed = {
            "DEFAULT_BLUE": 0xFF0061A4,
            "MAGENTA": 0xFFC425A8,
            "GREEN": 0xFF388E3C,
        }[key.rsplit("_", 1)[0]]
        is_dark = key.endswith("dark")
        assert tonal_spot_palette(seed, is_dark) == _REFERENCE_PALETTES[key]

    def test_hct_known_values(self):
        # CAM16 正向:#0061A4 → hue 254.04 / chroma 48.34 / tone 39.96
        hue, chroma, tone = hct_from_int(0xFF0061A4)
        assert round(hue, 2) == 254.04
        assert round(chroma, 2) == 48.34
        assert round(tone, 2) == 39.96

    def test_solver_tone_varies(self):
        hue, chroma, _tone = hct_from_int(0xFF0061A4)
        t30 = argb_from_hct(hue, chroma, 30)
        t80 = argb_from_hct(hue, chroma, 80)
        assert t30 != t80
        # 高 tone 的亮度(Y)必须大于低 tone
        gray30 = argb_from_hct(hue, 0.0, 30) & 0xFFFFFF
        gray80 = argb_from_hct(hue, 0.0, 80) & 0xFFFFFF
        assert gray80 > gray30

    def test_seed_from_hex_variants(self):
        assert seed_from_hex("#AB12CD") == 0xFFAB12CD
        assert seed_from_hex("ab12cd") == 0xFFAB12CD
        assert seed_from_hex("bad value") == 0xFF0061A4  # 非法回落默认种子


# ---------------------------------------------------------------------------
# cover_seed.py:提取与缓存
# ---------------------------------------------------------------------------


def _image_bytes(color: str, size: int = 120) -> bytes:
    """纯色 PNG 字节。"""
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(color)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.ReadWrite)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class TestCoverSeed:
    def setup_method(self):
        cover_seed.clear_cache()

    def test_solid_red_picks_vibrant_red(self):
        hex_color = cover_seed.extract_seed_hex(_image_bytes("#D32F2F"))
        assert hex_color == "#D32F2F"

    def test_solid_gray_falls_to_muted_or_dominant(self):
        # 灰图无 vibrant:走 muted/dominant,结果应为同一灰色
        assert cover_seed.extract_seed_hex(_image_bytes("#9E9E9E")) == "#9E9E9E"

    def test_bad_data_returns_none(self):
        assert cover_seed.extract_seed_hex(b"not an image") is None

    def test_cache_roundtrip_and_fifo(self):
        cover_seed.cache_put("u1", "#112233")
        assert cover_seed.cache_get("u1") == "#112233"
        assert cover_seed.cache_get("missing") is None
        for i in range(cover_seed._CACHE_MAX + 8):
            cover_seed.cache_put(f"u{i}", "#000000")
        assert cover_seed.cache_get("u1") is None  # 超限被 FIFO 淘汰


# ---------------------------------------------------------------------------
# ThemeManager:动态色板应用与回退
# ---------------------------------------------------------------------------


class TestThemeManagerDynamic:
    def test_apply_dynamic_overrides_palette(self, qapp):
        manager = theme.ThemeManager()
        manager.apply("dark")
        emitted: list[str] = []
        manager.theme_changed.connect(emitted.append)
        manager.apply_dynamic(0xFFC425A8)
        assert emitted == ["dark"]  # 模式名不变,但确实广播了
        assert theme.current_palette() == tonal_spot_palette(0xFFC425A8, True)
        assert theme.current_theme_name() == "dark"
        # 回静态:冻结色板恢复
        manager.apply("dark")
        assert theme.current_palette() == theme.PALETTES["dark"]
        assert len(emitted) == 2

    def test_apply_dynamic_respects_light_mode(self, qapp):
        manager = theme.ThemeManager()
        manager.apply("light")
        manager.apply_dynamic(0xFFC425A8)
        assert theme.current_palette() == tonal_spot_palette(0xFFC425A8, False)

    def test_dynamic_palette_keys_cover_qss_roles(self, qapp):
        # 动态色板必须覆盖冻结色板的全部角色(QSS @role 记号全集)
        manager = theme.ThemeManager()
        manager.apply("dark")
        manager.apply_dynamic(0xFF388E3C)
        assert set(theme.current_palette()) == set(theme.PALETTES["dark"])


# ---------------------------------------------------------------------------
# store:dynamic_color 设置
# ---------------------------------------------------------------------------


class TestDynamicColorSetting:
    def test_default_on(self):
        assert default_settings()[SETTING_DYNAMIC_COLOR] is True

    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        from neriplayer_win.data.store import LocalStore

        store = LocalStore()
        store.save_settings({SETTING_DYNAMIC_COLOR: False})
        assert store.load_settings()[SETTING_DYNAMIC_COLOR] is False

    def test_invalid_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
        from neriplayer_win.data.store import LocalStore

        store = LocalStore()
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text(
            json.dumps({SETTING_DYNAMIC_COLOR: "yes"}), encoding="utf-8"
        )
        assert store.load_settings()[SETTING_DYNAMIC_COLOR] is DEFAULT_DYNAMIC_COLOR


# ---------------------------------------------------------------------------
# MainWindow:接线
# ---------------------------------------------------------------------------


def _make_window(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    return window


def _close(window) -> None:
    window._force_exit = True
    window.close()


def _reset_static_theme() -> None:
    theme.ThemeManager().apply("dark")


def _song(url: str = "http://img/x.jpg") -> QueueSong:
    return QueueSong(
        source="netease", id=1, bvid="", title="t", artist="a",
        duration_ms=60000, cover_url=url,
    )


class TestMainWindowDynamicWiring:
    def test_cached_seed_applies_dynamic_theme(self, qapp, monkeypatch, tmp_path):
        cover_seed.clear_cache()
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            frozen_primary = theme.current_palette()["primary"]
            cover_seed.cache_put("http://img/x.jpg", "#C425A8")
            window._request_dynamic_seed(_song())
            qapp.processEvents()
            assert window._dynamic_seed == 0xFFC425A8
            assert theme.current_palette()["primary"] != frozen_primary
            assert theme.current_palette() == tonal_spot_palette(0xFFC425A8, True)
        finally:
            _close(window)
            _reset_static_theme()

    def test_same_seed_not_reapplied(self, qapp, monkeypatch, tmp_path):
        cover_seed.clear_cache()
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            cover_seed.cache_put("http://img/x.jpg", "#C425A8")
            window._request_dynamic_seed(_song())
            qapp.processEvents()
            calls: list[int] = []
            original = window.themes.apply_dynamic

            def spy(seed: int) -> None:
                calls.append(seed)
                original(seed)

            window.themes.apply_dynamic = spy  # type: ignore[method-assign]
            window._request_dynamic_seed(_song())  # 同 URL 缓存命中、同种子
            assert calls == []  # 去重:同种子不再重渲
        finally:
            _close(window)
            _reset_static_theme()

    def test_song_without_cover_reverts_static(self, qapp, monkeypatch, tmp_path):
        cover_seed.clear_cache()
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            cover_seed.cache_put("http://img/x.jpg", "#C425A8")
            window._request_dynamic_seed(_song())
            qapp.processEvents()
            assert window._dynamic_seed is not None
            window._request_dynamic_seed(_song(url=""))  # 无封面歌
            qapp.processEvents()
            assert window._dynamic_seed is None
            assert theme.current_palette() == theme.PALETTES["dark"]
        finally:
            _close(window)
            _reset_static_theme()

    def test_toggle_off_reverts_and_persists(self, qapp, monkeypatch, tmp_path):
        cover_seed.clear_cache()
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            assert window.settings_page.dynamic_color_check.isChecked() is True
            cover_seed.cache_put("http://img/x.jpg", "#C425A8")
            window._request_dynamic_seed(_song())
            qapp.processEvents()
            assert theme.current_palette() != theme.PALETTES["dark"]
            # 关闭开关:回静态 + 持久化
            window.settings_page.dynamic_color_check.setChecked(False)
            qapp.processEvents()
            assert theme.current_palette() == theme.PALETTES["dark"]
            saved = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8")
            )
            assert saved[SETTING_DYNAMIC_COLOR] is False
            # 重新点歌:开关关着,不应用动态(但记住种子)
            window._request_dynamic_seed(_song())
            qapp.processEvents()
            assert window._dynamic_seed == 0xFFC425A8
            assert theme.current_palette() == theme.PALETTES["dark"]
            # 重新打开开关:立即按记住的种子恢复动态主题
            window.settings_page.dynamic_color_check.setChecked(True)
            qapp.processEvents()
            assert theme.current_palette() == tonal_spot_palette(0xFFC425A8, True)
        finally:
            _close(window)
            _reset_static_theme()

    def test_appearance_switch_recomputes_dynamic(self, qapp, monkeypatch, tmp_path):
        cover_seed.clear_cache()
        window = _make_window(qapp, monkeypatch, tmp_path)
        try:
            cover_seed.cache_put("http://img/x.jpg", "#C425A8")
            window._request_dynamic_seed(_song())
            qapp.processEvents()
            dark_palette = theme.current_palette()
            # 切亮色:动态色板应在亮色下重算
            window.settings_page.appearance_combo.setCurrentIndex(1)  # 亮色
            qapp.processEvents()
            assert theme.current_theme_name() == "light"
            assert theme.current_palette() == tonal_spot_palette(0xFFC425A8, False)
            assert theme.current_palette() != dark_palette
        finally:
            _close(window)
            manager = theme.ThemeManager()
            manager.apply("dark")
