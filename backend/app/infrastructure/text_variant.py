"""繁简统一：把转写结果里的繁体字转成简体再落库。

Whisper 系列（含 faster-whisper）的中文输出经常是繁体——训练语料里繁体占比不低，
口语、带口音或噪声的内容尤其明显，而 SenseVoice 通常输出简体，于是同一个视频
换模型就会出现两种字形。这里做一次确定性的「繁 → 简」转换：

- 只映射字形，不改动用词、标点或断句；
- 对英文、日文、韩文等非中文文本无影响；
- 转换失败时保留原文，不影响转写结果本身。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from functools import lru_cache

from app.domain import TranscriptChunk

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _converter():
    # 延迟导入：转换器初始化要读词典，放在首次真正用到时
    from opencc import OpenCC

    return OpenCC("t2s")


def to_simplified(text: str) -> str:
    """繁体转简体；已是简体的文本原样返回。"""

    if not text.strip():
        return text
    try:
        return _converter().convert(text)
    # 转换只是字形美化，任何异常都不该让整条转写失败，所以这里有意宽泛兜底
    except Exception as exc:  # noqa: BLE001
        logger.warning("繁简转换失败，保留原文：%s", exc)
        return text


def simplify_chunks(chunks: list[TranscriptChunk]) -> list[TranscriptChunk]:
    """整批转写分段统一转简体（平台字幕同样适用）。"""

    return [replace(chunk, text=to_simplified(chunk.text)) for chunk in chunks]
