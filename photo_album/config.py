"""应用配置。

配置以 JSON 形式保存在 ``$XDG_CONFIG_HOME/photo-album/config.json``，
也可以通过环境变量 ``PHOTO_ALBUM_CONFIG`` 指定配置文件路径。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_ID = "io.github.dengguibao.PhotoAlbum"
APP_NAME = "相册"
APP_VERSION = "1.1.0"

#: 时间轴功能暂时关闭；实现保留，改回 True 即可恢复。
FEATURE_TIMELINE = False

#: 默认媒体库目录（可通过偏好设置或命令行参数修改）。
DEFAULT_ROOT = Path.home() / "OneDrive" / "Pictures"

#: 扫描时默认跳过的目录名（回收站、缩略图缓存等）。
DEFAULT_EXCLUDED_DIRS = (
    ".dtrash",
    ".trash",
    ".Trash",
    ".git",
    ".thumbnails",
    "@eaDir",
    "node_modules",
)

ROW_HEIGHT_MIN = 140
ROW_HEIGHT_MAX = 360


def _default_root() -> str:
    """默认媒体库：优先 OneDrive/Pictures，其次 ~/Pictures。

    即使两者都不存在也返回 ~/Pictures，交给界面提示用户选择目录，
    避免误扫描整个用户主目录。
    """
    if DEFAULT_ROOT.is_dir():
        return str(DEFAULT_ROOT)
    return str(Path.home() / "Pictures")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "photo-album"


def config_path() -> Path:
    override = os.environ.get("PHOTO_ALBUM_CONFIG")
    if override:
        return Path(override).expanduser()
    return config_dir() / "config.json"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "photo-album"


@dataclass
class Config:
    """用户可配置项。"""

    #: 媒体库位置，可以配置多个文件夹；扫描结果会合并展示。
    roots: list[str] = field(default_factory=lambda: [str(_default_root())])
    recursive: bool = True
    include_hidden: bool = False
    excluded_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED_DIRS))
    row_height: int = 220
    sort_descending: bool = True
    video_autoplay: bool = True
    show_timeline: bool = True
    show_date_headers: bool = True
    window_width: int = 1440
    window_height: int = 900
    window_maximized: bool = False

    # ------------------------------------------------------------------ 路径
    @property
    def root_paths(self) -> list[Path]:
        return [Path(value).expanduser() for value in self.roots]

    @property
    def root(self) -> str:
        """兼容旧配置/外部调用：返回第一个位置。"""
        return self.roots[0] if self.roots else _default_root()

    @root.setter
    def root(self, value: str) -> None:
        self.roots = [value]

    def _short_path(self, path: Path) -> str:
        try:
            return "~/" + str(path.relative_to(Path.home()))
        except ValueError:
            return str(path)

    @property
    def root_label(self) -> str:
        """用于界面展示的单个位置短名。"""
        paths = self.root_paths
        return self._short_path(paths[0]) if paths else _default_root()

    @property
    def library_label(self) -> str:
        """媒体库标题：单目录显示文件夹名，多目录显示数量。"""
        if len(self.roots) <= 1:
            paths = self.root_paths
            if not paths:
                return Path(_default_root()).name
            path = paths[0]
            return path.name or str(path)
        return f"{len(self.roots)} 个位置"

    def library_tooltip(self) -> str:
        return "\n".join(self._short_path(path) for path in self.root_paths)

    # ------------------------------------------------------------------ 读写
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        data: dict[str, object] = {}
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, ValueError):
            data = {}

        # 兼容旧版单目录配置 { "root": "..." }。
        if "roots" not in data and isinstance(data.get("root"), str):
            data["roots"] = [data["root"]]

        config = cls()
        for key, value in data.items():
            if not hasattr(config, key):
                continue
            field_type = type(getattr(config, key))
            # 严格做一次类型校验，避免损坏的配置让界面崩溃。
            if field_type is bool:
                if isinstance(value, bool):
                    setattr(config, key, value)
            elif field_type is int:
                if isinstance(value, int) and not isinstance(value, bool):
                    setattr(config, key, value)
            elif field_type is str:
                if isinstance(value, str):
                    setattr(config, key, value)
            elif field_type is list:
                if isinstance(value, list) and all(isinstance(v, str) for v in value):
                    setattr(config, key, list(value))
        config.sanitize()
        return config

    def sanitize(self) -> None:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in self.roots:
            text = str(value).strip()
            if not text:
                continue
            path = Path(text).expanduser()
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        self.roots = normalized or [str(_default_root())]
        self.row_height = max(ROW_HEIGHT_MIN, min(ROW_HEIGHT_MAX, int(self.row_height)))
        self.window_width = max(860, min(8000, int(self.window_width)))
        self.window_height = max(560, min(6000, int(self.window_height)))
        self.excluded_dirs = [name for name in self.excluded_dirs if name]

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        self.sanitize()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n"
            # 先写临时文件再原子替换，避免进程中断损坏配置。
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=".config-", suffix=".tmp", delete=False
            ) as handle:
                handle.write(payload)
                tmp_name = handle.name
            os.replace(tmp_name, path)
        except OSError:
            pass
