"""语音合成任务：后台线程跑，前端轮询进度。

1000 字单实例大约要合成 4~5 分钟（fp32 版、8 步采样 CPU 上 RTF≈1.3；12 核 4 路并行约 1 分钟），
把一个 HTTP 请求挂那么久不合适，
所以照「模型安装」那套来：后台线程 + 进度快照 + 结果落盘，接口只回状态。

记录落在数据库里（`tts_jobs` 表）：正在跑的那条同时挂在内存中（取消信号是 threading.Event，
入不了库），跑完之后以库里的记录为准。这样重启后历史还在，能回听、能删。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update

from app.config import settings
from app.domain import EngineUnavailableError
from app.infrastructure import tts_engine
from app.infrastructure.database import SessionLocal
from app.infrastructure.models import TtsJobRecord

logger = logging.getLogger(__name__)

# 历史记录条数上限：合成产物会留在磁盘上，给个上限兜底（超出时删最旧的一条连同音频）。
# 比原先内存里那 10 条宽得多——用户要的是「留得住、自己删」，不是「自动清光」。
MAX_HISTORY = 200
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
    # 提交时的原文：点历史记录时填回输入框，能接着改几个字再合成
    text: str = ""
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

    @property
    def audio_name(self) -> str | None:
        return Path(self.audio_path).name if self.audio_path else None


class TtsJobManager:
    """串行执行合成任务：同一时间只跑一个，避免多个任务互相抢 CPU 把彼此拖慢。

    记录分两处：正在跑的挂在 `_active`（要带取消信号），同时写一行进数据库；
    跑完就从内存移走，库里那行留作历史。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._active: dict[str, TtsJob] = {}

    def audio_dir(self) -> Path:
        directory = settings.data_dir / "tts"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # ---------- 数据库读写 ----------

    def _store(self, job: TtsJob) -> None:
        """把任务写进库（有则更新）。

        只在状态变化时调用，不逐句写：进度是给界面轮询的，没必要每句落一次盘。
        落库失败不影响合成本身，所以这里把异常吞掉只记日志。
        """

        try:
            with SessionLocal() as session:
                record = session.get(TtsJobRecord, job.id)
                if record is None:
                    record = TtsJobRecord(id=job.id)
                    session.add(record)
                record.status = job.status
                record.message = job.message[:200]
                record.model_id = job.model_id
                record.voice = job.voice
                record.speed = job.speed
                record.text = job.text
                record.chars = job.chars
                record.duration = job.duration
                record.elapsed = job.elapsed
                record.error = job.error
                record.audio_name = job.audio_name
                record.updated_at = datetime.now(UTC)
                session.commit()
        except Exception:  # noqa: BLE001 - 记录写不进去也不该让合成失败
            logger.warning("写入合成记录失败 %s", job.id, exc_info=True)

    def _to_job(self, record: TtsJobRecord) -> TtsJob:
        """把库里的记录还原成任务对象（历史记录用，没有取消信号）。"""

        path = self.audio_dir() / record.audio_name if record.audio_name else None
        return TtsJob(
            id=record.id,
            status=record.status,
            message=record.message,
            percent=100 if record.status == "completed" else 0,
            model_id=record.model_id,
            voice=record.voice,
            speed=record.speed,
            text=record.text,
            chars=record.chars,
            audio_path=str(path) if path is not None else None,
            duration=record.duration,
            elapsed=record.elapsed,
            error=record.error,
            created_at=record.created_at.timestamp(),
            updated_at=record.updated_at.timestamp(),
        )

    def _records(self) -> list[TtsJobRecord]:
        with SessionLocal() as session:
            return list(
                session.scalars(
                    select(TtsJobRecord).order_by(TtsJobRecord.created_at.desc())
                )
            )

    # ---------- 启动清理 ----------

    def sweep_orphans(self) -> None:
        """删掉没有任何记录引用的音频文件。

        记录现在落在数据库里（重启不丢），所以不能再像以前那样清空音频目录——
        那会把历史记录变成一堆打不开的条目。只清真正的孤儿。
        """

        try:
            known = {record.audio_name for record in self._records() if record.audio_name}
            for item in self.audio_dir().glob("*.wav"):
                if item.name not in known:
                    item.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理遗留的合成音频失败", exc_info=True)

    def mark_interrupted(self) -> None:
        """把上次进程退出时还在跑的任务标成已中断。

        否则它们会永远停在「合成中」：界面上既删不掉，也等不到结果。
        """

        message = "应用重启，本次合成已中断"
        try:
            with SessionLocal() as session:
                session.execute(
                    update(TtsJobRecord)
                    .where(TtsJobRecord.status.in_(("pending", "running", "cancelling")))
                    .values(
                        status="failed",
                        message=message,
                        error=message,
                        updated_at=datetime.now(UTC),
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001
            logger.warning("标记中断的合成任务失败", exc_info=True)

    # ---------- 任务生命周期 ----------

    def submit(self, text: str, *, model_id: str, voice: str, speed: float) -> TtsJob:
        job = TtsJob(
            id=uuid.uuid4().hex,
            model_id=model_id,
            voice=voice,
            speed=speed,
            text=text,
            chars=len(tts_engine.normalize_text(text)),
            message="排队中",
        )
        with self._lock:
            self._active[job.id] = job
        # 先落库：这样「最近合成」列表里立刻就有这一条
        self._store(job)
        self._trim()
        thread = threading.Thread(target=self._run, args=(job, text), name=f"tts-{job.id[:8]}", daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> TtsJob | None:
        with self._lock:
            job = self._active.get(job_id)
        if job is not None:
            return job
        try:
            with SessionLocal() as session:
                record = session.get(TtsJobRecord, job_id)
        except Exception:  # noqa: BLE001
            logger.warning("读取合成记录失败 %s", job_id, exc_info=True)
            return None
        return self._to_job(record) if record is not None else None

    def recent(self) -> list[TtsJob]:
        """全部记录（含正在跑的那条），按时间倒序。"""

        jobs = [self._to_job(record) for record in self._records()]
        with self._lock:
            live = dict(self._active)
        # 正在跑的那条用内存版本覆盖：进度与句数只有内存里最新
        return [live.pop(job.id, job) for job in jobs] + list(live.values())

    def cancel(self, job_id: str) -> TtsJob | None:
        with self._lock:
            job = self._active.get(job_id)
        if job is None:
            # 已经跑完或不存在：把记录原样还回去，由接口层决定怎么回
            return self.get(job_id)
        job.cancel.set()
        if job.active:
            job.status = "cancelling"
            job.message = "正在取消"
            job.updated_at = time.time()
            self._store(job)
        return job

    def remove(self, job_id: str) -> bool:
        """删掉一条记录与它的音频文件；正在合成的返回 False（先取消）。"""

        with self._lock:
            if job_id in self._active:
                return False
        try:
            with SessionLocal() as session:
                record = session.get(TtsJobRecord, job_id)
                if record is None:
                    return False
                if record.audio_name:
                    (self.audio_dir() / record.audio_name).unlink(missing_ok=True)
                session.delete(record)
                session.commit()
        except Exception:  # noqa: BLE001
            logger.warning("删除合成记录失败 %s", job_id, exc_info=True)
            return False
        return True

    def clear_finished(self) -> int:
        """删掉所有已结束的记录与音频，返回删掉几条；正在跑的不动。"""

        with self._lock:
            live = set(self._active)
        removed = 0
        try:
            with SessionLocal() as session:
                for record in list(session.scalars(select(TtsJobRecord))):
                    if record.id in live:
                        continue
                    if record.audio_name:
                        (self.audio_dir() / record.audio_name).unlink(missing_ok=True)
                    session.delete(record)
                    removed += 1
                session.commit()
        except Exception:  # noqa: BLE001
            logger.warning("清空合成记录失败", exc_info=True)
        return removed

    def is_busy(self) -> bool:
        with self._lock:
            return any(item.active for item in self._active.values())

    def _trim(self) -> None:
        """超出上限时删掉最旧的记录（连同音频文件）。"""

        records = self._records()
        if len(records) <= MAX_HISTORY:
            return
        with self._lock:
            live = set(self._active)
        for record in records[MAX_HISTORY:]:
            if record.id in live:
                continue
            if record.audio_name:
                (self.audio_dir() / record.audio_name).unlink(missing_ok=True)
            try:
                with SessionLocal() as session:
                    stale = session.get(TtsJobRecord, record.id)
                    if stale is not None:
                        session.delete(stale)
                        session.commit()
            except Exception:  # noqa: BLE001
                logger.warning("清理旧合成记录失败 %s", record.id, exc_info=True)

    def _run(self, job: TtsJob, text: str) -> None:
        with self._run_lock:
            started = time.perf_counter()
            try:
                if job.cancel.is_set():
                    raise TtsCancelled("已取消")
                job.status = "running"
                job.message = "正在加载语音模型"
                job.updated_at = time.time()
                self._store(job)

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
                job.percent = 0
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
                self._store(job)
                with self._lock:
                    # 跑完就移出内存：记录已经在库里，之后一律读库
                    self._active.pop(job.id, None)


manager = TtsJobManager()


__all__ = ["TtsJob", "TtsJobManager", "TtsCancelled", "manager"]
