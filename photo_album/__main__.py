"""允许通过 ``python3 -m photo_album`` 启动。"""

from __future__ import annotations

import sys

from .application import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
