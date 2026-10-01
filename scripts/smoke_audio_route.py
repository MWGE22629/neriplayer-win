"""真机冒烟:AudioRouteGuard 挂真实 COM 提供者跑 5 秒,打印快照与轮询次数。

另附一次「拔耳机」策略走查(假设备序列注入策略层,不碰真实设备)。
验证:真实 provider 能取到默认设备并正确分类;守卫 QTimer 正常轮询;
策略决策符合预期。手动端到端(拔插耳机/断连蓝牙)请直接跑主程序观察。
"""

from __future__ import annotations

import sys
import time

from PySide6.QtWidgets import QApplication

from neriplayer_win.log import setup_logging
from neriplayer_win.ui.audio_route import (
    KIND_SPEAKER,
    KIND_WIRED,
    AudioDevice,
    AudioRouteGuard,
    RoutePausePolicy,
    WindowsDefaultEndpointProvider,
)

setup_logging()


class _CountingProvider:
    """包一层真实提供者,只为了数轮询次数。"""

    def __init__(self) -> None:
        self._inner = WindowsDefaultEndpointProvider()
        self.calls = 0

    def default_render_device(self):
        self.calls += 1
        return self._inner.default_render_device()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])

    guard = AudioRouteGuard(parent=None, poll_interval_ms=500, provider=_CountingProvider())
    guard.start()
    events: list[str] = []
    guard.pause_requested.connect(lambda r: events.append(f"pause:{r}"))
    guard.resume_requested.connect(lambda r: events.append(f"resume:{r}"))
    print(f"[smoke] 基线快照: {guard._last.describe() if guard._last else None}")
    if guard._last is None:
        print("[smoke] 拿不到默认设备(异常环境),冒烟失败")
        return 1

    # 策略走查:有线耳机 → 扬声器(暂停)→ 耳机回来(恢复)
    policy = RoutePausePolicy()
    wired = AudioDevice("{wired}", "耳机 (Realtek(R) Audio)", KIND_WIRED)
    speaker = AudioDevice("{spk}", "扬声器 (Realtek(R) Audio)", KIND_SPEAKER)
    d1 = policy.on_route_changed(wired, speaker, playing=True)
    d2 = policy.on_route_changed(speaker, wired, playing=False)
    print(f"[smoke] 策略走查: {d1} -> {d2}")
    assert d1 and d1.action == "pause"
    assert d2 and d2.action == "resume"

    deadline = time.time() + 5.0
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
    guard.stop()
    polls = guard._provider.calls
    print(f"[smoke] 5 秒轮询 {polls} 次(500ms 间隔,应 ≥ 8),事件: {events}")
    assert polls >= 8, "轮询未按预期运行"
    assert not events, "设备未变化时不应有任何自动动作"
    print("[smoke] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
