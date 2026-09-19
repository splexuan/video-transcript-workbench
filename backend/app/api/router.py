from __future__ import annotations

import re
from importlib.util import find_spec
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.exporters import export_document
from app.application.services import (
    TERMINAL_JOB_STATUSES,
    DocumentInUseError,
    JobRetryError,
    auto_format_segments,
    create_job,
    delete_document,
    delete_job_record,
    find_document_media,
    get_document,
    list_documents,
    read_settings,
    replace_segments,
    retry_job,
    update_settings,
)
from app.config import settings
from app.domain import ModelNotFoundError
from app.infrastructure.cover_store import cover_path
from app.infrastructure.credential_store import all_status
from app.infrastructure.database import get_session
from app.infrastructure.media import tools_ready
from app.infrastructure.model_catalog import require_spec
from app.infrastructure.models import Document, Job
from app.schemas import (
    DocumentDetail,
    DocumentRead,
    DocumentUpdate,
    JobCreate,
    JobRead,
    SegmentsReplace,
    SettingPatch,
)

router = APIRouter(prefix="/api")
SessionDep = Annotated[Session, Depends(get_session)]


def validated_model_id(model_id: str | None) -> str | None:
    """校验首页传来的模型 id，避免把未知模型写进任务。"""

    if not model_id:
        return None
    try:
        require_spec(model_id)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return model_id


ALLOWED_MEDIA_SUFFIXES = {
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
}


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "name": settings.app_name, "version": settings.app_version}


@router.post("/jobs", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def enqueue_job(payload: JobCreate, session: SessionDep) -> Job:
    if payload.source_type == "file":
        raise HTTPException(status_code=400, detail="本地文件必须通过文件选择器导入")
    payload.model_id = validated_model_id(payload.model_id)
    return create_job(session, payload)


@router.post("/jobs/upload", response_model=JobRead, status_code=status.HTTP_201_CREATED)
async def upload_job(
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    mode: Annotated[str, Form()] = "auto",
    model_id: Annotated[str | None, Form()] = None,
    prefer_subtitle: Annotated[bool, Form()] = True,
) -> Job:
    original_name = file.filename or "media"
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_MEDIA_SUFFIXES:
        raise HTTPException(status_code=400, detail="不支持此文件格式")
    if mode not in {"auto", "fast", "accurate"}:
        raise HTTPException(status_code=400, detail="不支持此提取模式")

    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(original_name).name).strip()
    safe_name = safe_name[-180:] or f"media{suffix}"
    destination = settings.inbox_dir / f"{uuid4()}__{safe_name}"
    total = 0
    limit = settings.upload_max_mb * 1024 * 1024
    try:
        with destination.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(status_code=413, detail=f"文件不能超过 {settings.upload_max_mb} MB")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="文件为空")
        return create_job(
            session,
            JobCreate(
                source_type="file",
                source=str(destination),
                mode=mode,
                model_id=validated_model_id(model_id),
                prefer_subtitle=prefer_subtitle,
            ),
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@router.get("/jobs", response_model=list[JobRead])
def get_jobs(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    return list(session.scalars(stmt))


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobRead,
    status_code=status.HTTP_201_CREATED,
)
def retry_failed_job(job_id: str, session: SessionDep) -> Job:
    """按原参数重新排队一次；失败后重试不必回到工作台重新粘贴链接。"""

    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status not in TERMINAL_JOB_STATUSES:
        raise HTTPException(status_code=400, detail="任务还在进行中，不需要重试")
    try:
        return retry_job(session, job)
    except JobRetryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: str, session: SessionDep) -> Job:
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status not in {"completed", "failed", "cancelled"}:
        job.status = "cancelled"
        job.message = "已取消"
        session.commit()
    return job


@router.delete("/jobs/{job_id}")
def remove_job(job_id: str, session: SessionDep) -> dict[str, str | bool]:
    """删除一条已经结束的任务记录；进行中的任务要先取消。"""

    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status not in TERMINAL_JOB_STATUSES:
        raise HTTPException(status_code=400, detail="任务还在进行中，请先取消再删除")
    delete_job_record(session, job)
    return {"id": job_id, "removed": True}


@router.get("/documents", response_model=list[DocumentRead])
def documents(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> list[Document]:
    return list_documents(session, q)


def _document_detail(session: Session, document: Document) -> DocumentDetail:
    """文档详情：附带来源作品元信息、文案来源与「原始音轨是否还在本机」。

    前端据此决定要不要渲染播放器，以及全文是合并成段落还是保持一行一句。
    """

    payload = DocumentDetail.model_validate(document)
    payload.media_available = find_document_media(session, document.id) is not None
    job = session.scalar(select(Job).where(Job.document_id == document.id))
    payload.transcript_source = job.transcript_source if job else None
    return payload


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def document_detail(
    document_id: str,
    session: SessionDep,
) -> DocumentDetail:
    document = get_document(session, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="文案不存在")
    return _document_detail(session, document)


@router.patch("/documents/{document_id}", response_model=DocumentRead)
def edit_document(
    document_id: str,
    payload: DocumentUpdate,
    session: SessionDep,
) -> Document:
    document = session.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="文案不存在")
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(document, key, value)
    session.commit()
    return document


@router.delete("/documents/{document_id}")
def remove_document(document_id: str, session: SessionDep) -> dict[str, str | bool]:
    """删除文案：分段、封面、关联任务记录与工作目录一并清掉，不可恢复。"""

    document = session.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="文案不存在")
    try:
        delete_document(session, document)
    except DocumentInUseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": document_id, "removed": True}


@router.put("/documents/{document_id}/segments", response_model=DocumentDetail)
def save_segments(
    document_id: str,
    payload: SegmentsReplace,
    session: SessionDep,
) -> DocumentDetail:
    document = get_document(session, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="文案不存在")
    return _document_detail(session, replace_segments(session, document, payload.segments))


@router.post("/documents/{document_id}/auto-format", response_model=DocumentDetail)
def auto_format(document_id: str, session: SessionDep) -> DocumentDetail:
    """智能分句：把段落/长句文本重排为句级分段，便于逐句校对。"""

    document = get_document(session, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="文案不存在")
    if not document.segments:
        raise HTTPException(status_code=400, detail="文案还没有可整理的内容")
    return _document_detail(session, auto_format_segments(session, document))


@router.get("/documents/{document_id}/media")
def document_media(document_id: str, session: SessionDep) -> FileResponse:
    """播放文档的原始音视频；未开启「保留原始音视频」时文件已在任务结束时清理。"""

    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文案不存在")
    media = find_document_media(session, document_id)
    if media is None:
        raise HTTPException(
            status_code=404,
            detail="原始音视频已清理，可在「设置」里开启「保留原始音视频」后重新提取",
        )
    return FileResponse(media, media_type="audio/wav")


@router.get("/documents/{document_id}/cover")
def document_cover(document_id: str, session: SessionDep) -> FileResponse:
    """文档的封面图；本地文件或封面下载失败时返回 404，前端据此不渲染图片。"""

    document = session.get(Document, document_id)
    if document is None or not document.cover_file:
        raise HTTPException(status_code=404, detail="这条文案没有封面图")
    path = cover_path(document.cover_file)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="封面图已不存在")
    return FileResponse(path)


@router.get("/documents/{document_id}/export")
def download_export(
    document_id: str,
    session: SessionDep,
    file_format: Annotated[str, Query(alias="format")] = "txt",
) -> Response:
    document = get_document(session, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="文案不存在")
    # 字幕来源没有标点、条目边界也和语义无关，导出时保持一行一句而不是拼成段落
    job = session.scalar(select(Job).where(Job.document_id == document_id))
    line_by_line = bool(job and job.transcript_source == "subtitle")
    try:
        content, media_type, extension = export_document(
            document,
            file_format,
            line_by_line=line_by_line,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = quote(f"{document.title}.{extension}")
    return Response(
        content=content.encode("utf-8-sig") if extension in {"txt", "srt"} else content.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/connectors")
def connectors(session: SessionDep) -> list[dict[str, str]]:
    ffmpeg_ready = tools_ready()
    ytdlp_ready = find_spec("yt_dlp") is not None
    credentials = {item.platform: item for item in all_status()}
    # 视频号没有本机解析方案，依赖兜底解析接口的 Key
    fallback_ready = read_settings(session)["fallback_api_key_set"]

    def link_connector(platform_id: str, name: str, *, supports_subtitle: bool) -> dict[str, str]:
        """链接类平台：运行组件就绪即算可用；凭据必需的平台还要求先配置 Cookie。

        B站的凭据是可选的（公开视频不登录也能解析），配置后能读会员与登录可见内容。
        """

        if not ytdlp_ready:
            return {
                "id": platform_id,
                "name": name,
                "status": "needs_setup",
                "detail": "缺少 yt-dlp 运行组件",
            }
        credential = credentials.get(platform_id)
        if credential is not None and credential.required and not credential.configured:
            return {
                "id": platform_id,
                "name": name,
                "status": "needs_setup",
                "detail": "需要导入 Cookie 才能解析",
            }
        if credential is not None and credential.configured:
            return {
                "id": platform_id,
                "name": name,
                "status": "ready",
                "detail": "已配置访问凭据，可读取会员与登录可见内容"
                if supports_subtitle
                else "已配置 Cookie，支持公开作品链接",
            }
        return {
            "id": platform_id,
            "name": name,
            "status": "ready",
            "detail": "支持公开链接：字幕优先，音轨转写回退"
            + ("；配置访问凭据可读取会员内容" if supports_subtitle else ""),
        }

    def wechat_connector() -> dict[str, str]:
        """视频号只能通过兜底解析接口处理：先要有 Key，其次要有 FFmpeg。"""

        if not fallback_ready:
            return {
                "id": "wechat",
                "name": "视频号",
                "status": "needs_setup",
                "detail": "需要在「设置」页配置兜底解析接口的 API Key",
            }
        return {
            "id": "wechat",
            "name": "视频号",
            "status": "ready" if ffmpeg_ready else "needs_setup",
            "detail": "粘贴视频号分享链接即可提取" if ffmpeg_ready else "需要安装或配置 FFmpeg",
        }

    # 识别模型的状态由 /api/models 单独提供，这里只列内容来源。
    return [
        {
            "id": "local",
            "name": "本地文件",
            "status": "ready" if ffmpeg_ready else "needs_setup",
            "detail": "FFmpeg 已就绪" if ffmpeg_ready else "需要安装或配置 FFmpeg",
        },
        link_connector("bilibili", "B站", supports_subtitle=True),
        link_connector("douyin", "抖音", supports_subtitle=False),
        # 快手、小红书都自己读移动端分享页：游客就能解析，不需要 yt-dlp 和登录态
        {
            "id": "kuaishou",
            "name": "快手",
            "status": "ready" if ffmpeg_ready else "needs_setup",
            "detail": "支持公开分享链接：直接读取视频音轨"
            if ffmpeg_ready
            else "需要安装或配置 FFmpeg",
        },
        {
            "id": "xiaohongshu",
            "name": "小红书",
            "status": "ready" if ffmpeg_ready else "needs_setup",
            "detail": "公开视频无需登录，粘贴链接即可提取"
            if ffmpeg_ready
            else "需要安装或配置 FFmpeg",
        },
        wechat_connector(),
    ]


@router.get("/settings")
def get_settings(session: SessionDep) -> dict[str, str | bool]:
    return read_settings(session)


@router.patch("/settings")
def patch_settings(
    payload: SettingPatch,
    session: SessionDep,
) -> dict[str, str | bool]:
    return update_settings(session, payload)
