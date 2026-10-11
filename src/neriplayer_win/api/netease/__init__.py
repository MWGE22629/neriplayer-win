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
    first_recommended_song_array,
    merge_netease_request_cookies,
    merge_qr_credential_cookies,
    parse_playback_response,
    parse_recommended_songs_response,
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

# YdTokenFetcher(来自 .yd)拖挂 QtWebEngineCore(195MB DLL),只在网页
# 登录时才需要;模块级 import 会让每次启动都加载它(见 docs/PERF.md
# v0.8.1)。改为 PEP 562 惰性导出:`from ... import YdTokenFetcher`
# 依旧可用,只是首次取属性时才真正加载 yd。
def __getattr__(name: str):
    if name == "YdTokenFetcher":
        from .yd import YdTokenFetcher

        return YdTokenFetcher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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
    "first_recommended_song_array",
    "merge_netease_request_cookies",
    "merge_qr_credential_cookies",
    "parse_playback_response",
    "parse_recommended_songs_response",
    "should_preheat_netease_weapi_session",
    "split_user_playlists",
]
