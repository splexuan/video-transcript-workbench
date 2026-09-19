"""识别模型清单。

每个模型规格声明：安装后需要的文件、按优先级排列的下载地址、体积和兼容文件名。
下载地址只使用国内可直连的镜像（hf-mirror 与 ModelScope），并依次回退。
"""

from __future__ import annotations

from app.domain import ModelEngine, ModelFile, ModelSpec, ModelTier

# 只保留国内可直连的镜像源，不访问 huggingface.co 官方源。
HF_MIRROR = "https://hf-mirror.com"
MODEL_SCOPE = "https://www.modelscope.cn/models"

SENSE_VOICE_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SENSE_VOICE_MIRROR_REPO = "xiaowangge/sherpa-onnx-sense-voice-small"

SENSE_VOICE_MODEL_BYTES = 239_233_841
SENSE_VOICE_TOKENS_BYTES = 315_894
# ModelScope 上的同款量化模型（model_q8.onnx）与 hf-mirror 的分发差 275 字节。
SENSE_VOICE_MIRROR_MODEL_BYTES = 239_234_116


def _mirror_urls(repo: str, filename: str) -> tuple[str, ...]:
    """按优先级返回国内镜像地址。

    ModelScope 直接走国内 CDN；hf-mirror 实际会 302 到海外存储，只作兜底。
    """

    return (
        f"{MODEL_SCOPE}/{repo}/resolve/master/{filename}",
        f"{HF_MIRROR}/{repo}/resolve/main/{filename}",
    )


def _sense_voice_files() -> tuple[ModelFile, ...]:
    # ModelScope 上没有 csukuangfj 的镜像仓库，改用它上面的同款量化模型。
    return (
        ModelFile(
            name="model.int8.onnx",
            urls=(
                f"{MODEL_SCOPE}/{SENSE_VOICE_MIRROR_REPO}/resolve/master/model_q8.onnx",
                f"{HF_MIRROR}/{SENSE_VOICE_REPO}/resolve/main/model.int8.onnx",
            ),
            size=SENSE_VOICE_MODEL_BYTES,
            alternates=("model_q8.onnx", "model.onnx"),
            size_alternates=(SENSE_VOICE_MIRROR_MODEL_BYTES,),
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


WHISPER_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

# 只保留两档：small 覆盖日常，medium 留给愿意等更久换准确度的人。
WHISPER_MODEL_BYTES: dict[str, int] = {
    "small": 483_546_902,
    "medium": 1_527_906_378,
}

WHISPER_LABELS: dict[str, tuple[str, str]] = {
    "small": ("faster-whisper small（推荐）", "中文准确度与速度的折中，约 483 MB，适合大多数视频。"),
    "medium": ("faster-whisper medium（高精度）", "中文准确度更高，约 1.5 GB，CPU 转写明显变慢。"),
}


def _whisper_files(size: str) -> tuple[ModelFile, ...]:
    repo = f"Systran/faster-whisper-{size}"
    files: list[ModelFile] = []
    for filename in WHISPER_FILES:
        size_hint = WHISPER_MODEL_BYTES[size] if filename == "model.bin" else None
        files.append(
            ModelFile(name=filename, urls=_mirror_urls(repo, filename), size=size_hint)
        )
    return tuple(files)


def _build_catalog() -> dict[str, ModelSpec]:
    catalog: dict[str, ModelSpec] = {
        "sensevoice-small": ModelSpec(
            id="sensevoice-small",
            name="SenseVoice Small（极速文本）",
            engine=ModelEngine.SENSEVOICE,
            tier=ModelTier.FAST,
            description="阿里 SenseVoice 量化模型，速度极快，用于「极速文本」模式的本地识别。",
            files=_sense_voice_files(),
            languages="中文 / 英文 / 日文 / 韩文 / 粤语",
            recommended=True,
            note="时间轴为 30 秒粒度，适合先拿到通顺文案再人工校对。",
            requires_package="sherpa-onnx",
        )
    }
    for size, (label, description) in WHISPER_LABELS.items():
        catalog[f"faster-whisper-{size}"] = ModelSpec(
            id=f"faster-whisper-{size}",
            name=label,
            engine=ModelEngine.FASTER_WHISPER,
            tier=ModelTier.ACCURATE,
            description=description,
            files=_whisper_files(size),
            languages="多语言，中文效果优先",
            recommended=size == "small",
            note="输出句段级时间轴，可直接用于 SRT 字幕校对。",
            requires_package="faster-whisper",
        )
    return catalog


CATALOG: dict[str, ModelSpec] = _build_catalog()

ENGINE_LABELS: dict[str, str] = {
    ModelEngine.SENSEVOICE.value: "SenseVoice",
    ModelEngine.FASTER_WHISPER.value: "faster-whisper",
}

# 引擎对应的 Python 导入模块名（用于检测运行时依赖是否已安装）。
ENGINE_PACKAGES: dict[str, str] = {
    ModelEngine.SENSEVOICE.value: "sherpa_onnx",
    ModelEngine.FASTER_WHISPER.value: "faster_whisper",
}

# 面向用户的依赖名称，用在错误提示里。
ENGINE_PACKAGE_LABELS: dict[str, str] = {
    ModelEngine.SENSEVOICE.value: "sherpa-onnx",
    ModelEngine.FASTER_WHISPER.value: "faster-whisper",
}


def list_specs() -> list[ModelSpec]:
    return list(CATALOG.values())


def get_spec(model_id: str) -> ModelSpec | None:
    return CATALOG.get(model_id)


def require_spec(model_id: str) -> ModelSpec:
    from app.domain import ModelNotFoundError

    spec = CATALOG.get(model_id)
    if spec is None:
        raise ModelNotFoundError(f"未知的模型：{model_id}")
    return spec


def specs_for_engine(engine: ModelEngine | str) -> list[ModelSpec]:
    value = engine.value if isinstance(engine, ModelEngine) else str(engine)
    return [item for item in CATALOG.values() if item.engine.value == value]


def engine_for_tier(tier: ModelTier | str) -> ModelEngine:
    value = tier.value if isinstance(tier, ModelTier) else str(tier)
    if value == ModelTier.ACCURATE.value:
        return ModelEngine.FASTER_WHISPER
    return ModelEngine.SENSEVOICE


def preferred_spec(engine: ModelEngine | str) -> ModelSpec | None:
    """返回引擎的首选模型，用于自动挑选已安装的模型。"""

    candidates = specs_for_engine(engine)
    for item in candidates:
        if item.recommended:
            return item
    return candidates[0] if candidates else None
