from __future__ import annotations

from collections.abc import Mapping

from .parsers import TRANSIENT_ERROR_MARKERS, normalize_text

TRANSIENT_HTTP_STATUS_CODES = {500, 502, 503, 504}


def has_transient_marker(text: str | None) -> bool:
    body = normalize_text(text)
    return any(marker in body for marker in TRANSIENT_ERROR_MARKERS)


def is_transient_status_code(status_code: int | None) -> bool:
    return bool(status_code is not None and status_code in TRANSIENT_HTTP_STATUS_CODES)


def response_has_text_body(headers: Mapping[str, object] | None) -> bool:
    content_type = str((headers or {}).get("content-type") or (headers or {}).get("Content-Type") or "")
    return not content_type or any(token in content_type.lower() for token in ("text", "html", "json", "javascript"))


def transient_message(
    *,
    status_code: int | None = None,
    text: str | None = None,
    fallback: str = "temporary TMS error",
) -> str | None:
    if is_transient_status_code(status_code):
        return f"TMS returned HTTP {status_code}"
    if has_transient_marker(text):
        return "TMS reported a temporary save/server status error"
    return fallback if fallback else None
