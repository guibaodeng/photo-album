"""相册：基于 PyGObject + GTK4 + libadwaita 的本地照片/视频浏览器。"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")

__version__ = "1.1.2"
