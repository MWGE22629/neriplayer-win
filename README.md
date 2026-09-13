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

```bash
python -m venv .venv
source .venv/Scripts/activate   # CMD: .venv\Scripts\activate.bat
pip install -e .
neriplayer-win                  # 或 python -m neriplayer_win
```

> 播放内核基于 libmpv:运行时需要 `mpv-2.dll`(M1 接入播放时提供,详见 TODO)。

## 许可

GPL-3.0(见 [LICENSE](./LICENSE))。仅供学习与研究,
请只在平台规则与账号授权允许的范围内使用。
