"""酷狗音源接入。

职责:
- 扫码登录与 token 无感续期(crypto + client)
- 搜索、播放地址解析(网页接口,H5 签名)
- 用户歌单与歌单曲目(客户端网关接口,Android 签名)

接口契约以 2026-10-23/24 本机实测锚定,详见 docs/research/M6-kugou-api.md。
reference/NeriPlayer-Android 里酷狗只作为歌词源存在,没有播放实现可对照,
因此本包没有 Kotlin 对照物(TODO.md M6 立项说明)。

登录设计:只做扫码登录。网页登录 cookie 24 小时过期且无法续期;扫码 token
30 天有效且可用 refresh_token() 无感续期,用户一次扫码后长期不用重登。
"""

from .client import (
    ANDROID_UA,
    KUGOU_REFERER,
    REFRESH_AHEAD_SECONDS,
    SEARCH_PAGE_SIZE,
    WEB_UA,
    KugouClient,
    parse_play_response,
    parse_playlist_list_response,
    parse_playlist_tracks_response,
    parse_search_response,
    parse_song_info_response,
)
from .models import (
    AUTH_ERROR_CODES,
    KUGOU_LOGIN_KEYS,
    KugouAccount,
    KugouApiError,
    KugouAuthRequiredError,
    KugouCredentials,
    KugouNoPlayUrlError,
    KugouPlayableUrl,
    KugouPlaylist,
    KugouQrCheckResult,
    KugouQrSession,
    KugouRateLimitedError,
    KugouSong,
)
from .selector import (
    DEFAULT_QUALITY,
    QUALITY_LABELS,
    hash_field_bitrate,
    quality_chain,
    quality_label,
    select_hash,
)

# 命名别名:主窗口按「源名 + 用途」的习惯引用(对齐
# bili_quality_key_from_netease_level 的写法)
kugou_quality_label = quality_label

__all__ = [
    "ANDROID_UA",
    "AUTH_ERROR_CODES",
    "DEFAULT_QUALITY",
    "KUGOU_LOGIN_KEYS",
    "KUGOU_REFERER",
    "QUALITY_LABELS",
    "REFRESH_AHEAD_SECONDS",
    "SEARCH_PAGE_SIZE",
    "WEB_UA",
    "KugouAccount",
    "KugouApiError",
    "KugouAuthRequiredError",
    "KugouClient",
    "KugouCredentials",
    "KugouNoPlayUrlError",
    "KugouPlayableUrl",
    "KugouPlaylist",
    "KugouQrCheckResult",
    "KugouQrSession",
    "KugouRateLimitedError",
    "KugouSong",
    "hash_field_bitrate",
    "kugou_quality_label",
    "parse_play_response",
    "parse_playlist_list_response",
    "parse_playlist_tracks_response",
    "parse_search_response",
    "parse_song_info_response",
    "quality_chain",
    "quality_label",
    "select_hash",
]
