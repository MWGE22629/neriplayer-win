"""音频路由监视:耳机断开/切换设备自动暂停,切回耳机类设备自动恢复。

对照 reference/NeriPlayer-Android 蒸馏出的语义(去掉 USB 独占与
「一起听」等 Win 端不存在的能力):

- 有线路由丢失(默认输出从耳机类切走/回落到扬声器)且在播 → 立即暂停
  (Android: ACTION_AUDIO_BECOMING_NOISY → pauseForAudioRouteLoss,
  shouldPauseForImmediateOutputDisconnect)
- 蓝牙路由丢失 → 短确认后再暂停(蓝牙路由抖动常见:切编解码/挂电话/
  双连接切换都会让默认设备来回跳,Android 侧采样确认后才停)
- 播放路由回到耳机类设备 → 恢复播放(Android: handleDeviceChange 中
  newDevice != TYPE_BUILTIN_SPEAKER → restore)
- 插入耳机、或在两个耳机类设备之间切换 → 不暂停(新路由仍是耳机类,
  无外放风险;Android 同样只在回落到内放扬声器时才停)
- 用户任何手动播放/暂停动作 → 撤销自动恢复意图(用户意图优先)

线程模型(对齐 player/engine.py 的哲学):不注册任何系统回调
(IMMNotificationClient 的 COM 回调从 RPC 线程进来,跨线程边界难测难
控),改用 UI 线程 QTimer 快照对比:每秒一次 GetDefaultAudioEndpoint
(纯出站 COM 调用,微秒级)+ 注册表读端点属性(端点 id 不变时走缓存)。

设备分类的数据来源(注册表 MMDevices\\Audio\\Render\\{id}\\Properties,
键名经本机实测锚定):
- 友好名  {a45c254e-df1c-4efd-8020-67d146a850e0},2   如「耳机 (Realtek(R) Audio)」
- 形态因子 {1da5d803-d492-4edd-8c23-e0c0ffee7f0e},0   1=扬声器 3=耳机 5=头戴式耳机
- 总线枚举器 {a45c254e-df1c-4efd-8020-67d146a850e0},24  HDAUDIO/USB/BTHENUM/BTHLEENUM
"""

from __future__ import annotations

import ctypes
import sys
import winreg
from dataclasses import dataclass
from uuid import UUID

from PySide6.QtCore import QObject, QTimer, Signal

from ..log import get_logger

logger = get_logger("audio_route")

# 设备分类(对照 PlayerAudioDeviceHelper.kt 的 isBluetoothOutputType /
# isWiredOutputType / isHeadsetLikeOutput)
KIND_BLUETOOTH = "bluetooth"
KIND_WIRED = "wired"  # 有线耳机类:3.5mm 耳机 / USB 耳机 / USB 声卡耳放口
KIND_SPEAKER = "speaker"  # 扬声器类:内放/外接音箱/HDMI
KIND_UNKNOWN = "unknown"  # 其余(SPDIF/未识别 DAC):不触发任何自动动作
HEADSET_LIKE = (KIND_BLUETOOTH, KIND_WIRED)

# Windows EndpointFormFactor(mmdeviceapi.h):3=Headphones 5=Headset
_FORM_FACTOR_HEADPHONES = 3
_FORM_FACTOR_HEADSET = 5
_FORM_FACTOR_SPEAKERS = 1

# 名称分类关键词(casefold 匹配;覆盖中英文系统与常见 TWS 命名)
_BT_NAME_MARKERS = ("bluetooth", "蓝牙", "ble-", "le-audio")
_WIRED_NAME_MARKERS = (
    "耳机",
    "头戴",
    "headphone",
    "headset",
    "earphone",
    "earbud",
    "airpods",
    "earpods",
)
_SPEAKER_NAME_MARKERS = ("扬声器", "喇叭", "speaker")


@dataclass(frozen=True)
class AudioDevice:
    """一个默认输出端点的快照;kind 见上面的分类常量。"""

    endpoint_id: str
    name: str
    kind: str

    def headset_like(self) -> bool:
        return self.kind in HEADSET_LIKE

    def describe(self) -> str:
        return f"{self.kind}:{self.name}"


def classify_device(
    name: str,
    form_factor: int | None = None,
    enumerator_name: str | None = None,
) -> str:
    """把端点属性归类;蓝牙判定优先于名称耳机关键词(断开防抖只对蓝牙)。

    - 蓝牙:总线枚举器 BTHENUM/BTHLEENUM(可靠),或名称含 bluetooth/蓝牙
    - 有线耳机类:形态因子 Headphones/Headset(可靠),或名称含 耳机/headphone 等
    - 扬声器:形态因子 Speakers,或名称含 扬声器/speaker
    - 其余 unknown(只记录,不触发动作)
    """
    folded = (name or "").casefold()
    if enumerator_name:
        bus = enumerator_name.casefold()
        if bus.startswith("bth"):
            return KIND_BLUETOOTH
    if any(marker in folded for marker in _BT_NAME_MARKERS):
        return KIND_BLUETOOTH
    if form_factor in (_FORM_FACTOR_HEADPHONES, _FORM_FACTOR_HEADSET):
        return KIND_WIRED
    if any(marker in folded for marker in _WIRED_NAME_MARKERS):
        return KIND_WIRED
    if form_factor == _FORM_FACTOR_SPEAKERS:
        return KIND_SPEAKER
    if any(marker in folded for marker in _SPEAKER_NAME_MARKERS):
        return KIND_SPEAKER
    return KIND_UNKNOWN


@dataclass(frozen=True)
class RouteDecision:
    """策略输出:action ∈ {"pause", "resume"},reason 供日志留痕。"""

    action: str
    reason: str


class RoutePausePolicy:
    """纯逻辑状态机:喂路由快照与播放态,产出暂停/恢复决策。

    无 Qt 依赖,可离线单测;蓝牙确认计时由宿主(AudioRouteGuard)
    驱动 on_bluetooth_confirm_timeout。
    """

    def __init__(self) -> None:
        self._route_paused = False  # 因路由丢失自动暂停过(可自动恢复)
        self._bt_confirm_pending = False  # 蓝牙断开确认在途

    # -- 状态查询 --------------------------------------------------------

    @property
    def route_paused(self) -> bool:
        return self._route_paused

    @property
    def bluetooth_confirm_pending(self) -> bool:
        return self._bt_confirm_pending

    # -- 输入 ------------------------------------------------------------

    def on_route_changed(
        self,
        old: AudioDevice | None,
        new: AudioDevice | None,
        playing: bool,
    ) -> RouteDecision | None:
        """默认输出设备从 old 切到 new(old=None 表示首次快照)。"""
        left_headset = (
            old is not None
            and old.headset_like()
            and (new is None or not new.headset_like())
        )
        returned_headset = new is not None and new.headset_like()

        if left_headset:
            if not playing:
                # 没在播:路由丢了也不暂停;若此前因路由暂停过(没有恢复
                # 意图之外的用户操作),保持可恢复状态等耳机回来
                return None
            if old.kind == KIND_BLUETOOTH:
                # 蓝牙断开先挂起确认(防路由抖动),由宿主计时复核
                self._bt_confirm_pending = True
                return None
            self._route_paused = True
            return RouteDecision("pause", f"wired_left:{old.describe()}")

        if returned_headset:
            self._bt_confirm_pending = False  # 抖动回来了/耳机插回:撤销确认
            if self._route_paused:
                self._route_paused = False
                return RouteDecision("resume", f"headset_returned:{new.describe()}")
        return None

    def on_bluetooth_confirm_timeout(
        self,
        current: AudioDevice | None,
        playing: bool,
    ) -> RouteDecision | None:
        """蓝牙断开确认到点:current 为此刻的默认设备快照。"""
        if not self._bt_confirm_pending:
            return None
        self._bt_confirm_pending = False
        if current is not None and current.headset_like():
            return None  # 确认期间路由回耳机类:抖动,不停
        if not playing:
            return None  # 确认期间用户自己停了,尊重用户
        self._route_paused = True
        return RouteDecision("pause", "bluetooth_confirmed_left")

    def user_action(self) -> None:
        """用户手动播放/暂停/切歌:撤销自动恢复意图与在途蓝牙确认。"""
        self._route_paused = False
        self._bt_confirm_pending = False


# -- Windows 默认输出端点读取 -------------------------------------------------

_CLSID_MMDeviceEnumerator = UUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnumerator = UUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")

# IMMDeviceEnumerator vtable: 0-2=IUnknown, 4=GetDefaultAudioEndpoint
# IMMDevice vtable: 0-2=IUnknown, 2=Release, 5=GetId
_get_default_audio_endpoint_proto = ctypes.WINFUNCTYPE(
    ctypes.HRESULT,
    ctypes.c_void_p,  # this
    ctypes.c_ulong,  # eDataFlow (eRender=0)
    ctypes.c_ulong,  # eRole (eMultimedia=1)
    ctypes.POINTER(ctypes.c_void_p),  # out IMMDevice**
)
_get_id_proto = ctypes.WINFUNCTYPE(
    ctypes.HRESULT,
    ctypes.c_void_p,  # this
    ctypes.POINTER(ctypes.c_void_p),  # out LPWSTR*
)
_release_proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

# 注册表端点属性键(见模块 docstring 的实测锚定)
_REGISTRY_RENDER_ROOT = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
)
_PKEY_FRIENDLY_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_PKEY_FORM_FACTOR = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},0"
_PKEY_ENUMERATOR = "{a45c254e-df1c-4efd-8020-67d146a850e0},24"


def _guid_bytes(guid: UUID):
    return (ctypes.c_ubyte * 16).from_buffer_copy(guid.bytes_le)


def _vtable_func(obj: int, index: int, prototype):
    """读 COM 对象 vtable 第 index 项并转成可调用原型。"""
    vtable = ctypes.cast(
        ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0],
        ctypes.POINTER(ctypes.c_void_p),
    )
    return prototype(vtable[index])


def read_endpoint_properties(endpoint_id: str) -> tuple[str | None, int | None, str | None]:
    """从注册表读 (友好名, 形态因子, 总线枚举器);键缺失/不可读返回 None 项。"""
    # 端点 id 形如 "{0.0.0.00000000}.{8ada3d4c-...}",注册表键名取最后的 guid 段
    key_name = endpoint_id.rsplit(".", 1)[-1]
    path = _REGISTRY_RENDER_ROOT + "\\" + key_name + "\\Properties"
    values: dict[str, object] = {}
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as key:
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, index)
                values[name] = value
    except OSError:
        return None, None, None
    name = values.get(_PKEY_FRIENDLY_NAME)
    form_factor = values.get(_PKEY_FORM_FACTOR)
    enumerator_name = values.get(_PKEY_ENUMERATOR)
    return (
        name if isinstance(name, str) else None,
        form_factor if isinstance(form_factor, int) else None,
        enumerator_name if isinstance(enumerator_name, str) else None,
    )


class WindowsDefaultEndpointProvider:
    """默认输出端点快照提供者(ctypes 出站 COM 调用,win32 专用)。

    任何一步失败都返回 None(此刻没有可用默认设备/COM 不可用),
    不抛异常——守卫把 None 视为「路由丢失」。
    """

    def __init__(self) -> None:
        self._ole32 = ctypes.OleDLL("ole32")
        # GUI 线程通常已被 Qt 初始化为 STA;重复初始化的错误直接忽略
        try:
            self._ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        except OSError:
            pass
        self._props_cache: dict[str, tuple[str | None, int | None, str | None]] = {}
        self._clsid = _guid_bytes(_CLSID_MMDeviceEnumerator)
        self._iid = _guid_bytes(_IID_IMMDeviceEnumerator)

    def default_render_device(self) -> AudioDevice | None:
        try:
            return self._snapshot()
        except OSError as exc:  # COM/注册表层错误:视为无默认设备
            logger.warning("default endpoint snapshot failed: %s", exc)
            return None

    def _snapshot(self) -> AudioDevice | None:
        enumerator = ctypes.c_void_p()
        self._ole32.CoCreateInstance(
            ctypes.cast(self._clsid, ctypes.c_void_p),
            None,
            0x1,  # CLSCTX_INPROC_SERVER
            ctypes.cast(self._iid, ctypes.c_void_p),
            ctypes.byref(enumerator),
        )
        if not enumerator.value:
            return None
        try:
            device = ctypes.c_void_p()
            hr = _vtable_func(
                enumerator.value, 4, _get_default_audio_endpoint_proto
            )(enumerator, 0, 1, ctypes.byref(device))
            if hr != 0 or not device.value:
                return None
            try:
                wid_ptr = ctypes.c_void_p()
                hr = _vtable_func(device.value, 5, _get_id_proto)(
                    device, ctypes.byref(wid_ptr)
                )
                if hr != 0 or not wid_ptr.value:
                    return None
                endpoint_id = ctypes.wstring_at(wid_ptr.value)
                self._ole32.CoTaskMemFree(wid_ptr)
            finally:
                _vtable_func(device.value, 2, _release_proto)(device)
        finally:
            _vtable_func(enumerator.value, 2, _release_proto)(enumerator)

        props = self._cached_props(endpoint_id)
        name, form_factor, enumerator_name = props
        kind = classify_device(name or endpoint_id, form_factor, enumerator_name)
        return AudioDevice(endpoint_id, name or endpoint_id, kind)

    def _cached_props(
        self, endpoint_id: str
    ) -> tuple[str | None, int | None, str | None]:
        """端点属性缓存:同一 id 的名字/形态不会变,注册表只读一次。"""
        props = self._props_cache.get(endpoint_id)
        if props is None:
            props = read_endpoint_properties(endpoint_id)
            # 小容量 FIFO:常驻端点数量个位数,防长期运行的无限增长
            if len(self._props_cache) >= 16:
                self._props_cache.pop(next(iter(self._props_cache)))
            self._props_cache[endpoint_id] = props
        return props


class AudioRouteGuard(QObject):
    """组合 监视 QTimer + 路由策略,输出 pause/resume 请求信号。

    用法:MainWindow 创建后 start() 常驻轮询(快照始终新鲜,播放与否
    只影响决策);engine 的 playing_changed 喂 set_playing;用户手动
    播放/暂停路径调 notify_user_action。常开能力,无开关。
    """

    pause_requested = Signal(str)  # reason
    resume_requested = Signal(str)  # reason

    def __init__(
        self,
        parent: QObject | None = None,
        poll_interval_ms: int = 1000,
        bluetooth_confirm_ms: int = 1200,
        provider: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._policy = RoutePausePolicy()
        self._playing = False
        self._provider = provider  # None 时懒创建真实提供者(win32)
        self._last: AudioDevice | None = None

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(poll_interval_ms)
        self._poll_timer.timeout.connect(self.poll_now)

        self._bt_timer = QTimer(self)
        self._bt_timer.setSingleShot(True)
        self._bt_timer.setInterval(bluetooth_confirm_ms)
        self._bt_timer.timeout.connect(self._on_bluetooth_confirm)

    # -- 生命周期 --------------------------------------------------------

    def start(self) -> None:
        """开始轮询;首次快照只记录基线,不触发任何决策。"""
        if self._provider is None:
            if sys.platform != "win32":
                raise RuntimeError("AudioRouteGuard 真实提供者仅支持 Windows")
            self._provider = WindowsDefaultEndpointProvider()
        self.poll_now()
        self._poll_timer.start()

    def stop(self) -> None:
        self._poll_timer.stop()
        self._bt_timer.stop()

    # -- 外部输入 --------------------------------------------------------

    def set_playing(self, playing: bool) -> None:
        self._playing = playing

    def notify_user_action(self) -> None:
        """用户手动播放/暂停/切歌:撤销自动恢复意图与在途蓝牙确认。"""
        self._bt_timer.stop()
        self._policy.user_action()

    # -- 轮询 ------------------------------------------------------------

    def poll_now(self) -> None:
        """取一次快照并与上次对比;变化时交策略决策。"""
        provider = self._provider
        current: AudioDevice | None = None
        if provider is not None:
            current = provider.default_render_device()  # type: ignore[attr-defined]
        old = self._last
        if old is not None and current is not None and old.endpoint_id == current.endpoint_id:
            self._last = current
            return
        if old is None and current is None:
            return
        self._last = current
        if old is None:
            return  # 首次基线:只记快照
        logger.info("audio route changed: %s -> %s", old.describe(),
                    current.describe() if current else "none")
        decision = self._policy.on_route_changed(old, current, self._playing)
        if decision is not None:
            self._dispatch(decision)
        if self._policy.bluetooth_confirm_pending:
            self._bt_timer.start()
        else:
            self._bt_timer.stop()

    def _on_bluetooth_confirm(self) -> None:
        current: AudioDevice | None = None
        if self._provider is not None:
            current = self._provider.default_render_device()  # type: ignore[attr-defined]
        decision = self._policy.on_bluetooth_confirm_timeout(current, self._playing)
        if decision is not None:
            self._dispatch(decision)

    def _dispatch(self, decision: RouteDecision) -> None:
        logger.info("route decision: %s (%s)", decision.action, decision.reason)
        if decision.action == "pause":
            self.pause_requested.emit(decision.reason)
        elif decision.action == "resume":
            self.resume_requested.emit(decision.reason)

    # -- 测试辅助 ---------------------------------------------------------

    @property
    def policy(self) -> RoutePausePolicy:
        return self._policy
