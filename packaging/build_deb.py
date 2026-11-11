#!/usr/bin/env python3
"""构建可安装的 .deb 包（无需 root，产物在 dist/）。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "deb"
APP = "photo-album"


def version() -> str:
    text = (ROOT / "photo_album" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("无法从 config.py 读取版本号")
    return match.group(1)


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "dist", "build"),
    )


def make_launcher(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
exec python3 /usr/lib/photo-album/run.py "$@"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def installed_size(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        if Path(dirpath) == root / "DEBIAN":
            continue
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
    return max(1, (total + 1023) // 1024)


def main() -> int:
    ver = version()
    stage = BUILD / f"photo-album_{ver}_all"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    DIST.mkdir(exist_ok=True)

    # 程序文件
    lib = stage / "usr" / "lib" / "photo-album"
    lib.mkdir(parents=True)
    copy_tree(ROOT / "photo_album", lib / "photo_album")
    shutil.copy2(ROOT / "run.py", lib / "run.py")
    shutil.copy2(ROOT / "README.md", lib / "README.md")
    shutil.copy2(ROOT / "LICENSE", lib / "LICENSE")

    bindir = stage / "usr" / "bin"
    bindir.mkdir(parents=True)
    make_launcher(bindir / APP)

    # 桌面入口
    apps = stage / "usr" / "share" / "applications"
    apps.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging" / "photo-album.desktop", apps / f"{APP}.desktop")

    # 图标
    icons_src = ROOT / "photo_album" / "assets" / "hicolor"
    icons_dst = stage / "usr" / "share" / "icons" / "hicolor"
    for icon in icons_src.glob("*/apps/photo-album.png"):
        size = icon.parent.parent.name
        target = icons_dst / size / "apps"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icon, target / icon.name)

    # 文档
    doc = stage / "usr" / "share" / "doc" / APP
    doc.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", doc / "copyright")
    shutil.copy2(ROOT / "README.md", doc / "README.md")

    # Debian 控制信息
    debian = stage / "DEBIAN"
    debian.mkdir()
    control = f"""Package: {APP}
Version: {ver}
Section: graphics
Priority: optional
Architecture: all
Installed-Size: {installed_size(stage)}
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0 (>= 4.10), gir1.2-adw-1 (>= 1.5), python3-pil
Recommends: gir1.2-gexiv2-0.10, libheif-examples, ffmpeg, imagemagick, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-libav, gstreamer1.0-gtk4, gvfs, xdg-utils, adwaita-icon-theme
Maintainer: dengguibao <guibaodeng@icloud.com>
Description: local photo and video browser for Linux
 A GTK4/libadwaita photo browser with a justified photo wall,
 multiple library folders, an overlay timeline, HEIC support,
 video playback, EXIF display/editing and system trash integration.
"""
    (debian / "control").write_text(control, encoding="utf-8")

    postinst = """#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
"""
    postrm = """#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
"""
    for name, content in (("postinst", postinst), ("postrm", postrm)):
        path = debian / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    # 源文件来自工作区时权限可能过严，统一成普通安装包权限。
    for dirpath, dirnames, filenames in os.walk(stage):
        for dirname in dirnames:
            (Path(dirpath) / dirname).chmod(0o755)
        for filename in filenames:
            path = Path(dirpath) / filename
            relative = path.relative_to(stage)
            if relative in (
                Path("usr/bin/photo-album"),
                Path("DEBIAN/postinst"),
                Path("DEBIAN/postrm"),
            ):
                path.chmod(0o755)
            else:
                path.chmod(0o644)

    output = DIST / f"{APP}_{ver}_all.deb"
    subprocess.run(
        [
            "dpkg-deb",
            "--build",
            "--root-owner-group",
            str(stage),
            str(output),
        ],
        check=True,
    )
    # 方便直接拿到安装到系统时使用的桌面文件
    shutil.copy2(ROOT / "packaging" / "photo-album.desktop", DIST / f"{APP}.desktop")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
