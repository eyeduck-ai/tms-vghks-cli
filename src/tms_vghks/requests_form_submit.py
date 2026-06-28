from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
import re

from bs4 import BeautifulSoup, Tag

from .models import CourseDetail, CourseItem, ItemKind, ItemState, SiteState
from .parsers import absolute_url, classify_response, normalize_text, result_satisfies_condition
from .privacy import redact_sensitive_url
from .quiz import QuestionBank, QuizQuestion
from .quiz_resolver import GeminiQuizClient, GeminiQuizConfig, question_key, resolve_quiz_answers
from .requests_login import ajax_login_headers, stable_login_headers
from .requests_watch_time import find_check_pass_previous_url
from .session import LoginRequired, TmsError, TmsSession, TransientTmsError
from .survey_text import DEFAULT_NEUTRAL_SURVEY_TEXT


FORM_SUBMIT_STATUSES = {
    "requests_survey_submit_verified",
    "requests_quiz_submit_verified",
    "requests_quiz_submit_course_detail_only",
    "form_endpoint_unverified",
    "form_missing_required_fields",
    "form_submit_failed",
    "form_submit_not_verified",
    "mutation_unsupported",
    "requests_kexam_submit_verified",
    "requests_submit_response_failed_record_verified",
    "requests_submit_failed_record_blank",
    "kexam_entry_unavailable",
    "kexam_take_parse_failed",
    "kexam_missing_required_answers",
    "kexam_confirm_failed",
    "kexam_submit_failed",
    "kexam_submit_not_verified",
}


@dataclass(slots=True)
class FormOption:
    name: str
    value: str
    label: str
    input_type: str
    required: bool = False
    checked: bool = False


@dataclass(slots=True)
class ParsedFormQuestion:
    name: str
    text: str
    options: list[FormOption]
    multiple: bool = False
    required: bool = False


@dataclass(slots=True)
class ParsedActivityForm:
    entry_url: str
    action_url: str | None
    method: str
    hidden_fields: list[tuple[str, str]] = field(default_factory=list)
    questions: list[ParsedFormQuestion] = field(default_factory=list)
    text_fields: list[str] = field(default_factory=list)
    textarea_fields: list[str] = field(default_factory=list)
    contenteditable_fields: int = 0
    submit_fields: list[tuple[str, str]] = field(default_factory=list)
    submit_buttons: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def field_summary(self) -> dict[str, Any]:
        radio_groups = sum(1 for question in self.questions if not question.multiple)
        checkbox_groups = sum(1 for question in self.questions if question.multiple)
        return {
            "action_url": redact_sensitive_url(self.action_url or ""),
            "method": self.method,
            "hidden_fields": sorted({key for key, _ in self.hidden_fields}),
            "radio_groups": radio_groups,
            "checkbox_groups": checkbox_groups,
            "text_fields": len(self.text_fields),
            "textarea_fields": len(self.textarea_fields),
            "contenteditable_fields": self.contenteditable_fields,
            "submit_buttons": self.submit_buttons,
        }


@dataclass(slots=True)
class FormSubmitBuildResult:
    payload: list[tuple[str, str]]
    selected_answer_count: int = 0
    question_count: int = 0
    missing_required: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    answer_sources: dict[str, str] = field(default_factory=dict)
    answer_source_counts: dict[str, int] = field(default_factory=dict)
    answer_resolution_issues: list[str] = field(default_factory=list)
    answer_resolution_notes: list[str] = field(default_factory=list)

    @property
    def payload_keys(self) -> list[str]:
        return sorted({key for key, _ in self.payload})


@dataclass(slots=True)
class RequestsFormSubmitSample:
    observed_at: str
    result: str | None
    state: str
    passed_marker: str | None = None
    pass_condition: str | None = None


@dataclass(slots=True)
class RequestsFormSubmitResult:
    success: bool
    status: str
    course_title: str = ""
    course_url: str = ""
    course_id: str | None = None
    item_title: str = ""
    item_kind: str = ""
    item_order: int | None = None
    entry_url: str = ""
    form_action_url: str = ""
    method: str = ""
    before: RequestsFormSubmitSample | None = None
    after: RequestsFormSubmitSample | None = None
    form_summary: dict[str, Any] = field(default_factory=dict)
    payload_keys: list[str] = field(default_factory=list)
    question_count: int = 0
    selected_answer_count: int = 0
    answer_sources: dict[str, str] = field(default_factory=dict)
    answer_source_counts: dict[str, int] = field(default_factory=dict)
    answer_resolution_issues: list[str] = field(default_factory=list)
    answer_resolution_notes: list[str] = field(default_factory=list)
    response_status_code: int | None = None
    response_json_summary: dict[str, Any] = field(default_factory=dict)
    requests_reproduction_status: str = "requests_blocked"
    entry_attempts: list[dict[str, Any]] = field(default_factory=list)
    verification_strength: str = "none"
    verification_method: str = "none"
    record_id: str = ""
    score: str | None = None
    submit_time: str | None = None
    kexam_before_attempt_count: int | None = None
    kexam_after_attempt_count: int | None = None
    kexam_new_record_ids: list[str] = field(default_factory=list)
    kexam_updated_record_ids: list[str] = field(default_factory=list)
    kexam_verified_record_ids: list[str] = field(default_factory=list)
    kexam_unverified_record_ids: list[str] = field(default_factory=list)
    kexam_latest_submitted_attempt: dict[str, Any] | None = None
    kexam_record_probe_summary: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def parse_activity_form_html(html: str, entry_url: str, base_url: str, item_kind: ItemKind | str) -> ParsedActivityForm:
    soup = BeautifulSoup(html or "", "html.parser")
    form = soup.find("form")
    if not isinstance(form, Tag):
        return ParsedActivityForm(
            entry_url=entry_url,
            action_url=None,
            method="",
            issues=["form_missing"],
        )

    action = form.get("action")
    action_url = absolute_url(action, base_url) if action else None
    method = normalize_text(form.get("method") or "").upper()
    hidden_fields: list[tuple[str, str]] = []
    text_fields: list[str] = []
    textarea_fields: list[str] = []
    submit_fields: list[tuple[str, str]] = []
    submit_buttons: list[str] = []

    for node in form.find_all("input"):
        name = normalize_text(node.get("name"))
        input_type = normalize_text(node.get("type") or "text").lower()
        value = node.get("value") or ""
        if input_type == "hidden" and name:
            hidden_fields.append((name, value))
        elif input_type in {"submit", "button"}:
            label = _control_label(node)
            if label:
                submit_buttons.append(label)
            if input_type == "submit" and name:
                submit_fields.append((name, value or label))
        elif input_type not in {"radio", "checkbox", "reset", "image", "file"} and name:
            text_fields.append(name)

    for node in form.find_all("textarea"):
        name = normalize_text(node.get("name"))
        if name:
            textarea_fields.append(name)

    for node in form.find_all("button"):
        button_type = normalize_text(node.get("type") or "submit").lower()
        label = _control_label(node)
        if button_type == "submit":
            if label:
                submit_buttons.append(label)
            name = normalize_text(node.get("name"))
            if name:
                submit_fields.append((name, node.get("value") or label))

    questions = _extract_grouped_questions(form, ItemKind(item_kind))
    issues: list[str] = []
    if not action_url:
        issues.append("form_action_missing")
    if method != "POST":
        issues.append("form_post_method_missing")
    if form.select('[contenteditable="true"]'):
        issues.append("contenteditable_unsupported")
    return ParsedActivityForm(
        entry_url=entry_url,
        action_url=action_url,
        method=method,
        hidden_fields=hidden_fields,
        questions=questions,
        text_fields=text_fields,
        textarea_fields=textarea_fields,
        contenteditable_fields=len(form.select('[contenteditable="true"]')),
        submit_fields=submit_fields[:1],
        submit_buttons=submit_buttons,
        issues=issues,
    )


def build_survey_payload(form: ParsedActivityForm, neutral_text: str) -> FormSubmitBuildResult:
    payload = list(form.hidden_fields)
    selected_count = 0
    issues: list[str] = []
    for question in form.questions:
        if not question.options:
            continue
        checked = [option for option in question.options if option.checked]
        if question.multiple and checked:
            selected = checked
        elif question.multiple and question.required:
            selected = [question.options[len(question.options) // 2]]
        elif question.multiple:
            selected = []
        else:
            selected = [checked[0] if checked else question.options[len(question.options) // 2]]
        for option in selected:
            payload.append((option.name, option.value))
            selected_count += 1
    for name in form.text_fields:
        payload.append((name, neutral_text))
    for name in form.textarea_fields:
        payload.append((name, neutral_text))
    payload.extend(form.submit_fields)
    if not form.questions and not form.text_fields and not form.textarea_fields:
        issues.append("form_fillable_fields_missing")
    return FormSubmitBuildResult(
        payload=payload,
        selected_answer_count=selected_count,
        question_count=len(form.questions),
        issues=issues,
    )


def build_quiz_payload(
    form: ParsedActivityForm,
    course_title: str,
    item_title: str,
    question_bank: QuestionBank | None,
    quiz_policy: str,
    gemini_config: GeminiQuizConfig | None = None,
    gemini_client: GeminiQuizClient | None = None,
) -> FormSubmitBuildResult:
    payload = list(form.hidden_fields)
    missing: list[str] = []
    issues: list[str] = []
    selected_count = 0
    quiz_questions = [
        QuizQuestion(
            text=question.text,
            options=[option.label for option in question.options],
            name=question.name,
            multiple=question.multiple,
        )
        for question in form.questions
    ]
    resolution = resolve_quiz_answers(
        questions=quiz_questions,
        course_title=course_title,
        item_title=item_title,
        question_bank=question_bank,
        quiz_policy=quiz_policy,
        gemini_config=gemini_config,
        gemini_client=gemini_client,
    )
    for question, quiz_question in zip(form.questions, quiz_questions, strict=False):
        answers = resolution.answers.get(question_key(quiz_question), [])
        if not answers:
            missing.append(question.name or question.text)
            continue
        matched = _match_answer_options(question.options, answers)
        if not matched:
            missing.append(question.name or question.text)
            continue
        if not question.multiple:
            matched = matched[:1]
        for option in matched:
            payload.append((option.name, option.value))
            selected_count += 1

    unsupported_text = sorted(set(form.text_fields + form.textarea_fields))
    if unsupported_text:
        missing.extend(unsupported_text)
        issues.append("quiz_text_fields_unsupported")
    payload.extend(form.submit_fields)
    return FormSubmitBuildResult(
        payload=payload,
        selected_answer_count=selected_count,
        question_count=len(form.questions),
        missing_required=missing,
        issues=issues,
        answer_sources=resolution.sources,
        answer_source_counts=resolution.source_counts,
        answer_resolution_issues=resolution.issues,
        answer_resolution_notes=resolution.notes,
    )


def probe_form_submit_requests(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
) -> RequestsFormSubmitResult:
    return _run_form_submit_requests(
        session=session,
        course=course,
        item=item,
        neutral_text="",
        question_bank=None,
        quiz_policy="confirm",
        gemini_config=None,
        submit=False,
    )


def run_survey_requests_submit(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
) -> RequestsFormSubmitResult:
    return _run_form_submit_requests(
        session=session,
        course=course,
        item=item,
        neutral_text=DEFAULT_NEUTRAL_SURVEY_TEXT,
        question_bank=None,
        quiz_policy="confirm",
        gemini_config=None,
        submit=True,
    )


def run_quiz_requests_submit(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    question_bank: QuestionBank | None,
    quiz_policy: str,
    gemini_config: GeminiQuizConfig | None = None,
) -> RequestsFormSubmitResult:
    return _run_form_submit_requests(
        session=session,
        course=course,
        item=item,
        neutral_text="",
        question_bank=question_bank,
        quiz_policy=quiz_policy,
        gemini_config=gemini_config,
        submit=True,
    )


def resolve_activity_entry_url_requests(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
) -> tuple[str, list[str]]:
    if item.detail_url:
        return item.detail_url, []

    return _resolve_activity_entry_url_from_course_tree_requests(session, course, item)


def _resolve_activity_entry_url_from_course_tree_requests(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
) -> tuple[str, list[str]]:
    course_html = session.fetch_activity_html_requests(course.url)
    check_url = find_check_pass_previous_url(course_html, item, session.base_url)
    if not check_url:
        return "", ["activity_entry_url_missing", "check_pass_previous_url_missing"]
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
    entry_url = _extract_entry_url_from_json(payload, session.base_url)
    if entry_url:
        return entry_url, []
    return "", ["check_pass_previous_entry_url_missing"]


def _run_form_submit_requests(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    neutral_text: str,
    question_bank: QuestionBank | None,
    quiz_policy: str,
    gemini_config: GeminiQuizConfig | None,
    submit: bool,
) -> RequestsFormSubmitResult:
    kind = ItemKind(item.kind)
    if kind not in {ItemKind.SURVEY, ItemKind.QUIZ}:
        raise ValueError(f"requests form submit requires survey or quiz item, got {item.kind}")
    before_course, before_item = _refresh_matching_item(session, course, item)
    result = RequestsFormSubmitResult(
        success=False,
        status="not_started",
        course_title=before_course.title,
        course_url=before_course.url,
        course_id=before_course.course_id,
        item_title=before_item.title,
        item_kind=str(before_item.kind),
        item_order=before_item.order,
        before=_sample_from_item(before_item),
        after=_sample_from_item(before_item),
    )
    if submit and kind == ItemKind.QUIZ:
        kexam_result = _try_run_kexam_requests_form_submit(
            session,
            before_course,
            before_item,
            question_bank,
            quiz_policy,
            gemini_config,
        )
        if kexam_result is not None:
            return _requests_form_result_from_kexam(kexam_result, result)
    if not submit and kind == ItemKind.QUIZ:
        kexam_probe = _try_read_kexam_requests_form_probe(session, before_item)
        if kexam_probe is not None:
            return _requests_form_probe_result_from_kexam(kexam_probe, result)
        kexam_entry_url, kexam_entry_issues = resolve_activity_entry_url_requests(session, before_course, before_item)
        kexam_probe = _try_read_kexam_requests_form_probe_url(session, kexam_entry_url)
        if kexam_probe is not None:
            result.entry_attempts = [
                {
                    "source": "course_tree" if not before_item.detail_url else "direct",
                    "entry_url": redact_sensitive_url(kexam_entry_url),
                    "issues": kexam_entry_issues,
                }
            ]
            return _requests_form_probe_result_from_kexam(kexam_probe, result)

    form, entry_url, entry_issues, entry_attempts = _resolve_submittable_activity_form(
        session,
        before_course,
        before_item,
        kind,
    )
    result.entry_attempts = entry_attempts
    result.issues.extend(entry_issues)
    if not entry_url:
        result.status = "form_endpoint_unverified"
        return result
    result.entry_url = redact_sensitive_url(entry_url)
    result.form_summary = form.field_summary
    result.form_action_url = redact_sensitive_url(form.action_url or "")
    result.method = form.method
    result.issues.extend(form.issues)
    if not submit:
        result.status = "mutation_unsupported"
        result.requests_reproduction_status = "requests_partial" if form.action_url else "requests_blocked"
        result.issues.append("probe_only")
        return result
    if form.issues:
        result.status = "form_endpoint_unverified"
        return result

    if kind == ItemKind.SURVEY:
        built = build_survey_payload(form, neutral_text)
        verified_status = "requests_survey_submit_verified"
    else:
        built = build_quiz_payload(
            form,
            before_course.title,
            before_item.title,
            question_bank,
            quiz_policy,
            gemini_config=gemini_config,
        )
        verified_status = "requests_quiz_submit_course_detail_only"

    result.payload_keys = built.payload_keys
    result.question_count = built.question_count
    result.selected_answer_count = built.selected_answer_count
    result.answer_sources = dict(built.answer_sources)
    result.answer_source_counts = dict(built.answer_source_counts)
    result.answer_resolution_issues = list(built.answer_resolution_issues)
    result.answer_resolution_notes = list(built.answer_resolution_notes)
    result.issues.extend(built.issues)
    if built.missing_required:
        result.status = "form_missing_required_fields"
        result.issues.append("missing_required:" + ",".join(built.missing_required[:20]))
        return result
    if built.issues:
        result.status = "form_missing_required_fields" if kind == ItemKind.QUIZ else "form_endpoint_unverified"
        return result
    result.issues.extend(f"answer_resolution:{issue}" for issue in built.answer_resolution_issues)

    response = session._request_with_transient_retries(
        form.method,
        form.action_url or entry_url,
        data=built.payload,
        allow_redirects=False,
        headers=_form_submit_headers(entry_url),
    )
    result.response_status_code = response.status_code
    result.response_json_summary = _response_json_summary(response)
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
        result.status = "form_submit_failed"
        result.issues.append(f"form_submit_http_{response.status_code}")
        return result

    after_course, after_item = _refresh_matching_item(session, before_course, before_item)
    result.after = _sample_from_item(after_item)
    result.course_title = after_course.title
    result.course_url = after_course.url
    result.course_id = after_course.course_id
    if _item_passed(after_item):
        result.success = True
        result.status = verified_status
        result.requests_reproduction_status = (
            "requests_course_detail_verified" if kind == ItemKind.QUIZ else "requests_reproducible"
        )
        result.verification_strength = "course_detail"
        result.verification_method = "course_detail_item_passed"
        return result
    result.status = "form_submit_not_verified"
    result.requests_reproduction_status = "requests_partial"
    return result


def _try_run_kexam_requests_form_submit(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    question_bank: QuestionBank | None,
    quiz_policy: str,
    gemini_config: GeminiQuizConfig | None,
):
    from .requests_kexam import try_run_kexam_requests_submit

    return try_run_kexam_requests_submit(
        session=session,
        course=course,
        item=item,
        question_bank=question_bank,
        quiz_policy=quiz_policy,
        gemini_config=gemini_config,
    )


def _try_read_kexam_requests_form_probe(session: TmsSession, item: CourseItem):
    from .requests_kexam import try_read_kexam_requests_probe

    return try_read_kexam_requests_probe(
        session=session,
        item=item,
        include_unsubmitted_records=True,
    )


def _try_read_kexam_requests_form_probe_url(session: TmsSession, entry_url: str):
    from .requests_kexam import try_read_kexam_requests_probe_url

    return try_read_kexam_requests_probe_url(
        session=session,
        exam_url=entry_url,
        include_unsubmitted_records=True,
    )


def _requests_form_result_from_kexam(kexam_result: Any, result: RequestsFormSubmitResult) -> RequestsFormSubmitResult:
    endpoint_summary = kexam_result.submit_result.get("endpoint_summary", {})
    result.success = bool(kexam_result.success)
    result.status = str(kexam_result.status) if kexam_result.success else _map_kexam_failure_status(kexam_result.status)
    result.entry_url = kexam_result.redacted_exam_url
    result.form_action_url = endpoint_summary.get("submit_exam_url", "")
    result.method = "POST"
    result.form_summary = {
        "kexam": True,
        "confirm_record_url": endpoint_summary.get("confirm_record_url", ""),
        "submit_exam_url": endpoint_summary.get("submit_exam_url", ""),
        "submitted_redir_url": endpoint_summary.get("submitted_redir_url", ""),
        "record_id": endpoint_summary.get("record_id", ""),
    }
    result.payload_keys = list(kexam_result.submit_result.get("payload_keys") or [])
    result.question_count = kexam_result.question_count
    result.selected_answer_count = kexam_result.selected_answer_count
    result.answer_sources = dict(kexam_result.submit_result.get("answer_sources") or {})
    result.answer_source_counts = dict(kexam_result.submit_result.get("answer_source_counts") or {})
    result.answer_resolution_issues = list(kexam_result.submit_result.get("answer_resolution_issues") or [])
    result.answer_resolution_notes = list(kexam_result.submit_result.get("answer_resolution_notes") or [])
    result.response_status_code = kexam_result.submit_result.get("response_status_code")
    result.response_json_summary = kexam_result.submit_result.get("response_json_summary") or {}
    result.requests_reproduction_status = "requests_reproducible" if kexam_result.success else "requests_partial"
    result.verification_strength = getattr(kexam_result, "verification_strength", "none")
    result.verification_method = getattr(kexam_result, "verification_method", "none")
    result.record_id = getattr(kexam_result, "verification_record_id", "")
    result.score = getattr(kexam_result, "verification_score", None)
    result.submit_time = getattr(kexam_result, "verification_submit_time", None)
    result.kexam_before_attempt_count = getattr(kexam_result, "before_attempt_count", None)
    result.kexam_after_attempt_count = getattr(kexam_result, "after_attempt_count", None)
    result.kexam_new_record_ids = list(getattr(kexam_result, "new_record_ids", []))
    result.kexam_updated_record_ids = list(getattr(kexam_result, "updated_record_ids", []))
    result.kexam_verified_record_ids = list(getattr(kexam_result, "verified_record_ids", []))
    result.kexam_unverified_record_ids = list(getattr(kexam_result, "unverified_record_ids", []))
    result.kexam_latest_submitted_attempt = getattr(kexam_result, "latest_submitted_attempt", None)
    result.kexam_record_probe_summary = {
        "best_record_id_before": getattr(kexam_result, "best_record_id_before", ""),
        "best_record_id_after": getattr(kexam_result, "best_record_id_after", ""),
        "best_record_changed": getattr(kexam_result, "best_record_changed", False),
        "before_record_count": kexam_result.before.record_count if getattr(kexam_result, "before", None) else 0,
        "after_record_count": kexam_result.after.record_count if getattr(kexam_result, "after", None) else 0,
        "after_record_question_count": kexam_result.after.record_question_count if getattr(kexam_result, "after", None) else 0,
        "after_record_selected_answer_count": kexam_result.after.record_selected_answer_count if getattr(kexam_result, "after", None) else 0,
        "verified_record_ids": result.kexam_verified_record_ids,
        "unverified_record_ids": result.kexam_unverified_record_ids,
        "verification_strength": result.verification_strength,
        "verification_method": result.verification_method,
        "record_id": result.record_id,
        "score": result.score,
        "submit_time": result.submit_time,
        "verified_item_state": getattr(kexam_result, "verified_item_state", ""),
        "verified_item_result": getattr(kexam_result, "verified_item_result", None),
    }
    result.issues.extend(kexam_result.issues)
    return result


def _requests_form_probe_result_from_kexam(kexam_probe: Any, result: RequestsFormSubmitResult) -> RequestsFormSubmitResult:
    result.success = False
    result.status = "mutation_unsupported"
    result.entry_url = kexam_probe.redacted_exam_url
    result.form_action_url = ""
    result.method = "GET"
    result.form_summary = {
        "kexam": True,
        "record_modal_url": kexam_probe.record_modal_url,
        "best_record_url": kexam_probe.best_record_url,
        "take_operate_url": kexam_probe.take_operate_url,
        "take_url": kexam_probe.take_url,
        "attempt_count": kexam_probe.attempt_count,
        "record_count": kexam_probe.record_count,
        "continue_available": kexam_probe.continue_available,
    }
    result.question_count = kexam_probe.record_question_count
    result.selected_answer_count = kexam_probe.record_selected_answer_count
    result.requests_reproduction_status = "requests_partial"
    result.kexam_before_attempt_count = kexam_probe.attempt_count
    result.kexam_record_probe_summary = {
        "record_count": kexam_probe.record_count,
        "record_question_count": kexam_probe.record_question_count,
        "record_selected_answer_count": kexam_probe.record_selected_answer_count,
        "best_record_id": kexam_probe.best_record_id,
        "attempt_limit_text": kexam_probe.attempt_limit_text,
    }
    result.issues.extend([*kexam_probe.issues, "kexam_read_only_probe", "probe_only"])
    return result


def _map_kexam_failure_status(status: str) -> str:
    if status == "requests_submit_failed_record_blank":
        return status
    if status in {
        "kexam_entry_unavailable",
        "kexam_take_parse_failed",
        "kexam_missing_required_answers",
        "kexam_confirm_failed",
        "kexam_submit_failed",
        "kexam_submit_not_verified",
    }:
        return status
    if status == "submit_not_verified_by_record":
        return "kexam_submit_not_verified"
    return "kexam_submit_failed"


def _resolve_submittable_activity_form(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    kind: ItemKind,
) -> tuple[ParsedActivityForm | None, str, list[str], list[dict[str, Any]]]:
    attempts: list[tuple[str, list[str], str]] = []
    attempt_summaries: list[dict[str, Any]] = []
    primary_url, primary_issues = resolve_activity_entry_url_requests(session, course, item)
    if primary_url:
        attempts.append((primary_url, primary_issues, "direct"))

    if item.detail_url:
        fallback_url, fallback_issues = _resolve_activity_entry_url_from_course_tree_requests(session, course, item)
        if fallback_url and fallback_url not in {url for url, _, _ in attempts}:
            attempts.append((fallback_url, [*fallback_issues, "course_tree_entry_fallback"], "course_tree"))
        elif fallback_url:
            attempt_summaries.append(
                {
                    "source": "course_tree",
                    "entry_url": redact_sensitive_url(fallback_url),
                    "issues": [*fallback_issues, "course_tree_entry_duplicate"],
                }
            )
        elif fallback_issues:
            attempt_summaries.append(
                {
                    "source": "course_tree",
                    "entry_url": "",
                    "issues": fallback_issues,
                }
            )
        elif not primary_url:
            primary_issues.extend(fallback_issues)

    if not attempts:
        return None, "", primary_issues, attempt_summaries

    first_failure: tuple[ParsedActivityForm, str, list[str], list[dict[str, Any]]] | None = None
    for entry_url, entry_issues, source in attempts:
        try:
            html = session.fetch_activity_html_requests(entry_url, referer=course.url)
        except (LoginRequired, TransientTmsError):
            raise
        except Exception as exc:
            issues = [*entry_issues, f"activity_form_unavailable:{exc}"]
            attempt_summaries.append(
                {
                    "source": source,
                    "entry_url": redact_sensitive_url(entry_url),
                    "issues": issues,
                }
            )
            if first_failure is None:
                first_failure = (
                    ParsedActivityForm(entry_url=entry_url, action_url=None, method="", issues=issues),
                    entry_url,
                    [],
                    list(attempt_summaries),
                )
            continue

        form = parse_activity_form_html(html, entry_url, session.base_url, kind)
        attempt_summaries.append(
            {
                "source": source,
                "entry_url": redact_sensitive_url(entry_url),
                "issues": list(entry_issues + form.issues),
            }
        )
        if not form.issues:
            return form, entry_url, entry_issues, attempt_summaries
        if first_failure is None:
            first_failure = (form, entry_url, entry_issues, list(attempt_summaries))

    if first_failure is not None:
        form, entry_url, entry_issues, _ = first_failure
        return form, entry_url, entry_issues, attempt_summaries
    return None, "", primary_issues, attempt_summaries


def _extract_grouped_questions(form: Tag, item_kind: ItemKind) -> list[ParsedFormQuestion]:
    groups: dict[str, list[FormOption]] = {}
    for node in form.find_all("input"):
        input_type = normalize_text(node.get("type") or "text").lower()
        if input_type not in {"radio", "checkbox"}:
            continue
        name = normalize_text(node.get("name"))
        if not name:
            continue
        label = _option_label(node)
        option = FormOption(
            name=name,
            value=node.get("value") or label,
            label=label,
            input_type=input_type,
            required=node.has_attr("required"),
            checked=node.has_attr("checked"),
        )
        groups.setdefault(name, []).append(option)

    questions: list[ParsedFormQuestion] = []
    for name, options in groups.items():
        first = _input_by_name(form, name)
        question_text = _question_text(first, options) if first is not None else name
        multiple = any(option.input_type == "checkbox" for option in options)
        required = any(option.required for option in options) or item_kind == ItemKind.QUIZ
        questions.append(
            ParsedFormQuestion(
                name=name,
                text=question_text or name,
                options=options,
                multiple=multiple,
                required=required,
            )
        )
    return questions


def _input_by_name(form: Tag, name: str) -> Tag | None:
    node = form.find("input", attrs={"name": name})
    return node if isinstance(node, Tag) else None


def _option_label(node: Tag) -> str:
    label = node.find_parent("label")
    if isinstance(label, Tag):
        return _clean_label_text(label.get_text(" ", strip=True), node)
    node_id = node.get("id")
    if node_id:
        soup = node.find_parent("form")
        if soup:
            explicit = soup.find("label", attrs={"for": node_id})
            if isinstance(explicit, Tag):
                return normalize_text(explicit.get_text(" ", strip=True))
    value = node.get("value")
    return normalize_text(str(value or ""))


def _clean_label_text(text: str, node: Tag) -> str:
    value = normalize_text(node.get("value"))
    cleaned = normalize_text(text)
    return cleaned or value


def _question_text(node: Tag, options: list[FormOption]) -> str:
    parent = node.find_parent(["tr", "li"])
    if parent is None:
        parent = node.find_parent(class_=re.compile(r"(question|form-group|control-group|field|item)", re.I))
    if parent is None:
        parent = node.find_parent("div")
    text = normalize_text(parent.get_text(" ", strip=True) if parent else "")
    for option in options:
        if option.label:
            text = normalize_text(text.replace(option.label, " "))
        if option.value:
            text = normalize_text(text.replace(option.value, " "))
    return text


def _control_label(node: Tag) -> str:
    if node.name == "input":
        return normalize_text(node.get("value") or node.get("title") or "")
    return normalize_text(node.get_text(" ", strip=True) or node.get("title") or "")


def _match_answer_options(options: list[FormOption], answers: list[str]) -> list[FormOption]:
    matched: list[FormOption] = []
    for answer in answers:
        answer_text = normalize_text(answer)
        for option in options:
            if option in matched:
                continue
            label = normalize_text(option.label)
            value = normalize_text(option.value)
            if answer_text == label or answer_text == value or answer_text in label or label in answer_text:
                matched.append(option)
                break
    return matched


def _extract_entry_url_from_json(payload: Any, base_url: str) -> str:
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
        if isinstance(candidate, str) and candidate.strip():
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


def _sample_from_item(item: CourseItem) -> RequestsFormSubmitSample:
    return RequestsFormSubmitSample(
        observed_at=datetime.now(timezone.utc).isoformat(),
        result=item.result,
        state=str(item.state),
        passed_marker=item.passed_marker,
        pass_condition=item.pass_condition,
    )


def _item_passed(item: CourseItem) -> bool:
    return item.state == ItemState.PASSED or result_satisfies_condition(
        item.pass_condition,
        item.result,
        item.passed_marker,
    )


def _form_submit_headers(referer: str) -> dict[str, str]:
    return stable_login_headers(
        {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        }
    )


def _safe_json(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _response_json_summary(response) -> dict[str, Any]:
    payload = _safe_json(response)
    if isinstance(payload, dict):
        summary: dict[str, Any] = {"json": True, "keys": sorted(str(key) for key in payload)[:50]}
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


__all__ = [
    "FORM_SUBMIT_STATUSES",
    "FormOption",
    "FormSubmitBuildResult",
    "ParsedActivityForm",
    "ParsedFormQuestion",
    "RequestsFormSubmitResult",
    "RequestsFormSubmitSample",
    "build_quiz_payload",
    "build_survey_payload",
    "parse_activity_form_html",
    "probe_form_submit_requests",
    "resolve_activity_entry_url_requests",
    "run_quiz_requests_submit",
    "run_survey_requests_submit",
]
