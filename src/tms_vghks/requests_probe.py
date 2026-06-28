from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from .export_pacing import ExportPacerProtocol
from .models import CourseDetail, CourseItem, ItemKind
from .parsers import normalize_text
from .playwright_probe import (
    ActivityProbe,
    ExtractedQuestion,
    KExamAttempt,
    dedupe_kexam_attempts,
    extract_activity_probe_from_html,
    extract_kexam_attempts_from_modal_html,
    kexam_attempt_probe_key,
)
from .question_bank_export import ExportIssue
from .session import TmsSession


@dataclass(slots=True)
class ActivityFormClassification:
    classification: str
    entry_method: str | None
    fields: Any
    question_count: int
    selected_answer_count: int
    issues: list[ExportIssue]


def probe_activity_requests(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    include_unsubmitted_records: bool = False,
    pacer: ExportPacerProtocol | None = None,
) -> ActivityProbe:
    html_parts: list[tuple[str, str]] = []
    result_url = item.metadata.get("result_modal_url")
    if result_url:
        try:
            html_parts.append(
                (
                    "result_modal_url",
                    session.fetch_activity_html_requests(str(result_url), referer=course.url),
                )
            )
        except Exception as exc:
            return ActivityProbe(
                availability="unavailable",
                reason=str(exc),
                provenance_steps=["result_modal_url_error"],
                issues=[ExportIssue("result_endpoint_unavailable", str(exc))],
            )

    if item.detail_url:
        try:
            html_parts.append(
                (
                    "detail_url",
                    session.fetch_activity_html_requests(item.detail_url, referer=course.url),
                )
            )
        except Exception as exc:
            html_parts.append(("detail_url_error", f"<div>{exc}</div>"))

    best = ActivityProbe()
    attempt_probes: list[ActivityProbe] = []
    seen_attempt_keys: set[str] = set()
    for step, html in html_parts:
        probe = extract_activity_probe_from_html(html, item.kind)
        probe.provenance_steps.append(step)
        if ItemKind(item.kind) == ItemKind.QUIZ:
            for attempt in dedupe_kexam_attempts(extract_kexam_attempts_from_modal_html(html, session.base_url)):
                attempt_key = kexam_attempt_probe_key(attempt)
                if attempt_key and attempt_key in seen_attempt_keys:
                    continue
                if attempt_key:
                    seen_attempt_keys.add(attempt_key)
                should_fetch_record = include_unsubmitted_records or attempt.submitted_status != "unsubmitted"
                if pacer and should_fetch_record:
                    pacer.sleep("requests:kexam_record")
                attempt_probe = probe_kexam_attempt_requests(
                    session,
                    attempt,
                    item.kind,
                    include_unsubmitted_records=include_unsubmitted_records,
                )
                attempt_probe.provenance_steps.insert(0, step)
                attempt_probes.append(attempt_probe)
        if probe.availability == "available":
            probe.attempt_probes = attempt_probes
            return probe
        if probe.score or len(probe.raw_summary) > len(best.raw_summary):
            best = probe
            best.provenance_steps.append(step)

    if attempt_probes:
        available_attempts = [probe for probe in attempt_probes if probe.question_records]
        best = available_attempts[0] if available_attempts else best
        best.attempt_probes = attempt_probes
        return best

    if not html_parts:
        best.reason = "requests probe has no direct activity or result endpoint to fetch"
        best.issues.append(
            ExportIssue(
                "requests_activity_endpoint_unavailable",
                "No direct activity detail URL or result modal URL was available for requests probing.",
                "info",
            )
        )
    return best


def probe_kexam_attempt_requests(
    session: TmsSession,
    attempt: KExamAttempt,
    item_kind: ItemKind | str = ItemKind.QUIZ,
    include_unsubmitted_records: bool = False,
) -> ActivityProbe:
    if attempt.submitted_status == "unsubmitted" and not include_unsubmitted_records:
        return ActivityProbe(
            score=attempt.score,
            attempt_at=attempt.attempt_at,
            raw_summary=attempt.raw_summary,
            availability="unavailable",
            reason="unsubmitted kexam record is not a verified answer",
            provenance_steps=[f"kexam_record_skipped:{attempt.record_id or ''}"],
            attempt=attempt,
            issues=[
                ExportIssue(
                    "unsubmitted_record_unverified",
                    "KExam record was marked 未繳交 and was not used as a verified answer.",
                    "info",
                )
            ],
        )

    try:
        html = session.fetch_activity_html_requests(attempt.record_url)
    except Exception as exc:
        return ActivityProbe(
            score=attempt.score,
            attempt_at=attempt.attempt_at,
            raw_summary=attempt.raw_summary,
            availability="unavailable",
            reason=str(exc),
            provenance_steps=[f"kexam_record_error:{attempt.record_id or ''}"],
            attempt=attempt,
            issues=[ExportIssue("record_page_unavailable", str(exc))],
        )

    probe = extract_activity_probe_from_html(html, item_kind)
    if not probe.question_records:
        json_probe = extract_kexam_record_probe_from_json_html(html, item_kind, attempt)
        if json_probe.question_records:
            probe = json_probe
    probe.attempt = attempt
    probe.score = probe.score or attempt.score
    probe.attempt_at = probe.attempt_at or attempt.attempt_at
    probe.raw_summary = probe.raw_summary or attempt.raw_summary or normalize_text(html)[:1000]
    probe.provenance_steps.append(f"kexam_record:{attempt.record_id or ''}")
    if probe.question_records:
        probe.availability = "available"
        probe.reason = "kexam record page exposed question and answer DOM through requests"
    else:
        probe.reason = "kexam record page did not expose parseable question DOM through requests"
        probe.issues.append(ExportIssue("record_questions_unavailable", probe.reason))
    return probe


def extract_kexam_record_probe_from_json_html(
    html: str,
    item_kind: ItemKind | str = ItemKind.QUIZ,
    attempt: KExamAttempt | None = None,
) -> ActivityProbe:
    payload = _extract_kexam_record_payload(html)
    if not payload:
        return ActivityProbe(
            availability="unavailable",
            reason="kexam record JSON payload was not available",
        )

    record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
    question_data = payload.get("questionData") if isinstance(payload.get("questionData"), dict) else {}
    questions = _questions_from_kexam_record_payload(question_data)
    summary = normalize_text(BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True))[:1000]
    score = _first_text(record, "score", "getScore", "totalScore", "resultScore") or (attempt.score if attempt else None)
    attempt_at = _first_text(record, "submitTime", "finishTime", "endTime", "createTime") or (
        attempt.attempt_at if attempt else None
    )
    if questions:
        return ActivityProbe(
            question_records=questions,
            score=score,
            attempt_at=attempt_at,
            raw_summary=summary or (attempt.raw_summary if attempt else ""),
            availability="available",
            reason="kexam record page exposed question and answer JSON through requests",
            attempt=attempt,
        )
    return ActivityProbe(
        score=score,
        attempt_at=attempt_at,
        raw_summary=summary or (attempt.raw_summary if attempt else ""),
        availability="unavailable",
        reason=(
            "kexam record JSON did not contain question data"
            if ItemKind(item_kind) == ItemKind.QUIZ
            else "submitted answers were not available in kexam JSON"
        ),
        attempt=attempt,
        issues=[ExportIssue("record_questions_unavailable", "KExam record JSON did not contain question data.")],
    )


def _extract_kexam_record_payload(html: str) -> dict[str, Any] | None:
    match = re.search(
        r"""fs\.kexamRecord\.setData\(\s*['"][^'"]+['"]\s*,\s*JSON\.parse\('(?P<payload>(?:\\.|[^'])*)'\)\s*\)""",
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


def _questions_from_kexam_record_payload(question_data: dict[str, Any]) -> list[ExtractedQuestion]:
    questions: list[ExtractedQuestion] = []
    for key, row in question_data.items():
        if not isinstance(row, dict):
            continue
        options = [_kexam_option_text(option) for option in row.get("option") or []]
        display_options = _display_kexam_options(options)
        answer_record = row.get("record") if isinstance(row.get("record"), dict) else {}
        selected_value = _decode_kexam_answer(answer_record.get("userAnswer"))
        correct_value = _decode_kexam_answer(row.get("answer"))
        selected_answers = _answer_labels(selected_value, display_options)
        correct_answers = _answer_labels(correct_value, display_options)
        is_correct = _truthy(answer_record.get("isCorrect"))
        incorrect_answers = []
        if selected_answers and (correct_answers or answer_record.get("isCorrect") is not None):
            if not is_correct or (correct_answers and set(selected_answers) != set(correct_answers)):
                incorrect_answers = selected_answers
        text = _html_to_text(str(row.get("questionTitle") or "")) or str(row.get("id") or key)
        if not text or not display_options:
            continue
        questions.append(
            ExtractedQuestion(
                text=text,
                options=display_options,
                selected_answers=selected_answers,
                correct_answers=correct_answers,
                incorrect_answers=incorrect_answers,
                question_type=_kexam_question_type(row),
                confidence=0.9 if selected_answers else 0.75,
            )
        )
    return questions


def _decode_kexam_answer(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return int(stripped) if stripped.isdigit() else stripped
        return _decode_kexam_answer(decoded)
    if isinstance(value, dict):
        if "answer" in value:
            return _decode_kexam_answer(value.get("answer"))
        for key in ("index", "value", "text", "label"):
            if key in value:
                return _decode_kexam_answer(value.get(key))
        return value
    return value


def _answer_labels(value: Any, options: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        labels: list[str] = []
        for item in value:
            labels.extend(_answer_labels(item, options))
        return labels
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return [options[value]] if 0 <= value < len(options) else []
    if isinstance(value, str):
        normalized = normalize_text(value)
        if normalized.isdigit():
            return _answer_labels(int(normalized), options)
        for option in options:
            if normalized == normalize_text(option):
                return [option]
        return [normalized] if normalized else []
    return []


def _display_kexam_options(options: list[str]) -> list[str]:
    if len(options) > 2 and not any(re.match(r"^[A-Z]\.", normalize_text(option)) for option in options):
        return [f"{chr(ord('A') + index)}. {option}" for index, option in enumerate(options)]
    return list(options)


def _kexam_option_text(option: Any) -> str:
    if isinstance(option, dict):
        for key in ("text", "label", "title", "value", "name"):
            value = option.get(key)
            if value is not None and str(value).strip():
                return normalize_text(str(value))
        return normalize_text(" ".join(str(value) for value in option.values() if value))
    return normalize_text(str(option))


def _kexam_question_type(row: dict[str, Any]) -> str:
    answer = _decode_kexam_answer(row.get("answer"))
    if isinstance(answer, list):
        return "multiple_choice"
    return "single_choice"


def _html_to_text(value: str) -> str:
    return normalize_text(BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True))


def _first_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return normalize_text(str(value))
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是", "正確"}
    return False


def classify_activity_form_requests(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
) -> ActivityFormClassification:
    from .playwright_form_validation import classify_form_html

    issues: list[ExportIssue] = []
    entry_method: str | None = None
    html = ""
    if item.detail_url:
        try:
            html = session.fetch_activity_html_requests(item.detail_url, referer=course.url)
            entry_method = "detail_url"
        except Exception as exc:
            issues.append(ExportIssue("activity_detail_unavailable", str(exc)))
    if not html and item.metadata.get("result_modal_url"):
        try:
            html = session.fetch_activity_html_requests(str(item.metadata["result_modal_url"]), referer=course.url)
            entry_method = "result_modal_url"
        except Exception as exc:
            issues.append(ExportIssue("result_endpoint_unavailable", str(exc)))
    if not html:
        classification, fields, questions = classify_form_html("", item.kind)
        issues.append(
            ExportIssue(
                "requests_activity_endpoint_unavailable",
                "No direct activity detail URL or result modal URL was available for form classification.",
                "info",
            )
        )
    else:
        classification, fields, questions = classify_form_html(html, item.kind)
    return ActivityFormClassification(
        classification=classification,
        entry_method=entry_method,
        fields=fields,
        question_count=len(questions),
        selected_answer_count=sum(len(question.selected_answers) for question in questions),
        issues=issues,
    )


__all__ = [
    "ActivityFormClassification",
    "classify_activity_form_requests",
    "extract_kexam_record_probe_from_json_html",
    "probe_activity_requests",
    "probe_kexam_attempt_requests",
]
