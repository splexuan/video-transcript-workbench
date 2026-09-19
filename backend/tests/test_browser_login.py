"""浏览器助手的判定逻辑与 Cookie 转换：不启动浏览器，只验证纯函数。"""

from __future__ import annotations

import pytest

from app.infrastructure.browser_login import (
    GUEST_COOKIE_KEYS,
    LOGIN_COOKIE_KEYS,
    READY_COOKIE_KEYS,
    browser_candidates,
    entry_url,
    guest_ready,
    login_detected,
    partial_ready,
)
from app.infrastructure.credential_store import (
    COOKIE_DOMAINS,
    CredentialError,
    count_entries,
    netscape_from_records,
)


def test_entry_url_uses_signin_page_for_login_mode() -> None:
    """登录模式要直接落到登录页，否则用户看到首页会找不到登录入口。"""

    assert entry_url("xiaohongshu", "login") == "https://www.xiaohongshu.com/login"
    assert entry_url("xiaohongshu", "guest") == "https://www.xiaohongshu.com/"
    # 抖音没有独立登录页，仍打开首页（未登录时会自行弹出登录框）
    assert entry_url("douyin", "login") == "https://www.douyin.com/"
    assert entry_url("bilibili", "login") == "https://passport.bilibili.com/login"
    assert entry_url("bilibili", "guest") == "https://www.bilibili.com/"


def test_every_credential_platform_works_with_browser_helper() -> None:
    """凡是能配置凭据的平台，浏览器助手都必须有完整配置。

    漏配的后果是用户点「一键获取」直接失败（只看到一句 `浏览器助手失败：'bilibili'`），
    所以这里按平台清单逐一检查，新增平台时不会再漏。
    """

    for platform in COOKIE_DOMAINS:
        assert entry_url(platform, "guest").startswith("http"), platform
        assert entry_url(platform, "login").startswith("http"), platform
        assert GUEST_COOKIE_KEYS.get(platform), platform
        assert READY_COOKIE_KEYS.get(platform), platform
        assert LOGIN_COOKIE_KEYS.get(platform), platform


def test_unknown_platform_reports_readable_error() -> None:
    """没有配置入口页的平台要给出中文提示，而不是把 KeyError 抛给用户。"""

    with pytest.raises(CredentialError):
        entry_url("wechat", "guest")


def test_guest_ready_requires_full_field_set() -> None:
    """初始化字段出现只算「部分就绪」，真正可解析还需要 passport_csrf_token。"""

    started: list[dict[str, object]] = [
        {"name": "ttwid", "value": "a"},
        {"name": "s_v_web_id", "value": "b"},
    ]
    complete = [*started, {"name": "passport_csrf_token", "value": "c"}]

    assert partial_ready("douyin", started) is True
    assert guest_ready("douyin", started) is False
    assert guest_ready("douyin", complete) is True
    # 别的平台的字段组不能混用
    assert partial_ready("xiaohongshu", started) is False
    assert guest_ready("xiaohongshu", complete) is False


def test_login_detected_matches_session_cookie() -> None:
    assert login_detected("douyin", [{"name": "sessionid", "value": "x"}]) is True
    assert login_detected("douyin", [{"name": "ttwid", "value": "x"}]) is False
    assert login_detected("xiaohongshu", [{"name": "web_session", "value": "x"}]) is True
    # 空值不算登录成功
    assert login_detected("douyin", [{"name": "sessionid", "value": ""}]) is False


def test_netscape_from_records_filters_domains_and_maps_flags() -> None:
    """只保留本平台域名；HttpOnly 加前缀，会话级 Cookie 的过期时间写 0。"""

    records: list[dict[str, object]] = [
        {
            "domain": ".douyin.com",
            "name": "ttwid",
            "value": "v1",
            "path": "/",
            "expires": 1800000000,
            "secure": True,
            "httpOnly": True,
        },
        {
            "domain": ".douyin.com",
            "name": "UIFID_TEMP",
            "value": "v3",
            "path": "/",
            "expires": -1,
        },
        {"domain": ".example.com", "name": "other", "value": "v2", "path": "/"},
    ]

    text = netscape_from_records(records, ".douyin.com")

    assert "#HttpOnly_.douyin.com\tTRUE\t/\tTRUE\t1800000000\tttwid\tv1" in text
    assert "\t0\tUIFID_TEMP\tv3" in text
    assert "other" not in text
    assert count_entries(text) == 2


def test_netscape_from_records_ignores_records_without_name() -> None:
    text = netscape_from_records([{"domain": ".douyin.com", "value": "v"}], ".douyin.com")

    assert count_entries(text) == 0


def test_browser_candidates_only_returns_existing_files() -> None:
    """浏览器候选必须真实存在，避免把不存在的路径交给启动流程。"""

    for path in browser_candidates():
        assert path.is_file()
        assert path.suffix.lower() == ".exe"
