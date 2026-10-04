"""中文标点恢复：给没有标点的平台字幕补标点。

用 sherpa-onnx 的 CT-Transformer 标点模型（和 SenseVoice 共用同一套运行时），
模型由工作台自己下载到数据目录，推理全程离线。加载一次后驻留内存，
模型被删除或校验失败时用 reset_cache() 清掉。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from app.domain import EngineUnavailableError
from app.infrastructure.model_catalog import require_spec
from app.infrastructure.model_store import resolve_directory

logger = logging.getLogger(__name__)

PUNCT_MODEL_ID = "punct-ct-transformer-zh-en"
PUNCT_MODEL_FILENAME = "model.onnx"

# 单次推理的文本长度：标点模型对长文本会明显变慢，切块后拼接结果一致。
CHUNK_CHARS = 480
# 切块时优先在这些断点处切开，避免把英文单词或数字切开
_CHUNK_BREAKS = "。！？；，、,.!?;：: "

_lock = threading.Lock()
_engine: object | None = None
_engine_path: Path | None = None


def reset_cache() -> None:
    """模型被删除或重新校验后，丢掉内存里的实例。"""

    global _engine, _engine_path
    with _lock:
        _engine = None
        _engine_path = None


def model_path() -> Path | None:
    """返回可用的标点模型目录；未安装时为 None。"""

    spec = require_spec(PUNCT_MODEL_ID)
    return resolve_directory(spec)


def is_ready() -> bool:
    return model_path() is not None


def _load(directory: Path):
    global _engine, _engine_path
    with _lock:
        if _engine is not None and _engine_path == directory:
            return _engine
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - 依赖缺失时才触发
            raise EngineUnavailableError(
                "缺少 sherpa-onnx 组件，请先安装后再使用智能分句"
            ) from exc

        config = sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(
                ct_transformer=str(directory / PUNCT_MODEL_FILENAME),
                num_threads=1,
                debug=False,
                provider="cpu",
            )
        )
        engine = sherpa_onnx.OfflinePunctuation(config)
        _engine = engine
        _engine_path = directory
        return engine


def add_punctuation(text: str) -> str:
    """给一整段没有标点的文本补标点；未安装模型时抛 EngineUnavailableError。"""

    directory = model_path()
    if directory is None:
        raise EngineUnavailableError(
            "智能分句需要先安装「中文标点恢复」模型（设置 → 模型管理）"
        )
    if not text.strip():
        return text

    engine = _load(directory)
    return "".join(engine.add_punctuation(chunk) for chunk in split_chunks(text))


def split_chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """把长文本切成等长小块，尽量切在标点或空格处。"""

    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # 从切点往前找最近的断点，最多回退半个块
            floor = start + size // 2
            for index in range(end, floor, -1):
                if text[index - 1] in _CHUNK_BREAKS:
                    end = index
                    break
        chunks.append(text[start:end])
        start = end
    return chunks
