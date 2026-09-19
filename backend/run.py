"""打包版入口：启动本地服务并常驻系统托盘，不弹终端窗口。

用法：文案工作台.exe [--port 8110] [--no-browser] [--no-tray]

- 打包用 windowed 引导程序（spec 里 console=False），双击后只有托盘图标；
  服务跑在后台线程，主线程留给托盘消息循环（pystray 的 Windows 后端要求主线程）。
- 没有控制台时 stdout/stderr 是 None，uvicorn 一打日志就会报错，也留不下线索，
  因此启动时把这两个流改写到 <数据目录>/logs/app.log。
- 端口上已经有实例在跑时（重复双击），直接打开界面，不再起第二个进程、第二个托盘图标。
- 开发环境没装 pystray 或加了 --no-tray 时，退回「前台阻塞」的老行为。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from app.config import settings
from app.main import app

APP_NAME = "文案工作台"
DEFAULT_PORT = 8110
# 常驻托盘期间日志会一直追加，超过这个大小就留一代备份
MAX_LOG_BYTES = 2 * 1024 * 1024

logger = logging.getLogger(__name__)


def app_asset(name: str) -> Path | None:
    """找随程序分发的资源：打包后是 PyInstaller 的资源目录，开发时是仓库的 assets/。"""

    candidates: list[Path] = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / name)
    here = Path(__file__).resolve().parent
    candidates.append(here / name)
    candidates.append(here.parent / "assets" / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def log_file() -> Path:
    root = settings.data_dir / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root / "app.log"


def rotate_log(path: Path, limit: int = MAX_LOG_BYTES) -> None:
    """日志超过上限就留一代备份，避免长期挂在托盘时无限增长。"""

    try:
        if path.exists() and path.stat().st_size > limit:
            path.replace(path.with_suffix(".log.1"))
    except OSError as exc:  # 日志写不进去不该影响启动
        logger.warning("日志轮转失败：%s", exc)


def ensure_output_streams() -> Path | None:
    """没有控制台时把 stdout/stderr 落到日志文件。

    返回日志路径；在有终端的开发环境里返回 None，不改变原有行为。
    """

    if sys.stdout is not None and sys.stderr is not None:
        return None
    path = log_file()
    rotate_log(path)
    # 常驻到进程结束：它就是这两个流的落点，不需要关闭
    stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = stream
    sys.stderr = stream
    logging.basicConfig(
        level=logging.INFO,
        stream=stream,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print(f"\n===== {APP_NAME} 启动于 {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
    return path


def server_alive(base_url: str, timeout: float = 1.5) -> bool:
    """端口上是否已经有本程序在跑（重复双击时不再起第二个实例）。"""

    try:
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return response.status == 200 and payload.get("status") == "ok"


def open_when_ready(base_url: str, timeout: float = 25.0) -> None:
    """等服务真的起来再打开浏览器：用户看到的第一眼就是可用界面。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_alive(base_url, timeout=1.0):
            webbrowser.open(base_url)
            return
        time.sleep(0.4)


def tray_image() -> object:
    """托盘图标：优先用随包分发的 ico；取不到时交给 pystray 用默认图标。"""

    path = app_asset("icon.ico")
    if path is None:
        return None
    try:
        from PIL import Image

        return Image.open(path)
    except (ImportError, OSError) as exc:
        logger.warning("托盘图标加载失败：%s", exc)
        return None


def run_tray(server: uvicorn.Server, base_url: str) -> bool:
    """常驻系统托盘，阻塞到用户选择「退出」。

    没有 pystray（开发环境常见）时返回 False，由调用方退回前台阻塞模式。
    """

    try:
        import pystray
    except ImportError:
        logger.info("没有安装 pystray，跳过系统托盘")
        return False

    def open_ui(*_args: object) -> None:
        webbrowser.open(base_url)

    def open_logs(*_args: object) -> None:
        folder = log_file().parent
        if sys.platform == "win32":
            os.startfile(folder)
        else:
            webbrowser.open(folder.as_uri())

    def quit_app(icon: object, *_args: object) -> None:
        # 先让 uvicorn 优雅退出，再结束托盘消息循环，主线程随后返回
        server.should_exit = True
        icon.stop()  # type: ignore[attr-defined]

    menu = pystray.Menu(
        pystray.MenuItem("打开界面", open_ui, default=True),
        pystray.MenuItem("打开日志目录", open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon(APP_NAME, tray_image(), APP_NAME, menu)
    # 用 print 而不是 logger：打包版没有控制台时它同样落在日志文件里，便于排查
    print(f"已常驻系统托盘：{base_url}")
    icon.run()
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--no-tray", action="store_true", help="不常驻托盘，前台运行（便于调试）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ensure_output_streams()
    base_url = f"http://127.0.0.1:{args.port}"

    if server_alive(base_url):
        # 已经有一个实例在跑：打开它，避免端口冲突和第二个托盘图标
        print(f"{APP_NAME} 已在运行，直接打开界面：{base_url}")
        webbrowser.open(base_url)
        return 0

    # access_log 关掉：界面会周期性轮询任务状态，请求日志会把文件刷满
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    print(f"{APP_NAME} 正在启动：{base_url}")

    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(base_url,), daemon=True).start()

    if not args.no_tray and run_tray(server, base_url):
        # 托盘退出后给 uvicorn 一点时间收尾
        thread.join(timeout=5)
        return 0

    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=5)
    if not server.started and not server.should_exit:
        print(f"[错误] 本地服务没能启动，请检查端口 {args.port} 是否被占用", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
