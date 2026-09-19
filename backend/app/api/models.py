"""识别模型管理接口：状态、下载、校验与删除。"""

from __future__ import annotations

import shutil

from fastapi import APIRouter, HTTPException

from app.domain import (
    InstallProgress,
    ModelInstallBusyError,
    ModelNotFoundError,
    ModelSpec,
    ModelState,
    ModelStatus,
)
from app.infrastructure import transcriber, whisper_engine
from app.infrastructure.model_catalog import require_spec
from app.infrastructure.model_store import (
    engine_status,
    inspect,
    inspect_all,
    installer,
    models_root,
    remove,
    slot_paths,
    spec_dir,
)
from app.schemas import (
    InstallProgressRead,
    ModelCatalogRead,
    ModelStatusRead,
)

router = APIRouter(prefix="/api/models", tags=["models"])


def _spec_or_404(model_id: str) -> ModelSpec:
    try:
        return require_spec(model_id)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _status(model_id: str) -> ModelStatus:
    return inspect(_spec_or_404(model_id))


@router.get("", response_model=ModelCatalogRead)
def list_models() -> ModelCatalogRead:
    statuses = inspect_all()
    root = models_root()
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = None
    return ModelCatalogRead(
        storage={
            "models_root": str(root),
            "total_bytes": sum(item.installed_bytes for item in statuses),
            "disk_free_bytes": free,
        },
        engines=engine_status(),
        models=statuses,
        active_tasks=installer.snapshots(),
    )


@router.get("/tasks", response_model=list[InstallProgressRead])
def list_tasks() -> list[InstallProgress]:
    return installer.snapshots()


@router.get("/{model_id}", response_model=ModelStatusRead)
def model_detail(model_id: str) -> ModelStatus:
    return _status(model_id)


@router.post("/{model_id}/download", response_model=InstallProgressRead)
def download_model(model_id: str) -> InstallProgress:
    _spec_or_404(model_id)
    try:
        return installer.download(model_id)
    except ModelInstallBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{model_id}/cancel", response_model=InstallProgressRead)
def cancel_install(model_id: str) -> InstallProgress:
    _spec_or_404(model_id)
    snapshot = installer.cancel(model_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="当前没有可取消的安装任务")
    return snapshot


@router.post("/{model_id}/verify", response_model=ModelStatusRead)
def verify_model(model_id: str) -> ModelStatus:
    status = _status(model_id)
    if status.state != ModelState.READY.value:
        # 校验后发现不可用，清掉可能已加载进内存的旧模型。
        transcriber.reset_cache()
        whisper_engine.reset_cache()
    return status


@router.get("/{model_id}/files")
def model_files(model_id: str) -> dict[str, object]:
    """列出模型目录中的实际文件，方便排查不完整的安装。"""

    spec = _spec_or_404(model_id)
    directory = spec_dir(spec)
    resolved = slot_paths(spec, directory)
    files: list[dict[str, object]] = []
    for slot in spec.files:
        found = resolved.get(slot.name)
        files.append(
            {
                "name": slot.name,
                "found": bool(found),
                "actual_name": found.name if found else None,
                "size": found.stat().st_size if found else 0,
                "expected_size": slot.size,
            }
        )
    return {"path": str(directory), "exists": directory.is_dir(), "files": files}


@router.delete("/{model_id}")
def delete_model(model_id: str) -> dict[str, bool | str]:
    spec = _spec_or_404(model_id)
    active = installer.snapshot(model_id)
    if active is not None and active.active:
        raise HTTPException(status_code=409, detail="请先取消正在进行的安装任务")
    removed = remove(spec)
    transcriber.reset_cache()
    whisper_engine.reset_cache()
    return {"removed": removed, "model_id": model_id}
