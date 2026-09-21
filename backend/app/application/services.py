import hashlib
import json
import logging
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.application.platforms import detect_platform, extract_source_url
from app.application.text_formatting import reflow_segments
from app.config import settings
from app.domain import JobStage, JobStatus, ModelEngine
from app.infrastructure.cover_store import delete_cover
from app.infrastructure.credential_store import CredentialError, protect_secret, reveal_secret
from app.infrastructure.model_catalog import get_spec
from app.infrastructure.models import (
    AppSetting,
    Document,
    Job,
    JobBatch,
    TranscriptSegment,
)
from app.schemas import (
    JobBatchCreate,
    JobBatchDetailRead,
    JobBatchPreflightItem,
    JobBatchPreflightRead,
    JobBatchRead,
    JobCreate,
    JobRead,
    SegmentWrite,
    SettingPatch,
)

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "theme": "system",
    "default_model": "sensevoice-small",
    # 平台字幕（尤其 B站 AI 字幕）整段没有标点，读起来远不如识别结果；
    # 默认走识别，需要时可以在设置里开回来，届时文案按字幕原样一行一句展示。
    "prefer_subtitle": "false",
    "keep_media": "false",
    "storage_path": "",
    # 兜底解析接口的 API Key：加密后存在同一张设置表里，读取时只回传「是否已配置」
    "fallback_api_key": "",
}

# 兜底解析（第三方聚合接口）的 Key 设置项：只写不读，明文不出后端
FALLBACK_API_KEY_SETTING = "fallback_api_key"

# 旧版本存的是 auto / fast / accurate，读取时映射到具体模型，避免升级后设置失效。
LEGACY_MODE_MODELS = {
    "auto": "sensevoice-small",
    "fast": "sensevoice-small",
    "accurate": "faster-whisper-small",
}


def _build_job(
    payload: JobCreate,
    *,
    batch_position: int | None = None,
    display_name: str | None = None,
) -> Job:
    # 用户经常把整段分享文案贴进来，先取出其中的链接再判定平台
    source = (
        extract_source_url(payload.source)
        if payload.source_type == "url"
        else payload.source
    )
    platform = detect_platform(payload.source_type, source)
    mode = payload.mode
    if payload.model_id:
        spec = get_spec(payload.model_id)
        if spec is not None:
            # mode 是给旧逻辑和列表展示用的，按所选模型的引擎推导，保证含义一致
            mode = "accurate" if spec.engine == ModelEngine.FASTER_WHISPER else "fast"
    return Job(
        batch_position=batch_position,
        display_name=display_name,
        platform=platform.value,
        source_type=payload.source_type,
        source_value=source,
        mode=mode,
        requested_model_id=payload.model_id,
        prefer_subtitle=payload.prefer_subtitle,
        status=JobStatus.QUEUED.value,
        stage=JobStage.WAITING.value,
        progress=0,
        message="已加入本地任务队列",
    )


def create_job(session: Session, payload: JobCreate) -> Job:
    job = _build_job(payload)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


class JobBatchValidationError(Exception):
    """批次里有重复、空白或不支持的平台链接。"""


class JobBatchIdempotencyConflict(Exception):
    """相同幂等键被用于另一份批次内容。"""


class JobBatchControlError(Exception):
    """批次当前状态不允许执行请求的控制动作。"""


def preflight_job_batch(sources: list[str]) -> JobBatchPreflightRead:
    """逐行规范化批量链接，并明确标出重复项与不支持项。"""

    items: list[JobBatchPreflightItem] = []
    seen: set[str] = set()
    for position, raw_source in enumerate(sources, start=1):
        raw = raw_source.strip()
        normalized = extract_source_url(raw) if raw else ""
        platform = detect_platform("url", normalized)
        if not raw or len(raw) > 4000 or platform.value == "unknown":
            message = "链接为空或不是当前支持的平台"
            if len(raw) > 4000:
                message = "单条分享文案不能超过 4000 个字符"
            items.append(
                JobBatchPreflightItem(
                    position=position,
                    raw_source=raw,
                    normalized_source=normalized or None,
                    platform=platform.value,
                    status="unsupported",
                    message=message,
                )
            )
            continue
        if normalized in seen:
            items.append(
                JobBatchPreflightItem(
                    position=position,
                    raw_source=raw,
                    normalized_source=normalized,
                    platform=platform.value,
                    status="duplicate",
                    message="与批次中的前一条链接重复",
                )
            )
            continue
        seen.add(normalized)
        items.append(
            JobBatchPreflightItem(
                position=position,
                raw_source=raw,
                normalized_source=normalized,
                platform=platform.value,
                status="valid",
                message="可以加入批次",
            )
        )

    valid_count = sum(item.status == "valid" for item in items)
    duplicate_count = sum(item.status == "duplicate" for item in items)
    unsupported_count = sum(item.status == "unsupported" for item in items)
    return JobBatchPreflightRead(
        total_count=len(items),
        valid_count=valid_count,
        duplicate_count=duplicate_count,
        unsupported_count=unsupported_count,
        can_submit=valid_count == len(items),
        items=items,
    )


def _batch_fingerprint(payload: JobBatchCreate, normalized_sources: list[str]) -> str:
    canonical = json.dumps(
        {
            "title": (payload.title or "").strip(),
            "sources": normalized_sources,
            "mode": payload.mode,
            "model_id": payload.model_id,
            "prefer_subtitle": payload.prefer_subtitle,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_job_batch(session: Session, batch_id: str) -> JobBatch | None:
    stmt = (
        select(JobBatch)
        .options(selectinload(JobBatch.jobs))
        .where(JobBatch.id == batch_id)
    )
    return session.scalar(stmt)


def list_job_batches(session: Session, limit: int = 20) -> list[JobBatch]:
    stmt = (
        select(JobBatch)
        .options(selectinload(JobBatch.jobs))
        .order_by(JobBatch.created_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def create_job_batch(
    session: Session,
    payload: JobBatchCreate,
    *,
    parent_batch_id: str | None = None,
) -> JobBatch:
    """用一次数据库事务创建批次和全部子任务。"""

    preflight = preflight_job_batch(payload.sources)
    if not preflight.can_submit:
        problem_positions = [str(item.position) for item in preflight.items if item.status != "valid"]
        raise JobBatchValidationError(
            f"第 {', '.join(problem_positions)} 条链接重复、为空或暂不支持，请修正后重试"
        )
    normalized_sources = [item.normalized_source or "" for item in preflight.items]
    fingerprint = _batch_fingerprint(payload, normalized_sources)
    existing = session.scalar(
        select(JobBatch)
        .options(selectinload(JobBatch.jobs))
        .where(JobBatch.client_request_id == payload.client_request_id)
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise JobBatchIdempotencyConflict("该提交标识已用于另一份批次，请刷新后重试")
        return existing

    batch = JobBatch(
        title=(payload.title or "").strip() or f"批量提取（{len(normalized_sources)} 条）",
        kind="url",
        control_status="active",
        expected_count=len(normalized_sources),
        client_request_id=payload.client_request_id,
        request_fingerprint=fingerprint,
        parent_batch_id=parent_batch_id,
    )
    for position, item in enumerate(preflight.items, start=1):
        batch.jobs.append(
            _build_job(
                JobCreate(
                    source_type="url",
                    source=item.normalized_source or "",
                    mode=payload.mode,
                    model_id=payload.model_id,
                    prefer_subtitle=payload.prefer_subtitle,
                ),
                batch_position=position,
                display_name=item.raw_source[:300],
            )
        )
    session.add(batch)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(JobBatch)
            .options(selectinload(JobBatch.jobs))
            .where(JobBatch.client_request_id == payload.client_request_id)
        )
        if existing is None:
            raise
        if existing.request_fingerprint != fingerprint:
            raise JobBatchIdempotencyConflict("该提交标识已用于另一份批次，请刷新后重试")
        return existing
    session.refresh(batch)
    return batch


def serialize_job_batch(batch: JobBatch, *, include_jobs: bool = False) -> JobBatchRead:
    counts = {
        status: sum(job.status == status for job in batch.jobs)
        for status in ("queued", "running", "completed", "failed", "cancelled")
    }
    total = batch.expected_count
    terminal_count = counts["completed"] + counts["failed"] + counts["cancelled"]
    progress_units = terminal_count + sum(
        job.progress / 100 for job in batch.jobs if job.status == "running"
    )
    progress = round(progress_units / total * 100) if total else 0

    if batch.control_status == "cancelled":
        derived_status = "cancelled"
    elif batch.control_status == "paused" and (counts["queued"] or counts["running"]):
        derived_status = "paused"
    elif counts["running"]:
        derived_status = "running"
    elif counts["queued"]:
        derived_status = "running" if terminal_count else "queued"
    elif counts["completed"] == total:
        derived_status = "completed"
    elif counts["completed"] and (counts["failed"] or counts["cancelled"]):
        derived_status = "partial_failed"
    elif counts["failed"]:
        derived_status = "failed"
    else:
        derived_status = "cancelled"

    updated_at = max([batch.updated_at, *(job.updated_at for job in batch.jobs)])
    summary = JobBatchRead(
        id=batch.id,
        title=batch.title,
        kind=batch.kind,
        control_status=batch.control_status,
        status=derived_status,
        progress=progress,
        total_count=total,
        queued_count=counts["queued"],
        running_count=counts["running"],
        completed_count=counts["completed"],
        failed_count=counts["failed"],
        cancelled_count=counts["cancelled"],
        parent_batch_id=batch.parent_batch_id,
        created_at=batch.created_at,
        updated_at=updated_at,
    )
    if not include_jobs:
        return summary
    return JobBatchDetailRead(
        **summary.model_dump(),
        jobs=[JobRead.model_validate(job) for job in batch.jobs],
    )


def pause_job_batch(session: Session, batch: JobBatch) -> JobBatch:
    summary = serialize_job_batch(batch)
    if summary.status in {"completed", "failed", "partial_failed", "cancelled"}:
        raise JobBatchControlError("已结束的批次不能暂停")
    batch.control_status = "paused"
    session.commit()
    return batch


def resume_job_batch(session: Session, batch: JobBatch) -> JobBatch:
    if batch.control_status == "cancelled":
        raise JobBatchControlError("已取消的批次不能恢复")
    if serialize_job_batch(batch).status in {"completed", "failed", "partial_failed"}:
        raise JobBatchControlError("已结束的批次不能恢复")
    batch.control_status = "active"
    session.commit()
    return batch


def cancel_job_batch(session: Session, batch: JobBatch) -> JobBatch:
    if batch.control_status != "cancelled" and serialize_job_batch(batch).status in {
        "completed",
        "failed",
        "partial_failed",
    }:
        raise JobBatchControlError("已结束的批次不能取消")
    batch.control_status = "cancelled"
    for job in batch.jobs:
        if job.status in {"queued", "running"}:
            job.status = "cancelled"
            job.message = "批次已取消"
    session.commit()
    return batch


def retry_failed_job_batch(session: Session, batch: JobBatch) -> JobBatch:
    if any(job.status in {"queued", "running"} for job in batch.jobs):
        raise JobBatchControlError("批次还在处理中，结束后再重试失败项")
    failed_jobs = [job for job in batch.jobs if job.status == "failed"]
    if not failed_jobs:
        raise JobBatchControlError("这个批次没有失败任务")
    first_job = failed_jobs[0]
    return create_job_batch(
        session,
        JobBatchCreate(
            title=f"{batch.title[:294]}（重试）",
            sources=[job.source_value for job in failed_jobs],
            mode=first_job.mode,
            model_id=first_job.requested_model_id,
            prefer_subtitle=first_job.prefer_subtitle,
            client_request_id=f"retry-{uuid4()}",
        ),
        parent_batch_id=batch.id,
    )


class JobRetryError(Exception):
    """原任务无法重试（例如本地文件已被清理）。"""


def retry_job(session: Session, job: Job) -> Job:
    """按原任务的输入与选项重新排一次队，省去用户重新粘贴链接。

    本地文件在任务结束时已清理，没法重试；链接任务复制参数重建即可。
    """

    if job.source_type == "file":
        raise JobRetryError("本地文件在任务结束后已清理，请重新导入文件")
    return create_job(
        session,
        JobCreate(
            source_type="url",
            source=job.source_value,
            mode=job.mode,
            model_id=job.requested_model_id,
            prefer_subtitle=job.prefer_subtitle,
        ),
    )


def list_documents(session: Session, query: str | None = None) -> list[Document]:
    """文案列表；关键词同时匹配标题与正文，方便按记得的一句话找回文案。"""

    stmt = select(Document).order_by(Document.updated_at.desc())
    keyword = (query or "").strip()
    if keyword:
        matched_by_text = select(TranscriptSegment.document_id).where(
            TranscriptSegment.text.contains(keyword)
        )
        stmt = stmt.where(
            Document.title.contains(keyword) | Document.id.in_(matched_by_text)
        )
    return list(session.scalars(stmt))


def get_document(session: Session, document_id: str) -> Document | None:
    stmt = (
        select(Document)
        .options(selectinload(Document.segments))
        .where(Document.id == document_id)
    )
    return session.scalar(stmt)


def find_document_media(session: Session, document_id: str) -> Path | None:
    """返回文档对应的音轨文件；没保留时说明已在任务结束时清理。

    任务工作目录默认在完成后删除，开启设置里的「保留原始音视频」后会留下转码后的
    `audio.wav`，浏览器可以直接播放，用于校对时对照收听。
    这里只给音轨：文案提取要的是声音，播放原始视频除了多加载几 MB 没有别的用处。
    """

    job = session.scalar(select(Job).where(Job.document_id == document_id))
    if job is None:
        return None
    audio = settings.work_dir / job.id / "audio.wav"
    return audio if audio.is_file() else None


TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}


class DocumentInUseError(Exception):
    """文案还挂着未结束的任务，不能删除。"""


def safe_unlink_upload(source_value: str) -> None:
    """删除本地导入的临时文件；只清收件箱里的，防止误删用户自己的源文件。"""

    source = Path(source_value).resolve()
    inbox = settings.inbox_dir.resolve()
    if source.parent == inbox:
        source.unlink(missing_ok=True)


def delete_job_record(session: Session, job: Job) -> None:
    """删除终态任务记录：工作目录与本地导入的临时文件一并清理。

    注意 completed 任务的工作目录里可能有「保留原始音视频」留下的音轨，
    删除记录意味着放弃对照收听，文案本身不受影响。
    """

    shutil.rmtree(settings.work_dir / job.id, ignore_errors=True)
    if job.source_type == "file":
        safe_unlink_upload(job.source_value)
    session.delete(job)
    session.commit()


def delete_document(session: Session, document: Document) -> None:
    """删除文案：分段、封面、关联任务记录与工作目录一并清掉，不可恢复。"""

    if any(job.status not in TERMINAL_JOB_STATUSES for job in document.jobs):
        raise DocumentInUseError("这条文案还有任务在处理中，等任务结束或取消后再删除")
    for job in document.jobs:
        shutil.rmtree(settings.work_dir / job.id, ignore_errors=True)
        if job.source_type == "file":
            safe_unlink_upload(job.source_value)
        # 批次子任务是批次进度的事实记录；删除文案时只断开引用，不能把它一并删掉。
        if job.batch_id:
            job.document_id = None
        else:
            session.delete(job)
    delete_cover(document.cover_file)
    session.delete(document)
    session.commit()


def replace_segments(
    session: Session,
    document: Document,
    items: list[SegmentWrite],
) -> Document:
    # 编辑后的 text 可以变化，但 raw_text 必须始终保存第一次识别的原文。
    # 前端会回传稳定的 segment id；position 仅作为兼容旧客户端的回退。
    previous_by_id = {item.id: item for item in document.segments}
    previous_by_position = {item.position: item for item in document.segments}

    def original_text(item: SegmentWrite) -> str:
        previous = previous_by_id.get(item.id) if item.id is not None else None
        previous = previous or previous_by_position.get(item.position)
        return previous.raw_text if previous is not None else item.text

    document.segments.clear()
    document.segments.extend(
        TranscriptSegment(
            position=item.position,
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            raw_text=original_text(item),
            text=item.text,
        )
        for item in items
    )
    document.word_count = sum(len(item.text.strip()) for item in items)
    session.commit()
    return get_document(session, document.id) or document


def auto_format_segments(session: Session, document: Document) -> Document:
    """智能分句：把段落/长句文本重排为句级分段并保存。"""

    ordered = sorted(document.segments, key=lambda item: item.position)
    raw_sources = [
        (segment.start_ms, segment.end_ms, segment.raw_text)
        for segment in ordered
    ]
    all_raw_text = "\n".join(dict.fromkeys(raw_text for _, _, raw_text in raw_sources))
    chunks = reflow_segments(
        [(segment.start_ms, segment.end_ms, segment.text) for segment in ordered]
    )
    document.segments.clear()
    def raw_text_for(start_ms: int | None, end_ms: int | None, fallback: str) -> str:
        overlaps: list[str] = []
        for source_start, source_end, raw_text in raw_sources:
            if start_ms is None or end_ms is None or source_start is None or source_end is None:
                continue
            if source_start < end_ms and source_end > start_ms and raw_text not in overlaps:
                overlaps.append(raw_text)
        return "\n".join(overlaps) or all_raw_text or fallback

    document.segments.extend(
        TranscriptSegment(
            position=index,
            start_ms=chunk.start_ms,
            end_ms=chunk.end_ms,
            raw_text=raw_text_for(chunk.start_ms, chunk.end_ms, chunk.text),
            text=chunk.text,
        )
        for index, chunk in enumerate(chunks)
    )
    document.word_count = sum(len(chunk.text.strip()) for chunk in chunks)
    session.commit()
    return get_document(session, document.id) or document


def read_settings(session: Session) -> dict[str, str | bool]:
    stored = {item.key: item.value for item in session.scalars(select(AppSetting))}
    merged = DEFAULT_SETTINGS | stored
    default_model = stored.get("default_model") or LEGACY_MODE_MODELS.get(
        stored.get("default_mode", ""),
        DEFAULT_SETTINGS["default_model"],
    )
    return {
        "theme": merged["theme"],
        "default_model": default_model,
        "prefer_subtitle": merged["prefer_subtitle"] == "true",
        "keep_media": merged["keep_media"] == "true",
        "storage_path": merged["storage_path"],
        # Key 明文不回传，前端只知道配了没有
        "fallback_api_key_set": read_fallback_api_key(session) is not None,
    }


def read_fallback_api_key(session: Session) -> str | None:
    """读取兜底解析接口的 API Key；这是唯一的解密入口，明文不出本模块。"""

    setting = session.get(AppSetting, FALLBACK_API_KEY_SETTING)
    if setting is None or not setting.value:
        return None
    try:
        return reveal_secret(setting.value)
    except (CredentialError, ValueError) as exc:
        # 换机器或凭据库损坏时解不出来：按未配置处理，让用户重新填写
        logger.warning("兜底解析 API Key 解密失败：%s", exc)
        return None


def write_fallback_api_key(session: Session, api_key: str) -> None:
    """写入或清除兜底解析 API Key（落库前加密，空串表示清除）。"""

    setting = session.get(AppSetting, FALLBACK_API_KEY_SETTING)
    if not api_key:
        if setting is not None:
            session.delete(setting)
        return
    stored = protect_secret(api_key)
    if setting is not None:
        setting.value = stored
        return
    session.add(AppSetting(key=FALLBACK_API_KEY_SETTING, value=stored))


def update_settings(session: Session, patch: SettingPatch) -> dict[str, str | bool]:
    payload = patch.model_dump(exclude_none=True)
    # Key 要加密保存、且空串代表清除，不能走下面「原样入库」的通用分支
    api_key = payload.pop("fallback_api_key", None)
    if api_key is not None:
        write_fallback_api_key(session, api_key.strip())
    for key, value in payload.items():
        stored_value = str(value).lower() if isinstance(value, bool) else str(value)
        setting = session.get(AppSetting, key)
        if setting:
            setting.value = stored_value
        else:
            session.add(AppSetting(key=key, value=stored_value))
    session.commit()
    return read_settings(session)
