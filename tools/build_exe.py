#!/usr/bin/env python
"""Nuitka 打封装:把 NeriPlayer Win 编译为免装 Python / mpv 的独立发行目录。

用法(仓库根目录):
    uv run python tools/build_exe.py [--clean] [--console] [--jobs N]

产物:
    build/launcher.build/          Nuitka/SCons 编译中间物(可增量复用)
    build/launcher.dist/           Nuitka 原始 standalone 输出
    dist/neriplayer-win/           最终发行目录 = .dist + bin/mpv-2.dll + LICENSE/README

设计要点:
- 单目录分发,不做 onefile(onefile 每次启动要自解压临时目录,违背 ≤1.5s
  冷启动目标;单目录还能按需只读加载 DLL)。
- mpv-2.dll 的定位沿用 src/neriplayer_win/player/engine.py 的候选逻辑:
  引擎按「sys.executable 所在目录/bin」「仓库根/bin」「当前工作目录/bin」
  依次查找。打包后 sys.executable 即发行目录内的 NeriPlayerWin.exe,
  因此发行目录内放 bin/mpv-2.dll 后,无论双击、快捷方式还是从任意目录
  终端启动都能定位到运行库。
- MinGW 工具链:Nuitka 在无 MSVC 的机器上会自动下载 winlibs gcc(仅托管在
  GitHub)。本脚本在启动 Nuitka 前检查其缓存,若缺失则先经 GitHub 加速
  镜像下载同版本 zip 到 Nuitka 缓存(%LOCALAPPDATA%/Nuitka/Cache),保证
  在 GitHub 不可达的网络下可复跑;镜像全部失败则放行,交由 Nuitka 自行
  下载(带 --assume-yes-for-downloads)。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# 打包入口:包内 __main__.py 直接编译会丢包语境(相对 import 失败),
# 故用 tools/launcher.py 绝对 import 引导,详见该文件说明。
ENTRY = REPO / "tools" / "launcher.py"
BUILD_DIR = REPO / "build"  # Nuitka --output-dir
# Nuitka 以入口文件名命名 .build/.dist 目录
NUITKA_DIST = BUILD_DIR / "launcher.dist"
DIST = REPO / "dist" / "neriplayer-win"
EXE_NAME = "NeriPlayerWin.exe"

APP_NAME = "NeriPlayer Win"
APP_VERSION = "0.7.2"

# winlibs MinGW(Nuitka 4.2.1 指定版本;变更 Nuitka 版本时需同步更新)
MINGW_URL_PATH = (
    "brechtsanders/winlibs_mingw/releases/download/"
    "15.2.0posix-13.0.0-msvcrt-r6/"
    "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-13.0.0-r6.zip"
)
MINGW_SPECIFICITY = "15.2.0posix-13.0.0-msvcrt-r6"
MINGW_MIRRORS = (
    "https://gh-proxy.com/https://github.com/",
    "https://ghproxy.net/https://github.com/",
    "https://github.com/",  # 直连(GitHub 可达的环境)
)


def log(message: str) -> None:
    print(f"[build_exe] {message}", flush=True)


# ---------------------------------------------------------------- MinGW 缓存


def _mingw_cache_dir() -> Path:
    # nuitka.utils.AppDirs.getCacheDir("downloads"):appdirs.user_cache_dir("Nuitka")
    # → %LOCALAPPDATA%/Nuitka/Nuitka/Cache(appauthor 缺省取 appname)
    base = os.environ.get("LOCALAPPDATA")
    root = (
        Path(base) / "Nuitka" / "Nuitka" / "Cache"
        if base
        else Path.home() / ".cache" / "Nuitka"
    )
    return root / "downloads" / "gcc" / "x86_64" / MINGW_SPECIFICITY


def _download(url: str, dest: Path) -> bool:
    log(f"下载 {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "ab") as out:
            # 断点续传:镜像支持 Range 时从已有字节继续
            if dest.exists() and dest.stat().st_size:
                out.seek(0, os.SEEK_END)
                req = urllib.request.Request(
                    url, headers={"Range": f"bytes={out.tell()}-"}
                )
                resp = urllib.request.urlopen(req, timeout=60)
            total = resp.headers.get("Content-Length")
            done = out.tell()
            while True:
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // int(total)
                    print(f"\r  {done}/{total} bytes ({pct}%)", end="", flush=True)
        print()
        return True
    except Exception as exc:  # noqa: BLE001 - 镜像失败要轮换,不中断
        log(f"下载失败({exc}),换下一个源")
        return False


def ensure_mingw() -> None:
    """确保 Nuitka 的 winlibs MinGW 缓存就绪(GitHub 直连不可达时走镜像)。"""
    cache = _mingw_cache_dir()
    if (cache / "mingw64" / "bin" / "gcc.exe").is_file():
        log(f"MinGW 缓存已就绪:{cache}")
        return
    zip_name = MINGW_URL_PATH.rsplit("/", 1)[1]
    zip_path = cache / zip_name
    for mirror in MINGW_MIRRORS:
        if _download(mirror + MINGW_URL_PATH, zip_path):
            expected = 267_135_426  # winlibs r6 x86_64 zip 体积
            size = zip_path.stat().st_size
            if size != expected:
                log(f"警告:zip 体积 {size} != 预期 {expected},仍交给 Nuitka 校验")
            log(f"MinGW zip 已就位:{zip_path}(由 Nuitka 解压)")
            return
    log("所有镜像均失败;继续交由 Nuitka 自行下载(若 GitHub 可达)")


# ---------------------------------------------------------------- 构建


def find_icon() -> Path | None:
    for pattern in ("src/neriplayer_win/assets/**/*.ico", "assets/**/*.ico"):
        hits = sorted(REPO.glob(pattern))
        if hits:
            return hits[0]
    return None


def nuitka_args(console: bool, jobs: int | None) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "nuitka",
        str(ENTRY),
        "--standalone",
        "--mingw64",  # 无 MSVC 的机器固定用 winlibs gcc,保证可复现
        "--assume-yes-for-downloads",
        "--enable-plugin=pyside6",  # Qt DLL/插件/WebEngine 资产全由该插件搬运
        "--include-package=neriplayer_win",  # 含函数内延迟 import 的全部子模块
        # UI 资产(ico/png/svg):运行时经 Path(__file__) 相对定位,须镜像进包
        "--include-data-dir=src/neriplayer_win/assets=neriplayer_win/assets",
        "--lto=yes",  # 链接期优化:缩减体积、改善启动
        "--python-flag=no_site",
        "--windows-console-mode=" + ("force" if console else "disable"),
        f"--output-dir={BUILD_DIR}",
        f"--output-filename={EXE_NAME}",
        f"--company-name={APP_NAME}",
        f"--product-name={APP_NAME}",
        f"--file-description={APP_NAME} - 网易云 + B站桌面音乐播放器",
        f"--product-version={APP_VERSION}",
        f"--file-version={APP_VERSION}",
        f"--copyright=GPL-3.0-or-later, 衍生自 NeriPlayer (GPL-3.0)",
        f"--report={BUILD_DIR / 'nuitka-report.xml'}",
    ]
    icon = find_icon()
    if icon is not None:
        args.append(f"--windows-icon-from-ico={icon}")
        log(f"使用图标:{icon}")
    else:
        log("未找到 .ico(并行任务产出后自动纳入),本次不带图标")
    if jobs:
        args.append(f"--jobs={jobs}")
    return args


def prune_dist() -> None:
    """瘦身体积(每项都有依据,总省约 150MB):

    - qtwebengine_devtools_resources*.pak:仅 Chromium devtools(F12)用,
      正常网页登录不需要(debug 版 76MB、正式版 12MB);
    - *.debug.pak / v8_context_snapshot.debug.bin:调试符号资源;
    - 非 zh_CN 的 Qt 翻译与 Chromium locale pak:界面文案本项目自带,
      Qt 标准对话框仅保留简体中文,Chromium locale 保留 zh-CN + en-US 兜底;
    - qt6pdf.dll:全部二进制的导入闭包均未引用(应用不展示 PDF);
      qt6svg.dll 保留——qsvg iconengines/imageformats 插件运行时动态加载。
    """
    removed = 0
    patterns = [
        "qtwebengine_devtools_resources*.pak",
        "*.debug.pak",
        "v8_context_snapshot.debug.bin",
        "qt6pdf.dll",
    ]
    for pattern in patterns:
        for f in DIST.rglob(pattern):
            removed += f.stat().st_size
            f.unlink()
    for f in (DIST / "PySide6" / "translations").glob("*.qm"):
        if not f.name.endswith("zh_CN.qm"):
            removed += f.stat().st_size
            f.unlink()
    locales = DIST / "PySide6" / "translations" / "qtwebengine_locales"
    for f in locales.glob("*.pak"):
        if f.name not in ("zh-CN.pak", "en-US.pak"):
            removed += f.stat().st_size
            f.unlink()
    log(f"瘦身完成,删除 {removed / 1e6:.1f} MB")


def assemble_dist() -> None:
    if not (NUITKA_DIST / EXE_NAME).is_file():
        raise SystemExit(f"未找到 {NUITKA_DIST / EXE_NAME},构建疑似失败")
    log(f"组装发行目录 {DIST}")
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(NUITKA_DIST, DIST)

    mpv = REPO / "bin" / "mpv-2.dll"
    if not mpv.is_file():
        raise SystemExit("缺少 bin/mpv-2.dll;请见 README 构建章节先放置运行库")
    (DIST / "bin").mkdir(exist_ok=True)
    shutil.copy2(mpv, DIST / "bin" / "mpv-2.dll")

    for extra in ("LICENSE", "README.md"):
        if (REPO / extra).is_file():
            shutil.copy2(REPO / extra, DIST / extra)

    # 自检:UI 资产必须随包(icons.py 经 __file__ 相对定位)
    assets = DIST / "neriplayer_win" / "assets"
    expected = {p.name for p in (REPO / "src" / "neriplayer_win" / "assets").rglob("*") if p.is_file()}
    actual = {p.name for p in assets.rglob("*") if p.is_file()} if assets.is_dir() else set()
    missing = expected - actual
    if missing:
        raise SystemExit(f"UI 资产未打进发行包,缺:{sorted(missing)}")

    prune_dist()


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def report_size() -> None:
    total = dir_size(DIST)
    log(f"发行目录总大小:{total / 1e6:.1f} MB,文件数 {sum(1 for _ in DIST.rglob('*') if _.is_file())}")
    groups: dict[str, int] = {}
    for f in DIST.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(DIST).as_posix()
        if rel == EXE_NAME:
            key = EXE_NAME
        elif rel.startswith("resources/"):
            key = "resources/(WebEngine pak/icudtl/locales)"
        elif rel.startswith("PySide6/") or rel.startswith("plugins/"):
            key = "PySide6 运行时(plugins 等)"
        elif rel.endswith(".dll"):
            key = "Qt/Core DLL"
        elif rel.startswith("bin/"):
            key = "bin/mpv-2.dll"
        else:
            key = "其他"
        groups[key] = groups.get(key, 0) + f.stat().st_size
    for key, size in sorted(groups.items(), key=lambda kv: -kv[1]):
        print(f"    {size / 1e6:9.2f} MB  {key}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="清空 build/dist 后重建")
    parser.add_argument("--console", action="store_true", help="带控制台窗口(调试用)")
    parser.add_argument("--jobs", type=int, default=None, help="并行编译进程数")
    opts = parser.parse_args()

    if not ENTRY.is_file():
        raise SystemExit(f"入口缺失:{ENTRY}")
    if shutil.which("nuitka") is None and not (REPO / ".venv").is_dir():
        raise SystemExit("请先 uv sync 安装依赖(dev 组含 nuitka)")

    if opts.clean:
        for target in (BUILD_DIR, REPO / "dist"):
            if target.exists():
                log(f"清理 {target}")
                shutil.rmtree(target)

    ensure_mingw()

    log("启动 Nuitka(首次全量编译约需 10-30 分钟)……")
    result = subprocess.run(nuitka_args(opts.console, opts.jobs), cwd=REPO)
    if result.returncode != 0:
        raise SystemExit(f"Nuitka 退出码 {result.returncode}")

    assemble_dist()
    report_size()

    # exe 元数据自检:GUI 子系统(非控制台)且能被解析
    exe = DIST / EXE_NAME
    log(f"产物:{exe}({exe.stat().st_size / 1e6:.1f} MB)")
    log("冒烟提示:uv run python tools/measure_perf.py --target dist 验证启动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
