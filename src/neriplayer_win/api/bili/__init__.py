"""B站音源接入。

职责:
- 登录态检查与 cookie 会话(网页登录收割,QR 客户端留 API 备用)
- 读取收藏夹(默认收藏夹 + 自建收藏夹)
- 读取「稍后再看」列表(toview/web;参考实现无此接口,契约以实测为准)
- 收藏夹/稍后再看展开为视频列表(标题/UP主/时长)
- 视频音频流解析(DASH 音轨,html5/mp4 回退)

请求参数/WBI 签名细节逐字对照 reference/NeriPlayer-Android 的
core/api/bili/(BiliClient.kt / BiliQrLoginClient.kt)与
data/platform/bili/BiliAudioSelector.kt,详见各模块 docstring。
"""

from .client import (
    DEFAULT_AUDIO_QUALITY,
    BiliClient,
    PlayOptions,
    build_bili_stream_headers,
    ensure_https,
)
from .models import (
    BiliAccount,
    BiliApiError,
    BiliAudioStreamInfo,
    BiliAuthRequiredError,
    BiliFavFolder,
    BiliFavItem,
    BiliNoAudioStreamError,
    BiliPlayInfo,
    BiliQrLoginCheckResult,
    BiliQrLoginSession,
    BiliSong,
)
from .qr_login import BiliQrLoginClient
from .selector import (
    bili_quality_key_from_netease_level,
    prioritize_bili_stream_urls,
    select_stream_by_preference,
)

__all__ = [
    "DEFAULT_AUDIO_QUALITY",
    "BiliAccount",
    "BiliApiError",
    "BiliAudioStreamInfo",
    "BiliAuthRequiredError",
    "BiliClient",
    "BiliFavFolder",
    "BiliFavItem",
    "BiliNoAudioStreamError",
    "BiliPlayInfo",
    "BiliQrLoginCheckResult",
    "BiliQrLoginClient",
    "BiliQrLoginSession",
    "BiliSong",
    "PlayOptions",
    "build_bili_stream_headers",
    "bili_quality_key_from_netease_level",
    "ensure_https",
    "prioritize_bili_stream_urls",
    "select_stream_by_preference",
]
