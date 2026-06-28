from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .handlers import RunOptions, TmsRunner
from .models import (
    AuthOptions,
    CourseDetail,
    CourseItem,
    CourseSummary,
    ItemKind,
    ItemState,
    LoginMethod,
    LoginStatus,
    OperationBackend,
    RunResult,
    SiteState,
)
from .parsers import PENDING_PATH

if TYPE_CHECKING:
    from .session import TmsSession


@dataclass(frozen=True, slots=True)
class TmsBackendTools:
    session: TmsSession
    backend: OperationBackend

    def auto_login_tms(self, options: AuthOptions | None = None) -> LoginStatus:
        return self.ensure_authenticated(options)

    def login(self, options: AuthOptions | None = None) -> LoginStatus:
        return self.auto_login_tms(options)

    def ensure_authenticated(self, options: AuthOptions | None = None) -> LoginStatus:
        with self.session.using_backend(self.backend):
            return self.session.ensure_authenticated(self._auth_options(options))

    def check_status(self, fallback_browser: bool | None = None) -> LoginStatus:
        if self.backend == OperationBackend.PLAYWRIGHT and self.session.page is not None:
            return self.session.browser_status()
        if fallback_browser is None:
            fallback_browser = self.backend == OperationBackend.HYBRID
        return self.session.is_logged_in(fallback_browser=fallback_browser)

    def detect_error_state(self, fallback_browser: bool | None = None) -> LoginStatus:
        return self.check_status(fallback_browser=fallback_browser)

    def recover_transient_error(
        self,
        page=None,
        refresh_target: str | None = None,
        retries: int | None = None,
        delay_seconds: float | None = None,
    ) -> bool:
        browser_page = page if page is not None else self.session.page
        if browser_page is not None:
            return self.session.recover_transient_page(
                browser_page,
                refresh_target=refresh_target,
                retries=retries,
                delay_seconds=delay_seconds,
            )
        status = self.session.recover_transient_requests(
            refresh_target or PENDING_PATH,
            retries=retries,
            delay_seconds=delay_seconds,
        )
        return status.state != SiteState.TRANSIENT_ERROR

    def list_pending_courses(self) -> list[CourseSummary]:
        return self.session.list_pending_courses(backend=self.backend)

    def list_completed_courses(self) -> list[CourseSummary]:
        return self.session.list_completed_courses(backend=self.backend)

    def get_course_detail(self, course: str) -> CourseDetail:
        return self.session.get_course_detail(course, backend=self.backend)

    def fetch_activity_html(self, path_or_url: str, referer: str | None = None) -> str:
        return self.session.fetch_activity_html(path_or_url, referer=referer, backend=self.backend)

    def complete_item(self, course: CourseDetail, item: CourseItem, options: RunOptions | None = None) -> RunResult:
        return self._runner(options).run_item(course, item)

    def complete_reading(self, course: CourseDetail, item: CourseItem, options: RunOptions | None = None) -> RunResult:
        return self._complete_expected_item(
            course,
            item,
            expected_kinds={ItemKind.READING, ItemKind.VIDEO},
            helper_name="complete_reading",
            options=options,
        )

    def complete_survey(self, course: CourseDetail, item: CourseItem, options: RunOptions | None = None) -> RunResult:
        return self._complete_expected_item(
            course,
            item,
            expected_kinds={ItemKind.SURVEY},
            helper_name="complete_survey",
            options=options,
        )

    def complete_quiz(self, course: CourseDetail, item: CourseItem, options: RunOptions | None = None) -> RunResult:
        return self._complete_expected_item(
            course,
            item,
            expected_kinds={ItemKind.QUIZ},
            helper_name="complete_quiz",
            options=options,
        )

    def run_course(self, course_or_url: CourseDetail | str, options: RunOptions | None = None) -> list[RunResult]:
        return self._runner(options).run_course(course_or_url)

    def run_scheduler(self, options: RunOptions | None = None) -> RunResult:
        return self._runner(options).run_scheduler()

    def _runner(self, options: RunOptions | None) -> TmsRunner:
        return TmsRunner(self.session, self._run_options(options))

    def _complete_expected_item(
        self,
        course: CourseDetail,
        item: CourseItem,
        expected_kinds: set[ItemKind],
        helper_name: str,
        options: RunOptions | None = None,
    ) -> RunResult:
        kind = ItemKind(item.kind)
        if kind not in expected_kinds:
            expected = sorted(str(value) for value in expected_kinds)
            return RunResult(
                False,
                ItemState.BLOCKED,
                f"{helper_name} requires {'/'.join(expected)} item, got {item.kind}",
                course=course,
                item=item,
                data={
                    "backend": str(self.backend),
                    "helper": helper_name,
                    "expected_item_kinds": expected,
                    "item_kind": str(item.kind),
                },
            )
        return self.complete_item(course, item, options=options)

    def _run_options(self, options: RunOptions | None) -> RunOptions:
        if options is None:
            return RunOptions(backend=self.backend)
        return replace(options, backend=self.backend)

    def _auth_options(self, options: AuthOptions | None) -> AuthOptions:
        options = options or AuthOptions()
        if options.login_method != LoginMethod.AUTO:
            return options
        if self.backend == OperationBackend.REQUESTS:
            return replace(options, login_method=LoginMethod.REQUESTS)
        if self.backend == OperationBackend.PLAYWRIGHT:
            return replace(options, login_method=LoginMethod.PLAYWRIGHT)
        return options


class RequestsBackendTools(TmsBackendTools):
    def __init__(self, session: TmsSession) -> None:
        super().__init__(session=session, backend=OperationBackend.REQUESTS)


class PlaywrightBackendTools(TmsBackendTools):
    def __init__(self, session: TmsSession) -> None:
        super().__init__(session=session, backend=OperationBackend.PLAYWRIGHT)


class HybridBackendTools(TmsBackendTools):
    def __init__(self, session: TmsSession) -> None:
        super().__init__(session=session, backend=OperationBackend.HYBRID)


def backend_tools_for_session(session: TmsSession, backend: OperationBackend | str) -> TmsBackendTools:
    selected = OperationBackend(backend)
    if selected == OperationBackend.REQUESTS:
        return RequestsBackendTools(session)
    if selected == OperationBackend.PLAYWRIGHT:
        return PlaywrightBackendTools(session)
    return HybridBackendTools(session)


__all__ = [
    "HybridBackendTools",
    "PlaywrightBackendTools",
    "RequestsBackendTools",
    "TmsBackendTools",
    "backend_tools_for_session",
]
