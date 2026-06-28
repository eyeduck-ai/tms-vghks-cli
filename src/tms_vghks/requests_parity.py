from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from .capabilities import capability_contracts_by_feature
from .models import CourseDetail, CourseItem, CourseSummary, OperationBackend
from .requests_reproduction import RequestsReproductionReport


ParityStatus = Literal["equivalent", "partially_equivalent", "not_equivalent_yet"]
BackendComparisonStatus = Literal["equivalent", "mismatch", "error", "skipped"]


@dataclass(slots=True)
class RequestsParityRow:
    feature: str
    playwright_entrypoints: list[str]
    requests_entrypoints: list[str]
    required_http_conditions: list[str]
    status: ParityStatus
    gaps: list[str]
    verification: list[str]
    scope: str = "read_only"


@dataclass(slots=True)
class RequestsParityReport:
    title: str
    summary: list[str]
    matrix: list[RequestsParityRow] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    diagnostic_evidence: list[dict[str, Any]] = field(default_factory=list)
    read_only_verification_commands: list[str] = field(default_factory=list)
    live_mutation_manual_protocol: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BackendComparisonRow:
    feature: str
    status: BackendComparisonStatus
    requests_count: int | None = None
    playwright_count: int | None = None
    mismatches: list[str] = field(default_factory=list)
    requests_sample: list[dict[str, Any]] = field(default_factory=list)
    playwright_sample: list[dict[str, Any]] = field(default_factory=list)
    requests_error: str = ""
    playwright_error: str = ""


@dataclass(slots=True)
class BackendComparisonReport:
    title: str
    status: BackendComparisonStatus
    summary: list[str]
    rows: list[BackendComparisonRow] = field(default_factory=list)
    detail_targets: list[str] = field(default_factory=list)


def compare_backend_read_paths(
    session: Any,
    course: str | None = None,
    detail_limit: int = 1,
    include_pending: bool = True,
    include_completed: bool = True,
) -> BackendComparisonReport:
    """Compare read-only requests and Playwright backend outputs from one authenticated session."""
    detail_limit = max(detail_limit, 0)
    rows: list[BackendComparisonRow] = []
    list_results: list[tuple[str, list[CourseSummary], list[CourseSummary]]] = []

    if include_pending:
        requests_pending, requests_error = _capture_call(
            lambda: _list_courses_for_backend(session, "pending", OperationBackend.REQUESTS)
        )
        playwright_pending, playwright_error = _capture_call(
            lambda: _list_courses_for_backend(session, "pending", OperationBackend.PLAYWRIGHT)
        )
        rows.append(
            _compare_course_lists(
                "pending_courses",
                requests_pending,
                playwright_pending,
                requests_error,
                playwright_error,
            )
        )
        if not requests_error and not playwright_error:
            list_results.append(("pending", requests_pending or [], playwright_pending or []))

    if include_completed:
        requests_completed, requests_error = _capture_call(
            lambda: _list_courses_for_backend(session, "completed", OperationBackend.REQUESTS)
        )
        playwright_completed, playwright_error = _capture_call(
            lambda: _list_courses_for_backend(session, "completed", OperationBackend.PLAYWRIGHT)
        )
        rows.append(
            _compare_course_lists(
                "completed_courses",
                requests_completed,
                playwright_completed,
                requests_error,
                playwright_error,
            )
        )
        if not requests_error and not playwright_error:
            list_results.append(("completed", requests_completed or [], playwright_completed or []))

    detail_targets = _select_detail_targets(course, detail_limit, list_results)
    for target in detail_targets:
        requests_detail, requests_error = _capture_call(
            lambda target=target: _get_course_detail_for_backend(session, target, OperationBackend.REQUESTS)
        )
        playwright_detail, playwright_error = _capture_call(
            lambda target=target: _get_course_detail_for_backend(session, target, OperationBackend.PLAYWRIGHT)
        )
        rows.append(
            _compare_course_details(
                f"course_detail:{target}",
                requests_detail,
                playwright_detail,
                requests_error,
                playwright_error,
            )
        )

    if not detail_targets and detail_limit > 0:
        rows.append(
            BackendComparisonRow(
                feature="course_detail",
                status="skipped",
                mismatches=["No course target was provided or discovered from comparable course lists."],
            )
        )

    status = _overall_backend_comparison_status(rows)
    return BackendComparisonReport(
        title="Live Requests vs Playwright Read Comparison",
        status=status,
        summary=_backend_comparison_summary(rows, status),
        rows=rows,
        detail_targets=detail_targets,
    )


def build_requests_parity_matrix() -> list[RequestsParityRow]:
    contracts = capability_contracts_by_feature()

    def contract(feature: str):
        return contracts[feature]

    return [
        RequestsParityRow(
            feature="login_session",
            playwright_entrypoints=list(contract("login_session").playwright_entrypoints),
            requests_entrypoints=list(contract("login_session").requests_entrypoints),
            required_http_conditions=[
                "Login CSRF/captcha fields are available from the login page.",
                "The requests session receives the same authenticated cookie bundle.",
                "Multi-login responses can be handled without browser-only interaction.",
            ],
            status=contract("login_session").status,
            gaps=[
                "Manual browser login remains useful when captcha or login UI behavior changes.",
            ],
            verification=[
                "Run requests account login and then list courses with the saved account session.",
                "Run Playwright OCR login for the same account and compare logged-in status plus course counts.",
            ],
        ),
        RequestsParityRow(
            feature="course_list_and_detail",
            playwright_entrypoints=list(contract("course_list_and_detail").playwright_entrypoints),
            requests_entrypoints=list(contract("course_list_and_detail").requests_entrypoints),
            required_http_conditions=[
                "Pending/completed/detail pages return parseable HTML to authenticated GET requests.",
                "Course rows expose the same ids, titles, progress, item states, results, and detail URLs.",
            ],
            status=contract("course_list_and_detail").status,
            gaps=[
                "If TMS moves fields behind client-side rendering, requests parsing may become partial.",
            ],
            verification=[
                "Compare requests and Playwright list outputs for pending and completed courses.",
                "Compare requests and Playwright detail outputs for at least one representative course.",
            ],
        ),
        RequestsParityRow(
            feature="reading_video_completion",
            playwright_entrypoints=list(contract("reading_video_completion").playwright_entrypoints),
            requests_entrypoints=list(contract("reading_video_completion").requests_entrypoints),
            required_http_conditions=[
                "The course detail activity button exposes a normal checkPassPrevious AJAX entrypoint or a direct media URL.",
                "The media HTML contains ReadLog recordUrl with logID, timing, _lock, ajaxAuth, and recordTime.",
                "The runner replays watchTime in recordTime-sized intervals for long waits.",
                "Course detail confirms result_seconds increased after the requests flow.",
            ],
            status=contract("reading_video_completion").status,
            gaps=[
                "Verified for ReadLog/watchTime reading/video accumulation, not for every possible media template.",
                "Already-passed reading/video rows short-circuit without watchTime POST in normal automation; diagnostics can force a watchTime POST.",
                "Browser-only focus, audit, duplicate-session, or alternate completion flows still require Playwright fallback.",
            ],
            verification=[
                "Run diag reading-playwright or diag network to confirm the page uses watchTime.",
                "Run diag reading and verify course detail result_seconds increases; add --force-watch-time for already-passed diagnostic checks.",
            ],
            scope=contract("reading_video_completion").scope,
        ),
        RequestsParityRow(
            feature="survey_submission",
            playwright_entrypoints=list(contract("survey_submission").playwright_entrypoints),
            requests_entrypoints=list(contract("survey_submission").requests_entrypoints),
            required_http_conditions=[
                "The direct activity HTML exposes all fillable fields, hidden fields, and submit action.",
                "CSRF/ajax tokens can be derived from normal HTML or captured network metadata.",
                "Submitted answers can be verified from refreshed course detail.",
            ],
            status=contract("survey_submission").status,
            gaps=[
                "Requests survey submit can auto-attempt normal HTML form POSTs, but is not fully equivalent to Playwright rendered-DOM submission.",
                "Rendered-only fields or JavaScript-mutated state may be missing from raw HTML.",
                "Contenteditable survey fields remain Playwright-only.",
            ],
            verification=[
                "Capture or compare the same survey through Playwright rendered DOM and requests raw HTML.",
                "Run diag forms --probe-only --candidate-limit N on completed courses before any live submit attempt.",
                "Run diag forms against an explicitly authorized survey and verify course detail.",
            ],
            scope=contract("survey_submission").scope,
        ),
        RequestsParityRow(
            feature="quiz_submission",
            playwright_entrypoints=list(contract("quiz_submission").playwright_entrypoints),
            requests_entrypoints=list(contract("quiz_submission").requests_entrypoints),
            required_http_conditions=[
                "The quiz form HTML exposes questions, options, required groups, hidden fields, and submit action.",
                "The answer payload and tokens are captured from a normal browser submit.",
                "The result page or course detail verifies pass/fail after submit.",
                "For KExam, the exam page exposes takeExam, confirmRecord, submitExam, and record-list endpoints.",
            ],
            status=contract("quiz_submission").status,
            gaps=[
                "KExam record read, answer-record parsing, read-only completed-course probe, and resubmit verification are live-verified in requests.",
                "Generic non-KExam quiz submit remains limited to raw HTML standard forms.",
                "Question DOM may still require JavaScript state reflection before parsing selected answers.",
            ],
            verification=[
                "Run diag kexam-records and diag quiz-resubmit against course 5416 exam 10613.",
                "Capture or compare Playwright extracted questions with requests form classification.",
                "Run diag forms --probe-only --candidate-limit N on completed courses before any live submit attempt.",
                "Run diag forms against an explicitly authorized quiz and verify course detail/result.",
            ],
            scope=contract("quiz_submission").scope,
        ),
        RequestsParityRow(
            feature="kexam_records_resubmit",
            playwright_entrypoints=list(contract("kexam_records_resubmit").playwright_entrypoints),
            requests_entrypoints=list(contract("kexam_records_resubmit").requests_entrypoints),
            required_http_conditions=[
                "The exam page exposes record modal metadata, best-score record URL, and takeExam entrypoint.",
                "The take page exposes confirmRecord, submitExam, submittedRedir, record, and questionData.",
                "After submit, the record list or best record changes and can be verified.",
            ],
            status=contract("kexam_records_resubmit").status,
            gaps=[
                "Equivalent for KExam exam pages only; generic non-KExam engines stay under quiz_submission.",
            ],
            verification=[
                "Run Playwright/capture and requests KExam record diagnostics against course 5416 exam 10613.",
                "Run diag quiz-resubmit and verify a new or updated record id.",
            ],
            scope=contract("kexam_records_resubmit").scope,
        ),
        RequestsParityRow(
            feature="question_bank_history_export",
            playwright_entrypoints=list(contract("question_bank_history_export").playwright_entrypoints),
            requests_entrypoints=list(contract("question_bank_history_export").requests_entrypoints),
            required_http_conditions=[
                "Completed activity rows expose a direct result modal URL or detail URL.",
                "KExam record URLs are discoverable and fetchable with the authenticated requests session.",
                "KExam record pages expose parseable questionData JSON or question/answer DOM.",
            ],
            status=contract("question_bank_history_export").status,
            gaps=[
                "Playwright can click review/result labels when no direct URL is present.",
                "Generic non-KExam quizzes may still expose only score metadata to requests.",
            ],
            verification=[
                "Run bank probe --backend requests --historical-quiz-bank and compare issue counts with Playwright probe output.",
                "For known KExam record URLs, compare question count and selected/correct answer metadata.",
            ],
        ),
        RequestsParityRow(
            feature="form_validation_classification",
            playwright_entrypoints=list(contract("form_validation_classification").playwright_entrypoints),
            requests_entrypoints=list(contract("form_validation_classification").requests_entrypoints),
            required_http_conditions=[
                "The raw activity/result HTML contains the same form controls as the rendered page.",
                "Checked, disabled, textarea, and contenteditable values are present without JavaScript reflection.",
            ],
            status=contract("form_validation_classification").status,
            gaps=[
                "Rendered-only state can make requests classification undercount fields or answers.",
                "Requests classification intentionally does not click or submit anything.",
            ],
            verification=[
                "Compare classification, field counts, question count, and selected answer count for the same activity.",
            ],
        ),
        RequestsParityRow(
            feature="scheduler_run_backend",
            playwright_entrypoints=list(contract("scheduler_run_backend").playwright_entrypoints),
            requests_entrypoints=list(contract("scheduler_run_backend").requests_entrypoints),
            required_http_conditions=[
                "Both backends can fetch pending courses and refresh each course detail after every item.",
                "Each course worker owns an isolated session/context when real concurrency is used.",
                "Live mutation requires the per-item endpoint conditions listed above.",
            ],
            status=contract("scheduler_run_backend").status,
            gaps=[
                "Requests backend can attempt full course completion through watchTime, survey submit, and quiz/KExam submit.",
                "Hybrid remains available for requests-first fallback on unverified media templates or browser-only learning-state flows.",
            ],
            verification=[
                "Run the requests backend on pending courses and verify course_runs, item_results, summary, and errors.",
                "Run Playwright or hybrid with --concurrency > 1 and verify separate course workers do not reorder items within a course.",
            ],
            scope=contract("scheduler_run_backend").scope,
        ),
    ]


def build_requests_parity_report(
    reproduction_report: RequestsReproductionReport | None = None,
) -> RequestsParityReport:
    matrix = build_requests_parity_matrix()
    counts = {"equivalent": 0, "partially_equivalent": 0, "not_equivalent_yet": 0}
    for row in matrix:
        counts[row.status] += 1
    diagnostic_evidence = _diagnostic_evidence(reproduction_report)
    return RequestsParityReport(
        title="Playwright vs Requests Parity Report",
        summary=[
            "Requests covers authenticated read paths, session reuse, course listing, course detail, and some result probing.",
            "Requests can now replay the observed ReadLog/watchTime flow for reading/video time accumulation when course-detail verification increases.",
            "KExam requests record reading, completed-course read-only probing, and resubmit verification are live-verified; generic quiz/survey form submit remains partial because raw HTML may miss rendered-only fields.",
        ],
        matrix=matrix,
        status_counts=counts,
        diagnostic_evidence=diagnostic_evidence,
        read_only_verification_commands=[
            "uv run tms-vghks-cli auth requests-login --accounts .tms_accounts.toml --label account1",
            "uv run tms-vghks-cli courses list pending --accounts .tms_accounts.toml --label account1",
            "uv run tms-vghks-cli courses list pending --accounts .tms_accounts.toml --label account1 --backend playwright",
            "uv run tms-vghks-cli courses list completed --accounts .tms_accounts.toml --label account1",
            "uv run tms-vghks-cli courses list completed --accounts .tms_accounts.toml --label account1 --backend playwright",
            "uv run tms-vghks-cli courses inspect <course-url-or-id> --accounts .tms_accounts.toml --label account1",
            "uv run tms-vghks-cli courses inspect <course-url-or-id> --accounts .tms_accounts.toml --label account1 --backend playwright",
            "uv run tms-vghks-cli diag compare --accounts .tms_accounts.toml --label account1 --detail-limit 1",
            "uv run tms-vghks-cli bank export --login-method saved --probe-only",
            "uv run tms-vghks-cli bank probe --login-method saved --course-limit 1 --activity-limit 3",
            "uv run tms-vghks-cli diag network <course-url-or-id> --item-order 1 --action open-only --login-method saved --output .tms_session/network_observations.jsonl",
            "uv run tms-vghks-cli diag network <course-url-or-id> --item-order 1 --action read-wait --wait-ms 10000 --login-method saved",
            "uv run tms-vghks-cli diag network <course-url-or-id> --item-order 1 --action form-open --login-method saved",
            "uv run tms-vghks-cli diag reproduction --input .tms_session/network_observations.jsonl",
            "uv run tms-vghks-cli diag reading --accounts .tms_accounts.toml --label account1 --wait-seconds 60",
            "uv run tms-vghks-cli diag reading --accounts .tms_accounts.toml --label account1 --wait-seconds 60 --force-watch-time",
            "uv run tms-vghks-cli diag forms --accounts .tms_accounts.toml --label account1 --kind both --scope completed --probe-only --candidate-limit 3",
        ],
        live_mutation_manual_protocol=[
            "Choose one pending reading/video, quiz, or survey item with explicit permission to test it.",
            "Run diag network for the item and review only sanitized method, URL, header key, form key, and response summary data.",
            "Implement requests replay only for endpoints proven by that normal browser flow.",
            "Wait the real required time or watch interval for reading/video items.",
            "Verify success by re-reading course detail through requests.",
        ],
        assumptions=[
            "Requests is the default read and live automation backend and can attempt verified reading/video watchTime replay.",
            "Hybrid is an explicit requests-first fallback backend while quiz/survey requests submit continues accumulating live verification cases.",
            "No requests path should fake elapsed time, skip validation, or infer completion without course-detail verification.",
            "Survey choice fields use the middle option, and survey text/input/textarea fields use the fixed value 無.",
        ],
    )


def format_requests_parity_markdown(report: RequestsParityReport | None = None) -> str:
    report = report or build_requests_parity_report()
    lines = [
        f"# {report.title}",
        "",
        "## Summary",
        "",
    ]
    lines.extend(f"- {item}" for item in report.summary)
    lines.extend(
        [
            "",
            "## Status Counts",
            "",
        ]
    )
    lines.extend(f"- `{status}`: {count}" for status, count in report.status_counts.items())
    if report.diagnostic_evidence:
        lines.extend(
            [
                "",
                "## Diagnostic Evidence Summary",
                "",
                "| Feature | Requests reproduction status | Evidence | Missing evidence | URL patterns |",
                "|---|---|---|---|---|",
            ]
        )
        for row in report.diagnostic_evidence:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(str(row.get("feature") or "")),
                        _cell(f"`{row.get('status') or ''}`"),
                        _cell([str(value) for value in row.get("evidence") or []]),
                        _cell([str(value) for value in row.get("missing_evidence") or []]),
                        _cell([str(value) for value in row.get("url_patterns") or []]),
                    ]
                )
                + " |"
            )
    lines.extend(
        [
            "",
            "## Parity Matrix",
            "",
            "| Feature | Playwright entrypoints | Requests entrypoints | Required HTTP conditions | Status | Gaps | Verification |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in report.matrix:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(row.feature),
                    _cell(row.playwright_entrypoints),
                    _cell(row.requests_entrypoints),
                    _cell(row.required_http_conditions),
                    _cell(f"`{row.status}`"),
                    _cell(row.gaps),
                    _cell(row.verification),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Read-Only Verification Commands",
            "",
        ]
    )
    lines.extend(f"```powershell\n{command}\n```" for command in report.read_only_verification_commands)
    lines.extend(
        [
            "",
            "## Live Mutation Manual Protocol",
            "",
        ]
    )
    lines.extend(f"{index}. {step}" for index, step in enumerate(report.live_mutation_manual_protocol, start=1))
    lines.extend(
        [
            "",
            "## Assumptions",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in report.assumptions)
    lines.append("")
    return "\n".join(lines)


def format_backend_comparison_markdown(report: BackendComparisonReport) -> str:
    lines = [
        f"# {report.title}",
        "",
        f"- Status: `{report.status}`",
    ]
    lines.extend(f"- {item}" for item in report.summary)
    if report.detail_targets:
        lines.extend(["", "## Detail Targets", ""])
        lines.extend(f"- `{target}`" for target in report.detail_targets)
    lines.extend(
        [
            "",
            "## Comparison Rows",
            "",
            "| Feature | Status | Requests count | Playwright count | Mismatches | Requests error | Playwright error |",
            "|---|---|---:|---:|---|---|---|",
        ]
    )
    for row in report.rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(row.feature),
                    _cell(f"`{row.status}`"),
                    "" if row.requests_count is None else str(row.requests_count),
                    "" if row.playwright_count is None else str(row.playwright_count),
                    _cell(row.mismatches),
                    _cell(row.requests_error),
                    _cell(row.playwright_error),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _diagnostic_evidence(report: RequestsReproductionReport | None) -> list[dict[str, Any]]:
    if report is None:
        return []
    return [
        {
            "feature": feature.feature,
            "status": feature.status,
            "evidence": feature.evidence,
            "missing_evidence": feature.missing_evidence,
            "url_patterns": feature.url_patterns,
            "method_counts": feature.method_counts,
            "post_data_keys": feature.post_data_keys,
            "response_json_keys": feature.response_json_keys,
        }
        for feature in report.features
    ]


def _cell(value: str | list[str]) -> str:
    if isinstance(value, list):
        text = "<br>".join(value)
    else:
        text = value
    return text.replace("|", "\\|").replace("\n", "<br>")


def _capture_call(call):
    try:
        return call(), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _list_courses_for_backend(session: Any, kind: str, backend: OperationBackend) -> list[CourseSummary]:
    method_name = "list_pending_courses" if kind == "pending" else "list_completed_courses"
    method = getattr(session, method_name)
    try:
        return method(backend=backend)
    except TypeError as exc:
        if "backend" not in str(exc):
            raise
    backend_method_name = f"{method_name}_{backend.value}"
    backend_method = getattr(session, backend_method_name, None)
    if callable(backend_method):
        return backend_method()
    return method()


def _get_course_detail_for_backend(session: Any, course: str, backend: OperationBackend) -> CourseDetail:
    method = getattr(session, "get_course_detail")
    try:
        return method(course, backend=backend)
    except TypeError as exc:
        if "backend" not in str(exc):
            raise
    backend_method = getattr(session, f"get_course_detail_{backend.value}", None)
    if callable(backend_method):
        return backend_method(course)
    return method(course)


def _compare_course_lists(
    feature: str,
    requests_courses: list[CourseSummary] | None,
    playwright_courses: list[CourseSummary] | None,
    requests_error: str,
    playwright_error: str,
) -> BackendComparisonRow:
    requests_sample = [_course_summary_snapshot(course) for course in (requests_courses or [])[:5]]
    playwright_sample = [_course_summary_snapshot(course) for course in (playwright_courses or [])[:5]]
    if requests_error or playwright_error:
        return BackendComparisonRow(
            feature=feature,
            status="error",
            requests_count=None if requests_courses is None else len(requests_courses),
            playwright_count=None if playwright_courses is None else len(playwright_courses),
            requests_sample=requests_sample,
            playwright_sample=playwright_sample,
            requests_error=requests_error,
            playwright_error=playwright_error,
        )

    requests_by_key = {_course_summary_key(course): _course_summary_signature(course) for course in requests_courses or []}
    playwright_by_key = {_course_summary_key(course): _course_summary_signature(course) for course in playwright_courses or []}
    mismatches = _mapping_mismatches(requests_by_key, playwright_by_key, "course")
    return BackendComparisonRow(
        feature=feature,
        status="equivalent" if not mismatches else "mismatch",
        requests_count=len(requests_courses or []),
        playwright_count=len(playwright_courses or []),
        mismatches=mismatches,
        requests_sample=requests_sample,
        playwright_sample=playwright_sample,
    )


def _compare_course_details(
    feature: str,
    requests_detail: CourseDetail | None,
    playwright_detail: CourseDetail | None,
    requests_error: str,
    playwright_error: str,
) -> BackendComparisonRow:
    requests_sample = [_course_detail_snapshot(requests_detail)] if requests_detail is not None else []
    playwright_sample = [_course_detail_snapshot(playwright_detail)] if playwright_detail is not None else []
    if requests_error or playwright_error:
        return BackendComparisonRow(
            feature=feature,
            status="error",
            requests_count=None if requests_detail is None else len(requests_detail.items),
            playwright_count=None if playwright_detail is None else len(playwright_detail.items),
            requests_sample=requests_sample,
            playwright_sample=playwright_sample,
            requests_error=requests_error,
            playwright_error=playwright_error,
        )

    assert requests_detail is not None
    assert playwright_detail is not None
    mismatches: list[str] = []
    for field_name in ("course_id", "title", "completed"):
        requests_value = _normalized_value(getattr(requests_detail, field_name))
        playwright_value = _normalized_value(getattr(playwright_detail, field_name))
        if requests_value != playwright_value:
            mismatches.append(f"{field_name} differs: requests={requests_value!r}, playwright={playwright_value!r}")
    if len(requests_detail.blockers) != len(playwright_detail.blockers):
        mismatches.append(
            f"blocker count differs: requests={len(requests_detail.blockers)}, playwright={len(playwright_detail.blockers)}"
        )
    requests_items = {_course_item_key(item): _course_item_signature(item) for item in requests_detail.items}
    playwright_items = {_course_item_key(item): _course_item_signature(item) for item in playwright_detail.items}
    mismatches.extend(_mapping_mismatches(requests_items, playwright_items, "item"))
    return BackendComparisonRow(
        feature=feature,
        status="equivalent" if not mismatches else "mismatch",
        requests_count=len(requests_detail.items),
        playwright_count=len(playwright_detail.items),
        mismatches=mismatches[:25],
        requests_sample=requests_sample,
        playwright_sample=playwright_sample,
    )


def _select_detail_targets(
    course: str | None,
    detail_limit: int,
    list_results: list[tuple[str, list[CourseSummary], list[CourseSummary]]],
) -> list[str]:
    if detail_limit <= 0:
        return []
    if course:
        return [course]
    targets: list[str] = []
    seen: set[str] = set()
    for _, requests_courses, playwright_courses in list_results:
        for summary in [*requests_courses, *playwright_courses]:
            target = summary.detail_url or summary.course_id or summary.title
            key = _course_summary_key(summary)
            if target and key not in seen:
                targets.append(target)
                seen.add(key)
            if len(targets) >= detail_limit:
                return targets
    return targets


def _mapping_mismatches(
    requests_by_key: dict[str, dict[str, Any]],
    playwright_by_key: dict[str, dict[str, Any]],
    noun: str,
) -> list[str]:
    mismatches: list[str] = []
    requests_keys = set(requests_by_key)
    playwright_keys = set(playwright_by_key)
    for key in sorted(requests_keys - playwright_keys)[:10]:
        mismatches.append(f"{noun} missing from Playwright: {key}")
    for key in sorted(playwright_keys - requests_keys)[:10]:
        mismatches.append(f"{noun} missing from requests: {key}")
    for key in sorted(requests_keys & playwright_keys):
        requests_signature = requests_by_key[key]
        playwright_signature = playwright_by_key[key]
        for field_name, requests_value in requests_signature.items():
            playwright_value = playwright_signature.get(field_name)
            if requests_value != playwright_value:
                mismatches.append(
                    f"{noun} {key} field {field_name} differs: "
                    f"requests={requests_value!r}, playwright={playwright_value!r}"
                )
            if len(mismatches) >= 25:
                return mismatches
    return mismatches


def _course_summary_key(course: CourseSummary) -> str:
    return _first_nonempty(course.course_id, _url_path_key(course.detail_url), _normalized_text(course.title))


def _course_summary_signature(course: CourseSummary) -> dict[str, Any]:
    return {
        "course_id": _normalized_value(course.course_id),
        "title": _normalized_text(course.title),
        "detail": _url_path_key(course.detail_url),
        "progress": _normalized_value(course.progress),
        "completed": course.completed,
    }


def _course_summary_snapshot(course: CourseSummary) -> dict[str, Any]:
    return {
        **_course_summary_signature(course),
        "data_issues": list(course.data_issues),
    }


def _course_detail_snapshot(detail: CourseDetail) -> dict[str, Any]:
    return {
        "course_id": _normalized_value(detail.course_id),
        "title": _normalized_text(detail.title),
        "url": _url_path_key(detail.url),
        "completed": detail.completed,
        "item_count": len(detail.items),
        "blocker_count": len(detail.blockers),
        "items": [_course_item_signature(item) for item in detail.items[:5]],
    }


def _course_item_key(item: CourseItem) -> str:
    activity_id = item.metadata.get("activity_id")
    if activity_id:
        return f"activity:{activity_id}"
    if item.order is not None:
        return f"order:{item.order}"
    return _first_nonempty(_url_path_key(item.detail_url), _normalized_text(item.title))


def _course_item_signature(item: CourseItem) -> dict[str, Any]:
    return {
        "title": _normalized_text(item.title),
        "order": item.order,
        "kind": str(item.kind),
        "state": str(item.state),
        "detail": _url_path_key(item.detail_url),
        "pass_condition": _normalized_value(item.pass_condition),
        "result": _normalized_value(item.result),
        "passed_marker": _normalized_value(item.passed_marker),
        "activity_id": _normalized_value(item.metadata.get("activity_id")),
    }


def _overall_backend_comparison_status(rows: list[BackendComparisonRow]) -> BackendComparisonStatus:
    if any(row.status == "error" for row in rows):
        return "error"
    if any(row.status == "mismatch" for row in rows):
        return "mismatch"
    if rows and all(row.status == "skipped" for row in rows):
        return "skipped"
    return "equivalent"


def _backend_comparison_summary(rows: list[BackendComparisonRow], status: BackendComparisonStatus) -> list[str]:
    equivalent_count = sum(1 for row in rows if row.status == "equivalent")
    mismatch_count = sum(1 for row in rows if row.status == "mismatch")
    error_count = sum(1 for row in rows if row.status == "error")
    skipped_count = sum(1 for row in rows if row.status == "skipped")
    return [
        "This report compares read-only list and detail outputs from the same authenticated session.",
        f"Rows: {equivalent_count} equivalent, {mismatch_count} mismatched, {error_count} errored, {skipped_count} skipped.",
        f"Overall status is `{status}`.",
    ]


def _url_path_key(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        return parsed.path.rstrip("/") or "/"
    return url.strip().rstrip("/")


def _normalized_text(value: str | None) -> str:
    return " ".join((value or "").split())


def _normalized_value(value: Any) -> Any:
    if isinstance(value, str):
        return _normalized_text(value)
    return value


def _first_nonempty(*values: str | None) -> str:
    for value in values:
        text = _normalized_text(value)
        if text:
            return text
    return ""


__all__ = [
    "BackendComparisonReport",
    "BackendComparisonRow",
    "BackendComparisonStatus",
    "ParityStatus",
    "RequestsParityReport",
    "RequestsParityRow",
    "build_requests_parity_matrix",
    "build_requests_parity_report",
    "compare_backend_read_paths",
    "format_backend_comparison_markdown",
    "format_requests_parity_markdown",
]
