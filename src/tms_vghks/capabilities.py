from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


CapabilityStatus = Literal["equivalent", "partially_equivalent", "not_equivalent_yet"]
CapabilityScope = Literal["read_only", "live_mutation", "scheduler"]


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    feature: str
    playwright_entrypoints: tuple[str, ...]
    requests_entrypoints: tuple[str, ...]
    status: CapabilityStatus
    scope: CapabilityScope = "read_only"
    requests_success_statuses: tuple[str, ...] = ()
    requests_failure_statuses: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


CAPABILITY_CONTRACTS: tuple[CapabilityContract, ...] = (
    CapabilityContract(
        feature="login_session",
        playwright_entrypoints=(
            "TmsSession.ensure_login",
            "TmsSession.login_playwright_with_ocr",
            "TmsSession.sync_cookies_to_requests",
            "PlaywrightBackendTools.auto_login_tms",
            "PlaywrightBackendTools.ensure_authenticated",
            "PlaywrightBackendTools.check_status",
            "PlaywrightBackendTools.recover_transient_error",
        ),
        requests_entrypoints=(
            "TmsSession.prepare_requests_login",
            "TmsSession.submit_requests_login",
            "run_batch_requests_login",
            "TmsSession.sync_cookies_to_browser",
            "RequestsBackendTools.auto_login_tms",
            "RequestsBackendTools.ensure_authenticated",
            "RequestsBackendTools.check_status",
            "RequestsBackendTools.recover_transient_error",
        ),
        status="equivalent",
    ),
    CapabilityContract(
        feature="course_list_and_detail",
        playwright_entrypoints=(
            "TmsSession.list_pending_courses_playwright",
            "TmsSession.list_completed_courses_playwright",
            "TmsSession.get_course_detail_playwright",
            "PlaywrightBackendTools.list_pending_courses",
            "PlaywrightBackendTools.list_completed_courses",
            "PlaywrightBackendTools.get_course_detail",
        ),
        requests_entrypoints=(
            "TmsSession.list_pending_courses_requests",
            "TmsSession.list_completed_courses_requests",
            "TmsSession.get_course_detail_requests",
            "TmsSession.use_backend",
            "TmsSession.backend_tools",
            "RequestsBackendTools.list_pending_courses",
            "RequestsBackendTools.list_completed_courses",
            "RequestsBackendTools.get_course_detail",
        ),
        status="equivalent",
    ),
    CapabilityContract(
        feature="reading_video_completion",
        playwright_entrypoints=(
            "TmsRunner.run_reading_playwright",
            "TmsRunner.start_reading_playwright",
            "TmsRunner.finish_reading_playwright",
            "PlaywrightBackendTools.complete_item",
            "PlaywrightBackendTools.complete_reading",
        ),
        requests_entrypoints=(
            "TmsRunner.run_reading_requests",
            "TmsRunner.start_reading_requests",
            "TmsRunner.finish_reading_requests",
            "run_requests_watch_time",
            "diag reading",
            "RequestsBackendTools.complete_item",
            "RequestsBackendTools.complete_reading",
        ),
        status="partially_equivalent",
        scope="live_mutation",
        requests_success_statuses=("already_passed", "requests_watch_time_verified"),
        requests_failure_statuses=(
            "endpoint_unverified",
            "watch_time_missing_token",
            "watch_time_post_failed",
            "watch_time_not_verified",
        ),
    ),
    CapabilityContract(
        feature="survey_submission",
        playwright_entrypoints=(
            "TmsRunner.run_survey_playwright",
            "validate_playwright_forms",
            "PlaywrightBackendTools.complete_item",
            "PlaywrightBackendTools.complete_survey",
        ),
        requests_entrypoints=(
            "TmsRunner.run_survey_requests",
            "classify_activity_form_requests",
            "run_survey_requests_submit",
            "diag forms",
            "RequestsBackendTools.complete_item",
            "RequestsBackendTools.complete_survey",
        ),
        status="partially_equivalent",
        scope="live_mutation",
        requests_success_statuses=("requests_survey_submit_verified",),
        requests_failure_statuses=(
            "form_endpoint_unverified",
            "form_missing_required_fields",
            "form_submit_failed",
            "form_submit_not_verified",
        ),
    ),
    CapabilityContract(
        feature="quiz_submission",
        playwright_entrypoints=(
            "TmsRunner.run_quiz_playwright",
            "collect_quiz_questions",
            "apply_quiz_answers",
            "PlaywrightBackendTools.complete_item",
            "PlaywrightBackendTools.complete_quiz",
        ),
        requests_entrypoints=(
            "TmsRunner.run_quiz_requests",
            "classify_activity_form_requests",
            "run_quiz_requests_submit",
            "diag forms",
            "RequestsBackendTools.complete_item",
            "RequestsBackendTools.complete_quiz",
        ),
        status="partially_equivalent",
        scope="live_mutation",
        requests_success_statuses=(
            "requests_quiz_submit_course_detail_only",
            "requests_kexam_submit_verified",
            "requests_submit_response_failed_record_verified",
        ),
        requests_failure_statuses=(
            "form_endpoint_unverified",
            "form_missing_required_fields",
            "form_submit_failed",
            "form_submit_not_verified",
            "kexam_entry_unavailable",
            "kexam_take_parse_failed",
            "kexam_missing_required_answers",
            "kexam_confirm_failed",
            "kexam_submit_failed",
            "kexam_submit_not_verified",
        ),
    ),
    CapabilityContract(
        feature="kexam_records_resubmit",
        playwright_entrypoints=(
            "read_kexam_exam_page_playwright",
            "run_playwright_quiz_resubmit_diagnostic",
            "probe_kexam_attempt_with_page",
        ),
        requests_entrypoints=(
            "read_kexam_exam_page_requests",
            "run_requests_kexam_resubmit_diagnostic",
            "probe_kexam_attempt_requests",
            "diag kexam-records",
            "diag quiz-resubmit",
        ),
        status="equivalent",
        scope="live_mutation",
        requests_success_statuses=(
            "requests_kexam_submit_verified",
            "requests_submit_response_failed_record_verified",
            "resubmit_verified",
        ),
        requests_failure_statuses=(
            "kexam_entry_unavailable",
            "kexam_take_parse_failed",
            "kexam_missing_required_answers",
            "kexam_confirm_failed",
            "kexam_submit_failed",
            "kexam_submit_not_verified",
        ),
        notes=("Equivalent for KExam pages, not for every non-KExam quiz engine.",),
    ),
    CapabilityContract(
        feature="question_bank_history_export",
        playwright_entrypoints=(
            "export_question_bank_playwright",
            "export_historical_quiz_bank_playwright",
            "probe_activity_playwright",
            "probe_kexam_attempt_with_page",
        ),
        requests_entrypoints=(
            "export_question_bank",
            "export_historical_quiz_bank_requests",
            "fetch_result_modal_summary",
            "probe_activity_requests",
            "probe_kexam_attempt_requests",
        ),
        status="partially_equivalent",
        notes=("KExam historical records can be exported with requests; generic rendered-only quizzes may expose metadata only.",),
    ),
    CapabilityContract(
        feature="form_validation_classification",
        playwright_entrypoints=(
            "validate_playwright_forms",
            "classify_form_html after rendered-page capture",
        ),
        requests_entrypoints=(
            "TmsSession.fetch_activity_html",
            "classify_activity_form_requests",
            "fetch_activity_html_requests",
            "RequestsBackendTools.fetch_activity_html",
        ),
        status="partially_equivalent",
    ),
    CapabilityContract(
        feature="scheduler_run_backend",
        playwright_entrypoints=(
            "TmsRunner.run_scheduler with OperationBackend.PLAYWRIGHT",
            "TmsRunner.run_scheduler with OperationBackend.HYBRID",
            "PlaywrightBackendTools.run_scheduler",
            "HybridBackendTools.run_scheduler",
        ),
        requests_entrypoints=(
            "TmsRunner.run_scheduler with OperationBackend.REQUESTS",
            "TmsRunner.run_item_requests",
            "RequestsBackendTools.run_scheduler",
            "RequestsBackendTools.run_course",
        ),
        status="partially_equivalent",
        scope="scheduler",
        requests_success_statuses=(
            "requests_watch_time_verified",
            "already_passed",
            "requests_survey_submit_verified",
            "requests_quiz_submit_course_detail_only",
            "requests_kexam_submit_verified",
            "requests_submit_response_failed_record_verified",
        ),
        requests_failure_statuses=(
            "endpoint_unverified",
            "watch_time_not_verified",
            "form_endpoint_unverified",
            "form_missing_required_fields",
            "requests_submit_failed_record_blank",
            "kexam_submit_not_verified",
            "mutation_unsupported",
        ),
    ),
)


def capability_contracts_by_feature() -> dict[str, CapabilityContract]:
    return {contract.feature: contract for contract in CAPABILITY_CONTRACTS}


def validate_capability_contracts() -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    for contract in CAPABILITY_CONTRACTS:
        if contract.feature in seen:
            issues.append(f"duplicate_feature:{contract.feature}")
        seen.add(contract.feature)
        if not contract.playwright_entrypoints:
            issues.append(f"missing_playwright_entrypoints:{contract.feature}")
        if not contract.requests_entrypoints:
            issues.append(f"missing_requests_entrypoints:{contract.feature}")
        if contract.scope in {"live_mutation", "scheduler"}:
            if not contract.requests_success_statuses:
                issues.append(f"missing_requests_success_statuses:{contract.feature}")
            if not contract.requests_failure_statuses:
                issues.append(f"missing_requests_failure_statuses:{contract.feature}")
    return issues


__all__ = [
    "CAPABILITY_CONTRACTS",
    "CapabilityContract",
    "CapabilityScope",
    "CapabilityStatus",
    "capability_contracts_by_feature",
    "validate_capability_contracts",
]
