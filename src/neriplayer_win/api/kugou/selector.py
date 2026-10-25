"""酷狗音质档位映射(纯函数,便于单测)。

与 B站 的 BiliQuality 枚举不同,酷狗的档位不靠比特率比较:搜索/歌单结果
里直接给出各档的 hash,选档就是「挑哪个 hash 字段」,该字段为空则降档。
这一点是实测结论(见 docs/research/M6-kugou-api.md §5.3)。

实测四档的返回 bitrate:
    128k 用 FileHash       -> 128
    320k 用 HQFileHash     -> 320
    flac 用 SQFileHash     -> 937
    高码率用 ResFileHash   -> 1642

本项目沿用网易云的档位键(standard / exhigh / lossless)作为统一偏好,
酷狗侧只做映射,不新增设置项。
"""

from __future__ import annotations

# 统一档位键 -> 酷狗 hash 字段名(按优先级排列,前面的为空则降档)
# 顺序即降级顺序:用户选了 lossless 但没有 flac 时依次试 320、128。
_QUALITY_CHAIN: dict[str, tuple[str, ...]] = {
    "lossless": ("SQFileHash", "ResFileHash", "HQFileHash", "FileHash"),
    "exhigh": ("HQFileHash", "FileHash"),
    "standard": ("FileHash",),
}

# 统一档位键 -> 展示用短名(状态栏音质后缀)
QUALITY_LABELS: dict[str, str] = {
    "lossless": "无损",
    "exhigh": "320k",
    "standard": "128k",
}

# 实测各 hash 字段对应的码率,用于回填 KugouPlayableUrl.bitrate_kbps
_HASH_BITRATES: dict[str, int] = {
    "FileHash": 128,
    "HQFileHash": 320,
    "SQFileHash": 937,
    "ResFileHash": 1642,
    "SuperFileHash": 2000,
}

DEFAULT_QUALITY = "lossless"

def quality_chain(preferred: str) -> tuple[str, ...]:
    """取某个统一档位键的 hash 尝试链;未知键按默认档位处理。"""
    return _QUALITY_CHAIN.get(preferred, _QUALITY_CHAIN[DEFAULT_QUALITY])

def hash_field_bitrate(field_name: str) -> int:
    """hash 字段名 -> 码率(kbps);未知字段返回 0。"""
    return _HASH_BITRATES.get(field_name, 0)

def select_hash(hashes: dict[str, str], preferred: str) -> tuple[str, str]:
    """按偏好挑一个可用 hash,返回 (hash 值, 字段名)。

    hashes 是 `{字段名: hash}`(来自搜索结果或 getSongInfo 的 extra)。
    全部为空时返回 ("", ""),调用方据此判定「无可用音质」。
    """
    for field_name in quality_chain(preferred):
        value = (hashes.get(field_name) or "").strip()
        if value:
            return value, field_name
    return "", ""

def quality_label(preferred: str, field_name: str = "") -> str:
    """状态栏音质后缀。

    优先按实际生效的 hash 字段报码率(用户选无损但只有 320k 时如实报 320k),
    字段未知时回落到偏好档位的短名。
    """
    bitrate = hash_field_bitrate(field_name)
    if bitrate:
        if bitrate >= 900:
            return QUALITY_LABELS["lossless"]
        if bitrate >= 320:
            return "320k"
        return "128k"
    return QUALITY_LABELS.get(preferred, "")
