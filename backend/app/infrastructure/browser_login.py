"""浏览器助手：用本机已装的 Chrome/Edge 为平台生成访问凭据。

两种模式：
- `guest`（默认）：打开平台首页，等浏览器自己写入游客 Cookie（`ttwid`、`s_v_web_id` 等），
  读出来即可解析公开作品，用户无需任何操作，也不用登录；
- `login`（可选）：同样打开窗口，但等待用户扫码登录，用于只有登录后才能观看的内容。

不读取浏览器的 Cookie 数据库（Chrome/Edge 127+ 已加密，运行时还被占用），
而是用独立资料目录启动专用浏览器实例，通过 DevTools 协议让浏览器把 Cookie 交出来。
资料目录会保留，因此登录态和游客身份下次都能直接复用。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from websockets.sync.client import connect

from app.config import settings
from app.infrastructure.credential_store import (
    COOKIE_DOMAINS,
    CredentialError,
    require_platform,
    save_cookie_records,
)

logger = logging.getLogger(__name__)

LOGIN_MODES = ("guest", "login")

# guest 模式打开的页面：浏览器在这里自行初始化并写入游客 Cookie。
PLATFORM_LOGIN_URLS: dict[str, str] = {
    "bilibili": "https://www.bilibili.com/",
    "douyin": "https://www.douyin.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
}

# login 模式直接打开登录页，省得用户自己在首页里找登录入口。
PLATFORM_SIGNIN_URLS: dict[str, str] = {
    "bilibili": "https://passport.bilibili.com/login",
    "douyin": "https://www.douyin.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/login",
}


def entry_url(platform: str, mode: str) -> str:
    """按模式挑要打开的页面：要登录态就走登录页，取游客身份走首页。"""

    home = PLATFORM_LOGIN_URLS.get(platform)
    if home is None:
        # 平台没配入口页时给出可读提示，不要让 KeyError 冒到用户面前。
        raise CredentialError("该平台暂不支持用浏览器自动获取访问权限，请改用手动粘贴 Cookie")
    if mode == "login":
        return PLATFORM_SIGNIN_URLS.get(platform) or home
    return home

# 页面开始初始化就会写入的字段：出现它只说明浏览器已经在工作，还不足以解析。
GUEST_COOKIE_KEYS: dict[str, tuple[str, ...]] = {
    "bilibili": ("buvid3", "b_nut"),
    "douyin": ("ttwid", "s_v_web_id"),
    "xiaohongshu": ("a1", "webId"),
}

# 真正能调通解析接口的字段组合：齐全即可保存，不必再等固定时长。
READY_COOKIE_KEYS: dict[str, tuple[str, ...]] = {
    # B站公开视频本来就不需要登录态，拿到游客标识就够了。
    "bilibili": ("buvid3",),
    "douyin": ("ttwid", "s_v_web_id", "passport_csrf_token"),
    "xiaohongshu": ("a1", "webId"),
}

# 判定「已登录」的关键字段。
LOGIN_COOKIE_KEYS: dict[str, tuple[str, ...]] = {
    "bilibili": ("SESSDATA", "DedeUserID"),
    "douyin": ("sessionid", "sessionid_ss", "sid_tt"),
    "xiaohongshu": ("web_session", "customerClientId"),
}

BROWSER_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("chrome", r"Google\Chrome\Application\chrome.exe"),
    ("edge", r"Microsoft\Edge\Application\msedge.exe"),
)

START_TIMEOUT_SECONDS = 60.0
# 首次使用时浏览器要新建资料目录、首次访问平台还要加载全部脚本，可能超过 1 分钟；
# 这里给足时间，避免把还没补齐的凭据当成结果。
GUEST_TIMEOUT_SECONDS = 180.0
# 已经看到平台字段、但关键字段仍缺失时的兜底等待时长。
GUEST_SETTLE_SECONDS = 6.0
LOGIN_TIMEOUT_SECONDS = 600.0
POLL_INTERVAL_SECONDS = 2.0


class BrowserLoginError(RuntimeError):
    """浏览器助手不可用。"""


@dataclass
class LoginSession:
    platform: str
    mode: str = "guest"
    status: str = "running"
    message: str = "正在启动浏览器…"
    entries: int = 0
    updated_at: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen[bytes] | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "mode": self.mode,
            "status": self.status,
            "message": self.message,
            "entries": self.entries,
        }


_sessions: dict[str, LoginSession] = {}
_lock = threading.Lock()


def browser_candidates() -> list[Path]:
    """按优先级列出本机可用的浏览器可执行文件。"""

    roots = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    found: list[Path] = []
    for _name, relative in BROWSER_CANDIDATES:
        for root in roots:
            if not root:
                continue
            candidate = Path(root) / relative
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return found


def browser_available() -> bool:
    return bool(browser_candidates())


def cookie_names(cookies: list[dict[str, object]]) -> set[str]:
    """有实际取值的 Cookie 名；空值不算已具备该字段。"""

    return {
        str(item.get("name"))
        for item in cookies
        if item.get("name") and item.get("value")
    }


def guest_ready(platform: str, cookies: list[dict[str, object]]) -> bool:
    """凭据是否已经齐全，可以保存并用于解析。"""

    keys = READY_COOKIE_KEYS.get(platform, ())
    return bool(keys) and set(keys) <= cookie_names(cookies)


def partial_ready(platform: str, cookies: list[dict[str, object]]) -> bool:
    """浏览器是否已开始写入该平台的字段（用于启动兜底计时）。"""

    keys = GUEST_COOKIE_KEYS.get(platform, ())
    return bool(keys) and set(keys) <= cookie_names(cookies)


def login_detected(platform: str, cookies: list[dict[str, object]]) -> bool:
    keys = LOGIN_COOKIE_KEYS.get(platform, ())
    return bool(keys) and bool(set(keys) & cookie_names(cookies))


def _profile_dir(platform: str) -> Path:
    profile = settings.data_dir / "browser" / platform
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _read_debug_port(profile: Path, process: subprocess.Popen[bytes], deadline: float) -> int:
    """读取浏览器写出的调试端口；进程提前退出说明启动失败。"""

    marker = profile / "DevToolsActivePort"
    while time.time() < deadline:
        if marker.is_file():
            try:
                first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
                if first.isdigit():
                    return int(first)
            except (OSError, IndexError):
                pass
        if process.poll() is not None:
            raise BrowserLoginError("浏览器启动失败，请确认可以正常打开浏览器")
        time.sleep(0.3)
    raise BrowserLoginError("浏览器启动超时，请重试")


def _page_websocket(port: int, host_suffix: str, deadline: float) -> str:
    """找到登录页对应的调试通道。"""

    last_error = ""
    while time.time() < deadline:
        try:
            targets = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5.0).json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = str(exc)
            time.sleep(0.4)
            continue
        pages = [
            item
            for item in targets
            if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
        ]
        matched = [item for item in pages if host_suffix in str(item.get("url", ""))]
        chosen = (matched or pages)[:1]
        if chosen:
            return str(chosen[0]["webSocketDebuggerUrl"])
        time.sleep(0.4)
    raise BrowserLoginError(f"无法连接浏览器调试通道：{last_error or '没有可用页面'}")


def _cdp(websocket_url: str, method: str, params: dict[str, object] | None = None) -> dict:
    """发一条 DevTools 协议命令并等它的响应。"""

    try:
        with connect(websocket_url, open_timeout=15.0, close_timeout=2.0, max_size=None) as socket:
            socket.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            deadline = time.time() + 15.0
            while time.time() < deadline:
                message = json.loads(socket.recv(timeout=15.0))
                if message.get("id") != 1:
                    continue
                if "error" in message:
                    raise BrowserLoginError(str(message["error"].get("message", "浏览器返回错误")))
                return message.get("result") or {}
    except BrowserLoginError:
        raise
    except Exception as exc:  # websockets 的异常类型较多，统一成可读消息
        raise BrowserLoginError(f"与浏览器通信失败：{exc}") from exc
    raise BrowserLoginError("浏览器没有响应")


def read_browser_cookies(websocket_url: str, platform: str) -> list[dict[str, object]]:
    result = _cdp(websocket_url, "Network.getAllCookies")
    suffix = COOKIE_DOMAINS[platform].lstrip(".")
    return [
        item
        for item in result.get("cookies") or []
        if suffix in str(item.get("domain", ""))
    ]


def _close_browser(port: int | None, process: subprocess.Popen[bytes] | None) -> None:
    if port is not None:
        try:
            version = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=5.0).json()
            websocket_url = version.get("webSocketDebuggerUrl")
            if websocket_url:
                _cdp(str(websocket_url), "Browser.close")
        except (httpx.HTTPError, BrowserLoginError, ValueError) as exc:
            # 关闭失败不影响主流程，下面还会用进程终止兜底
            logger.info("通过调试协议关闭浏览器失败：%s", exc)
    _shutdown_process(process)


def _shutdown_process(process: subprocess.Popen[bytes] | None) -> None:
    """结束浏览器；Windows 上连子进程一起结束，避免它们占着资料目录不放。"""

    if process is None:
        return
    try:
        process.wait(timeout=8)
        return
    except subprocess.TimeoutExpired:
        pass
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.info("结束浏览器进程树失败：%s", exc)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()


def _kill_profile_browsers(profile: Path) -> None:
    """结束仍占用该资料目录的浏览器进程。

    Windows 上浏览器主进程退出后，GPU / 渲染 / crashpad 等子进程可能继续持有资料目录，
    导致下次启动时读到过期端口或直接卡住。这里按命令行里的 `--user-data-dir` 精确匹配，
    只结束我们自己启动的那一份，不影响用户正在使用的浏览器。
    """

    if sys.platform != "win32":
        return
    script = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.CommandLine -like '*--user-data-dir={profile}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # 清理失败不影响本次结果，下次启动前还会再清一次
        logger.info("清理浏览器残留进程失败：%s", exc)


def _launch(profile: Path, url: str) -> tuple[subprocess.Popen[bytes], int]:
    """依次尝试可用浏览器，返回进程与调试端口。"""

    last_error: BrowserLoginError | None = None
    # 启动前先清掉上次可能残留的进程，避免它们占着资料目录让新实例起不来
    _kill_profile_browsers(profile)
    for browser in browser_candidates():
        command = [
            str(browser),
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            # 关掉与本次获取无关的后台工作，明显缩短首次启动时间
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-client-side-phishing-detection",
            "--no-service-autorun",
            "--metrics-recording-only",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            url,
        ]
        logger.info("启动浏览器助手：%s", browser.name)
        # 必须清掉上次留下的端口文件：否则会立刻读到过期端口，
        # 连到一个已经关闭的调试通道，表现为「等一分钟然后失败」。
        (profile / "DevToolsActivePort").unlink(missing_ok=True)
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            port = _read_debug_port(profile, process, time.time() + START_TIMEOUT_SECONDS)
        except BrowserLoginError as exc:
            last_error = exc
            _close_browser(None, process)
            continue
        return process, port
    raise last_error or BrowserLoginError("没有可用的浏览器，请改用手动粘贴 Cookie")


def fetch_note_via_browser(platform: str, url: str, timeout_seconds: float = 75.0) -> dict:
    """用本机浏览器打开页面并读取 `window.__INITIAL_STATE__` 里的笔记数据。

    供游客 HTTP 请求被平台风控拦截时回退使用：真实浏览器指纹 + 资料目录里
    已保存的登录态（做过浏览器登录就有）不会被拦，登录弹窗由注入的脚本移除，
    数据早在服务端渲染进页面里，读完即走。
    """

    profile = _profile_dir(platform)
    process, port = _launch(profile, url)
    try:
        websocket_url = _page_websocket(
            port,
            COOKIE_DOMAINS[platform].lstrip("."),
            time.time() + START_TIMEOUT_SECONDS,
        )
        expression = (
            "(() => {"
            # 兼容桌面版（note.noteDetailMap）与移动版（noteData.data.noteData）两种结构
            "  const s = window.__INITIAL_STATE__ || {};"
            "  const maps = (s.note && s.note.noteDetailMap) || {};"
            "  const note = s?.noteData?.data?.noteData"
            "    || Object.values(maps).map((v) => v && v.note).find((n) => n && n.noteId)"
            "    || null;"
            "  if (!(note && note.noteId)) return '';"
            # 登录弹窗只是遮挡，数据已在 state 里；顺手移除避免后续交互受影响
            "  document.querySelectorAll('.login-container,.signin-container,[class*=login-mask]')"
            "    .forEach((el) => el.remove());"
            "  return JSON.stringify(note);"
            "})()"
        )
        deadline = time.time() + timeout_seconds
        last_value = ""
        while time.time() < deadline:
            result = _cdp(
                websocket_url,
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            last_value = str((result.get("result") or {}).get("value") or "")
            if last_value:
                note = json.loads(last_value)
                if isinstance(note, dict) and note.get("noteId"):
                    return note
            time.sleep(1.2)
        raise BrowserLoginError("浏览器打开页面后没有等到笔记数据，可能网络较慢或内容不可见")
    finally:
        _close_browser(port, process)


def _save(platform: str, cookies: list[dict[str, object]], session: LoginSession) -> None:
    """把读到的 Cookie 写入加密存储。"""

    saved = save_cookie_records(platform, cookies)
    session.entries = saved.entries
    session.status = "saved"


def _run_session(session: LoginSession) -> None:
    platform = session.platform
    port: int | None = None
    try:
        if not browser_available():
            raise BrowserLoginError("没有检测到 Chrome 或 Edge，请改用手动粘贴 Cookie")

        profile = _profile_dir(platform)
        session.process, port = _launch(profile, entry_url(platform, session.mode))
        websocket_url = _page_websocket(
            port,
            COOKIE_DOMAINS[platform].lstrip("."),
            time.time() + START_TIMEOUT_SECONDS,
        )
        started = time.time()
        session.message = (
            "正在获取访问权限…"
            if session.mode == "guest"
            else "请在浏览器窗口里扫码登录，登录成功后会自动获取"
        )

        timeout = GUEST_TIMEOUT_SECONDS if session.mode == "guest" else LOGIN_TIMEOUT_SECONDS
        deadline = started + timeout
        cookies: list[dict[str, object]] = []
        partial_since: float | None = None
        while time.time() < deadline:
            if session.cancel.is_set():
                session.status = "cancelled"
                session.message = "已取消"
                return
            if session.process.poll() is not None:
                raise BrowserLoginError("浏览器窗口已关闭，未能获取访问权限")
            try:
                cookies = read_browser_cookies(websocket_url, platform)
            except BrowserLoginError:
                # 页面切换或调试通道重建时会出现瞬时失败，继续轮询即可
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            elapsed = time.time() - started
            if session.mode == "guest":
                if partial_ready(platform, cookies) and partial_since is None:
                    partial_since = time.time()
                settled = (
                    partial_since is not None
                    and time.time() - partial_since >= GUEST_SETTLE_SECONDS
                )
                # 关键字段齐全就直接保存；等满兜底时长仍不全，就用当前字段保存
                if guest_ready(platform, cookies) or settled:
                    _save(platform, cookies, session)
                    session.message = (
                        f"已获取访问权限（{session.entries} 条 Cookie，"
                        f"用时 {elapsed:.0f} 秒），可以开始解析了"
                    )
                    logger.info(
                        "浏览器助手获取凭据完成：%s 条，用时 %.1f 秒",
                        session.entries,
                        elapsed,
                    )
                    return
                session.message = (
                    f"正在获取访问权限…（已用 {elapsed:.0f} 秒，已读到 {len(cookies)} 条）"
                )
            elif login_detected(platform, cookies):
                _save(platform, cookies, session)
                session.message = f"登录成功，已保存 {session.entries} 条 Cookie"
                logger.info("浏览器助手登录完成：%s 条，用时 %.1f 秒", session.entries, elapsed)
                return
            else:
                session.message = f"请在浏览器窗口里扫码登录…（已用 {elapsed:.0f} 秒）"
            time.sleep(POLL_INTERVAL_SECONDS)

        # 超时兜底：游客模式只要拿到了 Cookie 就先存下来，避免完全失败
        if session.mode == "guest" and cookies:
            _save(platform, cookies, session)
            session.message = (
                f"已保存 {session.entries} 条 Cookie（等待超时）；"
                "若解析失败，请重试或改用浏览器登录"
            )
            return
        raise BrowserLoginError("等待超时，请重试")
    except CredentialError as exc:
        session.status = "failed"
        session.message = str(exc)
    except BrowserLoginError as exc:
        if not session.cancel.is_set():
            session.status = "failed"
            session.message = str(exc)
    except Exception as exc:  # 兜底，避免后台线程静默退出
        logger.exception("浏览器助手失败")
        session.status = "failed"
        session.message = f"浏览器助手失败：{exc}"
    finally:
        session.updated_at = time.time()
        _close_browser(port, session.process)
        # 关闭后仍可能有子进程持有资料目录，这里做一次兜底清理
        _kill_profile_browsers(_profile_dir(platform))


def start_login(platform: str, mode: str = "guest") -> LoginSession:
    """启动一次浏览器助手；同一平台已在运行时直接复用。"""

    require_platform(platform)
    if mode not in LOGIN_MODES:
        raise CredentialError(f"不支持的登录模式：{mode}")
    with _lock:
        existing = _sessions.get(platform)
        if existing is not None and existing.status == "running":
            return existing
        session = LoginSession(platform=platform, mode=mode)
        _sessions[platform] = session
    threading.Thread(
        target=_run_session,
        args=(session,),
        name=f"browser-login-{platform}",
        daemon=True,
    ).start()
    return session


def current_login(platform: str) -> LoginSession | None:
    require_platform(platform)
    with _lock:
        return _sessions.get(platform)


def cancel_login(platform: str) -> LoginSession:
    require_platform(platform)
    session = current_login(platform)
    if session is None:
        session = LoginSession(platform=platform, status="idle", message="没有进行中的浏览器助手")
        with _lock:
            _sessions[platform] = session
        return session
    if session.status == "running":
        session.cancel.set()
        session.status = "cancelled"
        session.message = "已取消"
        session.updated_at = time.time()
    return session
