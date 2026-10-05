from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from app.domain import MediaInfo, TranscriptChunk


class MediaToolError(RuntimeError):
    pass


def find_tool(name: str) -> str | None:
    """按顺序找外部工具：环境变量指定目录 → 打包资源目录 → exe 同目录 → 项目内 vendor → PATH。

    打包版把 ffmpeg.exe / ffprobe.exe 放在 PyInstaller 的资源目录（`sys._MEIPASS`，
    onedir 下是 exe 同级的 `_internal`），用户无需另行安装 FFmpeg；
    把 ffmpeg.exe 直接放到 exe 旁边（或设好 `VTW_FFMPEG_DIR`）同样有效。

    源码运行时没有资源目录，额外搜索仓库里的 `backend/vendor/ffmpeg/`——那是打包时
    固定 FFmpeg 版本用的位置，开发时把两个 exe 放进去，本地文件提取就能直接用。
    """

    roots: list[Path] = []
    override = os.getenv("VTW_FFMPEG_DIR")
    if override:
        roots.append(Path(override))
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.append(Path(bundle))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).parent)
    else:
        # 本文件在 backend/app/infrastructure/ 下：parents[2] 即 backend
        roots.append(Path(__file__).resolve().parents[2] / "vendor" / "ffmpeg")

    for root in roots:
        for filename in (f"{name}.exe", name):
            candidate = root / filename
            if candidate.is_file():
                return str(candidate)
    return shutil.which(name)


def tools_ready() -> bool:
    return bool(find_tool("ffmpeg") and find_tool("ffprobe"))


def _failure_detail(
    result: subprocess.CompletedProcess[str],
    source: Path,
    target: Path | None = None,
) -> str:
    """把外部工具的失败原因说清楚。

    FFmpeg 自己的报错最有用；它一声不吭时（进程被打断、崩溃、被安全软件拦下
    都会这样），退出代码和输入文件大小就是仅剩的线索，一并带上便于排查。
    """

    detail = result.stderr.strip()
    if detail:
        return detail

    size = f"{source.stat().st_size / 1024 / 1024:.1f} MB" if source.is_file() else "文件已不存在"
    if result.returncode == 0:
        missing = f"，也没有生成 {target.name}" if target is not None else ""
        return f"FFmpeg 正常退出但没有结果{missing}（输入 {source.name}，{size}）"
    return f"FFmpeg 异常退出（代码 {result.returncode}，输入 {source.name}，{size}），没有输出错误信息"


def probe_media(path: Path) -> MediaInfo:
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        raise MediaToolError("未找到 FFprobe，请先安装 FFmpeg")

    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:format_tags=title",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise MediaToolError(f"无法读取媒体信息：{_failure_detail(result, path)}")

    payload = json.loads(result.stdout or "{}")
    format_info = payload.get("format") or {}
    tags = format_info.get("tags") or {}
    duration_raw = format_info.get("duration")
    duration = float(duration_raw) if duration_raw else None
    fallback_title = path.stem.split("__", 1)[-1]
    return MediaInfo(
        title=str(tags.get("title") or fallback_title),
        platform="local",
        duration_seconds=duration,
    )


def convert_to_wav(source: Path, destination: Path) -> Path:
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise MediaToolError("未找到 FFmpeg，请先安装或配置 FFmpeg")

    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    if result.returncode != 0 or not destination.exists():
        raise MediaToolError(f"FFmpeg 音轨转换失败：{_failure_detail(result, source, destination)}")
    return destination


def plain_text_chunk(text: str, duration_seconds: float | None = None) -> list[TranscriptChunk]:
    cleaned = text.strip()
    if not cleaned:
        return []
    end_ms = int(duration_seconds * 1000) if duration_seconds else None
    return [TranscriptChunk(start_ms=0 if end_ms else None, end_ms=end_ms, text=cleaned)]
