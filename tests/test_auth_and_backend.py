import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks import cli as cli_module
from tms_vghks.batch_login import AccountLoginConfig, AccountsLoginConfig
from tms_vghks.cli import build_parser, ensure_for_command, format_text_payload, safe_print_json, to_jsonable
from tms_vghks.handlers import RunOptions, TmsRunner, item_is_complete
from tms_vghks.models import (
    AuthOptions,
    CourseDetail,
    CourseItem,
    CourseSummary,
    ItemKind,
    ItemState,
    LoginMethod,
    LoginStatus,
    OperationBackend,
    RequestsLoginChallenge,
    RequestsLoginResult,
    RunResult,
    SiteState,
)
from tms_vghks.session import LoginRequired, TmsError, TmsSession, TransientTmsError


class FakeAuthSession(TmsSession):
    def __init__(self, statuses):
        super().__init__()
        self.statuses = list(statuses)
        self.loaded = False
        self.saved = False
        self.playwright_login = False
        self.requests_login = False
        self.prepared = False
        self.load_raises = False
        self.is_logged_calls = 0
        self.submit_result = RequestsLoginResult(False, "missing_fields", "missing required login fields")

    def is_logged_in(self, fallback_browser=False):
        self.is_logged_calls += 1
        if self.statuses:
            return self.statuses.pop(0)
        return LoginStatus(SiteState.LOGIN_REQUIRED, message="not logged in")

    def load_session_bundle(self, path=".tms_session"):
        self.loaded = True
        if self.load_raises:
            raise TmsError("session bundle not found")
        return {}

    def save_session_bundle(self, path=".tms_session"):
        self.saved = True
        return {}

    def ensure_login(
        self,
        headless=False,
        timeout_seconds=300,
        poll_interval_seconds=2.0,
        transient_retries=None,
        transient_delay_seconds=None,
    ):
        self.playwright_login = True
        return LoginStatus(SiteState.LOGGED_IN, message="playwright")

    def submit_requests_login(self, *args, **kwargs):
        self.requests_login = True
        return self.submit_result

    def prepare_requests_login(self, *args, **kwargs):
        self.prepared = True
        return RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
        )


class FakeContext:
    def __init__(self):
        self.added = []

    def add_cookies(self, cookies):
        self.added.extend(cookies)

    def cookies(self, base_url):
        return [
            {
                "name": "PHPSESSID",
                "value": "browser-cookie",
                "domain": "tms.vghks.gov.tw",
                "path": "/",
            }
        ]


class FakeTransientCliSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def ensure_login(self, **kwargs):
        raise TransientTmsError("TMS transient error persisted")


class FakeHeadlessListSession(TmsSession):
    last = None

    def __init__(self):
        super().__init__()
        type(self).last = self
        self.list_calls = []
        self.auth_options = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def ensure_authenticated(self, options=None):
        self.auth_options = options
        return LoginStatus(SiteState.LOGGED_IN)

    def list_pending_courses(self, backend=None):
        self.list_calls.append({"backend": backend, "browser_headless": self.browser_headless})
        return []


class FakeAccountBackendSession:
    def __init__(self):
        self.backends = []
        self.saved_browser_calls = []
        self.saved_requests_calls = []
        self.transient_policy = None
        self.browser_headless = False

    def configure_transient_policy(self, retries=None, delay_seconds=None):
        self.transient_policy = (retries, delay_seconds)

    def use_backend(self, backend):
        self.backends.append(OperationBackend(backend))

    def ensure_saved_browser_authenticated(self, session_dir, headless=False):
        self.saved_browser_calls.append((session_dir, headless))

    def ensure_authenticated(self, auth_options):
        self.saved_requests_calls.append(auth_options)


class FakePromptAuthSession:
    def __init__(self):
        self.backend = None
        self.browser_headless = False
        self.transient_policy = None
        self.ensure_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def use_backend(self, backend):
        self.backend = OperationBackend(backend)

    def configure_transient_policy(self, retries=None, delay_seconds=None):
        self.transient_policy = (retries, delay_seconds)

    def ensure_authenticated(self, options=None):
        self.ensure_calls.append(options)
        if len(self.ensure_calls) == 1:
            raise LoginRequired("missing credentials")
        return LoginStatus(SiteState.LOGGED_IN)


class FakeRequestsCapabilitySession(TmsSession):
    def __init__(self, html="<html><body>剩餘 00:01</body></html>"):
        super().__init__()
        self.html = html

    def ensure_authenticated(self, options=None):
        return LoginStatus(SiteState.LOGGED_IN, message="requests")

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        return self.html

    def list_pending_courses(self):
        return [CourseSummary(title="Course", detail_url="https://tms.vghks.gov.tw/course/1")]

    def get_course_detail(self, url_or_id):
        return CourseDetail(
            title="Course",
            url="https://tms.vghks.gov.tw/course/1",
            items=[
                CourseItem(
                    title="Read",
                    kind=ItemKind.READING,
                    state=ItemState.IN_PROGRESS,
                    pass_condition="閱讀達 1 分鐘",
                    result="00:00",
                )
            ],
        )


class AuthAndBackendTests(unittest.TestCase):
    def test_run_options_do_not_expose_submit_or_survey_text_gates(self):
        options = RunOptions()
        self.assertFalse(hasattr(options, "allow_submit"))
        self.assertFalse(hasattr(options, "neutral_survey_text"))

    def test_run_options_default_backend_is_requests(self):
        self.assertEqual(RunOptions().backend, OperationBackend.REQUESTS)

    def test_runner_treats_pass_condition_satisfied_item_as_already_passed(self):
        class NoMutationRunner(TmsRunner):
            def run_item_requests(self, course, item):
                raise AssertionError("already-complete item should not dispatch to requests mutation")

            def run_item_playwright(self, course, item):
                raise AssertionError("already-complete item should not dispatch to Playwright mutation")

        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(
            title="Read",
            kind=ItemKind.READING,
            state=ItemState.IN_PROGRESS,
            pass_condition="閱讀達 40 分鐘",
            result="41:03",
            passed_marker="-",
        )

        result = NoMutationRunner(FakeRequestsCapabilitySession(), RunOptions()).run_item(course, item)

        self.assertTrue(item_is_complete(item))
        self.assertTrue(result.success)
        self.assertEqual(result.state, ItemState.PASSED)
        self.assertEqual(result.message, "item already passed")

    def test_saved_session_method_loads_and_validates(self):
        session = FakeAuthSession(
            [
                LoginStatus(SiteState.LOGGED_IN, message="saved"),
            ]
        )
        status = session.ensure_authenticated(AuthOptions(login_method=LoginMethod.SAVED))
        self.assertTrue(status.logged_in)
        self.assertTrue(session.loaded)

    def test_auto_defaults_to_requests_when_saved_missing(self):
        session = FakeAuthSession(
            [
                LoginStatus(SiteState.LOGIN_REQUIRED),
                LoginStatus(SiteState.LOGGED_IN, message="requests"),
            ]
        )
        session.load_raises = True
        session.submit_result = RequestsLoginResult(True, "logged_in", "ok")
        status = session.ensure_authenticated(AuthOptions(login_method=LoginMethod.AUTO))
        self.assertTrue(status.logged_in)
        self.assertTrue(session.loaded)
        self.assertTrue(session.requests_login)
        self.assertFalse(session.playwright_login)

    def test_auto_uses_playwright_when_backend_is_playwright(self):
        session = FakeAuthSession([LoginStatus(SiteState.LOGIN_REQUIRED)])
        session.load_raises = True
        session.use_backend(OperationBackend.PLAYWRIGHT)
        status = session.ensure_authenticated(AuthOptions(login_method=LoginMethod.AUTO))
        self.assertTrue(status.logged_in)
        self.assertTrue(session.loaded)
        self.assertTrue(session.playwright_login)

    def test_auto_hybrid_tries_requests_before_playwright_fallback(self):
        session = FakeAuthSession([LoginStatus(SiteState.LOGIN_REQUIRED)])
        session.load_raises = True
        session.use_backend(OperationBackend.HYBRID)
        status = session.ensure_authenticated(AuthOptions(login_method=LoginMethod.AUTO))
        self.assertTrue(status.logged_in)
        self.assertTrue(session.requests_login)
        self.assertTrue(session.playwright_login)

    def test_requests_login_missing_fields_raises_login_required(self):
        session = FakeAuthSession([LoginStatus(SiteState.LOGIN_REQUIRED)])
        with self.assertRaisesRegex(LoginRequired, "missing_fields"):
            session.ensure_authenticated(AuthOptions(login_method=LoginMethod.REQUESTS))
        self.assertTrue(session.requests_login)

    def test_requests_login_prepares_when_challenge_is_missing(self):
        session = FakeAuthSession([LoginStatus(SiteState.LOGGED_IN)])
        session.submit_result = RequestsLoginResult(False, "missing_challenge", "prepare first")

        def submit(*args, **kwargs):
            if not session.prepared:
                return RequestsLoginResult(False, "missing_challenge", "prepare first")
            return RequestsLoginResult(True, "logged_in", "ok")

        session.submit_requests_login = submit
        status = session.ensure_authenticated(
            AuthOptions(login_method=LoginMethod.REQUESTS, account="a", password="p", captcha="1")
        )
        self.assertTrue(status.logged_in)
        self.assertTrue(session.prepared)

    def test_requests_login_uses_ocr_when_captcha_is_not_supplied(self):
        class OcrRequestsSession(FakeAuthSession):
            def __init__(self):
                super().__init__([LoginStatus(SiteState.LOGGED_IN)])
                self.submitted_captchas = []

            def submit_requests_login(self, *args, **kwargs):
                self.submitted_captchas.append(kwargs.get("captcha", ""))
                if not self.prepared:
                    return RequestsLoginResult(False, "missing_challenge", "prepare first")
                return RequestsLoginResult(True, "logged_in", "ok")

        session = OcrRequestsSession()

        with patch("tms_vghks.session.recognize_captcha", return_value=SimpleNamespace(text="2468")) as recognize:
            status = session.ensure_authenticated(
                AuthOptions(login_method=LoginMethod.REQUESTS, account="a", password="p")
            )

        self.assertTrue(status.logged_in)
        self.assertEqual(session.submitted_captchas, ["", "2468"])
        recognize.assert_called_once()

    def test_requests_login_retries_ocr_after_captcha_failure(self):
        class RetryOcrRequestsSession(FakeAuthSession):
            def __init__(self):
                super().__init__([LoginStatus(SiteState.LOGGED_IN)])
                self.submitted_captchas = []
                self.submit_count = 0
                self.prepare_paths = []

            def prepare_requests_login(self, *args, **kwargs):
                self.prepared = True
                self.prepare_paths.append(str(kwargs.get("captcha_path")))
                return super().prepare_requests_login(*args, **kwargs)

            def submit_requests_login(self, *args, **kwargs):
                self.submit_count += 1
                self.submitted_captchas.append(kwargs.get("captcha", ""))
                if self.submit_count == 1:
                    return RequestsLoginResult(False, "missing_challenge", "prepare first")
                if self.submit_count == 2:
                    return RequestsLoginResult(False, "captcha_failed", "bad captcha")
                return RequestsLoginResult(True, "logged_in", "ok")

        session = RetryOcrRequestsSession()
        ocr_results = [SimpleNamespace(text="1111"), SimpleNamespace(text="2222")]

        with patch("tms_vghks.session.recognize_captcha", side_effect=ocr_results):
            status = session.ensure_authenticated(
                AuthOptions(login_method=LoginMethod.REQUESTS, account="a", password="p")
            )

        self.assertTrue(status.logged_in)
        self.assertEqual(session.submitted_captchas, ["", "1111", "2222"])
        self.assertTrue(session.prepare_paths[0].endswith("captcha.jpg"))
        self.assertTrue(session.prepare_paths[1].endswith("captcha_retry.jpg"))

    def test_requests_login_requires_manual_captcha_after_three_ocr_failures(self):
        class RetryOcrRequestsSession(FakeAuthSession):
            def __init__(self):
                super().__init__([LoginStatus(SiteState.LOGIN_REQUIRED)])
                self.submitted_captchas = []
                self.submit_count = 0
                self.prepare_paths = []

            def prepare_requests_login(self, *args, **kwargs):
                self.prepared = True
                self.prepare_paths.append(str(kwargs.get("captcha_path")))
                return super().prepare_requests_login(*args, **kwargs)

            def submit_requests_login(self, *args, **kwargs):
                self.submit_count += 1
                self.submitted_captchas.append(kwargs.get("captcha", ""))
                if self.submit_count == 1:
                    return RequestsLoginResult(False, "missing_challenge", "prepare first")
                return RequestsLoginResult(False, "captcha_failed", "bad captcha")

        session = RetryOcrRequestsSession()
        ocr_results = [SimpleNamespace(text="1111"), SimpleNamespace(text="2222"), SimpleNamespace(text="3333")]

        with patch("tms_vghks.session.recognize_captcha", side_effect=ocr_results):
            with self.assertRaisesRegex(LoginRequired, "manual_captcha_required"):
                session.ensure_authenticated(
                    AuthOptions(login_method=LoginMethod.REQUESTS, account="a", password="p")
                )

        self.assertEqual(session.submitted_captchas, ["", "1111", "2222", "3333"])
        self.assertTrue(session.prepare_paths[0].endswith("captcha.jpg"))
        self.assertTrue(session.prepare_paths[1].endswith("captcha_retry.jpg"))
        self.assertTrue(session.prepare_paths[2].endswith("captcha_retry_3.jpg"))

    def test_playwright_login_with_credentials_uses_ocr_login(self):
        class OcrPlaywrightSession(FakeAuthSession):
            def __init__(self):
                super().__init__([LoginStatus(SiteState.LOGIN_REQUIRED), LoginStatus(SiteState.LOGGED_IN)])
                self.ocr_login_calls = []

            def login_playwright_with_ocr(self, **kwargs):
                self.ocr_login_calls.append(kwargs)
                return {"success": True, "status": "logged_in", "message": "ok"}

        session = OcrPlaywrightSession()
        status = session.ensure_authenticated(
            AuthOptions(
                login_method=LoginMethod.PLAYWRIGHT,
                account="a",
                password="p",
                headless=True,
            )
        )

        self.assertTrue(status.logged_in)
        self.assertFalse(session.playwright_login)
        self.assertEqual(session.ocr_login_calls[0]["account"], "a")
        self.assertEqual(session.ocr_login_calls[0]["password"], "p")
        self.assertEqual(session.ocr_login_calls[0]["captcha_mode"], "paddleocr-sdk")
        self.assertTrue(session.ocr_login_calls[0]["headless"])

    def test_cli_auth_options_default_to_paddleocr_sdk_without_external_mode(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "pending",
                "--login-method",
                "requests",
                "--account",
                "a",
                "--password",
                "p",
            ]
        )
        options = cli_module.auth_options_from_args(args)

        self.assertEqual(options.login_method, LoginMethod.REQUESTS)
        self.assertEqual(options.account, "a")
        self.assertEqual(options.password, "p")
        self.assertEqual(options.captcha_mode, "paddleocr-sdk")
        self.assertFalse(hasattr(args, "captcha_mode"))

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["courses", "list", "pending", "--captcha-mode", "manual"])

    def test_request_cookies_sync_to_playwright_context(self):
        session = TmsSession()
        session.http.cookies.set("PHPSESSID", "requests-cookie", domain="tms.vghks.gov.tw", path="/")
        context = FakeContext()
        session.context = context
        session.sync_cookies_to_browser()
        self.assertEqual(context.added[0]["name"], "PHPSESSID")
        self.assertEqual(context.added[0]["value"], "requests-cookie")

    def test_playwright_cookies_sync_to_requests_session(self):
        session = TmsSession()
        session.context = FakeContext()
        session.sync_cookies_to_requests()
        self.assertEqual(session.http.cookies.get("PHPSESSID", domain="tms.vghks.gov.tw", path="/"), "browser-cookie")

    def test_clone_authenticated_copies_headers_and_cookies(self):
        session = TmsSession()
        session.use_backend(OperationBackend.PLAYWRIGHT)
        session.browser_headless = True
        session.http.headers.update({"X-Test": "worker"})
        session.http.cookies.set("PHPSESSID", "requests-cookie", domain="tms.vghks.gov.tw", path="/")

        clone = session.clone_authenticated()

        self.assertIsNot(clone, session)
        self.assertIsNot(clone.http, session.http)
        self.assertEqual(clone.backend, OperationBackend.PLAYWRIGHT)
        self.assertTrue(clone.browser_headless)
        self.assertEqual(clone.http.headers["X-Test"], "worker")
        self.assertEqual(clone.http.cookies.get("PHPSESSID", domain="tms.vghks.gov.tw", path="/"), "requests-cookie")
        clone.http.cookies.set("PHPSESSID", "clone-cookie", domain="tms.vghks.gov.tw", path="/")
        self.assertEqual(session.http.cookies.get("PHPSESSID", domain="tms.vghks.gov.tw", path="/"), "requests-cookie")

    def test_session_can_switch_backend_course_tools_after_login(self):
        class BackendSwitchingSession(TmsSession):
            def _fetch_authenticated_html(self, path_or_url):
                return """
                <table>
                  <tr><th>課程名稱</th><th>完成度</th><th>操作</th></tr>
                  <tr><td>Requests Course</td><td>50%</td><td><a href="/course/1">進入</a></td></tr>
                </table>
                """

            def fetch_html_with_browser(self, path_or_url):
                return """
                <table>
                  <tr><th>課程名稱</th><th>完成度</th><th>操作</th></tr>
                  <tr><td>Playwright Course</td><td>50%</td><td><a href="/course/2">進入</a></td></tr>
                </table>
                """

        session = BackendSwitchingSession()

        self.assertEqual(session.backend, OperationBackend.REQUESTS)
        self.assertEqual(session.list_pending_courses()[0].title, "Requests Course")
        with session.using_backend(OperationBackend.PLAYWRIGHT):
            self.assertEqual(session.list_pending_courses()[0].title, "Playwright Course")
        self.assertEqual(session.backend, OperationBackend.REQUESTS)
        session.use_backend("playwright")
        self.assertEqual(session.list_pending_courses()[0].title, "Playwright Course")

    def test_session_fetch_activity_html_uses_selected_backend(self):
        class ActivityFetchSession(TmsSession):
            def fetch_activity_html_requests(self, path_or_url, referer=None):
                if "fail" in path_or_url:
                    raise TmsError("requests failed")
                return f"requests:{path_or_url}:{referer}"

            def fetch_activity_html_playwright(self, path_or_url, referer=None):
                return f"playwright:{path_or_url}:{referer}"

        session = ActivityFetchSession()

        self.assertEqual(session.fetch_activity_html("/activity/1", referer="/course/1"), "requests:/activity/1:/course/1")
        self.assertEqual(
            session.fetch_activity_html("/activity/1", backend=OperationBackend.PLAYWRIGHT),
            "playwright:/activity/1:None",
        )
        self.assertEqual(
            session.fetch_activity_html("/fail", backend=OperationBackend.HYBRID),
            "playwright:/fail:None",
        )

    def test_runner_exposes_symmetric_item_completion_methods(self):
        for suffix in ("requests", "playwright"):
            for action in ("run_reading", "run_survey", "run_quiz"):
                self.assertTrue(hasattr(TmsRunner, f"{action}_{suffix}"), f"{action}_{suffix}")

    def test_runner_dispatches_reading_to_named_backend_methods(self):
        calls = []

        class ReadingDispatchRunner(TmsRunner):
            def run_reading_requests(self, course, item):
                calls.append(("requests", item.kind))
                return RunResult(True, ItemState.PASSED, "requests reading", course=course, item=item)

            def run_reading_playwright(self, course, item):
                calls.append(("playwright", item.kind))
                return RunResult(True, ItemState.PASSED, "playwright reading", course=course, item=item)

        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(title="Read", kind=ItemKind.READING, state=ItemState.PENDING)

        requests_result = ReadingDispatchRunner(
            FakeRequestsCapabilitySession(),
            RunOptions(backend=OperationBackend.REQUESTS),
        ).run_item(course, item)
        playwright_result = ReadingDispatchRunner(
            FakeRequestsCapabilitySession(),
            RunOptions(backend=OperationBackend.PLAYWRIGHT),
        ).run_item(course, item)

        self.assertTrue(requests_result.success)
        self.assertTrue(playwright_result.success)
        self.assertEqual(calls, [("requests", ItemKind.READING), ("playwright", ItemKind.READING)])

    def test_runner_generic_item_methods_dispatch_to_selected_backend(self):
        calls = []

        class GenericDispatchRunner(TmsRunner):
            def run_reading_requests(self, course, item):
                calls.append(("run_reading_requests", item.kind))
                return RunResult(True, ItemState.PASSED, "requests reading", course=course, item=item)

            def run_survey_requests(self, course, item):
                calls.append(("run_survey_requests", item.kind))
                return RunResult(True, ItemState.PASSED, "requests survey", course=course, item=item)

            def run_quiz_requests(self, course, item):
                calls.append(("run_quiz_requests", item.kind))
                return RunResult(True, ItemState.PASSED, "requests quiz", course=course, item=item)

            def run_reading_playwright(self, course, item):
                calls.append(("run_reading_playwright", item.kind))
                return RunResult(True, ItemState.PASSED, "playwright reading", course=course, item=item)

            def run_survey_playwright(self, course, item):
                calls.append(("run_survey_playwright", item.kind))
                return RunResult(True, ItemState.PASSED, "playwright survey", course=course, item=item)

            def run_quiz_playwright(self, course, item):
                calls.append(("run_quiz_playwright", item.kind))
                return RunResult(True, ItemState.PASSED, "playwright quiz", course=course, item=item)

        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        reading = CourseItem(title="Read", kind=ItemKind.READING, state=ItemState.PENDING)
        survey = CourseItem(title="Survey", kind=ItemKind.SURVEY, state=ItemState.PENDING)
        quiz = CourseItem(title="Quiz", kind=ItemKind.QUIZ, state=ItemState.PENDING)

        requests_runner = GenericDispatchRunner(FakeRequestsCapabilitySession(), RunOptions())
        playwright_runner = GenericDispatchRunner(
            FakeRequestsCapabilitySession(),
            RunOptions(backend=OperationBackend.PLAYWRIGHT),
        )

        self.assertEqual(requests_runner.run_reading(course, reading).message, "requests reading")
        self.assertEqual(requests_runner.run_survey(course, survey).message, "requests survey")
        self.assertEqual(requests_runner.run_quiz(course, quiz).message, "requests quiz")
        self.assertEqual(playwright_runner.run_reading(course, reading).message, "playwright reading")
        self.assertEqual(playwright_runner.run_survey(course, survey).message, "playwright survey")
        self.assertEqual(playwright_runner.run_quiz(course, quiz).message, "playwright quiz")
        self.assertEqual(
            calls,
            [
                ("run_reading_requests", ItemKind.READING),
                ("run_survey_requests", ItemKind.SURVEY),
                ("run_quiz_requests", ItemKind.QUIZ),
                ("run_reading_playwright", ItemKind.READING),
                ("run_survey_playwright", ItemKind.SURVEY),
                ("run_quiz_playwright", ItemKind.QUIZ),
            ],
        )

    def test_runner_generic_item_method_hybrid_falls_back_to_playwright(self):
        calls = []

        class GenericHybridRunner(TmsRunner):
            def run_survey_requests(self, course, item):
                calls.append("requests")
                return RunResult(False, "form_endpoint_unverified", "requests failed", course=course, item=item)

            def run_survey_playwright(self, course, item):
                calls.append("playwright")
                return RunResult(True, ItemState.PASSED, "playwright survey", course=course, item=item)

        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        survey = CourseItem(title="Survey", kind=ItemKind.SURVEY, state=ItemState.PENDING)
        runner = GenericHybridRunner(
            FakeRequestsCapabilitySession(),
            RunOptions(backend=OperationBackend.HYBRID),
        )

        result = runner.run_survey(course, survey)

        self.assertTrue(result.success)
        self.assertEqual(result.message, "playwright survey")
        self.assertEqual(calls, ["requests", "playwright"])
        self.assertEqual(result.data["requests_attempt"]["state"], "form_endpoint_unverified")

    def test_requests_backend_reports_endpoint_unverified_for_reading_mutation(self):
        session = FakeRequestsCapabilitySession()
        runner = TmsRunner(session, RunOptions(backend=OperationBackend.REQUESTS))
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(
            title="Read",
            kind=ItemKind.READING,
            state=ItemState.PENDING,
            detail_url="https://tms.vghks.gov.tw/reading/1",
            pass_condition="閱讀達 1 分鐘",
            result="00:00",
        )
        result = runner.run_item(course, item)
        self.assertFalse(result.success)
        self.assertEqual(result.state, "endpoint_unverified")
        self.assertEqual(result.data["capability"], "endpoint_unverified")

    def test_requests_backend_attempts_form_submit_without_allow_submit_gate(self):
        session = FakeRequestsCapabilitySession(
            """
            <form>
              <label><input type="radio" name="s1">普通</label>
              <button type="submit">送出</button>
            </form>
            """
        )
        runner = TmsRunner(session, RunOptions(backend=OperationBackend.REQUESTS))
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(
            title="Survey",
            kind=ItemKind.SURVEY,
            state=ItemState.PENDING,
            detail_url="https://tms.vghks.gov.tw/survey/1",
        )
        result = runner.run_item(course, item)
        self.assertFalse(result.success)
        self.assertEqual(result.state, "form_endpoint_unverified")
        self.assertEqual(result.data["capability"], "form_endpoint_unverified")
        self.assertEqual(result.data["form_summary"]["radio_groups"], 1)
        self.assertIn("form_action_missing", result.data["issues"])

    def test_hybrid_backend_falls_back_to_playwright_after_requests_endpoint_issue(self):
        class HybridFallbackRunner(TmsRunner):
            def run_item_requests(self, course, item):
                return RunResult(
                    False,
                    "endpoint_unverified",
                    "requests endpoint unavailable",
                    course=course,
                    item=item,
                    data={"backend": "requests"},
                )

            def run_item_playwright(self, course, item):
                return RunResult(True, ItemState.PASSED, "playwright fallback ok", course=course, item=item)

        runner = HybridFallbackRunner(FakeRequestsCapabilitySession(), RunOptions(backend=OperationBackend.HYBRID))
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(title="Read", kind=ItemKind.READING, state=ItemState.PENDING)

        result = runner.run_item(course, item)

        self.assertTrue(result.success)
        self.assertEqual(result.message, "playwright fallback ok")
        self.assertEqual(result.data["requests_attempt"]["state"], "endpoint_unverified")

    def test_default_requests_survey_has_no_forbidden_neutral_text_gate(self):
        session = FakeRequestsCapabilitySession()
        runner = TmsRunner(session, RunOptions())
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(title="Survey", kind=ItemKind.SURVEY, state=ItemState.PENDING)

        result = runner.run_survey(course, item)

        self.assertFalse(result.success)
        self.assertEqual(result.state, "form_endpoint_unverified")
        self.assertNotIn("neutral_survey_text_forbidden", ",".join(result.data["issues"]))

    def test_requests_scheduler_reports_per_item_capability(self):
        session = FakeRequestsCapabilitySession()
        result = TmsRunner(session, RunOptions(backend=OperationBackend.REQUESTS)).run_scheduler()
        self.assertFalse(result.success)
        row = result.data["results"][0]
        self.assertEqual(row["state"], "endpoint_unverified")
        self.assertEqual(row["data"]["capability"], "endpoint_unverified")

    def test_cli_parser_accepts_login_method_and_backend(self):
        parser = build_parser()
        list_args = parser.parse_args(["pending", "--login-method", "saved"])
        self.assertEqual(list_args.login_method, "saved")
        self.assertEqual(list_args.backend, "requests")
        inspect_args = parser.parse_args(["course", "123"])
        self.assertEqual(inspect_args.backend, "requests")
        default_run_args = parser.parse_args(["go"])
        self.assertEqual(default_run_args.backend, "requests")
        run_args = parser.parse_args(
            [
                "go",
                "--backend",
                "requests",
                "--login-method",
                "requests",
                "--transient-retries",
                "1",
                "--quiz",
                "auto",
            ]
        )
        self.assertEqual(run_args.backend, "requests")
        self.assertEqual(run_args.login_method, "requests")
        self.assertEqual(run_args.transient_retries, 1)
        self.assertEqual(run_args.quiz, "auto")
        self.assertFalse(hasattr(run_args, "allow_submit"))
        self.assertFalse(hasattr(run_args, "neutral_survey_text"))
        validate_args = parser.parse_args(["diag", "playwright-forms"])
        self.assertFalse(hasattr(validate_args, "allow_submit"))
        self.assertFalse(hasattr(validate_args, "neutral_survey_text"))
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["go", "--allow-submit"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["go", "--neutral-survey-text", "無"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["diag", "playwright-forms", "--allow-submit"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["diag", "network", "123", "--allow-live-mutation"])
        export_args = parser.parse_args(["bank", "export", "--probe-only", "--include", "quiz,survey"])
        self.assertEqual(export_args.command, "export-question-bank")
        self.assertTrue(export_args.probe_only)
        self.assertEqual(export_args.delay_min_ms, 400)
        self.assertEqual(export_args.delay_max_ms, 1400)
        self.assertFalse(export_args.no_random_delay)
        accounts_export_args = parser.parse_args(
            [
                "bank",
                "export",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
                "--probe-only",
                "--no-random-delay",
                "--delay-seed",
                "99",
            ]
        )
        self.assertEqual(accounts_export_args.command, "export-question-bank")
        self.assertEqual(accounts_export_args.accounts, ".tms_accounts.toml")
        self.assertEqual(accounts_export_args.label, "account1")
        self.assertTrue(accounts_export_args.no_random_delay)
        self.assertEqual(accounts_export_args.delay_seed, 99)
        reference_args = parser.parse_args(
            [
                "bank",
                "build",
                "--history",
                ".tms_private_exports/question-bank-history.jsonl",
                "--ai-suggestions-jsonl",
                ".tms_private_exports/posttest-ai-suggestions.jsonl",
            ]
        )
        self.assertEqual(reference_args.command, "build-reference-question-bank")
        self.assertRegex(reference_args.output, r"^question-bank-\d{8}\.jsonl$")
        self.assertIsNone(reference_args.markdown)
        self.assertEqual(reference_args.posttest_ai_policy, "trusted")
        pw_args = parser.parse_args(
            [
                "bank",
                "probe",
                "--historical-quiz-bank",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
                "--allow-private-export",
                "--include",
                "quiz,survey",
                "--course-limit",
                "1",
                "--activity-limit",
                "2",
            ]
        )
        self.assertEqual(pw_args.command, "probe-question-bank-playwright")
        self.assertEqual(pw_args.accounts, ".tms_accounts.toml")
        self.assertEqual(pw_args.label, "account1")
        self.assertTrue(pw_args.historical_quiz_bank)
        self.assertTrue(pw_args.allow_private_export)
        self.assertEqual(pw_args.course_limit, 1)
        self.assertEqual(pw_args.activity_limit, 2)
        self.assertEqual(pw_args.backend, "playwright")
        self.assertEqual(pw_args.delay_min_ms, 400)
        self.assertEqual(pw_args.delay_max_ms, 1400)
        self.assertFalse(pw_args.no_random_delay)
        requests_history_args = parser.parse_args(
            [
                "bank",
                "probe",
                "--backend",
                "requests",
                "--historical-quiz-bank",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
                "--allow-private-export",
                "--no-random-delay",
                "--delay-seed",
                "123",
            ]
        )
        self.assertEqual(requests_history_args.command, "probe-question-bank-playwright")
        self.assertEqual(requests_history_args.backend, "requests")
        self.assertTrue(requests_history_args.historical_quiz_bank)
        self.assertEqual(requests_history_args.accounts, ".tms_accounts.toml")
        self.assertEqual(requests_history_args.label, "account1")
        self.assertTrue(requests_history_args.no_random_delay)
        self.assertEqual(requests_history_args.delay_seed, 123)
        kexam_records_args = parser.parse_args(
            [
                "diag",
                "playwright-kexam-records",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
                "--exam-url",
                "https://tms.vghks.gov.tw/course/5416/exam/10613",
                "--exclude-unsubmitted-records",
            ]
        )
        self.assertEqual(kexam_records_args.command, "playwright-kexam-records")
        self.assertEqual(kexam_records_args.accounts, ".tms_accounts.toml")
        self.assertEqual(kexam_records_args.label, "account1")
        self.assertFalse(kexam_records_args.include_unsubmitted_records)
        kexam_resubmit_args = parser.parse_args(
            [
                "diag",
                "playwright-quiz-resubmit",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
                "--course",
                "5416",
                "--exam-url",
                "https://tms.vghks.gov.tw/course/5416/exam/10613",
                "--quiz",
                "auto",
            ]
        )
        self.assertEqual(kexam_resubmit_args.command, "playwright-quiz-resubmit-diagnostics")
        self.assertEqual(kexam_resubmit_args.course, "5416")
        self.assertEqual(kexam_resubmit_args.quiz, "auto")
        validate_args = parser.parse_args(
            [
                "diag",
                "playwright-forms",
                "--scope",
                "completed,pending",
                "--include",
                "quiz,survey",
                "--course-limit",
                "2",
                "--activity-limit",
                "3",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
            ]
        )
        self.assertEqual(validate_args.command, "validate-playwright-forms")
        self.assertEqual(validate_args.scope, "completed,pending")
        self.assertFalse(hasattr(validate_args, "allow_submit"))
        self.assertEqual(validate_args.course_limit, 2)
        self.assertEqual(validate_args.activity_limit, 3)
        self.assertEqual(validate_args.accounts, ".tms_accounts.toml")
        self.assertEqual(validate_args.label, "account1")
        network_args = parser.parse_args(
            [
                "diag",
                "network",
                "123",
                "--item-order",
                "1",
                "--login-method",
                "saved",
                "--wait-ms",
                "10",
            ]
        )
        self.assertEqual(network_args.command, "network-diagnostics")
        self.assertEqual(network_args.item_order, 1)
        self.assertEqual(network_args.wait_ms, 10)
        self.assertEqual(network_args.action, "open-only")
        self.assertFalse(hasattr(network_args, "allow_live_mutation"))
        reading_accumulation_args = parser.parse_args(
            [
                "diag",
                "reading-playwright",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
                "--wait-seconds",
                "90",
                "--poll-seconds",
                "30",
            ]
        )
        self.assertEqual(reading_accumulation_args.command, "reading-accumulation-diagnostics")
        self.assertEqual(reading_accumulation_args.accounts, ".tms_accounts.toml")
        self.assertEqual(reading_accumulation_args.label, "account1")
        self.assertEqual(reading_accumulation_args.wait_seconds, 90)
        self.assertEqual(reading_accumulation_args.poll_seconds, 30)
        requests_reading_accumulation_args = parser.parse_args(
            [
                "diag",
                "reading",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
                "--wait-seconds",
                "60",
                "--force-watch-time",
            ]
        )
        self.assertEqual(requests_reading_accumulation_args.command, "requests-reading-accumulation-diagnostics")
        self.assertEqual(requests_reading_accumulation_args.accounts, ".tms_accounts.toml")
        self.assertEqual(requests_reading_accumulation_args.label, "account1")
        self.assertEqual(requests_reading_accumulation_args.wait_seconds, 60)
        self.assertTrue(requests_reading_accumulation_args.force_watch_time)
        analyze_args = parser.parse_args(
            [
                "diag",
                "reproduction",
                "--input",
                ".tms_session/network_observations.jsonl",
            ]
        )
        self.assertEqual(analyze_args.command, "analyze-requests-reproduction")
        self.assertEqual(analyze_args.input, ".tms_session/network_observations.jsonl")

    def test_cli_grouped_commands_map_to_legacy_handlers(self):
        parser = build_parser()

        auth_status = parser.parse_args(["auth", "status"])
        self.assertEqual(auth_status.command, "status")
        self.assertFalse(hasattr(auth_status, "json"))
        auth_status_agent = parser.parse_args(["auth", "status", "--agent"])
        self.assertEqual(auth_status_agent.cli_mode, "agent")
        auth_status_human = parser.parse_args(["auth", "status", "--human"])
        self.assertEqual(auth_status_human.cli_mode, "human")
        root_agent = parser.parse_args(["--agent", "auth", "status"])
        self.assertEqual(root_agent.cli_mode, "agent")
        root_human = parser.parse_args(["--human", "auth", "status"])
        self.assertEqual(root_human.cli_mode, "human")
        matching_modes = parser.parse_args(["--agent", "auth", "status", "--agent"])
        self.assertEqual(matching_modes.cli_mode, "agent")

        auth_login = parser.parse_args(["auth", "requests-login", "--accounts", ".tms_accounts.toml"])
        self.assertEqual(auth_login.command, "login-requests")
        self.assertEqual(auth_login.login_requests_command, "login")
        self.assertEqual(auth_login.accounts, ".tms_accounts.toml")

        completed = parser.parse_args(["courses", "list", "completed", "--login-method", "saved"])
        self.assertEqual(completed.command, "list")
        self.assertEqual(completed.kind, "completed")
        self.assertEqual(completed.backend, "requests")
        self.assertEqual(completed.login_method, "saved")

        inspected = parser.parse_args(["courses", "inspect", "5416"])
        self.assertEqual(inspected.command, "inspect-course")
        self.assertEqual(inspected.course, "5416")

        reading = parser.parse_args(["diag", "reading", "--wait-seconds", "600", "--force-watch-time"])
        self.assertEqual(reading.command, "requests-reading-accumulation-diagnostics")
        self.assertEqual(reading.wait_seconds, 600)
        self.assertTrue(reading.force_watch_time)

        forms = parser.parse_args(["diag", "forms", "--kind", "both", "--probe-only"])
        self.assertEqual(forms.command, "requests-form-submit-diagnostics")
        self.assertEqual(forms.kind, "both")
        self.assertTrue(forms.probe_only)

        bank_export = parser.parse_args(["bank", "export", "--probe-only", "--include", "quiz,survey"])
        self.assertEqual(bank_export.command, "export-question-bank")
        self.assertTrue(bank_export.probe_only)

    def test_bank_export_rejects_invalid_delay_before_login(self):
        with redirect_stdout(StringIO()) as output:
            code = cli_module.main(
                [
                    "bank",
                    "export",
                    "--probe-only",
                    "--delay-min-ms",
                    "20",
                    "--delay-max-ms",
                    "10",
                    "--agent",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "invalid_request")
        self.assertIn("delay min", payload["message"])

    def test_cli_short_commands_map_to_daily_handlers(self):
        parser = build_parser()

        sign_in = parser.parse_args(["sign-in"])
        self.assertEqual(sign_in.command, "login-requests")
        self.assertEqual(sign_in.login_requests_command, "login")

        pending = parser.parse_args(["pending"])
        self.assertEqual(pending.command, "list")
        self.assertEqual(pending.kind, "pending")
        self.assertEqual(pending.backend, "requests")

        completed = parser.parse_args(["completed"])
        self.assertEqual(completed.command, "list")
        self.assertEqual(completed.kind, "completed")

        course = parser.parse_args(["course", "5416"])
        self.assertEqual(course.command, "inspect-course")
        self.assertEqual(course.course, "5416")

        go = parser.parse_args(["go", "--quiz", "auto"])
        self.assertEqual(go.command, "run")
        self.assertEqual(go.backend, "requests")
        self.assertEqual(go.quiz, "auto")
        self.assertEqual(go.survey, "neutral")

        root_agent = parser.parse_args(["--agent", "pending"])
        leaf_agent = parser.parse_args(["pending", "--agent"])
        self.assertEqual(root_agent.cli_mode, "agent")
        self.assertEqual(leaf_agent.cli_mode, "agent")

        advanced = parser.parse_args(["go", "--accounts", ".tms_accounts.toml", "--backend", "requests"])
        self.assertEqual(advanced.accounts, ".tms_accounts.toml")
        self.assertEqual(advanced.backend, "requests")

    def test_cli_rejects_removed_output_and_interactivity_flags(self):
        parser = build_parser()
        cases = [
            ["auth", "status", "--json"],
            ["auth", "status", "--text"],
            ["courses", "list", "pending", "--non-interactive"],
            ["auth", "requests-login", "--json"],
            ["go", "--text"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    parser.parse_args(argv)

    def test_cli_rejects_removed_top_level_legacy_commands(self):
        parser = build_parser()
        cases = [
            ["complete"],
            ["run"],
            ["list", "pending"],
            ["inspect-course", "123"],
            ["login-requests", "login"],
            ["requests-reading-accumulation-diagnostics"],
            ["validate-playwright-forms"],
            ["export-question-bank"],
            ["build-reference-question-bank"],
            ["tms-vghks"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    parser.parse_args(argv)

    def test_cli_mode_flags_are_mutually_exclusive(self):
        parser = build_parser()
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["auth", "status", "--human", "--agent"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--agent", "auth", "status", "--human"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--human", "--agent", "auth", "status"])

    def test_cli_text_formatter_keeps_generic_success_out_of_complete_summary(self):
        text = format_text_payload({"success": True, "status": "logged_in", "message": "ok"})

        self.assertEqual(text, "success: logged_in: ok")
        self.assertNotIn("Complete result", text)

    def test_cli_text_formatter_formats_daily_payloads(self):
        course_list = format_text_payload([CourseSummary(title="Course", progress="1/2")])
        course_detail = format_text_payload(
            CourseDetail(
                title="Course",
                url="https://tms.example.test/course/1",
                items=[
                    CourseItem(
                        title="Read",
                        order=1,
                        kind=ItemKind.READING,
                        state=ItemState.PENDING,
                        result="00:00",
                    )
                ],
            )
        )
        account_operation = format_text_payload(
            {
                "success": True,
                "results": [
                    {"label": "account1", "success": True, "status": "logged_in"},
                ],
            }
        )
        complete = format_text_payload({"success": True, "summary": {"courses": 1, "failed": 0}, "course_runs": []})

        self.assertIn("- Course | 1/2", course_list)
        self.assertIn("Course", course_detail)
        self.assertIn("1. Read", course_detail)
        self.assertIn("account1: logged_in", account_operation)
        self.assertIn("Complete result: success", complete)

    def test_cli_auto_human_mode_uses_text_when_tty(self):
        buffer = StringIO()
        with patch("tms_vghks.cli.TmsSession", lambda: FakeAuthSession([LoginStatus(SiteState.LOGGED_IN, message="saved")])):
            with redirect_stdout(buffer):
                with patch("sys.stdin.isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True):
                    code = cli_module.main(["auth", "status"])

        self.assertEqual(code, 0)
        self.assertIn("logged_in", buffer.getvalue())
        self.assertFalse(buffer.getvalue().lstrip().startswith("{"))

    def test_cli_auto_agent_mode_uses_json_when_not_tty(self):
        buffer = StringIO()
        with patch("tms_vghks.cli.TmsSession", lambda: FakeAuthSession([LoginStatus(SiteState.LOGGED_IN, message="saved")])):
            with redirect_stdout(buffer):
                code = cli_module.main(["auth", "status"])

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["state"], "logged_in")

    def test_cli_agent_flag_forces_json_even_with_tty(self):
        buffer = StringIO()
        with patch("tms_vghks.cli.TmsSession", lambda: FakeAuthSession([LoginStatus(SiteState.LOGGED_IN, message="saved")])):
            with redirect_stdout(buffer):
                with patch("sys.stdin.isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True):
                    code = cli_module.main(["auth", "status", "--agent"])

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["state"], "logged_in")

    def test_cli_human_flag_forces_text_when_not_tty(self):
        buffer = StringIO()
        with patch("tms_vghks.cli.TmsSession", lambda: FakeAuthSession([LoginStatus(SiteState.LOGGED_IN, message="saved")])):
            with redirect_stdout(buffer):
                code = cli_module.main(["auth", "status", "--human"])

        self.assertEqual(code, 0)
        self.assertIn("logged_in", buffer.getvalue())
        self.assertFalse(buffer.getvalue().lstrip().startswith("{"))

    def test_cli_help_prioritizes_short_daily_commands(self):
        parser = build_parser()
        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as top_help:
            parser.parse_args(["--help"])
        self.assertEqual(top_help.exception.code, 0)
        help_text = output.getvalue()
        for command in ("sign-in", "pending", "completed", "course <id-or-url>", "go", "auth", "courses", "diag", "bank"):
            self.assertIn(command, help_text)
        self.assertNotIn("  complete  ", help_text)
        self.assertNotIn("requests-reading-accumulation-diagnostics", help_text)
        self.assertNotIn("validate-playwright-forms", help_text)

        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as go_help:
            parser.parse_args(["go", "--help"])
        self.assertEqual(go_help.exception.code, 0)
        go_help_text = output.getvalue()
        self.assertIn("usage: tms-vghks-cli go", go_help_text)
        self.assertIn("--quiz", go_help_text)
        self.assertIn("--survey", go_help_text)
        self.assertIn("--dry-run", go_help_text)
        self.assertIn("--human", go_help_text)
        self.assertIn("--agent", go_help_text)
        self.assertIn("--label", go_help_text)
        self.assertNotIn("--backend", go_help_text)
        self.assertNotIn("--session-dir", go_help_text)
        self.assertNotIn("--concurrency", go_help_text)

        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as auth_login_help:
            parser.parse_args(["auth", "requests-login", "--help"])
        self.assertEqual(auth_login_help.exception.code, 0)
        auth_login_help_text = output.getvalue()
        self.assertIn("usage: tms-vghks-cli auth requests-login", auth_login_help_text)
        self.assertNotIn("login-requests login", auth_login_help_text)
        self.assertIn("--human", auth_login_help_text)
        self.assertIn("--agent", auth_login_help_text)
        self.assertNotIn("--json", auth_login_help_text)
        self.assertNotIn("--text", auth_login_help_text)
        self.assertNotIn("--non-interactive", auth_login_help_text)
        for advanced in ("--accounts", "--account", "--password", "--session-dir", "--concurrency", "--show-captcha"):
            self.assertIn(advanced, auth_login_help_text)

        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as sign_in_help:
            parser.parse_args(["sign-in", "--help"])
        self.assertEqual(sign_in_help.exception.code, 0)
        sign_in_help_text = output.getvalue()
        self.assertIn("usage: tms-vghks-cli sign-in", sign_in_help_text)
        self.assertIn("--label", sign_in_help_text)
        self.assertIn("--human", sign_in_help_text)
        self.assertIn("--agent", sign_in_help_text)
        self.assertNotIn("--accounts", sign_in_help_text)
        self.assertNotIn("--password", sign_in_help_text)

        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as courses_list_help:
            parser.parse_args(["courses", "list", "--help"])
        self.assertEqual(courses_list_help.exception.code, 0)
        courses_list_help_text = output.getvalue()
        self.assertIn("--accounts", courses_list_help_text)
        self.assertIn("--login-method", courses_list_help_text)
        self.assertIn("--backend", courses_list_help_text)

        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as courses_inspect_help:
            parser.parse_args(["courses", "inspect", "--help"])
        self.assertEqual(courses_inspect_help.exception.code, 0)
        courses_inspect_help_text = output.getvalue()
        self.assertIn("--accounts", courses_inspect_help_text)
        self.assertIn("--login-method", courses_inspect_help_text)
        self.assertIn("--backend", courses_inspect_help_text)

        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as bank_export_help:
            parser.parse_args(["bank", "export", "--help"])
        self.assertEqual(bank_export_help.exception.code, 0)
        bank_export_help_text = output.getvalue()
        self.assertIn("usage: tms-vghks-cli bank export", bank_export_help_text)
        self.assertNotIn("export-question-bank", bank_export_help_text)

        with redirect_stderr(StringIO()) as error, self.assertRaises(SystemExit) as go_error:
            parser.parse_args(["go", "--unknown"])
        self.assertEqual(go_error.exception.code, 2)
        go_error_text = error.getvalue()
        self.assertIn("usage: tms-vghks-cli go", go_error_text)

        with redirect_stderr(StringIO()) as error, self.assertRaises(SystemExit) as group_error:
            parser.parse_args(["auth", "--bad"])
        self.assertEqual(group_error.exception.code, 2)
        group_error_text = error.getvalue()
        self.assertIn("usage: tms-vghks-cli auth", group_error_text)
        self.assertIn("unrecognized arguments: --bad", group_error_text)

        with redirect_stderr(StringIO()) as error, self.assertRaises(SystemExit) as missing_group_command:
            parser.parse_args(["auth"])
        self.assertEqual(missing_group_command.exception.code, 2)
        missing_group_command_text = error.getvalue()
        self.assertIn("usage: tms-vghks-cli auth", missing_group_command_text)
        self.assertIn("the following arguments are required: <auth-command>", missing_group_command_text)

        with redirect_stderr(StringIO()) as error, self.assertRaises(SystemExit) as grouped_leaf_error:
            parser.parse_args(["auth", "requests-login", "--bad"])
        self.assertEqual(grouped_leaf_error.exception.code, 2)
        grouped_leaf_error_text = error.getvalue()
        self.assertIn("usage: tms-vghks-cli auth requests-login", grouped_leaf_error_text)
        self.assertIn("unrecognized arguments: --bad", grouped_leaf_error_text)

        with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit) as diag_help:
            parser.parse_args(["diag", "--help"])
        self.assertEqual(diag_help.exception.code, 0)
        diag_help_text = output.getvalue()
        self.assertIn("reading", diag_help_text)
        self.assertIn("forms", diag_help_text)
        self.assertIn("parity", diag_help_text)

    def test_cli_docs_do_not_advertise_legacy_aliases_and_grouped_commands_parse(self):
        parser = build_parser()
        docs = (Path(__file__).resolve().parents[1] / "docs" / "CLI.md").read_text(encoding="utf-8")
        self.assertNotIn("Legacy Aliases", docs)
        self.assertNotIn("legacy command", docs)

        cases = [
            (["auth", "requests-login"], {"command": "login-requests", "login_requests_command": "login"}),
            (["courses", "list", "pending"], {"command": "list", "kind": "pending"}),
            (["go"], {"command": "run", "backend": "requests", "quiz": "confirm", "survey": "neutral"}),
            (["diag", "reading"], {"command": "requests-reading-accumulation-diagnostics", "wait_seconds": 60}),
            (["bank", "export"], {"command": "export-question-bank", "include": "quiz,survey", "probe_only": False}),
        ]
        for argv, expected in cases:
            args = parser.parse_args(argv)
            for attr, value in expected.items():
                self.assertEqual(getattr(args, attr), value, (argv, attr))

    def test_cli_select_detail_item_skips_result_complete_items(self):
        detail = CourseDetail(
            title="Course",
            url="https://tms.vghks.gov.tw/course/1",
            items=[
                CourseItem(
                    title="Read Done By Result",
                    order=1,
                    kind=ItemKind.READING,
                    state=ItemState.IN_PROGRESS,
                    pass_condition="閱讀達 40 分鐘",
                    result="41:03",
                    passed_marker="-",
                ),
                CourseItem(
                    title="Read Pending",
                    order=2,
                    kind=ItemKind.READING,
                    state=ItemState.PENDING,
                    pass_condition="閱讀達 40 分鐘",
                    result="00:00",
                ),
            ],
        )

        selected = cli_module._select_detail_item(detail, title=None, order=None)

        self.assertEqual(selected.order, 2)

    def test_json_output_falls_back_to_ascii_on_encoding_error(self):
        class Cp950LikeStdout:
            def __init__(self):
                self.parts = []

            def write(self, value):
                value.encode("cp950")
                self.parts.append(value)

            def flush(self):
                return None

        stdout = Cp950LikeStdout()
        with redirect_stdout(stdout):
            safe_print_json({"title": "🌐我的學習"})
        self.assertIn("\\ud83c\\udf10", "".join(stdout.parts))

    def test_cli_jsonable_redacts_sensitive_url_query_tokens(self):
        payload = to_jsonable(
            {"url": "https://tms.vghks.gov.tw/ajax/sys.app.learningItem/kexam/?activityID=1&ajaxAuth=secret"}
        )
        self.assertEqual(
            payload["url"],
            "https://tms.vghks.gov.tw/ajax/sys.app.learningItem/kexam/?activityID=1&ajaxAuth=REDACTED",
        )

    def test_cli_saved_login_method_loads_bundle_through_auth_options(self):
        parser = build_parser()
        args = parser.parse_args(["pending", "--login-method", "saved"])
        session = FakeAuthSession([LoginStatus(SiteState.LOGGED_IN)])
        ensure_for_command(session, args)
        self.assertTrue(session.loaded)
        self.assertEqual(session.is_logged_calls, 1)
        self.assertEqual(session.backend, OperationBackend.REQUESTS)

    def test_cli_ensure_for_command_prompts_for_missing_human_credentials(self):
        parser = build_parser()
        args = parser.parse_args(["pending", "--human"])
        session = FakePromptAuthSession()
        stdout = StringIO()
        stderr = StringIO()

        with patch("sys.stdin.isatty", return_value=True), patch(
            "tms_vghks.cli_impl._stderr_input",
            return_value="employee-id",
        ), patch("tms_vghks.cli_impl.getpass.getpass", return_value="password-secret"), redirect_stdout(stdout), redirect_stderr(stderr):
            ensure_for_command(session, args)

        self.assertEqual(len(session.ensure_calls), 2)
        self.assertEqual(session.ensure_calls[1].account, "employee-id")
        self.assertEqual(session.ensure_calls[1].password, "password-secret")
        self.assertNotIn("password-secret", stdout.getvalue())
        self.assertNotIn("password-secret", stderr.getvalue())

    def test_cli_ensure_for_command_agent_does_not_prompt_for_credentials(self):
        parser = build_parser()
        args = parser.parse_args(["pending", "--agent"])
        session = FakePromptAuthSession()

        with patch("sys.stdin.isatty", return_value=True), patch(
            "tms_vghks.cli_impl._stderr_input",
            side_effect=AssertionError("prompted unexpectedly"),
        ), patch("tms_vghks.cli_impl.getpass.getpass", side_effect=AssertionError("prompted unexpectedly")):
            with self.assertRaisesRegex(LoginRequired, "missing credentials"):
                ensure_for_command(session, args)

        self.assertEqual(len(session.ensure_calls), 1)

    def test_cli_pending_agent_missing_default_accounts_returns_json_error_without_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_default = str(Path(tmp) / ".tms_accounts.toml")
            session = FakePromptAuthSession()
            stdout = StringIO()
            stderr = StringIO()
            with patch("tms_vghks.cli_impl.DEFAULT_ACCOUNTS_PATH", missing_default), patch(
                "tms_vghks.cli.TmsSession",
                lambda: session,
            ), patch("tms_vghks.cli_impl._stderr_input", side_effect=AssertionError("prompted unexpectedly")), patch(
                "tms_vghks.cli_impl.getpass.getpass",
                side_effect=AssertionError("prompted unexpectedly"),
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_module.main(["pending", "--agent"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(code, 10)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "login_required")
        self.assertIn("missing credentials", payload["message"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(session.ensure_calls), 1)

    def test_cli_go_human_missing_default_accounts_prompts_before_runner(self):
        class FakeRunner:
            last_options = None

            def __init__(self, session, options):
                type(self).last_options = options

            def run_scheduler(self):
                return RunResult(True, SiteState.LOGGED_IN, "ok", data={"course_runs": [], "summary": {}})

        with tempfile.TemporaryDirectory() as tmp:
            missing_default = str(Path(tmp) / ".tms_accounts.toml")
            session = FakePromptAuthSession()
            stdout = StringIO()
            stderr = StringIO()
            with patch("tms_vghks.cli_impl.DEFAULT_ACCOUNTS_PATH", missing_default), patch(
                "tms_vghks.cli.TmsSession",
                lambda: session,
            ), patch("tms_vghks.cli.TmsRunner", FakeRunner), patch("sys.stdin.isatty", return_value=True), patch(
                "tms_vghks.cli_impl._stderr_input",
                return_value="employee-id",
            ), patch("tms_vghks.cli_impl.getpass.getpass", return_value="password-secret"), redirect_stdout(
                stdout
            ), redirect_stderr(
                stderr
            ):
                code = cli_module.main(["go", "--human"])

        self.assertEqual(code, 0)
        self.assertIn("was not found", stderr.getvalue())
        self.assertEqual(len(session.ensure_calls), 2)
        self.assertEqual(FakeRunner.last_options.auth_options.account, "employee-id")
        self.assertEqual(FakeRunner.last_options.auth_options.password, "password-secret")
        self.assertNotIn("password-secret", stdout.getvalue())
        self.assertNotIn("password-secret", stderr.getvalue())

    def test_cli_ensure_for_command_saved_method_does_not_prompt_for_credentials(self):
        parser = build_parser()
        args = parser.parse_args(["pending", "--login-method", "saved"])
        session = FakePromptAuthSession()

        with patch("sys.stdin.isatty", return_value=True), patch(
            "tms_vghks.cli_impl._stderr_input",
            side_effect=AssertionError("prompted unexpectedly"),
        ), patch("tms_vghks.cli_impl.getpass.getpass", side_effect=AssertionError("prompted unexpectedly")):
            with self.assertRaisesRegex(LoginRequired, "missing credentials"):
                ensure_for_command(session, args)

        self.assertEqual(len(session.ensure_calls), 1)

    def test_cli_ensure_for_command_applies_selected_backend_before_login(self):
        parser = build_parser()
        args = parser.parse_args(["pending", "--backend", "playwright", "--login-method", "saved"])
        session = FakeAuthSession([LoginStatus(SiteState.LOGGED_IN)])

        ensure_for_command(session, args)

        self.assertEqual(session.backend, OperationBackend.PLAYWRIGHT)

    def test_cli_playwright_only_command_uses_playwright_backend_before_auto_login(self):
        parser = build_parser()
        args = parser.parse_args(["diag", "playwright-forms"])
        session = FakeAuthSession([LoginStatus(SiteState.LOGIN_REQUIRED)])
        session.load_raises = True

        ensure_for_command(session, args)

        self.assertEqual(session.backend, OperationBackend.PLAYWRIGHT)
        self.assertTrue(session.playwright_login)
        self.assertFalse(session.requests_login)

    def test_accounts_playwright_command_uses_saved_browser_session(self):
        parser = build_parser()
        args = parser.parse_args(["diag", "playwright-kexam-records", "--login-method", "saved", "--headless"])
        config = AccountsLoginConfig()
        account = AccountLoginConfig(label="a", account="u", password="p", session_dir=".session-a")
        session = FakeAccountBackendSession()

        auth_options = cli_module._ensure_account_session_authenticated(config, account, args, session)

        self.assertEqual(session.backends, [OperationBackend.PLAYWRIGHT])
        self.assertTrue(session.browser_headless)
        self.assertEqual(session.saved_browser_calls, [(".session-a", True)])
        self.assertEqual(session.saved_requests_calls, [])
        self.assertEqual(auth_options.login_method, LoginMethod.SAVED)

    def test_runner_applies_selected_backend_to_session_before_auto_login(self):
        session = FakeAuthSession([LoginStatus(SiteState.LOGGED_IN)])
        TmsRunner(session, RunOptions(backend=OperationBackend.PLAYWRIGHT, headless=True))

        self.assertEqual(session.backend, OperationBackend.PLAYWRIGHT)
        self.assertTrue(session.browser_headless)

    def test_cli_playwright_read_path_preserves_headless_after_saved_auth(self):
        buffer = StringIO()
        with patch("tms_vghks.cli.TmsSession", FakeHeadlessListSession), patch(
            "tms_vghks.cli._apply_default_accounts_file",
            lambda args: None,
        ):
            with redirect_stdout(buffer):
                code = cli_module.main(
                    ["pending", "--backend", "playwright", "--login-method", "saved", "--headless"]
                )

        self.assertEqual(code, 0)
        session = FakeHeadlessListSession.last
        self.assertIsNotNone(session)
        self.assertEqual(session.auth_options.login_method, LoginMethod.SAVED)
        self.assertTrue(session.auth_options.headless)
        self.assertEqual(
            session.list_calls,
            [{"backend": OperationBackend.PLAYWRIGHT, "browser_headless": True}],
        )

    def test_cli_reports_transient_error_as_json(self):
        buffer = StringIO()
        with patch("tms_vghks.cli.TmsSession", return_value=FakeTransientCliSession()):
            with redirect_stdout(buffer):
                code = cli_module.main(["auth", "login", "--transient-retries", "1"])
        self.assertEqual(code, 12)
        self.assertIn('"error": "transient_error"', buffer.getvalue())

    def test_list_courses_falls_back_to_browser_html_when_requests_fails(self):
        class FallbackSession(TmsSession):
            context = object()

            def _fetch_authenticated_html(self, path_or_url):
                raise TmsError("TMS request timed out")

            def fetch_html_with_browser(self, path_or_url):
                return """
                <table>
                  <tr><th>課程名稱</th><th>完成度</th><th>操作</th></tr>
                  <tr><td>感染管制</td><td>50%</td><td><a href="/course/123">進入</a></td></tr>
                </table>
                """

        courses = FallbackSession().list_pending_courses(backend=OperationBackend.HYBRID)
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].title, "感染管制")

    def test_detail_falls_back_to_browser_html_when_requests_fails(self):
        class FallbackSession(TmsSession):
            context = object()

            def _fetch_authenticated_html(self, path_or_url):
                raise TmsError("TMS request timed out")

            def fetch_html_with_browser(self, path_or_url):
                return """
                <h1>感染管制</h1>
                <table>
                  <tr><th>項次</th><th>項目名稱</th><th>通過條件</th><th>學習成果</th><th>通過</th></tr>
                  <tr><td>1</td><td>閱讀教材</td><td>閱讀達 1 分鐘</td><td>01:01</td><td>-</td></tr>
                </table>
                """

        detail = FallbackSession().get_course_detail("123", backend=OperationBackend.HYBRID)
        self.assertEqual(detail.title, "感染管制")
        self.assertEqual(len(detail.items), 1)

    def test_runner_auto_loads_latest_root_question_bank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bank = root / "question-bank-20260201.jsonl"
            bank.write_text(
                json.dumps(
                    {
                        "course": {"title": "Course"},
                        "activity": {"title": "課後測驗"},
                        "quiz_stage": "posttest",
                        "question": {"text": "何者正確？", "options": ["A", "B"], "merge_key": "mk1"},
                        "answer": {"answers": ["B"], "status": "verified_correct", "trusted_for_auto": True},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                runner = TmsRunner(TmsSession(), RunOptions())
            finally:
                os.chdir(old_cwd)
            self.assertIsNotNone(runner.question_bank)
            self.assertEqual(len(runner.question_bank.entries), 1)

    def test_account_scoped_default_path_only_changes_default_output(self):
        default = ".tms_private_exports/playwright-form-validation.jsonl"

        self.assertEqual(
            Path(cli_module._account_scoped_default_path(default, default, "account 1")),
            Path(".tms_private_exports/playwright-form-validation-account_1.jsonl"),
        )
        self.assertEqual(
            cli_module._account_scoped_default_path(".custom/out.jsonl", default, "account 1"),
            ".custom/out.jsonl",
        )


if __name__ == "__main__":
    unittest.main()
