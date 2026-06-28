from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag

from .export_pacing import ExportPacerProtocol, ExportPacingOptions, make_export_pacer
from .models import CourseDetail, CourseItem, CourseSummary, ItemKind
from .parsers import BASE_URL, absolute_url, normalize_text
from .privacy import redact_sensitive_url
from .question_bank_export import (
    ActivitySource,
    AnswerSource,
    CourseSource,
    ExportIssue,
    QuestionBankRecord,
    SCHEMA_VERSION,
    parse_include,
)
from .session import TmsSession

DEFAULT_PLAYWRIGHT_JSONL_PATH = ".tms_private_exports/question-bank-playwright.jsonl"
DEFAULT_PLAYWRIGHT_MARKDOWN_PATH = ".tms_private_exports/question-bank-playwright.md"
DEFAULT_HISTORICAL_QUIZ_JSONL_PATH = ".tms_private_exports/question-bank-history.jsonl"
DEFAULT_HISTORICAL_QUIZ_MARKDOWN_PATH = ".tms_private_exports/question-bank-history.md"

REVIEW_LINK_MARKERS = ("檢視作答紀錄", "測驗結果", "詳細結果", "統計結果", "問卷回饋狀態", "學習成果")


@dataclass(slots=True)
class ExtractedQuestion:
    text: str
    options: list[str] = field(default_factory=list)
    selected_answers: list[str] = field(default_factory=list)
    correct_answers: list[str] = field(default_factory=list)
    incorrect_answers: list[str] = field(default_factory=list)
    question_type: str = "unknown"
    confidence: float = 0.5


@dataclass(slots=True)
class KExamAttempt:
    exam_id: str | None
    record_id: str | None
    record_url: str
    redacted_record_url: str
    attempt_at: str | None = None
    score: str | None = None
    submitted_status: str = "unknown"
    raw_summary: str = ""


@dataclass(slots=True)
class ActivityProbe:
    question_records: list[ExtractedQuestion] = field(default_factory=list)
    score: str | None = None
    attempt_at: str | None = None
    raw_summary: str = ""
    availability: str = "unavailable"
    reason: str = "question and answer DOM was not available"
    provenance_steps: list[str] = field(default_factory=list)
    attempt: KExamAttempt | None = None
    attempt_probes: list["ActivityProbe"] = field(default_factory=list)
    issues: list[ExportIssue] = field(default_factory=list)


@dataclass(slots=True)
class PlaywrightProbeResult:
    output_path: str | None
    markdown_path: str | None
    record_count: int
    course_count: int
    activity_count: int
    quiz_activity_count: int
    survey_activity_count: int
    available_record_count: int
    unavailable_record_count: int
    issue_count: int
    issues: list[ExportIssue] = field(default_factory=list)
    pacing: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HistoricalQuizBankResult:
    output_path: str
    markdown_path: str | None
    record_count: int
    course_count: int
    activity_count: int
    quiz_activity_count: int
    attempt_count: int
    question_count: int
    verified_correct_count: int
    verified_wrong_count: int
    unverified_selected_count: int
    unsubmitted_count: int
    issue_count: int
    issues: list[ExportIssue] = field(default_factory=list)
    pacing: dict[str, Any] = field(default_factory=dict)


def export_question_bank_playwright(
    session: TmsSession,
    output_path: str | Path = DEFAULT_PLAYWRIGHT_JSONL_PATH,
    markdown_path: str | Path | None = DEFAULT_PLAYWRIGHT_MARKDOWN_PATH,
    include: set[ItemKind] | None = None,
    source_account_label: str = "",
    allow_private_export: bool = False,
    include_unsubmitted_records: bool = False,
    course_limit: int | None = None,
    activity_limit: int | None = None,
    pacing_options: ExportPacingOptions | None = None,
    pacer: ExportPacerProtocol | None = None,
) -> PlaywrightProbeResult:
    if not allow_private_export:
        raise ValueError("private export requires --allow-private-export")
    include = include or {ItemKind.QUIZ, ItemKind.SURVEY}
    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    courses = session.list_completed_courses_playwright()
    if course_limit is not None:
        courses = courses[: max(0, course_limit)]
    records: list[QuestionBankRecord] = []
    issues: list[ExportIssue] = []
    activity_count = 0
    quiz_activity_count = 0
    survey_activity_count = 0

    processed_activities = 0
    stop = False
    export_pacer = pacer or make_export_pacer(pacing_options)
    for course in courses:
        export_pacer.sleep("playwright:course_detail")
        try:
            detail = session.get_course_detail_playwright(course.detail_url or course.course_id or course.title)
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
            if activity_limit is not None and processed_activities >= activity_limit:
                stop = True
                break
            processed_activities += 1
            export_pacer.sleep("playwright:activity_probe")
            probe = probe_activity_playwright(
                session,
                detail,
                item,
                include_unsubmitted_records=include_unsubmitted_records,
                pacer=export_pacer,
            )
            records.extend(
                build_probe_records(
                    course=course,
                    detail=detail,
                    item=item,
                    probe=probe,
                    exported_at=exported_at,
                    source_account_label=source_account_label,
                )
            )
        if stop:
            break

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(to_jsonable(record), ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    markdown_text = None
    if markdown_path:
        markdown = Path(markdown_path)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown_text = render_probe_markdown(records, pacing=dict(export_pacer.summary()))
        markdown.write_text(markdown_text, encoding="utf-8")

    record_issues = [issue for record in records for issue in record.issues]
    all_issues = issues + record_issues
    available = sum(1 for record in records if record.answer.status == "available")
    unavailable = len(records) - available
    return PlaywrightProbeResult(
        output_path=str(output),
        markdown_path=str(markdown_path) if markdown_path else None,
        record_count=len(records),
        course_count=len(courses),
        activity_count=activity_count,
        quiz_activity_count=quiz_activity_count,
        survey_activity_count=survey_activity_count,
        available_record_count=available,
        unavailable_record_count=unavailable,
        issue_count=len(all_issues),
        issues=all_issues,
        pacing=dict(export_pacer.summary()),
    )


def export_historical_quiz_bank_playwright(
    session: TmsSession,
    output_path: str | Path = DEFAULT_HISTORICAL_QUIZ_JSONL_PATH,
    markdown_path: str | Path | None = DEFAULT_HISTORICAL_QUIZ_MARKDOWN_PATH,
    source_account_label: str = "",
    allow_private_export: bool = False,
    include_unsubmitted_records: bool = False,
    course_limit: int | None = None,
    activity_limit: int | None = None,
    pacing_options: ExportPacingOptions | None = None,
    pacer: ExportPacerProtocol | None = None,
) -> HistoricalQuizBankResult:
    if not allow_private_export:
        raise ValueError("private export requires --allow-private-export")

    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    courses = session.list_completed_courses_playwright()
    if course_limit is not None:
        courses = courses[: max(0, course_limit)]

    records: list[QuestionBankRecord] = []
    issues: list[ExportIssue] = []
    activity_count = 0
    quiz_activity_count = 0
    attempt_count = 0
    processed_quiz_activities = 0
    stop = False
    export_pacer = pacer or make_export_pacer(pacing_options)

    for course in courses:
        export_pacer.sleep("playwright:course_detail")
        try:
            detail = session.get_course_detail_playwright(course.detail_url or course.course_id or course.title)
        except Exception as exc:
            issues.append(ExportIssue("course_detail_unavailable", f"{course.title}: {exc}"))
            continue
        activity_count += len(detail.items)
        for item in detail.items:
            if ItemKind(item.kind) != ItemKind.QUIZ:
                continue
            quiz_activity_count += 1
            if activity_limit is not None and processed_quiz_activities >= activity_limit:
                stop = True
                break
            processed_quiz_activities += 1
            export_pacer.sleep("playwright:activity_probe")
            probe = probe_activity_playwright(
                session,
                detail,
                item,
                include_unsubmitted_records=include_unsubmitted_records,
                pacer=export_pacer,
            )
            attempt_count += len(probe.attempt_probes or ([probe] if probe.attempt else []))
            built_records, built_issues = build_historical_quiz_records(
                course=course,
                detail=detail,
                item=item,
                probe=probe,
                exported_at=exported_at,
                source_account_label=source_account_label,
                include_unsubmitted_records=include_unsubmitted_records,
            )
            records.extend(built_records)
            issues.extend(built_issues)
        if stop:
            break

    annotate_canonical_answers(records)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(to_jsonable(record), ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    if markdown_path:
        markdown = Path(markdown_path)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(
            render_historical_quiz_bank_markdown(records, pacing=dict(export_pacer.summary())),
            encoding="utf-8",
        )

    record_issues = [issue for record in records for issue in record.issues]
    all_issues = issues + record_issues
    counts = answer_status_counts(records)
    return HistoricalQuizBankResult(
        output_path=str(output),
        markdown_path=str(markdown_path) if markdown_path else None,
        record_count=len(records),
        course_count=len(courses),
        activity_count=activity_count,
        quiz_activity_count=quiz_activity_count,
        attempt_count=attempt_count,
        question_count=sum(1 for record in records if record.question.get("text")),
        verified_correct_count=counts.get("verified_correct", 0),
        verified_wrong_count=counts.get("verified_wrong", 0),
        unverified_selected_count=counts.get("unverified_selected", 0),
        unsubmitted_count=counts.get("unsubmitted", 0),
        issue_count=len(all_issues),
        issues=all_issues,
        pacing=dict(export_pacer.summary()),
    )


def probe_activity_playwright(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    include_unsubmitted_records: bool = False,
    pacer: ExportPacerProtocol | None = None,
) -> ActivityProbe:
    session.start_browser(headless=False)
    assert session.context is not None
    session.sync_cookies_to_browser()
    page = session.context.new_page()
    html_parts: list[tuple[str, str]] = []
    try:
        result_url = item.metadata.get("result_modal_url")
        if result_url:
            page.goto(str(result_url), wait_until="domcontentloaded", timeout=60000)
            result_html = html_from_page_or_json_text(page)
            html_parts.append(("result_modal_url", result_html))

        page.goto(course.url, wait_until="domcontentloaded", timeout=60000)
        try:
            row = _row_locator_for_title(page, item.title)
            for label in REVIEW_LINK_MARKERS:
                target = row.get_by_text(label, exact=False).first
                if target.is_visible(timeout=1000):
                    target.click()
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                    html_parts.append((f"clicked:{label}", page.content()))
                    break
        except Exception:
            pass

        best = ActivityProbe()
        attempt_probes: list[ActivityProbe] = []
        seen_attempt_keys: set[str] = set()
        for step, html in html_parts:
            probe = extract_activity_probe_from_html(html, item.kind)
            probe.provenance_steps.append(step)
            if ItemKind(item.kind) == ItemKind.QUIZ:
                attempts = dedupe_kexam_attempts(extract_kexam_attempts_from_modal_html(html, session.base_url))
                for attempt in attempts:
                    attempt_key = kexam_attempt_probe_key(attempt)
                    if attempt_key and attempt_key in seen_attempt_keys:
                        continue
                    if attempt_key:
                        seen_attempt_keys.add(attempt_key)
                    should_fetch_record = _should_fetch_kexam_attempt_record(
                        attempt,
                        include_unsubmitted_records=include_unsubmitted_records,
                    )
                    if pacer and should_fetch_record:
                        pacer.sleep("playwright:kexam_record")
                    attempt_probe = probe_kexam_attempt_with_page(
                        page,
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
            if available_attempts:
                best = available_attempts[0]
            best.attempt_probes = attempt_probes
            return best
        return best
    finally:
        try:
            page.close()
        except Exception:
            pass


def html_from_page_or_json_text(page) -> str:
    body_text = page.locator("body").inner_text(timeout=10000)
    try:
        payload = json.loads(body_text)
    except ValueError:
        return page.content()
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("html"), str):
            return data["html"]
        if isinstance(payload.get("html"), str):
            return str(payload["html"])
    return body_text


def extract_kexam_attempts_from_modal_html(html: str, base_url: str = BASE_URL) -> list[KExamAttempt]:
    soup = BeautifulSoup(html or "", "html.parser")
    attempts: list[KExamAttempt] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        if not re.search(r"/kexam/\d+/record\b", href):
            continue
        record_url = absolute_url(href, base_url) or href
        if record_url in seen:
            continue
        seen.add(record_url)
        parsed = urlparse(record_url)
        query = parse_qs(parsed.query)
        exam_match = re.search(r"/kexam/(\d+)/record\b", parsed.path)
        row = anchor.find_parent("tr")
        row_text = normalize_text(row.get_text(" ", strip=True) if row else anchor.parent.get_text(" ", strip=True) if anchor.parent else "")
        attempt_at, score = extract_attempt_row_metadata(row)
        submitted_status = "unsubmitted" if "未繳交" in row_text or _kexam_attempt_row_unsubmitted(row_text, score) else "submitted"
        attempts.append(
            KExamAttempt(
                exam_id=exam_match.group(1) if exam_match else None,
                record_id=(query.get("recordID") or query.get("recordId") or query.get("recordid") or [None])[0],
                record_url=record_url,
                redacted_record_url=redact_sensitive_url(record_url),
                attempt_at=attempt_at,
                score=score,
                submitted_status=submitted_status,
                raw_summary=row_text,
            )
        )
    return attempts


def dedupe_kexam_attempts(attempts: list[KExamAttempt]) -> list[KExamAttempt]:
    deduped: list[KExamAttempt] = []
    seen: set[str] = set()
    for attempt in attempts:
        key = kexam_attempt_probe_key(attempt)
        if not key:
            deduped.append(attempt)
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(attempt)
    return deduped


def kexam_attempt_probe_key(attempt: KExamAttempt) -> str:
    if attempt.record_id:
        return f"record_id:{attempt.record_id}"
    return f"url:{redact_sensitive_url(attempt.record_url)}" if attempt.record_url else ""


def _should_fetch_kexam_attempt_record(
    attempt: KExamAttempt,
    *,
    include_unsubmitted_records: bool,
) -> bool:
    return bool(include_unsubmitted_records or attempt.submitted_status != "unsubmitted")


def extract_attempt_row_metadata(row: Tag | None) -> tuple[str | None, str | None]:
    if row is None:
        return None, None
    cells = [normalize_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"], recursive=False)]
    text = normalize_text(" ".join(cells) or row.get_text(" ", strip=True))
    attempt_at = next((cell for cell in cells if re.search(r"\d{4}-\d{2}-\d{2}", cell)), None)
    if not attempt_at:
        date_match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?", text)
        attempt_at = date_match.group(0) if date_match else None
    score: str | None = None
    if len(cells) >= 2 and re.fullmatch(r"\d+", cells[0]) and re.fullmatch(r"-+|\d+(?:\.\d+)?", cells[1]):
        return attempt_at, None if re.fullmatch(r"-+", cells[1]) else cells[1]
    for index, cell in enumerate(cells):
        if "分數" in cell and index + 1 < len(cells):
            score = cells[index + 1]
            break
    if not score:
        numeric_cells = [cell for cell in cells if re.fullmatch(r"\d+(?:\.\d+)?", cell)]
        if numeric_cells:
            score = numeric_cells[-1]
    return attempt_at, score


def _kexam_attempt_row_unsubmitted(row_text: str, score: str | None) -> bool:
    if score:
        return False
    return bool(re.search(r"\s-+\s", f" {row_text} "))


def probe_kexam_attempt_with_page(
    page,
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
    page.goto(attempt.record_url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    wait_for_record_question_dom(page)
    reflect_form_state_into_dom(page)
    html = page.content()
    probe = extract_activity_probe_from_html(html, item_kind)
    try:
        from .requests_probe import extract_kexam_record_probe_from_json_html

        json_probe = extract_kexam_record_probe_from_json_html(html, item_kind, attempt)
        if _json_probe_is_better(json_probe, probe):
            probe = json_probe
    except Exception:
        pass
    probe.attempt = attempt
    probe.score = probe.score or attempt.score
    probe.attempt_at = probe.attempt_at or attempt.attempt_at
    probe.raw_summary = probe.raw_summary or attempt.raw_summary
    probe.provenance_steps.append(f"kexam_record:{attempt.record_id or ''}")
    if probe.question_records:
        probe.availability = "available"
        probe.reason = "kexam record page exposed question and answer DOM"
    else:
        probe.reason = "kexam record page did not expose parseable question DOM"
        probe.issues.append(ExportIssue("record_questions_unavailable", probe.reason))
    return probe


def _json_probe_is_better(candidate: ActivityProbe, current: ActivityProbe) -> bool:
    if not candidate.question_records:
        return False
    if len(candidate.question_records) > len(current.question_records):
        return True
    candidate_selected = sum(len(question.selected_answers) for question in candidate.question_records)
    current_selected = sum(len(question.selected_answers) for question in current.question_records)
    return bool(candidate_selected > current_selected)


def wait_for_record_question_dom(page, timeout_ms: int = 10000) -> None:
    selectors = (
        'input[type="radio"]',
        'input[type="checkbox"]',
        ".question",
        "[data-question]",
        "textarea",
        "[contenteditable='true']",
    )
    for selector in selectors:
        try:
            page.wait_for_selector(selector, timeout=timeout_ms // len(selectors))
            return
        except Exception:
            continue


def reflect_form_state_into_dom(page) -> None:
    page.evaluate(
        """
        () => {
          document.querySelectorAll('input').forEach((input) => {
            if (input.checked) input.setAttribute('checked', 'checked');
            else input.removeAttribute('checked');
            if (input.disabled) input.setAttribute('disabled', 'disabled');
            else input.removeAttribute('disabled');
            if (input.value) input.setAttribute('value', input.value);
          });
          document.querySelectorAll('textarea').forEach((textarea) => {
            if (textarea.value && !textarea.textContent.trim()) textarea.textContent = textarea.value;
          });
          document.querySelectorAll('[contenteditable="true"]').forEach((node) => {
            if (node.innerText && !node.textContent.trim()) node.textContent = node.innerText;
          });
        }
        """
    )


def extract_activity_probe_from_html(html: str, item_kind: ItemKind | str = ItemKind.UNKNOWN) -> ActivityProbe:
    soup = BeautifulSoup(html or "", "html.parser")
    questions = extract_questions_from_soup(soup)
    score, attempt_at = extract_score_metadata(soup)
    summary = normalize_text(soup.get_text(" ", strip=True))[:1000]
    if questions:
        return ActivityProbe(
            question_records=questions,
            score=score,
            attempt_at=attempt_at,
            raw_summary=summary,
            availability="available",
            reason="question DOM was available",
        )
    reason = "quiz result exposed score metadata only" if ItemKind(item_kind) == ItemKind.QUIZ else "submitted survey answers were not available"
    return ActivityProbe(
        score=score,
        attempt_at=attempt_at,
        raw_summary=summary,
        availability="unavailable",
        reason=reason,
    )


def extract_questions_from_soup(soup: BeautifulSoup) -> list[ExtractedQuestion]:
    questions: list[ExtractedQuestion] = []
    seen: set[str] = set()
    groups: dict[str, list[Tag]] = {}
    for input_node in soup.select('input[type="radio"], input[type="checkbox"]'):
        name = input_node.get("name") or input_node.get("id") or ""
        if not name:
            continue
        groups.setdefault(name, []).append(input_node)
    for name, inputs in groups.items():
        question_text = infer_question_text(inputs[0], inputs)
        options = [option_text(input_node) for input_node in inputs]
        selected = [text for text, input_node in zip(options, inputs) if input_node.has_attr("checked")]
        correct = [text for text, input_node in zip(options, inputs) if is_correct_option(input_node)]
        incorrect = [text for text, input_node in zip(options, inputs) if is_incorrect_option(input_node)]
        key = normalize_text(question_text + "|" + "|".join(options))
        if key in seen or not question_text or not options:
            continue
        seen.add(key)
        questions.append(
            ExtractedQuestion(
                text=question_text,
                options=options,
                selected_answers=selected,
                correct_answers=correct,
                incorrect_answers=incorrect,
                question_type="multiple_choice" if any(node.get("type") == "checkbox" for node in inputs) else "single_choice",
                confidence=0.85 if selected or correct else 0.65,
            )
        )
    for field in soup.select("textarea, input[type='text'], [contenteditable='true']"):
        value = normalize_text(field.get("value") or field.get_text(" ", strip=True))
        label = field_label(field)
        if not label or not value:
            continue
        key = normalize_text(label + "|" + value)
        if key in seen:
            continue
        seen.add(key)
        questions.append(
            ExtractedQuestion(
                text=label,
                options=[],
                selected_answers=[value],
                question_type="free_text",
                confidence=0.7,
            )
        )
    return questions


def extract_score_metadata(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    rows = []
    for row in soup.find_all("tr"):
        cells = [normalize_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    for cells in rows:
        if len(cells) >= 2 and re.search(r"\d{4}-\d{2}-\d{2}", cells[0]) and re.search(r"\d+", cells[1]):
            return cells[1], cells[0]
    text = normalize_text(soup.get_text(" ", strip=True))
    score_match = re.search(r"(?:分數|成績)[:：]?\s*(\d+(?:\.\d+)?)", text)
    return (score_match.group(1) if score_match else None), None


def infer_question_text(input_node: Tag, inputs: list[Tag]) -> str:
    options = [option_text(node) for node in inputs]
    container = nearest_group_container(input_node, inputs)
    text = question_text_from_container(container, options)
    if meaningful_question_text(text, options):
        return text
    text = question_text_from_nearby_nodes(input_node, container, options)
    if meaningful_question_text(text, options):
        return text
    legend = input_node.find_previous(["legend", "th", "label"])
    fallback = normalize_text(legend.get_text(" ", strip=True) if legend else input_node.get("name"))
    return fallback if meaningful_question_text(fallback, options) else normalize_text(input_node.get("name"))


def nearest_group_container(input_node: Tag, inputs: list[Tag]) -> Tag | None:
    group_key = input_node.get("name") or input_node.get("id") or ""
    for ancestor in input_node.parents:
        if not isinstance(ancestor, Tag) or ancestor.name in {"html", "body"}:
            continue
        group_inputs = [
            node
            for node in ancestor.select('input[type="radio"], input[type="checkbox"]')
            if (node.get("name") or node.get("id") or "") == group_key
        ]
        if len(group_inputs) >= len(inputs):
            return ancestor
    return input_node.find_parent(["tr", "li", "fieldset", "div"])


def question_text_from_container(container: Tag | None, options: list[str]) -> str:
    if not container:
        return ""
    for selector in (
        ".question-title",
        ".question-text",
        ".question-stem",
        ".subject",
        ".stem",
        "legend",
        "th",
        "dt",
        "p",
    ):
        found = container.select_one(selector)
        if found:
            text = strip_options_from_text(found.get_text(" ", strip=True), options)
            if meaningful_question_text(text, options):
                return text
    return strip_options_from_text(container.get_text(" ", strip=True), options)


def question_text_from_nearby_nodes(input_node: Tag, container: Tag | None, options: list[str]) -> str:
    start = container or input_node
    for sibling in start.find_previous_siblings():
        if not isinstance(sibling, Tag):
            continue
        text = strip_options_from_text(sibling.get_text(" ", strip=True), options)
        if meaningful_question_text(text, options):
            return text
    for ancestor in start.parents:
        if not isinstance(ancestor, Tag) or ancestor.name in {"html", "body"}:
            continue
        for node in ancestor.find_all(["p", "div", "th", "dt", "label", "legend"], recursive=False):
            if node is start:
                break
            text = strip_options_from_text(node.get_text(" ", strip=True), options)
            if meaningful_question_text(text, options):
                return text
    return ""


def strip_options_from_text(text: str | None, options: list[str]) -> str:
    value = normalize_text(text)
    for option in sorted((option for option in options if option), key=len, reverse=True):
        if len(option) <= 2:
            value = re.sub(rf"(?:(?<=\s)|^){re.escape(option)}(?=\s|$)", " ", value)
        else:
            value = value.replace(option, " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ：:-")


def meaningful_question_text(text: str | None, options: list[str]) -> bool:
    value = normalize_text(text)
    if not value or value in set(options):
        return False
    if len(value) <= 2 and value in {"是", "否", "對", "錯"}:
        return False
    return len(value) >= 3


def option_text(input_node: Tag) -> str:
    if input_node.get("id"):
        label = input_node.find_parent().find("label", attrs={"for": input_node.get("id")}) if input_node.find_parent() else None
        if not label:
            label = input_node.find_parent(["form", "body"]).find("label", attrs={"for": input_node.get("id")}) if input_node.find_parent(["form", "body"]) else None
        if label:
            return normalize_text(label.get_text(" ", strip=True))
    label = input_node.find_parent("label")
    if label:
        text = normalize_text(label.get_text(" ", strip=True))
        if text:
            return text
    parent = input_node.parent
    text = normalize_text(parent.get_text(" ", strip=True) if parent else "")
    return text or normalize_text(input_node.get("value"))


def is_correct_option(input_node: Tag) -> bool:
    candidates = [input_node]
    label = input_node.find_parent("label")
    if label:
        candidates.append(label)
    parent = input_node.parent if isinstance(input_node.parent, Tag) else None
    if parent and parent.name not in {"form", "body"}:
        candidates.append(parent)
    for node in candidates:
        classes = {str(item).lower() for item in node.get("class", [])}
        text = normalize_text(node.get_text(" ", strip=True))
        if classes & {"correct", "right", "answer", "success"} or any(token in text for token in ("正確", "標準答案")):
            return True
    return False


def is_incorrect_option(input_node: Tag) -> bool:
    candidates = [input_node]
    label = input_node.find_parent("label")
    if label:
        candidates.append(label)
    parent = input_node.parent if isinstance(input_node.parent, Tag) else None
    if parent and parent.name not in {"form", "body"}:
        candidates.append(parent)
    for node in candidates:
        classes = {str(item).lower() for item in node.get("class", [])}
        text = normalize_text(node.get_text(" ", strip=True))
        if classes & {"incorrect", "wrong", "danger", "error", "fail"}:
            return True
        if any(token in text for token in ("答錯", "不正確", "錯誤答案", "回答錯誤")):
            return True
    return False


def field_label(field: Tag) -> str:
    if field.get("id"):
        root = field.find_parent(["form", "body"])
        label = root.find("label", attrs={"for": field.get("id")}) if root else None
        if label:
            return normalize_text(label.get_text(" ", strip=True))
    label = field.find_previous(["label", "th", "dt"])
    return normalize_text(label.get_text(" ", strip=True) if label else field.get("name") or field.get("id"))


def build_probe_records(
    course: CourseSummary,
    detail: CourseDetail,
    item: CourseItem,
    probe: ActivityProbe,
    exported_at: str,
    source_account_label: str = "",
) -> list[QuestionBankRecord]:
    if probe.attempt_probes:
        records: list[QuestionBankRecord] = []
        for attempt_probe in probe.attempt_probes:
            records.extend(
                build_probe_records(
                    course=course,
                    detail=detail,
                    item=item,
                    probe=attempt_probe,
                    exported_at=exported_at,
                    source_account_label=source_account_label,
                )
            )
        return records

    questions = probe.question_records or [None]
    records: list[QuestionBankRecord] = []
    for question in questions:
        issues: list[ExportIssue] = []
        status = "available" if question else "unavailable"
        if not question:
            issues.append(ExportIssue(f"{item.kind}_answers_unavailable", probe.reason))
        issues.extend(probe.issues)
        answer = AnswerSource(
            status=status,
            answer_type=str(item.kind),
            selected_answers=question.selected_answers if question else [],
            score=probe.score or item.result,
            attempt_at=probe.attempt_at,
            raw_summary=probe.raw_summary,
        )
        attempt = kexam_attempt_to_dict(probe.attempt) if probe.attempt else {}
        records.append(
            QuestionBankRecord(
                schema_version=SCHEMA_VERSION,
                source_system="tms.vghks.gov.tw",
                exported_at=exported_at,
                source_account_label=source_account_label,
                course=CourseSource(
                    course_id=course.course_id or detail.course_id,
                    title=course.title,
                    detail_url=course.detail_url or detail.url,
                    completed_at=course.progress,
                    raw_text="",
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
                    "confidence": question.confidence if question else 0.2,
                    "attempt": attempt,
                },
                question={
                    "text": question.text if question else None,
                    "options": question.options if question else [],
                    "type": question.question_type if question else str(item.kind),
                    "normalized_text": normalize_text(question.text).lower() if question else None,
                    "correct_answers": question.correct_answers if question else [],
                },
                answer=answer,
                provenance={
                    "collector": "tms-vghks",
                    "method": "playwright",
                    "course_url": detail.url,
                    "result_modal_url": redact_sensitive_url(str(item.metadata.get("result_modal_url")))
                    if item.metadata.get("result_modal_url")
                    else None,
                    "steps": probe.provenance_steps,
                    "availability_reason": probe.reason,
                },
                attempt=attempt,
                issues=issues,
            )
        )
    return records


def kexam_attempt_to_dict(attempt: KExamAttempt | None) -> dict[str, Any]:
    if not attempt:
        return {}
    return {
        "exam_id": attempt.exam_id,
        "record_id": attempt.record_id,
        "record_url": attempt.redacted_record_url,
        "attempt_at": attempt.attempt_at,
        "score": attempt.score,
        "submitted_status": attempt.submitted_status,
        "raw_summary": attempt.raw_summary,
    }


def build_historical_quiz_records(
    course: CourseSummary,
    detail: CourseDetail,
    item: CourseItem,
    probe: ActivityProbe,
    exported_at: str,
    source_account_label: str = "",
    include_unsubmitted_records: bool = False,
    collector_method: str = "playwright-historical-kexam-record",
) -> tuple[list[QuestionBankRecord], list[ExportIssue]]:
    attempt_probes = probe.attempt_probes or ([probe] if probe.attempt else [])
    records: list[QuestionBankRecord] = []
    issues: list[ExportIssue] = []
    if not attempt_probes:
        issues.append(ExportIssue("quiz_records_unavailable", f"No kexam record links were available for {item.title}."))
        return records, issues

    for attempt_probe in attempt_probes:
        attempt = attempt_probe.attempt
        issues.extend(attempt_probe.issues)
        if attempt and attempt.submitted_status == "unsubmitted" and not include_unsubmitted_records:
            continue
        questions: list[ExtractedQuestion | None] = attempt_probe.question_records or [None]
        for question in questions:
            if question is None and not include_unsubmitted_records:
                issues.append(
                    ExportIssue(
                        "record_questions_unavailable",
                        attempt_probe.reason,
                    )
                )
                continue
            status, method, confidence = classify_historical_answer(question, attempt_probe)
            record_issues = list(attempt_probe.issues)
            if status == "unverified_selected":
                record_issues.append(
                    ExportIssue(
                        "answer_unverified",
                        "TMS did not expose per-question correctness for this selected answer.",
                        "info",
                    )
                )
            if status == "unsubmitted":
                record_issues.append(
                    ExportIssue(
                        "unsubmitted_record_metadata_only",
                        "KExam record was marked 未繳交 and is not a verified answer.",
                        "info",
                    )
                )

            merge_key = historical_merge_key(question)
            selected_answers = question.selected_answers if question else []
            correct_answers = question.correct_answers if question else []
            attempt_dict = kexam_attempt_to_dict(attempt)
            answer = AnswerSource(
                status=status,
                answer_type=str(item.kind),
                selected_answers=selected_answers,
                score=attempt_probe.score or item.result,
                attempt_at=attempt_probe.attempt_at,
                raw_summary=attempt_probe.raw_summary,
            )
            records.append(
                QuestionBankRecord(
                    schema_version=SCHEMA_VERSION,
                    source_system="tms.vghks.gov.tw",
                    exported_at=exported_at,
                    source_account_label=source_account_label,
                    course=CourseSource(
                        course_id=course.course_id or detail.course_id,
                        title=course.title,
                        detail_url=course.detail_url or detail.url,
                        completed_at=course.progress,
                        raw_text="",
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
                        "answer_status": status,
                        "verification_method": method,
                        "confidence": confidence,
                        "merge_key": merge_key,
                        "is_canonical": False,
                    },
                    question={
                        "text": question.text if question else None,
                        "options": question.options if question else [],
                        "type": question.question_type if question else str(item.kind),
                        "normalized_text": normalize_text(question.text).lower() if question else None,
                        "merge_key": merge_key,
                        "correct_answers": correct_answers,
                        "incorrect_answers": question.incorrect_answers if question else [],
                    },
                    answer=answer,
                    provenance={
                        "collector": "tms-vghks",
                        "method": collector_method,
                        "course_url": detail.url,
                        "result_modal_url": redact_sensitive_url(str(item.metadata.get("result_modal_url")))
                        if item.metadata.get("result_modal_url")
                        else None,
                        "steps": attempt_probe.provenance_steps,
                        "availability_reason": attempt_probe.reason,
                    },
                    attempt=attempt_dict,
                    issues=record_issues,
                )
            )
    return records, issues


def classify_historical_answer(question: ExtractedQuestion | None, probe: ActivityProbe) -> tuple[str, str, float]:
    attempt = probe.attempt
    if attempt and attempt.submitted_status == "unsubmitted":
        return "unsubmitted", "unsubmitted_record", 0.0
    if question is None:
        return "unverified_selected", "question_unavailable", 0.1

    selected = {normalized_answer_key(answer) for answer in question.selected_answers}
    correct = {normalized_answer_key(answer) for answer in question.correct_answers}
    incorrect = {normalized_answer_key(answer) for answer in question.incorrect_answers}
    if selected and incorrect and selected & incorrect:
        return "verified_wrong", "per_question_marker", 0.95
    if selected and correct:
        if selected.issubset(correct):
            return "verified_correct", "per_question_marker", 0.95
        return "verified_wrong", "per_question_marker", 0.95
    if selected and parse_score_value(probe.score) == 100.0:
        return "verified_correct", "full_score_inferred", 0.9
    return "unverified_selected", "score_without_item_marker", question.confidence


def historical_merge_key(question: ExtractedQuestion | None) -> str | None:
    if not question:
        return None
    option_key = "|".join(normalized_answer_key(option) for option in question.options)
    return normalized_answer_key(f"{question.text}|{option_key}")


def normalized_answer_key(value: str | None) -> str:
    return re.sub(r"\W+", "", normalize_text(value).lower())


def parse_score_value(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def annotate_canonical_answers(records: list[QuestionBankRecord]) -> None:
    best_by_key: dict[str, QuestionBankRecord] = {}
    for record in records:
        merge_key = record.assessment.get("merge_key")
        if not merge_key:
            continue
        current = best_by_key.get(str(merge_key))
        if current is None or canonical_rank(record) > canonical_rank(current):
            best_by_key[str(merge_key)] = record
    for record in records:
        merge_key = record.assessment.get("merge_key")
        record.assessment["is_canonical"] = bool(merge_key and best_by_key.get(str(merge_key)) is record)


def canonical_rank(record: QuestionBankRecord) -> tuple[int, str]:
    status_rank = {
        "verified_correct": 4,
        "unverified_selected": 2,
        "verified_wrong": 1,
        "unsubmitted": 0,
    }.get(record.answer.status, 0)
    return status_rank, str(record.answer.attempt_at or "")


def answer_status_counts(records: list[QuestionBankRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.answer.status] = counts.get(record.answer.status, 0) + 1
    return counts


def render_historical_quiz_bank_markdown(
    records: list[QuestionBankRecord],
    pacing: dict[str, Any] | None = None,
) -> str:
    lines = ["# TMS Historical Quiz Question Bank", ""]
    counts = answer_status_counts(records)
    lines.append(f"- Records: {len(records)}")
    for status in ("verified_correct", "verified_wrong", "unverified_selected", "unsubmitted"):
        lines.append(f"- {status}: {counts.get(status, 0)}")
    if pacing:
        lines.append(f"- Pacing: {'enabled' if pacing.get('enabled') else 'disabled'}")
        lines.append(f"- PacingDelayMs: {pacing.get('min_ms', 0)}-{pacing.get('max_ms', 0)}")
        lines.append(f"- PacingSleepCount: {pacing.get('sleep_count', 0)}")
        lines.append(f"- PacingTotalSleepSeconds: {pacing.get('total_sleep_seconds', 0)}")
        label_counts = pacing.get("label_counts")
        if isinstance(label_counts, dict) and label_counts:
            labels = ", ".join(f"{key}={value}" for key, value in sorted(label_counts.items()))
            lines.append(f"- PacingLabelCounts: {labels}")
    lines.append("")

    current_course = None
    current_activity = None
    for record in records:
        course_key = (record.course.course_id, record.course.title)
        activity_key = (record.activity.activity_id, record.activity.title)
        if course_key != current_course:
            current_course = course_key
            current_activity = None
            lines.append(f"## {record.course.title}")
            lines.append(f"- CourseId: {record.course.course_id or ''}")
            lines.append("")
        if activity_key != current_activity:
            current_activity = activity_key
            lines.append(f"### {record.activity.title}")
            lines.append(f"- ActivityId: {record.activity.activity_id or ''}")
            lines.append(f"- PassingCondition: {record.activity.pass_condition or ''}")
            lines.append("")

        lines.append(f"- Status: {record.answer.status}")
        lines.append(f"  Verification: {record.assessment.get('verification_method') or ''}")
        if record.assessment.get("is_canonical"):
            lines.append("  Canonical: true")
        if record.attempt:
            lines.append(
                "  Attempt: "
                f"record_id={record.attempt.get('record_id') or ''}, "
                f"score={record.attempt.get('score') or ''}, "
                f"at={record.attempt.get('attempt_at') or ''}, "
                f"submitted={record.attempt.get('submitted_status') or ''}"
            )
        if record.question.get("text"):
            lines.append(f"  Question: {record.question['text']}")
        if record.question.get("options"):
            lines.append("  Options: " + " | ".join(record.question["options"]))
        if record.answer.selected_answers:
            lines.append("  Selected: " + " | ".join(record.answer.selected_answers))
        if record.question.get("correct_answers"):
            lines.append("  Correct: " + " | ".join(record.question["correct_answers"]))
        if record.issues:
            lines.append("  Issues: " + " | ".join(issue.code for issue in record.issues))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_probe_markdown(records: list[QuestionBankRecord], pacing: dict[str, Any] | None = None) -> str:
    lines = ["# TMS Playwright Question Bank Probe", ""]
    lines.append(f"- Records: {len(records)}")
    lines.append(f"- Available: {sum(1 for record in records if record.answer.status == 'available')}")
    lines.append(f"- Unavailable: {sum(1 for record in records if record.answer.status != 'available')}")
    if pacing:
        lines.append(f"- Pacing: {'enabled' if pacing.get('enabled') else 'disabled'}")
        lines.append(f"- PacingDelayMs: {pacing.get('min_ms', 0)}-{pacing.get('max_ms', 0)}")
        lines.append(f"- PacingSleepCount: {pacing.get('sleep_count', 0)}")
        lines.append(f"- PacingTotalSleepSeconds: {pacing.get('total_sleep_seconds', 0)}")
        label_counts = pacing.get("label_counts")
        if isinstance(label_counts, dict) and label_counts:
            labels = ", ".join(f"{key}={value}" for key, value in sorted(label_counts.items()))
            lines.append(f"- PacingLabelCounts: {labels}")
    lines.append("")
    for record in records:
        lines.append(f"## {record.course.title} / {record.activity.title}")
        lines.append(f"- CourseId: {record.course.course_id or ''}")
        lines.append(f"- ActivityId: {record.activity.activity_id or ''}")
        lines.append(f"- Kind: {record.activity.kind}")
        lines.append(f"- AnswerStatus: {record.answer.status}")
        if record.attempt:
            lines.append(f"- AttemptRecordId: {record.attempt.get('record_id') or ''}")
            lines.append(f"- AttemptStatus: {record.attempt.get('submitted_status') or ''}")
        if record.answer.score:
            lines.append(f"- Score: {record.answer.score}")
        if record.question.get("text"):
            lines.append(f"- Question: {record.question['text']}")
        if record.question.get("options"):
            lines.append("- Options: " + " | ".join(record.question["options"]))
        if record.answer.selected_answers:
            lines.append("- Answer: " + " | ".join(record.answer.selected_answers))
        if record.issues:
            lines.append("- Issues: " + " | ".join(issue.code for issue in record.issues))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _row_locator_for_title(page, title: str):
    escaped = _xpath_literal(title)
    return page.locator(
        f"xpath=//*[contains(normalize-space(.), {escaped}) and (self::tr or self::li or self::div)]"
    ).first


def _xpath_literal(value: str) -> str:
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
    "DEFAULT_HISTORICAL_QUIZ_JSONL_PATH",
    "DEFAULT_HISTORICAL_QUIZ_MARKDOWN_PATH",
    "DEFAULT_PLAYWRIGHT_JSONL_PATH",
    "DEFAULT_PLAYWRIGHT_MARKDOWN_PATH",
    "ActivityProbe",
    "ExtractedQuestion",
    "HistoricalQuizBankResult",
    "KExamAttempt",
    "PlaywrightProbeResult",
    "answer_status_counts",
    "build_historical_quiz_records",
    "export_question_bank_playwright",
    "export_historical_quiz_bank_playwright",
    "extract_activity_probe_from_html",
    "extract_kexam_attempts_from_modal_html",
    "extract_questions_from_soup",
    "historical_merge_key",
    "kexam_attempt_to_dict",
    "parse_include",
    "probe_kexam_attempt_with_page",
    "probe_activity_playwright",
    "render_historical_quiz_bank_markdown",
]
