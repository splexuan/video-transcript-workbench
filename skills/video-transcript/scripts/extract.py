#!/usr/bin/env python3
"""视频链接 → 口播文案。

独立技能入口：不依赖任何宿主程序，只依赖 requirements.txt 与 FFmpeg。

    python scripts/extract.py "<链接或本地文件>"
    python scripts/extract.py --check
    python scripts/extract.py --install-model sensevoice-small
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 直接运行时把 scripts/ 放进 sys.path，保证 `import vtlib` 可用
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    try:
        from vtlib.cli import build_parser, run
    except ImportError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "DEPENDENCY_MISSING",
                    "message": f"Python 依赖缺失（{exc}）。请先执行："
                    "python -m pip install -r requirements.txt",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
