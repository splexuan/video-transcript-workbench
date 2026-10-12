"""只读支持 WorkBuddy 的加密登录字段。

改编自 dsh-connect-workbuddy/src/at-rest.ts（MIT，Copyright 2026 LaoDing）。
密钥只在进程内存里存活，不写盘、不外发。

字段是明文时（老版本客户端）本模块完全用不到 cryptography；只有遇到
`$wbEncrypted` 信封才需要，缺依赖时给出可执行的安装提示。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import subprocess
from functools import lru_cache
from pathlib import Path

from .common import AiEngineError


@lru_cache(maxsize=1)
def read_field_key() -> bytes:
    """从本机 WorkBuddy 客户端的 native binding 里取 at-rest 密钥摘要。"""

    candidates: list[Path] = []
    if os.environ.get("WORKBUDDY_APP_EXECUTABLE"):
        candidates.append(Path(os.environ["WORKBUDDY_APP_EXECUTABLE"]))
    for root in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
        if root:
            for name in ("WorkBuddy", "WorkBuddyAI", "WorkBuddy AI"):
                prefix = Path(root) / "Programs" if root == os.environ.get("LOCALAPPDATA") else Path(root)
                candidates.append(prefix / name / "WorkBuddy.exe")
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise AiEngineError(
            "WorkBuddy 登录态已加密，请安装桌面客户端，或设置 WORKBUDDY_APP_EXECUTABLE 指向客户端",
            "auth_encrypted",
        )
    # 只取密钥元数据：不下载、不写文件，输出也不会进日志。
    source = (
        "try{process.stdout.write(process._linkedBinding('electron_browser_workbuddy_storage')"
        ".loggerGet())}catch(_){process.exitCode=3}"
    )
    try:
        result = subprocess.run(
            [str(executable), "-e", source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise ValueError("native binding unavailable")
        secret = json.loads(result.stdout)["atRestSecretKey"]
        if not isinstance(secret, str) or not secret or len(secret) > 65536:
            raise ValueError("invalid native metadata")
        return hashlib.sha256(secret.encode("utf-8")).digest()
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise AiEngineError(
            "无法读取 WorkBuddy 加密登录态，请检查客户端版本或安装路径；重新登录不会改变加密格式",
            "auth_encrypted",
        ) from None


def decode_field(value):
    """解密 `$wbEncrypted` 信封；非信封字段原样返回。"""

    if not isinstance(value, dict):
        return value
    if set(value) != {"$wbEncrypted", "envelope"} or value.get("$wbEncrypted") != 1:
        raise AiEngineError("WorkBuddy 登录字段的加密格式不兼容", "auth_invalid")
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise AiEngineError(
            "缺少读取本地登录态的依赖，请在 backend 目录执行 pip install -e \".[ai]\" 后重启",
            "auth_dependency_missing",
        ) from None
    try:
        raw = base64.b64decode(value["envelope"], validate=True)
        if len(raw) > 65536:
            raise ValueError("oversized envelope")
        envelope = json.loads(raw)
        key = read_field_key()
        key_id = hashlib.sha256(key).hexdigest()[:16]
        suite = envelope["suite"]
        if envelope["keyId"] != key_id or type(suite) is not int or suite != 1:
            raise ValueError("key or suite mismatch")

        def length_prefixed(text: str) -> bytes:
            data = text.encode("utf-8")
            return struct.pack(">I", len(data)) + data

        aad = (
            b"WB-AAD\0\x01"
            + length_prefixed("WBEV1")
            + length_prefixed("sym-v1")
            + struct.pack(">I", suite)
            + length_prefixed(key_id)
            + b"\x02\x00\x00"
        )
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        tag = base64.b64decode(envelope["authTag"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        if len(nonce) != 12 or len(tag) != 16 or len(ciphertext) > 32768:
            raise ValueError("invalid envelope lengths")
        return AESGCM(key).decrypt(nonce, ciphertext + tag, aad).decode("utf-8")
    except AiEngineError:
        raise
    except (ValueError, KeyError, TypeError, InvalidTag):
        raise AiEngineError(
            "WorkBuddy 加密登录字段校验失败，请检查是否使用了对应版本的客户端", "auth_invalid"
        ) from None
