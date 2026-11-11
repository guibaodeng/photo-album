#!/usr/bin/env python3
"""检查运行必需/可选依赖，供不同 Linux 发行版排查环境。"""

from __future__ import annotations

import shutil
import sys
from importlib import import_module


def result(ok: bool, name: str, detail: str = "") -> bool:
    print(("  OK   " if ok else "  MISS ") + name + (f"  {detail}" if detail else ""))
    return ok


def version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for item in value.split("."):
        digits = "".join(ch for ch in item if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check_gi(namespace: str, version: str, minimum: tuple[int, int], required: bool) -> bool:
    try:
        import gi

        gi.require_version(namespace, version)
        obj = import_module(f"gi.repository.{namespace}")
        major_getter = getattr(obj, "get_major_version", None)
        minor_getter = getattr(obj, "get_minor_version", None)
        if major_getter is None or minor_getter is None:
            return result(True, f"{namespace} (已安装)", "该命名空间不提供版本号")
        major = int(major_getter())
        minor = int(minor_getter())
        ok = (major, minor) >= minimum
        result(
            ok,
            f"{namespace} {major}.{minor}",
            f"需要 >= {minimum[0]}.{minimum[1]}",
        )
        return ok
    except Exception as error:  # noqa: BLE001
        return result(False, namespace, f"不可用：{error}")


def main() -> int:
    required_ok = True
    print("必需依赖：")
    required_ok &= sys.version_info >= (3, 10)
    result(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", "需要 >= 3.10")

    try:
        import gi

        pygobject = version_tuple(getattr(gi, "__version__", "0"))
        required_ok &= result(pygobject >= (3, 42), f"PyGObject {gi.__version__}", "需要 >= 3.42")
    except Exception as error:  # noqa: BLE001
        result(False, "PyGObject", f"不可用：{error}")
        required_ok = False

    required_ok &= check_gi("Gtk", "4.0", (4, 10), True)
    required_ok &= check_gi("Adw", "1", (1, 5), True)

    try:
        import PIL

        pillow = version_tuple(PIL.__version__)
        required_ok &= result(pillow >= (9, 1), f"Pillow {PIL.__version__}", "需要 >= 9.1")
    except Exception as error:  # noqa: BLE001
        result(False, "Pillow", f"不可用：{error}")
        required_ok = False

    print("可选功能：")
    check_gi("GExiv2", "0.10", (0, 12), False)
    for executable, label in (
        ("heif-convert", "HEIC 解码 (libheif)"),
        ("heif-info", "HEIC 尺寸 (libheif)"),
        ("ffmpeg", "视频/图片兜底解码"),
        ("ffprobe", "视频元数据"),
        ("magick", "ImageMagick 兜底"),
    ):
        path = shutil.which(executable)
        result(bool(path), f"{executable} ({label})", path or "未安装")

    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        factory = Gst.ElementFactory.find("gtk4paintablesink")
        result(bool(factory), "GStreamer GTK4 视频输出", "gtk4paintablesink")
    except Exception as error:  # noqa: BLE001
        result(False, "GStreamer GTK4 视频输出", str(error))

    print()
    if required_ok:
        print("必需依赖满足，可以从源码运行 ./photo-album。")
        return 0
    print("必需依赖不满足，请先安装/升级 GTK、libadwaita、PyGObject、Pillow。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
