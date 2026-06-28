from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .parsers import normalize_text
from .privacy import redact_sensitive_url, redact_sensitive_value
from .quiz import dated_question_bank_filename

DEFAULT_REFERENCE_BANK_JSONL_PATH = dated_question_bank_filename()
DEFAULT_REFERENCE_BANK_MARKDOWN_PATH: str | None = None
REFERENCE_BANK_SCHEMA_VERSION = "tms-vghks-shared-question-bank.v2"

FORBIDDEN_SHARED_BANK_KEYS = {
    "account",
    "api_key",
    "authorization",
    "captcha",
    "cookie",
    "cookies",
    "gemini_api_key",
    "google_api_key",
    "headers",
    "hidden_fields",
    "ocr_raw_response",
    "paddleocr_api_token",
    "password",
    "raw_html",
    "raw_response",
    "raw_summary",
    "response_json",
    "response_text",
    "session",
    "session_state",
    "source_account_label",
    "storage_state",
}

SHARED_BANK_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", re.IGNORECASE),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    "authorization_header": re.compile(r"\bauthorization\s*[:=]\s*(?:basic|bearer)\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    "cookie": re.compile(r"\b(?:PHPSESSID|laravel_session|set-cookie)\b", re.IGNORECASE),
    "password_assignment": re.compile(r"\bpassword\s*[:=]\s*(?!REDACTED\b)[^,\s\"'}]+", re.IGNORECASE),
    "unredacted_query_secret": re.compile(
        r"(?i)([?&](?:ajaxAuth|key|token|auth|authorization|sig|signature|userID|userId)=)(?!REDACTED(?:[&\"'\s]|$))[^&\"'\s]+"
    ),
}


@dataclass(slots=True)
class ReferenceQuestionBankResult:
    output_path: str
    markdown_path: str | None
    history_record_count: int
    reference_record_count: int
    ai_suggestion_count: int
    posttest_ai_suggestion_count: int
    pretest_record_count: int
    posttest_record_count: int
    skipped_untrusted_count: int
    issue_count: int
    issues: list[dict[str, str]] = field(default_factory=list)


def build_reference_question_bank(
    history_jsonl: str | Path,
    output_jsonl: str | Path = DEFAULT_REFERENCE_BANK_JSONL_PATH,
    output_markdown: str | Path | None = DEFAULT_REFERENCE_BANK_MARKDOWN_PATH,
    ai_suggestions_jsonl: str | Path | None = None,
    posttest_ai_policy: str = "trusted",
) -> ReferenceQuestionBankResult:
    history_rows = read_jsonl(history_jsonl)
    suggestions = read_ai_suggestions(ai_suggestions_jsonl)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    issues: list[dict[str, str]] = []
    for row in history_rows:
        stage = classify_activity_stage(row.get("activity", {}).get("title"))
        if stage == "survey":
            continue
        merge_key = row.get("assessment", {}).get("merge_key") or row.get("question", {}).get("merge_key")
        if not merge_key:
            issues.append({"code": "missing_merge_key", "message": "history row was skipped because merge_key was missing"})
            continue
        grouped.setdefault((stage, str(merge_key)), []).append(row)

    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    records: list[dict[str, Any]] = []
    skipped_untrusted = 0
    for (stage, merge_key), rows in grouped.items():
        record = build_reference_record_for_group(
            rows=rows,
            stage=stage,
            merge_key=merge_key,
            suggestion=suggestions.get(merge_key),
            created_at=created_at,
            posttest_ai_policy=posttest_ai_policy,
        )
        if record:
            records.append(record)
        else:
            skipped_untrusted += 1

    safe_records: list[dict[str, Any]] = []
    privacy_issues: list[str] = []
    for index, record in enumerate(records):
        safe_record = redact_sensitive_value(record)
        record_issues = shared_bank_privacy_issues(safe_record)
        if record_issues:
            privacy_issues.extend(f"record[{index}]:{issue}" for issue in record_issues)
        safe_records.append(safe_record)
    if privacy_issues:
        issue_text = ", ".join(privacy_issues[:5])
        if len(privacy_issues) > 5:
            issue_text += f", ...(+{len(privacy_issues) - 5})"
        raise ValueError(f"shared question bank privacy guard failed: {issue_text}")

    output = Path(output_jsonl)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in safe_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    if output_markdown:
        markdown = Path(output_markdown)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(render_reference_markdown(safe_records), encoding="utf-8")

    return ReferenceQuestionBankResult(
        output_path=str(output),
        markdown_path=str(output_markdown) if output_markdown else None,
        history_record_count=len(history_rows),
        reference_record_count=len(safe_records),
        ai_suggestion_count=len(suggestions),
        posttest_ai_suggestion_count=sum(1 for record in safe_records if record["answer"]["status"] == "ai_suggested_trusted"),
        pretest_record_count=sum(1 for record in safe_records if record["quiz_stage"] == "pretest"),
        posttest_record_count=sum(1 for record in safe_records if record["quiz_stage"] == "posttest"),
        skipped_untrusted_count=skipped_untrusted,
        issue_count=len(issues),
        issues=issues,
    )


def build_reference_record_for_group(
    rows: list[dict[str, Any]],
    stage: str,
    merge_key: str,
    suggestion: dict[str, Any] | None,
    created_at: str,
    posttest_ai_policy: str,
) -> dict[str, Any] | None:
    best_verified = best_row(rows, {"verified_correct"})
    if best_verified:
        return reference_record_from_history(best_verified, stage, merge_key, "verified_correct", True, created_at)

    if stage == "posttest":
        if suggestion and posttest_ai_policy == "trusted":
            base = best_row(rows, {"unverified_selected"}) or rows[0]
            return reference_record_from_suggestion(base, stage, merge_key, suggestion, created_at)
        return None

    if stage == "pretest":
        base = best_row(rows, {"unverified_selected", "verified_wrong"}) or rows[0]
        if selected_answers(base):
            return reference_record_from_history(base, stage, merge_key, "pretest_historical_selected", True, created_at)
        return None

    if suggestion and posttest_ai_policy == "trusted":
        base = best_row(rows, {"unverified_selected"}) or rows[0]
        return reference_record_from_suggestion(base, stage, merge_key, suggestion, created_at)
    return None


def reference_record_from_history(
    row: dict[str, Any],
    stage: str,
    merge_key: str,
    status: str,
    trusted_for_auto: bool,
    created_at: str,
) -> dict[str, Any]:
    answers = selected_answers(row)
    return {
        "schema_version": REFERENCE_BANK_SCHEMA_VERSION,
        "created_at": created_at,
        "source_system": row.get("source_system", "tms.vghks.gov.tw"),
        "quiz_stage": stage,
        "course": shared_course(row),
        "activity": shared_activity(row),
        "question": shared_question(row, merge_key),
        "answer": compact_dict({
            "answers": answers,
            "selected_answers": answers,
            "status": status,
            "trusted_for_auto": trusted_for_auto,
            "source": "history",
            "score": row.get("answer", {}).get("score") or row.get("assessment", {}).get("score"),
            "attempt_at": row.get("answer", {}).get("attempt_at") or row.get("attempt", {}).get("attempt_at"),
            "confidence": row.get("assessment", {}).get("confidence", 0.8),
        }),
        "assessment": shared_assessment(row, status, merge_key),
        "attempt": shared_attempt(row),
        "provenance": shared_provenance(row),
    }


def reference_record_from_suggestion(
    row: dict[str, Any],
    stage: str,
    merge_key: str,
    suggestion: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    options = row.get("question", {}).get("options") or []
    answers = [answer for answer in suggestion.get("answers", []) if answer in options]
    return {
        "schema_version": REFERENCE_BANK_SCHEMA_VERSION,
        "created_at": created_at,
        "source_system": row.get("source_system", "tms.vghks.gov.tw"),
        "quiz_stage": stage,
        "course": shared_course(row),
        "activity": shared_activity(row),
        "question": shared_question(row, merge_key, options=options),
        "answer": compact_dict({
            "answers": answers,
            "selected_answers": answers,
            "status": "ai_suggested_trusted",
            "trusted_for_auto": True,
            "source": "subagent",
            "score": row.get("answer", {}).get("score") or row.get("assessment", {}).get("score"),
            "attempt_at": row.get("answer", {}).get("attempt_at") or row.get("attempt", {}).get("attempt_at"),
            "confidence": suggestion.get("confidence", 0.5),
        }),
        "assessment": shared_assessment(row, "ai_suggested_trusted", merge_key),
        "attempt": shared_attempt(row),
        "provenance": compact_dict({**shared_provenance(row), "suggestion_reason": suggestion.get("reason", "")}),
    }


def shared_course(row: dict[str, Any]) -> dict[str, Any]:
    course = row.get("course", {}) if isinstance(row.get("course"), dict) else {}
    return compact_dict(
        {
            "course_id": course.get("course_id"),
            "title": course.get("title"),
            "completed_at": course.get("completed_at"),
        }
    )


def shared_activity(row: dict[str, Any]) -> dict[str, Any]:
    activity = row.get("activity", {}) if isinstance(row.get("activity"), dict) else {}
    return compact_dict(
        {
            "activity_id": activity.get("activity_id"),
            "order": activity.get("order"),
            "kind": activity.get("kind"),
            "title": activity.get("title"),
            "deadline": activity.get("deadline"),
            "pass_condition": activity.get("pass_condition"),
            "result": activity.get("result"),
            "passed": activity.get("passed"),
        }
    )


def shared_question(row: dict[str, Any], merge_key: str, options: list[str] | None = None) -> dict[str, Any]:
    question = row.get("question", {}) if isinstance(row.get("question"), dict) else {}
    text = question.get("text")
    return compact_dict(
        {
            "text": text,
            "options": options if options is not None else question.get("options") or [],
            "merge_key": merge_key,
            "type": question.get("type"),
            "normalized_text": question.get("normalized_text") or (normalize_text(text).lower() if text else None),
            "correct_answers": question.get("correct_answers") or [],
            "incorrect_answers": question.get("incorrect_answers") or [],
        }
    )


def shared_assessment(row: dict[str, Any], status: str, merge_key: str) -> dict[str, Any]:
    assessment = row.get("assessment", {}) if isinstance(row.get("assessment"), dict) else {}
    answer = row.get("answer", {}) if isinstance(row.get("answer"), dict) else {}
    activity = row.get("activity", {}) if isinstance(row.get("activity"), dict) else {}
    return compact_dict(
        {
            "type": assessment.get("type") or activity.get("kind"),
            "score": answer.get("score") or assessment.get("score"),
            "passing_condition": assessment.get("passing_condition") or activity.get("pass_condition"),
            "answer_status": status,
            "verification_method": assessment.get("verification_method"),
            "confidence": assessment.get("confidence"),
            "merge_key": merge_key,
            "is_canonical": assessment.get("is_canonical"),
        }
    )


def shared_attempt(row: dict[str, Any]) -> dict[str, Any]:
    attempt = row.get("attempt", {}) if isinstance(row.get("attempt"), dict) else {}
    record_url = attempt.get("record_url")
    return compact_dict(
        {
            "exam_id": attempt.get("exam_id"),
            "record_id": attempt.get("record_id"),
            "record_url": redact_sensitive_url(str(record_url)) if record_url else None,
            "attempt_at": attempt.get("attempt_at") or row.get("answer", {}).get("attempt_at"),
            "score": attempt.get("score") or row.get("answer", {}).get("score"),
            "submitted_status": attempt.get("submitted_status"),
        }
    )


def shared_provenance(row: dict[str, Any]) -> dict[str, Any]:
    provenance = row.get("provenance", {}) if isinstance(row.get("provenance"), dict) else {}
    assessment = row.get("assessment", {}) if isinstance(row.get("assessment"), dict) else {}
    answer = row.get("answer", {}) if isinstance(row.get("answer"), dict) else {}
    return compact_dict(
        {
            "collector": provenance.get("collector"),
            "method": provenance.get("method"),
            "history_status": answer.get("status"),
            "verification_method": assessment.get("verification_method"),
            "availability_reason": provenance.get("availability_reason"),
        }
    )


def compact_dict(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}


def shared_bank_privacy_issues(record: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for key_path in forbidden_key_paths(record):
        issues.append(f"forbidden_key:{key_path}")
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
    for name, pattern in SHARED_BANK_SECRET_PATTERNS.items():
        if pattern.search(payload):
            issues.append(f"secret_pattern:{name}")
    return issues


def forbidden_key_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if key_text.lower() in FORBIDDEN_SHARED_BANK_KEYS:
                paths.append(path)
            paths.extend(forbidden_key_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(forbidden_key_paths(item, f"{prefix}[{index}]"))
    return paths


def classify_activity_stage(activity_title: str | None) -> str:
    title = normalize_text(activity_title)
    if "問卷" in title:
        return "survey"
    if "課前" in title:
        return "pretest"
    if "課後" in title:
        return "posttest"
    return "unknown"


def best_row(rows: list[dict[str, Any]], statuses: set[str]) -> dict[str, Any] | None:
    candidates = [row for row in rows if row.get("answer", {}).get("status") in statuses and selected_answers(row)]
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            bool(row.get("assessment", {}).get("is_canonical")),
            parse_float(row.get("answer", {}).get("score") or row.get("assessment", {}).get("score")) or 0.0,
            str(row.get("answer", {}).get("attempt_at") or row.get("attempt", {}).get("attempt_at") or ""),
        ),
        reverse=True,
    )
    return candidates[0]


def selected_answers(row: dict[str, Any]) -> list[str]:
    return [str(answer) for answer in row.get("answer", {}).get("selected_answers") or row.get("answer", {}).get("answers") or []]


def read_ai_suggestions(path: str | Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    suggestions: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(path):
        merge_key = row.get("merge_key")
        if not merge_key:
            continue
        suggestions[str(merge_key)] = row
    return suggestions


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    rows: list[dict[str, Any]] = []
    if not source.exists():
        return rows
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def render_reference_markdown(records: list[dict[str, Any]]) -> str:
    lines = ["# TMS Reference Question Bank", ""]
    lines.append(f"- Records: {len(records)}")
    lines.append("")
    for record in records:
        lines.extend(
            [
                f"Course: {record.get('course', {}).get('title') or ''}",
                f"Item: {record.get('activity', {}).get('title') or ''}",
                f"Stage: {record.get('quiz_stage') or ''}",
                f"Status: {record.get('answer', {}).get('status') or ''}",
                f"Verified: {str(bool(record.get('answer', {}).get('trusted_for_auto'))).lower()}",
                f"Score: {record.get('answer', {}).get('score') or ''}",
                f"Question: {record.get('question', {}).get('text') or ''}",
                "Options: " + " | ".join(record.get("question", {}).get("options") or []),
                "Answer: " + " | ".join(record.get("answer", {}).get("answers") or []),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def parse_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value
