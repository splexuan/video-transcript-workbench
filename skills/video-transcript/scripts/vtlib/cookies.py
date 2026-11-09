"""读取 Netscape cookies.txt；只取指定域名下的键值对。"""

from __future__ import annotations

from pathlib import Path

COOKIE_DOMAINS = {
    "bilibili": ".bilibili.com",
    "douyin": ".douyin.com",
    "xiaohongshu": ".xiaohongshu.com",
}


def read_netscape_cookies(path: Path | None, domain: str) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}

    suffix = domain.lstrip(".")
    pairs: dict[str, str] = {}
    for line in lines:
        row = line.removeprefix("#HttpOnly_").strip()
        if not row or row.startswith("#"):
            continue
        parts = row.split("\t")
        if len(parts) < 7 or suffix not in parts[0]:
            continue
        pairs.setdefault(parts[5], parts[6])
    return pairs


def cookie_header(path: Path | None, platform: str) -> str | None:
    domain = COOKIE_DOMAINS.get(platform)
    if not domain:
        return None
    pairs = read_netscape_cookies(path, domain)
    if not pairs:
        return None
    return "; ".join(f"{name}={value}" for name, value in pairs.items())
