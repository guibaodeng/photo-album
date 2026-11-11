"""照片墙中的单张照片/视频卡片。

卡片是自绘的 ``Gtk.Widget``：圆角裁切、等比裁切填充、悬停高亮、
视频播放标记、时长胶囊以及键盘焦点环都在这一层完成。等高行布局
需要每张卡片具备精确的宽高，因此卡片支持 ``set_fixed_size``，由
布局器按图片宽高比分配尺寸。
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Graphene, Gsk, Gtk, Pango  # noqa: E402

from . import theme  # noqa: E402
from .models import MediaItem  # noqa: E402

_TILE_RADIUS = 12
_MIN_HEIGHT = 56


class MediaTile(Gtk.Widget):
    """单张照片 / 视频卡片。"""

    def __init__(
        self,
        item: MediaItem,
        on_activate: Callable[[MediaItem], None],
        on_context_menu: Callable[["MediaTile", float, float], None] | None = None,
        on_delete: Callable[[MediaItem], None] | None = None,
    ) -> None:
        super().__init__()
        self.item = item
        self._on_activate = on_activate
        self._on_context_menu = on_context_menu
        self._on_delete = on_delete
        self._fixed_size: tuple[int, int] | None = None
        self._texture: Gdk.Texture | None = None
        self._hovered = False
        self._failed = False
        self._preferred_width = 260
        self._duration_layout: Pango.Layout | None = None
        self._icon_texture = theme.placeholder_texture(
            theme.is_dark(),
            (1.0, 1.0, 1.0, 0.30) if theme.is_dark() else (0.10, 0.10, 0.12, 0.35),
        )

        self.set_hexpand(True)
        self.set_valign(Gtk.Align.START)
        self.set_focusable(True)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.set_tooltip_text(item.tooltip_text())

        if item.is_video:
            self._duration_layout = self.create_pango_layout(item.duration_text)
            description = Pango.FontDescription.from_string("Sans 9")
            self._duration_layout.set_font_description(description)

        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_click)
        self.add_controller(click)

        context = Gtk.GestureClick()
        context.set_button(Gdk.BUTTON_SECONDARY)
        context.connect("pressed", self._on_context_click)
        self.add_controller(context)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_enter)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        self.connect("notify::has-focus", lambda *_: self.queue_draw())

    # ------------------------------------------------------------------ 状态
    @property
    def has_texture(self) -> bool:
        return self._texture is not None

    def set_texture(self, texture: Gdk.Texture | None) -> None:
        self._texture = texture
        self._failed = False
        self.queue_draw()

    def clear_texture(self) -> None:
        if self._texture is not None:
            self._texture = None
            self.queue_draw()

    def mark_failed(self) -> None:
        self._failed = True
        self.queue_draw()

    def set_preferred_width(self, width: int) -> None:
        if width > 0 and width != self._preferred_width:
            self._preferred_width = width
            self.queue_resize()

    def set_fixed_size(self, width: int, height: int) -> None:
        """由等高行布局器指定精确的卡片尺寸。"""
        size = (max(24, int(width)), max(24, int(height)))
        if self._fixed_size == size:
            return
        self._fixed_size = size
        self.set_hexpand(False)
        self.set_size_request(*size)
        self.queue_resize()

    # ------------------------------------------------------------------ 尺寸
    def height_for_width(self, width: int) -> int:
        if self._fixed_size is not None:
            return self._fixed_size[1]
        return max(_MIN_HEIGHT, int(round(width / self.item.aspect)))

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        if self._fixed_size is not None:
            width, height = self._fixed_size
            if orientation == Gtk.Orientation.VERTICAL:
                return height, height, -1, -1
            return width, width, -1, -1
        if orientation == Gtk.Orientation.VERTICAL:
            width = for_size if for_size > 0 else self._preferred_width
            height = self.height_for_width(width)
            return height, height, -1, -1
        return 0, self._preferred_width, -1, -1

    # ------------------------------------------------------------------ 交互
    def _on_click(self, gesture: Gtk.GestureClick, _presses: int, _x: float, _y: float) -> None:
        if gesture.get_current_button() == 1:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._activate()

    def _on_context_click(self, gesture: Gtk.GestureClick, _presses: int, x: float, y: float) -> None:
        if gesture.get_current_button() != Gdk.BUTTON_SECONDARY:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.grab_focus()
        if self._on_context_menu is not None:
            self._on_context_menu(self, x, y)

    def _on_enter(self, _motion: Gtk.EventControllerMotion, _x: float, _y: float) -> None:
        self._hovered = True
        self.queue_draw()

    def _on_leave(self, _motion: Gtk.EventControllerMotion) -> None:
        self._hovered = False
        self.queue_draw()

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self._activate()
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            if self._on_delete is not None:
                self._on_delete(self.item)
            return True
        return False

    def _activate(self) -> None:
        try:
            self._on_activate(self.item)
        except Exception:  # noqa: BLE001 - 回调失败不应影响绘制
            pass

    # ------------------------------------------------------------------ 绘制
    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return

        radius = min(_TILE_RADIUS, width / 2, height / 2)
        focused = self.has_focus()
        if focused:
            outer = Gsk.RoundedRect()
            outer.init_from_rect(Graphene.Rect().init(0, 0, width, height), radius)
            snapshot.push_rounded_clip(outer)
            snapshot.append_color(theme.accent_color(self), Graphene.Rect().init(0, 0, width, height))
            snapshot.pop()
            inset = 2.0
            self._draw_content(snapshot, inset, inset, width - inset * 2, height - inset * 2, max(radius - inset, 0))
        else:
            self._draw_content(snapshot, 0, 0, width, height, radius)

        self._draw_badges(snapshot, width, height, radius)

    def _draw_content(self, snapshot: Gtk.Snapshot, x: float, y: float, width: float, height: float, radius: float) -> None:
        rect = Graphene.Rect().init(x, y, width, height)
        clip = Gsk.RoundedRect()
        clip.init_from_rect(rect, radius)
        snapshot.push_rounded_clip(clip)
        snapshot.append_color(_background_color(), rect)

        if self._texture is not None:
            self._draw_texture_cover(snapshot, self._texture, rect)
        else:
            icon_size = min(46.0, width * 0.4, height * 0.4)
            icon_rect = Graphene.Rect().init(
                x + (width - icon_size) / 2,
                y + (height - icon_size) / 2,
                icon_size,
                icon_size,
            )
            icon = theme.broken_texture(theme.is_dark()) if self._failed else self._icon_texture
            snapshot.append_texture(icon, icon_rect)

        if self._hovered and not self.has_focus():
            snapshot.append_color(_hover_color(), rect)
        snapshot.pop()

    def _draw_texture_cover(self, snapshot: Gtk.Snapshot, texture: Gdk.Texture, rect: Graphene.Rect) -> None:
        texture_width = texture.get_width()
        texture_height = texture.get_height()
        if texture_width <= 0 or texture_height <= 0:
            return
        scale = max(rect.size.width / texture_width, rect.size.height / texture_height)
        draw_width = texture_width * scale
        draw_height = texture_height * scale
        snapshot.save()
        snapshot.translate(
            Graphene.Point().init(
                rect.origin.x + (rect.size.width - draw_width) / 2,
                rect.origin.y + (rect.size.height - draw_height) / 2,
            )
        )
        snapshot.scale(scale, scale)
        snapshot.append_texture(texture, Graphene.Rect().init(0, 0, texture_width, texture_height))
        snapshot.restore()

    def _draw_badges(self, snapshot: Gtk.Snapshot, width: float, height: float, radius: float) -> None:
        if not self.item.is_video:
            return

        # 中央播放按钮
        circle_size = max(30.0, min(52.0, min(width, height) * 0.22))
        center_x = width / 2
        center_y = height / 2
        circle = Graphene.Rect().init(
            center_x - circle_size / 2,
            center_y - circle_size / 2,
            circle_size,
            circle_size,
        )
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(circle, circle_size / 2)
        snapshot.push_rounded_clip(rounded)
        snapshot.append_color(Gdk.RGBA(0, 0, 0, 0.38), circle)
        snapshot.pop()
        icon_size = circle_size * 0.9
        snapshot.append_texture(
            theme.play_texture(),
            Graphene.Rect().init(center_x - icon_size / 2, center_y - icon_size / 2, icon_size, icon_size),
        )

        # 时长胶囊
        if self._duration_layout is not None and self.item.duration > 0:
            text_width, text_height = self._duration_layout.get_pixel_size()
            padding_x = 7.0
            padding_y = 3.0
            badge_width = text_width + padding_x * 2
            badge_height = text_height + padding_y * 2
            badge_rect = Graphene.Rect().init(
                width - badge_width - 8,
                height - badge_height - 8,
                badge_width,
                badge_height,
            )
            badge = Gsk.RoundedRect()
            badge.init_from_rect(badge_rect, min(7.0, badge_height / 2))
            snapshot.push_rounded_clip(badge)
            snapshot.append_color(Gdk.RGBA(0, 0, 0, 0.62), badge_rect)
            snapshot.pop()
            snapshot.save()
            snapshot.translate(Graphene.Point().init(badge_rect.origin.x + padding_x, badge_rect.origin.y + padding_y))
            snapshot.append_layout(self._duration_layout, Gdk.RGBA(1, 1, 1, 0.95))
            snapshot.restore()


def _background_color() -> Gdk.RGBA:
    return Gdk.RGBA(1, 1, 1, 0.055) if theme.is_dark() else Gdk.RGBA(0, 0, 0, 0.045)


def _hover_color() -> Gdk.RGBA:
    return Gdk.RGBA(1, 1, 1, 0.07) if theme.is_dark() else Gdk.RGBA(0, 0, 0, 0.05)
