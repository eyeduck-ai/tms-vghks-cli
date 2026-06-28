from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse

from .handlers import (
    RunOptions,
    TmsRunner,
    apply_quiz_answers,
    collect_quiz_questions,
    missing_required_quiz_answers,
)
from .kexam_common import (
    DEFAULT_KEXAM_COURSE,
    DEFAULT_KEXAM_EXAM_URL,
    KEXAM_CONTINUE_LABELS,
    KEXAM_SUBMIT_LABELS,
    KExamExamPageParse,
    KExamExamPageReadResult,
    KExamResubmitDiagnosticResult,
    attempt_dict_record_id,
    best_record_attempt,
    build_kexam_resubmit_verification,
    parse_kexam_exam_page_html,
    probe_to_dict,
)
from .models import AuthOptions, CourseDetail, CourseItem, ItemKind, OperationBackend
from .parsers import normalize_text
from .playwright_probe import extract_kexam_attempts_from_modal_html, kexam_attempt_to_dict, probe_kexam_attempt_with_page
from .privacy import redact_sensitive_url
from .quiz import QuestionBank, QuizQuestion
from .quiz_resolver import GeminiQuizConfig, resolve_quiz_answers
from .session import TmsSession


def read_kexam_exam_page_playwright(
    session: TmsSession,
    exam_url: str = DEFAULT_KEXAM_EXAM_URL,
    include_unsubmitted_records: bool = True,
    headless: bool = False,
) -> KExamExamPageReadResult:
    session.start_browser(headless=headless)
    assert session.context is not None
    session.sync_cookies_to_browser()
    page = session.context.new_page()
    try:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(exam_url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        parsed = parse_kexam_exam_page_html(page.content(), exam_url, session.base_url)
        issues: list[str] = []
        modal_html = _click_attempt_records_and_capture_html(page)
        attempts = extract_kexam_attempts_from_modal_html(modal_html, session.base_url) if modal_html else []
        if not attempts:
            attempts = extract_kexam_attempts_from_modal_html(page.content(), session.base_url)
        if not attempts and parsed.attempt_count:
            issues.append("attempt_record_modal_unavailable")

        best_attempt = best_record_attempt(parsed, attempts, session.base_url)
        record_probes = [
            probe_to_dict(
                probe_kexam_attempt_with_page(
                    page,
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
                probe_kexam_attempt_with_page(
                    page,
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
    finally:
        try:
            page.close()
        except Exception:
            pass


def run_playwright_quiz_resubmit_diagnostic(
    session: TmsSession,
    course: str = DEFAULT_KEXAM_COURSE,
    exam_url: str = DEFAULT_KEXAM_EXAM_URL,
    quiz_policy: str = "auto",
    question_bank_path: str | None = None,
    auth_options: AuthOptions | None = None,
    headless: bool = False,
    gemini_config: GeminiQuizConfig | None = None,
) -> KExamResubmitDiagnosticResult:
    before = read_kexam_exam_page_playwright(
        session,
        exam_url=exam_url,
        include_unsubmitted_records=True,
        headless=headless,
    )
    course_detail = _load_course_detail(session, course, exam_url)
    item = _diagnostic_quiz_item(course_detail, exam_url)
    options = RunOptions(
        quiz_policy=quiz_policy,
        question_bank_path=question_bank_path,
        interactive=False,
        backend=OperationBackend.PLAYWRIGHT,
        auth_options=auth_options or AuthOptions(),
        headless=headless,
        gemini_config=gemini_config or GeminiQuizConfig(),
    )
    runner = TmsRunner(session, options)
    submit_result = _submit_kexam_from_exam_page(
        session=session,
        runner=runner,
        course=course_detail,
        item=item,
        exam_url=exam_url,
        quiz_policy=quiz_policy,
        headless=headless,
    )
    after = read_kexam_exam_page_playwright(
        session,
        exam_url=exam_url,
        include_unsubmitted_records=True,
        headless=headless,
    )
    verification = build_kexam_resubmit_verification(
        before,
        after,
        expected_question_count=int(submit_result.get("question_count") or 0),
        expected_selected_answer_count=int(submit_result.get("selected_answer_count") or 0),
    )
    success = submit_result.get("success", False) and verification["status"] == "resubmit_verified"
    status = "resubmit_verified" if success else verification["status"]
    if not submit_result.get("success", False) and status == "resubmit_verified":
        status = "submit_failed"
    issues = [*list(submit_result.get("issues", [])), *verification.get("verification_issues", [])]
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


def _submit_kexam_from_exam_page(
    session: TmsSession,
    runner: TmsRunner,
    course: CourseDetail,
    item: CourseItem,
    exam_url: str,
    quiz_policy: str,
    headless: bool,
) -> dict[str, Any]:
    session.start_browser(headless=headless)
    assert session.context is not None
    session.sync_cookies_to_browser()
    page = session.context.new_page()
    try:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(exam_url, wait_until="domcontentloaded", timeout=60000)
        page = _click_control_and_return_active_page(page, KEXAM_CONTINUE_LABELS)
        questions = _collect_kexam_take_questions(page, session.base_url) or collect_quiz_questions(page)
        if not questions:
            return {
                "success": False,
                "status": "continue_entry_unavailable",
                "question_count": 0,
                "selected_answer_count": 0,
                "issues": ["continue_entry_unavailable"],
            }
        selected_result = _select_quiz_answers(
            question_bank=runner.question_bank,
            questions=questions,
            course_title=course.title,
            item_title=item.title,
            quiz_policy=quiz_policy,
            gemini_config=runner.options.gemini_config,
        )
        if selected_result["missing"]:
            return {
                "success": False,
                "status": "quiz_answers_unavailable",
                "question_count": len(questions),
                "selected_answer_count": selected_result["selected_answer_count"],
                "answer_sources": selected_result["answer_sources"],
                "answer_source_counts": selected_result["answer_source_counts"],
                "issues": [
                    *selected_result["issues"],
                    "missing_required:" + ",".join(selected_result["missing"][:20]),
                ],
            }
        apply_quiz_answers(page, questions, selected_result["selected"])
        missing = missing_required_quiz_answers(page, questions)
        if missing:
            return {
                "success": False,
                "status": "quiz_required_groups_missing",
                "question_count": len(questions),
                "selected_answer_count": selected_result["selected_answer_count"],
                "issues": ["missing_required:" + ",".join(missing[:20])],
            }
        clicked = _click_first_visible_text(page, KEXAM_SUBMIT_LABELS, timeout=5000)
        if not clicked:
            return {
                "success": False,
                "status": "submit_button_unavailable",
                "question_count": len(questions),
                "selected_answer_count": selected_result["selected_answer_count"],
                "issues": ["submit_button_unavailable"],
            }
        confirmed = _click_visible_dialog_button(page, ("交卷", "確定", "送出"), timeout=5000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            try:
                page.wait_for_timeout(2000)
            except Exception:
                pass
        result_text = normalize_text(page.locator("body").inner_text(timeout=10000))
        return {
            "success": True,
            "status": "submitted",
            "question_count": len(questions),
            "selected_answer_count": selected_result["selected_answer_count"],
            "answer_sources": selected_result["answer_sources"],
            "answer_source_counts": selected_result["answer_source_counts"],
            "confirmed_submit": confirmed,
            "result_text": result_text[:500],
            "issues": selected_result["issues"],
        }
    finally:
        try:
            page.close()
        except Exception:
            pass


def _select_quiz_answers(
    question_bank: QuestionBank | None,
    questions: list[QuizQuestion],
    course_title: str,
    item_title: str,
    quiz_policy: str,
    gemini_config: GeminiQuizConfig | None = None,
) -> dict[str, Any]:
    resolution = resolve_quiz_answers(
        questions=questions,
        course_title=course_title,
        item_title=item_title,
        question_bank=question_bank,
        quiz_policy=quiz_policy,
        gemini_config=gemini_config,
    )
    return {
        "selected": resolution.answers,
        "missing": resolution.missing,
        "selected_answer_count": resolution.selected_answer_count,
        "answer_sources": resolution.sources,
        "answer_source_counts": resolution.source_counts,
        "issues": resolution.issues,
    }


def _collect_kexam_take_questions(page, base_url: str) -> list[QuizQuestion]:
    try:
        from .requests_kexam import parse_kexam_take_page_html

        take = parse_kexam_take_page_html(page.content(), str(getattr(page, "url", "")), base_url)
    except Exception:
        return []
    questions: list[QuizQuestion] = []
    for question in take.questions:
        if not question.text or not question.display_options:
            continue
        questions.append(
            QuizQuestion(
                text=question.text,
                options=question.display_options,
                name=_kexam_dom_group_name(page, question.kques_id),
                multiple=False,
            )
        )
    return questions


def _kexam_dom_group_name(page, kques_id: str) -> str:
    fallback = f"kques_option_{kques_id}"
    try:
        name = page.evaluate(
            """
            (kquesId) => {
              const controls = Array.from(document.querySelectorAll('input[type="radio"], input[type="checkbox"]'));
              const match = controls.find((input) => (input.name || '').includes(kquesId));
              return match ? match.name : '';
            }
            """,
            str(kques_id),
        )
    except Exception:
        return fallback
    return str(name or fallback)


def _load_course_detail(session: TmsSession, course: str, exam_url: str) -> CourseDetail:
    try:
        return session.get_course_detail_playwright(course)
    except Exception:
        parsed = urlparse(exam_url)
        match = re.search(r"/course/(\d+)/", parsed.path)
        course_id = match.group(1) if match else str(course)
        return CourseDetail(title=f"course-{course_id}", url=f"{session.base_url}/course/{course_id}", course_id=course_id)


def _diagnostic_quiz_item(course: CourseDetail, exam_url: str) -> CourseItem:
    exam_id = _exam_activity_id_from_exam_url(exam_url)
    for item in course.items:
        if item.detail_url == exam_url:
            return item
        if exam_id and str(item.metadata.get("activity_id") or "") == exam_id:
            return item
    title = _infer_quiz_title_from_course(course) or "KExam"
    return CourseItem(title=title, order=None, kind=ItemKind.QUIZ, detail_url=exam_url)


def _infer_quiz_title_from_course(course: CourseDetail) -> str:
    for item in course.items:
        if ItemKind(item.kind) == ItemKind.QUIZ and item.title:
            return item.title
    return ""


def _exam_activity_id_from_exam_url(exam_url: str) -> str | None:
    match = re.search(r"/exam/(\d+)", urlparse(exam_url).path)
    return match.group(1) if match else None


def _click_attempt_records_and_capture_html(page) -> str:
    candidates = (
        page.get_by_text("紀錄", exact=True).first,
        page.locator("a", has_text="紀錄").first,
    )
    for target in candidates:
        try:
            if target.is_visible(timeout=1000):
                target.click()
                page.wait_for_timeout(1000)
                return _visible_dialog_or_page_html(page)
        except Exception:
            continue
    return ""


def _visible_dialog_or_page_html(page) -> str:
    try:
        return page.evaluate(
            """
            () => {
              const candidates = Array.from(document.querySelectorAll(
                '.modal, .ui-dialog, [role="dialog"], .bootbox, .layui-layer'
              ));
              const visible = candidates.reverse().find((node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              });
              return visible ? visible.outerHTML : document.documentElement.outerHTML;
            }
            """
        )
    except Exception:
        return page.content()


def _click_control_and_return_active_page(page, labels: tuple[str, ...]):
    context = page.context
    before_ids = {id(candidate) for candidate in context.pages}
    clicked = _click_first_visible_text(page, labels, timeout=5000)
    if not clicked:
        return page
    deadline = time.monotonic() + 10.0
    fallback = None
    while time.monotonic() < deadline:
        for candidate in reversed(context.pages):
            try:
                if candidate.is_closed():
                    continue
            except Exception:
                continue
            is_new_page = id(candidate) not in before_ids
            is_take_page = "/kexam/" in candidate.url and "/take" in candidate.url
            if candidate is page and not is_take_page and not _page_has_quiz_controls(candidate):
                continue
            if is_new_page or is_take_page or _page_has_quiz_controls(candidate):
                fallback = candidate
                try:
                    candidate.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                if _page_has_quiz_controls(candidate):
                    return candidate
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    if fallback:
        return fallback
    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass
    return page


def _page_has_quiz_controls(page) -> bool:
    try:
        return page.locator('input[type="radio"], input[type="checkbox"]').count() > 0
    except Exception:
        return False


def _click_first_visible_text(page, labels: tuple[str, ...], timeout: int) -> bool:
    for label in labels:
        for locator in (
            page.get_by_role("button", name=re.compile(label)).first,
            page.get_by_text(label, exact=False).first,
        ):
            try:
                if locator.is_visible(timeout=timeout):
                    locator.click()
                    return True
            except Exception:
                continue
    return False


def _click_visible_dialog_button(page, labels: tuple[str, ...], timeout: int) -> bool:
    deadline = time.monotonic() + (timeout / 1000.0)
    dialog_selectors = (
        ".modal:visible",
        ".ui-dialog:visible",
        "[role=\"dialog\"]:visible",
        ".bootbox:visible",
        ".layui-layer:visible",
        ".swal2-container:visible",
    )
    while time.monotonic() < deadline:
        for label in labels:
            for selector in (
                f'.bootbox button.btn-primary:has-text("{label}")',
                f'.modal button.btn-primary:has-text("{label}")',
                f'[role="dialog"] button:has-text("{label}")',
                f'.layui-layer button:has-text("{label}")',
                f'.swal2-container button:has-text("{label}")',
            ):
                locator = page.locator(selector).last
                try:
                    if locator.is_visible(timeout=250):
                        locator.click()
                        return True
                except Exception:
                    continue
        for selector in dialog_selectors:
            dialog = page.locator(selector).last
            try:
                if not dialog.is_visible(timeout=250):
                    continue
            except Exception:
                continue
            for label in labels:
                for locator in (
                    dialog.get_by_role("button", name=re.compile(label)).last,
                    dialog.get_by_text(label, exact=False).last,
                ):
                    try:
                        if locator.is_visible(timeout=250):
                            locator.click()
                            return True
                    except Exception:
                        continue
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
    return False


__all__ = [
    "DEFAULT_KEXAM_COURSE",
    "DEFAULT_KEXAM_EXAM_URL",
    "KExamExamPageParse",
    "KExamExamPageReadResult",
    "KExamResubmitDiagnosticResult",
    "build_kexam_resubmit_verification",
    "parse_kexam_exam_page_html",
    "read_kexam_exam_page_playwright",
    "run_playwright_quiz_resubmit_diagnostic",
]
