"""语音合成任务：后台线程跑，前端轮询进度。

1000 字大约要合成 2~3 分钟（int8 版 CPU 上 RTF≈0.5），把一个 HTTP 请求挂那么久不合适，
所以照「模型安装」那套来：后台线程 + 进度快照 + 结果落盘，接口只回状态。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.domain import EngineUnavailableError
from app.infrastructure import tts_engine

logger = logging.getLogger(__name__)

# 内存里最多留几个任务记录；超出的连同音频文件一起清掉（合成产物是临时物料）
MAX_JOBS = 10
# 进度里留给「模型加载 + 写文件」的余量：逐句合成只占 5%~95%
_PROGRESS_BASE = 5
_PROGRESS_SPAN = 90


class TtsCancelled(RuntimeError):
    pass


@dataclass
class TtsJob:
    id: str
    status: str = "pending"
    message: str = "排队中"
    percent: int = 0
    model_id: str = tts_engine.DEFAULT_MODEL_ID
    # 音色 id：对应一段参考音频（ZipVoice 是零样本克隆，不是固定音色编号）
    voice: str = tts_engine.DEFAULT_VOICE
    speed: float = tts_engine.DEFAULT_SPEED
    chars: int = 0
    sentences_done: int = 0
    sentences_total: int = 0
    audio_path: str | None = None
    duration: float = 0.0
    elapsed: float = 0.0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def active(self) -> bool:
        return self.status in {"pending", "running", "cancelling"}

    @property
    def ready(self) -> bool:
        return self.status == "completed" and bool(self.audio_path)


class TtsJobManager:
    """串行执行合成任务：同一时间只跑一个，避免多个任务互相抢 CPU 把彼此拖慢。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._jobs: dict[str, TtsJob] = {}

    def audio_dir(self) -> Path:
        directory = settings.data_dir / "tts"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def sweep_orphans(self) -> None:
        """清掉上次会话遗留的音频文件。

        合成记录只存在内存里（重启即失效），音频却是落盘的：不清的话，每次重启都会
        留下一批再也引用不到的文件，而 `_prune_locked` 只能清理它自己 pop 掉的记录，
        磁盘只增不减。合成产物本就是临时物料，所以由应用启动流程调用它清空音频目录。

        刻意不做成构造时自动执行：那会让「import 这个模块」直接删掉用户数据目录里的
        文件，测试 import 一次就会造成真实破坏。
        """

        try:
            for item in self.audio_dir().glob("*.wav"):
                item.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理遗留的合成音频失败", exc_info=True)

    def submit(self, text: str, *, model_id: str, voice: str, speed: float) -> TtsJob:
        job = TtsJob(
            id=uuid.uuid4().hex,
            model_id=model_id,
            voice=voice,
            speed=speed,
            chars=len(tts_engine.normalize_text(text)),
            message="排队中",
        )
        with self._lock:
            self._jobs[job.id] = job
            self._prune_locked()
        thread = threading.Thread(target=self._run, args=(job, text), name=f"tts-{job.id[:8]}", daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> TtsJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self) -> list[TtsJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)

    def cancel(self, job_id: str) -> TtsJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.cancel.set()
        if job.active:
            job.status = "cancelling"
            job.message = "正在取消"
            job.updated_at = time.time()
        return job

    def remove(self, job_id: str) -> bool:
        """删掉已完成的任务记录与音频文件；正在合成的返回 False。"""

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.active:
                return False
            self._jobs.pop(job_id, None)
        if job.audio_path:
            Path(job.audio_path).unlink(missing_ok=True)
        return True

    def is_busy(self) -> bool:
        with self._lock:
            return any(item.active for item in self._jobs.values())

    def _prune_locked(self) -> None:
        """只留最近 MAX_JOBS 条：合成产物是临时的，留着占磁盘没意义。"""

        if len(self._jobs) <= MAX_JOBS:
            return
        ordered = sorted(self._jobs.values(), key=lambda item: item.created_at)
        for stale in ordered[: len(self._jobs) - MAX_JOBS]:
            if stale.active:
                continue
            self._jobs.pop(stale.id, None)
            if stale.audio_path:
                Path(stale.audio_path).unlink(missing_ok=True)

    def _run(self, job: TtsJob, text: str) -> None:
        with self._run_lock:
            started = time.perf_counter()
            try:
                if job.cancel.is_set():
                    raise TtsCancelled("已取消")
                job.status = "running"
                job.message = "正在加载语音模型"
                job.updated_at = time.time()

                def on_progress(done: int, total: int) -> None:
                    if job.cancel.is_set():
                        raise TtsCancelled("已取消")
                    job.sentences_done = done
                    job.sentences_total = total
                    job.percent = min(95, _PROGRESS_BASE + int(done / max(total, 1) * _PROGRESS_SPAN))
                    job.message = f"正在合成 {done}/{total} 句"
                    job.updated_at = time.time()

                samples, rate = tts_engine.synthesize(
                    text,
                    model_id=job.model_id,
                    voice_id=job.voice,
                    speed=job.speed,
                    on_progress=on_progress,
                )
                if job.cancel.is_set():
                    raise TtsCancelled("已取消")

                job.message = "正在写入音频"
                job.updated_at = time.time()
                target = self.audio_dir() / f"{job.id}.wav"
                tts_engine.write_audio(target, samples, rate)
                job.audio_path = str(target)
                job.duration = tts_engine.duration_seconds(samples, rate)
                job.percent = 100
                job.status = "completed"
                job.message = "合成完成"
            except TtsCancelled:
                job.status = "cancelled"
                job.message = "已取消合成"
            except EngineUnavailableError as exc:
                job.status = "failed"
                job.error = str(exc)
                job.message = str(exc)
            except (ValueError, OSError, RuntimeError) as exc:
                logger.warning("语音合成失败 %s: %s", job.id, exc, exc_info=True)
                job.status = "failed"
                job.error = str(exc)
                job.message = str(exc)
            finally:
                job.elapsed = round(time.perf_counter() - started, 2)
                job.updated_at = time.time()


manager = TtsJobManager()


__all__ = ["TtsJob", "TtsJobManager", "TtsCancelled", "manager"]
