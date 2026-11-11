"""媒体库扫描。

扫描指定的根目录，收集照片与视频，并尽量读取 EXIF / ffprobe 元数据
（拍摄时间、宽高、方向、视频时长）。整个扫描过程在后台线程执行，
UI 通过进度回调获得反馈。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import warnings
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

warnings.filterwarnings("ignore", category=DeprecationWarning, module=__name__)

from .models import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MediaItem,
    MediaKind,
    is_image_name,
    is_video_name,
)

try:  # GExiv2 属于可选依赖：缺失时自动退回到 Pillow。
    import gi

    gi.require_version("GExiv2", "0.10")
    from gi.repository import GExiv2  # type: ignore

    _HAS_GEXIV2 = True
    # 某些照片的 EXIF/XMP 结构损坏，exiv2 会向 stderr 打印大量告警，
    # 这里只保留静默模式，避免污染终端输出。
    GExiv2.log_set_level(GExiv2.LogLevel.MUTE)
except (ImportError, ValueError):  # pragma: no cover - 取决于运行环境
    GExiv2 = None  # type: ignore
    _HAS_GEXIV2 = False

try:
    from PIL import Image  # type: ignore

    _HAS_PILLOW = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    _HAS_PILLOW = False

_FFPROBE = shutil.which("ffprobe")
_HEIF_INFO = shutil.which("heif-info")

#: 应用内编辑过日期后写入的标记，用来让 EXIF 日期覆盖文件名的日期。
EXIF_EDITOR_MARKER = "Photo Album EXIF Editor"

#: 这些格式的显示尺寸由 HEIF 容器决定，EXIF 里的宽高可能是编辑前的原图尺寸。
_HEIF_SUFFIXES = frozenset({".heic", ".heif"})

_EXIF_DATE_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
)

# 很多相机/手机导出的文件名直接带拍摄时间，例如：
#   20190424_085456879_iOS.PNG
#   PXL_20240101_123456789.jpg
#   Screenshot_2024-01-01-12-34-56.png
# 没有 EXIF 时优先用文件名里的日期，避免把复制/同步时间当成拍摄时间。
_FILENAME_DATETIME_COMPACT = re.compile(
    r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])"
    r"[_\-.]?"
    r"([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?:\d{0,6})?(?!\d)"
)
_FILENAME_DATETIME_SEPARATED = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.](0[1-9]|1[0-2])[-_.](0[1-9]|[12]\d|3[01])"
    r"[ T_-]([01]\d|2[0-3])[-_.:]([0-5]\d)[-_.:]([0-5]\d)(?!\d)"
)
_FILENAME_DATE_ONLY = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.](0[1-9]|1[0-2])[-_.](0[1-9]|[12]\d|3[01])(?!\d)"
)


@dataclass
class ScanResult:
    items: list[MediaItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def image_count(self) -> int:
        return sum(1 for item in self.items if not item.is_video)

    @property
    def video_count(self) -> int:
        return sum(1 for item in self.items if item.is_video)


class ScanCancelled(Exception):
    """扫描被前一次扫描/窗口关闭取消。"""


# --------------------------------------------------------------------------- 工具
def _parse_exif_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().strip("\x00").strip()
    if not text or text.startswith("0000"):
        return None
    for fmt in _EXIF_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 部分设备写入 "2024:03:28 12:21:21+08:00"
    for fmt in ("%Y:%m:%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=None) if parsed.tzinfo is None else parsed.astimezone().replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _parse_filename_datetime(name: str) -> datetime | None:
    """从文件名中提取日期；优先带时分秒的格式，最后才用日期-only。"""
    for pattern, has_time in (
        (_FILENAME_DATETIME_COMPACT, True),
        (_FILENAME_DATETIME_SEPARATED, True),
        (_FILENAME_DATE_ONLY, False),
    ):
        for match in pattern.finditer(name):
            try:
                values = [int(match.group(index)) for index in range(1, 7 if has_time else 4)]
                if has_time:
                    return datetime(*values)
                return datetime(*values, 0, 0, 0)
            except ValueError:
                continue
    return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    """解析 ffprobe 的 creation_time（通常是 UTC）。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp)


def _probe_ffprobe(path: Path) -> dict | None:
    if not _FFPROBE:
        return None
    try:
        proc = subprocess.run(
            [
                _FFPROBE,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                os.fspath(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _exif_metadata(path: Path) -> tuple[int, int, int, datetime | None, str]:
    """返回 (宽, 高, 方向, 拍摄时间, 来源)。失败时宽高为 0。"""
    if not _HAS_GEXIV2:
        return 0, 0, 1, None, "mtime"
    try:
        metadata = GExiv2.Metadata()
        metadata.open_path(os.fspath(path))
        width = int(metadata.get_pixel_width())
        height = int(metadata.get_pixel_height())
        orientation = 1
        try:
            raw_orientation = metadata.get_tag_string("Exif.Image.Orientation")
            if raw_orientation:
                orientation = int(raw_orientation.strip() or 1)
        except (ValueError, Exception):  # noqa: BLE001 - GExiv2 抛出的异常类型较杂
            orientation = 1
        taken: datetime | None = None
        for tag in (
            "Exif.Photo.DateTimeOriginal",
            "Exif.Photo.DateTimeDigitized",
            # 少数写入器把 DateTimeOriginal 放在 Image IFD 中。
            "Exif.Image.DateTimeOriginal",
            "Exif.Image.DateTime",
        ):
            try:
                taken = _parse_exif_datetime(metadata.get_tag_string(tag))
            except Exception:  # noqa: BLE001
                taken = None
            if taken is not None:
                break
        if width > 0 and height > 0:
            return width, height, orientation, taken, "exif" if taken else "mtime"
        return 0, 0, orientation, taken, "mtime"
    except Exception:  # noqa: BLE001
        return 0, 0, 1, None, "mtime"


def _has_manual_exif_date(path: Path) -> bool:
    """检查文件是否由本应用编辑过，需要优先信任 EXIF 日期。"""
    if not _HAS_GEXIV2:
        return False
    try:
        metadata = GExiv2.Metadata()
        metadata.open_path(os.fspath(path))
        value = metadata.get_tag_string("Exif.Image.ProcessingSoftware")
        return (value or "").strip() == EXIF_EDITOR_MARKER
    except Exception:  # noqa: BLE001
        return False


def _heif_info_dimensions(path: Path) -> tuple[int, int] | None:
    """用 heif-info 读取 HEIF 主图的显示尺寸（已包含容器旋转/裁剪）。"""
    if not _HEIF_INFO:
        return None
    try:
        proc = subprocess.run(
            [_HEIF_INFO, os.fspath(path)],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    match = re.search(r"^image:\s*(\d+)x(\d+)", proc.stdout, re.MULTILINE)
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _pillow_metadata(path: Path) -> tuple[int, int, int, datetime | None]:
    if not _HAS_PILLOW:
        return 0, 0, 1, None
    try:
        with Image.open(os.fspath(path)) as image:
            width, height = image.size
            orientation = 1
            taken: datetime | None = None
            try:
                exif = image.getexif()
                orientation = int(exif.get(274) or 1)
                taken = _parse_exif_datetime(exif.get(36867) or exif.get(36868) or exif.get(306))
            except Exception:  # noqa: BLE001
                pass
            return width, height, orientation, taken
    except Exception:  # noqa: BLE001
        return 0, 0, 1, None


def _video_metadata(path: Path, stat: os.stat_result) -> tuple[int, int, float, datetime | None]:
    width = height = 0
    duration = 0.0
    taken: datetime | None = None
    data = _probe_ffprobe(path)
    if data:
        for stream in data.get("streams", []):
            if stream.get("codec_type") != "video":
                continue
            try:
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                width = height = 0
            if not duration:
                for key in ("duration", "dvr_duration"):
                    try:
                        duration = float(stream.get(key) or 0)
                    except (TypeError, ValueError):
                        duration = 0.0
                    if duration > 0:
                        break
            rotation = 0
            for side in stream.get("side_data_list", []) or []:
                try:
                    rotation = int(round(float(side.get("rotation") or 0)))
                except (TypeError, ValueError):
                    rotation = 0
                if rotation:
                    break
            if not rotation:
                try:
                    rotation = int(round(float(stream.get("tags", {}).get("rotate") or 0)))
                except (TypeError, ValueError):
                    rotation = 0
            if abs(rotation) % 180 == 90 and width and height:
                width, height = height, width
            if taken is None:
                tags = stream.get("tags") or {}
                taken = _parse_iso_datetime(tags.get("creation_time"))
            break
        fmt = data.get("format", {}) or {}
        if not duration:
            try:
                duration = float(fmt.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0.0
        if taken is None:
            taken = _parse_iso_datetime((fmt.get("tags") or {}).get("creation_time"))
    if width <= 0 or height <= 0:
        # 无法识别的视频仍然保留，按 16:9 占位展示。
        width, height = 1920, 1080
    if taken is None:
        taken = _from_timestamp(stat.st_mtime)
    return width, height, max(0.0, duration), taken


def _probe_item(path: Path, stat: os.stat_result, kind: MediaKind) -> MediaItem | None:
    """读取单个文件的元数据；无法识别时返回 None。"""
    size = int(stat.st_size)
    mtime = float(stat.st_mtime)
    if kind is MediaKind.VIDEO:
        width, height, duration, taken = _video_metadata(path, stat)
        return MediaItem(
            path=path,
            kind=kind,
            size=size,
            mtime=mtime,
            taken_at=taken,
            width=width,
            height=height,
            orientation=1,
            duration=duration,
            date_source="metadata",
        )

    # GExiv2 读取 HEIC/JPEG 的头部最快，也最可靠；PNG/WebP 等交给 Pillow。
    if path.suffix.lower() in {".heic", ".heif", ".avif", ".jpg", ".jpeg", ".jfif"}:
        width, height, orientation, taken, source = _exif_metadata(path)
    else:
        width, height, orientation, taken = _pillow_metadata(path)
        source = "exif" if taken else "mtime"
    if path.suffix.lower() in _HEIF_SUFFIXES:
        # HEIF 的 EXIF 可能来自原始拍摄图，主图经过裁剪/旋转后尺寸会不同；
        # 以 libheif 报告的显示尺寸为准，避免缩略图比例失真。
        info_dimensions = _heif_info_dimensions(path)
        if info_dimensions is not None:
            width, height = info_dimensions
            orientation = 1
    if width <= 0 or height <= 0:
        width, height, orientation, taken = _pillow_metadata(path)
        source = "exif" if taken else "mtime"
    if width <= 0 or height <= 0:
        # HEIC 等格式在没有 GExiv2/Pillow 支持时，尝试 ffprobe。
        data = _probe_ffprobe(path)
        if data:
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    try:
                        width = int(stream.get("width") or 0)
                        height = int(stream.get("height") or 0)
                    except (TypeError, ValueError):
                        width = height = 0
                    break
    if width <= 0 or height <= 0:
        return None
    filename_taken = _parse_filename_datetime(path.name)
    if taken is not None and filename_taken is not None:
        if taken.date() != filename_taken.date():
            # EXIF 与文件名日期对不上时优先文件名；但如果日期是本应用手动
            # 编辑过的，则信任编辑后的 EXIF 日期。
            if not _has_manual_exif_date(path):
                taken = filename_taken
                source = "filename"
    elif taken is None and filename_taken is not None:
        taken = filename_taken
        source = "filename"
    if taken is None:
        taken = _from_timestamp(mtime)
        source = "mtime"
    return MediaItem(
        path=path,
        kind=kind,
        size=size,
        mtime=mtime,
        taken_at=taken,
        width=width,
        height=height,
        orientation=orientation,
        duration=0.0,
        date_source=source,
    )


def read_media_item(path: Path) -> MediaItem | None:
    """重新读取单个文件的元数据（用于编辑 EXIF 后刷新条目）。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    if is_video_name(path.name):
        kind = MediaKind.VIDEO
    elif is_image_name(path.name):
        kind = MediaKind.IMAGE
    else:
        return None
    return _probe_item(path, stat, kind)


ProgressCallback = Callable[[int, int, str], None]
BatchCallback = Callable[[list[MediaItem]], None]


# --------------------------------------------------------------------------- 扫描器
class LibraryScanner:
    """遍历目录并并发读取元数据。"""

    def __init__(
        self,
        roots: Path | Iterable[Path],
        *,
        recursive: bool = True,
        include_hidden: bool = False,
        excluded_dirs: Iterable[str] = (),
        workers: int | None = None,
    ) -> None:
        if isinstance(roots, (str, Path)):
            roots = [roots]
        self.roots = [Path(root) for root in roots]
        self.root = self.roots[0] if self.roots else Path.home()
        self.recursive = recursive
        self.include_hidden = include_hidden
        self.excluded_dirs = {name.casefold() for name in excluded_dirs}
        self.workers = workers or min(8, max(2, (os.cpu_count() or 4)))

    # ------------------------------------------------------------------ 遍历
    def _walk(self, cancel: threading.Event, errors: list[str]) -> list[tuple[Path, os.stat_result, MediaKind]]:
        found: list[tuple[Path, os.stat_result, MediaKind]] = []
        visited: set[tuple[int, int]] = set()
        seen_files: set[tuple[int, int]] = set()
        stack: list[Path] = []
        for root in self.roots:
            try:
                root_stat = root.stat()
            except OSError as exc:
                if len(errors) < 20:
                    errors.append(f"{root}: {exc}")
                continue
            key = (int(root_stat.st_dev), int(root_stat.st_ino))
            if key in visited:
                continue
            visited.add(key)
            stack.append(root)
        while stack:
            if cancel.is_set():
                raise ScanCancelled
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if cancel.is_set():
                            raise ScanCancelled
                        name = entry.name
                        if not self.include_hidden and name.startswith("."):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=True):
                                if not self.recursive:
                                    continue
                                if name.casefold() in self.excluded_dirs:
                                    continue
                                resolved = entry.stat(follow_symlinks=True)
                                key = (int(resolved.st_dev), int(resolved.st_ino))
                                if key in visited:
                                    continue
                                visited.add(key)
                                stack.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=True):
                                continue
                            suffix = Path(name).suffix.lower()
                            if suffix in IMAGE_EXTENSIONS:
                                kind = MediaKind.IMAGE
                            elif suffix in VIDEO_EXTENSIONS:
                                kind = MediaKind.VIDEO
                            else:
                                continue
                            stat = entry.stat(follow_symlinks=True)
                            file_key = (int(stat.st_dev), int(stat.st_ino))
                            if file_key in seen_files:
                                continue
                            seen_files.add(file_key)
                            found.append((Path(entry.path), stat, kind))
                        except OSError as exc:
                            if len(errors) < 20:
                                errors.append(f"{entry.path}: {exc}")
            except OSError as exc:
                if len(errors) < 20:
                    errors.append(f"{directory}: {exc}")
        return found

    # ------------------------------------------------------------------ 对外入口
    def scan(
        self,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
        batch: BatchCallback | None = None,
    ) -> ScanResult:
        started = time.monotonic()
        cancel = cancel or threading.Event()
        errors: list[str] = []
        if progress:
            progress(0, 0, "walk")
        candidates = self._walk(cancel, errors)
        total = len(candidates)
        if progress:
            progress(0, total, "probe")

        items: list[MediaItem] = []
        done = 0
        lock = threading.Lock()

        def work(candidate: tuple[Path, os.stat_result, MediaKind]) -> MediaItem | None:
            path, stat, kind = candidate
            if cancel.is_set():
                return None
            try:
                return _probe_item(path, stat, kind)
            except Exception as exc:  # noqa: BLE001 - 单个坏文件不应中断扫描
                with lock:
                    if len(errors) < 20:
                        errors.append(f"{path}: {exc}")
                return None

        pending_batch: list[MediaItem] = []
        last_batch = time.monotonic()
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="scan") as pool:
            futures = [pool.submit(work, candidate) for candidate in candidates]
            for future in as_completed(futures):
                if cancel.is_set():
                    for pending in futures:
                        pending.cancel()
                    raise ScanCancelled
                try:
                    item = future.result()
                except CancelledError:
                    continue
                done += 1
                if item is not None:
                    items.append(item)
                    if batch is not None:
                        pending_batch.append(item)
                if progress and (done % 20 == 0 or done == total):
                    progress(done, total, "probe")
                if batch is not None and pending_batch:
                    now = time.monotonic()
                    if len(pending_batch) >= 40 or now - last_batch >= 0.12:
                        batch(list(pending_batch))
                        pending_batch.clear()
                        last_batch = now

        if batch is not None and pending_batch:
            batch(list(pending_batch))

        items.sort(key=lambda item: item.sort_key, reverse=True)
        elapsed = time.monotonic() - started
        if progress:
            progress(total, total, "done")
        return ScanResult(items=items, errors=errors, elapsed=elapsed)


def iter_media_files(
    roots: Path | Iterable[Path],
    *,
    recursive: bool = True,
    include_hidden: bool = False,
    excluded_dirs: Iterable[str] = (),
) -> Iterator[Path]:
    """仅列出媒体文件路径（供测试或命令行使用）。"""
    scanner = LibraryScanner(
        roots,
        recursive=recursive,
        include_hidden=include_hidden,
        excluded_dirs=excluded_dirs,
    )
    for path, _stat, _kind in scanner._walk(threading.Event(), []):  # noqa: SLF001
        yield path


def sort_files_by_name(paths: Iterable[Path]) -> list[Path]:
    return sorted(paths, key=lambda p: p.name.casefold())


__all__ = [
    "LibraryScanner",
    "ScanCancelled",
    "ScanResult",
    "iter_media_files",
    "is_image_name",
    "is_video_name",
]
