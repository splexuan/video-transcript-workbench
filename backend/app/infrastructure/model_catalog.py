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


PUNCT_REPO = "csukuangfj/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12"
PUNCT_FALLBACK_REPO = "pengzhendong/punct-ct-transformer-zh-en"
# 词表内嵌在 onnx 元数据里，目录中只需要 model.onnx 一个文件。
PUNCT_MODEL_BYTES = 294_372_519


def _punctuation_files() -> tuple[ModelFile, ...]:
    return (
        ModelFile(
            name="model.onnx",
            urls=(
                f"{MODEL_SCOPE}/{PUNCT_REPO}/resolve/master/model.onnx",
                f"{MODEL_SCOPE}/{PUNCT_FALLBACK_REPO}/resolve/master/model.onnx",
                f"{HF_MIRROR}/{PUNCT_REPO}/resolve/main/model.onnx",
            ),
            size=PUNCT_MODEL_BYTES,
        ),
    )


# 语音合成：Kokoro 官方只发 tar.bz2 安装包（里面有 espeak-ng-data/ 这类子目录与几百个文件），
# 逐文件下载既不现实也没必要，所以走「归档型」安装：下载后自动解压（见 model_store）。
KOKORO_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
KOKORO_V1_1_PACKAGE = "kokoro-int8-multi-lang-v1_1.tar.bz2"
KOKORO_V1_1_BYTES = 147_031_220
# 解压后必须存在的路径：判「装好了没有」只看这几项（含一个目录）。
KOKORO_V1_1_PATHS = (
    "model.int8.onnx",
    "voices.bin",
    "tokens.txt",
    "lexicon-zh.txt",
    "espeak-ng-data",
)
# 同一个模型的非量化版：音质明显更好，代价是下载体积与常驻内存都约为 int8 的 2.5 倍。
KOKORO_FP32_PACKAGE = "kokoro-multi-lang-v1_1.tar.bz2"
KOKORO_FP32_BYTES = 364_816_464
# 两个精度版本的模型文件名不同（model.onnx / model.int8.onnx），所以完整性只查两边
# 都有的这几个；真正用哪个文件由 tts_engine 在构造时挑，挑不到会明确报错。
KOKORO_FP32_PATHS = (
    "voices.bin",
    "tokens.txt",
    "lexicon-zh.txt",
    "espeak-ng-data",
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
    catalog["punct-ct-transformer-zh-en"] = ModelSpec(
        id="punct-ct-transformer-zh-en",
        name="中文标点恢复（智能分句）",
        engine=ModelEngine.PUNCTUATION,
        tier=ModelTier.UTILITY,
        description="CT-Transformer 中文标点模型，给没有标点的平台字幕补标点，智能分句据此整理出自然段。",
        files=_punctuation_files(),
        languages="中文 / 英文",
        recommended=True,
        note="只在点「智能分句」时用到，不参与语音识别；不安装则字幕文案无法自动整理标点。",
        requires_package="sherpa-onnx",
    )
    catalog["kokoro-int8-multi-lang-v1_1"] = ModelSpec(
        id="kokoro-int8-multi-lang-v1_1",
        name="Kokoro 82M v1.1（中文语音合成）",
        engine=ModelEngine.TTS,
        tier=ModelTier.UTILITY,
        description="Kokoro-82M v1.1 中英双语量化模型，100 个中文音色，把文案读成音频。",
        files=(
            ModelFile(
                name=KOKORO_V1_1_PACKAGE,
                urls=(f"{KOKORO_RELEASE}/{KOKORO_V1_1_PACKAGE}",),
                size=KOKORO_V1_1_BYTES,
            ),
        ),
        languages="中文 / 英文",
        recommended=True,
        note="官方以 tar.bz2 发布（含音素数据），下载后自动解压；约 147 MB，解压后约 183 MB。",
        requires_package="sherpa-onnx",
        archive=True,
        required_paths=KOKORO_V1_1_PATHS,
    )
    catalog["kokoro-multi-lang-v1_1"] = ModelSpec(
        id="kokoro-multi-lang-v1_1",
        name="Kokoro 82M v1.1 高精度版（语音合成）",
        engine=ModelEngine.TTS,
        tier=ModelTier.UTILITY,
        description="Kokoro-82M v1.1 的非量化版本，音色与 int8 版相同，音质明显更干净。",
        files=(
            ModelFile(
                name=KOKORO_FP32_PACKAGE,
                urls=(f"{KOKORO_RELEASE}/{KOKORO_FP32_PACKAGE}",),
                size=KOKORO_FP32_BYTES,
            ),
        ),
        languages="中文 / 英文",
        recommended=False,
        note="音质更干净，代价是体积与内存：每个合成实例常驻约 430MB（int8 版约 240MB），"
        "12 核开 4 个实例时合计约 1.8GB；内存较小的机器建议合成时先关掉识别任务。",
        requires_package="sherpa-onnx",
        archive=True,
        required_paths=KOKORO_FP32_PATHS,
    )
    return catalog


CATALOG: dict[str, ModelSpec] = _build_catalog()

ENGINE_LABELS: dict[str, str] = {
    ModelEngine.SENSEVOICE.value: "SenseVoice",
    ModelEngine.FASTER_WHISPER.value: "faster-whisper",
    ModelEngine.PUNCTUATION.value: "标点恢复",
    ModelEngine.TTS.value: "语音合成",
}

# 引擎对应的 Python 导入模块名（用于检测运行时依赖是否已安装）。
ENGINE_PACKAGES: dict[str, str] = {
    ModelEngine.SENSEVOICE.value: "sherpa_onnx",
    ModelEngine.FASTER_WHISPER.value: "faster_whisper",
    ModelEngine.PUNCTUATION.value: "sherpa_onnx",
    ModelEngine.TTS.value: "sherpa_onnx",
}

# 面向用户的依赖名称，用在错误提示里。
ENGINE_PACKAGE_LABELS: dict[str, str] = {
    ModelEngine.SENSEVOICE.value: "sherpa-onnx",
    ModelEngine.FASTER_WHISPER.value: "faster-whisper",
    ModelEngine.PUNCTUATION.value: "sherpa-onnx",
    ModelEngine.TTS.value: "sherpa-onnx",
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
