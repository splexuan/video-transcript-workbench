"""打开本机目录的薄封装。

打包版没有控制台，打不开文件夹不是致命错误，因此统一「尽力而为 + 写日志」，
由调用方决定怎么提示用户。
"""

from __future__ import annotations

import logging
import os
import sys
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)


def open_path(path: Path) -> bool:
    """在系统文件管理器里打开目录（或文件）；打不开返回 False。"""

    try:
        if sys.platform == "win32":
            os.startfile(path)
        else:
            webbrowser.open(path.as_uri())
    except OSError as exc:
        logger.warning("打开目录失败 %s：%s", path, exc)
        return False
    return True
