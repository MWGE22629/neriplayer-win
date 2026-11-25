#!/usr/bin/env python
"""字形审计:起一个真实主窗口,遍历全部控件文本,报出应用字体不覆盖的字符。

用法(仓库根目录,需交互式桌面会话;沿用本机数据目录,可用
NERIPLAYER_WIN_DATA_DIR 指到别处):
    uv run python tools/audit_glyphs.py [--idle 15]

背景:主字体没有的字符一旦上屏,会让 DirectWrite 加载整套回退字体
(v0.9.8 实测 +86MB,见 docs/PERF.md「字形回退陷阱」);现在由
disable_font_fallback 关死字体合并,代价降为显示空框。本工具回答
「是哪段文本的哪个字符」。退出码:0 = 无漏网字符,1 = 有。
"""

from __future__ import annotations

import argparse
import sys
import traceback
import unicodedata
from collections import defaultdict

from PySide6.QtCore import QTimer
from PySide6.QtGui import QRawFont
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMenu,
    QSystemTrayIcon,
    QTableWidget,
    QTabWidget,
    QTreeWidget,
    QWidget,
)

from neriplayer_win.ui.main_window import MainWindow


def _texts(widget: QWidget):
    """产出控件上的全部文本 (来源, 文本);空文本不产出。"""
    yield "windowTitle", widget.windowTitle()
    yield "toolTip", widget.toolTip()
    for action in widget.actions():
        yield "action", action.text()
    if isinstance(widget, QLabel):
        yield "label", widget.text()
    if isinstance(widget, QAbstractButton):
        yield "button", widget.text()
    if isinstance(widget, QGroupBox):
        yield "group", widget.title()
    if isinstance(widget, QLineEdit):
        yield "placeholder", widget.placeholderText()
        yield "lineEdit", widget.text()
    if isinstance(widget, QTabWidget):
        for index in range(widget.count()):
            yield "tab", widget.tabText(index)
    if isinstance(widget, QMenu):
        for action in widget.actions():
            yield "menu", action.text()
    if isinstance(widget, QSystemTrayIcon):
        yield "tray", widget.toolTip()
    items: list[str] = []

    def take(item) -> None:
        for column in range(item.columnCount()):
            items.append(item.text(column))
            items.append(item.toolTip(column))
        for row in range(item.childCount()):
            take(item.child(row))

    if isinstance(widget, QTreeWidget):
        for row in range(widget.topLevelItemCount()):
            take(widget.topLevelItem(row))
    if isinstance(widget, QTableWidget):
        for row in range(widget.rowCount()):
            for column in range(widget.columnCount()):
                item = widget.item(row, column)
                if item is not None:
                    items.append(item.text())
                    items.append(item.toolTip())
    for text in items:
        yield "item", text


def audit(app: QApplication) -> int:
    raw = QRawFont.fromFont(app.font())
    widgets = app.allWidgets()
    hits: dict[str, list[tuple]] = defaultdict(list)  # 字符 → [(控件类, 来源, 文本)]
    for widget in widgets:
        for source, text in _texts(widget):
            for char in set(text):
                # 非 BMP(emoji/数学字母)主字体必然没有,不必探测
                if char.strip() and (
                    ord(char) > 0xFFFF or raw.glyphIndexesForString(char)[0] == 0
                ):
                    hits[char].append((type(widget).__name__, source, text.strip()[:60]))
    print(f"应用字体 {raw.familyName()} / {raw.styleName()};检查 {len(widgets)} 个控件")
    for char, where in sorted(hits.items(), key=lambda pair: -len(pair[1])):
        print(
            f"U+{ord(char):04X} {char!r} ({unicodedata.category(char)})"
            f" x{len(where)}:{where[0]}"
        )
    print("无漏网字符" if not hits else f"{len(hits)} 个字符主字体不覆盖")
    return 1 if hits else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--idle", type=int, default=15, help="窗口出现后等待秒数(等歌单等动态内容加载)"
    )
    opts = parser.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("NeriPlayer Win")
    window = MainWindow()
    window.show()

    result: list[int] = []

    def finish() -> None:
        try:
            result.append(audit(app))
        except Exception:  # noqa: BLE001 - 审计失败也要退出并报非零
            traceback.print_exc()
            result.append(1)
        finally:
            app.quit()

    QTimer.singleShot(opts.idle * 1000, finish)
    app.exec()
    return result[0] if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
