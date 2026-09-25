"""UI 后台线程辅助:把网络调用丢进工作线程,结果经 Qt 信号回到 UI 线程。

用法:
    run_async(fn, on_done=..., on_error=...)

- 禁止在 UI 线程做任何网络请求;所有 NeteaseClient 调用都经此处。
- 信号为自动连接:工作线程 emit → UI 线程槽(队列投递),线程安全。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from ..log import get_logger

_log = get_logger("workers")


class _AsyncRelay(QObject):
    done = Signal(object)
    failed = Signal(str)


_active_lock = threading.Lock()
_active: set[tuple[_AsyncRelay, threading.Thread]] = set()


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """在工作线程执行 fn;成功回调 on_done(结果),异常回调 on_error(中文消息)。"""
    relay = _AsyncRelay()
    if on_done is not None:
        relay.done.connect(on_done)
    if on_error is not None:
        relay.failed.connect(on_error)

    def _wrapper() -> None:
        try:
            result = fn()
        except Exception as error:  # noqa: BLE001 - 边界处统一转消息
            _log.exception("后台任务失败:%s", error.__class__.__name__)
            relay.failed.emit(str(error) or error.__class__.__name__)
        else:
            relay.done.emit(result)

    thread = threading.Thread(target=_wrapper, daemon=True)
    with _active_lock:
        _active.add((relay, thread))

    def _cleanup() -> None:
        with _active_lock:
            _active.discard((relay, thread))

    if on_done is not None:
        relay.done.connect(lambda _result: _cleanup())
    if on_error is not None:
        relay.failed.connect(lambda _message: _cleanup())

    thread.start()
