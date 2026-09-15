"""动态文案字形安全(v0.3.0)单测。

背景:第三方标题中主字体不覆盖的字符(emoji/泰文/阿拉伯-印度数字等)
会让 DirectWrite 加载整套回退字体,实测 +45~50MB 常驻 WorkingSet
(docs/PERF.md「字形回退陷阱」)。sanitize_with 是纯函数核心;
GDI 集成路径依赖本机「微软雅黑 UI」(Windows 系统字体,套件环境恒有)。
"""

from __future__ import annotations

import sys

import pytest

from neriplayer_win.ui.textsafe import sanitize_ui_text, sanitize_with


class TestSanitizeWith:
    """纯函数核心:归一 + 过滤,覆盖判定由注入的 covered 决定。"""

    @staticmethod
    def _covered_ascii(ch: str) -> bool:
        return ord(ch) < 0x80

    def test_uncovered_replaced_with_question(self):
        assert sanitize_with("abc中", self._covered_ascii) == "abc?"

    def test_variant_selector_dropped(self):
        # U+FE0F(变体选择符,Cf)直接丢弃,不留「?」
        assert sanitize_with("a❤️b", self._covered_ascii) == "a?b"

    def test_control_chars_dropped(self):
        assert sanitize_with("a\x00b\x1fc", self._covered_ascii) == "abc"

    def test_nfkc_rescues_compat_forms(self):
        # 数学字母/带圈数字/全角归一成 ASCII 后即可保留,不再变「?」
        assert sanitize_with("𝕏①！", self._covered_ascii) == "X1!"

    def test_empty_and_all_unsafe_fallback(self):
        assert sanitize_with("", self._covered_ascii) == ""
        assert sanitize_with("中中", self._covered_ascii) == "??"


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

    def test_missing_glyphs_replaced(self, qapp):
        # ❤(U+2764)/♪(U+266A)/泰文与阿拉伯-印度数字均不在雅黑字形集内
        result = sanitize_ui_text("❤️蒸気火鸡❤")
        assert "❤" not in result
        assert "?" in result
        assert "蒸" in result  # 覆盖字符原样保留
        result = sanitize_ui_text("٩(๑)۶♪")
        assert "٩" not in result and "♪" not in result

    def test_non_bmp_replaced(self, qapp):
        # emoji(U+1F60A)与数学字母(U+1D54F):非 BMP 一律视为未覆盖
        result = sanitize_ui_text("𝕏𝟚😊")
        assert result == "X2?"  # 𝕏/𝟚 先经 NFKC 救回,😊 替换

    def test_fullwidth_rescued_by_nfkc(self, qapp):
        # 全角字母数字 NFKC 归一为半角(即使字体本身也覆盖全角形态)
        assert sanitize_ui_text("ＮｅｒｉＰｌａｙｅｒ") == "NeriPlayer"
