from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .kexam_common import (
    DEFAULT_KEXAM_COURSE,
    DEFAULT_KEXAM_EXAM_URL,
    KExamExamPageReadResult,
    KExamResubmitDiagnosticResult,
    attempt_dict_record_id,
    best_record_attempt,
    build_kexam_resubmit_verification,
    parse_kexam_exam_page_html,
    probe_to_dict,
)
from .models import CourseDetail, CourseItem, ItemKind, ItemState, SiteState
from .parsers import absolute_url, classify_response, normalize_text, result_satisfies_condition
from .playwright_probe import extract_kexam_attempts_from_modal_html, kexam_attempt_to_dict
from .privacy import redact_sensitive_url
from .quiz import QuestionBank, QuizQuestion, find_latest_question_bank_path
from .quiz_resolver import GeminiQuizClient, GeminiQuizConfig, question_key, resolve_quiz_answers
from .requests_login import ajax_login_headers, stable_login_headers
from .requests_probe import probe_kexam_attempt_requests
from .session import LoginRequired, TmsSession, TransientTmsError


@dataclass(slots=True)
class KExamTakeQuestion:
    kques_id: str
    text: str
    options: list[str]
    display_options: list[str]
    answer: Any
    score: float
    question_type: int
    opt_order: Any = None


@dataclass(slots=True)
class KExamTakePageParse:
    take_url: str
    record_id: str = ""
    record_time_used: int = 0
    confirm_record_url: str = ""
    submit_exam_url: str = ""
    submitted_redir_url: str = ""
    questions: list[KExamTakeQuestion] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def endpoint_summary(self) -> dict[str, Any]:
        return {
            "confirm_record_url": redact_sensitive_url(self.confirm_record_url),
            "submit_exam_url": redact_sensitive_url(self.submit_exam_url),
            "submitted_redir_url": redact_sensitive_url(self.submitted_redir_url),
            "record_id": self.record_id,
            "question_count": len(self.questions),
        }


@dataclass(slots=True)
class KExamSubmitBuildResult:
    payload: dict[str, str]
    question_count: int
    selected_answer_count: int = 0
    missing_required: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    answer_sources: dict[str, str] = field(default_factory=dict)
    answer_source_counts: dict[str, int] = field(default_factory=dict)
    answer_resolution_issues: list[str] = field(default_factory=list)
    answer_resolution_notes: list[str] = field(default_factory=list)

    @property
    def payload_keys(self) -> list[str]:
        return sorted(self.payload)


@dataclass(slots=True)
class KExamRequestsSubmitResult:
    success: bool
    status: str
    course_id: str
    exam_url: str
    redacted_exam_url: str
    submit_result: dict[str, Any]
    question_count: int = 0
    selected_answer_count: int = 0
    verified_item_state: str = ""
    verified_item_result: str | None = None
    before: KExamExamPageReadResult | None = None
    after: KExamExamPageReadResult | None = None
    before_attempt_count: int = 0
    after_attempt_count: int = 0
    new_record_ids: list[str] = field(default_factory=list)
    updated_record_ids: list[str] = field(default_factory=list)
    verified_record_ids: list[str] = field(default_factory=list)
    unverified_record_ids: list[str] = field(default_factory=list)
    latest_submitted_attempt: dict[str, Any] | None = None
    best_record_id_before: str | None = None
    best_record_id_after: str | None = None
    best_record_changed: bool = False
    verification_strength: str = "none"
    verification_method: str = "none"
    verification_record_id: str = ""
    verification_score: str | None = None
    verification_submit_time: str | None = None
    issues: list[str] = field(default_factory=list)


def read_kexam_exam_page_requests(
    session: TmsSession,
    exam_url: str = DEFAULT_KEXAM_EXAM_URL,
    include_unsubmitted_records: bool = True,
) -> KExamExamPageReadResult:
    html = session.fetch_activity_html_requests(exam_url)
    parsed = parse_kexam_exam_page_html(html, exam_url, session.base_url)
    issues: list[str] = []
    modal_html = ""
    if parsed.record_modal_url:
        try:
            modal_html = session.fetch_activity_html_requests(parsed.record_modal_url, referer=exam_url)
        except Exception as exc:
            issues.append(f"attempt_record_modal_unavailable:{exc}")
    attempts = extract_kexam_attempts_from_modal_html(modal_html, session.base_url) if modal_html else []
    if not attempts:
        attempts = extract_kexam_attempts_from_modal_html(html, session.base_url)
    if not attempts and parsed.attempt_count:
        issues.append("attempt_record_modal_unavailable")

    best_attempt = best_record_attempt(parsed, attempts, session.base_url)
    record_probes = [
        probe_to_dict(
            probe_kexam_attempt_requests(
                session,
                attempt,
                ItemKind.QUIZ,
                include_unsubmitted_records=include_unsubmitted_records,
            )
        )
        for attempt in attempts
    ]
    best_record_probe = None
    if best_attempt:
        best_record_probe = probe_to_dict(
            probe_kexam_attempt_requests(
                session,
                best_attempt,
                ItemKind.QUIZ,
                include_unsubmitted_records=include_unsubmitted_records,
            )
        )
        if not any(attempt_dict_record_id(row.get("attempt", {})) == best_attempt.record_id for row in record_probes):
            record_probes.append(best_record_probe)
    elif parsed.best_record_url:
        issues.append("best_record_probe_unavailable")

    record_question_count = sum(int(row.get("question_count") or 0) for row in record_probes)
    record_selected_answer_count = sum(int(row.get("selected_answer_count") or 0) for row in record_probes)
    return KExamExamPageReadResult(
        success=True,
        status="records_read",
        exam_url=exam_url,
        redacted_exam_url=redact_sensitive_url(exam_url),
        attempt_limit_text=parsed.attempt_limit_text,
        attempt_count=parsed.attempt_count,
        continue_available=parsed.continue_available,
        best_record_url=parsed.redacted_best_record_url,
        best_record_id=parsed.best_record_id,
        record_modal_url=parsed.redacted_record_modal_url,
        take_operate_url=parsed.redacted_take_operate_url,
        take_url=parsed.redacted_take_url,
        attempts=[kexam_attempt_to_dict(attempt) for attempt in attempts],
        record_probes=record_probes,
        best_record_probe=best_record_probe,
        record_count=len(attempts),
        record_question_count=record_question_count,
        record_selected_answer_count=record_selected_answer_count,
        issues=issues,
    )


def run_requests_kexam_resubmit_diagnostic(
    session: TmsSession,
    course: str = DEFAULT_KEXAM_COURSE,
    exam_url: str = DEFAULT_KEXAM_EXAM_URL,
    quiz_policy: str = "auto",
    question_bank_path: str | None = None,
    question_bank: QuestionBank | None = None,
    gemini_config: GeminiQuizConfig | None = None,
    probe_only: bool = False,
) -> KExamResubmitDiagnosticResult:
    before = read_kexam_exam_page_requests(session, exam_url=exam_url, include_unsubmitted_records=True)
    bank = question_bank if question_bank is not None else _load_question_bank(question_bank_path)
    course_detail = _load_course_detail(session, course, exam_url)
    item_title = _infer_quiz_title_from_course(course_detail) or "KExam"
    submit_result = _submit_kexam_requests(
        session=session,
        course_title=course_detail.title,
        item_title=item_title,
        exam_url=exam_url,
        quiz_policy=quiz_policy,
        question_bank=bank,
        gemini_config=gemini_config,
        probe_only=probe_only,
    )
    if probe_only:
        success = bool(submit_result.get("success"))
        status = str(submit_result.get("status") or ("kexam_submit_preflight_ready" if success else "kexam_submit_preflight_failed"))
        issues = list(submit_result.get("issues", []))
        if not success and status not in issues:
            issues.append(status)
        return KExamResubmitDiagnosticResult(
            success=success,
            status=status,
            course_id=str(course),
            exam_url=exam_url,
            redacted_exam_url=redact_sensitive_url(exam_url),
            before=before,
            before_attempt_count=int(before.attempt_count or before.record_count or 0),
            question_count=int(submit_result.get("question_count") or 0),
            selected_answer_count=int(submit_result.get("selected_answer_count") or 0),
            submit_result=submit_result,
            issues=issues,
        )
    after = read_kexam_exam_page_requests(session, exam_url=exam_url, include_unsubmitted_records=True)
    verification = build_kexam_resubmit_verification(
        before,
        after,
        expected_question_count=int(submit_result.get("question_count") or 0),
        expected_selected_answer_count=int(submit_result.get("selected_answer_count") or 0),
    )
    submit_success = submit_result.get("success", False)
    submit_failed_after_post = submit_result.get("status") == "kexam_submit_failed"
    success = (submit_success or submit_failed_after_post) and verification["status"] == "resubmit_verified"
    if success and submit_failed_after_post:
        status = "requests_submit_response_failed_record_verified"
    elif success:
        status = "resubmit_verified"
    elif submit_failed_after_post and verification["status"] == "kexam_submit_record_created_without_answers":
        status = "requests_submit_failed_record_blank"
    elif submit_failed_after_post and verification["status"] != "submit_not_verified_by_record":
        status = verification["status"]
    else:
        status = submit_result.get("status") or verification["status"]
    if submit_success and not success:
        status = "kexam_submit_not_verified"
    issues = [*list(submit_result.get("issues", [])), *verification.get("verification_issues", [])]
    if success and submit_failed_after_post:
        issues.append("requests_submit_response_failed_record_verified")
    if not success and status not in issues:
        issues.append(status)
    return KExamResubmitDiagnosticResult(
        success=success,
        status=status,
        course_id=str(course),
        exam_url=exam_url,
        redacted_exam_url=redact_sensitive_url(exam_url),
        before=before,
        after=after,
        before_attempt_count=verification["before_attempt_count"],
        after_attempt_count=verification["after_attempt_count"],
        new_record_ids=verification["new_record_ids"],
        updated_record_ids=verification["updated_record_ids"],
        latest_submitted_attempt=verification["latest_submitted_attempt"],
        best_record_id_before=before.best_record_id,
        best_record_id_after=after.best_record_id,
        best_record_changed=before.best_record_id != after.best_record_id,
        question_count=int(submit_result.get("question_count") or 0),
        selected_answer_count=int(submit_result.get("selected_answer_count") or 0),
        submit_result=submit_result,
        issues=issues,
    )


def try_run_kexam_requests_submit(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    question_bank: QuestionBank | None,
    quiz_policy: str,
    gemini_config: GeminiQuizConfig | None = None,
) -> KExamRequestsSubmitResult | None:
    exam_url = item.detail_url or ""
    if not _looks_like_kexam_exam_url(exam_url):
        return None
    course_id = course.course_id or _course_id_from_exam_url(exam_url) or course.url.rstrip("/").split("/")[-1]
    before = read_kexam_exam_page_requests(session, exam_url=exam_url, include_unsubmitted_records=True)
    submit_result = _submit_kexam_requests(
        session=session,
        course_title=course.title,
        item_title=item.title,
        exam_url=exam_url,
        quiz_policy=quiz_policy,
        question_bank=question_bank,
        gemini_config=gemini_config,
    )
    question_count = int(submit_result.get("question_count") or 0)
    selected_answer_count = int(submit_result.get("selected_answer_count") or 0)
    if not submit_result.get("success", False):
        submit_failed_after_post = submit_result.get("status") == "kexam_submit_failed"
        if not submit_failed_after_post:
            return KExamRequestsSubmitResult(
                success=False,
                status=str(submit_result.get("status") or "kexam_submit_failed"),
                course_id=str(course_id),
                exam_url=exam_url,
                redacted_exam_url=redact_sensitive_url(exam_url),
                submit_result=submit_result,
                question_count=question_count,
                selected_answer_count=selected_answer_count,
                before=before,
                before_attempt_count=int(before.attempt_count or before.record_count or 0),
                best_record_id_before=before.best_record_id,
                issues=list(submit_result.get("issues") or []),
            )

    after = read_kexam_exam_page_requests(session, exam_url=exam_url, include_unsubmitted_records=True)
    verification = build_kexam_resubmit_verification(
        before,
        after,
        expected_question_count=question_count,
        expected_selected_answer_count=selected_answer_count,
    )
    submit_success = bool(submit_result.get("success", False))
    submit_failed_after_post = submit_result.get("status") == "kexam_submit_failed"
    record_verified = verification["status"] == "resubmit_verified"
    success = (submit_success or submit_failed_after_post) and record_verified
    if success and submit_failed_after_post:
        status = "requests_submit_response_failed_record_verified"
    elif success:
        status = "requests_kexam_submit_verified"
    elif submit_failed_after_post and verification["status"] == "kexam_submit_record_created_without_answers":
        status = "requests_submit_failed_record_blank"
    elif submit_failed_after_post and verification["status"] != "submit_not_verified_by_record":
        status = verification["status"]
    elif submit_success:
        status = "kexam_submit_not_verified"
    else:
        status = str(submit_result.get("status") or verification["status"])
    verification_record = _verified_kexam_record_summary(after, verification.get("verified_record_ids", []))
    _, verified_item = _refresh_matching_item(session, course, item)
    issues = [*list(submit_result.get("issues") or []), *verification.get("verification_issues", [])]
    if success and submit_failed_after_post:
        issues.append("requests_submit_response_failed_record_verified")
    if not success and status not in issues:
        issues.append(status)
    return KExamRequestsSubmitResult(
        success=success,
        status=status,
        course_id=str(course_id),
        exam_url=exam_url,
        redacted_exam_url=redact_sensitive_url(exam_url),
        submit_result=submit_result,
        question_count=question_count,
        selected_answer_count=selected_answer_count,
        verified_item_state=str(verified_item.state),
        verified_item_result=verified_item.result,
        before=before,
        after=after,
        before_attempt_count=verification["before_attempt_count"],
        after_attempt_count=verification["after_attempt_count"],
        new_record_ids=verification["new_record_ids"],
        updated_record_ids=verification["updated_record_ids"],
        verified_record_ids=verification.get("verified_record_ids", []),
        unverified_record_ids=verification.get("unverified_record_ids", []),
        latest_submitted_attempt=verification["latest_submitted_attempt"],
        best_record_id_before=before.best_record_id,
        best_record_id_after=after.best_record_id,
        best_record_changed=before.best_record_id != after.best_record_id,
        verification_strength="record" if record_verified else "none",
        verification_method="kexam_record_probe" if record_verified else "kexam_record_probe_failed",
        verification_record_id=str(verification_record.get("record_id") or ""),
        verification_score=verification_record.get("score"),
        verification_submit_time=verification_record.get("submit_time"),
        issues=issues,
    )


def try_read_kexam_requests_probe(
    session: TmsSession,
    item: CourseItem,
    include_unsubmitted_records: bool = True,
) -> KExamExamPageReadResult | None:
    exam_url = item.detail_url or ""
    return try_read_kexam_requests_probe_url(
        session,
        exam_url=exam_url,
        include_unsubmitted_records=include_unsubmitted_records,
    )


def try_read_kexam_requests_probe_url(
    session: TmsSession,
    exam_url: str,
    include_unsubmitted_records: bool = True,
) -> KExamExamPageReadResult | None:
    if not _looks_like_kexam_exam_url(exam_url):
        return None
    return read_kexam_exam_page_requests(
        session,
        exam_url=exam_url,
        include_unsubmitted_records=include_unsubmitted_records,
    )


def _verified_kexam_record_summary(after: KExamExamPageReadResult, verified_record_ids: list[str]) -> dict[str, str | None]:
    record_id = verified_record_ids[0] if verified_record_ids else ""
    probes_by_id = {
        attempt_dict_record_id(row.get("attempt", {})): row
        for row in after.record_probes
        if attempt_dict_record_id(row.get("attempt", {}))
    }
    probe = probes_by_id.get(record_id) if record_id else None
    attempt = probe.get("attempt", {}) if isinstance(probe, dict) else {}
    return {
        "record_id": record_id,
        "score": (probe.get("score") if isinstance(probe, dict) else None) or attempt.get("score"),
        "submit_time": (probe.get("attempt_at") if isinstance(probe, dict) else None) or attempt.get("attempt_at"),
    }


def _refresh_matching_item(session: TmsSession, course: CourseDetail, item: CourseItem) -> tuple[CourseDetail, CourseItem]:
    try:
        refreshed = session.get_course_detail(course.url or course.course_id or "")
    except Exception:
        return course, item
    return refreshed, _find_matching_item(refreshed, item) or item


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


def _item_passed(item: CourseItem) -> bool:
    return item.state == ItemState.PASSED or result_satisfies_condition(
        item.pass_condition,
        item.result,
        item.passed_marker,
    )


def parse_kexam_take_page_html(html: str, take_url: str, base_url: str) -> KExamTakePageParse:
    payload = _extract_kexam_take_payload(html)
    if not isinstance(payload, dict):
        return KExamTakePageParse(take_url=take_url, issues=["kexam_take_payload_missing"])
    record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
    urls = payload.get("url") if isinstance(payload.get("url"), dict) else {}
    question_data = payload.get("questionData") if isinstance(payload.get("questionData"), dict) else {}
    questions: list[KExamTakeQuestion] = []
    issues: list[str] = []
    for key, row in question_data.items():
        if not isinstance(row, dict):
            continue
        question_type = _safe_int(row.get("type"), default=-1)
        options = [_option_text(option) for option in row.get("option") or []]
        answer = row.get("answer")
        if not options or answer is None:
            issues.append(f"kexam_question_unsupported:{key}")
            continue
        questions.append(
            KExamTakeQuestion(
                kques_id=str(row.get("id") or key),
                text=_html_to_text(str(row.get("questionTitle") or "")) or str(row.get("id") or key),
                options=options,
                display_options=_display_options(options),
                answer=answer,
                score=_safe_float(row.get("score")),
                question_type=question_type,
                opt_order=row.get("optOrder"),
            )
        )
    return KExamTakePageParse(
        take_url=take_url,
        record_id=str(record.get("id") or ""),
        record_time_used=_safe_int(record.get("timeUsed"), default=0),
        confirm_record_url=absolute_url(str(urls.get("confirmRecord") or ""), base_url) or "",
        submit_exam_url=absolute_url(str(urls.get("submitExam") or ""), base_url) or "",
        submitted_redir_url=absolute_url(str(urls.get("submittedRedir") or ""), base_url) or "",
        questions=questions,
        issues=issues,
    )


def build_kexam_submit_payload(
    take: KExamTakePageParse,
    course_title: str,
    item_title: str,
    question_bank: QuestionBank | None,
    quiz_policy: str,
    gemini_config: GeminiQuizConfig | None = None,
    gemini_client: GeminiQuizClient | None = None,
) -> KExamSubmitBuildResult:
    question_payload: list[dict[str, Any]] = []
    missing: list[str] = []
    issues: list[str] = []
    selected_count = 0
    supported: list[tuple[KExamTakeQuestion, QuizQuestion]] = []
    for question in take.questions:
        if not _is_supported_single_choice(question):
            missing.append(question.kques_id)
            issues.append(f"unsupported_question_type:{question.question_type}")
            continue
        quiz_question = QuizQuestion(
            text=question.text,
            options=question.display_options,
            name=question.kques_id,
            multiple=False,
        )
        supported.append((question, quiz_question))

    resolution = resolve_quiz_answers(
        questions=[quiz_question for _, quiz_question in supported],
        course_title=course_title,
        item_title=item_title,
        question_bank=question_bank,
        quiz_policy=quiz_policy,
        gemini_config=gemini_config,
        gemini_client=gemini_client,
    )
    for question, quiz_question in supported:
        answers = resolution.answers.get(question_key(quiz_question), [])
        selected_index = _match_answer_index(question, answers)
        if selected_index is None:
            missing.append(question.kques_id)
            continue
        correct = selected_index == _correct_answer_index(question)
        row = {
            "kquesID": question.kques_id,
            "answered": 1,
            "userAnswer": json.dumps(
                {"answer": _kexam_user_answer_value(question, selected_index)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "isCorrect": 1 if correct else 0,
            "getScore": _score_value(question.score if correct else 0),
            "type": question.question_type,
        }
        opt_order = _kexam_submit_opt_order(question)
        if opt_order is not None:
            row["optOrder"] = json.dumps(opt_order, ensure_ascii=False, separators=(",", ":"))
        question_payload.append(row)
        selected_count += 1
    payload = {
        "questionData": json.dumps(question_payload, ensure_ascii=False, separators=(",", ":")),
        "timeUsed": str(max(0, take.record_time_used)),
        "forceType": "",
    }
    return KExamSubmitBuildResult(
        payload=payload,
        question_count=len(take.questions),
        selected_answer_count=selected_count,
        missing_required=missing,
        issues=issues,
        answer_sources=resolution.sources,
        answer_source_counts=resolution.source_counts,
        answer_resolution_issues=resolution.issues,
        answer_resolution_notes=resolution.notes,
    )


def _submit_kexam_requests(
    session: TmsSession,
    course_title: str,
    item_title: str,
    exam_url: str,
    quiz_policy: str,
    question_bank: QuestionBank | None,
    gemini_config: GeminiQuizConfig | None,
    probe_only: bool = False,
) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "exam_url": redact_sensitive_url(exam_url),
        "probe_only": bool(probe_only),
    }
    exam_html = session.fetch_activity_html_requests(exam_url)
    parsed = parse_kexam_exam_page_html(exam_html, exam_url, session.base_url)
    trace["exam"] = {
        "take_operate_url": parsed.redacted_take_operate_url,
        "take_url": parsed.redacted_take_url,
        "record_modal_url": parsed.redacted_record_modal_url,
        "attempt_count": parsed.attempt_count,
        "continue_available": parsed.continue_available,
    }
    if not parsed.take_operate_url or not parsed.take_url:
        return {
            "success": False,
            "status": "kexam_entry_unavailable",
            "submit_trace": trace,
            "issues": ["take_operate_url_missing"],
        }

    operate_response = session._request_with_transient_retries(
        "POST",
        parsed.take_operate_url,
        data={},
        allow_redirects=False,
        headers=_kexam_ajax_headers(exam_url),
    )
    operate_status = _classify_or_raise(operate_response)
    trace["operate"] = _response_trace(operate_response)
    if operate_response.status_code >= 400:
        return {
            "success": False,
            "status": "kexam_entry_unavailable",
            "response_status_code": operate_response.status_code,
            "response_json_summary": _response_json_summary(operate_response),
            "submit_trace": trace,
            "issues": [f"take_operate_http_{operate_response.status_code}", operate_status.message],
        }

    take_html = session.fetch_activity_html_requests(parsed.take_url, referer=exam_url)
    take = parse_kexam_take_page_html(take_html, parsed.take_url, session.base_url)
    trace["take"] = {
        "take_url": redact_sensitive_url(parsed.take_url),
        "endpoint_summary": take.endpoint_summary,
        "issues": list(take.issues),
    }
    if take.issues or not take.confirm_record_url or not take.submit_exam_url:
        return {
            "success": False,
            "status": "kexam_take_parse_failed",
            "endpoint_summary": take.endpoint_summary,
            "question_count": len(take.questions),
            "selected_answer_count": 0,
            "submit_trace": trace,
            "issues": take.issues or ["kexam_take_endpoint_missing"],
        }

    built = build_kexam_submit_payload(
        take,
        course_title,
        item_title,
        question_bank,
        quiz_policy,
        gemini_config=gemini_config,
    )
    trace["payload"] = {
        "payload_keys": built.payload_keys,
        "question_count": built.question_count,
        "selected_answer_count": built.selected_answer_count,
        "answer_sources": built.answer_sources,
        "answer_source_counts": built.answer_source_counts,
        "answer_resolution_issues": built.answer_resolution_issues,
        "answer_resolution_notes": built.answer_resolution_notes,
        "missing_required": built.missing_required[:20],
    }
    if built.missing_required or built.issues:
        return {
            "success": False,
            "status": "kexam_missing_required_answers",
            "endpoint_summary": take.endpoint_summary,
            "payload_keys": built.payload_keys,
            "question_count": built.question_count,
            "selected_answer_count": built.selected_answer_count,
            "answer_sources": built.answer_sources,
            "answer_source_counts": built.answer_source_counts,
            "answer_resolution_issues": built.answer_resolution_issues,
            "answer_resolution_notes": built.answer_resolution_notes,
            "submit_trace": trace,
            "issues": [*built.issues, "missing_required:" + ",".join(built.missing_required[:20])],
        }
    if probe_only:
        return {
            "success": True,
            "status": "kexam_submit_preflight_ready",
            "endpoint_summary": take.endpoint_summary,
            "payload_keys": built.payload_keys,
            "question_count": built.question_count,
            "selected_answer_count": built.selected_answer_count,
            "answer_sources": built.answer_sources,
            "answer_source_counts": built.answer_source_counts,
            "answer_resolution_issues": built.answer_resolution_issues,
            "answer_resolution_notes": built.answer_resolution_notes,
            "submit_trace": trace,
            "issues": [f"answer_resolution:{issue}" for issue in built.answer_resolution_issues],
        }

    confirm_response = session._request_with_transient_retries(
        "POST",
        take.confirm_record_url,
        data={},
        allow_redirects=False,
        headers=_kexam_ajax_headers(parsed.take_url),
    )
    _classify_or_raise(confirm_response)
    confirm_json = _safe_json(confirm_response)
    trace["confirm"] = _response_trace(confirm_response)
    if confirm_response.status_code >= 400 or _json_indicates_failure(confirm_json):
        return {
            "success": False,
            "status": "kexam_confirm_failed",
            "endpoint_summary": take.endpoint_summary,
            "response_status_code": confirm_response.status_code,
            "response_json_summary": _response_json_summary(confirm_response),
            "answer_sources": built.answer_sources,
            "answer_source_counts": built.answer_source_counts,
            "answer_resolution_issues": built.answer_resolution_issues,
            "answer_resolution_notes": built.answer_resolution_notes,
            "submit_trace": trace,
            "issues": [f"kexam_confirm_http_{confirm_response.status_code}"],
        }

    submit_response = _post_kexam_submit_without_transient_raise(
        session,
        take.submit_exam_url,
        data=built.payload,
        headers=_kexam_ajax_headers(parsed.take_url),
    )
    submit_json = _safe_json(submit_response)
    trace["submit"] = _response_trace(submit_response)
    if submit_response.status_code >= 400 or _json_indicates_failure(submit_json):
        return {
            "success": False,
            "status": "kexam_submit_failed",
            "endpoint_summary": take.endpoint_summary,
            "payload_keys": built.payload_keys,
            "question_count": built.question_count,
            "selected_answer_count": built.selected_answer_count,
            "answer_sources": built.answer_sources,
            "answer_source_counts": built.answer_source_counts,
            "answer_resolution_issues": built.answer_resolution_issues,
            "answer_resolution_notes": built.answer_resolution_notes,
            "response_status_code": submit_response.status_code,
            "response_json_summary": _response_json_summary(submit_response),
            "submit_trace": trace,
            "issues": [f"kexam_submit_http_{submit_response.status_code}"],
        }
    return {
        "success": True,
        "status": "submitted",
        "endpoint_summary": take.endpoint_summary,
        "payload_keys": built.payload_keys,
        "question_count": built.question_count,
        "selected_answer_count": built.selected_answer_count,
        "answer_sources": built.answer_sources,
        "answer_source_counts": built.answer_source_counts,
        "answer_resolution_issues": built.answer_resolution_issues,
        "answer_resolution_notes": built.answer_resolution_notes,
        "response_status_code": submit_response.status_code,
        "response_json_summary": _response_json_summary(submit_response),
        "submit_trace": trace,
        "issues": [f"answer_resolution:{issue}" for issue in built.answer_resolution_issues],
    }


def _extract_kexam_take_payload(html: str) -> dict[str, Any] | None:
    match = re.search(
        r"""fs\.kexamTake\.setData\(\s*['"][^'"]+['"]\s*,\s*JSON\.parse\('(?P<payload>(?:\\.|[^'])*)'\)\s*\)""",
        html or "",
        re.S,
    )
    if not match:
        return None
    raw = match.group("payload")
    try:
        decoded = ast.literal_eval("'" + raw + "'")
    except Exception:
        decoded = raw.encode("utf-8").decode("unicode_escape")
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _load_question_bank(path: str | None) -> QuestionBank | None:
    if path:
        return QuestionBank.from_path(path)
    latest = find_latest_question_bank_path()
    return QuestionBank.from_path(latest) if latest else None


def _load_course_detail(session: TmsSession, course: str, exam_url: str) -> CourseDetail:
    try:
        return session.get_course_detail(course)
    except Exception:
        course_id = _course_id_from_exam_url(exam_url) or str(course)
        return CourseDetail(title=f"course-{course_id}", url=f"{session.base_url}/course/{course_id}", course_id=course_id)


def _infer_quiz_title_from_course(course: CourseDetail) -> str:
    for item in course.items:
        if ItemKind(item.kind) == ItemKind.QUIZ and item.title:
            return item.title
    return ""


def _course_id_from_exam_url(exam_url: str) -> str | None:
    match = re.search(r"/course/(\d+)/", urlparse(exam_url).path)
    return match.group(1) if match else None


def _looks_like_kexam_exam_url(url: str) -> bool:
    return bool(re.search(r"/course/\d+/exam/\d+", url or ""))


def _html_to_text(value: str) -> str:
    return normalize_text(BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True))


def _display_options(options: list[str]) -> list[str]:
    if len(options) > 2 and not any(re.match(r"^[A-Z]\.", normalize_text(option)) for option in options):
        return [f"{chr(ord('A') + index)}. {option}" for index, option in enumerate(options)]
    return list(options)


def _option_text(option: Any) -> str:
    if isinstance(option, dict):
        for key in ("text", "label", "title", "value", "name"):
            value = option.get(key)
            if value is not None and str(value).strip():
                return normalize_text(str(value))
        return normalize_text(" ".join(str(value) for value in option.values() if value))
    return normalize_text(str(option))


def _is_supported_single_choice(question: KExamTakeQuestion) -> bool:
    return bool(question.options) and isinstance(_correct_answer_index(question), int)


def _correct_answer_index(question: KExamTakeQuestion) -> int | None:
    return _answer_index(question.answer, question)


def _answer_index(answer: Any, question: KExamTakeQuestion) -> int | None:
    if isinstance(answer, bool):
        return int(answer)
    if isinstance(answer, int):
        return answer
    if isinstance(answer, str):
        text = normalize_text(answer)
        if text.isdigit():
            return int(text)
        for index, (raw, display) in enumerate(zip(question.options, question.display_options, strict=False)):
            if text in {normalize_text(raw), normalize_text(display)}:
                return index
        return None
    if isinstance(answer, dict):
        for key in ("answer", "index", "value", "text", "label"):
            if key in answer:
                found = _answer_index(answer.get(key), question)
                if found is not None:
                    return found
        return None
    if isinstance(answer, (list, tuple)) and len(answer) == 1:
        return _answer_index(answer[0], question)
    return None


def _match_answer_index(question: KExamTakeQuestion, answers: list[str]) -> int | None:
    for answer in answers:
        answer_text = normalize_text(answer)
        for index, (raw, display) in enumerate(zip(question.options, question.display_options, strict=False)):
            raw_text = normalize_text(raw)
            display_text = normalize_text(display)
            if (
                answer_text == display_text
                or answer_text == raw_text
                or answer_text in display_text
                or display_text in answer_text
                or answer_text in raw_text
                or raw_text in answer_text
            ):
                return index
    return None


def _kexam_user_answer_value(question: KExamTakeQuestion, selected_index: int) -> int | list[int]:
    if question.question_type == 1:
        return [selected_index]
    return selected_index


def _kexam_submit_opt_order(question: KExamTakeQuestion) -> Any:
    if question.opt_order is not None:
        return question.opt_order
    if question.question_type == 1:
        return list(range(len(question.options)))
    return None


def _score_value(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _kexam_ajax_headers(referer: str) -> dict[str, str]:
    return stable_login_headers(
        {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        }
    )


def _classify_or_raise(response):
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
    return status


def _json_indicates_failure(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("status") is False:
        return True
    ret = payload.get("ret")
    return isinstance(ret, dict) and str(ret.get("status")).lower() == "false"


def _safe_json(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _response_json_summary(response) -> dict[str, Any]:
    payload = _safe_json(response)
    if isinstance(payload, dict):
        summary: dict[str, Any] = {"json": True, "keys": sorted(str(key) for key in payload)[:50]}
        for key in ("status", "success", "message"):
            if isinstance(payload.get(key), (str, int, float, bool, type(None))):
                summary[key] = payload.get(key)
        ret = payload.get("ret")
        if isinstance(ret, dict):
            summary["ret_keys"] = sorted(str(key) for key in ret)[:50]
            for key in ("status", "success", "msg", "errorType"):
                if isinstance(ret.get(key), (str, int, float, bool, type(None))):
                    summary[f"ret_{key}"] = ret.get(key)
        return summary
    if isinstance(payload, list):
        return {"json": True, "type": "list", "length": len(payload)}
    return {"json": False}


def _response_trace(response) -> dict[str, Any]:
    return {
        "url": redact_sensitive_url(str(getattr(response, "url", ""))),
        "status_code": int(getattr(response, "status_code", 0) or 0),
        "json_summary": _response_json_summary(response),
    }


def _post_kexam_submit_without_transient_raise(
    session: TmsSession,
    url: str,
    *,
    data: dict[str, str],
    headers: dict[str, str],
):
    if hasattr(session, "http") and hasattr(session, "url"):
        return session.http.request(
            "POST",
            session.url(url),
            data=data,
            allow_redirects=False,
            headers=headers,
            timeout=getattr(session, "timeout", 30),
        )
    return session._request_with_transient_retries(
        "POST",
        url,
        data=data,
        allow_redirects=False,
        headers=headers,
    )


def _is_text_response(response) -> bool:
    content_type = response.headers.get("content-type") or response.headers.get("Content-Type") or ""
    return any(token in content_type.lower() for token in ("text", "html", "json", "javascript", "xml"))


__all__ = [
    "KExamRequestsSubmitResult",
    "KExamSubmitBuildResult",
    "KExamTakePageParse",
    "KExamTakeQuestion",
    "build_kexam_submit_payload",
    "parse_kexam_take_page_html",
    "read_kexam_exam_page_requests",
    "run_requests_kexam_resubmit_diagnostic",
    "try_read_kexam_requests_probe",
    "try_read_kexam_requests_probe_url",
    "try_run_kexam_requests_submit",
]
