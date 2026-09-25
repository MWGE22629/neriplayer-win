"""B站 Web 端 API 客户端(同步 httpx 实现)。

逐字对照 reference/NeriPlayer-Android 翻译:
- app/src/main/java/moe/ouom/neriplayer/core/api/bili/BiliClient.kt
  (WBI 签名、playurl DASH 音轨解析与 html5 回退、nav 登录态检查、
  收藏夹 list-all/分页/内容明细、pagelist/view 基础信息、匿名指纹)
- app/src/main/java/moe/ouom/neriplayer/data/platform/bili/BiliAudioSelector.kt
  (音轨选择,见 selector.py)
- app/src/main/java/moe/ouom/neriplayer/data/auth/bili/BiliCookieRepository.kt
  (登录 cookie 键:SESSDATA / DedeUserID / bili_jct)

与 Kotlin 版的取舍(偏差点):
- 协程换同步直调(UI 侧经 run_async 后台线程);收藏夹分页并行块
  (MAX_PARALLEL_PAGE_REQUESTS=6)简化为顺序翻页,避免桌面端限流。
- cookie 不走 okhttp CookieJar,与 NeteaseClient 同风格:显式 Cookie 头,
  持久 cookie 由上层 LocalStore 注入。
- validateLoginSession 的 Boolean?(null=网络错误)语义改为:网络错误抛
  BiliApiError、未登录返回 None(对齐本项目 NeteaseClient.get_login_status)。
- VideoBasicInfo 只保留播放路径所需字段(标题/UP主/时长/pages),略去
  stats / ugc_season / desc_v2 等展示用字段。
- WBI 值编码用 quote(safe="._-*") 等价 Java URLEncoder(+ 替换 %20 的
  行为一致);唯一差异是 Java 会把 '~' 编码为 %7E 而 Python 不编码,
  M2 请求参数(bvid/cid/mid/pn)均为字母数字,不受影响。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urlencode
from urllib.request import getproxies

import httpx

from ...log import get_logger
from .models import (
    BiliAccount,
    BiliApiError,
    BiliAudioStreamInfo,
    BiliDolbyAudio,
    BiliDurl,
    BiliDashStream,
    BiliFavFolder,
    BiliFavFolderPage,
    BiliFavItem,
    BiliFlacAudio,
    BiliNoAudioStreamError,
    BiliPlayInfo,
    BiliVideoPage,
)
from .selector import prioritize_bili_stream_urls, select_stream_by_preference

# ---- 官方接口 / WBI(对应 BiliClient companion object) --------------------
BASE_PLAY_URL = "https://api.bilibili.com/x/player/wbi/playurl"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
FINGERPRINT_URL = "https://api.bilibili.com/x/frontend/finger/spi"
WEB_TICKET_URL = "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
VIEW_URL = "https://api.bilibili.com/x/web-interface/wbi/view"
SEARCH_TYPE_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
FAV_FOLDER_CREATED_LIST_ALL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
FAV_FOLDER_CREATED_LIST = "https://api.bilibili.com/x/v3/fav/folder/created/list"
FAV_RESOURCE_LIST = "https://api.bilibili.com/x/v3/fav/resource/list"
PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist"
# 稍后再看(Web 端接口;参考实现无此端点,契约以 2026-09-15 本机登录态
# 实测为准:GET + 登录 cookie,无需 WBI 签名,data.list 全量不分页)
WATCH_LATER_URL = "https://api.bilibili.com/x/v2/history/toview/web"

# 默认 UA (Web),对应 DEFAULT_WEB_UA
DEFAULT_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# 指纹接口专用 UA (移动端),对应 FINGERPRINT_UA
FINGERPRINT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 "
    "Mobile/15E148 Safari/604.1 Edg/114.0.0.0"
)
# WebTicket 接口 UA,对应 WEB_TICKET_UA
WEB_TICKET_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0"
# 默认 Referer,对应 REFERER
BILI_REFERER = "https://www.bilibili.com"

# Wbi mixin 索引表,对应 MIXIN_INDEX
MIXIN_INDEX = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 62, 6, 63, 57, 20, 34, 52, 59, 11, 36, 44,
)

WBI_CACHE_MS = 10 * 60 * 1000  # Wbi key 缓存时间
ANON_COOKIE_CACHE_MS = 60 * 60 * 1000  # 匿名指纹缓存时间
EMPTY_AUDIO_RETRY_COUNT = 3  # 空音轨结果的重试次数
EMPTY_AUDIO_RETRY_DELAY_MS = 250.0  # 空音轨结果的重试基础等待
FAV_FOLDER_PAGE_SIZE = 20  # 收藏夹接口最大稳定分页尺寸
FAV_CONTENT_PAGE_SIZE = 20  # 收藏夹内容接口文档定义的最大分页尺寸
WEB_TICKET_KEY = "XgwSnGZ1p"  # WebTicket HMAC key

# fnval 位,对应 FNVAL_DASH / FNVAL_DOLBY
FNVAL_DASH = 1 << 4  # DASH 开关(必开,否则只有 durl/mp4)
FNVAL_DOLBY = 1 << 8  # 杜比音频(E-AC-3/Atmos)

# playurl 常见错误码的中文提示(-29001 之外均来自公开错误码表)
_PLAYURL_CODE_HINTS = {
    -404: "内容不存在(可能已删除)",
    -403: "无权限访问该内容",
    62002: "视频不可见(可能仅 UP 主可见或已失效)",
    62004: "视频审核中,暂不可播放",
    62012: "仅 UP 主自己可见",
}

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# 播放音频流所用的请求头(upos mirror 的 m4s 无 Referer/浏览器 UA 会 403)
BILI_STREAM_HEADERS = {
    "Referer": BILI_REFERER,
    "User-Agent": DEFAULT_WEB_UA,
}

DEFAULT_AUDIO_QUALITY = "high"

_log = get_logger("bili")


def build_bili_stream_headers() -> dict[str, str]:
    """给 mpv 逐文件请求头用的 B站音频流头(Referer + 浏览器 UA)。"""
    return dict(BILI_STREAM_HEADERS)


@dataclass(frozen=True)
class PlayOptions:
    """对应 BiliClient.PlayOptions。"""

    qn: int | None = None
    fnval: int = FNVAL_DASH | FNVAL_DOLBY
    fnver: int = 0
    fourk: int = 0
    platform: str = "pc"
    high_quality: int | None = None
    try_look: int | None = None
    session: str | None = None
    gaia_source: str | None = None
    is_gaia_avoided: bool | None = None


# ---------------------------------------------------------------------------
# 模块级纯函数(与 Kotlin internal/private fun 一一对应,便于单测)
# ---------------------------------------------------------------------------

def ensure_https(url: str | None) -> str:
    """对应 ensureHttps:"//" 开头补 https:。"""
    if not url:
        return ""
    url = str(url)
    return f"https:{url}" if url.startswith("//") else url


def md5_hex(text: str) -> str:
    """对应 md5。"""
    digest = hashlib.md5(text.encode("utf-8")).digest()
    return "".join(f"{b:02x}" for b in digest)


def web_ticket_hmac_sha256_hex(message: str) -> str:
    """对应 webTicketHmacSha256Hex(HmacSHA256, key=XgwSnGZ1p)。"""
    digest = hmac.new(
        WEB_TICKET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).digest()
    return "".join(f"{b:02x}" for b in digest)


def wbi_url_encode(value: str) -> str:
    """对应 urlEncode:Java URLEncoder(空格 %20,不编码 . _ - *)。"""
    return quote(str(value), safe="._-*")


def wbi_filter_value(value: str) -> str:
    """对应 filterValue:剔除 !'()*。"""
    return re.sub(r"[!'()*]", "", str(value))


def build_mixin_key_from_urls(img_url: str, sub_url: str) -> str:
    """对应 ensureValidMixin:取文件名(去扩展名)拼接后按 MIXIN_INDEX 重排。"""
    if not img_url.strip() or not sub_url.strip():
        raise BiliApiError(f"无效的 Wbi mixin url: img={img_url} sub={sub_url}")
    img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
    raw = img_key + sub_key
    mixed = "".join(raw[idx] for idx in MIXIN_INDEX if idx < len(raw))
    return mixed[:32] if len(mixed) >= 32 else mixed


def build_signed_wbi_query(
    params_in: Mapping[str, Any], mixin_key: str, wts: int
) -> tuple[list[tuple[str, str]], str]:
    """对应 signWbiUrl 的签名核心;返回 (排序后的参数对含 wts, w_rid)。

    与 Kotlin 一致:值先 filterValue,再补 wts,按 key 排序拼接,
    md5(query + mixinKey) 得 w_rid。
    """
    params: dict[str, str] = {
        str(k): wbi_filter_value(str(v)) for k, v in params_in.items()
    }
    params["wts"] = str(wts)
    sorted_pairs = sorted(params.items())
    query = "&".join(f"{wbi_url_encode(k)}={wbi_url_encode(v)}" for k, v in sorted_pairs)
    w_rid = md5_hex(query + mixin_key)
    return sorted_pairs, w_rid


def put_common_play_params(params: dict[str, str], opts: PlayOptions) -> None:
    """对应 MutableMap.putCommonParams。"""
    if opts.qn is not None:
        params["qn"] = str(opts.qn)
    params["fnval"] = str(opts.fnval)
    params["fnver"] = str(opts.fnver)
    params["fourk"] = str(opts.fourk)
    params["otype"] = "json"
    params["platform"] = opts.platform
    if opts.high_quality is not None:
        params["high_quality"] = str(opts.high_quality)
    if opts.try_look is not None:
        params["try_look"] = str(opts.try_look)
    if opts.session is not None:
        params["session"] = opts.session
    if opts.gaia_source is not None:
        params["gaia_source"] = opts.gaia_source
    if opts.is_gaia_avoided is not None:
        params["isGaiaAvoided"] = str(opts.is_gaia_avoided).lower()


def build_html5_fallback_options(opts: PlayOptions) -> PlayOptions:
    """对应 buildHtml5FallbackOptions。"""
    return PlayOptions(
        qn=opts.qn,
        fnval=0,
        fnver=opts.fnver,
        fourk=0,
        platform="html5",
        high_quality=1,
        try_look=opts.try_look,
        session=opts.session,
        gaia_source=opts.gaia_source,
        is_gaia_avoided=opts.is_gaia_avoided,
    )


def _opt_str(obj: Mapping[str, Any], key: str, fallback: str = "") -> str:
    """对应 JSONObject.optString(缺省/非字符串回落 fallback)。"""
    value = obj.get(key)
    if value is None:
        return fallback
    return value if isinstance(value, str) else str(value)


def _opt_int(obj: Mapping[str, Any], key: str, fallback: int = 0) -> int:
    value = obj.get(key)
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _opt_long_or_none(obj: Mapping[str, Any], key: str) -> int | None:
    """对应 optLongOrNull / optLongIfPresent。"""
    if key not in obj or obj[key] is None:
        return None
    try:
        return int(obj[key])
    except (TypeError, ValueError):
        return None


def _opt_bool(obj: Mapping[str, Any], key: str, fallback: bool = False) -> bool:
    value = obj.get(key)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    return value in (1, "1", "true", "True")


def _string_list(arr: Any) -> list[str]:
    """对应 JSONArray?.toStringList。"""
    if not isinstance(arr, list):
        return []
    return [str(item) if item is not None else "" for item in arr]


def _int_list(arr: Any) -> list[int]:
    if not isinstance(arr, list):
        return []
    out: list[int] = []
    for item in arr:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def bitrate_kbps_from_bandwidth(bandwidth: int) -> int:
    """对应 bitrateKbpsFromBandwidth。"""
    return max(0, bandwidth // 1000)


# ---------------------------------------------------------------------------
# 解析函数(对应 BiliClient 的 parseXxx)
# ---------------------------------------------------------------------------

def parse_durl(arr: Any) -> list[BiliDurl]:
    """对应 parseDurl。"""
    if not isinstance(arr, list):
        return []
    out: list[BiliDurl] = []
    for index, o in enumerate(arr):
        if not isinstance(o, dict):
            continue
        backups = _string_list(o.get("backup_url") or o.get("backupUrl"))
        out.append(
            BiliDurl(
                order=_opt_int(o, "order", index + 1),
                length_ms=_opt_int(o, "length", 0),
                size_bytes=_opt_int(o, "size", 0),
                url=_opt_str(o, "url", ""),
                backup_urls=backups,
            )
        )
    return out


def parse_dash_item(o: Mapping[str, Any]) -> BiliDashStream | None:
    """对应 parseDashItem(baseUrl 为空返回 None)。"""
    base_url = _opt_str(o, "baseUrl") or _opt_str(o, "base_url")
    backups = _string_list(o.get("backupUrl") or o.get("backup_url"))
    if not base_url:
        return None
    return BiliDashStream(
        id=_opt_int(o, "id", -1),
        base_url=base_url,
        backup_urls=backups,
        bandwidth=_opt_int(o, "bandwidth", 0),
        mime_type=_opt_str(o, "mimeType") or _opt_str(o, "mime_type"),
        codecs=_opt_str(o, "codecs"),
        width=_opt_int(o, "width", 0),
        height=_opt_int(o, "height", 0),
        frame_rate=_opt_str(o, "frameRate") or _opt_str(o, "frame_rate"),
        codecid=_opt_int(o, "codecid", 0),
    )


def parse_dash_array(arr: Any) -> list[BiliDashStream]:
    """对应 parseDashArray。"""
    if not isinstance(arr, list):
        return []
    out: list[BiliDashStream] = []
    for o in arr:
        if isinstance(o, dict):
            stream = parse_dash_item(o)
            if stream is not None:
                out.append(stream)
    return out


def parse_play_info(root: Mapping[str, Any]) -> BiliPlayInfo:
    """对应 requestPlayUrl 的响应解析部分。"""
    data = root.get("data") if isinstance(root.get("data"), dict) else {}

    qn_selected = data["quality"] if "quality" in data else None
    format_ = data["format"] if "format" in data else None
    time_length_ms = data["timelength"] if "timelength" in data else None

    dash = data.get("dash") if isinstance(data.get("dash"), dict) else {}
    dolby_obj = dash.get("dolby") if isinstance(dash.get("dolby"), dict) else None
    dolby = None
    if dolby_obj is not None:
        dolby = BiliDolbyAudio(
            type=_opt_int(dolby_obj, "type", 0),
            audios=parse_dash_array(dolby_obj.get("audio")),
        )
    flac_obj = dash.get("flac") if isinstance(dash.get("flac"), dict) else None
    flac = None
    if flac_obj is not None:
        flac_audio = (
            parse_dash_item(flac_obj["audio"])
            if isinstance(flac_obj.get("audio"), dict)
            else None
        )
        flac = BiliFlacAudio(display=_opt_bool(flac_obj, "display", False), audio=flac_audio)

    return BiliPlayInfo(
        code=_opt_int(root, "code", -1),
        message=_opt_str(root, "message"),
        qn_selected=qn_selected,
        format=format_,
        time_length_ms=time_length_ms,
        accept_description=_string_list(data.get("accept_description")),
        accept_quality=_int_list(data.get("accept_quality")),
        durl=parse_durl(data.get("durl")),
        dash_video=parse_dash_array(dash.get("video")),
        dash_audio=parse_dash_array(dash.get("audio")),
        dolby=dolby,
        flac=flac,
    )


def should_retry_empty_audio_fetch(info: BiliPlayInfo) -> bool:
    """对应 PlayInfo.shouldRetryEmptyAudioFetch。"""
    has_dash_video = bool(info.dash_video)
    has_mp4_fallback = bool(info.durl)
    dolby_audios = info.dolby.audios if info.dolby else []
    has_flac = info.flac is not None and info.flac.audio is not None
    return (
        not info.dash_audio
        and not dolby_audios
        and not has_flac
        and (has_dash_video or has_mp4_fallback)
    )


def to_audio_stream_infos(info: BiliPlayInfo) -> list[BiliAudioStreamInfo]:
    """对应 PlayInfo.toAudioStreamInfos:合并普通/杜比/Hi-Res 音轨。"""
    streams: list[BiliAudioStreamInfo] = []

    for a in info.dash_audio:
        candidates = prioritize_bili_stream_urls(a.base_url, a.backup_urls)
        preferred = candidates[0] if candidates else a.base_url
        streams.append(
            BiliAudioStreamInfo(
                id=a.id,
                mime_type=a.mime_type or "audio/mp4",
                bitrate_kbps=bitrate_kbps_from_bandwidth(a.bandwidth),
                quality_tag=None,
                url=preferred,
                candidate_urls=candidates or [a.base_url],
            )
        )

    for a in (info.dolby.audios if info.dolby else []):
        candidates = prioritize_bili_stream_urls(a.base_url, a.backup_urls)
        preferred = candidates[0] if candidates else a.base_url
        streams.append(
            BiliAudioStreamInfo(
                id=a.id,
                mime_type=a.mime_type or "audio/eac3",
                bitrate_kbps=bitrate_kbps_from_bandwidth(a.bandwidth),
                quality_tag="dolby",
                url=preferred,
                candidate_urls=candidates or [a.base_url],
            )
        )

    if info.flac is not None and info.flac.audio is not None:
        a = info.flac.audio
        candidates = prioritize_bili_stream_urls(a.base_url, a.backup_urls)
        preferred = candidates[0] if candidates else a.base_url
        streams.append(
            BiliAudioStreamInfo(
                id=a.id,
                mime_type=a.mime_type or "audio/flac",
                bitrate_kbps=bitrate_kbps_from_bandwidth(a.bandwidth),
                quality_tag="hires",
                url=preferred,
                candidate_urls=candidates or [a.base_url],
            )
        )

    return streams


def _estimate_progressive_bitrate_kbps(item: BiliDurl) -> int:
    """对应 estimateProgressiveBitrateKbps。"""
    if item.length_ms <= 0 or item.size_bytes <= 0:
        return 0
    return max(0, (item.size_bytes * 8) // item.length_ms)


def to_progressive_fallback_stream_infos(info: BiliPlayInfo) -> list[BiliAudioStreamInfo]:
    """对应 toProgressiveFallbackStreamInfos:仅单段 durl 的 MP4 可用。"""
    if len(info.durl) != 1:
        return []
    item = info.durl[0]
    candidates = prioritize_bili_stream_urls(item.url, item.backup_urls)
    preferred = candidates[0] if candidates else item.url
    if not preferred:
        return []
    return [
        BiliAudioStreamInfo(
            id=None,
            mime_type="video/mp4",
            bitrate_kbps=_estimate_progressive_bitrate_kbps(item),
            quality_tag=None,
            url=preferred,
            candidate_urls=candidates or [preferred],
        )
    ]


def parse_fav_folder(o: Mapping[str, Any]) -> BiliFavFolder:
    """对应 parseFavFolder。"""
    cnt = o.get("cnt_info") if isinstance(o.get("cnt_info"), dict) else {}
    upper = o.get("upper") if isinstance(o.get("upper"), dict) else {}
    collection_id = _opt_long_or_none(o, "season_id") or _opt_long_or_none(o, "series_id") or 0
    media_id = _opt_long_or_none(o, "id") or collection_id
    fallback_type = 21 if collection_id != 0 else 11
    mid = _opt_long_or_none(o, "mid") or 0
    if mid == 0 and isinstance(upper, dict):
        mid = _opt_long_or_none(upper, "mid") or 0
    title = _opt_str(o, "title") or _opt_str(o, "name")
    intro = _opt_str(o, "intro") or _opt_str(o, "description")
    count = _opt_int(o, "media_count", _opt_int(o, "total", 0))
    upper_name = _opt_str(upper, "name") if isinstance(upper, dict) else ""
    return BiliFavFolder(
        media_id=int(media_id),
        fid=int(_opt_long_or_none(o, "fid") or collection_id),
        mid=int(mid),
        title=title,
        cover_url=ensure_https(_opt_str(o, "cover")),
        intro=intro,
        count=count,
        like_count=_opt_long_or_none(cnt, "thumb_up") if isinstance(cnt, dict) else None,
        play_count=_opt_long_or_none(cnt, "play") if isinstance(cnt, dict) else None,
        collect_count=_opt_long_or_none(cnt, "collect") if isinstance(cnt, dict) else None,
        upper_name=upper_name,
        attr=_opt_int(o, "attr", 0),
        state=_opt_int(o, "state", 0),
        item_type=_opt_int(o, "type", fallback_type),
    )


def parse_fav_folder_list_result(data: Mapping[str, Any]) -> tuple[int, list[BiliFavFolder]]:
    """对应 parseFavFolderListResult;返回 (count, folders)。"""
    lst = data.get("list") if isinstance(data.get("list"), list) else []
    folders = [parse_fav_folder(item) for item in lst if isinstance(item, dict)]
    count = _opt_int(data, "count", len(folders))
    return count, folders


def merge_fav_folders(
    primary: list[BiliFavFolder], fallback: list[BiliFavFolder]
) -> list[BiliFavFolder]:
    """对应 mergeFavFolders:过滤无效项后按 mediaId 去重(保留首个)。"""
    seen: set[int] = set()
    out: list[BiliFavFolder] = []
    for folder in primary + fallback:
        if folder.media_id == 0 or not folder.title.strip():
            continue
        if folder.media_id in seen:
            continue
        seen.add(folder.media_id)
        out.append(folder)
    return out


def parse_fav_resource_page(data: Mapping[str, Any]) -> BiliFavFolderPage:
    """对应 getFavFolderContents 的响应解析。"""
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    folder = parse_fav_folder(info)

    medias = data.get("medias") if isinstance(data.get("medias"), list) else []
    items: list[BiliFavItem] = []
    for m in medias:
        if not isinstance(m, dict):
            continue
        upper = m.get("upper") if isinstance(m.get("upper"), dict) else {}
        cnt = m.get("cnt_info") if isinstance(m.get("cnt_info"), dict) else {}
        bvid = _opt_str(m, "bvid").strip() or _opt_str(m, "bv_id").strip() or None
        items.append(
            BiliFavItem(
                type=_opt_int(m, "type", 0),
                id=_opt_long_or_none(m, "id") or 0,
                bvid=bvid,
                title=_opt_str(m, "title"),
                cover_url=ensure_https(_opt_str(m, "cover")),
                intro=_opt_str(m, "intro"),
                duration_sec=_opt_int(m, "duration", 0),
                upper_mid=_opt_int(upper, "mid", 0) if isinstance(upper, dict) else 0,
                upper_name=_opt_str(upper, "name") if isinstance(upper, dict) else "",
                play=_opt_long_or_none(cnt, "play") if isinstance(cnt, dict) else None,
                danmaku=_opt_long_or_none(cnt, "danmaku") if isinstance(cnt, dict) else None,
                fav_time=_opt_long_or_none(m, "fav_time"),
            )
        )
    return BiliFavFolderPage(
        info=folder,
        items=items,
        has_more=_opt_bool(data, "has_more", False),
    )


def parse_watch_later_response(root: Mapping[str, Any]) -> list[BiliFavItem]:
    """稍后再看响应解析:data.list[] → BiliFavItem(保持接口顺序,最新在前)。

    字段对照实测响应:aid/bvid/title/pic/duration/owner.mid/owner.name/
    add_time;条目恒为视频稿件(type=2),复用 BiliFavItem 的 playable 语义。
    """
    data = root.get("data") if isinstance(root.get("data"), dict) else {}
    lst = data.get("list") if isinstance(data.get("list"), list) else []
    items: list[BiliFavItem] = []
    for m in lst:
        if not isinstance(m, dict):
            continue
        owner = m.get("owner") if isinstance(m.get("owner"), dict) else {}
        bvid = _opt_str(m, "bvid").strip() or None
        items.append(
            BiliFavItem(
                type=2,
                id=_opt_long_or_none(m, "aid") or 0,
                bvid=bvid,
                title=_opt_str(m, "title"),
                cover_url=ensure_https(_opt_str(m, "pic")),
                intro="",
                duration_sec=_opt_int(m, "duration", 0),
                upper_mid=_opt_long_or_none(owner, "mid") or 0 if isinstance(owner, dict) else 0,
                upper_name=_opt_str(owner, "name") if isinstance(owner, dict) else "",
                fav_time=_opt_long_or_none(m, "add_time"),
            )
        )
    return items


def _strip_search_highlight(title: str) -> str:
    """搜索标题剥 <em> 高亮标签并还原 HTML 实体(对应 stripHtml)。"""
    return html.unescape(re.sub(r"<[^>]+>", "", title))


def _parse_duration_text(text: str) -> int:
    """"mm:ss" / "h:mm:ss" → 秒;非数字输入返回 0。"""
    parts = text.strip().split(":")
    if 2 <= len(parts) <= 3 and all(part.isdigit() for part in parts):
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(part)
        return seconds
    return 0


def parse_search_video_response(
    root: Mapping[str, Any], page: int
) -> tuple[list[BiliFavItem], bool]:
    """对应 searchVideos 的响应解析:data.result[] → BiliFavItem。

    字段对照参考实现:aid/bvid/title(带 <em> 高亮)/author/pic/
    duration("mm:ss")/type(仅保留 "video");分页判定 page < numPages,
    numPages 缺失时按本页非空粗判。
    """
    data = root.get("data") if isinstance(root.get("data"), dict) else {}
    results = data.get("result") if isinstance(data.get("result"), list) else []
    items: list[BiliFavItem] = []
    for m in results:
        if not isinstance(m, dict) or _opt_str(m, "type") != "video":
            continue
        items.append(
            BiliFavItem(
                type=2,
                id=_opt_long_or_none(m, "aid") or 0,
                bvid=_opt_str(m, "bvid").strip() or None,
                title=_strip_search_highlight(_opt_str(m, "title")),
                cover_url=ensure_https(_opt_str(m, "pic")),
                intro="",
                duration_sec=_parse_duration_text(_opt_str(m, "duration")),
                upper_name=_opt_str(m, "author"),
            )
        )
    num_pages = _opt_int(data, "numPages", 0)
    has_more = page < num_pages if num_pages else bool(items)
    return items, has_more


def parse_page_list_response(root: Mapping[str, Any]) -> list[BiliVideoPage]:
    """对应 parsePageListResponse(data 为数组)。"""
    data = root.get("data") if isinstance(root.get("data"), list) else []
    pages: list[BiliVideoPage] = []
    for p in data:
        if not isinstance(p, dict):
            continue
        dim = p.get("dimension") if isinstance(p.get("dimension"), dict) else {}
        pages.append(
            BiliVideoPage(
                cid=_opt_long_or_none(p, "cid") or 0,
                page=_opt_int(p, "page", 0),
                part=_opt_str(p, "part"),
                duration_sec=_opt_int(p, "duration", 0),
                width=_opt_int(dim, "width", 0) if isinstance(dim, dict) else 0,
                height=_opt_int(dim, "height", 0) if isinstance(dim, dict) else 0,
            )
        )
    return pages


def parse_video_basic_info(data: Mapping[str, Any]) -> dict[str, Any]:
    """fetchVideoBasicInfo 的字段解析(M2 取播放路径所需子集)。"""
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    pages: list[BiliVideoPage] = []
    pages_arr = data.get("pages") if isinstance(data.get("pages"), list) else []
    for p in pages_arr:
        if not isinstance(p, dict):
            continue
        dim = p.get("dimension") if isinstance(p.get("dimension"), dict) else {}
        pages.append(
            BiliVideoPage(
                cid=_opt_long_or_none(p, "cid") or 0,
                page=_opt_int(p, "page", 0),
                part=_opt_str(p, "part"),
                duration_sec=_opt_int(p, "duration", 0),
                width=_opt_int(dim, "width", 0) if isinstance(dim, dict) else 0,
                height=_opt_int(dim, "height", 0) if isinstance(dim, dict) else 0,
            )
        )
    return {
        "aid": _opt_long_or_none(data, "aid") or 0,
        "bvid": _opt_str(data, "bvid"),
        "title": _opt_str(data, "title"),
        "cover_url": ensure_https(_opt_str(data, "pic")),
        "duration_sec": _opt_int(data, "duration", 0),
        "owner_mid": _opt_long_or_none(owner, "mid") or 0 if isinstance(owner, dict) else 0,
        "owner_name": _opt_str(owner, "name") if isinstance(owner, dict) else "",
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class BiliClient:
    """B站 API 客户端;线程安全,UI 侧放后台线程调用。"""

    def __init__(self, timeout: httpx.Timeout | None = None) -> None:
        self._timeout = timeout or httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=30.0)
        self._http = httpx.Client(timeout=self._timeout, follow_redirects=True)
        # 直连兜底客户端:主客户端读系统/环境代理(trust_env),代理进程
        # 已退出的残留配置会让所有请求 WinError 10061;B站接口国内直连
        # 总是可达,连接类失败时用它重试一次(见 _get_with_direct_fallback)
        self._http_direct = httpx.Client(
            timeout=self._timeout, follow_redirects=True, trust_env=False
        )

        self._cookie_lock = threading.Lock()
        self._stored_cookies: dict[str, str] = {}

        # Wbi mixin key 缓存(对应 keyMutex/cachedMixinKey)
        self._key_lock = threading.Lock()
        self._cached_mixin_key: str | None = None
        self._mixin_cached_at_ms = 0

        # 匿名指纹 cookie 缓存
        self._anon_lock = threading.Lock()
        self._cached_anon_cookies: dict[str, str] | None = None
        self._anon_cached_at_ms = 0

    def close(self) -> None:
        self._http.close()
        self._http_direct.close()

    # -- 登录态(cookie)管理 --------------------------------------------------

    def set_cookies(self, cookies: Mapping[str, str]) -> None:
        """网页登录成功后注入持久 cookie;同时清空 Wbi/匿名缓存。"""
        with self._cookie_lock:
            self._stored_cookies = {
                str(k).strip(): str(v).strip()
                for k, v in cookies.items()
                if str(k).strip() and str(v).strip()
            }
        with self._key_lock:
            self._cached_mixin_key = None
            self._mixin_cached_at_ms = 0

    def cookies_snapshot(self) -> dict[str, str]:
        with self._cookie_lock:
            return dict(self._stored_cookies)

    def has_login(self) -> bool:
        with self._cookie_lock:
            return bool(self._stored_cookies.get("SESSDATA", "").strip())

    def logout(self) -> None:
        self.set_cookies({})

    # -- 请求封装(对应 executeGetAsText / getJson / getJsonWbi) ---------------

    def _effective_cookies(self) -> dict[str, str]:
        stored = self.cookies_snapshot()
        if stored:
            return stored
        return self._ensure_anon_cookies()

    def _get_with_direct_fallback(
        self, url: str, headers: dict[str, str]
    ) -> httpx.Response:
        """走代理路径的连接类失败(ConnectError/ConnectTimeout)时直连重试一次。

        背景:httpx 默认读系统/环境代理,代理进程退出后的残留配置会让
        请求表现为 WinError 10061(拒绝连接)——本机代理端口没人听,而非
        B站不可达。直连重试对"真需代理"的用户无副作用(重试失败仍抛
        原始异常,保留用户配置路径的报错语义);无任何代理配置时不重试,
        连接失败即真实网络故障。
        """
        try:
            return self._http.get(url, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            if not getproxies():
                raise
            _log.warning(
                "代理路径连接失败,降级直连重试 url=%s(%s: %s)",
                url, error.__class__.__name__, error,
            )
            try:
                return self._http_direct.get(url, headers=headers)
            except httpx.HTTPError as direct_error:
                _log.warning(
                    "直连重试仍失败 url=%s(%s: %s)",
                    url, direct_error.__class__.__name__, direct_error,
                )
                raise error from direct_error

    def _execute_get_as_text(self, url: str) -> str:
        cookie_header = "; ".join(
            f"{k}={v}" for k, v in self._effective_cookies().items()
        )
        headers = {"User-Agent": DEFAULT_WEB_UA, "Referer": BILI_REFERER}
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            response = self._get_with_direct_fallback(url, headers)
        except httpx.HTTPError as error:
            # 环境代理快照随错误落盘:定位"系统/环境代理残留导致
            # WinError 10061"类用户侧网络问题的关键证据
            _log.warning(
                "B站网络请求失败 url=%s proxies=%r(%s: %s)",
                url, dict(getproxies()), error.__class__.__name__, error,
            )
            raise BiliApiError(
                f"网络请求失败: {url}({error.__class__.__name__}: {error})"
            ) from error
        content = response.content
        if len(content) > _MAX_RESPONSE_BYTES:
            raise BiliApiError("B站接口响应超过 4MB 限制")
        text = content.decode("utf-8", errors="replace")
        if response.status_code < 200 or response.status_code >= 300:
            raise BiliApiError(
                f"HTTP {response.status_code}: {text[:200]}", code=response.status_code
            )
        return text

    def get_json(self, base_url: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """对应 getJson:普通参数直拼。"""
        query = urlencode(
            {str(k): str(v) for k, v in params.items()}, quote_via=quote
        )
        url = f"{base_url}?{query}" if query else base_url
        return self._parse_json(self._execute_get_as_text(url))

    def get_json_wbi(self, base_url: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """对应 getJsonWbi:Wbi 加签后请求。"""
        url = self.sign_wbi_url(base_url, params)
        return self._parse_json(self._execute_get_as_text(url))

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        try:
            root = json.loads(text)
        except (json.JSONDecodeError, TypeError) as error:
            raise BiliApiError(f"B站接口返回非 JSON: {text[:120]}") from error
        if not isinstance(root, dict):
            raise BiliApiError("B站接口返回结构异常")
        return root

    # -- Wbi 签名(对应 signWbiUrl / getOrRefreshMixinKey) --------------------

    def sign_wbi_url(self, base_url: str, params_in: Mapping[str, Any]) -> str:
        mixin_key = self._get_or_refresh_mixin_key()
        sorted_pairs, w_rid = build_signed_wbi_query(
            params_in, mixin_key, wts=int(time.time())
        )
        query = urlencode(sorted_pairs, quote_via=quote)
        return f"{base_url}?{query}&w_rid={w_rid}"

    def _get_or_refresh_mixin_key(self) -> str:
        now_ms = int(time.time() * 1000)
        with self._key_lock:
            if (
                self._cached_mixin_key is not None
                and now_ms - self._mixin_cached_at_ms < WBI_CACHE_MS
            ):
                return self._cached_mixin_key
            self._cached_mixin_key = self._fetch_mixin_key()
            self._mixin_cached_at_ms = now_ms
            return self._cached_mixin_key

    def _fetch_mixin_key(self) -> str:
        """对应 fetchMixinKey:nav 优先,失败回退 ticket。"""
        try:
            return self._fetch_mixin_key_from_nav()
        except BiliApiError:
            return self._fetch_mixin_key_from_ticket()

    def _fetch_mixin_key_from_nav(self) -> str:
        root = self._parse_json(self._execute_get_as_text(NAV_URL))
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        wbi_img = data.get("wbi_img") if isinstance(data.get("wbi_img"), dict) else {}
        return build_mixin_key_from_urls(
            _opt_str(wbi_img, "img_url"), _opt_str(wbi_img, "sub_url")
        )

    def _fetch_mixin_key_from_ticket(self) -> str:
        ts = int(time.time())
        hex_sign = web_ticket_hmac_sha256_hex(f"ts{ts}")
        params: dict[str, str] = {
            "key_id": "ec02",
            "hexsign": hex_sign,
            "context[ts]": str(ts),
        }
        csrf = self._effective_cookies().get("bili_jct", "")
        if csrf.strip():
            params["csrf"] = csrf
        response = None
        try:
            response = self._http.post(
                WEB_TICKET_URL,
                params=params,
                headers={"User-Agent": WEB_TICKET_UA},
            )
        except httpx.HTTPError as error:
            raise BiliApiError(
                f"网络请求失败: {WEB_TICKET_URL}({error.__class__.__name__}: {error})"
            ) from error
        text = response.content.decode("utf-8", errors="replace")
        if response.status_code < 200 or response.status_code >= 300:
            raise BiliApiError(f"HTTP {response.status_code}: {text[:200]}")
        root = self._parse_json(text)
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        nav = data.get("nav") if isinstance(data.get("nav"), dict) else {}
        return build_mixin_key_from_urls(_opt_str(nav, "img"), _opt_str(nav, "sub"))

    # -- 匿名指纹(对应 ensureAnonCookies / fetchAnonCookies) ------------------

    def _ensure_anon_cookies(self) -> dict[str, str]:
        now_ms = int(time.time() * 1000)
        with self._anon_lock:
            if (
                self._cached_anon_cookies is not None
                and now_ms - self._anon_cached_at_ms < ANON_COOKIE_CACHE_MS
            ):
                return dict(self._cached_anon_cookies)
            cookies = self._fetch_anon_cookies()
            self._cached_anon_cookies = cookies
            self._anon_cached_at_ms = now_ms
            return dict(cookies)

    def _fetch_anon_cookies(self) -> dict[str, str]:
        try:
            response = self._http.get(FINGERPRINT_URL, headers={"User-Agent": FINGERPRINT_UA})
        except httpx.HTTPError as error:
            raise BiliApiError(
                f"网络请求失败: {FINGERPRINT_URL}({error.__class__.__name__}: {error})"
            ) from error
        if response.status_code < 200 or response.status_code >= 300:
            raise BiliApiError(f"HTTP {response.status_code}")
        root = self._parse_json(response.content.decode("utf-8", errors="replace"))
        data = root.get("data") if isinstance(root.get("data"), dict) else {}

        cookies: dict[str, str] = {}
        pairs = (
            ("buvid3", _opt_str(data, "b_3") or _opt_str(data, "buvid3")),
            ("buvid4", _opt_str(data, "b_4") or _opt_str(data, "buvid4")),
            ("buvid_fp", _opt_str(data, "buvid_fp")),
            ("buvid_fp_plain", _opt_str(data, "buvid_fp_plain")),
            ("b_lsid", _opt_str(data, "b_lsid")),
        )
        for name, value in pairs:
            if value.strip():
                cookies[name] = value
        return cookies

    # -- 登录状态检查(对应 validateLoginSession,nav 接口) ---------------------

    def get_login_status(self) -> BiliAccount | None:
        """SESSDATA 是否有效;有效返回账号(mid/uname),未登录返回 None。

        网络失败抛 BiliApiError,由调用方决定提示方式。
        """
        stored = self.cookies_snapshot()
        if not stored.get("SESSDATA", "").strip():
            return None
        headers = {"User-Agent": DEFAULT_WEB_UA, "Referer": BILI_REFERER}
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in stored.items())
        try:
            response = self._http.get(NAV_URL, headers=headers)
        except httpx.HTTPError as error:
            raise BiliApiError(
                f"网络请求失败: {NAV_URL}({error.__class__.__name__}: {error})"
            ) from error
        if response.status_code < 200 or response.status_code >= 300:
            raise BiliApiError(f"HTTP {response.status_code}")
        root = self._parse_json(response.content.decode("utf-8", errors="replace"))
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        if (
            _opt_int(root, "code", -1) == 0
            and _opt_bool(data, "isLogin", False)
            and _opt_long_or_none(data, "mid") not in (None, 0)
        ):
            return BiliAccount(
                mid=int(data["mid"]), uname=_opt_str(data, "uname")
            )
        return None

    # -- 收藏夹(对应 fav folder 系列接口) -------------------------------------

    def get_user_created_fav_folders(self, up_mid: int) -> list[BiliFavFolder]:
        """获取指定用户创建的所有收藏夹(list-all 优先,分页补齐合并)。"""
        count, folders = self._fetch_created_fav_folders_list_all(up_mid)
        if count <= len(folders):
            return folders
        try:
            paged = self._fetch_created_fav_folders_by_page(up_mid, count)
        except BiliApiError:
            paged = []
        return merge_fav_folders(folders, paged)

    def _fetch_created_fav_folders_list_all(self, up_mid: int) -> tuple[int, list[BiliFavFolder]]:
        root = self.get_json(
            FAV_FOLDER_CREATED_LIST_ALL,
            {"up_mid": str(up_mid), "web_location": "333.1387"},
        )
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        return parse_fav_folder_list_result(data)

    def _fetch_created_fav_folders_by_page(
        self, up_mid: int, expected_count: int
    ) -> list[BiliFavFolder]:
        total_pages = max(1, math.ceil(expected_count / FAV_FOLDER_PAGE_SIZE))
        out: list[BiliFavFolder] = []
        for page in range(1, total_pages + 1):
            root = self.get_json(
                FAV_FOLDER_CREATED_LIST,
                {
                    "up_mid": str(up_mid),
                    "pn": str(page),
                    "ps": str(FAV_FOLDER_PAGE_SIZE),
                    "web_location": "333.1387",
                },
            )
            data = root.get("data") if isinstance(root.get("data"), dict) else {}
            _, folders = parse_fav_folder_list_result(data)
            out.extend(folders)
        return out

    def get_fav_folder_contents(
        self,
        media_id: int,
        page: int = 1,
        page_size: int = 20,
        order: str = "mtime",
        keyword: str | None = None,
        tid: int | None = None,
        scope_type: int | None = None,
    ) -> BiliFavFolderPage:
        """收藏夹内容明细(分页)。"""
        params: dict[str, str] = {
            "media_id": str(media_id),
            "pn": str(page),
            "ps": str(page_size),
            "order": order,
            "platform": "web",
        }
        if keyword:
            params["keyword"] = keyword
        if tid is not None:
            params["tid"] = str(tid)
        if scope_type is not None:
            params["type"] = str(scope_type)
        root = self.get_json(FAV_RESOURCE_LIST, params)
        code = _opt_int(root, "code", -1)
        if code != 0:
            message = _opt_str(root, "message") or _opt_str(root, "msg")
            if code == -101 or "登录" in message:
                raise BiliAuthRequiredError(f"获取收藏夹内容失败: {message}(code={code})", code=code)
            raise BiliApiError(f"获取收藏夹内容失败: {message}(code={code})", code=code)
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        return parse_fav_resource_page(data)

    def get_all_fav_folder_items(self, media_id: int) -> list[BiliFavItem]:
        """收藏夹全部内容(顺序翻页 + 按 type:id:bvid 去重)。

        对应 getAllFavFolderItems + collectAllFavFolderItems;并行分块
        简化为顺序。
        """
        first_page = self.get_fav_folder_contents(
            media_id, page=1, page_size=FAV_CONTENT_PAGE_SIZE
        )
        total_count = first_page.info.count
        if not first_page.has_more or total_count <= len(first_page.items):
            return first_page.items

        total_pages = math.ceil(total_count / FAV_CONTENT_PAGE_SIZE)
        seen = {
            f"{item.type}:{item.id}:{item.bvid or ''}" for item in first_page.items
        }
        out: list[BiliFavItem] = list(first_page.items)
        for page in range(2, total_pages + 1):
            page_result = self.get_fav_folder_contents(
                media_id, page=page, page_size=FAV_CONTENT_PAGE_SIZE
            )
            for item in page_result.items:
                key = f"{item.type}:{item.id}:{item.bvid or ''}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
        return out

    # -- 稍后再看(参考实现无此接口,契约见 WATCH_LATER_URL 注释) ----------

    def get_watch_later_items(self) -> list[BiliFavItem]:
        """稍后再看全部条目(接口全量返回,不分页;需登录态)。"""
        root = self.get_json(WATCH_LATER_URL, {})
        code = _opt_int(root, "code", -1)
        if code != 0:
            message = _opt_str(root, "message") or _opt_str(root, "msg")
            if code == -101 or "登录" in message:
                raise BiliAuthRequiredError(
                    f"获取稍后再看失败: {message}(code={code})", code=code
                )
            raise BiliApiError(f"获取稍后再看失败: {message}(code={code})", code=code)
        return parse_watch_later_response(root)

    # -- 搜索(对应 wbi/search/type) ------------------------------------------

    def search_videos_raw(self, keyword: str, page: int = 1) -> str:
        """对应 searchVideos(GET,Wbi 加签;order/duration/tids 取参考实现默认)。"""
        url = self.sign_wbi_url(
            SEARCH_TYPE_URL,
            {
                "search_type": "video",
                "keyword": keyword,
                "order": "totalrank",
                "duration": "0",
                "tids": "0",
                "page": str(page),
            },
        )
        return self._execute_get_as_text(url)

    def search_videos(
        self, keyword: str, page: int = 1
    ) -> tuple[list[BiliFavItem], bool]:
        """关键词搜视频(音频视角);返回 (条目列表, 是否还有下一页)。"""
        root = self._parse_json(self.search_videos_raw(keyword, page))
        code = _opt_int(root, "code", -1)
        if code != 0:
            message = _opt_str(root, "message") or _opt_str(root, "msg")
            raise BiliApiError(f"搜索失败: {message}(code={code})", code=code)
        return parse_search_video_response(root, page)

    # -- 视频基础信息(对应 wbi/view 与 pagelist) ------------------------------

    def get_video_basic_info_by_bvid(self, bvid: str) -> dict[str, Any]:
        root = self.get_json_wbi(VIEW_URL, {"bvid": bvid})
        code = _opt_int(root, "code", -1)
        if code != 0:
            raise self._playurl_style_error("获取视频信息", code, _opt_str(root, "message"))
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        return parse_video_basic_info(data)

    def get_video_page_list(self, bvid: str) -> list[BiliVideoPage]:
        """通过 bvid 获取分 P 列表(对应 getVideoPageList)。"""
        root = self.get_json(PAGELIST_URL, {"bvid": bvid})
        code = _opt_int(root, "code", -1)
        if code != 0:
            raise self._playurl_style_error("获取视频分P", code, _opt_str(root, "message"))
        return parse_page_list_response(root)

    # -- 播放地址(对应 playurl 系列) -------------------------------------------

    def get_play_info_by_bvid(
        self, bvid: str, cid: int, opts: PlayOptions | None = None
    ) -> BiliPlayInfo:
        opts = opts or PlayOptions()
        params: dict[str, str] = {"bvid": bvid, "cid": str(cid)}
        put_common_play_params(params, opts)
        return self._request_play_url(params)

    def get_play_info_by_avid(
        self, avid: int, cid: int, opts: PlayOptions | None = None
    ) -> BiliPlayInfo:
        opts = opts or PlayOptions()
        params: dict[str, str] = {"avid": str(avid), "cid": str(cid)}
        put_common_play_params(params, opts)
        return self._request_play_url(params)

    def _request_play_url(self, params: dict[str, str]) -> BiliPlayInfo:
        """对应 requestPlayUrl:Wbi 加签 → GET → 解析(code!=0 抛异常)。"""
        url = self.sign_wbi_url(BASE_PLAY_URL, params)
        root = self._parse_json(self._execute_get_as_text(url))
        code = _opt_int(root, "code", -1)
        message = _opt_str(root, "message")
        if code != 0:
            raise self._playurl_style_error("解析播放地址", code, message)
        return parse_play_info(root)

    @staticmethod
    def _playurl_style_error(action: str, code: int, message: str) -> BiliApiError:
        hint = _PLAYURL_CODE_HINTS.get(code)
        if code == -101:
            return BiliAuthRequiredError(
                f"{action}失败: 登录态已失效,请重新登录(code={code})", code=code
            )
        text = f"{action}失败: code={code}, message={message}"
        if hint:
            text = f"{action}失败: {hint}(code={code})"
        return BiliApiError(text, code=code)

    def get_all_audio_streams(
        self, bvid: str, cid: int, opts: PlayOptions | None = None
    ) -> list[BiliAudioStreamInfo]:
        """对应 getAllAudioStreams:空音轨重试 + html5/mp4 回退。"""
        last_info: BiliPlayInfo | None = None
        for attempt in range(EMPTY_AUDIO_RETRY_COUNT):
            info = self.get_play_info_by_bvid(bvid, cid, opts)
            last_info = info
            streams = to_audio_stream_infos(info)
            if streams or not should_retry_empty_audio_fetch(info):
                if streams:
                    return streams
                return to_progressive_fallback_stream_infos(info)
            if attempt < EMPTY_AUDIO_RETRY_COUNT - 1:
                time.sleep((EMPTY_AUDIO_RETRY_DELAY_MS * (attempt + 1)) / 1000.0)

        html5_info = self.get_play_info_by_bvid(
            bvid, cid, build_html5_fallback_options(opts or PlayOptions())
        )
        html5_streams = to_progressive_fallback_stream_infos(html5_info)
        if html5_streams:
            return html5_streams
        return to_progressive_fallback_stream_infos(last_info) if last_info else []

    def resolve_audio_stream(
        self, bvid: str, cid: int = 0, preferred_quality: str = DEFAULT_AUDIO_QUALITY
    ) -> BiliAudioStreamInfo:
        """M2 播放主路径:cid 缺省时用分 P 列表第一 P 解析,再按音质偏好选轨。"""
        resolved_cid = cid
        if resolved_cid <= 0:
            pages = self.get_video_page_list(bvid)
            if not pages:
                raise BiliApiError("未能解析视频分P信息(视频可能不存在或不可见)")
            resolved_cid = pages[0].cid
        streams = self.get_all_audio_streams(bvid, resolved_cid)
        chosen = select_stream_by_preference(streams, preferred_quality)
        if chosen is None:
            raise BiliNoAudioStreamError(
                "该视频没有可用音频流(可能为纯图片稿件、地区受限或需要登录)"
            )
        return chosen
