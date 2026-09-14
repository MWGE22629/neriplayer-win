"""B站 API 数据模型与异常体系。

对照 reference/NeriPlayer-Android:
- core/api/bili/BiliClient.kt 的 data class(PlayInfo/DashStream/Durl/
  FavFolder/FavResourceItem/VideoBasicInfo/VideoPage)
- data/platform/bili/BiliAudioSelector.kt 的 BiliAudioStreamInfo
- data/auth/bili/BiliCookieRepository.kt 的登录 cookie 键(SESSDATA /
  DedeUserID / bili_jct)
"""

from __future__ import annotations

from dataclasses import dataclass, field

# BiliCookieRepository.kt 的 BILI_LOGIN_COOKIE_KEYS
BILI_LOGIN_COOKIE_KEYS = ("SESSDATA", "DedeUserID", "bili_jct")


class BiliApiError(Exception):
    """B站接口通用异常(网络失败 / HTTP 错误 / 返回异常)。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class BiliAuthRequiredError(BiliApiError):
    """登录态缺失或已失效,需要重新登录。"""


class BiliNoAudioStreamError(BiliApiError):
    """视频没有可用的音频流(纯图稿 / 地区受限 / 充款专属等)。"""


@dataclass(frozen=True)
class BiliAccount:
    """登录账号(对应 nav 接口的 data.mid / data.uname)。"""

    mid: int
    uname: str = ""


@dataclass(frozen=True)
class BiliAudioStreamInfo:
    """统一音频流结构(对应 BiliAudioSelector.kt 的 BiliAudioStreamInfo)。

    quality_tag: "dolby" / "hires" / None(普通音轨)。
    """

    id: int | None
    mime_type: str
    bitrate_kbps: int
    quality_tag: str | None
    url: str
    candidate_urls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.candidate_urls:
            object.__setattr__(self, "candidate_urls", [self.url])


@dataclass(frozen=True)
class BiliDurl:
    """MP4 进度格式的一段(对应 BiliClient.Durl)。"""

    order: int
    length_ms: int
    size_bytes: int
    url: str
    backup_urls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BiliDashStream:
    """DASH 的一条流(音/视频通用,对应 BiliClient.DashStream)。"""

    id: int
    base_url: str
    backup_urls: list[str] = field(default_factory=list)
    bandwidth: int = 0
    mime_type: str = ""
    codecs: str = ""
    width: int = 0
    height: int = 0
    frame_rate: str = ""
    codecid: int = 0


@dataclass(frozen=True)
class BiliDolbyAudio:
    """对应 BiliClient.DolbyAudio。"""

    type: int
    audios: list[BiliDashStream] = field(default_factory=list)


@dataclass(frozen=True)
class BiliFlacAudio:
    """对应 BiliClient.FlacAudio。"""

    display: bool
    audio: BiliDashStream | None = None


@dataclass(frozen=True)
class BiliPlayInfo:
    """playurl 统一封装(对应 BiliClient.PlayInfo;省略 raw)。"""

    code: int
    message: str
    qn_selected: int | None
    format: str | None
    time_length_ms: int | None
    accept_description: list[str] = field(default_factory=list)
    accept_quality: list[int] = field(default_factory=list)
    durl: list[BiliDurl] = field(default_factory=list)
    dash_video: list[BiliDashStream] = field(default_factory=list)
    dash_audio: list[BiliDashStream] = field(default_factory=list)
    dolby: BiliDolbyAudio | None = None
    flac: BiliFlacAudio | None = None


@dataclass(frozen=True)
class BiliVideoPage:
    """视频分 P(对应 BiliClient.VideoPage)。"""

    cid: int
    page: int
    part: str
    duration_sec: int
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class BiliFavFolder:
    """收藏夹(对应 BiliClient.FavFolder;item_type 11=收藏夹 21=合集)。"""

    media_id: int
    fid: int
    mid: int
    title: str
    cover_url: str = ""
    intro: str = ""
    count: int = 0
    like_count: int | None = None
    play_count: int | None = None
    collect_count: int | None = None
    upper_name: str = ""
    attr: int = 0
    state: int = 0
    item_type: int = 11


@dataclass(frozen=True)
class BiliFavItem:
    """收藏夹内一条内容(对应 BiliClient.FavResourceItem)。

    type: 2 视频稿件 / 12 音频 / 21 合集;仅 type=2 且有 bvid 的可播。
    """

    type: int
    id: int
    bvid: str | None
    title: str
    cover_url: str = ""
    intro: str = ""
    duration_sec: int = 0
    upper_mid: int = 0
    upper_name: str = ""
    play: int | None = None
    danmaku: int | None = None
    fav_time: int | None = None

    @property
    def playable(self) -> bool:
        return self.type == 2 and bool(self.bvid)

    def to_song(self) -> "BiliSong":
        """映射为歌单视角的歌曲(标题/UP主/时长;cid 播放时再解析)。"""
        title = self.title or (self.bvid or "")
        return BiliSong(
            avid=self.id,
            bvid=self.bvid or "",
            title=title,
            upper_name=self.upper_name,
            duration_sec=self.duration_sec,
            duration_ms=self.duration_sec * 1000,
        )


@dataclass(frozen=True)
class BiliFavFolderPage:
    """收藏夹内容分页(对应 BiliClient.FavResourcePage)。"""

    info: BiliFavFolder
    items: list[BiliFavItem] = field(default_factory=list)
    has_more: bool = False


@dataclass(frozen=True)
class BiliSong:
    """歌单视角的一首"B站歌曲"(收藏夹条目映射;cid 播放时解析)。"""

    avid: int
    bvid: str
    title: str
    upper_name: str
    duration_sec: int
    cid: int = 0
    duration_ms: int = 0

    @property
    def artist_line(self) -> str:
        return self.upper_name or "UP主未知"


@dataclass(frozen=True)
class BiliQrLoginSession:
    """QR 登录会话(对应 BiliQrLoginClient.BiliQrLoginSession)。"""

    key: str
    qr_content: str


@dataclass(frozen=True)
class BiliQrLoginCheckResult:
    """QR 轮询结果(对应 BiliQrLoginClient.BiliQrLoginCheckResult)。

    code: 0 成功 / 86038 过期 / 86101 未扫码 / 86090 已扫待确认。
    """

    code: int
    message: str
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def is_confirmed(self) -> bool:
        return self.code == 0
