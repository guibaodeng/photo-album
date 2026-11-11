"""主题颜色与程序内生成的小图标纹理。"""

from __future__ import annotations

import io
from functools import lru_cache

from gi.repository import Adw, Gdk, GLib

try:
    from PIL import Image, ImageDraw  # type: ignore

    _HAS_PILLOW = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    _HAS_PILLOW = False

_ACCENT_FALLBACK = (0.207, 0.517, 0.894, 1.0)  # GNOME 蓝色


def is_dark() -> bool:
    try:
        return bool(Adw.StyleManager.get_default().get_dark())
    except Exception:  # noqa: BLE001
        return True


def accent_color(widget) -> Gdk.RGBA:
    """取当前主题的强调色，取不到时退回到 GNOME 蓝。"""
    try:
        found, rgba = widget.get_style_context().lookup_color("accent_color")
        if found:
            return rgba
    except Exception:  # noqa: BLE001
        pass
    return Gdk.RGBA(*_ACCENT_FALLBACK)


def foreground_color(widget) -> Gdk.RGBA:
    try:
        return widget.get_color()
    except Exception:  # noqa: BLE001
        return Gdk.RGBA(1, 1, 1, 1)


def with_alpha(color: Gdk.RGBA, alpha: float) -> Gdk.RGBA:
    return Gdk.RGBA(color.red, color.green, color.blue, max(0.0, min(1.0, alpha)))


def _texture_from_png(data: bytes) -> Gdk.Texture:
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))


def _save_png(image: "Image.Image") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@lru_cache(maxsize=8)
def placeholder_texture(dark: bool, rgba: tuple[float, float, float, float]) -> Gdk.Texture:
    """占位缩略图：一张暗色渐变 + 极简「照片」图标。"""
    if not _HAS_PILLOW:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(b""))
    size = 128
    top = (44, 44, 48) if dark else (243, 243, 245)
    bottom = (30, 30, 33) if dark else (231, 231, 234)
    image = Image.new("RGB", (size, size), top)
    draw = ImageDraw.Draw(image)
    for y in range(size):
        factor = y / (size - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * factor) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color)
    # 图标
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(icon)
    color = tuple(int(round(c * 255)) for c in rgba[:3]) + (int(round(rgba[3] * 255)),)
    pen.rounded_rectangle([34, 42, 94, 88], radius=10, outline=color, width=4)
    pen.ellipse([74, 52, 84, 62], fill=color)
    pen.polygon([(40, 80), (56, 60), (66, 71), (74, 64), (88, 80)], fill=color)
    image = Image.alpha_composite(image.convert("RGBA"), icon)
    return _texture_from_png(_save_png(image))


@lru_cache(maxsize=2)
def play_texture() -> Gdk.Texture:
    """视频卡片中央的播放三角形。"""
    if not _HAS_PILLOW:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(b""))
    size = 96
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.polygon([(36, 24), (72, 48), (36, 72)], fill=(255, 255, 255, 235))
    return _texture_from_png(_save_png(image))


@lru_cache(maxsize=4)
def broken_texture(dark: bool) -> Gdk.Texture:
    """损坏/无法生成缩略图时的图标。"""
    if not _HAS_PILLOW:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(b""))
    size = 96
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = (255, 255, 255, 110) if dark else (0, 0, 0, 110)
    draw.rounded_rectangle([16, 24, 80, 72], radius=8, outline=color, width=4)
    draw.line([(24, 30), (72, 66)], fill=color, width=4)
    draw.line([(72, 30), (24, 66)], fill=color, width=4)
    return _texture_from_png(_save_png(image))


def preload() -> None:
    """在界面出现前把常用纹理准备好，避免首帧卡顿。"""
    dark = is_dark()
    placeholder_texture(dark, (1.0, 1.0, 1.0, 0.30) if dark else (0.1, 0.1, 0.12, 0.35))
    play_texture()
    broken_texture(dark)
