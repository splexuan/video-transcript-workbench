"""本机 AI 引擎注册表。

调用方只通过 ENGINES / PROVIDERS / 这里的几个小函数访问引擎，不直接 import 具体模块，
所以「再加一个本机驱动」只需要写一个同形模块并在下面登记：

    ENGINES = {"workbuddy": workbuddy, "codex": codex, "mycli": mycli}
    PROVIDERS["mycli"] = {"name": ..., "mode": "local", "note": ...}

引擎必须提供的最小接口（见各模块）：
    status() / chat(messages, *, model, auth_path, region, proxy) -> {"text", "usage"}
    fetch_chat_models() / default_chat_model(models) / catalog_key()
"""
from __future__ import annotations

from . import codex, workbuddy
from .common import MAX_TASK_TEXT, AiEngineError

ENGINES = {"workbuddy": workbuddy, "codex": codex}

PROVIDERS: dict[str, dict[str, str]] = {
    "workbuddy": {"name": workbuddy.NAME, "mode": workbuddy.MODE, "note": workbuddy.NOTE},
    "codex": {"name": codex.NAME, "mode": codex.MODE, "note": codex.NOTE},
}


def engine_status(provider: str) -> dict[str, object]:
    """某个本机引擎的登录态（未登记或未登录都返回可展示的说明）。"""

    engine = ENGINES.get(provider)
    if engine is None:
        return {"configured": False, "available": False, "message": "未知的本机 AI 驱动"}
    return engine.status()


def chat_models(provider: str) -> list[dict[str, object]]:
    """某个引擎可用于对话的模型清单；取不到就返回空列表（调用方据 status 说明原因）。"""

    engine = ENGINES.get(provider)
    if engine is None:
        return []
    try:
        return engine.fetch_chat_models()
    except AiEngineError:
        return []


__all__ = [
    "ENGINES",
    "MAX_TASK_TEXT",
    "PROVIDERS",
    "AiEngineError",
    "chat_models",
    "engine_status",
]
