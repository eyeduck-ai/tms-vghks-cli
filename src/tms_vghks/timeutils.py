from __future__ import annotations

import re


def parse_timer_to_seconds(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    if text in {"--", "-", ""}:
        return None

    chinese = re.search(
        r"(?:(?P<h>\d+)\s*(?:小時|時))?\s*"
        r"(?:(?P<m>\d+)\s*(?:分鐘|分))?\s*"
        r"(?:(?P<s>\d+)\s*秒)?",
        text,
    )
    if chinese and any(chinese.groupdict().values()):
        h = int(chinese.group("h") or 0)
        m = int(chinese.group("m") or 0)
        s = int(chinese.group("s") or 0)
        return h * 3600 + m * 60 + s

    match = re.search(r"\b(\d{1,2})(?::(\d{1,2}))(?::(\d{1,2}))?\b", text)
    if match:
        first = int(match.group(1))
        second = int(match.group(2))
        third = match.group(3)
        if third is None:
            return first * 60 + second
        return first * 3600 + second * 60 + int(third)

    if re.fullmatch(r"\d+", text):
        return int(text)
    return None


def parse_required_seconds(condition: str | None) -> int | None:
    if not condition:
        return None
    text = condition.strip()
    hour_match = re.search(r"(\d+)\s*(?:小時|時)", text)
    minute_match = re.search(r"(\d+)\s*(?:分鐘|分)", text)
    second_match = re.search(r"(\d+)\s*秒", text)

    if hour_match or minute_match or second_match:
        hours = int(hour_match.group(1)) if hour_match else 0
        minutes = int(minute_match.group(1)) if minute_match else 0
        seconds = int(second_match.group(1)) if second_match else 0
        return hours * 3600 + minutes * 60 + seconds

    timer = parse_timer_to_seconds(text)
    if timer is not None and any(token in text for token in ("閱讀", "觀看", "影片", "達")):
        return timer
    return None


def parse_passing_score(condition: str | None) -> int | None:
    if not condition:
        return None
    match = re.search(r"(\d+)\s*分\s*及格", condition)
    if match:
        return int(match.group(1))
    return None


def parse_numeric_score(value: str | None) -> int | None:
    if not value:
        return None
    if value.strip() in {"--", "-", ""}:
        return None
    match = re.search(r"\b(\d{1,3})\b", value)
    if match:
        return int(match.group(1))
    return None


def remaining_seconds(required_seconds: int | None, elapsed_text: str | None) -> int | None:
    if required_seconds is None:
        return None
    elapsed = parse_timer_to_seconds(elapsed_text) or 0
    return max(0, required_seconds - elapsed)

