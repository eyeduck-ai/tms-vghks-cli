from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .models import CourseDetail, CourseItem, ItemKind, ItemState, SiteState
from .parsers import absolute_url, classify_response, normalize_text, result_satisfies_condition
from .privacy import redact_sensitive_url
from .requests_login import ajax_login_headers
from .session import LoginRequired, TmsError, TmsSession, TransientTmsError
from .timeutils import parse_timer_to_seconds


DEFAULT_REQUESTS_WATCH_INTERVAL_SECONDS = 60


@dataclass(slots=True)
class WatchTimeEndpoint:
    record_url: str
    post_url: str
    record_time: int
    timing: str = ""
    log_id: str = ""
    has_ajax_auth: bool = False
    exit_url: str = ""


@dataclass(slots=True)
class WatchTimeParseResult:
    endpoint: WatchTimeEndpoint | None
    issues: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RequestsWatchTimeSample:
    observed_at: str
    result: str | None
    result_seconds: int | None
    state: str
    passed_marker: str | None = None
    pass_condition: str | None = None


@dataclass(slots=True)
class RequestsWatchTimeResult:
    success: bool
    status: str
    course_title: str = ""
    course_url: str = ""
    course_id: str | None = None
    item_title: str = ""
    item_kind: str = ""
    item_order: int | None = None
    before: RequestsWatchTimeSample | None = None
    after: RequestsWatchTimeSample | None = None
    samples: list[RequestsWatchTimeSample] = field(default_factory=list)
    before_result: str | None = None
    before_result_seconds: int | None = None
    after_result: str | None = None
    after_result_seconds: int | None = None
    progress_increased: bool = False
    already_passed: bool = False
    waited_seconds: int = 0
    media_url: str = ""
    watch_time_url: str = ""
    endpoint_summary: dict[str, Any] = field(default_factory=dict)
    response_status_code: int | None = None
    response_json_summary: dict[str, Any] = field(default_factory=dict)
    observed_url_patterns: list[str] = field(default_factory=list)
    requests_reproduction_status: str = "requests_blocked"
    issues: list[str] = field(default_factory=list)


def parse_watch_time_endpoint(
    media_html: str,
    media_url: str,
    base_url: str,
) -> WatchTimeParseResult:
    config, issues = _extract_readlog_config(media_html)
    if config is None:
        return WatchTimeParseResult(None, issues or ["readlog_config_missing"])

    record_url_value = config.get("recordUrl")
    if not isinstance(record_url_value, str) or not record_url_value.strip():
        return WatchTimeParseResult(None, [*issues, "watch_time_record_url_missing"])
    record_url = absolute_url(record_url_value, base_url) or record_url_value
    record_time = _int_or_default(config.get("recordTime"), DEFAULT_REQUESTS_WATCH_INTERVAL_SECONDS)
    post_url = _with_query_param(record_url, "t", str(record_time))
    query = _query_map(record_url)
    log_id = query.get("logid", "")
    ajax_auth = query.get("ajaxauth", "")
    if not log_id:
        issues.append("watch_time_log_id_missing")
    if not ajax_auth:
        issues.append("watch_time_ajax_auth_missing")

    endpoint = WatchTimeEndpoint(
        record_url=record_url,
        post_url=post_url,
        record_time=record_time,
        timing=str(config.get("timing") or query.get("timing") or ""),
        log_id=log_id,
        has_ajax_auth=bool(ajax_auth),
        exit_url=absolute_url(str(config.get("exitUrl") or ""), base_url) or "",
    )
    return WatchTimeParseResult(endpoint, issues)


def find_check_pass_previous_url(course_html: str, item: CourseItem, base_url: str) -> str | None:
    soup = BeautifulSoup(course_html or "", "html.parser")
    node = _find_activity_node(soup, item)
    candidates: list[str] = []
    if node is not None:
        for anchor in node.select(".fs-singleLineText a, .node-title a, a"):
            href = anchor.get("href")
            if href and "/media/" in href:
                return absolute_url(href, base_url)
            anchor_id = anchor.get("id")
            if anchor_id:
                candidates.append(f"#{anchor_id}")
            candidates.extend(f".{class_name}" for class_name in anchor.get("class") or [])

    if not candidates:
        item_title = normalize_text(item.title)
        for anchor in soup.select(".fs-singleLineText a, .node-title a, a"):
            if item_title and item_title not in normalize_text(anchor.get_text(" ", strip=True)):
                continue
            href = anchor.get("href")
            if href and "/media/" in href:
                return absolute_url(href, base_url)
            anchor_id = anchor.get("id")
            if anchor_id:
                candidates.append(f"#{anchor_id}")
            candidates.extend(f".{class_name}" for class_name in anchor.get("class") or [])

    for selector in _dedupe(candidates):
        url = _find_fs_post_url_for_selector(course_html, selector)
        if url and "checkPassPrevious" in url:
            return absolute_url(url, base_url)
    return None


def run_requests_watch_time(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    wait_seconds: int = DEFAULT_REQUESTS_WATCH_INTERVAL_SECONDS,
    wait_func: Callable[[float], None] = time.sleep,
    force_watch_time: bool = False,
) -> RequestsWatchTimeResult:
    _validate_reading_or_video(item)
    wait_seconds = max(0, int(wait_seconds))
    before_course, before_item = _refresh_matching_item(session, course, item)
    before = _sample_from_item(before_item)
    result = RequestsWatchTimeResult(
        success=False,
        status="not_started",
        course_title=before_course.title,
        course_url=before_course.url,
        course_id=before_course.course_id,
        item_title=before_item.title,
        item_kind=str(before_item.kind),
        item_order=before_item.order,
        before=before,
        after=before,
        samples=[before],
        before_result=before.result,
        before_result_seconds=before.result_seconds,
        after_result=before.result,
        after_result_seconds=before.result_seconds,
        already_passed=_item_already_passed(before_item),
        waited_seconds=wait_seconds,
    )
    if result.already_passed and not force_watch_time:
        result.success = True
        result.status = "already_passed"
        result.requests_reproduction_status = "requests_not_needed"
        return result
    if result.already_passed and force_watch_time:
        result.issues.append("forced_watch_time_on_already_passed_item")

    media_url = ""
    try:
        media_url, media_issues = resolve_media_url_requests(session, before_course, before_item)
    except (LoginRequired, TransientTmsError):
        raise
    except Exception as exc:
        media_issues = [f"media_url_error:{exc}"]
    if media_issues:
        result.issues.extend(media_issues)
    if not media_url:
        result.status = "endpoint_unverified"
        result.requests_reproduction_status = "requests_blocked"
        result.observed_url_patterns = _url_patterns([before_course.url])
        return result

    result.media_url = redact_sensitive_url(media_url)
    try:
        media_html = session.fetch_activity_html_requests(media_url, referer=before_course.url)
    except LoginRequired:
        raise
    except TransientTmsError:
        raise
    except Exception as exc:
        result.status = "endpoint_unverified"
        result.issues.append(f"media_html_unavailable:{exc}")
        result.observed_url_patterns = _url_patterns([before_course.url, media_url])
        return result

    parsed = parse_watch_time_endpoint(media_html, media_url, session.base_url)
    result.issues.extend(parsed.issues)
    if parsed.endpoint is None:
        result.status = "watch_time_missing_token"
        result.observed_url_patterns = _url_patterns([before_course.url, media_url])
        return result

    endpoint = parsed.endpoint
    result.watch_time_url = redact_sensitive_url(endpoint.post_url)
    result.endpoint_summary = {
        "record_time": endpoint.record_time,
        "timing": endpoint.timing,
        "has_log_id": bool(endpoint.log_id),
        "has_ajax_auth": endpoint.has_ajax_auth,
        "record_url": redact_sensitive_url(endpoint.record_url),
        "post_url": redact_sensitive_url(endpoint.post_url),
    }
    result.observed_url_patterns = _url_patterns([before_course.url, media_url, endpoint.post_url])
    if not endpoint.log_id or not endpoint.has_ajax_auth:
        result.status = "watch_time_missing_token"
        return result

    post_intervals = _watch_time_post_intervals(wait_seconds, endpoint.record_time)
    result.endpoint_summary["post_count"] = len(post_intervals)
    result.endpoint_summary["post_intervals"] = post_intervals
    for post_index, interval_seconds in enumerate(post_intervals, start=1):
        wait_func(interval_seconds)
        post_url = _watch_time_post_url(endpoint, interval_seconds)
        try:
            response = session._request_with_transient_retries(
                "POST",
                post_url,
                data={},
                allow_redirects=False,
                headers=ajax_login_headers(media_url),
            )
        except LoginRequired:
            raise
        except TransientTmsError:
            raise
        except Exception as exc:
            result.status = "watch_time_post_failed"
            result.issues.append(f"watch_time_post_error:{exc}")
            result.issues.append(f"watch_time_post_segment:{post_index}/{len(post_intervals)}")
            return result

        result.response_status_code = response.status_code
        result.response_json_summary = _response_json_summary(response)
        result.watch_time_url = redact_sensitive_url(post_url)
        status = classify_response(
            response.status_code,
            response.url,
            response.headers,
            response.text if _is_text_response(response) else "",
        )
        if status.state == SiteState.LOGIN_REQUIRED:
            raise LoginRequired("TMS login is required")
        if status.state == SiteState.TRANSIENT_ERROR:
            raise TransientTmsError(status.message)
        if response.status_code >= 400:
            result.status = "watch_time_post_failed"
            result.issues.append(f"watch_time_post_http_{response.status_code}")
            result.issues.append(f"watch_time_post_segment:{post_index}/{len(post_intervals)}")
            return result

    after_course, after_item = _refresh_matching_item(session, before_course, before_item)
    after = _sample_from_item(after_item)
    result.after = after
    result.samples.append(after)
    result.after_result = after.result
    result.after_result_seconds = after.result_seconds
    result.course_title = after_course.title
    result.course_url = after_course.url
    result.course_id = after_course.course_id
    result.item_title = after_item.title
    result.item_kind = str(after_item.kind)
    result.item_order = after_item.order
    result.progress_increased = _result_increased(before.result, after.result)
    if result.progress_increased:
        result.success = True
        result.status = "requests_watch_time_verified"
        result.requests_reproduction_status = "requests_reproducible"
        return result

    result.status = "watch_time_not_verified"
    result.requests_reproduction_status = "requests_partial"
    return result


def _watch_time_post_intervals(wait_seconds: int, record_time: int) -> list[int]:
    record_time = max(1, int(record_time or DEFAULT_REQUESTS_WATCH_INTERVAL_SECONDS))
    wait_seconds = max(0, int(wait_seconds))
    if wait_seconds <= 0:
        return [0]
    full_intervals, remainder = divmod(wait_seconds, record_time)
    intervals = [record_time] * full_intervals
    if remainder:
        intervals.append(remainder)
    return intervals or [wait_seconds]


def _watch_time_post_url(endpoint: WatchTimeEndpoint, interval_seconds: int) -> str:
    posted_seconds = int(interval_seconds) if interval_seconds > 0 else endpoint.record_time
    return _with_query_param(endpoint.record_url, "t", str(posted_seconds))


def resolve_media_url_requests(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
) -> tuple[str, list[str]]:
    if item.detail_url and "/media/" in item.detail_url:
        return item.detail_url, []

    issues: list[str] = []
    course_html = session.fetch_activity_html_requests(course.url)
    direct = _find_direct_media_url(course_html, item, session.base_url)
    if direct:
        return direct, []

    check_url = find_check_pass_previous_url(course_html, item, session.base_url)
    if not check_url:
        return "", ["media_url_missing", "check_pass_previous_url_missing"]

    response = session._request_with_transient_retries(
        "POST",
        check_url,
        data={},
        allow_redirects=False,
        headers=ajax_login_headers(course.url),
    )
    status = classify_response(
        response.status_code,
        response.url,
        response.headers,
        response.text if _is_text_response(response) else "",
    )
    if status.state == SiteState.LOGIN_REQUIRED:
        raise LoginRequired("TMS login is required")
    if status.state == SiteState.TRANSIENT_ERROR:
        raise TransientTmsError(status.message)
    if response.status_code >= 400:
        return "", [f"check_pass_previous_http_{response.status_code}"]
    payload = _safe_json(response)
    media_url = _extract_media_url_from_json(payload, session.base_url)
    if media_url:
        return media_url, []
    if isinstance(payload, dict) and payload.get("status") is False:
        message = normalize_text(str(payload.get("msg") or payload.get("message") or ""))
        if message:
            issues.append(f"check_pass_previous_blocked:{message[:120]}")
    issues.append("check_pass_previous_media_url_missing")
    return "", issues


def _extract_readlog_config(media_html: str) -> tuple[dict[str, Any] | None, list[str]]:
    match = re.search(r"new\s+ReadLog\s*\(\s*(\{[\s\S]*?\})\s*\)\s*;", media_html or "")
    if not match:
        return None, ["readlog_config_missing"]
    raw = match.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = _extract_readlog_config_by_regex(raw)
        if not data:
            return None, ["readlog_config_unparseable"]
        return data, ["readlog_config_regex_fallback"]
    if not isinstance(data, dict):
        return None, ["readlog_config_unparseable"]
    return data, []


def _extract_readlog_config_by_regex(raw: str) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for key in ("recordUrl", "timing", "exitUrl"):
        match = re.search(rf"""["']{re.escape(key)}["']\s*:\s*["'](?P<value>.*?)["']""", raw, flags=re.DOTALL)
        if match:
            config[key] = match.group("value")
    record_time = re.search(r"""["']recordTime["']\s*:\s*(?P<value>\d+)""", raw)
    if record_time:
        config["recordTime"] = int(record_time.group("value"))
    return config


def _find_activity_node(soup: BeautifulSoup, item: CourseItem) -> Tag | None:
    ids = [
        str(value)
        for value in (
            item.metadata.get("activity_tree_id"),
            item.metadata.get("activity_id"),
        )
        if value
    ]
    for identifier in ids:
        node = soup.find(id=identifier)
        if isinstance(node, Tag):
            return node
        node = soup.find(attrs={"data-id": identifier})
        if isinstance(node, Tag):
            return node
    item_title = normalize_text(item.title)
    for node in soup.select("#activityTree li.xtree-node, li.xtree-node"):
        if item_title and item_title in normalize_text(node.get_text(" ", strip=True)):
            return node
    return None


def _find_direct_media_url(course_html: str, item: CourseItem, base_url: str) -> str | None:
    soup = BeautifulSoup(course_html or "", "html.parser")
    node = _find_activity_node(soup, item)
    anchors = node.find_all("a", href=True) if node is not None else soup.find_all("a", href=True)
    item_title = normalize_text(item.title)
    for anchor in anchors:
        href = anchor.get("href")
        if not href or "/media/" not in href:
            continue
        if node is not None or not item_title or item_title in normalize_text(anchor.get_text(" ", strip=True)):
            return absolute_url(href, base_url)
    return None


def _find_fs_post_url_for_selector(html: str, selector: str) -> str | None:
    needles = _selector_needles(selector)
    for needle in needles:
        start = html.find(needle)
        while start >= 0:
            window = html[start : start + 6000]
            match = re.search(r"""fs\.post\s*\(\s*(['"])(?P<url>.*?)\1""", window, flags=re.DOTALL)
            if match:
                return match.group("url")
            start = html.find(needle, start + len(needle))
    return None


def _selector_needles(selector: str) -> list[str]:
    escaped = selector.replace("\\", "\\\\").replace('"', '\\"')
    single = selector.replace("\\", "\\\\").replace("'", "\\'")
    return [f'$("{escaped}")', f"$('{single}')"]


def _extract_media_url_from_json(payload: Any, base_url: str) -> str:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.extend([data.get("url"), data.get("href"), data.get("location")])
        elif isinstance(data, str):
            candidates.append(data)
        ret = payload.get("ret")
        if isinstance(ret, dict):
            candidates.extend([ret.get("url"), ret.get("href"), ret.get("location")])
        candidates.extend([payload.get("url"), payload.get("href"), payload.get("location")])
    for candidate in candidates:
        if isinstance(candidate, str) and "/media/" in candidate:
            return absolute_url(candidate, base_url) or candidate
    return ""


def _refresh_matching_item(session: TmsSession, course: CourseDetail, item: CourseItem) -> tuple[CourseDetail, CourseItem]:
    try:
        refreshed = session.get_course_detail(course.url)
    except Exception:
        return course, item
    matched = _find_matching_item(refreshed, item) or item
    return refreshed, matched


def _find_matching_item(course: CourseDetail, item: CourseItem) -> CourseItem | None:
    for candidate in course.items:
        if item.order is not None and candidate.order == item.order:
            return candidate
    item_title = normalize_text(item.title)
    for candidate in course.items:
        if normalize_text(candidate.title) == item_title:
            return candidate
    for candidate in course.items:
        if item_title and item_title in normalize_text(candidate.title):
            return candidate
    return None


def _sample_from_item(item: CourseItem) -> RequestsWatchTimeSample:
    return RequestsWatchTimeSample(
        observed_at=datetime.now(timezone.utc).isoformat(),
        result=item.result,
        result_seconds=parse_timer_to_seconds(item.result),
        state=str(item.state),
        passed_marker=item.passed_marker,
        pass_condition=item.pass_condition,
    )


def _item_already_passed(item: CourseItem) -> bool:
    return item.state == ItemState.PASSED or result_satisfies_condition(
        item.pass_condition,
        item.result,
        item.passed_marker,
    )


def _validate_reading_or_video(item: CourseItem) -> None:
    if ItemKind(item.kind) not in {ItemKind.READING, ItemKind.VIDEO}:
        raise ValueError(f"requests watchTime diagnostics require a reading or video item, got {item.kind}")


def _result_increased(before: str | None, after: str | None) -> bool:
    after_seconds = parse_timer_to_seconds(after)
    if after_seconds is None:
        return False
    before_seconds = parse_timer_to_seconds(before) or 0
    return after_seconds > before_seconds


def _safe_json(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _response_json_summary(response) -> dict[str, Any]:
    payload = _safe_json(response)
    if isinstance(payload, dict):
        summary: dict[str, Any] = {
            "json": True,
            "keys": sorted(str(key) for key in payload)[:50],
        }
        for key in ("status", "success"):
            if isinstance(payload.get(key), (str, int, float, bool, type(None))):
                summary[key] = payload.get(key)
        ret = payload.get("ret")
        if isinstance(ret, dict):
            summary["ret_keys"] = sorted(str(key) for key in ret)[:50]
            for key in ("status", "success"):
                if isinstance(ret.get(key), (str, int, float, bool, type(None))):
                    summary[f"ret_{key}"] = ret.get(key)
        return summary
    if isinstance(payload, list):
        return {"json": True, "type": "list", "length": len(payload)}
    return {"json": False}


def _is_text_response(response) -> bool:
    content_type = response.headers.get("content-type") or response.headers.get("Content-Type") or ""
    return any(token in content_type.lower() for token in ("text", "html", "json", "javascript", "xml"))


def _query_map(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    return {key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}


def _with_query_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    rows = [(row_key, row_value) for row_key, row_value in parse_qsl(parts.query, keep_blank_values=True) if row_key != key]
    rows.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(rows), parts.fragment))


def _int_or_default(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _url_patterns(urls: list[str]) -> list[str]:
    patterns = []
    for url in urls:
        parsed = urlparse(url)
        path = parsed.path or url
        path = re.sub(r"/\d+(?=/|$)", "/{id}", path)
        if path and path not in patterns:
            patterns.append(path)
    return patterns
