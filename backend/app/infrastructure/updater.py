"""版本更新：检查远端 Release、下载新版压缩包。

两条取舍写在最前面，界面文案与 README 都按这个说法：

- **只下载，不替换自身**。免安装目录里的 exe 正在运行，Windows 不允许覆盖它；
  想自动替换就得先退出进程、再由外部脚本搬文件，而「搬一半失败」的代价是留下
  一个坏掉的程序目录。所以这里做到「检查 → 下载 → 打开目录」，解压覆盖由用户完成。
  数据（文案、模型、凭据）都在数据目录里，替换程序目录不会丢东西。
- 检查结果缓存在进程内并按设置节流：界面每打开一次都会问一次版本，不该每次都打远程。

下载走后台线程 + 进度快照，模式与模型安装（`model_store.installer`）保持一致。
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

USER_AGENT = f"VideoTranscriptWorkbench/{settings.app_version} (+local)"
REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
# 分块读下载流：太小会让进度回调过于频繁，太大在慢链路上进度条会一跳一跳
CHUNK_BYTES = 1 << 18
# 界面只需要一段说明文字，Release 正文过长时截断，别把整篇 markdown 塞进响应
MAX_NOTES_CHARS = 4000
# 包里至少要有一个 exe，否则多半是下错了资产（源码包、校验文件）
ARCHIVE_SUFFIX = ".zip"
EXECUTABLE_SUFFIX = ".exe"
# 下之前先看磁盘：包本身几百 MB，解压还要一份空间
SPACE_HEADROOM = 2.0

VERSION_RE = re.compile(r"\d+(?:\.\d+)*")
FILE_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)")


class UpdateError(RuntimeError):
    """更新流程里可以直接展示给用户的错误。"""


class UpdateBusyError(UpdateError):
    """已经有一个下载任务在进行。"""


class _Cancelled(Exception):
    """下载被用户取消；只在模块内部流转。"""


@dataclass(slots=True)
class ReleaseAsset:
    name: str
    size: int
    url: str


@dataclass(slots=True)
class ReleaseInfo:
    version: str
    tag: str
    name: str
    notes: str
    published_at: str
    page_url: str
    asset: ReleaseAsset | None = None


@dataclass(slots=True)
class CheckResult:
    """一次检查的结论；失败时保留上一次的 latest，只把原因写进 error。"""

    latest: ReleaseInfo | None = None
    checked_at: float | None = None
    error: str | None = None


@dataclass(slots=True)
class DownloadProgress:
    version: str
    file_name: str
    status: str = "pending"
    percent: int = 0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    path: str = ""
    message: str = ""
    error: str | None = None
    updated_at: float = 0.0

    @property
    def active(self) -> bool:
        return self.status in {"pending", "running", "verifying", "cancelling"}


@dataclass(slots=True)
class PackageInfo:
    file_name: str
    version: str
    size: int
    path: str


# ---------------------------------------------------------------- 版本比较


def normalize_version(text: str) -> str:
    """把 tag（v0.2.0）归一化成 0.2.0；取不出数字时返回去空白的原串。"""

    cleaned = (text or "").strip()
    match = VERSION_RE.match(cleaned.lstrip("vV"))
    return match.group(0) if match else cleaned


def _version_key(text: str) -> tuple[int, ...]:
    match = VERSION_RE.match((text or "").strip().lstrip("vV"))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


def is_newer(candidate: str, current: str) -> bool:
    """版本比较：按数字段比，段数不同按 0 补齐（0.2 与 0.2.0 视为同一个版本）。

    解析不出数字时一律返回 False —— 宁可少提示一次，也不要给用户一个假的更新。
    """

    left, right = _version_key(candidate), _version_key(current)
    if not left or not right:
        return False
    length = max(len(left), len(right))
    left += (0,) * (length - len(left))
    right += (0,) * (length - len(right))
    return left > right


def packaged() -> bool:
    """是否跑在 PyInstaller 产物里。"""

    return bool(getattr(sys, "frozen", False))


# ---------------------------------------------------------------- 检查新版本


def parse_release(payload: dict[str, Any]) -> ReleaseInfo:
    """把 GitHub Releases 的响应变成界面要用的字段。"""

    tag = str(payload.get("tag_name") or "")
    return ReleaseInfo(
        version=normalize_version(tag or str(payload.get("name") or "")),
        tag=tag,
        name=str(payload.get("name") or tag),
        notes=_trim(payload.get("body")),
        published_at=str(payload.get("published_at") or ""),
        page_url=str(payload.get("html_url") or ""),
        asset=select_asset(payload.get("assets")),
    )


def select_asset(assets: Any) -> ReleaseAsset | None:
    """挑下载资产：优先 win64 的 zip，其次任意 zip。

    非 zip（校验文件、源码包）不选：用户拿到它也没法替换程序目录。
    """

    candidates: list[ReleaseAsset] = []
    for item in assets or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        if not name.lower().endswith(ARCHIVE_SUFFIX) or not url:
            continue
        candidates.append(ReleaseAsset(name=name, size=int(item.get("size") or 0), url=url))
    if not candidates:
        return None
    for asset in candidates:
        if "win64" in asset.name.lower():
            return asset
    return candidates[0]


def _trim(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= MAX_NOTES_CHARS:
        return text
    return text[:MAX_NOTES_CHARS].rstrip() + "…"


def describe_error(exc: BaseException) -> str:
    """把网络异常说成人话；细节留给日志。"""

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 404:
            return "更新服务里还没有发布记录。"
        if status == 403:
            return "更新服务拒绝了这次请求（本机请求可能过于频繁），稍后再试。"
        return f"更新服务返回了 {status}，稍后再试。"
    if isinstance(exc, httpx.TimeoutException):
        return "连接更新服务超时，检查网络后重试。"
    if isinstance(exc, httpx.HTTPError):
        return "无法连接更新服务，检查网络或代理后重试。"
    return "检查更新失败，稍后再试。"


_check_lock = threading.Lock()
# 同一个进程里只允许一个检查在跑：并发请求不该打出多次远程请求
_fetch_lock = threading.Lock()
_check_result = CheckResult()


def _interval_seconds() -> float:
    return max(settings.update_check_interval_hours, 0.0) * 3600.0


def snapshot() -> CheckResult:
    with _check_lock:
        return replace(_check_result)


def stale() -> bool:
    """距上次检查是否已经超过节流间隔；从没检查过也算需要检查。"""

    with _check_lock:
        checked_at = _check_result.checked_at
    if checked_at is None:
        return True
    return time.time() - checked_at >= _interval_seconds()


def check(*, force: bool = False) -> CheckResult:
    """检查最新版本；非 force 时遵守节流间隔。

    失败时保留上一次的 latest：网络抖一下不该把「有新版本」的提示抹掉。
    """

    global _check_result
    if not force and not stale():
        return snapshot()
    with _fetch_lock:
        if not force and not stale():
            return snapshot()
        try:
            payload = _fetch_latest()
        except Exception as exc:  # noqa: BLE001 网络问题在更新检查里是常态
            logger.warning("检查更新失败：%s", exc)
            with _check_lock:
                _check_result = replace(_check_result, checked_at=time.time(), error=describe_error(exc))
                return replace(_check_result)
        latest = parse_release(payload) if payload else None
        with _check_lock:
            _check_result = CheckResult(
                latest=latest,
                checked_at=time.time(),
                error=None if latest else "更新服务里还没有发布记录。",
            )
            return replace(_check_result)


def _fetch_latest() -> dict[str, Any] | None:
    """取 latest release；仓库一个 Release 都没有时返回 None。"""

    base = settings.update_api_base.rstrip("/")
    url = f"{base}/repos/{settings.update_repo}/releases/latest"
    response = httpx.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------- 下载更新包


@dataclass(slots=True)
class _Task:
    progress: DownloadProgress
    cancel: threading.Event
    lock: threading.Lock

    def report(self, **changes: object) -> None:
        with self.lock:
            for key, value in changes.items():
                setattr(self.progress, key, value)
            self.progress.updated_at = time.time()


_download_lock = threading.Lock()
_download_task: _Task | None = None


def download_progress() -> DownloadProgress | None:
    with _download_lock:
        task = _download_task
    if task is None:
        return None
    with task.lock:
        return replace(task.progress)


def start_download(release: ReleaseInfo) -> DownloadProgress:
    asset = release.asset
    if asset is None:
        raise UpdateError("这个版本没有提供 Windows 免安装包，请到发布页手动下载。")

    global _download_task
    with _download_lock:
        if _download_task is not None and _download_task.progress.active:
            raise UpdateBusyError("已经有一个下载任务在进行")
        task = _Task(
            progress=DownloadProgress(
                version=release.version,
                file_name=asset.name,
                status="pending",
                total_bytes=asset.size,
                message="正在准备下载",
                updated_at=time.time(),
            ),
            cancel=threading.Event(),
            lock=threading.Lock(),
        )
        _download_task = task

    threading.Thread(
        target=_run_download,
        args=(task, asset),
        name="update-download",
        daemon=True,
    ).start()
    snapshot = download_progress()
    assert snapshot is not None
    return snapshot


def cancel_download() -> DownloadProgress | None:
    with _download_lock:
        task = _download_task
    if task is None:
        return None
    task.cancel.set()
    task.report(status="cancelling", message="正在取消")
    return download_progress()


def _run_download(task: _Task, asset: ReleaseAsset) -> None:
    target = settings.updates_dir / asset.name
    partial = target.with_name(target.name + ".part")
    try:
        settings.updates_dir.mkdir(parents=True, exist_ok=True)
        if _reuse_existing(target, asset):
            task.report(
                status="ready",
                percent=100,
                downloaded_bytes=target.stat().st_size,
                total_bytes=target.stat().st_size,
                path=str(target),
                message="已经下载过这个版本，可直接解压替换",
            )
            return
        _ensure_space(asset.size)
        task.report(status="running", message="正在连接下载源", path=str(target))
        _stream_to_file(task, asset, partial)
        task.report(status="verifying", percent=100, message="正在校验下载包")
        _verify_archive(partial)
        partial.replace(target)
        task.report(status="ready", percent=100, path=str(target), message="新版本已下载完成")
    except _Cancelled:
        _discard(partial)
        task.report(status="cancelled", message="已取消下载", error=None)
    except Exception as exc:  # noqa: BLE001 任何失败都要落到进度里给用户看
        logger.warning("下载更新包失败：%s", exc)
        _discard(partial)
        task.report(status="failed", error=_download_error(exc), message="下载失败")


def _reuse_existing(target: Path, asset: ReleaseAsset) -> bool:
    """已经下过同一个包（重启后重新点下载）就不再下一次。"""

    if not target.is_file():
        return False
    size = target.stat().st_size
    if asset.size and size != asset.size:
        return False
    try:
        _verify_archive(target)
    except UpdateError:
        return False
    return True


def _stream_to_file(task: _Task, asset: ReleaseAsset, partial: Path) -> None:
    downloaded = 0
    total = asset.size
    with httpx.stream(
        "GET",
        asset.url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    ) as response:
        response.raise_for_status()
        # 资产走 CDN，Content-Length 一般都有；没有时用 Release 里的 size 兜底
        declared = int(response.headers.get("Content-Length") or 0)
        if declared:
            total = declared
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes(CHUNK_BYTES):
                if task.cancel.is_set():
                    raise _Cancelled()
                handle.write(chunk)
                downloaded += len(chunk)
                task.report(
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    percent=_percent(downloaded, total),
                    message="正在下载新版本",
                )
    if task.cancel.is_set():
        raise _Cancelled()


def _percent(downloaded: int, total: int) -> int:
    if total <= 0:
        return 0
    # 留 1% 给校验和落盘，下载条别在写完之前就满格
    return max(0, min(99, int(downloaded * 100 / total)))


def _ensure_space(size: int) -> None:
    if size <= 0:
        return
    try:
        free = shutil.disk_usage(settings.updates_dir).free
    except OSError:
        return
    if free < size * SPACE_HEADROOM:
        need = size * SPACE_HEADROOM / 1024**2
        raise UpdateError(f"磁盘剩余空间不足，需要约 {need:.0f} MB（含解压），先清理后再试")


def _verify_archive(path: Path) -> None:
    """粗校验：是能打开的 zip，且里面有 exe。

    挡的是「下到 HTML 错误页」「半截文件」这类典型失败；真正的完整性由用户替换时暴露。
    """

    if not zipfile.is_zipfile(path):
        raise UpdateError("下载到的文件不是有效的压缩包，可能是网络中断造成的")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    if not any(name.lower().endswith(EXECUTABLE_SUFFIX) for name in names):
        raise UpdateError("压缩包里没有可执行文件，请到发布页手动下载")


def _download_error(exc: BaseException) -> str:
    if isinstance(exc, UpdateError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"下载失败：下载源返回了 {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return describe_error(exc)
    if isinstance(exc, OSError):
        return "写入下载文件失败，检查磁盘空间与文件占用后重试"
    return "下载失败，稍后再试"


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("清理未完成的下载文件失败 %s：%s", path, exc)


# ---------------------------------------------------------------- 已下载的包


def package_info() -> PackageInfo | None:
    """数据目录里已经下好的更新包（重启后仍在）。取最新那个。"""

    root = settings.updates_dir
    if not root.is_dir():
        return None
    archives = [item for item in root.glob(f"*{ARCHIVE_SUFFIX}") if item.is_file()]
    if not archives:
        return None
    newest = max(archives, key=lambda item: item.stat().st_mtime)
    match = FILE_VERSION_RE.search(newest.name)
    return PackageInfo(
        file_name=newest.name,
        version=match.group(1) if match else "",
        size=newest.stat().st_size,
        path=str(newest),
    )


def remove_package() -> bool:
    """删掉下载过的更新包（以及可能残留的半截文件）。"""

    root = settings.updates_dir
    if not root.is_dir():
        return False
    removed = False
    for pattern in (f"*{ARCHIVE_SUFFIX}", "*.part"):
        for item in root.glob(pattern):
            if not item.is_file():
                continue
            try:
                item.unlink()
                removed = True
            except OSError as exc:
                logger.warning("删除更新包失败 %s：%s", item, exc)
    return removed
