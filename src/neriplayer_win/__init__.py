"""NeriPlayer Win:网易云 + B站的 Windows 桌面音乐播放器。"""

import os
import sys

if sys.platform == "win32":
    # Qt6Core 动态依赖 icuuc.dll;若 PATH 中存在 miniconda 等自带的 ICU
    # (如 conda 的 ICU 73),会被抢先加载并导致 WinError 127。
    # 先显式加载系统自带的 ICU,钉住进程内版本。
    # (与 app.py 入口处的 hack 一致;放在包级 __init__ 是为了任何
    #  `import neriplayer_win.*` / 离屏冒烟路径也能先于 PySide6 执行。)
    import ctypes

    try:
        ctypes.CDLL(os.path.join(os.environ["SystemRoot"], "System32", "icuuc.dll"))
    except OSError:
        pass

__version__ = "0.7.2"
