"""全局媒体键(M3,Windows):QAbstractNativeEventFilter 过滤 WM_APPCOMMAND。

Windows 上键盘媒体键 / 耳机线控会以 WM_APPCOMMAND(0x0219) 投递给焦点
窗口;native event filter 能看到本进程所有窗口的原生消息,因此应用在前台
(或任何本进程窗口有焦点)时媒体键均可被拦截,返回 True 吞掉避免其他程序
再次响应。

限制(已知,留待后续里程碑):本应用完全在后台、焦点属于其他进程时,
WM_APPCOMMAND 不会投递给本进程,需要 RegisterShellHookWindow 才能做到
真正的全局后台捕获;当前按 M3 范围只做前台过滤。

注意:
- PySide6 的 nativeEventFilter 必须返回 (bool, result) 元组。
- filter 对象由 MediaKeyHandler 持有引用、MainWindow 持有 handler,
  防止被 GC(见过拦截器被 GC 导致失效的同型事故)。
"""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

WM_APPCOMMAND = 0x0219
APPCOMMAND_MEDIA_STOP = 13
APPCOMMAND_MEDIA_PLAY_PAUSE = 14
APPCOMMAND_MEDIA_NEXTTRACK = 571
APPCOMMAND_MEDIA_PREVTRACK = 570

# Windows SDK: FAPPCOMMAND_MASK(0xF000)——HIWORD 高 4 位是鼠标/键盘
# 附加标记,GET_APPCOMMAND_LPARAM 取低 12 位(见 extract_appcommand)
_FAPPCOMMAND_MASK = 0xF000


class _MSG(ctypes.Structure):
    """WIN32 MSG 的前 4 个字段(对齐方式与 64 位 C 一致)。"""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
    ]


def extract_appcommand(lparam: int) -> int:
    """对应 GET_APPCOMMAND_LPARAM 宏:取 HIWORD 的低 12 位。"""
    return (int(lparam) >> 16) & 0x0FFF


class _AppCommandFilter(QAbstractNativeEventFilter):
    """原生消息过滤;命中媒体命令时回调 dispatch(cmd)。"""

    def __init__(self, dispatch) -> None:
        super().__init__()
        self._dispatch = dispatch

    def nativeEventFilter(self, eventType, message):  # noqa: N802 - Qt 命名
        try:
            if bytes(eventType) != b"windows_generic_MSG":
                return False, 0
            msg = _MSG.from_address(int(message))
            if msg.message != WM_APPCOMMAND:
                return False, 0
            command = extract_appcommand(msg.lParam)
            if command in (
                APPCOMMAND_MEDIA_PLAY_PAUSE,
                APPCOMMAND_MEDIA_STOP,
                APPCOMMAND_MEDIA_NEXTTRACK,
                APPCOMMAND_MEDIA_PREVTRACK,
            ):
                self._dispatch(command)
                return True, 0  # 吞掉,不再让其他程序响应
            return False, 0
        except Exception:  # noqa: BLE001 - 原生过滤内绝不抛
            return False, 0


class MediaKeyHandler(QObject):
    """媒体键信号源;构造时向 QApplication 安装原生过滤器。

    仅 Windows 可用;其他平台构造抛 RuntimeError。
    """

    play_pause_requested = Signal()
    stop_requested = Signal()
    next_requested = Signal()
    prev_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if sys.platform != "win32":
            raise RuntimeError("MediaKeyHandler 仅支持 Windows")
        self._filter = _AppCommandFilter(self._dispatch)
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._filter)

    def _dispatch(self, command: int) -> None:
        if command == APPCOMMAND_MEDIA_PLAY_PAUSE:
            self.play_pause_requested.emit()
        elif command == APPCOMMAND_MEDIA_STOP:
            self.stop_requested.emit()
        elif command == APPCOMMAND_MEDIA_NEXTTRACK:
            self.next_requested.emit()
        elif command == APPCOMMAND_MEDIA_PREVTRACK:
            self.prev_requested.emit()
