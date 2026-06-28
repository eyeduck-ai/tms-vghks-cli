from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_QUERY_KEYS = {
    "ajaxauth",
    "auth",
    "authorization",
    "key",
    "sig",
    "signature",
    "token",
    "user_id",
    "userid",
}
SENSITIVE_VALUE_KEYS = {
    "account",
    "access_token",
    "ajax_auth",
    "ajaxauth",
    "anticsrf",
    "api_token",
    "authorization",
    "captcha",
    "captcha_path",
    "captcha_url",
    "cookie",
    "cookies",
    "headers",
    "hidden_fields",
    "ocr_raw_response",
    "password",
    "paddleocr_api_token",
    "response_json",
    "response_text",
    "token",
}
REDACTED = "REDACTED"


def redact_sensitive_url(value: str) -> str:
    if "?" not in value:
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.query:
        return value
    changed = False
    query: list[tuple[str, str]] = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_QUERY_KEYS:
            query.append((key, REDACTED))
            changed = True
        else:
            redacted_item = redact_sensitive_url(item) if "?" in item else item
            query.append((key, redacted_item))
            changed = changed or redacted_item != item
    if not changed:
        return value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact_sensitive_value(value):
    if isinstance(value, str):
        return redact_sensitive_url(value)
    if isinstance(value, list):
        return [redact_sensitive_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_value(item) for item in value)
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_VALUE_KEYS:
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_sensitive_value(item)
        return redacted
    return value
