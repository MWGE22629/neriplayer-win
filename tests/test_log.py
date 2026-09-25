"""log 模块回归:落盘、幂等、未初始化静默、失败不阻断启动。"""

from __future__ import annotations

import logging
import logging.handlers

import pytest

from neriplayer_win import log as app_log


@pytest.fixture()
def isolated_log(tmp_path, monkeypatch):
    """数据目录指到临时目录;测试后卸下 handler,不污染其他用例。"""
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    app_log.teardown_logging()
    yield tmp_path
    app_log.teardown_logging()


def test_setup_writes_startup_header_and_proxy_snapshot(isolated_log):
    app_log.setup_logging()
    app_log.get_logger("test").warning("marker-网络失败")

    text = (isolated_log / "logs" / "app.log").read_text(encoding="utf-8")
    assert "启动 NeriPlayer Win v" in text, "启动头(含版本)缺失"
    assert "环境代理快照" in text, "代理快照缺失(定位代理残留问题的关键)"
    assert "marker-网络失败" in text


def test_setup_is_idempotent_single_handler(isolated_log):
    app_log.setup_logging()
    root = logging.getLogger("neriplayer")
    before = sum(
        1 for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    app_log.setup_logging()
    after = sum(
        1 for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert before == 1 and after == 1

    app_log.get_logger("test").warning("once")
    text = (isolated_log / "logs" / "app.log").read_text(encoding="utf-8")
    assert text.count("once") == 1, "重复 handler 导致同条日志写多次"


def test_silent_without_setup(isolated_log, capsys):
    # 未 setup:仅 NullHandler 兜底,不写文件、不喷 stderr
    app_log.get_logger("test").warning("should-not-appear")
    captured = capsys.readouterr()
    assert "should-not-appear" not in captured.err
    assert not (isolated_log / "logs" / "app.log").exists()


def test_setup_never_raises(isolated_log, monkeypatch):
    def broken():
        raise PermissionError("目录不可写")

    monkeypatch.setattr(app_log, "log_file_path", broken)
    app_log.setup_logging()  # 不得抛
    assert app_log._setup_done is False
