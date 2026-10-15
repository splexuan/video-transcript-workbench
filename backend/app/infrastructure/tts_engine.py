"""语音合成引擎：用 sherpa-onnx 跑 ZipVoice（ONNX），把文案读成 24kHz 音频。

和识别共用同一个运行时（sherpa-onnx）与同一套模型管理（下载 / 校验 / 删除），
不引入 PyTorch、不依赖 CUDA。

ZipVoice 与 Kokoro 的根本差别：它不是「从固定音色库里挑一个」，而是**克隆**——
给一段参考音频和它的确切文本，模型把该说话人的声音复现出来。所以这里的「音色」存的是
一段音频而不是编号，质量上限由参考音频决定：底噪、混响、环境声都会被一并克隆进去
（实测谱平坦度：干净参考 0.14、带底噪参考 0.64）。所以引导用户用干净的单人录音是关键。

实测（12 核 CPU，int8 版，4 采样步、4 线程）：60 字约 16.4 秒音频，RTF 0.47。
并行提速仍靠「多实例」：把句子分给几个实例同时合成（见 lanes / _threads）。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np
import soundfile as sf

from app.config import settings
from app.domain import EngineUnavailableError
from app.infrastructure.media import MediaToolError, convert_to_wav, find_tool, probe_media
from app.infrastructure.model_catalog import require_spec
from app.infrastructure.model_store import is_complete, spec_dir
from app.infrastructure.text_numbers import normalize_chinese_numbers

logger = logging.getLogger(__name__)

ENGINE = "tts"
DEFAULT_MODEL_ID = "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"
# 单次合成上限。按 RTF≈0.5 估算 2000 字≈7 分钟音频；再长就该分段出稿，而不是让一个请求挂着。
MAX_CHARS = 2000
DEFAULT_SPEED = 1.0
SPEED_RANGE = (0.6, 1.6)
SENTENCE_GAP_SECONDS = 0.22
# 并行实例数上限：实例数比「每实例多少线程」更管用。每个实例要独立加载一份模型。
MAX_LANES = 4
# 太短的碎句并进上一句：逐句合成会在句间插静音，碎句多了口气就断了
MIN_SENTENCE_CHARS = 8
# 采样步数：蒸馏版官方示例用 4，实测与 8/16 步在中文上听感无实质差别，代价只有一半。
NUM_STEPS = 4
# ZipVoice 内部按这个长度自己分句（官方示例的取值），太小会切碎语气。
MIN_CHAR_IN_SENTENCE = 30
# 输出电平保护。实测未加保护时峰值会到 1.18~1.32，写 PCM_16 时超限部分被削掉，
# 产生的失真听感就是「有噪音」——这不是模型问题，是缺这一步。
PEAK_CEILING = 0.97

# emoji 与零宽字符：小红书文案里很常见，合成时会变成未知 token（念不出还可能报错），先清掉
_NOISE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u20E3]")
_SENTENCE = re.compile(r"[^。！？!?；;\n…]+[。！？!?；;…]*")

_lock = threading.Lock()
# 按「模型 id + 并行槽位」缓存：槽位之间必须是彼此独立的实例，
# 同一个 OfflineTts 不保证能被并发调用（见 get_tts）。
_cache: dict[tuple[str, int], tuple[object, str]] = {}


class AsrWindow(NamedTuple):
    """一次转写的结果：文字、时长（秒）、是否因超出可用长度被截断。"""

    text: str
    seconds: float
    truncated: bool


@dataclass
class Voice:
    """一个可用音色。

    `builtin` 的 id 直接对应模型目录里 test_wavs/ 下的文件名；`custom` 来自用户上传，
    实际音频与元数据存在数据目录下（见 _custom_dir）。
    """

    id: str
    name: str
    reference_text: str
    builtin: bool
    # 参考音频时长（秒）。内置音色不参与编辑，没有这个数（0）。
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ReferenceEdit:
    """一次参考音频改动（新建 / 替换 / 裁剪）的结果。

    `text_source` 说明参考文本是怎么来的：手填 `typed`、重新转写 `transcribed`、
    沿用旧值 `kept`——最后一种发生在转写没成功时，把文本抹掉比留着更糟。
    `note` 带着需要如实告诉用户的原因（转写失败之类）。
    """

    voice: Voice
    text_source: str
    note: str = ""


# 模型包自带的参考音频及其**精确**文本。文本必须与音频逐字一致，否则克隆出的音色会明显变差，
# 所以这三份写死在代码里，不去解析包里的 prompt.txt（那个文件的格式没有稳定约定）。
# 显示名是产品侧定的：id 只对应包里的示例音频（来自公开演讲片段），与本人无关、也不代表授权。
_BUILTIN_VOICES = (
    Voice("leijun-1", "雷总", "那还是三十六年前, 一九八七年. 我呢考上了武汉大学的计算机系.", True),
    Voice("news-female", "女声 · 现场连线", "各位村民, 大家新年好! 近期, 湖北省武汉市等多个地区", True),
    Voice(
        "news-female-2",
        "女声 · 资讯播报",
        "本台消息, 中共中央国务院, 近日印发关于构建数据基础制度, 更好发挥数据要素作用的意见.",
        True,
    ),
)
DEFAULT_VOICE = "leijun-1"

# 参考音频的时长上限。ZipVoice 的耗时随参考音频长度增长明显（实测短参考 RTF 0.47、
# 长的到 1.9），超过这个长度直接截断并给提示。
REFERENCE_MAX_SECONDS = 20.0
# 裁剪时的下界：比这更短的片段克隆出的音色不稳，所以直接拒绝而不是默默接受。
REFERENCE_MIN_SECONDS = 2.0
# 界面据此给建议区间（也是 ZipVoice 官方示例推荐的参考音频长度）。
REFERENCE_RECOMMENDED = (3.0, 10.0)


def lanes(cores: int | None = None) -> int:
    """并行实例数（也用于给界面估算等待时间）：每实例大约 3 线程，最多 MAX_LANES 个。

    12 核 → 4 个（每实例 3 线程）；8 核 → 2 个（每实例 4 线程）；4 核及以下 → 1 个。
    核数少的机器不会因为并行而更慢。
    """

    cores = cores or os.cpu_count() or 4
    return max(1, min(MAX_LANES, cores // 3))


def _threads(count: int | None = None, cores: int | None = None) -> int:
    """每个实例的线程数。ZipVoice 的 flow matching 在多线程下扩展性一般，3~4 线程是甜点区。"""

    cores = cores or os.cpu_count() or 4
    return max(2, min(4, cores // max(count or lanes(cores), 1)))


def normalize_text(text: str) -> str:
    """清掉 emoji、压缩空白、把中文数字改写成汉字。

    数字改写是必须的：ZipVoice 走 espeak-ng 做 G2P，而它把阿拉伯数字一律按英文读
    （详见 text_numbers 模块）。空文本是调用方的错误，这里返回空串由上层报错。
    """

    cleaned = _NOISE.sub("", text or "")
    cleaned = re.sub(r"[ \t\u3000]+", " ", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    cleaned = normalize_chinese_numbers(cleaned)
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


def clamp_speed(speed: float) -> float:
    low, high = SPEED_RANGE
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    return max(low, min(high, value))


def _custom_dir() -> Path:
    directory = settings.data_dir / "tts" / "voices"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _builtin_path(directory: Path, voice_id: str) -> Path:
    return directory / "test_wavs" / f"{voice_id}.wav"


def _load_custom(voice_id: str) -> Voice | None:
    directory = _custom_dir() / voice_id
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        name = str(payload["name"]).strip() or "自定义音色"
        reference_text = str(payload["reference_text"]).strip()
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        logger.warning("自定义音色元数据不可用：%s", voice_id, exc_info=True)
        return None
    if not reference_text or not (directory / "reference.wav").is_file():
        return None
    return Voice(voice_id, name, reference_text, False, _read_seconds(directory, payload))


def _read_seconds(directory: Path, payload: dict) -> float:
    """参考音频时长。优先信元数据里的秒数，早期音色没写这个字段时就量一遍文件。"""

    try:
        recorded = float(payload.get("seconds", 0.0))
    except (TypeError, ValueError):
        recorded = 0.0
    if recorded > 0:
        return round(recorded, 2)
    try:
        return duration_seconds(*sf.read(str(directory / "reference.wav"), dtype="float32"))
    except Exception:  # noqa: BLE001 - 量不出时长不该让整个音色从清单里消失
        logger.warning("读取参考音频时长失败：%s", directory, exc_info=True)
        return 0.0


def voices() -> list[Voice]:
    """全部可用音色：模型自带的在前，自定义上传的按名称排在后面。"""

    builtin = list(_BUILTIN_VOICES)
    found: list[Voice] = []
    try:
        for entry in sorted(_custom_dir().iterdir()):
            if entry.is_dir() and (entry / "meta.json").is_file():
                voice = _load_custom(entry.name)
                if voice is not None:
                    found.append(voice)
    except OSError:
        logger.warning("读取自定义音色目录失败", exc_info=True)
    return [*builtin, *sorted(found, key=lambda item: item.name)]


def find_voice(voice_id: str | None) -> Voice | None:
    """按 id 找音色，找不到返回 None。

    刻意不做回落：调用方分两种诉求——「校验用户传来的 id」（接口层）与「拿一个能用的
    音色兜底」（合成时），混在一起会让前者的校验形同虚设。
    """

    wanted = (voice_id or "").strip()
    if not wanted:
        return None
    return next((item for item in voices() if item.id == wanted), None)


def resolve_voice(voice_id: str | None) -> Voice:
    """合成时用这个：id 失效（模型重装了、自定义音色被删了、老设置里存的是旧编号）就回落默认。"""

    return find_voice(voice_id) or find_voice(DEFAULT_VOICE)


def save_custom_voice(
    name: str,
    reference_text: str,
    audio: bytes,
    *,
    start: float | None = None,
    end: float | None = None,
) -> Voice:
    """存一段用户上传的参考音频，返回新音色。

    `start` / `end` 是可选的选区（秒）：给了就只取这一段，所以「转写出来的文字」
    与「最终落盘的音频」始终是同一段，不会出现文字比音频长的错位。

    统一转成 24kHz 单声道 WAV：ZipVoice 的参考音频与输出同率最省事，也能顺手把立体声、
    过长录音这类问题在入口就挡掉。
    """

    text = (reference_text or "").strip()
    if not text:
        raise ValueError("请填写这段音频里说出的文字，它必须和音频逐字一致")
    return _place_reference(
        audio,
        voice_id=None,
        name=name,
        reference_text=text,
        start=start,
        end=end,
    ).voice


def replace_voice_audio(
    voice_id: str,
    audio: bytes,
    *,
    name: str = "",
    reference_text: str = "",
    start: float | None = None,
    end: float | None = None,
) -> ReferenceEdit:
    """就地换掉某个自定义音色的参考音频（界面上的「重新上传音频」）。

    刻意不走 save_custom_voice：那条路会另起一个新音色，而这里是覆盖原来那个。
    参考文本按 手填 → 转写 → **保留旧值** 的顺序定：把旧文本抹掉比留着更糟——
    音色会变成「没有参考文本」，得靠人重新来一遍。
    """

    meta, directory = _require_custom(voice_id)
    return _place_reference(
        audio,
        voice_id=voice_id,
        directory=directory,
        name=name or str(meta.get("name", "")),
        reference_text=reference_text,
        start=start,
        end=end,
        previous_text=str(meta.get("reference_text", "")),
    )


def trim_voice(
    voice_id: str,
    start: float,
    end: float,
    *,
    reference_text: str = "",
    retranscribe: bool = False,
) -> ReferenceEdit:
    """把已存音色的参考音频裁到 [start, end] 秒。

    选区会先按真实时长夹住，因此「越界一点」的拖拽结果是可预期的，不会报错。
    `retranscribe` 为真时按裁剪后的音频重新转写参考文本——不勾就沿用旧文本
    （前提是用户自己把文本改成选区里说的那些字）。
    """

    meta, directory = _require_custom(voice_id)
    path = directory / "reference.wav"
    payload = path.read_bytes()
    return _place_reference(
        payload,
        voice_id=voice_id,
        directory=directory,
        name=str(meta.get("name", "")),
        reference_text=reference_text,
        start=start,
        end=end,
        previous_text=str(meta.get("reference_text", "")),
        allow_transcribe=retranscribe,
    )


def rename_custom_voice(voice_id: str, name: str) -> Voice:
    """改音色名。只动元数据——音色 id 是目录名，与名字无关，所以不用搬文件。"""

    meta, directory = _require_custom(voice_id)
    label = (name or "").strip()[:40] or "自定义音色"
    payload = dict(meta)
    payload["name"] = label
    _write_meta(directory, payload)
    return Voice(voice_id, label, str(meta.get("reference_text", "")).strip(), False, _read_seconds(directory, payload))


def retranscribe_voice(voice_id: str) -> Voice:
    """按当前参考音频重新转写一遍参考文本。

    转写失败一律报错（不静默改文本）：这个动作是用户明确发起的，
    悄悄跳过只会让他以为文字已经更新了。
    """

    meta, directory = _require_custom(voice_id)
    text, _seconds, _truncated = transcribe_reference((directory / "reference.wav").read_bytes())
    payload = dict(meta)
    payload["reference_text"] = text
    _write_meta(directory, payload)
    return Voice(voice_id, str(meta.get("name", "")).strip(), text, False, _read_seconds(directory, payload))


def transcribe_voice_window(voice_id: str, start: float | None, end: float | None) -> AsrWindow:
    """按选区转写已保存的参考音频，**只返回文字、不落盘**。

    与 retranscribe_voice 的区别有两条，都是为了不骗用户：
      - 转写的是界面上选中的那一段，而不是整段音频——整段转出来的文字比选区长，
        照着核对只会一直对不上；
      - 不写 meta.json——编辑弹窗里点「重新转写」只是回填文本框，
        真要落盘得再点一次「保存修改」。写盘了就变成「看一眼也改了音色」。
    """

    _meta, directory = _require_custom(voice_id)
    audio = (directory / "reference.wav").read_bytes()
    text, seconds, truncated = transcribe_reference(trim_window(audio, start, end))
    return AsrWindow(text=text, seconds=seconds, truncated=truncated)


def reference_path(voice_id: str) -> Path:
    """自定义音色的参考音频文件；不存在时抛错（内置音色不走这里）。"""

    _meta, directory = _require_custom(voice_id)
    return directory / "reference.wav"


def _require_custom(voice_id: str) -> tuple[dict, Path]:
    """取自定义音色的元数据与目录；内置音色或已删除都给上层一个明确错误。"""

    if voice_id in {item.id for item in _BUILTIN_VOICES}:
        raise ValueError("内置音色来自语音模型包，不能改参考音频")
    directory = _custom_dir() / voice_id
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        raise ValueError("这个音色已不存在，可能已经被删掉了")
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")), directory
    except (OSError, ValueError, json.JSONDecodeError):
        raise ValueError("这个音色的元数据已损坏，请删除后重新上传") from None


def _write_meta(directory: Path, payload: dict) -> None:
    (directory / "meta.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def trim_window(audio: bytes, start: float | None, end: float | None) -> bytes:
    """按选区（秒）裁一段上传音频，返回 wav 字节。

    「自动转写」与「保存音色」都要先过这里：转写出来的文字与最终落盘的音频
    必须是同一段，否则用户看到的文字比音频长，核对时无从下手。
    两端都不给就原样返回，省掉一次没必要的重编码。
    """

    if start is None and end is None:
        return audio
    mono, rate = _read_wave(audio)
    mono = _slice(mono, rate, start, end)
    return _wav_bytes(mono, rate)


def _read_wave(audio: bytes) -> tuple[np.ndarray, int]:
    """读任意格式的参考音频，返回（单声道 float32, 采样率）。"""

    if not audio:
        raise ValueError("请先选择或录制一段参考音频")
    try:
        samples, rate = sf.read(io.BytesIO(audio), dtype="float32", always_2d=True)
    except Exception as exc:
        # libsndfile 的异常类型很杂（RuntimeError / soundfile.LibsndfileError…），
        # 统一转成一句用户看得懂的话，不要把英文栈直接抛到界面上
        raise ValueError("无法读取这段音频，请换 wav / mp3 / m4a 试试") from exc
    return samples.mean(axis=1), int(rate)


def _slice(mono: np.ndarray, rate: int, start: float | None, end: float | None) -> np.ndarray:
    """按秒取一段。选区为空（两端都没给）表示整段。

    先把选区夹到音频范围内再判长度：界面上拖出来的边界常常略微越界，
    直接报错会让一次本来没问题的操作失败。
    """

    if start is None and end is None:
        return mono
    total = mono.size / rate
    begin = 0.0 if start is None else max(0.0, min(float(start), total))
    finish = total if end is None else max(0.0, min(float(end), total))
    if finish < begin:
        begin, finish = finish, begin
    if finish - begin < REFERENCE_MIN_SECONDS:
        raise ValueError(
            f"选区只有 {finish - begin:.1f} 秒，太短了：请至少留 {REFERENCE_MIN_SECONDS:.0f} 秒，"
            f"{REFERENCE_RECOMMENDED[0]:.0f}~{REFERENCE_RECOMMENDED[1]:.0f} 秒最合适"
        )
    return mono[int(round(begin * rate)) : int(round(finish * rate))]


def _finalize(mono: np.ndarray, rate: int) -> tuple[np.ndarray, int]:
    """落盘前的统一处理：对齐 24kHz、封顶时长、峰值归一、去直流。"""

    if rate != 24000:
        mono = _resample(mono, rate, 24000)
        rate = 24000
    limit = int(rate * REFERENCE_MAX_SECONDS)
    if mono.size > limit:
        mono = mono[:limit]
    peak = float(np.abs(mono).max())
    if peak < 1e-4:
        raise ValueError("这段音频几乎没有声音，换一段有人说话的试试")
    mono = mono / peak * 0.9
    return mono - float(np.mean(mono)), rate


def _wav_bytes(mono: np.ndarray, rate: int) -> bytes:
    """内存里的波形 → wav 字节。写盘与转写走同一份编码，两边不会差出一点偏差。"""

    buffer = io.BytesIO()
    sf.write(buffer, mono, rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def _resolve_text(
    reference_text: str,
    audio: bytes | None,
    previous: str,
    *,
    allow_transcribe: bool = True,
) -> tuple[str, str, str]:
    """定下参考文本，返回（文本, 来源, 需要告知用户的原因）。

    顺序是 手填 → 转写 → 保留旧值。转写没成功时保留旧文本：音色会变成
    「没有参考文本」，得靠人重新来一遍，那比留着一段对不上的文字更糟。
    """

    typed = (reference_text or "").strip()
    if typed:
        return typed, "typed", ""
    if audio is None or not allow_transcribe:
        return (previous or "").strip(), "kept", ""
    try:
        text, _seconds, _truncated = transcribe_reference(audio)
    except (ValueError, EngineUnavailableError, MediaToolError) as exc:
        return (previous or "").strip(), "kept", str(exc)
    if text.strip():
        return text.strip(), "transcribed", ""
    return (previous or "").strip(), "kept", "没有识别出这段音频里说了什么"


def _place_reference(
    audio: bytes,
    *,
    voice_id: str | None,
    name: str,
    reference_text: str,
    start: float | None = None,
    end: float | None = None,
    previous_text: str = "",
    directory: Path | None = None,
    allow_transcribe: bool = True,
) -> ReferenceEdit:
    """参考音频落盘的唯一入口：新建、替换、裁剪都走这里。

    **必须共用**：裁剪怎么做、什么时候重采样、文本从哪来，只要三条路径各写一遍，
    换个入口进来行为就会不一样（比如裁剪那条忘了重采样，克隆出的音色就会变调）。
    """

    mono, rate = _read_wave(audio)
    mono = _slice(mono, rate, start, end)
    mono, rate = _finalize(mono, rate)
    wav = _wav_bytes(mono, rate)

    text, source, note = _resolve_text(
        reference_text, wav, previous_text, allow_transcribe=allow_transcribe
    )
    if not text:
        raise ValueError("请填写这段音频里说出的文字，它必须和音频逐字一致")

    if voice_id is None:
        voice_id = uuid.uuid4().hex[:12]
        directory = _custom_dir() / voice_id
    directory.mkdir(parents=True, exist_ok=True)
    label = (name or "").strip()[:40] or "自定义音色"
    sf.write(directory / "reference.wav", mono, rate, subtype="PCM_16")
    _write_meta(
        directory,
        {"name": label, "reference_text": text, "seconds": duration_seconds(mono, rate)},
    )
    return ReferenceEdit(
        voice=Voice(voice_id, label, text, False, duration_seconds(mono, rate)),
        text_source=source,
        note=note,
    )


def remove_custom_voice(voice_id: str) -> bool:
    """删除自定义音色；内置音色不归这里管，删不掉就返回 False。"""

    directory = _custom_dir() / voice_id
    if voice_id in {item.id for item in _BUILTIN_VOICES} or not directory.is_dir():
        return False
    for item in sorted(directory.iterdir(), reverse=True):
        item.rmdir() if item.is_dir() else item.unlink(missing_ok=True)
    directory.rmdir()
    return True


def transcribe_reference(audio: bytes) -> tuple[str, float, bool]:
    """转写一段参考音频，返回（文字, 时长秒, 是否截断过）。

    这是给「音色克隆」用的：参考文本必须与音频逐字一致，手打既费劲又容易打错，
    所以直接复用识别引擎转一遍，用户只要核对就行。

    引擎优先选 SenseVoice——十几秒的音频是秒级，正好适合放在同步请求里；
    没装 SenseVoice 就退回 faster-whisper（慢一些但结果同样可用）。
    只做识别，不走下载 / 解封装 / 任务队列那一套完整流程。

    截断与 save_custom_voice 用的是同一个上限：转换阶段就按它裁掉，转写出来的文字
    与最终保存的音频是同一段，否则超长音频会出现「文字比音频长」的错位。
    """

    if not audio:
        raise ValueError("请先选择一段参考音频")

    staging = settings.data_dir / "tts" / "_transcribe"
    staging.mkdir(parents=True, exist_ok=True)
    source = staging / f"input-{uuid.uuid4().hex[:8]}"
    wav = staging / f"{source.name}.wav"
    try:
        source.write_bytes(audio)
        original = probe_media(source).duration_seconds
        # 识别引擎要 16kHz 单声道；参考音频最后仍存成 24kHz，两者不冲突
        convert_to_wav(source, wav, max_seconds=REFERENCE_MAX_SECONDS)
        chunks, seconds = _run_recognition(wav)
    finally:
        for item in (source, wav):
            item.unlink(missing_ok=True)

    text = "".join(chunk.text for chunk in chunks).strip()
    if not text:
        raise ValueError("没听清这段音频里说了什么，换一段人声更清楚的试试")
    return text, round(seconds, 2), original > REFERENCE_MAX_SECONDS + 0.5


def _run_recognition(wav: Path) -> tuple[list, float]:
    """按「已装的识别引擎」跑一遍，返回 (分句列表, 时长秒)。"""

    from app.domain import ModelEngine
    from app.infrastructure import transcriber, whisper_engine
    from app.infrastructure.model_store import resolve_engine

    fast = resolve_engine(ModelEngine.SENSEVOICE)
    if fast is not None:
        try:
            return transcriber.transcribe(wav)
        except transcriber.NoSpeechError as exc:
            raise ValueError("这段音频里没有人声，确认选的是语音文件") from exc

    accurate = resolve_engine(ModelEngine.FASTER_WHISPER)
    if accurate is None:
        raise EngineUnavailableError(
            "自动填参考文本需要一个识别模型，请先到「模型管理」安装 SenseVoice 或 faster-whisper；"
            "也可以手动填写这段音频里说出的文字"
        )
    try:
        return whisper_engine.transcribe(wav, accurate[1])
    except whisper_engine.WhisperNoSpeechError as exc:
        raise ValueError("这段音频里没有人声，确认选的是语音文件") from exc


def _resample(samples: np.ndarray, source: int, target: int) -> np.ndarray:
    """线性重采样。参考音频用它对齐到 24kHz，不追求保真度，够用即可。"""

    if source == target or samples.size == 0:
        return samples
    count = round(samples.size * target / source)
    positions = np.linspace(0, samples.size - 1, count, dtype=np.float64)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)


def model_directory(model_id: str) -> Path:
    """语音模型目录；没装好时给出可操作的提示（去模型管理下载）。"""

    spec = require_spec(model_id)
    directory = spec_dir(spec)
    if not is_complete(spec, directory):
        raise EngineUnavailableError(f"「{spec.name}」还没安装，请先到「模型管理」下载")
    return directory


def load_reference(voice: Voice, model_id: str) -> tuple[np.ndarray, int]:
    """取参考音频的波形；找不到或读不出来时明确报错，不静默换成别的音色。"""

    directory = model_directory(model_id)
    path = (
        _builtin_path(directory, voice.id)
        if voice.builtin
        else _custom_dir() / voice.id / "reference.wav"
    )
    if not path.is_file():
        hint = "模型包里没带上这段参考音频，请重新下载语音模型" if voice.builtin else "这段参考音频已被删除，请重新上传"
        raise EngineUnavailableError(hint)
    samples, rate = sf.read(str(path), dtype="float32")
    if samples.ndim > 1:
        samples = samples[:, 0]
    return samples, int(rate)


# ZipVoice 的模型文件要「成对」挑：编码器与解码器必须同代同精度，否则可能把 int8 的编码器
# 配上 fp32 的解码器。int8 排在前面——它体积小、速度快，是默认那一档；想要更高精度就装
# fp32 的包（另一个规格），那种目录里只有 fp32 的文件，顺序不影响。
_MODEL_PAIRS = (
    ("encoder.int8.onnx", "decoder.int8.onnx"),
    ("text_encoder_int8.onnx", "fm_decoder_int8.onnx"),
    ("encoder.onnx", "decoder.onnx"),
    ("text_encoder.onnx", "fm_decoder.onnx"),
)


def _pick_model_pair(directory: Path) -> tuple[Path, Path]:
    """按精度成对挑出编码器与解码器；两代布局的文件名都认。"""

    for encoder_name, decoder_name in _MODEL_PAIRS:
        encoder = directory / encoder_name
        decoder = directory / decoder_name
        if encoder.is_file() and decoder.is_file():
            return encoder, decoder
    raise EngineUnavailableError("语音模型缺少模型文件，请重新下载")


def _require_lexicon(directory: Path) -> Path:
    """拼音词典。

    sherpa-onnx 的 ZipVoice 实现必须要有 lexicon.txt——缺了它会在构造时**直接中止进程**，
    连 Python 异常都抛不出来（实测踩过），所以这里先自己查一遍，把话说清楚。
    """

    lexicon = directory / "lexicon.txt"
    if not lexicon.is_file():
        raise EngineUnavailableError("语音模型缺少拼音词典（lexicon.txt），请重新下载模型")
    return lexicon


def get_tts(model_id: str = DEFAULT_MODEL_ID, lane: int = 0):
    """按「模型 id + 槽位」缓存的实例；线程数变化时才重建。

    槽位只是为了并行合成：每个槽位一个独立实例，各由一个线程独占使用。
    """

    try:
        import sherpa_onnx
    except ImportError:  # pragma: no cover - 依赖缺失属于环境问题
        raise EngineUnavailableError("缺少 sherpa-onnx，无法使用语音合成") from None

    directory = model_directory(model_id)
    threads = _threads()
    key = f"{directory}:{threads}"
    # 注意：_lock 覆盖「查缓存 + 构造 + 写缓存」整段，中间不能再取一次锁——
    # threading.Lock 不可重入，嵌套 with 会直接死锁（构造一次要几秒，串行化可以接受）。
    with _lock:
        cached = _cache.get((model_id, lane))
        if cached is not None and cached[1] == key:
            return cached[0]

        encoder, decoder = _pick_model_pair(directory)
        lexicon = _require_lexicon(directory)

        # feat_scale / guidance_scale / t_shift / target_rms 一律不传：官方示例也是全默认，
        # 自己填值会改变响度条件（实测 target_rms=0.1 会让输出掉到 -37dBFS，听着全是沙沙声）。
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
                    encoder=str(encoder),
                    decoder=str(decoder),
                    vocoder=str(directory / "vocos_24khz.onnx"),
                    tokens=str(directory / "tokens.txt"),
                    lexicon=str(lexicon),
                    data_dir=str(directory / "espeak-ng-data"),
                ),
                num_threads=threads,
                provider="cpu",
            ),
        )
        try:
            tts = sherpa_onnx.OfflineTts(config)
        except RuntimeError as exc:
            # espeak-ng 初始化失败会让 sherpa-onnx 直接抛 RuntimeError。实测最常见的
            # 触发方式是：本进程早先初始化过它，期间语音模型被删除或重装过（音素数据
            # 已经不在原处），之后再建实例就必然失败——只能靠重启进程恢复。
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


def _generation_config(sherpa_onnx, reference, rate: int, reference_text: str, speed: float):
    """构造 ZipVoice 的生成参数。

    GenerationConfig 是空构造 + 属性赋值（pybind 没有暴露带参构造）。
    min_char_in_sentence 让模型自己分句，避免外部切分打断它的韵律。
    """

    config = sherpa_onnx.GenerationConfig()
    config.num_steps = NUM_STEPS
    config.speed = speed
    config.reference_audio = reference
    config.reference_sample_rate = rate
    config.reference_text = reference_text
    config.extra["min_char_in_sentence"] = str(MIN_CHAR_IN_SENTENCE)
    return config


def _speak(tts, sentence: str, reference, rate: int, voice: Voice, speed: float) -> np.ndarray:
    import sherpa_onnx

    audio = tts.generate(
        sentence,
        _generation_config(sherpa_onnx, reference, rate, voice.reference_text, speed),
    )
    return np.asarray(audio.samples, dtype=np.float32)


def _synthesize(
    model_id: str,
    sentences: list[str],
    voice: Voice,
    speed: float,
    on_progress,
    lanes_count: int,
) -> tuple[list[np.ndarray], int]:
    """按句子生成音频。lanes_count > 1 且有多句时走多实例并行，否则单实例串行。"""

    if lanes_count > 1 and len(sentences) > 1:
        return _generate_parallel(model_id, sentences, voice, speed, on_progress, lanes_count)
    return _generate_serial(model_id, sentences, voice, speed, on_progress)


def _generate_serial(
    model_id: str,
    sentences: list[str],
    voice: Voice,
    speed: float,
    on_progress,
) -> tuple[list[np.ndarray], int]:
    """一个实例逐句生成：短文本和核数少的机器走这条。"""

    tts = get_tts(model_id)
    reference, rate = load_reference(voice, model_id)
    parts: list[np.ndarray] = []
    for index, sentence in enumerate(sentences, start=1):
        samples = _speak(tts, sentence, reference, rate, voice, speed)
        if samples.size:
            parts.append(samples)
        if on_progress is not None:
            on_progress(index, len(sentences))
    return parts, int(tts.sample_rate)


def _generate_parallel(
    model_id: str,
    sentences: list[str],
    voice: Voice,
    speed: float,
    on_progress,
    lanes_count: int,
) -> tuple[list[np.ndarray], int]:
    """多个实例同时生成：按字符数把句子分给各槽位，最后按原句序拼回去。"""

    total = len(sentences)
    # 实例数不超过句子数：两句话不值得为此加载几个上百 MB 的模型
    width = min(lanes_count, total)
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
            reference, rate = load_reference(voice, model_id)
            with guard:
                rates.append(int(tts.sample_rate))
            for index, sentence in buckets[slot]:
                samples = _speak(tts, sentence, reference, rate, voice, speed)
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
    voice_id: str | None = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    on_progress=None,  # Callable[[int, int], None] | None
) -> tuple[np.ndarray, int]:
    """把文本合成成单声道 float32 波形。

    句子会按需分给多个实例并行生成（见 lanes），再按原顺序拼接；
    on_progress 收到的仍是「已完成句数 / 总句数」，与并行度无关。
    """

    cleaned = normalize_text(text)
    if not cleaned:
        raise ValueError("请先输入要合成的文本")
    if len(cleaned) > MAX_CHARS:
        raise ValueError(f"一次最多合成 {MAX_CHARS} 字，当前 {len(cleaned)} 字，请分段处理")

    voice = resolve_voice(voice_id)

    speed = clamp_speed(speed)
    sentences = split_sentences(cleaned)
    parts, sample_rate = _synthesize(
        model_id, sentences, voice, speed, on_progress, lanes()
    )
    if not parts:
        raise ValueError("没有生成任何音频，请检查文本内容")

    gap = np.zeros(int(sample_rate * SENTENCE_GAP_SECONDS), dtype=np.float32)
    merged = parts[0]
    for part in parts[1:]:
        merged = np.concatenate((merged, gap, part))
    return merged, sample_rate


def duration_seconds(samples: np.ndarray, sample_rate: int) -> float:
    return round(len(samples) / max(sample_rate, 1), 2)


def _normalize_peak(samples: np.ndarray) -> np.ndarray:
    """去直流并把峰值压到 PEAK_CEILING 以内。

    两步都要：直流分量会让低频发闷；超幅部分写 PCM_16 时被削平，失真听感就是「有噪音」。
    """

    offset = float(np.mean(samples))
    cleaned = samples - offset if abs(offset) > 1e-4 else samples
    peak = float(np.abs(cleaned).max())
    if peak > PEAK_CEILING:
        cleaned = cleaned * (PEAK_CEILING / peak)
    return cleaned


def to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, _normalize_peak(samples), sample_rate, format="WAV", subtype="PCM_16")
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
    sf.write(path, _normalize_peak(samples), sample_rate, format="WAV", subtype="PCM_16")