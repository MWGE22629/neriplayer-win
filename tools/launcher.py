"""Nuitka 打包专用入口(独立于 src/,不参与应用逻辑)。

为什么需要它:直接把 src/neriplayer_win/__main__.py 交给 Nuitka 时,该文件
以「顶层脚本」语境编译,`from .app import main` 的相对 import 在运行期
失败;--python-flag=-m 的包模式对文件路径入口亦不生效。本文件以绝对
import 引导,neriplayer_win 整包由 --include-package 携带,语义与
`python -m neriplayer_win` 完全一致。
"""

from neriplayer_win.app import main

if __name__ == "__main__":
    raise SystemExit(main())
