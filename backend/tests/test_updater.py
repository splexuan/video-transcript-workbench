"""版本更新模块的测试（不联网：远程响应与设置都在用例里替换掉）。"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import updates as updates_api
from app.config import settings
from app.infrastructure import updater
from app.main import app


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch) -> None:
    """每个用例都从「没检查过」开始，避免模块级缓存串场。"""

    monkeypatch.setattr(updater, "_check_result", updater.CheckResult())


@pytest.fixture(autouse=True)
def _auto_check_on(monkeypatch) -> None:
    """默认按「已开启自动检查、没跳过任何版本」来读设置，个别用例再各自覆盖。"""

    monkeypatch.setattr(
        updates_api,
        "read_settings",
        lambda _session: {"auto_check_update": True, "ignored_version": ""},
    )


def _payload(
    *,
    tag: str = "v0.2.0",
    assets: list[dict[str, object]] | None = None,
    notes: str = "修复若干问题",
) -> dict[str, object]:
    return {
        "tag_name": tag,
        "name": f"文案工作台 {tag}",
        "body": notes,
        "published_at": "2026-09-01T10:00:00Z",
        "html_url": f"https://example.test/releases/tag/{tag}",
        "assets": assets
        if assets is not None
        else [
            {
                "name": f"video-transcript-workbench-{tag}-win64.zip",
                "size": 1024,
                "browser_download_url": "https://example.test/app.zip",
            }
        ],
    }


def _write_zip(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "data")
    return path


# ---------------------------------------------------------------- 版本比较


def test_is_newer_compares_number_segments() -> None:
    assert updater.is_newer("v0.2.0", "0.1.4") is True
    assert updater.is_newer("0.1.10", "0.1.9") is True
    assert updater.is_newer("0.2", "0.2.0") is False
    assert updater.is_newer("0.1.4", "0.1.4") is False
    assert updater.is_newer("0.1.3", "0.1.4") is False


def test_is_newer_is_silent_when_version_is_unparsable() -> None:
    """解析不出数字时宁可少提示一次，也不要给出假的「有新版本」。"""

    assert updater.is_newer("nightly", "0.1.4") is False
    assert updater.is_newer("0.2.0", "") is False


def test_normalize_version_strips_prefix_and_suffix() -> None:
    assert updater.normalize_version("v0.2.0") == "0.2.0"
    assert updater.normalize_version(" v0.2.0-beta.1 ") == "0.2.0"


# ---------------------------------------------------------------- Release 解析


def test_select_asset_prefers_win64_package() -> None:
    asset = updater.select_asset(
        [
            {"name": "source.zip", "size": 10, "browser_download_url": "https://example.test/s.zip"},
            {
                "name": "video-transcript-workbench-v0.2.0-win64.zip",
                "size": 20,
                "browser_download_url": "https://example.test/w.zip",
            },
            {"name": "SHA256SUMS.txt", "size": 1, "browser_download_url": "https://example.test/sum"},
        ]
    )
    assert asset is not None
    assert asset.name.endswith("win64.zip")


def test_select_asset_falls_back_to_any_zip() -> None:
    asset = updater.select_asset(
        [{"name": "bundle.zip", "size": 5, "browser_download_url": "https://example.test/b.zip"}]
    )
    assert asset is not None and asset.name == "bundle.zip"


def test_select_asset_requires_a_zip() -> None:
    """只有源码包/校验文件时不给下载入口，界面会引导去发布页。"""

    assert updater.select_asset([{"name": "SHA256SUMS.txt", "browser_download_url": "https://example.test/s"}]) is None
    assert updater.select_asset(None) is None


def test_parse_release_trims_long_notes() -> None:
    release = updater.parse_release(_payload(notes="说" * (updater.MAX_NOTES_CHARS + 50)))
    assert len(release.notes) == updater.MAX_NOTES_CHARS + 1
    assert release.notes.endswith("…")


# ---------------------------------------------------------------- 检查行为


def test_check_throttles_repeat_calls(monkeypatch) -> None:
    """两次检查之间隔着节流间隔，第二次不该再打远程。"""

    calls: list[int] = []

    def fetch() -> dict[str, object]:
        calls.append(1)
        return _payload()

    monkeypatch.setattr(updater, "_fetch_latest", fetch)
    updater.check(force=True)
    updater.check()
    assert len(calls) == 1


def test_check_failure_keeps_previous_release(monkeypatch) -> None:
    """网络抖一下不该把「有新版本」的提示抹掉。"""

    monkeypatch.setattr(updater, "_fetch_latest", lambda: _payload())
    assert updater.check(force=True).latest is not None

    def boom() -> dict[str, object]:
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(updater, "_fetch_latest", boom)
    result = updater.check(force=True)
    assert result.latest is not None and result.latest.version == "0.2.0"
    assert result.error is not None and "网络" in result.error


def test_check_reports_empty_repository(monkeypatch) -> None:
    """仓库还没有 Release（404）时给一句说明，而不是当成网络故障。"""

    monkeypatch.setattr(updater, "_fetch_latest", lambda: None)
    result = updater.check(force=True)
    assert result.latest is None
    assert result.error is not None and "发布记录" in result.error


# ---------------------------------------------------------------- 下载包校验


def test_verify_archive_accepts_zip_with_executable(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "ok.zip", ["文案工作台/文案工作台.exe", "文案工作台/_internal/base.dll"])
    updater._verify_archive(archive)


def test_verify_archive_rejects_non_zip_and_zip_without_exe(tmp_path: Path) -> None:
    text = tmp_path / "error.zip"
    text.write_text("<html>502</html>", encoding="utf-8")
    with pytest.raises(updater.UpdateError, match="有效的压缩包"):
        updater._verify_archive(text)

    archive = _write_zip(tmp_path / "source.zip", ["src/main.py"])
    with pytest.raises(updater.UpdateError, match="可执行文件"):
        updater._verify_archive(archive)


def test_package_info_picks_newest_archive(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    folder = tmp_path / "updates"
    folder.mkdir()
    older = _write_zip(folder / "video-transcript-workbench-v0.1.4-win64.zip", ["文案工作台.exe"])
    newer = _write_zip(folder / "video-transcript-workbench-v0.2.0-win64.zip", ["文案工作台.exe"])
    # 显式拉开 mtime：同一秒里建的两个文件靠 touch 分不出先后，用例会飘
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_600, 1_700_000_600))

    package = updater.package_info()
    assert package is not None
    assert package.version == "0.2.0"
    assert package.file_name.endswith("v0.2.0-win64.zip")


def test_download_without_release_is_rejected() -> None:
    with TestClient(app) as client:
        response = client.post("/api/updates/download")
    assert response.status_code == 409


def test_download_requires_an_asset(monkeypatch) -> None:
    """Release 里没有 Windows 包时给可操作的提示，而不是去下个源码包。"""

    release = updater.parse_release(_payload(assets=[{"name": "SHA256SUMS.txt"}]))
    with pytest.raises(updater.UpdateError, match="手动下载"):
        updater.start_download(release)


# ---------------------------------------------------------------- 接口


def test_updates_endpoint_reports_current_version(monkeypatch) -> None:
    monkeypatch.setattr(updater, "_fetch_latest", lambda: _payload(tag="v0.0.1"))
    with TestClient(app) as client:
        payload = client.get("/api/updates").json()
    assert payload["current_version"] == settings.app_version
    assert payload["has_update"] is False
    assert payload["latest"]["version"] == "0.0.1"
    assert payload["error"] is None


def test_updates_endpoint_flags_newer_release(monkeypatch) -> None:
    monkeypatch.setattr(updater, "_fetch_latest", lambda: _payload(tag="v9.9.9"))
    with TestClient(app) as client:
        payload = client.get("/api/updates").json()
    assert payload["has_update"] is True
    assert payload["latest"]["asset"]["name"].endswith("win64.zip")
    assert payload["package"] is None


def test_updates_endpoint_skips_remote_when_auto_check_off(monkeypatch) -> None:
    """关掉自动检查后，读取状态不会发出任何远程请求。"""

    calls: list[int] = []
    monkeypatch.setattr(updater, "_fetch_latest", lambda: calls.append(1))
    monkeypatch.setattr(
        updates_api,
        "read_settings",
        lambda _session: {"auto_check_update": False, "ignored_version": ""},
    )
    with TestClient(app) as client:
        payload = client.get("/api/updates").json()
    assert payload["auto_check"] is False
    assert calls == []


def test_updates_endpoint_hides_ignored_version(monkeypatch) -> None:
    """点过「跳过此版本」之后不再提示，但版本信息仍然返回。"""

    monkeypatch.setattr(updater, "_fetch_latest", lambda: _payload(tag="v9.9.9"))
    monkeypatch.setattr(
        updates_api,
        "read_settings",
        lambda _session: {"auto_check_update": True, "ignored_version": "9.9.9"},
    )
    with TestClient(app) as client:
        payload = client.get("/api/updates").json()
    assert payload["has_update"] is False
    assert payload["ignored_version"] == "9.9.9"
    assert payload["latest"]["version"] == "9.9.9"
