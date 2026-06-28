from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .export_pacing import ExportPacerProtocol, ExportPacingOptions, make_export_pacer
from .models import CourseDetail, CourseItem, CourseSummary, ItemKind
from .parsers import normalize_text
from .privacy import redact_sensitive_url
from .session import TmsSession

DEFAULT_EXPORT_PATH = ".tms_private_exports/question-bank.jsonl"
SCHEMA_VERSION = "tms-vghks-question-bank.v1"


@dataclass(slots=True)
class ExportIssue:
    code: str
    message: str
    severity: str = "warning"


@dataclass(slots=True)
class CourseSource:
    course_id: str | None
    title: str
    detail_url: str | None
    completed_at: str | None = None
    raw_text: str = ""


@dataclass(slots=True)
class ActivitySource:
    activity_id: str | None
    order: int | None
    kind: str
    title: str
    deadline: str | None = None
    pass_condition: str | None = None
    result: str | None = None
    passed: bool | None = None
    result_modal_url: str | None = None


@dataclass(slots=True)
class AnswerSource:
    status: str
    answer_type: str
    selected_answers: list[str] = field(default_factory=list)
    free_text: str | None = None
    score: str | None = None
    attempt_at: str | None = None
    raw_summary: str = ""


@dataclass(slots=True)
class QuestionBankRecord:
    schema_version: str
    source_system: str
    exported_at: str
    source_account_label: str
    course: CourseSource
    activity: ActivitySource
    assessment: dict[str, Any]
    question: dict[str, Any]
    answer: AnswerSource
    provenance: dict[str, Any]
    attempt: dict[str, Any] = field(default_factory=dict)
    issues: list[ExportIssue] = field(default_factory=list)


@dataclass(slots=True)
class ExportResult:
    output_path: str | None
    record_count: int
    course_count: int
    activity_count: int
    quiz_activity_count: int
    survey_activity_count: int
    quiz_result_endpoint_count: int
    issue_count: int
    probe_only: bool = False
    issues: list[ExportIssue] = field(default_factory=list)
    pacing: dict[str, Any] = field(default_factory=dict)


def export_question_bank(
    session: TmsSession,
    output_path: str | Path = DEFAULT_EXPORT_PATH,
    include: set[ItemKind] | None = None,
    source_account_label: str = "",
    allow_private_export: bool = False,
    probe_only: bool = False,
    pacing_options: ExportPacingOptions | None = None,
    pacer: ExportPacerProtocol | None = None,
) -> ExportResult:
    include = include or {ItemKind.QUIZ, ItemKind.SURVEY}
    export_pacer = pacer or make_export_pacer(pacing_options)
    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    completed_courses = session.list_completed_courses()
    records: list[QuestionBankRecord] = []
    issues: list[ExportIssue] = []
    activity_count = 0
    quiz_activity_count = 0
    survey_activity_count = 0
    quiz_result_endpoint_count = 0

    for course in completed_courses:
        export_pacer.sleep("requests:course_detail")
        try:
            detail = session.get_course_detail(course.detail_url or course.course_id or course.title)
        except Exception as exc:
            issues.append(ExportIssue("course_detail_unavailable", f"{course.title}: {exc}"))
            continue
        activity_count += len(detail.items)
        for item in detail.items:
            kind = ItemKind(item.kind)
            if kind == ItemKind.QUIZ:
                quiz_activity_count += 1
            if kind == ItemKind.SURVEY:
                survey_activity_count += 1
            if kind not in include:
                continue
            if kind == ItemKind.QUIZ and item.metadata.get("result_modal_url"):
                quiz_result_endpoint_count += 1
            records.append(
                build_record(
                    session=session,
                    course=course,
                    detail=detail,
                    item=item,
                    exported_at=exported_at,
                    source_account_label=source_account_label,
                    pacer=export_pacer,
                )
            )

    record_issues = [issue for record in records for issue in record.issues]
    all_issues = issues + record_issues
    path_text = str(output_path) if output_path else None
    if records and not probe_only:
        if not allow_private_export:
            raise ValueError("private export requires --allow-private-export")
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(to_jsonable(record), ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    return ExportResult(
        output_path=None if probe_only else path_text,
        record_count=len(records),
        course_count=len(completed_courses),
        activity_count=activity_count,
        quiz_activity_count=quiz_activity_count,
        survey_activity_count=survey_activity_count,
        quiz_result_endpoint_count=quiz_result_endpoint_count,
        issue_count=len(all_issues),
        probe_only=probe_only,
        issues=all_issues,
        pacing=dict(export_pacer.summary()),
    )


def build_record(
    session: TmsSession,
    course: CourseSummary,
    detail: CourseDetail,
    item: CourseItem,
    exported_at: str,
    source_account_label: str = "",
    pacer: ExportPacerProtocol | None = None,
) -> QuestionBankRecord:
    answer = AnswerSource(status="unavailable", answer_type=str(item.kind), score=item.result)
    issues: list[ExportIssue] = []
    provenance: dict[str, Any] = {
        "collector": "tms-vghks",
        "method": "requests",
        "course_url": detail.url,
    }
    if item.metadata.get("result_modal_url"):
        provenance["result_modal_url"] = redact_sensitive_url(str(item.metadata.get("result_modal_url")))
        try:
            if pacer:
                pacer.sleep("requests:result_modal")
            result = fetch_result_modal_summary(session, str(item.metadata["result_modal_url"]))
        except Exception as exc:
            result = None
            issues.append(ExportIssue("result_endpoint_unavailable", str(exc)))
        if result:
            answer.score = result.get("score") or answer.score
            answer.attempt_at = result.get("attempt_at")
            answer.raw_summary = result.get("raw_summary", "")
            provenance["result_endpoint_kind"] = result.get("kind")
    if ItemKind(item.kind) == ItemKind.QUIZ:
        issues.append(
            ExportIssue(
                "quiz_answers_unavailable",
                "TMS result endpoint exposed score metadata only; question text and selected answers were not available.",
            )
        )
    elif ItemKind(item.kind) == ItemKind.SURVEY:
        issues.append(
            ExportIssue(
                "survey_answers_unavailable",
                "Course detail only exposed survey completion status; submitted survey answers were not available.",
            )
        )

    return QuestionBankRecord(
        schema_version=SCHEMA_VERSION,
        source_system="tms.vghks.gov.tw",
        exported_at=exported_at,
        source_account_label=source_account_label,
        course=CourseSource(
            course_id=course.course_id or detail.course_id,
            title=course.title,
            detail_url=course.detail_url or detail.url,
            completed_at=course.progress,
            raw_text=course.raw_text,
        ),
        activity=ActivitySource(
            activity_id=item.metadata.get("activity_id"),
            order=item.order,
            kind=str(item.kind),
            title=item.title,
            deadline=item.metadata.get("deadline"),
            pass_condition=item.pass_condition,
            result=item.result,
            passed=item.passed,
            result_modal_url=redact_sensitive_url(str(item.metadata.get("result_modal_url")))
            if item.metadata.get("result_modal_url")
            else None,
        ),
        assessment={
            "type": str(item.kind),
            "score": answer.score,
            "passing_condition": item.pass_condition,
            "answer_status": answer.status,
            "confidence": 0.2,
        },
        question={
            "text": None,
            "options": [],
            "type": str(item.kind),
            "normalized_text": None,
        },
        answer=answer,
        provenance=provenance,
        issues=issues,
    )


def fetch_result_modal_summary(session: TmsSession, result_modal_url: str) -> dict[str, str] | None:
    response = session.get(result_modal_url, allow_redirects=False)
    html = response.text
    kind = "html"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        kind = "json"
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("html"), str):
            html = data["html"]
    soup = BeautifulSoup(html or "", "html.parser")
    rows = []
    for row in soup.find_all("tr"):
        cells = [normalize_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    result: dict[str, str] = {"kind": kind, "raw_summary": normalize_text(soup.get_text(" ", strip=True))[:1000]}
    for cells in rows:
        if len(cells) >= 2 and cells[0] not in {"測驗日期", "分數"}:
            result["attempt_at"] = cells[0]
            result["score"] = cells[1]
            break
    return result


def parse_include(value: str) -> set[ItemKind]:
    mapping = {
        "quiz": ItemKind.QUIZ,
        "survey": ItemKind.SURVEY,
    }
    include: set[ItemKind] = set()
    for part in (value or "").split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key not in mapping:
            raise ValueError(f"unsupported export include value: {part}")
        include.add(mapping[key])
    return include or {ItemKind.QUIZ, ItemKind.SURVEY}


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value
