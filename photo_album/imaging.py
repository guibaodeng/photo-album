"""图片解码与缩略图生成。

Pillow 负责常见格式；HEIC/HEIF、损坏的嵌入缩略图以及视频首帧
则交给 ffmpeg 处理。所有函数都可以在后台线程调用。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .models import MediaItem

try:
    from PIL import Image, ImageOps  # type: ignore

    _HAS_PILLOW = True
except ImportError:  # pragma: no cover - 取决于运行环境
    Image = None  # type: ignore
    ImageOps = None  # type: ignore
    _HAS_PILLOW = False

_FFMPEG = shutil.which("ffmpeg")
_HEIF_CONVERT = shutil.which("heif-convert")
_IMAGEMAGICK = shutil.which("magick") or shutil.which("convert")

#: 这些格式由 libheif/ImageMagick 解码；它们的 EXIF 方向由解码器直接应用，
#: 不能再手动旋转一次（否则会出现横竖颠倒）。
_HEIF_SUFFIXES = frozenset({".heic", ".heif", ".avif"})

# HEIC 解码需要把完整 12MP 图像读入内存，限制同时解码数量。
_HEIF_SEMAPHORE = threading.Semaphore(2)

# EXIF Orientation -> Pillow 的转置操作
_ORIENTATION_OPS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT if _HAS_PILLOW else None,
    3: Image.Transpose.ROTATE_180 if _HAS_PILLOW else None,
    4: Image.Transpose.FLIP_TOP_BOTTOM if _HAS_PILLOW else None,
    5: Image.Transpose.TRANSPOSE if _HAS_PILLOW else None,
    6: Image.Transpose.ROTATE_270 if _HAS_PILLOW else None,
    7: Image.Transpose.TRANSVERSE if _HAS_PILLOW else None,
    8: Image.Transpose.ROTATE_90 if _HAS_PILLOW else None,
}


def apply_orientation(image: "Image.Image", orientation: int) -> "Image.Image":
    operation = _ORIENTATION_OPS.get(int(orientation or 1))
    if operation is None:
        return image
    return image.transpose(operation)


def _encode_jpeg(image: "Image.Image", quality: int = 86) -> bytes:
    if image.mode not in ("RGB", "L"):
        # 透明背景（PNG/GIF）压到深色底上，避免出现黑块。
        background = Image.new("RGB", image.size, (24, 24, 26))
        if image.mode in ("RGBA", "LA", "P"):
            converted = image.convert("RGBA")
            background.paste(converted, mask=converted.getchannel("A"))
        else:
            background.paste(image.convert("RGB"))
        image = background
    else:
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
    return buffer.getvalue()


def _ffmpeg_frame(path: Path, max_side: int, seek: float | None = None) -> bytes | None:
    """用 ffmpeg 解码一帧（HEIC 静态图或视频）。"""
    if not _FFMPEG:
        return None
    command = [_FFMPEG, "-v", "error", "-nostdin"]
    if seek is not None and seek > 0:
        command += ["-ss", f"{seek:.3f}"]
    command += [
        "-i",
        os.fspath(path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={max_side}:{max_side}:force_original_aspect_ratio=decrease",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=40)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def _decode_heif(path: Path, max_side: int, quality: int) -> bytes | None:
    """用 libheif（heif-convert）或 ImageMagick 解码 HEIC/HEIF/AVIF。

    这两者都会正确处理 iPhone 的 HEVC grid 图片以及 EXIF 方向；
    ffmpeg 的 HEIC 支持不完整（Apple 照片可能只解码出辅助层/缩略图），
    因此只作为最后的兜底。
    """
    with _HEIF_SEMAPHORE:
        return _decode_heif_locked(path, max_side, quality)


def _decode_heif_locked(path: Path, max_side: int, quality: int) -> bytes | None:
    if _HEIF_CONVERT and _HAS_PILLOW:
        handle = tempfile.NamedTemporaryFile(prefix="photo-album-heif-", suffix=".jpg", delete=False)
        temp_path = Path(handle.name)
        handle.close()
        base_command = [
            _HEIF_CONVERT,
            "--quiet",
            "-q",
            "88",
            os.fspath(path),
            os.fspath(temp_path),
        ]
        try:
            proc = subprocess.run(
                base_command[:1] + ["--transparency-composition-mode", "white"] + base_command[1:],
                capture_output=True,
                timeout=90,
            )
            if proc.returncode != 0:
                # 旧版 heif-convert 不认识 --transparency-composition-mode。
                proc = subprocess.run(base_command, capture_output=True, timeout=90)
            if proc.returncode == 0 and temp_path.stat().st_size > 0:
                with Image.open(temp_path) as image:
                    image = image.convert("RGB")
                    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                    return _encode_jpeg(image, quality)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    if _IMAGEMAGICK:
        try:
            proc = subprocess.run(
                [
                    _IMAGEMAGICK,
                    os.fspath(path),
                    "-auto-orient",
                    "-resize",
                    f"{max_side}x{max_side}>",
                    "-quality",
                    str(max(70, quality)),
                    "jpg:-",
                ],
                capture_output=True,
                timeout=90,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except (OSError, subprocess.SubprocessError):
            pass

    raw = _ffmpeg_frame(Path(path), max_side)
    if raw is not None and _HAS_PILLOW:
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image = image.convert("RGB")
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                return _encode_jpeg(image, quality)
        except Exception:  # noqa: BLE001
            return None
    return raw


def decode_image(path: Path, max_side: int, orientation: int = 1, quality: int = 86) -> bytes | None:
    """把图片文件解码成不超过 ``max_side`` 的 JPEG 字节。

    HEIC/HEIF/AVIF 交给 libheif；其它格式优先 Pillow，失败后回退 ffmpeg。
    """
    path = Path(path)
    if path.suffix.lower() in _HEIF_SUFFIXES:
        data = _decode_heif(path, max_side, quality)
        if data is not None:
            return data
        # 极端情况下（没有 libheif/ImageMagick/ffmpeg）再让 Pillow 试一次。
        if not _HAS_PILLOW:
            return None
        try:
            with Image.open(os.fspath(path)) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                return _encode_jpeg(image, quality)
        except Exception:  # noqa: BLE001
            return None

    if _HAS_PILLOW:
        try:
            with Image.open(os.fspath(path)) as image:
                try:
                    image.draft("RGB", (max_side, max_side))
                except Exception:  # noqa: BLE001 - draft 仅对 JPEG 有效
                    pass
                image = ImageOps.exif_transpose(image)
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                return _encode_jpeg(image, quality)
        except Exception:  # noqa: BLE001 - 交给 ffmpeg 再试一次
            pass

    raw = _ffmpeg_frame(path, max_side)
    if raw is None or not _HAS_PILLOW:
        return raw
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = apply_orientation(image.convert("RGB"), orientation)
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            return _encode_jpeg(image, quality)
    except Exception:  # noqa: BLE001
        return None


def video_frame(path: Path, duration: float, max_side: int) -> bytes | None:
    """截取视频首帧；优先 1 秒处，避免黑场。"""
    seeks: list[float | None] = []
    if duration > 1.5:
        seeks.append(min(1.0, duration / 3))
    seeks.extend([0.0, None])
    for seek in seeks:
        raw = _ffmpeg_frame(Path(path), max_side, seek=seek)
        if raw:
            return raw
    return None


def render_media_preview(item: MediaItem, max_side: int) -> bytes | None:
    if item.is_video:
        return video_frame(item.path, item.duration, max_side)
    return decode_image(item.path, max_side, orientation=item.orientation, quality=90)
