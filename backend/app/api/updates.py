"""版本与更新接口：看版本、检查新版本、下载新版压缩包。

这里只提供「检查 + 下载 + 打开目录」：替换程序目录由用户手动完成（原因见
`app/infrastructure/updater.py` 顶部说明）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.application.services import read_settings
from app.config import settings
from app.infrastructure import updater
from app.infrastructure.database import get_session
from app.infrastructure.desktop import open_path
from app.schemas import (
    UpdateAssetRead,
    UpdatePackageRead,
    UpdateProgressRead,
    UpdateReleaseRead,
    UpdateStateRead,
)

router = APIRouter(prefix="/api/updates", tags=["updates"])
SessionDep = Annotated[Session, Depends(get_session)]


def _release_read(release: updater.ReleaseInfo) -> UpdateReleaseRead:
    asset = release.asset
    return UpdateReleaseRead(
        version=release.version,
        tag=release.tag,
        name=release.name,
        notes=release.notes,
        published_at=release.published_at,
        page_url=release.page_url,
        asset=None
        if asset is None
        else UpdateAssetRead(name=asset.name, size=asset.size, url=asset.url),
    )


def _package_read(package: updater.PackageInfo) -> UpdatePackageRead:
    return UpdatePackageRead(
        file_name=package.file_name,
        version=package.version,
        size=package.size,
        path=package.path,
    )


def _state(session: Session, *, force: bool) -> UpdateStateRead:
    """组装界面要的完整状态。

    自动检查由这里触发（而不是定时任务）：界面一打开就问一次，超过节流间隔才真的
    打远程，所以关掉开关之后连一次请求都不会发出去。
    """

    stored = read_settings(session)
    auto_check = bool(stored["auto_check_update"])
    ignored = str(stored["ignored_version"])
    result = updater.snapshot()
    if force or (auto_check and updater.stale()):
        result = updater.check(force=force)

    latest = result.latest
    has_update = bool(
        latest
        and updater.is_newer(latest.version, settings.app_version)
        and latest.version != ignored
    )
    progress = updater.download_progress()
    package = updater.package_info()
    return UpdateStateRead(
        current_version=settings.app_version,
        packaged=updater.packaged(),
        latest=None if latest is None else _release_read(latest),
        has_update=has_update,
        ignored_version=ignored,
        checked_at=result.checked_at,
        error=result.error,
        auto_check=auto_check,
        download=None if progress is None else UpdateProgressRead.model_validate(progress),
        package=None if package is None else _package_read(package),
    )


@router.get("", response_model=UpdateStateRead)
def read_updates(session: SessionDep) -> UpdateStateRead:
    return _state(session, force=False)


@router.post("/check", response_model=UpdateStateRead)
def check_updates(session: SessionDep) -> UpdateStateRead:
    """界面上的「检查更新」：忽略节流，立即问一次。"""

    return _state(session, force=True)


@router.post("/download", response_model=UpdateProgressRead)
def download_update() -> updater.DownloadProgress:
    release = updater.snapshot().latest
    if release is None:
        raise HTTPException(status_code=409, detail="还没有可下载的版本，请先检查更新")
    try:
        return updater.start_download(release)
    except updater.UpdateBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except updater.UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/cancel", response_model=UpdateProgressRead)
def cancel_download() -> updater.DownloadProgress:
    snapshot = updater.cancel_download()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="当前没有正在进行的下载")
    return snapshot


@router.delete("/package")
def delete_package() -> dict[str, bool]:
    return {"removed": updater.remove_package()}


@router.post("/reveal")
def reveal_package() -> dict[str, object]:
    """在资源管理器里打开更新包所在目录（数据目录，不是程序目录）。"""

    folder = settings.updates_dir
    folder.mkdir(parents=True, exist_ok=True)
    return {"path": str(folder), "opened": open_path(folder)}
