"""流光背景的五色调色板:从当前主题色板派生光斑颜色。

不直接用 Android 的 Palette 多 swatch 提取(需要图像聚类),而是从
TonalSpot 色板派生五色。理由:零成本、与 QSS 主题天然同源,换歌时
背景色调与界面一致,不会出现界面里没有的颜色。

五个角色对应 Android 的 Base/Accent/Light/Dark/Bridge,但取的是
「光斑色」而不是界面色:界面色的 tone 是压暗过的,直接拿去做光斑
会灰扑扑,所以按明度拉开层次。
"""

from __future__ import annotations

from typing import Mapping

from PySide6.QtGui import QColor


def _to_rgb(hex_color: str) -> tuple[float, float, float] | None:
    color = QColor(hex_color)
    if not color.isValid():
        return None
    return (color.redF(), color.greenF(), color.blueF())


def _shift_lightness(
    rgb: tuple[float, float, float], factor: float
) -> tuple[float, float, float]:
    """调明度:factor > 1 提亮,< 1 压暗;带轻微饱和补偿避免发灰。"""
    color = QColor.fromRgbF(*rgb)
    h, s, v, a = color.getHsvF()
    h = h if h >= 0 else 0.0
    v = max(0.0, min(1.0, v * factor))
    # 提亮时略降饱和(过亮高饱和刺眼),压暗时略提饱和(暗色下才有色)
    s = max(0.0, min(1.0, s * (0.88 if factor > 1.0 else 1.12)))
    result = QColor.fromHsvF(h, s, v, a)
    return (result.redF(), result.greenF(), result.blueF())


def flow_colors_from_palette(
    palette: Mapping[str, str], *, dark: bool = True
) -> list[tuple[float, float, float]]:
    """主题色板 → 五个光斑色。

    真机的光场是**单色系不同明度**,不是多色相拼贴:官网 demo 的五色
    (#534b75 / #231f44 / #9189ad / #2a2742 / #5c5475)全部落在同一个
    紫蓝色系内,只有明度差异。多色相会让画面读成「五个彩球」,与真机
    观感不符。

    做法:取 primary 的色相作为全场基色,secondary 拉一点色相偏移做
    微弱层次,然后按明度拉开 5 档。饱和度整体压低,避免发光刺眼。
    """
    primary = _to_rgb(palette.get("primary", "")) or (0.35, 0.55, 0.80)
    secondary = _to_rgb(palette.get("secondary", "")) or (0.55, 0.50, 0.62)

    # 统一色相:以 primary 为锚,混入少量 secondary 让层次不死板
    base = _blend_hue(primary, secondary, 0.18 if dark else 0.12)
    # 明度与饱和度直接对齐真机 demo 的实测值:
    # 官网五色 HSV 是 V=0.26~0.68、S=0.21~0.54。定得比这更低会让光场
    # 在深色界面上"几乎看不见"(实测踩过:全黑,只剩极弱起伏)。
    if dark:
        levels = (0.27, 0.42, 0.46, 0.58, 0.68)
        sats = (0.50, 0.32, 0.36, 0.24, 0.21)
    else:
        levels = (0.62, 0.72, 0.78, 0.86, 0.92)
        sats = (0.38, 0.28, 0.24, 0.18, 0.14)
    return [_tone(base, value, sat) for value, sat in zip(levels, sats)]


def _blend_hue(
    a: tuple[float, float, float], b: tuple[float, float, float], amount: float
) -> tuple[float, float, float]:
    """取 a 的明度与饱和度,色相向 b 偏一点(短路径)。"""
    ha, sa, va, _ = QColor.fromRgbF(*a).getHsvF()
    hb, _sb, _vb, _ = QColor.fromRgbF(*b).getHsvF()
    ha = ha if ha >= 0 else 0.0
    hb = hb if hb >= 0 else 0.0
    delta = hb - ha
    if delta > 0.5:
        delta -= 1.0
    elif delta < -0.5:
        delta += 1.0
    hue = (ha + delta * amount) % 1.0
    result = QColor.fromHsvF(hue, sa, va, 1.0)
    return (result.redF(), result.greenF(), result.blueF())


def _tone(
    base: tuple[float, float, float], value: float, sat: float
) -> tuple[float, float, float]:
    """按指定明度与饱和度生成同色相的颜色。"""
    hue, _s, _v, _ = QColor.fromRgbF(*base).getHsvF()
    hue = hue if hue >= 0 else 0.0
    color = QColor.fromHsvF(hue, sat, value, 1.0)
    return (color.redF(), color.greenF(), color.blueF())
