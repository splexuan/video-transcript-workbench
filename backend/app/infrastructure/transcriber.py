"""SenseVoice 极速识别引擎（sherpa-onnx）。

用于「极速文本」模式：速度优先，时间轴为 30 秒粒度。
模型路径统一由 model_store 解析，不隐含任何固定的本机目录。
只在 CPU 上推理（`provider="cpu"`），并行策略见 `plan_parallelism`。
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
import soundfile as sf

from app.domain import ModelEngine, ModelNotFoundError, TranscriptChunk
from app.infrastructure.model_catalog import require_spec
from app.infrastructure.model_store import resolve_engine, slot_paths

logger = logging.getLogger(__name__)

MODEL_ID = "sensevoice-small"
MODEL_DIR_NAME = "sherpa-onnx-sense-voice-small"
CHUNK_SECONDS = 30
TAG_RE = re.compile(r"<\|[^|]+\|>")
# 并行识别的音频块数上限，以及每块最少分到的线程数。
# 每块少于 2 线程会明显变慢（实测 9 块 × 1 线程比 × 2 线程慢一倍），所以块数上限
# 同时受「核数 // 2」约束——2 核到 64 核的机器都不会线程超订。
MAX_PARALLEL_CHUNKS = 8
MIN_THREADS_PER_CHUNK = 2
_recognizer = None
_recognizer_threads = 0
_recognizer_lock = threading.Lock()


class NoSpeechError(RuntimeError):
    """没有识别到可用语音。"""


def _sense_voice_spec():  # type: ignore[no-untyped-def]
    return require_spec(MODEL_ID)


def find_model_dir() -> Path | None:
    """返回可用于识别的 SenseVoice 模型目录。"""

    resolved = resolve_engine(ModelEngine.SENSEVOICE)
    return resolved[1] if resolved else None


def model_ready() -> bool:
    return find_model_dir() is not None


def _model_files(directory: Path) -> tuple[Path, Path] | None:
    paths = slot_paths(_sense_voice_spec(), directory)
    model = paths.get("model.int8.onnx")
    tokens = paths.get("tokens.txt")
    if model is None or tokens is None:
        return None
    return model, tokens


def plan_parallelism(total: int) -> tuple[int, int]:
    """决定「同时识别几块」与「每块几个线程」。

    实测（16 逻辑核、30 秒块、交替多轮取中位数）：每块线程太少会明显变慢，所以先
    保证每块至少 2 线程，块数据此封顶在核数的一半、且不超过 MAX_PARALLEL_CHUNKS；
    再把核平均分给同时在跑的块。块数少于上限时（短视频）每块分到更多线程，
    核不会被闲置。
    """

    cores = max(2, multiprocessing.cpu_count())
    workers = max(1, min(total, MAX_PARALLEL_CHUNKS, cores // MIN_THREADS_PER_CHUNK))
    return workers, max(MIN_THREADS_PER_CHUNK, cores // workers)


def get_recognizer(threads: int):  # type: ignore[no-untyped-def]
    """取识别器；线程数变了就重建。

    只常驻一份模型（约 230 MB）：长短视频交替处理时重建一次约 1 秒，比同时留着
    几份模型更划算（低配机器尤其如此）。
    """

    global _recognizer, _recognizer_threads
    if _recognizer is not None and _recognizer_threads == threads:
        return _recognizer

    with _recognizer_lock:
        if _recognizer is not None and _recognizer_threads == threads:
            return _recognizer
        model_dir = find_model_dir()
        if not model_dir:
            raise ModelNotFoundError("未找到 SenseVoice 模型，请先在平台连接页面安装模型")
        files = _model_files(model_dir)
        if files is None:
            raise ModelNotFoundError("SenseVoice 模型文件不完整，请重新下载")
        model_path, tokens_path = files

        import sherpa_onnx

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
        _recognizer_threads = threads
        return _recognizer


def reset_cache() -> None:
    global _recognizer, _recognizer_threads
    _recognizer = None
    _recognizer_threads = 0


def resample(data: np.ndarray, original_rate: int, target_rate: int = 16000) -> np.ndarray:
    if original_rate == target_rate or len(data) == 0:
        return data.astype(np.float32, copy=False)
    scale = original_rate / target_rate
    output_size = int(len(data) / scale)
    positions = np.arange(output_size) * scale
    return np.interp(positions, np.arange(len(data)), data).astype(np.float32)


def decode_chunk(recognizer, samples: np.ndarray) -> str:  # type: ignore[no-untyped-def]
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    return TAG_RE.sub("", stream.result.text or "").strip()


def transcribe(
    wav_path: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[TranscriptChunk], float]:
    samples, sample_rate = sf.read(wav_path, dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    samples = np.ascontiguousarray(resample(samples, sample_rate))
    duration = len(samples) / 16000
    chunk_size = CHUNK_SECONDS * 16000
    total = max(1, (len(samples) + chunk_size - 1) // chunk_size)
    chunks = [np.ascontiguousarray(samples[index * chunk_size : (index + 1) * chunk_size]) for index in range(total)]
    workers, threads = plan_parallelism(total)
    recognizer = get_recognizer(threads)
    texts = [""] * total

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(decode_chunk, recognizer, chunk): index for index, chunk in enumerate(chunks)}
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
