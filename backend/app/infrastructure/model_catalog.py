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


# 语音合成：ZipVoice 是零样本音色克隆模型，用一段参考音频克隆出说话人。
# 官方同样只发 tar.bz2 安装包（含 espeak-ng-data/ 这类子目录与几百个文件），逐文件下载既不现实
# 也没必要，所以走「归档型」安装：下载后自动解压（见 model_store）。
ZIPVOICE_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
# 声码器（Vocos）单独发布，不在 tar 包里；它与模型版本绑定，因此作为「附属文件」
# 跟着模型一起装到同一个目录，由 model_store 在解压后单独下载（files 的第二项起）。
ZIPVOICE_VOCODER_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models"

# ZipVoice 官方只有 GitHub Releases 这一个渠道：Hugging Face 与 ModelScope 上都没有它的
# ONNX 整包（HF 上只有 k2-fsa/ZipVoice 的 PyTorch 权重，不是 sherpa-onnx 要的东西）。
# 而官方直连实测只有 47 KB/s——455 MB 的包要等两个多小时，比其他走 ModelScope 的模型
# （10 MB/s 量级）慢两个数量级。所以下载源排成四级：
#   1. 社区维护的 ModelScope 国内镜像（原样镜像官方文件，字节一致，实测 10 MB/s）
#   2~3. GitHub 加速代理（镜像失效时的等价替代）
#   4. 官方源兜底（代理挂了还能有个能用的，只是慢）
# 另有「慢到不像话就换源」的机制兜底（见 model_store 的 SLOW_BYTES_PER_SEC）。
GH_PROXY = "https://gh-proxy.com/"
GH_FAST = "https://ghfast.top/"
# 国内镜像仓库：lexuan/sherpa-onnx-tts-models，只放 ZipVoice 的官方文件（原样上传，字节与官方一致）
TTS_MIRROR_REPO = "lexuan/sherpa-onnx-tts-models"


def _zipvoice_sources(github_url: str) -> tuple[str, ...]:
    """ZipVoice 文件的下载源，优先级：国内镜像 → GitHub 加速 → 官方。

    文件名从 GitHub 地址里取：两个 release（tts-models 与 vocoder-models）里的文件名与
    国内镜像仓库里的完全一致，所以两个文件能共用这一个函数。
    """
    name = github_url.rsplit("/", 1)[-1]
    return (
        f"{MODEL_SCOPE}/{TTS_MIRROR_REPO}/resolve/master/{name}",
        f"{GH_PROXY}{github_url}",
        f"{GH_FAST}{github_url}",
        github_url,
    )


ZIPVOICE_FP32_PACKAGE = "sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia.tar.bz2"
ZIPVOICE_FP32_BYTES = 477_800_463
ZIPVOICE_VOCODER_FILE = "vocos_24khz.onnx"
ZIPVOICE_VOCODER_BYTES = 54_157_409

# 上游换过两代文件名（encoder/decoder 与 text_encoder/fm_decoder），具体用哪一代随发布变，
# 所以这两项用 required_any 表达「每组至少存在其一」；其余文件同名，走必需路径。
# encoder/decoder 是核心模型——不检查的话，缺了它照样被判成「已就绪」，要等真正合成时才报错。
ZIPVOICE_REQUIRED_PATHS = (
    "tokens.txt",
    "lexicon.txt",
    "espeak-ng-data",
    "test_wavs/leijun-1.wav",
    ZIPVOICE_VOCODER_FILE,
)
ZIPVOICE_REQUIRED_ANY = (
    ("encoder.onnx", "text_encoder.onnx"),
    ("decoder.onnx", "fm_decoder.onnx"),
)

# 上游还有非蒸馏的完整版（sherpa-onnx-zipvoice-zh-en-emilia，约 605 MB），这里**故意不提供**：
# 实测它在这个 sherpa-onnx 实现下音质不如蒸馏版（同样的参考音频与文本，响度低约 4 dB、
# 听感更差），而且它不自带拼音词典、也没有内置音色需要的参考音频、模型文件名与蒸馏版
# 完全不同。官方文档里同样只有蒸馏版的示例，看起来完整版没被充分验证过。

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
    catalog["sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia"] = ModelSpec(
        id="sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia",
        name="ZipVoice 蒸馏版 fp32（语音合成）",
        engine=ModelEngine.TTS,
        tier=ModelTier.UTILITY,
        description="ZipVoice 零样本音色克隆模型：给一段参考音频，克隆出该说话人的声音朗读文案。",
        files=(
            ModelFile(
                name=ZIPVOICE_FP32_PACKAGE,
                urls=_zipvoice_sources(f"{ZIPVOICE_RELEASE}/{ZIPVOICE_FP32_PACKAGE}"),
                size=ZIPVOICE_FP32_BYTES,
            ),
            ModelFile(
                name=ZIPVOICE_VOCODER_FILE,
                urls=_zipvoice_sources(f"{ZIPVOICE_VOCODER_RELEASE}/{ZIPVOICE_VOCODER_FILE}"),
                size=ZIPVOICE_VOCODER_BYTES,
            ),
        ),
        languages="中文 / 英文",
        recommended=True,
        note="未量化版，人声细节更干净；约 455 MB 模型 + 51 MB 声码器。"
        "每个合成实例常驻内存约 0.5GB，12 核开 4 路时合计接近 2GB。",
        requires_package="sherpa-onnx",
        archive=True,
        required_paths=ZIPVOICE_REQUIRED_PATHS,
        required_any=ZIPVOICE_REQUIRED_ANY,
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

# 真正参与语音识别的引擎。模型目录是全类型的：标点恢复只服务「智能分句」、
# 语音合成把文案读成音频，两者都不能用来转写——首页的识别模型选择、设置页的
# 默认识别模型、以及提交任务时的模型校验都按这个集合筛。
RECOGNITION_ENGINES = (ModelEngine.SENSEVOICE, ModelEngine.FASTER_WHISPER)

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
