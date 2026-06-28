from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping
from urllib.parse import urlparse


ReproductionStatus = Literal["requests_reproducible", "requests_partial", "requests_blocked"]


@dataclass(slots=True)
class RequestsReproductionFeature:
    feature: str
    status: ReproductionStatus
    evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    url_patterns: list[str] = field(default_factory=list)
    method_counts: dict[str, int] = field(default_factory=dict)
    post_data_keys: list[str] = field(default_factory=list)
    response_json_keys: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RequestsReproductionReport:
    source_path: str | None
    observation_count: int
    features: list[RequestsReproductionFeature] = field(default_factory=list)


def load_network_observations(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    observations: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                observations.append(payload)
    return observations


def analyze_requests_reproduction_file(path: str | Path) -> RequestsReproductionReport:
    observations = load_network_observations(path)
    return analyze_requests_reproduction_observations(observations, source_path=str(path))


def analyze_requests_reproduction_observations(
    observations: Iterable[Mapping[str, Any]],
    source_path: str | None = None,
) -> RequestsReproductionReport:
    rows = [dict(row) for row in observations]
    return RequestsReproductionReport(
        source_path=source_path,
        observation_count=len(rows),
        features=[
            _analyze_reading_video(rows),
            _analyze_survey(rows),
            _analyze_quiz(rows),
            _analyze_question_bank_history(rows),
            _analyze_form_classification(rows),
        ],
    )


def format_requests_reproduction_markdown(report: RequestsReproductionReport) -> str:
    lines = [
        "# Requests Reproduction Analysis",
        "",
        f"- Source: {report.source_path or 'in-memory observations'}",
        f"- Observations: {report.observation_count}",
        "",
        "| Feature | Status | Evidence | Missing evidence | URL patterns |",
        "|---|---|---|---|---|",
    ]
    for feature in report.features:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(feature.feature),
                    _cell(f"`{feature.status}`"),
                    _cell(feature.evidence or [""]),
                    _cell(feature.missing_evidence or [""]),
                    _cell(feature.url_patterns or [""]),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def feature_map(report: RequestsReproductionReport) -> dict[str, RequestsReproductionFeature]:
    return {feature.feature: feature for feature in report.features}


def _analyze_reading_video(rows: list[dict[str, Any]]) -> RequestsReproductionFeature:
    subset = _rows_for_feature(rows, "reading_video_completion", ("reading", "video"))
    evidence: list[str] = []
    missing: list[str] = []
    if _has_activity_get(subset):
        evidence.append("activity_get")
    else:
        missing.append("activity_get")
    if _has_endpoint_path(
        subset,
        ("heartbeat", "progress", "elapsed", "duration", "readtime", "read_time", "watchtime", "watch_time", "timer"),
    ):
        evidence.append("start_or_heartbeat_endpoint")
    else:
        missing.append("start_or_heartbeat_endpoint")
    if _has_progress_increase_summary(subset):
        evidence.append("course_detail_progress_increased")
    if _has_endpoint_path(subset, ("finish", "complete", "completed", "done", "readfinish", "read_complete")) or _has_action(
        subset, "read-finish:verification"
    ):
        evidence.append("finish_endpoint")
    else:
        missing.append("finish_endpoint")
    if _has_verified_summary(subset):
        evidence.append("course_detail_verification")
    else:
        missing.append("course_detail_verification")
    status: ReproductionStatus
    if {"finish_endpoint", "course_detail_verification"}.issubset(evidence) and "activity_get" in evidence:
        status = "requests_reproducible"
    elif evidence:
        status = "requests_partial"
    else:
        status = "requests_blocked"
    return _feature("reading_video_completion", status, subset, evidence, missing)


def _analyze_survey(rows: list[dict[str, Any]]) -> RequestsReproductionFeature:
    subset = _rows_for_feature(rows, "survey_submission", ("survey",))
    return _analyze_form_submit("survey_submission", subset)


def _analyze_quiz(rows: list[dict[str, Any]]) -> RequestsReproductionFeature:
    subset = _rows_for_feature(rows, "quiz_submission", ("quiz", "kexam"))
    return _analyze_form_submit("quiz_submission", subset)


def _analyze_form_submit(feature: str, rows: list[dict[str, Any]]) -> RequestsReproductionFeature:
    evidence: list[str] = []
    missing: list[str] = []
    if _has_activity_get(rows) or _has_action(rows, "form-open"):
        evidence.append("form_open")
    else:
        missing.append("form_open")
    if _has_form_metadata(rows):
        evidence.append("rendered_form_metadata")
    else:
        missing.append("rendered_form_metadata")
    if _has_submit_post(rows):
        evidence.append("submit_endpoint")
    else:
        missing.append("submit_endpoint")
    if _has_token_keys(rows):
        evidence.append("token_or_hidden_fields")
    else:
        missing.append("token_or_hidden_fields")
    if _has_verified_summary(rows):
        evidence.append("course_detail_verification")
    else:
        missing.append("course_detail_verification")
    if {"submit_endpoint", "token_or_hidden_fields", "course_detail_verification"}.issubset(evidence):
        status: ReproductionStatus = "requests_reproducible"
    elif evidence:
        status = "requests_partial"
    else:
        status = "requests_blocked"
    return _feature(feature, status, rows, evidence, missing)


def _analyze_question_bank_history(rows: list[dict[str, Any]]) -> RequestsReproductionFeature:
    subset = _rows_for_feature(rows, "question_bank_history_export", ("quiz", "survey", "kexam", "record", "result"))
    evidence: list[str] = []
    missing: list[str] = []
    if _has_keyword(subset, ("result", "learningitem", "activity")):
        evidence.append("result_or_activity_endpoint")
    else:
        missing.append("result_or_activity_endpoint")
    if _has_keyword(subset, ("kexam", "record")):
        evidence.append("kexam_record_endpoint")
    else:
        missing.append("kexam_record_endpoint")
    if _has_keyword(subset, ("question", "answer", "selected", "correct")) or _has_action(subset, "rendered-form-metadata"):
        evidence.append("question_answer_metadata")
    else:
        missing.append("question_answer_metadata")
    if {"result_or_activity_endpoint", "kexam_record_endpoint"}.issubset(evidence):
        status: ReproductionStatus = "requests_reproducible"
    elif evidence:
        status = "requests_partial"
    else:
        status = "requests_blocked"
    return _feature("question_bank_history_export", status, subset, evidence, missing)


def _analyze_form_classification(rows: list[dict[str, Any]]) -> RequestsReproductionFeature:
    subset = _rows_for_feature(rows, "form_validation_classification", ("survey", "quiz", "kexam", "form"))
    evidence: list[str] = []
    missing: list[str] = []
    if _has_activity_get(subset) or _has_action(subset, "form-open"):
        evidence.append("activity_or_form_html")
    else:
        missing.append("activity_or_form_html")
    if _has_form_metadata(subset):
        evidence.append("rendered_form_metadata")
    else:
        missing.append("rendered_form_metadata")
    if evidence:
        status: ReproductionStatus = "requests_partial"
    else:
        status = "requests_blocked"
    return _feature("form_validation_classification", status, subset, evidence, missing)


def _feature(
    name: str,
    status: ReproductionStatus,
    rows: list[dict[str, Any]],
    evidence: list[str],
    missing: list[str],
) -> RequestsReproductionFeature:
    return RequestsReproductionFeature(
        feature=name,
        status=status,
        evidence=evidence,
        missing_evidence=missing,
        url_patterns=_url_patterns(rows),
        method_counts=_method_counts(rows),
        post_data_keys=_all_post_keys(rows),
        response_json_keys=_all_response_json_keys(rows),
    )


def _rows_for_feature(rows: list[dict[str, Any]], feature: str, tokens: tuple[str, ...]) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        text = _row_text(row)
        action = str(row.get("action") or "").lower()
        if feature in action or any(token in text for token in tokens):
            selected.append(row)
    return selected


def _has_activity_get(rows: list[dict[str, Any]]) -> bool:
    return any(str(row.get("method") or "").upper() == "GET" and row.get("url") for row in rows)


def _has_submit_post(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        method = str(row.get("method") or "").upper()
        if method not in {"POST", "PUT", "PATCH"}:
            continue
        if _has_keyword([row], ("submit", "save", "finish", "complete", "survey", "quiz", "kexam", "answer")):
            return True
        if row.get("post_data_keys"):
            return True
    return False


def _has_token_keys(rows: list[dict[str, Any]]) -> bool:
    return _has_keyword(rows, ("anticsrf", "csrf", "ajaxauth", "token", "recordid", "activityid")) or any(
        row.get("post_data_keys") for row in rows
    )


def _has_form_metadata(rows: list[dict[str, Any]]) -> bool:
    return _has_action(rows, "rendered-form-metadata") or _has_keyword(
        rows,
        ("radio_groups", "checkbox_groups", "text_fields", "hidden_fields", "submit_buttons"),
    )


def _has_verified_summary(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        summary = row.get("response_json_summary")
        if isinstance(summary, dict) and summary.get("verified") is True:
            return True
        if "verification" in str(row.get("action") or "").lower() and _has_keyword([row], ("verified", "passed", "success")):
            return True
    return False


def _has_progress_increase_summary(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        summary = row.get("response_json_summary")
        if isinstance(summary, dict) and summary.get("progress_increased") is True:
            return True
    return False


def _has_action(rows: list[dict[str, Any]], token: str) -> bool:
    token = token.lower()
    return any(token in str(row.get("action") or "").lower() for row in rows)


def _has_keyword(rows: list[dict[str, Any]], tokens: tuple[str, ...]) -> bool:
    return any(any(token.lower() in _row_text(row) for token in tokens) for row in rows)


def _has_endpoint_path(rows: list[dict[str, Any]], tokens: tuple[str, ...]) -> bool:
    lowered = tuple(token.lower() for token in tokens)
    for row in rows:
        parsed = urlparse(str(row.get("url") or ""))
        path = parsed.path.lower()
        if path.startswith("/res/") or path.startswith("/sys/res/") or "/cdn-cgi/" in path:
            continue
        if any(token in path for token in lowered):
            return True
    return False


def _row_text(row: Mapping[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True).lower()


def _url_patterns(rows: list[dict[str, Any]]) -> list[str]:
    patterns = sorted({_url_pattern(str(row.get("url") or "")) for row in rows if row.get("url")})
    return [pattern for pattern in patterns if pattern]


def _url_pattern(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or url
    path = re.sub(r"/\d+(?=/|$)", "/{id}", path)
    path = re.sub(r"recordID=[^&]+", "recordID={value}", path, flags=re.IGNORECASE)
    return path or parsed.netloc


def _method_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        method = str(row.get("method") or "").upper()
        if not method:
            continue
        counts[method] = counts.get(method, 0) + 1
    return counts


def _all_post_keys(rows: list[dict[str, Any]]) -> list[str]:
    keys: set[str] = set()
    for row in rows:
        for key in row.get("post_data_keys") or []:
            keys.add(str(key))
    return sorted(keys)


def _all_response_json_keys(rows: list[dict[str, Any]]) -> list[str]:
    keys: set[str] = set()
    for row in rows:
        summary = row.get("response_json_summary")
        if isinstance(summary, dict):
            for key in summary.get("keys") or []:
                keys.add(str(key))
    return sorted(keys)


def _cell(value: str | list[str]) -> str:
    if isinstance(value, list):
        text = "<br>".join(value)
    else:
        text = value
    return text.replace("|", "\\|").replace("\n", "<br>")


__all__ = [
    "ReproductionStatus",
    "RequestsReproductionFeature",
    "RequestsReproductionReport",
    "analyze_requests_reproduction_file",
    "analyze_requests_reproduction_observations",
    "feature_map",
    "format_requests_reproduction_markdown",
    "load_network_observations",
]
