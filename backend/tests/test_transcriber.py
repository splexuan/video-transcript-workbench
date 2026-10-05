"""SenseVoice 的并行策略与识别器缓存（不加载真实模型）。"""

from __future__ import annotations

import multiprocessing
import sys
import types
from pathlib import Path

import pytest

from app.infrastructure import transcriber

CORES = max(2, multiprocessing.cpu_count())
# 每块至少 2 线程，因此并行块数最多是核数的一半；再受 8 块上限约束
BLOCK_CAP = max(1, min(transcriber.MAX_PARALLEL_CHUNKS, CORES // transcriber.MIN_THREADS_PER_CHUNK))


def test_plan_parallelism_never_oversubscribes_cores() -> None:
    """并行块数 × 每块线程数不超过核数：机器规格未知，不能靠超订换速度。"""

    for total in (1, 2, 3, 8, 9, 600):
        workers, threads = transcriber.plan_parallelism(total)

        assert 1 <= workers <= min(total, transcriber.MAX_PARALLEL_CHUNKS)
        assert threads >= transcriber.MIN_THREADS_PER_CHUNK
        assert workers * threads <= CORES


def test_plan_parallelism_opens_more_blocks_for_long_audio() -> None:
    """长音频开到并行上限（核够多时就是 8 块），短视频按块数少开。"""

    assert transcriber.plan_parallelism(600)[0] == BLOCK_CAP
    assert transcriber.plan_parallelism(BLOCK_CAP + 20) == transcriber.plan_parallelism(600)
    assert transcriber.plan_parallelism(2)[0] == 2


def test_plan_parallelism_gives_short_audio_more_threads_per_block() -> None:
    """块数不到上限时把核分给这些块，不能闲着；单块时用满全部核。"""

    assert transcriber.plan_parallelism(1) == (1, CORES)
    # 块数越多，每块线程数只会不增
    previous = CORES + 1
    for total in range(1, transcriber.MAX_PARALLEL_CHUNKS * 2):
        _workers, threads = transcriber.plan_parallelism(total)
        assert threads <= previous
        previous = threads


def test_recognizer_is_reused_for_same_thread_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一线程数复用识别器，线程数变了才重建——只常驻一份模型。"""

    created: list[int] = []

    class FakeOfflineRecognizer:
        @staticmethod
        def from_sense_voice(**kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["num_threads"])
            return f"recognizer-{kwargs['num_threads']}"

    monkeypatch.setattr(transcriber, "find_model_dir", lambda: Path("."))
    monkeypatch.setattr(transcriber, "_model_files", lambda _dir: (Path("m.onnx"), Path("t.txt")))
    monkeypatch.setitem(
        sys.modules,
        "sherpa_onnx",
        types.SimpleNamespace(OfflineRecognizer=FakeOfflineRecognizer),
    )
    transcriber.reset_cache()

    assert transcriber.get_recognizer(8) == "recognizer-8"
    assert transcriber.get_recognizer(8) == "recognizer-8"
    assert created == [8]

    assert transcriber.get_recognizer(2) == "recognizer-2"
    assert created == [8, 2], "线程数不同必须重建"

    transcriber.reset_cache()
    assert transcriber._recognizer is None
    assert transcriber._recognizer_threads == 0
