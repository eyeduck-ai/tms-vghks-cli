from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import CourseDetail, CourseItem, CourseSummary, ItemKind, ItemState
from .network_diagnostics import (
    NetworkObservation,
    _append_summary_observation,
    _capture_response,
    page_url,
    to_jsonable,
)
from .parsers import normalize_text, result_satisfies_condition
from .requests_reproduction import analyze_requests_reproduction_observations, feature_map
from .session import TmsSession
from .timeutils import parse_timer_to_seconds


DEFAULT_READING_ACCUMULATION_OBSERVATIONS_PATH = ".tms_session/reading_accumulation_observations.jsonl"
READING_ACCUMULATION_ACTION = "reading-accumulation"


@dataclass(slots=True)
class ReadingAccumulationSample:
    observed_at: str
    elapsed_seconds: int
    result: str | None
    result_seconds: int | None
    state: str
    passed_marker: str | None = None
    pass_condition: str | None = None
    page_url: str = ""
    timer_texts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReadingAccumulationDiagnosticResult:
    status: str
    output_path: str
    observation_count: int = 0
    course_title: str = ""
    course_url: str = ""
    course_id: str | None = None
    item_title: str = ""
    item_kind: str = ""
    item_order: int | None = None
    before: ReadingAccumulationSample | None = None
    after: ReadingAccumulationSample | None = None
    samples: list[ReadingAccumulationSample] = field(default_factory=list)
    progress_increased: bool = False
    already_passed: bool = False
    observed_url_patterns: list[str] = field(default_factory=list)
    requests_reproduction_status: str = ""
    requests_reproduction_evidence: list[str] = field(default_factory=list)
    requests_reproduction_missing_evidence: list[str] = field(default_factory=list)
    blocker: str = ""


@dataclass(slots=True)
class ReadingAccumulationTarget:
    course: CourseDetail
    item: CourseItem


def result_seconds(value: str | None) -> int | None:
    return parse_timer_to_seconds(value)


def result_increased(before: str | None, after: str | None) -> bool:
    after_seconds = result_seconds(after)
    if after_seconds is None:
        return False
    before_seconds = result_seconds(before) or 0
    return after_seconds > before_seconds


def item_is_accumulation_candidate(item: CourseItem, include_passed: bool = False) -> bool:
    if ItemKind(item.kind) not in {ItemKind.READING, ItemKind.VIDEO}:
        return False
    if include_passed:
        return True
    return item_already_passed(item)


def item_already_passed(item: CourseItem) -> bool:
    return bool(
        item.state == ItemState.PASSED
        or result_satisfies_condition(item.pass_condition, item.result, item.passed_marker)
    )


def select_reading_accumulation_target(
    session: TmsSession,
    course: str | None = None,
    item_title: str | None = None,
    item_order: int | None = None,
    course_limit: int | None = None,
) -> ReadingAccumulationTarget:
    if course:
        detail = session.get_course_detail(course)
        if item_title or item_order is not None:
            item = _select_detail_item(detail, item_title, item_order)
            _validate_reading_or_video(item)
            return ReadingAccumulationTarget(detail, item)
        item = _first_accumulation_candidate(detail.items)
        if item:
            return ReadingAccumulationTarget(detail, item)
        raise ValueError(f"no completed reading/video item was found in {detail.title}")

    completed = session.list_completed_courses()
    for summary in _limited_courses(completed, course_limit):
        detail = session.get_course_detail(summary.detail_url or summary.course_id or summary.title)
        item = _first_accumulation_candidate(detail.items)
        if item:
            return ReadingAccumulationTarget(detail, item)
    raise ValueError("no completed reading/video item was found")


def run_reading_accumulation_diagnostic(
    session: TmsSession,
    course: str | None = None,
    item_title: str | None = None,
    item_order: int | None = None,
    output_path: str | Path = DEFAULT_READING_ACCUMULATION_OBSERVATIONS_PATH,
    headless: bool = False,
    wait_seconds: int = 90,
    poll_seconds: int = 30,
    course_limit: int | None = None,
) -> ReadingAccumulationDiagnosticResult:
    output = Path(output_path)
    try:
        target = select_reading_accumulation_target(
            session,
            course=course,
            item_title=item_title,
            item_order=item_order,
            course_limit=course_limit,
        )
    except ValueError as exc:
        if course or item_title or item_order is not None:
            raise
        return ReadingAccumulationDiagnosticResult(
            status="no_completed_reading_item",
            output_path=str(output),
            blocker=str(exc),
        )
    return run_reading_accumulation_for_item(
        session=session,
        course=target.course,
        item=target.item,
        output_path=output,
        headless=headless,
        wait_seconds=wait_seconds,
        poll_seconds=poll_seconds,
    )


def run_reading_accumulation_for_item(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    output_path: str | Path = DEFAULT_READING_ACCUMULATION_OBSERVATIONS_PATH,
    headless: bool = False,
    wait_seconds: int = 90,
    poll_seconds: int = 30,
) -> ReadingAccumulationDiagnosticResult:
    from .handlers import TmsRunner, find_matching_item

    _validate_reading_or_video(item)
    wait_seconds = max(0, int(wait_seconds))
    poll_seconds = max(1, int(poll_seconds))
    opened_at = time.monotonic()
    before = _sample_from_item(course, item, page=None, opened_at=opened_at)
    observations: list[NetworkObservation] = []
    output = Path(output_path)
    result = ReadingAccumulationDiagnosticResult(
        status="not_started",
        output_path=str(output),
        course_title=course.title,
        course_url=course.url,
        course_id=course.course_id,
        item_title=item.title,
        item_kind=str(item.kind),
        item_order=item.order,
        before=before,
        after=before,
        samples=[],
        already_passed=item_already_passed(item),
    )
    _append_summary_observation(observations, f"{READING_ACCUMULATION_ACTION}:before", item, course.url, _sample_payload(before))

    session.start_browser(headless=headless)
    assert session.context is not None
    session.sync_cookies_to_browser()
    page = session.context.new_page()
    page.on("response", _capture_response(observations, READING_ACCUMULATION_ACTION, item))

    try:
        runner = TmsRunner(session)
        runner._open_item_page(page, course, item)
        recovered = runner._recover_known_transient_dialog(page, course, item)
        if recovered:
            course, item = recovered
            result.course_title = course.title
            result.course_url = course.url
            result.course_id = course.course_id
            result.item_title = item.title
            result.item_kind = str(item.kind)
            result.item_order = item.order

        blocking_dialog = _blocking_dialog_summary(page)
        if blocking_dialog:
            result.status = "blocked"
            result.blocker = blocking_dialog
            _append_summary_observation(
                observations,
                f"{READING_ACCUMULATION_ACTION}:blocked",
                item,
                page_url(page) or course.url,
                {"blocked": True, "message": blocking_dialog},
            )
            return _finalize_result(result, observations, output)

        end_at = time.monotonic() + wait_seconds
        while True:
            sample = _poll_course_detail_sample(session, course, item, page, opened_at)
            result.samples.append(sample)
            result.after = sample
            increased = result_increased(before.result, sample.result)
            result.progress_increased = result.progress_increased or increased
            _append_summary_observation(
                observations,
                f"{READING_ACCUMULATION_ACTION}:sample",
                item,
                course.url,
                {
                    **_sample_payload(sample),
                    "progress_increased": result.progress_increased,
                    "increased_from_before": increased,
                },
            )

            remaining = end_at - time.monotonic()
            if remaining <= 0:
                break
            page.wait_for_timeout(int(min(remaining, poll_seconds) * 1000))

        refreshed = session.get_course_detail(course.url)
        matched = find_matching_item(refreshed, item) or item
        final_sample = _sample_from_item(refreshed, matched, page=page, opened_at=opened_at)
        result.after = final_sample
        if not result.samples or result.samples[-1].result != final_sample.result:
            result.samples.append(final_sample)
        result.progress_increased = result.progress_increased or result_increased(before.result, final_sample.result)
        result.status = "accumulating" if result.progress_increased else "not_accumulating"
        _append_summary_observation(
            observations,
            f"{READING_ACCUMULATION_ACTION}:after",
            matched,
            refreshed.url,
            {
                **_sample_payload(final_sample),
                "progress_increased": result.progress_increased,
                "verified_by": "course_detail_row",
            },
        )
        return _finalize_result(result, observations, output)
    except Exception as exc:
        result.status = "blocked"
        result.blocker = str(exc)
        _append_summary_observation(
            observations,
            f"{READING_ACCUMULATION_ACTION}:error",
            item,
            page_url(page) or course.url,
            {"error": str(exc)},
        )
        return _finalize_result(result, observations, output)
    finally:
        try:
            page.close()
        except Exception:
            pass
        session.sync_cookies_to_requests()


def _finalize_result(
    result: ReadingAccumulationDiagnosticResult,
    observations: list[NetworkObservation],
    output: Path,
) -> ReadingAccumulationDiagnosticResult:
    output.parent.mkdir(parents=True, exist_ok=True)
    payloads = [to_jsonable(observation) for observation in observations]
    with output.open("a", encoding="utf-8", newline="\n") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    report = analyze_requests_reproduction_observations(payloads, source_path=str(output))
    reading = feature_map(report).get("reading_video_completion")
    result.observation_count = len(observations)
    result.observed_url_patterns = _url_patterns(payloads)
    if reading:
        result.requests_reproduction_status = reading.status
        result.requests_reproduction_evidence = reading.evidence
        result.requests_reproduction_missing_evidence = reading.missing_evidence
    return result


def _poll_course_detail_sample(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    page,
    opened_at: float,
) -> ReadingAccumulationSample:
    from .handlers import find_matching_item

    session.sync_cookies_to_requests()
    refreshed = session.get_course_detail(course.url)
    current = find_matching_item(refreshed, item) or item
    return _sample_from_item(refreshed, current, page=page, opened_at=opened_at)


def _sample_from_item(
    course: CourseDetail,
    item: CourseItem,
    page,
    opened_at: float,
) -> ReadingAccumulationSample:
    return ReadingAccumulationSample(
        observed_at=_utc_now(),
        elapsed_seconds=max(0, int(time.monotonic() - opened_at)),
        result=item.result,
        result_seconds=result_seconds(item.result),
        state=str(item.state),
        passed_marker=item.passed_marker,
        pass_condition=item.pass_condition,
        page_url=page_url(page) if page is not None else course.url,
        timer_texts=_dom_timer_texts(page) if page is not None else [],
    )


def _sample_payload(sample: ReadingAccumulationSample) -> dict[str, Any]:
    return {
        "elapsed_seconds": sample.elapsed_seconds,
        "result": sample.result,
        "result_seconds": sample.result_seconds,
        "state": sample.state,
        "passed_marker": sample.passed_marker,
        "pass_condition": sample.pass_condition,
        "page_url": sample.page_url,
        "timer_texts": sample.timer_texts,
    }


def _first_accumulation_candidate(items: list[CourseItem]) -> CourseItem | None:
    for item in sorted(items, key=lambda row: (row.order is None, row.order or 0)):
        if item_is_accumulation_candidate(item):
            return item
    return None


def _limited_courses(courses: list[CourseSummary], limit: int | None) -> list[CourseSummary]:
    if limit is None or limit <= 0:
        return list(courses)
    return list(courses[:limit])


def _select_detail_item(detail: CourseDetail, title: str | None, order: int | None) -> CourseItem:
    if order is not None:
        for item in detail.items:
            if item.order == order:
                return item
        raise ValueError(f"activity order {order} was not found in {detail.title}")
    if title:
        matches = [item for item in detail.items if title in item.title]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"activity title {title!r} was not found in {detail.title}")
        labels = ", ".join(f"{item.order}:{item.title}" for item in matches)
        raise ValueError(f"activity title {title!r} matched multiple items: {labels}")
    raise ValueError("pass --item-order or --item-title, or omit --course for automatic completed selection")


def _validate_reading_or_video(item: CourseItem) -> None:
    if ItemKind(item.kind) not in {ItemKind.READING, ItemKind.VIDEO}:
        raise ValueError(f"reading accumulation diagnostics require a reading or video item, got {item.kind}")


def _blocking_dialog_summary(page) -> str:
    try:
        texts = page.evaluate(
            """
            () => {
              const visible = (node) => {
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              return Array.from(document.querySelectorAll('[role="dialog"], .modal, .swal2-popup, .bootbox, .ui-dialog'))
                .filter(visible)
                .map((node) => (node.innerText || '').trim())
                .filter(Boolean)
                .slice(0, 10);
            }
            """
        )
    except Exception:
        return ""
    markers = ("重複登入", "重覆登入", "同一帳號", "另一個裝置", "duplicate", "another session")
    for text in texts or []:
        normalized = normalize_text(str(text))
        lowered = normalized.lower()
        if any(marker.lower() in lowered for marker in markers):
            return normalized[:200]
    return ""


def _dom_timer_texts(page) -> list[str]:
    try:
        values = page.evaluate(
            """
            () => {
              const seen = new Set();
              const rows = [];
              const timerRe = /(?:\\d{1,2}:)?\\d{1,2}:\\d{2}/;
              const contextRe = /剩餘|閱讀|觀看|學習|時間|進度|計時|timer|duration|progress/i;
              for (const node of Array.from(document.querySelectorAll('body *'))) {
                const text = (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ');
                if (!text || text.length > 160 || !timerRe.test(text) || !contextRe.test(text)) continue;
                if (seen.has(text)) continue;
                seen.add(text);
                rows.push(text);
                if (rows.length >= 20) break;
              }
              return rows;
            }
            """
        )
    except Exception:
        return []
    return [str(value) for value in values or []]


def _url_patterns(rows: list[dict[str, Any]]) -> list[str]:
    patterns = sorted({_url_pattern(str(row.get("url") or "")) for row in rows if row.get("url")})
    return [pattern for pattern in patterns if pattern]


def _url_pattern(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or url
    path = re.sub(r"/\d+(?=/|$)", "/{id}", path)
    path = re.sub(r"recordID=[^&]+", "recordID={value}", path, flags=re.IGNORECASE)
    return path or parsed.netloc


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "DEFAULT_READING_ACCUMULATION_OBSERVATIONS_PATH",
    "READING_ACCUMULATION_ACTION",
    "ReadingAccumulationDiagnosticResult",
    "ReadingAccumulationSample",
    "ReadingAccumulationTarget",
    "item_already_passed",
    "item_is_accumulation_candidate",
    "result_increased",
    "result_seconds",
    "run_reading_accumulation_diagnostic",
    "run_reading_accumulation_for_item",
    "select_reading_accumulation_target",
]
