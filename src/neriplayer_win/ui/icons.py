"""图标加载与主题染色(M4)。

染色方案(简单可靠,QIcon+QSS 无法直接给 SVG 换色,故选 QPainter):

1. QIcon 原生读取 assets/icons/ 下的 SVG(PySide6 自带 QtSvg,零依赖);
2. 单色图标先以透明 QPixmap 为画布把 SVG 画上去,再切
   QPainter.CompositionMode_SourceIn 盖一层纯色 —— 结果是按图标
   alpha 裁剪的纯色图,即"矢量蒙版染色";
3. 染色结果 QIcon 按 (图标名, 颜色 RGBA) 缓存,主题切换时
   clear_cache() 后各控件 retheme() 重取。

品牌标(bilibili/netease)为固定多色,用 plain_icon 原样加载不染色。
资产由 scripts/make_assets.py 生成(含许可头),路径经 Path(__file__)
解析,不用 .qrc(docs/UI_ASSETS.md C6)。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

from . import theme

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
ICONS_DIR = ASSETS_DIR / "icons"

# 生成脚本落地的全部图标名(测试逐个断言非空用)
ICON_NAMES = (
    "play",
    "pause",
    "skip_previous",
    "skip_next",
    "volume_up",
    "favorite",
    "favorite_border",
    "shuffle",
    "search",
    "library_music",
    "refresh",
    "queue_music",
    "settings",
    "repeat",
    "repeat_one",
    "playlist_play",
    "person",
    "bilibili",
    "netease",
)
BRAND_ICON_NAMES = ("bilibili", "netease")

_RENDER_SIZE = 64  # 染色画布尺寸; QIcon 缩放到控件尺寸足够清晰
_cache: dict[tuple[str, int], QIcon] = {}


def icon_path(name: str) -> Path:
    return ICONS_DIR / f"{name}.svg"


def plain_icon(name: str) -> QIcon:
    """原样加载(品牌标等多色图标);不存在时返回空 QIcon。"""
    return QIcon(str(icon_path(name)))


def tinted_icon(
    name: str, role: str = "onSurfaceVariant", size: int = _RENDER_SIZE
) -> QIcon:
    """SVG 蒙版染色:取当前主题色板 role 颜色,返回该纯色的 QIcon。

    同 (name, 颜色) 结果缓存;QColor 支持 alpha(空状态淡色图标用)。
    """
    color = QColor(theme.current_palette().get(role, "#FFFFFF"))
    return tinted_icon_with_color(name, color, size)


def tinted_icon_with_color(
    name: str, color: QColor, size: int = _RENDER_SIZE
) -> QIcon:
    key = (name, color.rgba())
    cached = _cache.get(key)
    if cached is not None:
        return cached
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        QIcon(str(icon_path(name))).paint(painter, 0, 0, size, size)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(0, 0, size, size, color)
    finally:
        painter.end()
    # 显式注册全部模式:分区头等 NoItemFlags 条目会以 Disabled 模式渲染,
    # QIcon 若只有 Normal pixmap,Qt 会自动生成"禁用灰"版本(品牌标
    # 恒灰的根因)。四种模式给同一张图,渲染始终是染色结果。
    icon = QIcon()
    for mode in (
        QIcon.Mode.Normal,
        QIcon.Mode.Active,
        QIcon.Mode.Disabled,
        QIcon.Mode.Selected,
    ):
        icon.addPixmap(pixmap, mode)
    _cache[key] = icon
    return icon


def clear_cache() -> None:
    """主题切换后清空染色缓存(配合各控件 retheme 重取)。"""
    _cache.clear()


def app_icon() -> QIcon:
    """应用图标(assets/app.ico);资产缺失时回退运行时占位图。"""
    icon = QIcon(str(ASSETS_DIR / "app.ico"))
    if not icon.isNull():
        return icon
    from .tray import build_placeholder_icon  # 局部导入避免环

    return build_placeholder_icon()


def tray_icon(theme_name: str) -> QIcon:
    """托盘图标:随主题选深/浅版 32x32 PNG;缺失回退占位图。"""
    file_name = "tray_light.png" if theme_name == theme.THEME_LIGHT else "tray_dark.png"
    icon = QIcon(str(ASSETS_DIR / file_name))
    if not icon.isNull():
        return icon
    from .tray import build_placeholder_icon

    return build_placeholder_icon()
