"""本机 AI 引擎的公共契约。

这里只定义「对话」这一件事所需的最小接口：引擎负责拿本机已登录的账号把一段
messages 变成 text。凭证、上游原始响应都不进结果对象，也不落盘。

新增一个引擎 = 写一个同形模块（status / chat / catalog_key / fetch_chat_models），
再到 __init__.py 的 ENGINES 与 PROVIDERS 里登记即可，调用方不用改。
"""
from __future__ import annotations

import re
from typing import Iterable

# 单次任务的正文上限：口播文案再长也就几万字，超出直接挡在本地，避免把整本书发给上游。
MAX_TASK_TEXT = 200_000
# 上游返回文本的上限（8MB 事件流足够装下任何摘要）。
MAX_STREAM_BYTES = 8 * 1024 * 1024
# 模型名的形态约束：与上游同源，只挡明显非法的取值。
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class AiEngineError(Exception):
    """本机引擎的失败；code 给调用方区分「没登录 / 额度不足 / 内容被拦」等。"""

    def __init__(self, message: str, code: str = "engine_error") -> None:
        super().__init__(message)
        self.code = code


def clean_model(model: str) -> str:
    """校验并规范化模型名；空串表示「交给引擎挑默认值」。"""

    model = (model or "").strip()
    if model and not MODEL_PATTERN.fullmatch(model):
        raise AiEngineError("模型名称无效", "invalid_model")
    return model


def clamp_text(text: str, limit: int = MAX_TASK_TEXT) -> str:
    """正文去空白并限长；空正文是调用方的错误，这里不吞掉。"""

    text = (text or "").strip()
    if not text:
        raise AiEngineError("内容为空，无法处理", "empty_text")
    if len(text) > limit:
        raise AiEngineError(f"内容超过 {limit} 字上限，请先精简后再试", "text_too_long")
    return text


def text_messages(system: str, rounds: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """把「系统提示 + 若干轮对话」拼成 OpenAI 兼容的 messages 列表。"""

    messages: list[dict[str, str]] = []
    if system.strip():
        messages.append({"role": "system", "content": system.strip()})
    messages.extend({"role": role, "content": content} for role, content in rounds)
    return messages
