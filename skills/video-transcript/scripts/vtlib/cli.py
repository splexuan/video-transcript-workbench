"""技能的命令行流程。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from .config import SKILL_ROOT, get_config
from .errors import (
    FallbackError,
    PlatformError,
    SkillError,
    UnsupportedSourceError,
)
from .fallback import FallbackMedia, download as download_fallback
from .fallback import resolve as resolve_fallback
from .media import MediaToolError, convert_to_wav, find_tool, probe_media
from .models import (
    ModelError,
    ensure_model,
    install as install_model,
    require_spec,
    status as model_status,
)
from .platforms import (
    FALLBACK_ONLY_PLATFORMS,
    SUPPORTED_PLATFORMS,
    detect_platform,
    extract_source_url,
    platform_label,
)
from .resolve import download_audio, expand_wechat_short_link, resolve_video
from .text import simplify_chunks
from .transcribe import transcribe
from .types import DirectMedia, MediaInfo, TranscriptChunk

log = logging.getLogger("video-transcript")
DEFAULT_MODEL = "sensevoice-small"


# ---------------------------------------------------------------------------
# 环境检查与模型安装
# ---------------------------------------------------------------------------


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def run_check() -> int:
    cfg = get_config()
    model_id = DEFAULT_MODEL
    ffmpeg = find_tool("ffmpeg", cfg.ffmpeg_dir)
    ffprobe = find_tool("ffprobe", cfg.ffmpeg_dir)
    dependencies = {
        "yt_dlp": _module_available("yt_dlp"),
        "sherpa_onnx": _module_available("sherpa_onnx"),
        "numpy": _module_available("numpy"),
        "opencc": _module_available("opencc"),
    }
    model = model_status(model_id)
    missing: list[str] = []
    warnings: list[str] = []
    if not ffmpeg:
        missing.append("FFmpeg")
    for name, available in dependencies.items():
        if not available:
            missing.append(name)
    if not model["ready"]:
        missing.append(f"模型 {model_id}")
    if not ffprobe:
        warnings.append(
            "未找到 FFprobe：本地音视频文件的标题/时长探测不可用，URL 提取不受影响"
        )
    ok = not missing
    result = {
        "ok": ok,
        "python": sys.version.split()[0],
        "skill_dir": str(SKILL_ROOT),
        "models_dir": str(cfg.models_dir),
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "dependencies": dependencies,
        "model": model,
        "fallback_api_key": bool(cfg.fallback_api_key),
        "cookies_configured": sorted(cfg.cookies),
    }
    if not cfg.fallback_api_key:
        result["fallback_key_register_url"] = f"{cfg.fallback_base_url}/auth/register"
        result["fallback_key_hint"] = (
            "视频号必须有兜底 Key；打开上面的地址用邮箱注册，"
            "登录后在「密钥管理」新建 Key，填入 config.json 的 fallback_api_key。"
        )
    if warnings:
        result["warnings"] = warnings
    if not ok:
        result["hint"] = (
            f"缺少：{'、'.join(missing)}。先执行 python -m pip install -r requirements.txt；"
            "FFmpeg 可放进技能目录的 vendor/ffmpeg/；模型运行 --install-model"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def run_install(model_id: str) -> int:
    spec = require_spec(model_id)
    log.info("准备 %s（约 %s）", spec.name, spec.approx_label)
    last_percent = -1

    def progress(done: int, total: int, filename: str) -> None:
        nonlocal last_percent
        percent = int(done / max(total, 1) * 100)
        if percent >= last_percent + 2:
            last_percent = percent
            log.info("下载中 %d%% %s", percent, filename)

    directory = install_model(model_id, progress)
    print(
        json.dumps(
            {
                "ok": True,
                "model": spec.id,
                "name": spec.name,
                "path": str(directory),
                "message": "模型安装完成",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _is_local_file(raw: str) -> bool:
    if raw.startswith(("http://", "https://")):
        return False
    try:
        return Path(raw).expanduser().is_file()
    except OSError:
        return False


def _resolve_with_fallback(
    source: str,
    platform: str,
    cookies_file: Path | None,
    api_key: str | None,
) -> tuple[DirectMedia, FallbackMedia | None, str]:
    """本机解析优先；失败且有 Key 时走兜底接口。"""

    cfg = get_config()
    if platform in FALLBACK_ONLY_PLATFORMS:
        media = resolve_fallback(source, api_key, platform, cfg.fallback_base_url)
        return DirectMedia(info=media.to_media_info(platform)), media, "fallback"

    try:
        return resolve_video(source, platform, cookies_file), None, "platform"
    except PlatformError as exc:
        if not api_key:
            raise
        log.warning("本机解析失败：%s", exc)
        try:
            media = resolve_fallback(source, api_key, platform, cfg.fallback_base_url)
        except FallbackError as fallback_exc:
            raise PlatformError(
                f"{exc}；兜底解析也没成功：{fallback_exc}", exc.code
            ) from exc
        return DirectMedia(info=media.to_media_info(platform)), media, "fallback"


def _obtain_media(
    media: DirectMedia,
    fallback_media: FallbackMedia | None,
    source: str,
    platform: str,
    cookies_file: Path | None,
    api_key: str | None,
    workspace: Path,
) -> tuple[Path, str]:
    """返回可转码的媒体文件与最终解析方式。"""

    if fallback_media is not None:
        return download_fallback(fallback_media, workspace), "fallback"

    try:
        return download_audio(media, source, platform, workspace, cookies_file), "platform"
    except PlatformError as exc:
        if not api_key:
            raise
        log.warning("音轨下载失败：%s", exc)
        cfg = get_config()
        try:
            fallback_media = resolve_fallback(
                source, api_key, platform, cfg.fallback_base_url
            )
            return download_fallback(fallback_media, workspace), "fallback"
        except FallbackError as fallback_exc:
            raise PlatformError(
                f"{exc}；兜底下载也没成功：{fallback_exc}", exc.code
            ) from exc


def _transcribe(wav_path: Path) -> tuple[object, list[TranscriptChunk], float]:
    spec, model_dir = ensure_model(DEFAULT_MODEL)
    log.info("正在使用 %s 识别", spec.name)

    def progress(current: int, total: int) -> None:
        log.info("识别中 %d/%d 段", current, total)

    chunks, duration = transcribe(wav_path, model_dir, progress)
    return spec, chunks, duration


def _build_result(
    *,
    info: MediaInfo,
    source: str,
    resolved_by: str,
    spec: object,
    chunks: list[TranscriptChunk],
    duration: float | None,
    include_segments: bool,
) -> dict:
    chunks = simplify_chunks(chunks)
    lines = [chunk.text.strip() for chunk in chunks if chunk.text and chunk.text.strip()]
    transcript = "\n".join(lines)
    if not transcript:
        raise SkillError("没有识别到可用语音，这条作品可能没有说话内容", "NO_SPEECH")

    label = "本地文件" if info.platform == "local" else platform_label(info.platform)
    result: dict[str, object] = {
        "ok": True,
        "title": info.title or "未命名视频",
        "platform": info.platform,
        "platform_label": label,
        "uploader": info.uploader,
        "duration_seconds": round(duration, 1) if duration else info.duration_seconds,
        "source": source,
        "resolved_by": resolved_by,
        "model": getattr(spec, "name", str(spec)),
        "word_count": sum(len(line) for line in lines),
        "transcript": transcript,
    }
    if include_segments:
        result["segments"] = [
            {
                "start_ms": chunk.start_ms,
                "end_ms": chunk.end_ms,
                "text": chunk.text.strip(),
            }
            for chunk in chunks
            if chunk.text and chunk.text.strip()
        ]
    return result


def _extract_local(
    path: Path,
    workspace: Path,
    keep_media: bool,
    include_segments: bool,
) -> dict:
    cfg = get_config()
    title, duration = probe_media(path, cfg.ffmpeg_dir)
    log.info("正在提取本地音轨")
    wav_path = convert_to_wav(path, workspace / "audio.wav", cfg.ffmpeg_dir)
    spec, chunks, duration = _transcribe(wav_path)
    result = _build_result(
        info=MediaInfo(title=title, platform="local", duration_seconds=duration),
        source=str(path),
        resolved_by="local",
        spec=spec,
        chunks=chunks,
        duration=duration,
        include_segments=include_segments,
    )
    if keep_media:
        result["media_dir"] = str(workspace)
    return result


def _extract_url(raw: str, args: argparse.Namespace, workspace: Path) -> dict:
    cfg = get_config()
    source = extract_source_url(raw)
    platform = args.platform or detect_platform(source)
    if platform not in SUPPORTED_PLATFORMS:
        raise UnsupportedSourceError(
            "暂不支持这个链接。当前支持 B站、抖音、快手、小红书、视频号，"
            "也可以直接传入本地音视频文件路径。"
        )
    if platform == "wechat":
        # 视频号分享短链必须先换成 channels.weixin.qq.com 的真实地址
        source = expand_wechat_short_link(source)
    log.info("平台：%s", platform_label(platform))
    log.info("链接：%s", source)

    api_key = args.fallback_key or cfg.fallback_api_key
    cookies_file = (
        Path(args.cookie_file).expanduser()
        if args.cookie_file
        else cfg.cookie_for(platform)
    )

    log.info("正在解析链接")
    media, fallback_media, resolved_by = _resolve_with_fallback(
        source, platform, cookies_file, api_key
    )
    info = media.info
    log.info("作品：%s（%s）", info.title, info.uploader or "未知作者")

    log.info("正在获取音轨")
    media_path, download_source = _obtain_media(
        media, fallback_media, source, platform, cookies_file, api_key, workspace
    )
    if download_source == "fallback":
        resolved_by = "fallback"

    log.info("正在转换音轨")
    wav_path = convert_to_wav(media_path, workspace / "audio.wav", cfg.ffmpeg_dir)
    spec, chunks, duration = _transcribe(wav_path)
    result = _build_result(
        info=info,
        source=source,
        resolved_by=resolved_by,
        spec=spec,
        chunks=chunks,
        duration=duration,
        include_segments=args.segments,
    )
    if args.keep_audio:
        result["media_dir"] = str(workspace)
    return result


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _format_duration(seconds: object) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "未知"
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _text_block(result: dict, plain: bool = False, include_path: bool = False) -> str:
    if plain:
        return str(result["transcript"])
    header = [
        str(result["title"]),
        f"平台：{result['platform_label']}    作者：{result['uploader'] or '未知'}    "
        f"时长：{_format_duration(result.get('duration_seconds'))}    "
        f"字数：{result['word_count']}",
        f"来源：{result['source']}",
    ]
    if result.get("resolved_by") == "fallback":
        header.append("解析方式：兜底接口")
    if include_path and result.get("output_path"):
        header.append(f"文件：{result['output_path']}")
    return "\n".join(header) + "\n\n" + str(result["transcript"])


def _json_block(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def _render(
    result: dict,
    output_format: str,
    plain: bool = False,
    include_path: bool = False,
) -> None:
    print(
        _text_block(result, plain, include_path)
        if output_format == "text"
        else _json_block(result)
    )


_INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_filename(title: str) -> str:
    cleaned = _INVALID_FILENAME.sub("_", title).strip(" .")
    return (cleaned[:80] or "video-transcript").rstrip(" .")


def _output_format(
    args: argparse.Namespace, output_target: Path | None = None
) -> str:
    """显式 --format 优先；落文件时默认 text，完全不落文件时默认 json。"""

    if args.format:
        return args.format
    if output_target is None:
        return "json"
    suffix = Path(output_target).suffix.lower()
    if suffix == ".json":
        return "json"
    return "text"


def _resolve_output_path(
    requested: str | Path,
    result: dict,
    output_format: str,
    *,
    directory: bool = False,
) -> Path:
    """确定输出文件：给目录就按标题命名，没扩展名就按格式补 .txt / .json。"""

    suffix = ".json" if output_format == "json" else ".txt"
    path = Path(requested).expanduser()
    if directory or path.is_dir() or (
        isinstance(requested, str) and requested.endswith(("/", "\\"))
    ):
        path.mkdir(parents=True, exist_ok=True)
        path = path / (_safe_filename(str(result.get("title") or "")) + suffix)
    else:
        if not path.suffix:
            path = path.with_suffix(suffix)
        path.parent.mkdir(parents=True, exist_ok=True)

    # 不覆盖已有文件：同名时依次加 -2、-3
    if path.exists():
        for index in range(2, 1000):
            candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
            if not candidate.exists():
                return candidate
    return path


def _write_output(
    path: Path, result: dict, output_format: str, plain: bool = False
) -> None:
    content = (
        _text_block(result, plain)
        if output_format == "text"
        else _json_block(result)
    )
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _classify(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, SkillError):
        return exc.code, str(exc)
    if isinstance(exc, ModelError):
        return "MODEL_ERROR", str(exc)
    if isinstance(exc, MediaToolError):
        return "MEDIA_TOOL_ERROR", str(exc)
    return "PROCESSING_ERROR", str(exc) or "处理失败"


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(message)s",
    )
    # httpx 默认把每个请求打到 INFO，对 Agent 的进度输出是噪音
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        if args.check:
            return run_check()
        if args.install_model:
            return run_install(args.install_model or DEFAULT_MODEL)
        if not args.source:
            raise SkillError("请提供视频链接或本地文件路径", "USAGE_ERROR")

        cfg = get_config()
        if not find_tool("ffmpeg", cfg.ffmpeg_dir):
            raise MediaToolError(
                "未找到 FFmpeg。请安装并加入 PATH，或放到技能目录的 "
                "vendor/ffmpeg/，或用 VTS_FFMPEG_DIR 指定目录。"
            )

        workspace = Path(tempfile.mkdtemp(prefix="video-transcript-"))
        try:
            source = args.source.strip().strip("\"'“”‘’")
            if _is_local_file(source):
                result = _extract_local(
                    Path(source).expanduser(),
                    workspace,
                    args.keep_audio,
                    args.segments,
                )
            else:
                result = _extract_url(args.source, args, workspace)
        finally:
            if not args.keep_audio:
                shutil.rmtree(workspace, ignore_errors=True)
            else:
                log.info("临时文件已保留：%s", workspace)

        if args.no_output:
            output_target: Path | None = None
        elif args.output:
            output_target = Path(args.output).expanduser()
        else:
            # 默认落一份 txt，Agent 不必每次显式指定 -o
            output_target = cfg.output_dir or (Path.cwd() / "output")

        output_format = _output_format(args, output_target)
        if output_target is not None:
            try:
                output_path = _resolve_output_path(
                    output_target,
                    result,
                    output_format,
                    directory=args.output is None,
                )
                result["output_path"] = str(output_path)
                _write_output(output_path, result, output_format, args.plain)
                log.info("文案已保存：%s", output_path)
            except OSError as exc:
                if args.output:
                    raise
                # 默认目录不可写时不能把已经识别出来的文案也弄丢
                result.pop("output_path", None)
                log.warning("文案文件写入失败：%s；改为只打印结果", exc)

        _render(result, output_format, args.plain, include_path=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转成结构化错误
        code, message = _classify(exc)
        if args.verbose:
            traceback.print_exc()
        print(
            json.dumps(
                {"ok": False, "code": code, "message": message},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="视频链接 → 口播文案（本机解析优先，失败走兜底接口，音频用完即删）"
    )
    parser.add_argument("source", nargs="?", help="视频链接或本地音视频文件路径")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        help="输出格式；不指定时默认 text（落 .txt），--no-output 时默认 json",
    )
    parser.add_argument(
        "--platform",
        choices=("bilibili", "douyin", "kuaishou", "xiaohongshu", "wechat"),
        help="手动指定平台（链接识别失败时使用）",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "-o",
        "--output",
        help="指定输出文件或目录；默认写到 ./output/<标题>.txt",
    )
    output_group.add_argument(
        "--no-output",
        action="store_true",
        help="不写文件，只打印结果（默认会生成 txt）",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="text 输出/文件只保留正文，不带标题与来源信息",
    )
    parser.add_argument("--fallback-key", help="兜底解析 API Key（默认读 config.json 或环境变量）")
    parser.add_argument("--cookie-file", help="Netscape cookies.txt 路径（可选）")
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="保留本次下载/转码的临时文件（默认识别完立即删除）",
    )
    parser.add_argument("--segments", action="store_true", help="在 JSON 中附带带时间码的分段")
    parser.add_argument("--check", action="store_true", help="检查运行环境后退出")
    parser.add_argument(
        "--install-model",
        nargs="?",
        const=DEFAULT_MODEL,
        metavar="MODEL_ID",
        help="下载 SenseVoice 模型后退出（可省略模型名）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志与完整异常栈")
    return parser
