"""封面小图加载:内存/磁盘缓存 + 后台线程下载 + 低清缩略规则。

- 网易云封面 CDN 支持 ?param=96y96 裁切(96px 足够 40px 控件在
  150% DPI 下清晰);
- B站封面 CDN 支持 @96w_96h.jpg 后缀等比缩放(仅 hdslb 域且原 URL
  无 @ 参数时追加);
- 缓存落盘为 <数据目录>/covers/<sha1(url)>(QImage.loadFromData
  不依赖扩展名),命中即不发网络请求;写盘走临时文件 + os.replace,
  多线程并发写同一缓存不会留下半截文件;
- 磁盘缓存是唯一无界增长的持久化数据,启动后异步 LRU 定量清理
  (trim_disk_cache,默认 32MiB):命中时 touch 更新 mtime 作为
  「最近使用」标记,超预算时从最旧开始删,腾到预算内为止;
- request() 供播放条单曲加载(每 URL 一个线程,同 URL 去重);
  preload() 供歌曲表整表预取:URL 按行序去重入队,由常驻 5 线程池
  并发消费(一次最多 5 个在途请求),再次调用即作废上一批任务;
- cover_ready 携带 (url, bytes) 由 UI 线程解码成圆角 pixmap。
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
from pathlib import Path

import httpx
from PySide6.QtCore import QObject, Signal

from ..data.store import default_data_dir
from ..log import get_logger

logger = get_logger("covers")

_THUMB_SIZE = 96
_TIMEOUT = httpx.Timeout(connect=5.0, read=8.0, write=8.0, pool=5.0)
# 整表预取并发线程数:全缓存的表本地读几毫秒一张,未缓存的表受网络
# 支配;5 在途已足够吃满带宽而不像几百线程那样同时打 CDN
_PRELOAD_WORKERS = 5
# 内存缓存条数上限(FIFO 淘汰):96px 缩略图每张几 KB,512 条约几 MB
_MEM_CACHE_MAX = 512
# 磁盘缓存定量上限(LRU 清理预算):命中即 touch mtime,启动后异步
# 超限清理——96px 缩略每张几 KB~几十 KB,32MiB 约能容纳两三千张
_DISK_CACHE_MAX_BYTES = 32 * 1024 * 1024
# 合法缓存文件名:sha1 hexdigest(40 个十六进制字符);.tmp 落盘残渣
# 与其他文件不进清理账,也不被删除
_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_cache_file(path: Path) -> bool:
    name = path.name
    return len(name) == 40 and _HEX_DIGITS.issuperset(name)


def trim_disk_cache(
    cache_dir: Path | None = None,
    max_bytes: int = _DISK_CACHE_MAX_BYTES,
) -> tuple[int, int]:
    """封面磁盘缓存 LRU 定量清理;返回 (删除文件数, 释放字节数)。

    mtime 即「最近使用」时间:写入时天然是新时间,命中时由 _load_sync
    touch 刷新。总量未超预算直接返回;超了从最旧开始删,删到预算内
    为止。扫描/删除中的 IO 错误(并发写/文件消失/权限)静默跳过,
    绝不抛出——清理失败只是下次再清,不能影响任何主流程。
    """
    directory = cache_dir if cache_dir is not None else default_data_dir() / "covers"
    entries: list[tuple[float, int, Path]] = []
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return 0, 0
    for path in candidates:
        try:
            if not (_is_cache_file(path) and path.is_file()):
                continue
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _, size, _ in entries)
    if total <= max_bytes or not entries:
        return 0, 0
    entries.sort(key=lambda item: (item[0], item[2].name))
    deleted = freed = 0
    for _mtime, size, path in entries:
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue  # 被并发读着(Windows 共享冲突)等:留给下次
        total -= size
        freed += size
        deleted += 1
    if deleted:
        logger.info(
            "covers disk cache trimmed: %d files, %.1f MiB freed",
            deleted,
            freed / 1024 / 1024,
        )
    return deleted, freed


def schedule_disk_cache_trim() -> threading.Thread:
    """启动后异步清一次封面缓存(不阻塞启动;返回线程供测试 join)。"""
    thread = threading.Thread(
        target=trim_disk_cache, daemon=True, name="cover-cache-trim"
    )
    thread.start()
    return thread


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

    使用方需自行做"过期请求丢弃"(比对回调携带的 url 与当前曲目/表格)。
    """

    cover_ready = Signal(str, bytes)  # (原始请求 url, 图像字节)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache_dir = default_data_dir() / "covers"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._mem: dict[str, bytes] = {}
        self._tasks: queue.Queue[tuple[int, str]] = queue.Queue()
        self._preload_version = 0
        self._workers_running = False

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

    def preload(self, urls: list[str]) -> None:
        """整表预取:按行序去重后入队,由常驻线程池并发加载。

        再次调用时版本号自增,上一批尚未开始(及已完成但未投递)的
        任务全部作废;空列表同样生效,即"仅取消"。
        """
        ordered = list(dict.fromkeys(url for url in urls if url))
        with self._lock:
            self._preload_version += 1
            version = self._preload_version
            start_workers = not self._workers_running and bool(ordered)
            if start_workers:
                self._workers_running = True
        if start_workers:
            for index in range(_PRELOAD_WORKERS):
                threading.Thread(
                    target=self._work, daemon=True, name=f"cover-preload-{index}"
                ).start()
        for url in ordered:
            self._tasks.put((version, url))

    # -- 内部 ---------------------------------------------------------------

    def _work(self) -> None:
        while True:
            version, url = self._tasks.get()
            try:
                if version != self._preload_version:
                    continue  # 已被更新的 preload 作废
                try:
                    data = self._load_sync(url)
                except Exception:  # noqa: BLE001 - 封面属增强信息,失败静默
                    data = b""
                if data and version == self._preload_version:
                    self.cover_ready.emit(url, data)
            finally:
                self._tasks.task_done()

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
        cached = self._mem.get(url)
        if cached:
            return cached
        cache_file = self._cache_dir / hashlib.sha1(url.encode()).hexdigest()
        if cache_file.is_file():
            data = cache_file.read_bytes()
            if data:
                try:
                    os.utime(cache_file)  # LRU:命中即记为最近使用
                except OSError:
                    pass
                self._remember(url, data)
                return data
        response = httpx.get(build_thumb_url(url), timeout=_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        data = response.content
        if data:
            tmp = cache_file.with_name(f"{cache_file.name}.{threading.get_ident()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cache_file)
            self._remember(url, data)
        return data

    def _remember(self, url: str, data: bytes) -> None:
        if len(self._mem) >= _MEM_CACHE_MAX:
            self._mem.pop(next(iter(self._mem)), None)
        self._mem[url] = data
