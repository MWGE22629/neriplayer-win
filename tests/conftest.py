"""pytest 全局配置。

Windows 上 libmpv(python-mpv 事件线程)在解释器终结阶段存在平台级
竞争,偶发进程级崩溃(SEH 码 0xE24C4A02 / access violation),会吞掉
pytest 的退出码。因此在全部输出完成后(pytest_unconfigure)直接
os._exit 跳过解释器终结,保证门禁能拿到确定的结果码。代价:跳过终结
钩子(本仓库未用 coverage 等依赖终结的插件,可接受)。
"""

from __future__ import annotations

import os
import sys

import pytest


@pytest.fixture(scope="session")
def qapp():
    """进程内共享的 QApplication(UI 层测试用;沿用已存在的实例)。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.hookimpl(hookwrapper=True)
def pytest_sessionfinish(session, exitstatus):
    yield
    session.config._neriplayer_exitstatus = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(getattr(config, "_neriplayer_exitstatus", 0))
