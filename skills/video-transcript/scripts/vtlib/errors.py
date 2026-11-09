"""带错误码的异常：CLI 据此输出结构化错误，Agent 据此决定下一步。"""

from __future__ import annotations


class SkillError(RuntimeError):
    code = "PROCESSING_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class PlatformError(SkillError):
    code = "PLATFORM_ERROR"


class FallbackError(SkillError):
    code = "FALLBACK_ERROR"


class FallbackKeyMissingError(FallbackError):
    code = "FALLBACK_KEY_MISSING"


class UnsupportedSourceError(PlatformError):
    code = "UNSUPPORTED_SOURCE"


class CookieRequiredError(PlatformError):
    code = "COOKIE_REQUIRED"


class NoSpeechError(SkillError):
    code = "NO_SPEECH"
