"""语音合成接口：配置、音色管理、提交合成任务、轮询进度、取音频。

合成是慢活（int8 版 CPU 上单实例 RTF≈0.5，现在按核心数把句子摊到 2~4 个实例并行跑），
所以走「提交任务 + 轮询」而不是一个长请求；音频存在数据目录的 tts/ 下，
由 /audio 端点读取（wav 直接给，mp3 用 FFmpeg 现转）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.application import tts_jobs
from app.application.services import read_settings
from app.domain import EngineUnavailableError, ModelNotFoundError
from app.infrastructure import tts_engine
from app.infrastructure.database import get_session
from app.infrastructure.media import MediaToolError
from app.infrastructure.model_catalog import require_spec, specs_for_engine
from app.infrastructure.model_store import inspect, package_available
from app.schemas import (
    TtsConfigRead,
    TtsJobRead,
    TtsModelOption,
    TtsSynthesizeRequest,
    TtsTranscribeRead,
    TtsVoiceItem,
)

router = APIRouter(prefix="/api/tts", tags=["tts"])
SessionDep = Annotated[Session, Depends(get_session)]

# 估算依据：中文约 4.5 字/秒，int8 版实测单实例合成耗时约为音频时长的 0.5 倍；
# 实际等待还要按并行实例数摊薄（parallel_lanes），所以这里给的是单实例口径。
CHARS_PER_SECOND = 4.5
REALTIME_FACTOR = 0.5
# 单个参考音频的大小上限：ZipVoice 的耗时随它明显变长，超过就没必要收下
MAX_REFERENCE_BYTES = 20 * 1024 * 1024


def _option(spec) -> TtsModelOption:  # type: ignore[no-untyped-def]
    target = inspect(spec)
    return TtsModelOption(
        id=spec.id,
        name=spec.name,
        state=target.state,
        ready=target.ready,
        approx_bytes=spec.approx_bytes,
    )


def _is_usable_model(model_id: str) -> bool:
    """模型 id 是否还在清单里、且已装好。已下架或没装都算不可用。"""

    try:
        require_spec(model_id)
    except ModelNotFoundError:
        return False
    return inspect(require_spec(model_id)).ready


def _pick_model(session: Session, requested: str = "") -> str:
    """当前用哪个语音模型：本次指定 → 设置里的 → 已装的 → 默认规格。

    两类来源要区别对待：
      * 本次显式指定 —— 非法就直接报错，让调用方知道是自己传错了；
      * 设置里存的值 —— 可能来自上一版引擎（换成 ZipVoice 前的 Kokoro 规格已下架），
        这种失效 id 必须静默跳到「已装的第一个」。否则老用户升级后打开语音合成页
        就会整个 500，只能手动进数据库改值才能恢复。
    """

    explicit = (requested or "").strip()
    if explicit:
        require_spec(explicit)
        return explicit
    stored = str(read_settings(session).get("tts_model") or "").strip()
    if stored and _is_usable_model(stored):
        return stored
    for spec in specs_for_engine(tts_engine.ENGINE):
        if inspect(spec).ready:
            return spec.id
    return tts_engine.DEFAULT_MODEL_ID


def _pick_voice(session: Session, requested: str | None) -> str:
    """音色 id：本次指定 → 设置里的 → 默认音色。

    逐级校验而不是直接采用：老设置里可能存着上一版引擎的编号（那时音色是整数 sid），
    模型重装后自定义音色也可能已被删除，这类失效 id 要静默回落到默认音色，
    而不是让整单失败——否则用户只是清了个旧音色，合成就彻底不能用了。
    """

    if requested is not None and tts_engine.find_voice(requested) is not None:
        return requested.strip()
    stored = read_settings(session).get("tts_voice")
    if isinstance(stored, str) and tts_engine.find_voice(stored) is not None:
        return stored
    return tts_engine.DEFAULT_VOICE


def _job_read(job: tts_jobs.TtsJob) -> TtsJobRead:
    return TtsJobRead(
        id=job.id,
        status=job.status,
        message=job.message,
        percent=job.percent,
        model_id=job.model_id,
        voice=job.voice,
        speed=job.speed,
        text=job.text,
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
        voices=[
            TtsVoiceItem(
                id=item.id,
                name=item.name,
                reference_text=item.reference_text,
                builtin=item.builtin,
            )
            for item in tts_engine.voices()
        ],
        voice=_pick_voice(session, None),
        speed=tts_engine.DEFAULT_SPEED,
        max_chars=tts_engine.MAX_CHARS,
        chars_per_second=CHARS_PER_SECOND,
        realtime_factor=REALTIME_FACTOR,
        parallel_lanes=tts_engine.lanes(),
    )


@router.get("/voices", response_model=list[TtsVoiceItem])
def list_tts_voices() -> list[TtsVoiceItem]:
    """音色清单。内置音色随模型发布，自定义音色由用户上传后出现在这里。"""

    return [
        TtsVoiceItem(
            id=item.id,
            name=item.name,
            reference_text=item.reference_text,
            builtin=item.builtin,
        )
        for item in tts_engine.voices()
    ]


@router.post("/voices/transcribe", response_model=TtsTranscribeRead)
async def transcribe_tts_voice(
    audio: Annotated[UploadFile, File()],
) -> TtsTranscribeRead:
    """转写一段参考音频，给「音色克隆」回填参考文本。

    走同步识别（十几秒音频是秒级），但识别是 CPU 密集的阻塞调用，必须丢到线程池里跑，
    否则会把事件循环占住，连带整站接口一起卡住。
    """

    payload = await audio.read()
    if not payload:
        raise HTTPException(status_code=400, detail="请先选择一段参考音频")
    if len(payload) > MAX_REFERENCE_BYTES:
        raise HTTPException(status_code=400, detail="参考音频超过 20 MB，请剪短后再试")
    try:
        text, seconds, truncated = await run_in_threadpool(
            tts_engine.transcribe_reference, payload
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EngineUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MediaToolError as exc:
        raise HTTPException(status_code=400, detail=f"无法读取这段音频：{exc}") from exc
    return TtsTranscribeRead(text=text, seconds=seconds, truncated=truncated)


@router.post("/voices", response_model=TtsVoiceItem, status_code=status.HTTP_201_CREATED)
async def create_tts_voice(
    name: Annotated[str, Form()],
    reference_text: Annotated[str, Form()],
    audio: Annotated[UploadFile, File()],
) -> TtsVoiceItem:
    """上传一段参考音频，存成自己的音色。

    ZipVoice 是零样本克隆，参考音频的质量直接决定合成质量：底噪与环境声都会被克隆进去，
    所以这里只做最基础的把关（可读、有声、时长封顶），更细的取舍交给前端提示。
    """

    payload = await audio.read()
    if len(payload) > MAX_REFERENCE_BYTES:
        raise HTTPException(status_code=400, detail="参考音频超过 20 MB，请剪短后再试")
    try:
        voice = tts_engine.save_custom_voice(name, reference_text, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TtsVoiceItem(
        id=voice.id,
        name=voice.name,
        reference_text=voice.reference_text,
        builtin=voice.builtin,
    )


@router.delete("/voices/{voice_id}")
def remove_tts_voice(voice_id: str) -> dict[str, str | bool]:
    if not tts_engine.remove_custom_voice(voice_id):
        raise HTTPException(status_code=400, detail="这个音色删不掉（内置音色或已不存在）")
    return {"id": voice_id, "removed": True}


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
        voice=_pick_voice(session, payload.voice),
        speed=tts_engine.clamp_speed(payload.speed if payload.speed is not None else tts_engine.DEFAULT_SPEED),
    )
    return _job_read(job)


@router.get("/jobs", response_model=list[TtsJobRead])
def list_tts_jobs() -> list[TtsJobRead]:
    return [_job_read(item) for item in tts_jobs.manager.recent()]


@router.delete("/jobs")
def clear_tts_jobs() -> dict[str, int]:
    """清空所有已结束的合成记录与音频文件；正在合成的那条不动。"""

    return {"removed": tts_jobs.manager.clear_finished()}


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