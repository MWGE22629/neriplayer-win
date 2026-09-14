"""全局媒体键(M3,Windows)。

两层策略:
1. **RegisterHotKey 线程热键**(主路径):向本 GUI 线程注册
   VK_MEDIA_PLAY_PAUSE / NEXT / PREV / STOP,焦点在任意进程时均触发,
   WM_HOTKEY(0x0312) 作为线程消息进入 Qt 消息循环,由原生过滤器接住。
   热键被系统拦截后不会再产生 WM_APPCOMMAND,天然无双发。
2. **WM_APPCOMMAND 过滤**(回退):任一热键被其他程序占用导致注册失败时,
   撤销已注册项、回退到仅前台生效的 WM_APPCOMMAND(0x0219)方案。

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
WM_HOTKEY = 0x0312
APPCOMMAND_MEDIA_STOP = 13
APPCOMMAND_MEDIA_PLAY_PAUSE = 14
APPCOMMAND_MEDIA_NEXTTRACK = 571
APPCOMMAND_MEDIA_PREVTRACK = 570

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

# Windows SDK: FAPPCOMMAND_MASK(0xF000)——HIWORD 高 4 位是鼠标/键盘
# 附加标记,GET_APPCOMMAND_LPARAM 取低 12 位(见 extract_appcommand)
_FAPPCOMMAND_MASK = 0xF000

# 热键 id -> 统一命令名(RegisterHotKey 的 id 只要线程内唯一即可)
_HOTKEY_MAP = {
    1: "play_pause",
    2: "next",
    3: "prev",
    4: "stop",
}
_HOTKEY_VKS = {
    1: VK_MEDIA_PLAY_PAUSE,
    2: VK_MEDIA_NEXT_TRACK,
    3: VK_MEDIA_PREV_TRACK,
    4: VK_MEDIA_STOP,
}

_APPCOMMAND_MAP = {
    APPCOMMAND_MEDIA_PLAY_PAUSE: "play_pause",
    APPCOMMAND_MEDIA_NEXTTRACK: "next",
    APPCOMMAND_MEDIA_PREVTRACK: "prev",
    APPCOMMAND_MEDIA_STOP: "stop",
}


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
    """原生消息过滤;命中媒体命令时回调 dispatch(cmd)。

    hotkeys_active 为 True 时只认 WM_HOTKEY(全局热键路径),
    并忽略 WM_APPCOMMAND 的媒体命令以防双发;False 时反之。
    """

    def __init__(self, dispatch, hotkeys_active) -> None:
        super().__init__()
        self._dispatch = dispatch
        self._hotkeys_active = hotkeys_active

    def set_hotkeys_active(self, active: bool) -> None:
        self._hotkeys_active = active

    def nativeEventFilter(self, eventType, message):  # noqa: N802 - Qt 命名
        try:
            if bytes(eventType) != b"windows_generic_MSG":
                return False, 0
            msg = _MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and self._hotkeys_active:
                command = _HOTKEY_MAP.get(int(msg.wParam))
                if command is not None:
                    self._dispatch(command)
                    return True, 0
                return False, 0
            if msg.message == WM_APPCOMMAND and not self._hotkeys_active:
                command = _APPCOMMAND_MAP.get(extract_appcommand(msg.lParam))
                if command is not None:
                    self._dispatch(command)
                    return True, 0  # 吞掉,不再让其他程序响应
            return False, 0
        except Exception:  # noqa: BLE001 - 原生过滤内绝不抛
            return False, 0


class MediaKeyHandler(QObject):
    """媒体键信号源;构造时注册全局热键并安装原生过滤器。

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
        self._user32 = ctypes.windll.user32
        self._registered_ids: list[int] = []
        hotkeys_ok = self._register_media_hotkeys()
        self._filter = _AppCommandFilter(self._dispatch, hotkeys_ok)
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._filter)

    def _register_media_hotkeys(self) -> bool:
        """全量注册媒体热键;任一失败则全部撤销并返回 False(回退 APPCOMMAND)。"""
        for hotkey_id, vk in _HOTKEY_VKS.items():
            if self._user32.RegisterHotKey(None, hotkey_id, 0, vk):
                self._registered_ids.append(hotkey_id)
            else:
                self._unregister_hotkeys()
                return False
        return True

    def _unregister_hotkeys(self) -> None:
        for hotkey_id in self._registered_ids:
            self._user32.UnregisterHotKey(None, hotkey_id)
        self._registered_ids = []

    def stop(self) -> None:
        """退出前清理(进程退出时系统也会自动回收线程热键)。"""
        self._unregister_hotkeys()

    @property
    def hotkeys_active(self) -> bool:
        """全局热键是否注册成功(True=全局生效,False=回退前台 APPCOMMAND)。"""
        return bool(self._registered_ids)

    def _dispatch(self, command: str) -> None:
        if command == "play_pause":
            self.play_pause_requested.emit()
        elif command == "next":
            self.next_requested.emit()
        elif command == "prev":
            self.prev_requested.emit()
        elif command == "stop":
            self.stop_requested.emit()
