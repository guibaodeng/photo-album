"""全屏查看器：照片缩放 / 拖拽，视频播放，信息面板。

照片使用自绘画布，支持滚轮缩放、拖动平移、双击在「适应窗口」和
200% 之间切换；视频使用 GtkMediaFile + GtkVideo 播放。
"""

from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, GLib, Graphene, Gtk, Pango  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning, module=__name__)

from .config import Config  # noqa: E402
from .imaging import decode_image  # noqa: E402
from .models import MediaItem, format_size  # noqa: E402
from .thumbnails import ThumbnailService  # noqa: E402

_VIEW_MAX_SIDE = 3200


class PhotoCanvas(Gtk.Widget):
    """可缩放、可平移的照片画布。"""

    MAX_ZOOM = 8.0

    def __init__(self) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._texture: Gdk.Texture | None = None
        self._zoom = 1.0
        self._offset = [0.0, 0.0]
        self._drag_origin: tuple[float, float] | None = None
        self._drag_offset: tuple[float, float] | None = None
        self._zoom_base = 1.0
        self._on_zoom_changed: Callable[[float], None] | None = None

        self.set_cursor(Gdk.Cursor.new_from_name("default"))

        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        pinch = Gtk.GestureZoom()
        pinch.connect("begin", self._on_pinch_begin)
        pinch.connect("scale-changed", self._on_pinch_changed)
        self.add_controller(pinch)

        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_click)
        self.add_controller(click)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

    # ------------------------------------------------------------------ 状态
    @property
    def zoom(self) -> float:
        return self._zoom

    def set_on_zoom_changed(self, callback: Callable[[float], None]) -> None:
        self._on_zoom_changed = callback

    def set_texture(self, texture: Gdk.Texture | None) -> None:
        self._texture = texture
        self._zoom = 1.0
        self._offset = [0.0, 0.0]
        self._notify_zoom()
        self.queue_draw()

    def reset_zoom(self) -> None:
        if self._zoom != 1.0:
            self._zoom = 1.0
            self._offset = [0.0, 0.0]
            self._notify_zoom()
            self.queue_draw()

    def zoom_by(self, factor: float) -> None:
        self._set_zoom(self._zoom * factor)

    def _notify_zoom(self) -> None:
        if self._on_zoom_changed is not None:
            self._on_zoom_changed(self._zoom)

    # ------------------------------------------------------------------ 缩放
    def _base_scale(self) -> float:
        if self._texture is None:
            return 1.0
        width = self.get_width()
        height = self.get_height()
        texture_width = self._texture.get_width()
        texture_height = self._texture.get_height()
        if width <= 0 or height <= 0 or texture_width <= 0 or texture_height <= 0:
            return 1.0
        return min(width / texture_width, height / texture_height)

    def _set_zoom(self, zoom: float, focus: tuple[float, float] | None = None) -> None:
        if self._texture is None:
            return
        new_zoom = max(1.0, min(self.MAX_ZOOM, zoom))
        if abs(new_zoom - self._zoom) < 1e-4:
            return
        width = self.get_width()
        height = self.get_height()
        texture_width = self._texture.get_width()
        texture_height = self._texture.get_height()
        base = self._base_scale()
        if focus is None:
            focus = (width / 2, height / 2)

        old_scale = base * self._zoom
        if old_scale <= 0:
            old_scale = 1.0
        old_width = texture_width * old_scale
        old_height = texture_height * old_scale
        image_x = (focus[0] - (width - old_width) / 2 - self._offset[0]) / old_scale
        image_y = (focus[1] - (height - old_height) / 2 - self._offset[1]) / old_scale

        new_scale = base * new_zoom
        new_width = texture_width * new_scale
        new_height = texture_height * new_scale
        self._offset[0] = focus[0] - (width - new_width) / 2 - image_x * new_scale
        self._offset[1] = focus[1] - (height - new_height) / 2 - image_y * new_scale
        self._zoom = new_zoom
        self._clamp_offset()
        self._update_cursor()
        self._notify_zoom()
        self.queue_draw()

    def _clamp_offset(self) -> None:
        if self._texture is None:
            self._offset = [0.0, 0.0]
            return
        width = self.get_width()
        height = self.get_height()
        scale = self._base_scale() * self._zoom
        image_width = self._texture.get_width() * scale
        image_height = self._texture.get_height() * scale
        # 图片以视口中心为原点绘制。放大后超出的部分两侧都可以移动，
        # 限制在 ±(图片尺寸 - 视口尺寸) / 2 内。
        limit_x = max(0.0, image_width - width) / 2.0
        limit_y = max(0.0, image_height - height) / 2.0
        self._offset[0] = max(-limit_x, min(limit_x, self._offset[0]))
        self._offset[1] = max(-limit_y, min(limit_y, self._offset[1]))

    def _update_cursor(self) -> None:
        name = "grabbing" if self._drag_origin is not None else ("grab" if self._zoom > 1.0 else "default")
        try:
            self.set_cursor(Gdk.Cursor.new_from_name(name))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 交互
    def _on_click(self, gesture: Gtk.GestureClick, presses: int, x: float, y: float) -> None:
        if presses >= 2:
            if self._zoom > 1.0:
                self.reset_zoom()
            else:
                self._set_zoom(2.0, (x, y))
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_scroll(self, controller: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        if self._texture is None:
            return False
        focus: tuple[float, float] | None = None
        event = controller.get_current_event()
        if event is not None and hasattr(event, "get_position"):
            try:
                _ok, x, y = event.get_position()
                focus = (x, y)
            except Exception:  # noqa: BLE001
                focus = None
        factor = 1.15 ** (-dy)
        self._set_zoom(self._zoom * factor, focus)
        return True

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        if self._zoom <= 1.0:
            return
        self._drag_origin = self._offset[0], self._offset[1]
        self._update_cursor()

    def _on_drag_update(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if self._drag_origin is None:
            return
        self._offset[0] = self._drag_origin[0] + offset_x
        self._offset[1] = self._drag_origin[1] + offset_y
        self._clamp_offset()
        self.queue_draw()

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, _offset_x: float, _offset_y: float) -> None:
        self._drag_origin = None
        self._update_cursor()

    def _on_pinch_begin(self, gesture: Gtk.GestureZoom, _sequence: object) -> None:
        self._zoom_base = self._zoom
        self.queue_draw()

    def _on_pinch_changed(self, gesture: Gtk.GestureZoom, scale: float) -> None:
        if self._texture is None:
            return
        self._set_zoom(self._zoom_base * scale)

    # ------------------------------------------------------------------ 绘制
    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        return 0, 0, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self._texture is None:
            return
        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return
        scale = self._base_scale() * self._zoom
        texture_width = self._texture.get_width()
        texture_height = self._texture.get_height()
        draw_width = texture_width * scale
        draw_height = texture_height * scale
        x = (width - draw_width) / 2 + self._offset[0]
        y = (height - draw_height) / 2 + self._offset[1]
        snapshot.push_clip(Graphene.Rect().init(0, 0, width, height))
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x, y))
        snapshot.scale(scale, scale)
        snapshot.append_texture(
            self._texture,
            Graphene.Rect().init(0, 0, texture_width, texture_height),
        )
        snapshot.restore()
        snapshot.pop()


class MediaViewer(Adw.Window):
    """独立的照片/视频查看窗口。"""

    def __init__(
        self,
        items: list[MediaItem],
        index: int,
        config: Config,
        thumbnails: ThumbnailService,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(application=parent.get_application() if parent else None)
        self._items = items
        self._index = max(0, min(index, len(items) - 1)) if items else 0
        self._config = config
        self._thumbnails = thumbnails
        self._load_generation = 0
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="viewer")
        self._media: Gtk.MediaFile | None = None
        self._closed = False

        self.set_default_size(1280, 860)
        self.add_css_class("viewer-window")
        if parent is not None:
            self.set_transient_for(parent)
        self.set_destroy_with_parent(True)

        self._title = Adw.WindowTitle()
        header = Adw.HeaderBar()
        header.set_title_widget(self._title)
        close_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        close_button.set_tooltip_text("关闭 (Esc)")
        close_button.connect("clicked", lambda *_: self.close())
        header.pack_start(close_button)

        self._info_button = Gtk.ToggleButton()
        self._info_button.set_icon_name("info-outline-symbolic")
        self._info_button.set_tooltip_text("详细信息 (I)")
        self._info_button.connect("toggled", self._on_info_toggled)
        header.pack_end(self._info_button)

        fullscreen_button = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic")
        fullscreen_button.set_tooltip_text("全屏 (F11)")
        fullscreen_button.connect("clicked", lambda *_: self._toggle_fullscreen())
        header.pack_end(fullscreen_button)


        self._canvas = PhotoCanvas()
        self._canvas.set_on_zoom_changed(self._on_zoom_changed)

        self._photo_box = Gtk.Box()
        self._photo_box.set_hexpand(True)
        self._photo_box.set_vexpand(True)
        self._photo_box.append(self._canvas)

        self._video = Gtk.Video()
        self._video.set_hexpand(True)
        self._video.set_vexpand(True)
        self._video.set_autoplay(config.video_autoplay)
        self._video.set_loop(False)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(120)
        self._stack.add_named(self._photo_box, "photo")
        self._stack.add_named(self._video, "video")
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        self._info_panel = self._build_info_panel()
        self._info_revealer = Gtk.Revealer()
        self._info_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self._info_revealer.set_transition_duration(200)
        self._info_revealer.set_child(self._info_panel)
        self._info_revealer.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        content.append(self._stack)
        content.append(self._info_revealer)

        overlay = Gtk.Overlay()
        overlay.set_child(content)

        self._loading = self._build_loading_pill()
        overlay.add_overlay(self._loading)

        self._zoom_pill = Gtk.Label()
        self._zoom_pill.add_css_class("zoom-pill")
        self._zoom_pill.set_halign(Gtk.Align.START)
        self._zoom_pill.set_valign(Gtk.Align.END)
        self._zoom_pill.set_margin_start(18)
        self._zoom_pill.set_margin_bottom(18)
        self._zoom_pill.set_visible(False)
        overlay.add_overlay(self._zoom_pill)

        self._prev_button = self._build_nav_button("go-previous-symbolic", Gtk.Align.START)
        self._prev_button.connect("clicked", lambda *_: self._navigate(-1))
        overlay.add_overlay(self._prev_button)
        self._next_button = self._build_nav_button("go-next-symbolic", Gtk.Align.END)
        self._next_button.connect("clicked", lambda *_: self._navigate(1))
        overlay.add_overlay(self._next_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(overlay)
        self.set_content(toolbar)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        self.connect("close-request", self._on_close_request)
        self._show(self._index)

    # ------------------------------------------------------------------ 构建
    def _build_info_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.add_css_class("info-panel")
        box.set_size_request(340, -1)
        title = Gtk.Label(label="详细信息")
        title.add_css_class("info-title")
        title.set_xalign(0.0)
        box.append(title)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self._info_grid = Gtk.Grid()
        self._info_grid.set_column_spacing(12)
        self._info_grid.set_row_spacing(10)
        self._info_grid.set_margin_end(16)
        self._info_grid.set_margin_bottom(20)
        scroller.set_child(self._info_grid)
        box.append(scroller)
        return box

    def _build_loading_pill(self) -> Gtk.Widget:
        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        pill.add_css_class("loading-pill")
        pill.set_halign(Gtk.Align.CENTER)
        pill.set_valign(Gtk.Align.START)
        pill.set_margin_top(14)
        spinner = Adw.Spinner()
        spinner.set_size_request(18, 18)
        pill.append(spinner)
        label = Gtk.Label(label="正在加载高质量预览…")
        pill.append(label)
        pill.set_visible(False)
        return pill

    def _build_nav_button(self, icon_name: str, align: Gtk.Align) -> Gtk.Button:
        button = Gtk.Button.new_from_icon_name(icon_name)
        button.add_css_class("osd")
        button.add_css_class("circular")
        button.add_css_class("nav-button")
        button.set_halign(align)
        button.set_valign(Gtk.Align.CENTER)
        button.set_margin_start(18)
        button.set_margin_end(18)
        return button

    # ------------------------------------------------------------------ 导航
    def _show(self, index: int) -> None:
        if not self._items:
            self._title.set_title("没有可显示的内容")
            return
        index = max(0, min(index, len(self._items) - 1))
        self._index = index
        item = self._items[index]
        self._title.set_title(item.name)
        subtitle = f"{index + 1} / {len(self._items)} · {item.date_short}"
        if item.is_video and item.duration > 0:
            subtitle += f" · {item.duration_text}"
        self._title.set_subtitle(subtitle)
        self._prev_button.set_sensitive(index > 0)
        self._next_button.set_sensitive(index < len(self._items) - 1)
        has_multiple = len(self._items) > 1
        self._prev_button.set_visible(has_multiple)
        self._next_button.set_visible(has_multiple)
        self._populate_info(item)

        if item.is_video:
            self._show_video(item)
        else:
            self._show_photo(item)

    def _navigate(self, delta: int) -> None:
        target = self._index + delta
        if 0 <= target < len(self._items):
            self._show(target)

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            if self.is_fullscreen():
                self.unfullscreen()
            else:
                self.close()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up):
            self._navigate(-1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down):
            self._navigate(1)
            return True
        if keyval in (Gdk.KEY_F11, Gdk.KEY_f):
            self._toggle_fullscreen()
            return True
        if keyval in (Gdk.KEY_i, Gdk.KEY_I):
            self._info_button.set_active(not self._info_button.get_active())
            return True
        if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self._canvas.zoom_by(1.25)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self._canvas.zoom_by(1 / 1.25)
            return True
        if keyval == Gdk.KEY_0:
            self._canvas.reset_zoom()
            return True
        if keyval == Gdk.KEY_space and self._stack.get_visible_child_name() == "video":
            self._toggle_video()
            return True
        return False

    def _toggle_fullscreen(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    # ------------------------------------------------------------------ 照片
    def _show_photo(self, item: MediaItem) -> None:
        self._stop_video()
        self._stack.set_visible_child_name("photo")
        self._load_generation += 1
        generation = self._load_generation
        self._canvas.set_texture(None)
        self._loading.set_visible(True)

        preview = self._thumbnails.cached_bytes(item)
        if preview:
            try:
                self._canvas.set_texture(Gdk.Texture.new_from_bytes(GLib.Bytes.new(preview)))
            except Exception:  # noqa: BLE001
                pass

        self._executor.submit(self._load_full_image, item, generation)

    def _load_full_image(self, item: MediaItem, generation: int) -> None:
        data: bytes | None = None
        try:
            data = decode_image(item.path, _VIEW_MAX_SIDE, orientation=item.orientation, quality=92)
        except Exception:  # noqa: BLE001
            data = None
        GLib.idle_add(self._on_image_loaded, generation, data)

    def _on_image_loaded(self, generation: int, data: bytes | None) -> bool:
        if self._closed or generation != self._load_generation:
            return False
        self._loading.set_visible(False)
        if data is None:
            self._canvas.set_texture(None)
            return False
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except Exception:  # noqa: BLE001
            return False
        if self._stack.get_visible_child_name() == "photo":
            self._canvas.set_texture(texture)
        return False

    # ------------------------------------------------------------------ 视频
    def _show_video(self, item: MediaItem) -> None:
        self._canvas.set_texture(None)
        self._loading.set_visible(False)
        self._stack.set_visible_child_name("video")
        self._stop_video()
        try:
            media = Gtk.MediaFile.new_for_filename(str(item.path))
        except Exception:  # noqa: BLE001
            return
        self._media = media
        self._video.set_media_stream(media)
        if self._config.video_autoplay:
            media.set_playing(True)

    def _toggle_video(self) -> None:
        if self._media is None:
            return
        try:
            self._media.set_playing(not self._media.get_playing())
        except Exception:  # noqa: BLE001
            pass

    def _stop_video(self) -> None:
        if self._media is not None:
            try:
                self._media.set_playing(False)
            except Exception:  # noqa: BLE001
                pass
            self._media = None
        try:
            self._video.set_media_stream(None)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 信息
    def _on_info_toggled(self, button: Gtk.ToggleButton) -> None:
        self._info_revealer.set_reveal_child(button.get_active())

    def _on_zoom_changed(self, zoom: float) -> None:
        if zoom <= 1.01:
            self._zoom_pill.set_visible(False)
        else:
            self._zoom_pill.set_label(f"{round(zoom * 100)}%")
            self._zoom_pill.set_visible(True)

    def _clear_info(self) -> None:
        child = self._info_grid.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self._info_grid.remove(child)
            child = next_child

    def _add_info(self, row: int, label: str, value: str) -> None:
        name = Gtk.Label(label=label)
        name.add_css_class("info-label")
        name.set_xalign(0.0)
        name.set_valign(Gtk.Align.START)
        self._info_grid.attach(name, 0, row, 1, 1)
        content = Gtk.Label(label=value)
        content.add_css_class("info-value")
        content.set_xalign(0.0)
        content.set_wrap(True)
        content.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        content.set_max_width_chars(30)
        content.set_selectable(True)
        content.set_valign(Gtk.Align.START)
        self._info_grid.attach(content, 1, row, 1, 1)

    def _populate_info(self, item: MediaItem) -> None:
        self._clear_info()
        row = 0
        self._add_info(row, "文件名", item.name)
        row += 1
        self._add_info(row, "时间", item.date_text)
        row += 1
        self._add_info(row, "类型", item.kind_text)
        row += 1
        self._add_info(row, "尺寸", item.resolution_text)
        row += 1
        self._add_info(row, "大小", format_size(item.size))
        row += 1
        if item.is_video and item.duration > 0:
            self._add_info(row, "时长", item.duration_text)
            row += 1
        self._add_info(row, "路径", str(item.parent))
        row += 1

        if item.is_video:
            return
        for label, value in _read_exif_details(item):
            self._add_info(row, label, value)
            row += 1

    # ------------------------------------------------------------------ 其它
    def _on_close_request(self, *_args: object) -> bool:
        self._closed = True
        self._load_generation += 1
        self._stop_video()
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        return False


def _read_exif_details(item: MediaItem) -> list[tuple[str, str]]:
    """读取相机、曝光等 EXIF 字段（失败时静默返回空列表）。"""
    try:
        import gi

        gi.require_version("GExiv2", "0.10")
        from gi.repository import GExiv2  # type: ignore

        GExiv2.log_set_level(GExiv2.LogLevel.MUTE)
    except (ImportError, ValueError):
        return []
    try:
        metadata = GExiv2.Metadata()
        metadata.open_path(str(item.path))
    except Exception:  # noqa: BLE001
        return []

    available_tags: set[str] = set()
    for getter in ("get_exif_tags", "get_xmp_tags", "get_iptc_tags"):
        try:
            available_tags.update(getattr(metadata, getter)())
        except Exception:  # noqa: BLE001
            pass

    def tag(name: str) -> str:
        if name not in available_tags:
            return ""
        try:
            value = metadata.get_tag_string(name)
        except Exception:  # noqa: BLE001
            return ""
        return (value or "").strip().strip("\x00")

    rows: list[tuple[str, str]] = []
    make = tag("Exif.Photo.Make") or tag("Exif.Image.Make")
    model = tag("Exif.Photo.Model") or tag("Exif.Image.Model")
    camera = " ".join(part for part in (make, model) if part)
    if camera:
        rows.append(("相机", camera))
    lens = tag("Exif.Photo.LensModel")
    if lens:
        rows.append(("镜头", lens))

    fnumber = _rational(tag("Exif.Photo.FNumber"))
    if fnumber:
        rows.append(("光圈", f"f/{fnumber:.1f}"))
    exposure = _rational(tag("Exif.Photo.ExposureTime"))
    if exposure:
        if exposure >= 1:
            rows.append(("快门", f"{exposure:.1f} 秒"))
        else:
            rows.append(("快门", f"1/{round(1 / exposure)} 秒"))
    iso = tag("Exif.Photo.ISOSpeedRatings")
    if iso:
        rows.append(("ISO", iso))
    focal = _rational(tag("Exif.Photo.FocalLength"))
    if focal:
        rows.append(("焦距", f"{focal:.0f} mm"))

    software = tag("Exif.Image.Software")
    if software:
        rows.append(("软件", software))
    return rows


def _rational(text: str) -> float | None:
    if not text:
        return None
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            value = float(numerator) / float(denominator or 1)
        else:
            value = float(text)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None
