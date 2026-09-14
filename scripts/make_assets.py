"""NeriPlayer Win 的 UI 资产生成脚本(M4)。

三件事,全部可重复执行:

1. 把 reference/NeriPlayer-Android 的 Android drawable 矢量 XML 机械转换为
   SVG(规则见 docs/UI_ASSETS.md A2:viewportWidth/Height -> viewBox、
   pathData -> d、fillColor -> fill、丢弃 android:tint),写入
   src/neriplayer_win/assets/icons/。
2. 落地 Material Symbols Outlined 补缺图标(path data 内嵌在本脚本,
   Apache 2.0,声明写入每个 SVG 头部)。
3. 用 QtSvg(PySide6 自带,与运行时同一 SVG 后端)+ Pillow 生成位图资产到
   src/neriplayer_win/assets/:
   - app.ico:16/32/48/64/128/256。小尺寸用「mascot 裁剪版 + 深藏青圆角
     底」(16px 下完整字标会糊,按施工图 C1 处理);128/256 用完整
     neriplayer.svg(透明底)。
   - tray_dark.png / tray_light.png:32x32 托盘深浅两版(同为圆角底 +
     mascot,仅底色不同)。

   (原计划 cairosvg,但 Windows 上缺原生 cairo DLL;QtSvg 零额外依赖,
   且与 QIcon 运行时渲染一致,故改用。)

用法(仓库根目录):

    uv run --with pillow python scripts/make_assets.py

reference/ 只读;本脚本只写 src/neriplayer_win/assets/。
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = REPO_ROOT / "reference" / "NeriPlayer-Android"
DRAWABLE_DIR = REFERENCE / "app" / "src" / "main" / "res" / "drawable"
ICON_SVG_DIR = REFERENCE / "icon"
ASSETS_DIR = REPO_ROOT / "src" / "neriplayer_win" / "assets"
ICONS_DIR = ASSETS_DIR / "icons"

UPSTREAM_URL = "https://github.com/cwuom/NeriPlayer"

# ---------------------------------------------------------------------------
# 1. drawable XML -> SVG
# ---------------------------------------------------------------------------

# (输出名, 源 XML 文件名, 备注)
_DRAWABLE_ICONS = [
    ("play", "round_play_arrow_24.xml"),
    ("pause", "round_pause_24.xml"),
    ("skip_previous", "round_skip_previous_24.xml"),
    ("skip_next", "round_skip_next_24.xml"),
    ("volume_up", "round_volume_up_24.xml"),
    ("favorite", "ic_baseline_favorite_24.xml"),
    ("favorite_border", "ic_outline_favorite_24.xml"),
    ("shuffle", "ic_shortcut_shuffle.xml"),
    ("search", "ic_shortcut_search.xml"),
    ("library_music", "ic_shortcut_library.xml"),
    ("refresh", "outline_refresh_24.xml"),
]

# 品牌标:多色/固定色,运行时不做主题染色。
_BRAND_ICONS = [
    # ic_bilibili.xml 本身即品牌蓝 #1296DB,深浅底都可见,保持原色
    ("bilibili", "ic_bilibili.xml", None),
    # ic_netease_cloud_music.xml 原始填充为白色(上游显示在红色底上),
    # 此处改填网易云品牌红 #C20C0C,保证浅色主题下可见 —— 有意偏离机械转换
    ("netease", "ic_netease_cloud_music.xml", "#C20C0C"),
]

_ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def _attr(element: ET.Element, name: str) -> str | None:
    return element.get(_ANDROID_NS + name)


def _svg_color(raw: str | None, default: str = "#FFFFFF") -> str:
    """fillColor 取值归一化为 #RRGGBB。"""
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "@android:color/white":
        return "#FFFFFF"
    if raw == "@android:color/black":
        return "#000000"
    if raw.startswith("#") and len(raw) == 9:  # #AARRGGBB -> #RRGGBB
        return f"#{raw[3:]}"
    return raw


def _paths_to_svg(root: ET.Element, override_fill: str | None) -> str:
    viewport_w = root.get("viewportWidth") or root.get(
        _ANDROID_NS + "viewportWidth", "24"
    )
    viewport_h = root.get("viewportHeight") or root.get(
        _ANDROID_NS + "viewportHeight", "24"
    )

    def emit(node: ET.Element, depth: int) -> list[str]:
        pad = "  " * depth
        lines: list[str] = []
        tag = node.tag.replace(_ANDROID_NS, "")
        if tag == "group":
            transforms = []
            tx, ty = _attr(node, "translateX"), _attr(node, "translateY")
            if tx or ty:
                transforms.append(f"translate({tx or '0'} {ty or '0'})")
            sx, sy = _attr(node, "scaleX"), _attr(node, "scaleY")
            if sx or sy:
                transforms.append(f"scale({sx or '1'} {sy or '1'})")
            rot = _attr(node, "rotation")
            if rot:
                transforms.append(f"rotate({rot})")
            transform = f" transform=\"{' '.join(transforms)}\"" if transforms else ""
            lines.append(f"{pad}<g{transform}>")
            for child in node:
                lines.extend(emit(child, depth + 1))
            lines.append(f"{pad}</g>")
        elif tag == "path":
            d = (_attr(node, "pathData") or "").strip()
            fill = override_fill or _svg_color(_attr(node, "fillColor"))
            alpha = _attr(node, "fillAlpha")
            attrs = f' d="{d}" fill="{fill}"'
            if alpha and float(alpha) != 1.0:
                attrs += f' fill-opacity="{alpha}"'
            lines.append(f"{pad}<path{attrs}/>")
        return lines

    body: list[str] = []
    for child in root:
        body.extend(emit(child, 1))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {viewport_w} {viewport_h}">\n' + "\n".join(body) + "\n</svg>\n"
    )


def _drawable_header(source: str, note: str = "") -> str:
    text = (
        f"  Mechanical conversion from Android drawable vector XML:\n"
        f"  reference/NeriPlayer-Android/app/src/main/res/drawable/{source}\n"
        f"  Upstream: NeriPlayer (GPL-3.0), {UPSTREAM_URL}\n"
        f"  Rules: viewportWidth/Height -> viewBox, pathData -> d,\n"
        f"         fillColor -> fill, android:tint dropped.\n"
    )
    if note:
        text += f"  Note: {note}\n"
    return f"<!--\n{text}-->\n"


def convert_drawables() -> list[Path]:
    written: list[Path] = []
    for name, source in _DRAWABLE_ICONS:
        tree = ET.parse(DRAWABLE_DIR / source)
        svg = _drawable_header(source) + _paths_to_svg(tree.getroot(), None)
        path = ICONS_DIR / f"{name}.svg"
        path.write_text(svg, encoding="utf-8")
        written.append(path)
    for name, source, fill in _BRAND_ICONS:
        tree = ET.parse(DRAWABLE_DIR / source)
        note = (
            "fill recolored from white to NetEase brand red #C20C0C "
            "(original white is invisible on light background)."
            if fill
            else "brand colors kept as-is."
        )
        svg = _drawable_header(source, note) + _paths_to_svg(tree.getroot(), fill)
        path = ICONS_DIR / f"{name}.svg"
        path.write_text(svg, encoding="utf-8")
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# 2. Material Symbols Outlined 补缺(Apache 2.0,path data 内嵌)
# ---------------------------------------------------------------------------

_APACHE_HEADER = """<!--
  Vendored from Material Symbols (Google LLC), licensed under the
  Apache License, Version 2.0. https://www.apache.org/licenses/LICENSE-2.0
  Source: https://github.com/google/material-design-icons
  Symbol: {name} (outlined, 24px) — path data unchanged.
-->
"""

# symbol name -> path @d(viewBox 均为 "0 -960 960 960")
_MATERIAL_SYMBOLS = {
    "settings": "m370-80-16-128q-13-5-24.5-12T307-235l-119 50L78-375l103-78q-1-7-1-13.5v-27q0-6.5 1-13.5L78-585l110-190 119 50q11-8 23-15t24-12l16-128h220l16 128q13 5 24.5 12t22.5 15l119-50 110 190-103 78q1 7 1 13.5v27q0 6.5-2 13.5l103 78-110 190-118-50q-11 8-23 15t-24 12L590-80H370Zm70-80h79l14-106q31-8 57.5-23.5T639-327l99 41 39-68-86-65q5-14 7-29.5t2-31.5q0-16-2-31.5t-7-29.5l86-65-39-68-99 42q-22-23-48.5-38.5T533-694l-13-106h-79l-14 106q-31 8-57.5 23.5T321-633l-99-41-39 68 86 64q-5 15-7 30t-2 32q0 16 2 31t7 30l-86 65 39 68 99-42q22 23 48.5 38.5T427-266l13 106Zm42-180q58 0 99-41t41-99q0-58-41-99t-99-41q-59 0-99.5 41T342-480q0 58 40.5 99t99.5 41Zm-2-140Z",
    "queue_music": "M640-160q-50 0-85-35t-35-85q0-50 35-85t85-35q11 0 21 1.5t19 6.5v-328h200v80H760v360q0 50-35 85t-85 35ZM120-320v-80h320v80H120Zm0-160v-80h480v80H120Zm0-160v-80h480v80H120Z",
    "repeat": "M280-80 120-240l160-160 56 58-62 62h406v-160h80v240H274l62 62-56 58Zm-80-440v-240h486l-62-62 56-58 160 160-160 160-56-58 62-62H280v160h-80Z",
    "repeat_one": "M460-360v-180h-60v-60h120v240h-60ZM280-80 120-240l160-160 56 58-62 62h406v-160h80v240H274l62 62-56 58Zm-80-440v-240h486l-62-62 56-58 160 160-160 160-56-58 62-62H280v160h-80Z",
    "playlist_play": "M120-320v-80h320v80H120Zm0-160v-80h480v80H120Zm0-160v-80h480v80H120Zm520 520v-320l240 160-240 160Z",
    "person": "M480-480q-66 0-113-47t-47-113q0-66 47-113t113-47q66 0 113 47t47 113q0 66-47 113t-113 47ZM160-160v-112q0-34 17.5-62.5T224-378q62-31 126-46.5T480-440q66 0 130 15.5T736-378q29 15 46.5 43.5T800-272v112H160Zm80-80h480v-32q0-11-5.5-20T700-306q-54-27-109-40.5T480-360q-56 0-111 13.5T260-306q-9 5-14.5 14t-5.5 20v32Zm240-320q33 0 56.5-23.5T560-640q0-33-23.5-56.5T480-720q-33 0-56.5 23.5T400-640q0 33 23.5 56.5T480-560Zm0-80Zm0 400Z",
}


def write_material_symbols() -> list[Path]:
    written: list[Path] = []
    for name, d in _MATERIAL_SYMBOLS.items():
        svg = (
            _APACHE_HEADER.format(name=name)
            + f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 -960 960 960"><path d="{d}"/></svg>\n'
        )
        path = ICONS_DIR / f"{name}.svg"
        path.write_text(svg, encoding="utf-8")
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# 3. 位图资产:app.ico + 托盘 PNG(QtSvg 渲染 + Pillow 合成)
# ---------------------------------------------------------------------------

_MASCOT_RGB = (201, 168, 251)  # neriplayer.svg 吉祥物淡紫 #c9a8fb
_MASCOT_TOL = 55
_TILE_DARK = (30, 41, 58, 255)  # #1e293a 深藏青(施工图 A1 ic_neriplayer 底色)
_TILE_LIGHT = (243, 244, 249, 255)  # #F3F4F9 浅色 surface 层级色


def _render_png(svg_path: Path, size: int):
    """QtSvg 栅格化(抗锯齿,透明底),转 PIL RGBA。

    先 import neriplayer_win 以复用包级 ICU 预加载,避免 Windows 上
    Qt6Core 被 PATH 里外来 icuuc.dll 抢载(见包 __init__)。
    """
    import neriplayer_win  # noqa: F401  # 触发 win32 ICU 预加载

    from PIL import Image
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise ValueError(f"SVG 无法解析:{svg_path}")
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    # QImage 逐像素拷出太慢,走 PNG 内存缓冲转 PIL
    import io

    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError(f"QImage PNG 编码失败:{svg_path}")
    return Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGBA")


def _purple_mask(image):
    """距 #c9a8fb 容差以内的像素掩码(吉祥物、圆环与同色字标)。"""
    from PIL import Image, ImageChops

    solid = Image.new("RGB", image.size, _MASCOT_RGB)
    diff = ImageChops.difference(image.convert("RGB"), solid)
    threshold = lambda v: 255 if v <= _MASCOT_TOL else 0  # noqa: E731
    channels = [ch.point(threshold) for ch in diff.split()[:3]]
    return ImageChops.multiply(
        ImageChops.multiply(channels[0], channels[1]), channels[2]
    )


def _largest_row_run(mask) -> tuple[int, int] | None:
    """掩码按行占用切分连通竖带,返回最高的一条(吉祥物)。

    neriplayer.svg 里 "NeriPlayer" 字标与吉祥物同为 #c9a8fb(深灰蓝
    #494565 实为吉祥物旁的小猫),直接按颜色裁会把字标带上;字标位于
    底部、与主体之间有空隙,取最大行带即可排除。
    """
    width, height = mask.size
    pixels = mask.load()
    step = 4
    runs: list[list[int]] = []
    start: int | None = None
    for y in range(height):
        occupied = any(pixels[x, y] for x in range(0, width, step))
        if occupied and start is None:
            start = y
        elif not occupied and start is not None:
            runs.append([start, y])
            start = None
    if start is not None:
        runs.append([start, height])
    merged: list[list[int]] = []
    for run in runs:  # 容忍 6px 以内的细缝(抗锯齿)
        if merged and run[0] - merged[-1][1] <= 6:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    if not merged:
        return None
    return max(merged, key=lambda r: r[1] - r[0])


def _mascot_crop(full_1024):
    """裁出吉祥物主体(排除底部同色字标,含少量留白),返回正方形 RGBA。"""
    from PIL import Image

    mask = _purple_mask(full_1024)
    band = _largest_row_run(mask)
    if band is None:
        raise RuntimeError("在 neriplayer.svg 渲染结果中未找到吉祥物色块")
    bbox = mask.crop((0, band[0], full_1024.width, band[1])).getbbox()
    if bbox is None:
        raise RuntimeError("吉祥物行带内没有有效像素")
    x0, y0, x1, y1 = bbox
    pad_x = int((x1 - x0) * 0.06)
    pad_y = int((y1 - y0) * 0.06)
    x0, y0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
    x1, y1 = min(full_1024.width, x1 + pad_x), min(full_1024.height, y1 + pad_y)
    w, h = x1 - x0, y1 - y0
    side = max(w, h)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    left = max(0, min(full_1024.width - side, cx - side // 2))
    top = max(0, min(full_1024.height - side, cy - side // 2))
    return full_1024.crop((left, top, left + side, top + side))


def _rounded_tile(size: int, color: tuple[int, int, int, int]):
    """圆角纯色底(4x 超采样抗锯齿),圆角比例对齐 ic_neriplayer 观感。"""
    from PIL import Image, ImageDraw

    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    draw = ImageDraw.Draw(mask)
    radius = int(size * scale * 0.225)
    draw.rounded_rectangle(
        [0, 0, size * scale - 1, size * scale - 1], radius=radius, fill=255
    )
    mask = mask.resize((size, size), Image.LANCZOS)
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(color, (0, 0), mask)
    return tile


def _mascot_tile(size: int, mascot, tile_color) -> "Image.Image":
    """深/浅圆角底 + 居中吉祥物(占边长约 68%)。"""
    from PIL import Image

    inner = int(size * 0.68)
    mascot_scaled = mascot.resize((inner, inner), Image.LANCZOS)
    tile = _rounded_tile(size, tile_color)
    offset = (size - inner) // 2
    tile.alpha_composite(mascot_scaled, (offset, offset))
    return tile


def build_raster_assets() -> list[Path]:
    from PIL import Image

    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    full_logo_svg = ICON_SVG_DIR / "neriplayer.svg"
    if not full_logo_svg.exists():
        raise FileNotFoundError(f"缺参考资产:{full_logo_svg}")

    render_1024 = _render_png(full_logo_svg, 1024)
    mascot = _mascot_crop(render_1024)

    written: list[Path] = []

    # -- app.ico:小尺寸(16/32/48/64)用 mascot 圆角底,大尺寸(128/256)用完整字标
    frames: dict[int, Image.Image] = {}
    for size in (16, 32, 48, 64):
        frames[size] = _mascot_tile(size, mascot, _TILE_DARK)
    for size in (128, 256):
        frames[size] = _render_png(full_logo_svg, size)
    ico_path = ASSETS_DIR / "app.ico"
    frames[256].save(
        ico_path,
        format="ICO",
        append_images=[frames[s] for s in (16, 32, 48, 64, 128)],
        sizes=[(s, s) for s in (16, 32, 48, 64, 128, 256)],
    )
    written.append(ico_path)

    # -- 托盘 32x32 深浅两版
    tray_dark = ASSETS_DIR / "tray_dark.png"
    tray_light = ASSETS_DIR / "tray_light.png"
    _mascot_tile(32, mascot, _TILE_DARK).save(tray_dark, format="PNG")
    _mascot_tile(32, mascot, _TILE_LIGHT).save(tray_light, format="PNG")
    written.extend([tray_dark, tray_light])
    return written


def main() -> int:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    for path in convert_drawables():
        print(f"[svg] {path.relative_to(REPO_ROOT)}")
    for path in write_material_symbols():
        print(f"[svg] {path.relative_to(REPO_ROOT)}")
    if "--skip-raster" in sys.argv:
        print("[skip] 位图资产(--skip-raster)")
        return 0
    for path in build_raster_assets():
        print(f"[raster] {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
