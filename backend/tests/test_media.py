"""FFmpeg 等外部工具的查找顺序（不依赖真实安装）。"""

from __future__ import annotations

import sys
from pathlib import Path

from app.infrastructure import media


def test_find_tool_prefers_ffmpeg_dir_env(tmp_path: Path, monkeypatch) -> None:
    """VTW_FFMPEG_DIR 指定的目录优先于 PATH，方便用户自带一份 FFmpeg。"""

    monkeypatch.delenv("VTW_FFMPEG_DIR", raising=False)
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"stub")
    monkeypatch.setenv("VTW_FFMPEG_DIR", str(tmp_path))

    assert media.find_tool("ffmpeg") == str(fake)


def test_find_tool_looks_inside_pyinstaller_bundle(tmp_path: Path, monkeypatch) -> None:
    """打包版把 ffmpeg.exe 放在资源目录（onedir 下是 _internal），必须能找到。"""

    monkeypatch.delenv("VTW_FFMPEG_DIR", raising=False)
    bundled = tmp_path / "ffprobe.exe"
    bundled.write_bytes(b"stub")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert media.find_tool("ffprobe") == str(bundled)
    # 资源目录里没有的工具仍然回落到 PATH
    assert media.find_tool("ffmpeg-not-bundled") == media.shutil.which("ffmpeg-not-bundled")


def test_find_tool_without_bundle_falls_back_to_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("VTW_FFMPEG_DIR", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert media.find_tool("ffmpeg-definitely-missing") is None
