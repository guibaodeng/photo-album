"""缩略图磁盘缓存与后台生成服务。

缩略图按「文件路径 + 修改时间 + 尺寸」做键，缓存为 JPEG 文件；
生成任务在后台线程池中按优先级处理，回调在主线程（GLib）执行。
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Callable

from gi.repository import GLib

from .config import cache_dir
from .imaging import render_media_preview
from .models import MediaItem

#: 缩略图长边像素。卡片宽度最大约 460px，512 足够清晰，
#: 同时把单张纹理的内存控制在 ~1MB 以内。
THUMB_MAX_SIDE = 512

Callback = Callable[[MediaItem, bytes | None], None]


class ThumbnailRequest:
    """一次缩略图请求；滚动离开视野后可以取消，避免无谓的解码。"""

    __slots__ = ("item", "key", "priority", "callbacks", "cancelled")

    def __init__(self, item: MediaItem, key: str, priority: int, callback: Callback) -> None:
        self.item = item
        self.key = key
        self.priority = priority
        self.callbacks: list[Callback] = [callback]
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        self.callbacks.clear()


class ThumbnailCache:
    """磁盘缩略图缓存。"""

    # 解码逻辑变化（HEIC 改用 libheif）时递增，让旧缓存自动失效。
    VERSION = 4

    def __init__(self, directory: Path | None = None, max_side: int = THUMB_MAX_SIDE) -> None:
        self.directory = Path(directory) if directory else cache_dir() / "thumbnails"
        self.max_side = max_side

    # ------------------------------------------------------------------ 键
    def key(self, item: MediaItem) -> str:
        raw = "\0".join(
            [
                str(self.VERSION),
                os.fspath(item.path),
                str(int(item.mtime * 1_000_000)),
                str(int(item.ctime * 1_000_000)),
                str(item.size),
                str(self.max_side),
                item.kind.value,
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.jpg"

    # ------------------------------------------------------------------ 读写
    def read(self, key: str) -> bytes | None:
        path = self.path_for(key)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data or None

    def write(self, key: str, data: bytes) -> bool:
        if not data:
            return False
        path = self.path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            logging.warning("写入缩略图缓存失败：%s (%s)", path, exc)
            return False

    def clear(self) -> int:
        """删除全部缓存，返回释放的字节数。"""
        freed = 0
        if not self.directory.exists():
            return 0
        for root, _dirs, files in os.walk(self.directory, topdown=False):
            for name in files:
                try:
                    target = Path(root) / name
                    freed += target.stat().st_size
                    target.unlink()
                except OSError:
                    pass
            try:
                os.rmdir(root)
            except OSError:
                pass
        return freed

    def total_size(self) -> tuple[int, int]:
        """返回 (字节数, 文件数)。"""
        total = 0
        count = 0
        if not self.directory.exists():
            return (0, 0)
        for root, _dirs, files in os.walk(self.directory):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                    count += 1
                except OSError:
                    pass
        return (total, count)


class ThumbnailService:
    """后台缩略图生成服务。"""

    def __init__(
        self,
        cache: ThumbnailCache | None = None,
        *,
        workers: int = 4,
        max_side: int = THUMB_MAX_SIDE,
    ) -> None:
        self.cache = cache or ThumbnailCache(max_side=max_side)
        self.max_side = max_side
        self._queue: "queue.PriorityQueue[tuple[int, int, ThumbnailRequest | None]]" = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._lock = threading.Lock()
        self._pending: dict[str, ThumbnailRequest] = {}
        self._failed: set[str] = set()
        self._closed = False
        self._threads = [
            threading.Thread(target=self._worker, name=f"thumbnail-{index}", daemon=True)
            for index in range(max(1, workers))
        ]
        for thread in self._threads:
            thread.start()

    # ------------------------------------------------------------------ API
    def request(self, item: MediaItem, callback: Callback, *, priority: int = 1) -> ThumbnailRequest:
        """请求缩略图；``priority`` 越小越先处理（0 = 当前可见区域）。"""
        key = self.cache.key(item)
        with self._lock:
            if self._closed:
                return ThumbnailRequest(item, key, priority, callback)
            if key in self._failed:
                request = ThumbnailRequest(item, key, priority, callback)
                request.cancelled = True
                GLib.idle_add(self._invoke, callback, item, None, priority=GLib.PRIORITY_DEFAULT_IDLE)
                return request
            existing = self._pending.get(key)
            if existing is not None and not existing.cancelled:
                existing.callbacks.append(callback)
                return existing
            request = ThumbnailRequest(item, key, priority, callback)
            self._pending[key] = request
        self._queue.put((priority, next(self._sequence), request))
        return request

    def cached_bytes(self, item: MediaItem) -> bytes | None:
        """同步读取缓存（用于查看器快速显示预览）。"""
        return self.cache.read(self.cache.key(item))

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for request in self._pending.values():
                request.cancelled = True
                request.callbacks.clear()
            self._pending.clear()
        for _ in self._threads:
            self._queue.put((0, next(self._sequence), None))
        for thread in self._threads:
            thread.join(timeout=1.0)

    # ------------------------------------------------------------------ 内部
    def _worker(self) -> None:
        while True:
            _priority, _sequence, request = self._queue.get()
            if request is None:
                return
            try:
                if request.cancelled or self._closed:
                    continue
                key = request.key
                data: bytes | None = None
                try:
                    data = self.cache.read(key)
                    if data is None and not request.cancelled:
                        data = render_media_preview(request.item, self.max_side)
                        if data:
                            self.cache.write(key, data)
                except Exception:  # noqa: BLE001 - 一张坏图不应拖垮服务
                    data = None
                with self._lock:
                    if self._pending.get(key) is request:
                        del self._pending[key]
                    if request.cancelled or self._closed:
                        continue
                    callbacks = list(request.callbacks)
                    request.callbacks.clear()
                    if data is None:
                        self._failed.add(key)
                if not callbacks:
                    continue
                for callback in callbacks:
                    GLib.idle_add(self._invoke, callback, request.item, data, priority=GLib.PRIORITY_DEFAULT_IDLE)
            finally:
                self._queue.task_done()

    @staticmethod
    def _invoke(callback: Callback, item: MediaItem, data: bytes | None) -> bool:
        try:
            callback(item, data)
        except Exception:  # noqa: BLE001 - 界面对象可能已经销毁
            pass
        return False
