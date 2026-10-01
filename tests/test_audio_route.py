"""音频路由自动暂停/恢复回归(耳机断开自动暂停,切回自动恢复)。

策略部分(RoutePausePolicy/classify_device)为纯逻辑离线单测;守卫部分
(AudioRouteGuard)注入假快照提供者驱动,不碰真实音频设备。

语义对照 reference/NeriPlayer-Android:
- 有线路由丢失 → 立即暂停(BECOMING_NOISY → pauseForAudioRouteLoss)
- 蓝牙路由丢失 → 确认后暂停(采样防抖)
- 路由回到耳机类 → 恢复(handleDeviceChange → restore)
- 插入耳机/耳机间切换 → 不暂停(新路由仍是耳机类)
- 用户手动操作 → 撤销自动恢复意图
"""

from __future__ import annotations

from neriplayer_win.ui.audio_route import (
    KIND_BLUETOOTH,
    KIND_SPEAKER,
    KIND_UNKNOWN,
    KIND_WIRED,
    AudioDevice,
    AudioRouteGuard,
    RoutePausePolicy,
    classify_device,
)


def _dev(kind: str, name: str, uid: str | None = None) -> AudioDevice:
    return AudioDevice(uid or f"{{{kind}-{name}}}", name, kind)


WIRED = _dev(KIND_WIRED, "耳机 (Realtek(R) Audio)", "{wired-1}")
WIRED_USB = _dev(KIND_WIRED, "Headset (USB)", "{wired-2}")
BT = _dev(KIND_BLUETOOTH, "耳机 (WH-1000XM5 Stereo)", "{bt-1}")
SPEAKER = _dev(KIND_SPEAKER, "扬声器 (Realtek(R) Audio)", "{spk-1}")
UNKNOWN = _dev(KIND_UNKNOWN, "HDMI Output", "{hdmi-1}")


# ---------------------------------------------------------------------------
# 设备分类
# ---------------------------------------------------------------------------


def test_classify_by_form_factor():
    assert classify_device("任意名字", form_factor=3) == KIND_WIRED
    assert classify_device("任意名字", form_factor=5) == KIND_WIRED
    assert classify_device("任意名字", form_factor=1) == KIND_SPEAKER
    assert classify_device("任意名字", form_factor=8) == KIND_UNKNOWN  # SPDIF


def test_classify_by_name_keywords():
    assert classify_device("耳机 (Realtek(R) Audio)") == KIND_WIRED
    assert classify_device("Headphones (USB Audio)") == KIND_WIRED
    assert classify_device("WH-1000XM5") == KIND_UNKNOWN  # 无关键词不打耳机标
    assert classify_device("扬声器 (Realtek(R) Audio)") == KIND_SPEAKER
    assert classify_device("Speakers (HDMI)") == KIND_SPEAKER


def test_classify_bluetooth_by_enumerator_or_name():
    # 蓝牙走 BTHENUM/BTHLEENUM 总线枚举器(最可靠,名称可能毫无特征)
    assert classify_device("WH-1000XM5", enumerator_name="BTHENUM") == KIND_BLUETOOTH
    assert classify_device("WH-1000XM5", enumerator_name="BTHLEENUM") == KIND_BLUETOOTH
    assert classify_device("Bluetooth Audio") == KIND_BLUETOOTH
    assert classify_device("蓝牙耳机") == KIND_BLUETOOTH


def test_classify_bluetooth_wins_over_headphone_name():
    # 名字带「耳机」但走蓝牙总线:归蓝牙(断开时走确认防抖而非立即停)
    assert (
        classify_device("耳机 (WH-1000XM5)", form_factor=3, enumerator_name="BTHENUM")
        == KIND_BLUETOOTH
    )


def test_classify_empty_and_unreadable_props():
    assert classify_device("") == KIND_UNKNOWN
    assert classify_device("", form_factor=None, enumerator_name=None) == KIND_UNKNOWN


# ---------------------------------------------------------------------------
# 策略:暂停判定
# ---------------------------------------------------------------------------


def test_wired_disconnect_pauses_immediately():
    policy = RoutePausePolicy()
    decision = policy.on_route_changed(WIRED, SPEAKER, playing=True)
    assert decision is not None and decision.action == "pause"
    assert policy.route_paused


def test_disconnect_ignored_when_not_playing():
    policy = RoutePausePolicy()
    assert policy.on_route_changed(WIRED, SPEAKER, playing=False) is None
    assert not policy.route_paused


def test_plugging_in_does_not_pause():
    # 扬声器 → 插耳机:新路由仍是耳机类,无外放风险,不暂停(对齐 Android)
    policy = RoutePausePolicy()
    assert policy.on_route_changed(SPEAKER, WIRED, playing=True) is None


def test_headset_to_headset_does_not_pause():
    policy = RoutePausePolicy()
    assert policy.on_route_changed(WIRED, BT, playing=True) is None
    assert policy.on_route_changed(BT, WIRED_USB, playing=True) is None


def test_device_gone_entirely_pauses():
    policy = RoutePausePolicy()
    decision = policy.on_route_changed(WIRED, None, playing=True)
    assert decision is not None and decision.action == "pause"


def test_speaker_route_change_never_pauses():
    policy = RoutePausePolicy()
    assert policy.on_route_changed(SPEAKER, UNKNOWN, playing=True) is None
    assert policy.on_route_changed(UNKNOWN, SPEAKER, playing=True) is None


def test_first_snapshot_is_baseline_only():
    policy = RoutePausePolicy()
    assert policy.on_route_changed(None, SPEAKER, playing=True) is None
    assert policy.on_route_changed(None, WIRED, playing=True) is None


# ---------------------------------------------------------------------------
# 策略:恢复判定
# ---------------------------------------------------------------------------


def test_route_return_resumes_after_route_pause():
    policy = RoutePausePolicy()
    policy.on_route_changed(WIRED, SPEAKER, playing=True)  # 暂停
    decision = policy.on_route_changed(SPEAKER, WIRED, playing=False)
    assert decision is not None and decision.action == "resume"
    assert not policy.route_paused  # 恢复意图已消费
    # 再切一次耳机:意图已清,不再恢复
    assert policy.on_route_changed(SPEAKER, BT, playing=False) is None


def test_resume_only_after_actual_route_pause():
    # 没因路由暂停过(手动暂停)→ 耳机插回不自动恢复
    policy = RoutePausePolicy()
    assert policy.on_route_changed(SPEAKER, WIRED, playing=False) is None


def test_user_action_cancels_resume_intent():
    policy = RoutePausePolicy()
    policy.on_route_changed(WIRED, SPEAKER, playing=True)  # 自动暂停
    policy.user_action()  # 用户手动播放/暂停
    assert policy.on_route_changed(SPEAKER, WIRED, playing=False) is None


def test_disconnect_while_idle_keeps_waiting_for_headset():
    # 没在播时路由丢了不暂停;但此后耳机回来也不恢复(从未因路由暂停)
    policy = RoutePausePolicy()
    policy.on_route_changed(WIRED, SPEAKER, playing=False)
    assert policy.on_route_changed(SPEAKER, WIRED, playing=False) is None


# ---------------------------------------------------------------------------
# 策略:蓝牙确认防抖
# ---------------------------------------------------------------------------


def test_bluetooth_disconnect_waits_for_confirmation():
    policy = RoutePausePolicy()
    assert policy.on_route_changed(BT, SPEAKER, playing=True) is None
    assert policy.bluetooth_confirm_pending
    assert not policy.route_paused  # 确认期间不暂停


def test_bluetooth_confirmed_pause_after_timeout():
    policy = RoutePausePolicy()
    policy.on_route_changed(BT, SPEAKER, playing=True)
    decision = policy.on_bluetooth_confirm_timeout(SPEAKER, playing=True)
    assert decision is not None and decision.action == "pause"
    assert policy.route_paused


def test_bluetooth_transient_flap_does_not_pause():
    policy = RoutePausePolicy()
    policy.on_route_changed(BT, SPEAKER, playing=True)  # 挂起确认
    # 确认到点时路由已回耳机类(抖动/快速重连)→ 不停,且不留恢复意图
    decision = policy.on_bluetooth_confirm_timeout(BT, playing=True)
    assert decision is None
    assert not policy.route_paused
    assert not policy.bluetooth_confirm_pending


def test_bluetooth_route_return_before_timeout_cancels_confirm():
    policy = RoutePausePolicy()
    policy.on_route_changed(BT, SPEAKER, playing=True)  # 挂起确认
    decision = policy.on_route_changed(SPEAKER, BT, playing=True)  # 路由回来了
    assert decision is None  # 播放中回耳机不恢复(从未暂停),撤销确认
    assert not policy.bluetooth_confirm_pending
    # 迟到的确认到点:无在途确认,无事发生
    assert policy.on_bluetooth_confirm_timeout(BT, playing=True) is None


def test_bluetooth_confirm_respects_user_pause_during_window():
    policy = RoutePausePolicy()
    policy.on_route_changed(BT, SPEAKER, playing=True)
    # 确认窗口内用户自己暂停了
    decision = policy.on_bluetooth_confirm_timeout(SPEAKER, playing=False)
    assert decision is None
    assert not policy.route_paused  # 不标记路由暂停 → 之后不自动恢复


# ---------------------------------------------------------------------------
# 守卫:快照对比与信号(注入假提供者)
# ---------------------------------------------------------------------------


class _FakeProvider:
    """脚本化快照提供者:测试直接改 current 模拟设备切换。"""

    def __init__(self) -> None:
        self.current: AudioDevice | None = None
        self.calls = 0

    def default_render_device(self) -> AudioDevice | None:
        self.calls += 1
        return self.current


class _Recorder:
    def __init__(self, guard: AudioRouteGuard) -> None:
        self.paused: list[str] = []
        self.resumed: list[str] = []
        guard.pause_requested.connect(self.paused.append)
        guard.resume_requested.connect(self.resumed.append)


def _make_guard(qapp, **kwargs) -> tuple[AudioRouteGuard, _FakeProvider, _Recorder]:
    provider = _FakeProvider()
    guard = AudioRouteGuard(parent=None, provider=provider, **kwargs)
    guard.start()  # 首次 poll_now 建立基线
    return guard, provider, _Recorder(guard)


def test_guard_pauses_and_resumes_on_real_flow(qapp):
    guard, provider, rec = _make_guard(qapp)
    try:
        provider.current = WIRED
        guard.poll_now()  # 基线:耳机
        guard.set_playing(True)
        provider.current = SPEAKER
        guard.poll_now()  # 拔耳机 → 扬声器
        assert len(rec.paused) == 1, "拔出有线耳机应自动暂停"
        guard.set_playing(False)  # engine.playing_changed(False) 回灌
        provider.current = WIRED
        guard.poll_now()  # 插回耳机
        assert len(rec.resumed) == 1, "切回耳机应自动恢复"
    finally:
        guard.stop()


def test_guard_ignores_same_device_updates(qapp):
    guard, provider, rec = _make_guard(qapp)
    try:
        provider.current = WIRED
        guard.poll_now()
        guard.set_playing(True)
        provider.current = AudioDevice(WIRED.endpoint_id, WIRED.name, WIRED.kind)
        guard.poll_now()
        assert not rec.paused and not rec.resumed
    finally:
        guard.stop()


def test_guard_bluetooth_confirm_via_timer_path(qapp):
    guard, provider, rec = _make_guard(qapp, bluetooth_confirm_ms=30)
    try:
        provider.current = BT
        guard.poll_now()
        guard.set_playing(True)
        provider.current = SPEAKER
        guard.poll_now()
        assert not rec.paused, "蓝牙断开需等确认,不立即暂停"
        guard._on_bluetooth_confirm()  # 到点复核(设备仍非耳机类)
        assert len(rec.paused) == 1
    finally:
        guard.stop()


def test_guard_user_action_blocks_resume(qapp):
    guard, provider, rec = _make_guard(qapp)
    try:
        provider.current = WIRED
        guard.poll_now()
        guard.set_playing(True)
        provider.current = SPEAKER
        guard.poll_now()
        assert len(rec.paused) == 1
        guard.notify_user_action()  # 用户手动按了播放/暂停
        provider.current = WIRED
        guard.poll_now()
        assert not rec.resumed, "用户操作后不应再自动恢复"
    finally:
        guard.stop()


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------


def test_i18n_route_keys_exist():
    from neriplayer_win import i18n

    for key in ("route.paused", "route.resumed"):
        zh = i18n.tr(key)
        i18n.set_language("en")
        try:
            en = i18n.tr(key)
        finally:
            i18n.set_language("zh")
        assert zh != key and en != key, f"文案键缺失: {key}"
        assert zh != en, f"中英文案相同(疑似漏翻): {key}"
