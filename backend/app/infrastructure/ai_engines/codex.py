"""复用本机 Codex CLI 的登录态，跑纯文本对话。

只读 ~/.codex/auth.json；唯一的例外是 access token 过期时用 refresh token
换一次新令牌并原子写回（Codex CLI 自己也是这么做的）。不碰其它客户端文件，
密钥只在进程内存里存活。

Codex 通道是内部、未公开的端点，随时可能因服务端改动而失效，因此只在本机使用、
绝不自动重试（重试就是重复消耗 ChatGPT 套餐额度）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import httpx

from .common import MAX_STREAM_BYTES, MODEL_PATTERN, AiEngineError, clean_model

PROVIDER = "codex"
NAME = "Codex · 本机登录"
NOTE = "复用本机 Codex CLI 登录态，消耗 ChatGPT 套餐额度，不自动重试。"
MODE = "local"

# 只在「读不到本机模型缓存」时兜底（见 fetch_chat_models）；上游换版本时会过时，
# 所以真实清单以 CLI 自己缓存的 models_cache.json 为准。
DEFAULT_MAIN_MODEL = "gpt-6.1-sol"
DEFAULT_AUTH_FILE = Path.home() / ".codex" / "auth.json"
# Codex CLI 启动时会从服务端拉模型目录并写在这个文件里（与本项目无关的只读文件）。
MODELS_CACHE_NAME = "models_cache.json"
MAX_CACHE_BYTES = 8 * 1024 * 1024

# Codex CLI 公开的 client id；不是机密。
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
BASE_URL = "https://chatgpt.com/backend-api/codex"
RESPONSES_URL = BASE_URL + "/responses"

# 后端只服务它认得的官方 Codex CLI。
ORIGINATOR = "codex-tui"
USER_AGENT = "codex-tui/0.118.0 (Mac OS 26.3.1; arm64) Codex Desktop"
MAX_AUTH_BYTES = 512 * 1024
TIMEOUT = httpx.Timeout(600.0, connect=15.0)

# 兜底目录：Codex 没有可直接查询的远端模型接口，本机 CLI 的缓存才是权威来源。
CATALOG = ({"id": DEFAULT_MAIN_MODEL, "label": DEFAULT_MAIN_MODEL},)

_refresh_lock = threading.Lock()


def _auth_file(custom_path: str = "") -> Path:
    return Path(custom_path).expanduser() if custom_path else DEFAULT_AUTH_FILE


def _clean(value, limit: int = 16384) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        return ""
    return "" if any(c.isspace() for c in value) else value


def read_credentials(custom_path: str = "") -> dict[str, object]:
    """只读登录文件：不发请求、不刷新、不写入。"""

    path = _auth_file(custom_path)
    try:
        if not path.is_file():
            raise AiEngineError("未找到 Codex 登录态，请先安装 Codex CLI 并执行 codex login", "auth_missing")
        if path.stat().st_size > MAX_AUTH_BYTES:
            raise ValueError("oversized file")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("invalid document")
        tokens = document.get("tokens") if isinstance(document.get("tokens"), dict) else document
        access = _clean(tokens.get("access_token"))
        if not access:
            raise ValueError("invalid access token")
        return {
            "access_token": access,
            "refresh_token": _clean(tokens.get("refresh_token")),
            "account_id": _clean(tokens.get("account_id"), 64),
            "raw": document,
            "path": path,
        }
    except AiEngineError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise AiEngineError("Codex 登录文件不可读取或格式不兼容，请重新执行 codex login", "auth_invalid") from None


def status(custom_path: str = "") -> dict[str, object]:
    try:
        read_credentials(custom_path)
        return {
            "configured": True,
            "available": True,
            "message": "已读取本机 Codex 登录态；消耗 ChatGPT 套餐额度，不自动重试",
        }
    except AiEngineError as exc:
        return {"configured": False, "available": False, "message": str(exc), "error_code": exc.code}


def models_cache_file(custom_path: str = "") -> Path:
    """模型缓存与本机登录文件同目录，自定义登录路径时跟着走。"""

    return _auth_file(custom_path).parent / MODELS_CACHE_NAME


def catalog_key(custom_path: str = "", region: str = "cn") -> str:
    """登录态或模型缓存一变，清单缓存立刻失效。"""

    material = []
    for path in (_auth_file(custom_path), models_cache_file(custom_path)):
        try:
            stat = path.stat()
            material.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            material.append(str(path))
    return hashlib.sha256("|".join(material).encode("utf-8")).hexdigest()


def _read_models_cache(custom_path: str = "") -> list[dict[str, object]]:
    """读 Codex CLI 缓存的模型目录。

    CLI 启动时会从服务端拉一次并写盘，所以这份清单是「上游此刻开放了哪些模型」的
    权威来源，比在代码里写死模型名可靠得多（实测上游已经从 gpt-5.6 走到 gpt-6 了）。
    只认界面可见（visibility=list）且允许 API 调用的条目，按上游给的 priority 排序。
    """

    path = models_cache_file(custom_path)
    models: list[dict[str, object]] = []
    try:
        if not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
            return []
        document = json.loads(path.read_text(encoding="utf-8"))
        raw = document.get("models") if isinstance(document, dict) else None
        if not isinstance(raw, list):
            return []
        for item in raw:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            if not isinstance(slug, str) or not MODEL_PATTERN.fullmatch(slug):
                continue
            # 隐藏条目（内部/评审模型）与不支持 API 的不进下拉
            if str(item.get("visibility") or "").lower() != "list":
                continue
            if item.get("supported_in_api") is False:
                continue
            priority = item.get("priority")
            models.append(
                {
                    "id": slug,
                    "label": str(item.get("display_name") or slug),
                    "_priority": priority if isinstance(priority, (int, float)) else 999,
                }
            )
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    if not models:
        return []
    models.sort(key=lambda model: (model["_priority"], model["id"]))
    return [{"id": model["id"], "label": model["label"]} for model in models]


def fetch_chat_models(custom_path: str = "", region: str = "cn") -> list[dict[str, object]]:
    """模型清单：优先用 Codex CLI 缓存的最新目录，读不到才退回内置兜底。"""

    read_credentials(custom_path)
    return _read_models_cache(custom_path) or [dict(item) for item in CATALOG]


def default_chat_model(models: list[dict[str, object]]) -> str:
    return str(models[0]["id"]) if models else DEFAULT_MAIN_MODEL


def _headers(auth: dict[str, object], accept: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + str(auth["access_token"]),
        "Accept": accept,
        "Connection": "Keep-Alive",
        "Originator": ORIGINATOR,
        "User-Agent": USER_AGENT,
        "Session_id": uuid.uuid4().hex,
        "X-Client-Request-Id": uuid.uuid4().hex,
    }
    if auth["account_id"]:
        headers["Chatgpt-Account-Id"] = str(auth["account_id"])
    return headers


def _write_credentials(auth: dict[str, object], access: str, refresh: str) -> dict:
    """只改写轮换后的令牌，其余字段原样保留。"""

    document = dict(auth["raw"])
    tokens = dict(document.get("tokens") or {})
    tokens.update({"access_token": access, "refresh_token": refresh})
    document["tokens"] = tokens
    document["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = auth["path"]
    try:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        raise AiEngineError("Codex 新登录态无法写回登录文件，请检查文件权限后重试", "auth_write_failed") from None
    return document


def refresh_credentials(auth: dict[str, object]) -> dict[str, object]:
    """轮换一次令牌；可能已有别的线程先刷新过，所以先重读。"""

    if not auth["refresh_token"]:
        raise AiEngineError(
            "Codex 登录态已失效且没有 refresh_token，请重新执行 codex login", "auth_rejected"
        )
    with _refresh_lock:
        try:
            current = read_credentials(str(auth["path"]))
            if current["access_token"] != auth["access_token"]:
                return current
        except AiEngineError:
            pass
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=False) as client:
                response = client.post(
                    TOKEN_URL,
                    data={
                        "client_id": CLIENT_ID,
                        "grant_type": "refresh_token",
                        "refresh_token": auth["refresh_token"],
                        "scope": "openid profile email",
                    },
                )
        except httpx.HTTPError:
            raise AiEngineError("刷新 Codex 登录态失败，请检查网络或代理后重试", "connection_failed") from None
        if response.status_code != 200:
            # 上游响应体可能带凭证相关字样，不进界面、不进日志。
            raise AiEngineError(
                "Codex 登录态已失效，请重新执行 codex login（避免同时开多个 Codex 客户端）",
                "auth_rejected",
            )
        try:
            payload = response.json()
            access = _clean(payload.get("access_token"))
            refresh = _clean(payload.get("refresh_token")) or str(auth["refresh_token"])
            if not access:
                raise ValueError("missing access token")
        except (ValueError, TypeError, AttributeError):
            raise AiEngineError("Codex 登录态刷新返回异常，请重新执行 codex login", "auth_invalid") from None
        document = _write_credentials(auth, access, refresh)
        return {**auth, "access_token": access, "refresh_token": refresh, "raw": document}


def _explain(status_code: int, raw: bytes) -> AiEngineError:
    """把上游状态码 + 响应体翻成一句可操作的中文错误（401/403 不回显原文）。"""

    text = raw.decode("utf-8", errors="replace")
    lowered = text.lower()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    detail = ""
    if isinstance(payload, dict):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else None
        if error is not None:
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
            if "moderation" in code.lower() or "safety" in message.lower():
                return AiEngineError(
                    "Codex 安全审核拦截了本次请求，请改写内容后重试（这类拦截由上游强制）",
                    "content_blocked",
                )
            detail = code or message
        elif isinstance(payload.get("detail"), str):
            detail = payload["detail"]
    if status_code == 401:
        return AiEngineError("Codex 登录态已失效，请重新登录后再试", "auth_rejected")
    if status_code == 403:
        return AiEngineError("Codex 拒绝了本次请求，账号可能无权使用该通道", "forbidden")
    if status_code == 429 or "usage_limit" in lowered or "rate limit" in lowered:
        return AiEngineError("ChatGPT 套餐额度不足或请求过快，请检查账号用量后再试", "quota_exhausted")
    if detail:
        return AiEngineError(
            f"Codex 服务返回 HTTP {status_code}：{' '.join(str(detail).split())[:200]}", "upstream_error"
        )
    return AiEngineError(f"Codex 服务返回 HTTP {status_code}，请检查模型与参数后重试", "upstream_error")


def _post(url: str, auth: dict[str, object], payload: dict, proxy: str = "") -> bytes:
    """一次尝试：不跟随重定向、不自动重发（POST 会消耗额度）。"""

    headers = _headers(auth, "text/event-stream")
    kwargs: dict[str, object] = {}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, **kwargs) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                chunks, length = [], 0
                for chunk in response.iter_bytes(65536):
                    length += len(chunk)
                    if length > MAX_STREAM_BYTES:
                        raise AiEngineError("Codex 返回内容超过大小限制", "response_too_large")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if 200 <= response.status_code < 300:
                    return raw
                raise _explain(response.status_code, raw[:8192])
    except AiEngineError:
        raise
    except httpx.TimeoutException:
        raise AiEngineError("Codex 响应超时，请稍后在 ChatGPT 侧确认；本工具不会自动重复提交", "timeout") from None
    except httpx.HTTPError:
        raise AiEngineError("无法连接 Codex 服务，请检查网络或代理设置后重试", "connection_failed") from None


def _post_with_refresh(url: str, auth: dict[str, object], payload: dict, proxy: str = "") -> bytes:
    """令牌被拒时只轮换一次，然后原样重放一次。"""

    try:
        return _post(url, auth, payload, proxy)
    except AiEngineError as exc:
        if exc.code != "auth_rejected" or not auth["refresh_token"]:
            raise
    return _post(url, refresh_credentials(auth), payload, proxy)


def _parse_sse_text(raw: bytes) -> tuple[str, dict[str, float]]:
    """收集纯文本事件流的输出（不带 image_generation 工具时上游回的是文本）。"""

    deltas: list[str] = []
    items: list[str] = []
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
        kind = event.get("type")
        if kind in ("error", "response.failed", "response.incomplete"):
            raise AiEngineError("Codex 拒绝或中断了本次请求，请检查内容与账号额度后重试", "generation_rejected")
        if kind == "response.output_text.delta":
            if isinstance(event.get("delta"), str):
                deltas.append(event["delta"])
        elif kind == "response.completed":
            response = event.get("response") or {}
            stats = response.get("usage") if isinstance(response, dict) else None
            if isinstance(stats, dict):
                usage = {
                    key: float(value)
                    for key, value in stats.items()
                    if key in ("input_tokens", "output_tokens", "total_tokens")
                    and isinstance(value, (int, float))
                }
            for item in response.get("output") or []:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for part in item.get("content") or []:
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        items.append(part["text"])
    return "".join(deltas) if deltas else "".join(items), usage


def chat(
    messages: list[dict[str, str]],
    *,
    model: str = "",
    auth_path: str = "",
    region: str = "cn",
    proxy: str = "",
) -> dict[str, object]:
    """纯文本对话：不生成图片、不占生图额度，只算主模型的 token。"""

    if not messages:
        raise AiEngineError("对话内容为空", "empty_text")
    model = clean_model(model) or DEFAULT_MAIN_MODEL
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system").strip()
    rounds = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not rounds:
        raise AiEngineError("对话内容为空", "empty_text")
    payload = {
        "instructions": system,
        "stream": True,
        "reasoning": {"effort": "low", "summary": "auto"},
        "store": False,
        "model": model,
        "input": [
            {
                "type": "message",
                "role": message["role"],
                "content": [{"type": "input_text", "text": message["content"]}],
            }
            for message in rounds
        ],
    }
    raw = _post_with_refresh(RESPONSES_URL, read_credentials(auth_path), payload, proxy)
    text, tokens = _parse_sse_text(raw)
    text = text.strip()
    if not text:
        raise AiEngineError("Codex 没有返回文本，请稍后重试", "empty_text")
    usage: dict[str, object] = {"provider": PROVIDER, "model": model, "chars": len(text)}
    if tokens:
        usage["tokens"] = tokens
    return {"text": text, "usage": usage}


def run(task_messages: list[dict[str, str]], **kwargs) -> dict[str, object]:
    """统一入口：上层只认 run(messages, ...)。"""

    return chat(task_messages, **kwargs)


__all__ = [
    "AiEngineError",
    "CATALOG",
    "DEFAULT_MAIN_MODEL",
    "MODE",
    "NAME",
    "NOTE",
    "PROVIDER",
    "catalog_key",
    "chat",
    "default_chat_model",
    "fetch_chat_models",
    "run",
    "status",
]
