"""网易云音源接入。

职责:
- 扫码登录与登录态维护(crypto + client)
- 读取「我喜欢的音乐」、自建歌单与收藏的他人歌单
- 歌曲信息与播放地址解析

请求参数/加密细节逐字对照 reference/NeriPlayer-Android 的 Kotlin 实现,
详见 client.py / crypto.py 模块 docstring 中的对照说明。
"""

from .client import (
    DEFAULT_QUALITY,
    QUALITY_FALLBACK_ORDER,
    NeteaseClient,
    build_quality_candidates,
    build_qr_account_params,
    merge_netease_request_cookies,
    merge_qr_credential_cookies,
    parse_playback_response,
    should_preheat_netease_weapi_session,
    split_user_playlists,
)
from .models import (
    NeteaseAccount,
    NeteaseApiError,
    NeteaseAuthRequiredError,
    NeteaseNoPermissionError,
    NeteaseNoPlayUrlError,
    NeteasePlaylist,
    NeteaseSong,
    NeteaseUserPlaylists,
    NeteaseYdSnapshot,
    PlayableUrl,
    QrLoginCheckResult,
    QrLoginSession,
)
from .yd import YdTokenFetcher

__all__ = [
    "DEFAULT_QUALITY",
    "QUALITY_FALLBACK_ORDER",
    "NeteaseAccount",
    "NeteaseApiError",
    "NeteaseAuthRequiredError",
    "NeteaseClient",
    "NeteaseNoPermissionError",
    "NeteaseNoPlayUrlError",
    "NeteasePlaylist",
    "NeteaseSong",
    "NeteaseUserPlaylists",
    "NeteaseYdSnapshot",
    "PlayableUrl",
    "QrLoginCheckResult",
    "QrLoginSession",
    "YdTokenFetcher",
    "build_quality_candidates",
    "build_qr_account_params",
    "merge_netease_request_cookies",
    "merge_qr_credential_cookies",
    "parse_playback_response",
    "should_preheat_netease_weapi_session",
    "split_user_playlists",
]
