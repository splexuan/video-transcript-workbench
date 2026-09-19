"""faster-whisper 精准识别引擎。

用于「精准时间轴」模式：输出句级时间戳，可直接生成 SRT 字幕。
**只在 CPU 上推理**：不引入 CUDA/cuDNN 依赖，换来的是装完即用、没有显卡门槛。
引擎依赖 `faster-whisper` 可选组件，未安装时由调用方给出可理解的提示。
"""

from __future__ import annotations

import importlib.util
import logging
import multiprocessing
import subprocess
import sys
import threading
import warnings
from collections.abc import Callable
from pathlib import Path

from app.config import settings
from app.domain import EngineUnavailableError, TranscriptChunk

# ctranslate2 4.5 导入时会警告 pkg_resources 已废弃（它自己还在用），与用户无关
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

logger = logging.getLogger(__name__)

_models: dict[tuple[str, str], object] = {}
_model_lock = threading.Lock()
_vad_available: bool | None = None


class WhisperUnavailableError(EngineUnavailableError):
    pass


class WhisperNoSpeechError(RuntimeError):
    pass


def _cpu_threads() -> int:
    """CPU 推理线程数。

    用满逻辑核：实测 6 线程改 12 线程后同一段音频从 88.6 秒降到 48.4 秒。
    转写是单任务串行的，不存在与其它任务抢核的问题。
    """

    return max(2, multiprocessing.cpu_count())


def engine_installed() -> bool:
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def compute_type() -> str:
    """CPU 推理精度。

    默认 int8：同一段音频实测比默认精度快得多，而中文口播的准确度差别很小。
    可用 `VTW_WHISPER_COMPUTE_TYPE` 覆盖（例如 float32 换更高精度）。
    """

    return (settings.whisper_compute_type or "").strip() or "int8"


def get_model(model_dir: Path):  # type: ignore[no-untyped-def]
    """加载并缓存模型；只在 CPU 上推理，不需要任何 CUDA 组件。"""

    if not engine_installed():
        raise WhisperUnavailableError("缺少 faster-whisper 组件，请先安装后再使用精准时间轴")

    precision = compute_type()
    key = (str(model_dir), precision)
    cached = _models.get(key)
    if cached is not None:
        return cached

    with _model_lock:
        cached = _models.get(key)
        if cached is not None:
            return cached
        from faster_whisper import WhisperModel

        logger.info("加载 faster-whisper 模型：%s（cpu/%s）", model_dir, precision)
        model = WhisperModel(
            str(model_dir),
            device="cpu",
            compute_type=precision,
            cpu_threads=_cpu_threads(),
        )
        _models[key] = model
        return model


def reset_cache() -> None:
    with _model_lock:
        _models.clear()


def vad_available() -> bool:
    """探测 onnxruntime 是否真的可用。

    某些 Windows 环境下 onnxruntime 的扩展模块加载即触发访问违例，
    一旦在主进程里 import 就会直接终止整个应用。因此这里用子进程探测，
    结果缓存复用；探测失败时安静地退化为不带 VAD 的转写。
    """

    global _vad_available
    if _vad_available is not None:
        return _vad_available

    if not settings.whisper_vad:
        _vad_available = False
        return False

    if importlib.util.find_spec("onnxruntime") is None:
        logger.info("未安装 onnxruntime，精准识别将不启用 VAD 预处理")
        _vad_available = False
        return False

    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import onnxruntime"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        _vad_available = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        _vad_available = False

    if not _vad_available:
        logger.warning("onnxruntime 在本机不可用（加载失败），精准识别将关闭 VAD 预处理")
    return _vad_available


def transcribe(
    wav_path: Path,
    model_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
    language: str | None = None,
) -> tuple[list[TranscriptChunk], float]:
    """用 faster-whisper 逐段转写，返回带精细时间戳的分段与总时长。"""

    model = get_model(model_dir)
    segments, info = model.transcribe(
        str(wav_path),
        language=language,
        beam_size=max(1, settings.whisper_beam_size),
        vad_filter=vad_available(),
        word_timestamps=False,
    )

    duration = float(getattr(info, "duration", 0.0) or 0.0)
    chunks: list[TranscriptChunk] = []
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        chunks.append(
            TranscriptChunk(
                start_ms=int(segment.start * 1000),
                end_ms=int(segment.end * 1000),
                text=text,
            )
        )
        if on_progress is not None and duration:
            on_progress(min(int(segment.end * 1000), int(duration * 1000)), int(duration * 1000))

    if not chunks:
        raise WhisperNoSpeechError("没有识别到可用语音")

    return chunks, duration or (chunks[-1].end_ms or 0) / 1000
