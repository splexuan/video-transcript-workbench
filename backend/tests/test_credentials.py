"""平台 Cookie 存储：解析、加密保存与状态查询，不依赖网络。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.infrastructure import credential_store
from app.infrastructure.credential_store import (
    COOKIE_TTL_SECONDS,
    CredentialError,
    count_entries,
    delete_cookie,
    materialize_cookie_file,
    normalize_cookie_text,
    read_netscape_cookies,
    save_cookie,
    status,
)


@pytest.fixture()
def isolated_data(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "data"
    monkeypatch.setattr(settings, "data_dir", root)
    return root


def test_normalize_header_text_writes_netscape_rows() -> None:
    text = normalize_cookie_text("ttwid=abc; msToken=xyz", ".douyin.com", now=1000.0)

    rows = [line for line in text.splitlines() if not line.startswith("#")]
    assert rows[0].split("\t") == [
        ".douyin.com",
        "TRUE",
        "/",
        "TRUE",
        str(int(1000.0 + COOKIE_TTL_SECONDS)),
        "ttwid",
        "abc",
    ]
    assert rows[1].endswith("\tmsToken\txyz")
    assert count_entries(text) == 2


def test_normalize_header_text_handles_cookie_prefix_and_duplicates() -> None:
    text = normalize_cookie_text("Cookie: a=1; b=2; a=3", ".douyin.com", now=0.0)

    assert count_entries(text) == 2
    assert "\ta\t1" in text
    # 重复字段只保留第一个，避免粘贴到多份 Cookie 时互相覆盖
    assert "\ta\t3" not in text


def test_normalize_keeps_exported_netscape_text() -> None:
    exported = ".xiaohongshu.com\tTRUE\t/\tTRUE\t1893456000\twebId\tabc123"

    text = normalize_cookie_text(exported, ".xiaohongshu.com")

    assert count_entries(text) == 1
    assert "webId\tabc123" in text


def test_normalize_rejects_empty_or_invalid_content() -> None:
    with pytest.raises(CredentialError):
        normalize_cookie_text("   ", ".douyin.com")
    with pytest.raises(CredentialError):
        normalize_cookie_text("这是一段网页代码，不是 Cookie", ".douyin.com")


def test_save_status_materialize_and_delete(isolated_data: Path) -> None:
    saved = save_cookie("douyin", "ttwid=abc; msToken=xyz")

    assert saved.configured is True
    assert saved.entries == 2
    assert saved.updated_at is not None
    assert status("douyin").configured is True

    handle = materialize_cookie_file("douyin")
    assert handle is not None
    content = handle.read_text(encoding="utf-8")
    assert "ttwid\tabc" in content
    handle.unlink(missing_ok=True)

    assert delete_cookie("douyin") is True
    assert status("douyin").configured is False
    assert materialize_cookie_file("douyin") is None


def test_cookie_is_not_stored_in_plaintext(isolated_data: Path) -> None:
    save_cookie("douyin", "ttwid=very-secret-value")

    raw = (isolated_data / "credentials" / "douyin.cookie").read_bytes()

    assert b"very-secret-value" not in raw


def test_unknown_platform_is_rejected(isolated_data: Path) -> None:
    with pytest.raises(CredentialError):
        save_cookie("wechat", "a=b")
    with pytest.raises(CredentialError):
        status("wechat")


def test_status_is_per_platform(isolated_data: Path) -> None:
    save_cookie("douyin", "ttwid=abc")

    assert credential_store.status("douyin").configured is True
    assert credential_store.status("xiaohongshu").configured is False
    items = credential_store.all_status()
    assert [item.platform for item in items] == ["bilibili", "douyin", "xiaohongshu"]
    # B站、小红书的公开视频游客都能看，凭据是可选的；抖音不配置就无法解析。
    assert [item.required for item in items] == [False, True, False]


def test_read_netscape_cookies_filters_domain(isolated_data: Path) -> None:
    """自建请求（例如 B站字幕接口）只该拿到属于本平台的 Cookie。"""

    save_cookie("bilibili", "SESSDATA=abc; bili_jct=def")
    handle = materialize_cookie_file("bilibili")
    assert handle is not None
    try:
        cookies = read_netscape_cookies(handle, ".bilibili.com")
        other = read_netscape_cookies(handle, ".douyin.com")
    finally:
        handle.unlink(missing_ok=True)

    assert cookies == {"SESSDATA": "abc", "bili_jct": "def"}
    assert other == {}
    assert read_netscape_cookies(None, ".bilibili.com") == {}
