"""外部工具查找、音轨转码与 WAV 读取。"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from .config import SKILL_ROOT

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
SAMPLE_RATE = 16000


class MediaToolError(RuntimeError):
    pass


def find_tool(name: str, ffmpeg_dir: Path | None = None) -> str | None:
    """按顺序查找 ffmpeg / ffprobe：配置目录 → 技能内 vendor → 当前目录 → PATH。"""

    roots: list[Path] = []
    if ffmpeg_dir is not None:
        roots.append(ffmpeg_dir.expanduser())
    roots.append(SKILL_ROOT / "vendor" / "ffmpeg")
    roots.append(SKILL_ROOT / "vendor")
    roots.append(SKILL_ROOT)
    roots.append(Path.cwd())

    for root in roots:
        for filename in (f"{name}.exe", name):
            candidate = root / filename
            if candidate.is_file():
                return str(candidate)
    return shutil.which(name)


def tools_ready(ffmpeg_dir: Path | None = None) -> bool:
    return bool(find_tool(FFMPEG, ffmpeg_dir) and find_tool(FFPROBE, ffmpeg_dir))


def _failure_detail(result: subprocess.CompletedProcess[str], source: Path) -> str:
    detail = (result.stderr or "").strip()
    if detail:
        return detail
    size = (
        f"{source.stat().st_size / 1024 / 1024:.1f} MB"
        if source.is_file()
        else "文件已不存在"
    )
    return f"FFmpeg 异常退出（代码 {result.returncode}，输入 {source.name}，{size}）"


def convert_to_wav(
    source: Path,
    destination: Path,
    ffmpeg_dir: Path | None = None,
) -> Path:
    """统一转成 16 kHz 单声道 PCM WAV；识别引擎只认这种输入。"""

    ffmpeg = find_tool(FFMPEG, ffmpeg_dir)
    if not ffmpeg:
        raise MediaToolError(
            "未找到 FFmpeg。请安装并加入 PATH，或把 ffmpeg.exe / ffprobe.exe "
            "放到技能的 vendor/ffmpeg/ 目录，或用 VTS_FFMPEG_DIR 指定目录。"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
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
        str(SAMPLE_RATE),
        "-sample_fmt",
        "s16",
        str(destination),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    if result.returncode != 0 or not destination.exists():
        raise MediaToolError(f"FFmpeg 音轨转换失败：{_failure_detail(result, source)}")
    return destination


def probe_media(path: Path, ffmpeg_dir: Path | None = None) -> tuple[str, float | None]:
    """读取本地文件的标题与时长；失败时用文件名兜底。"""

    ffprobe = find_tool(FFPROBE, ffmpeg_dir)
    if not ffprobe:
        raise MediaToolError("未找到 FFprobe，请与 FFmpeg 一起安装")
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
    return str(tags.get("title") or path.stem), duration


def resample(samples: np.ndarray, original_rate: int, target_rate: int = SAMPLE_RATE) -> np.ndarray:
    if original_rate == target_rate or len(samples) == 0:
        return samples.astype(np.float32, copy=False)
    scale = original_rate / target_rate
    output_size = int(len(samples) / scale)
    positions = np.arange(output_size) * scale
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


def read_wav(path: Path) -> tuple[np.ndarray, float]:
    """读取 PCM WAV 并转成 16 kHz 单声道 float32；返回 (采样点, 秒数)。"""

    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, OSError) as exc:
        raise MediaToolError(f"读取音频失败：{exc}") from exc

    if width != 2:
        raise MediaToolError(f"只支持 16 位 PCM WAV，当前采样宽度为 {width * 8} 位")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    samples = resample(np.ascontiguousarray(samples), rate)
    return samples, len(samples) / SAMPLE_RATE
