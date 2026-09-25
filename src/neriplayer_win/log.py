"""应用日志:RotatingFileHandler 写入数据目录 logs/app.log。

动机:用户侧网络类故障(如系统/环境代理残留导致 httpx WinError 10061
连不上 B站,远程用户环境各异)此前只有状态栏一闪而过的文本,事后排障
无迹可查。网络失败详情连同环境代理快照(getproxies())落盘后,用户
回传一份日志即可定位这类环境问题。

设计:
- 仅 app.main() 调 setup_logging();未初始化时(如 pytest)静默,
  由挂在 "neriplayer" 根上的 NullHandler 兜底,不向 stderr 喷日志。
- 初始化失败(目录不可写等)吞掉,日志属可观测性增强,绝不阻断启动。
- 隐私:只记 URL 与异常摘要,绝不含 Cookie / 凭据。
"""

from __future__ import annotations

import logging
import platform
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.request import getproxies

from . import __version__
from .data.store import default_data_dir

_ROOT_NAME = "neriplayer"
_MAX_BYTES = 1 * 1024 * 1024
_BACKUP_COUNT = 3

# setup 状态:getLogger 侧无须感知,setup 幂等与 handler 复挂靠这两项防守
_setup_done = False
_handler: RotatingFileHandler | None = None

# 未 setup 时吞掉所有日志输出(仅本包 logger;root logger 不动)
logging.getLogger(_ROOT_NAME).addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """取 neriplayer.<name> 子 logger;未 setup 时输出为空。"""
    return logging.getLogger(f"{_ROOT_NAME}.{name}")


def log_file_path() -> Path:
    return default_data_dir() / "logs" / "app.log"


def setup_logging() -> None:
    """初始化文件日志(幂等;任何失败静默)。在 app.main() 最早调用。"""
    global _setup_done, _handler
    if _setup_done:
        return
    try:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        ))
        root = logging.getLogger(_ROOT_NAME)
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        _handler = handler
        _setup_done = True

        logger = get_logger("app")
        logger.info(
            "启动 NeriPlayer Win v%s python=%s platform=%s",
            __version__, sys.version.split()[0], platform.platform(),
        )
        logger.info("环境代理快照 getproxies()=%r", dict(getproxies()))
    except Exception:  # noqa: BLE001 - 日志失败绝不影响启动
        pass


def teardown_logging() -> None:
    """关闭并卸下文件 handler(测试用;正常运行无须调用)。"""
    global _setup_done, _handler
    if _handler is not None:
        logging.getLogger(_ROOT_NAME).removeHandler(_handler)
        _handler.close()
        _handler = None
    _setup_done = False
