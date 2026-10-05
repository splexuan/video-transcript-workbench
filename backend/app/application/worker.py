from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.application.services import read_fallback_api_key, read_settings, safe_unlink_upload
from app.config import settings
from app.domain import (
    EngineUnavailableError,
    JobStage,
    JobStatus,
    MediaInfo,
    ModelEngine,
    TranscriptChunk,
)
from app.infrastructure import fallback_api, transcriber, whisper_engine
from app.infrastructure.bilibili import fetch_platform_subtitles
from app.infrastructure.cover_store import save_cover
from app.infrastructure.credential_store import (
    COOKIE_DOMAINS,
    CredentialError,
    materialize_cookie_file,
    read_netscape_cookies,
)
from app.infrastructure.database import SessionLocal
from app.infrastructure.media import MediaToolError, convert_to_wav, probe_media
from app.infrastructure.model_catalog import require_spec
from app.infrastructure.model_store import ensure_engine_ready, ensure_model_ready
from app.infrastructure.models import Document, Job, JobBatch, TranscriptSegment
from app.infrastructure.platform_media import (
    PlatformError,
    download_audio_source,
    fetch_subtitles,
    platform_label,
    resolve_video,
)
from app.infrastructure.text_variant import simplify_chunks
from app.infrastructure.transcriber import ModelNotFoundError, NoSpeechError

logger = logging.getLogger(__name__)

ACCURATE_MODE = "accurate"
FAST_MODE_LABEL = "极速文本"
ACCURATE_MODE_LABEL = "精准时间轴"

# 已接入的链接平台。除 B站外都没有可读取的平台字幕，解析后直接走音轨识别。
SUPPORTED_PLATFORMS = {"bilibili", "douyin", "kuaishou", "xiaohongshu", "wechat"}
PLATFORM_SUBTITLE_PLATFORMS = {"bilibili"}
# 本机没有解析方案的平台，只能走兜底解析接口：视频号（网关的 /api/wxsph）。
# 这条依赖是明确的：没配 API Key 时给出可操作提示，而不是伪装成「暂不支持」。
FALLBACK_ONLY_PLATFORMS = {"wechat"}


class JobCancelled(RuntimeError):
    pass


class UnsupportedSource(RuntimeError):
    pass


def format_clock(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds) // 1000
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def audio_stage_message(job: Job, label: str) -> str:
    """下载音轨前的提示；区分「主动跳过字幕」和「平台本来就没有字幕」。"""

    if not job.prefer_subtitle:
        return "已跳过平台字幕，正在下载音轨"
    if job.platform in PLATFORM_SUBTITLE_PLATFORMS:
        return "没有可用字幕，正在下载音轨"
    return f"{label}没有可读取的字幕，正在下载音轨"


def update_job(
    session: Session,
    job: Job,
    *,
    stage: JobStage | None = None,
    progress: int | None = None,
    message: str | None = None,
) -> None:
    if stage is not None:
        job.stage = stage.value
    if progress is not None:
        job.progress = max(0, min(progress, 100))
    if message is not None:
        job.message = message
    session.commit()


def ensure_active(session: Session, job: Job) -> None:
    session.refresh(job)
    if job.status == JobStatus.CANCELLED.value:
        raise JobCancelled("任务已取消")


def create_document(session: Session, job: Job, info: MediaInfo) -> Document:
    document = Document(
        title=info.title,
        platform=info.platform,
        source_type=job.source_type,
        source_value=job.source_value,
        # 文案库里要能看出这条是单条提取还是批量提取来的
        source_kind="batch" if job.batch_id else "single",
        status="processing",
        duration_seconds=info.duration_seconds,
        uploader=info.uploader,
        description=info.description,
    )
    session.add(document)
    session.flush()
    # 封面是带时效的远程地址，立刻另存到本机；失败也不影响提取
    document.cover_file = save_cover(document.id, info.thumbnail)
    job.document_id = document.id
    session.commit()
    return document


def save_transcript(
    session: Session,
    document: Document,
    chunks: list[TranscriptChunk],
    duration: float | None,
) -> None:
    # 落库前统一字形：faster-whisper 的中文输出常是繁体，识别原文也一并转，
    # 避免正文与原文两种字形对不上。
    chunks = simplify_chunks(chunks)
    document.segments.clear()
    document.segments.extend(
        TranscriptSegment(
            position=index,
            start_ms=chunk.start_ms,
            end_ms=chunk.end_ms,
            raw_text=chunk.text,
            text=chunk.text,
        )
        for index, chunk in enumerate(chunks)
    )
    document.duration_seconds = duration or document.duration_seconds
    document.word_count = sum(len(chunk.text.strip()) for chunk in chunks)
    document.status = "draft"
    session.commit()


def _credential_summary(platform: str, cookie_file: Path | None) -> str:
    """把本次任务实际带上的凭据写进日志，便于区分「没送到」和「被平台拒绝」。"""

    if cookie_file is None:
        return "未携带访问凭据"
    domain = COOKIE_DOMAINS.get(platform)
    if not domain:
        return "该平台不读取凭据"
    pairs = read_netscape_cookies(cookie_file, domain)
    names = "、".join(sorted(pairs)[:10]) or "无匹配条目"
    return f"携带 {len(pairs)} 条凭据（{names}）"


def error_details(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ModelNotFoundError):
        return "MODEL_NOT_FOUND", str(exc)
    if isinstance(exc, EngineUnavailableError):
        return "ENGINE_UNAVAILABLE", str(exc)
    if isinstance(exc, MediaToolError):
        return "MEDIA_TOOL_ERROR", str(exc)
    if isinstance(exc, CredentialError):
        return "COOKIE_REQUIRED", str(exc)
    if isinstance(exc, PlatformError):
        return exc.code, str(exc)
    if isinstance(exc, (NoSpeechError, whisper_engine.WhisperNoSpeechError)):
        return "NO_SPEECH", str(exc)
    if isinstance(exc, UnsupportedSource):
        return "UNSUPPORTED_SOURCE", str(exc)
    return "PROCESSING_ERROR", str(exc) or "处理失败"


class LocalWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with SessionLocal() as session:
            session.execute(
                update(Job)
                .where(Job.status == JobStatus.RUNNING.value)
                .values(
                    status=JobStatus.QUEUED.value,
                    stage=JobStage.WAITING.value,
                    message="应用重启，任务已恢复等待",
                )
            )
            session.commit()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="local-transcript-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _claim_next(self) -> str | None:
        with SessionLocal() as session:
            job = session.scalar(
                select(Job)
                .outerjoin(JobBatch, Job.batch_id == JobBatch.id)
                .where(
                    Job.status == JobStatus.QUEUED.value,
                    or_(Job.batch_id.is_(None), JobBatch.control_status == "active"),
                )
                .order_by(Job.created_at.asc(), Job.batch_position.asc())
                .limit(1)
            )
            if not job:
                return None
            job.status = JobStatus.RUNNING.value
            job.stage = JobStage.RESOLVING.value
            job.progress = 3
            job.message = "正在读取来源信息"
            session.commit()
            return job.id

    def _run(self) -> None:
        while not self._stop.is_set():
            job_id = self._claim_next()
            if not job_id:
                self._stop.wait(0.6)
                continue
            self._process(job_id)

    def _process(self, job_id: str) -> None:
        workspace = settings.work_dir / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if not job:
                    return
                chunks, info = self._extract(session, job, workspace)
                ensure_active(session, job)

                document = session.get(Document, job.document_id) if job.document_id else None
                if not document:
                    document = create_document(session, job, info)
                save_transcript(session, document, chunks, info.duration_seconds)

                job.status = JobStatus.COMPLETED.value
                job.stage = JobStage.DONE.value
                job.progress = 100
                if job.transcript_source == "subtitle":
                    job.message = "平台字幕提取完成"
                elif job.model_name:
                    # 模型名都以中文括号结尾，后面直接接中文，不留空格更整齐
                    job.message = f"已用 {job.model_name}识别完成"
                else:
                    job.message = "文案提取完成"
                session.commit()

                keep_media = bool(read_settings(session)["keep_media"])
                if not keep_media:
                    shutil.rmtree(workspace, ignore_errors=True)
                    if job.source_type == "file":
                        safe_unlink_upload(job.source_value)
        except JobCancelled:
            logger.info("Job %s cancelled", job_id)
            self._cleanup_incomplete(job_id, workspace)
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            code, message = error_details(exc)
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job and job.status != JobStatus.CANCELLED.value:
                    job.status = JobStatus.FAILED.value
                    job.message = message
                    job.error_code = code
                    job.error_message = message
                    session.commit()
            self._cleanup_incomplete(job_id, workspace)

    def _cleanup_incomplete(self, job_id: str, workspace: Path) -> None:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if not job:
                return
            document = session.get(Document, job.document_id) if job.document_id else None
            if document and not document.segments:
                job.document_id = None
                session.delete(document)
                session.commit()
            if job.source_type == "file":
                safe_unlink_upload(job.source_value)
        shutil.rmtree(workspace, ignore_errors=True)

    def _extract(
        self,
        session: Session,
        job: Job,
        workspace: Path,
    ) -> tuple[list[TranscriptChunk], MediaInfo]:
        ensure_active(session, job)
        if job.source_type == "file":
            source = Path(job.source_value)
            if not source.is_file():
                raise MediaToolError("导入的本地文件不存在")
            info = probe_media(source)
            document = create_document(session, job, info)
            update_job(session, job, stage=JobStage.DOWNLOADING, progress=18, message="正在提取本地音轨")
            wav_path = convert_to_wav(source, workspace / "audio.wav")
            ensure_active(session, job)
            return self._transcribe(session, job, document, wav_path, info)

        if job.platform not in SUPPORTED_PLATFORMS:
            raise UnsupportedSource(
                "当前版本已接入本地文件、B站、抖音、快手、小红书和视频号，其他平台正在开发"
            )
        return self._extract_link(session, job, workspace)

    def _extract_link(
        self,
        session: Session,
        job: Job,
        workspace: Path,
    ) -> tuple[list[TranscriptChunk], MediaInfo]:
        """B站 / 抖音 / 小红书：先取平台字幕，没有字幕再下载音轨在本机识别。"""

        label = platform_label(job.platform)
        cookie_file: Path | None = None
        credential_error: CredentialError | None = None
        if job.platform not in FALLBACK_ONLY_PLATFORMS:
            # 需要登录态的平台必须先有 Cookie；没配置时下载器会给出可读的错误。
            try:
                cookie_file = materialize_cookie_file(job.platform)
            except CredentialError as exc:
                # 没配凭据时先别急着失败：配了兜底 Key 就交给兜底接口试一次
                # （实测兜底端点能解析抖音，这条分支让「没 Cookie 的抖音」也有救）
                if read_fallback_api_key(session) is None:
                    raise
                credential_error = exc
        try:
            # 主链路失败后由兜底接口解析出来的结果（含直链）；主链路成功时保持为 None
            fallback: fallback_api.FallbackMedia | None = None
            if job.platform in FALLBACK_ONLY_PLATFORMS:
                fallback = self._resolve_fallback_only(session, job)
                info = fallback.to_media_info(job.platform)
            else:
                try:
                    if credential_error is not None:
                        raise credential_error
                    info = resolve_video(job.source_value, job.platform, cookie_file)
                except (PlatformError, CredentialError) as exc:
                    # 排查用：平台报「需要凭据」时，日志里要能看出这次到底带了什么，
                    # 否则「已配置凭据但被拒」和「凭据没送到」两种情况长得一模一样
                    logger.warning(
                        "任务 %s 解析失败（%s）：%s",
                        job.id,
                        job.platform,
                        _credential_summary(job.platform, cookie_file),
                    )
                    fallback = self._resolve_via_fallback(session, job, exc)
                    info = fallback.to_media_info(job.platform)
            document = create_document(session, job, info)

            if job.prefer_subtitle and job.platform in PLATFORM_SUBTITLE_PLATFORMS:
                update_job(session, job, stage=JobStage.FETCHING_SUBTITLE, progress=15, message="正在读取平台字幕")
                # 先走平台字幕接口（免登录也能拿 AI 字幕，带 Cookie 时覆盖面更广），
                # 拿不到再用 yt-dlp 试一次
                chunks = fetch_platform_subtitles(job.source_value, cookie_file)
                ensure_active(session, job)
                if not chunks:
                    chunks = fetch_subtitles(job.source_value, workspace, job.platform, cookie_file)
                    ensure_active(session, job)
                if chunks:
                    job.transcript_source = "subtitle"
                    update_job(session, job, stage=JobStage.WRITING, progress=88, message="正在整理平台字幕")
                    return chunks, info

            update_job(
                session,
                job,
                stage=JobStage.DOWNLOADING,
                progress=25,
                message=audio_stage_message(job, label),
            )
            source = self._obtain_media(session, job, workspace, cookie_file, fallback)
            ensure_active(session, job)
            wav_path = convert_to_wav(source, workspace / "audio.wav")
            return self._transcribe(session, job, document, wav_path, info)
        finally:
            # Cookie 只在本次任务里短暂落盘，结束立即删除。
            if cookie_file is not None:
                cookie_file.unlink(missing_ok=True)

    def _resolve_via_fallback(
        self,
        session: Session,
        job: Job,
        cause: PlatformError | CredentialError,
    ) -> fallback_api.FallbackMedia:
        """主链路解析失败后改用兜底接口；没配 Key 时原样抛回主链路的错误。

        没配 Key 是常态，所以这条分支必须与接入兜底之前完全一致：不额外发请求、
        不改任务提示，错误也照旧抛出（主链路的说明最具体）。
        """

        api_key = read_fallback_api_key(session)
        if not api_key:
            raise cause
        try:
            return self._call_fallback(session, job, api_key, reason="主链路解析失败")
        except fallback_api.FallbackError as exc:
            # 兜底也没成：主链路的原因更具体，放前面，兜底的原因跟在后面。
            # 用原异常的类型重建，保留 COOKIE_REQUIRED 这类错误码（前端据此给操作引导）。
            raise type(cause)(f"{cause}；兜底解析也没成功：{exc}") from exc

    def _resolve_fallback_only(self, session: Session, job: Job) -> fallback_api.FallbackMedia:
        """本机没有解析方案的平台（视频号）：只能走兜底接口，没配 Key 时给可操作提示。"""

        label = platform_label(job.platform)
        api_key = read_fallback_api_key(session)
        if not api_key:
            raise PlatformError(
                f"{label}需要通过兜底解析接口提取：请在「设置」页填写 API Key 后重试"
            )
        try:
            return self._call_fallback(session, job, api_key, reason=f"{label}由兜底解析接口处理")
        except fallback_api.FallbackError as exc:
            raise PlatformError(f"{label}处理失败：{exc}") from exc

    @staticmethod
    def _call_fallback(
        session: Session,
        job: Job,
        api_key: str,
        *,
        reason: str,
    ) -> fallback_api.FallbackMedia:
        """调用兜底接口并把结果写进任务提示。异常交给调用方决定怎么合并文案。"""

        media = fallback_api.resolve(job.source_value, api_key, job.platform)
        logger.info("兜底接口解析成功：%s", media.title)
        update_job(session, job, message=f"{reason}（{media.title}）")
        return media

    def _obtain_media(
        self,
        session: Session,
        job: Job,
        workspace: Path,
        cookie_file: Path | None,
        fallback: fallback_api.FallbackMedia | None,
    ) -> Path:
        """拿到可转码的视频文件：主链路优先，失败时改用兜底直链。"""

        if fallback is not None:
            # 主链路刚失败过一次，这里直接用兜底直链，不做注定失败的重复请求
            return self._download_fallback(fallback, workspace)
        try:
            return download_audio_source(job.source_value, workspace, job.platform, cookie_file)
        except PlatformError as exc:
            return self._download_fallback(self._resolve_via_fallback(session, job, exc), workspace)

    @staticmethod
    def _download_fallback(media: fallback_api.FallbackMedia, workspace: Path) -> Path:
        try:
            return fallback_api.download(media, workspace)
        except fallback_api.FallbackError as exc:
            # 用 PlatformError 包一层：与平台下载失败共用同一个错误码
            raise PlatformError(str(exc)) from exc

    def _transcribe(
        self,
        session: Session,
        job: Job,
        document: Document,
        wav_path: Path,
        info: MediaInfo,
    ) -> tuple[list[TranscriptChunk], MediaInfo]:
        job.transcript_source = "asr"
        # 用户在首页选了模型就用它；没选（旧任务或直接调接口）才按 mode 走
        model_id = job.requested_model_id
        accurate = (
            require_spec(model_id).engine == ModelEngine.FASTER_WHISPER
            if model_id
            else job.mode == ACCURATE_MODE
        )
        if accurate:
            chunks, duration = self._transcribe_accurate(session, job, wav_path, model_id)
        else:
            chunks, duration = self._transcribe_fast(session, job, wav_path, model_id)

        info.duration_seconds = duration or info.duration_seconds
        document.duration_seconds = info.duration_seconds
        update_job(session, job, stage=JobStage.WRITING, progress=90, message="正在生成可编辑文案")
        return chunks, info

    def _transcribe_fast(
        self,
        session: Session,
        job: Job,
        wav_path: Path,
        model_id: str | None = None,
    ) -> tuple[list[TranscriptChunk], float]:
        spec, _model_dir = (
            ensure_model_ready(model_id, FAST_MODE_LABEL)
            if model_id
            else ensure_engine_ready(ModelEngine.SENSEVOICE, FAST_MODE_LABEL)
        )
        job.model_id = spec.id
        job.model_name = spec.name
        update_job(
            session,
            job,
            stage=JobStage.TRANSCRIBING,
            progress=58,
            message=f"正在使用 {spec.name} 识别",
        )

        def progress(current: int, total: int) -> None:
            ensure_active(session, job)
            value = 58 + int(current / max(total, 1) * 28)
            update_job(
                session,
                job,
                stage=JobStage.TRANSCRIBING,
                progress=value,
                message=f"正在识别语音（{current}/{total} 段）",
            )

        return transcriber.transcribe(wav_path, progress)

    def _transcribe_accurate(
        self,
        session: Session,
        job: Job,
        wav_path: Path,
        model_id: str | None = None,
    ) -> tuple[list[TranscriptChunk], float]:
        spec, model_dir = (
            ensure_model_ready(model_id, ACCURATE_MODE_LABEL)
            if model_id
            else ensure_engine_ready(ModelEngine.FASTER_WHISPER, ACCURATE_MODE_LABEL)
        )
        job.model_id = spec.id
        job.model_name = spec.name
        update_job(
            session,
            job,
            stage=JobStage.TRANSCRIBING,
            progress=55,
            message=f"正在使用 {spec.name} 生成精准时间轴",
        )

        def progress(processed_ms: int, total_ms: int) -> None:
            ensure_active(session, job)
            value = 55 + int(processed_ms / max(total_ms, 1) * 33)
            update_job(
                session,
                job,
                stage=JobStage.TRANSCRIBING,
                progress=value,
                message=f"正在生成精准时间轴（{format_clock(processed_ms)}/{format_clock(total_ms)}）",
            )

        return whisper_engine.transcribe(wav_path, model_dir, progress)


local_worker = LocalWorker()
