"""模型存储：完整性检测、断点续传下载与删除。

模型一律由工作台自己下载到数据目录下，识别只读取该目录，
保证「在一台没有任何历史环境的电脑上也能独立安装运行」。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.config import settings
from app.domain import (
    EngineUnavailableError,
    InstallProgress,
    ModelDownloadError,
    ModelEngine,
    ModelFile,
    ModelInstallBusyError,
    ModelSpec,
    ModelState,
    ModelStatus,
)
from app.infrastructure.model_catalog import (
    ENGINE_PACKAGE_LABELS,
    ENGINE_PACKAGES,
    list_specs,
    preferred_spec,
    require_spec,
    specs_for_engine,
)

logger = logging.getLogger(__name__)

MARKER_FILENAME = "workbench-model.json"
PART_SUFFIX = ".part"
CHUNK_BYTES = 1024 * 1024
USER_AGENT = "VideoTranscriptWorkbench/0.2 (+local)"


class _Cancelled(RuntimeError):
    pass


def models_root() -> Path:
    root = settings.models_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def spec_dir(spec: ModelSpec) -> Path:
    return models_root() / spec.id


def _part_path(destination: Path, url: str | None = None) -> Path:
    """下载中的临时文件。

    同款模型在不同源上的字节可能不同（例如量化差异），因此按来源区分临时文件，
    避免换源后把两个源的内容拼接成一个文件。
    """

    if url is None:
        return destination.with_name(destination.name + PART_SUFFIX)
    digest = hashlib.sha1(_host(url).encode("utf-8")).hexdigest()[:8]
    return destination.with_name(f"{destination.name}.{digest}{PART_SUFFIX}")


def slot_file(directory: Path, slot: ModelFile) -> Path | None:
    """返回该文件槽位在目录中实际存在的文件（含兼容文件名）。"""

    for name in slot.accepted_names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def slot_paths(spec: ModelSpec, directory: Path) -> dict[str, Path]:
    """把规格中的文件槽位映射到目录中的实际路径，缺失的槽位不返回。"""

    resolved: dict[str, Path] = {}
    for slot in spec.files:
        found = slot_file(directory, slot)
        if found is not None:
            resolved[slot.name] = found
    return resolved


def is_complete(spec: ModelSpec, directory: Path) -> bool:
    return all(slot_file(directory, slot) is not None for slot in spec.files)


def package_available(engine: ModelEngine | str) -> bool:
    """检查引擎所需的运行时依赖是否已安装（不实际导入，避免拖慢接口）。"""

    value = engine.value if isinstance(engine, ModelEngine) else str(engine)
    module = ENGINE_PACKAGES.get(value)
    if not module:
        return False
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _read_marker(directory: Path) -> dict[str, object]:
    path = directory / MARKER_FILENAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_marker(spec: ModelSpec, directory: Path, source: str, files: dict[str, Path]) -> None:
    payload = {
        "model_id": spec.id,
        "engine": spec.engine.value,
        "source": source,
        "installed_at": datetime.now(UTC).isoformat(),
        # 记录磁盘上的真实文件名，兼容文件名（如 model_q8.onnx）也能被正确校验。
        "files": {path.name: path.stat().st_size for path in files.values()},
    }
    try:
        (directory / MARKER_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("无法写入模型标记文件：%s", directory, exc_info=True)


def inspect(spec: ModelSpec) -> ModelStatus:
    directory = spec_dir(spec)
    missing: list[str] = []
    installed_bytes = 0
    for slot in spec.files:
        found = slot_file(directory, slot)
        if found is None:
            missing.append(slot.name)
            continue
        installed_bytes += found.stat().st_size

    marker = _read_marker(directory)
    recorded = marker.get("files")
    broken: list[str] = []
    if isinstance(recorded, dict):
        for name, expected in recorded.items():
            path = directory / str(name)
            stale = isinstance(expected, int) and path.is_file() and path.stat().st_size != expected
            if not path.is_file() or stale:
                broken.append(str(name))

    leftover = any(
        list(directory.glob(f"{slot.name}*{PART_SUFFIX}")) for slot in spec.files
    )

    if not missing and not broken:
        state = ModelState.READY
        message = "模型已就绪"
        source = str(marker.get("source") or "unknown")
    elif not missing and broken:
        state = ModelState.BROKEN
        message = f"模型文件不完整：{', '.join(broken)}，建议重新下载"
        source = None
    elif installed_bytes == 0 and not leftover:
        state = ModelState.MISSING
        message = "尚未安装，需要下载模型"
        source = None
    else:
        state = ModelState.PARTIAL
        source = None
        if leftover:
            message = "存在未完成的下载，可继续下载"
        else:
            message = f"缺少文件：{', '.join(missing)}"

    active = installer.snapshot(spec.id)
    if active is not None and active.active:
        state = ModelState.INSTALLING

    return ModelStatus(
        id=spec.id,
        name=spec.name,
        engine=spec.engine.value,
        tier=spec.tier.value,
        description=spec.description,
        languages=spec.languages,
        recommended=spec.recommended,
        note=spec.note,
        state=state.value,
        path=str(directory) if state != ModelState.MISSING else None,
        installed_bytes=installed_bytes,
        approx_bytes=spec.approx_bytes,
        missing_files=missing,
        message=message,
        source=source,
        engine_ready=package_available(spec.engine),
        progress=active,
    )


def inspect_all() -> list[ModelStatus]:
    order = {engine.value: index for index, engine in enumerate(ModelEngine)}
    statuses = [inspect(spec) for spec in list_specs()]
    statuses.sort(key=lambda item: (order.get(item.engine, 9), item.approx_bytes))
    return statuses


def resolve_directory(spec: ModelSpec) -> Path | None:
    """返回可用于识别的模型目录，只认工作台自己的目录。"""

    directory = spec_dir(spec)
    if is_complete(spec, directory):
        return directory
    return None


def resolve_engine(engine: ModelEngine | str) -> tuple[ModelSpec, Path] | None:
    """为一个引擎挑选可用的模型目录：首选推荐模型，其次任意已安装模型。"""

    candidates = specs_for_engine(engine)
    ordered: list[ModelSpec] = []
    preferred = preferred_spec(engine)
    if preferred is not None:
        ordered.append(preferred)
    ordered.extend(item for item in candidates if item not in ordered)

    for spec in ordered:
        directory = resolve_directory(spec)
        if directory is not None:
            return spec, directory
    return None


def ensure_engine_ready(engine: ModelEngine | str, mode_label: str) -> tuple[ModelSpec, Path]:
    value = engine.value if isinstance(engine, ModelEngine) else str(engine)
    resolved = resolve_engine(value)

    if not package_available(value):
        label = ENGINE_PACKAGE_LABELS.get(value, value)
        raise EngineUnavailableError(f"缺少 {label} 组件，请先安装后再使用{mode_label}")

    if resolved is None:
        names = "、".join(item.name for item in specs_for_engine(value))
        raise EngineUnavailableError(f"{mode_label}需要先安装识别模型（{names}）")
    return resolved


def ensure_model_ready(model_id: str, mode_label: str) -> tuple[ModelSpec, Path]:
    """按用户指定的模型准备引擎：先确认依赖包，再确认模型文件完整。"""

    spec = require_spec(model_id)
    engine = spec.engine.value
    if not package_available(engine):
        label = ENGINE_PACKAGE_LABELS.get(engine, engine)
        raise EngineUnavailableError(f"缺少 {label} 组件，请先安装后再使用{mode_label}")
    directory = resolve_directory(spec)
    if directory is None:
        raise EngineUnavailableError(f"{mode_label}需要先安装识别模型（{spec.name}）")
    return spec, directory


def remove(spec: ModelSpec) -> bool:
    directory = spec_dir(spec).resolve()
    root = models_root().resolve()
    if root not in directory.parents:
        raise ModelDownloadError("拒绝删除模型目录以外的路径")
    if not directory.exists():
        return False
    shutil.rmtree(directory)
    return True


@dataclass
class _Task:
    progress: InstallProgress
    cancel: threading.Event
    lock: threading.Lock
    thread: threading.Thread | None = None

    def report(self, **changes: object) -> None:
        with self.lock:
            for key, value in changes.items():
                setattr(self.progress, key, value)
            self.progress.updated_at = time.time()


class ModelInstaller:
    """串行管理每个模型的安装任务，对外只暴露进度快照。"""

    def __init__(self) -> None:
        self._tasks: dict[str, _Task] = {}
        self._lock = threading.Lock()

    def snapshot(self, model_id: str) -> InstallProgress | None:
        with self._lock:
            task = self._tasks.get(model_id)
        if task is None:
            return None
        with task.lock:
            return replace(task.progress)

    def snapshots(self) -> list[InstallProgress]:
        with self._lock:
            ids = list(self._tasks)
        return [item for item in (self.snapshot(model_id) for model_id in ids) if item]

    def cancel(self, model_id: str) -> InstallProgress | None:
        with self._lock:
            task = self._tasks.get(model_id)
        if task is None:
            return None
        task.cancel.set()
        task.report(status="cancelling", message="正在取消")
        return self.snapshot(model_id)

    def _start(
        self,
        spec: ModelSpec,
        action: str,
        worker: Callable[[_Task], None],
        total_files: int,
        message: str,
    ) -> InstallProgress:
        with self._lock:
            current = self._tasks.get(spec.id)
            if current is not None and current.progress.active:
                raise ModelInstallBusyError(f"{spec.name} 正在安装中")
            task = _Task(
                progress=InstallProgress(
                    model_id=spec.id,
                    action=action,
                    status="pending",
                    percent=0,
                    total_files=total_files,
                    message=message,
                    updated_at=time.time(),
                ),
                cancel=threading.Event(),
                lock=threading.Lock(),
            )
            self._tasks[spec.id] = task

        def run() -> None:
            try:
                worker(task)
            except _Cancelled:
                task.report(status="cancelled", message="已取消安装")
            except Exception as exc:  # noqa: BLE001
                logger.warning("模型安装失败 %s: %s", spec.id, exc)
                task.report(status="failed", error=str(exc), message=str(exc))
            finally:
                task.report(updated_at=time.time())

        task.thread = threading.Thread(target=run, name=f"model-install-{spec.id}", daemon=True)
        task.thread.start()
        snapshot = self.snapshot(spec.id)
        assert snapshot is not None
        return snapshot

    def download(self, model_id: str) -> InstallProgress:
        spec = require_spec(model_id)
        return self._start(
            spec,
            "download",
            lambda task: self._run_download(spec, task),
            len(spec.files),
            "正在准备下载模型",
        )

    def _run_download(self, spec: ModelSpec, task: _Task) -> None:
        task.report(status="running", message="正在连接模型下载源")
        destination = spec_dir(spec)
        destination.mkdir(parents=True, exist_ok=True)

        known_total = sum(slot.size or 0 for slot in spec.files)
        downloaded = 0
        total = known_total
        resolved: dict[str, Path] = {}

        for index, slot in enumerate(spec.files, start=1):
            target = destination / slot.name
            existing = slot_file(destination, slot)
            if existing is not None and existing != target:
                # 已经存在兼容文件名的同款文件，直接沿用。
                resolved[slot.name] = existing
                downloaded += existing.stat().st_size
                continue
            if target.is_file() and target.stat().st_size in slot.accepted_sizes:
                resolved[slot.name] = target
                downloaded += target.stat().st_size
                task.report(
                    percent=int(downloaded / max(total, 1) * 100),
                    completed_files=index,
                    message=f"{slot.name} 已存在，跳过下载",
                )
                continue

            task.report(
                completed_files=index - 1,
                file_name=slot.name,
                message=f"正在下载 {slot.name}",
            )
            written = self._fetch_with_fallback(spec, task, slot, target)
            resolved[slot.name] = target
            downloaded += written
            if slot.size is None:
                total += written
            task.report(
                downloaded_bytes=downloaded,
                total_bytes=total,
                percent=min(99, int(downloaded / max(total, 1) * 100)),
                completed_files=index,
                message=f"{slot.name} 下载完成",
            )

        _write_marker(spec, destination, "download", resolved)
        task.report(
            status="completed",
            percent=100,
            downloaded_bytes=downloaded,
            total_bytes=total,
            message="模型下载完成",
        )

    def _fetch_with_fallback(self, spec: ModelSpec, task: _Task, slot: ModelFile, target: Path) -> int:
        errors: list[str] = []
        attempts = max(1, settings.download_retries)

        for url in slot.urls:
            # 每个源用自己的临时文件：不同源的同款文件可能相差少量字节，混用会拼出损坏的模型。
            part = _part_path(target, url)
            for attempt in range(attempts):
                if task.cancel.is_set():
                    raise _Cancelled("下载已取消")
                try:
                    written = self._fetch(task, slot, url, target, part)
                except _Cancelled:
                    raise
                except httpx.HTTPError as exc:
                    errors.append(f"{_host(url)}：{exc.__class__.__name__}")
                except OSError as exc:
                    errors.append(f"{_host(url)}：{exc}")
                else:
                    # 成功后清掉同名历史临时文件（含早期不带来源标识的命名）。
                    for leftover in target.parent.glob(f"{target.name}*{PART_SUFFIX}"):
                        leftover.unlink(missing_ok=True)
                    return written
                if attempt < attempts - 1:
                    task.report(message=f"{_host(url)} 连接失败，正在重试（{attempt + 1}/{attempts}）")
                    time.sleep(1.0 + attempt)
            # 该源不可用，丢弃它的残留
            part.unlink(missing_ok=True)

        detail = "；".join(errors[-3:]) if errors else "所有下载源均不可用"
        raise ModelDownloadError(f"{spec.name} 下载失败：{detail}")

    def _fetch(self, task: _Task, slot: ModelFile, url: str, target: Path, part: Path) -> int:
        existing = part.stat().st_size if part.is_file() else 0
        headers = {"User-Agent": USER_AGENT}
        if existing:
            headers["Range"] = f"bytes={existing}-"

        timeout = httpx.Timeout(settings.download_timeout_seconds, connect=20.0)
        with (
            httpx.Client(follow_redirects=True, timeout=timeout) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            if response.status_code == 416 and existing:
                part.replace(target)
                return target.stat().st_size
            response.raise_for_status()
            resuming = existing > 0 and response.status_code == 206
            if existing and not resuming:
                existing = 0
            declared = _int_or_none(response.headers.get("Content-Length"))
            total = slot.size or ((declared or 0) + existing) or 0
            written = existing
            mode = "ab" if resuming else "wb"
            with part.open(mode) as handle:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    if task.cancel.is_set():
                        raise _Cancelled("下载已取消")
                    handle.write(chunk)
                    written += len(chunk)
                    if total:
                        task.report(
                            percent=min(99, int(written / total * 100)),
                            downloaded_bytes=written,
                            total_bytes=total,
                            message=f"正在下载 {slot.name}（{written * 100 // total}%）",
                        )

        if slot.accepted_sizes and written not in slot.accepted_sizes:
            part.unlink(missing_ok=True)
            expected = " / ".join(str(item) for item in slot.accepted_sizes)
            raise ModelDownloadError(f"{slot.name} 大小校验失败：期望 {expected}，实际 {written}")
        part.replace(target)
        return written


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host or url
    except (httpx.InvalidURL, ValueError):
        return url


installer = ModelInstaller()


def engine_status() -> list[dict[str, object]]:
    """给接口用的引擎级概览。"""

    overview: list[dict[str, object]] = []
    for engine in ModelEngine:
        specs = specs_for_engine(engine)
        resolved = resolve_engine(engine)
        package_ready = package_available(engine)
        overview.append(
            {
                "engine": engine.value,
                "package_ready": package_ready,
                # 工作台模型目录完整即视为可用。
                "model_ready": resolved is not None,
                "ready": bool(package_ready and resolved is not None),
                "active_model": resolved[0].id if resolved else None,
                "specs": [spec.id for spec in specs],
            }
        )
    return overview
