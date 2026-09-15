"""动态文案字形安全(v0.3.0)。

第三方标题(收藏歌单名/收藏夹名/B站视频标题等)可能含主字体不覆盖的字符
——emoji、变体选择符、泰文/阿拉伯-印度数字、数学字母、杂项符号……
任一命中都会让 DirectWrite 加载整套回退字体并映射进进程:实测仅两个
几何字形就 +45~50MB 常驻 WorkingSet(见 docs/PERF.md「字形回退陷阱」;
v0.3.0 接入收藏歌单后,真实标题中的 ❤/٩/๑ 曾把登录态内存从 ~120MB
顶到 ~170MB)。

静态文案由 test_sidebar_text_has_no_exotic_glyph 按保守码区拦截;
动态标题不可控,渲染前经 sanitize_ui_text 净化:

- 先 NFKC 归一(𝕏→X、①→1、全角→半角等兼容形式救回可读 ASCII);
- 再按应用实际字体逐码点探测字形(GDI GetGlyphIndicesW +
  GGI_MARK_NONEXISTING_GLYPHS,结果按码点缓存);
- 未覆盖码点替换为「?」,控制/格式字符(含 U+FE0F 变体选择符)丢弃;
- 非 BMP 字符(emoji/数学字母区)不经探测直接视为未覆盖——它们必然
  触发 Segoe UI Emoji / Cambria Math 回退。

覆盖探测失败(非 Windows / GDI 异常)时按「已覆盖」放行:净化是性能
优化,失败不应吞掉标题内容。
"""

from __future__ import annotations

import ctypes
import threading
import unicodedata
from typing import Callable

# 离屏平台(QT_QPA_PLATFORM=offscreen)拿到的通用族名 "Sans Serif" 对 GDI
# 无意义,回退到中文 Windows 的系统默认 UI 字体
_GENERIC_FAMILY = "Sans Serif"
_FALLBACK_FONT = "Microsoft YaHei UI"

_GGI_MARK_NONEXISTING_GLYPHS = 1
_DEFAULT_CHARSET = 1

_lock = threading.Lock()
_covered: dict[int, bool] = {}  # 码点 → 字体是否覆盖
_font_resolved = False


def _is_droppable(ch: str) -> bool:
    """控制/格式字符与变体选择符:纯展示提示,直接丢弃不留「?」。"""
    if unicodedata.category(ch) in ("Cc", "Cf"):
        return True
    # U+FE00-FE0F 变体选择符类别是 Mn(组合记号),单独出现无意义
    return 0xFE00 <= ord(ch) <= 0xFE0F


def sanitize_with(text: str, covered: Callable[[str], bool]) -> str:
    """净化核心(纯函数,便于单测):归一 + 逐字符按 covered() 过滤。

    未覆盖 → 「?」;控制/格式字符与变体选择符直接丢弃;
    净化后为空则至少返回一个「?」(空串在侧栏/表格里不可辨认)。
    """
    if not text:
        return text
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", text):
        if _is_droppable(ch):
            continue
        out.append(ch if covered(ch) else "?")
    return "".join(out) or "?"


# ---------------------------------------------------------------------------
# GDI 字形探测(Windows;应用仅支持 Windows,其余平台 fail-open)
# ---------------------------------------------------------------------------

_hdc = None
_hfont = None


def _resolve_font_family() -> str:
    """应用当前默认字体族;无 QApplication 或离屏通用族名时回退雅黑。"""
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            family = app.font().family()
            if family and family != _GENERIC_FAMILY:
                return family
    except Exception:  # noqa: BLE001 - 字体解析失败回退默认,不阻断净化
        pass
    return _FALLBACK_FONT


def _probe_char(hdc, ch: str) -> bool | None:
    """GDI 探测单字符是否缺字形;异常/失败返回 None(fail-open)。"""
    try:
        buf = ctypes.create_unicode_buffer(ch)
        index = ctypes.c_uint16(0xFFFF)
        count = ctypes.windll.gdi32.GetGlyphIndicesW(
            hdc, buf, 1, ctypes.byref(index), _GGI_MARK_NONEXISTING_GLYPHS
        )
    except Exception:  # noqa: BLE001 - GDI 异常按已覆盖放行
        return None
    if count != 1:
        return None
    return index.value != 0xFFFF


def _ensure_probe_resources() -> tuple[int, int] | None:
    """惰性创建并复用 GDI 探测资源(HDC + HFONT);失败返回 None。"""
    global _hdc, _hfont
    if _hfont is not None:
        return _hdc, _hfont
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        if not hdc:
            return None
        hfont = gdi32.CreateFontW(
            -16, 0, 0, 0, 400, 0, 0, 0, _DEFAULT_CHARSET,
            0, 0, 0, 0, _resolve_font_family(),
        )
        if not hfont:
            user32.ReleaseDC(0, hdc)
            return None
        gdi32.SelectObject(hdc, hfont)
        _hdc, _hfont = hdc, hfont
        return _hdc, _hfont
    except Exception:  # noqa: BLE001 - GDI 异常 fail-open
        return None


def _char_covered(ch: str) -> bool:
    """码点级覆盖判定(带缓存);非 BMP 直接判未覆盖。"""
    if ord(ch) > 0xFFFF:
        return False  # 非 BMP(emoji/数学字母):必然走回退字体
    with _lock:
        global _font_resolved
        if not _font_resolved:
            _covered.clear()
            _font_resolved = True
        cached = _covered.get(ord(ch))
        if cached is not None:
            return cached
        result = True
        resources = _ensure_probe_resources()
        if resources is not None:
            hdc, _hfont_cached = resources
            probed = _probe_char(hdc, ch)
            if probed is not None:
                result = probed
        _covered[ord(ch)] = result
        return result


def reset_cache() -> None:
    """清空码点缓存并丢弃 GDI 资源(测试用:换字体后需重新探测)。"""
    global _hdc, _hfont, _font_resolved
    with _lock:
        if _hfont is not None:
            try:
                ctypes.windll.gdi32.DeleteObject(_hfont)
            except Exception:  # noqa: BLE001 - 清理失败不阻断
                pass
            _hdc = None
            _hfont = None
        _covered.clear()
        _font_resolved = False


def sanitize_ui_text(text: str) -> str:
    """把动态标题净化为主字体可覆盖的安全文本(入口,UI 线程调用)。"""
    return sanitize_with(text, _char_covered)
