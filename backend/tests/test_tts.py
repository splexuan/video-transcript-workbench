"""语音合成：归档安装（含附属文件）、音色克隆、文本处理与接口。

不真的加载 ZipVoice（上百 MB 的模型 + 声码器），只验证编排与契约。
"""
import io
import sys
import tarfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.application import tts_jobs
from app.application.services import read_settings, update_settings
from app.config import settings
from app.domain import EngineUnavailableError
from app.infrastructure import database, model_store, tts_engine
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.model_catalog import require_spec
from app.infrastructure.text_numbers import normalize_chinese_numbers
from app.main import app
from app.schemas import SettingPatch

ZIPVOICE = "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"
# vocoder 不在 tar 包里，由 model_store 在解压后单独下载，因此是归档之外的「附属文件」
VOCODER = "vocos_24khz.onnx"


def _make_tar(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:bz2") as handle:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            handle.addfile(info, io.BytesIO(payload))


def _reference_wav_bytes() -> bytes:
    """一段能真正被 soundfile 读出来的参考音频（引擎会真的去读它）。"""

    rate = 24000
    tone = 0.3 * np.sin(2 * np.pi * 200 * np.arange(rate) / rate)
    buffer = io.BytesIO()
    import soundfile as sf

    sf.write(buffer, tone.astype(np.float32), rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def _longer_reference_wav_bytes() -> bytes:
    """6 秒的参考音频：裁剪用例需要一段明显长于选区的素材。"""

    rate = 24000
    tone = 0.3 * np.sin(2 * np.pi * 200 * np.arange(rate * 6) / rate)
    buffer = io.BytesIO()
    sf.write(buffer, tone.astype(np.float32), rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def _package_bytes(spec) -> bytes:
    """造一份仿真的官方包：正常成员都带包名前缀，另夹一个顶层的路径穿越成员。

    `../evil.txt` 必须是顶层而不是 `<包名>/../evil.txt`——后者 resolve 后仍在解压目录内，
    是完全合法的路径，就测不到穿越防护了。
    """

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as handle:
        entries = {
            f"{spec.id}/encoder.int8.onnx": b"encoder",
            f"{spec.id}/decoder.int8.onnx": b"decoder",
            f"{spec.id}/tokens.txt": b"tokens",
            f"{spec.id}/lexicon.txt": b"lexicon",
            f"{spec.id}/espeak-ng-data/phontab": b"phontab",
            f"{spec.id}/test_wavs/leijun-1.wav": _reference_wav_bytes(),
            "../evil.txt": b"evil",
        }
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            handle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _install_sample(tmp_path: Path, monkeypatch) -> tuple:
    """把一份仿真的模型装到模型目录（含 vocoder），返回 (spec, 目录)。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    spec = require_spec(ZIPVOICE)
    directory = model_store.spec_dir(spec)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / spec.files[0].name
    archive.write_bytes(_package_bytes(spec))
    model_store._extract_archive(archive, directory)
    archive.unlink()
    (directory / VOCODER).write_bytes(b"vocoder")
    return spec, directory


def test_zipvoice_is_an_archive_tts_model_with_a_bundled_vocoder() -> None:
    """官方只发 tar.bz2，且声码器单独发布——规格要同时体现这两点。"""

    spec = require_spec(ZIPVOICE)
    assert spec.engine.value == "tts"
    assert spec.archive is True
    # files[0] 是归档包，files[1:] 是要单独下载的附属文件
    assert spec.files[0].name.endswith(".tar.bz2")
    assert [item.name for item in spec.files[1:]] == [VOCODER]
    assert all(item.urls and item.urls[0].startswith("https://") for item in spec.files)
    # 完整性只查两边共有的文件，具体用哪个模型文件由引擎探测
    assert "tokens.txt" in spec.required_paths
    assert "espeak-ng-data" in spec.required_paths
    assert VOCODER in spec.required_paths
    # 声码器体积要计入总体积，否则界面上的下载预估会少算一半
    assert spec.approx_bytes >= spec.files[1].size


def test_inspect_reports_missing_before_install(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    status = model_store.inspect(require_spec(ZIPVOICE))
    assert status.state == "missing"
    assert status.path is None


def test_archive_extract_strips_top_dir_and_blocks_escape(tmp_path, monkeypatch) -> None:
    """解压要去掉包名那层目录，并挡住用 ../ 写到模型目录外的成员。"""

    _, directory = _install_sample(tmp_path, monkeypatch)
    assert (directory / "tokens.txt").read_bytes() == b"tokens"
    assert (directory / "espeak-ng-data" / "phontab").read_bytes() == b"phontab"
    assert not (directory / "_extract").exists(), "临时目录要清掉"
    assert not (directory / ZIPVOICE).exists(), "包名那层目录不能留下来"
    assert not (directory / "evil.txt").exists()
    assert not (tmp_path / "data" / "models" / "evil.txt").exists()


def test_missing_core_model_file_is_not_reported_ready(tmp_path, monkeypatch) -> None:
    """缺 encoder / decoder 不能算「已就绪」——要等到真合成才报错就太晚了。

    这正是 required_any 存在的原因：int8 与 fp32 的文件名不同，但缺哪一个都不行，
    而 required_paths（全都要有）表达不了这种关系。
    """

    spec, directory = _install_sample(tmp_path, monkeypatch)
    assert model_store.inspect(spec).state == "ready"

    # 只剩 fp32 版文件名的 encoder 也算数（int8 版应当同样认 *.int8.onnx）
    (directory / "encoder.int8.onnx").unlink()
    (directory / "encoder.onnx").write_bytes(b"fp32-encoder")
    assert model_store.inspect(spec).state == "ready"

    # 两个 encoder 都没有 → 不就绪
    (directory / "encoder.onnx").unlink()
    status = model_store.inspect(spec)
    assert status.state == "partial"
    assert "encoder" in status.message

    # 补回来就恢复
    (directory / "encoder.onnx").write_bytes(b"fp32-encoder")
    assert model_store.inspect(spec).state == "ready"


def test_required_any_group_is_reported_together(tmp_path, monkeypatch) -> None:
    """整组缺失时报的是可读名字（两个候选都列出来），不是原始文件名。"""

    spec, directory = _install_sample(tmp_path, monkeypatch)
    for name in ("decoder.int8.onnx", "decoder.onnx"):
        (directory / name).unlink(missing_ok=True)
    status = model_store.inspect(spec)
    assert status.state == "partial"
    assert "decoder.int8.onnx / decoder.onnx" in status.message


def test_missing_vocoder_keeps_the_model_partial(tmp_path, monkeypatch) -> None:
    """声码器缺失时应报「缺少文件」，而不是让用户重下一遍上百 MB 的模型。"""

    _install_sample(tmp_path, monkeypatch)
    spec = require_spec(ZIPVOICE)
    directory = model_store.spec_dir(spec)
    (directory / VOCODER).unlink()

    status = model_store.inspect(spec)
    assert status.state == "partial"
    assert status.message == f"缺少文件：{VOCODER}"
    # 模型本体已经解开，所以补装时要走「只下附属文件」这条路
    assert model_store.archive_extracted(spec, directory) is True


def test_extra_slots_only_apply_to_archive_specs() -> None:
    """非归档规格没有附属文件（files 里每一项都要单独下）。"""

    assert model_store.extra_slots(require_spec(ZIPVOICE))
    assert model_store.extra_slots(require_spec("sensevoice-small")) == ()


def test_installed_zipvoice_counts_as_ready(tmp_path, monkeypatch) -> None:
    _install_sample(tmp_path, monkeypatch)
    status = model_store.inspect(require_spec(ZIPVOICE))
    assert status.state == "ready"
    assert status.installed_bytes > 0


def test_engine_exposes_tts_with_sherpa_package() -> None:
    from app.infrastructure.model_catalog import ENGINE_LABELS, ENGINE_PACKAGES

    assert ENGINE_LABELS["tts"] == "语音合成"
    assert ENGINE_PACKAGES["tts"] == "sherpa_onnx"


def test_builtin_voices_carry_their_exact_reference_text() -> None:
    """参考文本必须与音频逐字一致，所以要随音色一起给出，不能让用户自己猜。"""

    voices = {item.id: item for item in tts_engine.voices()}
    assert tts_engine.DEFAULT_VOICE in voices
    for voice in voices.values():
        assert voice.reference_text.strip(), f"{voice.id} 缺少参考文本"
    assert voices["leijun-1"].builtin is True
    # 与官方示例里 leijun-1.wav 的配套文本一致（数字用汉字）
    assert "三十六年前" in voices["leijun-1"].reference_text


def test_find_voice_is_strict_but_resolve_falls_back(tmp_path, monkeypatch) -> None:
    """两种诉求要分开：接口层要靠 find_voice 判断 id 是否还有效，合成时用 resolve_voice 兜底。

    如果 find_voice 也做回落，接口层那层校验就会永远成立，形同虚设。
    """

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    assert tts_engine.find_voice("leijun-1").id == "leijun-1"
    # 严格模式：查不到就返回 None
    assert tts_engine.find_voice("does-not-exist") is None
    assert tts_engine.find_voice(None) is None
    # 兜底模式：任何失效 id 都能拿到一个能用的音色
    assert tts_engine.resolve_voice("does-not-exist").id == tts_engine.DEFAULT_VOICE
    assert tts_engine.resolve_voice(None).id == tts_engine.DEFAULT_VOICE
    assert tts_engine.resolve_voice("news-female").id == "news-female"


def _stub_media(monkeypatch, seconds: float = 6.0) -> dict[str, object]:
    """桩掉转写前的 ffmpeg 转换与时长探测，返回记录调用参数的字典。

    transcribe_reference 会先落盘 → 转 16k 单声道 → 探测原始时长 → 才调识别，
    测试只想验证后半段（拼句、截断判断），所以把前半段整体替换掉。
    """

    seen: dict[str, object] = {}

    def fake_convert(source, destination, max_seconds=None):
        seen["max_seconds"] = max_seconds
        destination.write_bytes(b"wav")
        return destination

    def fake_probe(path):
        return SimpleNamespace(duration_seconds=seconds)

    monkeypatch.setattr(tts_engine, "convert_to_wav", fake_convert)
    monkeypatch.setattr(tts_engine, "probe_media", fake_probe)
    return seen


def test_chinese_numbers_are_spelled_out_in_chinese_context() -> None:
    """中文语境里的阿拉伯数字要改写成汉字——espeak-ng 会把它们按英文读。"""

    spell = normalize_chinese_numbers
    assert spell("3个技巧") == "三个技巧"
    assert spell("成绩是100分") == "成绩是一百分"
    assert spell("1200元和3.5倍") == "一千二百元和三点五倍"
    assert spell("增长了30%") == "增长了百分之三十"
    assert spell("10000元") == "一万元"
    assert spell("共 3 项。") == "共 三 项。"


def test_year_is_read_digit_by_digit() -> None:
    """年份逐位读（二零二六）比按值读（二千零二十六）自然；非年份不适用。"""

    spell = normalize_chinese_numbers
    assert spell("2026年的数据") == "二零二六年的数据"
    assert spell("会议定在2026年5月1日") == "会议定在二零二六年五月一日"
    assert spell("2026个苹果") == "二千零二十六个苹果"


def test_wan_yi_units_are_folded_into_the_number() -> None:
    """1.2万 是一万二千，不是「一点二万」；原文的「万」要被吃掉。"""

    spell = normalize_chinese_numbers
    assert spell("约1.2万人参加") == "约一万二千人参加"
    assert spell("3.5万人") == "三万五千人"


def test_latin_context_keeps_arabic_digits() -> None:
    """产品名 / 版本号 / 单位里的数字要保留，否则会念成「v二点零」。"""

    spell = normalize_chinese_numbers
    assert spell("用了iPhone 15拍摄") == "用了iPhone 15拍摄"
    assert spell("升级到v2.0版本") == "升级到v2.0版本"
    assert spell("5G网络很快") == "5G网络很快"
    # 整段没有汉字时整体放行
    assert spell("hello 2026 test") == "hello 2026 test"


def test_number_ranges_are_left_alone() -> None:
    """范围表达式两种读法会混在一起，宁可整体保留也不要念成拼接的怪音。"""

    spell = normalize_chinese_numbers
    assert spell("2020-2025年的增长") == "2020-2025年的增长"


def test_normalize_text_applies_number_spelling() -> None:
    """normalize_text 是合成入口，数字改写必须挂在它上面。"""

    assert tts_engine.normalize_text("3 个技巧 很好") == "三 个技巧 很好"
    assert tts_engine.normalize_text("  你好 🎉 世界\u200d  \n\n\n再来  ") == "你好 世界 \n再来"


def test_submit_job_reaches_the_manager_with_a_voice_id(tmp_path, monkeypatch) -> None:
    """回归：接口层把音色按 voice= 传给任务管理器，任务管理器再转给引擎的 voice_id=。

    这里刻意不打桩 submit：之前「模型没装时提交要 400」那条用例在模型检查处就返回了，
    根本走不到 submit，于是 tts_job 的参数名对不上（sid vs voice）这类错误能一路溜到
    线上变成 500，而测试全绿。现在让模型就绪，真跑一遍提交。
    """

    _install_sample(tmp_path, monkeypatch)
    _install_fake(monkeypatch, [])

    captured: dict[str, object] = {}

    def fake_synthesize(text, *, model_id, voice_id, speed, on_progress=None):
        captured["voice_id"] = voice_id
        return np.zeros([2400], dtype=np.float32), 24000

    monkeypatch.setattr(tts_engine, "synthesize", fake_synthesize)

    with TestClient(app) as client:
        created = client.post(
            "/api/tts/jobs", json={"text": "这是一段足够长的测试文本内容。", "voice": "news-female"}
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]

        deadline = time.time() + 10
        while time.time() < deadline:
            state = client.get(f"/api/tts/jobs/{job_id}").json()
            if state["status"] not in {"pending", "running", "cancelling"}:
                break
            time.sleep(0.2)

    assert state["status"] == "completed", state
    # 任务管理器必须把音色按 voice_id 传给引擎，而不是老的 sid
    assert captured["voice_id"] == "news-female"


def test_sweep_orphans_clears_leftover_audio(tmp_path, monkeypatch) -> None:
    """重启后合成记录消失，但音频文件仍留在磁盘上——不清理就会越积越多。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    audio_dir = tts_jobs.manager.audio_dir()
    audio_dir.mkdir(parents=True, exist_ok=True)
    keep = audio_dir / "abc.wav"
    keep.write_bytes(b"stale")
    (audio_dir / "def.wav").write_bytes(b"stale")

    tts_jobs.manager.sweep_orphans()

    assert not keep.exists()
    assert not (audio_dir / "def.wav").exists()


def test_sweep_orphans_keeps_custom_voices(tmp_path, monkeypatch) -> None:
    """只清合成产物，别碰用户上传的参考音频——那是要长期保存的。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    import soundfile as sf

    rate = 24000
    tone = 0.3 * np.sin(2 * np.pi * 200 * np.arange(rate) / rate)
    payload = io.BytesIO()
    sf.write(payload, tone.astype(np.float32), rate, format="WAV", subtype="PCM_16")
    voice = tts_engine.save_custom_voice("我的音色", "参考文本。", payload.getvalue())

    tts_jobs.manager.sweep_orphans()

    stored = tts_engine._custom_dir() / voice.id / "reference.wav"
    assert stored.is_file(), "自定义音色的参考音频不能被清掉"


def test_transcribe_reference_joins_chunks_and_reports_duration(tmp_path, monkeypatch) -> None:
    """自动转写：把识别引擎的分句拼成整段，并把时长带回给前端提示截断。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    media = _stub_media(monkeypatch, seconds=6.0)
    seen: dict[str, object] = {}

    class _Chunk:
        def __init__(self, text: str) -> None:
            self.text = text

    def fake_recognition(wav):
        seen["called"] = True
        return [_Chunk("各位村民，"), _Chunk("大家新年好。")], 6.84

    monkeypatch.setattr(tts_engine, "_run_recognition", fake_recognition)

    text, seconds, truncated = tts_engine.transcribe_reference(b"fake-audio")
    assert seen["called"] is True
    assert text == "各位村民，大家新年好。"
    assert seconds == 6.84
    assert truncated is False
    # 转换阶段就要按参考音频的长度上限裁掉，转写结果才和最终保存的音频是同一段
    assert media["max_seconds"] == tts_engine.REFERENCE_MAX_SECONDS


def test_transcribe_reference_flags_long_audio(tmp_path, monkeypatch) -> None:
    """超过上限的音频要如实回报「截断了」，否则文字会比音频长。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    _stub_media(monkeypatch, seconds=tts_engine.REFERENCE_MAX_SECONDS * 3)
    monkeypatch.setattr(
        tts_engine, "_run_recognition", lambda wav: ([_One("很长的音频")], 20.0)
    )

    _text, seconds, truncated = tts_engine.transcribe_reference(b"fake-audio")
    assert seconds == 20.0
    assert truncated is True


class _One:
    def __init__(self, text: str) -> None:
        self.text = text


def test_transcribe_reference_rejects_empty_audio(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with pytest.raises(ValueError, match="请先选择"):
        tts_engine.transcribe_reference(b"")


def test_transcribe_reference_rejects_silence(tmp_path, monkeypatch) -> None:
    """识别不到人声时要给出可操作的提示，而不是把空文本丢给前端。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    _stub_media(monkeypatch)
    monkeypatch.setattr(tts_engine, "_run_recognition", lambda wav: ([], 1.0))
    with pytest.raises(ValueError, match="没听清"):
        tts_engine.transcribe_reference(b"fake-audio")


def test_transcribe_endpoint_returns_text(tmp_path, monkeypatch) -> None:
    """端点把转写结果原样返回；空音频要在入口就拦住（不浪费一次识别）。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    def fake(audio: bytes):
        assert audio == b"x"
        return "参考文本。", 3.0, True

    monkeypatch.setattr(tts_engine, "transcribe_reference", fake)

    with TestClient(app) as client:
        ok = client.post(
            "/api/tts/voices/transcribe",
            files={"audio": ("ref.wav", b"x", "audio/wav")},
        )
        bad = client.post(
            "/api/tts/voices/transcribe",
            files={"audio": ("ref.wav", b"", "audio/wav")},
        )
    assert ok.status_code == 200
    assert ok.json() == {"text": "参考文本。", "seconds": 3.0, "truncated": True}
    assert bad.status_code == 400


def test_stale_voice_setting_falls_back_to_default(tmp_path, monkeypatch) -> None:
    """老设置里存的是上一版引擎的整数 sid，清掉后合成仍要能用（只是音色回到默认）。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    with SessionLocal() as session:
        update_settings(session, SettingPatch(tts_voice="47"))
        with TestClient(app) as client:
            response = client.post("/api/tts/jobs", json={"text": "测试一下。"})
    # 模型没装，提交会被拦下；但拦它的必须是「模型未安装」而不是「音色无效」
    assert response.status_code == 400
    assert "还没安装" in response.json()["detail"]


def test_custom_voice_roundtrip(tmp_path, monkeypatch) -> None:
    """上传的参考音频要落成 24kHz 单声道 WAV，并记住它的参考文本。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    # 造一段 8kHz 立体声、含直流偏置的测试音频
    rate = 8000
    tone = 0.4 * np.sin(2 * np.pi * 220 * np.arange(rate * 3) / rate)
    stereo = np.stack([tone, tone], axis=1) + 0.1
    buffer = io.BytesIO()
    import soundfile as sf

    sf.write(buffer, stereo.astype(np.float32), rate, format="WAV", subtype="PCM_16")
    payload = buffer.getvalue()

    voice = tts_engine.save_custom_voice("我的声音", "这是一段参考文本。", payload)
    assert voice.id
    assert voice.builtin is False
    assert voice.reference_text == "这是一段参考文本。"

    stored, stored_rate = sf.read(
        str(tts_engine._custom_dir() / voice.id / "reference.wav"), dtype="float32"
    )
    assert stored_rate == 24000, "参考音频要统一到输出采样率"
    assert stored.ndim == 1, "多声道要混成单声道"
    assert abs(float(np.mean(stored))) < 1e-3, "直流偏置要被去掉"
    assert float(np.abs(stored).max()) <= 1.0

    # 出现在音色清单里，且能被 find_voice 找到
    assert any(item.id == voice.id for item in tts_engine.voices())
    assert tts_engine.find_voice(voice.id).name == "我的声音"

    assert tts_engine.remove_custom_voice(voice.id) is True
    assert tts_engine.find_voice(voice.id) is None, "删掉后不该再出现在清单里"
    assert tts_engine.resolve_voice(voice.id).id == tts_engine.DEFAULT_VOICE


def test_custom_voice_rejects_bad_input(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    # 参考文本为空会让克隆出的音色明显变差，必须挡在入口
    with pytest.raises(ValueError, match="逐字一致"):
        tts_engine.save_custom_voice("名字", "  ", b"not-audio")
    with pytest.raises(ValueError, match="参考音频"):
        tts_engine.save_custom_voice("名字", "文本", b"")
    with pytest.raises(ValueError, match="无法读取"):
        tts_engine.save_custom_voice("名字", "文本", b"definitely not audio")


def test_builtin_voices_cannot_be_deleted() -> None:
    assert tts_engine.remove_custom_voice("leijun-1") is False
    assert tts_engine.remove_custom_voice("never-existed") is False


def test_text_cleanup_and_sentence_split() -> None:
    assert tts_engine.normalize_text("  你好 🎉 世界\u200d  \n\n\n再来  ") == "你好 世界 \n再来"
    # 碎句并进上一句，避免句间插太多静音
    assert tts_engine.split_sentences("大家好。今天我们聊三个点：先算成本，再选位置。谢谢。") == [
        "大家好。今天我们聊三个点：先算成本，再选位置。",
        "谢谢。",
    ]
    assert tts_engine.clamp_speed(9) == tts_engine.SPEED_RANGE[1]
    assert tts_engine.clamp_speed(0.1) == tts_engine.SPEED_RANGE[0]
    assert tts_engine.clamp_speed("bad") == tts_engine.DEFAULT_SPEED


def test_peak_ceiling_prevents_clipping() -> None:
    """ZipVoice 的输出会越过满刻度（实测峰值 1.18~1.32），削平后的失真听感就是「有噪音」。"""

    loud = np.array([0.5, -1.4, 1.2, 0.1], dtype=np.float32)
    result = tts_engine._normalize_peak(loud)
    peak = float(np.abs(result).max())
    assert peak <= tts_engine.PEAK_CEILING + 1e-5
    # 超限样本被压到上限上，而不是原样保留——原样保留写 PCM_16 时会被削平，
    # 那种失真听感就是「有噪音」，正是要避免的。
    assert abs(peak - tts_engine.PEAK_CEILING) < 1e-3
    assert peak < float(np.abs(loud).max())
    # 直流偏置也要一并去掉
    assert abs(float(np.mean(result))) < 1e-5

    # 本来就在范围内时不要动它，避免无谓的音量变化（数据取零均值，否则会先被去直流）
    quiet = np.array([0.1, -0.2, 0.1], dtype=np.float32)
    assert np.allclose(tts_engine._normalize_peak(quiet), quiet)


def test_tts_config_reports_a_missing_model(tmp_path, monkeypatch) -> None:
    """模型没装时：配置接口要说清去哪儿装，提交任务要直接 400（不白跑一次）。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        config = client.get("/api/tts")
        created = client.post("/api/tts/jobs", json={"text": "测试一下", "voice": "leijun-1"})
    assert config.status_code == 200
    body = config.json()
    assert body["ready"] is False
    assert "模型管理" in body["message"]
    assert body["model"]["id"] == ZIPVOICE
    assert body["max_chars"] == tts_engine.MAX_CHARS
    # 音色清单不依赖模型安装，所以没装模型也要给出来
    assert [item["id"] for item in body["voices"]]
    assert created.status_code == 400
    assert "还没安装" in created.json()["detail"]


def test_voice_endpoints_manage_custom_voices(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    rate = 24000
    tone = 0.3 * np.sin(2 * np.pi * 200 * np.arange(rate * 2) / rate)
    buffer = io.BytesIO()
    import soundfile as sf

    sf.write(buffer, tone.astype(np.float32), rate, format="WAV", subtype="PCM_16")

    with TestClient(app) as client:
        assert client.get("/api/tts/voices").status_code == 200
        created = client.post(
            "/api/tts/voices",
            data={"name": "我的声音", "reference_text": "参考文本内容。"},
            files={"audio": ("ref.wav", buffer.getvalue(), "audio/wav")},
        )
        assert created.status_code == 201
        voice_id = created.json()["id"]
        assert created.json()["builtin"] is False

        listed = client.get("/api/tts/voices").json()
        assert any(item["id"] == voice_id for item in listed)

        # 参考文本缺失要 400，不能存下一个必然难听的声音
        bad = client.post(
            "/api/tts/voices",
            data={"name": "缺文本", "reference_text": ""},
            files={"audio": ("ref.wav", tone.tobytes(), "audio/wav")},
        )
        assert bad.status_code == 422

        assert client.delete(f"/api/tts/voices/{voice_id}").json()["removed"] is True
        # 内置音色删不掉
        assert client.delete("/api/tts/voices/leijun-1").status_code == 400


def test_trim_voice_keeps_only_the_selected_window(tmp_path, monkeypatch) -> None:
    """裁剪是「就地」发生的：id 与目录不变，音频与时长都换成选区那一段。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "参考文本。", _longer_reference_wav_bytes())

    edit = tts_engine.trim_voice(voice.id, 1.0, 4.0)

    assert edit.voice.id == voice.id
    assert edit.voice.seconds == pytest.approx(3.0, abs=0.05)
    # 裁剪不动参考文本：用户没要求改，就不该被悄悄换掉
    assert edit.voice.reference_text == "参考文本。"
    assert edit.text_source == "kept"
    assert tts_engine.find_voice(voice.id).seconds == pytest.approx(3.0, abs=0.05)
    stored, rate = sf.read(str(tts_engine.reference_path(voice.id)), dtype="float32")
    assert stored.size / rate == pytest.approx(3.0, abs=0.05)


def test_trim_rejects_windows_that_are_too_short(tmp_path, monkeypatch) -> None:
    """太短的选区克隆出的音色不稳，要在保存前就说清楚，而不是留一个必然难听的声音。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "参考文本。", _longer_reference_wav_bytes())

    with pytest.raises(ValueError, match="太短"):
        tts_engine.trim_voice(voice.id, 0.0, 1.0)
    # 失败后原音频保持不变
    assert tts_engine.find_voice(voice.id).seconds == pytest.approx(6.0, abs=0.05)


def test_trim_clamps_an_out_of_range_selection(tmp_path, monkeypatch) -> None:
    """界面上拖出来的选区常常略微越界，这不该让一次本来没问题的操作失败。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "参考文本。", _longer_reference_wav_bytes())

    edit = tts_engine.trim_voice(voice.id, 4.0, 99.0)

    assert edit.voice.seconds == pytest.approx(2.0, abs=0.05)


def test_reference_text_keeps_its_old_value_when_transcribe_fails(tmp_path, monkeypatch) -> None:
    """替换音频时转写没成功，宁可沿用旧文本：抹掉会得到一个「没有参考文本」的音色。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "原来的参考文本。", _longer_reference_wav_bytes())

    def boom(_audio: bytes):
        raise EngineUnavailableError("缺少识别模型")

    monkeypatch.setattr(tts_engine, "transcribe_reference", boom)
    edit = tts_engine.replace_voice_audio(voice.id, _reference_wav_bytes())

    assert edit.text_source == "kept"
    assert edit.voice.reference_text == "原来的参考文本。"
    assert "缺少识别模型" in edit.note


def test_replacing_audio_reuses_the_typed_text_first(tmp_path, monkeypatch) -> None:
    """手填优先于转写：用户亲手核对过的文字比识别结果更可信。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "原来的参考文本。", _longer_reference_wav_bytes())
    monkeypatch.setattr(
        tts_engine,
        "transcribe_reference",
        lambda _audio: ("不该被调用", 1.0, False),
    )

    edit = tts_engine.replace_voice_audio(
        voice.id, _reference_wav_bytes(), name="新名字", reference_text="新音频里说的话。"
    )

    assert edit.text_source == "typed"
    assert edit.voice.name == "新名字"
    assert edit.voice.reference_text == "新音频里说的话。"


def test_rename_does_not_move_the_audio(tmp_path, monkeypatch) -> None:
    """音色 id 就是目录名，所以改名只是改元数据——参考音频必须留在原处。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "参考文本。", _reference_wav_bytes())
    before = tts_engine.reference_path(voice.id).read_bytes()

    renamed = tts_engine.rename_custom_voice(voice.id, "  口播音色  ")

    assert renamed.id == voice.id
    assert renamed.name == "口播音色"
    assert tts_engine.reference_path(voice.id).read_bytes() == before


def test_retranscribe_voice_rewrites_the_reference_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "手打的旧文本。", _reference_wav_bytes())
    monkeypatch.setattr(
        tts_engine,
        "transcribe_reference",
        lambda _audio: ("重新转写出来的文本。", 1.0, False),
    )

    updated = tts_engine.retranscribe_voice(voice.id)

    assert updated.reference_text == "重新转写出来的文本。"
    assert tts_engine.find_voice(voice.id).reference_text == "重新转写出来的文本。"


def test_retranscribe_reports_failure_instead_of_silently_skipping(tmp_path, monkeypatch) -> None:
    """这是用户明确发起的动作，静默跳过只会让他以为文字已经更新了。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "手打的旧文本。", _reference_wav_bytes())

    def boom(_audio: bytes):
        raise ValueError("没听清这段音频里说了什么")

    monkeypatch.setattr(tts_engine, "transcribe_reference", boom)
    with pytest.raises(ValueError, match="没听清"):
        tts_engine.retranscribe_voice(voice.id)
    assert tts_engine.find_voice(voice.id).reference_text == "手打的旧文本。"


def test_builtin_voice_reference_cannot_be_edited(tmp_path, monkeypatch) -> None:
    """内置音色的参考音频来自模型包，改它等于改了模型的示例文件。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with pytest.raises(ValueError, match="内置音色"):
        tts_engine.trim_voice(tts_engine.DEFAULT_VOICE, 0.0, 3.0)
    with pytest.raises(ValueError, match="内置音色"):
        tts_engine.rename_custom_voice(tts_engine.DEFAULT_VOICE, "改名")


def test_voice_edit_endpoints(tmp_path, monkeypatch) -> None:
    """编辑链路走接口：建音色 → 试听音频 → 裁剪 → 改名 → 重新转写 → 删除。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    monkeypatch.setattr(
        tts_engine,
        "transcribe_reference",
        lambda _audio: ("裁剪后重新转写的内容。", 3.0, False),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/tts/voices",
            data={"name": "我的声音", "reference_text": "参考文本内容。"},
            files={"audio": ("ref.wav", _longer_reference_wav_bytes(), "audio/wav")},
        )
        assert created.status_code == 201
        voice = created.json()
        voice_id = voice["id"]
        assert voice["audio_url"] == f"/api/tts/voices/{voice_id}/audio"
        assert voice["seconds"] == pytest.approx(6.0, abs=0.05)

        # 波形编辑要拿到原始音频才画得出波形
        audio = client.get(f"/api/tts/voices/{voice_id}/audio")
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"
        assert len(audio.content) > 44

        trimmed = client.post(
            f"/api/tts/voices/{voice_id}/trim",
            json={"start": 1.0, "end": 4.0, "retranscribe": True},
        )
        assert trimmed.status_code == 200
        assert trimmed.json()["voice"]["seconds"] == pytest.approx(3.0, abs=0.05)
        assert trimmed.json()["text_source"] == "transcribed"

        renamed = client.patch(f"/api/tts/voices/{voice_id}", json={"name": "口播音色"})
        assert renamed.status_code == 200
        assert renamed.json()["voice"]["name"] == "口播音色"

        # 内置音色与不存在的音色都要明确拒绝，而不是 500
        assert client.post("/api/tts/voices/leijun-1/trim", json={"start": 0, "end": 3}).status_code == 400
        assert client.get("/api/tts/voices/nope/audio").status_code == 404
        assert client.post("/api/tts/voices/nope/trim", json={"start": 0, "end": 3}).status_code == 400

        assert client.delete(f"/api/tts/voices/{voice_id}").json()["removed"] is True


def test_transcribe_voice_window_uses_the_selection_and_writes_nothing(tmp_path, monkeypatch) -> None:
    """编辑弹窗的「重新转写」：只转写选中的那一段，且不动盘。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "手打的旧文本。", _longer_reference_wav_bytes())
    captured: dict[str, float | None] = {}
    real = tts_engine.trim_window

    def spy(audio: bytes, start, end):
        captured["start"], captured["end"] = start, end
        return real(audio, start, end)

    monkeypatch.setattr(tts_engine, "trim_window", spy)
    monkeypatch.setattr(
        tts_engine,
        "transcribe_reference",
        lambda audio: (f"选区内的文字（{len(audio)} 字节）", 3.0, False),
    )

    window = tts_engine.transcribe_voice_window(voice.id, 1.0, 4.0)

    assert captured == {"start": 1.0, "end": 4.0}
    assert window.text.startswith("选区内的文字")
    assert window.seconds == 3.0
    # 不写盘：元数据与音频都得原样
    assert tts_engine.find_voice(voice.id).reference_text == "手打的旧文本。"


def test_transcribe_voice_window_keeps_text_on_failure(tmp_path, monkeypatch) -> None:
    """转写失败要报错，让界面保留原文——静默跳过等于骗用户。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    voice = tts_engine.save_custom_voice("我的音色", "手打的旧文本。", _longer_reference_wav_bytes())

    def boom(_audio: bytes):
        raise ValueError("没听清这段音频里说了什么")

    monkeypatch.setattr(tts_engine, "transcribe_reference", boom)
    with pytest.raises(ValueError, match="没听清"):
        tts_engine.transcribe_voice_window(voice.id, 0.0, 3.0)
    assert tts_engine.find_voice(voice.id).reference_text == "手打的旧文本。"


def test_preview_transcript_endpoint_leaves_the_voice_alone(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    monkeypatch.setattr(
        tts_engine,
        "transcribe_reference",
        lambda _audio: ("按选区转写的内容。", 3.0, False),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/tts/voices",
            data={"name": "我的声音", "reference_text": "原来的参考文本。"},
            files={"audio": ("ref.wav", _longer_reference_wav_bytes(), "audio/wav")},
        )
        voice_id = created.json()["id"]

        preview = client.post(
            f"/api/tts/voices/{voice_id}/preview-transcript",
            json={"start": 1.0, "end": 4.0},
        )
        assert preview.status_code == 200
        assert preview.json()["text"] == "按选区转写的内容。"
        # 接口只是回填编辑框，音色本身不能被动过
        assert client.get("/api/tts/voices").json()
        assert tts_engine.find_voice(voice_id).reference_text == "原来的参考文本。"

        assert client.post("/api/tts/voices/leijun-1/preview-transcript", json={}).status_code == 400
        assert client.post("/api/tts/voices/nope/preview-transcript", json={}).status_code == 400


def test_creating_a_voice_trims_to_the_given_window(tmp_path, monkeypatch) -> None:
    """新建时的选区：转写与保存取同一段，所以文字不会比音频长。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    captured: dict[str, bytes] = {}
    real = tts_engine.trim_window

    def spy(audio: bytes, start, end):
        window = real(audio, start, end)
        captured["window"] = window
        return window

    monkeypatch.setattr(tts_engine, "trim_window", spy)
    monkeypatch.setattr(
        tts_engine,
        "transcribe_reference",
        lambda audio: (f"选区内的文字（{len(audio)} 字节）", 3.0, False),
    )

    with TestClient(app) as client:
        transcribed = client.post(
            "/api/tts/voices/transcribe",
            data={"start": "1.0", "end": "4.0"},
            files={"audio": ("ref.wav", _longer_reference_wav_bytes(), "audio/wav")},
        )
        created = client.post(
            "/api/tts/voices",
            data={
                "name": "我的声音",
                "reference_text": transcribed.json()["text"],
                "start": "1.0",
                "end": "4.0",
            },
            files={"audio": ("ref.wav", _longer_reference_wav_bytes(), "audio/wav")},
        )

    assert transcribed.status_code == 200
    assert created.status_code == 201
    # 转写与保存两次请求取到的必须是同一段，否则核对时文字会和音频对不上
    assert captured["window"] == tts_engine.trim_window(_longer_reference_wav_bytes(), 1.0, 4.0)
    assert created.json()["seconds"] == pytest.approx(3.0, abs=0.05)


def test_retired_model_id_in_settings_does_not_break_the_page(tmp_path, monkeypatch) -> None:
    """升级路径：老用户设置里存的是上一版引擎的模型 id（已下架），页面不能因此整个报错。

    这是换引擎时最容易漏的一处——声音的失效 id 做了回落，模型这边一度是直接抛
    ModelNotFoundError 的，结果是升级后语音合成页直接 500，只能改数据库才能恢复。
    """

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    with SessionLocal() as session:
        update_settings(session, SettingPatch(tts_model="kokoro-int8-multi-lang-v1_1"))
        with TestClient(app) as client:
            config = client.get("/api/tts")
            # 显式指定已下架的 id 仍要报错（是调用方自己传错了）
            created = client.post(
                "/api/tts/jobs",
                json={"text": "测试一下。", "model_id": "kokoro-int8-multi-lang-v1_1"},
            )
    assert config.status_code == 200, "已下架的设置值不能让配置接口挂掉"
    assert config.json()["model"]["id"] == tts_engine.DEFAULT_MODEL_ID
    assert created.status_code == 400
    assert "未知的模型" in created.json()["detail"]


def test_tts_settings_roundtrip_and_bad_voice(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    with SessionLocal() as session:
        saved = update_settings(session, SettingPatch(tts_model=ZIPVOICE, tts_voice="news-female"))
        assert saved["tts_model"] == ZIPVOICE
        assert saved["tts_voice"] == "news-female"

        # 空串表示「跟随默认」
        update_settings(session, SettingPatch(tts_model="", tts_voice=None))
        session.execute(text("UPDATE app_settings SET value='   ' WHERE key='tts_voice'"))
        session.commit()
        current = read_settings(session)
        assert current["tts_model"] == ""
        assert current["tts_voice"] == ""


def test_synthesize_rejects_empty_and_too_long_text(monkeypatch) -> None:
    monkeypatch.setattr(tts_engine, "get_tts", lambda *_a, **_k: pytest.fail("不该加载模型"))
    with pytest.raises(ValueError, match="请先输入"):
        tts_engine.synthesize("   ", model_id=ZIPVOICE)
    with pytest.raises(ValueError, match="一次最多合成"):
        tts_engine.synthesize("字" * (tts_engine.MAX_CHARS + 1), model_id=ZIPVOICE)


# 四句等长的测试文本：每句都超过 MIN_SENTENCE_CHARS，不会被并句。
# 用汉字而非「1号/2号」：合成入口会把阿拉伯数字改写成汉字，数字开头会被变成「一号」，
# 那样下面按首字符取标记的断言就失真了。
FOUR = "一号句子内容测试。二号句子内容测试。三号句子内容测试。四号句子内容测试。"
TWO = "一号句子内容测试。二号句子内容测试。"
# 假实例用「句首字的码位」当标记，顺序对了值就该是这个序列
FOUR_MARKS = [float(ord(char)) for char in "一二三四"]


class _FakeTts:
    """假实例：波形里放一个能认出来的标记（句子首字符），用来验证拼接顺序。"""

    def __init__(self, lane: int) -> None:
        self.lane = lane
        self.sample_rate = 24000


@pytest.fixture
def fake_reference(tmp_path, monkeypatch):
    """把模型目录与参考音频都摆好，让合成只差实例本身。"""

    _install_sample(tmp_path, monkeypatch)


def _install_fake(monkeypatch, made: list[int]):
    """让 get_tts 返回假实例，并把 _speak 换成标记生成。

    参考音频的加载与句序编排都要真的跑到，所以只替换最内层的「问模型要音频」。
    """

    def factory(model_id=tts_engine.DEFAULT_MODEL_ID, lane=0):
        made.append(lane)
        return _FakeTts(lane)

    def fake_speak(tts, sentence, reference, rate, voice, speed):
        return np.array([float(ord(sentence[0]))], dtype=np.float32)

    monkeypatch.setattr(tts_engine, "get_tts", factory)
    monkeypatch.setattr(tts_engine, "_speak", fake_speak)


def test_lane_plan_scales_with_cores() -> None:
    """核数少就退回单实例；12 核摊成 4 个、每个 3 线程。"""

    assert tts_engine.lanes(2) == 1
    assert tts_engine.lanes(4) == 1
    assert tts_engine.lanes(8) == 2
    assert tts_engine.lanes(12) == 4
    # 核再多也不超上限：每个实例都要各自加载一份上百 MB 的模型
    assert tts_engine.lanes(64) == tts_engine.MAX_LANES
    assert tts_engine._threads(cores=12) == 3
    assert tts_engine._threads(cores=8) == 4
    assert tts_engine._threads(cores=4) == 4


def test_parallel_synthesis_keeps_sentence_order(tmp_path, monkeypatch) -> None:
    """并行合成必须按原句序拼接，且实例数不超过句子数。"""

    _install_sample(tmp_path, monkeypatch)
    made: list[int] = []
    _install_fake(monkeypatch, made)
    monkeypatch.setattr(tts_engine, "lanes", lambda cores=None: 3)

    samples, rate = tts_engine.synthesize(FOUR, model_id=ZIPVOICE, voice_id="leijun-1")

    assert rate == 24000
    assert sorted(made) == [0, 1, 2], "四个句子只该建三个实例"
    # 句间是静音（0），滤掉之后剩下的就是句序——并行乱了这里会立刻现形
    assert [value for value in samples if value] == FOUR_MARKS


def test_short_text_never_fans_out(tmp_path, monkeypatch) -> None:
    """短文本不摊开：一两句话不值得为此多加载几个上百 MB 的模型。"""

    _install_sample(tmp_path, monkeypatch)
    made: list[int] = []
    _install_fake(monkeypatch, made)
    monkeypatch.setattr(tts_engine, "lanes", lambda cores=None: 4)

    tts_engine.synthesize("一号句子内容测试。", model_id=ZIPVOICE, voice_id="leijun-1")
    assert made == [0], "单句走原路径，只用主实例"

    made.clear()
    tts_engine.synthesize(TWO, model_id=ZIPVOICE, voice_id="leijun-1")
    assert made == [0, 1], "两句时实例数按句子数收敛"


def test_synthesis_reports_progress_and_propagates_cancel(tmp_path, monkeypatch) -> None:
    """进度按完成句数上报；回调里抛出的取消要带回调用方。"""

    _install_sample(tmp_path, monkeypatch)
    _install_fake(monkeypatch, [])
    monkeypatch.setattr(tts_engine, "lanes", lambda cores=None: 3)

    seen: list[tuple[int, int]] = []
    tts_engine.synthesize(
        FOUR,
        model_id=ZIPVOICE,
        voice_id="leijun-1",
        on_progress=lambda d, t: seen.append((d, t)),
    )
    assert sorted(seen) == [(1, 4), (2, 4), (3, 4), (4, 4)]

    class _Cancel(RuntimeError):
        pass

    def boom(done: int, total: int) -> None:
        raise _Cancel("取消")

    with pytest.raises(_Cancel):
        tts_engine.synthesize(
            FOUR, model_id=ZIPVOICE, voice_id="leijun-1", on_progress=boom
        )


def test_missing_reference_audio_makes_the_model_incomplete(tmp_path, monkeypatch) -> None:
    """内置音色的参考音频属于模型包的一部分，缺了它就该判为未就绪。

    这样用户会在「模型管理」页看到「重新下载」，而不是等到合成时才收到一句
    「参考音频不见了」——后者不知道该点什么。
    """

    spec, directory = _install_sample(tmp_path, monkeypatch)
    (directory / "test_wavs" / "leijun-1.wav").unlink()

    status = model_store.inspect(spec)
    assert status.state == "partial"
    assert "test_wavs/leijun-1.wav" in status.message

    # 合成时也会被拦在入口，报的是「模型没装好，去重装」而不是一句含糊的参考音频丢失
    _install_fake(monkeypatch, [])
    tts_engine.reset_cache()
    with pytest.raises(EngineUnavailableError, match="模型管理"):
        tts_engine.synthesize("测试一下。", model_id=spec.id, voice_id="leijun-1")


def test_fp32_zipvoice_is_offered_as_an_option() -> None:
    """int8 听着有底噪时能换非量化版：默认仍推荐体积小的 int8。"""

    spec = require_spec("sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia")
    assert spec.archive is True
    assert spec.recommended is False
    assert [item.name for item in spec.files[1:]] == [VOCODER]
    assert tts_engine.DEFAULT_MODEL_ID == ZIPVOICE


def test_zipvoice_model_files_are_picked_in_pairs(tmp_path, monkeypatch) -> None:
    """编码器与解码器必须成对挑：不能 int8 配 fp32，也不能只有一半就凑。"""

    monkeypatch.setattr(settings, "models_dir", tmp_path / "models")
    int8_dir = model_store.spec_dir(require_spec("sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"))
    fp32_dir = model_store.spec_dir(require_spec("sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia"))
    int8_dir.mkdir(parents=True)
    fp32_dir.mkdir(parents=True)

    (int8_dir / "encoder.int8.onnx").write_bytes(b"x")
    (int8_dir / "decoder.int8.onnx").write_bytes(b"x")
    assert tts_engine._pick_model_pair(int8_dir)[0].name == "encoder.int8.onnx"  # noqa: SLF001

    # 另一代布局的文件名（text_encoder / fm_decoder）同样认
    (fp32_dir / "text_encoder.onnx").write_bytes(b"x")
    (fp32_dir / "fm_decoder.onnx").write_bytes(b"x")
    assert tts_engine._pick_model_pair(fp32_dir)[0].name == "text_encoder.onnx"  # noqa: SLF001

    # 混精度不能凑对：只有 fp32 编码器配 int8 解码器
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    (mixed / "encoder.onnx").write_bytes(b"x")
    (mixed / "decoder.int8.onnx").write_bytes(b"x")
    with pytest.raises(EngineUnavailableError):
        tts_engine._pick_model_pair(mixed)  # noqa: SLF001


def _fresh_tts_manager(tmp_path, monkeypatch) -> tts_jobs.TtsJobManager:
    """干净的库 + 新的 manager。

    记录现在落库、而数据库在同一进程内是共用的（engine 只建一次），所以测试之间
    必须自己把表清空，否则计数会被上一个测试的残留记录污染。
    """

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    with SessionLocal() as session:
        session.execute(text("DELETE FROM tts_jobs"))
        session.commit()
    return tts_jobs.TtsJobManager()


def test_tts_records_persist_across_managers(tmp_path, monkeypatch) -> None:
    """合成记录要落库：换个 manager（等同重启进程）之后仍查得到、能删、删时带走音频。"""

    manager = _fresh_tts_manager(tmp_path, monkeypatch)
    audio = manager.audio_dir() / "job1.wav"
    audio.write_bytes(b"RIFF")
    manager._store(  # noqa: SLF001
        tts_jobs.TtsJob(
            id="job1",
            voice="v",
            text="要能回填的原文。",
            chars=10,
            status="completed",
            audio_path=str(audio),
        )
    )

    fresh = tts_jobs.TtsJobManager()  # 新进程里的 manager
    loaded = fresh.get("job1")
    assert loaded is not None
    assert loaded.status == "completed"
    assert loaded.audio_path == str(audio)
    assert loaded.ready is True
    assert loaded.text == "要能回填的原文。"
    assert [item.id for item in fresh.recent()] == ["job1"]

    assert fresh.remove("job1") is True
    assert not audio.exists()
    assert fresh.get("job1") is None


def test_older_tts_jobs_table_gets_the_text_column(tmp_path, monkeypatch) -> None:
    """老库升级：tts_jobs 原本没有 text 列，启动时要能补上（补不上重启就失败）。

    补出来的列对已有行是空串而不是 NULL——那些记录确实没有原文，前端按「空则不动
    输入框」处理，不能因此报错。
    """

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    try:
        with SessionLocal() as session:
            session.execute(text("DROP TABLE tts_jobs"))
            session.execute(
                text(
                    "CREATE TABLE tts_jobs (id VARCHAR(32) PRIMARY KEY, "
                    "status VARCHAR(16) DEFAULT 'pending', message VARCHAR(200), "
                    "model_id VARCHAR(120), voice VARCHAR(120), speed FLOAT, "
                    "chars INTEGER, duration FLOAT, elapsed FLOAT, error TEXT, "
                    "audio_name VARCHAR(80), created_at DATETIME, updated_at DATETIME)"
                )
            )
            session.execute(text("INSERT INTO tts_jobs (id, status) VALUES ('old', 'completed')"))
            session.commit()

        database._add_missing_columns()  # noqa: SLF001 - 启动时干的就是这件事

        with SessionLocal() as session:
            columns = {
                row[1]
                for row in session.execute(text("PRAGMA table_info(tts_jobs)"))
            }
            value = session.execute(text("SELECT text FROM tts_jobs WHERE id = 'old'")).scalar()
        assert "text" in columns
        assert value == ""
    finally:
        # 把标准表建回来，免得给后面的用例留下一个缺列的表
        with SessionLocal() as session:
            session.execute(text("DROP TABLE tts_jobs"))
            session.commit()
        init_database()


def test_submitted_text_is_kept_with_the_record(tmp_path, monkeypatch, fake_reference) -> None:
    """提交时的原文要留在记录里：点历史记录能把它填回输入框继续改。"""

    manager = _fresh_tts_manager(tmp_path, monkeypatch)
    _install_fake(monkeypatch, [])
    text = "这段原文要能回填到输入框。"
    job = manager.submit(text, model_id=ZIPVOICE, voice=tts_engine.DEFAULT_VOICE, speed=1.0)

    # 记录是同步建好的，原文立刻就在
    assert job.text == text

    # 等后台线程收工，再从库里读一遍：确认原文不只是留在内存对象上
    for _ in range(200):
        if not manager.is_busy():
            break
        time.sleep(0.05)
    loaded = manager.get(job.id)
    assert loaded is not None
    assert loaded.status == "completed"
    assert loaded.text == text


def test_interrupted_jobs_are_marked_on_startup(tmp_path, monkeypatch) -> None:
    """上次退出时还在跑的任务要标成中断，否则会永远停在「合成中」。"""

    manager = _fresh_tts_manager(tmp_path, monkeypatch)
    for index, status in enumerate(("pending", "running", "cancelling", "completed")):
        manager._store(tts_jobs.TtsJob(id=f"j{index}", status=status))  # noqa: SLF001

    manager.mark_interrupted()

    for index in range(3):
        job = manager.get(f"j{index}")
        assert job is not None and job.status == "failed"
        assert "中断" in job.message
    done = manager.get("j3")
    assert done is not None and done.status == "completed"


def test_sweep_orphans_keeps_referenced_audio(tmp_path, monkeypatch) -> None:
    """记录落库之后不能再清空音频目录：只清没有记录引用的孤儿文件。"""

    manager = _fresh_tts_manager(tmp_path, monkeypatch)
    kept = manager.audio_dir() / "keep.wav"
    kept.write_bytes(b"RIFF")
    orphan = manager.audio_dir() / "orphan.wav"
    orphan.write_bytes(b"RIFF")
    manager._store(tts_jobs.TtsJob(id="keep", status="completed", audio_path=str(kept)))  # noqa: SLF001

    manager.sweep_orphans()

    assert kept.exists()
    assert not orphan.exists()


def test_clear_finished_keeps_running_job(tmp_path, monkeypatch) -> None:
    """清空只动已结束的记录，正在跑的那条留着。"""

    manager = _fresh_tts_manager(tmp_path, monkeypatch)
    done_audio = manager.audio_dir() / "done.wav"
    done_audio.write_bytes(b"RIFF")
    manager._store(  # noqa: SLF001
        tts_jobs.TtsJob(id="done", status="completed", audio_path=str(done_audio))
    )
    running = tts_jobs.TtsJob(id="live", status="running")
    with manager._lock:  # noqa: SLF001 - 模拟「正在跑」的任务
        manager._active["live"] = running  # noqa: SLF001
    manager._store(running)  # noqa: SLF001

    removed = manager.clear_finished()

    assert removed == 1
    assert not done_audio.exists()
    assert manager.get("done") is None
    assert manager.get("live") is not None


def test_missing_lexicon_is_reported_instead_of_crashing(tmp_path) -> None:
    """缺 lexicon.txt 要给出可读报错：sherpa-onnx 自己会直接中止进程，连异常都抛不出来。"""

    directory = tmp_path / "model"
    directory.mkdir()
    with pytest.raises(EngineUnavailableError, match="lexicon"):
        tts_engine._require_lexicon(directory)  # noqa: SLF001

    (directory / "lexicon.txt").write_bytes(b"zhong z0 ong1\n")
    assert tts_engine._require_lexicon(directory).name == "lexicon.txt"  # noqa: SLF001


def test_espeak_failure_becomes_an_actionable_message(tmp_path, monkeypatch) -> None:
    """espeak-ng 初始化失败是底层报错，要转成「重启 / 重装模型」这种能照做的提示。"""

    _install_sample(tmp_path, monkeypatch)

    class _Boom:
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("Failed to set eSpeak-ng voice")

    module = types.SimpleNamespace(
        OfflineTts=_Boom,
        OfflineTtsConfig=lambda **kwargs: kwargs,
        OfflineTtsModelConfig=lambda **kwargs: kwargs,
        OfflineTtsZipvoiceModelConfig=lambda **kwargs: kwargs,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    tts_engine.reset_cache()

    with pytest.raises(EngineUnavailableError) as caught:
        tts_engine.get_tts(ZIPVOICE)
    message = str(caught.value)
    assert "espeak" in message
    assert "重启" in message
    tts_engine.reset_cache()