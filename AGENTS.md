# 仓库规则

## Python 环境与命令(必读)

- 本仓库统一使用 **uv** 管理 Python 环境与依赖;不要用系统 `pip`、裸 `python` 或手建 venv。
- 环境位于仓库根 `.venv/`(uv 自动创建,不进版本库);Python 版本由 `.python-version` 固定,uv 会自动获取匹配解释器。
- 常用命令:
  - 同步依赖:`uv sync`
  - 运行应用:`uv run neriplayer-win`(或 `uv run python -m neriplayer_win`)
  - 运行任意脚本/测试:`uv run python <path>`
  - 增删依赖:编辑 `pyproject.toml` 后 `uv sync`,或直接 `uv add <pkg>` / `uv remove <pkg>`
- 依赖锁定在 `uv.lock`,随代码一起提交;不要手改 lock 文件。

## 工作约定

- `reference/NeriPlayer-Android/` 为上游 Android 参考实现(gitignore,自带独立 git 历史):只读参考,不参与构建,不改动它。
- 蓝图与进度在 `TODO.md`:开工前先读,只写「做什么」;完成一项勾一项,新想法先补进 TODO 再动手。
- 音源 API 的请求参数/加密/签名细节,一律以 `reference/NeriPlayer-Android` 中对应实现为准,不要凭记忆或猜测写接口。
- 本项目为 GPL-3.0(衍生自 NeriPlayer);涉及发布的改动注意合规标注。
