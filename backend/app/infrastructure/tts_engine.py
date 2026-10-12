"""语音合成引擎：用 sherpa-onnx 跑 Kokoro（ONNX），把文案读成 24kHz 音频。

和识别共用同一个运行时（sherpa-onnx）与同一套模型管理（下载 / 校验 / 删除），
不引入 PyTorch、不依赖 CUDA。

单实例线程数实测（12 核 CPU，Kokoro int8 v1_1，10.7 秒音频）：
    2 线程 RTF 0.91 / 4 线程 0.93 / 8 线程 1.01 / 16 线程 2.26
所以每个实例只给 2~4 线程——这是它在这个架构上的最优点，给多了反而更慢。

既然一个实例吃不满多核，提速就不靠加线程，而是靠「多实例并行」：把句子分给几个
实例同时合成，每个实例仍是 2~4 线程（见 lanes / _threads）。口播稿通常一二十句，
摊到 3~4 个实例上，墙钟时间大致按实例数下降；短文本（一两句）仍走单实例，
不为它多加载几个几百 MB 的模型。
"""
from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from app.domain import EngineUnavailableError
from app.infrastructure.media import find_tool
from app.infrastructure.model_catalog import require_spec
from app.infrastructure.model_store import is_complete, spec_dir

logger = logging.getLogger(__name__)

ENGINE = "tts"
DEFAULT_MODEL_ID = "kokoro-int8-multi-lang-v1_1"
# zf_001：中文女声的第一个音色，作为新用户的默认
DEFAULT_VOICE = 3
# 单次合成上限。按 RTF≈0.93 估算，2000 字≈8 分钟音频；多实例并行后实际等待大概是
# 它的 1/3 左右（见 _lanes）。再长就该考虑分段出稿，而不是让一个请求挂在那里等。
MAX_CHARS = 2000
DEFAULT_SPEED = 1.0
SPEED_RANGE = (0.6, 1.6)
SENTENCE_GAP_SECONDS = 0.22
# 并行实例数上限。每个实例要独立加载一份模型（实测常驻约 240MB），所以拿内存换速度：
# 12 核实测（48 秒音频）1 实例 51.4s、2 实例 43.1s、3 实例 29.2s、4 实例 26.8s——
# 实例数比「每实例多少线程」管用得多，4 个之后才明显不划算（合计约 1GB）。
MAX_LANES = 4
# 太短的碎句并进上一句：逐句合成会在句间插静音，碎句多了口气就断了
MIN_SENTENCE_CHARS = 8

# Kokoro v1.1 的说话人（官方模型卡：0-2 英文、3-57 中文女声、58-102 中文男声）。
# 顺序即 sid —— 拼接后的下标就是传给 tts.generate 的 sid，改动顺序会让音色错位。
_ENGLISH = ("af_maple", "af_sol", "bf_vale")
_ZH_FEMALE = (
    "zf_001", "zf_002", "zf_003", "zf_004", "zf_005", "zf_006", "zf_007", "zf_008",
    "zf_017", "zf_018", "zf_019", "zf_021", "zf_022", "zf_023", "zf_024", "zf_026",
    "zf_027", "zf_028", "zf_032", "zf_036", "zf_038", "zf_039", "zf_040", "zf_042",
    "zf_043", "zf_044", "zf_046", "zf_047", "zf_048", "zf_049", "zf_051", "zf_059",
    "zf_060", "zf_067", "zf_070", "zf_071", "zf_072", "zf_073", "zf_074", "zf_075",
    "zf_076", "zf_077", "zf_078", "zf_079", "zf_083", "zf_084", "zf_085", "zf_086",
    "zf_087", "zf_088", "zf_090", "zf_092", "zf_093", "zf_094", "zf_099",
)
_ZH_MALE = (
    "zm_009", "zm_010", "zm_011", "zm_012", "zm_013", "zm_014", "zm_015", "zm_016",
    "zm_020", "zm_025", "zm_029", "zm_030", "zm_031", "zm_033", "zm_034", "zm_035",
    "zm_037", "zm_041", "zm_045", "zm_050", "zm_052", "zm_053", "zm_054", "zm_055",
    "zm_056", "zm_057", "zm_058", "zm_061", "zm_062", "zm_063", "zm_064", "zm_065",
    "zm_066", "zm_068", "zm_069", "zm_080", "zm_081", "zm_082", "zm_089", "zm_091",
    "zm_095", "zm_096", "zm_097", "zm_098", "zm_100",
)
VOICES: tuple[str, ...] = _ENGLISH + _ZH_FEMALE + _ZH_MALE

VOICE_GROUPS: tuple[tuple[str, int, int], ...] = (
    ("中文女声", len(_ENGLISH), len(_ENGLISH) + len(_ZH_FEMALE)),
    ("中文男声", len(_ENGLISH) + len(_ZH_FEMALE), len(VOICES)),
    ("英文", 0, len(_ENGLISH)),
)

# emoji 与零宽字符：小红书文案里很常见，合成时会变成未知 token（念不出还可能报错），先清掉
_NOISE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u20E3]")
_SENTENCE = re.compile(r"[^。！？!?；;\n…]+[。！？!?；;…]*")

_lock = threading.Lock()
# 按「模型 id + 并行槽位」缓存：槽位之间必须是彼此独立的实例，
# 同一个 OfflineTts 不保证能被并发调用（见 get_tts）。
_cache: dict[tuple[str, int], tuple[object, str]] = {}


def lanes(cores: int | None = None) -> int:
    """并行实例数（也用于给界面估算等待时间）：每实例大约 3 线程，最多 MAX_LANES 个。

    12 核 → 4 个（每实例 3 线程）；8 核 → 2 个（每实例 4 线程）；4 核及以下 → 1 个，
    也就是回到原来的单实例行为。核数少的机器不会因为并行而更慢。
    """

    cores = cores or os.cpu_count() or 4
    return max(1, min(MAX_LANES, cores // 3))


def _threads(count: int | None = None, cores: int | None = None) -> int:
    """每个实例的线程数：2~4 是 Kokoro 在 CPU 上的最优区间（给多了反而慢，见模块注释）。"""

    cores = cores or os.cpu_count() or 4
    return max(2, min(4, cores // max(count or lanes(cores), 1)))


def voice_groups() -> list[dict[str, object]]:
    """给界面用的音色分组（每组带 sid 与名字，顺序与 VOICES 一致）。"""

    return [
        {
            "label": label,
            "voices": [{"sid": sid, "name": VOICES[sid]} for sid in range(start, end)],
        }
        for label, start, end in VOICE_GROUPS
    ]


def normalize_text(text: str) -> str:
    """清掉 emoji、压缩空白；空文本是调用方的错误，这里返回空串由上层报错。"""

    cleaned = _NOISE.sub("", text or "")
    cleaned = re.sub(r"[ \t\u3000]+", " ", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()


def split_sentences(text: str, min_chars: int = MIN_SENTENCE_CHARS) -> list[str]:
    """按句末标点切分，过短的碎句并进上一句。"""

    merged: list[str] = []
    for piece in (item.strip() for item in _SENTENCE.findall(text)):
        if not piece:
            continue
        if merged and len(merged[-1]) < min_chars:
            merged[-1] += piece
        else:
            merged.append(piece)
    return merged or [text]


def model_directory(model_id: str) -> Path:
    """语音模型目录；没装好时给出可操作的提示（去模型管理下载）。"""

    spec = require_spec(model_id)
    directory = spec_dir(spec)
    if not is_complete(spec, directory):
        raise EngineUnavailableError(f"「{spec.name}」还没安装，请先到「模型管理」下载")
    return directory


def get_tts(model_id: str = DEFAULT_MODEL_ID, lane: int = 0):
    """按「模型 id + 槽位」缓存的实例；线程数变化时才重建。

    槽位只是为了并行合成：每个槽位一个独立实例，各由一个线程独占使用。
    非并行场景（lane=0）就是原来那个单例。
    """

    try:
        import sherpa_onnx
    except ImportError:  # pragma: no cover - 依赖缺失属于环境问题
        raise EngineUnavailableError("缺少 sherpa-onnx，无法使用语音合成") from None

    directory = model_directory(model_id)
    threads = _threads()
    key = f"{directory}:{threads}"
    # 注意：_lock 覆盖「查缓存 + 构造 + 写缓存」整段，中间不能再取一次锁——
    # threading.Lock 不可重入，嵌套 with 会直接死锁（构造一次要 1 秒多，串行化可以接受）。
    with _lock:
        cached = _cache.get((model_id, lane))
        if cached is not None and cached[1] == key:
            return cached[0]

        model_path = next(
            (directory / name for name in ("model.int8.onnx", "model.onnx") if (directory / name).is_file()),
            None,
        )
        if model_path is None:
            raise EngineUnavailableError("语音模型缺少 onnx 文件，请重新下载")

        # 中文 + 美式英文两份就够：三份一起加载时上游会在 stderr 刷大量重复词条警告
        lexicons = [directory / name for name in ("lexicon-zh.txt", "lexicon-us-en.txt")]
        rule_fsts = [directory / name for name in ("date-zh.fst", "number-zh.fst", "phone-zh.fst")]
        data_dir = directory / "espeak-ng-data"
        dict_dir = directory / "dict"

        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(model_path),
                    voices=str(directory / "voices.bin"),
                    tokens=str(directory / "tokens.txt"),
                    lexicon=",".join(str(item) for item in lexicons if item.is_file()),
                    data_dir=str(data_dir),
                    dict_dir=str(dict_dir) if dict_dir.is_dir() else "",
                    lang="zh",
                ),
                num_threads=threads,
                provider="cpu",
            ),
            rule_fsts=",".join(str(item) for item in rule_fsts if item.is_file()),
        )
        try:
            tts = sherpa_onnx.OfflineTts(config)
        except RuntimeError as exc:
            # espeak-ng 初始化失败会让 sherpa-onnx 直接抛 RuntimeError。实测最常见的
            # 触发方式是：本进程早先初始化过它，期间语音模型被删除或重装过（音素数据
            # 已经不在原处），之后再建实例就必然失败——只能靠重启进程恢复，
            # 所以把可操作的办法直接说给用户，别丢一串英文上去。
            if "espeak" in str(exc).lower():
                raise EngineUnavailableError(
                    "语音引擎初始化失败（espeak-ng）。如果刚删除或重装过语音模型，"
                    "请重启应用后再试；仍未恢复请到「模型管理」重新下载语音模型。"
                ) from exc
            raise
        _cache[(model_id, lane)] = (tts, key)
        return tts


def reset_cache() -> None:
    """模型被删除或重新校验后清掉内存里的实例。"""

    with _lock:
        _cache.clear()


def clamp_voice(tts, sid: int) -> int:
    total = int(getattr(tts, "num_speakers", 0) or 0)
    if total <= 0:
        return max(0, sid)
    return max(0, min(int(sid), total - 1))


def clamp_speed(speed: float) -> float:
    low, high = SPEED_RANGE
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    return max(low, min(high, value))


def _generate_serial(
    model_id: str,
    sentences: list[str],
    sid: int,
    speed: float,
    on_progress,
) -> tuple[list[np.ndarray], int]:
    """一个实例逐句生成：短文本和核数少的机器走这条。"""

    tts = get_tts(model_id)
    voice = clamp_voice(tts, sid)
    parts: list[np.ndarray] = []
    for index, sentence in enumerate(sentences, start=1):
        audio = tts.generate(sentence, sid=voice, speed=speed)
        samples = np.asarray(audio.samples, dtype=np.float32)
        if samples.size:
            parts.append(samples)
        if on_progress is not None:
            on_progress(index, len(sentences))
    # 采样率顺手带回去，省得调用方为了拿它再去取一次实例
    return parts, int(tts.sample_rate)


def _generate_parallel(
    model_id: str,
    sentences: list[str],
    sid: int,
    speed: float,
    lanes: int,
    on_progress,
) -> tuple[list[np.ndarray], int]:
    """多个实例同时生成：按字符数把句子分给各槽位，最后按原句序拼回去。"""

    total = len(sentences)
    # 实例数不超过句子数：两句话不值得为此加载四个几百 MB 的模型
    width = min(lanes, total)
    buckets: list[list[tuple[int, str]]] = [[] for _ in range(width)]
    loads = [0] * width
    for index, sentence in enumerate(sentences):
        # 贪心：谁手上的活最少就给谁。长短句混在一起时比逐个轮流分配均衡得多。
        slot = loads.index(min(loads))
        buckets[slot].append((index, sentence))
        loads[slot] += len(sentence)

    ordered: list[np.ndarray | None] = [None] * total
    errors: list[Exception] = []
    rates: list[int] = []
    done = 0
    guard = threading.Lock()

    def worker(slot: int) -> None:
        nonlocal done
        try:
            tts = get_tts(model_id, slot)
            voice = clamp_voice(tts, sid)
            with guard:
                rates.append(int(tts.sample_rate))
            for index, sentence in buckets[slot]:
                audio = tts.generate(sentence, sid=voice, speed=speed)
                samples = np.asarray(audio.samples, dtype=np.float32)
                if samples.size:
                    ordered[index] = samples
                with guard:
                    done += 1
                    if on_progress is not None:
                        # 取消就是在这行抛出来的（见 tts_jobs），不能吞掉
                        on_progress(done, total)
        except Exception as exc:  # noqa: BLE001 - 取消与引擎错误都要带回主线程
            errors.append(exc)

    workers = [
        threading.Thread(target=worker, args=(slot,), name=f"tts-lane-{slot}")
        for slot in range(width)
    ]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join()

    # 取消或报错：原样抛给上层，由它把任务标成「已取消 / 失败」
    if errors:
        raise errors[0]
    # 各槽位是同一份模型，采样率必然一致，随便取一个带走
    return [part for part in ordered if part is not None], (rates[0] if rates else 0)


def synthesize(
    text: str,
    *,
    model_id: str = DEFAULT_MODEL_ID,
    sid: int = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    on_progress=None,  # Callable[[int, int], None] | None
) -> tuple[np.ndarray, int]:
    """把文本合成成单声道 float32 波形。

    句子会按需分给多个实例并行生成（见 _lanes），再按原顺序拼接；
    on_progress 收到的仍是「已完成句数 / 总句数」，与并行度无关。
    """

    cleaned = normalize_text(text)
    if not cleaned:
        raise ValueError("请先输入要合成的文本")
    if len(cleaned) > MAX_CHARS:
        raise ValueError(f"一次最多合成 {MAX_CHARS} 字，当前 {len(cleaned)} 字，请分段处理")

    speed = clamp_speed(speed)
    sentences = split_sentences(cleaned)
    width = lanes()
    if width > 1 and len(sentences) > 1:
        parts, sample_rate = _generate_parallel(model_id, sentences, sid, speed, width, on_progress)
    else:
        parts, sample_rate = _generate_serial(model_id, sentences, sid, speed, on_progress)
    if not parts:
        raise ValueError("没有生成任何音频，请检查文本内容")

    gap = np.zeros(int(sample_rate * SENTENCE_GAP_SECONDS), dtype=np.float32)
    merged = parts[0]
    for part in parts[1:]:
        merged = np.concatenate((merged, gap, part))
    return merged, sample_rate


def duration_seconds(samples: np.ndarray, sample_rate: int) -> float:
    return round(len(samples) / max(sample_rate, 1), 2)


def _without_dc(samples: np.ndarray) -> np.ndarray:
    """去掉直流偏移。

    Kokoro 的输出带约满刻度 2.8% 的直流分量（实测），直接放出去低频偏重、
    听着发闷发沙；减掉均值不改变语音内容，人耳也听不出差别。
    偏移本来就干净时原样返回，省一次拷贝。
    """

    offset = float(np.mean(samples))
    return samples - offset if abs(offset) > 1e-4 else samples


def to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, _without_dc(samples), sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def to_mp3_bytes(samples: np.ndarray, sample_rate: int) -> bytes | None:
    """用 FFmpeg 转 mp3（体积约为 wav 的 1/10）；没装 FFmpeg 时返回 None 由上层回退 wav。"""

    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "wav", "-i", "pipe:0",
        "-codec:a", "libmp3lame", "-b:a", "128k",
        "-f", "mp3", "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            input=to_wav_bytes(samples, sample_rate),
            capture_output=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("FFmpeg 转 mp3 失败，回退 wav", exc_info=True)
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def wav_file_to_mp3(path: Path) -> bytes | None:
    """把已有的 wav 文件转成 mp3（不经内存里的波形，直接让 FFmpeg 读文件）。"""

    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(path),
        "-codec:a", "libmp3lame", "-b:a", "128k",
        "-f", "mp3", "pipe:1",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError):
        logger.warning("FFmpeg 转 mp3 失败：%s", path, exc_info=True)
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def write_audio(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, _without_dc(samples), sample_rate, format="WAV", subtype="PCM_16")
