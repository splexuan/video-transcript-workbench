from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ModelEngine(StrEnum):
    """识别引擎类型。一个引擎可以对应多个可下载的模型规格。"""

    SENSEVOICE = "sensevoice"
    FASTER_WHISPER = "faster_whisper"


class ModelTier(StrEnum):
    """模型面向的提取模式。"""

    FAST = "fast"
    ACCURATE = "accurate"


class ModelState(StrEnum):
    READY = "ready"
    MISSING = "missing"
    PARTIAL = "partial"
    INSTALLING = "installing"
    BROKEN = "broken"


@dataclass(frozen=True, slots=True)
class ModelFile:
    """模型目录中的一个必需文件。

    `name` 是安装后的规范文件名；`alternates` 列出内容等价的兼容文件名，
    用于识别并复用旧版本或第三方分发的同款模型目录。
    """

    name: str
    urls: tuple[str, ...] = ()
    size: int | None = None
    alternates: tuple[str, ...] = ()
    # 不同镜像分发的同一模型可能相差少量字节（例如量化差异），这里显式列出可接受的字节数。
    size_alternates: tuple[int, ...] = ()

    @property
    def accepted_names(self) -> tuple[str, ...]:
        return (self.name, *self.alternates)

    @property
    def accepted_sizes(self) -> tuple[int, ...]:
        return (self.size, *self.size_alternates) if self.size is not None else ()


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    name: str
    engine: ModelEngine
    tier: ModelTier
    description: str
    files: tuple[ModelFile, ...]
    languages: str = "中文 / 英文 / 日文 / 韩文 / 粤语"
    recommended: bool = False
    note: str | None = None
    requires_package: str | None = None

    @property
    def approx_bytes(self) -> int:
        return sum(item.size or 0 for item in self.files)

    @property
    def approx_label(self) -> str:
        return human_size(self.approx_bytes)


@dataclass(slots=True)
class ModelStatus:
    """某个模型规格在当前电脑上的实际状态。"""

    id: str
    name: str
    engine: str
    tier: str
    description: str
    languages: str
    recommended: bool
    note: str | None
    state: str
    path: str | None
    installed_bytes: int
    approx_bytes: int
    missing_files: list[str] = field(default_factory=list)
    message: str = ""
    source: str | None = None
    engine_ready: bool = False
    progress: InstallProgress | None = None

    @property
    def ready(self) -> bool:
        return self.state == ModelState.READY.value


@dataclass(slots=True)
class InstallProgress:
    """后台安装任务的实时进度快照。"""

    model_id: str
    action: str
    status: str
    percent: int
    downloaded_bytes: int = 0
    total_bytes: int = 0
    file_name: str = ""
    completed_files: int = 0
    total_files: int = 0
    message: str = ""
    error: str | None = None
    updated_at: float = 0.0

    @property
    def active(self) -> bool:
        return self.status in {"pending", "running", "cancelling"}


def human_size(value: int | None) -> str:
    if not value or value <= 0:
        return "未知"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"


class ModelError(RuntimeError):
    """模型相关错误基类。"""

    code = "MODEL_ERROR"


class ModelNotFoundError(ModelError):
    code = "MODEL_NOT_FOUND"


class ModelDownloadError(ModelError):
    code = "MODEL_DOWNLOAD_ERROR"


class ModelInstallBusyError(ModelError):
    code = "MODEL_BUSY"


class EngineUnavailableError(ModelError):
    """识别引擎的运行时依赖没有安装。"""

    code = "ENGINE_UNAVAILABLE"
