from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from .models import ItemKind
from .parsers import BASE_URL, absolute_url, normalize_text
from .playwright_probe import ActivityProbe, ExtractedQuestion, KExamAttempt, kexam_attempt_to_dict
from .privacy import redact_sensitive_url


DEFAULT_KEXAM_COURSE = "5416"
DEFAULT_KEXAM_EXAM_URL = "https://tms.vghks.gov.tw/course/5416/exam/10613"
KEXAM_CONTINUE_LABELS = ("繼續測驗", "重新測驗", "開始測驗", "進入測驗", "開始", "繼續")
KEXAM_SUBMIT_LABELS = ("送出", "提交", "確定", "交卷")


@dataclass(slots=True)
class KExamExamPageParse:
    exam_url: str
    attempt_limit_text: str = ""
    attempt_count: int | None = None
    best_record_url: str = ""
    redacted_best_record_url: str = ""
    best_record_id: str | None = None
    record_modal_url: str = ""
    redacted_record_modal_url: str = ""
    take_operate_url: str = ""
    redacted_take_operate_url: str = ""
    take_url: str = ""
    redacted_take_url: str = ""
    continue_available: bool = False


@dataclass(slots=True)
class KExamExamPageReadResult:
    success: bool
    status: str
    exam_url: str
    redacted_exam_url: str
    attempt_limit_text: str = ""
    attempt_count: int | None = None
    continue_available: bool = False
    best_record_url: str = ""
    best_record_id: str | None = None
    record_modal_url: str = ""
    take_operate_url: str = ""
    take_url: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    record_probes: list[dict[str, Any]] = field(default_factory=list)
    best_record_probe: dict[str, Any] | None = None
    record_count: int = 0
    record_question_count: int = 0
    record_selected_answer_count: int = 0
    issues: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KExamResubmitDiagnosticResult:
    success: bool
    status: str
    course_id: str
    exam_url: str
    redacted_exam_url: str
    before: KExamExamPageReadResult | None = None
    after: KExamExamPageReadResult | None = None
    before_attempt_count: int = 0
    after_attempt_count: int = 0
    new_record_ids: list[str] = field(default_factory=list)
    updated_record_ids: list[str] = field(default_factory=list)
    latest_submitted_attempt: dict[str, Any] | None = None
    best_record_id_before: str | None = None
    best_record_id_after: str | None = None
    best_record_changed: bool = False
    question_count: int = 0
    selected_answer_count: int = 0
    submit_result: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def parse_kexam_exam_page_html(html: str, exam_url: str, base_url: str = BASE_URL) -> KExamExamPageParse:
    soup = BeautifulSoup(html or "", "html.parser")
    text = normalize_text(soup.get_text(" ", strip=True))
    attempt_limit_text = _extract_attempt_limit_text(text)
    attempt_count = None
    attempt_match = re.search(r"已測驗\s*(\d+)\s*次", text)
    if attempt_match:
        attempt_count = int(attempt_match.group(1))

    best_record_url = ""
    record_modal_url = ""
    for anchor in soup.find_all("a"):
        label = normalize_text(anchor.get_text(" ", strip=True))
        href = anchor.get("href") or ""
        data_url = anchor.get("data-url") or ""
        if not record_modal_url and "紀錄" in label and data_url:
            record_modal_url = absolute_url(data_url, base_url) or data_url
        if not best_record_url and ("作答記錄" in label or re.search(r"/kexam/\d+/record\b", href)):
            if re.search(r"/kexam/\d+/record\b", href):
                best_record_url = absolute_url(href, base_url) or href

    take_operate_url = _extract_take_operate_url(html, base_url)
    take_url = _take_url_from_operate_url(take_operate_url, base_url)
    return KExamExamPageParse(
        exam_url=exam_url,
        attempt_limit_text=attempt_limit_text,
        attempt_count=attempt_count,
        best_record_url=best_record_url,
        redacted_best_record_url=redact_sensitive_url(best_record_url) if best_record_url else "",
        best_record_id=record_id_from_url(best_record_url),
        record_modal_url=record_modal_url,
        redacted_record_modal_url=redact_sensitive_url(record_modal_url) if record_modal_url else "",
        take_operate_url=take_operate_url,
        redacted_take_operate_url=redact_sensitive_url(take_operate_url) if take_operate_url else "",
        take_url=take_url,
        redacted_take_url=redact_sensitive_url(take_url) if take_url else "",
        continue_available=any(label in text for label in KEXAM_CONTINUE_LABELS) or bool(take_operate_url),
    )


def build_kexam_resubmit_verification(
    before: KExamExamPageReadResult,
    after: KExamExamPageReadResult,
    expected_question_count: int = 0,
    expected_selected_answer_count: int = 0,
) -> dict[str, Any]:
    before_ids = {str(row.get("record_id")) for row in before.attempts if row.get("record_id")}
    after_ids = {str(row.get("record_id")) for row in after.attempts if row.get("record_id")}
    new_record_ids = sorted(after_ids - before_ids)
    before_by_id = {str(row.get("record_id")): row for row in before.attempts if row.get("record_id")}
    after_by_id = {str(row.get("record_id")): row for row in after.attempts if row.get("record_id")}
    updated_record_ids = sorted(
        record_id
        for record_id, after_row in after_by_id.items()
        if record_id in before_by_id and _attempt_row_changed(before_by_id[record_id], after_row)
    )
    latest_submitted = latest_submitted_attempt(after.attempts)
    before_count = before.attempt_count if before.attempt_count is not None else len(before.attempts)
    after_count = after.attempt_count if after.attempt_count is not None else len(after.attempts)
    changed_record_ids = sorted(set(new_record_ids + updated_record_ids))
    if not changed_record_ids and after_count is not None and before_count is not None and after_count > before_count:
        latest_id = attempt_dict_record_id(latest_submitted or {})
        if latest_id:
            changed_record_ids = [latest_id]
    if not changed_record_ids and after.best_record_id and before.best_record_id and after.best_record_id != before.best_record_id:
        changed_record_ids = [after.best_record_id]
    verified_record_ids, unverified_record_ids, verification_issues = _verify_changed_records(
        changed_record_ids,
        after,
        expected_question_count=expected_question_count,
        expected_selected_answer_count=expected_selected_answer_count,
    )
    if changed_record_ids and verified_record_ids:
        status = "resubmit_verified"
    elif changed_record_ids and _has_blank_answer_record(unverified_record_ids, after):
        status = "kexam_submit_record_created_without_answers"
    elif changed_record_ids:
        status = "kexam_submit_not_verified"
    else:
        status = "submit_not_verified_by_record"
    return {
        "status": status,
        "before_attempt_count": int(before_count or 0),
        "after_attempt_count": int(after_count or 0),
        "new_record_ids": new_record_ids,
        "updated_record_ids": updated_record_ids,
        "verified_record_ids": verified_record_ids,
        "unverified_record_ids": unverified_record_ids,
        "verification_issues": verification_issues,
        "latest_submitted_attempt": latest_submitted,
    }


def _verify_changed_records(
    record_ids: list[str],
    after: KExamExamPageReadResult,
    *,
    expected_question_count: int = 0,
    expected_selected_answer_count: int = 0,
) -> tuple[list[str], list[str], list[str]]:
    if not record_ids:
        return [], [], []
    probes_by_id = {
        attempt_dict_record_id(row.get("attempt", {})): row
        for row in after.record_probes
        if attempt_dict_record_id(row.get("attempt", {}))
    }
    verified: list[str] = []
    unverified: list[str] = []
    issues: list[str] = []
    for record_id in record_ids:
        probe = probes_by_id.get(record_id)
        if not probe:
            unverified.append(record_id)
            issues.append(f"record_probe_missing:{record_id}")
            continue
        if _record_probe_has_saved_answers(
            probe,
            expected_question_count=expected_question_count,
            expected_selected_answer_count=expected_selected_answer_count,
        ):
            verified.append(record_id)
        else:
            unverified.append(record_id)
            issues.append(f"record_probe_unverified:{record_id}")
    return verified, unverified, issues


def _record_probe_has_saved_answers(
    probe: dict[str, Any],
    *,
    expected_question_count: int = 0,
    expected_selected_answer_count: int = 0,
) -> bool:
    question_count = int(probe.get("question_count") or 0)
    selected_count = int(probe.get("selected_answer_count") or 0)
    if question_count <= 0 or selected_count <= 0:
        return False
    if expected_question_count and question_count < expected_question_count:
        return False
    if expected_selected_answer_count and selected_count < expected_selected_answer_count:
        return False
    if not expected_selected_answer_count and selected_count < question_count:
        return False
    return _record_probe_has_score_or_submit_time(probe)


def _record_probe_has_score_or_submit_time(probe: dict[str, Any]) -> bool:
    score = _score_value(probe.get("score") or (probe.get("attempt") or {}).get("score"))
    if score is not None and score > 0:
        return True
    summary = normalize_text(
        " ".join(
            str(value or "")
            for value in (
                probe.get("raw_summary"),
                (probe.get("attempt") or {}).get("raw_summary"),
            )
        )
    )
    return bool(re.search(r"交卷時間.*\d{4}-\d{2}-\d{2}", summary) or _summary_has_completion_signal(summary))


def _score_value(value: Any) -> float | None:
    text = normalize_text(str(value or ""))
    if not text or text in {"-", "--"}:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _has_blank_answer_record(record_ids: list[str], after: KExamExamPageReadResult) -> bool:
    if not record_ids:
        return False
    probes_by_id = {
        attempt_dict_record_id(row.get("attempt", {})): row
        for row in after.record_probes
        if attempt_dict_record_id(row.get("attempt", {}))
    }
    for record_id in record_ids:
        probe = probes_by_id.get(record_id)
        if probe and int(probe.get("question_count") or 0) > 0 and int(probe.get("selected_answer_count") or 0) == 0:
            return True
    return False


def _attempt_row_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    before_score = normalize_text(str(before.get("score") or ""))
    after_score = normalize_text(str(after.get("score") or ""))
    before_summary = normalize_text(str(before.get("raw_summary") or ""))
    after_summary = normalize_text(str(after.get("raw_summary") or ""))
    before_status = normalize_text(str(before.get("submitted_status") or ""))
    after_status = normalize_text(str(after.get("submitted_status") or ""))
    if after_status == "submitted" and before_status and before_status != after_status:
        return True
    if after_score and after_score not in {"-", "--"} and before_score != after_score:
        return True
    if before_summary != after_summary and _summary_has_completion_signal(after_summary):
        return True
    return False


def _summary_has_completion_signal(summary: str) -> bool:
    if not summary:
        return False
    if re.search(r"\b\d+(?:\.\d+)?\b.*\d{4}-\d{2}-\d{2}.*\d{4}-\d{2}-\d{2}", summary):
        return True
    return bool(re.search(r"\b\d+(?:\.\d+)?\b", summary)) and " - " not in f" {summary} "


def best_record_attempt(
    parsed: KExamExamPageParse,
    attempts: list[KExamAttempt],
    base_url: str,
) -> KExamAttempt | None:
    if parsed.best_record_id:
        for attempt in attempts:
            if attempt.record_id == parsed.best_record_id:
                return attempt
    if not parsed.best_record_url:
        return None
    parsed_url = urlparse(parsed.best_record_url)
    exam_match = re.search(r"/kexam/(\d+)/record\b", parsed_url.path)
    record_id = record_id_from_url(parsed.best_record_url)
    return KExamAttempt(
        exam_id=exam_match.group(1) if exam_match else None,
        record_id=record_id,
        record_url=absolute_url(parsed.best_record_url, base_url) or parsed.best_record_url,
        redacted_record_url=redact_sensitive_url(parsed.best_record_url),
        submitted_status="submitted",
    )


def probe_to_dict(probe: ActivityProbe) -> dict[str, Any]:
    return {
        "availability": probe.availability,
        "reason": probe.reason,
        "score": probe.score,
        "attempt_at": probe.attempt_at,
        "raw_summary": probe.raw_summary,
        "provenance_steps": list(probe.provenance_steps),
        "attempt": kexam_attempt_to_dict(probe.attempt),
        "question_count": len(probe.question_records),
        "selected_answer_count": sum(len(question.selected_answers) for question in probe.question_records),
        "questions": [question_to_dict(question) for question in probe.question_records],
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "severity": issue.severity,
            }
            for issue in probe.issues
        ],
    }


def question_to_dict(question: ExtractedQuestion) -> dict[str, Any]:
    return {
        "text": question.text,
        "options": list(question.options),
        "selected_answers": list(question.selected_answers),
        "correct_answers": list(question.correct_answers),
        "incorrect_answers": list(question.incorrect_answers),
        "question_type": question.question_type,
        "confidence": question.confidence,
    }


def attempt_dict_record_id(attempt: dict[str, Any]) -> str | None:
    value = attempt.get("record_id") if isinstance(attempt, dict) else None
    return str(value) if value else None


def latest_submitted_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    submitted = [row for row in attempts if row.get("submitted_status") == "submitted"]
    if not submitted:
        return attempts[-1] if attempts else None
    return sorted(submitted, key=lambda row: str(row.get("attempt_at") or ""))[-1]


def record_id_from_url(record_url: str) -> str | None:
    if not record_url:
        return None
    query = parse_qs(urlparse(record_url).query)
    return (query.get("recordID") or query.get("recordId") or query.get("recordid") or [None])[0]


def _extract_attempt_limit_text(text: str) -> str:
    match = re.search(r"次數限制\s*(.+?)(?:成績|通過條件|測驗說明|繼續測驗|$)", text)
    return normalize_text(match.group(1)) if match else ""


def _extract_take_operate_url(html: str, base_url: str) -> str:
    for match in re.finditer(r"""fs\.post\(\s*["'](?P<url>[^"']*act=takeExam[^"']*)["']""", html or ""):
        return absolute_url(match.group("url"), base_url) or match.group("url")
    return ""


def _take_url_from_operate_url(operate_url: str, base_url: str) -> str:
    if not operate_url:
        return ""
    redir = (parse_qs(urlparse(operate_url).query).get("redir") or [""])[0]
    return absolute_url(redir, base_url) if redir else ""


__all__ = [
    "DEFAULT_KEXAM_COURSE",
    "DEFAULT_KEXAM_EXAM_URL",
    "KEXAM_CONTINUE_LABELS",
    "KEXAM_SUBMIT_LABELS",
    "KExamExamPageParse",
    "KExamExamPageReadResult",
    "KExamResubmitDiagnosticResult",
    "attempt_dict_record_id",
    "best_record_attempt",
    "build_kexam_resubmit_verification",
    "latest_submitted_attempt",
    "parse_kexam_exam_page_html",
    "probe_to_dict",
    "question_to_dict",
    "record_id_from_url",
]
