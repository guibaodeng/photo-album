"""偏好设置对话框。"""

from __future__ import annotations

from typing import Callable

from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from .config import FEATURE_TIMELINE, Config, ROW_HEIGHT_MAX, ROW_HEIGHT_MIN
from .models import format_size
from .thumbnails import ThumbnailCache


class PreferencesDialog(Adw.PreferencesDialog):
    """媒体库 / 外观 / 播放三组设置。"""

    def __init__(
        self,
        config: Config,
        window: Gtk.Window,
        cache: ThumbnailCache,
        on_change: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._config = config
        self._window = window
        self._cache = cache
        self._on_change = on_change
        self._cache_update_source = 0
        self.set_title("偏好设置")

        self._build_library_page()
        self._build_appearance_page()
        self._build_playback_page()

    # ------------------------------------------------------------------ 页面
    def _build_library_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("媒体库")
        page.set_icon_name("folder-pictures-symbolic")

        self._locations_group = Adw.PreferencesGroup()
        self._locations_group.set_title("媒体库位置")
        self._locations_group.set_description("可以添加多个文件夹，扫描结果会合并展示")
        page.add(self._locations_group)
        self._location_rows: list[Adw.ActionRow] = []
        self._rebuild_locations()

        options = Adw.PreferencesGroup()
        options.set_title("扫描选项")
        self._recursive_row = Adw.SwitchRow()
        self._recursive_row.set_title("包含子文件夹")
        self._recursive_row.set_subtitle("关闭后只显示每个文件夹第一层的媒体")
        self._recursive_row.set_active(self._config.recursive)
        self._recursive_row.connect("notify::active", self._on_recursive)
        options.add(self._recursive_row)

        self._hidden_row = Adw.SwitchRow()
        self._hidden_row.set_title("显示隐藏文件")
        self._hidden_row.set_subtitle("以 . 开头的文件和文件夹")
        self._hidden_row.set_active(self._config.include_hidden)
        self._hidden_row.connect("notify::active", self._on_hidden)
        options.add(self._hidden_row)
        page.add(options)

        action_group = Adw.PreferencesGroup()
        action_group.set_title("维护")

        rescan_row = Adw.ActionRow()
        rescan_row.set_title("重新扫描")
        rescan_row.set_subtitle("重新读取目录与照片元数据")
        rescan_button = Gtk.Button(label="立即扫描")
        rescan_button.set_valign(Gtk.Align.CENTER)
        rescan_button.add_css_class("flat")
        rescan_button.connect("clicked", lambda *_: self._rescan())
        rescan_row.add_suffix(rescan_button)
        action_group.add(rescan_row)

        self._cache_row = Adw.ActionRow()
        self._cache_row.set_title("缩略图缓存")
        self._cache_row.set_subtitle("正在统计…")
        clear_button = Gtk.Button(label="清空")
        clear_button.set_valign(Gtk.Align.CENTER)
        clear_button.add_css_class("flat")
        clear_button.connect("clicked", lambda *_: self._clear_cache())
        self._cache_row.add_suffix(clear_button)
        action_group.add(self._cache_row)
        page.add(action_group)

        self.add(page)
        self._cache_update_source = GLib.idle_add(self._update_cache_row)
        self.connect("closed", self._on_closed)

    def _build_appearance_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("外观")
        page.set_icon_name("view-grid-symbolic")

        group = Adw.PreferencesGroup()
        group.set_title("照片墙")

        size_row = Adw.ActionRow()
        size_row.set_title("照片行高")
        size_row.set_subtitle("每一行照片的基准高度，卡片宽度按原始比例自动分配")
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, ROW_HEIGHT_MIN, ROW_HEIGHT_MAX, 10)
        scale.set_value(self._config.row_height)
        scale.set_size_request(220, -1)
        scale.set_draw_value(True)
        scale.set_digits(0)
        scale.set_valign(Gtk.Align.CENTER)
        scale.connect("value-changed", self._on_row_height_changed)
        size_row.add_suffix(scale)
        group.add(size_row)

        sort_row = Adw.SwitchRow()
        sort_row.set_title("最新优先")
        sort_row.set_subtitle("按拍摄时间从新到旧排列")
        sort_row.set_active(self._config.sort_descending)
        sort_row.connect("notify::active", self._on_sort_changed)
        group.add(sort_row)

        header_row = Adw.SwitchRow()
        header_row.set_title("显示月份标题")
        header_row.set_subtitle("关闭后隐藏年月标题，照片首尾相接成一整片")
        header_row.set_active(self._config.show_date_headers)
        header_row.connect("notify::active", self._on_date_headers_changed)
        group.add(header_row)

        if FEATURE_TIMELINE:
            timeline_row = Adw.SwitchRow()
            timeline_row.set_title("显示右侧时间线")
            timeline_row.set_subtitle("在窗口右侧显示年/月刻度，可点击跳转")
            timeline_row.set_active(self._config.show_timeline)
            timeline_row.connect("notify::active", self._on_timeline_changed)
            group.add(timeline_row)

        page.add(group)
        self.add(page)

    def _build_playback_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("播放")
        page.set_icon_name("media-playback-start-symbolic")

        group = Adw.PreferencesGroup()
        group.set_title("视频")
        autoplay = Adw.SwitchRow()
        autoplay.set_title("自动播放")
        autoplay.set_subtitle("打开视频时立即开始播放")
        autoplay.set_active(self._config.video_autoplay)
        autoplay.connect("notify::active", self._on_autoplay_changed)
        group.add(autoplay)
        page.add(group)
        self.add(page)

    # ------------------------------------------------------------------ 文件夹
    def _rebuild_locations(self) -> None:
        for row in self._location_rows:
            self._locations_group.remove(row)
        self._location_rows.clear()

        for value in self._config.roots:
            path = Path(value)
            row = Adw.ActionRow()
            row.set_title(path.name or value)
            row.set_subtitle(value)
            row.set_subtitle_selectable(True)
            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            remove.set_valign(Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text("从媒体库中移除")
            remove.set_sensitive(len(self._config.roots) > 1)
            remove.connect("clicked", lambda _button, target=value: self._remove_location(target))
            row.add_suffix(remove)
            self._locations_group.add(row)
            self._location_rows.append(row)

        add_row = Adw.ActionRow()
        add_row.set_title("添加文件夹…")
        add_row.set_activatable(True)
        add_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_row.connect("activated", lambda *_: self._add_folder())
        self._locations_group.add(add_row)
        self._location_rows.append(add_row)

    def _add_folder(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("添加照片文件夹")
        try:
            dialog.set_initial_folder(Gio.File.new_for_path(str(self._config.root_paths[0])))
        except Exception:  # noqa: BLE001
            pass

        def finished(file_dialog: Gtk.FileDialog, result) -> None:
            try:
                folder = file_dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is None:
                return
            path = folder.get_path()
            if not path or path in self._config.roots:
                return
            self._config.roots.append(path)
            self._persist("roots")
            self._rebuild_locations()

        dialog.select_folder(self._window, None, finished)

    def _remove_location(self, path: str) -> None:
        if len(self._config.roots) <= 1 or path not in self._config.roots:
            return
        self._config.roots = [value for value in self._config.roots if value != path]
        self._persist("roots")
        self._rebuild_locations()

    def _rescan(self) -> None:
        self._persist("rescan")

    def _clear_cache(self) -> None:
        freed = self._cache.clear()
        self._cache_row.set_subtitle(f"已清空（释放 {format_size(freed)}）")
        self._persist("cache")

    def _update_cache_row(self) -> bool:
        self._cache_update_source = 0
        total, count = self._cache.total_size()
        subtitle = self._cache_row.get_subtitle() or ""
        if subtitle.startswith("已清空"):
            return False
        self._cache_row.set_subtitle(f"{format_size(total)} · {count} 个文件")
        return False

    def _on_closed(self, *_args: object) -> None:
        if self._cache_update_source:
            GLib.source_remove(self._cache_update_source)
            self._cache_update_source = 0

    # ------------------------------------------------------------------ 回调
    def _on_recursive(self, row: Adw.SwitchRow, _param) -> None:
        value = row.get_active()
        if value == self._config.recursive:
            return
        self._config.recursive = value
        self._persist("root")

    def _on_hidden(self, row: Adw.SwitchRow, _param) -> None:
        value = row.get_active()
        if value == self._config.include_hidden:
            return
        self._config.include_hidden = value
        self._persist("root")

    def _on_row_height_changed(self, scale: Gtk.Scale) -> None:
        value = int(round(scale.get_value()))
        if value == self._config.row_height:
            return
        self._config.row_height = value
        self._persist("row-height")

    def _on_sort_changed(self, row: Adw.SwitchRow, _param) -> None:
        value = row.get_active()
        if value == self._config.sort_descending:
            return
        self._config.sort_descending = value
        self._persist("sort")

    def _on_date_headers_changed(self, row: Adw.SwitchRow, _param) -> None:
        value = row.get_active()
        if value == self._config.show_date_headers:
            return
        self._config.show_date_headers = value
        self._persist("date-headers")

    def _on_timeline_changed(self, row: Adw.SwitchRow, _param) -> None:
        value = row.get_active()
        if value == self._config.show_timeline:
            return
        self._config.show_timeline = value
        self._persist("timeline")

    def _on_autoplay_changed(self, row: Adw.SwitchRow, _param) -> None:
        value = row.get_active()
        if value == self._config.video_autoplay:
            return
        self._config.video_autoplay = value
        self._persist("autoplay")

    def _persist(self, reason: str) -> None:
        self._config.save()
        self._on_change(reason)
