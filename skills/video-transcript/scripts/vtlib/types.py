"""技能内部共用的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class MediaInfo:
    title: str
    platform: str
    duration_seconds: float | None = None
    uploader: str | None = None
    thumbnail: str | None = None
    description: str | None = None


@dataclass(slots=True)
class TranscriptChunk:
    start_ms: int | None
    end_ms: int | None
    text: str


@dataclass(slots=True)
class DirectMedia:
    """解析阶段已经拿到的直链（快手 / 小红书 / 兜底接口）。"""

    info: MediaInfo
    urls: list[str] = field(default_factory=list)
