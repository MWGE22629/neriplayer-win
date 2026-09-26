"""Windows 任务栏缩略图工具栏(M5):预览框底部的 上一首/播放暂停/下一首。

鼠标悬停任务栏图标弹出的窗口预览框(Aero Peek)自带一条小工具栏,
经 ITaskbarList3::ThumbBarAddButtons 注册(最多 7 个按钮);点击不经过
主窗口,系统直接向窗口发 WM_COMMAND(LOORD(wParam)=按钮 id,
HIWORD(wParam)=THBN_CLICKED),由原生过滤器接住转信号。

PySide6 无对应封装,按仓库「轻量优先」用 ctypes 直调 COM(不引 pywin32):
- ole32.CLSIDFromString + CoCreateInstance 拿 ITaskbarList3,vtable 槽位
  (含 IUnknown/ITaskbarList/ITaskbarList2):Release=2 / HrInit=3 /
  ThumbBarAddButtons=15 / ThumbBarUpdateButtons=16;
- 图标:现有 SVG 资产按系统明暗(AppsUseLightTheme)染成 QIcon,
  经 CreateDIBSection + CreateIconIndirect 转成 HICON,进程存活期间
  不销毁(工具栏持有图标句柄);
- WM_COMMAND 过滤与 media_keys 同套路(QAbstractNativeEventFilter,
  filter 由本对象持有引用防 GC)。

失败策略与托盘一致:非 Windows / COM 不可用 / 附加失败(窗口尚无任务栏
按钮)一律静默降级为空操作,绝不影响主流程;attach 每次 showEvent 重试,
成功一次即固定。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import winreg

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QSize, Signal
from PySide6.QtGui import QColor, QIcon, QImage
from PySide6.QtWidgets import QApplication

from ..i18n import tr
from .icons import tinted_icon_with_color
from .media_keys import _MSG

WM_COMMAND = 0x0111
# commctrl.h THBN_CLICKED = (0u-1801u) = 0xFFFFF8FF;进 HIWORD 16 位即 0xF8FF
_THBN_CLICKED = 0xF8FF

# 按钮命令 id(WM_COMMAND 的 LOWORD);避开 Qt 菜单/控件常用的低位段
_ID_PREV = 0x8001
_ID_PLAY_PAUSE = 0x8002
_ID_NEXT = 0x8003

# THUMBBUTTON mask / flags(commctrl.h)
_THB_ICON = 0x2
_THB_TOOLTIP = 0x4
_THB_FLAGS = 0x8
_THBF_ENABLED = 0x0
_THBF_DISABLED = 0x1

# ITaskbarList3 vtable 槽位(0-2 为 IUnknown)
_VT_RELEASE = 2
_VT_HR_INIT = 3
_VT_THUMB_BAR_ADD = 15
_VT_THUMB_BAR_UPDATE = 16

_CLSID_TaskbarList = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
_IID_ITaskbarList3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"

_SM_CXSMICON = 49  # GetSystemMetrics:小图标尺寸(随系统 DPI 缩放)


class _GUID(ctypes.Structure):
    """win32 GUID(ctypes.wintypes 部分版本无 GUID,自定义等价布局)。"""

    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _THUMBBUTTON(ctypes.Structure):
    """commctrl.h THUMBBUTTON;字段对齐与 64 位 C 一致(2+2pad+4+4+8+520+4+4pad)。"""

    _fields_ = [
        ("dwMask", ctypes.c_uint16),
        ("iId", ctypes.c_uint32),
        ("iBitmap", ctypes.c_uint32),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 260),
        ("dwFlags", ctypes.c_uint32),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    # bmiColors 占 1 个 DWORD:32bpp BI_RGB 不查颜色表,纯对齐占位
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", ctypes.c_uint32 * 1),
    ]


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wt.BOOL),
        ("xHotspot", wt.DWORD),
        ("yHotspot", wt.DWORD),
        ("hbmMask", wt.HBITMAP),
        ("hbmColor", wt.HBITMAP),
    ]


def _system_taskbar_light() -> bool:
    """任务栏/预览页的系统明暗:预览页底色跟随 SystemUsesLightTheme
    (任务栏主题),不是 AppsUseLightTheme;图标配色以它为准。读不到时
    依次回落 AppsUseLightTheme / 浅色。"""
    for value_name in ("SystemUsesLightTheme", "AppsUseLightTheme"):
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
        except OSError:
            break
        try:
            value, _ = winreg.QueryValueEx(key, value_name)
            return bool(value)
        except OSError:
            continue
        finally:
            winreg.CloseKey(key)
    return True


def _hicon_from_icon(icon: QIcon, size: int) -> int | None:
    """QIcon -> HICON:按 size 取 ARGB32 位图,经 32bpp top-down DIB +
    CreateIconIndirect 合成(带逐像素 alpha)。失败返回 None。"""
    pixmap = icon.pixmap(QSize(size, size))
    if pixmap.isNull():
        return None
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()
    if width <= 0 or height <= 0:
        return None
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # 负高 = top-down,与 QImage 行序一致
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    bits = ctypes.c_void_p()
    color = gdi32.CreateDIBSection(
        None, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
    )
    if not color or not bits.value:
        return None
    try:
        source = image.constBits()
        data = source.tobytes() if hasattr(source, "tobytes") else bytes(source)
        stride = image.bytesPerLine()
        row_bytes = width * 4
        if stride == row_bytes:
            ctypes.memmove(bits.value, data, row_bytes * height)
        else:  # 行尾有对齐填充时逐行拷贝
            for row in range(height):
                ctypes.memmove(
                    bits.value + row * row_bytes,
                    data[row * stride : row * stride + row_bytes],
                    row_bytes,
                )
        mask = gdi32.CreateBitmap(width, height, 1, 1, None)
        if not mask:
            return None
        try:
            info = _ICONINFO()
            info.fIcon = True
            info.hbmMask = mask
            info.hbmColor = color
            hicon = user32.CreateIconIndirect(ctypes.byref(info))
            return int(hicon) if hicon else None
        finally:
            gdi32.DeleteObject(mask)
    finally:
        gdi32.DeleteObject(color)


class _ThumbBarFilter(QAbstractNativeEventFilter):
    """接 WM_COMMAND(按钮点击)与 TaskbarButtonCreated(任务栏按钮就绪)。

    任务栏按钮就绪消息只在按钮真正建立时送达一次,晚于首次 showEvent,
    这是附加失败后自动重试的主路径;不吞掉该消息,Qt 侧无感。
    """

    def __init__(self, owner: "TaskbarThumbBar") -> None:
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, eventType, message):  # noqa: N802 - Qt 命名
        try:
            if bytes(eventType) != b"windows_generic_MSG":
                return False, 0
            msg = _MSG.from_address(int(message))
            if (
                self._owner._created_msg
                and msg.message == self._owner._created_msg
                and int(msg.hwnd or 0) == self._owner._watch_hwnd
            ):
                self._owner._on_taskbar_created()
                return False, 0
            if msg.message != WM_COMMAND:
                return False, 0
            if int(msg.hwnd or 0) != self._owner.hwnd:
                return False, 0
            if ((msg.wParam >> 16) & 0xFFFF) != _THBN_CLICKED:
                return False, 0
            command_id = msg.wParam & 0xFFFF
            if self._owner._dispatch(command_id):
                return True, 0
            return False, 0
        except Exception:  # noqa: BLE001 - 原生过滤内绝不抛
            return False, 0


class TaskbarThumbBar(QObject):
    """任务栏缩略图工具栏控制器;按钮点击以信号交给 MainWindow 执行。"""

    prev_requested = Signal()
    play_pause_requested = Signal()
    next_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._hwnd = 0
        self._watch_hwnd = 0  # 等待任务栏按钮就绪消息的窗口
        self._created_msg = 0  # RegisterWindowMessage("TaskbarButtonCreated")
        self._attached = False
        self._playing = False
        self._enabled = False
        self._icons: dict[str, int] = {}  # 图标名 -> HICON(存活期间不销毁)
        self._punk: int | None = None
        self._add_buttons = None
        self._update_buttons = None
        self._release = None
        self._filter: _ThumbBarFilter | None = None
        if sys.platform == "win32":
            self._created_msg = ctypes.windll.user32.RegisterWindowMessageW(
                "TaskbarButtonCreated"
            )
            self._init_com()
            self._filter = _ThumbBarFilter(self)
            app = QApplication.instance()
            if app is not None:
                app.installNativeEventFilter(self._filter)

    # -- 属性 ----------------------------------------------------------------

    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def attached(self) -> bool:
        return self._attached

    # -- COM 基础 ------------------------------------------------------------

    def _init_com(self) -> None:
        """CoCreateInstance(ITaskbarList3) 并备好三个 vtable 调用;失败即禁用。"""
        try:
            ole32 = ctypes.oledll.ole32
            # 真实 windows 平台 Qt 已做 OLE 初始化;离屏等场景没有,
            # 自己补 STA(S_OK/S_FALSE 均可用;已按其他模式初始化也可用,
            # 进程存活期间不配对 CoUninitialize,交由进程退出回收)
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
            clsid = _GUID()
            iid = _GUID()
            ole32.CLSIDFromString(_CLSID_TaskbarList, ctypes.byref(clsid))
            ole32.CLSIDFromString(_IID_ITaskbarList3, ctypes.byref(iid))
            punk = ctypes.c_void_p()
            # CLSCTX_INPROC_SERVER = 1
            ole32.CoCreateInstance(
                ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(punk)
            )
            if not punk.value:
                return

            def vfunc(slot: int, restype, *argtypes):
                vtable = ctypes.cast(
                    punk, ctypes.POINTER(ctypes.c_void_p)
                ).contents.value
                fn_addr = ctypes.cast(
                    vtable + slot * ctypes.sizeof(ctypes.c_void_p),
                    ctypes.POINTER(ctypes.c_void_p),
                ).contents.value
                return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn_addr)

            hr_init = vfunc(_VT_HR_INIT, ctypes.c_long)
            hr_init(punk.value)  # HrInit:其他方法的前置
            self._release = vfunc(_VT_RELEASE, ctypes.c_long)
            self._add_buttons = vfunc(
                _VT_THUMB_BAR_ADD,
                ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
            )
            self._update_buttons = vfunc(
                _VT_THUMB_BAR_UPDATE,
                ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
            )
            self._punk = punk.value
        except Exception:  # noqa: BLE001 - 增强能力,失败不影响主流程
            self._punk = None

    # -- 附加与状态同步 --------------------------------------------------------

    def attach(self, hwnd: int) -> None:
        """向窗口注册缩略图按钮(需窗口已有任务栏按钮);成功一次即固定。

        首次尝试通常早于任务栏按钮建立而失败,失败后转为监听
        TaskbarButtonCreated 消息,按钮就绪即自动重试。
        """
        if self._attached or self._punk is None or not hwnd:
            return
        self._watch_hwnd = hwnd
        try:
            self._build_icons()
            buttons = self._make_buttons()
            # vtable 函数首参为 this(COM 指针),其后才是接口参数
            hr = self._add_buttons(self._punk, hwnd, 3, ctypes.byref(buttons))
            if hr == 0:
                self._hwnd = hwnd
                self._attached = True
                self._refresh_buttons()  # 补发当前播放/禁用态
        except Exception:  # noqa: BLE001 - 等任务栏按钮就绪消息再重试
            return

    def _on_taskbar_created(self) -> None:
        """任务栏按钮(重)建立:Explorer 重启或窗口从托盘隐藏后重新
        显示都会触发,旧按钮上的注册随按钮销毁失效,必须强制重加。

        若消息实为按钮未重建时的重复送达(Add 只允许一次会被拒),
        回落 Update 恢复图标/状态,注册仍然有效。"""
        hwnd = self._watch_hwnd
        if not hwnd or self._punk is None:
            return
        was_attached = self._attached
        self._attached = False
        self._hwnd = 0
        self.attach(hwnd)
        if not self._attached and was_attached:
            self._attached = True
            self._hwnd = hwnd
            self._refresh_buttons()

    def set_enabled(self, enabled: bool) -> None:
        """有无在播曲目:三键整体可用/禁用(未附加时只记状态)。"""
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._refresh_buttons()

    def set_playing(self, playing: bool) -> None:
        """播放态:中键图标/提示在 播放<->暂停 间切换。"""
        if playing == self._playing:
            return
        self._playing = playing
        self._refresh_buttons()

    def retranslate(self) -> None:
        """语言切换:按钮 tooltip 重取文案。"""
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        if not self._attached or self._update_buttons is None:
            return
        try:
            buttons = self._make_buttons()
            self._update_buttons(self._punk, self._hwnd, 3, ctypes.byref(buttons))
        except Exception:  # noqa: BLE001 - 状态同步失败静默(UI 内仍有等价控制)
            return

    # -- 按钮与图标 ------------------------------------------------------------

    def _build_icons(self) -> None:
        if self._icons:
            return
        color = QColor("#1F1F1F" if _system_taskbar_light() else "#FFFFFF")
        size = ctypes.windll.user32.GetSystemMetrics(_SM_CXSMICON) or 16
        for name in ("skip_previous", "play", "pause", "skip_next"):
            hicon = _hicon_from_icon(tinted_icon_with_color(name, color), size)
            if hicon is not None:
                self._icons[name] = hicon

    def _make_buttons(self) -> ctypes.Array:
        flags = _THBF_ENABLED if self._enabled else _THBF_DISABLED

        def button(command_id: int, icon_name: str, tip: str) -> _THUMBBUTTON:
            item = _THUMBBUTTON()
            item.dwMask = _THB_ICON | _THB_TOOLTIP | _THB_FLAGS
            item.iId = command_id
            item.hIcon = self._icons.get(icon_name)
            item.szTip = tip[:259]
            item.dwFlags = flags
            return item

        play_name = "pause" if self._playing else "play"
        play_tip = tr("player.pause" if self._playing else "player.play")
        buttons = (_THUMBBUTTON * 3)(
            button(_ID_PREV, "skip_previous", tr("player.prev")),
            button(_ID_PLAY_PAUSE, play_name, play_tip),
            button(_ID_NEXT, "skip_next", tr("player.next")),
        )
        return buttons

    def _dispatch(self, command_id: int) -> bool:
        if command_id == _ID_PREV:
            self.prev_requested.emit()
        elif command_id == _ID_PLAY_PAUSE:
            self.play_pause_requested.emit()
        elif command_id == _ID_NEXT:
            self.next_requested.emit()
        else:
            return False
        return True

    # -- 清理 ----------------------------------------------------------------

    def shutdown(self) -> None:
        """退出清理:卸过滤器、销毁图标、Release COM;窗口销毁时工具栏随之消失。"""
        if self._filter is not None:
            app = QApplication.instance()
            if app is not None:
                try:
                    app.removeNativeEventFilter(self._filter)
                except Exception:  # noqa: BLE001
                    pass
            self._filter = None
        user32 = ctypes.windll.user32
        for hicon in self._icons.values():
            user32.DestroyIcon(hicon)
        self._icons.clear()
        if self._punk is not None and self._release is not None:
            try:
                self._release(self._punk)
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass
            self._punk = None
        self._attached = False
