"""主窗口：顶部工具栏、照片墙、右侧时间线、搜索与筛选。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .config import APP_NAME, Config
from .exif_editor import ExifEditor
from .file_actions import copy_files, delete_permanently, move_to_trash, show_in_file_manager
from .models import MediaItem
from .preferences import PreferencesDialog
from .scanner import LibraryScanner, ScanCancelled, ScanResult, read_media_item
from .thumbnails import ThumbnailService
from .timeline import Timeline
from .viewer import MediaViewer
from .wall import Gallery

_FILTER_ALL = 0
_FILTER_PHOTOS = 1
_FILTER_VIDEOS = 2

_FILTER_OPTIONS = {
    _FILTER_ALL: ("nautilus-search-filters-symbolic", "全部"),
    _FILTER_PHOTOS: ("filter-photos-symbolic", "照片"),
    _FILTER_VIDEOS: ("filter-videos-symbolic", "视频"),
}


class _PageHost(Adw.Bin):
    """简易页面容器。

    没有内容时 Gtk.Stack 会给页面分配 0 宽度，导致照片墙拿不到视口宽度；
    这里用 Adw.Bin 只承载当前页面，保证子页面始终获得完整分配。
    """

    def __init__(self) -> None:
        super().__init__()
        self._pages: dict[str, Gtk.Widget] = {}
        self._current = ""

    def add_named(self, widget: Gtk.Widget, name: str) -> None:
        self._pages[name] = widget

    def set_visible_child_name(self, name: str) -> None:
        widget = self._pages.get(name)
        if widget is None or name == self._current:
            return
        self.set_child(widget)
        self._current = name

    def get_visible_child_name(self) -> str:
        return self._current

    def get_visible_child(self) -> Gtk.Widget | None:
        return self._pages.get(self._current)

    def set_transition_type(self, *_args: object) -> None:
        pass

    def set_transition_duration(self, *_args: object) -> None:
        pass


class MainWindow(Adw.ApplicationWindow):
    """应用主窗口。"""

    def __init__(self, application: Adw.Application, config: Config, thumbnails: ThumbnailService) -> None:
        super().__init__(application=application)
        self._config = config
        self._thumbnails = thumbnails
        self._cache = thumbnails.cache
        self._all_items: list[MediaItem] = []
        self._filtered_items: list[MediaItem] = []
        self._scan_generation = 0
        self._scan_cancel: threading.Event | None = None
        self._scan_thread: threading.Thread | None = None
        self._scanning = False
        self._scan_first_batch = True
        self._scan_total = 0
        self._scan_refresh_source = 0
        self._scan_ui_started = False
        self._viewer: MediaViewer | None = None
        self._preferences: PreferencesDialog | None = None
        self._exif_editor: ExifEditor | None = None
        self._filter_source = 0
        self._filter_mode = _FILTER_ALL
        self._filter_checks: dict[int, Gtk.Image] = {}
        self._refresh_source = 0
        self._scroll_animation_source = 0
        self._closed = False
        self._toasts: Adw.ToastOverlay | None = None
        self._context_popover: Gtk.Popover | None = None
        self._context_item: MediaItem | None = None
        self._delete_dialog: Adw.AlertDialog | None = None
        self._permanent_dialog: Adw.AlertDialog | None = None
        self._pending_scroll: tuple[tuple[int, int] | None, float, float, int] | None = None
        self._delete_scroll_token = 0
        self._delete_scroll_before: float | None = None
        self._dialog_scroll_handler = 0
        self._holding_dialog_scroll = False

        self.set_title(APP_NAME)
        self.set_default_size(config.window_width, config.window_height)
        if config.window_maximized:
            GLib.idle_add(self._maximize_idle)

        self._build_ui()
        self._install_actions()
        self.connect("close-request", self._on_close_request)
        GLib.idle_add(self._start_scan)

    # ------------------------------------------------------------------ 界面
    def _maximize_idle(self) -> bool:
        self.maximize()
        return False

    def _build_ui(self) -> None:
        header = Adw.HeaderBar()
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_tooltip_text("主菜单")
        menu_button.set_menu_model(self._build_menu())
        header.pack_start(menu_button)
        self._menu_button = menu_button

        self._window_title = Adw.WindowTitle(title=APP_NAME, subtitle=self._config.library_label)
        self._window_title.set_tooltip_text(f"媒体库：\n{self._config.library_tooltip()}")
        header.set_title_widget(self._window_title)

        self._build_filter_button(header)

        self._search_button = Gtk.ToggleButton()
        self._search_button.set_icon_name("system-search-symbolic")
        self._search_button.set_tooltip_text("搜索 (Ctrl+F)")
        header.pack_end(self._search_button)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("搜索文件名或文件夹…")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", lambda *_: self._schedule_filter())
        self._search_bar = Gtk.SearchBar()
        self._search_bar.set_child(self._search_entry)
        self._search_bar.connect_entry(self._search_entry)
        self._search_bar.set_key_capture_widget(self)
        self._search_button.connect("toggled", self._on_search_toggled)
        self._search_bar.connect("notify::search-mode-enabled", self._on_search_mode_changed)

        # 内容区
        self._page_stack = _PageHost()
        self._page_stack.set_vexpand(True)
        self._page_stack.set_hexpand(True)

        self._loading_page, self._scan_progress = self._build_loading_page()
        self._empty_page, self._empty_button = self._build_empty_page()
        self._error_page, self._error_description, self._error_retry = self._build_error_page()
        gallery_page = self._build_gallery_page()

        self._page_stack.add_named(self._loading_page, "loading")
        self._page_stack.add_named(self._empty_page, "empty")
        self._page_stack.add_named(self._error_page, "error")
        self._page_stack.add_named(gallery_page, "gallery")

        overlay = Gtk.Overlay()
        overlay.set_child(self._page_stack)
        self._scroll_top = Gtk.Button.new_from_icon_name("go-up-symbolic")
        self._scroll_top.add_css_class("osd")
        self._scroll_top.add_css_class("circular")
        self._scroll_top.add_css_class("scroll-top-button")
        self._scroll_top.set_tooltip_text("回到顶部")
        self._scroll_top.set_halign(Gtk.Align.END)
        self._scroll_top.set_valign(Gtk.Align.END)
        self._scroll_top.set_margin_end(102)  # 由 _set_timeline_visible 调整
        self._scroll_top.set_margin_bottom(26)
        self._scroll_top.set_visible(False)
        self._scroll_top.connect("clicked", lambda *_: self._animate_scroll(0.0))
        overlay.add_overlay(self._scroll_top)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self._search_bar)
        toolbar.set_content(overlay)

        self._toasts = Adw.ToastOverlay()
        self._toasts.set_child(toolbar)
        self.set_content(self._toasts)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)
        self._page_stack.set_visible_child_name("gallery")
        self._set_timeline_visible(self._config.show_timeline)

    def _build_gallery_page(self) -> Gtk.Widget:
        self._scroller = Gtk.ScrolledWindow()
        # 滚动条通过 CSS 隐藏（不能把 vertical policy 设为 NEVER，
        # 否则 ScrolledWindow 会按内容高度撑大窗口），只保留滚轮/触控板滚动。
        # 横向也允许收缩（滚动条仍由 CSS 隐藏），否则切换时间线时
        # ScrolledWindow 会按照片墙最小宽度把页面撑出窗口。
        self._scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_hexpand(True)
        self._scroller.set_vexpand(True)
        # 不要按内容自然宽度撑大页面，否则隐藏时间线再打开时页面会宽出窗口。
        self._scroller.set_propagate_natural_width(False)
        self._scroller.set_size_request(1, -1)
        self._scroller.set_overlay_scrolling(True)
        self._scroller.add_css_class("gallery-scroll")

        self._gallery = Gallery(
            self._thumbnails,
            self._open_viewer,
            self._show_tile_menu,
            self._request_delete,
        )
        self._gallery.set_headers_visible(self._config.show_date_headers)
        self._gallery.on_layout_changed = self._on_gallery_layout
        self._scroller.set_child(self._gallery)

        self._timeline = Timeline()
        self._timeline.set_on_jump(self._on_timeline_jump)
        self._timeline.set_halign(Gtk.Align.END)
        self._timeline.set_valign(Gtk.Align.FILL)
        self._set_timeline_visible(self._config.show_timeline)

        # 时间轴浮在页面右侧的透明区域里；照片墙通过右边距让出同一宽度，
        # 所以相册仍在时间轴左侧，但切换显示时不会改变页面/窗口宽度。
        page = Gtk.Overlay()
        self._gallery_page = page
        page.set_hexpand(True)
        page.set_vexpand(True)
        page.set_halign(Gtk.Align.FILL)
        page.set_child(self._scroller)
        page.add_overlay(self._timeline)

        self._scan_pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._scan_pill.add_css_class("loading-pill")
        self._scan_pill.set_halign(Gtk.Align.CENTER)
        self._scan_pill.set_valign(Gtk.Align.START)
        self._scan_pill.set_margin_top(18)
        spinner = Adw.Spinner()
        spinner.set_size_request(16, 16)
        self._scan_pill.append(spinner)
        self._scan_pill_label = Gtk.Label(label="正在扫描…")
        self._scan_pill.append(self._scan_pill_label)
        self._scan_pill.set_visible(False)
        page.add_overlay(self._scan_pill)

        timeline_scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        timeline_scroll.connect("scroll", self._on_timeline_scroll)
        self._timeline.add_controller(timeline_scroll)

        user_scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        user_scroll.connect("scroll", self._on_user_scroll)
        self._scroller.add_controller(user_scroll)

        self._vadjustment = self._scroller.get_vadjustment()
        self._vadjustment.connect("value-changed", self._on_scrolled)
        self._vadjustment.connect("changed", self._on_scrolled)
        return page

    def _build_filter_button(self, header: Adw.HeaderBar) -> None:
        """右上角只显示一个筛选图标，点击后弹出全部/照片/视频选项。"""
        button = Gtk.MenuButton()
        button.set_valign(Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.set_tooltip_text("筛选媒体类型")
        self._filter_button = button

        popover = Gtk.Popover()
        popover.add_css_class("filter-popover")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        for mode in (_FILTER_ALL, _FILTER_PHOTOS, _FILTER_VIDEOS):
            icon_name, label = _FILTER_OPTIONS[mode]
            option = Gtk.Button()
            option.add_css_class("flat")
            option.set_halign(Gtk.Align.FILL)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.append(Gtk.Image.new_from_icon_name(icon_name))
            text = Gtk.Label(label=label)
            text.set_xalign(0.0)
            text.set_hexpand(True)
            row.append(text)
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_visible(mode == self._filter_mode)
            self._filter_checks[mode] = check
            row.append(check)
            option.set_child(row)
            option.connect("clicked", self._on_filter_option_clicked, mode)
            box.append(option)
        popover.set_child(box)
        button.set_popover(popover)
        self._filter_popover = popover
        header.pack_end(button)
        self._update_filter_button()

    def _on_filter_option_clicked(self, _button: Gtk.Button, mode: int) -> None:
        self._filter_popover.popdown()
        self._set_filter(mode)

    def _set_filter(self, mode: int, apply: bool = True) -> None:
        if mode not in _FILTER_OPTIONS:
            mode = _FILTER_ALL
        changed = mode != self._filter_mode
        self._filter_mode = mode
        self._update_filter_button()
        if apply and changed:
            self._apply_filters(reset_scroll=True)

    def _update_filter_button(self) -> None:
        icon_name, label = _FILTER_OPTIONS[self._filter_mode]
        self._filter_button.set_icon_name(icon_name)
        self._filter_button.set_tooltip_text(f"筛选：{label}（点击切换）")
        for mode, check in self._filter_checks.items():
            check.set_visible(mode == self._filter_mode)

    def _build_loading_page(self) -> tuple[Adw.StatusPage, Gtk.ProgressBar]:
        page = Adw.StatusPage()
        page.set_title("正在整理相册")
        page.set_description("正在扫描目录并读取照片信息…")
        spinner = Adw.Spinner()
        spinner.set_size_request(44, 44)
        progress = Gtk.ProgressBar()
        progress.set_size_request(260, -1)
        progress.add_css_class("scan-progress")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_halign(Gtk.Align.CENTER)
        box.append(spinner)
        box.append(progress)
        page.set_child(box)
        return page, progress

    def _build_empty_page(self) -> tuple[Adw.StatusPage, Gtk.Button]:
        page = Adw.StatusPage()
        page.set_icon_name("folder-pictures-symbolic")
        page.set_title("这里还没有照片或视频")
        page.set_description("尝试选择其他文件夹，或在偏好设置中打开「包含子文件夹」")
        button = Gtk.Button(label="添加文件夹")
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self._on_empty_button())
        page.set_child(button)
        self._empty_is_filter = False
        return page, button

    def _on_empty_button(self) -> None:
        if self._empty_is_filter:
            self._search_entry.set_text("")
            self._set_filter(_FILTER_ALL, apply=False)
            self._apply_filters(reset_scroll=True)
        else:
            self._choose_folder()

    def _build_error_page(self) -> tuple[Adw.StatusPage, Gtk.Label, Gtk.Button]:
        page = Adw.StatusPage()
        page.set_icon_name("dialog-warning-symbolic")
        page.set_title("无法打开文件夹")
        description = Gtk.Label()
        description.set_wrap(True)
        description.set_justify(Gtk.Justification.CENTER)
        description.add_css_class("dim-label")
        retry = Gtk.Button(label="重试")
        retry.add_css_class("pill")
        retry.add_css_class("suggested-action")
        choose = Gtk.Button(label="添加文件夹")
        choose.add_css_class("pill")
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.append(retry)
        buttons.append(choose)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.append(description)
        content.append(buttons)
        page.set_child(content)
        retry.connect("clicked", lambda *_: self._start_scan())
        choose.connect("clicked", lambda *_: self._choose_folder())
        return page, description, retry

    # ------------------------------------------------------------------ 菜单
    def _build_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        first = Gio.Menu()
        first.append("添加文件夹…", "win.choose-folder")
        first.append("重新扫描", "win.rescan")
        menu.append_section(None, first)

        view = Gio.Menu()
        view.append("最新优先", "win.sort-descending")
        menu.append_section(None, view)

        last = Gio.Menu()
        last.append("偏好设置", "win.preferences")
        last.append("关于相册", "app.about")
        last.append("退出", "app.quit")
        menu.append_section(None, last)
        return menu

    def _install_actions(self) -> None:
        for name, callback in (
            ("choose-folder", lambda *_: self._choose_folder()),
            ("rescan", lambda *_: self._start_scan()),
            ("preferences", lambda *_: self._open_preferences()),
            ("search", lambda *_: self._toggle_search()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        sort_action = Gio.SimpleAction.new_stateful(
            "sort-descending", None, GLib.Variant.new_boolean(self._config.sort_descending)
        )
        sort_action.connect("activate", self._on_sort_action)
        self.add_action(sort_action)

    def _on_sort_action(self, action: Gio.SimpleAction, _parameter: GLib.Variant | None) -> None:
        state = not bool(action.get_state().get_boolean())
        action.set_state(GLib.Variant.new_boolean(state))
        self._config.sort_descending = state
        self._config.save()
        self._apply_filters(reset_scroll=True)

    def _toggle_search(self) -> None:
        self._search_button.set_active(not self._search_button.get_active())
        if self._search_button.get_active():
            self._search_entry.grab_focus()

    def _on_search_toggled(self, button: Gtk.ToggleButton) -> None:
        active = button.get_active()
        self._search_bar.set_search_mode(active)
        if active:
            self._search_entry.grab_focus()
        else:
            if self._search_entry.get_text():
                self._search_entry.set_text("")
            self._schedule_filter(immediate=True)

    def _on_search_mode_changed(self, search_bar: Gtk.SearchBar, _param: object) -> None:
        if not search_bar.get_search_mode() and self._search_button.get_active():
            self._search_button.set_active(False)

    # ------------------------------------------------------------------ 扫描
    def _start_scan(self) -> None:
        self._delete_scroll_token += 1
        self._close_context_popover()
        roots = self._config.root_paths
        valid_roots = [root for root in roots if root.is_dir()]
        if not valid_roots:
            listing = "\n".join(str(root) for root in roots)
            self._show_error(f"以下媒体库位置不存在：\n{listing}", retry=True)
            self._window_title.set_subtitle("媒体库位置不可用")
            self._window_title.set_tooltip_text(f"媒体库：\n{self._config.library_tooltip()}")
            return

        if self._scan_cancel is not None:
            self._scan_cancel.set()
        self._scan_cancel = threading.Event()
        self._scan_generation += 1
        generation = self._scan_generation
        if self._scan_refresh_source:
            GLib.source_remove(self._scan_refresh_source)
            self._scan_refresh_source = 0
        self._scanning = True
        self._scan_first_batch = True
        self._scan_total = 0
        self._window_title.set_title(APP_NAME)
        self._window_title.set_subtitle("正在扫描…")
        self._window_title.set_tooltip_text(f"媒体库：\n{self._config.library_tooltip()}")
        self._pending_scroll = None
        self._scan_progress.set_fraction(0.0)
        # 直接进入相册；扫描结果按批次填入，不再阻塞在加载页。
        self._page_stack.set_visible_child_name("gallery")
        self._set_timeline_visible(self._config.show_timeline)
        self._set_scan_pill(True, "正在扫描目录…")

        config = self._config
        cancel = self._scan_cancel
        thread = threading.Thread(
            target=self._scan_worker,
            args=(
                generation,
                valid_roots,
                config.recursive,
                config.include_hidden,
                list(config.excluded_dirs),
                cancel,
            ),
            name="library-scan",
            daemon=True,
        )
        self._scan_thread = thread
        thread.start()

    def _scan_worker(
        self,
        generation: int,
        roots: list[Path],
        recursive: bool,
        include_hidden: bool,
        excluded: list[str],
        cancel: threading.Event,
    ) -> None:
        scanner = LibraryScanner(
            roots,
            recursive=recursive,
            include_hidden=include_hidden,
            excluded_dirs=excluded,
        )
        try:
            result = scanner.scan(
                cancel,
                progress=lambda done, total, phase: GLib.idle_add(
                    self._on_scan_progress, generation, done, total, phase
                ),
                batch=lambda items: GLib.idle_add(
                    self._on_scan_batch, generation, items
                ),
            )
        except ScanCancelled:
            return
        except Exception as error:  # noqa: BLE001
            GLib.idle_add(self._on_scan_failed, generation, str(error))
            return
        GLib.idle_add(self._on_scan_finished, generation, result)

    def _set_scan_pill(self, visible: bool, text: str | None = None) -> None:
        if not hasattr(self, "_scan_pill"):
            return
        if text is not None:
            self._scan_pill_label.set_text(text)
        self._scan_pill.set_visible(visible)

    def _update_scan_status(self, phase: str = "probe") -> None:
        if not self._scanning:
            return
        if phase == "walk":
            text = "正在扫描目录…"
        elif self._scan_total:
            text = f"正在加载 {len(self._all_items)}/{self._scan_total}…"
        else:
            text = "正在加载照片…"
        self._window_title.set_subtitle(text)
        self._set_scan_pill(True, text)

    def _on_scan_progress(self, generation: int, done: int, total: int, phase: str) -> bool:
        if generation != self._scan_generation or self._closed:
            return False
        if phase == "walk":
            self._update_scan_status("walk")
        elif phase == "probe":
            self._scan_total = total
            fraction = done / total if total else 0.0
            self._scan_progress.set_fraction(fraction)
            self._update_scan_status("probe")
        elif phase == "done":
            self._scan_progress.set_fraction(1.0)
        return False

    def _on_scan_batch(self, generation: int, items: list[MediaItem]) -> bool:
        if generation != self._scan_generation or self._closed or not items:
            return False
        if self._scan_first_batch:
            # 第一次收到结果时替换旧内容，避免重扫时新旧条目混在一起。
            self._all_items = []
            self._filtered_items = []
            self._scan_first_batch = False
        existing = {item.path: item for item in self._all_items}
        for item in items:
            existing[item.path] = item
        self._all_items = list(existing.values())
        if not self._scan_ui_started:
            # 第一批立即显示，避免快速扫描时等到最后才看到相册。
            self._scan_ui_started = True
            self._refresh_scan_items()
        elif self._scan_refresh_source == 0:
            self._scan_refresh_source = GLib.timeout_add(120, self._flush_scan_batch)
        return False

    def _flush_scan_batch(self) -> bool:
        self._scan_refresh_source = 0
        if self._closed:
            return False
        if self._all_items:
            self._refresh_scan_items()
        return False

    def _on_scan_failed(self, generation: int, message: str) -> bool:
        if generation != self._scan_generation or self._closed:
            return False
        self._scanning = False
        if self._scan_refresh_source:
            GLib.source_remove(self._scan_refresh_source)
            self._scan_refresh_source = 0
        self._set_scan_pill(False)
        self._show_error(message, retry=True)
        self._window_title.set_subtitle("扫描失败")
        return False

    def _on_scan_finished(self, generation: int, result: ScanResult) -> bool:
        if generation != self._scan_generation or self._closed:
            return False
        self._scanning = False
        if self._scan_refresh_source:
            GLib.source_remove(self._scan_refresh_source)
            self._scan_refresh_source = 0
        self._scan_total = len(result.items)
        self._set_scan_pill(False)
        self._all_items = list(result.items)
        self._all_items.sort(
            key=lambda item: item.sort_key, reverse=self._config.sort_descending
        )
        if result.errors:
            print("扫描时遇到问题：")
            for error in result.errors[:10]:
                print("  ", error)
        missing = [root for root in self._config.root_paths if not root.is_dir()]
        if missing:
            self._show_toast(f"{len(missing)} 个媒体库位置无法访问")
        if not self._all_items:
            self._filtered_items = []
            self._gallery.update_items([])
            self._show_empty(no_results=False)
            self._window_title.set_subtitle("没有找到媒体文件")
            return False

        items = self._filtered_items_for_current_settings()
        if not items:
            self._filtered_items = []
            self._gallery.update_items([])
            self._show_empty(no_results=True)
            self._window_title.set_subtitle("没有匹配的项目")
            return False

        value = self._vadjustment.get_value()
        active = self._gallery.active_month(value)
        anchor = self._gallery.jump_offset(*active) if active is not None else None
        delta = max(0.0, value - anchor) if anchor is not None else 0.0
        self._filtered_items = items
        self._gallery.update_items(items)
        self._set_timeline_visible(self._config.show_timeline)
        self._timeline.set_entries(self._gallery.timeline_entries())
        self._page_stack.set_visible_child_name("gallery")
        self._update_header()
        self._pending_scroll = (active, delta, value, 0)
        GLib.idle_add(self._update_view)
        return False

    def _refresh_scan_items(self) -> None:
        value = self._vadjustment.get_value()
        active = self._gallery.active_month(value)
        anchor = self._gallery.jump_offset(*active) if active is not None else None
        delta = max(0.0, value - anchor) if anchor is not None else 0.0
        self._all_items.sort(
            key=lambda item: item.sort_key, reverse=self._config.sort_descending
        )
        items = self._filtered_items_for_current_settings()
        self._filtered_items = items
        self._gallery.update_items(items)
        self._set_timeline_visible(self._config.show_timeline)
        self._timeline.set_entries(self._gallery.timeline_entries())
        self._page_stack.set_visible_child_name("gallery")
        self._update_header()
        self._update_scan_status()
        if items:
            self._pending_scroll = (active, delta, value, 0)
        GLib.idle_add(self._update_view)

    # ------------------------------------------------------------------ 筛选
    def _schedule_filter(self, immediate: bool = False) -> None:
        if self._filter_source:
            GLib.source_remove(self._filter_source)
            self._filter_source = 0
        if immediate:
            self._apply_filters(reset_scroll=True)
        else:
            self._filter_source = GLib.timeout_add(180, self._filter_idle)

    def _filter_idle(self) -> bool:
        self._filter_source = 0
        self._apply_filters(reset_scroll=True)
        return False

    def _current_filter(self) -> int:
        return self._filter_mode

    def _filtered_items_for_current_settings(self) -> list[MediaItem]:
        items = self._all_items
        mode = self._current_filter()
        if mode == _FILTER_PHOTOS:
            items = [item for item in items if not item.is_video]
        elif mode == _FILTER_VIDEOS:
            items = [item for item in items if item.is_video]
        query = self._search_entry.get_text().strip()
        if query:
            items = [item for item in items if item.matches(query)]
        return sorted(
            items, key=lambda item: item.sort_key, reverse=self._config.sort_descending
        )

    def _apply_filters(self, reset_scroll: bool = True) -> None:
        self._delete_scroll_token += 1
        self._close_context_popover()
        self._pending_scroll = None
        if not self._all_items:
            self._gallery.set_items([])
            self._timeline.set_entries([])
            self._show_empty(no_results=False)
            self._window_title.set_subtitle("没有找到媒体文件")
            return
        items = self._filtered_items_for_current_settings()
        self._filtered_items = items

        if not items:
            self._show_empty(no_results=True)
            self._window_title.set_subtitle("没有匹配的项目")
            return

        self._gallery.set_items(items)
        self._set_timeline_visible(self._config.show_timeline)
        self._timeline.set_entries(self._gallery.timeline_entries())
        self._page_stack.set_visible_child_name("gallery")
        if reset_scroll:
            self._vadjustment.set_value(0.0)
        self._update_header()
        GLib.idle_add(self._update_view)

    def _show_empty(self, no_results: bool) -> None:
        self._set_scan_pill(False)
        self._delete_scroll_token += 1
        self._pending_scroll = None
        self._empty_is_filter = no_results
        if no_results:
            self._empty_page.set_icon_name("system-search-symbolic")
            self._empty_page.set_title("没有找到匹配的内容")
            self._empty_page.set_description("换个关键词，或把筛选条件改回「全部」")
            self._empty_button.set_label("清除筛选")
            self._empty_button.set_visible(True)
        else:
            self._empty_page.set_icon_name("folder-pictures-symbolic")
            self._empty_page.set_title("这里还没有照片或视频")
            self._empty_page.set_description(
                "当前媒体库：" + self._config.library_tooltip().replace("\n", " · ")
            )
            self._empty_button.set_label("选择文件夹")
            self._empty_button.set_visible(True)
        self._page_stack.set_visible_child_name("empty")
        self._set_timeline_visible(False)
        self._scroll_top.set_visible(False)

    def _show_error(self, message: str, retry: bool = False) -> None:
        self._set_scan_pill(False)
        self._delete_scroll_token += 1
        self._pending_scroll = None
        self._error_description.set_label(message)
        self._error_retry.set_visible(retry)
        self._page_stack.set_visible_child_name("error")
        self._set_timeline_visible(False)
        self._scroll_top.set_visible(False)

    def _update_header(self) -> None:
        total = len(self._all_items)
        shown = len(self._filtered_items)
        photos = sum(1 for item in self._all_items if not item.is_video)
        videos = total - photos
        library = self._config.library_label
        if shown and shown != total:
            subtitle = f"显示 {shown} / {total} 个项目"
        elif total:
            subtitle = f"{photos} 张照片 · {videos} 个视频 · {library}"
        else:
            subtitle = library
        self._window_title.set_subtitle(subtitle)
        self._window_title.set_tooltip_text(f"媒体库：\n{self._config.library_tooltip()}")

    # ------------------------------------------------------------------ 视图
    def _on_gallery_layout(self) -> None:
        GLib.idle_add(self._update_view)

    def _on_scrolled(self, *_args: object) -> None:
        if self._refresh_source:
            return
        self._refresh_source = GLib.timeout_add(80, self._refresh_view_idle)

    def _refresh_view_idle(self) -> bool:
        self._refresh_source = 0
        self._update_view()
        return False

    def _update_view(self) -> bool:
        if self._closed or self._page_stack.get_visible_child_name() != "gallery":
            return False
        value = self._vadjustment.get_value()
        page_size = self._vadjustment.get_page_size()
        # 窗口尚未完成分配时 page_size 可能等于内容总高度，会把整库缩略图
        # 都排队生成；这里用窗口高度兜底。
        window_height = float(self.get_height())
        if page_size <= 1 or page_size > window_height * 1.2:
            page_size = max(window_height - 120.0, 320.0)
        # 时间线跳转动画期间只释放不可见纹理，动画结束后再请求缩略图。
        allow_requests = self._scroll_animation_source == 0
        self._gallery.update_visibility(value, page_size, allow_requests=allow_requests)
        month = self._gallery.active_month(value)
        if month is not None:
            self._timeline.set_active(*month)
        self._scroll_top.set_visible(value > 500)
        if self._pending_scroll is not None:
            self._apply_pending_scroll()
        return False

    def _animate_scroll(self, target: float) -> None:
        self._delete_scroll_token += 1
        if self._scroll_animation_source:
            GLib.source_remove(self._scroll_animation_source)
            self._scroll_animation_source = 0
        start = self._vadjustment.get_value()
        distance = target - start
        if abs(distance) < 1.0:
            return
        steps = max(6, min(22, int(abs(distance) / 60) + 6))
        state = {"step": 0}

        def tick() -> bool:
            state["step"] += 1
            progress = state["step"] / steps
            eased = 1 - (1 - progress) ** 3
            self._vadjustment.set_value(start + distance * eased)
            if state["step"] >= steps:
                self._scroll_animation_source = 0
                GLib.idle_add(self._update_view)
                return False
            return True

        self._scroll_animation_source = GLib.timeout_add(16, tick)

    def _set_timeline_visible(self, visible: bool) -> None:
        self._timeline.set_visible(visible)
        if hasattr(self, "_gallery"):
            self._gallery.set_side_inset(76 if visible else 0)
        if hasattr(self, "_scroll_top"):
            self._scroll_top.set_margin_end(102 if visible else 26)

    def _on_user_scroll(self, *_args: object) -> bool:
        # 用户主动滚动时取消删除后的滚动保护，避免和用户抢滚动条。
        self._delete_scroll_token += 1
        return False

    def _on_timeline_scroll(self, _controller: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        # 指针位于时间线上时，滚轮仍然滚动照片墙。
        self._delete_scroll_token += 1
        step = dy if abs(dy) >= 4 else dy * 90.0
        upper = self._vadjustment.get_upper()
        page = self._vadjustment.get_page_size()
        value = self._vadjustment.get_value() + step
        value = max(0.0, min(value, max(0.0, upper - page)))
        self._vadjustment.set_value(value)
        return True

    def _on_timeline_jump(self, year: int, month: int) -> None:
        self._delete_scroll_token += 1
        offset = self._gallery.jump_offset(year, month)
        if offset is not None:
            self._animate_scroll(offset)
            self._timeline.set_active(year, month)

    # ------------------------------------------------------------------ 动作
    def _choose_folder(self) -> None:
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
            if not path:
                return
            self.add_roots([path])

        dialog.select_folder(self, None, finished)

    def set_roots(self, paths: list[str], replace: bool = False) -> None:
        """设置媒体库位置；replace=True 时替换，否则追加。"""
        normalized = [str(Path(path).expanduser()) for path in paths if path]
        if not normalized:
            return
        if replace:
            new_roots = list(dict.fromkeys(normalized))
        else:
            new_roots = list(dict.fromkeys([*self._config.roots, *normalized]))
        if new_roots == self._config.roots:
            return
        self._config.roots = new_roots
        self._config.save()
        self._window_title.set_subtitle(self._config.library_label)
        self._window_title.set_tooltip_text(f"媒体库：\n{self._config.library_tooltip()}")
        self._start_scan()

    def add_roots(self, paths: list[str]) -> None:
        self.set_roots(paths, replace=False)

    def set_root(self, path: str) -> None:
        """兼容单目录调用：替换整个媒体库。"""
        self.set_roots([path], replace=True)

    def _open_preferences(self) -> None:
        if self._preferences is not None:
            self._preferences.present(self)
            return
        dialog = PreferencesDialog(self._config, self, self._cache, self._on_preference_change)
        dialog.connect("closed", self._on_preferences_closed)
        self._preferences = dialog
        dialog.present(self)

    def _on_preferences_closed(self, *_args: object) -> None:
        self._preferences = None

    def _on_preference_change(self, reason: str) -> None:
        if reason in ("root", "roots", "rescan"):
            self._window_title.set_tooltip_text(f"媒体库：\n{self._config.library_tooltip()}")
            self._start_scan()
        elif reason == "row-height":
            self._gallery.set_row_height(self._config.row_height)
            GLib.idle_add(self._update_view)
        elif reason == "sort":
            self._apply_filters(reset_scroll=True)
        elif reason == "timeline":
            value = self._vadjustment.get_value()
            active = self._gallery.active_month(value)
            anchor = self._gallery.jump_offset(*active) if active is not None else None
            delta = max(0.0, value - anchor) if anchor is not None else 0.0
            self._set_timeline_visible(self._config.show_timeline)
            if self._config.show_timeline:
                self._timeline.set_entries(self._gallery.timeline_entries())
            self._pending_scroll = (active, delta, value, 0)
            GLib.idle_add(self._update_view)
        elif reason == "date-headers":
            value = self._vadjustment.get_value()
            active = self._gallery.active_month(value)
            anchor = self._gallery.jump_offset(*active) if active is not None else None
            delta = max(0.0, value - anchor) if anchor is not None else 0.0
            self._gallery.set_headers_visible(self._config.show_date_headers)
            self._timeline.set_entries(self._gallery.timeline_entries())
            self._pending_scroll = (active, delta, value, 0)
            GLib.idle_add(self._update_view)
        elif reason == "cache":
            self._gallery.reload_textures()
            GLib.idle_add(self._update_view)

    def _open_viewer(self, item: MediaItem) -> None:
        items = self._filtered_items or self._all_items
        try:
            index = items.index(item)
        except ValueError:
            index = 0
        if self._viewer is not None and self._viewer.get_visible():
            self._viewer.close()
        viewer = MediaViewer(items, index, self._config, self._thumbnails, parent=self)
        viewer.present()
        self._viewer = viewer

    # ------------------------------------------------------------------ 右键菜单 / 删除
    def _show_tile_menu(self, tile: object, x: float, y: float) -> None:
        """在卡片上右键时弹出上下文菜单。"""
        item = getattr(tile, "item", None)
        if not isinstance(item, MediaItem):
            return
        self._context_item = item
        popover = self._ensure_context_popover()
        if hasattr(self, "_context_edit_button"):
            self._context_edit_button.set_visible(not item.is_video)
        if popover.get_parent() is not None:
            popover.unparent()
        popover.set_parent(tile)
        rect = Gdk.Rectangle()
        rect.x = int(max(0, x))
        rect.y = int(max(0, y))
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        popover.popup()

    def _ensure_context_popover(self) -> Gtk.Popover:
        if self._context_popover is not None:
            return self._context_popover
        popover = Gtk.Popover()
        popover.add_css_class("context-menu")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        for label, icon_name, callback in (
            ("打开", "document-open-symbolic", self._context_open),
            ("复制", "edit-copy-symbolic", self._context_copy_files),
            ("在文件管理器中打开", "folder-open-symbolic", self._context_show_in_folder),
        ):
            button = Gtk.Button()
            button.add_css_class("flat")
            button.set_halign(Gtk.Align.FILL)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.append(Gtk.Image.new_from_icon_name(icon_name))
            text = Gtk.Label(label=label)
            text.set_xalign(0.0)
            text.set_hexpand(True)
            row.append(text)
            button.set_child(row)
            button.connect("clicked", callback)
            box.append(button)

        edit_button = Gtk.Button()
        edit_button.add_css_class("flat")
        edit_button.set_halign(Gtk.Align.FILL)
        edit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        edit_row.append(Gtk.Image.new_from_icon_name("document-edit-symbolic"))
        edit_label = Gtk.Label(label="编辑 EXIF 信息")
        edit_label.set_xalign(0.0)
        edit_label.set_hexpand(True)
        edit_row.append(edit_label)
        edit_button.set_child(edit_row)
        edit_button.connect("clicked", self._context_edit_exif)
        box.append(edit_button)
        self._context_edit_button = edit_button

        delete_button = Gtk.Button()
        delete_button.add_css_class("flat")
        delete_button.add_css_class("context-delete")
        delete_button.set_halign(Gtk.Align.FILL)
        delete_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        delete_row.append(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
        delete_label = Gtk.Label(label="删除文件")
        delete_label.set_xalign(0.0)
        delete_label.set_hexpand(True)
        delete_row.append(delete_label)
        delete_button.set_child(delete_row)
        delete_button.connect("clicked", self._context_delete)
        box.append(delete_button)
        popover.set_child(box)
        popover.connect("closed", lambda p: p.unparent())
        self._context_popover = popover
        return popover

    def _close_context_popover(self) -> None:
        if self._context_popover is not None:
            self._context_popover.popdown()

    def _context_open(self, *_args: object) -> None:
        item = self._context_item
        self._close_context_popover()
        if item is not None:
            self._open_viewer(item)

    def _context_show_in_folder(self, *_args: object) -> None:
        item = self._context_item
        self._close_context_popover()
        if item is not None:
            show_in_file_manager(item.path)

    def _context_copy_files(self, *_args: object) -> None:
        item = self._context_item
        self._close_context_popover()
        if item is None:
            return
        if copy_files([item.path]):
            self._show_toast(f"「{item.name}」已复制到剪贴板")
        else:
            self._show_toast("无法访问剪贴板")

    def _context_edit_exif(self, *_args: object) -> None:
        item = self._context_item
        self._close_context_popover()
        if item is not None:
            self._open_exif_editor(item)

    def _open_exif_editor(self, item: MediaItem) -> None:
        if item.is_video or self._closed:
            return
        if self._exif_editor is not None:
            self._exif_editor.close()
        editor = ExifEditor(item, lambda: self._on_exif_saved(item))
        editor.connect("closed", self._on_exif_editor_closed)
        self._exif_editor = editor
        editor.present(self)

    def _on_exif_editor_closed(self, *_args: object) -> None:
        self._exif_editor = None

    def _on_exif_saved(self, item: MediaItem) -> None:
        new_item = read_media_item(item.path)
        if new_item is None:
            self._show_toast("无法重新读取编辑后的照片")
            return
        value = self._vadjustment.get_value()
        active = self._gallery.active_month(value)
        anchor = self._gallery.jump_offset(*active) if active is not None else None
        delta = max(0.0, value - anchor) if anchor is not None else 0.0
        try:
            self._cache.path_for(self._cache.key(item)).unlink(missing_ok=True)
        except OSError:
            pass
        self._all_items = [
            new_item if entry.path == item.path else entry for entry in self._all_items
        ]
        self._apply_filters(reset_scroll=False)
        self._pending_scroll = (active, delta, value, 0)
        GLib.idle_add(self._update_view)
        self._show_toast("EXIF 信息已保存")

    def _context_delete(self, *_args: object) -> None:
        item = self._context_item
        self._close_context_popover()
        if item is not None:
            self._request_delete(item)

    def _set_dialog_scroll_hold(self, active: bool) -> None:
        self._holding_dialog_scroll = False
        if active:
            if self._dialog_scroll_handler == 0:
                self._dialog_scroll_handler = self._vadjustment.connect(
                    "value-changed", self._hold_dialog_scroll
                )
        elif self._dialog_scroll_handler:
            self._vadjustment.disconnect(self._dialog_scroll_handler)
            self._dialog_scroll_handler = 0

    def _hold_dialog_scroll(self, adjustment: Gtk.Adjustment) -> None:
        """确认框打开/关闭时 GTK 可能因焦点重置滚动，这里立即压回原位置。"""
        target = self._delete_scroll_before
        if target is None or self._holding_dialog_scroll:
            return
        upper = adjustment.get_upper()
        page = adjustment.get_page_size()
        available = max(0.0, upper - page)
        if available <= 1 and target > 1:
            return
        target = min(target, available)
        if abs(adjustment.get_value() - target) > 1.0:
            self._holding_dialog_scroll = True
            adjustment.set_value(target)
            self._holding_dialog_scroll = False

    def _request_delete(self, item: MediaItem) -> None:
        """删除确认对话框；默认移到系统回收站。"""
        self._delete_scroll_before = self._vadjustment.get_value()
        self._set_dialog_scroll_hold(True)
        # 把焦点移出照片墙，避免 ScrolledWindow 为了“聚焦当前卡片”而偷偷滚动。
        try:
            self._search_button.grab_focus()
        except Exception:  # noqa: BLE001
            pass
        self._close_context_popover()
        dialog = Adw.AlertDialog()
        dialog.set_heading("删除这个文件？")
        dialog.set_body(f"「{item.name}」将被移到系统回收站，之后可以从回收站恢复。")
        dialog.add_response("cancel", "取消")
        dialog.add_response("trash", "移到回收站")
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_response, item)
        self._delete_dialog = dialog
        dialog.present(self)

    def _on_delete_response(self, dialog: Adw.AlertDialog, response: str, item: MediaItem) -> None:
        self._delete_dialog = None
        if response != "trash":
            self._delete_scroll_before = None
            self._set_dialog_scroll_hold(False)
            return

        def finished(error: str | None) -> None:
            if error:
                self._offer_permanent_delete(item, error)
            else:
                self._item_deleted(item, trashed=True)

        move_to_trash(item.path, finished)

    def _offer_permanent_delete(self, item: MediaItem, error: str) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading("无法移到回收站")
        dialog.set_body(f"{error}\n\n是否直接永久删除「{item.name}」？此操作无法撤销。")
        dialog.add_response("cancel", "取消")
        dialog.add_response("delete", "永久删除")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_permanent_delete_response, item)
        self._permanent_dialog = dialog
        dialog.present(self)

    def _on_permanent_delete_response(self, dialog: Adw.AlertDialog, response: str, item: MediaItem) -> None:
        self._permanent_dialog = None
        if response != "delete":
            self._delete_scroll_before = None
            self._set_dialog_scroll_hold(False)
            return

        def finished(error: str | None) -> None:
            if error:
                self._show_toast(f"删除失败：{error}")
            else:
                self._item_deleted(item, trashed=False)

        delete_permanently(item.path, finished)

    def _item_deleted(self, item: MediaItem, *, trashed: bool) -> None:
        if self._closed:
            return
        if self._scroll_animation_source:
            GLib.source_remove(self._scroll_animation_source)
            self._scroll_animation_source = 0
        if self._viewer is not None and self._viewer.get_visible():
            try:
                if any(entry.path == item.path for entry in self._viewer._items):
                    self._viewer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._cache.path_for(self._cache.key(item)).unlink(missing_ok=True)
        except OSError:
            pass

        # 确认对话框打开期间可能因焦点变化发生滚动，删除前回到打开对话框时的位置。
        restore = self._delete_scroll_before
        self._delete_scroll_before = None
        self._set_dialog_scroll_hold(False)
        if restore is not None:
            upper = self._vadjustment.get_upper()
            page = self._vadjustment.get_page_size()
            self._vadjustment.set_value(
                max(0.0, min(restore, max(0.0, upper - page)))
            )
        scroll_value = self._vadjustment.get_value()

        self._all_items = [entry for entry in self._all_items if entry.path != item.path]
        self._filtered_items = [entry for entry in self._filtered_items if entry.path != item.path]
        if self._context_item is not None and self._context_item.path == item.path:
            self._context_item = None

        removed = self._gallery.remove_item(item)
        if not removed:
            # 不在当前照片墙里（例如筛选后），退回通用重建。
            self._apply_filters(reset_scroll=False)
        else:
            self._set_timeline_visible(self._config.show_timeline)
            self._timeline.set_entries(self._gallery.timeline_entries())
            self._update_header()
        self._show_toast(f"「{item.name}」已移到回收站" if trashed else f"已永久删除「{item.name}」")

        if not self._all_items:
            self._show_empty(no_results=False)
            self._window_title.set_subtitle("没有找到媒体文件")
            return
        if not self._filtered_items:
            self._show_empty(no_results=True)
            self._window_title.set_subtitle("没有匹配的项目")
            return
        # 增量重排本身会保留当前滚动值，后面的照片直接向前挤；
        # 下面的保护只用于兜底 GTK 因焦点/布局把滚动值重置到顶部的情况。
        self._pending_scroll = None
        self._delete_scroll_token += 1
        token = self._delete_scroll_token
        for delay in (120, 280, 520, 900):
            GLib.timeout_add(
                delay, self._guard_delete_scroll, token, None, 0.0, scroll_value
            )
        GLib.idle_add(self._update_view)

    def _guard_delete_scroll(
        self,
        token: int,
        active: tuple[int, int] | None,
        delta: float,
        fallback: float,
    ) -> bool:
        """防止删除后 GTK 聚焦/重排把滚动值悄悄重置到顶部。"""
        if token != self._delete_scroll_token or self._closed:
            return False
        if self._page_stack.get_visible_child_name() != "gallery":
            return False
        target = fallback
        if active is not None:
            offset = self._gallery.jump_offset(*active)
            if offset is not None:
                target = offset + delta
        upper = self._vadjustment.get_upper()
        page = self._vadjustment.get_page_size()
        available = max(0.0, upper - page)
        if available <= 1 and target > 80:
            return False
        target = min(target, available)
        # 只在滚动被重置到顶部时纠正；正常位置或用户已滚动都不打扰。
        if target > 80 and self._vadjustment.get_value() < 20:
            self._vadjustment.set_value(target)
        return False

    def _apply_pending_scroll(self) -> bool:
        pending = self._pending_scroll
        if pending is None:
            return False
        month, delta, fallback, attempts = pending
        if self._closed or self._page_stack.get_visible_child_name() != "gallery":
            self._pending_scroll = None
            return False
        target = fallback
        if month is not None:
            offset = self._gallery.jump_offset(*month)
            if offset is None:
                if attempts < 20:
                    self._pending_scroll = (month, delta, fallback, attempts + 1)
                    GLib.timeout_add(60, self._apply_pending_scroll)
                    return False
            else:
                target = offset + delta
        upper = self._vadjustment.get_upper()
        page = self._vadjustment.get_page_size()
        available = max(0.0, upper - page)
        target = max(0.0, min(target, available))
        self._pending_scroll = None
        self._vadjustment.set_value(target)
        actual = self._vadjustment.get_value()
        if abs(actual - target) > 1.0 and attempts < 20:
            self._pending_scroll = (month, delta, fallback, attempts + 1)
            GLib.timeout_add(60, self._apply_pending_scroll)
        return False

    def _restore_scroll(self, value: float) -> bool:
        if self._closed or self._page_stack.get_visible_child_name() != "gallery":
            return False
        upper = self._vadjustment.get_upper()
        page = self._vadjustment.get_page_size()
        self._vadjustment.set_value(max(0.0, min(value, max(0.0, upper - page))))
        return False

    def _show_toast(self, text: str) -> None:
        if self._toasts is None or self._closed:
            return
        toast = Adw.Toast.new(text)
        toast.set_timeout(3)
        self._toasts.add_toast(toast)

    # ------------------------------------------------------------------ 拖放
    def _on_drop(self, _target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        files = None
        if isinstance(value, Gdk.FileList):
            files = value.get_files()
        if files:
            paths = [
                path
                for gfile in files
                if (path := gfile.get_path()) and os.path.isdir(path)
            ]
            if paths:
                self.add_roots(paths)
                return True
        return False

    # ------------------------------------------------------------------ 关闭
    def _on_close_request(self, *_args: object) -> bool:
        self._closed = True
        if self._scan_cancel is not None:
            self._scan_cancel.set()
        if not self.is_maximized():
            self._config.window_width = max(860, self.get_width())
            self._config.window_height = max(560, self.get_height())
            self._config.window_maximized = False
        else:
            self._config.window_maximized = True
        self._config.save()
        return False
