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
