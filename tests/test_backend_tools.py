import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.backends import HybridBackendTools, PlaywrightBackendTools, RequestsBackendTools, TmsBackendTools
from tms_vghks.handlers import RunOptions, TmsRunner
from tms_vghks.models import CourseDetail, CourseItem, ItemKind, ItemState, LoginMethod, LoginStatus, OperationBackend, RunResult, SiteState
from tms_vghks.parsers import PENDING_PATH
from tms_vghks.session import TmsSession


class BackendHtmlSession(TmsSession):
    def _is_detail_url(self, path_or_url):
        return str(path_or_url).rstrip("/").split("/")[-1].isdigit()

    def _fetch_authenticated_html(self, path_or_url):
        if self._is_detail_url(path_or_url):
            return """
            <h1>Requests Detail</h1>
            <table>
              <tr><th>項次</th><th>項目名稱</th><th>通過條件</th><th>學習成果</th><th>通過</th></tr>
              <tr><td>1</td><td>Requests Reading</td><td>閱讀達 1 分鐘</td><td>00:00</td><td>-</td></tr>
            </table>
            """
        return """
        <table>
          <tr><th>課程名稱</th><th>完成度</th><th>操作</th></tr>
          <tr><td>Requests Course</td><td>50%</td><td><a href="/course/1">進入</a></td></tr>
        </table>
        """

    def fetch_html_with_browser(self, path_or_url):
        if self._is_detail_url(path_or_url):
            return """
            <h1>Playwright Detail</h1>
            <table>
              <tr><th>項次</th><th>項目名稱</th><th>通過條件</th><th>學習成果</th><th>通過</th></tr>
              <tr><td>1</td><td>Playwright Reading</td><td>閱讀達 1 分鐘</td><td>00:00</td><td>-</td></tr>
            </table>
            """
        return """
        <table>
          <tr><th>課程名稱</th><th>完成度</th><th>操作</th></tr>
          <tr><td>Playwright Course</td><td>50%</td><td><a href="/course/2">進入</a></td></tr>
        </table>
        """

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        return f"activity requests:{path_or_url}:{referer}"

    def fetch_activity_html_playwright(self, path_or_url, referer=None):
        return f"activity playwright:{path_or_url}:{referer}"


class RecordingAuthSession(TmsSession):
    def __init__(self):
        super().__init__()
        self.auth_options = []
        self.auth_backends = []
        self.status_fallbacks = []
        self.browser_status_calls = 0
        self.recover_calls = []
        self.requests_recover_calls = []
        self.requests_recover_status = LoginStatus(SiteState.LOGGED_IN, message="recovered")

    def ensure_authenticated(self, options=None):
        self.auth_options.append(options)
        self.auth_backends.append(self.backend)
        return LoginStatus(SiteState.LOGGED_IN, message="ok")

    def is_logged_in(self, fallback_browser=False):
        self.status_fallbacks.append(fallback_browser)
        return LoginStatus(SiteState.LOGGED_IN, message=f"requests fallback={fallback_browser}")

    def browser_status(self):
        self.browser_status_calls += 1
        return LoginStatus(SiteState.LOGGED_IN, message="browser")

    def recover_transient_page(self, page, refresh_target=None, retries=None, delay_seconds=None):
        self.recover_calls.append(
            {
                "page": page,
                "refresh_target": refresh_target,
                "retries": retries,
                "delay_seconds": delay_seconds,
            }
        )
        return True

    def recover_transient_requests(self, path_or_url=PENDING_PATH, retries=None, delay_seconds=None):
        self.requests_recover_calls.append(
            {
                "path_or_url": path_or_url,
                "retries": retries,
                "delay_seconds": delay_seconds,
            }
        )
        return self.requests_recover_status


class BackendToolsTests(unittest.TestCase):
    def test_objective_required_backend_api_contract_is_explicit(self):
        required_tool_methods = {
            "auto_login_tms",
            "login",
            "ensure_authenticated",
            "check_status",
            "detect_error_state",
            "recover_transient_error",
            "list_pending_courses",
            "list_completed_courses",
            "get_course_detail",
            "fetch_activity_html",
            "complete_item",
            "complete_reading",
            "complete_survey",
            "complete_quiz",
            "run_course",
            "run_scheduler",
        }
        required_runner_methods = {
            "run_item_requests",
            "run_item_playwright",
            "run_reading_requests",
            "run_reading_playwright",
            "start_reading_requests",
            "start_reading_playwright",
            "finish_reading_requests",
            "finish_reading_playwright",
            "run_survey_requests",
            "run_survey_playwright",
            "run_quiz_requests",
            "run_quiz_playwright",
        }
        required_session_methods = {
            "list_pending_courses_requests",
            "list_pending_courses_playwright",
            "list_completed_courses_requests",
            "list_completed_courses_playwright",
            "get_course_detail_requests",
            "get_course_detail_playwright",
            "fetch_activity_html_requests",
            "fetch_activity_html_playwright",
            "requests_tools",
            "playwright_tools",
            "hybrid_tools",
            "using_backend",
        }

        for tool_class in (RequestsBackendTools, PlaywrightBackendTools, HybridBackendTools):
            for method_name in required_tool_methods:
                self.assertTrue(callable(getattr(tool_class, method_name, None)), f"{tool_class.__name__}.{method_name}")
        for method_name in required_runner_methods:
            self.assertTrue(callable(getattr(TmsRunner, method_name, None)), f"TmsRunner.{method_name}")
        for method_name in required_session_methods:
            self.assertTrue(callable(getattr(TmsSession, method_name, None)), f"TmsSession.{method_name}")
        self.assertEqual(RunOptions().backend, OperationBackend.REQUESTS)
        self.assertEqual(TmsSession().backend, OperationBackend.REQUESTS)

    def test_backend_tool_classes_expose_symmetric_public_api(self):
        expected_methods = {
            name
            for name, value in TmsBackendTools.__dict__.items()
            if callable(value) and not name.startswith("_")
        }

        for tool_class in (RequestsBackendTools, PlaywrightBackendTools, HybridBackendTools):
            available_methods = {
                name
                for name in dir(tool_class)
                if callable(getattr(tool_class, name, None)) and not name.startswith("_")
            }
            self.assertTrue(expected_methods.issubset(available_methods), tool_class.__name__)

    def test_session_backend_tools_default_and_switching(self):
        session = BackendHtmlSession()

        self.assertIsInstance(session.tools, RequestsBackendTools)
        self.assertEqual(session.tools.list_pending_courses()[0].title, "Requests Course")
        self.assertEqual(session.playwright_tools().list_pending_courses()[0].title, "Playwright Course")
        self.assertEqual(
            session.requests_tools().fetch_activity_html("/activity/1", referer="/course/1"),
            "activity requests:/activity/1:/course/1",
        )
        self.assertEqual(
            session.playwright_tools().fetch_activity_html("/activity/1"),
            "activity playwright:/activity/1:None",
        )

        session.use_backend("playwright")
        self.assertIsInstance(session.tools, PlaywrightBackendTools)
        self.assertEqual(session.tools.get_course_detail("2").title, "Playwright Detail")

        with session.using_backend("hybrid"):
            self.assertIsInstance(session.tools, HybridBackendTools)
        self.assertIsInstance(session.tools, PlaywrightBackendTools)

    def test_backend_tools_map_auto_login_method_to_selected_backend(self):
        session = RecordingAuthSession()

        session.requests_tools().auto_login_tms()
        session.requests_tools().login()
        session.requests_tools().ensure_authenticated()
        session.playwright_tools().ensure_authenticated()
        session.hybrid_tools().ensure_authenticated()
        session.requests_tools().ensure_authenticated(options=RunOptions().auth_options)

        self.assertEqual(session.auth_options[0].login_method, LoginMethod.REQUESTS)
        self.assertEqual(session.auth_options[1].login_method, LoginMethod.REQUESTS)
        self.assertEqual(session.auth_options[2].login_method, LoginMethod.REQUESTS)
        self.assertEqual(session.auth_options[3].login_method, LoginMethod.PLAYWRIGHT)
        self.assertEqual(session.auth_options[4].login_method, LoginMethod.AUTO)
        self.assertEqual(session.auth_options[5].login_method, LoginMethod.REQUESTS)
        self.assertEqual(
            session.auth_backends,
            [
                OperationBackend.REQUESTS,
                OperationBackend.REQUESTS,
                OperationBackend.REQUESTS,
                OperationBackend.PLAYWRIGHT,
                OperationBackend.HYBRID,
                OperationBackend.REQUESTS,
            ],
        )

    def test_backend_tools_detect_status_and_recover_transient_errors(self):
        session = RecordingAuthSession()

        self.assertEqual(session.requests_tools().check_status().message, "requests fallback=False")
        self.assertEqual(session.hybrid_tools().detect_error_state().message, "requests fallback=True")
        session.page = object()
        self.assertEqual(session.playwright_tools().check_status().message, "browser")
        recovered = session.playwright_tools().recover_transient_error(
            refresh_target="/course/1",
            retries=1,
            delay_seconds=0,
        )

        self.assertTrue(recovered)
        self.assertEqual(session.status_fallbacks, [False, True])
        self.assertEqual(session.browser_status_calls, 1)
        self.assertEqual(session.recover_calls[0]["refresh_target"], "/course/1")
        self.assertEqual(session.recover_calls[0]["retries"], 1)

    def test_backend_tools_recover_transient_errors_through_requests(self):
        session = RecordingAuthSession()

        recovered = session.requests_tools().recover_transient_error(retries=2, delay_seconds=0)

        self.assertTrue(recovered)
        self.assertEqual(session.requests_recover_calls[0]["path_or_url"], PENDING_PATH)
        self.assertEqual(session.requests_recover_calls[0]["retries"], 2)

        session.requests_recover_status = LoginStatus(SiteState.TRANSIENT_ERROR, message="still transient")
        self.assertFalse(session.requests_tools().recover_transient_error(refresh_target="/course/1", retries=1))
        self.assertEqual(session.requests_recover_calls[1]["path_or_url"], "/course/1")

    def test_backend_tools_complete_item_forces_runner_backend(self):
        session = BackendHtmlSession()
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(title="Read", kind=ItemKind.READING, state=ItemState.PENDING)
        captured_backends = []
        captured_login_methods = []

        class FakeRunner:
            def __init__(self, session, options):
                captured_backends.append(options.backend)
                captured_login_methods.append(options.auth_options.login_method)

            def run_item(self, course, item):
                return RunResult(True, ItemState.PASSED, "ok", course=course, item=item)

        with patch("tms_vghks.backends.TmsRunner", FakeRunner):
            result = session.playwright_tools().complete_item(course, item, options=RunOptions(backend=OperationBackend.REQUESTS))

        self.assertTrue(result.success)
        self.assertEqual(captured_backends, [OperationBackend.PLAYWRIGHT])
        self.assertEqual(captured_login_methods, [LoginMethod.AUTO])

    def test_backend_tools_specialized_completion_helpers_are_symmetric(self):
        session = BackendHtmlSession()
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        items = [
            CourseItem(title="Read", kind=ItemKind.READING, state=ItemState.PENDING),
            CourseItem(title="Survey", kind=ItemKind.SURVEY, state=ItemState.PENDING),
            CourseItem(title="Quiz", kind=ItemKind.QUIZ, state=ItemState.PENDING),
        ]
        captured_backends = []
        captured_items = []

        class FakeRunner:
            def __init__(self, session, options):
                captured_backends.append(options.backend)

            def run_item(self, course, item):
                captured_items.append(item.kind)
                return RunResult(True, ItemState.PASSED, "ok", course=course, item=item)

        with patch("tms_vghks.backends.TmsRunner", FakeRunner):
            tools = session.requests_tools()
            results = [
                tools.complete_reading(course, items[0], options=RunOptions(backend=OperationBackend.PLAYWRIGHT)),
                tools.complete_survey(course, items[1], options=RunOptions(backend=OperationBackend.PLAYWRIGHT)),
                tools.complete_quiz(course, items[2], options=RunOptions(backend=OperationBackend.PLAYWRIGHT)),
            ]

        self.assertTrue(all(result.success for result in results))
        self.assertEqual(captured_backends, [OperationBackend.REQUESTS, OperationBackend.REQUESTS, OperationBackend.REQUESTS])
        self.assertEqual(captured_items, [ItemKind.READING, ItemKind.SURVEY, ItemKind.QUIZ])

    def test_backend_tools_specialized_completion_helpers_reject_wrong_item_kind(self):
        session = BackendHtmlSession()
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(title="Read", kind=ItemKind.READING, state=ItemState.PENDING)
        runner_created = False

        class FakeRunner:
            def __init__(self, session, options):
                nonlocal runner_created
                runner_created = True

        with patch("tms_vghks.backends.TmsRunner", FakeRunner):
            result = session.requests_tools().complete_survey(course, item)

        self.assertFalse(result.success)
        self.assertEqual(result.state, ItemState.BLOCKED)
        self.assertIn("complete_survey requires survey item", result.message)
        self.assertEqual(result.data["backend"], "requests")
        self.assertEqual(result.data["expected_item_kinds"], ["survey"])
        self.assertEqual(result.data["item_kind"], "reading")
        self.assertFalse(runner_created)


if __name__ == "__main__":
    unittest.main()
