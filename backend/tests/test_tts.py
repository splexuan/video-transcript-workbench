"""语音合成：归档安装、音色表、文本处理与接口。

不真的加载 Kokoro（那是几百 MB 的模型），只验证编排与契约。
"""
import io
import sys
import tarfile
import types
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.application.services import read_settings, update_settings
from app.config import settings
from app.domain import EngineUnavailableError
from app.infrastructure import model_store, tts_engine
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.model_catalog import require_spec
from app.main import app
from app.schemas import SettingPatch

KOKORO = "kokoro-int8-multi-lang-v1_1"


def _make_tar(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:bz2") as handle:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            handle.addfile(info, io.BytesIO(payload))


def _extract_sample(tmp_path: Path, monkeypatch) -> Path:
    """把一份「像官方包那样带顶层目录」的 tar 解压到模型目录，返回该目录。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    spec = require_spec(KOKORO)
    directory = model_store.spec_dir(spec)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / spec.files[0].name
    _make_tar(
        archive,
        {
            f"{KOKORO}/model.int8.onnx": b"model",
            f"{KOKORO}/voices.bin": b"voices",
            f"{KOKORO}/tokens.txt": b"tokens",
            f"{KOKORO}/lexicon-zh.txt": b"lexicon",
            f"{KOKORO}/espeak-ng-data/phontab": b"phontab",
            "../evil.txt": b"evil",
        },
    )
    model_store._extract_archive(archive, directory)  # noqa: SLF001 - 直接验证解压这一层
    return directory


def test_kokoro_is_an_archive_tts_model() -> None:
    """Kokoro 官方只发 tar.bz2：规格要标成归档型，并声明解压后的必需路径。"""

    spec = require_spec(KOKORO)
    assert spec.engine.value == "tts"
    assert spec.archive is True
    assert spec.files[0].name.endswith(".tar.bz2")
    assert spec.files[0].urls and spec.files[0].urls[0].startswith("https://")
    assert "model.int8.onnx" in spec.required_paths
    assert "espeak-ng-data" in spec.required_paths


def test_inspect_reports_missing_before_extract(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    status = model_store.inspect(require_spec(KOKORO))
    assert status.state == "missing"
    assert status.path is None


def test_archive_extract_strips_top_dir_and_blocks_escape(tmp_path, monkeypatch) -> None:
    """解压要把包名那层目录去掉，并且挡住用 ../ 写到模型目录外的成员。"""

    directory = _extract_sample(tmp_path, monkeypatch)
    assert (directory / "model.int8.onnx").read_bytes() == b"model"
    assert (directory / "espeak-ng-data" / "phontab").read_bytes() == b"phontab"
    # 临时目录要清掉，包名那层目录不能留下来
    assert not (directory / "_extract").exists()
    assert not (directory / KOKORO).exists()
    # 路径穿越的成员要被丢掉
    assert not (directory / "evil.txt").exists()
    assert not (tmp_path / "data" / "models" / "evil.txt").exists()


def test_extracted_archive_counts_as_ready(tmp_path, monkeypatch) -> None:
    _extract_sample(tmp_path, monkeypatch)
    status = model_store.inspect(require_spec(KOKORO))
    assert status.state == "ready"
    assert status.installed_bytes > 0


def test_engine_exposes_tts_with_sherpa_package() -> None:
    from app.infrastructure.model_catalog import ENGINE_LABELS, ENGINE_PACKAGES

    assert ENGINE_LABELS["tts"] == "语音合成"
    assert ENGINE_PACKAGES["tts"] == "sherpa_onnx"


def test_voice_table_matches_kokoro_v1_1() -> None:
    """音色表必须与官方模型卡的说话人顺序一致，否则音色会整体错位。"""

    assert len(tts_engine.VOICES) == 103
    assert tts_engine.VOICES[0] == "af_maple"
    assert tts_engine.VOICES[3] == "zf_001"
    assert tts_engine.VOICES[57] == "zf_099"
    assert tts_engine.VOICES[58] == "zm_009"
    assert tts_engine.VOICES[102] == "zm_100"

    groups = {group["label"]: len(group["voices"]) for group in tts_engine.voice_groups()}
    assert groups == {"中文女声": 55, "中文男声": 45, "英文": 3}
    # 默认音色落在中文女声区间里
    assert tts_engine.VOICES[tts_engine.DEFAULT_VOICE].startswith("zf_")


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


def test_tts_config_reports_a_missing_model(tmp_path, monkeypatch) -> None:
    """模型没装时：配置接口要说清去哪儿装，提交任务要直接 400（不白跑一次）。"""

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with TestClient(app) as client:
        config = client.get("/api/tts")
        created = client.post("/api/tts/jobs", json={"text": "测试一下", "voice": 3})
    assert config.status_code == 200
    body = config.json()
    assert body["ready"] is False
    assert "模型管理" in body["message"]
    assert body["model"]["id"] == KOKORO
    assert body["max_chars"] == tts_engine.MAX_CHARS
    assert created.status_code == 400
    assert "还没安装" in created.json()["detail"]


def test_tts_settings_roundtrip_and_bad_voice(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    init_database()
    with SessionLocal() as session:
        saved = update_settings(session, SettingPatch(tts_model=KOKORO, tts_voice=47))
        assert saved["tts_model"] == KOKORO
        assert saved["tts_voice"] == 47

        # 空串表示「跟随默认」，坏值按未设置处理（不能把字符串塞给前端）
        update_settings(session, SettingPatch(tts_model="", tts_voice=None))
        session.execute(text("UPDATE app_settings SET value='abc' WHERE key='tts_voice'"))
        session.commit()
        current = read_settings(session)
        assert current["tts_model"] == ""
        assert current["tts_voice"] is None


def test_synthesize_rejects_empty_and_too_long_text(monkeypatch) -> None:
    monkeypatch.setattr(tts_engine, "get_tts", lambda *_args, **_kwargs: pytest.fail("不该加载模型"))
    with pytest.raises(ValueError):
        tts_engine.synthesize("   ", model_id=KOKORO)
    with pytest.raises(ValueError):
        tts_engine.synthesize("字" * (tts_engine.MAX_CHARS + 1), model_id=KOKORO)


# 四句等长的测试文本：每句都超过 MIN_SENTENCE_CHARS，不会被并句。
FOUR = "1号句子内容测试。2号句子内容测试。3号句子内容测试。4号句子内容测试。"
TWO = "1号句子内容测试。2号句子内容测试。"


class _FakeAudio:
    def __init__(self, samples) -> None:
        self.samples = samples


class _FakeTts:
    """假实例：波形里放一个能认出来的标记（句子首字符），用来验证拼接顺序。"""

    def __init__(self, lane: int) -> None:
        self.lane = lane
        self.sample_rate = 24000
        self.num_speakers = 103

    def generate(self, text, sid=0, speed=1.0):  # noqa: ARG002 - 只要签名一致
        return _FakeAudio(np.array([float(text[0])], dtype=np.float32))


def _fake_get_tts(made: list[int]):
    def factory(model_id=tts_engine.DEFAULT_MODEL_ID, lane=0):  # noqa: ARG001 - 只关心槽位
        made.append(lane)
        return _FakeTts(lane)

    return factory


def test_lane_plan_scales_with_cores() -> None:
    """核数少就退回单实例（与原来完全一致）；12 核摊成 4 个、每个 3 线程。"""

    assert tts_engine.lanes(2) == 1
    assert tts_engine.lanes(4) == 1
    assert tts_engine.lanes(8) == 2
    assert tts_engine.lanes(12) == 4
    # 核再多也不超上限：每个实例都要各自加载一份几百 MB 的模型
    assert tts_engine.lanes(64) == tts_engine.MAX_LANES
    assert tts_engine._threads(cores=12) == 3  # noqa: SLF001 - 线程规划本身就是要测的东西
    assert tts_engine._threads(cores=8) == 4  # noqa: SLF001
    assert tts_engine._threads(cores=4) == 4  # noqa: SLF001


def test_parallel_synthesis_keeps_sentence_order(monkeypatch) -> None:
    """并行合成必须按原句序拼接，且实例数不超过句子数。"""

    made: list[int] = []
    monkeypatch.setattr(tts_engine, "get_tts", _fake_get_tts(made))
    monkeypatch.setattr(tts_engine, "lanes", lambda cores=None: 3)

    samples, rate = tts_engine.synthesize(FOUR, model_id=KOKORO)

    assert rate == 24000
    assert sorted(made) == [0, 1, 2], "四个句子只该建三个实例"
    # 句间是静音（0），滤掉之后剩下的就是句序——并行乱了这里会立刻现形
    assert [value for value in samples if value] == [1.0, 2.0, 3.0, 4.0]


def test_short_text_never_fans_out(monkeypatch) -> None:
    """短文本不摊开：一两句话不值得为此多加载几个几百 MB 的模型。"""

    made: list[int] = []
    monkeypatch.setattr(tts_engine, "get_tts", _fake_get_tts(made))
    monkeypatch.setattr(tts_engine, "lanes", lambda cores=None: 4)

    tts_engine.synthesize("1号句子内容测试。", model_id=KOKORO)
    assert made == [0], "单句走原路径，只用主实例"

    made.clear()
    tts_engine.synthesize(TWO, model_id=KOKORO)
    assert made == [0, 1], "两句时实例数按句子数收敛"


def test_parallel_reports_progress_and_propagates_cancel(monkeypatch) -> None:
    """进度按完成句数上报；回调里抛出的取消要带回调用方。"""

    monkeypatch.setattr(tts_engine, "get_tts", _fake_get_tts([]))
    monkeypatch.setattr(tts_engine, "lanes", lambda cores=None: 3)

    seen: list[tuple[int, int]] = []
    tts_engine.synthesize(FOUR, model_id=KOKORO, on_progress=lambda d, t: seen.append((d, t)))
    assert sorted(seen) == [(1, 4), (2, 4), (3, 4), (4, 4)]

    class _Cancel(RuntimeError):
        pass

    def boom(done: int, total: int) -> None:
        raise _Cancel("取消")

    with pytest.raises(_Cancel):
        tts_engine.synthesize(FOUR, model_id=KOKORO, on_progress=boom)


def test_high_precision_kokoro_is_offered_as_an_option() -> None:
    """嫌 int8 沙哑时要能换非量化版：它同样是归档安装，默认仍推荐体积小的 int8。"""

    from app.infrastructure.model_catalog import KOKORO_FP32_PATHS, KOKORO_V1_1_PATHS

    spec = require_spec("kokoro-multi-lang-v1_1")
    assert spec.archive is True
    assert spec.files[0].name.endswith(".tar.bz2")
    assert spec.recommended is False
    # 两版包里的模型文件名不同，完整性检查不能写死某一个
    assert set(KOKORO_FP32_PATHS) <= set(KOKORO_V1_1_PATHS)
    assert "voices.bin" in KOKORO_FP32_PATHS
    assert tts_engine.DEFAULT_MODEL_ID == KOKORO


def test_espeak_failure_becomes_an_actionable_message(tmp_path, monkeypatch) -> None:
    """espeak-ng 初始化失败是底层报错，要转成「重启 / 重装模型」这种能照做的提示。"""

    _extract_sample(tmp_path, monkeypatch)

    class _Boom:
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("Failed to set eSpeak-ng voice")

    module = types.SimpleNamespace(
        OfflineTts=_Boom,
        OfflineTtsConfig=lambda **kwargs: kwargs,
        OfflineTtsModelConfig=lambda **kwargs: kwargs,
        OfflineTtsKokoroModelConfig=lambda **kwargs: kwargs,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    tts_engine.reset_cache()

    with pytest.raises(EngineUnavailableError) as caught:
        tts_engine.get_tts(KOKORO)
    message = str(caught.value)
    assert "espeak" in message
    assert "重启" in message
    tts_engine.reset_cache()
