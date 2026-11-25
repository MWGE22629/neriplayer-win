"""动态文案字形安全(v0.3.0)单测。

背景:第三方标题中主字体不覆盖的字符(emoji/泰文/阿拉伯-印度数字等)
会让 DirectWrite 加载整套回退字体,实测 +45~50MB 常驻 WorkingSet
(docs/PERF.md「字形回退陷阱」)。sanitize_with 是纯函数核心;
真实平台按应用字体本身(QRawFont)探测,离屏套件没有字体引擎、走 GDI,
两条路径都依赖本机「微软雅黑 UI」(Windows 系统字体,套件环境恒有)。
"""

from __future__ import annotations

import sys

import pytest

from neriplayer_win.ui import textsafe
from neriplayer_win.ui.textsafe import disable_font_fallback, sanitize_ui_text, sanitize_with


class TestSanitizeWith:
    """纯函数核心:归一 + 过滤,覆盖判定由注入的 covered 决定。"""

    @staticmethod
    def _covered_ascii(ch: str) -> bool:
        return ord(ch) < 0x80

    def test_uncovered_omitted(self):
        # 未覆盖字符显示空白(静默省略),不用「?」(用户反馈:问号易困惑)
        assert sanitize_with("abc中", self._covered_ascii) == "abc"

    def test_variant_selector_dropped(self):
        # U+FE0F(变体选择符,Cf)直接丢弃
        assert sanitize_with("a❤️b", self._covered_ascii) == "ab"

    def test_control_chars_dropped(self):
        assert sanitize_with("a\x00b\x1fc", self._covered_ascii) == "abc"

    def test_nfkc_rescues_compat_forms(self):
        # 数学字母/带圈数字/全角归一成 ASCII 后即可保留
        assert sanitize_with("𝕏①！", self._covered_ascii) == "X1!"

    def test_all_unsafe_kept_empty(self):
        # 全部不可用时保留空串:所在行有封面可辨,「?」反而是干扰
        assert sanitize_with("", self._covered_ascii) == ""
        assert sanitize_with("中中", self._covered_ascii) == ""


@pytest.mark.skipif(sys.platform != "win32", reason="GDI 字形探测仅 Windows")
class TestSanitizeUiText:
    """GDI 集成:按本机微软雅黑 UI 的真实覆盖判定。"""

    def test_cjk_and_common_punctuation_kept(self, qapp):
        # 注:NFKC 会把兼容形式展开(…→...、①→1),此处只放无分解的字符
        text = "【Jazz Blues】爵士乐句演绎12小节『合集』·—★"
        assert sanitize_ui_text(text) == text

    def test_nfkc_normalizes_compat_forms(self, qapp):
        # Ⅳ/全角/省略号等兼容形式经 NFKC 归一为 ASCII 形态(可读性更好)
        assert sanitize_ui_text("ⅫⅣ") == "XIIIV"
        assert sanitize_ui_text("九…") == "九..."

    def test_missing_glyphs_omitted(self, qapp):
        # ❤(U+2764)/♪(U+266A)/泰文与阿拉伯-印度数字均不在雅黑字形集内
        result = sanitize_ui_text("❤️蒸気火鸡❤")
        assert result == "蒸気火鸡"
        assert "?" not in result  # 未覆盖字符静默省略,不显示问号
        result = sanitize_ui_text("٩(๑)۶♪")
        assert result == "()"

    def test_non_bmp_omitted(self, qapp):
        # emoji(U+1F60A)与数学字母(U+1D54F):非 BMP 一律视为未覆盖
        result = sanitize_ui_text("𝕏𝟚😊")
        assert result == "X2"  # 𝕏/𝟚 先经 NFKC 救回,😊 省略
        assert sanitize_ui_text("🎵😊") == ""  # 整段不可用:留空,不兜底

    def test_fullwidth_rescued_by_nfkc(self, qapp):
        # 全角字母数字 NFKC 归一为半角(即使字体本身也覆盖全角形态)
        assert sanitize_ui_text("ＮｅｒｉＰｌａｙｅｒ") == "NeriPlayer"


class TestHardening20261125:
    """一个组合重音符(U+0301)让 DirectWrite 翻遍全部系统字体(+86MB)后的加固。"""

    def test_probe_uses_app_font_glyph_table(self, monkeypatch):
        # 真实平台按应用字体本身(QRawFont)的字形表判定,与渲染同一套字体;
        # 离屏没有字体引擎,这里桩一个:♪ 无字形,其余有
        class FakeFont:
            @staticmethod
            def glyphIndexesForString(text):
                return [0 if ch == "\u266a" else 1 for ch in text]

        monkeypatch.setattr(textsafe, "_resolve_raw_font", lambda: FakeFont())
        textsafe.reset_cache()
        try:
            assert sanitize_ui_text("歌\u266a单") == "歌单"
        finally:
            textsafe.reset_cache()

    def test_every_widget_disallows_font_merging(self, qapp):
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import (
            QApplication,
            QLabel,
            QStatusBar,
            QTreeWidget,
            QWidget,
        )

        no_merging = QFont.StyleStrategy.NoFontMerging
        saved_font, saved_sheet = qapp.font(), qapp.styleSheet()
        try:
            window = QWidget()
            tree = QTreeWidget(window)
            QStatusBar(window)
            QLabel("x", window)
            # 首次挂 QSS 时 Qt 会重建主题类字体表:项视图/状态栏等的天然字体
            # 取自那张表而非应用字体,只改应用字体盖不住它们
            qapp.setStyleSheet("QWidget { font-size: 13px; }")
            window.show()
            qapp.processEvents()

            disable_font_fallback(qapp)

            assert qapp.font().styleStrategy() & no_merging
            for widget in (window, tree, *qapp.allWidgets()):
                assert widget.font().styleStrategy() & no_merging, type(
                    widget
                ).__name__
            # 之后再建的控件(含 tooltip 的类字体)同样不合并
            assert QTreeWidget().font().styleStrategy() & no_merging
            assert QApplication.font("QTipLabel").styleStrategy() & no_merging
        finally:
            qapp.setStyleSheet(saved_sheet)
            qapp.setFont(saved_font)
