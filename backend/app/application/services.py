import logging
import shutil
from pathlib import Path

from sqlalchemy import select
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
    TranscriptSegment,
)
from app.schemas import JobCreate, SegmentWrite, SettingPatch

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


def create_job(session: Session, payload: JobCreate) -> Job:
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
    job = Job(
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
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


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
