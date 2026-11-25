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
- 再按应用字体逐码点探测字形(真实平台走应用字体的 QRawFont,结果按
  码点缓存;没有字体引擎的离屏测试退回 GDI GetGlyphIndicesW);
- 未覆盖码点与控制/格式字符(含 U+FE0F 变体选择符)一并省略——
  显示空白比「?」更不突兀,问号会让用户以为标题损坏(2026-09-15
  用户反馈);净化后为空也保留空串:这些标题都在带封面的行上,
  「?」是干扰(2026-11-25 用户反馈,此前兜底为「?」);
- 非 BMP 字符(emoji/数学字母区)不经探测直接视为未覆盖——它们必然
  触发 Segoe UI Emoji / Cambria Math 回退。

两道防线(2026-11-25 又中招后加固,实测见 docs/PERF.md「字形回退陷阱」):

- 净化按**应用字体本身**探测(Qt 渲染所用的 DirectWrite 字形表)。此前
  用 GDI 探测,与渲染侧不是同一套字体选择:注册表 FontSubstitutes 把
  「微软雅黑」替换成别的字体时,GDI 说有、DirectWrite 说没有,漏网的
  组合重音符 U+0301 一个字符就让 DirectWrite 翻遍全部系统字体
  (本机 271 个字体文件,+86MB 常驻)。
- disable_font_fallback 关死字体合并:即使再有漏网字符,至多显示空框,
  不再去加载回退字体。

覆盖探测失败(没有字体引擎 / GDI 异常)时按「已覆盖」放行:净化是
性能优化,失败不应吞掉标题内容。
"""

from __future__ import annotations

import ctypes
import threading
import unicodedata
from typing import Callable

from PySide6.QtCore import QEvent
from PySide6.QtGui import QFont, QGuiApplication, QRawFont
from PySide6.QtWidgets import QApplication

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
    """控制/格式字符与变体选择符:纯展示提示,无信息量,直接省略。"""
    if unicodedata.category(ch) in ("Cc", "Cf"):
        return True
    # U+FE00-FE0F 变体选择符类别是 Mn(组合记号),单独出现无意义
    return 0xFE00 <= ord(ch) <= 0xFE0F


def sanitize_with(text: str, covered: Callable[[str], bool]) -> str:
    """净化核心(纯函数,便于单测):归一 + 逐字符按 covered() 过滤。

    未覆盖字符与控制/格式字符/变体选择符一并省略(显示空白);
    净化后为空也保留空串——所在行有封面可辨,「?」是干扰。没有封面
    可辨的场合由调用方自行兜底。
    """
    if not text:
        return text
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", text):
        if _is_droppable(ch):
            continue
        if covered(ch):
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 字形探测:应用字体本身(Qt 字体引擎,与渲染同一套字体选择);拿不到时
# 退回 GDI(离屏测试没有字体引擎),再不行按已覆盖放行。仅支持 Windows。
# ---------------------------------------------------------------------------

_hdc = None
_hfont = None
_raw_font: QRawFont | None = None


def _resolve_raw_font() -> QRawFont | None:
    """应用字体本身;拿不到(无 GUI 实例 / 非真实平台 / 字体无效)返回 None。"""
    try:
        app = QApplication.instance()
        if app is None or QGuiApplication.platformName() != "windows":
            return None
        raw = QRawFont.fromFont(app.font())
    except Exception:  # noqa: BLE001 - 探测属优化,失败退回 GDI
        return None
    return raw if raw.isValid() else None


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
        global _font_resolved, _raw_font
        if not _font_resolved:
            _covered.clear()
            _raw_font = _resolve_raw_font()
            _font_resolved = True
        cached = _covered.get(ord(ch))
        if cached is not None:
            return cached
        result = True
        if _raw_font is not None:
            result = _raw_font.glyphIndexesForString(ch)[0] != 0
        else:
            resources = _ensure_probe_resources()
            if resources is not None:
                probed = _probe_char(resources[0], ch)
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


def disable_font_fallback(app) -> None:
    """硬闸:应用字体改 NoFontMerging,缺字形也不加载回退字体。

    净化只管得到经过它的动态文案;这里兜住其余一切(静态文案、tooltip、
    第三方控件内部文本):漏网字符至多显示空框,不会把整套系统字体映射
    进进程。

    调用时机:主题首次挂上 QSS 之后、窗口显示之前。首次 setStyleSheet
    会经 setStyle 重建 Qt 的主题类字体表(QAbstractItemView / QStatusBar /
    QTipLabel 等控件的天然字体取自那张表,不是应用字体),盖印早了会被
    冲掉;不带类名的 setFont 则连带清空该表,这些控件从此与应用字体同源。
    """
    font = app.font()
    font.setStyleStrategy(QFont.StyleStrategy.NoFontMerging)
    app.setFont(font)
    # 不带类名的 setFont 不通知任何已建控件:逐个补发 ApplicationFontChange,
    # 让它们按新应用字体重解析(含顶层窗口)
    change = QEvent(QEvent.Type.ApplicationFontChange)
    for widget in app.allWidgets():
        QApplication.sendEvent(widget, change)
