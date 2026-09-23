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

# 默认离屏渲染:测试不弹真窗口(实测 UI 用例约快一倍,且不干扰前台);
# 需要真实平台(托盘/字体/DWM)跑测试时,先设 QT_QPA_PLATFORM=windows。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """进程内共享的 QApplication(UI 层测试用;沿用已存在的实例)。

    离屏平台默认字体族是通用名 "Sans Serif",与真实 GUI(微软雅黑 UI)
    的字形覆盖差异会误导 textsafe 的净化判定(以及任何字体相关断言),
    这里统一设为系统默认 UI 字体,测试行为对齐真机。
    """
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    if app.font().family() in ("", "Sans Serif"):
        app.setFont(QFont("Microsoft YaHei UI"))
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
