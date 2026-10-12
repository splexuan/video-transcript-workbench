"""语音合成接口：配置、提交合成任务、轮询进度、取音频。

合成是慢活（CPU 上单实例 RTF≈0.93，现在按核心数把句子摊到 2~4 个实例并行跑），
所以走「提交任务 + 轮询」而不是一个长请求；音频存在数据目录的 tts/ 下，
由 /audio 端点读取（wav 直接给，mp3 用 FFmpeg 现转）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.application import tts_jobs
from app.application.services import read_settings
from app.domain import EngineUnavailableError, ModelNotFoundError
from app.infrastructure import tts_engine
from app.infrastructure.database import get_session
from app.infrastructure.model_catalog import require_spec, specs_for_engine
from app.infrastructure.model_store import inspect, package_available
from app.schemas import (
    TtsConfigRead,
    TtsJobRead,
    TtsModelOption,
    TtsSynthesizeRequest,
    TtsVoiceGroup,
)

router = APIRouter(prefix="/api/tts", tags=["tts"])
SessionDep = Annotated[Session, Depends(get_session)]

# 估算依据：中文约 4.5 字/秒，实测单个实例的合成耗时约为音频时长的 0.95 倍；
# 实际等待还要按并行实例数摊薄（parallel_lanes），所以这里给的是单实例口径。
CHARS_PER_SECOND = 4.5
REALTIME_FACTOR = 0.95


def _option(spec) -> TtsModelOption:  # type: ignore[no-untyped-def]
    target = inspect(spec)
    return TtsModelOption(
        id=spec.id,
        name=spec.name,
        state=target.state,
        ready=target.ready,
        approx_bytes=spec.approx_bytes,
    )


def _pick_model(session: Session, requested: str = "") -> str:
    """当前用哪个语音模型：本次指定 → 设置里的 → 已装的 → 默认规格。"""

    candidate = (requested or "").strip() or str(read_settings(session).get("tts_model") or "").strip()
    if candidate:
        require_spec(candidate)
        return candidate
    for spec in specs_for_engine(tts_engine.ENGINE):
        if inspect(spec).ready:
            return spec.id
    return tts_engine.DEFAULT_MODEL_ID


def _pick_voice(session: Session, requested: int | None) -> int:
    if requested is not None:
        return int(requested)
    stored = read_settings(session).get("tts_voice")
    return int(stored) if isinstance(stored, int) else tts_engine.DEFAULT_VOICE


def _job_read(job: tts_jobs.TtsJob) -> TtsJobRead:
    return TtsJobRead(
        id=job.id,
        status=job.status,
        message=job.message,
        percent=job.percent,
        model_id=job.model_id,
        voice=job.sid,
        speed=job.speed,
        chars=job.chars,
        sentences_done=job.sentences_done,
        sentences_total=job.sentences_total,
        duration=job.duration,
        elapsed=job.elapsed,
        error=job.error,
        audio_url=f"/api/tts/jobs/{job.id}/audio" if job.ready else None,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _disposition(filename: str, download: bool) -> dict[str, str]:
    kind = "attachment" if download else "inline"
    return {"Content-Disposition": f"{kind}; filename*=UTF-8''{quote(filename)}"}


@router.get("", response_model=TtsConfigRead)
def tts_config(session: SessionDep) -> TtsConfigRead:
    """语音合成页的配置：可选的模型与音色、当前选择、就绪状态。"""

    model_id = _pick_model(session, "")
    spec = require_spec(model_id)
    target = inspect(spec)
    engine_ready = package_available(tts_engine.ENGINE)
    ready = target.ready and engine_ready
    if not engine_ready:
        message = "缺少 sherpa-onnx 运行组件，语音合成不可用"
    elif not target.ready:
        message = f"「{spec.name}」还没安装，请到「模型管理」下载（约 {spec.approx_label}）"
    else:
        message = "可以合成"

    return TtsConfigRead(
        ready=ready,
        message=message,
        model=_option(spec),
        models=[_option(item) for item in specs_for_engine(tts_engine.ENGINE)],
        voice_groups=[TtsVoiceGroup(**group) for group in tts_engine.voice_groups()],
        voice=_pick_voice(session, None),
        speed=tts_engine.DEFAULT_SPEED,
        max_chars=tts_engine.MAX_CHARS,
        chars_per_second=CHARS_PER_SECOND,
        realtime_factor=REALTIME_FACTOR,
        parallel_lanes=tts_engine.lanes(),
    )


@router.post("/jobs", response_model=TtsJobRead, status_code=status.HTTP_201_CREATED)
def create_tts_job(payload: TtsSynthesizeRequest, session: SessionDep) -> TtsJobRead:
    text = tts_engine.normalize_text(payload.text)
    if not text:
        raise HTTPException(status_code=400, detail="请先输入要合成的文本")
    if len(text) > tts_engine.MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多合成 {tts_engine.MAX_CHARS} 字，当前 {len(text)} 字，请分段处理",
        )
    try:
        model_id = _pick_model(session, payload.model_id or "")
        tts_engine.model_directory(model_id)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EngineUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = tts_jobs.manager.submit(
        text,
        model_id=model_id,
        sid=_pick_voice(session, payload.voice),
        speed=tts_engine.clamp_speed(payload.speed if payload.speed is not None else tts_engine.DEFAULT_SPEED),
    )
    return _job_read(job)


@router.get("/jobs", response_model=list[TtsJobRead])
def list_tts_jobs() -> list[TtsJobRead]:
    return [_job_read(item) for item in tts_jobs.manager.recent()]


@router.get("/jobs/{job_id}", response_model=TtsJobRead)
def tts_job(job_id: str) -> TtsJobRead:
    job = tts_jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="合成任务不存在或已被清理")
    return _job_read(job)


@router.post("/jobs/{job_id}/cancel", response_model=TtsJobRead)
def cancel_tts_job(job_id: str) -> TtsJobRead:
    job = tts_jobs.manager.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="合成任务不存在或已被清理")
    return _job_read(job)


@router.delete("/jobs/{job_id}")
def remove_tts_job(job_id: str) -> dict[str, str | bool]:
    job = tts_jobs.manager.get(job_id)
    job = tts_jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="合成任务不存在或已被清理")
    if not tts_jobs.manager.remove(job_id):
        raise HTTPException(status_code=400, detail="任务还在合成中，请先取消")
    return {"id": job_id, "removed": True}


@router.get("/jobs/{job_id}/audio")
def tts_audio(
    job_id: str,
    file_format: Annotated[str, Query(alias="format")] = "wav",
    download: Annotated[bool, Query()] = False,
) -> Response:
    job = tts_jobs.manager.get(job_id)
    if job is None or not job.ready or not job.audio_path:
        raise HTTPException(status_code=404, detail="音频还没生成")
    path = Path(job.audio_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="音频文件已被清理，请重新合成")

    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(job.created_at))
    if file_format == "mp3":
        payload = tts_engine.wav_file_to_mp3(path)
        if payload is None:
            raise HTTPException(status_code=400, detail="没有可用的 FFmpeg，无法导出 MP3")
        return Response(
            content=payload,
            media_type="audio/mpeg",
            headers=_disposition(f"语音合成-{stamp}.mp3", download),
        )
    return Response(
        content=path.read_bytes(),
        media_type="audio/wav",
        headers=_disposition(f"语音合成-{stamp}.wav", download),
    )
