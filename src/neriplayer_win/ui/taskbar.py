"""Windows 任务栏缩略图工具栏(M5):预览框底部的 上一首/播放暂停/下一首。

鼠标悬停任务栏图标弹出的窗口预览框(Aero Peek)自带一条小工具栏,
经 ITaskbarList3::ThumbBarAddButtons 注册(最多 7 个按钮);点击不经过
主窗口,系统直接向窗口发 WM_COMMAND(LOWORD(wParam)=按钮 id,
HIWORD(wParam)=THBN_CLICKED)。

PySide6 无对应封装,按仓库「轻量优先」用 ctypes 直调 COM(不引 pywin32):
- ole32.CLSIDFromString + CoCreateInstance 拿 ITaskbarList3,vtable 槽位
  (含 IUnknown/ITaskbarList/ITaskbarList2):Release=2 / HrInit=3 /
  ThumbBarAddButtons=15 / ThumbBarUpdateButtons=16;
- 图标:现有 SVG 资产按系统明暗(SystemUsesLightTheme,预览页随系统
  主题)染成 QIcon,经 CreateDIBSection + CreateIconIndirect 转成 HICON,
  进程存活期间不销毁(工具栏持有图标句柄);
- 消息路由走 **QWidget.nativeEvent(窗口过程层)**:缩略图按钮点击是
  Explorer 跨进程 SendMessage 直发窗口过程的,应用级
  QAbstractNativeEventFilter 只看得到消息队列里的消息、看不到直发
  消息(实测 PostMessage 可见 / 跨线程 SendMessage 不可见)——
  这也是 WPF(HwndSource.AddHook)与 Chromium(HWNDMessageHandler)
  处理 THBN_CLICKED 的同一位置。MainWindow.nativeEvent 把原始消息
  交给 handle_native_event 解析分发。

失败策略与托盘一致:非 Windows / COM 不可用 / 附加失败(窗口尚无任务栏
按钮)一律静默降级为空操作,绝不影响主流程;attach 在每次 showEvent
重试,TaskbarButtonCreated 消息(任务栏按钮就绪/重建)到达时再重试。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import winreg

from PySide6.QtCore import QObject, QSize, Signal
from PySide6.QtGui import QColor, QIcon, QImage

from ..i18n import tr
from ..log import get_logger
from .icons import tinted_icon_with_color
from .media_keys import _MSG

_log = get_logger("taskbar")

WM_COMMAND = 0x0111
# commctrl.h 文档值 THBN_CLICKED = (0u-1801u),进 HIWORD 16 位即 0xF8F6。
# 真机日志(2026-09-26,Win11 26100)实测任务栏发 0x1800,与文档值不符
# ——故通知码不做门槛,仅按按钮 id 白名单匹配并留痕。

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
        self._native_seen = False  # 首条窗口过程消息留痕(通道接通证明)
        if sys.platform == "win32":
            self._created_msg = ctypes.windll.user32.RegisterWindowMessageW(
                "TaskbarButtonCreated"
            )
            self._init_com()

    def handle_native_event(self, event_type, message) -> bool:
        """窗口过程层消息入口(MainWindow.nativeEvent 转发原始参数)。

        处理两类消息:
        - TaskbarButtonCreated(任务栏按钮就绪/重建):触发附加重试,
          不吞消息;
        - WM_COMMAND(LOWORD=按钮 id):按 id 白名单分发并吞掉。通知码
          (HIWORD)不做门槛——真机实测 Win11 24H2 发 0x1800 而非文档值
          0xF8F6,仅留痕供观察;按钮 id 为我们独有,无误触风险。

        返回 True 表示消息已消费(nativeEvent 应短路 Qt 默认处理)。
        解析失败/非目标消息一律返回 False,绝不抛出。
        """
        try:
            if bytes(event_type) != b"windows_generic_MSG":
                return False
            msg = _MSG.from_address(int(message))
            if not self._native_seen:
                self._native_seen = True
                _log.info(
                    "nativeEvent 通道接通(首条消息 message=0x%X)", msg.message
                )
            if (
                self._created_msg
                and msg.message == self._created_msg
                and int(msg.hwnd or 0) == self._watch_hwnd
            ):
                _log.info("收到 TaskbarButtonCreated(hwnd=%s)", self._watch_hwnd)
                self._on_taskbar_created()
                return False  # 让 Qt 继续处理
            if msg.message != WM_COMMAND:
                return False
            hwnd = int(msg.hwnd or 0)
            hiword = (msg.wParam >> 16) & 0xFFFF
            loword = msg.wParam & 0xFFFF
            if hwnd != self._hwnd:
                # 目标窗口不是我们注册的:仍留痕(点击路由异常的排查线索)
                if loword in (_ID_PREV, _ID_PLAY_PAUSE, _ID_NEXT):
                    _log.info(
                        "缩略图点击发到别的窗口(hwnd=%s 期望=%s id=0x%X)",
                        hwnd, self._hwnd, loword,
                    )
                return False
            dispatched = self._dispatch(loword)
            _log.info(
                "缩略图按钮点击 id=0x%X hi=0x%X %s(hwnd=%s, attached=%s)",
                loword, hiword,
                "已分发" if dispatched else "未知按钮(未分发)",
                hwnd, self._attached,
            )
            return dispatched
        except Exception as exc:  # noqa: BLE001 - 窗口过程内绝不抛
            _log.exception("消息解析异常:%s", exc)
            return False

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
            _log.info(
                "ITaskbarList3 就绪(TaskbarButtonCreated msg=0x%X)",
                self._created_msg,
            )
        except Exception as exc:  # noqa: BLE001 - 增强能力,失败不影响主流程
            self._punk = None
            _log.info("ITaskbarList3 初始化失败:%r", exc)

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
                _log.info(
                    "缩略图按钮注册成功(hwnd=%s, 图标%d个, enabled=%s)",
                    hwnd, len(self._icons), self._enabled,
                )
                self._refresh_buttons()  # 补发当前播放/禁用态
            else:
                _log.info(
                    "缩略图按钮注册被拒 hwnd=%s hr=0x%08X(等 TaskbarButtonCreated 重试)",
                    hwnd, hr & 0xFFFFFFFF,
                )
        except Exception as exc:  # noqa: BLE001 - 等任务栏按钮就绪消息再重试
            _log.info("缩略图按钮注册异常 hwnd=%s:%r", hwnd, exc)
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
            _log.info("任务栏按钮未重建,Add 被拒后回落 Update 恢复状态")
            self._refresh_buttons()
        elif self._attached:
            _log.info("任务栏按钮重建后重新注册完成(hwnd=%s)", hwnd)

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
            hr = self._update_buttons(self._punk, self._hwnd, 3, ctypes.byref(buttons))
            _log.info(
                "按钮状态刷新 hr=0x%08X(enabled=%s, playing=%s, hwnd=%s)",
                hr & 0xFFFFFFFF, self._enabled, self._playing, self._hwnd,
            )
        except Exception as exc:  # noqa: BLE001 - 状态同步失败静默(UI 内仍有等价控制)
            _log.info("按钮状态刷新异常:%r", exc)
            return

    # -- 按钮与图标 ------------------------------------------------------------

    def _build_icons(self) -> None:
        if self._icons:
            return
        light = _system_taskbar_light()
        color = QColor("#1F1F1F" if light else "#FFFFFF")
        size = ctypes.windll.user32.GetSystemMetrics(_SM_CXSMICON) or 16
        for name in ("skip_previous", "play", "pause", "skip_next"):
            hicon = _hicon_from_icon(tinted_icon_with_color(name, color), size)
            if hicon is not None:
                self._icons[name] = hicon
        _log.info(
            "图标构建:%s主题 %s, %dpx, %d个",
            "系统" + ("浅" if light else "深"), color.name(), size, len(self._icons),
        )

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
        """退出清理:销毁图标、Release COM;窗口销毁时工具栏随之消失。"""
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
