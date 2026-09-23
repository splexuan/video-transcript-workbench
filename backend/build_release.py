"""打包发行包：把 PyInstaller 产物压成可以直接上传到 Releases 的 zip。

用法（在 backend 目录下，用虚拟环境的 python；build.bat 会自动调用）：

    python build_release.py                 # 打包 dist/文案工作台
    python build_release.py --level 1       # 只求快，压缩率低一些
    python build_release.py --output D:\\release
    python build_release.py --print-version # 只打印版本号（给批处理取用）

产物（默认在仓库根目录的 release/ 下，版本号取自 app/config.py）：

    video-transcript-workbench-v<版本>-win64.zip
    video-transcript-workbench-v<版本>-win64.zip.sha256

三个约定不是随便定的：

- **资产名必须以 `-win64.zip` 结尾**：工作台的「检查更新」只认带 win64 的 zip
  （见 `app/infrastructure/updater.py` 的 select_asset），名字错了用户在界面里就下不到。
- **版本号从 `app/config.py` 读**：界面显示的版本、OpenAPI 版本、发行包文件名必须是
  同一个来源，手动改名迟早对不上。
- **zip 里保留 `文案工作台/` 这一层目录**：与历史发行包一致（解压后双击目录里的 exe），
  更新时用户解压覆盖程序目录即可。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent
# 与 文案工作台.spec 里 EXE/COLLECT 的 name 保持一致：产物目录名、zip 内层目录名都用它
APP_NAME = "文案工作台"
DEFAULT_OUTPUT = ROOT / "release"
# 每压缩这么多文件报一次进度：450 MB 的产物要跑一两分钟，没输出会让人以为卡死
PROGRESS_FILES = 200
# 压缩后大约是原来的四成上下，按一半估空间，留够余量再开工
SPACE_RATIO = 0.5


def human(size: float) -> str:
    units = ["B", "KB", "MB", "GB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.0f} {units[index]}" if index == 0 else f"{size:.1f} {units[index]}"


def read_version() -> str:
    """版本号从应用配置读，避免和界面显示的版本对不上。"""

    sys.path.insert(0, str(BACKEND))
    try:
        from app.config import settings
    except ImportError as exc:  # pragma: no cover 只在没用虚拟环境时发生
        raise SystemExit(f"[错误] 读不到应用版本，请用 backend 虚拟环境的 python 运行：{exc}") from exc
    return settings.app_version


def zip_name(version: str) -> str:
    return f"video-transcript-workbench-v{version}-win64.zip"


def collect_files(app_dir: Path) -> list[Path]:
    return sorted(item for item in app_dir.rglob("*") if item.is_file())


def check_space(output: Path, total_bytes: int) -> None:
    try:
        free = shutil.disk_usage(output).free
    except OSError:
        return
    need = int(total_bytes * SPACE_RATIO)
    if free < need:
        raise SystemExit(
            f"[错误] 磁盘剩余空间不足：{output.drive} 还有 {human(free)}，"
            f"压缩这个产物大约还要 {human(need)}"
        )


def compress(app_dir: Path, target: Path, files: list[Path], total_bytes: int, level: int) -> None:
    done_bytes = 0
    started = time.monotonic()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as archive:
        for index, item in enumerate(files, start=1):
            archive.write(item, arcname=f"{APP_NAME}/{item.relative_to(app_dir).as_posix()}")
            done_bytes += item.stat().st_size
            if index % PROGRESS_FILES == 0 or index == len(files):
                elapsed = time.monotonic() - started
                print(
                    f"    已压缩 {index}/{len(files)} 个文件 · "
                    f"{human(done_bytes)}/{human(total_bytes)} · {elapsed:.0f}s",
                    flush=True,
                )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="把打包产物压成发行包 zip")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="发行包输出目录（默认仓库根的 release/）")
    parser.add_argument("--level", type=int, default=6, help="zip 压缩级别 0-9，默认 6")
    parser.add_argument("--print-version", action="store_true", help="只打印版本号后退出")
    args = parser.parse_args()

    version = read_version()
    if args.print_version:
        print(version)
        return 0

    app_dir = BACKEND / "dist" / APP_NAME
    executable = app_dir / f"{APP_NAME}.exe"
    if not executable.is_file():
        print(f"[错误] 找不到打包产物：{executable}")
        print("       先运行 build.bat（或 pyinstaller 文案工作台.spec --noconfirm）再打包。")
        return 1

    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / zip_name(version)

    files = collect_files(app_dir)
    total_bytes = sum(item.stat().st_size for item in files)
    if not files:
        print(f"[错误] 产物目录是空的：{app_dir}")
        return 1

    print(f"    版本：v{version}")
    print(f"    来源：{app_dir}（{len(files)} 个文件 · {human(total_bytes)}）")
    print(f"    产物：{target}")
    if target.exists():
        print("    已存在同名发行包，将覆盖")
    check_space(output, total_bytes)

    # 先写临时文件再改名：中途失败（磁盘满、Ctrl+C）不会留下一个像是正常的半截 zip
    partial = target.with_name(f"{target.name}.part")
    partial.unlink(missing_ok=True)
    started = time.monotonic()
    try:
        compress(app_dir, partial, files, total_bytes, min(9, max(0, args.level)))
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    digest = sha256_of(target)
    checksum = Path(f"{target}.sha256")
    checksum.write_text(f"{digest}  {target.name}\n", encoding="utf-8")

    size = target.stat().st_size
    ratio = size * 100 / total_bytes if total_bytes else 0
    print()
    print("打包完成：")
    print(f"    发行包：{target}")
    print(f"    大小：{human(size)}（压缩前 {human(total_bytes)}，{ratio:.0f}%）· 用时 {time.monotonic() - started:.0f}s")
    print(f"    校验：{checksum}（sha256 {digest}）")
    print()
    print("上传到 Releases 时保持这个文件名，工作台的「检查更新」才认得出：")
    print(f'    gh release create v{version} "{target}" --title "文案工作台 v{version}" --notes "…"')
    print("    发版前确认三处版本号一致：backend/app/config.py、backend/pyproject.toml、frontend/package.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
