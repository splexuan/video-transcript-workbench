"""平台凭据存储：把用户导入的 Cookie 加密保存在本机，供下载流程临时读取。

设计约束：
- Cookie 只在访问平台时短暂落地为临时文件，任务结束立即删除；
- 存储使用 Windows DPAPI 加密（只有同一用户能解密），其它平台退化为私有权限文件；
- 对外只暴露「是否已配置 / 条目数 / 更新时间」，任何接口都不回传 Cookie 内容。

`protect_secret` / `reveal_secret` 是同一套 DPAPI 的通用入口，兜底解析接口的
API Key 等小段密钥也走这里，避免各处自己写一份加解密。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.config import settings

logger = logging.getLogger(__name__)

# 允许导入 Cookie 的平台，以及它在 Netscape 格式里归属的站点域名。
COOKIE_DOMAINS: dict[str, str] = {
    "bilibili": ".bilibili.com",
    "douyin": ".douyin.com",
    "xiaohongshu": ".xiaohongshu.com",
}
# 没有 Cookie 就无法解析的平台。B站、小红书的公开视频游客都能看，
# 凭据属于「可选」：配了才能读登录可见内容和更完整的数据。
REQUIRED_COOKIE_PLATFORMS = frozenset({"douyin"})

MAX_COOKIE_BYTES = 256 * 1024
PLAIN_PREFIX = b"vtw-plain:"
# 从请求头复制的 Cookie 没有过期时间，统一按一年有效期写入。
COOKIE_TTL_SECONDS = 365 * 24 * 3600
# 禁止解密时弹出系统界面，避免后台任务被阻塞。
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class CredentialError(RuntimeError):
    """Cookie 内容或本机凭据存储不可用。"""


@dataclass(frozen=True)
class CredentialStatus:
    platform: str
    configured: bool
    entries: int
    updated_at: float | None
    # False 表示凭据是可选的：没配置也能用，配置后能力更强。
    required: bool = True


def require_platform(platform: str) -> str:
    if platform not in COOKIE_DOMAINS:
        raise CredentialError(f"不支持为「{platform}」配置 Cookie")
    return platform


def normalize_cookie_text(raw: str, domain: str, now: float | None = None) -> str:
    """把用户粘贴的内容整理成 Netscape cookies.txt 文本。

    接受两种常见形式：
    1. 浏览器开发者工具里复制的请求头：`ttwid=xxx; msToken=yyy`；
    2. 插件导出的 Netscape cookies.txt（制表符分隔的七列文本）。
    两者都按七列格式重新输出，交给下载器时只认这一种格式。
    """

    text = raw.strip()
    if not text:
        raise CredentialError("Cookie 内容为空")
    if len(text.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise CredentialError("Cookie 内容过长，请确认粘贴的是 Cookie 而不是整段网页代码")
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()

    exported = [line for line in text.splitlines() if line.count("\t") >= 6]
    if exported:
        return "# Netscape HTTP Cookie File\n" + "\n".join(line.rstrip() for line in exported) + "\n"

    expiry = int((now if now is not None else time.time()) + COOKIE_TTL_SECONDS)
    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
    rows: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[;\n]+", text):
        pair = chunk.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append(f"{domain}\t{include_subdomains}\t/\tTRUE\t{expiry}\t{name}\t{value.strip()}")
    if not rows:
        raise CredentialError("没有解析出有效的 Cookie，请确认内容包含 name=value 形式的字段")
    return "# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n"


def count_entries(text: str) -> int:
    """统计有效 Cookie 行数；`#HttpOnly_` 前缀也是有效数据行。"""

    total = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_") or not line.startswith("#"):
            total += 1
    return total


class _Blob(ctypes.Structure):
    _fields_ = (("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p))


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 必须声明指针签名：否则句柄会被当成 32 位整数传递并在 64 位下溢出。
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data, len(data))
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    result = _Blob()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result),
        )
    if not ok:
        raise CredentialError("本机凭据解密失败，请重新导入 Cookie")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.c_void_p(result.pbData))


def _protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        return PLAIN_PREFIX + data
    return _dpapi(data, protect=True)


def _unprotect(data: bytes) -> bytes:
    if data.startswith(PLAIN_PREFIX):
        return data[len(PLAIN_PREFIX):]
    if sys.platform != "win32":
        raise CredentialError("当前系统无法解密本机凭据，请重新导入 Cookie")
    return _dpapi(data, protect=False)


def protect_secret(text: str) -> str:
    """把一段明文密钥加密成可入库的字符串（Windows 用 DPAPI，其它平台退化为明文标记）。"""

    return base64.b64encode(_protect(text.encode("utf-8"))).decode("ascii")


def reveal_secret(token: str) -> str:
    """还原 `protect_secret` 的结果；内容损坏或换了机器时抛 `CredentialError`。"""

    return _unprotect(base64.b64decode(token.encode("ascii"))).decode("utf-8")


def _root() -> Path:
    root = settings.data_dir / "credentials"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cookie_path(platform: str) -> Path:
    return _root() / f"{platform}.cookie"


def _meta_path(platform: str) -> Path:
    return _root() / f"{platform}.json"


def _harden(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning("无法收紧凭据文件权限：%s", path)


def _store(platform: str, text: str, now: float | None = None) -> CredentialStatus:
    path = _cookie_path(platform)
    path.write_bytes(_protect(text.encode("utf-8")))
    _harden(path)
    stamp = now if now is not None else time.time()
    _meta_path(platform).write_text(
        json.dumps({"entries": count_entries(text), "updated_at": stamp}),
        encoding="utf-8",
    )
    return status(platform)


def save_cookie(platform: str, raw: str, now: float | None = None) -> CredentialStatus:
    require_platform(platform)
    text = normalize_cookie_text(raw, COOKIE_DOMAINS[platform], now=now)
    return _store(platform, text, now)


def netscape_from_records(records: list[dict[str, object]], domain: str) -> str:
    """把浏览器给出的 Cookie 记录整理成 Netscape cookies.txt 文本。

    只保留属于该平台的域名，避免把整机其它站点的 Cookie 一起存进来。
    会话级 Cookie（expires 为 -1/0）按 Netscape 约定写 0，HttpOnly 用 `#HttpOnly_` 前缀。
    """

    suffix = domain.lstrip(".")
    rows: list[str] = ["# Netscape HTTP Cookie File"]
    for record in records:
        record_domain = str(record.get("domain") or "")
        name = str(record.get("name") or "")
        if not record_domain or not name or suffix not in record_domain:
            continue
        include_subdomains = "TRUE" if record_domain.startswith(".") else "FALSE"
        path = str(record.get("path") or "/")
        secure = "TRUE" if record.get("secure") else "FALSE"
        try:
            expires = int(float(record.get("expires") or 0))
        except (TypeError, ValueError):
            expires = 0
        prefix = "#HttpOnly_" if record.get("httpOnly") else ""
        rows.append(
            f"{prefix}{record_domain}\t{include_subdomains}\t{path}\t{secure}\t"
            f"{max(expires, 0)}\t{name}\t{record.get('value')}"
        )
    return "\n".join(rows) + "\n"


def save_cookie_records(
    platform: str,
    records: list[dict[str, object]],
    now: float | None = None,
) -> CredentialStatus:
    """保存浏览器扫码拿到的 Cookie；只在确有属于该平台的条目时写入。"""

    require_platform(platform)
    text = netscape_from_records(records, COOKIE_DOMAINS[platform])
    if count_entries(text) == 0:
        raise CredentialError("没有从浏览器读取到属于该平台的 Cookie")
    return _store(platform, text, now)


def delete_cookie(platform: str) -> bool:
    require_platform(platform)
    removed = False
    for path in (_cookie_path(platform), _meta_path(platform)):
        if path.is_file():
            path.unlink(missing_ok=True)
            removed = True
    return removed


def status(platform: str) -> CredentialStatus:
    require_platform(platform)
    cookie_file = _cookie_path(platform)
    if not cookie_file.is_file():
        return CredentialStatus(
            platform=platform,
            configured=False,
            entries=0,
            updated_at=None,
            required=platform in REQUIRED_COOKIE_PLATFORMS,
        )

    entries = 0
    updated_at: float | None = None
    meta_file = _meta_path(platform)
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            entries = int(meta.get("entries") or 0)
            updated_at = float(meta["updated_at"]) if meta.get("updated_at") else None
        except (ValueError, KeyError, OSError):
            entries, updated_at = 0, None
    if updated_at is None:
        updated_at = cookie_file.stat().st_mtime
    return CredentialStatus(
        platform=platform,
        configured=True,
        entries=entries,
        updated_at=updated_at,
        required=platform in REQUIRED_COOKIE_PLATFORMS,
    )


def all_status() -> list[CredentialStatus]:
    return [status(platform) for platform in COOKIE_DOMAINS]


def read_netscape_cookies(path: Path | None, domain: str) -> dict[str, str]:
    """从 cookies.txt 里挑出属于该域名的键值对，供自建 HTTP 请求拼 Cookie 头。

    下载器自己会读 cookies.txt；只有我们自己发出的请求（例如 B站字幕接口）才需要它，
    这样登录态能同时作用于外链解析和平台接口。
    """

    if path is None:
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.warning("读取 Cookie 临时文件失败：%s", path)
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
        # 同名 Cookie 只保留第一条，与 normalize_cookie_text 的处理保持一致。
        pairs.setdefault(parts[5], parts[6])
    return pairs


def materialize_cookie_file(platform: str) -> Path | None:
    """把 Cookie 解密到临时文件供下载器读取；调用方必须在用完后删除。"""

    if platform not in COOKIE_DOMAINS:
        return None
    cookie_file = _cookie_path(platform)
    if not cookie_file.is_file():
        return None
    try:
        text = _unprotect(cookie_file.read_bytes()).decode("utf-8")
    except (CredentialError, UnicodeDecodeError) as exc:
        raise CredentialError("本机保存的 Cookie 无法解密，请在平台连接页重新导入") from exc

    workspace = settings.work_dir / "credentials"
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / f"{platform}-{uuid4().hex[:8]}.txt"
    target.write_text(text, encoding="utf-8", newline="\n")
    _harden(target)
    return target
