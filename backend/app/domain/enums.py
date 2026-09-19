from enum import StrEnum


class Platform(StrEnum):
    BILIBILI = "bilibili"
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    KUAISHOU = "kuaishou"
    WECHAT = "wechat"
    LOCAL = "local"
    UNKNOWN = "unknown"


class SourceType(StrEnum):
    URL = "url"
    FILE = "file"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStage(StrEnum):
    WAITING = "waiting"
    RESOLVING = "resolving"
    FETCHING_SUBTITLE = "fetching_subtitle"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    WRITING = "writing"
    DONE = "done"

