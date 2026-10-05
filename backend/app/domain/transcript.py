from dataclasses import dataclass


@dataclass(slots=True)
class MediaInfo:
    title: str
    platform: str
    duration_seconds: float | None = None
    uploader: str | None = None
    # 封面图地址（远程临时地址，落库前会另存到本机）
    thumbnail: str | None = None
    # 作品介绍：与标题不同的完整描述，没有就为空
    description: str | None = None


@dataclass(slots=True)
class TranscriptChunk:
    start_ms: int | None
    end_ms: int | None
    text: str

