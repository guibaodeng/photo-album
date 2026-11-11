#!/usr/bin/env python3
"""开发/本地运行入口：``python3 run.py [照片目录]``。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from photo_album.application import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
