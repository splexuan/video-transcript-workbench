from pathlib import Path

from app.application import worker as worker_module
from app.application.services import create_job
from app.application.worker import LocalWorker
from app.domain import MediaInfo, TranscriptChunk
from app.infrastructure import fallback_api
from app.infrastructure.credential_store import CredentialError
from app.infrastructure.database import SessionLocal, init_database
from app.infrastructure.model_catalog import require_spec
from app.infrastructure.models import Document, Job
from app.infrastructure.platform_media import PlatformError
from app.schemas import JobCreate


def stub_engine(monkeypatch, spec_id: str) -> None:
    """让 Worker 跳过真实的模型/依赖检查，直接返回指定模型规格。"""

    spec = require_spec(spec_id)
    monkeypatch.setattr(
        worker_module,
        "ensure_engine_ready",
        lambda _engine, _label: (spec, Path(".")),
    )


def test_local_worker_persists_transcript(monkeypatch, tmp_path) -> None:
    init_database()
    source = tmp_path / "voice.wav"
    source.write_bytes(b"placeholder")

    monkeypatch.setattr(
        worker_module,
        "probe_media",
        lambda _path: MediaInfo(title="本地录音", platform="local", duration_seconds=3),
    )
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: source)
    stub_engine(monkeypatch, "sensevoice-small")
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 3000, "测试文案")], 3.0),
    )

    with SessionLocal() as session:
        job = create_job(session, JobCreate(source_type="file", source=str(source), mode="auto"))
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.document_id is not None
        # 本地文件没有字幕可读，必须记录成识别来源
        assert job.transcript_source == "asr"
        assert job.model_id == "sensevoice-small"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.title == "本地录音"
        assert document.status == "draft"
        assert document.segments[0].text == "测试文案"


def test_bilibili_worker_prefers_platform_subtitles(monkeypatch) -> None:
    init_database()
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: MediaInfo(title="字幕视频", platform="bilibili", duration_seconds=8),
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_subtitles",
        lambda *_args, **_kwargs: [TranscriptChunk(0, 8000, "平台字幕")],
    )
    monkeypatch.setattr(
        worker_module,
        "download_audio_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应下载音轨")),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://www.bilibili.com/video/BV1test", mode="auto"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.transcript_source == "subtitle"
        assert job.model_name is None
        document = session.get(Document, job.document_id)
        assert document is not None
        # 平台字幕原样保留：不补标点（字幕没有标点，规则补不出正确位置）
        assert document.segments[0].text == "平台字幕"


def test_platform_subtitles_skip_audio_download(monkeypatch) -> None:
    """开启优先字幕时：平台接口命中字幕，就不该再去动 yt-dlp 和音轨。"""

    init_database()
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: MediaInfo(title="AI 字幕视频", platform="bilibili", duration_seconds=10),
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_platform_subtitles",
        lambda *_args, **_kwargs: [TranscriptChunk(0, 10000, "AI 字幕")],
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("平台字幕已命中，不该再走 yt-dlp 字幕")),
    )
    monkeypatch.setattr(
        worker_module,
        "download_audio_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不该下载音轨")),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://www.bilibili.com/video/BV1aitest", mode="auto"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.transcript_source == "subtitle"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.segments[0].text == "AI 字幕"


def test_bilibili_worker_falls_back_to_local_asr(monkeypatch, tmp_path) -> None:
    init_database()
    audio = tmp_path / "source.m4a"
    wav = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    wav.write_bytes(b"wav")
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: MediaInfo(title="无字幕视频", platform="bilibili", duration_seconds=6),
    )
    monkeypatch.setattr(worker_module, "fetch_subtitles", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(worker_module, "download_audio_source", lambda *_args, **_kwargs: audio)
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: wav)
    stub_engine(monkeypatch, "sensevoice-small")
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 6000, "回退识别")], 6.0),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://www.bilibili.com/video/BV1fallback", mode="fast"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.transcript_source == "asr"
        assert job.model_id == "sensevoice-small"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.segments[0].text == "回退识别"


def test_accurate_mode_uses_faster_whisper_engine(monkeypatch, tmp_path) -> None:
    init_database()
    source = tmp_path / "voice.wav"
    source.write_bytes(b"placeholder")
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        worker_module,
        "probe_media",
        lambda _path: MediaInfo(title="精准视频", platform="local", duration_seconds=12),
    )
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: source)
    stub_engine(monkeypatch, "faster-whisper-small")

    def fake_accurate(wav_path: Path, model_dir: Path, callback, requested_device=None):  # type: ignore[no-untyped-def]
        seen["wav"] = wav_path
        seen["model_dir"] = model_dir
        seen["device"] = requested_device
        callback(6000, 12000)
        callback(12000, 12000)
        return [TranscriptChunk(0, 4200, "精准片段一"), TranscriptChunk(4200, 12000, "精准片段二")], 12.0

    monkeypatch.setattr(worker_module.whisper_engine, "transcribe", fake_accurate)
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda *_args: (_ for _ in ()).throw(AssertionError("精准模式不应调用 SenseVoice")),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="file", source=str(source), mode="accurate"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.stage == "done"
        assert job.transcript_source == "asr"
        assert job.model_name == "faster-whisper small（推荐）"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert [item.text for item in document.segments] == ["精准片段一", "精准片段二"]
        assert document.segments[1].start_ms == 4200

    assert seen["model_dir"] == Path(".")


def test_disabling_prefer_subtitle_skips_platform_subtitles(monkeypatch, tmp_path) -> None:
    """关掉「优先平台字幕」后必须跳过硬幕，直接按所选模型转写。"""

    init_database()
    audio = tmp_path / "source.m4a"
    wav = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    wav.write_bytes(b"wav")
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: MediaInfo(title="强制转写", platform="bilibili", duration_seconds=5),
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_platform_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("关闭后不该读取平台字幕")),
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("关闭后不该走 yt-dlp 字幕")),
    )
    monkeypatch.setattr(worker_module, "download_audio_source", lambda *_args, **_kwargs: audio)
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: wav)
    monkeypatch.setattr(
        worker_module,
        "ensure_model_ready",
        lambda model_id, _label: (require_spec(model_id), Path(".")),
    )
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 5000, "模型转写")], 5.0),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(
                source_type="url",
                source="https://www.bilibili.com/video/BV1sktest",
                model_id="sensevoice-small",
                prefer_subtitle=False,
            ),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.transcript_source == "asr"
        assert job.model_id == "sensevoice-small"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.segments[0].text == "模型转写"


def test_requested_model_overrides_mode(monkeypatch, tmp_path) -> None:
    """首页指定了模型就按它走：mode 是 auto 也照样用 faster-whisper。"""

    init_database()
    source = tmp_path / "voice.wav"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        worker_module,
        "probe_media",
        lambda _path: MediaInfo(title="指定模型", platform="local", duration_seconds=4),
    )
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: source)
    monkeypatch.setattr(
        worker_module,
        "ensure_model_ready",
        lambda model_id, _label: (require_spec(model_id), Path(".")),
    )
    monkeypatch.setattr(
        worker_module.whisper_engine,
        "transcribe",
        lambda *_args, **_kwargs: ([TranscriptChunk(0, 4000, "指定模型片段")], 4.0),
    )
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda *_args: (_ for _ in ()).throw(AssertionError("指定 faster-whisper 时不应调用 SenseVoice")),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(
                source_type="file",
                source=str(source),
                mode="auto",
                model_id="faster-whisper-small",
            ),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.requested_model_id == "faster-whisper-small"
        assert job.model_id == "faster-whisper-small"
        assert job.transcript_source == "asr"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.segments[0].text == "指定模型片段"


def test_accurate_mode_reports_missing_engine(monkeypatch, tmp_path) -> None:
    """精准引擎未安装时必须给出可理解的错误，而不是崩溃。"""

    init_database()
    source = tmp_path / "voice.wav"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(
        worker_module,
        "probe_media",
        lambda _path: MediaInfo(title="缺少引擎", platform="local", duration_seconds=5),
    )
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: source)
    monkeypatch.setattr(
        worker_module,
        "ensure_engine_ready",
        lambda *_args: (_ for _ in ()).throw(
            worker_module.EngineUnavailableError("缺少 faster-whisper 组件，请先安装后再使用精准时间轴")
        ),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="file", source=str(source), mode="accurate"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error_code == "ENGINE_UNAVAILABLE"
        assert "faster-whisper" in (job.error_message or "")


def test_douyin_worker_uses_cookie_and_skips_subtitles(monkeypatch, tmp_path) -> None:
    """抖音没有平台字幕：直接下载音轨，并把 Cookie 临时文件交给下载器、用完即删。"""

    init_database()
    seen: dict[str, object] = {}
    audio = tmp_path / "source.m4a"
    wav = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    wav.write_bytes(b"wav")
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    monkeypatch.setattr(
        worker_module,
        "materialize_cookie_file",
        lambda platform: cookie_file if platform == "douyin" else None,
    )
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda url, platform, cookies=None: (
            seen.update({"platform": platform, "cookies": cookies})
            or MediaInfo(title="抖音作品", platform=platform, duration_seconds=9)
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_platform_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("抖音不该走 B站字幕接口")),
    )
    monkeypatch.setattr(
        worker_module,
        "download_audio_source",
        lambda url, workspace, platform, cookies=None: (
            seen.update({"download_cookies": cookies}) or audio
        ),
    )
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: wav)
    stub_engine(monkeypatch, "sensevoice-small")
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 9000, "抖音文案")], 9.0),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://www.douyin.com/video/123", mode="fast"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.transcript_source == "asr"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.segments[0].text == "抖音文案"

    assert seen["platform"] == "douyin"
    assert seen["cookies"] == cookie_file
    assert seen["download_cookies"] == cookie_file
    # Cookie 只在任务期间落盘，任务结束必须清掉
    assert not cookie_file.exists()


def test_bilibili_subtitle_request_receives_cookie(monkeypatch, tmp_path) -> None:
    """B站配了访问凭据时，字幕接口也要拿到它——会员视频的字幕是登录后才可见的。"""

    init_database()
    seen: dict[str, object] = {}
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    monkeypatch.setattr(
        worker_module,
        "materialize_cookie_file",
        lambda platform: cookie_file if platform == "bilibili" else None,
    )
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: MediaInfo(title="会员视频", platform="bilibili", duration_seconds=20),
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_platform_subtitles",
        lambda url, cookies=None: (
            seen.update({"url": url, "cookies": cookies}) or [TranscriptChunk(0, 20000, "会员字幕")]
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "download_audio_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("字幕已命中，不该下载音轨")),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(
                source_type="url",
                source="https://www.bilibili.com/video/BV1xx411c7mD",
                mode="fast",
            ),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.transcript_source == "subtitle"

    assert seen["cookies"] == cookie_file
    assert not cookie_file.exists()


def test_kuaishou_worker_uses_audio_pipeline_without_cookie(monkeypatch, tmp_path) -> None:
    """快手既不依赖下载器也不依赖登录态：解析分享页后直接走音轨识别。

    平台层的解析细节在 test_kuaishou.py 里验证，这里只确认任务编排：
    不读平台字幕、不需要 Cookie、识别结果正确落到文案上。
    """

    init_database()
    seen: dict[str, object] = {}
    audio = tmp_path / "source.mp4"
    wav = tmp_path / "audio.wav"
    audio.write_bytes(b"video")
    wav.write_bytes(b"wav")

    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda url, platform, cookies=None: (
            seen.update({"url": url, "platform": platform, "cookies": cookies})
            or MediaInfo(title="快手视频", platform=platform, duration_seconds=12)
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_platform_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("快手没有平台字幕")),
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("快手没有可读字幕")),
    )
    monkeypatch.setattr(worker_module, "download_audio_source", lambda *_args, **_kwargs: audio)
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: wav)
    stub_engine(monkeypatch, "sensevoice-small")
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 12000, "快手文案")], 12.0),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://v.kuaishou.com/example", mode="fast"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.transcript_source == "asr"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.platform == "kuaishou"
        assert document.segments[0].text == "快手文案"

    # 快手没有凭据可配，解析时传下去的应当是 None
    assert seen["platform"] == "kuaishou"
    assert seen["cookies"] is None


def test_worker_persists_source_metadata(monkeypatch, tmp_path) -> None:
    """来源作者与作品介绍要落到文档上，封面交给 save_cover 另存到本机。"""

    init_database()
    seen: dict[str, object] = {}
    audio = tmp_path / "source.mp4"
    wav = tmp_path / "audio.wav"
    audio.write_bytes(b"video")
    wav.write_bytes(b"wav")

    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: MediaInfo(
            title="快手视频",
            platform="kuaishou",
            duration_seconds=12,
            uploader="好想吃巧克力",
            thumbnail="https://p5.a.yximgs.com/upic/cover.jpg",
            description="是谁拥有了一米长的玫瑰花呀 #一束花的仪式感",
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "save_cover",
        lambda document_id, url: (
            seen.update({"cover_document": document_id, "cover_url": url}) or "cover.jpg"
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "fetch_platform_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("快手没有平台字幕")),
    )
    monkeypatch.setattr(worker_module, "download_audio_source", lambda *_args, **_kwargs: audio)
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: wav)
    stub_engine(monkeypatch, "sensevoice-small")
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 12000, "快手文案")], 12.0),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://v.kuaishou.com/example", mode="fast"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.uploader == "好想吃巧克力"
        assert document.description == "是谁拥有了一米长的玫瑰花呀 #一束花的仪式感"
        assert document.cover_file == "cover.jpg"

    # 封面用的是远程临时地址，落盘时要原样交给下载逻辑
    assert seen["cover_url"] == "https://p5.a.yximgs.com/upic/cover.jpg"


def test_worker_uses_fallback_api_when_resolve_fails(monkeypatch, tmp_path) -> None:
    """主链路解析失败但配了兜底 Key 时：用兜底元信息建文案，并直接下它的直链。"""

    init_database()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    media = fallback_api.FallbackMedia(
        title="兜底标题",
        url="https://cdn.example/v.mp4",
        uploader="兜底作者",
    )
    seen: dict[str, str] = {}

    monkeypatch.setattr(worker_module, "read_fallback_api_key", lambda _session: "sk-test")
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PlatformError("小红书处理失败：触发了风控拦截")),
    )
    monkeypatch.setattr(
        worker_module.fallback_api,
        "resolve",
        lambda url, api_key, platform=None: (seen.update(url=url, key=api_key, platform=platform), media)[1],
    )
    monkeypatch.setattr(worker_module.fallback_api, "download", lambda _media, _workspace: source)
    monkeypatch.setattr(
        worker_module,
        "download_audio_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("兜底命中后不该再走主链路下载")),
    )
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: source)
    stub_engine(monkeypatch, "sensevoice-small")
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 4000, "兜底文案")], 4.0),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(
                source_type="url",
                source="https://www.xiaohongshu.com/explore/6f0000000000000000000001",
                mode="auto",
            ),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.title == "兜底标题"
        assert document.uploader == "兜底作者"

    assert seen == {
        "url": "https://www.xiaohongshu.com/explore/6f0000000000000000000001",
        "key": "sk-test",
        "platform": "xiaohongshu",
    }


def test_worker_without_fallback_key_keeps_platform_error(monkeypatch) -> None:
    """没配兜底 Key 时行为与接入前一致：不发兜底请求，错误照旧。"""

    init_database()
    monkeypatch.setattr(worker_module, "read_fallback_api_key", lambda _session: None)
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PlatformError("小红书处理失败：触发了风控拦截")),
    )
    monkeypatch.setattr(
        worker_module.fallback_api,
        "resolve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("没配 Key 时不该调用兜底接口")),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(
                source_type="url",
                source="https://www.xiaohongshu.com/explore/6f0000000000000000000001",
                mode="auto",
            ),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error_code == "PLATFORM_ERROR"
        assert job.error_message == "小红书处理失败：触发了风控拦截"


def test_worker_falls_back_when_download_fails(monkeypatch, tmp_path) -> None:
    """解析成功但下载被拦（风控是概率性的）时，也要用兜底直链兜住。"""

    init_database()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    media = fallback_api.FallbackMedia(title="兜底标题", url="https://cdn.example/v.mp4")

    monkeypatch.setattr(worker_module, "read_fallback_api_key", lambda _session: "sk-test")
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: MediaInfo(title="平台标题", platform="xiaohongshu", duration_seconds=9),
    )
    monkeypatch.setattr(
        worker_module,
        "download_audio_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PlatformError("下载小红书视频失败，请稍后重试")),
    )
    monkeypatch.setattr(worker_module.fallback_api, "resolve", lambda *_args, **_kwargs: media)
    monkeypatch.setattr(worker_module.fallback_api, "download", lambda _media, _workspace: source)
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: source)
    stub_engine(monkeypatch, "sensevoice-small")
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 4000, "兜底文案")], 4.0),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(
                source_type="url",
                source="https://www.xiaohongshu.com/explore/6f0000000000000000000001",
                mode="auto",
            ),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        document = session.get(Document, job.document_id)
        assert document is not None
        # 元信息仍用主链路解析到的（更完整），只有音视频来源换成兜底直链
        assert document.title == "平台标题"


def test_worker_uses_fallback_for_douyin_without_cookie(monkeypatch, tmp_path) -> None:
    """抖音没配 Cookie 时不再直接失败：配了兜底 Key 就交给兜底接口（实测该端点可用）。"""

    init_database()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    media = fallback_api.FallbackMedia(title="兜底抖音", url="https://cdn.example/v.mp4")

    monkeypatch.setattr(worker_module, "read_fallback_api_key", lambda _session: "sk-test")
    monkeypatch.setattr(
        worker_module,
        "materialize_cookie_file",
        lambda _platform: (_ for _ in ()).throw(CredentialError("抖音需要访问凭据才能解析")),
    )
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("没凭据时不该调主链路解析")),
    )
    monkeypatch.setattr(worker_module.fallback_api, "resolve", lambda *_args, **_kwargs: media)
    monkeypatch.setattr(worker_module.fallback_api, "download", lambda _media, _workspace: source)
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: source)
    stub_engine(monkeypatch, "sensevoice-small")
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 3000, "兜底文案")], 3.0),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://v.douyin.com/XBwlKFr1ya0/", mode="auto"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.title == "兜底抖音"


def test_worker_without_cookie_and_key_keeps_cookie_error(monkeypatch) -> None:
    """既没配凭据也没配兜底 Key 时行为不变：错误码仍是 COOKIE_REQUIRED。"""

    init_database()
    monkeypatch.setattr(worker_module, "read_fallback_api_key", lambda _session: None)
    monkeypatch.setattr(
        worker_module,
        "materialize_cookie_file",
        lambda _platform: (_ for _ in ()).throw(CredentialError("抖音需要访问凭据才能解析")),
    )
    monkeypatch.setattr(
        worker_module.fallback_api,
        "resolve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("没配 Key 不该调用兜底接口")),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://v.douyin.com/XBwlKFr1ya0/", mode="auto"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error_code == "COOKIE_REQUIRED"
        assert job.error_message == "抖音需要访问凭据才能解析"


def test_worker_merges_fallback_failure_into_cookie_error(monkeypatch) -> None:
    """兜底也失败时，错误码保留 COOKIE_REQUIRED，消息里带上两边原因。"""

    init_database()
    monkeypatch.setattr(worker_module, "read_fallback_api_key", lambda _session: "sk-test")
    monkeypatch.setattr(
        worker_module,
        "materialize_cookie_file",
        lambda _platform: (_ for _ in ()).throw(CredentialError("抖音需要访问凭据才能解析")),
    )
    monkeypatch.setattr(
        worker_module.fallback_api,
        "resolve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(fallback_api.FallbackError("API Key 无效")),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://v.douyin.com/XBwlKFr1ya0/", mode="auto"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error_code == "COOKIE_REQUIRED"
        assert "兜底解析也没成功" in job.error_message
        assert "API Key 无效" in job.error_message


def test_worker_handles_wechat_via_fallback_only(monkeypatch, tmp_path) -> None:
    """视频号没有本机解析方案：配了 Key 就直接走兜底接口，完全不碰主链路与凭据。"""

    init_database()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    media = fallback_api.FallbackMedia(title="视频号内容", url="https://cdn.example/v.mp4")

    monkeypatch.setattr(worker_module, "read_fallback_api_key", lambda _session: "sk-test")
    monkeypatch.setattr(
        worker_module,
        "resolve_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("视频号不该走本机解析")),
    )
    monkeypatch.setattr(
        worker_module,
        "download_audio_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("视频号不该走本机下载")),
    )
    monkeypatch.setattr(
        worker_module,
        "materialize_cookie_file",
        lambda _platform: (_ for _ in ()).throw(AssertionError("视频号不参与凭据体系")),
    )
    monkeypatch.setattr(worker_module.fallback_api, "resolve", lambda *_args, **_kwargs: media)
    monkeypatch.setattr(worker_module.fallback_api, "download", lambda _media, _workspace: source)
    monkeypatch.setattr(worker_module, "convert_to_wav", lambda _source, _target: source)
    stub_engine(monkeypatch, "sensevoice-small")
    monkeypatch.setattr(
        worker_module.transcriber,
        "transcribe",
        lambda _path, callback: ([TranscriptChunk(0, 3000, "视频号文案")], 3.0),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://weixin.qq.com/sph/AUTQxifV6X", mode="auto"),
        )
        job_id = job.id
        assert job.platform == "wechat"

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.transcript_source == "asr"
        document = session.get(Document, job.document_id)
        assert document is not None
        assert document.title == "视频号内容"


def test_worker_wechat_without_key_reports_actionable_error(monkeypatch) -> None:
    """没配 Key 时视频号给出「去设置页填 Key」的提示，而不是把它当成「暂不支持」。"""

    init_database()
    monkeypatch.setattr(worker_module, "read_fallback_api_key", lambda _session: None)
    monkeypatch.setattr(
        worker_module.fallback_api,
        "resolve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("没配 Key 不该调用兜底接口")),
    )

    with SessionLocal() as session:
        job = create_job(
            session,
            JobCreate(source_type="url", source="https://weixin.qq.com/sph/AUTQxifV6X", mode="auto"),
        )
        job_id = job.id

    LocalWorker()._process(job_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error_code == "PLATFORM_ERROR"
        assert "视频号" in job.error_message
        assert "API Key" in job.error_message
