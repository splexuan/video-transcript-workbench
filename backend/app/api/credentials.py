"""平台 Cookie 接口：扫码登录、导入、查看配置状态、删除。

接口只回传「是否已配置 / 条目数 / 更新时间」，Cookie 内容不出后端。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.infrastructure import browser_login
from app.infrastructure.credential_store import (
    CredentialError,
    CredentialStatus,
    all_status,
    delete_cookie,
    require_platform,
    save_cookie,
)
from app.schemas import CookieStatusRead, CookieWrite, LoginStatusRead

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


def _read(item: CredentialStatus) -> CookieStatusRead:
    return CookieStatusRead(
        platform=item.platform,
        configured=item.configured,
        entries=item.entries,
        updated_at=item.updated_at,
        required=item.required,
    )


@router.get("", response_model=list[CookieStatusRead])
def list_credentials() -> list[CookieStatusRead]:
    return [_read(item) for item in all_status()]


@router.put("/{platform}", response_model=CookieStatusRead)
def save_credential(platform: str, payload: CookieWrite) -> CookieStatusRead:
    try:
        return _read(save_cookie(platform, payload.cookie))
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{platform}")
def delete_credential(platform: str) -> dict[str, object]:
    try:
        removed = delete_cookie(platform)
    except CredentialError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"platform": platform, "removed": removed}


def _login_status(session: browser_login.LoginSession) -> LoginStatusRead:
    return LoginStatusRead(**session.snapshot())


@router.post("/{platform}/login", response_model=LoginStatusRead)
def start_browser_login(
    platform: str,
    mode: Annotated[str, Query(pattern="^(guest|login)$")] = "guest",
) -> LoginStatusRead:
    """启动浏览器助手：guest 取游客身份（无需操作），login 等待扫码登录。"""

    try:
        require_platform(platform)
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not browser_login.browser_available():
        raise HTTPException(
            status_code=400,
            detail="没有检测到 Chrome 或 Edge，请改用手动粘贴 Cookie",
        )
    try:
        session = browser_login.start_login(platform, mode)
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _login_status(session)


@router.get("/{platform}/login", response_model=LoginStatusRead)
def read_browser_login(platform: str) -> LoginStatusRead:
    try:
        require_platform(platform)
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = browser_login.current_login(platform)
    if session is None:
        return LoginStatusRead(
            platform=platform,
            mode="guest",
            status="idle",
            message="没有进行中的浏览器助手",
            entries=0,
        )
    return _login_status(session)


@router.delete("/{platform}/login", response_model=LoginStatusRead)
def cancel_browser_login(platform: str) -> LoginStatusRead:
    try:
        return _login_status(browser_login.cancel_login(platform))
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
