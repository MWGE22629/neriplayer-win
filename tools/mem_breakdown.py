#!/usr/bin/env python
"""常驻内存拆解:把主进程 WorkingSet 逐页归到「镜像(DLL/EXE)/ 映射文件(字体等)/ 私有」。

用法(仓库根目录,需交互式桌面会话):
    uv run python tools/mem_breakdown.py                        # dev
    uv run python tools/mem_breakdown.py --target dist          # 发行 exe
    uv run python tools/mem_breakdown.py --pythonpath <src目录> --json a.json
    uv run python tools/mem_breakdown.py --diff a.json b.json   # 两次拆解做差

口径同 measure_perf.py(主窗口可见后空闲 --idle 秒,只看主进程)。
归属方法:VirtualQueryEx 列出已提交区域,QueryWorkingSet 列出常驻页,
逐页二分落到所属区域;镜像/映射区域按分配基址取文件名。页合计应与
WorkingSetSize 吻合(偏差 >5% 会告警)。
"""

from __future__ import annotations

import argparse
import bisect
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import measure_perf as perf  # 同目录:复用窗口查找、残留清理、内存计数

PAGE = 1 << 12
MEM_COMMIT, MEM_PRIVATE, MEM_MAPPED, MEM_IMAGE = 0x1000, 0x20000, 0x40000, 0x1000000
PAGE_WRITECOPY = 0x08
MAX_USER_ADDR = 0x7FFFFFFEFFFF  # x64 用户态上界
MB = float(1024**2)


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


k32, psapi = perf.kernel32, perf.psapi
k32.VirtualQueryEx.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t,
]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.QueryDosDeviceW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
k32.QueryDosDeviceW.restype = wt.DWORD
psapi.GetMappedFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
psapi.GetMappedFileNameW.restype = wt.DWORD
psapi.QueryWorkingSet.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
psapi.QueryWorkingSet.restype = wt.BOOL


def _drive_letters() -> dict[str, str]:
    """\\Device\\HarddiskVolumeN → 盘符。"""
    out: dict[str, str] = {}
    for code in range(ord("A"), ord("Z") + 1):
        buf = ctypes.create_unicode_buffer(1024)
        if k32.QueryDosDeviceW(f"{chr(code)}:", buf, 1024):
            out[buf.value] = f"{chr(code)}:"
    return out


def _file_name(handle: int, allocation_base: int, drives: dict[str, str]) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    if not psapi.GetMappedFileNameW(handle, allocation_base, buf, 1024):
        return "匿名映射"
    parts = buf.value.split("\\")  # ['', 'Device', 'HarddiskVolumeN', 其余...]
    drive = drives.get("\\".join(parts[:3]))
    return f"{drive}\\{'\\'.join(parts[3:])}" if drive else buf.value


def _commit_regions(
    handle: int, drives: dict[str, str]
) -> tuple[list[int], list[tuple[int, int, str]]]:
    """已提交区域:(各区域起址列表, [(起址, 止址, 分类名)])。"""
    regions: list[tuple[int, int, str]] = []
    starts: list[int] = []
    mbi = _MBI()
    address = 0
    while address < MAX_USER_ADDR:
        if not k32.VirtualQueryEx(handle, address, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base = mbi.BaseAddress or address
        if not mbi.RegionSize:
            break
        if mbi.State == MEM_COMMIT:
            if mbi.Type == MEM_PRIVATE:
                # 读写=堆/栈/普通数据;写时复制=镜像页被改写;其余=可执行等
                suffix = {4: "", PAGE_WRITECOPY: "(写时复制)"}.get(mbi.Protect & 0xFF, "(其它保护)")
                label = "私有内存" + suffix
            else:
                path = _file_name(handle, mbi.AllocationBase, drives)
                label = "映射:" + path if mbi.Type == MEM_MAPPED else "镜像:" + Path(path).name
            regions.append((base, base + mbi.RegionSize, label))
            starts.append(base)
        address = base + mbi.RegionSize
    return starts, regions


def _resident_pages(handle: int) -> list[tuple[int, int]]:
    """常驻页: (地址, 共享位)。"""
    size = 8 << 20
    while True:
        buf = ctypes.create_string_buffer(size)
        if psapi.QueryWorkingSet(handle, buf, size):
            break
        size *= 2
    words = (ctypes.c_uint64 * (size // 8)).from_buffer(buf)
    # x64 块布局(实测,与 MSDN 的「20 位页号」说明不符):低 12 位是标志,
    # 第 8 位为共享位,其上直通到第 63 位都是虚拟页号
    return [(b >> 12 << 12, (b >> 8) & 1) for b in words[1 : words[0] + 1]]


def _label(regions: tuple[list[int], list], address: int) -> str | None:
    starts, items = regions
    index = bisect.bisect_right(starts, address) - 1
    if index >= 0 and address < items[index][1]:
        return items[index][2]
    return None


def breakdown(pid: int) -> dict[str, list[int]]:
    """分类名 → [独占页数, 共享页数](下标即共享位)。"""
    handle = k32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY|VM_READ
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess")
    try:
        drives = _drive_letters()
        before = _commit_regions(handle, drives)
        pages = _resident_pages(handle)
        after = _commit_regions(handle, drives)
    finally:
        k32.CloseHandle(handle)
    rows: dict[str, list[int]] = {}
    for address, shared in pages:
        # 目标进程还在跑,两次快照之间可能有区域新建/释放,任一快照能认领即可
        label = _label(before, address) or _label(after, address) or "未归属"
        rows.setdefault(label, [0, 0])[shared] += 1
    return rows


def _total(pages: list[int]) -> float:
    return (pages[0] + pages[1]) * PAGE / MB


def _print_rows(rows: dict[str, list[int]], top: int) -> None:
    for label, pages in sorted(rows.items(), key=lambda kv: -_total(kv[1]))[:top]:
        print(
            f"  {_total(pages):8.1f} MB  {label}"
            f"  (独占 {pages[0] * PAGE / MB:.1f} / 共享 {pages[1] * PAGE / MB:.1f})"
        )


def measure(opts: argparse.Namespace) -> int:
    stale = perf._find_window_pids(perf.WINDOW_TITLE)
    for pid in stale:
        perf._kill_tree(pid)
    if stale:
        time.sleep(1.0)

    if opts.pythonpath:
        env = dict(
            os.environ,
            PYTHONPATH=opts.pythonpath + os.pathsep + os.environ.get("PYTHONPATH", ""),
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "neriplayer_win"], cwd=perf.REPO, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        process = perf._launch(opts.target)
    found = perf.wait_window(perf._find_window_pids(perf.WINDOW_TITLE), 40.0)
    if found is None:
        perf._kill_tree(process.pid)
        print("40s 内未见主窗口")
        return 1
    elapsed, pid = found
    print(f"  窗口可见(pid {pid}),空闲 {opts.idle}s 后拆解……")
    time.sleep(opts.idle)
    working_set, private = perf._memory(pid)
    rows = breakdown(pid)
    perf._kill_tree(process.pid)

    pages_mb = sum(_total(p) for p in rows.values())
    print(
        f"  WorkingSet {working_set / MB:.1f} MB(页合计 {pages_mb:.1f} MB)"
        f" | 私有提交 {private / MB:.1f} MB | 启动 {elapsed * 1000:.0f} ms"
    )
    if abs(pages_mb - working_set / MB) > 0.05 * pages_mb:
        print("  告警:页合计与 WorkingSetSize 偏差超过 5%,归属可能不准")
    _print_rows(rows, opts.top)
    if opts.json:
        opts.json.write_text(
            json.dumps({"ws": working_set, "private": private, "rows": rows}),
            encoding="utf-8",
        )
    return 0


def diff(path_a: Path, path_b: Path, top: int) -> int:
    a = json.loads(path_a.read_text("utf-8"))
    b = json.loads(path_b.read_text("utf-8"))
    print(
        f"WorkingSet {a['ws'] / MB:.1f} → {b['ws'] / MB:.1f} MB"
        f" | 私有提交 {a['private'] / MB:.1f} → {b['private'] / MB:.1f} MB"
    )
    empty = [0, 0]
    deltas = sorted(
        (
            (_total(b["rows"].get(k, empty)) - _total(a["rows"].get(k, empty)), k)
            for k in a["rows"].keys() | b["rows"].keys()
        ),
        key=lambda item: -abs(item[0]),
    )
    for delta, label in deltas[:top]:
        if abs(delta) < 0.2:
            break
        print(
            f"  {delta:+8.1f} MB  {label}"
            f"  ({_total(a['rows'].get(label, empty)):.1f} → {_total(b['rows'].get(label, empty)):.1f})"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--target", choices=["dev", "dist"], default="dev")
    parser.add_argument(
        "--pythonpath", help="改用该目录下的 neriplayer_win 源码(当前解释器跑),跨版本对比用"
    )
    parser.add_argument("--idle", type=int, default=20, help="拆解前空闲秒数")
    parser.add_argument("--top", type=int, default=25, help="按分类列出前 N 项")
    parser.add_argument("--json", type=Path, help="拆解结果写到该文件")
    parser.add_argument("--diff", nargs=2, type=Path, metavar=("A", "B"))
    opts = parser.parse_args()
    if opts.diff:
        return diff(*opts.diff, opts.top)
    return measure(opts)


if __name__ == "__main__":
    raise SystemExit(main())
