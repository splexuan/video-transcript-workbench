"""AI 驱动编排：把「用哪个驱动」和「跑哪件任务」解耦。

- 驱动（driver）来自基础设施层的 ai_engines 注册表，也可以是不经过后端的浏览器驱动
  （DeepSeek：前端跳转，见 frontend/src/lib/deepseek.ts）。
- 任务（task）在 AI_TASKS 里登记：一条系统提示 + 一个提示词构造函数。

所以「再加一件能让 AI 干的事」只要往 AI_TASKS 里加一条、前端加一个入口即可，
不用碰引擎；「再加一个本机驱动」只要往 ai_engines 里登记，不用碰任务。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.application.services import read_settings
from app.infrastructure.ai_engines import (
    ENGINES,
    PROVIDERS,
    AiEngineError,
    chat_models,
    engine_status,
)
from app.infrastructure.ai_engines.common import clamp_text, text_messages

# 浏览器驱动：总结在 DeepSeek 网页里完成，后端只负责告诉前端「该跳转了」。
DEEPSEEK = "deepseek"
DEEPSEEK_NAME = "DeepSeek · 网页版"
DEEPSEEK_NOTE = "跳转到 DeepSeek 网页用你自己的账号完成，内容会发送给第三方服务。"
BROWSER_MODE = "browser"

# 对话模型的清单要联网读一次（WorkBuddy），这里做 30 分钟的进程内缓存，
# 登录态或地区一变（catalog_key 变化）立即重新取。
_MODEL_TTL = 1800
_model_cache: dict[str, tuple[float, str, list[dict[str, object]]]] = {}

SUMMARY_SYSTEM = (
    "你是资深内容编辑，擅长把视频口播文案提炼成结构清晰、可直接使用的摘要。"
    "一律使用简体中文，只输出摘要本身，不要寒暄、不要复述原文、不要解释你的做法。"
)


def summary_messages(title: str, text: str) -> list[dict[str, str]]:
    """AI 总结的提示词：固定三段结构，便于用户直接拿去用。"""

    body = clamp_text(text)
    name = (title or "").strip() or "未命名文案"
    ask = (
        f"标题：{name}\n\n"
        "以下是这条视频的口播/字幕文案，请阅读后输出摘要，固定分三部分：\n"
        "【一句话主题】\n"
        "【核心要点】3-5 条短句，每条独占一行并以「- 」开头\n"
        "【可用金句】从文案里挑 1-2 句最值得复用的原句\n\n"
        "文案：\n" + body
    )
    return text_messages(SUMMARY_SYSTEM, [("user", ask)])


# 已登记的任务：前端只传 task 名，提示词与系统提示都留在这里，便于统一调优。
AI_TASKS: dict[str, dict[str, Any]] = {
    "summary": {
        "label": "AI 总结",
        "build": summary_messages,
    },
}


def _cached_chat_models(provider: str) -> list[dict[str, object]]:
    engine = ENGINES[provider]
    try:
        key = str(engine.catalog_key())
    except AiEngineError:
        return []
    cached = _model_cache.get(provider)
    if cached and cached[1] == key and time.time() - cached[0] < _MODEL_TTL:
        return cached[2]
    models = chat_models(provider)
    if models:
        _model_cache[provider] = (time.time(), key, models)
    return models


def _driver_entries() -> list[dict[str, Any]]:
    """三个驱动都列出来：不可用的也带上原因，设置页才能解释「为什么选不了」。"""

    entries: list[dict[str, Any]] = [
        {
            "type": DEEPSEEK,
            "name": DEEPSEEK_NAME,
            "mode": BROWSER_MODE,
            "note": DEEPSEEK_NOTE,
            "available": True,
            "message": "无需配置，点「AI 总结」会打开 DeepSeek 网页",
            "models": [],
            "default_model": "",
        }
    ]
    for provider, meta in PROVIDERS.items():
        state = engine_status(provider)
        models = _cached_chat_models(provider) if state.get("configured") else []
        message = str(state.get("message") or "")
        if state.get("configured") and not models:
            # 登录态读到了但模型清单是空的：多半是网络不通或登录已失效，
            # 这时不能沿用 status 那句「已读取登录态」，否则用户不知道卡在哪。
            message = "已读到本机登录态，但取不到可用模型，请检查网络或客户端的登录是否仍然有效"
        entries.append(
            {
                "type": provider,
                "name": meta["name"],
                "mode": meta["mode"],
                "note": meta["note"],
                "available": bool(state.get("configured") and models),
                "message": message,
                "models": models,
                "default_model": str(models[0]["id"]) if models else "",
            }
        )
    return entries


def _preference(entries: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, str]:
    """上次选的驱动/模型；驱动已不可用、模型已下架时自动退回 DeepSeek。"""

    by_type = {entry["type"]: entry for entry in entries}
    provider = str(settings.get("ai_provider") or "").strip().lower() or DEEPSEEK
    entry = by_type.get(provider)
    if entry is None or not entry["available"]:
        provider, entry = DEEPSEEK, by_type[DEEPSEEK]
    model = str(settings.get("ai_model") or "").strip()
    if provider == DEEPSEEK or not any(m["id"] == model for m in entry["models"]):
        model = str(entry["default_model"])
    return {"provider": provider, "model": model}


def provider_catalog(session: Session) -> dict[str, Any]:
    """设置页与前端分流都读这一份：可用驱动、各自的模型、以及上次的选择。"""

    entries = _driver_entries()
    return {"providers": entries, "preference": _preference(entries, read_settings(session))}


def run_task(
    session: Session,
    task: str,
    *,
    title: str = "",
    text: str = "",
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    """本机驱动执行一件 AI 任务；浏览器驱动（DeepSeek）会明确报错，由前端处理。"""

    spec = AI_TASKS.get(task)
    if spec is None:
        raise AiEngineError("未知的 AI 任务", "invalid_task")
    settings = read_settings(session)
    provider = str(provider or settings.get("ai_provider") or "").strip().lower() or DEEPSEEK
    if provider == DEEPSEEK:
        raise AiEngineError("DeepSeek 由浏览器网页完成，不需要本机引擎", "browser_provider")
    engine = ENGINES.get(provider)
    if engine is None:
        raise AiEngineError("未知的 AI 驱动，请到设置里重新选择", "invalid_provider")
    state = engine_status(provider)
    if not state.get("configured"):
        raise AiEngineError(str(state.get("message") or "该驱动当前不可用"), "auth_missing")

    model = str(model or settings.get("ai_model") or "").strip()
    proxy = str(settings.get("ai_proxy") or "").strip() if provider == "codex" else ""
    messages = spec["build"](title, text)
    result = engine.chat(messages, model=model, proxy=proxy)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return {
        "provider": provider,
        "provider_name": PROVIDERS[provider]["name"],
        "model": str(usage.get("model") or model or ""),
        "text": str(result.get("text") or ""),
        "usage": usage,
    }


__all__ = [
    "AI_TASKS",
    "DEEPSEEK",
    "provider_catalog",
    "run_task",
    "summary_messages",
]
