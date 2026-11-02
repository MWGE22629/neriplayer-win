"""流光动态背景:整窗底层自绘的彩色光斑场。

视觉对齐 Android 端 Hyper 背景的核心机制(见
docs/research/M9-android-ambient-background.md),但用 QPainter 的
径向渐变实现,不引 GPU 依赖。实测 1200x700 全屏绘制 1.71ms/帧,
50fps 下单核约 10%。

三条从原型迭代中得来的关键参数,改动前先读 docs/research/
M9-win-ambient-background.md §4.2:

1. 色标必须按 Android 的真实权重曲线 1/(1+9.3*(d/r)^2)^2 取样。
   该曲线在 d/r=0.5 处只剩 9%,衰减极陡;若用线性缓降色标,光斑会
   摊成一片均匀雾气,完全失去光核。
2. 光斑半径取 0.42 * 长边。用 0.62 时同样会糊成雾。
3. 叠加用 SourceOver 而非 Plus:相加在中部与两侧大面积过曝削顶,
   色彩细节丢失,只在深色主题且 alpha 极低时才可考虑。

流动一直进行,不随播放状态启停(与 Android 一致:其常驻漂移项在
音频电平为 0 时仍生效)。
"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

# Android 的五个光斑基准位置与半径(BgEffectPainter.java:60-66)
_POINTS = (
    (0.52, 0.46, 0.92),
    (0.14, 0.32, 0.74),
    (0.92, 0.30, 0.76),
    (0.26, 0.88, 0.80),
    (0.84, 0.86, 0.84),
)

# 常驻漂移振幅。Android 原值 0.10,但桌面上 1.5 秒只移约 30px,
# 静止看几乎察觉不到,实测 0.14 可感且不躁。
_DRIFT = 0.14
# 光斑半径系数(相对长边)。0.75 是还原真机「弥散光场」的关键:
# 半径小时五个场各自成型、能数出五个球;放大后边界互相吃掉,
# 融合成一片连续光场(与官网 WebGL demo 的观感一致)。
_RADIUS_SCALE = 0.95
# 光斑强度上限(alpha 峰值)。真机 shader 输出不透明色、不叠暗底,
# 这里靠 alpha 层层叠加出亮部;0.40 太暗(实测整体近黑),取 0.85
# 让最亮处接近真机的 V=0.68。
_ALPHA_GAIN = 2.1
# 色标取样段数:13 个停止点足够贴合陡衰减曲线
_STOPS = 13
# 目标帧率:50fps;对缓慢漂移足够,比 60fps 省电
_FRAME_MS = 20
# 换歌颜色过渡时长(对齐 Android 的 520ms)
_TRANSITION_MS = 520
# 噪声瓦片边长(只生成一次,逐帧复用)
_NOISE_TILE = 128
# 噪声不透明度:低于 0.03 压不住色带,高于 0.08 显脏
_NOISE_OPACITY = 0.05
# 斜向波形扰动的振幅(归一化坐标)。对齐 shader 的 motionEase*0.0062,
# 但纯音频无联动时取固定的 0.010,让静止画面也有斜向走向。
_WAVE_AMPLITUDE = 0.010

# 默认五色(暗色主题下无歌曲时用)
_DEFAULT_COLORS = (
    (0.42, 0.52, 0.72),
    (0.72, 0.55, 0.48),
    (0.68, 0.46, 0.58),
    (0.46, 0.44, 0.68),
    (0.50, 0.58, 0.64),
)


def _weight(frac: float) -> float:
    """Android shader 的光斑权重:w = 1/(1 + 9.3*(d/r)^2)^2。"""
    u = frac * frac
    return 1.0 / (1.0 + 9.3 * u) ** 2


_NOISE_CACHE: QPixmap | None = None


def _noise_pixmap() -> QPixmap:
    """灰度噪声瓦片(模块级共享,只生成一次)。

    生成一次约 17ms(16384 次 setPixel);进程内所有背景层共用同一张,
    避免每次新建窗口都重算。
    """
    global _NOISE_CACHE
    if _NOISE_CACHE is not None:
        return _NOISE_CACHE
    rng = random.Random(20261030)
    img = QImage(_NOISE_TILE, _NOISE_TILE, QImage.Format.Format_ARGB32_Premultiplied)
    for y in range(_NOISE_TILE):
        for x in range(_NOISE_TILE):
            value = rng.randint(118, 138)  # 围绕中灰的窄幅抖动
            img.setPixel(x, y, QColor(value, value, value, 255).rgba())
    _NOISE_CACHE = QPixmap.fromImage(img)
    return _NOISE_CACHE


class FlowBackground(QWidget):
    """流光背景层:五个彩色光斑缓慢流动。

    作为窗口底层控件使用,需配合 resizeEvent 同步 geometry。
    上层内容必须是半透明或透明,否则背景看不见。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)
        self._t = 0.0
        self._noise = _noise_pixmap()
        # 当前与目标颜色组(每个是 5 个 (r,g,b) 浮点三元组)
        self._colors: list[tuple[float, float, float]] = list(_DEFAULT_COLORS)
        self._from_colors: list[tuple[float, float, float]] = list(_DEFAULT_COLORS)
        self._to_colors: list[tuple[float, float, float]] = list(_DEFAULT_COLORS)
        self._transition = 1.0  # 1.0 表示过渡已完成
        self._base = QColor(26, 28, 34)
        self._dark = True
        self._enabled = False  # 默认关闭:不绘制、不跑定时器,观感与改动前一致
        self._solid = False

        self._timer = QTimer(self)
        self._timer.setInterval(_FRAME_MS)
        self._timer.timeout.connect(self._on_frame)

    # -- 对外 ----------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        """开关:关闭时停表并停止绘制,背景退回纯色。"""
        self._enabled = enabled
        if enabled:
            self.update()
        else:
            self._timer.stop()
            self.update()

    def set_solid(self, solid: bool) -> None:
        """纯色档:画一个主色渐变,不画光斑场。"""
        self._solid = solid
        if solid:
            self._timer.stop()
        self.update()

    def set_colors(
        self,
        colors: list[tuple[float, float, float]],
        *,
        base_hex: str = "",
        dark: bool = True,
        animate: bool = True,
    ) -> None:
        """设定五色(0-1 浮点 RGB)与底色;animate 时走 520ms 插值。"""
        if len(colors) != len(_POINTS):
            raise ValueError("流光背景需要恰好 5 个颜色")
        self._dark = dark
        if base_hex:
            color = QColor(base_hex)
            if color.isValid():
                self._base = color
        target = list(colors)
        if not animate or target == self._colors:
            self._colors = target
            self._from_colors = target
            self._to_colors = target
            self._transition = 1.0
            self.update()
            return
        self._from_colors = list(self._colors)
        self._to_colors = target
        self._transition = 0.0
        self.update()

    def start(self) -> None:
        """开始流动(窗口可见时调用)。"""
        if self._enabled and not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        """停止流动(窗口隐藏到托盘时调用),不占后台 CPU。"""
        self._timer.stop()

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    # -- 生命周期 ------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        self.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().hideEvent(event)
        self.stop()

    def _on_frame(self) -> None:
        # 时间推进:周期 2π ≈ 6.3 秒,与 Android 的 sin/cos(t) 一致
        self._t += _FRAME_MS / 1000.0
        if self._transition < 1.0:
            self._transition = min(1.0, self._transition + _FRAME_MS / _TRANSITION_MS)
            ease = self._transition * self._transition * (3.0 - 2.0 * self._transition)
            self._colors = [
                (
                    self._from_colors[i][0] + (self._to_colors[i][0] - self._from_colors[i][0]) * ease,
                    self._from_colors[i][1] + (self._to_colors[i][1] - self._from_colors[i][1]) * ease,
                    self._from_colors[i][2] + (self._to_colors[i][2] - self._from_colors[i][2]) * ease,
                )
                for i in range(len(_POINTS))
            ]
        self.update()

    # -- 绘制 ----------------------------------------------------------------

    def _average_color(self) -> QColor:
        """五色的平均色,作为光场底色(对齐真机的"不透明输出"语义)。"""
        n = len(self._colors)
        r = sum(c[0] for c in self._colors) / n
        g = sum(c[1] for c in self._colors) / n
        b = sum(c[2] for c in self._colors) / n
        if self._dark:
            # 暗色主题:再压暗一档,保持整体深色基调
            return QColor.fromRgbF(r * 0.26, g * 0.26, b * 0.26)
        return QColor.fromRgbF(r, g, b)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 本控件设了 WA_OpaquePaintEvent(Qt 不会替它清屏),所以每条路径
        # 都必须自己铺满整个区域,否则上一次的画面会残留成拖影。
        painter = QPainter(self)
        width = self.width()
        height = self.height()
        if width <= 0 or height <= 0:
            painter.end()
            return

        if not self._enabled or self._solid:
            # 纯色档 = 不加任何特效:铺一层与主题一致的底色,不画光场。
            # 不能直接 return(控件设了 WA_OpaquePaintEvent,不留残影),
            # 也不能画渐变:用户明确要求纯色档就是原本的干净底色。
            painter.fillRect(QRectF(0, 0, width, height), self._base)
            painter.end()
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 底色不再用固定深灰,而是五色的加权平均色。真机 shader 输出的是
        # 不透明颜色(五色按权重平均),不是"深底 + 半透明叠加";若铺深灰底,
        # 光场怎么叠都比真机暗一档(实测反复踩过)。
        painter.fillRect(QRectF(0, 0, width, height), self._average_color())

        # 斜向波形扰动:真机 shader 里 uv += motionEase * 0.0062 *
        # vec2(ribbonWave, -ribbonWave*0.75),作用在整幅采样坐标上,
        # 让光场等值线呈现出斜向的流动走向(少了这层就只是普通径向辉光)。
        # QPainter 里等价做法:把坐标系整体做一次微小的斜切+平移。
        ribbon = math.sin(
            ((self._t * 0.12) * 6.2832)
        ) * _WAVE_AMPLITUDE
        painter.save()
        painter.translate(ribbon * width, -ribbon * 0.75 * height)

        # Plus 相加:SourceOver 的单层 alpha 上限(1.0)把亮度锁死,
        # 实测增益加到 2.5 最亮也只有 102,到不了真机的 200+。
        # 发灰问题靠「底色压到很暗 + 光斑高饱和」解决,而不是放弃相加。
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for index, (px, py, radius) in enumerate(_POINTS):
            x = (px + _DRIFT * math.sin(self._t + py)) * width
            y = (py + _DRIFT * math.cos(self._t + px)) * height
            r = radius * max(width, height) * _RADIUS_SCALE
            rgb = self._colors[index]
            gradient = QRadialGradient(QPointF(x, y), r)
            # 按真实权重曲线取样:陡衰减才有光核
            for step in range(_STOPS):
                frac = step / (_STOPS - 1)
                color = QColor.fromRgbF(*rgb)
                color.setAlphaF(min(1.0, _weight(frac) * _ALPHA_GAIN))
                gradient.setColorAt(frac, color)
            painter.fillRect(QRectF(0, 0, width, height), gradient)

        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.restore()  # 结束波形扰动的坐标系变换

        # 噪声打散色带(只压 5% 不透明度,肉眼是细颗粒)
        painter.setOpacity(_NOISE_OPACITY)
        painter.drawTiledPixmap(QRectF(0, 0, width, height), self._noise, QPointF(0, 0))
        painter.setOpacity(1.0)

        # 斜向光带:真机画面是「左上亮、右下暗」的 135° 走向,不是中心
        # 放射。这一层决定了纵深感和"流光"的方向性,少了它画面会发平。
        sweep = QLinearGradient(0, 0, width, height)
        sweep.setColorAt(0.0, QColor(255, 255, 255, 16))
        sweep.setColorAt(0.35, QColor(255, 255, 255, 8))
        sweep.setColorAt(0.65, QColor(0, 0, 0, 18))
        sweep.setColorAt(1.0, QColor(0, 0, 0, 52))
        painter.fillRect(QRectF(0, 0, width, height), sweep)

        # 径向暗角:四角压深,把亮部收拢到画面中部偏上
        vignette = QRadialGradient(
            QPointF(width * 0.46, height * 0.40),
            max(width, height) * 0.78,
        )
        vignette.setColorAt(0.0, QColor(8, 9, 13, 0))
        vignette.setColorAt(0.42, QColor(8, 9, 13, 60))
        vignette.setColorAt(1.0, QColor(8, 9, 13, 205))
        painter.fillRect(QRectF(0, 0, width, height), vignette)

        # 顶亮底暗:保证标题与底栏文字对比度,同时强化纵深
        veil = QLinearGradient(0, 0, 0, height)
        veil.setColorAt(0.0, QColor(255, 255, 255, 10))
        veil.setColorAt(0.30, QColor(10, 11, 14, 0))
        veil.setColorAt(1.0, QColor(10, 11, 14, 70))
        painter.fillRect(QRectF(0, 0, width, height), veil)
        painter.end()
