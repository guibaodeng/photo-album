"""文件操作：回收站、永久删除、文件管理器定位、复制文件。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from gi.repository import Gdk, Gio, GLib

#: 回调参数为 None 表示成功，否则是错误信息。
ResultCallback = Callable[[str | None], None]


def move_to_trash(path: Path, callback: ResultCallback) -> None:
    gfile = Gio.File.new_for_path(os.fspath(path))

    def finished(source: Gio.File, result) -> None:
        try:
            source.trash_finish(result)
        except GLib.Error as error:
            callback(error.message)
            return
        callback(None)

    try:
        gfile.trash_async(GLib.PRIORITY_DEFAULT, None, finished)
    except GLib.Error as error:
        callback(error.message)


def delete_permanently(path: Path, callback: ResultCallback) -> None:
    gfile = Gio.File.new_for_path(os.fspath(path))

    def finished(source: Gio.File, result) -> None:
        try:
            source.delete_finish(result)
        except GLib.Error as error:
            callback(error.message)
            return
        callback(None)

    try:
        gfile.delete_async(GLib.PRIORITY_DEFAULT, None, finished)
    except GLib.Error as error:
        callback(error.message)


def copy_files(paths: list[Path]) -> bool:
    """把真实文件放入剪贴板，可直接在文件管理器中粘贴。"""
    display = Gdk.Display.get_default()
    if display is None or not paths:
        return False
    try:
        files = [Gio.File.new_for_path(os.fspath(path)) for path in paths]
        uris = [gfile.get_uri() for gfile in files]
        providers = []
        try:
            uri_data = ("\r\n".join(uris) + "\r\n").encode("utf-8")
            providers.append(
                Gdk.ContentProvider.new_for_bytes(
                    "text/uri-list", GLib.Bytes.new(uri_data)
                )
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            gnome_data = ("copy\n" + "\n".join(uris)).encode("utf-8")
            providers.append(
                Gdk.ContentProvider.new_for_bytes(
                    "x-special/gnome-copied-files", GLib.Bytes.new(gnome_data)
                )
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            file_list = Gdk.FileList.new_from_list(files)
            providers.append(Gdk.ContentProvider.new_for_value(file_list))
        except Exception:  # noqa: BLE001
            pass
        if not providers:
            return False
        provider = (
            providers[0]
            if len(providers) == 1
            else Gdk.ContentProvider.new_union(providers)
        )
        display.get_clipboard().set_content(provider)
        return True
    except Exception:  # noqa: BLE001
        return False


def show_in_file_manager(path: Path) -> None:
    uri = path.resolve().as_uri()
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.call_sync(
            "org.freedesktop.FileManager1",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1",
            "ShowItems",
            GLib.Variant("(ass)", ([uri], "")),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        GLib.spawn_async(["xdg-open", os.fspath(path.parent)])
    except Exception:  # noqa: BLE001
        pass
