"""核心逻辑单元测试（不依赖显示器）。

运行：``python3 -m unittest discover -s tests -v``
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

from photo_album.config import Config, ROW_HEIGHT_MAX
from photo_album.imaging import decode_image
from photo_album.models import (
    MAX_ASPECT,
    MIN_ASPECT,
    MediaItem,
    MediaKind,
    format_duration,
    format_size,
)
from photo_album.scanner import (
    EXIF_EDITOR_MARKER,
    LibraryScanner,
    _parse_filename_datetime,
    read_media_item,
)
from photo_album.thumbnails import ThumbnailCache
from photo_album.wall import _split_widths


def make_image(path: Path, size: tuple[int, int], color=(200, 120, 80)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG", quality=90)


class ConfigTests(unittest.TestCase):
    def test_sanitize_clamps_values(self) -> None:
        config = Config(row_height=9999, window_width=1, window_height=99999, excluded_dirs=["", ".git"])
        config.sanitize()
        self.assertEqual(config.row_height, ROW_HEIGHT_MAX)
        self.assertGreaterEqual(config.window_width, 860)
        self.assertLessEqual(config.window_height, 6000)
        self.assertEqual(config.excluded_dirs, [".git"])

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = Config(
                roots=["/tmp/photos", "/tmp/other"],
                recursive=False,
                row_height=300,
                video_autoplay=False,
                show_date_headers=False,
            )
            original.save(path)
            loaded = Config.load(path)
            self.assertEqual(loaded.roots, ["/tmp/photos", "/tmp/other"])
            self.assertFalse(loaded.recursive)
            self.assertEqual(loaded.row_height, 300)
            self.assertFalse(loaded.video_autoplay)
            self.assertFalse(loaded.show_date_headers)
            self.assertEqual(loaded.library_label, "2 个位置")

    def test_legacy_single_root_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"root": "/tmp/legacy"}', encoding="utf-8")
            loaded = Config.load(path)
            self.assertEqual(loaded.roots, ["/tmp/legacy"])

    def test_roots_are_deduplicated(self) -> None:
        config = Config(roots=["/photos", "/photos", " /other ", ""])
        config.sanitize()
        self.assertEqual(config.roots, ["/photos", "/other"])
        self.assertEqual(len(config.root_paths), 2)

    def test_invalid_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            loaded = Config.load(path)
            self.assertIsInstance(loaded, Config)
            self.assertTrue(loaded.recursive)

    def test_root_label_shortens_home(self) -> None:
        config = Config(roots=[str(Path.home() / "OneDrive" / "Pictures")])
        self.assertTrue(config.root_label.startswith("~/"))


class MediaItemTests(unittest.TestCase):
    def make_item(self, width=4000, height=3000, orientation=1, duration=0.0) -> MediaItem:
        return MediaItem(
            path=Path("/tmp/a.jpg"),
            kind=MediaKind.IMAGE,
            size=2048,
            mtime=0.0,
            taken_at=datetime(2024, 7, 15, 10, 30),
            width=width,
            height=height,
            orientation=orientation,
            duration=duration,
        )

    def test_aspect_respects_orientation(self) -> None:
        item = self.make_item(4000, 3000, orientation=6)
        self.assertAlmostEqual(item.aspect, 0.75, places=3)

    def test_aspect_is_clamped(self) -> None:
        self.assertEqual(self.make_item(10000, 100, orientation=1).aspect, MAX_ASPECT)
        self.assertEqual(self.make_item(100, 10000, orientation=1).aspect, MIN_ASPECT)

    def test_text_helpers(self) -> None:
        item = self.make_item()
        self.assertEqual(item.date_text, "2024年7月15日 10:30")
        self.assertEqual(item.month_text, "2024年7月")
        self.assertEqual(item.resolution_text, "4000 × 3000")
        self.assertEqual(format_duration(3671), "1:01:11")
        self.assertEqual(format_duration(59), "0:59")
        self.assertEqual(format_size(1536), "1.5 KB")

    def test_matches_searches_name_and_folder(self) -> None:
        item = MediaItem(
            path=Path("/tmp/旅行/2024/beach.jpg"),
            kind=MediaKind.IMAGE,
            size=1,
            mtime=0,
            taken_at=datetime(2024, 1, 1),
            width=100,
            height=100,
        )
        self.assertTrue(item.matches("beach"))
        self.assertTrue(item.matches("旅行"))
        self.assertFalse(item.matches("mountain"))


class FilenameDateTests(unittest.TestCase):
    def test_ios_and_android_filenames(self) -> None:
        self.assertEqual(
            _parse_filename_datetime("20190424_085456879_iOS.PNG"),
            datetime(2019, 4, 24, 8, 54, 56),
        )
        self.assertEqual(
            _parse_filename_datetime("PXL_20240101_123456789.jpg"),
            datetime(2024, 1, 1, 12, 34, 56),
        )
        self.assertEqual(
            _parse_filename_datetime("Screenshot_2024-01-01-12-34-56.png"),
            datetime(2024, 1, 1, 12, 34, 56),
        )

    def test_invalid_or_absent_dates_return_none(self) -> None:
        self.assertIsNone(_parse_filename_datetime("IMG_1234.JPG"))
        self.assertIsNone(_parse_filename_datetime("20191340_085456.jpg"))
        self.assertIsNone(_parse_filename_datetime("2024-02-30 10:00:00.jpg"))


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def test_recursive_and_non_recursive(self) -> None:
        make_image(self.root / "top.jpg", (800, 600))
        make_image(self.root / "sub" / "nested.jpg", (600, 800))
        make_image(self.root / ".hidden" / "hidden.jpg", (300, 300))
        make_image(self.root / ".dtrash" / "trash.jpg", (300, 300))

        shallow = LibraryScanner(self.root, recursive=False).scan()
        self.assertEqual([item.name for item in shallow.items], ["top.jpg"])

        deep = LibraryScanner(self.root, recursive=True, excluded_dirs=[".dtrash"]).scan()
        names = {item.name for item in deep.items}
        self.assertEqual(names, {"top.jpg", "nested.jpg"})

        with_hidden = LibraryScanner(
            self.root, recursive=True, include_hidden=True, excluded_dirs=[".dtrash"]
        ).scan()
        self.assertIn("hidden.jpg", {item.name for item in with_hidden.items})

    def test_reads_dimensions_and_sorts_newest_first(self) -> None:
        make_image(self.root / "a.jpg", (640, 480))
        make_image(self.root / "b.jpg", (480, 640))
        old = datetime(2020, 1, 2, 3, 4).timestamp()
        new = datetime(2023, 5, 6, 7, 8).timestamp()
        os.utime(self.root / "a.jpg", (old, old))
        os.utime(self.root / "b.jpg", (new, new))

        result = LibraryScanner(self.root).scan()
        self.assertEqual([item.name for item in result.items], ["b.jpg", "a.jpg"])
        self.assertEqual((result.items[0].width, result.items[0].height), (480, 640))
        self.assertEqual(result.items[0].taken_at.year, 2023)

    def test_filename_wins_when_exif_and_filename_disagree(self) -> None:
        path = self.root / "20190424_085456879_iOS.JPG"
        exif = Image.Exif()
        exif[36867] = "2026:11:06 09:40:56"
        Image.new("RGB", (320, 200), (180, 120, 80)).save(path, "JPEG", exif=exif)

        result = LibraryScanner(self.root).scan()
        item = next(item for item in result.items if item.name == path.name)
        self.assertEqual(item.taken_at, datetime(2019, 4, 24, 8, 54, 56))
        self.assertEqual(item.date_source, "filename")

    def test_exif_wins_when_exif_and_filename_agree_on_day(self) -> None:
        path = self.root / "20190424_085456879_iOS.JPG"
        exif = Image.Exif()
        exif[36867] = "2019:04:24 10:11:12"
        Image.new("RGB", (320, 200), (180, 120, 80)).save(path, "JPEG", exif=exif)

        result = LibraryScanner(self.root).scan()
        item = next(item for item in result.items if item.name == path.name)
        self.assertEqual(item.taken_at, datetime(2019, 4, 24, 10, 11, 12))
        self.assertEqual(item.date_source, "exif")

    def test_exif_date_edited_in_app_wins_over_filename(self) -> None:
        path = self.root / "20190424_085456879_iOS.JPG"
        exif = Image.Exif()
        exif[36867] = "2020:01:02 03:04:05"
        exif[11] = EXIF_EDITOR_MARKER
        Image.new("RGB", (320, 200), (180, 120, 80)).save(path, "JPEG", exif=exif)

        item = read_media_item(path)
        self.assertIsNotNone(item)
        self.assertEqual(item.taken_at, datetime(2020, 1, 2, 3, 4, 5))
        self.assertEqual(item.date_source, "exif")

    def test_filename_date_is_used_when_exif_is_missing(self) -> None:
        path = self.root / "20190424_085456879_iOS.PNG"
        Image.new("RGB", (320, 200), (180, 120, 80)).save(path, "PNG")
        copied = datetime(2026, 11, 6, 9, 40, 56).timestamp()
        os.utime(path, (copied, copied))

        result = LibraryScanner(self.root).scan()
        item = next(item for item in result.items if item.name == path.name)
        self.assertEqual(item.taken_at, datetime(2019, 4, 24, 8, 54, 56))
        self.assertEqual(item.date_source, "filename")

    def test_scan_streams_items_in_batches(self) -> None:
        for index in range(12):
            make_image(self.root / f"{index:02d}.jpg", (400, 300))
        streamed: list[MediaItem] = []
        result = LibraryScanner(self.root).scan(batch=streamed.extend)
        self.assertEqual(len(result.items), 12)
        self.assertEqual(len(streamed), 12)
        self.assertEqual({item.path for item in streamed}, {item.path for item in result.items})

    def test_missing_directory_returns_empty_result(self) -> None:
        result = LibraryScanner(self.root / "does-not-exist").scan()
        self.assertEqual(result.items, [])
        self.assertTrue(result.errors)

    def test_multiple_roots_and_overlap_are_deduplicated(self) -> None:
        second = self.root / "second"
        make_image(second / "one.jpg", (500, 400))
        make_image(self.root / "top.jpg", (400, 500))
        # 第一个根目录包含第二个，扫描结果不应重复。
        result = LibraryScanner([self.root, second]).scan()
        names = [item.name for item in result.items]
        self.assertEqual(sorted(names), ["one.jpg", "top.jpg"])


class ThumbnailCacheTests(unittest.TestCase):
    def make_item(self, path: Path, mtime: float = 100.0) -> MediaItem:
        return MediaItem(
            path=path,
            kind=MediaKind.IMAGE,
            size=1234,
            mtime=mtime,
            taken_at=datetime(2024, 1, 1),
            width=800,
            height=600,
        )

    def test_key_changes_with_file_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ThumbnailCache(Path(directory))
            item = self.make_item(Path("/tmp/photo.jpg"))
            key = cache.key(item)
            self.assertEqual(key, cache.key(item))
            item.mtime += 1
            self.assertNotEqual(key, cache.key(item))

    def test_write_read_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ThumbnailCache(Path(directory))
            key = cache.key(self.make_item(Path("/tmp/photo.jpg")))
            self.assertIsNone(cache.read(key))
            cache.write(key, b"JPEGDATA")
            self.assertEqual(cache.read(key), b"JPEGDATA")
            freed = cache.clear()
            self.assertGreaterEqual(freed, 8)
            self.assertIsNone(cache.read(key))


class JustifiedLayoutTests(unittest.TestCase):
    def test_widths_fill_available_space(self) -> None:
        widths = _split_widths([1.0, 1.5, 0.75, 2.0], 1000)
        self.assertEqual(sum(widths), 1000)
        self.assertEqual(len(widths), 4)

    def test_widths_follow_aspect_ratio(self) -> None:
        widths = _split_widths([1.0, 2.0, 1.0], 400)
        self.assertEqual(sum(widths), 400)
        self.assertGreater(widths[1], widths[0])
        self.assertEqual(widths[0], widths[2])

    def test_single_image(self) -> None:
        self.assertEqual(_split_widths([1.6], 640), [640])


class ImagingTests(unittest.TestCase):
    def test_decode_downscales(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.jpg"
            make_image(path, (2000, 1000), color=(10, 100, 200))
            data = decode_image(path, max_side=256)
            self.assertIsNotNone(data)
            assert data is not None
            self.assertGreater(len(data), 100)
            with Image.open(__import__("io").BytesIO(data)) as image:
                self.assertLessEqual(max(image.size), 256)


if __name__ == "__main__":
    unittest.main()
