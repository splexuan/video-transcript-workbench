from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from app.config import settings
from app.domain import EngineUnavailableError
from app.infrastructure import whisper_engine


@pytest.fixture(autouse=True)
def clear_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(whisper_engine, "_vad_available", None)
    monkeypatch.setattr(whisper_engine, "_models", {})


def test_compute_type_defaults_to_int8(monkeypatch) -> None:
    """只在 CPU 上推理，默认 int8：同一段音频实测比默认精度快得多。"""

    monkeypatch.setattr(settings, "whisper_compute_type", "")

    assert whisper_engine.compute_type() == "int8"


def test_explicit_compute_type_wins(monkeypatch) -> None:
    """需要更高精度时可以用环境变量覆盖成 ctranslate2 支持的其它精度。"""

    monkeypatch.setattr(settings, "whisper_compute_type", "float32")

    assert whisper_engine.compute_type() == "float32"


def test_cpu_threads_uses_all_cores() -> None:
    """实测线程数从半数逻辑核改满核后，同一段音频 88.6 秒降到 48.4 秒。"""

    assert whisper_engine._cpu_threads() == max(2, multiprocessing.cpu_count())


def test_vad_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "whisper_vad", False)
    assert whisper_engine.vad_available() is False


def test_vad_skipped_when_package_missing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "whisper_vad", True)
    monkeypatch.setattr(whisper_engine.importlib.util, "find_spec", lambda _name: None)
    assert whisper_engine.vad_available() is False


def test_vad_probe_runs_in_subprocess(monkeypatch) -> None:
    """onnxruntime 在部分环境会崩，必须在子进程里探测，不能在主进程 import。"""

    monkeypatch.setattr(settings, "whisper_vad", True)
    monkeypatch.setattr(whisper_engine.importlib.util, "find_spec", lambda _name: object())
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(command)
        return Result()

    monkeypatch.setattr(whisper_engine.subprocess, "run", fake_run)
    assert whisper_engine.vad_available() is True
    assert calls and "onnxruntime" in calls[0][-1]
    assert calls[0][0] == whisper_engine.sys.executable


def test_vad_probe_failure_is_silent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "whisper_vad", True)
    monkeypatch.setattr(whisper_engine.importlib.util, "find_spec", lambda _name: object())

    class Result:
        returncode = -1073741819

    monkeypatch.setattr(whisper_engine.subprocess, "run", lambda *_a, **_k: Result())
    assert whisper_engine.vad_available() is False


def test_get_model_requires_engine(monkeypatch) -> None:
    monkeypatch.setattr(whisper_engine, "engine_installed", lambda: False)
    with pytest.raises(EngineUnavailableError):
        whisper_engine.get_model(Path("."))


def test_transcribe_uses_configured_options(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "whisper_beam_size", 3)
    monkeypatch.setattr(whisper_engine, "vad_available", lambda: False)

    captured: dict[str, object] = {}

    class Segment:
        def __init__(self, start: float, end: float, text: str) -> None:
            self.start = start
            self.end = end
            self.text = text

    class Info:
        duration = 10.0

    class FakeModel:
        def transcribe(self, path: str, **kwargs):  # type: ignore[no-untyped-def]
            captured["path"] = path
            captured.update(kwargs)
            return iter([Segment(0.0, 2.5, " 第一句 "), Segment(2.5, 10.0, "")]), Info()

    monkeypatch.setattr(whisper_engine, "get_model", lambda *_args, **_kwargs: FakeModel())

    chunks, duration = whisper_engine.transcribe(tmp_path / "a.wav", tmp_path)

    assert duration == 10.0
    assert [(item.start_ms, item.end_ms, item.text) for item in chunks] == [(0, 2500, "第一句")]
    assert captured["vad_filter"] is False
    assert captured["beam_size"] == 3
    assert captured["word_timestamps"] is False


def test_transcribe_raises_when_no_speech(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(whisper_engine, "vad_available", lambda: False)

    class Info:
        duration = 4.0

    class FakeModel:
        def transcribe(self, _path: str, **_kwargs):  # type: ignore[no-untyped-def]
            return iter([]), Info()

    monkeypatch.setattr(whisper_engine, "get_model", lambda *_args, **_kwargs: FakeModel())
    with pytest.raises(whisper_engine.WhisperNoSpeechError):
        whisper_engine.transcribe(tmp_path / "a.wav", tmp_path)
