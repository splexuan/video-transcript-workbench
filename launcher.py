from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND_DIST = ROOT / "frontend" / "dist"
HEALTH_URL = "http://127.0.0.1:8765/api/health"
APP_URL = "http://127.0.0.1:8765"

# 可选依赖：缺了不影响启动，但对应能力会不可用。它们装在独立 extra 里，所以
# 「按 README 装了 .[dev]」的环境默认就没有——而用户往往要到用那个功能时才发现。
# 启动时统一提示一次，比等到功能报错再解释要早。
#
# 这里刻意只提示、不拦截：这些功能都是可选的，为它们挡住整个应用不合理。
OPTIONAL_DEPENDENCIES = (
    ("faster_whisper", "accurate", "「精准时间轴」识别模式"),
)


def health_check(timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and payload.get("status") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def preflight() -> list[str]:
    errors: list[str] = []
    if not BACKEND.is_dir():
        errors.append("未找到 backend 目录")
    if not FRONTEND_DIST.is_dir():
        errors.append("未找到前端构建，请先在 frontend 目录运行 npm run build")
    return errors


def optional_dependency_notes() -> list[str]:
    """列出没装的「可选依赖」及其影响，给出可直接粘贴的安装命令。

    用 find_spec 而不是 import：后者会把模块真正加载一遍（faster-whisper 这类要几秒），
    而这里的结论只看「装没装」。
    """

    notes: list[str] = []
    for module, extra, feature in OPTIONAL_DEPENDENCIES:
        if importlib.util.find_spec(module) is not None:
            continue
        notes.append(f'{feature}不可用：缺少 {module}。安装：pip install -e ".[{extra}]"')
    return notes


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def main() -> int:
    errors = preflight()
    if errors:
        for message in errors:
            print(f"[错误] {message}")
        return 1

    # 可选依赖只提示不拦截：缺了照样启动，只是对应功能用不了。
    # --check 也照常提示，那正是它存在的意义。
    for note in optional_dependency_notes():
        print(f"[提示] {note}")

    if "--check" in sys.argv:
        print("启动环境检查通过")
        return 0

    open_browser = "--no-browser" not in sys.argv

    if health_check():
        print("创作工作室已经在运行，正在打开浏览器……")
        if open_browser:
            webbrowser.open(APP_URL)
        return 0

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app", 
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--app-dir",
        str(BACKEND),
    ]
    print("正在启动创作工作室，请稍候……")
    process = subprocess.Popen(command, cwd=ROOT)

    try:
        for _ in range(60):
            if process.poll() is not None:
                print(f"[错误] 本地服务启动失败，退出代码：{process.returncode}")
                return process.returncode or 1
            if health_check():
                print(f"启动成功：{APP_URL}")
                print("请保留此窗口。关闭窗口或按 Ctrl+C 可停止创作工作室。")
                if open_browser:
                    webbrowser.open(APP_URL)
                return process.wait()
            time.sleep(0.25)

        print("[错误] 本地服务启动超时")
        return 1
    except KeyboardInterrupt:
        print("\n正在停止创作工作室……")
        return 0
    finally:
        stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
