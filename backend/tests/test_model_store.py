from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.config import settings
from app.domain import ModelDownloadError, ModelEngine, ModelFile, ModelSpec, ModelTier
from app.infrastructure import model_catalog, model_store


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    directory: Path

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        target = (self.directory / self.path.lstrip("/")).resolve()
        root = self.directory.resolve()
        if not target.is_file() or root not in target.parents:
            self.send_error(404)
            return

        data = target.read_bytes()
        start = 0
        header = self.headers.get("Range") or ""
        if header.startswith("bytes="):
            raw = header.removeprefix("bytes=").split("-", 1)[0]
            start = int(raw) if raw.isdigit() else 0
        body = data[start:]

        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def media_server(tmp_path: Path):
    directory = tmp_path / "served"
    directory.mkdir()

    handler = type("Handler", (_QuietHandler,), {"directory": directory})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield directory, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def fresh_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    # 安装任务注册表是模块级单例，测试之间必须隔离，避免残留的「正在安装」状态。
    monkeypatch.setattr(model_store, "installer", model_store.ModelInstaller())


@pytest.fixture
def isolated_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "models"
    monkeypatch.setattr(settings, "models_dir", root)
    return root


def make_spec(base: str, payload: bytes) -> ModelSpec:
    return ModelSpec(
        id="test-model",
        name="测试模型",
        engine=ModelEngine.SENSEVOICE,
        tier=ModelTier.FAST,
        description="仅用于测试",
        files=(
            ModelFile(name="model.bin", urls=(f"{base}/model.bin",), size=len(payload)),
            ModelFile(name="tokens.txt", urls=(f"{base}/tokens.txt",)),
        ),
    )


def register(monkeypatch: pytest.MonkeyPatch, spec: ModelSpec) -> ModelSpec:
    monkeypatch.setitem(model_catalog.CATALOG, spec.id, spec)
    return spec


def wait_for_install(model_id: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = model_store.installer.snapshot(model_id)
        if snapshot is not None and not snapshot.active:
            return snapshot
        time.sleep(0.05)
    raise AssertionError("安装任务超时未结束")


def test_inspect_reports_missing_then_ready(media_server, isolated_models, monkeypatch) -> None:
    directory, base = media_server
    payload = b"x" * 4096
    (directory / "model.bin").write_bytes(payload)
    (directory / "tokens.txt").write_bytes(b"tokens")
    spec = register(monkeypatch, make_spec(base, payload))

    assert model_store.inspect(spec).state == "missing"

    model_store.installer.download(spec.id)
    snapshot = wait_for_install(spec.id)
    assert snapshot.status == "completed", snapshot.error

    status = model_store.inspect(spec)
    assert status.state == "ready"
    assert status.installed_bytes == len(payload) + len(b"tokens")
    assert Path(status.path or "").joinpath("model.bin").is_file()

    marker = isolated_models / spec.id / model_store.MARKER_FILENAME
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["source"] == "download"


def test_download_resumes_from_partial_file(media_server, isolated_models, monkeypatch) -> None:
    directory, base = media_server
    payload = bytes(range(256)) * 64
    (directory / "model.bin").write_bytes(payload)
    (directory / "tokens.txt").write_bytes(b"tok")
    spec = register(monkeypatch, make_spec(base, payload))

    target = isolated_models / spec.id
    target.mkdir(parents=True)
    part = model_store._part_path(target / "model.bin", spec.files[0].urls[0])
    part.write_bytes(payload[:1000])

    model_store.installer.download(spec.id)
    snapshot = wait_for_install(spec.id)
    assert snapshot.status == "completed", snapshot.error
    assert (target / "model.bin").read_bytes() == payload


def test_download_does_not_stitch_two_sources(media_server, isolated_models, monkeypatch) -> None:
    """别的源残留的临时文件不能被接着写，否则会拼出一个损坏的模型。"""

    directory, base = media_server
    payload = bytes(range(256)) * 64
    (directory / "model.bin").write_bytes(payload)
    (directory / "tokens.txt").write_bytes(b"tok")
    spec = register(
        monkeypatch,
        ModelSpec(
            id="test-model",
            name="测试模型",
            engine=ModelEngine.SENSEVOICE,
            tier=ModelTier.FAST,
            description="仅用于测试",
            files=(
                ModelFile(
                    name="model.bin",
                    urls=(f"{base}/missing.bin", f"{base}/model.bin"),
                    size=len(payload),
                ),
                ModelFile(name="tokens.txt", urls=(f"{base}/tokens.txt",)),
            ),
        ),
    )

    target = isolated_models / spec.id
    target.mkdir(parents=True)
    stale = model_store._part_path(target / "model.bin", f"{base}/missing.bin")
    stale.write_bytes(b"x" * 100)

    model_store.installer.download(spec.id)
    snapshot = wait_for_install(spec.id)
    assert snapshot.status == "completed", snapshot.error
    assert (target / "model.bin").read_bytes() == payload
    assert not list(target.glob(f"*{model_store.PART_SUFFIX}"))


def test_download_falls_back_to_next_mirror(media_server, isolated_models, monkeypatch) -> None:
    directory, base = media_server
    payload = b"payload" * 100
    (directory / "model.bin").write_bytes(payload)
    (directory / "tokens.txt").write_bytes(b"tok")
    spec = register(
        monkeypatch,
        ModelSpec(
            id="test-model",
            name="测试模型",
            engine=ModelEngine.SENSEVOICE,
            tier=ModelTier.FAST,
            description="仅用于测试",
            files=(
                ModelFile(
                    name="model.bin",
                    urls=(f"{base}/missing.bin", f"{base}/model.bin"),
                    size=len(payload),
                ),
                ModelFile(name="tokens.txt", urls=(f"{base}/tokens.txt",)),
            ),
        ),
    )

    model_store.installer.download(spec.id)
    snapshot = wait_for_install(spec.id)
    assert snapshot.status == "completed", snapshot.error
    assert model_store.inspect(spec).state == "ready"


def test_size_mismatch_marks_model_broken(media_server, isolated_models, monkeypatch) -> None:
    directory, base = media_server
    (directory / "model.bin").write_bytes(b"short")
    (directory / "tokens.txt").write_bytes(b"tok")
    spec = register(
        monkeypatch,
        ModelSpec(
            id="test-model",
            name="测试模型",
            engine=ModelEngine.SENSEVOICE,
            tier=ModelTier.FAST,
            description="仅用于测试",
            files=(
                ModelFile(name="model.bin", urls=(f"{base}/model.bin",), size=999),
                ModelFile(name="tokens.txt", urls=(f"{base}/tokens.txt",)),
            ),
        ),
    )

    model_store.installer.download(spec.id)
    snapshot = wait_for_install(spec.id)
    assert snapshot.status == "failed"
    assert "大小校验失败" in (snapshot.error or "")
    assert model_store.inspect(spec).state != "ready"


def test_download_accepts_declared_alternate_size(media_server, isolated_models, monkeypatch) -> None:
    """不同镜像分发的同款文件相差少量字节时，显式声明的可接受大小应通过校验。"""

    directory, base = media_server
    payload = b"model-bytes"
    (directory / "model.bin").write_bytes(payload)
    (directory / "tokens.txt").write_bytes(b"tok")
    spec = register(
        monkeypatch,
        ModelSpec(
            id="test-model",
            name="测试模型",
            engine=ModelEngine.SENSEVOICE,
            tier=ModelTier.FAST,
            description="仅用于测试",
            files=(
                ModelFile(
                    name="model.bin",
                    urls=(f"{base}/model.bin",),
                    size=len(payload) + 275,
                    size_alternates=(len(payload),),
                ),
                ModelFile(name="tokens.txt", urls=(f"{base}/tokens.txt",)),
            ),
        ),
    )

    model_store.installer.download(spec.id)
    snapshot = wait_for_install(spec.id)
    assert snapshot.status == "completed", snapshot.error
    assert model_store.inspect(spec).state == "ready"



def test_remove_deletes_model_directory(isolated_models, monkeypatch) -> None:
    spec = register(
        monkeypatch,
        ModelSpec(
            id="test-model",
            name="测试模型",
            engine=ModelEngine.SENSEVOICE,
            tier=ModelTier.FAST,
            description="仅用于测试",
            files=(ModelFile(name="model.bin"),),
        ),
    )
    directory = isolated_models / spec.id
    directory.mkdir(parents=True)
    (directory / "model.bin").write_bytes(b"data")

    assert model_store.inspect(spec).state == "ready"
    assert model_store.remove(spec) is True
    assert not directory.exists()
    assert model_store.remove(spec) is False


def test_remove_refuses_path_outside_models_root(tmp_path, monkeypatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(settings, "models_dir", tmp_path / "inner")
    spec = ModelSpec(
        id="..",
        name="越界模型",
        engine=ModelEngine.SENSEVOICE,
        tier=ModelTier.FAST,
        description="仅用于测试",
        files=(ModelFile(name="model.bin"),),
    )
    with pytest.raises(ModelDownloadError):
        model_store.remove(spec)
    assert outside.exists()


def test_existing_directory_is_ignored(tmp_path, monkeypatch) -> None:
    """本机其它位置的同款模型不再被探测：状态保持未安装，识别也不复用。"""

    existing = tmp_path / "somewhere" / "sherpa-onnx-sense-voice-small"
    existing.mkdir(parents=True)
    (existing / "model_q8.onnx").write_bytes(b"existing")
    (existing / "tokens.txt").write_bytes(b"tok")

    monkeypatch.setattr(settings, "models_dir", tmp_path / "models")
    spec = register(
        monkeypatch,
        ModelSpec(
            id="test-model",
            name="测试模型",
            engine=ModelEngine.SENSEVOICE,
            tier=ModelTier.FAST,
            description="仅用于测试",
            files=(
                ModelFile(name="model.int8.onnx", alternates=("model_q8.onnx",)),
                ModelFile(name="tokens.txt"),
            ),
        ),
    )

    status = model_store.inspect(spec)
    assert status.state == "missing"
    assert status.source is None
    assert model_store.resolve_directory(spec) is None


def test_marker_file_accepts_early_style_entries(tmp_path) -> None:
    """早期标记只记文件名：子目录里的文件要能找到，否则装好的模型会被误报「文件异常」。"""

    root = tmp_path / "model"
    (root / "test_wavs").mkdir(parents=True)
    target = root / "test_wavs" / "leijun-1.wav"
    target.write_bytes(b"RIFF")

    assert model_store._marker_file(root, "test_wavs/leijun-1.wav") == target  # noqa: SLF001
    assert model_store._marker_file(root, "leijun-1.wav") == target  # noqa: SLF001
    assert model_store._marker_file(root, "nope.wav") is None  # noqa: SLF001

    # 目录里另有同名文件时无法确定是哪一个，宁可判异常
    (root / "backup").mkdir()
    (root / "backup" / "leijun-1.wav").write_bytes(b"RIFF")
    assert model_store._marker_file(root, "leijun-1.wav") is None  # noqa: SLF001


def test_archive_marker_records_paths_relative_to_the_model_directory(tmp_path, monkeypatch) -> None:
    """新写的标记要带上子目录，校验时不必再退回到按文件名猜。"""

    monkeypatch.setattr(settings, "models_dir", tmp_path / "models")
    spec = model_catalog.require_spec("sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia")
    directory = model_store.spec_dir(spec)
    (directory / "test_wavs").mkdir(parents=True)
    wav = directory / "test_wavs" / "leijun-1.wav"
    wav.write_bytes(b"RIFF")
    model_store._write_marker(spec, directory, "download", {"leijun-1.wav": wav})  # noqa: SLF001

    marker = json.loads((directory / model_store.MARKER_FILENAME).read_text(encoding="utf-8"))
    assert marker["files"] == {"test_wavs/leijun-1.wav": 4}


def test_download_skips_a_source_that_is_too_slow(isolated_models, monkeypatch) -> None:
    """慢源不重试，直接换下一个。

    官方 GitHub 直连只有 47 KB/s，重试它也还是那么慢（455 MB 要等两个多小时），
    所以判慢后必须跳出重试循环换源，而不是在同一个源上反复等待。
    """

    monkeypatch.setattr(settings, "download_retries", 3)
    spec = register(
        monkeypatch,
        ModelSpec(
            id="test-model",
            name="测试模型",
            engine=ModelEngine.SENSEVOICE,
            tier=ModelTier.FAST,
            description="仅用于测试",
            files=(
                ModelFile(
                    name="model.bin",
                    urls=("https://slow.example/model.bin", "https://fast.example/model.bin"),
                ),
            ),
        ),
    )

    seen: list[str] = []

    def fake_fetch(self, task, slot, url, target, part):  # noqa: ARG001 - 只关心换源顺序
        seen.append(url)
        if "slow" in url:
            raise model_store._TooSlow("slow.example 只有 12 KB/s")  # noqa: SLF001
        part.write_bytes(b"fast")
        part.replace(target)
        return 4

    monkeypatch.setattr(model_store.ModelInstaller, "_fetch", fake_fetch)

    model_store.installer.download(spec.id)
    snapshot = wait_for_install(spec.id)

    assert snapshot.status == "completed", snapshot.error
    assert seen == ["https://slow.example/model.bin", "https://fast.example/model.bin"], (
        "慢源只该试一次，随后换下一个"
    )
    assert (isolated_models / spec.id / "model.bin").read_bytes() == b"fast"


def test_zipvoice_puts_the_domestic_mirror_first() -> None:
    """ZipVoice 的源顺序：国内 ModelScope 镜像 → GitHub 加速 → 官方兜底。

    官方直连实测 47 KB/s（455 MB 要等两个多小时），国内镜像 10 MB/s 量级；而加速代理
    随时可能失效，所以官方源必须留在最后兜底。
    """

    spec = model_catalog.require_spec("sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia")
    assert spec.files, "语音合成模型应当有文件"
    for slot in spec.files:
        assert slot.urls[0].startswith("https://www.modelscope.cn/"), f"{slot.name} 国内镜像没排在最前"
        assert slot.urls[-1].startswith("https://github.com/"), f"{slot.name} 少了官方源兜底"


def test_is_complete_needs_the_core_model_files(tmp_path, monkeypatch) -> None:
    """只有附属文件不算装好：encoder/decoder 缺一个就还不完整（等合成时才报错就太晚了）。"""

    monkeypatch.setattr(settings, "models_dir", tmp_path / "models")
    spec = model_catalog.require_spec("sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia")
    directory = model_store.spec_dir(spec)
    for name in spec.required_paths:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix:
            target.write_bytes(b"x")
        else:
            target.mkdir(exist_ok=True)

    assert model_store.is_complete(spec, directory) is False
    (directory / "encoder.onnx").write_bytes(b"x")
    assert model_store.is_complete(spec, directory) is False
    (directory / "decoder.onnx").write_bytes(b"x")
    assert model_store.is_complete(spec, directory) is True
