"""启动计时桩:从进程创建到窗口首显的毫秒级分段。

动机(2026-10-11):用户自开始菜单启动,点击到窗口可见近 10 秒,而同版本
测量口径冷启动中位 744ms,缺口无法用代码路径解释,必须用现场数据区分
「进程创建→首行 Python」(loader/DLL/杀软扫描,Python 之前的世界)与
「首行 Python→窗口首显」(我们的代码)。桩永久保留:远程用户再报
"启动慢"时,回传日志即可定位是哪一段。

口径:
- 锚点「进程创建」= GetProcessTimes 的 CreationTime(UTC FILETIME),
  可事后回溯,不受桩代码位置影响;与 GetSystemTimePreciseAsFileTime
  差分,精度远优于 1ms。
- 锚点「首行代码」= 包 __init__ 顶部的 perf_counter(),本包最早执行
  Python 字节码的时刻(此前是 Nuitka 运行时引导与 DLL 解析,其耗时
  体现在 锚点1→锚点2 的差里)。
- 输出走 app.log(INFO,[neriplayer.boot]),前缀「启动桩」;未初始化
  日志时静默(NullHandler 兜底,测试路径零输出)。
"""

from __future__ import annotations

import sys
import time

_first_python_t0: float | None = None
_show_reported = False


def mark_first_python() -> None:
    """包 __init__ 最早处调用,记下本包首行代码时刻(幂等)。"""
    global _first_python_t0
    if _first_python_t0 is None:
        _first_python_t0 = time.perf_counter()


def _filetime_now() -> int:
    import ctypes

    value = ctypes.c_ulonglong()
    ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(value))
    return value.value


def _process_create_filetime() -> int | None:
    import ctypes

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", ctypes.c_uint32),
            ("dwHighDateTime", ctypes.c_uint32),
        ]

    kernel32 = ctypes.windll.kernel32
    # 伪句柄必须按 64 位 HANDLE 传:不设 argtypes 时 Python int -1 会被
    # 截成 32 位 0xFFFFFFFF,GetProcessTimes 直接失败(桩首测踩过)
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    creation = FILETIME()
    exit_time = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    if not kernel32.GetProcessTimes(
        ctypes.c_void_p(-1),  # GetCurrentProcess() 的伪句柄 (HANDLE)-1
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return None
    return creation.dwLowDateTime | (creation.dwHighDateTime << 32)


def since_process_create_ms() -> float | None:
    """进程创建至今毫秒;含 Nuitka 引导、DLL 加载、杀软扫描等全部前置。"""
    if sys.platform != "win32":
        return None
    created = _process_create_filetime()
    if created is None:
        return None
    return (_filetime_now() - created) / 10_000  # FILETIME 单位 100ns


def since_first_python_ms() -> float | None:
    """包 __init__ 首行代码至今毫秒。"""
    if _first_python_t0 is None:
        return None
    return (time.perf_counter() - _first_python_t0) * 1000


def report(label: str) -> None:
    """落一行启动桩:两个锚点到此刻的毫秒数。"""
    from .log import get_logger

    since_create = since_process_create_ms()
    since_python = since_first_python_ms()
    get_logger("boot").info(
        "启动桩|%-12s|进程创建→此刻 %7.0f ms|首行代码→此刻 %7.0f ms",
        label,
        since_create if since_create is not None else -1,
        since_python if since_python is not None else -1,
    )


def report_show_once(label: str = "窗口首显") -> None:
    """窗口第一次可见时收尾报一次(重复 show 只报首次)。"""
    global _show_reported
    if _show_reported:
        return
    _show_reported = True
    report(label)
