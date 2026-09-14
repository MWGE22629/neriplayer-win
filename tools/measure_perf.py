#!/usr/bin/env python
"""性能验收测量:冷启动(进程启动 → 主窗口可见)与常驻内存(空闲 20s 后)。

用法(仓库根目录,需交互式桌面会话):
    uv run python tools/measure_perf.py                  # dev + dist 都测
    uv run python tools/measure_perf.py --target dist    # 只测发行 exe
    uv run python tools/measure_perf.py --runs 5 --idle 20

口径:
- 冷启动:CreateProcess 到枚举到可见主窗口(标题恰为 "NeriPlayer Win",
  EnumWindows + IsWindowVisible)的墙钟时间,取 N 次中位数。注意这是
  「OS 文件缓存已热」口径(等价于日常点图标启动;onefile 自解压等
  一次性开销不存在,详见 docs/PERF.md)。
- 常驻内存:窗口出现后空闲 --idle 秒,连续采样 3 次取中位:
  WorkingSet(任务管理器"内存"列)与 PrivateUsage(提交)。
- 只测主进程:mpv 以 libmpv 形式跑在主进程内,故已包含;WebEngine
  仅登录弹窗才加载,常态无 QtWebEngineProcess 子进程。
- dev 模式走 `uv run python -m neriplayer_win`(含 uv/解释器启动开销,
  作为对照);dist 模式以发行目录为 cwd 启动 exe(等价双击)。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import platform
import statistics
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST_EXE = REPO / "dist" / "neriplayer-win" / "NeriPlayerWin.exe"
WINDOW_TITLE = "NeriPlayer Win"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

# 64 位下 HANDLE 返回值必须显式声明 restype,否则被按 c_int 截断。
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, wt.BOOL, ctypes.c_uint32]


# ---------------------------------------------------------------- Win32 封装


def _find_window_pids(title: str) -> set[int]:
    """返回拥有可见顶层窗口(标题恰为 title)的进程 PID 集合。"""
    hits: set[int] = set()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def on_window(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value == title:
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            hits.add(pid.value)
        return True

    user32.EnumWindows(on_window, 0)
    return hits


# 注:不使用 CreateToolhelp32Snapshot 做进程族归属——本机(Python 3.12 /
# Win11 26100)上 Process32FirstW 恒以 ERROR_BAD_LENGTH 拒收(结构体与
# argtypes 均已核对),归属改用「启动前后窗口 PID 集合差分」,同样可靠
# (测量前会清掉残留实例,标题精确匹配)。


def _memory(pid: int) -> tuple[int, int] | None:
    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY|VM_READ
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return counters.WorkingSetSize, counters.PrivateUsage
    finally:
        kernel32.CloseHandle(handle)


def _kill_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------- 机器信息


def machine_info() -> str:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wt.DWORD),
            ("dwMemoryLoad", wt.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    cpu = platform.processor() or "未知 CPU"
    return (
        f"OS: {platform.platform()} | CPU: {cpu} x{__import__('os').cpu_count()} "
        f"| 内存: {stat.ullTotalPhys / 1024**3:.0f} GB"
    )


# ---------------------------------------------------------------- 测量


def _launch(target: str) -> subprocess.Popen:
    if target == "dist":
        return subprocess.Popen(
            [str(DIST_EXE)], cwd=DIST_EXE.parent,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return subprocess.Popen(
        ["uv", "run", "python", "-m", "neriplayer_win"], cwd=REPO,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_window(pre_hits: set[int], timeout_s: float) -> tuple[float, int] | None:
    """轮询直到出现「启动前不存在」的主窗口;返回 (耗时秒, 窗口进程 PID)。"""
    start = time.perf_counter()
    deadline = start + timeout_s
    while time.perf_counter() < deadline:
        owned = _find_window_pids(WINDOW_TITLE) - pre_hits
        if owned:
            return time.perf_counter() - start, owned.pop()
        time.sleep(0.01)
    return None


def measure_cold_start(target: str, runs: int) -> tuple[list[float], str]:
    samples: list[float] = []
    failure = ""
    for index in range(runs):
        stale = _find_window_pids(WINDOW_TITLE)
        if stale:
            for pid in stale:
                _kill_tree(pid)
            time.sleep(1.0)
        process = _launch(target)
        pre_hits = _find_window_pids(WINDOW_TITLE)
        result = wait_window(pre_hits, timeout_s=40.0)
        if result is None:
            failure = f"第 {index + 1} 次启动 40s 内未见主窗口"
            _kill_tree(process.pid)
            break
        elapsed, window_pid = result
        samples.append(elapsed)
        print(f"  run {index + 1}: {elapsed * 1000:7.0f} ms  (pid {window_pid})")
        _kill_tree(process.pid)
        time.sleep(1.0)  # 让 OS 收尾,避免句柄/文件残留影响下一轮
    return samples, failure


def measure_memory(target: str, idle_s: int) -> tuple[int, int, int] | None:
    """启动 → 窗口可见 → 空闲 idle_s 秒 → 采样内存;返回 (ws_med, priv_med, pid)。"""
    stale = _find_window_pids(WINDOW_TITLE)
    for pid in stale:
        _kill_tree(pid)
    time.sleep(1.0)

    process = _launch(target)
    pre_hits = _find_window_pids(WINDOW_TITLE)
    result = wait_window(pre_hits, timeout_s=40.0)
    if result is None:
        _kill_tree(process.pid)
        return None
    _, window_pid = result
    print(f"  窗口可见(pid {window_pid}),空闲 {idle_s}s 后采样……")
    time.sleep(idle_s)

    working_sets: list[int] = []
    privates: list[int] = []
    for _ in range(3):
        mem = _memory(window_pid)
        if mem is None:
            _kill_tree(process.pid)
            return None
        working_sets.append(mem[0])
        privates.append(mem[1])
        time.sleep(1.0)
    _kill_tree(process.pid)
    return int(statistics.median(working_sets)), int(statistics.median(privates)), window_pid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=["dev", "dist", "both"], default="both")
    parser.add_argument("--runs", type=int, default=5, help="冷启动次数(取中位)")
    parser.add_argument("--idle", type=int, default=20, help="内存采样前空闲秒数")
    opts = parser.parse_args()

    print("测量环境:", machine_info())
    print(
        "口径:进程启动→主窗口可见(OS 文件缓存已热);"
        "内存=主进程 WorkingSet/私有提交中位(含 libmpv,不含未打开的 WebEngine)\n"
    )

    targets = ["dev", "dist"] if opts.target == "both" else [opts.target]
    summary: list[tuple[str, list[float], tuple | None]] = []

    for target in targets:
        if target == "dist" and not DIST_EXE.is_file():
            print(f"[dist] 缺少 {DIST_EXE},请先:uv run python tools/build_exe.py")
            continue
        print(f"[{target}] 冷启动 x{opts.runs}")
        samples, failure = measure_cold_start(target, opts.runs)
        if failure:
            print(f"[{target}] 失败:{failure}")
            continue
        median = statistics.median(samples)
        print(
            f"  中位 {median * 1000:.0f} ms | "
            f"min {min(samples) * 1000:.0f} | max {max(samples) * 1000:.0f} ms"
        )

        print(f"[{target}] 常驻内存(空闲 {opts.idle}s)")
        memory = measure_memory(target, opts.idle)
        if memory is None:
            print(f"[{target}] 内存测量失败")
            summary.append((target, samples, None))
            continue
        ws, private, _pid = memory
        print(f"  WorkingSet {ws / 1024**2:.0f} MB | 私有提交 {private / 1024**2:.0f} MB")
        summary.append((target, samples, memory))
        print()

    print("== 汇总 ==")
    print(f"{'目标':6} {'冷启动中位':>10} {'WorkingSet':>10} {'私有提交':>10}")
    for target, samples, memory in summary:
        median = statistics.median(samples) * 1000 if samples else float("nan")
        ws = memory[0] / 1024**2 if memory else float("nan")
        private = memory[1] / 1024**2 if memory else float("nan")
        print(f"{target:6} {median:9.0f}ms {ws:9.0f}MB {private:9.0f}MB")
    verdict_cold = all(
        statistics.median(s) <= 1.5 for _t, s, _m in summary if s
    )
    verdict_mem = all(
        m[0] <= 150 * 1024**2 for _t, _s, m in summary if m
    )
    print(f"\n验收(M4):冷启动≤1.5s {'PASS' if verdict_cold else 'FAIL'};"
          f"常驻内存≤150MB {'PASS' if verdict_mem else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
