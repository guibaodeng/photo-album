"""编辑照片 EXIF 信息的对话框。"""

from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from .models import MediaItem  # noqa: E402
from .scanner import EXIF_EDITOR_MARKER  # noqa: E402

try:  # GExiv2 是可选依赖。
    import gi as _gi

    _gi.require_version("GExiv2", "0.10")
    from gi.repository import GExiv2  # type: ignore

    GExiv2.log_set_level(GExiv2.LogLevel.MUTE)
    _HAS_EXIV2 = True
except (ImportError, ValueError):  # pragma: no cover
    GExiv2 = None  # type: ignore
    _HAS_EXIV2 = False

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def _parse_date(text: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _format_date(text: str) -> str:
    if not text:
        return ""
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return text.strip()


def _rational_parts(text: str) -> tuple[int, int] | None:
    """把 2.8、1/125、0.008 解析成约分后的 (分子, 分母)。"""
    value = text.strip().lower().replace("f/", "").replace("mm", "").strip()
    if not value:
        return None
    try:
        if "/" in value:
            left, right = value.split("/", 1)
            numerator = int(left.strip())
            denominator = int(right.strip())
        else:
            number = float(value)
            if number <= 0:
                return None
            if number < 1:
                numerator, denominator = 1, int(round(1 / number))
            else:
                numerator, denominator = int(round(number * 100)), 100
        if numerator <= 0 or denominator <= 0:
            return None
        divisor = math.gcd(numerator, denominator)
        return numerator // divisor, denominator // divisor
    except (ValueError, ZeroDivisionError):
        return None


def _rational_text(raw: str, *, aperture: bool = False) -> str:
    if not raw:
        return ""
    try:
        if "/" in raw:
            numerator, denominator = raw.split("/", 1)
            number = float(numerator) / float(denominator or 1)
        else:
            number = float(raw)
    except (ValueError, ZeroDivisionError):
        return raw
    if aperture:
        return f"{number:.1f}".rstrip("0").rstrip(".")
    if 0 < number < 1:
        return f"1/{round(1 / number)}"
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


class ExifEditor(Adw.Dialog):
    """一个简单的 EXIF 编辑表单，保存后由主窗口重新读取文件。"""

    def __init__(self, item: MediaItem, on_saved: Callable[[], None]) -> None:
        super().__init__()
        self._item = item
        self._on_saved = on_saved
        self._metadata = None
        self._entries: dict[str, Adw.EntryRow] = {}
        self._original_date: datetime | None = None
        self._marker = False

        self.set_title("编辑 EXIF 信息")
        self.set_content_width(500)
        self.set_content_height(620)
        self._build()
        self._load()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="编辑 EXIF 信息", subtitle=self._item.name)
        header.set_title_widget(title)

        cancel = Gtk.Button(label="取消")
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        self._save_button = Gtk.Button(label="保存")
        self._save_button.add_css_class("suggested-action")
        self._save_button.connect("clicked", lambda *_: self._save())
        header.pack_end(self._save_button)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.set_title("拍摄信息")
        group.set_description("留空即可清除对应字段；时间格式如 2019-04-24 08:54:56。")

        self._error_row = Adw.ActionRow()
        self._error_row.add_css_class("error")
        self._error_row.set_visible(False)
        group.add(self._error_row)

        fields = (
            ("date", "拍摄时间"),
            ("make", "相机厂商"),
            ("model", "相机型号"),
            ("lens", "镜头"),
            ("aperture", "光圈"),
            ("shutter", "快门"),
            ("iso", "ISO"),
            ("focal", "焦距 (mm)"),
            ("description", "说明"),
        )
        for key, label in fields:
            row = Adw.EntryRow()
            row.set_title(label)
            group.add(row)
            self._entries[key] = row
        page.add(group)
        toolbar.set_content(page)
        self.set_child(toolbar)

    # ------------------------------------------------------------------ 读取
    def _tag(self, *names: str) -> str:
        if self._metadata is None:
            return ""
        for name in names:
            try:
                if not self._metadata.has_tag(name):
                    continue
                value = self._metadata.get_tag_string(name)
                if value:
                    return value.strip().strip("\x00")
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _load(self) -> None:
        if not _HAS_EXIV2:
            self._show_error("系统未安装 GExiv2，无法编辑 EXIF。")
            self._save_button.set_sensitive(False)
            return
        try:
            metadata = GExiv2.Metadata()
            metadata.open_path(os.fspath(self._item.path))
        except Exception as error:  # noqa: BLE001
            self._show_error(f"无法读取 EXIF：{error}")
            self._save_button.set_sensitive(False)
            return
        self._metadata = metadata
        raw_date = self._tag(
            "Exif.Photo.DateTimeOriginal",
            "Exif.Photo.DateTimeDigitized",
            "Exif.Image.DateTimeOriginal",
            "Exif.Image.DateTime",
        )
        self._original_date = _parse_date(_format_date(raw_date))
        try:
            self._marker = (
                self._tag("Exif.Image.ProcessingSoftware") == EXIF_EDITOR_MARKER
            )
        except Exception:  # noqa: BLE001
            self._marker = False

        self._entries["date"].set_text(_format_date(raw_date))
        self._entries["make"].set_text(
            self._tag("Exif.Image.Make", "Exif.Photo.Make")
        )
        self._entries["model"].set_text(
            self._tag("Exif.Image.Model", "Exif.Photo.Model")
        )
        self._entries["lens"].set_text(self._tag("Exif.Photo.LensModel"))
        self._entries["aperture"].set_text(
            _rational_text(self._tag("Exif.Photo.FNumber"), aperture=True)
        )
        self._entries["shutter"].set_text(
            _rational_text(self._tag("Exif.Photo.ExposureTime"))
        )
        self._entries["iso"].set_text(self._tag("Exif.Photo.ISOSpeedRatings"))
        self._entries["focal"].set_text(
            _rational_text(self._tag("Exif.Photo.FocalLength"))
        )
        self._entries["description"].set_text(
            self._tag("Exif.Image.ImageDescription")
        )

    # ------------------------------------------------------------------ 保存
    def _show_error(self, message: str) -> None:
        self._error_row.set_title(message)
        self._error_row.set_visible(True)

    @staticmethod
    def _set_string(metadata, tag: str, value: str) -> None:
        value = value.strip()
        if value:
            metadata.set_tag_string(tag, value)
        else:
            try:
                metadata.clear_tag(tag)
            except Exception:  # noqa: BLE001
                pass

    def _save(self) -> None:
        if self._metadata is None:
            return
        self._error_row.set_visible(False)
        date_text = self._entries["date"].get_text().strip()
        parsed_date = _parse_date(date_text)
        if date_text and parsed_date is None:
            self._show_error("时间格式无效，请使用 YYYY-MM-DD HH:MM:SS。")
            return
        iso_text = self._entries["iso"].get_text().strip()
        try:
            iso = int(iso_text) if iso_text else None
        except ValueError:
            self._show_error("ISO 必须是整数。")
            return
        if iso is not None and iso <= 0:
            self._show_error("ISO 必须是正整数。")
            return
        aperture = _rational_parts(self._entries["aperture"].get_text())
        if self._entries["aperture"].get_text().strip() and aperture is None:
            self._show_error("光圈格式无效，例如 2.8 或 f/2.8。")
            return
        shutter = _rational_parts(self._entries["shutter"].get_text())
        if self._entries["shutter"].get_text().strip() and shutter is None:
            self._show_error("快门格式无效，例如 1/125 或 0.008。")
            return
        focal = _rational_parts(self._entries["focal"].get_text())
        if self._entries["focal"].get_text().strip() and focal is None:
            self._show_error("焦距格式无效，例如 50。")
            return

        metadata = self._metadata
        try:
            if parsed_date is None:
                metadata.clear_tag("Exif.Photo.DateTimeOriginal")
                metadata.clear_tag("Exif.Photo.DateTimeDigitized")
            else:
                exif_date = parsed_date.strftime("%Y:%m:%d %H:%M:%S")
                metadata.set_tag_string("Exif.Photo.DateTimeOriginal", exif_date)
                metadata.set_tag_string("Exif.Photo.DateTimeDigitized", exif_date)
            date_changed = parsed_date != self._original_date
            if date_changed or self._marker:
                metadata.set_tag_string(
                    "Exif.Image.ProcessingSoftware", EXIF_EDITOR_MARKER
                )

            self._set_string(metadata, "Exif.Image.Make", self._entries["make"].get_text())
            self._set_string(metadata, "Exif.Image.Model", self._entries["model"].get_text())
            self._set_string(metadata, "Exif.Photo.LensModel", self._entries["lens"].get_text())
            self._set_string(
                metadata,
                "Exif.Image.ImageDescription",
                self._entries["description"].get_text(),
            )

            for tag, parts in (
                ("Exif.Photo.FNumber", aperture),
                ("Exif.Photo.ExposureTime", shutter),
                ("Exif.Photo.FocalLength", focal),
            ):
                if parts is None:
                    metadata.clear_tag(tag)
                else:
                    metadata.try_set_exif_tag_rational(tag, parts[0], parts[1])

            if iso is None:
                metadata.clear_tag("Exif.Photo.ISOSpeedRatings")
            else:
                metadata.set_tag_long("Exif.Photo.ISOSpeedRatings", iso)

            metadata.save_file(os.fspath(self._item.path))
        except Exception as error:  # noqa: BLE001
            self._show_error(f"保存失败：{error}")
            return
        self._on_saved()
        self.close()
