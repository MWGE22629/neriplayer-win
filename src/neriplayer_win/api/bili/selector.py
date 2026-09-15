"""B站音频流候选排序与音质选择(纯函数,便于单测)。

逐字对照 reference/NeriPlayer-Android 的
data/platform/bili/BiliAudioSelector.kt:优先 upos 镜像、按标签/比特率
选轨、不硬编码 dash 音频 ID。断言移植自 BiliAudioSelectorTest.kt。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping
from urllib.parse import urlparse

from .models import BiliAudioStreamInfo


def is_bili_stream_host(host: str) -> bool:
    """对应 isBiliStreamHost:upos*.bilivideo.* 或 *.mountaintoys.cn。"""
    normalized = host.strip().lower()
    if not normalized:
        return False
    return "bilivideo." in normalized or normalized.endswith(".mountaintoys.cn")


def is_bili_stream_url(url: str) -> bool:
    """对应 isBiliStreamUrl。"""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return is_bili_stream_host(host)


def _score_bili_stream_url(url: str) -> int:
    """对应 scoreBiliStreamUrl:upos 镜像 3 > 其他 bilivideo 2 > mountaintoys 1。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host.startswith("upos-") and "bilivideo." in host:
        return 3
    if "bilivideo." in host:
        return 2
    if host.endswith(".mountaintoys.cn"):
        return 1
    return 0


def prioritize_bili_stream_urls(
    primary_url: str, backup_urls: list[str] | tuple[str, ...] | None
) -> list[str]:
    """对应 prioritizeBiliStreamUrls:去重后按 host 评分稳定排序。"""
    merged: list[str] = [primary_url]
    merged.extend(backup_urls or [])
    deduped: list[str] = []
    for url in merged:
        trimmed = url.strip()
        if trimmed and trimmed not in deduped:
            deduped.append(trimmed)

    scored = sorted(
        enumerate(deduped),
        key=lambda pair: (-_score_bili_stream_url(pair[1]), pair[0]),
    )
    return [url for _, url in scored]


class BiliQuality(Enum):
    """对应 BiliQuality:统一音质偏好 key 与最低比特率。"""

    DOLBY = ("dolby", 0)
    HIRES = ("hires", 1000)
    LOSSLESS = ("lossless", 500)
    HIGH = ("high", 180)
    MEDIUM = ("medium", 120)
    LOW = ("low", 60)

    def __init__(self, key: str, min_bitrate_kbps: int) -> None:
        self.key = key
        self.min_bitrate_kbps = min_bitrate_kbps


_BILI_QUALITY_ORDER: list[BiliQuality] = [
    BiliQuality.DOLBY,
    BiliQuality.HIRES,
    BiliQuality.LOSSLESS,
    BiliQuality.HIGH,
    BiliQuality.MEDIUM,
    BiliQuality.LOW,
]


def bili_quality_from_key(key: str) -> BiliQuality:
    """对应 BiliQuality.fromKey:未知 key 回退 HIGH。"""
    normalized = key.strip().lower()
    for quality in _BILI_QUALITY_ORDER:
        if quality.key == normalized:
            return quality
    return BiliQuality.HIGH


# M5「音质偏好」共用一套档位:网易云侧只有 standard/exhigh/lossless 三档,
# 这里换算成 B站 selector 的实际偏好键(低/中/高三档各取其位)。
_NETEASE_TO_BILI_QUALITY_KEY = {
    "standard": BiliQuality.LOW.key,
    "exhigh": BiliQuality.HIGH.key,
    "lossless": BiliQuality.LOSSLESS.key,
}


def bili_quality_key_from_netease_level(netease_quality: str) -> str:
    """网易云音质档位 → B站音质偏好 key(纯函数,便于单测)。

    对应关系(以 _BILI_QUALITY_ORDER 的实际键为准):
    - standard(标准 128K)→ low:B站常规音轨的最低档;
    - exhigh(极高 320K)→ high:常规有损音轨的最高档,在「无损之上还有
      Hi-Res/杜比」的完整阶梯里属中档,且 320K 落在 high 档 180–500kbps 区间;
    - lossless(无损 FLAC)→ lossless:该偏好的选轨分支会命中 Hi-Res/FLAC
      无损流(即 B站事实上的最高音质),不满足时自动降级,无需在这里展开;
      刻意不映射 dolby——杜比是环绕声制式而非「更高音质」的通用诉求。
    未知/空档位回退 high(与 BiliClient.DEFAULT_AUDIO_QUALITY 一致)。
    """
    normalized = netease_quality.strip().lower()
    return _NETEASE_TO_BILI_QUALITY_KEY.get(normalized, BiliQuality.HIGH.key)


def bili_quality_degrade_chain(from_quality: BiliQuality) -> list[BiliQuality]:
    """对应 BiliQuality.degradeChain:从当前到更低的一条降级链。"""
    try:
        start = _BILI_QUALITY_ORDER.index(from_quality)
    except ValueError:
        start = 0
    return _BILI_QUALITY_ORDER[start:]


def _regular_quality_upper_bound_exclusive(quality: BiliQuality) -> int:
    if quality is BiliQuality.LOSSLESS:
        return BiliQuality.HIRES.min_bitrate_kbps
    if quality is BiliQuality.HIGH:
        return BiliQuality.LOSSLESS.min_bitrate_kbps
    if quality is BiliQuality.MEDIUM:
        return BiliQuality.HIGH.min_bitrate_kbps
    if quality is BiliQuality.LOW:
        return BiliQuality.MEDIUM.min_bitrate_kbps
    return 2**31 - 1  # Int.MAX_VALUE


def _normalized_quality_tag(stream: BiliAudioStreamInfo) -> str | None:
    tag = (stream.quality_tag or "").strip().lower()
    return tag or None


def _matches_regular_quality(stream: BiliAudioStreamInfo, quality: BiliQuality) -> bool:
    if _normalized_quality_tag(stream) is not None:
        return False
    upper = _regular_quality_upper_bound_exclusive(quality)
    return quality.min_bitrate_kbps <= stream.bitrate_kbps < upper


def _is_lossless_like_stream(stream: BiliAudioStreamInfo) -> bool:
    tag = _normalized_quality_tag(stream)
    if tag in ("lossless", "hires"):
        return True
    mime = stream.mime_type.split(";", 1)[0].strip().lower()
    return mime in ("audio/flac", "audio/x-flac")


def select_stream_by_preference(
    available: list[BiliAudioStreamInfo], preferred_key: str
) -> BiliAudioStreamInfo | None:
    """对应 selectStreamByPreference:按偏好从高到低选,不满足自动降级。"""
    if not available:
        return None
    pref = bili_quality_from_key(preferred_key)

    regular_sorted = sorted(
        (s for s in available if _normalized_quality_tag(s) is None),
        key=lambda s: s.bitrate_kbps,
        reverse=True,
    )
    tagged_sorted = sorted(
        (s for s in available if _normalized_quality_tag(s) is not None),
        key=lambda s: s.bitrate_kbps,
        reverse=True,
    )
    seen_urls: set[str] = set()
    sorted_streams: list[BiliAudioStreamInfo] = []
    for stream in regular_sorted + tagged_sorted:
        if stream.url not in seen_urls:
            seen_urls.add(stream.url)
            sorted_streams.append(stream)

    if pref is BiliQuality.DOLBY:
        for stream in sorted_streams:
            if _normalized_quality_tag(stream) == "dolby":
                return stream
    elif pref is BiliQuality.HIRES:
        for stream in sorted_streams:
            if _normalized_quality_tag(stream) == "hires":
                return stream
    elif pref is BiliQuality.LOSSLESS:
        for stream in sorted_streams:
            if _is_lossless_like_stream(stream):
                return stream

    for quality in bili_quality_degrade_chain(pref):
        hit: BiliAudioStreamInfo | None = None
        if quality is BiliQuality.DOLBY:
            hit = next(
                (s for s in sorted_streams if _normalized_quality_tag(s) == "dolby"),
                None,
            )
        elif quality is BiliQuality.HIRES:
            hit = next(
                (s for s in sorted_streams if _normalized_quality_tag(s) == "hires"),
                None,
            )
        elif quality is BiliQuality.LOSSLESS:
            hit = next(
                (s for s in sorted_streams if _is_lossless_like_stream(s)),
                None,
            )
            if hit is None:
                hit = next(
                    (s for s in regular_sorted if _matches_regular_quality(s, quality)),
                    None,
                )
        else:
            hit = next(
                (s for s in regular_sorted if _matches_regular_quality(s, quality)),
                None,
            )
        if hit is not None:
            return hit

    return sorted_streams[0] if sorted_streams else None
