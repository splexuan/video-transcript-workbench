# -*- mode: python ; coding: utf-8 -*-
"""文案工作台打包配置。

用法（在 backend 目录下）：
    1. 先在 frontend 目录构建前端：npm run build
    2. pyinstaller 文案工作台.spec --noconfirm

产物：dist/文案工作台/ 目录，双击其中的 exe 启动。

FFmpeg：优先取 backend/vendor/ffmpeg/ 下的 ffmpeg.exe + ffprobe.exe（想固定版本就放这里），
找不到时回退到打包机 PATH 上的同名工具。产物把它们放进 PyInstaller 的资源目录，
运行时由 `media.find_tool` 从 `sys._MEIPASS` 取用，用户不需要另行安装 FFmpeg。
排除 nvidia CUDA 库（约 2GB），显卡加速维持运行时按需下载。
"""

import importlib.util
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

SPEC_DIR = Path(SPECPATH)
ROOT = SPEC_DIR.parent
# 应用图标：assets/icon.svg 是矢量源文件，图标改动后由 assets/build_icon.py 重新导出。
# exe 用它当文件图标，运行时也用它画系统托盘图标（run.py 从资源目录取）。
ICON = ROOT / "assets" / "icon.ico"


def collect_ffmpeg() -> list[tuple[str, str]]:
    """收集 ffmpeg / ffprobe 可执行文件，让打包产物自带转码能力。

    终点目录写 "."：PyInstaller 会放进资源目录，与应用其它资源同级。
    """

    vendor = SPEC_DIR / "vendor" / "ffmpeg"
    collected: list[tuple[str, str]] = []
    missing: list[str] = []
    for name in ("ffmpeg", "ffprobe"):
        source = next(
            (
                Path(item)
                for item in (vendor / f"{name}.exe", shutil.which(name))
                if item and Path(item).is_file()
            ),
            None,
        )
        if source is None:
            missing.append(name)
            continue
        collected.append((str(source), "."))
    if missing:
        print(
            f"[打包提示] 没找到 {'、'.join(missing)}：产物将依赖用户自行安装 FFmpeg。"
            f"把它们放进 {vendor} 后重新打包即可自带。"
        )
    return collected


datas = [
    (str(ROOT / "frontend" / "dist"), "frontend_dist"),
]
if ICON.is_file():
    # 托盘图标要能被运行时读到，所以放进资源目录（不是只作为 exe 的文件图标）
    datas.append((str(ICON), "."))
else:
    print(f"[打包提示] 没找到 {ICON}：产物仍可运行，但系统托盘会退化成默认图标。")
binaries = collect_ffmpeg()
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

# 这些包都是运行时才导入的（yt-dlp 的 extractor 动态加载，识别引擎按模式选择），
# 静态分析看不到，必须整包收集，否则打包版会「少功能」而不是报错。
# faster-whisper / ctranslate2 缺一不可：少了它们「精准时间轴」在 exe 里无法使用。
for package in ("yt_dlp", "sherpa_onnx", "anyio", "faster_whisper", "ctranslate2"):
    if importlib.util.find_spec(package) is None:
        print(
            f"[打包提示] 当前环境没有 {package}：产物里不会有对应能力"
            f"（精准时间轴需要 faster-whisper 与 ctranslate2）"
        )
        continue
    collected_datas, collected_binaries, collected_hidden = collect_all(package)
    datas += collected_datas
    binaries += collected_binaries
    hiddenimports += collected_hidden

# PyAV 自带的 ffmpeg 动态库（部分平台下载用）
datas += collect_data_files("av")

# 系统托盘：pystray 的平台后端与 Pillow 都是运行时才导入的，显式带上更稳
if importlib.util.find_spec("pystray") is None or importlib.util.find_spec("PIL") is None:
    print(
        "[打包提示] 当前环境没有 pystray 或 Pillow：产物不会常驻系统托盘，"
        "双击后既没有窗口也没有托盘入口（只能从任务管理器结束）。"
    )
else:
    hiddenimports += ["pystray", "pystray._win32", "PIL.Image", "PIL.ImageDraw"]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GPU 加速的 CUDA 库：2GB，维持运行时按需下载
        "nvidia",
        "nvidia_cudnn_cu12",
        "nvidia_cublas_cu12",
        "nvidia_cuda_runtime_cu12",
        "nvidia_cufft_cu12",
        "nvidia_curand_cu12",
        "nvidia_cusolver_cu12",
        "nvidia_cusparse_cu12",
        "nvidia_nccl_cu12",
        "torch",
        "tensorflow",
        # onnxruntime 只用于 faster-whisper 的 VAD 预处理（默认关闭），且在本机
        # 的 PyInstaller 子进程里导入即崩；代码已容错缺失，打包时整包剔除
        "onnxruntime",
        # 开发工具与无关大件
        "tkinter",
        "PySide6",
        "matplotlib",
        "jupyter",
        "IPython",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="文案工作台",
    icon=str(ICON) if ICON.is_file() else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 不弹终端窗口：双击后只出现系统托盘图标，日志写到 <数据目录>/logs/app.log
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="文案工作台",
)
