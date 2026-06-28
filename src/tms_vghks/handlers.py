from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import AuthOptions, CourseDetail, CourseItem, ItemKind, ItemState, OperationBackend, RunResult, SiteState
from .parsers import (
    normalize_text,
)
from .quiz import QuestionBank, QuizQuestion, find_latest_question_bank_path, sanitized_question_bank_snippet, suggest_answer
from .quiz_resolver import GeminiQuizConfig, question_key, resolve_quiz_answers
from .scheduler import (
    find_matching_item,
    first_incomplete_item,
    get_course_detail_for_runner,
    item_is_complete,
    list_pending_courses_for_runner,
    new_worker_session,
    next_adaptive_limit,
    run_course_until_complete,
    run_course_with_worker,
    run_item_for_scheduler,
    run_scheduler_for_runner,
    selected_backend,
    serialize_run_result,
)
from .session import TmsError, TmsSession, TransientTmsError
from .survey_text import DEFAULT_NEUTRAL_SURVEY_TEXT
from .timeutils import parse_required_seconds, remaining_seconds


@dataclass(slots=True)
class RunOptions:
    concurrency: int = 4
    max_concurrency: int = 8
    adaptive: bool = True
    survey_policy: str = "neutral"
    quiz_policy: str = "confirm"
    question_bank_path: str | None = None
    question_bank_content: str | None = None
    export_question_bank_snippets: bool = False
    dry_run: bool = False
    interactive: bool = True
    headless: bool = False
    max_wait_seconds: int | None = None
    poll_interval_seconds: int = 30
    backend: OperationBackend = OperationBackend.REQUESTS
    auth_options: AuthOptions = field(default_factory=AuthOptions)
    gemini_config: GeminiQuizConfig = field(default_factory=GeminiQuizConfig)
    transient_retries: int = 3
    transient_delay_seconds: float = 2.0


@dataclass(slots=True)
class RunningReading:
    course: CourseDetail
    item: CourseItem
    page: Any
    deadline_monotonic: float
    started_at_monotonic: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class RequestsReading:
    course: CourseDetail
    item: CourseItem
    activity_url: str | None
    html: str
    endpoint_verified: bool = False
    reason: str = "requests reading completion endpoint is not verified"
    deadline_monotonic: float = field(default_factory=time.monotonic)
    started_at_monotonic: float = field(default_factory=time.monotonic)


class TmsRunner:
    def __init__(self, session: TmsSession, options: RunOptions | None = None) -> None:
        self.session = session
        self.options = options or RunOptions()
        if hasattr(self.session, "use_backend"):
            self.session.use_backend(self.options.backend)
        self.options.auth_options.headless = self.options.headless
        self.options.auth_options.transient_retries = self.options.transient_retries
        self.options.auth_options.transient_delay_seconds = self.options.transient_delay_seconds
        if hasattr(self.session, "browser_headless"):
            self.session.browser_headless = self.options.headless
        self.session.configure_transient_policy(self.options.transient_retries, self.options.transient_delay_seconds)
        self.question_bank = self._load_question_bank()

    def run_item(self, course: CourseDetail, item: CourseItem) -> RunResult:
        try:
            if item_is_complete(item):
                return RunResult(True, ItemState.PASSED, "item already passed", course=course, item=item)
            if self.options.dry_run:
                return RunResult(True, item.state, "dry run; no live action performed", course=course, item=item)
            backend = OperationBackend(self.options.backend)
            if backend == OperationBackend.REQUESTS:
                return self.run_item_requests(course, item)
            if backend == OperationBackend.HYBRID:
                requests_result = self.run_item_requests(course, item)
                if requests_result.success or not _should_fallback_to_playwright(requests_result, item, self.options):
                    return requests_result
                try:
                    playwright_result = self.run_item_playwright(course, item)
                except TransientTmsError:
                    raise
                except Exception as exc:
                    requests_result.data["playwright_fallback_error"] = str(exc)
                    return requests_result
                playwright_result.data.setdefault("requests_attempt", serialize_run_result(requests_result))
                return playwright_result
            return self.run_item_playwright(course, item)
        except TransientTmsError as exc:
            return RunResult(False, SiteState.TRANSIENT_ERROR, str(exc), course=course, item=item)

    def run_item_playwright(self, course: CourseDetail, item: CourseItem) -> RunResult:
        try:
            if item.kind in {ItemKind.READING, ItemKind.VIDEO}:
                return self.run_reading_playwright(course, item)
            if item.kind == ItemKind.SURVEY:
                return self.run_survey_playwright(course, item)
            if item.kind == ItemKind.QUIZ:
                return self.run_quiz_playwright(course, item)
            return RunResult(False, ItemState.BLOCKED, "unknown item kind; user action required", course=course, item=item)
        except TransientTmsError as exc:
            return RunResult(False, SiteState.TRANSIENT_ERROR, str(exc), course=course, item=item)

    def run_course(self, course_or_url: CourseDetail | str) -> list[RunResult]:
        course = course_or_url if isinstance(course_or_url, CourseDetail) else self._get_course_detail(course_or_url)
        results, _record = self._run_course_until_complete(course, course_index=0, worker_index=0)
        return results

    def run_scheduler(self) -> RunResult:
        return run_scheduler_for_runner(self)

    def _run_course_with_worker(self, course: CourseDetail, course_index: int, worker_index: int) -> dict[str, Any]:
        return run_course_with_worker(self, course, course_index, worker_index, TmsRunner)

    def _new_worker_session(self) -> TmsSession:
        return new_worker_session(self.session)

    def _run_course_until_complete(
        self,
        course: CourseDetail,
        course_index: int,
        worker_index: int | None,
    ) -> tuple[list[RunResult], dict[str, Any]]:
        return run_course_until_complete(self, course, course_index, worker_index)

    def _run_item_for_scheduler(
        self,
        course: CourseDetail,
        item: CourseItem,
        record: dict[str, Any],
        worker_index: int | None,
    ) -> RunResult:
        return run_item_for_scheduler(self, course, item, record, worker_index)

    def _selected_backend(self) -> OperationBackend:
        return selected_backend(self.options)

    def _list_pending_courses(self) -> list[Any]:
        return list_pending_courses_for_runner(self)

    def _get_course_detail(self, course: str) -> CourseDetail:
        return get_course_detail_for_runner(self, course)

    def run_item_requests(self, course: CourseDetail, item: CourseItem) -> RunResult:
        try:
            kind = ItemKind(item.kind)
            if kind in {ItemKind.READING, ItemKind.VIDEO}:
                return self.run_reading_requests(course, item)
            if kind == ItemKind.SURVEY:
                return self.run_survey_requests(course, item)
            if kind == ItemKind.QUIZ:
                return self.run_quiz_requests(course, item)
            return RunResult(
                False,
                "mutation_unsupported",
                "requests backend has no live mutation path for this item kind",
                course=course,
                item=item,
                data={
                    "capability": "mutation_unsupported",
                    "backend": "requests",
                    "item_kind": str(item.kind),
                },
            )
        except TransientTmsError:
            raise
        except Exception as exc:
            return RunResult(
                False,
                "read_only_unavailable",
                str(exc),
                course=course,
                item=item,
                data={
                    "capability": "read_only_unavailable",
                    "backend": "requests",
                    "item_kind": str(item.kind),
                },
            )

    def run_reading(self, course: CourseDetail, item: CourseItem) -> RunResult:
        return self._run_item_method_for_selected_backend(
            course,
            item,
            requests_method=self.run_reading_requests,
            playwright_method=self.run_reading_playwright,
        )

    def run_reading_playwright(self, course: CourseDetail, item: CourseItem) -> RunResult:
        running = self.start_reading_playwright(course, item)
        return self.finish_reading_playwright(running)

    def run_reading_requests(self, course: CourseDetail, item: CourseItem) -> RunResult:
        running = self.start_reading_requests(course, item)
        return self.finish_reading_requests(running)

    def start_reading(self, course: CourseDetail, item: CourseItem) -> RunningReading | RequestsReading:
        if self._selected_backend() == OperationBackend.PLAYWRIGHT:
            return self.start_reading_playwright(course, item)
        return self.start_reading_requests(course, item)

    def start_reading_playwright(self, course: CourseDetail, item: CourseItem) -> RunningReading:
        self.session.ensure_authenticated(self.options.auth_options)
        self.session.start_browser(headless=self.options.headless)
        assert self.session.context is not None
        self.session.sync_cookies_to_browser()
        page = self.session.context.new_page()
        self._open_item_page(page, course, item)
        recovered = self._recover_known_transient_dialog(page, course, item)
        if recovered:
            course, item = recovered
        required = parse_required_seconds(item.pass_condition)
        remaining = remaining_seconds(required, item.result)
        if remaining is None:
            remaining = self._remaining_from_live_page(page)
        if remaining is None:
            remaining = 0
        if self.options.max_wait_seconds is not None:
            remaining = min(remaining, self.options.max_wait_seconds)
        return RunningReading(course=course, item=item, page=page, deadline_monotonic=time.monotonic() + remaining)

    def start_reading_requests(self, course: CourseDetail, item: CourseItem) -> RequestsReading:
        self.session.ensure_authenticated(self.options.auth_options)
        activity_url = item.detail_url or course.url
        html = self.session.fetch_activity_html_requests(activity_url, referer=course.url)
        required = parse_required_seconds(item.pass_condition)
        remaining = remaining_seconds(required, item.result)
        if remaining is None:
            remaining = self._remaining_from_activity_html(html)
        if remaining is None:
            remaining = 0
        if self.options.max_wait_seconds is not None:
            remaining = min(remaining, self.options.max_wait_seconds)
        return RequestsReading(
            course=course,
            item=item,
            activity_url=activity_url,
            html=html,
            endpoint_verified=False,
            reason="requests reading completion endpoint is not verified by network diagnostics",
            deadline_monotonic=time.monotonic() + remaining,
        )

    def finish_reading(self, running: RunningReading | RequestsReading) -> RunResult:
        if isinstance(running, RequestsReading):
            return self.finish_reading_requests(running)
        return self.finish_reading_playwright(running)

    def finish_reading_playwright(self, running: RunningReading) -> RunResult:
        seconds = max(0.0, running.deadline_monotonic - time.monotonic())
        while seconds > 0:
            time.sleep(min(seconds, max(1, self.options.poll_interval_seconds)))
            seconds = max(0.0, running.deadline_monotonic - time.monotonic())
        page = running.page
        self._click_first_visible(page, ("結束閱讀", "完成", "結束", "確定", "送出"), timeout=3000, required=False)
        self._recover_known_transient_dialog(page, running.course, running.item)
        self.session.sync_cookies_to_requests()
        try:
            refreshed = self._get_course_detail(running.course.url)
            item = find_matching_item(refreshed, running.item)
            if item and item_is_complete(item):
                return RunResult(True, ItemState.PASSED, "reading item verified from course detail", course=refreshed, item=item)
            if item and item.state == ItemState.IN_PROGRESS:
                return RunResult(False, ItemState.IN_PROGRESS, "reading progress visible but not complete", course=refreshed, item=item)
            return RunResult(False, ItemState.BLOCKED, "reading item was not verified after wait", course=refreshed, item=item or running.item)
        finally:
            try:
                page.close()
            except Exception:
                pass

    def finish_reading_requests(self, running: RequestsReading) -> RunResult:
        from .requests_watch_time import run_requests_watch_time

        wait_seconds = max(0, int(round(running.deadline_monotonic - time.monotonic())))
        result = run_requests_watch_time(
            self.session,
            running.course,
            running.item,
            wait_seconds=wait_seconds,
        )
        data = {
            "capability": result.status,
            "backend": "requests",
            "before_result": result.before_result,
            "after_result": result.after_result,
            "before_result_seconds": result.before_result_seconds,
            "after_result_seconds": result.after_result_seconds,
            "progress_increased": result.progress_increased,
            "waited_seconds": result.waited_seconds,
            "media_url": result.media_url,
            "watch_time_url": result.watch_time_url,
            "endpoint_summary": result.endpoint_summary,
            "response_status_code": result.response_status_code,
            "response_json_summary": result.response_json_summary,
            "requests_reproduction_status": result.requests_reproduction_status,
            "verification_strength": "course_detail" if result.success else "none",
            "verification_method": "watch_time_progress_increased" if result.success else result.status,
            "record_id": "",
            "score": None,
            "submit_time": None,
            "issues": result.issues,
        }
        refreshed = running.course
        item = running.item
        try:
            refreshed = self._get_course_detail(running.course.url)
            item = find_matching_item(refreshed, running.item) or running.item
        except Exception:
            pass
        if result.status == "already_passed":
            return RunResult(
                True,
                ItemState.PASSED,
                "reading item already passed",
                course=refreshed,
                item=item or running.item,
                data=data,
            )
        if result.success:
            return RunResult(
                True,
                item.state if item else ItemState.IN_PROGRESS,
                "requests watchTime verified from course detail",
                course=refreshed,
                item=item,
                data=data,
            )
        return RunResult(
            False,
            result.status,
            _requests_watch_time_message(result.status, running.reason),
            course=refreshed,
            item=item or running.item,
            data=data,
        )

    def run_survey(self, course: CourseDetail, item: CourseItem) -> RunResult:
        return self._run_item_method_for_selected_backend(
            course,
            item,
            requests_method=self.run_survey_requests,
            playwright_method=self.run_survey_playwright,
        )

    def run_survey_playwright(self, course: CourseDetail, item: CourseItem) -> RunResult:
        if self.options.survey_policy != "neutral":
            return RunResult(False, ItemState.BLOCKED, "survey completion not authorized", course=course, item=item)
        page = self._new_item_page(course, item)
        filled = page.evaluate(
            """
            (neutralText) => {
              const radioGroups = new Map();
              document.querySelectorAll('input[type="radio"]').forEach((el) => {
                const name = el.name || el.getAttribute('data-name') || el.id;
                if (!name) return;
                if (!radioGroups.has(name)) radioGroups.set(name, []);
                radioGroups.get(name).push(el);
              });
              for (const group of radioGroups.values()) {
                const target = group[Math.floor(group.length / 2)];
                if (target && !target.checked) target.click();
              }
              document.querySelectorAll('textarea, input[type="text"]').forEach((el) => {
                if (!el.value) {
                  el.focus();
                  el.value = neutralText;
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                  el.blur();
                }
              });
              document.querySelectorAll('[contenteditable="true"]').forEach((el) => {
                if (!el.textContent.trim()) {
                  el.focus();
                  el.textContent = neutralText;
                  el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: neutralText }));
                  el.blur();
                }
              });
              return { radioGroups: radioGroups.size };
            }
            """,
            DEFAULT_NEUTRAL_SURVEY_TEXT,
        )
        counter = normalize_text(page.locator("body").inner_text(timeout=5000))
        match = re.search(r"已填寫[:：]\s*(\d+)\s*/\s*(\d+)", counter)
        if match and int(match.group(1)) < int(match.group(2)):
            return RunResult(False, ItemState.BLOCKED, "survey validation counter is still short", course=course, item=item, data=filled)
        self._click_first_visible(page, ("送出", "提交", "確定"), timeout=5000, required=True)
        self._recover_known_transient_dialog(page, course, item)
        self.session.sync_cookies_to_requests()
        refreshed = self._get_course_detail(course.url)
        verified = find_matching_item(refreshed, item)
        if verified and item_is_complete(verified):
            return RunResult(True, ItemState.PASSED, "survey item verified", course=refreshed, item=verified)
        return RunResult(False, ItemState.BLOCKED, "survey submitted but not verified from detail row", course=refreshed, item=verified or item)

    def run_survey_requests(self, course: CourseDetail, item: CourseItem) -> RunResult:
        from .requests_form_submit import run_survey_requests_submit

        result = run_survey_requests_submit(
            self.session,
            course,
            item,
        )
        return _run_result_from_requests_form_submit(result, course, item)

    def run_quiz(self, course: CourseDetail, item: CourseItem) -> RunResult:
        if self.options.quiz_policy == "skip":
            return RunResult(False, ItemState.BLOCKED, "quiz skipped by policy", course=course, item=item)
        return self._run_item_method_for_selected_backend(
            course,
            item,
            requests_method=self.run_quiz_requests,
            playwright_method=self.run_quiz_playwright,
        )

    def run_quiz_playwright(self, course: CourseDetail, item: CourseItem) -> RunResult:
        page = self._new_item_page(course, item)
        questions = collect_quiz_questions(page)
        if not questions:
            return RunResult(False, ItemState.BLOCKED, "no quiz questions were detected", course=course, item=item)

        resolution = resolve_quiz_answers(
            questions=questions,
            course_title=course.title,
            item_title=item.title,
            question_bank=self.question_bank,
            quiz_policy=self.options.quiz_policy,
            gemini_config=self.options.gemini_config,
        )

        if resolution.missing:
            if self.options.quiz_policy == "confirm" and self.options.interactive:
                unresolved = [question for question in questions if question_key(question) in set(resolution.missing)]
                selected = dict(resolution.answers)
                selected.update(prompt_for_quiz_answers(unresolved))
            else:
                return RunResult(
                    False,
                    ItemState.BLOCKED,
                    "quiz has questions without resolved answers; confirmation required",
                    course=course,
                    item=item,
                    data={
                        "quiz_policy": self.options.quiz_policy,
                        "missing_required": list(resolution.missing),
                        "answer_sources": dict(resolution.sources),
                        "answer_source_counts": resolution.source_counts,
                        "answer_resolution_issues": list(resolution.issues),
                        "answer_resolution_notes": list(resolution.notes),
                        "questions": [
                            asdict(question)
                            for question in questions
                            if question_key(question) in set(resolution.missing)
                        ],
                    },
                )
        else:
            selected = dict(resolution.answers)

        apply_quiz_answers(page, questions, selected)
        missing = missing_required_quiz_answers(page, questions)
        if missing:
            return RunResult(
                False,
                ItemState.BLOCKED,
                "quiz required groups were not fully answered",
                course=course,
                item=item,
                data={"missing_required_groups": missing},
            )
        self._click_first_visible(page, ("送出", "提交", "確定", "交卷"), timeout=5000, required=True)
        result_text = normalize_text(page.locator("body").inner_text(timeout=10000))
        self._recover_known_transient_dialog(page, course, item)
        self.session.sync_cookies_to_requests()
        refreshed = self._get_course_detail(course.url)
        verified = find_matching_item(refreshed, item)
        passed = bool(verified and item_is_complete(verified)) or any(token in result_text for token in ("通過", "及格"))
        snippet = None
        if passed:
            snippet = sanitized_question_bank_snippet(course.title, item.title, questions, selected)
            record_probe_data = self._extract_latest_kexam_record_summary(refreshed, verified or item)
            record_probe_data["answer_sources"] = dict(resolution.sources)
            record_probe_data["answer_source_counts"] = resolution.source_counts
            if resolution.issues:
                record_probe_data["answer_resolution_issues"] = list(resolution.issues)
            if resolution.notes:
                record_probe_data["answer_resolution_notes"] = list(resolution.notes)
            return RunResult(
                True,
                ItemState.PASSED,
                "quiz passed or verified",
                course=refreshed,
                item=verified or item,
                data=record_probe_data,
                sanitized_question_bank_snippet=snippet,
            )
        return RunResult(False, ItemState.BLOCKED, "quiz did not verify as passed", course=refreshed, item=verified or item)

    def run_quiz_requests(self, course: CourseDetail, item: CourseItem) -> RunResult:
        from .requests_form_submit import run_quiz_requests_submit

        result = run_quiz_requests_submit(
            self.session,
            course,
            item,
            question_bank=self.question_bank,
            quiz_policy=self.options.quiz_policy,
            gemini_config=self.options.gemini_config,
        )
        return _run_result_from_requests_form_submit(result, course, item)

    def _new_item_page(self, course: CourseDetail, item: CourseItem):
        self.session.ensure_authenticated(self.options.auth_options)
        self.session.start_browser(headless=self.options.headless)
        assert self.session.context is not None
        self.session.sync_cookies_to_browser()
        page = self.session.context.new_page()
        self._open_item_page(page, course, item)
        return page

    def _open_item_page(self, page, course: CourseDetail, item: CourseItem) -> None:
        if item.detail_url:
            page.goto(item.detail_url, wait_until="domcontentloaded")
            return
        page.goto(course.url, wait_until="domcontentloaded")
        row = _row_locator_for_item(page, item)
        labels = ("開始閱讀", "繼續閱讀", "開始", "進入測驗", "填寫問卷", "觀看", "進入")
        for label in labels:
            try:
                target = row.get_by_text(label, exact=False).first
                if target.is_visible(timeout=1000):
                    target.click()
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    return
            except Exception:
                continue
        try:
            title_target = page.get_by_text(item.title, exact=False).first
            if title_target.is_visible(timeout=1000):
                title_target.click()
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                return
        except Exception:
            pass
        raise TmsError(f"could not find an item control for {item.title}")

    def _remaining_from_live_page(self, page) -> int | None:
        try:
            text = normalize_text(page.locator("body").inner_text(timeout=5000))
        except Exception:
            return None
        match = re.search(r"剩餘[:：]?\s*(\d{1,2}:\d{2}(?::\d{2})?)", text)
        if match:
            from .timeutils import parse_timer_to_seconds

            return parse_timer_to_seconds(match.group(1))
        return None

    def _remaining_from_activity_html(self, html: str) -> int | None:
        text = normalize_text(html)
        match = re.search(r"剩餘[:：]?\s*(\d{1,2}:\d{2}(?::\d{2})?)", text)
        if match:
            from .timeutils import parse_timer_to_seconds

            return parse_timer_to_seconds(match.group(1))
        return None

    def _dismiss_known_transient_dialog(self, page) -> bool:
        return self._recover_known_transient_dialog(page) is not None

    def _recover_known_transient_dialog(
        self,
        page,
        course: CourseDetail | None = None,
        item: CourseItem | None = None,
    ) -> tuple[CourseDetail, CourseItem] | None:
        if not self.session.page_has_transient_error(page):
            return None
        self.session.recover_transient_page(
            page,
            course.url if course else None,
            retries=self.options.transient_retries,
            delay_seconds=self.options.transient_delay_seconds,
        )
        if not course or not item:
            return None
        self.session.sync_cookies_to_requests()
        refreshed = self._get_course_detail(course.url)
        verified = find_matching_item(refreshed, item)
        if verified:
            return refreshed, verified
        return None

    def _click_first_visible(self, page, labels: tuple[str, ...], timeout: int, required: bool) -> bool:
        for label in labels:
            try:
                locator = page.get_by_role("button", name=re.compile(label)).first
                if locator.is_visible(timeout=timeout):
                    locator.click()
                    return True
            except Exception:
                pass
            try:
                locator = page.get_by_text(label, exact=False).first
                if locator.is_visible(timeout=timeout):
                    locator.click()
                    return True
            except Exception:
                pass
        if required:
            raise TmsError(f"none of the expected controls were visible: {labels}")
        return False

    def _load_question_bank(self) -> QuestionBank | None:
        if self.options.question_bank_content:
            return QuestionBank.from_markdown(self.options.question_bank_content)
        if self.options.question_bank_path:
            return QuestionBank.from_path(self.options.question_bank_path)
        latest = find_latest_question_bank_path()
        if latest:
            return QuestionBank.from_path(latest)
        return None

    def _extract_latest_kexam_record_summary(self, course: CourseDetail, item: CourseItem) -> dict[str, Any]:
        if ItemKind(item.kind) != ItemKind.QUIZ:
            return {}
        try:
            from .playwright_probe import probe_activity_playwright

            probe = probe_activity_playwright(
                self.session,
                course,
                item,
                include_unsubmitted_records=True,
            )
        except Exception as exc:
            return {"kexam_record_error": str(exc)}
        attempts = probe.attempt_probes or ([probe] if probe.attempt else [])
        return {
            "kexam_attempt_count": len(attempts),
            "kexam_record_question_count": sum(len(attempt.question_records) for attempt in attempts),
            "kexam_record_selected_answer_count": sum(
                len(question.selected_answers)
                for attempt in attempts
                for question in attempt.question_records
            ),
            "kexam_record_available": any(attempt.question_records for attempt in attempts),
        }

    def _run_item_method_for_selected_backend(
        self,
        course: CourseDetail,
        item: CourseItem,
        requests_method,
        playwright_method,
    ) -> RunResult:
        backend = self._selected_backend()
        if backend == OperationBackend.REQUESTS:
            return requests_method(course, item)
        if backend == OperationBackend.PLAYWRIGHT:
            return playwright_method(course, item)
        requests_result = requests_method(course, item)
        if requests_result.success or not _should_fallback_to_playwright(requests_result, item, self.options):
            return requests_result
        try:
            playwright_result = playwright_method(course, item)
        except TransientTmsError:
            raise
        except Exception as exc:
            requests_result.data["playwright_fallback_error"] = str(exc)
            return requests_result
        playwright_result.data.setdefault("requests_attempt", serialize_run_result(requests_result))
        return playwright_result


def _requests_watch_time_message(status: str, fallback: str) -> str:
    messages = {
        "endpoint_unverified": "requests could not resolve a verified media watchTime endpoint",
        "watch_time_missing_token": "requests could not parse the required watchTime token from the media page",
        "watch_time_post_failed": "requests watchTime POST failed",
        "watch_time_not_verified": "requests watchTime POST completed but course detail did not show increased time",
    }
    return messages.get(status, fallback)


def _run_result_from_requests_form_submit(result: Any, course: CourseDetail, item: CourseItem) -> RunResult:
    data = {
        "capability": result.status,
        "backend": "requests",
        "entry_url": result.entry_url,
        "form_action_url": result.form_action_url,
        "method": result.method,
        "form_summary": result.form_summary,
        "payload_keys": result.payload_keys,
        "question_count": result.question_count,
        "selected_answer_count": result.selected_answer_count,
        "answer_sources": result.answer_sources,
        "answer_source_counts": result.answer_source_counts,
        "answer_resolution_issues": result.answer_resolution_issues,
        "answer_resolution_notes": result.answer_resolution_notes,
        "response_status_code": result.response_status_code,
        "response_json_summary": result.response_json_summary,
        "requests_reproduction_status": result.requests_reproduction_status,
        "verification_strength": result.verification_strength,
        "verification_method": result.verification_method,
        "record_id": result.record_id,
        "score": result.score,
        "submit_time": result.submit_time,
        "entry_attempts": result.entry_attempts,
        "before": asdict(result.before) if result.before else None,
        "after": asdict(result.after) if result.after else None,
        "kexam_verified_record_ids": result.kexam_verified_record_ids,
        "kexam_unverified_record_ids": result.kexam_unverified_record_ids,
        "issues": result.issues,
    }
    if result.success:
        return RunResult(
            True,
            ItemState.PASSED,
            "requests form submit verified from course detail",
            course=course,
            item=item,
            data=data,
        )
    return RunResult(
        False,
        result.status,
        _requests_form_submit_message(result.status),
        course=course,
        item=item,
        data=data,
    )


def _requests_form_submit_message(status: str) -> str:
    messages = {
        "form_endpoint_unverified": "requests could not resolve a supported form submit endpoint",
        "form_missing_required_fields": "requests could not fill every required form field",
        "form_submit_failed": "requests form submit failed",
        "form_submit_not_verified": "requests form submit completed but course detail did not verify success",
        "requests_quiz_submit_course_detail_only": "requests quiz submit was verified from course detail only",
        "requests_submit_failed_record_blank": "requests KExam submit produced a blank or incomplete record",
        "requests_submit_response_failed_record_verified": "requests KExam submit response failed, but the saved record verified",
        "kexam_submit_not_verified": "requests KExam submit did not produce a verified saved record",
        "mutation_unsupported": "requests form submit probe did not submit",
    }
    return messages.get(status, "requests form submit did not complete")


def _should_fallback_to_playwright(result: RunResult, item: CourseItem, options: RunOptions) -> bool:
    if result.success:
        return False
    return str(result.state) in {
        "endpoint_unverified",
        "watch_time_missing_token",
        "watch_time_post_failed",
        "watch_time_not_verified",
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
        "mutation_unsupported",
        "read_only_unavailable",
    }


def collect_quiz_questions(page) -> list[QuizQuestion]:
    payload = page.evaluate(
        """
        () => {
          const groups = new Map();
          document.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach((input) => {
            const name = input.name || input.id;
            if (!name) return;
            if (!groups.has(name)) groups.set(name, []);
            const label = input.closest('label') || document.querySelector(`label[for="${input.id}"]`);
            const text = label ? label.innerText : (input.value || '');
            groups.get(name).push({ value: input.value || text, text, type: input.type });
          });
          const questions = [];
          for (const [name, options] of groups.entries()) {
            let node = document.querySelector(`[name="${CSS.escape(name)}"]`);
            let parent = node ? node.closest('tr, .question, .form-group, li, div') : null;
            let text = parent ? parent.innerText : name;
            for (const option of options) text = text.replace(option.text, '');
            questions.push({ name, text: text.trim() || name, options, multiple: options.some(o => o.type === 'checkbox') });
          }
          return questions;
        }
        """
    )
    questions: list[QuizQuestion] = []
    for row in payload:
        options = [normalize_text(option.get("text") or option.get("value")) for option in row.get("options", [])]
        options = [option for option in options if option]
        questions.append(
            QuizQuestion(
                text=normalize_text(row.get("text")),
                options=options,
                name=row.get("name"),
                multiple=bool(row.get("multiple")),
            )
        )
    return questions


def prompt_for_quiz_answers(questions: list[QuizQuestion]) -> dict[str, list[str]]:
    selected: dict[str, list[str]] = {}
    for question in questions:
        suggested = suggest_answer(question)
        print()
        print(f"題目: {question.text}")
        for index, option in enumerate(question.options, start=1):
            print(f"  {index}. {option}")
        print(f"建議答案: {', '.join(suggested)}")
        raw = input("請輸入要送出的選項編號，直接 Enter 使用建議答案: ").strip()
        if not raw:
            answers = suggested
        else:
            indexes = [int(part) for part in re.split(r"[,，\s]+", raw) if part]
            answers = [question.options[index - 1] for index in indexes if 1 <= index <= len(question.options)]
        selected[question.name or question.text] = answers
    return selected


def apply_quiz_answers(page, questions: list[QuizQuestion], selected: dict[str, list[str]]) -> None:
    mapping = {
        question.name or question.text: selected.get(question.name or question.text, [])
        for question in questions
    }
    page.evaluate(
        """
        (mapping) => {
          const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          for (const [name, answers] of Object.entries(mapping)) {
            const controls = Array.from(document.querySelectorAll(`[name="${CSS.escape(name)}"]`));
            for (const answer of answers) {
              const normalizedAnswer = norm(answer);
              const target = controls.find((input) => {
                const label = input.closest('label') || document.querySelector(`label[for="${input.id}"]`);
                const text = label ? norm(label.innerText) : norm(input.value);
                return text === normalizedAnswer || input.value === answer || text.includes(normalizedAnswer);
              });
              if (target && !target.checked) target.click();
            }
          }
        }
        """,
        mapping,
    )


def missing_required_quiz_answers(page, questions: list[QuizQuestion]) -> list[str]:
    names = [question.name for question in questions if question.name]
    if not names:
        return []
    missing = page.evaluate(
        """
        (names) => {
          const missing = [];
          for (const name of names) {
            const controls = Array.from(document.querySelectorAll(`[name="${CSS.escape(name)}"]`));
            if (!controls.length) continue;
            const required = controls.some((input) => input.required) || controls.length > 1;
            if (required && !controls.some((input) => input.checked)) missing.push(name);
          }
          return missing;
        }
        """,
        names,
    )
    return [str(name) for name in missing]


def _form_fields_to_dict(fields: Any) -> dict[str, Any]:
    return {
        "radio_groups": getattr(fields, "radio_groups", 0),
        "checkbox_groups": getattr(fields, "checkbox_groups", 0),
        "text_fields": getattr(fields, "text_fields", 0),
        "contenteditable_fields": getattr(fields, "contenteditable_fields", 0),
        "submit_buttons": list(getattr(fields, "submit_buttons", []) or []),
        "fill_counter": getattr(fields, "fill_counter", None),
    }


def _row_locator_for_item(page, item: CourseItem):
    escaped = _xpath_literal(item.title)
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


def run_item(session: TmsSession, course: CourseDetail, item: CourseItem, options: RunOptions | None = None) -> RunResult:
    return TmsRunner(session, options).run_item(course, item)


def run_course(session: TmsSession, course_or_url: CourseDetail | str, options: RunOptions | None = None) -> list[RunResult]:
    return TmsRunner(session, options).run_course(course_or_url)


def run_scheduler(session: TmsSession, options: RunOptions | None = None) -> RunResult:
    return TmsRunner(session, options).run_scheduler()
