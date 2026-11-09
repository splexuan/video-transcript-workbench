from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    """用户数据目录。

    注意：下面两个目录名（`VideoTranscriptWorkbench` / `.video-transcript-workbench`）
    是**历史遗留的数据位置，不是品牌名**，长期冻结、不随仓库名或界面名一起改：
    识别模型、加密凭据、文案库都在这里面，改掉就等于给老用户清库。
    """

    override = os.getenv("VTW_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "VideoTranscriptWorkbench"
    return Path.home() / ".video-transcript-workbench"


class Settings(BaseSettings):
    app_name: str = "创作工作室"
    app_version: str = "0.1.5"
    data_dir: Path = default_data_dir()
    worker_enabled: bool = True
    upload_max_mb: int = 4096
    # 模型安装目录。留空时使用 data_dir/models。
    models_dir: Path | None = None
    # 随应用分发的独立技能目录。留空时按仓库 / 打包资源目录自动查找。
    skills_dir: Path | None = None
    download_retries: int = 3
    download_timeout_seconds: float = 60.0
    # 精准识别（faster-whisper）运行参数：只在 CPU 上推理，不依赖 CUDA / cuDNN。
    # 留空用 int8（实测最快的 CPU 精度），可显式指定 ctranslate2 支持的其它精度。
    whisper_compute_type: str = ""
    whisper_beam_size: int = 5
    # VAD 预处理需要 onnxruntime。部分 Windows 环境下 onnxruntime 加载即崩溃，
    # 因此默认关闭；显式打开时会先在子进程里探测可用性，避免拖垮主进程。
    whisper_vad: bool = False
    dev_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    model_config = SettingsConfigDict(env_prefix="VTW_", case_sensitive=False)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{(self.data_dir / 'workbench.db').as_posix()}"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def models_root(self) -> Path:
        return self.models_dir or (self.data_dir / "models")


settings = Settings()
