"""播放条封面小图加载:磁盘缓存 + 后台线程下载 + 低清缩略规则。

- 网易云封面 CDN 支持 ?param=96y96 裁切(96px 足够 40px 控件在
  150% DPI 下清晰);
- B站封面 CDN 支持 @96w_96h.jpg 后缀等比缩放(仅 hdslb 域且原 URL
  无 @ 参数时追加);
- 缓存落盘为 <数据目录>/covers/<sha1(url)>.img(扩展名按 Content-Type
  猜,QImage.loadFromData 不依赖扩展名),命中即不发网络请求;
- 下载经 workers 线程;cover_ready 携带 (url, bytes) 由 UI 线程解码
  成圆角 pixmap。
"""

from __future__ import annotations

import hashlib
import threading

import httpx
from PySide6.QtCore import QObject, Signal

from ..data.store import default_data_dir

_THUMB_SIZE = 96
_TIMEOUT = httpx.Timeout(connect=5.0, read=8.0, write=8.0, pool=5.0)


def build_thumb_url(url: str) -> str:
    """按 CDN 规则把原图 URL 换成低清缩略版;不认识的原样返回。"""
    if not url:
        return url
    if "music.126.net" in url or "163.com" in url:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}param={_THUMB_SIZE}y{_THUMB_SIZE}"
    if "hdslb.com" in url and "@" not in url.rsplit("/", 1)[-1]:
        return f"{url}@{_THUMB_SIZE}w_{_THUMB_SIZE}h.jpg"
    return url


class CoverLoader(QObject):
    """按 URL 异步加载封面;同 URL 去重,结果经 cover_ready 回 UI 线程。

    使用方需自行做"过期请求丢弃"(比对回调携带的 url 与当前曲目)。
    """

    cover_ready = Signal(str, bytes)  # (原始请求 url, 图像字节)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache_dir = default_data_dir() / "covers"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    def request(self, url: str) -> None:
        """请求一个封面;空 URL 直接忽略(由调用方清占位)。"""
        if not url:
            return
        with self._lock:
            if url in self._inflight:
                return
            self._inflight.add(url)
        threading.Thread(
            target=self._load, args=(url,), daemon=True, name="cover-loader"
        ).start()

    # -- 内部 ---------------------------------------------------------------

    def _load(self, url: str) -> None:
        try:
            data = self._load_sync(url)
        except Exception:  # noqa: BLE001 - 封面属增强信息,失败静默
            data = b""
        finally:
            with self._lock:
                self._inflight.discard(url)
        if data:
            self.cover_ready.emit(url, data)

    def _load_sync(self, url: str) -> bytes:
        cache_file = self._cache_dir / hashlib.sha1(url.encode()).hexdigest()
        if cache_file.is_file():
            data = cache_file.read_bytes()
            if data:
                return data
        response = httpx.get(build_thumb_url(url), timeout=_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        data = response.content
        if data:
            cache_file.write_bytes(data)
        return data
