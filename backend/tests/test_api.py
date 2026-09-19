import json

from fastapi.testclient import TestClient

from app.application.services import read_fallback_api_key, read_settings, update_settings
from app.config import settings
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.models import AppSetting, Document, Job
from app.main import app
from app.schemas import SettingPatch


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_and_list_job() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/jobs",
            json={"source_type": "url", "source": "https://b23.tv/example", "mode": "auto"},
        )
        jobs = client.get("/api/jobs")
    assert created.status_code == 201
    assert created.json()["platform"] == "bilibili"
    assert jobs.status_code == 200
    assert any(item["id"] == created.json()["id"] for item in jobs.json())


def test_job_can_request_a_specific_model() -> None:
    """首页选定的模型要写进任务，mode 按引擎推导，保持旧字段含义一致。"""

    with TestClient(app) as client:
        fast = client.post(
            "/api/jobs",
            json={"source_type": "url", "source": "https://b23.tv/example", "model_id": "sensevoice-small"},
        )
        accurate = client.post(
            "/api/jobs",
            json={"source_type": "url", "source": "https://b23.tv/example", "model_id": "faster-whisper-small"},
        )
    assert fast.status_code == 201
    assert fast.json()["requested_model_id"] == "sensevoice-small"
    assert fast.json()["mode"] == "fast"
    assert accurate.status_code == 201
    assert accurate.json()["mode"] == "accurate"


def test_job_rejects_unknown_model() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            json={"source_type": "url", "source": "https://b23.tv/example", "model_id": "not-a-model"},
        )
    assert response.status_code == 400


def test_job_payload_includes_recognition_model_fields() -> None:
    """任务记录要带上实际用到的识别模型；还没开始识别时为空。"""

    with TestClient(app) as client:
        created = client.post(
            "/api/jobs",
            json={"source_type": "url", "source": "https://b23.tv/example", "mode": "auto"},
        )
    body = created.json()
    assert body["model_id"] is None
    assert body["model_name"] is None
    assert body["transcript_source"] is None


def test_upload_local_file() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs/upload",
            data={"mode": "auto"},
            files={"file": ("recording.wav", b"RIFF-test", "audio/wav")},
        )
    assert response.status_code == 201
    assert response.json()["platform"] == "local"
    assert response.json()["source_type"] == "file"
    assert response.json()["source_value"].endswith("__recording.wav")


def test_file_path_cannot_be_submitted_through_json() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            json={"source_type": "file", "source": "C:/private/file.wav", "mode": "auto"},
        )
    assert response.status_code == 400


def test_model_catalog_only_exposes_user_facing_fields() -> None:
    """对外接口不暴露本机来源路径，状态文案不含内部说法。"""

    with TestClient(app) as client:
        response = client.get("/api/models")
    assert response.status_code == 200
    body = response.json()

    assert set(body["storage"]) == {"models_root", "total_bytes", "disk_free_bytes"}
    assert set(body) == {"storage", "engines", "models", "active_tasks"}

    for model in body["models"]:
        assert "旧项目" not in model["message"]
        assert "\\" not in model["message"]
        assert "/" not in model["message"]

    for engine in body["engines"]:
        assert set(engine) == {
            "engine",
            "package_ready",
            "model_ready",
            "ready",
            "active_model",
            "specs",
        }


def test_document_media_follows_kept_audio(tmp_path, monkeypatch) -> None:
    """原始音视频能否播放，取决于任务目录里还有没有音轨。"""

    data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "data_dir", data_dir)
    init_database()
    with SessionLocal() as session:
        document = Document(
            title="测试文案",
            platform="bilibili",
            source_type="url",
            source_value="https://www.bilibili.com/video/BV1xx411c7mD",
        )
        session.add(document)
        session.flush()
        job = Job(
            document_id=document.id,
            platform="bilibili",
            source_type="url",
            source_value=document.source_value,
        )
        session.add(job)
        session.commit()
        document_id, job_id = document.id, job.id

    with TestClient(app) as client:
        # 默认清理：详情里标记不可用，媒体接口给出可读提示
        before = client.get(f"/api/documents/{document_id}")
        missing = client.get(f"/api/documents/{document_id}/media")

        # 开启「保留原始音视频」后任务目录里会留下转码音轨
        workspace = data_dir / "work" / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "audio.wav").write_bytes(b"RIFF-test")

        after = client.get(f"/api/documents/{document_id}")
        media = client.get(f"/api/documents/{document_id}/media")

        # 即使原始视频还留在目录里，也只播转码后的音轨：文案提取不需要画面
        (workspace / "source.mp4").write_bytes(b"ftyp-video")
        with_video = client.get(f"/api/documents/{document_id}/media")

    assert before.status_code == 200
    assert before.json()["media_available"] is False
    assert missing.status_code == 404
    assert "保留" in missing.json()["detail"]

    assert after.json()["media_available"] is True
    assert media.status_code == 200
    assert media.headers["content-type"] == "audio/wav"
    assert media.content == b"RIFF-test"
    assert with_video.headers["content-type"] == "audio/wav"


def test_document_detail_exposes_source_metadata(tmp_path, monkeypatch) -> None:
    """详情要带上作者、作品介绍与封面标记，前端据此渲染来源信息。"""

    data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "data_dir", data_dir)
    init_database()
    with SessionLocal() as session:
        document = Document(
            title="带元信息的文案",
            platform="kuaishou",
            source_type="url",
            source_value="https://v.kuaishou.com/example",
            uploader="好想吃巧克力",
            description="是谁拥有了一米长的玫瑰花呀 #一束花的仪式感",
        )
        session.add(document)
        session.commit()
        document_id = document.id

    with TestClient(app) as client:
        detail = client.get(f"/api/documents/{document_id}").json()
        missing_cover = client.get(f"/api/documents/{document_id}/cover")

        # 封面落盘并记进文档后，详情标记与图片接口都要跟上
        covers = data_dir / "covers"
        covers.mkdir(parents=True, exist_ok=True)
        (covers / f"{document_id}.jpg").write_bytes(b"\xff\xd8\xff")
        with SessionLocal() as session:
            session.get(Document, document_id).cover_file = f"{document_id}.jpg"  # type: ignore[union-attr]
            session.commit()
        covered = client.get(f"/api/documents/{document_id}").json()
        image = client.get(f"/api/documents/{document_id}/cover")

    assert detail["uploader"] == "好想吃巧克力"
    assert detail["description"] == "是谁拥有了一米长的玫瑰花呀 #一束花的仪式感"
    assert detail["has_cover"] is False
    assert missing_cover.status_code == 404

    assert covered["has_cover"] is True
    assert image.status_code == 200
    assert image.content == b"\xff\xd8\xff"


def test_settings_expose_and_update(tmp_path, monkeypatch) -> None:
    """设置项读写：精度固定按 CPU 处理，界面不再暴露推理设备开关。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        before = client.get("/api/settings").json()
        saved = client.patch("/api/settings", json={"keep_media": True})

    assert before["default_model"] == "sensevoice-small"
    assert "whisper_device" not in before
    assert saved.status_code == 200
    assert saved.json()["keep_media"] is True


def test_fallback_api_key_is_encrypted_and_cleared(tmp_path, monkeypatch) -> None:
    """兜底解析的 Key：加密落库、读取时能取回明文、空串表示清除。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    with SessionLocal() as session:
        update_settings(session, SettingPatch(fallback_api_key="sk-0123456789abcdef"))
        stored = session.get(AppSetting, "fallback_api_key")
        assert stored is not None
        # 落库的不是明文
        assert stored.value != "sk-0123456789abcdef"
        assert read_fallback_api_key(session) == "sk-0123456789abcdef"

        snapshot = read_settings(session)
        assert snapshot["fallback_api_key_set"] is True
        assert "sk-0123456789abcdef" not in json.dumps(snapshot, ensure_ascii=False)

        update_settings(session, SettingPatch(fallback_api_key=""))
        assert session.get(AppSetting, "fallback_api_key") is None
        assert read_settings(session)["fallback_api_key_set"] is False


def test_fallback_api_key_is_not_returned_by_api(tmp_path, monkeypatch) -> None:
    """接口层也只回传状态；不带该字段的设置更新不会误清除已配置的 Key。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        before = client.get("/api/settings").json()
        saved = client.patch("/api/settings", json={"fallback_api_key": "sk-abcdef012345"}).json()
        touched = client.patch("/api/settings", json={"theme": "dark"}).json()
        after = client.get("/api/settings").json()
        cleared = client.patch("/api/settings", json={"fallback_api_key": ""}).json()

    assert before["fallback_api_key_set"] is False
    assert saved["fallback_api_key_set"] is True
    # 明文只写不读
    assert "fallback_api_key" not in saved
    assert touched["fallback_api_key_set"] is True
    assert after["fallback_api_key_set"] is True
    assert cleared["fallback_api_key_set"] is False


def test_unknown_model_returns_404() -> None:
    with TestClient(app) as client:
        response = client.get("/api/models/not-a-model")
    assert response.status_code == 404


def test_cookie_endpoints_drive_connector_status(tmp_path, monkeypatch) -> None:
    """导入 Cookie 后，抖音连接器从「需要配置」变为「可用」。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        before = {item["id"]: item for item in client.get("/api/connectors").json()}
        saved = client.put("/api/credentials/douyin", json={"cookie": "ttwid=abc; msToken=xyz"})
        after = {item["id"]: item for item in client.get("/api/connectors").json()}
        listed = client.get("/api/credentials").json()
        removed = client.delete("/api/credentials/douyin")

    assert before["douyin"]["status"] == "needs_setup"
    assert "Cookie" in before["douyin"]["detail"]

    body = saved.json()
    assert saved.status_code == 200
    assert body["configured"] is True
    assert body["entries"] == 2
    # 接口只回状态，不回 Cookie 内容
    assert set(body) == {"platform", "configured", "entries", "updated_at", "required"}
    # 抖音的凭据是必需的：没有它就无法解析
    assert body["required"] is True

    assert after["douyin"]["status"] == "ready"
    assert [item["platform"] for item in listed] == ["bilibili", "douyin", "xiaohongshu"]
    assert removed.status_code == 200


def test_bilibili_credential_is_optional(tmp_path, monkeypatch) -> None:
    """B站公开视频不登录也能解析：没配凭据时仍是「可用」，配了才提示能力增强。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        before = {item["id"]: item for item in client.get("/api/connectors").json()}
        listed = {item["platform"]: item for item in client.get("/api/credentials").json()}
        saved = client.put(
            "/api/credentials/bilibili",
            json={"cookie": "SESSDATA=abc; bili_jct=def"},
        )
        after = {item["id"]: item for item in client.get("/api/connectors").json()}

    assert listed["bilibili"]["required"] is False
    assert before["bilibili"]["status"] == "ready"
    # 未配置也要让用户知道「配了能多拿到什么」
    assert "会员" in before["bilibili"]["detail"]

    assert saved.status_code == 200
    assert saved.json()["required"] is False
    assert after["bilibili"]["detail"] == "已配置访问凭据，可读取会员与登录可见内容"


def test_kuaishou_connector_requires_no_credential(tmp_path, monkeypatch) -> None:
    """快手读平台分享页：不需要登录态，也不该出现在可配置凭据的平台清单里。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        items = {item["id"]: item for item in client.get("/api/connectors").json()}
        platforms = [item["platform"] for item in client.get("/api/credentials").json()]

    assert "kuaishou" in items
    # 就绪条件与「本地文件」一致：只要 FFmpeg，不依赖 yt-dlp
    assert items["kuaishou"]["status"] == items["local"]["status"]
    assert items["kuaishou"]["status"] in {"ready", "needs_setup"}
    # 没有凭据入口，也不会误报「需要配置」
    assert "kuaishou" not in platforms


def test_wechat_connector_follows_fallback_key(tmp_path, monkeypatch) -> None:
    """视频号只能靠兜底解析接口：没配 Key 时提示去设置页配置，配了之后不再提示。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        before = {item["id"]: item for item in client.get("/api/connectors").json()}["wechat"]
        client.patch("/api/settings", json={"fallback_api_key": "sk-wechat"})
        after = {item["id"]: item for item in client.get("/api/connectors").json()}["wechat"]

    assert before["status"] == "needs_setup"
    assert "API Key" in before["detail"]
    assert "API Key" not in after["detail"]
    # 就绪与否还取决于 FFmpeg，这里只要求不再提示缺 Key
    assert after["status"] in {"ready", "needs_setup"}


def test_cookie_endpoint_rejects_invalid_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        invalid = client.put("/api/credentials/douyin", json={"cookie": "这里没有等号"})
        unsupported = client.put("/api/credentials/wechat", json={"cookie": "a=b"})
    assert invalid.status_code == 400
    assert unsupported.status_code == 400


def test_browser_login_status_before_start(tmp_path, monkeypatch) -> None:
    """没启动过浏览器助手时，状态查询应是 idle，且不会拉起浏览器。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        response = client.get("/api/credentials/douyin/login")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "idle"
    assert body["platform"] == "douyin"
    assert body["entries"] == 0


def test_share_text_keeps_only_the_link() -> None:
    """用户常把整段分享文案贴进来，任务里应存干净链接并识别出平台。"""

    with TestClient(app) as client:
        created = client.post(
            "/api/jobs",
            json={
                "source_type": "url",
                "source": "7.43 复制打开抖音，看看【某人的作品】 https://v.douyin.com/mbD3tCXlUZg/ ！",
            },
        )
    assert created.status_code == 201
    body = created.json()
    assert body["platform"] == "douyin"
    assert body["source_value"] == "https://v.douyin.com/mbD3tCXlUZg/"


def test_xiaohongshu_share_text_keeps_only_the_link() -> None:
    """小红书 App 复制的文案里链接夹在中文之间（且多为 http），同样只保留链接。"""

    with TestClient(app) as client:
        created = client.post(
            "/api/jobs",
            json={
                "source_type": "url",
                "source": (
                    "32 某某 发布了一篇小红书笔记，快来看吧！ 😆 zGfbc4cKZwM73Xw 😆 "
                    "http://xhslink.com/a/JdMVQvX9NSab，复制本条信息，打开【小红书】App查看精彩内容！"
                ),
            },
        )
    assert created.status_code == 201
    body = created.json()
    assert body["platform"] == "xiaohongshu"
    assert body["source_value"] == "http://xhslink.com/a/JdMVQvX9NSab"
