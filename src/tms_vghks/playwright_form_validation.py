from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .handlers import RunOptions, TmsRunner, serialize_run_result
from .models import AuthOptions, CourseDetail, CourseItem, CourseSummary, ItemKind, OperationBackend, RunResult
from .parsers import normalize_text
from .playwright_probe import (
    REVIEW_LINK_MARKERS,
    ActivityProbe,
    ExtractedQuestion,
    KExamAttempt,
    extract_kexam_attempts_from_modal_html,
    extract_activity_probe_from_html,
    html_from_page_or_json_text,
    kexam_attempt_to_dict,
    probe_kexam_attempt_with_page,
)
from .privacy import redact_sensitive_url
from .question_bank_export import ExportIssue, parse_include
from .session import TmsError, TmsSession

DEFAULT_VALIDATION_JSONL_PATH = ".tms_private_exports/playwright-form-validation.jsonl"
DEFAULT_VALIDATION_MARKDOWN_PATH = ".tms_private_exports/playwright-form-validation.md"

VALIDATION_SCHEMA_VERSION = "tms-vghks-playwright-form-validation.v1"
FORM_ENTRY_LABELS = (
    "進入測驗",
    "開始測驗",
    "重新測驗",
    "填寫問卷",
    "重新填寫",
    "開始",
    "進入",
)
PASSED_QUIZ_ENTRY_LABELS = ("檢視作答紀錄", "測驗結果", "詳細結果", "學習成果")
PASSED_SURVEY_ENTRY_LABELS = ("問卷回饋狀態", "統計結果", "學習成果")
OPEN_QUIZ_ENTRY_LABELS = ("進入測驗", "開始測驗", "重新測驗", "進入", "開始")
OPEN_SURVEY_ENTRY_LABELS = ("填寫問卷", "重新填寫", "進入", "開始")


@dataclass(slots=True)
class FormFieldSummary:
    radio_groups: int = 0
    checkbox_groups: int = 0
    text_fields: int = 0
    contenteditable_fields: int = 0
    submit_buttons: list[str] = field(default_factory=list)
    fill_counter: str | None = None


@dataclass(slots=True)
class FormValidationRecord:
    schema_version: str
    exported_at: str
    scope: str
    source_system: str
    course: dict[str, Any]
    activity: dict[str, Any]
    classification: str
    entry_method: str | None = None
    questions: list[dict[str, Any]] = field(default_factory=list)
    form_fields: FormFieldSummary = field(default_factory=FormFieldSummary)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    record_count: int = 0
    record_question_count: int = 0
    record_selected_answer_count: int = 0
    selected_answers: dict[str, list[str]] = field(default_factory=dict)
    submit_attempted: bool = False
    submit_result: dict[str, Any] | None = None
    verification_item: dict[str, Any] | None = None
    issues: list[ExportIssue] = field(default_factory=list)


@dataclass(slots=True)
class FormValidationResult:
    output_path: str
    markdown_path: str | None
    record_count: int
    course_count: int
    activity_count: int
    classification_counts: dict[str, int]
    submit_attempt_count: int
    submit_success_count: int
    issue_count: int
    issues: list[ExportIssue] = field(default_factory=list)


@dataclass(slots=True)
class ActivityHtmlPart:
    entry: str
    html: str = ""
    attempt: KExamAttempt | None = None
    probe: ActivityProbe | None = None


def validate_playwright_forms(
    session: TmsSession,
    scope: set[str] | None = None,
    include: set[ItemKind] | None = None,
    output_path: str | Path = DEFAULT_VALIDATION_JSONL_PATH,
    markdown_path: str | Path | None = DEFAULT_VALIDATION_MARKDOWN_PATH,
    auth_options: AuthOptions | None = None,
    course_limit: int | None = None,
    activity_limit: int | None = None,
    include_unsubmitted_records: bool = False,
) -> FormValidationResult:
    scope = scope or {"completed", "pending"}
    include = include or {ItemKind.QUIZ, ItemKind.SURVEY}
    auth_options = auth_options or AuthOptions()
    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    records: list[FormValidationRecord] = []
    issues: list[ExportIssue] = []
    course_count = 0
    activity_count = 0

    processed_courses = 0
    stop = False
    for scope_name, courses in _courses_for_scope(session, scope):
        if course_limit is not None:
            courses = courses[: max(0, course_limit - processed_courses)]
        course_count += len(courses)
        if scope_name == "pending" and not courses:
            issues.append(ExportIssue("no_pending_courses", "No pending TMS courses were found.", "info"))
        for course in courses:
            processed_courses += 1
            try:
                detail = session.get_course_detail_playwright(course.detail_url or course.course_id or course.title)
            except Exception as exc:
                issues.append(ExportIssue("course_detail_unavailable", f"{course.title}: {exc}"))
                continue
            activity_count += len(detail.items)
            for item in detail.items:
                if ItemKind(item.kind) not in include:
                    continue
                if activity_limit is not None and len(records) >= activity_limit:
                    stop = True
                    break
                records.append(
                    validate_activity_playwright(
                        session=session,
                        scope_name=scope_name,
                        course=course,
                        detail=detail,
                        item=item,
                        exported_at=exported_at,
                        auth_options=auth_options,
                        include_unsubmitted_records=include_unsubmitted_records,
                    )
                )
            if stop:
                break
        if stop or (course_limit is not None and processed_courses >= course_limit):
            break

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(to_jsonable(record), ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    markdown_text = render_validation_markdown(records, issues)
    if markdown_path:
        markdown = Path(markdown_path)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(markdown_text, encoding="utf-8")

    all_issues = issues + [issue for record in records for issue in record.issues]
    counts: dict[str, int] = {}
    for record in records:
        counts[record.classification] = counts.get(record.classification, 0) + 1
    return FormValidationResult(
        output_path=str(output),
        markdown_path=str(markdown_path) if markdown_path else None,
        record_count=len(records),
        course_count=course_count,
        activity_count=activity_count,
        classification_counts=counts,
        submit_attempt_count=sum(1 for record in records if record.submit_attempted),
        submit_success_count=sum(1 for record in records if record.submit_result and record.submit_result.get("success")),
        issue_count=len(all_issues),
        issues=all_issues,
    )


def validate_activity_playwright(
    session: TmsSession,
    scope_name: str,
    course: CourseSummary,
    detail: CourseDetail,
    item: CourseItem,
    exported_at: str,
    auth_options: AuthOptions,
    include_unsubmitted_records: bool = False,
) -> FormValidationRecord:
    record = base_record(scope_name, course, detail, item, exported_at)
    html_parts = collect_activity_html_parts(
        session,
        detail,
        item,
        include_unsubmitted_records=include_unsubmitted_records,
    )
    best_classification = "result_only" if item.result else "review_unavailable"
    best_entry = None
    best_questions: list[ExtractedQuestion] = []
    best_fields = FormFieldSummary()

    for part in html_parts:
        if part.attempt:
            record.attempts.append(kexam_attempt_to_dict(part.attempt))
            record.record_count += 1
        if part.probe:
            fields = FormFieldSummary()
            questions = part.probe.question_records
            record.record_question_count += len(questions)
            record.record_selected_answer_count += sum(len(question.selected_answers) for question in questions)
            if questions:
                classification = "record_available"
            elif part.attempt and part.attempt.submitted_status == "unsubmitted":
                classification = "result_only"
                record.issues.extend(part.probe.issues)
            else:
                classification = "review_unavailable"
                record.issues.extend(part.probe.issues)
        else:
            classification, fields, questions = classify_form_html(part.html, item.kind)
        if classification_rank(classification) > classification_rank(best_classification):
            best_classification = classification
            best_entry = part.entry
            best_questions = questions
            best_fields = fields
    record.classification = best_classification
    record.entry_method = best_entry
    record.form_fields = best_fields
    record.questions = [question_to_dict(question) for question in best_questions]

    if best_classification in {"result_only", "review_unavailable"}:
        code = "result_only_unavailable" if best_classification == "result_only" else "review_unavailable"
        record.issues.append(ExportIssue(code, "No fillable or reviewable question form was available."))
        return record
    if best_classification == "review_available":
        record.issues.append(ExportIssue("review_available_no_submit", "Questions were visible but no submit control was available.", "info"))
        return record
    if best_classification == "record_available":
        record.issues.append(ExportIssue("record_available_no_submit", "KExam record questions were available read-only.", "info"))
        return record
    if best_classification == "blocked_or_sequence_guard":
        record.issues.append(ExportIssue("blocked_or_sequence_guard", "TMS blocked the activity or required ordered completion."))
        return record
    if best_classification == "form_available":
        run_result = submit_activity_with_runner(session, detail, item, auth_options)
        record.submit_attempted = True
        record.submit_result = serialize_run_result(run_result)
        if run_result.item:
            record.verification_item = item_to_dict(run_result.item)
        if not run_result.success:
            record.issues.append(ExportIssue("submit_not_verified", run_result.message))
    return record


def collect_activity_html_parts(
    session: TmsSession,
    detail: CourseDetail,
    item: CourseItem,
    include_unsubmitted_records: bool = False,
) -> list[ActivityHtmlPart]:
    session.start_browser(headless=False)
    assert session.context is not None
    session.sync_cookies_to_browser()
    page = session.context.new_page()
    parts: list[ActivityHtmlPart] = []
    try:
        result_url = item.metadata.get("result_modal_url")
        if result_url:
            try:
                page.goto(str(result_url), wait_until="domcontentloaded", timeout=60000)
                result_html = html_from_page_or_json_text(page)
                parts.append(ActivityHtmlPart("result_modal_url", result_html))
                if ItemKind(item.kind) == ItemKind.QUIZ:
                    for attempt in extract_kexam_attempts_from_modal_html(result_html, session.base_url):
                        try:
                            probe = probe_kexam_attempt_with_page(
                                page,
                                attempt,
                                item.kind,
                                include_unsubmitted_records=include_unsubmitted_records,
                            )
                            parts.append(
                                ActivityHtmlPart(
                                    entry=f"kexam_record:{attempt.record_id or ''}",
                                    attempt=attempt,
                                    probe=probe,
                                )
                            )
                        except Exception as exc:
                            parts.append(
                                ActivityHtmlPart(
                                    entry=f"kexam_record_error:{attempt.record_id or ''}",
                                    html=f"<div>{exc}</div>",
                                    attempt=attempt,
                                    probe=ActivityProbe(
                                        score=attempt.score,
                                        attempt_at=attempt.attempt_at,
                                        raw_summary=attempt.raw_summary,
                                        reason=str(exc),
                                        attempt=attempt,
                                        issues=[ExportIssue("record_page_unavailable", str(exc))],
                                    ),
                                )
                            )
            except Exception as exc:
                parts.append(ActivityHtmlPart("result_modal_url_error", f"<div>{exc}</div>"))

        try:
            page.goto(detail.url, wait_until="domcontentloaded", timeout=60000)
            row = row_locator_for_title(page, item.title)
            for label in entry_labels_for_item(item):
                try:
                    target = row.get_by_text(label, exact=False).first
                    if target.is_visible(timeout=400):
                        target.click()
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            pass
                        parts.append(ActivityHtmlPart(f"clicked:{label}", page.content()))
                        page.goto(detail.url, wait_until="domcontentloaded", timeout=60000)
                        row = row_locator_for_title(page, item.title)
                except Exception:
                    continue
            try:
                title_target = row.get_by_text(item.title, exact=False).first
                if title_target.is_visible(timeout=400):
                    title_target.click()
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    parts.append(ActivityHtmlPart("clicked:title", page.content()))
            except Exception:
                pass
        except Exception as exc:
            parts.append(ActivityHtmlPart("course_row_error", f"<div>{exc}</div>"))
    finally:
        try:
            page.close()
        except Exception:
            pass
    return parts


def entry_labels_for_item(item: CourseItem) -> tuple[str, ...]:
    kind = ItemKind(item.kind)
    if item.passed and kind == ItemKind.QUIZ:
        return PASSED_QUIZ_ENTRY_LABELS
    if item.passed and kind == ItemKind.SURVEY:
        return PASSED_SURVEY_ENTRY_LABELS
    if kind == ItemKind.QUIZ:
        return OPEN_QUIZ_ENTRY_LABELS + PASSED_QUIZ_ENTRY_LABELS
    if kind == ItemKind.SURVEY:
        return OPEN_SURVEY_ENTRY_LABELS + PASSED_SURVEY_ENTRY_LABELS
    return FORM_ENTRY_LABELS + REVIEW_LINK_MARKERS


def classify_form_html(html: str, item_kind: ItemKind | str) -> tuple[str, FormFieldSummary, list[ExtractedQuestion]]:
    soup = BeautifulSoup(html or "", "html.parser")
    text = normalize_text(soup.get_text(" ", strip=True))
    if "儲存失敗" in text or "請檢查伺服器狀態" in text:
        return "transient_error", summarize_form_fields(soup), []
    fields = summarize_form_fields(soup)
    probe = extract_activity_probe_from_html(html, item_kind)
    questions = probe.question_records
    if has_fillable_form(fields):
        return "form_available", fields, questions
    if questions:
        return "review_available", fields, questions
    if probe.score or any(token in text for token in ("測驗日期", "分數", "成績", "學習成果", "已完成", "通過")):
        return "result_only", fields, []
    if ("請依序完成" in text or "依序完成" in text) and not soup.select_one("#activityTree"):
        return "blocked_or_sequence_guard", fields, []
    return "review_unavailable", fields, []


def summarize_form_fields(soup: BeautifulSoup) -> FormFieldSummary:
    radio_names = {node.get("name") or node.get("id") for node in soup.select('input[type="radio"]') if node.get("name") or node.get("id")}
    checkbox_names = {node.get("name") or node.get("id") for node in soup.select('input[type="checkbox"]') if node.get("name") or node.get("id")}
    submit_buttons = []
    for node in soup.find_all(["button", "input", "a"]):
        text = normalize_text(node.get("value") or node.get_text(" ", strip=True) or node.get("title"))
        kind = (node.get("type") or "").lower()
        if kind == "submit" or any(label in text for label in ("送出", "提交", "確定", "交卷")):
            if text:
                submit_buttons.append(text)
    text = normalize_text(soup.get_text(" ", strip=True))
    counter_match = re.search(r"已填寫[:：]\s*\d+\s*/\s*\d+", text)
    return FormFieldSummary(
        radio_groups=len(radio_names),
        checkbox_groups=len(checkbox_names),
        text_fields=len(soup.select("textarea, input[type='text']")),
        contenteditable_fields=len(soup.select("[contenteditable='true']")),
        submit_buttons=submit_buttons,
        fill_counter=counter_match.group(0) if counter_match else None,
    )


def has_fillable_form(fields: FormFieldSummary) -> bool:
    return bool(
        fields.submit_buttons
        and (fields.radio_groups or fields.checkbox_groups or fields.text_fields or fields.contenteditable_fields)
    )


def submit_activity_with_runner(
    session: TmsSession,
    detail: CourseDetail,
    item: CourseItem,
    auth_options: AuthOptions,
) -> RunResult:
    options = RunOptions(
        survey_policy="neutral",
        quiz_policy="auto",
        backend=OperationBackend.PLAYWRIGHT,
        auth_options=auth_options,
        interactive=False,
    )
    runner = TmsRunner(session, options)
    if ItemKind(item.kind) == ItemKind.QUIZ:
        return runner.run_quiz(detail, item)
    if ItemKind(item.kind) == ItemKind.SURVEY:
        return runner.run_survey(detail, item)
    raise TmsError(f"unsupported form item kind: {item.kind}")


def base_record(
    scope_name: str,
    course: CourseSummary,
    detail: CourseDetail,
    item: CourseItem,
    exported_at: str,
) -> FormValidationRecord:
    return FormValidationRecord(
        schema_version=VALIDATION_SCHEMA_VERSION,
        exported_at=exported_at,
        scope=scope_name,
        source_system="tms.vghks.gov.tw",
        course={
            "course_id": course.course_id or detail.course_id,
            "title": course.title,
            "detail_url": course.detail_url or detail.url,
            "completed_at": course.progress,
        },
        activity={
            "activity_id": item.metadata.get("activity_id"),
            "order": item.order,
            "kind": str(item.kind),
            "title": item.title,
            "pass_condition": item.pass_condition,
            "result": item.result,
            "passed": item.passed,
            "result_modal_url": redact_sensitive_url(str(item.metadata.get("result_modal_url")))
            if item.metadata.get("result_modal_url")
            else None,
        },
        classification="unknown",
    )


def render_validation_markdown(records: list[FormValidationRecord], issues: list[ExportIssue] | None = None) -> str:
    lines = ["# TMS Playwright Form Validation", ""]
    lines.append(f"- Records: {len(records)}")
    counts: dict[str, int] = {}
    for record in records:
        counts[record.classification] = counts.get(record.classification, 0) + 1
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    if issues:
        lines.append(f"- GlobalIssues: {len(issues)}")
    lines.append("")
    for record in records:
        lines.append(f"## {record.course.get('title')} / {record.activity.get('title')}")
        lines.append(f"- Scope: {record.scope}")
        lines.append(f"- Kind: {record.activity.get('kind')}")
        lines.append(f"- Classification: {record.classification}")
        if record.entry_method:
            lines.append(f"- EntryMethod: {record.entry_method}")
        lines.append(
            "- Fields: "
            f"radio={record.form_fields.radio_groups}, "
            f"checkbox={record.form_fields.checkbox_groups}, "
            f"text={record.form_fields.text_fields}, "
            f"editor={record.form_fields.contenteditable_fields}"
        )
        if record.record_count:
            lines.append(
                "- KExamRecords: "
                f"records={record.record_count}, "
                f"questions={record.record_question_count}, "
                f"selected_answers={record.record_selected_answer_count}"
            )
        lines.append(f"- SubmitAttempted: {record.submit_attempted}")
        if record.submit_result:
            lines.append(f"- SubmitSuccess: {record.submit_result.get('success')}")
            lines.append(f"- SubmitMessage: {record.submit_result.get('message')}")
        if record.questions:
            lines.append(f"- Questions: {len(record.questions)}")
        if record.issues:
            lines.append("- Issues: " + " | ".join(issue.code for issue in record.issues))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def parse_scope(value: str) -> set[str]:
    allowed = {"completed", "pending"}
    scope = {part.strip().lower() for part in (value or "").split(",") if part.strip()}
    invalid = scope - allowed
    if invalid:
        raise ValueError(f"unsupported validation scope: {', '.join(sorted(invalid))}")
    return scope or {"completed", "pending"}


def _courses_for_scope(session: TmsSession, scope: set[str]):
    if "completed" in scope:
        yield "completed", session.list_completed_courses_playwright()
    if "pending" in scope:
        yield "pending", session.list_pending_courses_playwright()


def classification_rank(value: str) -> int:
    return {
        "review_unavailable": 0,
        "blocked_or_sequence_guard": 0,
        "transient_error": 0,
        "result_only": 1,
        "record_available": 3,
        "review_available": 3,
        "form_available": 4,
    }.get(value, 0)


def question_to_dict(question: ExtractedQuestion) -> dict[str, Any]:
    return {
        "text": question.text,
        "options": question.options,
        "selected_answers": question.selected_answers,
        "correct_answers": question.correct_answers,
        "question_type": question.question_type,
        "confidence": question.confidence,
    }


def item_to_dict(item: CourseItem) -> dict[str, Any]:
    return {
        "title": item.title,
        "order": item.order,
        "kind": str(item.kind),
        "state": str(item.state),
        "pass_condition": item.pass_condition,
        "result": item.result,
        "passed": item.passed,
    }


def row_locator_for_title(page, title: str):
    escaped = xpath_literal(title)
    return page.locator(
        f"xpath=//*[contains(normalize-space(.), {escaped}) and (self::tr or self::li or self::div)]"
    ).first


def xpath_literal(value: str) -> str:
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ', \'"\', '.join(f'"{part}"' for part in parts) + ")"


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


__all__ = [
    "DEFAULT_VALIDATION_JSONL_PATH",
    "DEFAULT_VALIDATION_MARKDOWN_PATH",
    "ActivityHtmlPart",
    "FormValidationRecord",
    "FormValidationResult",
    "classify_form_html",
    "parse_include",
    "parse_scope",
    "render_validation_markdown",
    "validate_playwright_forms",
]
