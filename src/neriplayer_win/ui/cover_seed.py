"""封面种子色提取:播放时从封面挑一个「主色」喂给 M3 动态主题。

对照上游 Android 实现(reference/NeriPlayer-Android 的
CoverArtColorCache.extract):androidx Palette 在 96px 缩略图上做
量化直方图,按 vibrant → muted → dominant 的优先级择色。此处用同语义
的轻量近似:5bit/通道量化直方图 + HSL 打分:

- vibrant:饱和度 ≥0.35 且亮度 0.30~0.70 的桶里,按
  score = 2×饱和度 + 桶占比 取最高(贴近 androidx Vibrant 权重);
- muted:饱和度 <0.30 且同亮度区间的桶里取最大占比;
- dominant:全图最大占比桶(兜底,任何封面都有)。

返回桶内像素的平均色;提取失败(坏图/空数据)返回 None,调用方维持
现状。带 FIFO 内存缓存(64 条,对齐上游 LruCache(64));URL 的磁盘
缓存由 CoverLoader 承担,这里只缓存提取结果。
"""

from __future__ import annotations

import colorsys

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

# 与上游一致的采样尺寸:96px 足够估计主色,直方图仅 9216 像素
_SAMPLE_SIZE = 96
_CACHE_MAX = 64

_cache: dict[str, str] = {}


def cache_get(url: str) -> str | None:
    """查缓存;命中会刷新到 FIFO 尾部(最近使用)。"""
    hex_color = _cache.pop(url, None)
    if hex_color is not None:
        _cache[url] = hex_color
    return hex_color


def cache_put(url: str, hex_color: str) -> None:
    """写入缓存并裁剪到上限(超出丢最旧)。"""
    _cache.pop(url, None)
    _cache[url] = hex_color
    while len(_cache) > _CACHE_MAX:
        _cache.pop(next(iter(_cache)))


def clear_cache() -> None:
    _cache.clear()


def extract_seed_hex(data: bytes) -> str | None:
    """封面图像字节 → 种子色 '#RRGGBB';无法解码返回 None。"""
    image = QImage()
    if not image.loadFromData(data):
        return None
    scaled = image.scaled(
        _SAMPLE_SIZE, _SAMPLE_SIZE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    ).convertToFormat(QImage.Format.Format_RGB32)
    if scaled.width() < 1 or scaled.height() < 1:
        return None
    # 量化直方图:5bit/通道(32768 桶),桶内累加原色供平均
    bins: dict[tuple[int, int, int], list[int]] = {}  # key -> [count, r_sum, g_sum, b_sum]
    for y in range(scaled.height()):
        for x in range(scaled.width()):
            pixel = scaled.pixel(x, y)
            r, g, b = (pixel >> 16) & 0xFF, (pixel >> 8) & 0xFF, pixel & 0xFF
            key = (r >> 3, g >> 3, b >> 3)
            acc = bins.get(key)
            if acc is None:
                bins[key] = [1, r, g, b]
            else:
                acc[0] += 1
                acc[1] += r
                acc[2] += g
                acc[3] += b
    return _pick_seed(bins)


def _pick_seed(bins: dict[tuple[int, int, int], list[int]]) -> str | None:
    if not bins:
        return None
    max_count = max(acc[0] for acc in bins.values())
    best_vibrant: tuple[float, list[int]] | None = None
    best_muted: tuple[float, list[int]] | None = None
    best_dominant: list[int] | None = None
    for acc in bins.values():
        count = acc[0]
        r, g, b = acc[1] / count, acc[2] / count, acc[3] / count
        if best_dominant is None or count > best_dominant[0]:
            best_dominant = acc
        hue, lightness, saturation = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        if 0.30 <= lightness <= 0.70:
            if saturation >= 0.35:
                score = 2.0 * saturation + count / max_count
                if best_vibrant is None or score > best_vibrant[0]:
                    best_vibrant = (score, acc)
            elif saturation < 0.30:
                score = count / max_count
                if best_muted is None or score > best_muted[0]:
                    best_muted = (score, acc)
    chosen = (
        best_vibrant[1] if best_vibrant is not None
        else best_muted[1] if best_muted is not None
        else best_dominant
    )
    count = chosen[0]
    return "#{:02X}{:02X}{:02X}".format(
        int(round(chosen[1] / count)),
        int(round(chosen[2] / count)),
        int(round(chosen[3] / count)),
    )
