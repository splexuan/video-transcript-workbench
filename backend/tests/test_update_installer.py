"""自动更新（解压 + 外部脚本替换/回滚）的测试。

PowerShell 集成用例只在 Windows 上跑：它用一个复制的 ping.exe 冒充程序，
真实执行 `HELPER_SCRIPT`，验证「替换成功」和「健康检查失败后回滚」两条路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import updates as updates_api
from app.config import settings
from app.infrastructure import lifecycle, update_installer as installer
from app.main import app


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch) -> None:
    monkeypatch.setattr(installer, "_install_task", None)
    monkeypatch.setattr(installer, "_support_cache", None)
    lifecycle.clear_shutdown()


def _write_package(
    path: Path,
    *,
    root: str = "创作工作室",
    executable: str = "创作工作室.exe",
    extra: dict[str, bytes] | None = None,
) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{root}/{executable}", b"exe")
        archive.writestr(f"{root}/_internal/base.dll", b"dll")
        for name, data in (extra or {}).items():
            archive.writestr(f"{root}/{name}", data)
    return path


# ---------------------------------------------------------------- 解压


def test_extract_package_strips_top_level_directory(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "update.zip", extra={"data/voice.txt": b"hello"})
    staging = tmp_path / "staging"
    seen: list[int] = []

    payload, executable = installer.extract_package(
        package,
        staging,
        expected_executable="创作工作室.exe",
        progress=seen.append,
    )

    assert executable == "创作工作室.exe"
    assert payload == (staging / "创作工作室").resolve()
    assert (payload / "_internal" / "base.dll").is_file()
    assert (payload / "data" / "voice.txt").read_text(encoding="utf-8") == "hello"
    assert seen[-1] == 100


def test_extract_package_accepts_flat_layout(tmp_path: Path) -> None:
    package = tmp_path / "flat.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("app.exe", b"exe")
        archive.writestr("_internal/base.dll", b"dll")

    payload, executable = installer.extract_package(
        package, tmp_path / "staging", expected_executable="app.exe"
    )

    assert executable == "app.exe"
    assert payload == (tmp_path / "staging").resolve()


def test_extract_package_rejects_path_traversal(tmp_path: Path) -> None:
    package = tmp_path / "evil.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../outside.txt", b"bad")

    with pytest.raises(installer.InstallError, match="非法路径"):
        installer.extract_package(package, tmp_path / "staging", expected_executable="app.exe")
    assert not (tmp_path / "outside.txt").exists()


def test_find_payload_root_requires_internal_directory(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "app.exe").write_bytes(b"exe")

    with pytest.raises(installer.InstallError, match="_internal"):
        installer.find_payload_root(staging, "app.exe")


# ---------------------------------------------------------------- 计划与结果


def test_build_plan_reads_port_and_suppresses_duplicate_browser(tmp_path: Path) -> None:
    plan = installer.build_plan(
        version="9.9.9",
        directory=tmp_path / "app",
        payload=tmp_path / "payload",
        executable="app.exe",
        arguments=["--port", "9001"],
    )

    assert plan.health_url == "http://127.0.0.1:9001/api/health"
    # 旧页面会在新进程起来后自动恢复，重启时不再新开浏览器标签页
    assert plan.arguments == ["--port", "9001", "--no-browser"]
    assert Path(plan.backup_dir) == tmp_path / ".creator-studio-update" / "backup"


def test_write_plan_files_uses_utf8_bom_and_static_script(tmp_path: Path) -> None:
    plan = installer.InstallPlan(
        version="9.9.9",
        parent_pid=0,
        program_dir=str(tmp_path / "程序目录"),
        payload_dir=str(tmp_path / "payload"),
        work_dir=str(tmp_path / "work"),
        backup_dir=str(tmp_path / "work" / "backup"),
        executable="app.exe",
        arguments=[],
        health_url="",
    )
    script = installer.write_plan_files(plan)

    assert script.read_bytes().startswith(b"\xef\xbb\xbf")
    saved = json.loads((tmp_path / "work" / "plan.json").read_text(encoding="utf-8"))
    assert saved["program_dir"] == str(tmp_path / "程序目录")
    assert saved["result_path"].endswith(installer.RESULT_FILE_NAME)


def test_read_install_result_reports_stale_running_as_failed(monkeypatch, tmp_path: Path) -> None:
    program = tmp_path / "app"
    work = tmp_path / installer.WORK_DIR_NAME
    work.mkdir()
    monkeypatch.setattr(installer, "packaged", lambda: True)
    monkeypatch.setattr(installer, "program_dir", lambda: program)
    result_path = work / installer.RESULT_FILE_NAME

    result_path.write_text(
        json.dumps(
            {"status": "running", "message": "正在替换", "version": "9.9.9", "updated_at": 1}
        ),
        encoding="utf-8",
    )
    result = installer.read_install_result()
    assert result is not None
    assert result.status == "failed"
    assert "没有完成" in result.message

    result_path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "message": "已更新到 v9.9.9",
                "version": "9.9.9",
                "updated_at": time.time(),
                "log_path": "x.log",
            }
        ),
        encoding="utf-8",
    )
    result = installer.read_install_result()
    assert result is not None and result.status == "succeeded"


# ---------------------------------------------------------------- 能力检测与安装任务


def test_install_supported_explains_dev_mode() -> None:
    supported, reason = installer.install_supported()
    assert supported is False
    assert reason is not None and "开发模式" in reason


def test_install_supported_honours_permissions(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "packaged", lambda: True)
    monkeypatch.setattr(installer, "program_dir", lambda: tmp_path / "app")
    monkeypatch.setattr(installer, "powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(installer, "_write_probe", lambda _folder: True)
    assert installer.install_supported() == (True, None)

    installer.reset_support_cache()
    monkeypatch.setattr(installer, "_write_probe", lambda _folder: False)
    supported, reason = installer.install_supported()
    assert supported is False
    assert reason is not None and "不可写" in reason


def test_start_install_extracts_and_launches_helper(monkeypatch, tmp_path: Path) -> None:
    program = tmp_path / "app"
    program.mkdir()
    package = tmp_path / "update.zip"
    package.write_bytes(b"not a real zip")
    payload = tmp_path / "payload"
    payload.mkdir()
    launched: dict[str, object] = {}

    monkeypatch.setattr(installer, "packaged", lambda: True)
    monkeypatch.setattr(installer, "program_dir", lambda: program)
    monkeypatch.setattr(installer, "install_supported", lambda: (True, None))
    monkeypatch.setattr(
        installer, "extract_package", lambda *args, **kwargs: (payload, "app.exe")
    )

    def launch(script: Path) -> None:
        launched["script"] = script

    monkeypatch.setattr(installer, "_launch_helper", launch)
    monkeypatch.setattr(
        lifecycle,
        "request_shutdown",
        lambda delay=0.0: launched.setdefault("shutdown", delay) is not None,
    )

    installer.start_install(package, "9.9.9")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        progress = installer.install_progress()
        if progress is not None and progress.status in {"launching", "failed"}:
            break
        time.sleep(0.02)

    progress = installer.install_progress()
    assert progress is not None and progress.status == "launching", progress
    script = launched["script"]
    assert isinstance(script, Path) and script.is_file()
    assert launched["shutdown"] == 1.0
    plan = json.loads((script.parent / installer.PLAN_FILE_NAME).read_text(encoding="utf-8"))
    assert plan["payload_dir"] == str(payload)
    assert plan["version"] == "9.9.9"


# ---------------------------------------------------------------- API


def _seed_downloaded_package(monkeypatch, tmp_path: Path, version: str = "9.9.9") -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    folder = tmp_path / "updates"
    folder.mkdir()
    return _write_package(folder / f"creator-studio-v{version}-win64.zip")


def test_state_exposes_install_support(monkeypatch) -> None:
    monkeypatch.setattr(updates_api.updater, "_fetch_latest", lambda: None)
    with TestClient(app) as client:
        payload = client.get("/api/updates").json()
    assert payload["install_supported"] is False
    assert payload["install_reason"]
    assert payload["install"] is None


def test_install_endpoint_requires_downloaded_package() -> None:
    with TestClient(app) as client:
        response = client.post("/api/updates/install")
    assert response.status_code == 409


def test_install_endpoint_refuses_dev_mode(monkeypatch, tmp_path: Path) -> None:
    _seed_downloaded_package(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/updates/install")
    assert response.status_code == 400
    assert "开发模式" in response.json()["detail"]


# ---------------------------------------------------------------- 真实脚本执行


PING = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe"
powershell = installer.powershell_executable()
requires_helper = pytest.mark.skipif(
    sys.platform != "win32" or not PING.is_file() or powershell is None,
    reason="需要 Windows PowerShell 与 ping.exe",
)


def _run_helper(program: Path, payload: Path, *, health_url: str) -> dict[str, object]:
    work = program.parent / installer.WORK_DIR_NAME
    work.mkdir(parents=True, exist_ok=True)
    plan = installer.InstallPlan(
        version="9.9.9",
        parent_pid=0,
        program_dir=str(program),
        payload_dir=str(payload),
        work_dir=str(work),
        backup_dir=str(work / installer.BACKUP_DIR_NAME),
        executable="app.exe",
        arguments=["-n", "2", "127.0.0.1"],
        health_url=health_url,
        health_seconds=1,
        wait_seconds=1,
    )
    script = installer.write_plan_files(plan)
    completed = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-PlanPath",
            str(work / installer.PLAN_FILE_NAME),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        encoding="utf-8",
        errors="replace",
    )
    log = ""
    log_path = work / installer.LOG_FILE_NAME
    if log_path.is_file():
        log = log_path.read_text(encoding="utf-8", errors="replace")
    assert completed.returncode == 0, (completed.stdout, completed.stderr, log)
    return json.loads((work / installer.RESULT_FILE_NAME).read_text(encoding="utf-8-sig"))


def _make_program(path: Path, marker: str) -> Path:
    path.mkdir(parents=True)
    (path / "app.exe").write_bytes(PING.read_bytes())
    (path / marker).write_text(marker, encoding="utf-8")
    return path


@requires_helper
def test_helper_replaces_program_and_keeps_backup(tmp_path: Path) -> None:
    program = _make_program(tmp_path / "创作工作室", "old.txt")
    payload = _make_program(
        tmp_path / installer.WORK_DIR_NAME / "staging" / "创作工作室", "new.txt"
    )

    result = _run_helper(program, payload, health_url="")

    assert result["status"] == "succeeded", result
    assert (program / "new.txt").is_file()
    assert not (program / "old.txt").exists()
    backup = tmp_path / installer.WORK_DIR_NAME / installer.BACKUP_DIR_NAME
    assert (backup / "old.txt").is_file()


@requires_helper
def test_helper_rolls_back_when_new_version_fails(tmp_path: Path) -> None:
    program = _make_program(tmp_path / "创作工作室", "old.txt")
    payload = _make_program(
        tmp_path / installer.WORK_DIR_NAME / "staging" / "创作工作室", "new.txt"
    )

    result = _run_helper(program, payload, health_url="http://127.0.0.1:1/api/health")

    assert result["status"] == "failed", result
    assert "健康检查" in result["message"]
    assert (program / "old.txt").is_file()
    assert not (program / "new.txt").exists()
    backup = tmp_path / installer.WORK_DIR_NAME / installer.BACKUP_DIR_NAME
    assert not backup.exists()
