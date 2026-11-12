"""自动替换程序目录：解压 → 退出 → 外部脚本备份/替换/重启 → 失败回滚。

为什么不能自己替换：Windows 会锁住正在运行的 exe 和已加载的 DLL，进程活着时
连重命名它所在的目录都不行。所以流程拆成两段：

1. 本进程把新版本解压到程序目录旁边的暂存目录（同一个磁盘，后面可以用改名完成
   替换），写好 PowerShell 脚本并启动它；
2. 本进程正常退出后，脚本等待父进程结束，把旧目录改名为备份、把暂存目录改名为
   程序目录，启动新版本并轮询 `/api/health`；启动失败就删掉新目录、把备份改回来，
   再启动旧版本。

用 PowerShell 而不是再打一个 helper.exe：Windows 10/11 自带 Windows PowerShell
5.1，不需要额外产物；脚本与计划文件都放在程序目录**外**的工作目录里，替换过程
不会把脚本自己搬走。脚本开头会先写一条 `running` 结果，中途断电或被杀掉时，下次
启动能看出「上次更新没有完成」。

备份只保留一份（上一次更新前的版本），放在 `<程序目录的上级>/.creator-studio-update/backup`。
程序目录不可写（例如装在 Program Files）时不会强行提权，界面会退回手动解压。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.infrastructure import lifecycle

logger = logging.getLogger(__name__)

WORK_DIR_NAME = ".creator-studio-update"
STAGING_DIR_NAME = "staging"
BACKUP_DIR_NAME = "backup"
PLAN_FILE_NAME = "plan.json"
SCRIPT_FILE_NAME = "apply-update.ps1"
RESULT_FILE_NAME = "install-result.json"
LOG_FILE_NAME = "install.log"

EXECUTABLE_SUFFIX = ".exe"
# 解压后的体积之外再留一点余量：目录项、文件系统块都不计入 file_size
EXTRACT_HEADROOM = 1.05
# 包解压出来的目录至少要长这样，否则不是我们发布的免安装包
INTERNAL_DIR_NAME = "_internal"
# 结果文件里出现 running 但已经过去这么久，基本就是进程被强行结束过
RUNNING_STALE_SECONDS = 600.0
# 可写性探测结果缓存：状态接口会周期性轮询，不必每次都真的写文件
SUPPORT_CACHE_SECONDS = 30.0


class InstallError(RuntimeError):
    """可以展示给用户的自动更新错误。"""


class InstallBusyError(InstallError):
    """已经有解压/替换任务在进行。"""


@dataclass(slots=True)
class InstallProgress:
    version: str
    status: str = "pending"
    percent: int = 0
    message: str = ""
    error: str | None = None
    updated_at: float = 0.0

    @property
    def active(self) -> bool:
        return self.status in {"pending", "extracting", "launching"}


@dataclass(slots=True)
class InstallPlan:
    version: str
    parent_pid: int
    program_dir: str
    payload_dir: str
    work_dir: str
    backup_dir: str
    executable: str
    arguments: list[str]
    health_url: str
    # 父进程的 exe 名（不带扩展名）：等待退出前核对，避免 PID 被复用后误杀别的进程
    parent_name: str = ""
    health_seconds: int = 45
    wait_seconds: int = 90

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "parent_pid": self.parent_pid,
            "parent_name": self.parent_name,
            "program_dir": self.program_dir,
            "payload_dir": self.payload_dir,
            "work_dir": self.work_dir,
            "backup_dir": self.backup_dir,
            "executable": self.executable,
            "arguments": list(self.arguments),
            "health_url": self.health_url,
            "health_seconds": self.health_seconds,
            "wait_seconds": self.wait_seconds,
            "result_path": str(Path(self.work_dir) / RESULT_FILE_NAME),
            "log_path": str(Path(self.work_dir) / LOG_FILE_NAME),
        }


@dataclass(slots=True)
class InstallResult:
    status: str
    message: str
    version: str
    updated_at: float
    log_path: str


@dataclass(slots=True)
class _Task:
    progress: InstallProgress
    lock: threading.Lock

    def report(self, **changes: object) -> None:
        with self.lock:
            for key, value in changes.items():
                setattr(self.progress, key, value)
            self.progress.updated_at = time.time()


# ---------------------------------------------------------------- 位置与能力检测


def packaged() -> bool:
    """与 updater.packaged 保持同一个判断，但这里不 import updater，避免循环依赖。"""

    return bool(getattr(sys, "frozen", False))


def program_dir() -> Path | None:
    """当前正在运行的程序目录；开发模式下没有可替换的目录。"""

    if not packaged():
        return None
    return Path(sys.executable).resolve().parent


def work_root(directory: Path) -> Path:
    return directory.parent / WORK_DIR_NAME


def _write_probe(folder: Path) -> bool:
    probe = folder / f".vtw-write-test-{os.getpid()}-{threading.get_ident()}"
    try:
        with probe.open("w", encoding="utf-8") as handle:
            handle.write("ok")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


_support_lock = threading.Lock()
_support_cache: tuple[float, Path, bool, str | None] | None = None


def install_supported() -> tuple[bool, str | None]:
    """能不能自动替换；不能时返回一句可操作的原因。"""

    global _support_cache
    if sys.platform != "win32":
        return False, "自动替换目前只支持 Windows，请手动解压更新包。"
    directory = program_dir()
    if directory is None:
        return False, "开发模式下不会替换程序目录，请手动解压更新包。"

    now = time.time()
    with _support_lock:
        if (
            _support_cache is not None
            and _support_cache[1] == directory
            and now - _support_cache[0] < SUPPORT_CACHE_SECONDS
        ):
            return _support_cache[2], _support_cache[3]

    if powershell_executable() is None:
        supported, reason = False, "系统里找不到 Windows PowerShell，请手动解压更新包。"
    elif not _write_probe(directory.parent):
        supported, reason = (
            False,
            "程序目录所在的文件夹不可写（可能装在 Program Files）。"
            "请用管理员身份运行程序，或手动解压更新包。",
        )
    else:
        supported, reason = True, None

    with _support_lock:
        _support_cache = (now, directory, supported, reason)
    return supported, reason


def reset_support_cache() -> None:
    """权限变化（例如换到可写目录）后重新探测；测试也用它。"""

    global _support_cache
    with _support_lock:
        _support_cache = None


def powershell_executable() -> str | None:
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("powershell.exe") or shutil.which("powershell")
    return found


# ---------------------------------------------------------------- 解压


def _safe_member_target(root: Path, name: str) -> Path | None:
    """把 zip 条目名落到 root 之下；拒绝绝对路径、`..` 与冒号（NTFS ADS）。"""

    cleaned = (name or "").replace("\\", "/")
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise InstallError("更新包里有非法路径，已停止安装")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if any(part == ".." or ":" in part for part in parts):
        raise InstallError("更新包里有非法路径，已停止安装")
    if not parts:
        return None
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        raise InstallError("更新包里有非法路径，已停止安装")
    return target


def extract_package(
    package_path: Path,
    destination: Path,
    *,
    expected_executable: str,
    progress: Callable[[int], None] | None = None,
) -> tuple[Path, str]:
    """把压缩包解压到 destination，返回（真正的程序目录, 新 exe 文件名）。

    发行包里面有一层「创作工作室/」，但这里不假设这一层一定存在：以能找到的
    exe 为准，只要它旁边有 `_internal`，就是可以直接替换的 PyInstaller 目录。
    """

    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        archive = zipfile.ZipFile(package_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallError("更新包打不开，可能已经损坏，请重新下载") from exc

    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        total = sum(max(item.file_size, 0) for item in members) or 1
        done = 0
        for member in members:
            target = _safe_member_target(root, member.filename)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(member) as source, target.open("wb") as sink:
                    while True:
                        chunk = source.read(1 << 20)
                        if not chunk:
                            break
                        sink.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(min(99, int(done * 100 / total)))
            except OSError as exc:
                raise InstallError(f"解压更新包失败：{exc}") from exc

    payload, executable = find_payload_root(root, expected_executable)
    if progress is not None:
        progress(100)
    return payload, executable


def find_payload_root(staging: Path, expected_executable: str) -> tuple[Path, str]:
    """在解压结果里找可以直接搬到程序目录的那一层。"""

    executables = [item for item in staging.rglob(f"*{EXECUTABLE_SUFFIX}") if item.is_file()]
    if not executables:
        raise InstallError("更新包里没有可执行文件，请重新下载")

    named = [item for item in executables if item.name.lower() == expected_executable.lower()]
    if named:
        chosen = min(named, key=lambda item: (len(item.relative_to(staging).parts), str(item)))
    else:
        bundled = [
            item
            for item in executables
            if (item.parent / INTERNAL_DIR_NAME).is_dir()
            and item.parent.name != INTERNAL_DIR_NAME
        ]
        if not bundled:
            raise InstallError("更新包不是可替换的免安装目录（缺少 _internal），请手动解压更新包")
        chosen = min(bundled, key=lambda item: (len(item.relative_to(staging).parts), str(item)))

    payload = chosen.parent
    if not (payload / INTERNAL_DIR_NAME).is_dir():
        raise InstallError("更新包结构不完整（缺少 _internal），请重新下载")
    return payload, chosen.name


# ---------------------------------------------------------------- 计划与脚本


def _arguments_from_argv(argv: list[str]) -> tuple[tuple[str, ...], int]:
    port = 8110
    for index, arg in enumerate(argv):
        if arg == "--port" and index + 1 < len(argv):
            try:
                port = int(argv[index + 1])
            except ValueError:
                pass
        elif arg.startswith("--port="):
            try:
                port = int(arg.split("=", 1)[1])
            except ValueError:
                pass
    # 更新是用户在当前页面点的，旧页面在新进程启动后会自动恢复；再开一个浏览器
    # 标签页只会多出一份重复界面。新进程保持静默，托盘菜单仍可随时打开界面。
    arguments = list(argv)
    if "--no-browser" not in arguments:
        arguments.append("--no-browser")
    return tuple(arguments), port


def build_plan(
    *,
    version: str,
    directory: Path,
    payload: Path,
    executable: str,
    arguments: tuple[str, ...] | list[str] | None = None,
) -> InstallPlan:
    work = work_root(directory)
    args, port = _arguments_from_argv(list(sys.argv[1:] if arguments is None else arguments))
    return InstallPlan(
        version=version,
        parent_pid=os.getpid(),
        program_dir=str(directory),
        payload_dir=str(payload),
        work_dir=str(work),
        backup_dir=str(work / BACKUP_DIR_NAME),
        executable=executable,
        arguments=list(args),
        health_url=f"http://127.0.0.1:{port}/api/health",
        parent_name=Path(sys.executable).stem,
    )


def write_plan_files(plan: InstallPlan) -> Path:
    """写计划与脚本，返回脚本路径。脚本用 UTF-8 BOM，Windows PowerShell 5.1 才不会乱码。"""

    work = Path(plan.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    plan_path = work / PLAN_FILE_NAME
    plan_path.write_text(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    script_path = work / SCRIPT_FILE_NAME
    script_path.write_text(HELPER_SCRIPT, encoding="utf-8-sig")
    return script_path


def _launch_helper(script_path: Path) -> subprocess.Popen[bytes]:
    """启动后不再等待：脚本自己会等父进程退出，然后替换目录。"""

    executable = powershell_executable()
    if executable is None:
        raise InstallError("系统里找不到 Windows PowerShell，无法自动替换程序目录")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    return subprocess.Popen(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-PlanPath",
            str(script_path.parent / PLAN_FILE_NAME),
        ],
        cwd=str(script_path.parent),
        creationflags=flags,
        close_fds=True,
    )


def _result_path() -> Path | None:
    directory = program_dir()
    if directory is None:
        return None
    return work_root(directory) / RESULT_FILE_NAME


def read_install_result() -> InstallResult | None:
    """读取上一次自动更新的结果；损坏或缺失时当作没有。"""

    path = _result_path()
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status") or "")
    if status not in {"running", "succeeded", "failed"}:
        return None
    try:
        updated_at = float(payload.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    message = str(payload.get("message") or "")
    if status == "running" and (updated_at <= 0 or time.time() - updated_at > RUNNING_STALE_SECONDS):
        status = "failed"
        message = "上次自动更新没有完成（可能被中断），请重试或手动解压更新包。"
    return InstallResult(
        status=status,
        message=message,
        version=str(payload.get("version") or ""),
        updated_at=updated_at,
        log_path=str(payload.get("log_path") or path.with_name(LOG_FILE_NAME)),
    )


# ---------------------------------------------------------------- 安装任务


_install_lock = threading.Lock()
_install_task: _Task | None = None


def install_progress() -> InstallProgress | None:
    with _install_lock:
        task = _install_task
    if task is None:
        return None
    with task.lock:
        return replace(task.progress)


def start_install(package_path: Path, version: str) -> InstallProgress:
    """解压并在后台启动替换脚本；返回后接口立刻响应，进程随后退出。"""

    if not packaged():
        raise InstallError("开发模式下不会替换程序目录，请手动解压更新包。")
    supported, reason = install_supported()
    if not supported:
        raise InstallError(reason or "当前程序目录不支持自动替换，请手动解压更新包。")
    if not package_path.is_file():
        raise InstallError("更新包不存在，请重新下载")

    global _install_task
    with _install_lock:
        if _install_task is not None and _install_task.progress.active:
            raise InstallBusyError("已经在准备更新，请稍候")
        task = _Task(
            progress=InstallProgress(
                version=version,
                status="pending",
                message="正在准备更新",
                updated_at=time.time(),
            ),
            lock=threading.Lock(),
        )
        _install_task = task

    threading.Thread(
        target=_run_install,
        args=(task, package_path, version),
        name="update-install",
        daemon=True,
    ).start()
    snapshot = install_progress()
    assert snapshot is not None
    return snapshot


def _run_install(task: _Task, package_path: Path, version: str) -> None:
    directory = program_dir()
    if directory is None:
        task.report(status="failed", error="开发模式不能自动替换程序目录", message="无法自动更新")
        return
    work = work_root(directory)
    staging = work / STAGING_DIR_NAME
    try:
        _ensure_extract_space(package_path, directory.parent)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        task.report(status="extracting", percent=0, message="正在解压更新包")
        payload, executable = extract_package(
            package_path,
            staging,
            expected_executable=Path(sys.executable).name,
            progress=lambda percent: task.report(percent=percent, message="正在解压更新包"),
        )
        plan = build_plan(
            version=version,
            directory=directory,
            payload=payload,
            executable=executable,
        )
        script = write_plan_files(plan)
        # 旧结果先清掉：新一次尝试开始后，界面不该还挂着上一次的成功/失败
        (work / RESULT_FILE_NAME).unlink(missing_ok=True)
        task.report(
            status="launching",
            percent=100,
            message="正在退出并替换程序，稍后会自动重启",
        )
        _launch_helper(script)
        # 先把响应和页面提示送出去，再让服务收尾；脚本会等待本进程退出
        lifecycle.request_shutdown(delay=1.0)
    except Exception as exc:  # noqa: BLE001 任何失败都落到进度里，界面能重试
        logger.warning("准备自动更新失败：%s", exc)
        task.report(
            status="failed",
            message="自动更新准备失败",
            error=str(exc) if isinstance(exc, InstallError) else f"自动更新准备失败：{exc}",
        )


def _ensure_extract_space(package_path: Path, folder: Path) -> None:
    try:
        with zipfile.ZipFile(package_path) as archive:
            needed = sum(max(item.file_size, 0) for item in archive.infolist() if not item.is_dir())
        free = shutil.disk_usage(folder).free
    except (OSError, zipfile.BadZipFile):
        return
    required = int(needed * EXTRACT_HEADROOM)
    if required and free < required:
        raise InstallError(
            f"磁盘剩余空间不足：解压需要约 {required / 1024**2:.0f} MB，"
            f"当前只剩 {free / 1024**2:.0f} MB"
        )


# PowerShell 5.1 脚本。所有可变内容都在 plan.json 里，脚本本身可以完全静态化，
# 避免路径里的引号/中文被字符串拼接弄坏。
HELPER_SCRIPT = r"""param(
    [Parameter(Mandatory = $true)][string]$PlanPath
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$logPath = [string]$plan.log_path
$resultPath = [string]$plan.result_path
$backupDir = [string]$plan.backup_dir
$programDir = [string]$plan.program_dir
$payloadDir = [string]$plan.payload_dir
$exeName = [string]$plan.executable
$startedProcess = $null
$movedOld = $false

function Write-Log([string]$message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    try { Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8 } catch { }
}

function Write-Result([string]$status, [string]$message) {
    $payload = [ordered]@{
        status = $status
        message = $message
        version = [string]$plan.version
        updated_at = [DateTimeOffset]::Now.ToUnixTimeSeconds()
        log_path = $logPath
    }
    $json = $payload | ConvertTo-Json -Compress
    try {
        Set-Content -LiteralPath $resultPath -Value $json -Encoding UTF8
    } catch {
        Write-Log ('写结果文件失败：' + $_.Exception.Message)
    }
}

function Start-App([string]$folder) {
    $exe = Join-Path $folder $exeName
    if (-not (Test-Path -LiteralPath $exe)) {
        throw ('程序目录里找不到 ' + $exeName)
    }
    $arguments = @($plan.arguments)
    if ($arguments.Count -gt 0) {
        return Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $folder -WindowStyle Hidden -PassThru
    }
    return Start-Process -FilePath $exe -WorkingDirectory $folder -WindowStyle Hidden -PassThru
}

function Move-WithRetry([string]$source, [string]$destination) {
    # 杀毒软件、刚退出的子进程都可能短暂占用文件；Windows 的目录改名失败是瞬时的
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        try {
            Move-Item -LiteralPath $source -Destination $destination -Force
            return
        } catch {
            if ($attempt -eq 6) { throw }
            Start-Sleep -Seconds 2
        }
    }
}

function Remove-WithRetry([string]$path) {
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        if (-not (Test-Path -LiteralPath $path)) { return }
        try {
            Remove-Item -LiteralPath $path -Recurse -Force
            return
        } catch {
            if ($attempt -eq 6) { throw }
            Start-Sleep -Seconds 2
        }
    }
}

function Test-Health() {
    if ([string]::IsNullOrWhiteSpace([string]$plan.health_url)) {
        Start-Sleep -Seconds 3
        return $true
    }
    $deadline = (Get-Date).AddSeconds([int]$plan.health_seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri ([string]$plan.health_url) -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

try {
    Write-Result 'running' '正在替换程序目录'
    Write-Log ('开始自动更新到 v' + [string]$plan.version)

    if ([int]$plan.parent_pid -gt 0) {
        try {
            $parent = Get-Process -Id ([int]$plan.parent_pid) -ErrorAction Stop
            # PID 可能被系统复用：名字对不上就当作旧进程已经退出，绝不误杀
            if (-not [string]::IsNullOrWhiteSpace([string]$plan.parent_name) -and
                $parent.ProcessName -ne [string]$plan.parent_name) {
                throw 'PID 已被其他进程复用'
            }
            if (-not $parent.WaitForExit([int]$plan.wait_seconds * 1000)) {
                Write-Log '旧进程没有按时退出，强制结束'
                Stop-Process -Id ([int]$plan.parent_pid) -Force
                Start-Sleep -Seconds 2
            }
        } catch {
            # 进程已经退出（最常见的情况）或查不到，都继续替换
        }
    }
    Start-Sleep -Milliseconds 800

    if (-not (Test-Path -LiteralPath $programDir)) {
        throw '程序目录不见了，已停止更新'
    }
    $stagedExe = Join-Path $payloadDir $exeName
    if (-not (Test-Path -LiteralPath $stagedExe)) {
        throw ('暂存目录里找不到 ' + $exeName)
    }

    if (Test-Path -LiteralPath $backupDir) {
        Remove-WithRetry $backupDir
    }
    Write-Log '把旧版本移动到备份目录'
    Move-WithRetry $programDir $backupDir
    $movedOld = $true

    Write-Log '把新版本移动到程序目录'
    Move-WithRetry $payloadDir $programDir

    Write-Log '启动新版本'
    $startedProcess = Start-App $programDir
    if (-not (Test-Health)) {
        throw '新版本启动后没有通过健康检查'
    }
    Write-Log '新版本启动成功，自动更新完成'
    Write-Result 'succeeded' ('已更新到 v' + [string]$plan.version)
} catch {
    $message = $_.Exception.Message
    Write-Log ('自动更新失败：' + $message)
    try {
        if ($null -ne $startedProcess -and -not $startedProcess.HasExited) {
            Stop-Process -Id $startedProcess.Id -Force
        }
    } catch { }

    if ($movedOld) {
        try {
            if (Test-Path -LiteralPath $programDir) {
                Remove-WithRetry $programDir
            }
            Move-WithRetry $backupDir $programDir
            Write-Log '已回滚到旧版本'
        } catch {
            Write-Log ('回滚失败：' + $_.Exception.Message)
        }
    }

    Write-Result 'failed' $message
    try {
        Start-App $programDir | Out-Null
        Write-Log '已重新启动旧版本'
    } catch {
        Write-Log ('重新启动旧版本失败：' + $_.Exception.Message)
    }
}
"""
