import sys
import unittest
from pathlib import Path

from requests import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.models import SiteState
from tms_vghks.session import TmsSession, TransientTmsError


def make_response(status_code: int, body: str, url: str = "https://tms.vghks.gov.tw/course/notCompleteList") -> Response:
    response = Response()
    response.status_code = status_code
    response.url = url
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    response.headers["content-type"] = "text/html; charset=UTF-8"
    return response


class FakeBodyLocator:
    def __init__(self, page):
        self.page = page

    def inner_text(self, timeout=0):
        return self.page.current_text


class FakeButtonLocator:
    def __init__(self, page):
        self.page = page
        self.first = self

    def is_visible(self, timeout=0):
        return True

    def click(self):
        self.page.clicks += 1


class FakeTransientPage:
    def __init__(self, texts):
        self.texts = texts
        self.index = 0
        self.url = "https://tms.vghks.gov.tw/course/123"
        self.clicks = 0
        self.goto_calls = 0
        self.reload_calls = 0

    @property
    def current_text(self):
        return self.texts[min(self.index, len(self.texts) - 1)]

    def locator(self, selector):
        return FakeBodyLocator(self)

    def get_by_role(self, role, name=None):
        return FakeButtonLocator(self)

    def get_by_text(self, label, exact=False):
        return FakeButtonLocator(self)

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls += 1
        self.url = url
        if self.index < len(self.texts) - 1:
            self.index += 1

    def reload(self, wait_until=None, timeout=None):
        self.reload_calls += 1
        if self.index < len(self.texts) - 1:
            self.index += 1

    def wait_for_load_state(self, state, timeout=None):
        return None


class TransientRecoveryTests(unittest.TestCase):
    def test_recover_transient_page_refreshes_until_marker_disappears(self):
        page = FakeTransientPage(["儲存失敗 請檢查伺服器狀態", "待修課程 課程名稱 完成度"])
        session = TmsSession()
        recovered = session.recover_transient_page(
            page,
            "/course/123",
            retries=2,
            delay_seconds=0,
        )
        self.assertTrue(recovered)
        self.assertEqual(page.clicks, 1)
        self.assertEqual(page.goto_calls, 1)

    def test_recover_transient_page_raises_after_retries(self):
        page = FakeTransientPage(["儲存失敗", "儲存失敗"])
        session = TmsSession()
        with self.assertRaises(TransientTmsError):
            session.recover_transient_page(page, "/course/123", retries=1, delay_seconds=0)

    def test_recover_transient_requests_retries_until_course_page(self):
        session = TmsSession()
        responses = [
            make_response(503, "temporary outage"),
            make_response(200, "待修課程 課程名稱 完成度"),
        ]
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url))
            return responses.pop(0)

        session.http.request = request

        status = session.recover_transient_requests(retries=1, delay_seconds=0)

        self.assertEqual(status.state, SiteState.LOGGED_IN)
        self.assertEqual(len(calls), 2)

    def test_recover_transient_requests_reports_persisted_transient(self):
        session = TmsSession()
        session.http.request = lambda method, url, **kwargs: make_response(503, "temporary outage")

        status = session.recover_transient_requests(retries=0, delay_seconds=0)

        self.assertEqual(status.state, SiteState.TRANSIENT_ERROR)
        self.assertIn("HTTP 503", status.message)


if __name__ == "__main__":
    unittest.main()
