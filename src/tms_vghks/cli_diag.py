from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .handlers import RunOptions, TmsRunner, item_is_complete
from .models import CourseDetail, CourseItem, CourseSummary, ItemKind
from .requests_form_submit import probe_form_submit_requests, run_quiz_requests_submit, run_survey_requests_submit
from .session import TmsSession


@dataclass(slots=True)
class RequestsFormSubmitDiagnosticBatch:
    success: bool
    status: str
    kind: str
    scope: str
    requested_kinds: list[str]
    results: list[Any] = field(default_factory=list)
    skipped_candidates: list[dict[str, Any]] = field(default_factory=list)


def run_requests_form_submit_diagnostic(session: TmsSession, args):
    if args.course and (args.item_title or args.item_order is not None):
        detail = session.get_course_detail(args.course)
        item = select_detail_item(detail, args.item_title, args.item_order)
        if args.kind != "both" and str(item.kind) != args.kind:
            raise ValueError(f"selected item kind is {item.kind}, expected {args.kind}")
        if str(item.kind) not in {"survey", "quiz"}:
            raise ValueError(f"requests form submit diagnostics require survey or quiz item, got {item.kind}")
        return _run_requests_form_submit_for_item(session, args, detail, item)

    return _run_requests_form_submit_auto_diagnostic(session, args)


def requests_form_diagnostic_completed(result: Any, probe_only: bool = False) -> bool:
    if diagnostic_result_success(result):
        return True
    if not probe_only:
        return False
    status = diagnostic_result_status(result)
    if status == "no_form_submit_candidate":
        return False
    if hasattr(result, "results"):
        return any(diagnostic_result_status(row) != "no_form_submit_candidate" for row in result.results)
    return bool(status)


def select_detail_item(detail, title: str | None, order: int | None):
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
    incomplete = [item for item in detail.items if not item_is_complete(item)]
    if len(incomplete) == 1:
        return incomplete[0]
    if len(detail.items) == 1:
        return detail.items[0]
    labels = ", ".join(f"{item.order}:{item.title}" for item in detail.items)
    raise ValueError(f"pass --item-order or --item-title; available activities: {labels}")


def _run_requests_form_submit_auto_diagnostic(
    session: TmsSession,
    args,
) -> RequestsFormSubmitDiagnosticBatch:
    requested_kinds = _requested_form_submit_kinds(args.kind)
    details = _form_submit_diagnostic_details(session, args)
    results: list[Any] = []
    skipped_candidates: list[dict[str, Any]] = []
    probe_candidate_limit = _requests_form_candidate_limit(args) if args.probe_only else 1

    for kind in requested_kinds:
        if args.probe_only:
            kind_results = _run_requests_form_submit_probe_candidates(
                session=session,
                args=args,
                details=details,
                kind=kind,
                candidate_limit=probe_candidate_limit,
            )
            if kind_results:
                results.extend(kind_results)
            else:
                results.append(
                    {
                        "success": False,
                        "status": "no_form_submit_candidate",
                        "kind": kind.value,
                    }
                )
        else:
            result = _run_first_requests_form_submit_candidate(
                session=session,
                args=args,
                details=details,
                kind=kind,
                skipped_candidates=skipped_candidates,
            )
            if result is None:
                results.append(
                    {
                        "success": False,
                        "status": "no_form_submit_candidate",
                        "kind": kind.value,
                    }
                )
            else:
                results.append(result)

    if args.probe_only:
        success = all(diagnostic_result_status(result) != "no_form_submit_candidate" for result in results)
        if success:
            status = "requests_form_probe_completed"
        elif all(diagnostic_result_status(result) == "no_form_submit_candidate" for result in results):
            status = "no_form_submit_candidate"
        else:
            status = "requests_form_probe_partial"
    else:
        success = all(diagnostic_result_success(result) for result in results)
        if success:
            status = "requests_form_submit_verified"
        elif all(diagnostic_result_status(result) == "no_form_submit_candidate" for result in results):
            status = "no_form_submit_candidate"
        else:
            status = "requests_form_submit_partial"
    return RequestsFormSubmitDiagnosticBatch(
        success=success,
        status=status,
        kind=args.kind,
        scope=args.scope,
        requested_kinds=[kind.value for kind in requested_kinds],
        results=results,
        skipped_candidates=skipped_candidates,
    )


def _run_requests_form_submit_probe_candidates(
    session: TmsSession,
    args,
    details: list[CourseDetail],
    kind: ItemKind,
    candidate_limit: int,
) -> list[Any]:
    results: list[Any] = []
    for detail in details:
        for item in _iter_form_items(detail, kind):
            results.append(_run_requests_form_submit_for_item(session, args, detail, item))
            if len(results) >= candidate_limit:
                return results
    return results


def _run_first_requests_form_submit_candidate(
    session: TmsSession,
    args,
    details: list[CourseDetail],
    kind: ItemKind,
    skipped_candidates: list[dict[str, Any]],
):
    for detail in details:
        for item in _iter_form_items(detail, kind):
            result = _run_requests_form_submit_for_item(session, args, detail, item)
            if args.probe_only and diagnostic_result_status(result) == "mutation_unsupported":
                return result
            if result.success:
                return result
            if getattr(result, "response_status_code", None) is not None:
                return result
            skipped_candidates.append(_form_submit_candidate_skip(detail, item, result))
            if result.status not in {"form_endpoint_unverified", "form_missing_required_fields", "mutation_unsupported"}:
                return result
    return None


def _run_requests_form_submit_for_item(session: TmsSession, args, detail: CourseDetail, item: CourseItem):
    if getattr(args, "probe_only", False):
        return probe_form_submit_requests(session, detail, item)
    if str(item.kind) == "survey":
        return run_survey_requests_submit(
            session=session,
            course=detail,
            item=item,
        )
    if str(item.kind) == "quiz":
        from .cli_impl import auth_options_from_args

        runner = TmsRunner(
            session,
            RunOptions(
                question_bank_path=args.question_bank,
                quiz_policy=args.quiz,
                auth_options=auth_options_from_args(args),
                gemini_config=getattr(args, "gemini_config", None),
            ),
        )
        return run_quiz_requests_submit(
            session=session,
            course=detail,
            item=item,
            question_bank=runner.question_bank,
            quiz_policy=args.quiz,
            gemini_config=getattr(args, "gemini_config", None),
        )
    raise ValueError(f"requests form submit diagnostics require survey or quiz item, got {item.kind}")


def _requests_form_candidate_limit(args) -> int:
    return max(1, int(getattr(args, "candidate_limit", 1) or 1))


def _requested_form_submit_kinds(kind: str) -> list[ItemKind]:
    if kind == "survey":
        return [ItemKind.SURVEY]
    if kind == "quiz":
        return [ItemKind.QUIZ]
    return [ItemKind.SURVEY, ItemKind.QUIZ]


def _form_submit_diagnostic_details(session: TmsSession, args) -> list[CourseDetail]:
    if args.course:
        return [session.get_course_detail(args.course)]

    details: list[CourseDetail] = []
    summaries: list[CourseSummary] = []
    if args.scope in {"completed", "both"}:
        summaries.extend(session.list_completed_courses())
    if args.scope in {"pending", "both"}:
        summaries.extend(session.list_pending_courses())
    for summary in summaries:
        details.append(session.get_course_detail(_course_summary_ref(summary)))
    return details


def _course_summary_ref(summary: CourseSummary) -> str:
    return summary.detail_url or summary.course_id or summary.title


def _iter_form_items(detail: CourseDetail, kind: ItemKind):
    return (
        item
        for item in sorted(detail.items, key=lambda row: (row.order is None, row.order or 0, row.title))
        if ItemKind(item.kind) == kind
    )


def _form_submit_candidate_skip(detail: CourseDetail, item: CourseItem, result) -> dict[str, Any]:
    return {
        "course_title": detail.title,
        "course_url": detail.url,
        "course_id": detail.course_id,
        "item_title": item.title,
        "item_kind": str(item.kind),
        "item_order": item.order,
        "status": result.status,
        "entry_attempts": list(getattr(result, "entry_attempts", []) or []),
        "issues": list(result.issues),
    }


def diagnostic_result_success(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("success"))
    return bool(getattr(result, "success", False))


def diagnostic_result_status(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("status", "unknown"))
    return str(getattr(result, "status", "unknown"))


__all__ = [
    "RequestsFormSubmitDiagnosticBatch",
    "diagnostic_result_status",
    "diagnostic_result_success",
    "requests_form_diagnostic_completed",
    "run_requests_form_submit_diagnostic",
    "select_detail_item",
]
