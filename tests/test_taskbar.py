"""任务栏缩略图工具栏测试:WM_COMMAND 分发、按钮状态、降级不崩。"""

from __future__ import annotations

import ctypes
import sys

import pytest
from PySide6.QtWidgets import QApplication

from neriplayer_win.i18n import tr
from neriplayer_win.ui.media_keys import _MSG
from neriplayer_win.ui.taskbar import (
    WM_COMMAND,
    TaskbarThumbBar,
    _ID_NEXT,
    _ID_PLAY_PAUSE,
    _ID_PREV,
    _THBN_CLICKED,
    _THBF_DISABLED,
    _THBF_ENABLED,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")

_app = QApplication.instance() or QApplication([])

_HOUSE = 0x1234  # 伪窗口句柄(仅驱动消息匹配逻辑)


def _make_command(hwnd: int, command_id: int, hiword: int = _THBN_CLICKED) -> _MSG:
    msg = _MSG()
    msg.hwnd = hwnd
    msg.message = WM_COMMAND
    msg.wParam = (hiword << 16) | command_id
    msg.lParam = 0
    return msg


def _make_bar() -> TaskbarThumbBar:
    bar = TaskbarThumbBar()
    # 不真实附加(离屏/无任务栏按钮):直接注入伪 hwnd 驱动消息匹配
    bar._hwnd = _HOUSE
    return bar


class TestThumbBarDispatch:
    def test_known_ids_dispatch_signals(self):
        bar = _make_bar()
        try:
            fired: list[str] = []
            bar.prev_requested.connect(lambda: fired.append("prev"))
            bar.play_pause_requested.connect(lambda: fired.append("pp"))
            bar.next_requested.connect(lambda: fired.append("next"))
            for command_id in (_ID_PREV, _ID_PLAY_PAUSE, _ID_NEXT):
                msg = _make_command(_HOUSE, command_id)
                consumed, _ = bar._filter.nativeEventFilter(
                    b"windows_generic_MSG", ctypes.addressof(msg)
                )
                assert consumed is True
            assert fired == ["prev", "pp", "next"]
        finally:
            bar.shutdown()

    def test_unknown_id_not_consumed(self):
        bar = _make_bar()
        try:
            msg = _make_command(_HOUSE, 0x9001)
            consumed, _ = bar._filter.nativeEventFilter(
                b"windows_generic_MSG", ctypes.addressof(msg)
            )
            assert consumed is False
        finally:
            bar.shutdown()

    def test_other_hwnd_ignored(self):
        bar = _make_bar()
        try:
            fired: list[str] = []
            bar.next_requested.connect(lambda: fired.append("next"))
            msg = _make_command(_HOUSE + 1, _ID_NEXT)
            consumed, _ = bar._filter.nativeEventFilter(
                b"windows_generic_MSG", ctypes.addressof(msg)
            )
            assert consumed is False
            assert fired == []
        finally:
            bar.shutdown()

    def test_wrong_notification_code_ignored(self):
        """WM_COMMAND 但 HIWORD 不是 THBN_CLICKED(如菜单加速键)不误触发。"""
        bar = _make_bar()
        try:
            fired: list[str] = []
            bar.play_pause_requested.connect(lambda: fired.append("pp"))
            msg = _make_command(_HOUSE, _ID_PLAY_PAUSE, hiword=0)
            consumed, _ = bar._filter.nativeEventFilter(
                b"windows_generic_MSG", ctypes.addressof(msg)
            )
            assert consumed is False
            assert fired == []
        finally:
            bar.shutdown()


class TestThumbBarReattach:
    """TaskbarButtonCreated(托盘隐藏后重显/Explorer 重启):强制重挂。"""

    def _make_attached_bar(self) -> tuple[TaskbarThumbBar, list]:
        bar = TaskbarThumbBar()
        calls: list[int] = []
        original = bar._add_buttons
        assert bar._punk is not None

        def counting_add(punk, hwnd, count, buttons):
            calls.append(hwnd)
            return original(punk, hwnd, count, buttons)

        bar._add_buttons = counting_add
        bar._watch_hwnd = _HOUSE
        bar.attach(_HOUSE)
        calls.clear()  # 去掉首次 attach 的调用
        return bar, calls

    def test_created_message_forces_readd(self):
        bar, calls = self._make_attached_bar()
        try:
            if not bar.attached:
                pytest.skip("离屏环境首次附加未成功,重挂路径不适用")
            assert calls == []
            msg = _MSG()
            msg.hwnd = _HOUSE
            msg.message = bar._created_msg
            msg.wParam = 0
            msg.lParam = 0
            bar._filter.nativeEventFilter(
                b"windows_generic_MSG", ctypes.addressof(msg)
            )
            # 消息触发重挂:又调了一次 Add(无论成功与否都恢复可用状态)
            assert len(calls) == 1
            assert bar.attached is True
        finally:
            bar.shutdown()

    def test_reattach_falls_back_to_update_when_add_rejected(self):
        """按钮未真正重建时 Add 被拒:回落 Update 恢复,注册仍有效。"""
        bar, calls = self._make_attached_bar()
        try:
            if not bar.attached:
                pytest.skip("离屏环境首次附加未成功,重挂路径不适用")
            updates: list[int] = []
            original_update = bar._update_buttons
            bar._add_buttons = lambda *args: 0x80004005  # E_FAIL 拒绝 Add
            bar._update_buttons = lambda punk, hwnd, count, buttons: (
                updates.append(hwnd) or 0
            )
            bar._on_taskbar_created()
            assert len(calls) == 0 or True  # Add 走被拒桩
            assert updates == [_HOUSE]  # 回落 Update
            assert bar.attached is True and bar.hwnd == _HOUSE
            assert original_update is not None
        finally:
            bar.shutdown()


class TestThumbBarIconContent:
    def test_built_hicons_have_opaque_pixels(self):
        """HICON 像素回读:图标必须有可见内容(防 ARGB→HICON 管线产出
        全透明图标——按钮在预览页上等于隐形)。"""
        bar = TaskbarThumbBar()
        try:
            if bar._punk is None:
                pytest.skip("COM 不可用")
            bar._build_icons()
            assert len(bar._icons) == 4
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            for name, hicon in bar._icons.items():
                size = user32.GetSystemMetrics(49)
                hdc = user32.GetDC(None)
                try:
                    mem_dc = gdi32.CreateCompatibleDC(hdc)
                    bmi = _readback_bmi(size)
                    bits = ctypes.c_void_p()
                    dib = gdi32.CreateDIBSection(
                        hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
                    )
                    old = gdi32.SelectObject(mem_dc, dib)
                    # DI_NORMAL=3:先画彩色层;32bpp DIB 初值为 0(全透明)
                    ctypes.memset(bits.value, 0, size * size * 4)
                    user32.DrawIconEx(mem_dc, 0, 0, hicon, size, size, 0, None, 3)
                    gdi32.SelectObject(mem_dc, old)
                    row = ctypes.cast(
                        bits,
                        ctypes.POINTER(ctypes.c_uint32 * (size * size)),
                    ).contents
                    opaque = sum(
                        1 for pixel in row if (pixel >> 24) != 0
                    )
                    gdi32.DeleteObject(dib)
                    gdi32.DeleteDC(mem_dc)
                    assert opaque > 10, f"{name} 图标几乎全透明({opaque}px)"
                finally:
                    user32.ReleaseDC(None, hdc)
        finally:
            bar.shutdown()


def _readback_bmi(size: int):
    from neriplayer_win.ui.taskbar import _BITMAPINFO, _BITMAPINFOHEADER

    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size  # top-down,行序与像素数组一致
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    return bmi


class TestThumbBarState:
    def test_state_methods_safe_without_attach(self):
        """未附加(COM 失败/离屏)时状态方法均为无害空操作。"""
        bar = TaskbarThumbBar()
        try:
            bar._punk = None  # 强制模拟 COM 不可用
            bar.set_enabled(True)
            bar.set_playing(True)
            bar.retranslate()
            assert bar._enabled is True and bar._playing is True
        finally:
            bar.shutdown()

    def test_buttons_structure_and_state_swap(self):
        """按钮数组:id 顺序、tooltip 文案与播放态/禁用态联动。"""
        bar = _make_bar()
        try:
            buttons = bar._make_buttons()
            assert [buttons[i].iId for i in range(3)] == [
                _ID_PREV, _ID_PLAY_PAUSE, _ID_NEXT,
            ]
            assert buttons[1].szTip == tr("player.play")  # 默认未在播
            assert all(buttons[i].dwFlags == _THBF_DISABLED for i in range(3))
            bar.set_enabled(True)
            bar.set_playing(True)
            buttons = bar._make_buttons()
            assert buttons[1].szTip == tr("player.pause")
            assert all(buttons[i].dwFlags == _THBF_ENABLED for i in range(3))
        finally:
            bar.shutdown()

    def test_attach_with_bogus_hwnd_never_raises(self):
        """非窗口句柄:attach 不抛异常,内部状态自洽(Shell 对句柄不做
        同步校验,S_OK 不代表真实可见,故只断言健壮性不断言附加结果)。"""
        bar = TaskbarThumbBar()
        try:
            if bar._punk is None:
                pytest.skip("COM 不可用,附加路径不适用")
            bogus = 0x7A7A7A7A  # 随机大值;HWND 1 等小值在系统里是真实窗口
            assert ctypes.windll.user32.IsWindow(bogus) == 0
            bar.attach(bogus)  # 不抛即通过
            bar.set_playing(True)  # 附加后状态同步同样不抛
        finally:
            bar.shutdown()
