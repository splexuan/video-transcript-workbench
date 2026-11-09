"""繁简统一：Whisper 的中文输出常为繁体，落库前统一转简体。"""

from __future__ import annotations

import logging
from dataclasses import replace
from functools import lru_cache

from .types import TranscriptChunk

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _converter():
    from opencc import OpenCC

    return OpenCC("t2s")


def to_simplified(text: str) -> str:
    if not text.strip():
        return text
    try:
        return _converter().convert(text)
    except Exception as exc:  # noqa: BLE001 - 字形转换失败不该让转写失败
        logger.warning("繁简转换失败，保留原文：%s", exc)
        return text


def simplify_chunks(chunks: list[TranscriptChunk]) -> list[TranscriptChunk]:
    return [replace(chunk, text=to_simplified(chunk.text)) for chunk in chunks]
