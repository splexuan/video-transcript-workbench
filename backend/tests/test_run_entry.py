"""打包版入口里可单测的部分（不启动服务、不碰托盘）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import run


def test_parse_args_defaults() -> None:
    args = run.parse_args([])

    assert args.port == run.DEFAULT_PORT
    assert args.no_browser is False
    assert args.no_tray is False


def test_parse_args_flags() -> None:
    args = run.parse_args(["--port", "9000", "--no-browser", "--no-tray"])

    assert (args.port, args.no_browser, args.no_tray) == (9000, True, True)


def test_app_asset_finds_repo_icon() -> None:
    """开发环境下要能找到仓库 assets/ 里的图标（打包后走 sys._MEIPASS）。"""

    icon = run.app_asset("icon.ico")

    assert icon is not None
    assert icon.name == "icon.ico"


def test_app_asset_missing_returns_none() -> None:
    assert run.app_asset("不存在的资源.bin") is None


def test_server_alive_is_false_without_service() -> None:
    """端口上没有服务时要安静地返回 False，而不是抛异常。"""

    assert run.server_alive("http://127.0.0.1:9", timeout=0.3) is False


def test_ensure_output_streams_keeps_console(monkeypatch) -> None:
    """有终端（开发/调试）时不改写输出，也不创建日志文件。"""

    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())

    assert run.ensure_output_streams() is None


def test_ensure_output_streams_redirects_when_windowed(monkeypatch, tmp_path: Path) -> None:
    """windowed 打包版没有 stdout/stderr：要落到数据目录的日志文件里。"""

    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        path = run.ensure_output_streams()
        assert path == tmp_path / "logs" / "app.log"
        assert path is not None and path.is_file()
        print("这行会写进日志文件")
        assert "这行会写进日志文件" in path.read_text(encoding="utf-8")
    finally:
        # 别把测试进程的 stdout/stderr 永久换掉
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__


def test_rotate_log_keeps_one_backup(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("x" * 128, encoding="utf-8")

    run.rotate_log(path, limit=64)

    assert not path.exists()
    assert (tmp_path / "app.log.1").read_text(encoding="utf-8") == "x" * 128


def test_rotate_log_ignores_small_file(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("x" * 32, encoding="utf-8")

    run.rotate_log(path, limit=64)

    assert path.is_file()
    assert not (tmp_path / "app.log.1").exists()


@pytest.mark.parametrize("port", [8110, 9000])
def test_main_opens_existing_instance(monkeypatch, port: int) -> None:
    """端口上已有实例时：只打开界面，不再起第二个进程。"""

    opened: list[str] = []
    monkeypatch.setattr(run, "server_alive", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(run, "ensure_output_streams", lambda: None)
    monkeypatch.setattr(run.webbrowser, "open", lambda url: opened.append(url))

    assert run.main(["--port", str(port)]) == 0
    assert opened == [f"http://127.0.0.1:{port}"]
