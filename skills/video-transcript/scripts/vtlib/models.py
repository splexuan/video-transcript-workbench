"""识别模型的清单、完整性检查与断点续传下载。

只使用 SenseVoice（约 230 MB），从国内可直连的镜像下载：
ModelScope 优先，hf-mirror 兜底。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import get_config

HF_MIRROR = "https://hf-mirror.com"
MODEL_SCOPE = "https://www.modelscope.cn/models"
USER_AGENT = "video-transcript-skill/1.0"
CHUNK_BYTES = 1 << 16

SENSE_VOICE_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SENSE_VOICE_MIRROR_REPO = "xiaowangge/sherpa-onnx-sense-voice-small"
SENSE_VOICE_MODEL_BYTES = 239_233_841
SENSE_VOICE_MIRROR_BYTES = 239_234_116
SENSE_VOICE_TOKENS_BYTES = 315_894


class ModelError(RuntimeError):
    code = "MODEL_ERROR"


@dataclass(frozen=True)
class ModelFile:
    name: str
    urls: tuple[str, ...]
    size: int | None = None
    alternates: tuple[str, ...] = ()
    size_alternates: tuple[int, ...] = ()

    @property
    def accepted_names(self) -> tuple[str, ...]:
        return (self.name, *self.alternates)

    @property
    def accepted_sizes(self) -> tuple[int, ...]:
        return (self.size, *self.size_alternates) if self.size is not None else ()


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    files: tuple[ModelFile, ...]
    approx_bytes: int
    description: str

    @property
    def approx_label(self) -> str:
        return human_size(self.approx_bytes)


def human_size(value: int | None) -> str:
    if not value or value <= 0:
        return "未知"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{int(size)} {units[index]}" if index == 0 else f"{size:.1f} {units[index]}"


def _sense_voice() -> ModelSpec:
    files = (
        ModelFile(
            name="model.int8.onnx",
            urls=(
                f"{MODEL_SCOPE}/{SENSE_VOICE_MIRROR_REPO}/resolve/master/model_q8.onnx",
                f"{HF_MIRROR}/{SENSE_VOICE_REPO}/resolve/main/model.int8.onnx",
            ),
            size=SENSE_VOICE_MODEL_BYTES,
            alternates=("model_q8.onnx", "model.onnx"),
            size_alternates=(SENSE_VOICE_MIRROR_BYTES,),
        ),
        ModelFile(
            name="tokens.txt",
            urls=(
                f"{MODEL_SCOPE}/{SENSE_VOICE_MIRROR_REPO}/resolve/master/tokens.txt",
                f"{HF_MIRROR}/{SENSE_VOICE_REPO}/resolve/main/tokens.txt",
            ),
            size=SENSE_VOICE_TOKENS_BYTES,
        ),
    )
    return ModelSpec(
        id="sensevoice-small",
        name="SenseVoice Small",
        files=files,
        approx_bytes=SENSE_VOICE_MODEL_BYTES + SENSE_VOICE_TOKENS_BYTES,
        description="中文/英文/日文/韩文/粤语，速度快，30 秒粒度时间轴。",
    )


CATALOG: dict[str, ModelSpec] = {"sensevoice-small": _sense_voice()}


def get_spec(model_id: str) -> ModelSpec | None:
    return CATALOG.get(model_id)


def require_spec(model_id: str) -> ModelSpec:
    spec = CATALOG.get(model_id)
    if spec is None:
        raise ModelError(f"未知的识别模型：{model_id}。本技能只使用 sensevoice-small")
    return spec


def model_dir(model_id: str) -> Path:
    return get_config().models_dir / model_id


def _slot_file(directory: Path, slot: ModelFile) -> Path | None:
    for name in slot.accepted_names:
        candidate = directory / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def missing_files(spec: ModelSpec, directory: Path) -> list[str]:
    missing: list[str] = []
    for slot in spec.files:
        found = _slot_file(directory, slot)
        if found is None or (
            slot.accepted_sizes and found.stat().st_size not in slot.accepted_sizes
        ):
            missing.append(slot.name)
    return missing


def is_complete(model_id: str) -> bool:
    spec = require_spec(model_id)
    return not missing_files(spec, model_dir(model_id))


def resolve(model_id: str) -> tuple[ModelSpec, Path] | None:
    spec = get_spec(model_id)
    if spec is None:
        return None
    directory = model_dir(model_id)
    return (spec, directory) if not missing_files(spec, directory) else None


def ensure_model(model_id: str) -> tuple[ModelSpec, Path]:
    spec = require_spec(model_id)
    directory = model_dir(model_id)
    missing = missing_files(spec, directory)
    if missing:
        raise ModelError(
            f"{spec.name} 尚未安装（缺少 {', '.join(missing)}）。"
            f"先运行：python scripts/extract.py --install-model {model_id}"
        )
    return spec, directory


Progress = Callable[[int, int, str], None]


def _part_path(target: Path, url: str) -> Path:
    """不同镜像的字节可能略有差异，临时文件按来源区分，避免拼接出损坏的文件。"""

    host = httpx.URL(url).host or "source"
    digest = hashlib.sha1(host.encode("utf-8")).hexdigest()[:8]
    return target.with_name(f"{target.name}.{digest}.part")


def _download_file(
    url: str,
    target: Path,
    slot: ModelFile,
    progress: Progress,
    *,
    base_done: int,
    total: int,
) -> None:
    part = _part_path(target, url)
    existing = part.stat().st_size if part.is_file() else 0
    headers = {"User-Agent": USER_AGENT}
    if existing:
        headers["Range"] = f"bytes={existing}-"

    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=20.0)) as client:
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 416 and existing:
                if not slot.accepted_sizes or existing in slot.accepted_sizes:
                    part.replace(target)
                    return
                part.unlink(missing_ok=True)
                raise OSError("续传位置与文件大小不符，已丢弃临时文件")

            response.raise_for_status()

            # 不能只看状态码：ModelScope 用 200 返回部分内容，靠 Content-Range 指明起点。
            resuming = False
            if existing:
                content_range = response.headers.get("content-range", "")
                match = re.match(r"bytes\s+(\d+)-", content_range)
                if match is not None:
                    resuming = int(match.group(1)) == existing
                else:
                    length = response.headers.get("content-length")
                    if length and slot.size:
                        resuming = int(length) == slot.size - existing
            if not resuming:
                existing = 0
            written = existing
            mode = "ab" if resuming else "wb"
            with part.open(mode) as handle:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    handle.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(base_done + written, total, slot.name)

    actual = part.stat().st_size
    if slot.accepted_sizes and actual not in slot.accepted_sizes:
        expected = " / ".join(str(item) for item in slot.accepted_sizes)
        part.unlink(missing_ok=True)
        raise ModelError(f"{slot.name} 大小校验失败：期望 {expected}，实际 {actual}")
    part.replace(target)
    for leftover in target.parent.glob(f"{target.name}*.part"):
        leftover.unlink(missing_ok=True)


def install(model_id: str, progress: Progress | None = None) -> Path:
    """下载并校验模型；已存在的文件跳过，返回模型目录。"""

    spec = require_spec(model_id)
    destination = model_dir(model_id)
    destination.mkdir(parents=True, exist_ok=True)

    pending: list[tuple[ModelFile, Path]] = []
    for slot in spec.files:
        found = _slot_file(destination, slot)
        if found is not None and (
            not slot.accepted_sizes or found.stat().st_size in slot.accepted_sizes
        ):
            continue
        pending.append((slot, destination / slot.name))

    known_total = sum(
        slot.size or 0 for slot, _ in pending
    )
    done = 0
    for slot, target in pending:
        errors: list[str] = []
        for url in slot.urls:
            try:
                _download_file(
                    url,
                    target,
                    slot,
                    progress or (lambda *_: None),
                    base_done=done,
                    total=known_total or spec.approx_bytes,
                )
            except (httpx.HTTPError, OSError, ModelError) as exc:
                errors.append(f"{httpx.URL(url).host or url}：{exc}")
                # 保留该来源的 .part，下次运行可以续传；换源时用另一个临时文件
                continue
            done += slot.size or target.stat().st_size
            break
        else:
            detail = "；".join(errors[-3:]) or "所有下载源均不可用"
            raise ModelError(f"{spec.name} 下载失败：{detail}")

    missing = missing_files(spec, destination)
    if missing:
        raise ModelError(f"{spec.name} 安装不完整，仍缺少：{', '.join(missing)}")
    return destination


def status(model_id: str) -> dict:
    spec = get_spec(model_id)
    if spec is None:
        return {"id": model_id, "ready": False, "error": "unknown model"}
    directory = model_dir(model_id)
    missing = missing_files(spec, directory)
    installed_bytes = (
        sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())
        if directory.is_dir()
        else 0
    )
    return {
        "id": spec.id,
        "name": spec.name,
        "ready": not missing,
        "path": str(directory),
        "installed_bytes": installed_bytes,
        "installed_label": human_size(installed_bytes),
        "missing": missing,
    }
