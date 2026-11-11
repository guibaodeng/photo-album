"""应用入口：加载样式、创建主窗口、注册全局动作。"""

from __future__ import annotations

import sys
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import theme
from .config import APP_ID, APP_NAME, APP_VERSION, Config
from .thumbnails import ThumbnailCache, ThumbnailService
from .window import MainWindow

_ASSETS = Path(__file__).resolve().parent / "assets"
_STYLE = Path(__file__).resolve().parent / "style.css"


class PhotoAlbumApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self._config: Config | None = None
        self._thumbnails: ThumbnailService | None = None
        self._window: MainWindow | None = None

    # ------------------------------------------------------------------ 生命周期
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None:
            icon_theme = Gtk.IconTheme.get_for_display(display)
            if _ASSETS.is_dir():
                icon_theme.add_search_path(str(_ASSETS))
            Gtk.Window.set_default_icon_name("photo-album")
        self._load_css()
        theme.preload()
        self._install_app_actions()

    def do_activate(self) -> None:
        self._ensure_window()

    def do_open(self, files: list[Gio.File], _n_files: int, _hint: str) -> None:
        roots: list[str] = []
        for gfile in files:
            path = gfile.get_path()
            if path and Path(path).is_dir():
                roots.append(path)
        self._ensure_window(roots or None)

    def do_shutdown(self) -> None:
        if self._thumbnails is not None:
            self._thumbnails.shutdown()
            self._thumbnails = None
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------ 内部
    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None or not _STYLE.is_file():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(_STYLE))
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)

    def _install_app_actions(self) -> None:
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("win.rescan", ["F5"])
        self.set_accels_for_action("win.choose-folder", ["<Primary>o"])
        self.set_accels_for_action("win.preferences", ["<Primary>comma"])
        self.set_accels_for_action("win.search", ["<Primary>f"])

    def _on_about(self, *_args: object) -> None:
        dialog = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="photo-album",
            version=APP_VERSION,
            developer_name="dengguibao",
            comments="简洁的本地照片与视频浏览工具：多目录媒体库、等高行照片墙、"
            "右侧时间线、EXIF 信息查看、视频播放与右键删除。",
            license_type=Gtk.License.MIT_X11,
        )
        dialog.present(self.get_active_window())

    def _ensure_window(self, override_roots: list[str] | None = None) -> None:
        if self._config is None:
            self._config = Config.load()

        if self._thumbnails is None:
            self._thumbnails = ThumbnailService(ThumbnailCache(), workers=4)

        if self._window is None:
            if override_roots:
                self._config.roots = list(dict.fromkeys(override_roots))
                self._config.save()
            self._window = MainWindow(self, self._config, self._thumbnails)
            self._window.connect("close-request", self._on_window_close_request)
            self._window.present()
        else:
            if override_roots:
                self._window.set_roots(override_roots, replace=True)
            self._window.present()

    def _on_window_close_request(self, *_args: object) -> bool:
        self._window = None
        return False


def main(argv: list[str] | None = None) -> int:
    app = PhotoAlbumApplication()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
