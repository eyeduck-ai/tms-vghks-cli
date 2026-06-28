from __future__ import annotations

from dataclasses import dataclass


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"


@dataclass(slots=True)
class GeminiQuizConfig:
    api_key: str = ""
    model: str = DEFAULT_GEMINI_MODEL
    timeout_seconds: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())


__all__ = ["DEFAULT_GEMINI_MODEL", "GeminiQuizConfig"]
