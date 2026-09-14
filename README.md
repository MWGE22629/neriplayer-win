# NeriPlayer Win

网易云 + Bilibili 的 Windows 桌面音乐播放器(Python + PySide6 + libmpv)。
自用优先、轻量优先:扫码登录、读取收藏/歌单、在线播放。

衍生自 [NeriPlayer](https://github.com/cwuom/NeriPlayer)(GPL-3.0),
音源 API 行为以其 Android 实现为参考翻译。

## 仓库结构

- `src/neriplayer_win/` — 本项目代码
- `reference/NeriPlayer-Android/` — 上游 Android 参考实现(自带独立 git 历史,已被 gitignore,不参与构建)
- `TODO.md` — 蓝图与进度(只写做什么)

## 开发

本仓库统一使用 [uv](https://docs.astral.sh/uv/) 管理环境(规则见 [AGENTS.md](./AGENTS.md)):

```bash
uv sync                # 创建 .venv 并安装依赖(自动匹配 .python-version)
uv run neriplayer-win  # 或 uv run python -m neriplayer_win
```

> 播放内核基于 libmpv:运行时需要 `mpv-2.dll`(M1 接入播放时提供,详见 TODO)。

## 构建与分发

一条命令把应用打包为**免装 Python / mpv 的独立发行目录**(Nuitka 编译为原生码):

```bash
uv run python tools/build_exe.py
```

- 前置:仓库 `bin/mpv-2.dll` 就位(gitignore,下载方式见
  [player/engine.py](./src/neriplayer_win/player/engine.py) 模块注释);
  首次构建会自动获取 MinGW 工具链(GitHub 直连不可达时脚本会经镜像
  下载到 Nuitka 本地缓存,见 `tools/build_exe.py`)。首次全量编译约
  10~30 分钟,之后增量约 1~2 分钟。
- 产物:`dist/neriplayer-win/`(约 465MB,159 个文件)——`NeriPlayerWin.exe`
  + Qt/WebEngine 运行时 + `bin/mpv-2.dll` + `LICENSE`/`README.md`。
  精确体积明细与瘦身说明见 [docs/PERF.md](./docs/PERF.md)。
- 分发:整个 `dist/neriplayer-win/` 目录打成 zip 即可;用户解压后双击
  `NeriPlayerWin.exe` 运行(从终端启动请先 cd 进该目录——mpv-2.dll 按
  exe 所在目录的 `bin/` 相对定位)。不做 onefile:单文件模式每次启动
  都要自解压临时目录,违背冷启动 ≤1.5s 的目标。
- 性能验收(M4 实测:冷启动中位 530ms / 常驻 141MB,达标):
  `uv run python tools/measure_perf.py --target both`。

### GPL-3.0 分发注意

本项目衍生自 [NeriPlayer](https://github.com/cwuom/NeriPlayer)(GPL-3.0),
发行包内已附带 `LICENSE`;对外分发时须保持许可证与衍生标注(exe 属性、
README 均已注明),并以源码形式提供对应版本的完整对应源码(本仓库)。
发行包同时包含按各自条款分发的第三方组件:Qt(PySide6,LGPL/GPL,
含 QtWebEngine/Chromium)、libmpv(mpv,LGPL/GPL,shinchiro 构建)、
Python 3.12(PSF)、httpx/segno/pycryptodome/python-mpv 等(见各包许可证)。

## 许可

GPL-3.0(见 [LICENSE](./LICENSE))。仅供学习与研究,
请只在平台规则与账号授权允许的范围内使用。
