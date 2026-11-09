"""本地语音识别：SenseVoice。

只做 CPU 推理，不依赖显卡或云服务。
"""

from __future__ import annotations

import logging
import multiprocessing
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from .errors import NoSpeechError, SkillError
from .media import read_wav
from .types import TranscriptChunk

logger = logging.getLogger(__name__)

TAG_RE = re.compile(r"<\|[^|]+\|>")
CHUNK_SECONDS = 30
MAX_PARALLEL_CHUNKS = 8
MIN_THREADS_PER_CHUNK = 2

_recognizer = None
_recognizer_key: tuple[str, int] | None = None
_recognizer_lock = threading.Lock()


def plan_parallelism(total: int) -> tuple[int, int]:
    cores = max(2, multiprocessing.cpu_count())
    workers = max(1, min(total, MAX_PARALLEL_CHUNKS, cores // MIN_THREADS_PER_CHUNK))
    return workers, max(MIN_THREADS_PER_CHUNK, cores // workers)


def _sense_voice_files(model_dir: Path) -> tuple[Path, Path]:
    for name in ("model.int8.onnx", "model_q8.onnx", "model.onnx"):
        candidate = model_dir / name
        if candidate.is_file():
            tokens = model_dir / "tokens.txt"
            if tokens.is_file():
                return candidate, tokens
            break
    raise FileNotFoundError(f"SenseVoice 模型文件不完整：{model_dir}")


def get_recognizer(model_dir: Path, threads: int):
    global _recognizer, _recognizer_key
    key = (str(model_dir), threads)
    if _recognizer is not None and _recognizer_key == key:
        return _recognizer

    with _recognizer_lock:
        if _recognizer is not None and _recognizer_key == key:
            return _recognizer
        model_path, tokens_path = _sense_voice_files(model_dir)
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise SkillError(
                "缺少 sherpa-onnx 组件，请执行：pip install -r requirements.txt",
                "DEPENDENCY_MISSING",
            ) from exc

        logger.info("加载 SenseVoice 识别器（%d 线程）", threads)
        _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_path),
            tokens=str(tokens_path),
            provider="cpu",
            num_threads=threads,
            sample_rate=16000,
            use_itn=True,
            language="",
            debug=False,
        )
        _recognizer_key = key
        return _recognizer


def reset_cache() -> None:
    global _recognizer, _recognizer_key
    _recognizer = None
    _recognizer_key = None


def _decode_chunk(recognizer, samples: np.ndarray) -> str:
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    return TAG_RE.sub("", stream.result.text or "").strip()


def transcribe_sensevoice(
    wav_path: Path,
    model_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[TranscriptChunk], float]:
    samples, duration = read_wav(wav_path)
    chunk_size = CHUNK_SECONDS * 16000
    total = max(1, (len(samples) + chunk_size - 1) // chunk_size)
    chunks = [
        np.ascontiguousarray(samples[index * chunk_size : (index + 1) * chunk_size])
        for index in range(total)
    ]
    workers, threads = plan_parallelism(total)
    recognizer = get_recognizer(model_dir, threads)
    texts = [""] * total

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_decode_chunk, recognizer, chunk): index
            for index, chunk in enumerate(chunks)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            index = futures[future]
            texts[index] = future.result()
            if on_progress:
                on_progress(completed, total)

    segments = [
        TranscriptChunk(
            start_ms=index * CHUNK_SECONDS * 1000,
            end_ms=min(int(duration * 1000), (index + 1) * CHUNK_SECONDS * 1000),
            text=text,
        )
        for index, text in enumerate(texts)
        if text
    ]
    if not segments:
        raise NoSpeechError("没有识别到可用语音")
    return segments, duration


def transcribe(
    wav_path: Path,
    model_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[TranscriptChunk], float]:
    return transcribe_sensevoice(wav_path, model_dir, on_progress)
