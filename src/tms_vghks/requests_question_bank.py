from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .export_pacing import ExportPacerProtocol, ExportPacingOptions, make_export_pacer
from .models import ItemKind
from .playwright_probe import (
    HistoricalQuizBankResult,
    answer_status_counts,
    annotate_canonical_answers,
    build_historical_quiz_records,
    render_historical_quiz_bank_markdown,
)
from .question_bank_export import ExportIssue, QuestionBankRecord, to_jsonable
from .requests_probe import probe_activity_requests
from .session import TmsSession

DEFAULT_REQUESTS_HISTORICAL_QUIZ_JSONL_PATH = ".tms_private_exports/question-bank-history-requests.jsonl"
DEFAULT_REQUESTS_HISTORICAL_QUIZ_MARKDOWN_PATH = ".tms_private_exports/question-bank-history-requests.md"


def export_historical_quiz_bank_requests(
    session: TmsSession,
    output_path: str | Path = DEFAULT_REQUESTS_HISTORICAL_QUIZ_JSONL_PATH,
    markdown_path: str | Path | None = DEFAULT_REQUESTS_HISTORICAL_QUIZ_MARKDOWN_PATH,
    source_account_label: str = "",
    allow_private_export: bool = False,
    include_unsubmitted_records: bool = False,
    course_limit: int | None = None,
    activity_limit: int | None = None,
    pacing_options: ExportPacingOptions | None = None,
    pacer: ExportPacerProtocol | None = None,
) -> HistoricalQuizBankResult:
    if not allow_private_export:
        raise ValueError("private export requires --allow-private-export")

    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    courses = session.list_completed_courses()
    if course_limit is not None:
        courses = courses[: max(0, course_limit)]

    records: list[QuestionBankRecord] = []
    issues: list[ExportIssue] = []
    activity_count = 0
    quiz_activity_count = 0
    attempt_count = 0
    processed_quiz_activities = 0
    stop = False
    export_pacer = pacer or make_export_pacer(pacing_options)

    for course in courses:
        export_pacer.sleep("requests:course_detail")
        try:
            detail = session.get_course_detail(course.detail_url or course.course_id or course.title)
        except Exception as exc:
            issues.append(ExportIssue("course_detail_unavailable", f"{course.title}: {exc}"))
            continue
        activity_count += len(detail.items)
        for item in detail.items:
            if ItemKind(item.kind) != ItemKind.QUIZ:
                continue
            quiz_activity_count += 1
            if activity_limit is not None and processed_quiz_activities >= activity_limit:
                stop = True
                break
            processed_quiz_activities += 1
            export_pacer.sleep("requests:activity_probe")
            probe = probe_activity_requests(
                session,
                detail,
                item,
                include_unsubmitted_records=include_unsubmitted_records,
                pacer=export_pacer,
            )
            attempt_count += len(probe.attempt_probes or ([probe] if probe.attempt else []))
            built_records, built_issues = build_historical_quiz_records(
                course=course,
                detail=detail,
                item=item,
                probe=probe,
                exported_at=exported_at,
                source_account_label=source_account_label,
                include_unsubmitted_records=include_unsubmitted_records,
                collector_method="requests-historical-kexam-record",
            )
            records.extend(built_records)
            issues.extend(built_issues)
        if stop:
            break

    annotate_canonical_answers(records)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(to_jsonable(record), ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    if markdown_path:
        markdown = Path(markdown_path)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(
            render_historical_quiz_bank_markdown(records, pacing=dict(export_pacer.summary())),
            encoding="utf-8",
        )

    record_issues = [issue for record in records for issue in record.issues]
    all_issues = issues + record_issues
    counts = answer_status_counts(records)
    return HistoricalQuizBankResult(
        output_path=str(output),
        markdown_path=str(markdown_path) if markdown_path else None,
        record_count=len(records),
        course_count=len(courses),
        activity_count=activity_count,
        quiz_activity_count=quiz_activity_count,
        attempt_count=attempt_count,
        question_count=sum(1 for record in records if record.question.get("text")),
        verified_correct_count=counts.get("verified_correct", 0),
        verified_wrong_count=counts.get("verified_wrong", 0),
        unverified_selected_count=counts.get("unverified_selected", 0),
        unsubmitted_count=counts.get("unsubmitted", 0),
        issue_count=len(all_issues),
        issues=all_issues,
        pacing=dict(export_pacer.summary()),
    )


__all__ = [
    "DEFAULT_REQUESTS_HISTORICAL_QUIZ_JSONL_PATH",
    "DEFAULT_REQUESTS_HISTORICAL_QUIZ_MARKDOWN_PATH",
    "export_historical_quiz_bank_requests",
]
