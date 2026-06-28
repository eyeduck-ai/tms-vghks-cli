from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import inspect
import time
from dataclasses import replace
from typing import Any, Callable

from .models import CourseDetail, CourseItem, ItemState, LoginMethod, OperationBackend, RunResult, SiteState
from .parsers import normalize_text, result_satisfies_condition
from .session import TmsSession


def run_scheduler_for_runner(runner: Any) -> RunResult:
    runner.session.ensure_authenticated(runner.options.auth_options)
    runner.session.sync_cookies_to_requests()
    pending = runner._list_pending_courses()
    requested_limit = max(1, runner.options.concurrency)
    if not pending:
        return RunResult(
            True,
            "no_pending_courses",
            "no pending TMS courses were found",
            data={
                "pending_count_before": 0,
                "pending_count_after": 0,
                "results": [],
                "item_results": [],
                "course_runs": [],
                "events": [],
                "summary": _scheduler_summary(
                    backend=str(runner.options.backend),
                    requested_concurrency=requested_limit,
                    effective_concurrency=0,
                    pending_count_before=0,
                    pending_count_after=0,
                    course_runs=[],
                    item_results=[],
                ),
                "errors": [],
            },
        )

    details: list[tuple[int, CourseDetail]] = []
    course_runs: list[dict[str, Any]] = []
    for course_index, course in enumerate(pending):
        started = time.monotonic()
        try:
            detail = runner._get_course_detail(course.detail_url or course.course_id or course.title)
            details.append((course_index, detail))
        except Exception as exc:
            course_runs.append(
                _failed_course_run_record(
                    course_index=course_index,
                    worker_index=None,
                    title=course.title,
                    url=course.detail_url or "",
                    course_id=course.course_id,
                    status="detail_unavailable",
                    message=str(exc),
                    started_at_monotonic=started,
                )
            )

    max_limit = max(requested_limit, runner.options.max_concurrency)
    current_limit = min(max_limit, requested_limit)
    max_effective_limit = 0
    if details:
        detail_index = 0
        while detail_index < len(details):
            batch = details[detail_index : detail_index + current_limit]
            effective_limit = max(1, min(current_limit, len(batch)))
            max_effective_limit = max(max_effective_limit, effective_limit)
            batch_records = _run_course_batch(runner, batch, effective_limit)
            course_runs.extend(batch_records)
            batch_success = bool(batch_records) and all(record.get("status") == "succeeded" for record in batch_records)
            current_limit = next_adaptive_limit(current_limit, batch_success, max_limit, runner.options.adaptive)
            detail_index += len(batch)

    try:
        final_pending = runner._list_pending_courses()
    except Exception:
        final_pending = pending

    course_runs.sort(key=lambda record: record.get("course_index", 0))
    item_results = [row for record in course_runs for row in record.get("item_results", [])]
    events = [event for record in course_runs for event in record.get("events", [])]
    errors = [error for record in course_runs for error in record.get("errors", [])]
    summary = _scheduler_summary(
        backend=str(runner.options.backend),
        requested_concurrency=requested_limit,
        effective_concurrency=max_effective_limit if details else 0,
        pending_count_before=len(pending),
        pending_count_after=len(final_pending),
        course_runs=course_runs,
        item_results=item_results,
    )
    success = summary["failed_courses"] == 0 and summary["failed_items"] == 0
    return RunResult(
        success,
        SiteState.LOGGED_IN,
        "scheduler run completed",
        data={
            "pending_count_before": len(pending),
            "pending_count_after": len(final_pending),
            "results": item_results,
            "item_results": item_results,
            "course_runs": course_runs,
            "events": events,
            "summary": summary,
            "errors": errors,
        },
    )


def _run_course_batch(
    runner: Any,
    batch: list[tuple[int, CourseDetail]],
    effective_limit: int,
) -> list[dict[str, Any]]:
    if effective_limit == 1:
        return [
            runner._run_course_with_worker(detail, course_index, worker_index=course_index)
            for course_index, detail in batch
        ]

    batch_records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=effective_limit) as executor:
        future_to_detail = {
            executor.submit(runner._run_course_with_worker, detail, course_index, course_index): (course_index, detail)
            for course_index, detail in batch
        }
        for future in as_completed(future_to_detail):
            course_index, detail = future_to_detail[future]
            try:
                batch_records.append(future.result())
            except Exception as exc:
                batch_records.append(
                    _failed_course_run_record(
                        course_index=course_index,
                        worker_index=course_index,
                        title=detail.title,
                        url=detail.url,
                        course_id=detail.course_id,
                        status="worker_failed",
                        message=str(exc),
                        started_at_monotonic=time.monotonic(),
                    )
                )
    return batch_records


def run_course_with_worker(
    runner: Any,
    course: CourseDetail,
    course_index: int,
    worker_index: int,
    runner_factory: Callable[[TmsSession, Any], Any],
) -> dict[str, Any]:
    worker_session = new_worker_session(runner.session)
    worker_runner = runner if worker_session is runner.session else runner_factory(worker_session, _copy_run_options(runner.options))
    try:
        _results, record = worker_runner._run_course_until_complete(
            course,
            course_index=course_index,
            worker_index=worker_index,
        )
        return record
    finally:
        if worker_session is not runner.session:
            worker_session.close()


def new_worker_session(session: TmsSession) -> TmsSession:
    if type(session) is TmsSession or "clone_authenticated" in type(session).__dict__:
        return session.clone_authenticated()
    return session


def run_course_until_complete(
    runner: Any,
    course: CourseDetail,
    course_index: int,
    worker_index: int | None,
) -> tuple[list[RunResult], dict[str, Any]]:
    started = time.monotonic()
    record = _course_run_record(course, course_index, worker_index, started)
    results: list[RunResult] = []
    attempted_signatures: set[tuple[int | None, str, str, str | None]] = set()
    record["status"] = "running"
    record["events"].append(_scheduler_event("course_started", course=course, worker_index=worker_index))

    if runner.options.dry_run:
        for item in _sorted_course_items(course):
            if item_is_complete(item):
                record["events"].append(_scheduler_event("item_skipped", course=course, item=item, worker_index=worker_index))
                continue
            result = runner._run_item_for_scheduler(course, item, record, worker_index)
            results.append(result)
            if not result.success:
                record["status"] = "failed"
                break
        if record["status"] != "failed":
            record["status"] = "succeeded"
            record["events"].append(_scheduler_event("course_completed", course=course, worker_index=worker_index))
        _finish_course_run_record(record, started)
        return results, record

    while True:
        item = first_incomplete_item(course)
        if not item:
            record["status"] = "succeeded"
            record["events"].append(_scheduler_event("course_completed", course=course, worker_index=worker_index))
            break

        signature = (item.order, normalize_text(item.title), str(item.state), item.result)
        if signature in attempted_signatures:
            message = "course detail did not advance after a successful item run"
            record["status"] = "failed"
            record["errors"].append(_scheduler_error(course, item, "course_not_advancing", message))
            record["events"].append(
                _scheduler_event(
                    "course_failed",
                    course=course,
                    item=item,
                    worker_index=worker_index,
                    status="course_not_advancing",
                    message=message,
                )
            )
            break
        attempted_signatures.add(signature)

        result = runner._run_item_for_scheduler(course, item, record, worker_index)
        results.append(result)
        if not result.success:
            record["status"] = "failed"
            break
        try:
            course = runner._get_course_detail(course.url)
        except Exception as exc:
            record["status"] = "failed"
            record["errors"].append(_scheduler_error(course, item, "detail_refresh_failed", str(exc)))
            record["events"].append(
                _scheduler_event(
                    "course_failed",
                    course=course,
                    item=item,
                    worker_index=worker_index,
                    status="detail_refresh_failed",
                    message=str(exc),
                )
            )
            break

    _finish_course_run_record(record, started)
    return results, record


def run_item_for_scheduler(
    runner: Any,
    course: CourseDetail,
    item: CourseItem,
    record: dict[str, Any],
    worker_index: int | None,
) -> RunResult:
    item_started = time.monotonic()
    record["events"].append(_scheduler_event("item_started", course=course, item=item, worker_index=worker_index))
    try:
        result = runner.run_item(course, item)
    except Exception as exc:
        result = RunResult(False, ItemState.BLOCKED, str(exc), course=course, item=item)
    elapsed = time.monotonic() - item_started
    row = serialize_run_result(result)
    row.update(
        {
            "course_id": course.course_id,
            "course_url": course.url,
            "item_order": item.order,
            "item_kind": str(item.kind),
            "elapsed_seconds": round(elapsed, 3),
            "worker_index": worker_index,
        }
    )
    record["item_results"].append(row)
    record["events"].append(
        _scheduler_event(
            "item_finished",
            course=course,
            item=item,
            worker_index=worker_index,
            status=str(result.state),
            message=result.message,
            elapsed_seconds=round(elapsed, 3),
            success=result.success,
        )
    )
    if not result.success:
        record["errors"].append(_scheduler_error(course, item, str(result.state), result.message))
        record["events"].append(
            _scheduler_event(
                "course_failed",
                course=course,
                item=item,
                worker_index=worker_index,
                status=str(result.state),
                message=result.message,
            )
        )
    return result


def selected_backend(options: Any) -> OperationBackend:
    return OperationBackend(options.backend)


def list_pending_courses_for_runner(runner: Any) -> list[Any]:
    method = runner.session.list_pending_courses
    if _call_accepts_backend(method):
        return method(backend=selected_backend(runner.options))
    return method()


def get_course_detail_for_runner(runner: Any, course: str) -> CourseDetail:
    method = runner.session.get_course_detail
    if _call_accepts_backend(method):
        return method(course, backend=selected_backend(runner.options))
    return method(course)


def _copy_run_options(options: Any) -> Any:
    auth_options = replace(options.auth_options, login_method=LoginMethod.AUTO)
    return replace(options, auth_options=auth_options)


def _call_accepts_backend(method: Any) -> bool:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return True
    return "backend" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    )


def _sorted_course_items(course: CourseDetail) -> list[CourseItem]:
    return sorted(course.items, key=lambda item: (item.order is None, item.order or 0))


def _course_run_record(
    course: CourseDetail,
    course_index: int,
    worker_index: int | None,
    started_at_monotonic: float,
) -> dict[str, Any]:
    return {
        "course_index": course_index,
        "worker_index": worker_index,
        "course": course.title,
        "course_id": course.course_id,
        "course_url": course.url,
        "status": "queued",
        "started_at_monotonic": round(started_at_monotonic, 3),
        "elapsed_seconds": 0.0,
        "item_results": [],
        "events": [_scheduler_event("course_queued", course=course, worker_index=worker_index)],
        "errors": [],
    }


def _failed_course_run_record(
    course_index: int,
    worker_index: int | None,
    title: str,
    url: str,
    course_id: str | None,
    status: str,
    message: str,
    started_at_monotonic: float,
) -> dict[str, Any]:
    elapsed = round(time.monotonic() - started_at_monotonic, 3)
    error = {
        "course": title,
        "course_id": course_id,
        "course_url": url,
        "item": None,
        "item_order": None,
        "item_kind": None,
        "status": status,
        "message": message,
    }
    event = {
        "event": "course_failed",
        "course": title,
        "course_id": course_id,
        "course_url": url,
        "item": None,
        "item_order": None,
        "item_kind": None,
        "worker_index": worker_index,
        "status": status,
        "message": message,
        "elapsed_seconds": elapsed,
    }
    return {
        "course_index": course_index,
        "worker_index": worker_index,
        "course": title,
        "course_id": course_id,
        "course_url": url,
        "status": "failed",
        "failure_status": status,
        "message": message,
        "started_at_monotonic": round(started_at_monotonic, 3),
        "elapsed_seconds": elapsed,
        "item_results": [],
        "events": [event],
        "errors": [error],
    }


def _finish_course_run_record(record: dict[str, Any], started_at_monotonic: float) -> None:
    record["elapsed_seconds"] = round(time.monotonic() - started_at_monotonic, 3)
    record["item_count"] = len(record.get("item_results", []))
    record["failed_item_count"] = sum(1 for row in record.get("item_results", []) if not row.get("success"))
    if record.get("status") == "failed" and record.get("errors") and not record.get("failure_status"):
        record["failure_status"] = record["errors"][0].get("status")


def _scheduler_event(
    event: str,
    course: CourseDetail,
    item: CourseItem | None = None,
    worker_index: int | None = None,
    status: str | None = None,
    message: str = "",
    elapsed_seconds: float | None = None,
    success: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": event,
        "course": course.title,
        "course_id": course.course_id,
        "course_url": course.url,
        "item": item.title if item else None,
        "item_order": item.order if item else None,
        "item_kind": str(item.kind) if item else None,
        "worker_index": worker_index,
    }
    if status is not None:
        payload["status"] = status
    if message:
        payload["message"] = message
    if elapsed_seconds is not None:
        payload["elapsed_seconds"] = elapsed_seconds
    if success is not None:
        payload["success"] = success
    return payload


def _scheduler_error(course: CourseDetail, item: CourseItem | None, status: str, message: str) -> dict[str, Any]:
    return {
        "course": course.title,
        "course_id": course.course_id,
        "course_url": course.url,
        "item": item.title if item else None,
        "item_order": item.order if item else None,
        "item_kind": str(item.kind) if item else None,
        "status": status,
        "message": message,
    }


def _scheduler_summary(
    backend: str,
    requested_concurrency: int,
    effective_concurrency: int,
    pending_count_before: int,
    pending_count_after: int,
    course_runs: list[dict[str, Any]],
    item_results: list[dict[str, Any]],
) -> dict[str, Any]:
    succeeded_courses = sum(1 for record in course_runs if record.get("status") == "succeeded")
    failed_courses = sum(1 for record in course_runs if record.get("status") == "failed")
    running_courses = sum(1 for record in course_runs if record.get("status") == "running")
    succeeded_items = sum(1 for row in item_results if row.get("success"))
    failed_items = sum(1 for row in item_results if not row.get("success"))
    return {
        "backend": backend,
        "requested_concurrency": requested_concurrency,
        "effective_concurrency": effective_concurrency,
        "pending_count_before": pending_count_before,
        "pending_count_after": pending_count_after,
        "course_count": len(course_runs),
        "succeeded_courses": succeeded_courses,
        "failed_courses": failed_courses,
        "running_courses": running_courses,
        "item_count": len(item_results),
        "succeeded_items": succeeded_items,
        "failed_items": failed_items,
    }


def first_incomplete_item(course: CourseDetail) -> CourseItem | None:
    for item in _sorted_course_items(course):
        if not item_is_complete(item):
            return item
    return None


def item_is_complete(item: CourseItem) -> bool:
    return item.state == ItemState.PASSED or result_satisfies_condition(
        item.pass_condition,
        item.result,
        item.passed_marker,
    )


def find_matching_item(course: CourseDetail, item: CourseItem) -> CourseItem | None:
    for candidate in course.items:
        if item.order is not None and candidate.order == item.order:
            return candidate
    for candidate in course.items:
        if normalize_text(candidate.title) == normalize_text(item.title):
            return candidate
    for candidate in course.items:
        if normalize_text(item.title) and normalize_text(item.title) in normalize_text(candidate.title):
            return candidate
    return None


def next_adaptive_limit(current: int, success: bool, max_limit: int, adaptive: bool = True) -> int:
    if not adaptive:
        return current
    if success:
        return min(max_limit, current + 2)
    return max(4, current // 2)


def serialize_run_result(result: RunResult) -> dict[str, Any]:
    return {
        "success": result.success,
        "state": str(result.state),
        "message": result.message,
        "course": getattr(result.course, "title", None),
        "item": getattr(result.item, "title", None),
        "data": result.data,
        "has_question_bank_snippet": bool(result.sanitized_question_bank_snippet),
    }


__all__ = [
    "find_matching_item",
    "first_incomplete_item",
    "get_course_detail_for_runner",
    "item_is_complete",
    "list_pending_courses_for_runner",
    "new_worker_session",
    "next_adaptive_limit",
    "run_course_until_complete",
    "run_course_with_worker",
    "run_item_for_scheduler",
    "run_scheduler_for_runner",
    "selected_backend",
    "serialize_run_result",
]
