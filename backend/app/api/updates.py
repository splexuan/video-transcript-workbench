"""版本与更新接口：看版本、检查新版本、下载并自动替换。

下载只负责把包取到数据目录；真正替换程序目录由 `update_installer` 完成：
它解压、启动一个在进程退出后才动手的 PowerShell 脚本，失败会自动回滚。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.application.services import read_settings
from app.config import settings
from app.infrastructure import update_installer, updater
from app.infrastructure.database import get_session
from app.infrastructure.desktop import open_path
from app.schemas import (
    UpdateAssetRead,
    UpdateInstallRead,
    UpdateInstallResultRead,
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


def _install_result_read(result: update_installer.InstallResult) -> UpdateInstallResultRead:
    return UpdateInstallResultRead(
        status=result.status,
        message=result.message,
        version=result.version,
        updated_at=result.updated_at,
        log_path=result.log_path,
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
    install = update_installer.install_progress()
    package = updater.package_info()
    supported, reason = update_installer.install_supported()
    last_install = update_installer.read_install_result()
    # 失败结果只对「还没升到那个版本」的用户有意义；后来手动升上去了就别再吓人
    if (
        last_install is not None
        and last_install.status == "failed"
        and not updater.is_newer(last_install.version, settings.app_version)
    ):
        last_install = None
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
        install_supported=supported,
        install_reason=reason,
        install=None if install is None else UpdateInstallRead.model_validate(install),
        last_install=None if last_install is None else _install_result_read(last_install),
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


@router.post("/install", response_model=UpdateInstallRead)
def install_update() -> update_installer.InstallProgress:
    """解压下载好的包并安排替换；接口返回后进程会退出并自动重启。"""

    package = updater.package_info()
    if package is None:
        raise HTTPException(status_code=409, detail="还没有下载好更新包，请先下载")
    try:
        return update_installer.start_install(Path(package.path), package.version)
    except update_installer.InstallBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except update_installer.InstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reveal")
def reveal_package() -> dict[str, object]:
    """在资源管理器里打开更新包所在目录（数据目录，不是程序目录）。"""

    folder = settings.updates_dir
    folder.mkdir(parents=True, exist_ok=True)
    return {"path": str(folder), "opened": open_path(folder)}
