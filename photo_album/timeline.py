"""右侧透明时间轴。

时间轴是照片墙右侧的独立透明列：始终显示，父容器没有背景，
只绘制一根时间轴线、年份标签和月份刻度点。相册位于时间轴左侧，
不会延伸到时间轴下方。

每个「年月」对应一个刻度点：滚动时高亮当前月份、点击快速跳转；
悬停刻度会放大圆点，并浮出「年-月」标签。
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("PangoCairo", "1.0")

from gi.repository import Gtk, Pango, PangoCairo  # noqa: E402

from . import theme  # noqa: E402

_PADDING_TOP = 26.0
_PADDING_BOTTOM = 26.0


def _rounded_rect(cr, x: float, y: float, width: float, height: float, radius: float) -> None:
    radius = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -1.5707963267948966, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, 1.5707963267948966)
    cr.arc(x + radius, y + height - radius, radius, 1.5707963267948966, 3.141592653589793)
    cr.arc(x + radius, y + radius, radius, 3.141592653589793, 4.71238898038469)
    cr.close_path()


class Timeline(Gtk.DrawingArea):
    """右侧年月时间线。"""

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(76, -1)
        self.set_hexpand(False)
        self.set_vexpand(True)
        self.add_css_class("timeline")
        self.set_draw_func(self._draw)
        self.set_tooltip_text("点击跳转到对应月份")

        self._entries: list[tuple[int, int]] = []
        self._active: tuple[int, int] | None = None
        self._hovered = -1
        self._on_jump: Callable[[int, int], None] | None = None

        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

    # ------------------------------------------------------------------ API
    def set_entries(self, entries: list[tuple[int, int]]) -> None:
        if entries == self._entries:
            return
        self._entries = list(entries)
        if self._active not in self._entries:
            self._active = self._entries[0] if self._entries else None
        self.queue_draw()

    def set_active(self, year: int, month: int) -> None:
        value = (year, month)
        if value != self._active:
            self._active = value
            self.queue_draw()

    def set_on_jump(self, callback: Callable[[int, int], None]) -> None:
        self._on_jump = callback


    # ------------------------------------------------------------------ 交互
    def _geometry(self, height: int) -> tuple[float, float]:
        count = max(1, len(self._entries))
        usable = max(1.0, height - _PADDING_TOP - _PADDING_BOTTOM)
        return usable / count, _PADDING_TOP

    def _index_at(self, y: float, height: int) -> int:
        if not self._entries:
            return -1
        slot, top = self._geometry(height)
        index = int((y - top) // slot)
        if index < 0:
            index = 0
        if index >= len(self._entries):
            index = len(self._entries) - 1
        return index

    def _index_near(self, y: float, height: int) -> int:
        """悬停时只有靠近刻度点才高亮，避免整条时间线都在闪。"""
        index = self._index_at(y, height)
        if index < 0:
            return -1
        slot, top = self._geometry(height)
        center_y = top + (index + 0.5) * slot
        if abs(y - center_y) > max(10.0, min(slot * 0.4, 18.0)):
            return -1
        return index

    def _on_click(self, gesture: Gtk.GestureClick, _presses: int, _x: float, y: float) -> None:
        if gesture.get_current_button() != 1:
            return
        index = self._index_at(y, self.get_height())
        if index < 0 or index >= len(self._entries):
            return
        if self._on_jump is not None:
            year, month = self._entries[index]
            self._active = (year, month)
            self.queue_draw()
            self._on_jump(year, month)

    def _on_motion(self, _motion: Gtk.EventControllerMotion, _x: float, y: float) -> None:
        index = self._index_near(y, self.get_height())
        if index != self._hovered:
            self._hovered = index
            self.queue_draw()

    def _on_leave(self, _motion: Gtk.EventControllerMotion) -> None:
        self._hovered = -1
        self.queue_draw()

    # ------------------------------------------------------------------ 绘制
    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        if width <= 0 or height <= 0 or not self._entries:
            return
        foreground = theme.foreground_color(self)
        accent = theme.accent_color(self)
        dark = theme.is_dark()

        slot, top = self._geometry(height)
        rail_x = width - 10.0
        bottom = height - _PADDING_BOTTOM

        # 只有一根时间轴线：为了在照片上仍然看得清，先画一层描边底色，
        # 再画细线。父容器保持完全透明，不显示卡片背景。
        halo = (0.0, 0.0, 0.0, 0.24) if dark else (1.0, 1.0, 1.0, 0.70)
        cr.set_line_cap(1)
        cr.set_line_width(4.0)
        cr.set_source_rgba(*halo)
        cr.move_to(rail_x, top)
        cr.line_to(rail_x, bottom)
        cr.stroke()
        cr.set_line_width(2.0)
        cr.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.82)
        cr.move_to(rail_x, top)
        cr.line_to(rail_x, bottom)
        cr.stroke()

        radius_base = min(2.8, max(1.4, slot * 0.30))
        for index, (year, month) in enumerate(self._entries):
            center_y = top + (index + 0.5) * slot
            is_active = self._active == (year, month)
            is_hovered = index == self._hovered
            if is_hovered:
                radius = max(5.0, radius_base + 2.4)
            elif is_active:
                radius = 4.6
            else:
                radius = radius_base
            # 圆点描边，避免落在浅色/深色照片上看不见
            cr.set_source_rgba(*halo)
            cr.arc(rail_x, center_y, radius + 1.2, 0, 6.283185307179586)
            cr.fill()
            if is_hovered:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.30)
                cr.arc(rail_x, center_y, radius + 5.0, 0, 6.283185307179586)
                cr.fill()
            if is_hovered or is_active:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 1.0)
            elif month == 1:
                cr.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.78)
            else:
                cr.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.48)
            cr.arc(rail_x, center_y, radius, 0, 6.283185307179586)
            cr.fill()

        # 每个年份一个文字标签，放在该年份占据的时间段中间。
        ranges: list[list[int]] = []
        for index, (year, _month) in enumerate(self._entries):
            if not ranges or ranges[-1][0] != year:
                ranges.append([year, index, index])
            else:
                ranges[-1][2] = index
        active_year = self._active[0] if self._active is not None else None
        placed: list[float] = []
        for year, first, last in ranges:
            center_y = top + ((first + last + 1) * 0.5) * slot
            label = self.create_pango_layout(str(year))
            label.set_font_description(
                Pango.FontDescription.from_string(
                    "Sans Bold 9" if year == active_year else "Sans 9"
                )
            )
            text_width, text_height = label.get_pixel_size()
            if any(abs(center_y - other) < text_height + 5 for other in placed):
                if year != active_year:
                    continue
            placed.append(center_y)
            x = rail_x - 6.0 - text_width
            y = max(2.0, min(center_y - text_height / 2, height - 2.0 - text_height))
            if year == active_year:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.95)
            else:
                cr.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.48)
            cr.move_to(x, y)
            PangoCairo.update_layout(cr, label)
            PangoCairo.show_layout(cr, label)

        # 悬停时在时间轴左侧浮出「年-月」标签；没有悬停时不绘制任何文字。
        if 0 <= self._hovered < len(self._entries):
            year, month = self._entries[self._hovered]
            label = self.create_pango_layout(f"{year}年{month}月")
            label.set_font_description(Pango.FontDescription.from_string("Sans Bold 8"))
            text_width, text_height = label.get_pixel_size()
            pad_x = 6.0
            pad_y = 3.0
            pill_width = text_width + pad_x * 2
            pill_height = text_height + pad_y * 2
            center_y = top + (self._hovered + 0.5) * slot
            pill_x = max(2.0, rail_x - 6.0 - pill_width)
            pill_y = center_y - pill_height / 2
            _rounded_rect(cr, pill_x, pill_y, pill_width, pill_height, 8.0)
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.96)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.98)
            cr.move_to(pill_x + pad_x, pill_y + pad_y)
            PangoCairo.update_layout(cr, label)
            PangoCairo.show_layout(cr, label)
