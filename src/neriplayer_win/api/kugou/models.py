"""酷狗 API 数据模型与异常体系。

字段与接口契约以 2026-10-23/24 本机实测为准(见 docs/research/M6-kugou-api.md),
不是照抄参考实现:reference/NeriPlayer-Android 里酷狗只作为歌词源存在,
没有播放音源实现可对照(TODO.md M6 立项说明)。

登录模型要点(实测):扫码登录下发的 token 有效期 30 天(`t_expire_time`),
可用 /v5/login_by_token 无感续期;网页登录 cookie 只有 24 小时且无法续期,
因此本项目只做扫码登录。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 登录态判定:扫码成功后一定同时拿到 userid 与 token
KUGOU_LOGIN_KEYS = ("userid", "token")

class KugouApiError(Exception):
    """酷狗接口通用异常(网络失败 / HTTP 错误 / 返回格式异常)。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code

class KugouAuthRequiredError(KugouApiError):
    """登录态缺失或已失效,需要重新扫码登录。

    与 NeteaseAuthRequiredError / BiliAuthRequiredError 同构,主窗口的
    _resolve_and_start 据此区分「该提示重登」与「普通播放失败」。
    """

class KugouNoPlayUrlError(KugouApiError):
    """歌曲没有可播放地址(无版权 / 付费未购 / 地区受限)。"""

class KugouRateLimitedError(KugouApiError):
    """请求被限流(实测密集请求触发,退避后可恢复)。

    与凭据失效区分开:限流不该提示用户重新登录,退避重试即可。
    早期调研把限流误判成凭据过期,导致反复排查签名,特此独立成类。
    """

# 错误码语义(实测,见 docs/research/M6-kugou-api.md §6.4)
ERR_SIGNATURE = 20006  # 签名错:盐值/排序/请求体拼接方式不对
ERR_PARAMS = 20010  # 参数错:缺必需参数或类型不对
ERR_BUSINESS = 20017  # 业务层拒绝:签名已过,查登录态或业务参数
ERR_UNAUTHORIZED = 30020  # 未授权:token 为空/过期/只放 query 没放 cookie
ERR_TOKEN_INVALID = 35005  # token 无效:常见于用已过期 token

# 凭据失效类错误码:上层应触发续期重试或提示重登
AUTH_ERROR_CODES = (ERR_UNAUTHORIZED, ERR_TOKEN_INVALID)
# 其中明确表示 token 本身无效(而非限流)的码
TOKEN_INVALID_CODES = (ERR_TOKEN_INVALID,)

@dataclass(frozen=True)
class KugouAccount:
    """酷狗账号(userid 与昵称)。"""

    userid: int
    nickname: str = ""

@dataclass(frozen=True)
class KugouSong:
    """搜索结果或歌单里的一首歌。

    酷狗用 `hash` 作播放标识,`encode_album_audio_id` 是更稳的播放主键;
    `album_id` 仅在高码率档解析时需要。三者都不是 int,所以 QueueSong
    侧用 extra 字典承载(见 data/store.py 的 LocalPlaylistItem 同一设计)。
    """

    hash: str
    title: str
    artist: str
    duration_ms: int
    # 播放解析主键(search 的 EMixSongID / 歌单条目的 emixsongid)
    encode_album_audio_id: str = ""
    album_id: str = ""
    # 各档位 hash:quality key -> hash
    hashes: dict[str, str] = field(default_factory=dict)
    cover_url: str = ""
    # 权限与付费标记(实测 Privilege / PayType);用于展示与失败提示
    privilege: int = 0
    pay_type: int = 0

    @property
    def playable_key(self) -> str:
        """播放解析首选键:encode_album_audio_id 优先,回落到 hash。"""
        return self.encode_album_audio_id or self.hash

@dataclass(frozen=True)
class KugouPlaylist:
    """用户歌单。

    listid 取曲目用,global_collection_id 查详情用,两者都要存
    (实测两者不可互推:曲目接口只认 listid,详情接口只认后者)。
    """

    listid: int
    global_collection_id: str
    name: str
    count: int = 0
    cover_url: str = ""
    # is_def: 1=默认收藏, 2=我喜欢, None=普通自建歌单(实测)
    is_def: int | None = None
    creator_id: int = 0

    @property
    def is_liked(self) -> bool:
        """是否为「我喜欢」歌单(实测 is_def == 2)。"""
        return self.is_def == 2

@dataclass(frozen=True)
class KugouPlayableUrl:
    """播放地址解析结果。"""

    url: str
    backup_url: str = ""
    bitrate_kbps: int = 0
    quality_key: str = ""
    duration_ms: int = 0

    @property
    def candidate_urls(self) -> list[str]:
        """主 URL + 备用 URL(供 BackupUrlRotator 轮换)。"""
        urls = [self.url]
        if self.backup_url and self.backup_url != self.url:
            urls.append(self.backup_url)
        return urls

@dataclass(frozen=True)
class KugouCredentials:
    """落盘的登录凭据,两条链路各一组(实测两者不可互推)。

    为什么需要两组(见 docs/research/M6-kugou-api.md §2.6):
    - `token` 系列来自扫码登录,有效期 30 天且可续期,用于歌单/收藏。
    - `kugoo_cookie` 是网页登录下发的 KuGoo 复合串(含 KugooPwd),
      播放接口只认它;KugooPwd 无法由 token 推导,也拿不到续期。
    只做其中一种登录会导致另一半功能不可用,所以两组都要落盘。

    t_expire_time 是服务端给的过期时间(epoch 秒),续期后刷新;
    启动时据此决定是否先静默续期。
    """

    userid: str
    token: str
    t1: str = ""
    vip_token: str = ""
    vip_type: int = 0
    t_expire_time: int = 0
    nickname: str = ""
    mid: str = ""
    dfid: str = ""
    # 网页登录的 KuGoo 复合串(播放接口鉴权载体);为空表示播放不可用
    kugoo_cookie: str = ""
    # 网页登录态签发时间(epoch 秒),取自 KuGoo 里的 ct 字段
    kugoo_cookie_at: int = 0

    @property
    def has_login(self) -> bool:
        """歌单侧登录态:有 token 即可调客户端接口。"""
        return bool(self.userid and self.token)

    @property
    def has_web_login(self) -> bool:
        """播放侧登录态:有 KuGoo 串才能解析播放地址。"""
        return bool(self.kugoo_cookie)

    def seconds_left(self, now: int) -> int:
        """距 token 过期还剩多少秒;未知过期时间时返回 -1(调用方按需立即续期)。"""
        if not self.t_expire_time:
            return -1
        return self.t_expire_time - now

@dataclass(frozen=True)
class KugouQrSession:
    """扫码登录会话:key 用于轮询,image_base64 直接喂 QPixmap。"""

    key: str
    image_base64: str = ""
    url: str = ""

@dataclass(frozen=True)
class KugouQrCheckResult:
    """扫码状态轮询结果。

    status 语义(实测):0 已过期、1 等待扫码、2 已扫码待确认、4 授权成功。
    """

    status: int
    credentials: KugouCredentials | None = None
    message: str = ""

    @property
    def expired(self) -> bool:
        return self.status == 0

    @property
    def waiting_scan(self) -> bool:
        return self.status == 1

    @property
    def waiting_confirm(self) -> bool:
        return self.status == 2

    @property
    def succeeded(self) -> bool:
        return self.status == 4 and self.credentials is not None
