"""照片墙布局。

照片按原始宽高比排成「等高行」：同一行高度一致，卡片宽度按比例分配，
行宽铺满可用宽度（与 Windows 11 照片的排版一致）。

支持两种模式：

* 显示月份标题：每个月标题独立成行，照片在该月内成行排列；
* 不显示月份标题：所有照片首尾相接，跨越月份连续成行，形成一整片照片墙。

缩略图按行懒加载；离开视口较远的纹理会被释放，过期的解码任务会被取消。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from gi.repository import GLib, Gdk, Gtk

from .media_tile import MediaTile
from .models import MediaItem
from .thumbnails import ThumbnailRequest, ThumbnailService

_ROW_GAP = 10
_MARGIN_TOP = 8
_MARGIN_BOTTOM = 56
_MARGIN_X = 22
_RELEASE_MARGINS = 3.0
#: 最后一行至少填充这个比例、且拉伸后高度不超过目标行高的这个倍数时才铺满，
#: 否则保持目标行高、左对齐（避免几张竖图被拉成巨大的方块）。
_LAST_ROW_FILL_RATIO = 0.72
_MAX_LAST_ROW_SCALE = 1.45


def _split_widths(aspects: list[float], available: float) -> list[int]:
    """按宽高比把一行像素宽度拆成整数，并保证总和精确等于 available。"""
    if not aspects:
        return []
    total = max(1.0, float(available))
    floats = [max(1.0, aspect) for aspect in aspects]
    scale = total / sum(floats)
    floats = [value * scale for value in floats]
    widths = [int(math.floor(value)) for value in floats]
    remainder = int(round(total)) - sum(widths)
    order = sorted(range(len(floats)), key=lambda index: floats[index] - widths[index], reverse=True)
    for offset in range(max(0, remainder)):
        widths[order[offset % len(widths)]] += 1
    while sum(widths) > int(round(total)) and len(widths) > 1:
        widest = max(range(len(widths)), key=lambda index: widths[index])
        if widths[widest] <= 24:
            break
        widths[widest] -= 1
    return widths


@dataclass(slots=True)
class _Row:
    widget: Gtk.Box
    tiles: list[MediaTile]
    month: tuple[int, int]
    height: int
    top: float = 0.0


@dataclass(slots=True)
class _RowPlan:
    tiles: list[MediaTile]
    widths: list[int]
    height: int


@dataclass(slots=True)
class _Anchor:
    year: int
    month: int
    y: float


class Gallery(Gtk.Box):
    """照片墙（等高行，可选月份标题）。"""

    def __init__(
        self,
        thumbnails: ThumbnailService,
        on_activate: Callable[[MediaItem], None],
        on_context_menu: Callable[[MediaTile, float, float], None] | None = None,
        on_delete: Callable[[MediaItem], None] | None = None,
        on_month_click: Callable[[tuple[int, int], Gtk.Widget], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=_ROW_GAP)
        self.set_margin_top(_MARGIN_TOP)
        self.set_margin_bottom(_MARGIN_BOTTOM)
        self.set_margin_start(_MARGIN_X)
        self.set_margin_end(_MARGIN_X)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.START)
        self.set_hexpand(True)
        # 内容为空时 Gtk.Stack 会按 0 自然宽度分配页面；给一个最小宽度，
        # 让 ScrolledWindow/Stack 能正常把视口宽度传下来。
        self.set_size_request(1, -1)

        self.on_layout_changed: Callable[[], None] | None = None
        self._thumbnails = thumbnails
        self._on_activate = on_activate
        self._on_context_menu = on_context_menu
        self._on_delete = on_delete
        self._on_month_click = on_month_click

        self._items: list[MediaItem] = []
        self._side_inset = 0
        self._tiles: dict[Path, MediaTile] = {}
        self._rows: list[_Row] = []
        self._month_headers: dict[tuple[int, int], Gtk.Widget] = {}
        self._anchors: list[_Anchor] = []
        self._row_height = 220
        self._show_headers = True
        self._geometry_valid = False
        self._geometry_retry = 0
        self._relayout_source = 0
        self._last_width = 0
        self._generation = 0

        self._requested: set[Path] = set()
        self._loaded: set[Path] = set()
        self._wanted: set[Path] = set()
        self._tasks: dict[Path, ThumbnailRequest] = {}

        # GTK4 的 GtkWidget 不会在尺寸变化时发出可用的 width 通知，
        # 这里用低频轮询检查宽度，保证窗口缩放时照片墙重新排版。
        GLib.timeout_add(110, self._check_width)

    # ------------------------------------------------------------------ 状态
    @property
    def row_height(self) -> int:
        return self._row_height

    def set_row_height(self, height: int) -> None:
        height = max(100, min(600, int(height)))
        if height != self._row_height:
            self._row_height = height
            self._relayout_now()

    def set_headers_visible(self, visible: bool) -> None:
        if bool(visible) != self._show_headers:
            self._show_headers = bool(visible)
            self._relayout_now()

    def set_side_inset(self, inset: int) -> None:
        """在照片墙右侧预留透明区域（给浮动时间轴），不改变控件宽度。"""
        inset = max(0, int(inset))
        if inset != self._side_inset:
            self._side_inset = inset
            self._reflow_now()

    @property
    def headers_visible(self) -> bool:
        return self._show_headers

    @property
    def item_count(self) -> int:
        return len(self._items)

    def set_items(self, items: Iterable[MediaItem]) -> None:
        self._generation += 1
        self._cancel_all_tasks()
        self._requested.clear()
        self._loaded.clear()
        self._wanted.clear()
        self._tiles.clear()
        self._rows.clear()
        self._anchors.clear()
        self._month_headers.clear()
        self._items = list(items)

        for tile in self._all_tile_widgets():
            tile.unparent()
        child = self.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            child.unparent()
            child = next_child

        for item in self._items:
            tile = MediaTile(item, self._on_activate, self._on_context_menu, self._on_delete)
            self._tiles[item.path] = tile

        self._geometry_valid = False
        self._relayout_now()
        GLib.idle_add(self._relayout_after_idle)
        GLib.timeout_add(120, self._relayout_after_timeout)

    def update_items(self, items: Iterable[MediaItem]) -> None:
        """渐进扫描时更新条目，复用已存在卡片的纹理，只创建新增卡片。"""
        incoming = list(items)
        paths = {item.path for item in incoming}
        for path in list(self._tiles):
            if path in paths:
                continue
            task = self._tasks.pop(path, None)
            if task is not None:
                task.cancel()
            self._requested.discard(path)
            self._loaded.discard(path)
            self._wanted.discard(path)
            tile = self._tiles.pop(path)
            tile.unparent()
        for item in incoming:
            if item.path in self._tiles:
                continue
            self._tiles[item.path] = MediaTile(
                item,
                self._on_activate,
                self._on_context_menu,
                self._on_delete,
            )
        self._items = incoming
        self._reflow_now()

    def remove_item(self, item: MediaItem) -> bool:
        """从照片墙移除一条媒体（对应文件已被删除），保留其他已加载纹理。"""
        if item.path not in self._tiles:
            return False
        self._generation += 1
        task = self._tasks.pop(item.path, None)
        if task is not None:
            task.cancel()
        self._requested.discard(item.path)
        self._loaded.discard(item.path)
        self._wanted.discard(item.path)
        tile = self._tiles.pop(item.path)
        tile.unparent()
        self._items = [entry for entry in self._items if entry.path != item.path]
        self._reflow_now()
        return True

    def _all_tile_widgets(self) -> list[MediaTile]:
        return list(self._tiles.values())

    def clear(self) -> None:
        self.set_items([])

    # ------------------------------------------------------------------ 布局
    def _available_width(self) -> int:
        width = self.get_width()
        if width > 1:
            return max(1, width - self._side_inset)
        parent = self.get_parent()
        if parent is not None and parent.get_width() > 1:
            return max(
                1,
                parent.get_width()
                - self.get_margin_start()
                - self.get_margin_end()
                - self._side_inset,
            )
        return 0

    def _check_width(self) -> bool:
        if not self.get_mapped():
            return True
        width = self._available_width()
        if width > 1 and abs(width - self._last_width) > 1:
            self._last_width = width
            self._relayout_now()
        return True

    def _relayout_after_idle(self) -> bool:
        self._relayout_now()
        return False

    def _relayout_after_timeout(self) -> bool:
        self._relayout_now()
        return False

    def _schedule_geometry_retry(self) -> None:
        if self._geometry_retry:
            return
        self._geometry_retry = GLib.timeout_add(40, self._geometry_retry_idle)

    def _geometry_retry_idle(self) -> bool:
        self._geometry_retry = 0
        self._ensure_geometry()
        GLib.idle_add(self._notify_layout)
        return False

    def _relayout_now(self) -> None:
        width = self._available_width()
        if not self._items:
            for tile in self._tiles.values():
                tile.unparent()
            child = self.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                child.unparent()
                child = next_child
            self._rows = []
            self._month_headers = {}
            self._anchors = []
            self._geometry_valid = True
            return
        if width <= 1:
            self._geometry_valid = False
            return
        self._last_width = width

        # 旧行/标题全部拆掉，卡片控件复用。
        for tile in self._tiles.values():
            tile.unparent()
        child = self.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            child.unparent()
            child = next_child

        self._rows = []
        self._month_headers = {}
        self._anchors = []

        if self._show_headers:
            for (year, month), group in itertools.groupby(
                self._items, key=lambda item: item.year_month
            ):
                group_items = list(group)
                header = self._make_month_header(year, month, group_items)
                self.append(header)
                self._month_headers[(year, month)] = header
                tiles = [self._tiles[item.path] for item in group_items]
                self._build_rows(tiles, width, group_last_fill=True)
        else:
            tiles = [self._tiles[item.path] for item in self._items]
            self._build_rows(tiles, width, group_last_fill=False)

        self._geometry_valid = False
        GLib.idle_add(self._notify_layout)
        GLib.timeout_add(30, self._ensure_geometry)

    def _plan_rows(
        self, tiles: list[MediaTile], width: int, *, group_last_fill: bool
    ) -> list[_RowPlan]:
        """计算一组的等高行方案，不创建/移动任何控件。"""
        plans: list[list[tuple[MediaTile, float]]] = []
        current: list[tuple[MediaTile, float]] = []
        aspect_sum = 0.0
        target = float(self._row_height)
        for tile in tiles:
            aspect = max(0.1, tile.item.aspect)
            current.append((tile, aspect))
            aspect_sum += aspect
            natural_width = aspect_sum * target + _ROW_GAP * (len(current) - 1)
            if natural_width >= width:
                plans.append(current)
                current = []
                aspect_sum = 0.0
        if current:
            plans.append(current)

        result: list[_RowPlan] = []
        for index, row_tiles in enumerate(plans):
            aspects = [aspect for _tile, aspect in row_tiles]
            total_aspect = sum(aspects)
            count = len(row_tiles)
            available = width - _ROW_GAP * (count - 1)
            fill_height = available / max(total_aspect, 0.1)
            natural_width = total_aspect * target + _ROW_GAP * (count - 1)
            is_last = index == len(plans) - 1
            can_fill = natural_width >= width or (
                is_last
                and group_last_fill
                and natural_width >= width * _LAST_ROW_FILL_RATIO
                and fill_height <= target * _MAX_LAST_ROW_SCALE
            )
            row_height = fill_height if can_fill else target
            row_height = max(60.0, row_height)
            height_int = max(40, int(round(row_height)))
            widths = (
                _split_widths(aspects, available)
                if can_fill
                else [max(24, int(round(aspect * row_height))) for aspect in aspects]
            )
            result.append(
                _RowPlan(
                    tiles=[tile for tile, _aspect in row_tiles],
                    widths=widths,
                    height=height_int,
                )
            )
        return result

    def _fill_row(self, widget: Gtk.Box, plan: _RowPlan) -> None:
        child = widget.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            child.unparent()
            child = next_child
        for tile, tile_width in zip(plan.tiles, plan.widths):
            if tile.get_parent() is not None:
                tile.unparent()
            tile.set_fixed_size(tile_width, plan.height)
            widget.append(tile)

    def _make_row(self, plan: _RowPlan) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=_ROW_GAP)
        row.set_halign(Gtk.Align.FILL)
        row.set_valign(Gtk.Align.START)
        row.set_hexpand(True)
        self._fill_row(row, plan)
        return row

    def _build_rows(self, tiles: list[MediaTile], width: int, *, group_last_fill: bool) -> None:
        """把一组卡片按等高行排好并挂到 Gallery 上。"""
        for plan in self._plan_rows(tiles, width, group_last_fill=group_last_fill):
            row = self._make_row(plan)
            self.append(row)
            self._rows.append(
                _Row(
                    widget=row,
                    tiles=list(plan.tiles),
                    month=plan.tiles[0].item.year_month,
                    height=plan.height,
                )
            )

    def _reflow_now(self) -> None:
        """删除一条媒体后的增量重排。

        保留所有行控件，只移动卡片并重建月份标题，避免整棵照片墙被拆掉重建。
        行高度在同步操作期间保持稳定，ScrolledWindow 不会把 adjustment 重置到 0，
        因此删除后后面的照片会直接向前挤，而不是先跳到顶部再回来。
        """
        if not self._items:
            self._relayout_now()
            return
        width = self._available_width()
        if width <= 1:
            self._geometry_valid = False
            return
        self._last_width = width

        # 标题重建；行控件保留。
        for header in self._month_headers.values():
            header.unparent()
        self._month_headers.clear()

        groups: list[tuple[int, int, list[MediaItem], list[_RowPlan]]] = []
        if self._show_headers:
            for (year, month), group in itertools.groupby(
                self._items, key=lambda item: item.year_month
            ):
                group_items = list(group)
                tiles = [self._tiles[item.path] for item in group_items]
                groups.append(
                    (
                        year,
                        month,
                        group_items,
                        self._plan_rows(tiles, width, group_last_fill=True),
                    )
                )
        else:
            tiles = [self._tiles[item.path] for item in self._items]
            groups.append(
                (0, 0, self._items, self._plan_rows(tiles, width, group_last_fill=False))
            )

        flat_plans = [plan for _year, _month, _items, plans in groups for plan in plans]

        # 先把所有卡片从旧行中摘下，再同步填回；行控件本身不移除。
        for tile in self._tiles.values():
            if tile.get_parent() is not None:
                tile.unparent()

        old_rows = self._rows
        new_rows: list[_Row] = []
        for index, plan in enumerate(flat_plans):
            if index < len(old_rows):
                row = old_rows[index]
                widget = row.widget
                self._fill_row(widget, plan)
                row.tiles = list(plan.tiles)
                row.month = plan.tiles[0].item.year_month
                row.height = plan.height
            else:
                widget = self._make_row(plan)
                row = _Row(
                    widget=widget,
                    tiles=list(plan.tiles),
                    month=plan.tiles[0].item.year_month,
                    height=plan.height,
                )
                self.append(widget)
            new_rows.append(row)

        for row in old_rows[len(flat_plans):]:
            row.widget.unparent()

        self._rows = new_rows
        self._anchors = []
        self._geometry_valid = False

        if self._show_headers:
            row_cursor = 0
            previous: Gtk.Widget | None = None
            for year, month, group_items, plans in groups:
                if not plans:
                    continue
                header = self._make_month_header(year, month, group_items)
                if previous is None:
                    self.insert_child_after(header, None)
                else:
                    self.insert_child_after(header, previous)
                self._month_headers[(year, month)] = header
                previous = new_rows[row_cursor + len(plans) - 1].widget
                row_cursor += len(plans)

        GLib.idle_add(self._notify_layout)
        GLib.timeout_add(30, self._ensure_geometry)

    def _make_month_header(self, year: int, month: int, items: list[MediaItem]) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.set_valign(Gtk.Align.CENTER)
        header.set_margin_top(14)
        header.set_margin_bottom(2)

        title = Gtk.Button(label=f"{year}年{month}月")
        title.add_css_class("flat")
        title.add_css_class("section-title")
        title.add_css_class("month-title")
        title.set_halign(Gtk.Align.START)
        title.set_valign(Gtk.Align.CENTER)
        title.set_tooltip_text("打开月份导航")
        title.connect("clicked", lambda *_: self._month_clicked((year, month), title))
        header.append(title)

        photos = sum(1 for item in items if not item.is_video)
        videos = len(items) - photos
        parts: list[str] = []
        if photos:
            parts.append(f"{photos} 张照片")
        if videos:
            parts.append(f"{videos} 个视频")
        count = Gtk.Label(label=" · ".join(parts))
        count.add_css_class("section-count")
        count.set_xalign(0.0)
        header.append(count)

        line = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        line.set_hexpand(True)
        line.set_valign(Gtk.Align.CENTER)
        line.add_css_class("section-line")
        header.append(line)
        return header

    def _month_clicked(self, month: tuple[int, int], anchor: Gtk.Widget) -> None:
        if self._on_month_click is not None:
            self._on_month_click(month, anchor)

    def month_summary(self) -> list[tuple[tuple[int, int], int]]:
        """按当前排序汇总所有月份及数量，供月份导航使用。"""
        counts: dict[tuple[int, int], int] = {}
        for item in self._items:
            counts[item.year_month] = counts.get(item.year_month, 0) + 1
        return [(month, counts[month]) for month in self._month_order() if month in counts]

    # ------------------------------------------------------------------ 几何
    def _ensure_geometry(self) -> bool:
        if self._geometry_valid:
            return True
        if not self._rows:
            self._geometry_valid = True
            self._anchors = []
            return True

        margin_top = float(self.get_margin_top())
        anchors: list[_Anchor] = []
        seen_months: set[tuple[int, int]] = set()
        header_tops: dict[tuple[int, int], float] = {}
        for month in self._month_order():
            header = self._month_headers.get(month)
            if header is None:
                continue
            header_ok, header_rect = header.compute_bounds(self)
            if not header_ok or header_rect.size.height <= 0:
                self._geometry_valid = False
                self._schedule_geometry_retry()
                return False
            header_tops[month] = max(0.0, float(header_rect.origin.y) + margin_top - 4.0)

        for row in self._rows:
            ok, rect = row.widget.compute_bounds(self)
            if not ok or rect.size.height <= 0:
                self._geometry_valid = False
                self._schedule_geometry_retry()
                return False
            row.top = float(rect.origin.y) + margin_top
            # 连续模式下，一个月可能从某一行中间开始；锚点记录该月第一张
            # 照片所在行的顶部，这样时间线跳转仍然精确。
            for tile in row.tiles:
                month = tile.item.year_month
                if month in seen_months:
                    continue
                seen_months.add(month)
                anchor_y = header_tops.get(month, row.top)
                anchors.append(_Anchor(month[0], month[1], max(0.0, anchor_y)))
        self._anchors = anchors
        self._geometry_valid = True
        return True

    def _month_order(self) -> list[tuple[int, int]]:
        months: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for item in self._items:
            key = item.year_month
            if key not in seen:
                seen.add(key)
                months.append(key)
        return months

    def timeline_entries(self) -> list[tuple[int, int]]:
        return self._month_order()

    def active_month(self, scroll_y: float) -> tuple[int, int] | None:
        if not self._ensure_geometry() or not self._anchors:
            return None
        active = self._anchors[0]
        for anchor in self._anchors:
            if anchor.y <= scroll_y + 6:
                active = anchor
            else:
                break
        return active.year, active.month

    def jump_offset(self, year: int, month: int) -> float | None:
        if not self._ensure_geometry():
            return None
        for anchor in self._anchors:
            if anchor.year == year and anchor.month == month:
                return anchor.y
        return None

    # ------------------------------------------------------------------ 懒加载
    def update_visibility(
        self, scroll_y: float, viewport_height: float, *, allow_requests: bool = True
    ) -> None:
        if viewport_height <= 0 or not self._items:
            return
        if not self._ensure_geometry():
            return
        view_top = scroll_y
        view_bottom = scroll_y + viewport_height
        request_margin = viewport_height * 0.9
        keep_top = view_top - viewport_height * _RELEASE_MARGINS
        keep_bottom = view_bottom + viewport_height * _RELEASE_MARGINS
        wanted: set[Path] = set()

        for row in self._rows:
            top = row.top
            bottom = top + row.height
            if bottom > view_top - request_margin and top < view_bottom + request_margin:
                priority = 0 if (bottom > view_top and top < view_bottom) else 1
                for tile in row.tiles:
                    wanted.add(tile.item.path)
                    if allow_requests:
                        self._request_tile(tile, priority)
            elif bottom < keep_top or top > keep_bottom:
                for tile in row.tiles:
                    if tile.has_texture:
                        tile.clear_texture()
                    self._loaded.discard(tile.item.path)
                    self._requested.discard(tile.item.path)
                    task = self._tasks.pop(tile.item.path, None)
                    if task is not None:
                        task.cancel()

        self._wanted = wanted
        if not allow_requests:
            return
        for path, task in list(self._tasks.items()):
            if path not in wanted:
                task.cancel()
                del self._tasks[path]
                self._requested.discard(path)

    def _request_tile(self, tile: MediaTile, priority: int) -> None:
        path = tile.item.path
        if tile.has_texture or path in self._requested or path in self._loaded:
            return
        self._requested.add(path)
        generation = self._generation

        def deliver(item: MediaItem, data: bytes | None) -> None:
            if generation != self._generation:
                return
            if item.path not in self._wanted:
                self._requested.discard(item.path)
                self._tasks.pop(item.path, None)
                return
            self._requested.discard(item.path)
            self._tasks.pop(item.path, None)
            current = self._tiles.get(item.path)
            if current is None or current is not tile:
                return
            if data is None:
                current.mark_failed()
                return
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except Exception:  # noqa: BLE001
                current.mark_failed()
                return
            current.set_texture(texture)
            self._loaded.add(item.path)

        handle = self._thumbnails.request(tile.item, deliver, priority=priority)
        self._tasks[path] = handle

    def _cancel_all_tasks(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    def reload_textures(self) -> None:
        """清空内存中的纹理，等待重新生成（用于清理缓存后）。"""
        self._cancel_all_tasks()
        for tile in self._tiles.values():
            tile.clear_texture()
        self._loaded.clear()
        self._requested.clear()
        self._wanted.clear()

    # ------------------------------------------------------------------ 通知
    def _notify_layout(self) -> bool:
        if self.on_layout_changed is not None:
            try:
                self.on_layout_changed()
            except Exception:  # noqa: BLE001
                pass
        return False
