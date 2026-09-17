"""全局媒体键测试:RegisterHotKey 注册、WM_HOTKEY 分发、APPCOMMAND 回退。"""

from __future__ import annotations

import ctypes
import sys

import pytest
from PySide6.QtWidgets import QApplication

from neriplayer_win.ui.media_keys import (
    _MSG,
    MediaKeyHandler,
    APPCOMMAND_MEDIA_PLAY_PAUSE,
    WM_APPCOMMAND,
    WM_HOTKEY,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")

_app = QApplication.instance() or QApplication([])


def _make_msg(message: int, wparam: int, lparam: int) -> _MSG:
    """构造 _MSG;调用方须将其保存到局部变量再用 addressof,防止缓冲被回收。"""
    msg = _MSG()
    msg.hwnd = None
    msg.message = message
    msg.wParam = wparam
    msg.lParam = lparam
    return msg


def test_global_hotkeys_registered():
    handler = MediaKeyHandler()
    try:
        # 本机无其他程序占用媒体键时应注册成功(全局生效);
        # 被占用则优雅回退——两种结果都合法,但注册数与 active 必须一致
        if handler.hotkeys_active:
            assert len(handler._registered_ids) == 4
    finally:
        handler.stop()
    assert not handler.hotkeys_active


def test_hotkey_message_dispatches_play_pause():
    handler = MediaKeyHandler()
    fired: list[str] = []
    handler.play_pause_requested.connect(lambda: fired.append("pp"))
    try:
        if not handler.hotkeys_active:
            pytest.skip("媒体键被其他程序占用(如应用本体正在运行),热键分发不适用")
        # 用实际注册到的 hotkey id,不写死 1(注册失败时集合为空)
        msg = _make_msg(WM_HOTKEY, handler._registered_ids[0], 0)
        consumed, _ = handler._filter.nativeEventFilter(
            b"windows_generic_MSG", ctypes.addressof(msg)
        )
        assert consumed is True
        assert fired == ["pp"]
    finally:
        handler.stop()


def test_appcommand_fallback_when_hotkeys_off():
    handler = MediaKeyHandler()
    fired: list[str] = []
    handler.prev_requested.connect(lambda: fired.append("prev"))
    try:
        handler._filter.set_hotkeys_active(False)
        msg = _make_msg(WM_APPCOMMAND, 0, (APPCOMMAND_MEDIA_PLAY_PAUSE & 0x0FFF) << 16)
        consumed, _ = handler._filter.nativeEventFilter(
            b"windows_generic_MSG", ctypes.addressof(msg)
        )
        assert consumed is True
        assert fired == []
        msg2 = _make_msg(WM_APPCOMMAND, 0, (570 & 0x0FFF) << 16)  # PREVTRACK
        handler._filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg2))
        assert fired == ["prev"]
    finally:
        handler.stop()


def test_hotkey_and_appcommand_no_double_fire():
    """热键模式激活时,APPCOMMAND 媒体命令必须被忽略(防双发)。"""
    handler = MediaKeyHandler()
    fired: list[str] = []
    handler.play_pause_requested.connect(lambda: fired.append("pp"))
    try:
        if not handler.hotkeys_active:
            pytest.skip("热键未注册成功,双发场景不适用")
        msg = _make_msg(WM_APPCOMMAND, 0, (APPCOMMAND_MEDIA_PLAY_PAUSE & 0x0FFF) << 16)
        consumed, _ = handler._filter.nativeEventFilter(
            b"windows_generic_MSG", ctypes.addressof(msg)
        )
        assert consumed is False
        assert fired == []
    finally:
        handler.stop()
