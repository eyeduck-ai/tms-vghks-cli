import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from requests import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks import cli as cli_module
from tms_vghks import login_error_probes as probe_module
from tms_vghks.batch_login import BatchAccountLoginResult, BatchLoginResult
from tms_vghks.captcha_recognizers import OcrConfig, OcrResult, PaddleOcrApiConfig
from tms_vghks.cli import build_parser, to_jsonable
from tms_vghks.models import AuthOptions, LoginMethod, LoginStatus, RequestsLoginChallenge, RequestsLoginResult, RunResult, SiteState
from tms_vghks.requests_login import (
    build_login_payload,
    classify_login_response,
    cookiejar_to_playwright_storage_state,
    extract_login_failure_message,
    extract_multi_login_modal_url,
    load_challenge,
    load_session_bundle,
    login_response_text_excerpt,
    missing_login_fields,
    parse_login_challenge_html,
    parse_multi_login_modal_html,
    save_challenge,
    save_session_bundle,
    serialize_cookiejar,
)
from tms_vghks.session import LoginRequired, TmsSession


LOGIN_HTML = """
<form id="login_form" action="/index/login" method="post" onsubmit="return false;">
  <input type="hidden" name="next" value="/course/notCompleteList">
  <input type="hidden" name="act" value="">
  <input type="text" name="account" value="">
  <input type="password" name="password" value="">
  <img class="js-captcha" src="/sys/libs/class/capcha/secimg.php">
  <input type="text" name="captcha">
  <input type="hidden" name="anticsrf" value="token.123">
  <button type="button" data-role="form-submit">登入</button>
</form>
"""

MULTI_LOGIN_CUSTOM_JS = """
$('#checkMultiLogin_modal').data('url', '/ajax/sys.pages.index/checkMultiLogin/?next=%2Fcourse%2FnotCompleteList&id=11078&_lock=next%2Cid&ajaxAuth=secret')
$('#checkMultiLogin_modal').modal('show');
"""

MULTI_LOGIN_MODAL_HTML = """
<form id="categoryForm" action="/index/?_pageMode=checkMultiLogin&amp;next=%2Fcourse%2FnotCompleteList&amp;_lock=_pageMode%2Cnext&amp;ajaxAuth=modal-secret">
  <input type="hidden" name="anticsrf" value="modal-token">
  <a href="####" class="btn btn-primary kickOtherBtn">logout all</a>
  <a href="####" class="btn btn-default keepLoginBtn">keep login</a>
</form>
"""


def make_response(status_code: int, body: str, url: str = "https://tms.vghks.gov.tw/index/login") -> Response:
    response = Response()
    response.status_code = status_code
    response.url = url
    response._content = body.encode("utf-8")
    response.headers["content-type"] = "text/html; charset=UTF-8"
    return response


def make_json_response(payload, url: str = "https://tms.vghks.gov.tw/index/login") -> Response:
    response = make_response(200, json.dumps(payload, ensure_ascii=False), url)
    response.headers["content-type"] = "application/json; charset=UTF-8"
    return response


class ClassifiedSession(TmsSession):
    def __init__(self, login_status: LoginStatus):
        super().__init__()
        self.login_status = login_status

    def is_logged_in(self, fallback_browser: bool = False):
        return self.login_status


class FakeCliRequestsSession:
    def __init__(self):
        self.prepared = False
        self.submitted = []
        self.saved = False
        self.transient_policy = None
        self.challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
            captcha_path="captcha.jpg",
        )
        self.submit_result = RequestsLoginResult(True, "logged_in", "ok")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def configure_transient_policy(self, retries=None, delay_seconds=None):
        self.transient_policy = (retries, delay_seconds)

    def prepare_requests_login(self, captcha_path, show_captcha=True, session_dir=".tms_session"):
        self.prepared = True
        self.challenge.captcha_path = str(captcha_path)
        return self.challenge

    def submit_requests_login(self, **kwargs):
        self.submitted.append(kwargs)
        return self.submit_result


class FakeAccountCommandSession:
    instances = []

    def __init__(self, base_url="https://tms.vghks.gov.tw"):
        self.base_url = base_url
        self.auth_options = None
        self.transient_policy = None
        FakeAccountCommandSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def configure_transient_policy(self, retries=None, delay_seconds=None):
        self.transient_policy = (retries, delay_seconds)

    def ensure_authenticated(self, auth_options):
        self.auth_options = auth_options
        return LoginStatus(SiteState.LOGGED_IN, message="ok")

    def ensure_saved_browser_authenticated(self, session_dir=".tms_session", headless=False):
        self.auth_options = AuthOptions(login_method=LoginMethod.SAVED, session_dir=session_dir, headless=headless)
        self.saved_browser_session_dir = session_dir
        self.saved_browser_headless = headless
        return LoginStatus(SiteState.LOGGED_IN, message="ok")

    def list_pending_courses(self):
        return [{"title": f"pending:{self.auth_options.session_dir}"}]

    def list_completed_courses(self):
        return [{"title": f"completed:{self.auth_options.session_dir}"}]

    def list_pending_courses_playwright(self):
        return [{"title": f"pending-playwright:{self.auth_options.session_dir}"}]

    def list_completed_courses_playwright(self):
        return [{"title": f"completed-playwright:{self.auth_options.session_dir}"}]

    def get_course_detail(self, course):
        return {"title": course, "session_dir": self.auth_options.session_dir}

    def get_course_detail_playwright(self, course):
        return {"title": course, "session_dir": self.auth_options.session_dir, "backend": "playwright"}


class FakeExpiredThenSavedAccountSession(FakeAccountCommandSession):
    def __init__(self, base_url="https://tms.vghks.gov.tw"):
        super().__init__(base_url=base_url)
        self.ensure_calls = 0

    def ensure_authenticated(self, auth_options):
        self.auth_options = auth_options
        self.ensure_calls += 1
        if self.ensure_calls == 1:
            raise LoginRequired("saved session expired")
        return LoginStatus(SiteState.LOGGED_IN, message="ok")


class FakeAccountPlaywrightMethodSession(FakeAccountCommandSession):
    def __init__(self, base_url="https://tms.vghks.gov.tw"):
        super().__init__(base_url=base_url)
        self.login_calls = []

    def login_playwright_with_ocr(self, **kwargs):
        self.login_calls.append(kwargs)
        self.auth_options = AuthOptions(login_method=LoginMethod.SAVED, session_dir=kwargs["session_dir"])
        return {
            "success": True,
            "status": "logged_in",
            "session_dir": kwargs["session_dir"],
        }


class FakeRunner:
    instances = []

    def __init__(self, session, options):
        self.session = session
        self.options = options
        FakeRunner.instances.append(self)

    def run_scheduler(self):
        return RunResult(True, "done", "ok")


class FakePlaywrightLoginSession:
    instances = []

    def __init__(self, base_url="https://tms.vghks.gov.tw"):
        self.base_url = base_url
        self.login_calls = []
        FakePlaywrightLoginSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def login_playwright_with_ocr(self, **kwargs):
        self.login_calls.append(kwargs)
        return {
            "success": True,
            "status": "logged_in",
            "message": "ok",
            "session_dir": kwargs["session_dir"],
            "requests_cookies_path": f"{kwargs['session_dir']}/requests_cookies.json",
            "playwright_storage_state_path": f"{kwargs['session_dir']}/playwright_storage_state.json",
            "ocr_source": "paddleocr-sdk",
            "ocr_confidence": 0.99,
        }

    def list_pending_courses_playwright(self):
        return [{"title": "pending"}]

    def list_completed_courses_playwright(self):
        return [{"title": "completed 1"}, {"title": "completed 2"}]


class FakeDiagnosticPage:
    def on(self, event, callback):
        self.event = event
        self.callback = callback


class FakeDiagnosticSession(FakePlaywrightLoginSession):
    def __init__(self, base_url="https://tms.vghks.gov.tw"):
        super().__init__(base_url=base_url)
        self.page = FakeDiagnosticPage()

    def start_browser(self, headless=False):
        return object()


class FakeErrorProbeRequestsSession:
    instances = []
    statuses = []

    def __init__(self, base_url="https://tms.vghks.gov.tw"):
        self.base_url = base_url
        self.prepared = []
        self.submitted = []
        FakeErrorProbeRequestsSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def configure_transient_policy(self, retries=None, delay_seconds=None):
        self.transient_policy = (retries, delay_seconds)

    def prepare_requests_login(self, captcha_path, show_captcha=False, session_dir=".tms_session"):
        self.prepared.append((str(captcha_path), session_dir))
        return RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
            captcha_path=str(captcha_path),
        )

    def submit_requests_login(self, **kwargs):
        self.submitted.append(kwargs)
        status = (
            FakeErrorProbeRequestsSession.statuses[len(self.submitted) - 1]
            if len(self.submitted) <= len(FakeErrorProbeRequestsSession.statuses)
            else "credential_failed"
        )
        return RequestsLoginResult(
            success=False,
            status=status,
            message="驗證碼錯誤" if status == "captcha_failed" else "帳號或密碼錯誤",
            failure_message="驗證碼錯誤" if status == "captcha_failed" else "帳號或密碼錯誤",
            response_status_code=200,
            redirect_url="https://tms.vghks.gov.tw/index/login",
            login_state_after_post="login_required",
            response_text_excerpt="驗證碼錯誤" if status == "captcha_failed" else "帳號或密碼錯誤",
        )


class RequestsLoginTests(unittest.TestCase):
    def setUp(self):
        FakeAccountCommandSession.instances = []
        FakeRunner.instances = []
        FakePlaywrightLoginSession.instances = []
        FakeErrorProbeRequestsSession.instances = []
        FakeErrorProbeRequestsSession.statuses = []

    def test_parse_login_challenge_html(self):
        challenge = parse_login_challenge_html(
            LOGIN_HTML,
            "https://tms.vghks.gov.tw/index/login?next=x",
            "https://tms.vghks.gov.tw",
            ".tms_session/captcha.jpg",
            [{"name": "PHPSESSID", "value": "abc"}],
        )
        self.assertEqual(challenge.action_url, "https://tms.vghks.gov.tw/index/login")
        self.assertEqual(challenge.hidden_fields["next"], "/course/notCompleteList")
        self.assertEqual(challenge.anticsrf, "token.123")
        self.assertEqual(challenge.captcha_url, "https://tms.vghks.gov.tw/sys/libs/class/capcha/secimg.php")

    def test_parse_multi_login_custom_js_and_modal_html(self):
        payload = {"ret": {"action": {"customJs": MULTI_LOGIN_CUSTOM_JS}}}
        modal_url = extract_multi_login_modal_url(payload, "https://tms.vghks.gov.tw")
        modal = parse_multi_login_modal_html(
            MULTI_LOGIN_MODAL_HTML,
            modal_url or "",
            "https://tms.vghks.gov.tw",
        )

        self.assertEqual(
            modal_url,
            "https://tms.vghks.gov.tw/ajax/sys.pages.index/checkMultiLogin/?next=%2Fcourse%2FnotCompleteList&id=11078&_lock=next%2Cid&ajaxAuth=secret",
        )
        self.assertEqual(
            modal.form_action_url,
            "https://tms.vghks.gov.tw/index/?_pageMode=checkMultiLogin&next=%2Fcourse%2FnotCompleteList&_lock=_pageMode%2Cnext&ajaxAuth=modal-secret",
        )
        self.assertEqual(modal.hidden_fields["anticsrf"], "modal-token")
        self.assertTrue(modal.has_keep_login)
        self.assertTrue(modal.has_kick_other)

    def test_parse_multi_login_custom_js_accepts_double_quotes_and_newlines(self):
        payload = {
            "ret": {
                "action": {
                    "customJs": """
                    $("#checkMultiLogin_modal")
                      .data("url", "/ajax/sys.pages.index/checkMultiLogin/?ajaxAuth=secret")
                      .modal("show");
                    """
                }
            }
        }
        modal_url = extract_multi_login_modal_url(payload, "https://tms.vghks.gov.tw")

        self.assertEqual(
            modal_url,
            "https://tms.vghks.gov.tw/ajax/sys.pages.index/checkMultiLogin/?ajaxAuth=secret",
        )

    def test_build_login_payload(self):
        challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"next": "/course/notCompleteList", "act": "", "anticsrf": "token"},
        )
        payload = build_login_payload(challenge, account="a", password="p", captcha="1234")
        self.assertEqual(payload["account"], "a")
        self.assertEqual(payload["password"], "p")
        self.assertEqual(payload["captcha"], "1234")
        self.assertEqual(payload["_fmSubmit"], "yes")
        self.assertEqual(payload["formVer"], "3.0")
        self.assertEqual(payload["formId"], "login_form")
        self.assertEqual(payload["anticsrf"], "token")

    def test_missing_fields(self):
        self.assertEqual(missing_login_fields("", "", ""), ["account", "password", "captcha"])
        self.assertEqual(missing_login_fields("a", "p", "1"), [])

    def test_save_load_challenge(self):
        challenge = RequestsLoginChallenge(
            login_url="https://example.test/login",
            action_url="https://example.test/login",
            hidden_fields={"anticsrf": "token"},
            anticsrf="token",
            captcha_url="https://example.test/captcha",
            captcha_path="captcha.jpg",
            cookies=[{"name": "PHPSESSID", "value": "abc"}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            save_challenge(challenge, tmp)
            loaded = load_challenge(tmp)
        self.assertEqual(loaded.anticsrf, "token")
        self.assertEqual(loaded.cookies[0]["name"], "PHPSESSID")

    def test_save_load_session_bundle(self):
        session = requests.Session()
        session.cookies.set("PHPSESSID", "abc", domain="tms.vghks.gov.tw", path="/", secure=True)
        with tempfile.TemporaryDirectory() as tmp:
            paths = save_session_bundle(session.cookies, "https://tms.vghks.gov.tw", tmp)
            restored = load_session_bundle(tmp)
            state = json.loads(Path(paths["playwright_storage_state_path"]).read_text(encoding="utf-8"))
        self.assertEqual(restored.get("PHPSESSID", domain="tms.vghks.gov.tw", path="/"), "abc")
        self.assertEqual(state["cookies"][0]["name"], "PHPSESSID")

    def test_submit_requests_login_blocks_blank_by_default(self):
        challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
        )
        session = TmsSession()
        result = session.submit_requests_login(challenge=challenge)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "missing_fields")

    def test_submit_requests_login_reports_missing_challenge(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = TmsSession().submit_requests_login(session_dir=tmp)
        self.assertFalse(result.success)
        self.assertEqual(result.status, "missing_challenge")

    def test_submit_requests_login_reports_transient_marker(self):
        challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
        )
        session = TmsSession()
        session.configure_transient_policy(retries=1, delay_seconds=0)
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url))
            return make_response(200, "儲存失敗，請檢查伺服器狀態")

        session.http.request = request
        result = session.submit_requests_login(
            account="a",
            password="p",
            captcha="1234",
            challenge=challenge,
            transient_retries=1,
            transient_delay_seconds=0,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.status, "transient_error")
        self.assertEqual(len(calls), 2)

    def test_submit_requests_login_reports_http_503_as_transient(self):
        challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
        )
        session = TmsSession()

        def request(method, url, **kwargs):
            return make_response(503, "Service Unavailable")

        session.http.request = request
        result = session.submit_requests_login(
            account="a",
            password="p",
            captcha="1234",
            challenge=challenge,
            transient_retries=0,
            transient_delay_seconds=0,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.status, "transient_error")

    def test_login_response_classification_helpers(self):
        self.assertEqual(classify_login_response({"ret": {"msg": "驗證碼錯誤"}}, "", False), "captcha_failed")
        self.assertEqual(classify_login_response({"msg": "帳號或密碼錯誤"}, "", False), "credential_failed")
        self.assertEqual(classify_login_response({"msg": "登入失敗"}, "", False), "credential_failed")
        self.assertEqual(
            classify_login_response(
                {"ret": {"msg": [{"id": "login_form", "msg": "登入資訊錯誤，已經登入失敗 1 次"}]}},
                "",
                False,
            ),
            "credential_failed",
        )
        self.assertEqual(classify_login_response({"msg": "其他錯誤"}, "", False), "login_failed")
        self.assertEqual(classify_login_response(None, "", True), "logged_in")
        self.assertEqual(extract_login_failure_message({"ret": {"msg": "驗證碼錯誤"}}, ""), "驗證碼錯誤")

    def test_login_response_excerpt_redacts_submitted_values_and_hidden_tokens(self):
        excerpt = login_response_text_excerpt(
            """
            <form>
              <input type="hidden" name="anticsrf" value="secret-token">
              <p>user-a pass-p 1234 驗證碼錯誤 token=abcdef</p>
            </form>
            """,
            account="user-a",
            password="pass-p",
            captcha="1234",
        )
        self.assertNotIn("user-a", excerpt)
        self.assertNotIn("pass-p", excerpt)
        self.assertNotIn("1234", excerpt)
        self.assertNotIn("secret-token", excerpt)
        self.assertNotIn("abcdef", excerpt)
        self.assertIn("驗證碼錯誤", excerpt)

    def test_submit_requests_login_classifies_captcha_failed_response_metadata(self):
        challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
        )
        session = ClassifiedSession(LoginStatus(SiteState.LOGIN_REQUIRED, message="login page"))

        def request(method, url, **kwargs):
            response = make_json_response({"ret": {"msg": "驗證碼錯誤"}})
            response.cookies.set("TMS_TEST", "cookie-value", domain="tms.vghks.gov.tw", path="/")
            return response

        session.http.request = request
        result = session.submit_requests_login(
            account="a",
            password="p",
            captcha="0000",
            challenge=challenge,
            save=False,
            transient_retries=0,
            transient_delay_seconds=0,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.status, "captcha_failed")
        self.assertEqual(result.failure_message, "驗證碼錯誤")
        self.assertEqual(result.login_state_after_post, "login_required")
        self.assertEqual(result.set_cookie_names, ["TMS_TEST"])
        self.assertEqual(result.response_text_excerpt, '{"ret": {"msg": "驗證碼錯誤"}}')

    def test_submit_requests_login_classifies_credential_and_generic_failures(self):
        challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
        )
        cases = [
            ({"msg": "帳號或密碼錯誤"}, "credential_failed"),
            ({"msg": "系統拒絕登入"}, "login_failed"),
        ]
        for payload, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                session = ClassifiedSession(LoginStatus(SiteState.LOGIN_REQUIRED, message="login page"))
                session.http.request = lambda method, url, **kwargs: make_json_response(payload)
                result = session.submit_requests_login(
                    account="a",
                    password="p",
                    captcha="0000",
                    challenge=challenge,
                    save=False,
                    transient_retries=0,
                    transient_delay_seconds=0,
                )
                self.assertEqual(result.status, expected_status)

    def test_submit_requests_login_reports_unexpected_logged_in_without_saving_when_probe_style(self):
        challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
        )
        session = ClassifiedSession(LoginStatus(SiteState.LOGGED_IN, message="course page detected"))
        session.http.request = lambda method, url, **kwargs: make_json_response({"msg": "ok"})
        with tempfile.TemporaryDirectory() as tmp:
            result = session.submit_requests_login(
                account="a",
                password="p",
                captcha="0000",
                challenge=challenge,
                save=False,
                session_dir=tmp,
                transient_retries=0,
                transient_delay_seconds=0,
            )
            self.assertFalse((Path(tmp) / "requests_cookies.json").exists())
        self.assertTrue(result.success)
        self.assertEqual(result.status, "logged_in")
        self.assertIsNone(result.requests_cookies_path)

    def test_submit_requests_login_handles_multi_login_keep_and_saves_session(self):
        challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
        )
        session = ClassifiedSession(LoginStatus(SiteState.LOGIN_REQUIRED, message="login page"))
        calls = []

        def request(method, url, **kwargs):
            data = dict(kwargs.get("data") or {})
            calls.append((method, url, sorted(data), data.get("act")))
            if method == "GET" and "checkMultiLogin" in url:
                return make_response(200, MULTI_LOGIN_MODAL_HTML, url=url)
            if method == "POST" and len([call for call in calls if call[0] == "POST"]) == 1:
                return make_json_response({"ret": {"status": "true", "action": {"customJs": MULTI_LOGIN_CUSTOM_JS}}})
            session.login_status = LoginStatus(SiteState.LOGGED_IN, message="course page detected")
            response = make_json_response({"ret": {"status": "true"}})
            response.cookies.set("PHPSESSID", "session-value", domain="tms.vghks.gov.tw", path="/")
            return response

        session.http.request = request
        with tempfile.TemporaryDirectory() as tmp:
            result = session.submit_requests_login(
                account="a",
                password="p",
                captcha="1234",
                challenge=challenge,
                session_dir=tmp,
                transient_retries=0,
                transient_delay_seconds=0,
            )

            self.assertTrue((Path(tmp) / "requests_cookies.json").exists())
            self.assertTrue((Path(tmp) / "playwright_storage_state.json").exists())

        self.assertTrue(result.success)
        self.assertEqual(result.status, "logged_in")
        self.assertTrue(result.handled_multi_login)
        self.assertEqual(result.multi_login_action, "keep")
        self.assertEqual(result.multi_login_status, "ok")
        self.assertEqual(result.multi_login_response_status_code, 200)
        self.assertEqual([call[0] for call in calls], ["POST", "GET", "POST"])
        self.assertEqual(calls[2][2], ["_fmSubmit", "account", "act", "anticsrf", "captcha", "formId", "formVer", "next", "password"])
        self.assertEqual(calls[2][3], "keep")

    def test_submit_requests_login_reports_multi_login_failed_without_keep_button(self):
        challenge = RequestsLoginChallenge(
            login_url="https://tms.vghks.gov.tw/index/login",
            action_url="https://tms.vghks.gov.tw/index/login",
            hidden_fields={"anticsrf": "token"},
        )
        session = ClassifiedSession(LoginStatus(SiteState.LOGIN_REQUIRED, message="login page"))

        def request(method, url, **kwargs):
            if method == "GET" and "checkMultiLogin" in url:
                return make_response(
                    200,
                    '<form id="categoryForm" action="/index/"><input type="hidden" name="anticsrf" value="modal-token"></form>',
                    url=url,
                )
            return make_json_response({"ret": {"status": "true", "action": {"customJs": MULTI_LOGIN_CUSTOM_JS}}})

        session.http.request = request
        with tempfile.TemporaryDirectory() as tmp:
            result = session.submit_requests_login(
                account="a",
                password="p",
                captcha="1234",
                challenge=challenge,
                session_dir=tmp,
                transient_retries=0,
                transient_delay_seconds=0,
            )

            self.assertFalse((Path(tmp) / "requests_cookies.json").exists())

        self.assertFalse(result.success)
        self.assertEqual(result.status, "multi_login_failed")
        self.assertTrue(result.handled_multi_login)
        self.assertEqual(result.multi_login_action, "keep")
        self.assertEqual(result.multi_login_status, "keep_login_missing")

    def test_cli_parser_accepts_grouped_auth_commands(self):
        parser = build_parser()
        account_login_args = parser.parse_args(
            [
                "auth",
                "login",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account-alpha",
                "--verify-courses",
            ]
        )
        self.assertEqual(account_login_args.command, "login")
        self.assertEqual(account_login_args.accounts, ".tms_accounts.toml")
        self.assertEqual(account_login_args.label, "account-alpha")
        self.assertTrue(account_login_args.verify_courses)
        self.assertFalse(hasattr(account_login_args, "captcha_mode"))
        login_args = parser.parse_args(
            ["auth", "requests-login", "--accounts", ".tms_accounts.toml", "--label", "account1"]
        )
        self.assertEqual(login_args.login_requests_command, "login")
        self.assertEqual(login_args.accounts, ".tms_accounts.toml")
        self.assertEqual(login_args.label, "account1")
        prepare_args = parser.parse_args(["auth", "requests-prepare", "--show-captcha"])
        self.assertEqual(prepare_args.login_requests_command, "prepare")
        submit_args = parser.parse_args(["auth", "requests-submit", "--account", "a", "--password", "p", "--captcha", "0000"])
        self.assertEqual(submit_args.login_requests_command, "submit")
        auto_args = parser.parse_args(["auth", "requests-auto", "--accounts", ".tms_accounts.toml", "--label", "account1"])
        self.assertEqual(auto_args.login_requests_command, "auto")
        probe_args = parser.parse_args(["auth", "requests-probe-wrong-captcha", "--account", "a", "--password", "p"])
        self.assertEqual(probe_args.login_requests_command, "probe-wrong-captcha")
        batch_args = parser.parse_args(["auth", "requests-batch", "--accounts", ".tms_accounts.toml"])
        self.assertEqual(batch_args.login_requests_command, "batch")

    def test_cli_parser_rejects_user_facing_captcha_mode_flags(self):
        parser = build_parser()
        cases = [
            ["auth", "login", "--captcha-mode", "paddleocr-sdk"],
            ["auth", "diagnostics", "--captcha-mode", "paddleocr-sdk"],
            ["auth", "error-probes", "--captcha-mode", "paddleocr-sdk"],
            ["auth", "requests-login", "--captcha-mode", "paddleocr-sdk"],
            ["sign-in", "--captcha-mode", "paddleocr-sdk"],
            ["go", "--captcha-mode", "paddleocr-sdk"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    parser.parse_args(argv)

    def test_cli_parser_accepts_accounts_for_list_inspect_and_run(self):
        parser = build_parser()
        list_args = parser.parse_args(["pending", "--accounts", ".tms_accounts.toml", "--label", "account1"])
        self.assertEqual(list_args.accounts, ".tms_accounts.toml")
        self.assertEqual(list_args.label, "account1")

        inspect_args = parser.parse_args(["course", "course-1", "--accounts", ".tms_accounts.toml"])
        self.assertEqual(inspect_args.accounts, ".tms_accounts.toml")

        run_args = parser.parse_args(["go", "--accounts", ".tms_accounts.toml"])
        self.assertEqual(run_args.accounts, ".tms_accounts.toml")
        self.assertFalse(hasattr(run_args, "json"))

    def test_cli_requests_login_uses_every_account_from_toml_by_default(self):
        parser = build_parser()
        session = FakeCliRequestsSession()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [[accounts]]
                label = "one"
                account = "a1"
                password = "p1"

                [[accounts]]
                label = "two"
                account = "a2"
                password = "p2"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(["auth", "requests-login", "--accounts", str(accounts_path)])
            batch_result = BatchLoginResult(
                success=True,
                results=[
                    BatchAccountLoginResult(label="one", success=True, status="logged_in"),
                    BatchAccountLoginResult(label="two", success=True, status="logged_in"),
                ],
            )
            with patch("tms_vghks.cli.run_batch_requests_login", return_value=batch_result) as run_batch, redirect_stdout(StringIO()):
                code = cli_module.handle_requests_login(session, args)
            config = run_batch.call_args.args[0]

        self.assertEqual(code, 0)
        self.assertEqual([account.label for account in config.accounts], ["one", "two"])
        self.assertIsNone(run_batch.call_args.kwargs["concurrency"])

    def test_cli_requests_prepare_agent_show_captcha_keeps_stdout_json_only(self):
        parser = build_parser()
        session = FakeCliRequestsSession()
        args = parser.parse_args(["auth", "requests-prepare", "--show-captcha", "--agent"])
        stdout = StringIO()
        stderr = StringIO()

        with patch("tms_vghks.cli_impl._stderr_input", side_effect=AssertionError("prompted unexpectedly")), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            code = cli_module.handle_requests_login(session, args)
        payload = json.loads(stdout.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["captcha_path"], "REDACTED")
        self.assertIn("captcha image: .tms_session/captcha.jpg", stderr.getvalue())

    def test_cli_requests_prepare_agent_wait_returns_json_invalid_request(self):
        session = FakeCliRequestsSession()
        stdout = StringIO()
        stderr = StringIO()

        with patch("tms_vghks.cli_impl.TmsSession", lambda: session), patch("sys.stdin.isatty", return_value=False), patch(
            "tms_vghks.cli_impl._stderr_input",
            side_effect=AssertionError("prompted unexpectedly"),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_module.main(["auth", "requests-prepare", "--show-captcha", "--wait", "--agent"])
        payload = json.loads(stdout.getvalue())

        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "invalid_request")
        self.assertIn("--wait requires --human with TTY stdin", payload["message"])
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_requests_prepare_human_wait_requires_tty_stdin(self):
        session = FakeCliRequestsSession()
        stdout = StringIO()
        stderr = StringIO()

        with patch("tms_vghks.cli_impl.TmsSession", lambda: session), patch("sys.stdin.isatty", return_value=False), patch(
            "tms_vghks.cli_impl._stderr_input",
            side_effect=AssertionError("prompted unexpectedly"),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_module.main(["auth", "requests-prepare", "--show-captcha", "--wait", "--human"])

        self.assertEqual(code, 2)
        self.assertIn("--wait requires --human with TTY stdin", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_cli_requests_prepare_human_wait_prompts_when_tty(self):
        parser = build_parser()
        session = FakeCliRequestsSession()
        args = parser.parse_args(["auth", "requests-prepare", "--show-captcha", "--wait", "--human"])
        stdout = StringIO()
        stderr = StringIO()

        with patch("sys.stdin.isatty", return_value=True), patch(
            "tms_vghks.cli_impl._stderr_input",
            return_value="",
        ) as prompt, redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_module.handle_requests_login(session, args)

        self.assertEqual(code, 0)
        self.assertIn("captcha.jpg", stderr.getvalue())
        prompt.assert_called_once()

    def test_cli_requests_login_prompts_when_default_accounts_config_is_missing(self):
        parser = build_parser()
        session = FakeCliRequestsSession()
        args = parser.parse_args(["sign-in", "--human"])
        batch_result = BatchLoginResult(
            success=True,
            results=[BatchAccountLoginResult(label="account", success=True, status="logged_in", session_dir=".tms_session")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            missing_default = str(Path(tmp) / ".tms_accounts.toml")
            stdout = StringIO()
            stderr = StringIO()
            with patch("tms_vghks.cli_impl.DEFAULT_ACCOUNTS_PATH", missing_default), patch(
                "sys.stdin.isatty",
                return_value=True,
            ), patch("tms_vghks.cli_impl._stderr_input", return_value="employee-id"), patch(
                "tms_vghks.cli_impl.getpass.getpass",
                return_value="password-secret",
            ), patch("tms_vghks.cli.run_batch_requests_login", return_value=batch_result) as run_batch, redirect_stdout(
                stdout
            ), redirect_stderr(
                stderr
            ):
                code = cli_module.handle_requests_login(session, args)
            config = run_batch.call_args.args[0]

        self.assertEqual(code, 0)
        self.assertEqual(config.accounts[0].label, "account")
        self.assertEqual(config.accounts[0].account, "employee-id")
        self.assertEqual(config.accounts[0].password, "password-secret")
        self.assertIn("was not found", stderr.getvalue())
        self.assertNotIn("password-secret", stdout.getvalue())
        self.assertNotIn("password-secret", stderr.getvalue())

    def test_cli_requests_login_agent_missing_credentials_returns_json_error_without_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_default = str(Path(tmp) / ".tms_accounts.toml")
            session = FakeCliRequestsSession()
            stdout = StringIO()
            stderr = StringIO()
            with patch("tms_vghks.cli_impl.DEFAULT_ACCOUNTS_PATH", missing_default), patch(
                "tms_vghks.cli_impl.TmsSession",
                lambda: session,
            ), patch(
                "tms_vghks.cli_impl._stderr_input",
                side_effect=AssertionError("prompted unexpectedly"),
            ), patch("tms_vghks.cli_impl.getpass.getpass", side_effect=AssertionError("prompted unexpectedly")), redirect_stdout(
                stdout
            ), redirect_stderr(
                stderr
            ):
                code = cli_module.main(["sign-in", "--agent"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(code, 10)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "login_required")
        self.assertIn("pass --accounts", payload["message"])
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_requests_login_explicit_bad_accounts_config_fails_fast(self):
        parser = build_parser()
        session = FakeCliRequestsSession()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "empty.toml"
            accounts_path.write_text("", encoding="utf-8")
            args = parser.parse_args(["auth", "requests-login", "--accounts", str(accounts_path)])

            with patch("sys.stdin.isatty", return_value=True), patch(
                "tms_vghks.cli_impl._stderr_input",
                side_effect=AssertionError("prompted unexpectedly"),
            ), patch("tms_vghks.cli_impl.getpass.getpass", side_effect=AssertionError("prompted unexpectedly")):
                with self.assertRaisesRegex(ValueError, "accounts config requires at least one"):
                    cli_module.handle_requests_login(session, args)

    def test_cli_accounts_playwright_login_uses_toml_credentials_and_verify_counts(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [ocr]
                paddleocr_api_token = "token-123"

                [[accounts]]
                label = "account-alpha"
                account = "a-secret"
                password = "p-secret"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(
                [
                    "auth",
                    "login",
                    "--accounts",
                    str(accounts_path),
                    "--label",
                    "account-alpha",
                    "--verify-courses",
                ]
            )
            with patch("tms_vghks.cli.TmsSession", FakePlaywrightLoginSession), redirect_stdout(StringIO()) as output:
                code = cli_module.handle_accounts_playwright_login(args)
            payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["pending_count"], 1)
        self.assertEqual(payload["results"][0]["completed_count"], 2)
        login_call = FakePlaywrightLoginSession.instances[0].login_calls[0]
        self.assertEqual(login_call["account"], "a-secret")
        self.assertEqual(login_call["password"], "p-secret")
        self.assertEqual(login_call["captcha_mode"], "paddleocr-sdk")
        self.assertEqual(login_call["ocr_config"].api.token, "token-123")

    def test_login_diagnostics_writes_redacted_payload(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [ocr]
                paddleocr_api_token = "token-123"

                [[accounts]]
                label = "diag"
                account = "a-secret"
                password = "p-secret"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(["auth", "diagnostics", "--accounts", str(accounts_path), "--label", "diag"])
            with patch("tms_vghks.cli.TmsSession", FakeDiagnosticSession), patch(
                "tms_vghks.cli._run_requests_login_diagnostic",
                return_value={"success": False, "status": "login_failed", "payload_keys": ["account", "password", "captcha"]},
            ), redirect_stdout(StringIO()) as output:
                code = cli_module.handle_login_diagnostics(args)
            payload = json.loads(output.getvalue())
            diagnostics_path = Path(payload["results"][0]["diagnostics_path"])
            diagnostics_text = diagnostics_path.read_text(encoding="utf-8")
            login_call = FakePlaywrightLoginSession.instances[0].login_calls[0]

        self.assertEqual(code, 0)
        self.assertTrue(diagnostics_path.exists())
        self.assertNotIn("a-secret", diagnostics_text)
        self.assertNotIn("p-secret", diagnostics_text)
        self.assertIn("payload_keys", diagnostics_text)
        self.assertEqual(login_call["ocr_config"].api.token, "token-123")

    def test_cli_parser_accepts_login_error_probes(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "auth",
                "error-probes",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account-alpha",
                "--backend",
                "both",
                "--scenarios",
                "wrong-captcha,wrong-credentials",
            ]
        )

        self.assertEqual(args.command, "login-error-probes")
        self.assertEqual(args.backend, "both")
        self.assertEqual(probe_module.parse_login_error_probe_scenarios(args.scenarios), ["wrong-captcha", "wrong-credentials"])

    def test_requests_error_probe_wrong_credentials_uses_fake_account_and_no_save(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            args = parser.parse_args(
                [
                    "auth",
                    "error-probes",
                    "--accounts",
                    "unused.toml",
                    "--fake-account",
                    "fake-account",
                    "--fake-password",
                    "fake-password",
                ]
            )
            config = cli_module.AccountsLoginConfig(
                ocr=OcrConfig(api=PaddleOcrApiConfig(token="token-123")),
                accounts=[
                    cli_module.AccountLoginConfig(
                        label="real",
                        account="real-account",
                        password="real-password",
                        session_dir=str(Path(tmp) / "real"),
                    )
                ]
            )
            options = probe_module.LoginErrorProbeOptions(
                fake_account=args.fake_account,
                fake_password=args.fake_password,
            )
            ocr_tokens = []
            observation = probe_module.run_requests_error_probe(
                config,
                config.accounts[0],
                options,
                "wrong-credentials",
                session_factory=FakeErrorProbeRequestsSession,
                ocr_func=lambda path, config, mode: (
                    ocr_tokens.append(config.api.token) or OcrResult(text="1234", confidence=0.99, source="paddleocr-api")
                ),
            )
            submitted = FakeErrorProbeRequestsSession.instances[0].submitted[0]

        self.assertEqual(submitted["account"], "fake-account")
        self.assertEqual(submitted["password"], "fake-password")
        self.assertFalse(submitted["save"])
        self.assertEqual(observation["classified_status"], "credential_failed")
        self.assertEqual(ocr_tokens, ["token-123"])

    def test_requests_error_probe_wrong_credentials_retries_after_ocr_captcha_failure(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            args = parser.parse_args(
                [
                    "auth",
                    "error-probes",
                    "--accounts",
                    "unused.toml",
                    "--fake-account",
                    "fake-account",
                    "--fake-password",
                    "fake-password",
                ]
            )
            config = cli_module.AccountsLoginConfig(
                accounts=[
                    cli_module.AccountLoginConfig(
                        label="real",
                        account="real-account",
                        password="real-password",
                        session_dir=str(Path(tmp) / "real"),
                    )
                ]
            )
            FakeErrorProbeRequestsSession.statuses = ["captcha_failed", "credential_failed"]
            ocr_texts = iter(["bad-captcha", "good-captcha"])
            options = probe_module.LoginErrorProbeOptions(
                fake_account=args.fake_account,
                fake_password=args.fake_password,
            )
            observation = probe_module.run_requests_error_probe(
                config,
                config.accounts[0],
                options,
                "wrong-credentials",
                session_factory=FakeErrorProbeRequestsSession,
                ocr_func=lambda path, config, mode: OcrResult(text=next(ocr_texts), confidence=0.99, source="paddleocr-sdk"),
            )
            session = FakeErrorProbeRequestsSession.instances[0]

        self.assertEqual(observation["classified_status"], "credential_failed")
        self.assertEqual([row["captcha"] for row in session.submitted], ["bad-captcha", "good-captcha"])
        self.assertTrue(session.prepared[0][0].endswith("captcha.jpg"))
        self.assertTrue(session.prepared[1][0].endswith("captcha_retry.jpg"))

    def test_login_error_observation_jsonl_redacts_known_values_and_ajax_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            observation = probe_module.login_error_observation(
                backend="requests",
                scenario="wrong-captcha",
                classified_status="captcha_failed",
                failure_message="real-account fake-password INVALID-CAPTCHA",
                final_url="https://tms.vghks.gov.tw/ajax/path?ajaxAuth=secret-token",
                response_text_excerpt="real-account fake-password INVALID-CAPTCHA ajaxAuth=secret-token",
                captcha_image_path=str(Path(tmp) / "captcha.jpg"),
            )
            sanitized = probe_module.sanitize_probe_observation(
                observation,
                secrets=("real-account", "fake-password", "INVALID-CAPTCHA"),
            )
            path = probe_module.append_login_error_observation(tmp, sanitized)
            text = Path(path).read_text(encoding="utf-8")

        self.assertIn("captcha_failed", text)
        self.assertNotIn("real-account", text)
        self.assertNotIn("fake-password", text)
        self.assertNotIn("INVALID-CAPTCHA", text)
        self.assertNotIn("secret-token", text)
        self.assertIn("ajaxAuth=REDACTED", text)

    def test_login_error_probe_handler_runs_four_default_observations(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [ocr]
                paddleocr_api_token = "token-123"

                [[accounts]]
                label = "probe"
                account = "real-account"
                password = "real-password"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(["auth", "error-probes", "--accounts", str(accounts_path)])
            config = cli_module._accounts_config_from_args(args)
            observation_file = Path(config.accounts[0].session_dir) / "login_error_observations.jsonl"
            observation_file.unlink(missing_ok=True)
            observed_tokens = []

            def fake_observation(config, account, options, scenario, session_factory=probe_module.TmsSession, ocr_func=probe_module.recognize_captcha):
                observed_tokens.append(config.ocr.api.token)
                return probe_module.login_error_observation(
                    backend="requests",
                    scenario=scenario,
                    classified_status="captcha_failed" if scenario == "wrong-captcha" else "credential_failed",
                    failure_message="real-account real-password INVALID-CAPTCHA",
                )

            def fake_playwright_observation(config, account, options, scenario, session_factory=probe_module.TmsSession, ocr_func=probe_module.recognize_captcha):
                observed_tokens.append(config.ocr.api.token)
                return probe_module.login_error_observation(
                    backend="playwright",
                    scenario=scenario,
                    classified_status="captcha_failed" if scenario == "wrong-captcha" else "credential_failed",
                    failure_message="real-account real-password INVALID-CAPTCHA",
                )

            with patch("tms_vghks.login_error_probes.run_requests_error_probe", side_effect=fake_observation), patch(
                "tms_vghks.login_error_probes.run_playwright_error_probe",
                side_effect=fake_playwright_observation,
            ), redirect_stdout(StringIO()) as output:
                code = cli_module.handle_login_error_probes(args)
            payload = json.loads(output.getvalue())
            observation_path = Path(payload["results"][0]["observations"][0]["observation_path"])
            lines = observation_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(code, 0)
        self.assertEqual(len(payload["results"][0]["observations"]), 4)
        self.assertEqual(len(lines), 4)
        self.assertNotIn("real-account", "\n".join(lines))
        self.assertNotIn("real-password", "\n".join(lines))
        self.assertEqual(observed_tokens, ["token-123"] * 4)

    def test_diagnostic_post_data_keys_summarizes_form_and_json_payloads(self):
        self.assertEqual(
            cli_module._post_data_keys("_fmSubmit=yes&account=a&password=p&captcha=1234"),
            ["_fmSubmit", "account", "captcha", "password"],
        )
        self.assertEqual(
            cli_module._post_data_keys('{"memory":1,"location":"https://example.test","referrer":""}'),
            ["location", "memory", "referrer"],
        )

    def test_diagnostic_string_redacts_embedded_ajax_auth(self):
        value = "$('#modal').data('url', '/ajax/path?next=x&ajaxAuth=secret-token&id=1')"
        redacted = cli_module._redact_diagnostic_string(value)

        self.assertIn("ajaxAuth=REDACTED", redacted)
        self.assertNotIn("secret-token", redacted)

    def test_cli_requests_auto_uses_batch_core_for_direct_credentials(self):
        session = FakeCliRequestsSession()
        args = SimpleNamespace(
            login_requests_command="auto",
            accounts=None,
            label=None,
            account="a",
            password="p",
            session_dir=".tms_session",
            show_captcha=False,
            transient_retries=3,
            transient_delay_seconds=2.0,
            cli_mode="agent",
        )
        batch_result = BatchLoginResult(
            success=True,
            results=[BatchAccountLoginResult(label="account", success=True, status="logged_in", session_dir=".tms_session")],
        )
        with patch("tms_vghks.cli.run_batch_requests_login", return_value=batch_result) as run_batch, redirect_stdout(StringIO()):
            code = cli_module.handle_requests_login(session, args)
        config = run_batch.call_args.args[0]

        self.assertEqual(code, 0)
        self.assertEqual(config.accounts[0].label, "account")
        self.assertEqual(config.accounts[0].account, "a")
        self.assertEqual(config.accounts[0].password, "p")
        self.assertEqual(config.accounts[0].session_dir, ".tms_session")
        self.assertEqual(run_batch.call_args.kwargs["concurrency"], 1)

    def test_cli_requests_auto_selects_one_account_from_toml(self):
        session = FakeCliRequestsSession()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [[accounts]]
                label = "one"
                account = "a1"
                password = "p1"

                [[accounts]]
                label = "two"
                account = "a2"
                password = "p2"
                """,
                encoding="utf-8",
            )
            args = SimpleNamespace(
                login_requests_command="auto",
                accounts=str(accounts_path),
                label="two",
                account="",
                password="",
                session_dir=".tms_session",
                show_captcha=False,
                transient_retries=3,
                transient_delay_seconds=2.0,
                cli_mode="agent",
            )
            batch_result = BatchLoginResult(
                success=True,
                results=[
                    BatchAccountLoginResult(
                        label="two",
                        success=True,
                        status="logged_in",
                        session_dir=".tms_session/accounts/two",
                    )
                ],
            )
            with patch("tms_vghks.cli.run_batch_requests_login", return_value=batch_result) as run_batch, redirect_stdout(StringIO()):
                code = cli_module.handle_requests_login(session, args)
            config = run_batch.call_args.args[0]

        self.assertEqual(code, 0)
        self.assertEqual(config.accounts[0].label, "two")
        self.assertEqual(config.accounts[0].account, "a2")
        self.assertEqual(config.accounts[0].password, "p2")
        self.assertEqual(Path(config.accounts[0].session_dir).parts[-3:], (".tms_session", "accounts", "two"))

    def test_cli_accounts_list_uses_saved_session_for_each_account(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [[accounts]]
                label = "one"
                account = "a1"
                password = "p1"

                [[accounts]]
                label = "two"
                account = "a2"
                password = "p2"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(["pending", "--accounts", str(accounts_path)])
            with patch("tms_vghks.cli.TmsSession", FakeAccountCommandSession), redirect_stdout(StringIO()) as output:
                code = cli_module.handle_accounts_list(args)
            payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual([row["label"] for row in payload["results"]], ["one", "two"])
        self.assertEqual([instance.auth_options.login_method for instance in FakeAccountCommandSession.instances], [LoginMethod.SAVED, LoginMethod.SAVED])
        self.assertEqual(
            [Path(instance.auth_options.session_dir).parts[-3:] for instance in FakeAccountCommandSession.instances],
            [(".tms_session", "accounts", "one"), (".tms_session", "accounts", "two")],
        )

    def test_cli_accounts_list_auto_relogs_with_requests_when_saved_expired(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [[accounts]]
                label = "one"
                account = "a1"
                password = "p1"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(["pending", "--accounts", str(accounts_path)])
            batch_result = BatchLoginResult(
                success=True,
                results=[
                    BatchAccountLoginResult(
                        label="one",
                        success=True,
                        status="logged_in",
                        session_dir=".tms_session/accounts/one",
                    )
                ],
            )
            with patch("tms_vghks.cli.TmsSession", FakeExpiredThenSavedAccountSession), patch(
                "tms_vghks.cli.run_batch_requests_login",
                return_value=batch_result,
            ) as run_batch, redirect_stdout(StringIO()) as output:
                code = cli_module.handle_accounts_list(args)
            payload = json.loads(output.getvalue())
            instance = FakeExpiredThenSavedAccountSession.instances[0]
            login_config = run_batch.call_args.args[0]

        self.assertEqual(code, 0)
        self.assertTrue(payload["success"])
        self.assertEqual(instance.ensure_calls, 2)
        self.assertEqual(login_config.accounts[0].account, "a1")
        self.assertEqual(run_batch.call_args.kwargs["concurrency"], 1)

    def test_cli_accounts_list_saved_method_does_not_auto_relogin(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [[accounts]]
                label = "one"
                account = "a1"
                password = "p1"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(
                ["pending", "--accounts", str(accounts_path), "--login-method", "saved"]
            )
            with patch("tms_vghks.cli.TmsSession", FakeExpiredThenSavedAccountSession), patch(
                "tms_vghks.cli.run_batch_requests_login",
            ) as run_batch, redirect_stdout(StringIO()) as output:
                code = cli_module.handle_accounts_list(args)
            payload = json.loads(output.getvalue())

        self.assertEqual(code, 12)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["results"][0]["status"], "login_required")
        run_batch.assert_not_called()

    def test_cli_accounts_list_playwright_method_uses_toml_credentials_and_ocr(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [ocr]
                paddleocr_api_token = "token-123"

                [[accounts]]
                label = "one"
                account = "a-secret"
                password = "p-secret"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(
                ["pending", "--accounts", str(accounts_path), "--login-method", "playwright"]
            )
            with patch("tms_vghks.cli.TmsSession", FakeAccountPlaywrightMethodSession), redirect_stdout(StringIO()) as output:
                code = cli_module.handle_accounts_list(args)
            payload = json.loads(output.getvalue())
            instance = FakeAccountPlaywrightMethodSession.instances[0]
            login_call = instance.login_calls[0]

        self.assertEqual(code, 0)
        self.assertTrue(payload["success"])
        self.assertEqual(login_call["account"], "a-secret")
        self.assertEqual(login_call["password"], "p-secret")
        self.assertEqual(login_call["captcha_mode"], "paddleocr-sdk")
        self.assertEqual(login_call["ocr_config"].api.token, "token-123")

    def test_cli_accounts_list_playwright_uses_browser_saved_session_check(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [[accounts]]
                label = "one"
                account = "a1"
                password = "p1"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(
                ["pending", "--accounts", str(accounts_path), "--backend", "playwright", "--headless"]
            )
            with patch("tms_vghks.cli.TmsSession", FakeAccountCommandSession), redirect_stdout(StringIO()) as output:
                code = cli_module.handle_accounts_list(args)
            payload = json.loads(output.getvalue())
            instance = FakeAccountCommandSession.instances[0]

        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["result"][0]["title"], f"pending-playwright:{instance.auth_options.session_dir}")
        self.assertTrue(instance.saved_browser_headless)
        self.assertEqual(Path(instance.saved_browser_session_dir).parts[-3:], (".tms_session", "accounts", "one"))

    def test_cli_accounts_run_uses_saved_session_and_filters_label(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.toml"
            accounts_path.write_text(
                """
                [[accounts]]
                label = "one"
                account = "a1"
                password = "p1"

                [[accounts]]
                label = "two"
                account = "a2"
                password = "p2"
                """,
                encoding="utf-8",
            )
            args = parser.parse_args(["go", "--accounts", str(accounts_path), "--label", "two"])
            with patch("tms_vghks.cli.TmsSession", FakeAccountCommandSession), patch(
                "tms_vghks.cli.TmsRunner",
                FakeRunner,
            ), redirect_stdout(StringIO()) as output:
                code = cli_module.handle_accounts_run(args)
            payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual([row["label"] for row in payload["results"]], ["two"])
        self.assertEqual(len(FakeRunner.instances), 1)
        self.assertEqual(FakeRunner.instances[0].options.auth_options.login_method, LoginMethod.SAVED)
        self.assertEqual(Path(FakeRunner.instances[0].options.auth_options.session_dir).parts[-3:], (".tms_session", "accounts", "two"))

    def test_cli_wrong_captcha_probe_writes_redacted_metadata_without_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = FakeCliRequestsSession()
            session.submit_result = RequestsLoginResult(
                success=False,
                status="captcha_failed",
                message="驗證碼錯誤",
                response_json={"ret": {"msg": "驗證碼錯誤"}},
                response_text="secret response",
                response_text_excerpt="驗證碼錯誤",
                set_cookie_names=["PHPSESSID"],
            )
            args = SimpleNamespace(
                login_requests_command="probe-wrong-captcha",
                account="account-secret",
                password="password-secret",
                captcha="0000",
                session_dir=tmp,
                transient_retries=3,
                transient_delay_seconds=2.0,
                cli_mode="agent",
            )
            with redirect_stdout(StringIO()):
                code = cli_module.handle_requests_login(session, args)
            probe_path = Path(tmp) / "wrong_captcha_probe.json"
            saved_payload = json.loads(probe_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertTrue(probe_path.name, "wrong_captcha_probe.json")
        self.assertEqual(session.submitted[0]["captcha"], "0000")
        self.assertFalse(session.submitted[0]["save"])
        text = json.dumps(saved_payload, ensure_ascii=False)
        self.assertNotIn("account-secret", text)
        self.assertNotIn("password-secret", text)
        self.assertNotIn('"captcha": "0000"', text)
        self.assertIn("驗證碼錯誤", text)

    def test_cli_jsonable_keeps_response_excerpt_but_redacts_raw_response_fields(self):
        payload = to_jsonable(
            RequestsLoginResult(
                success=False,
                status="captcha_failed",
                response_json={"token": "secret"},
                response_text="raw secret",
                response_text_excerpt="驗證碼錯誤",
            )
        )
        self.assertEqual(payload["response_json"], "REDACTED")
        self.assertEqual(payload["response_text"], "REDACTED")
        self.assertEqual(payload["response_text_excerpt"], "驗證碼錯誤")

    def test_load_session_bundle_reports_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(Exception, "session bundle not found"):
                TmsSession().load_session_bundle(tmp)


if __name__ == "__main__":
    unittest.main()
