"""封面图落盘。

平台给的封面地址通常是带时效签名的临时地址，还可能带防盗链；直接把它存进数据库，
过一阵子就会变成坏图。所以拿到地址后立刻下载到数据目录，之后只记文件名。
封面属于「锦上添花」：下载失败不影响提取，只是详情里没有图。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

COVER_DIR_NAME = "covers"
# 只接受常见图片类型，避免把非图片内容写进数据目录
SUFFIX_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_COVER_BYTES = 5 * 1024 * 1024
DOWNLOAD_TIMEOUT = 20.0
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
}


def cover_dir() -> Path:
    return settings.data_dir / COVER_DIR_NAME


def cover_path(filename: str) -> Path:
    return cover_dir() / filename


def save_cover(document_id: str, url: str | None) -> str | None:
    """下载封面并返回文件名；拿不到就返回 None。"""

    if not url:
        return None

    try:
        with httpx.stream(
            "GET",
            url,
            headers=REQUEST_HEADERS,
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            suffix = SUFFIX_BY_TYPE.get(content_type)
            if suffix is None:
                logger.info("封面类型不在允许范围内，跳过：%s", content_type)
                return None

            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes(1 << 16):
                size += len(chunk)
                if size > MAX_COVER_BYTES:
                    logger.info("封面超过 %s 字节，跳过", MAX_COVER_BYTES)
                    return None
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        logger.info("下载封面失败：%s", exc)
        return None

    cover_dir().mkdir(parents=True, exist_ok=True)
    filename = f"{document_id}{suffix}"
    cover_path(filename).write_bytes(b"".join(chunks))
    return filename


def delete_cover(filename: str | None) -> None:
    if filename:
        cover_path(filename).unlink(missing_ok=True)
