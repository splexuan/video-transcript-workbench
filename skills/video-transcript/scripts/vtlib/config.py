"""配置读取：config.json + 环境变量，不依赖任何宿主程序。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# scripts/vtlib/config.py -> scripts/vtlib -> scripts -> 技能根目录
SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS_DIR = Path.home() / ".video-transcript-skill" / "models"
DEFAULT_FALLBACK_BASE = "https://api-new.ifphp.com"


def _config_file() -> Path:
    override = os.getenv("VTS_CONFIG_FILE")
    return Path(override).expanduser() if override else SKILL_ROOT / "config.json"


def _read_config() -> dict:
    path = _config_file()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _env(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value or None


def _path(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser()


@dataclass(frozen=True)
class Config:
    models_dir: Path
    fallback_api_key: str | None
    fallback_base_url: str
    ffmpeg_dir: Path | None
    cookies: dict[str, Path]
    output_dir: Path | None = None
    request_timeout: float = 25.0
    download_timeout: float = 600.0

    def cookie_for(self, platform: str) -> Path | None:
        candidate = self.cookies.get(platform)
        if candidate is not None and candidate.is_file():
            return candidate
        return None


@lru_cache(maxsize=1)
def get_config() -> Config:
    """读取一次并缓存；模块级调用方无需反复解析配置文件。"""

    data = _read_config()

    models_dir = (
        _path(_env("VTS_MODELS_DIR"))
        or _path(data.get("models_dir"))
        or DEFAULT_MODELS_DIR
    )
    fallback_base = (
        _env("VTS_FALLBACK_BASE")
        or str(data.get("fallback_base_url") or "").strip()
        or DEFAULT_FALLBACK_BASE
    ).rstrip("/")
    fallback_key = _env("VTS_FALLBACK_API_KEY") or str(
        data.get("fallback_api_key") or ""
    ).strip() or None

    ffmpeg_dir = _path(_env("VTS_FFMPEG_DIR")) or _path(data.get("ffmpeg_dir"))
    output_dir = _path(_env("VTS_OUTPUT_DIR")) or _path(data.get("output_dir"))

    cookies: dict[str, Path] = {}
    configured = data.get("cookies")
    if isinstance(configured, dict):
        for platform, value in configured.items():
            path = _path(value)
            if path is not None:
                cookies[str(platform)] = path
    shared_cookie = _path(_env("VTS_COOKIE_FILE"))
    if shared_cookie is not None:
        # 设置成全局 Cookie 文件时，所有需要登录态的平台都用它
        for platform in ("bilibili", "douyin", "xiaohongshu"):
            cookies[platform] = shared_cookie

    return Config(
        models_dir=models_dir,
        fallback_api_key=fallback_key,
        fallback_base_url=fallback_base,
        ffmpeg_dir=ffmpeg_dir,
        cookies=cookies,
        output_dir=output_dir,
    )
