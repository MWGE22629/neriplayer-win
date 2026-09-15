"""网易云 API 数据模型与异常类型。"""

from __future__ import annotations

from dataclasses import dataclass, field


class NeteaseApiError(Exception):
    """网易云接口通用异常(网络失败 / HTTP 错误 / 返回异常)。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class NeteaseAuthRequiredError(NeteaseApiError):
    """登录态缺失或已失效(接口返回 301 等),需要重新登录。"""


class NeteaseNoPermissionError(NeteaseApiError):
    """无权限播放(VIP / 版权受限 / 无可用音质)。"""


class NeteaseNoPlayUrlError(NeteaseApiError):
    """接口未返回播放地址。"""


@dataclass(frozen=True)
class NeteaseYdSnapshot:
    """易盾(YD)指纹快照,对应 Kotlin NeteaseYdDeviceSnapshot。

    token: createNEFingerprint 生成的设备令牌;空串表示不可用(回退路径)。
    s_device_id: 指纹页下发的 sDeviceId Cookie,参与登录 chainId 生成。
    cookies: 指纹页收集到的域 Cookie,创建扫码会话前种进 API 会话。
    """

    token: str = ""
    s_device_id: str = ""
    cookies: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NeteaseAccount:
    user_id: int
    nickname: str


@dataclass(frozen=True)
class QrLoginSession:
    """扫码登录会话(对应 Kotlin NeteaseQrLoginSession)。"""

    key: str
    chain_id: str
    yd_device_token: str
    qr_content: str


@dataclass(frozen=True)
class QrLoginCheckResult:
    """扫码轮询结果(对应 Kotlin NeteaseQrLoginCheckResult)。

    code: 800 过期 / 801 等待扫码 / 802 已扫待确认 / 803 成功。
    """

    code: int
    message: str
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def is_confirmed(self) -> bool:
        return self.code == 803


@dataclass(frozen=True)
class NeteasePlaylist:
    id: int
    name: str
    track_count: int
    special_type: int = 0
    is_liked: bool = False


@dataclass(frozen=True)
class NeteaseUserPlaylists:
    """用户歌单按归属分流(user/playlist 响应一次解析的两份视图)。

    created: 「我喜欢的音乐」置前 + 自建歌单(creator==uid 或未订阅);
    subscribed: 收藏的他人歌单(subscribed==true 且 creator!=uid)。
    对应 Kotlin getUserCreatedPlaylists / getUserSubscribedPlaylists;
    两过滤器的并集划分,条目不会重复落侧。
    """

    created: list[NeteasePlaylist] = field(default_factory=list)
    subscribed: list[NeteasePlaylist] = field(default_factory=list)


@dataclass(frozen=True)
class NeteaseSong:
    id: int
    title: str
    artist: str
    duration_ms: int = 0
    cover_url: str = ""


@dataclass(frozen=True)
class PlayableUrl:
    url: str
    level: str | None = None
    is_preview: bool = False
