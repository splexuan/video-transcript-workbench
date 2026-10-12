"""AI 驱动：设置项、驱动清单与总结编排。

本机引擎的调用一律打桩（不读真实登录态、不联网），只验证编排与契约。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.application import ai_assist
from app.application.ai_assist import AI_TASKS, provider_catalog, run_task
from app.application.services import read_settings, update_settings
from app.config import settings
from app.infrastructure.ai_engines import AiEngineError
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import AppSetting
from app.main import app
from app.schemas import SettingPatch


def _unavailable(_provider: str) -> dict[str, object]:
    return {"configured": False, "available": False, "message": "未检测到本机登录态"}


def test_ai_settings_roundtrip_and_unknown_provider_falls_back(tmp_path, monkeypatch) -> None:
    """驱动与模型要能落库；库里的白名单外取值一律退回 DeepSeek。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    with SessionLocal() as session:
        saved = update_settings(
            session,
            SettingPatch(ai_provider="codex", ai_model="gpt-5.6-luna", ai_proxy="http://127.0.0.1:7890"),
        )
        assert saved["ai_provider"] == "codex"
        assert saved["ai_model"] == "gpt-5.6-luna"
        assert saved["ai_proxy"] == "http://127.0.0.1:7890"

        # 直接写入一个非法值：读出来必须是默认的 deepseek，不能让前端拿到选不中的项
        session.merge(AppSetting(key="ai_provider", value="not-a-provider"))
        session.commit()
        assert read_settings(session)["ai_provider"] == "deepseek"


def test_provider_catalog_lists_every_driver_with_reason(monkeypatch) -> None:
    """不可用的本机驱动也要出现，并带上「为什么不可用」，设置页才能解释。"""

    monkeypatch.setattr(ai_assist, "engine_status", _unavailable)
    monkeypatch.setattr(ai_assist, "_cached_chat_models", lambda provider: [])
    init_database()
    with SessionLocal() as session:
        catalog = provider_catalog(session)
    by_type = {entry["type"]: entry for entry in catalog["providers"]}
    assert set(by_type) == {"deepseek", "workbuddy", "codex"}
    assert by_type["deepseek"]["available"] is True
    assert by_type["deepseek"]["mode"] == "browser"
    assert by_type["codex"]["available"] is False
    assert by_type["codex"]["message"] == "未检测到本机登录态"
    # 默认驱动是 DeepSeek，且浏览器驱动不带模型清单
    assert catalog["preference"] == {"provider": "deepseek", "model": ""}


def test_provider_catalog_keeps_the_saved_local_driver(monkeypatch) -> None:
    """已登录的本机驱动要能被选上，并回填它自己的默认模型。"""

    monkeypatch.setattr(
        ai_assist,
        "engine_status",
        lambda provider: {
            "configured": provider == "codex",
            "available": provider == "codex",
            "message": "已读取 Codex 登录态",
        },
    )
    monkeypatch.setattr(
        ai_assist,
        "_cached_chat_models",
        lambda provider: [{"id": "gpt-5.6-luna", "label": "gpt-5.6-luna"}] if provider == "codex" else [],
    )
    init_database()
    with SessionLocal() as session:
        update_settings(session, SettingPatch(ai_provider="codex"))
        catalog = provider_catalog(session)
    assert catalog["preference"] == {"provider": "codex", "model": "gpt-5.6-luna"}


def test_provider_catalog_explains_an_empty_model_list(monkeypatch) -> None:
    """拿到了登录态却取不到模型：不能沿用「已读取登录态」那句话，得说清卡在哪一步。"""

    monkeypatch.setattr(
        ai_assist,
        "engine_status",
        lambda provider: {
            "configured": True,
            "available": True,
            "message": "已读取 WorkBuddy 国内站登录态；是否可用以实际调用为准",
        },
    )
    monkeypatch.setattr(ai_assist, "_cached_chat_models", lambda provider: [])
    init_database()
    with SessionLocal() as session:
        catalog = provider_catalog(session)
    by_type = {entry["type"]: entry for entry in catalog["providers"]}
    assert by_type["workbuddy"]["available"] is False
    assert "取不到可用模型" in by_type["workbuddy"]["message"]


def test_summary_prompt_carries_title_and_body() -> None:
    """任务提示词要带上标题与正文，并固定成三段式摘要结构。"""

    messages = AI_TASKS["summary"]["build"]("三分钟看懂摆摊", "今天讲讲摆摊的三个坑")
    assert messages[0]["role"] == "system"
    assert "三分钟看懂摆摊" in messages[-1]["content"]
    assert "今天讲讲摆摊的三个坑" in messages[-1]["content"]
    assert "【核心要点】" in messages[-1]["content"]


def test_run_task_rejects_the_browser_driver(tmp_path, monkeypatch) -> None:
    """DeepSeek 在前端跳网页完成，后端不能被当成引擎调用。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    with SessionLocal() as session, pytest.raises(AiEngineError) as info:
        run_task(session, "summary", title="t", text="正文", provider="deepseek")
    assert info.value.code == "browser_provider"


def test_run_task_dispatches_to_the_local_engine(tmp_path, monkeypatch) -> None:
    """编排层只认注册表：换了引擎，调用方一行都不用改。"""

    calls: list[dict] = []

    class FakeEngine:
        @staticmethod
        def chat(messages, *, model="", proxy=""):
            calls.append({"messages": messages, "model": model, "proxy": proxy})
            return {"text": "摘要正文", "usage": {"model": model or "fake-default", "chars": 4}}

    monkeypatch.setitem(ai_assist.ENGINES, "workbuddy", FakeEngine)
    monkeypatch.setattr(
        ai_assist,
        "engine_status",
        lambda provider: {"configured": True, "available": True, "message": "ok"},
    )
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    with SessionLocal() as session:
        update_settings(session, SettingPatch(ai_provider="workbuddy", ai_model="glm-4"))
        result = run_task(session, "summary", title="标题", text="正文")
    assert result["provider"] == "workbuddy"
    assert result["model"] == "glm-4"
    assert result["text"] == "摘要正文"
    assert calls and calls[0]["messages"][-1]["role"] == "user"


def test_summarize_endpoint_reports_an_unavailable_driver(monkeypatch) -> None:
    monkeypatch.setattr(ai_assist, "engine_status", _unavailable)
    with TestClient(app) as client:
        response = client.post(
            "/api/ai/summarize",
            json={"title": "标题", "text": "正文", "provider": "workbuddy"},
        )
    assert response.status_code == 400
    assert "未检测到本机登录态" in response.json()["detail"]


def test_providers_endpoint_lists_deepseek_first(monkeypatch) -> None:
    """驱动清单接口：DeepSeek 永远排第一，不可用的本机驱动也带着原因返回。"""

    monkeypatch.setattr(ai_assist, "engine_status", _unavailable)
    monkeypatch.setattr(ai_assist, "_cached_chat_models", lambda provider: [])
    with TestClient(app) as client:
        response = client.get("/api/ai/providers")
    assert response.status_code == 200
    body = response.json()
    assert body["providers"][0]["type"] == "deepseek"
    assert body["providers"][0]["mode"] == "browser"
    # 保存过的驱动若不可用，前端的下拉要回到这个 preference
    assert body["preference"]["provider"] == "deepseek"


def test_workbuddy_region_auto_falls_back_to_the_installed_edition(monkeypatch) -> None:
    """region=auto：优先国内版登录态，本机只有国际版账号时才用国际版。"""

    from app.infrastructure.ai_engines import workbuddy

    def fake_credentials(_path: str = "", region: str = "cn"):
        if region == "cn":
            raise AiEngineError("只找到国际版登录态", "region_mismatch")
        return "https://www.workbuddy.ai", {}

    monkeypatch.setattr(workbuddy, "read_credentials", fake_credentials)
    assert workbuddy.resolve_region("", "auto") == "global"
    # 显式指定地区时不改口径
    assert workbuddy.resolve_region("", "cn") == "cn"
    with pytest.raises(AiEngineError):
        workbuddy.resolve_region("", "mars")


def test_codex_models_come_from_the_cli_cache(tmp_path, monkeypatch) -> None:
    """Codex 的清单要读 CLI 缓存的目录：上游已经到 gpt-6.x，写死模型名必然过时。"""

    from app.infrastructure.ai_engines import codex

    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-9-big", "display_name": "GPT-9-Big", "visibility": "list",
                     "supported_in_api": True, "priority": 2},
                    {"slug": "gpt-9-top", "display_name": "GPT-9-Top", "visibility": "list",
                     "supported_in_api": True, "priority": 1},
                    {"slug": "gpt-internal", "display_name": "内部模型", "visibility": "hide",
                     "supported_in_api": True, "priority": 0},
                    {"slug": "gpt-no-api", "display_name": "不支持 API", "visibility": "list",
                     "supported_in_api": False, "priority": 0},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(codex, "read_credentials", lambda *args, **kwargs: {})
    monkeypatch.setattr(codex, "models_cache_file", lambda *args, **kwargs: cache)

    models = codex.fetch_chat_models()
    # 只留界面可见且支持 API 的条目，并按上游 priority 排序
    assert [model["id"] for model in models] == ["gpt-9-top", "gpt-9-big"]
    assert models[0]["label"] == "GPT-9-Top"
    assert codex.default_chat_model(models) == "gpt-9-top"


def test_codex_models_fall_back_when_the_cache_is_missing(tmp_path, monkeypatch) -> None:
    """读不到缓存（没装 CLI / 格式变了）时退回内置兜底，不能因此就报错。"""

    from app.infrastructure.ai_engines import codex

    monkeypatch.setattr(codex, "read_credentials", lambda *args, **kwargs: {})
    monkeypatch.setattr(codex, "models_cache_file", lambda *args, **kwargs: tmp_path / "nope.json")
    assert codex.fetch_chat_models() == [dict(item) for item in codex.CATALOG]


def test_workbuddy_chat_models_skip_code_completion_entries(monkeypatch) -> None:
    """清单里的代码补全/占位条目不进「AI 总结」的下拉，视觉版也让位给纯文本版。"""

    from app.infrastructure.ai_engines import workbuddy

    monkeypatch.setattr(workbuddy, "resolve_region", lambda *args, **kwargs: "cn")
    monkeypatch.setattr(
        workbuddy,
        "_fetch_config_models",
        lambda *args, **kwargs: [
            ("glm-5v-turbo", []),
            ("glm-5.1", []),
            ("completion-gf", []),
            ("codewise-jump", []),
            ("default-1.1", []),
            ("auto", []),
            ("hunyuan-image-v3.0-art", ["text-to-image"]),
        ],
    )
    assert [model["id"] for model in workbuddy.fetch_chat_models()] == ["glm-5.1", "glm-5v-turbo"]


def _sse(events: list[dict]) -> bytes:
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"


def test_local_engines_parse_their_event_streams() -> None:
    """两个本机引擎的 SSE 解析：增量文本要拼起来，用量只取白名单字段。"""

    from app.infrastructure.ai_engines import codex, workbuddy

    chat_text, chat_usage = workbuddy._parse_chat_stream(
        _sse(
            [
                {"choices": [{"delta": {"content": "摘要"}}]},
                {"choices": [{"delta": {"content": "正文"}}]},
                {"usage": {"total_tokens": 12, "noise": "x"}},
            ]
        )
    )
    assert chat_text == "摘要正文"
    assert chat_usage == {"total_tokens": 12.0}

    reply_text, reply_usage = codex._parse_sse_text(
        _sse(
            [
                {"type": "response.output_text.delta", "delta": "摘要"},
                {"type": "response.output_text.delta", "delta": "正文"},
                {"type": "response.completed", "response": {"usage": {"total_tokens": 7}, "output": []}},
            ]
        )
    )
    assert reply_text == "摘要正文"
    assert reply_usage == {"total_tokens": 7.0}
