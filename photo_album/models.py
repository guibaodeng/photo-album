"""媒体数据模型。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

#: 支持的图片扩展名（小写，含点）。
IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".jfif",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
        ".avif",
    }
)

#: 支持的视频扩展名（小写，含点）。
VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".m4v",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".wmv",
        ".flv",
        ".3gp",
        ".mpg",
        ".mpeg",
        ".ts",
        ".mts",
    }
)

#: 单张卡片在照片墙里的宽高比范围。过宽/过高的素材按此裁切展示，
#: 避免极端全景图或长截图破坏整体网格节奏。
MIN_ASPECT = 0.55
MAX_ASPECT = 2.4


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


def is_video_name(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXTENSIONS


@dataclass(slots=True)
class MediaItem:
    """一条照片或视频记录。"""

    path: Path
    kind: MediaKind
    size: int
    mtime: float
    taken_at: datetime
    width: int
    height: int
    orientation: int = 1
    duration: float = 0.0
    date_source: str = "mtime"

    # ------------------------------------------------------------------ 基本属性
    @property
    def name(self) -> str:
        return self.path.name

    @property
    def parent(self) -> Path:
        return self.path.parent

    @property
    def is_video(self) -> bool:
        return self.kind is MediaKind.VIDEO

    @property
    def year(self) -> int:
        return self.taken_at.year

    @property
    def month(self) -> int:
        return self.taken_at.month

    @property
    def year_month(self) -> tuple[int, int]:
        return (self.taken_at.year, self.taken_at.month)

    @property
    def sort_key(self) -> tuple[datetime, str]:
        return (self.taken_at, str(self.path))

    @property
    def aspect(self) -> float:
        """展示用宽高比（考虑 EXIF 旋转，并做上下限裁剪）。"""
        width, height = self.width, self.height
        if self.orientation in (5, 6, 7, 8):
            width, height = height, width
        if width <= 0 or height <= 0:
            return 1.0
        ratio = width / height
        return max(MIN_ASPECT, min(MAX_ASPECT, ratio))

    # ------------------------------------------------------------------ 展示文本
    @property
    def date_text(self) -> str:
        return self.taken_at.strftime("%Y年%-m月%-d日 %H:%M")

    @property
    def date_short(self) -> str:
        return self.taken_at.strftime("%Y-%m-%d %H:%M")

    @property
    def month_text(self) -> str:
        return f"{self.taken_at.year}年{self.taken_at.month}月"

    @property
    def size_text(self) -> str:
        return format_size(self.size)

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration)

    @property
    def resolution_text(self) -> str:
        width, height = self.width, self.height
        if self.orientation in (5, 6, 7, 8):
            width, height = height, width
        return f"{width} × {height}"

    @property
    def kind_text(self) -> str:
        return "视频" if self.is_video else "照片"

    def tooltip_text(self) -> str:
        bits = [self.name, self.date_text]
        if self.is_video and self.duration > 0:
            bits.append(self.duration_text)
        return "\n".join(bits)

    def matches(self, query: str) -> bool:
        if not query:
            return True
        needle = query.casefold()
        if needle in self.name.casefold():
            return True
        folder = str(self.parent).casefold()
        return needle in folder


def format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_relative_path(item: MediaItem, root: Path) -> str:
    try:
        return str(item.path.relative_to(root))
    except ValueError:
        return str(item.path)


def same_file_entry(a: MediaItem, b: MediaItem) -> bool:
    return os.fspath(a.path) == os.fspath(b.path)
