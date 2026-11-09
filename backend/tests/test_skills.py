import io
import zipfile

from fastapi.testclient import TestClient

from app.main import app


def _skill(client: TestClient) -> dict:
    response = client.get("/api/skills")
    assert response.status_code == 200
    items = response.json()["items"]
    return next(item for item in items if item["id"] == "video-transcript")


def test_list_skills_only_exposes_source() -> None:
    with TestClient(app) as client:
        skill = _skill(client)

    assert skill["title"] == "视频文案提取"
    assert skill["description"].startswith("从视频链接提取口播文案")
    paths = {item["path"] for item in skill["files"]}
    assert skill["files"][0]["path"] == "SKILL.md"
    assert "SKILL.md" in paths
    assert "config.example.json" in paths
    assert "scripts/extract.py" in paths
    assert "scripts/vtlib/cli.py" in paths
    # 真实配置与运行产物绝不会出现在列表里。
    assert "config.json" not in paths
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in paths)
    assert skill["total_bytes"] > 0
    assert skill["download_url"] == "/api/skills/video-transcript/download"


def test_preview_skill_file() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/skills/video-transcript/file",
            params={"path": "scripts/extract.py"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "scripts/extract.py"
    assert "def main" in body["content"]
    assert body["truncated"] is False


def test_preview_rejects_unknown_and_traversal_paths() -> None:
    with TestClient(app) as client:
        unknown = client.get(
            "/api/skills/video-transcript/file",
            params={"path": "config.json"},
        )
        traversal = client.get(
            "/api/skills/video-transcript/file",
            params={"path": "../SKILL.md"},
        )
        missing = client.get("/api/skills/not-a-skill")

    assert unknown.status_code == 404
    assert traversal.status_code == 404
    assert missing.status_code == 404


def test_download_zip_contains_skill_folder() -> None:
    with TestClient(app) as client:
        response = client.get("/api/skills/video-transcript/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="video-transcript.zip"' in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "video-transcript/SKILL.md" in names
        assert "video-transcript/scripts/vtlib/cli.py" in names
        assert "video-transcript/config.json" not in names
        assert not any("__pycache__" in name for name in names)
        assert archive.read("video-transcript/SKILL.md").startswith(b"---")
