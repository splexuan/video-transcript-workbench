"""读取本机 WorkBuddy 桌面端的登录态，用它跑文本对话。

只读登录文件、不做 token 刷新、不改客户端任何文件；一次请求绝不自动重试
（一次重试就是一份积分），失败就把可操作的原因交给上层。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import time
import uuid
from functools import lru_cache
from pathlib import Path

import httpx

from .common import (
    MAX_STREAM_BYTES,
    AiEngineError,
    clean_model,
    clamp_text,
)
from .workbuddy_crypto import decode_field

PROVIDER = "workbuddy"
NAME = "WorkBuddy · 本机登录"
NOTE = "复用本机 WorkBuddy 客户端登录态，按账号积分计费，不占用生图额度。"
MODE = "local"

# 对话走这条路由（与生图分开）：/v2/chat/completions
CHAT_ROUTE = "/v2/chat/completions"
CONFIG_ROUTE = "/v3/config"
# 默认模型偏好：按实测挑一批通用对话模型，排在前面的先用；用户可在设置里换。
# 本机账号没开放的名字会自然跳过（实测这份清单里没有 gpt-5 / gemini / claude，
# 于是落到 glm / kimi），所以这里只排"族"的先后，不写死具体版本。
CHAT_MODEL_PREFERENCE = ("gpt-5", "gemini", "glm-5", "glm", "kimi", "claude", "hy3", "hunyuan")
# 名字就不是通用对话模型的条目：代码补全 / 行内补全 / 改写，以及 "auto" 这类占位。
# 本机 /v3/config 的清单里确实混着它们（completion-gf、codewise-*、default-1.1…），
# 但它们只会写代码、不会写摘要，列进「AI 总结」的下拉里只会让用户选错。
NOT_CHAT_IDS = ("auto",)
NOT_CHAT_PREFIXES = ("default-",)
NOT_CHAT_HINTS = ("completion", "codewise", "nes-", "rewrite", "jump")
ENDPOINTS = {
    "": "https://www.workbuddy.cn",
    "www.workbuddy.cn": "https://www.workbuddy.cn",
    "workbuddy.cn": "https://www.workbuddy.cn",
    "www.codebuddy.cn": "https://www.workbuddy.cn",
    "codebuddy.cn": "https://www.workbuddy.cn",
    "copilot.tencent.com": "https://www.workbuddy.cn",
    "www.workbuddy.ai": "https://www.workbuddy.ai",
    "workbuddy.ai": "https://www.workbuddy.ai",
}
TIMEOUT = httpx.Timeout(300.0, connect=15.0)


@lru_cache(maxsize=1)
def installed_client_headers() -> dict[str, str]:
    """只读客户端版本元数据；不执行已安装客户端的任何代码。"""

    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / name
        for name in ("WorkBuddy", "WorkBuddyAI")
    ]
    for root in roots:
        try:
            archive = root / "resources" / "app.asar"
            with archive.open("rb") as handle:
                prefix = handle.read(16)
                _, header_size, _, json_size = struct.unpack("<IIII", prefix)
                if json_size > 32 * 1024 * 1024 or header_size < json_size + 8:
                    continue
                header = json.loads(handle.read(json_size))
                entry = header["files"]["package.json"]
                if entry.get("unpacked") or entry["size"] > 1024 * 1024:
                    continue
                handle.seek(8 + header_size + int(entry["offset"]))
                package = json.loads(handle.read(entry["size"]))
            version = package["version"]
            product = package.get("productName") or "WorkBuddy"
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.A-Za-z0-9]*)", version):
                continue
            if not re.fullmatch(r"[A-Za-z0-9 _-]{1,40}", product):
                continue
            return {
                "User-Agent": f"WorkBuddy/{version} {product}/{version} CreatorStudio/1.0",
                "X-IDE-Type": "WorkBuddy",
                "X-IDE-Name": product,
                "X-IDE-Version": version,
            }
        except (OSError, ValueError, TypeError, KeyError, struct.error):
            continue
    return {"User-Agent": "CreatorStudio-Local/1.0"}


def resolve_region(custom_path: str = "", region: str = "auto") -> str:
    """把 region='auto' 落成真实地区：优先国内版，本机只有国际版登录文件时用国际版。

    换边只发生在「读到的登录文件本身就属于另一个地区」时（region_mismatch），
    不做跨地区凭据混用，也不因为过期/格式不符就换到另一个站。
    """

    if region in ("cn", "global"):
        return region
    if region != "auto":
        raise AiEngineError("WorkBuddy 地区设置无效", "region_invalid")
    try:
        read_credentials(custom_path, "cn")
        return "cn"
    except AiEngineError as exc:
        # 只有「读到了另一个地区的登录文件」才换边；其余（没装/过期/格式不符）
        # 一律保持国内版口径，报错信息才对得上用户的实际情况。
        return "global" if exc.code == "region_mismatch" else "cn"


def credential_paths(custom_path: str = "") -> list[Path]:
    if custom_path:
        return [Path(custom_path).expanduser()]
    roots = [os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")]
    paths: list[Path] = []
    for root in filter(None, roots):
        auth = Path(root) / "CodeBuddyExtension" / "Data" / "Public" / "auth"
        paths.extend(auth / name for name in ("workbuddy-desktop-ai.info", "workbuddy-desktop.info"))
    return paths


def read_credentials(custom_path: str = "", region: str = "cn") -> tuple[str, dict[str, str]]:
    """只取目标地区的有效登录态，绝不跨地区兜底。"""

    if region not in ("cn", "global"):
        raise AiEngineError("WorkBuddy 地区设置无效", "region_invalid")
    errors: list[AiEngineError] = []
    other_region = False
    for path in credential_paths(custom_path):
        if not path.is_file():
            continue
        try:
            endpoint, headers = _read_credential_file(path)
            if endpoint.endswith(".ai") != (region == "global"):
                other_region = True
                continue
            return endpoint, headers
        except AiEngineError as exc:
            errors.append(exc)
    if errors:
        raise errors[0]
    label = "国内版" if region == "cn" else "国际版"
    if other_region:
        raise AiEngineError(
            f"只找到其他地区的 WorkBuddy 登录态，请在 {label}客户端登录后重新检测",
            "region_mismatch",
        )
    raise AiEngineError(f"未找到 WorkBuddy {label}登录态，请先在本机客户端登录", "auth_missing")


def _read_credential_file(path: Path) -> tuple[str, dict[str, str]]:
    try:
        if path.stat().st_size > 512 * 1024:
            raise ValueError("oversized file")
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        auth = document.get("auth") or document
        account = document.get("account") or document
        token = decode_field(auth.get("accessToken")) or ""
        if not isinstance(token, str) or not token or len(token) > 16384 or any(c.isspace() for c in token):
            raise ValueError("invalid token")
        expiry = float(auth.get("expiresAt") or 0)
        if not math.isfinite(expiry) or expiry < 0:
            raise ValueError("invalid expiry")
        if expiry > 1e12:
            expiry /= 1000
        if expiry and expiry <= time.time():
            raise AiEngineError("WorkBuddy 登录态已过期，请在客户端重新登录后再试", "auth_expired")
        domain = decode_field(auth.get("domain") or account.get("domain")) or ""
        if domain not in ENDPOINTS:
            raise ValueError("unknown region")
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Origin": ENDPOINTS[domain],
            "Referer": ENDPOINTS[domain] + "/",
            "X-Product": "SaaS",
            **installed_client_headers(),
        }
        uid = decode_field(account.get("uid"))
        enterprise = decode_field(account.get("enterpriseId"))
        for key, value in (("X-User-Id", uid), ("X-Domain", domain), ("X-Enterprise-Id", enterprise)):
            if value:
                value = str(value)
                if len(value) > 4096 or "\r" in value or "\n" in value:
                    raise ValueError("invalid header")
                headers[key] = value
        if not uid:
            headers["X-No-User-Id"] = "1"
        if not enterprise:
            headers["X-No-Enterprise-Id"] = "1"
        if not domain:
            headers["X-No-Department-Info"] = "1"
        return ENDPOINTS[domain], headers
    except AiEngineError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise AiEngineError(
            "WorkBuddy 登录文件不可读取或格式不兼容，请重新登录或检查文件路径", "auth_invalid"
        ) from None


def status(custom_path: str = "", region: str = "auto") -> dict[str, object]:
    try:
        region = resolve_region(custom_path, region)
        endpoint, _ = read_credentials(custom_path, region)
        region_label = "国际站" if endpoint.endswith(".ai") else "国内站"
        return {
            "configured": True,
            "available": True,
            "message": f"已读取 WorkBuddy {region_label}登录态；是否可用以实际调用为准",
        }
    except AiEngineError as exc:
        return {"configured": False, "available": False, "message": str(exc), "error_code": exc.code}


def catalog_key(auth_path: str = "", region: str = "auto") -> str:
    base, headers = read_credentials(auth_path, resolve_region(auth_path, region))
    return hashlib.sha256((base + headers["Authorization"]).encode()).hexdigest()


def _fetch_config_models(auth_path: str, region: str) -> list[tuple[str, list[str]]]:
    base, headers = read_credentials(auth_path, region)
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", base + CONFIG_ROUTE, headers=headers) as response:
                if response.status_code in (401, 403):
                    response.read()
                    raise AiEngineError("WorkBuddy 登录态已失效，请在客户端重新登录", "auth_rejected")
                chunks, length = [], 0
                for chunk in response.iter_bytes(65536):
                    length += len(chunk)
                    if length > 4 * 1024 * 1024:
                        raise AiEngineError("WorkBuddy 模型配置超过大小限制", "invalid_catalog")
                    chunks.append(chunk)
                if response.status_code != 200:
                    raise AiEngineError("无法读取 WorkBuddy 可用模型，请检查客户端版本、登录与网络", "catalog_unavailable")
                result = json.loads(b"".join(chunks))
                if result.get("code") != 0:
                    raise AiEngineError("无法读取 WorkBuddy 可用模型，请检查客户端版本、登录与网络", "catalog_unavailable")
    except AiEngineError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise AiEngineError("WorkBuddy 模型配置读取失败，请检查网络或客户端版本", "catalog_unavailable") from None
    models: list[tuple[str, list[str]]] = []
    for model in (result.get("data") or {}).get("models", []):
        model_id = model.get("id", "")
        if isinstance(model_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model_id):
            tags = [str(t) for t in (model.get("tags") or []) if isinstance(t, (str, int))]
            models.append((model_id, tags))
    if not models:
        raise AiEngineError("WorkBuddy 当前账号或地区未开放任何模型，请在客户端检查", "model_unavailable")
    return models


def is_general_chat_model(model_id: str) -> bool:
    """是不是一个能用来写摘要的通用对话模型（清单里的补全/占位条目要挡掉）。"""

    low = (model_id or "").lower()
    if low in NOT_CHAT_IDS or low.startswith(NOT_CHAT_PREFIXES):
        return False
    return not any(hint in low for hint in NOT_CHAT_HINTS)


def looks_visual(model_id: str) -> bool:
    """名字像视觉版（glm-4.6v / *-vision）：能看图，但纯文本总结没必要多付这份钱。"""

    low = (model_id or "").lower()
    return bool(re.search(r"\d+v(?![a-z0-9])", low) or "vision" in low or "vl-" in low)


def fetch_chat_models(auth_path: str = "", region: str = "auto") -> list[dict[str, object]]:
    """对话模型清单：排除图像模型与代码补全条目，通用对话模型排在前面。"""

    region = resolve_region(auth_path, region)
    models = [
        (mid, tags)
        for mid, tags in _fetch_config_models(auth_path, region)
        if not [t for t in tags if t in ("text-to-image", "image-to-image")]
        and is_general_chat_model(mid)
    ]
    if not models:
        raise AiEngineError("WorkBuddy 当前账号或地区未开放对话模型，请在客户端检查", "model_unavailable")

    def rank(item: tuple[str, list[str]]) -> tuple[int, int]:
        """先按族的先后，再让纯文本版排在视觉版前面（默认那个模型就是这么挑出来的）。"""

        model_id = item[0].lower()
        offset = 1 if looks_visual(model_id) else 0
        for index, prefix in enumerate(CHAT_MODEL_PREFERENCE):
            if model_id.startswith(prefix):
                return (offset, index)
        return (offset + 1, len(CHAT_MODEL_PREFERENCE))

    models.sort(key=rank)
    return [{"id": mid, "label": mid, "tags": tags} for mid, tags in models]


def default_chat_model(models: list[dict[str, object]]) -> str:
    if not models:
        raise AiEngineError("WorkBuddy 当前账号没有可用的对话模型", "model_unavailable")
    return str(models[0]["id"])


def _error_code_of(error_data: dict) -> str:
    for key in ("code", "errorCode", "error_code"):
        value = error_data.get(key)
        if isinstance(value, (int, str)) and str(value).strip():
            return str(value)
    return ""


def _error_message_of(error_data: dict) -> str:
    for key in ("msg", "message", "error"):
        value = error_data.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    return ""


def _is_content_blocked(code: str, message: str) -> bool:
    if code == "11140":
        return True
    return any(
        word in message
        for word in ("safety", "audit", "policy", "violat", "内容", "审核", "违规")
    )


def _raise_for_status(status_code: int, body: bytes) -> None:
    """把失败响应翻成可操作的中文错误；401/403 不回显上游原文。"""

    try:
        parsed = json.loads(body)
        error_data = parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        error_data = {}
    code = _error_code_of(error_data)
    message = _error_message_of(error_data)
    if status_code == 403 and _is_content_blocked(code, message):
        raise AiEngineError("WorkBuddy 内容策略拦截了本次请求，请调整提示词或稍后再试", "content_blocked")
    if status_code in (401, 403):
        raise AiEngineError("WorkBuddy 登录失效或账号无权限，请重新登录客户端", "auth_rejected")
    if status_code == 429:
        raise AiEngineError("WorkBuddy 请求过于频繁，请稍后再试", "rate_limited")
    if "route config not found" in message or "model not found" in message:
        raise AiEngineError("WorkBuddy 当前账号或地区未开放该模型，请在设置里更换模型", "model_unavailable")
    if any(word in message for word in ("credit", "quota", "积分", "额度", "余额")):
        raise AiEngineError("WorkBuddy 可用积分或额度不足，请在客户端检查额度", "quota_exhausted")
    if "check ua" in message:
        raise AiEngineError("WorkBuddy 客户端版本信息不兼容，请更新客户端并重启本工具", "client_version_invalid")
    raise AiEngineError(f"WorkBuddy 服务返回 HTTP {status_code}，请检查模型与参数后重试", "upstream_error")


def _parse_chat_stream(raw: bytes) -> tuple[str, dict[str, float]]:
    """解析对话接口的 SSE：取 choices[].delta.content。"""

    parts: list[str] = []
    usage: dict[str, float] = {}
    for line in raw.splitlines():
        if not line.startswith(b"data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == b"[DONE]":
            continue
        try:
            event = json.loads(chunk.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("error"), dict) or event.get("code") not in (None, 0):
            code = _error_code_of(event)
            message = _error_message_of(event)
            if _is_content_blocked(code, message):
                raise AiEngineError("WorkBuddy 内容策略拦截了本次请求，请调整提示词或稍后再试", "content_blocked")
            if any(word in message for word in ("credit", "quota", "balance", "额度", "积分", "余额")):
                raise AiEngineError("WorkBuddy 可用积分或额度不足，请在客户端检查额度", "quota_exhausted")
            raise AiEngineError("WorkBuddy 拒绝了本次请求，请检查账号权限、模型或提示词", "generation_rejected")
        choices = event.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta") or choices[0].get("message") or {}
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                parts.append(delta["content"])
        stats = event.get("usage")
        if isinstance(stats, dict):
            usage = {
                key: float(value)
                for key, value in stats.items()
                if key in ("prompt_tokens", "completion_tokens", "total_tokens")
                and isinstance(value, (int, float))
            }
    return "".join(parts), usage


def chat(
    messages: list[dict[str, str]],
    *,
    model: str = "",
    auth_path: str = "",
    region: str = "auto",
    proxy: str = "",
) -> dict[str, object]:
    """一次对话调用：绝不自动重试，失败直接抛 AiEngineError。"""

    if not messages:
        raise AiEngineError("对话内容为空", "empty_text")
    model = clean_model(model)
    region = resolve_region(auth_path, region)
    endpoint, base_headers = read_credentials(auth_path, region)
    if not model:
        model = default_chat_model(fetch_chat_models(auth_path, region))
    payload = {"model": model, "stream": True, "temperature": 0.3, "messages": messages}
    headers = dict(base_headers)
    message_id = uuid.uuid4().hex
    headers.update(
        {
            "X-Conversation-ID": uuid.uuid4().hex,
            "X-Conversation-Request-ID": uuid.uuid4().hex,
            "X-Conversation-Message-ID": message_id,
            "X-Request-ID": message_id,
            "X-Agent-Intent": "craft",
            "X-Agent-Type": "cli",
        }
    )
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
            with client.stream("POST", endpoint + CHAT_ROUTE, headers=headers, json=payload) as response:
                if not 200 <= response.status_code < 300:
                    _raise_for_status(response.status_code, response.read())
                chunks, length = [], 0
                for chunk in response.iter_bytes(65536):
                    length += len(chunk)
                    if length > MAX_STREAM_BYTES:
                        raise AiEngineError("WorkBuddy 返回内容超过大小限制", "response_too_large")
                    chunks.append(chunk)
        raw = b"".join(chunks)
    except AiEngineError:
        raise
    except httpx.TimeoutException:
        raise AiEngineError("WorkBuddy 响应超时，请稍后重试；本工具不会自动重复提交", "timeout") from None
    except httpx.HTTPError:
        raise AiEngineError("无法连接 WorkBuddy，请检查网络后重试", "connection_failed") from None

    text, tokens = _parse_chat_stream(raw)
    text = text.strip()
    if not text:
        raise AiEngineError("WorkBuddy 没有返回文本，请换个模型或稍后重试", "empty_text")
    usage: dict[str, object] = {"provider": PROVIDER, "model": model, "chars": len(text)}
    if tokens:
        usage["tokens"] = tokens
    return {"text": text, "usage": usage}


def run(task_messages: list[dict[str, str]], **kwargs) -> dict[str, object]:
    """统一入口：上层只认 run(messages, ...)，不关心是哪个引擎。"""

    return chat(task_messages, **kwargs)


__all__ = [
    "AiEngineError",
    "MODE",
    "NAME",
    "NOTE",
    "PROVIDER",
    "catalog_key",
    "chat",
    "clamp_text",
    "default_chat_model",
    "fetch_chat_models",
    "resolve_region",
    "run",
    "status",
]
