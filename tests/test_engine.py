"""PlayerEngine 功能测试:本地生成 WAV,实际驱动 libmpv 播放到结束。

音量设为 0,避免测试时外放声音。mpv-2.dll 缺失时 skip。

线程模型注意:libmpv 的 terminate 与 python-mpv 事件线程存在平台级竞争
(Windows 下以 LuaJIT 自定义 SEH 码 0xE24C4A02 随机崩溃),因此引擎在
整个测试会话只创建一次(模块级 fixture),用例间只 stop 不销毁;
进程退出由 conftest 的硬退出兜底,避免解释器终结阶段触发销毁竞争。
"""

from __future__ import annotations

import math
import struct
import time
import wave

import pytest
from PySide6.QtWidgets import QApplication

from neriplayer_win.player.engine import PlayerEngine, PlayerEngineError


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def engine(qapp):
    try:
        # ao=null:不占用真实音频设备,规避当前 mpv 构建音频线程偶发崩溃
        player = PlayerEngine(audio_output="null")
    except PlayerEngineError as error:
        pytest.skip(f"mpv 运行库不可用: {error}")
    yield player
    player.stop()


def _make_wav(path, seconds: int = 4, rate: int = 8000) -> None:
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for i in range(rate * seconds):
            value = int(2000 * math.sin(2 * math.pi * 440 * i / rate))
            frames += struct.pack("<h", value)
        handle.writeframes(bytes(frames))


def _wait(qapp, condition, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    qapp.processEvents()
    return condition()


def test_play_until_end(qapp, engine, tmp_path):
    wav = tmp_path / "tone.wav"
    _make_wav(wav, seconds=2)

    ended: list[bool] = []
    engine.track_ended.connect(lambda: ended.append(True))
    positions: list[tuple[float, float]] = []
    engine.progress.connect(lambda pos, dur: positions.append((pos, dur)))

    engine.play_url(str(wav))
    try:
        assert _wait(qapp, lambda: positions and positions[-1][0] > 0.5, timeout_s=15)
        assert _wait(qapp, lambda: bool(ended), timeout_s=15)

        durations = [dur for _, dur in positions if dur > 0]
        assert durations and abs(durations[-1] - 2.0) < 0.5
        assert not engine.is_playing()
    finally:
        engine.track_ended.disconnect()
        engine.progress.disconnect()
        engine.stop()


def test_pause_seek_volume_stop(qapp, engine, tmp_path):
    wav = tmp_path / "tone.wav"
    _make_wav(wav, seconds=6)

    engine.play_url(str(wav))
    try:
        assert _wait(qapp, lambda: engine.position() > 1.0, timeout_s=15)

        engine.set_paused(True)
        paused_at = engine.position()
        time.sleep(0.4)
        qapp.processEvents()
        assert engine.position() <= paused_at + 0.2
        assert not engine.is_playing()

        engine.set_paused(False)
        engine.seek(4.0)
        assert _wait(qapp, lambda: engine.position() >= 3.8, timeout_s=15)

        engine.set_volume(30)
        assert engine.volume() == 30

        engine.stop()
        time.sleep(0.2)
        qapp.processEvents()
        assert not engine.is_playing()
    finally:
        engine.stop()
