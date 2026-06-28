import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.models import CourseDetail, CourseItem, ItemKind, ItemState
from tms_vghks.requests_watch_time import (
    find_check_pass_previous_url,
    parse_watch_time_endpoint,
    run_requests_watch_time,
)


COURSE_HTML = """
<ul id="activityTree">
  <li id="23977" class="xtree-node" data-id="23977">
    <span class="sn">2</span>
    <span class="fs-singleLineText">
      <a id="btn_read" href="#" class="__button_read_2"><span class="text">Read Item</span></a>
    </span>
    <span class="col-char7">閱讀達 40 分鐘</span>
    <span class="col-char4">41:03</span>
  </li>
</ul>
<script>
$(".__button_read_2").click(function(event) {
  fs.post('/ajax/sys.pages.course/checkPassPrevious/?userID=11078&itemID=19054&_lock=userID%2CitemID&ajaxAuth=secret', {}, function(o) {
    window.location.href = o.data.url;
  });
});
</script>
"""


MEDIA_HTML = r"""
<script>
new ReadLog({"recordUrl":"\/ajax\/sys.pages.media\/watchTime\/?logID=1218396&timing=pageload&_lock=logID%2Ctiming&ajaxAuth=secret","recordTime":60,"timing":"pageload","exitUrl":"\/course\/5416"});
</script>
"""


class FakeResponse:
    def __init__(self, url, status_code=200, payload=None, text=""):
        self.url = url
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeWatchTimeSession:
    base_url = "https://tms.vghks.gov.tw"

    def __init__(
        self,
        before_result="39:03",
        after_result="40:03",
        media_html=MEDIA_HTML,
        before_state=ItemState.IN_PROGRESS,
        after_state=ItemState.PASSED,
    ):
        self.before_result = before_result
        self.after_result = after_result
        self.before_state = before_state
        self.after_state = after_state
        self.media_html = media_html
        self.detail_calls = 0
        self.posted_urls = []

    def get_course_detail(self, url_or_id):
        self.detail_calls += 1
        result = self.before_result if self.detail_calls == 1 else self.after_result
        state = self.before_state if self.detail_calls == 1 else self.after_state
        return CourseDetail(
            title="Course",
            url="https://tms.vghks.gov.tw/course/5416",
            course_id="5416",
            items=[
                CourseItem(
                    title="Read Item",
                    order=2,
                    kind=ItemKind.READING,
                    state=state,
                    pass_condition="閱讀達 40 分鐘",
                    result=result,
                    metadata={"activity_tree_id": "23977", "activity_id": "23977"},
                )
            ],
        )

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        if "/media/" in path_or_url:
            return self.media_html
        return COURSE_HTML

    def _request_with_transient_retries(self, method, path_or_url, **kwargs):
        self.posted_urls.append(path_or_url)
        if "checkPassPrevious" in path_or_url:
            return FakeResponse(
                "https://tms.vghks.gov.tw/ajax/sys.pages.course/checkPassPrevious/",
                payload={"status": True, "data": {"url": "/media/12691"}},
            )
        return FakeResponse(
            "https://tms.vghks.gov.tw/ajax/sys.pages.media/watchTime/",
            payload={"status": True, "ret": {"status": True}},
        )


class RequestsWatchTimeTests(unittest.TestCase):
    def test_parse_readlog_watch_time_endpoint_appends_record_time(self):
        parsed = parse_watch_time_endpoint(MEDIA_HTML, "https://tms.vghks.gov.tw/media/12691", "https://tms.vghks.gov.tw")

        self.assertEqual(parsed.issues, [])
        self.assertIsNotNone(parsed.endpoint)
        assert parsed.endpoint is not None
        self.assertEqual(parsed.endpoint.log_id, "1218396")
        self.assertTrue(parsed.endpoint.has_ajax_auth)
        self.assertIn("/ajax/sys.pages.media/watchTime/", parsed.endpoint.post_url)
        self.assertIn("t=60", parsed.endpoint.post_url)

    def test_parse_readlog_missing_ajax_auth_reports_token_issue(self):
        html = r"""
        <script>
        new ReadLog({"recordUrl":"\/ajax\/sys.pages.media\/watchTime\/?logID=1&timing=pageload","recordTime":60});
        </script>
        """
        parsed = parse_watch_time_endpoint(html, "https://tms.vghks.gov.tw/media/1", "https://tms.vghks.gov.tw")

        self.assertIsNotNone(parsed.endpoint)
        self.assertIn("watch_time_ajax_auth_missing", parsed.issues)

    def test_find_check_pass_previous_url_from_activity_button(self):
        item = CourseItem(
            title="Read Item",
            kind=ItemKind.READING,
            metadata={"activity_tree_id": "23977"},
        )

        url = find_check_pass_previous_url(COURSE_HTML, item, "https://tms.vghks.gov.tw")

        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("/ajax/sys.pages.course/checkPassPrevious/", url)
        self.assertIn("ajaxAuth=secret", url)

    def test_requests_watch_time_success_when_detail_row_increases(self):
        session = FakeWatchTimeSession(before_result="39:03", after_result="40:03")
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/5416")
        item = course.items[0]
        session.detail_calls = 0

        result = run_requests_watch_time(session, course, item, wait_seconds=0, wait_func=lambda seconds: None)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_watch_time_verified")
        self.assertEqual(result.before_result, "39:03")
        self.assertEqual(result.after_result, "40:03")
        self.assertTrue(result.progress_increased)
        self.assertEqual(result.requests_reproduction_status, "requests_reproducible")
        self.assertTrue(any("checkPassPrevious" in url for url in session.posted_urls))
        self.assertTrue(any("watchTime" in url and "t=60" in url for url in session.posted_urls))
        self.assertIn("ajaxAuth=REDACTED", result.watch_time_url)

    def test_requests_watch_time_already_passed_does_not_post(self):
        session = FakeWatchTimeSession(
            before_result="41:03",
            after_result="41:03",
            before_state=ItemState.PASSED,
            after_state=ItemState.PASSED,
        )
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/5416")
        item = course.items[0]
        session.detail_calls = 0

        result = run_requests_watch_time(session, course, item, wait_seconds=0, wait_func=lambda seconds: None)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "already_passed")
        self.assertTrue(result.already_passed)
        self.assertEqual(result.requests_reproduction_status, "requests_not_needed")
        self.assertEqual(session.posted_urls, [])

    def test_requests_watch_time_force_posts_for_already_passed_diagnostic(self):
        session = FakeWatchTimeSession(
            before_result="41:03",
            after_result="42:03",
            before_state=ItemState.PASSED,
            after_state=ItemState.PASSED,
        )
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/5416")
        item = course.items[0]
        session.detail_calls = 0

        result = run_requests_watch_time(
            session,
            course,
            item,
            wait_seconds=0,
            wait_func=lambda seconds: None,
            force_watch_time=True,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_watch_time_verified")
        self.assertTrue(result.already_passed)
        self.assertIn("forced_watch_time_on_already_passed_item", result.issues)
        self.assertTrue(any("watchTime" in url for url in session.posted_urls))

    def test_requests_watch_time_segments_long_wait_by_record_time(self):
        session = FakeWatchTimeSession(before_result="39:03", after_result="49:03")
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/5416")
        item = course.items[0]
        session.detail_calls = 0
        waited = []

        result = run_requests_watch_time(
            session,
            course,
            item,
            wait_seconds=600,
            wait_func=waited.append,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.waited_seconds, 600)
        self.assertEqual(result.endpoint_summary["post_count"], 10)
        self.assertEqual(result.endpoint_summary["post_intervals"], [60] * 10)
        self.assertEqual(waited, [60] * 10)
        watch_posts = [url for url in session.posted_urls if "watchTime" in url]
        self.assertEqual(len(watch_posts), 10)
        self.assertTrue(all("t=60" in url for url in watch_posts))

    def test_requests_watch_time_post_without_detail_increase_is_not_verified(self):
        session = FakeWatchTimeSession(
            before_result="39:03",
            after_result="39:03",
            before_state=ItemState.IN_PROGRESS,
            after_state=ItemState.IN_PROGRESS,
        )
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/5416")
        item = course.items[0]
        session.detail_calls = 0

        result = run_requests_watch_time(session, course, item, wait_seconds=0, wait_func=lambda seconds: None)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "watch_time_not_verified")
        self.assertEqual(result.requests_reproduction_status, "requests_partial")

    def test_requests_watch_time_missing_token_is_explicit(self):
        media_html = r"""
        <script>
        new ReadLog({"recordUrl":"\/ajax\/sys.pages.media\/watchTime\/?timing=pageload","recordTime":60});
        </script>
        """
        session = FakeWatchTimeSession(media_html=media_html)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/5416")
        item = course.items[0]
        session.detail_calls = 0

        result = run_requests_watch_time(session, course, item, wait_seconds=0, wait_func=lambda seconds: None)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "watch_time_missing_token")
        self.assertIn("watch_time_log_id_missing", result.issues)
        self.assertIn("watch_time_ajax_auth_missing", result.issues)
        self.assertFalse(any("watchTime" in url for url in session.posted_urls if "checkPassPrevious" not in url))


if __name__ == "__main__":
    unittest.main()
