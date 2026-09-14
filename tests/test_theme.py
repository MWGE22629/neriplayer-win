"""主题系统单测(M4):冻结色板完整性、QSS 渲染、ThemeManager 行为。"""

from __future__ import annotations

import pytest

from neriplayer_win.ui import theme
from neriplayer_win.ui.theme import (
    DEFAULT_THEME,
    PALETTES,
    THEME_DARK,
    THEME_LIGHT,
    VALID_THEMES,
    ThemeManager,
    build_qss,
)

_REQUIRED_ROLES = (
    "primary", "onPrimary", "primaryContainer", "onPrimaryContainer",
    "secondary", "secondaryContainer", "onSecondaryContainer",
    "surface", "onSurface", "surfaceVariant", "onSurfaceVariant",
    "outline", "outlineVariant", "error", "errorContainer",
    "surfaceLowest", "surfaceLow", "surfaceContainer",
    "surfaceHigh", "surfaceHighest",
)


class TestPalettes:
    @pytest.mark.parametrize("name", VALID_THEMES)
    def test_roles_complete(self, name):
        palette = PALETTES[name]
        for role in _REQUIRED_ROLES:
            assert role in palette, f"{name} 缺角色 {role}"
            assert palette[role].startswith("#") and len(palette[role]) == 7

    def test_dark_light_differ(self):
        assert PALETTES[THEME_DARK]["surface"] != PALETTES[THEME_LIGHT]["surface"]
        assert PALETTES[THEME_DARK]["primary"] != PALETTES[THEME_LIGHT]["primary"]

    def test_frozen_values_match_docs(self):
        # docs/UI_ASSETS.md B1 冻结值抽样
        assert PALETTES[THEME_DARK]["primary"] == "#9ECAFF"
        assert PALETTES[THEME_DARK]["surface"] == "#111418"
        assert PALETTES[THEME_LIGHT]["primary"] == "#0061A4"
        assert PALETTES[THEME_LIGHT]["surface"] == "#F8F9FF"


class TestBuildQss:
    def test_tokens_substituted(self):
        qss = build_qss(PALETTES[THEME_DARK])
        assert f"background: {PALETTES[THEME_DARK]['surface']};" in qss
        # 没有残留记号
        for token in ("@surface", "@primary", "@onSurfaceVariant", "@outline"):
            assert token not in qss

    def test_design_values_present(self):
        qss = build_qss(PALETTES[THEME_DARK])
        # B2:卡片圆角 20 / 小件 8 / 播放条顶角 20 / 字号四级
        assert "border-radius: 20px" in qss
        assert "border-radius: 8px" in qss
        assert "border-top-left-radius: 20px" in qss
        for size in ("18px", "15px", "13px", "11px"):
            assert f"font-size: {size}" in qss

    def test_light_palette_renders_light_surface(self):
        qss = build_qss(PALETTES[THEME_LIGHT])
        assert PALETTES[THEME_LIGHT]["surface"] in qss


class TestThemeManager:
    def test_default_is_dark(self):
        manager = ThemeManager()
        assert manager.name == DEFAULT_THEME == "dark"

    def test_apply_switches(self):
        manager = ThemeManager()
        manager.apply(THEME_LIGHT)
        assert manager.name == "light"
        assert theme.current_theme_name() == "light"
        assert manager.palette() is PALETTES[THEME_LIGHT]
        assert "@primary" not in manager.qss()

    def test_apply_invalid_raises(self):
        manager = ThemeManager()
        with pytest.raises(ValueError):
            manager.apply("solarized")

    def test_theme_changed_signal(self):
        manager = ThemeManager()
        received: list[str] = []
        manager.theme_changed.connect(received.append)
        manager.apply(THEME_LIGHT)
        manager.apply(THEME_LIGHT)  # 同名重复应用不发信号
        manager.apply(THEME_DARK)
        assert received == ["light", "dark"]
        # 复位,避免影响同进程其他测试
        manager.apply(DEFAULT_THEME)

    def test_qss_written_to_application(self, qapp):
        manager = ThemeManager()
        manager.apply(THEME_LIGHT)
        try:
            assert qapp.styleSheet().strip() != ""
            assert PALETTES[THEME_LIGHT]["surface"] in qapp.styleSheet()
        finally:
            manager.apply(DEFAULT_THEME)
