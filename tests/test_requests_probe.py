import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.models import CourseDetail, CourseItem, ItemKind
from tms_vghks.requests_probe import classify_activity_form_requests, probe_activity_requests


class FakeProbeSession:
    base_url = "https://tms.vghks.gov.tw"

    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        self.fetched.append((path_or_url, referer))
        if path_or_url not in self.pages:
            raise ValueError(f"missing page: {path_or_url}")
        return self.pages[path_or_url]


class RecordingPacer:
    def __init__(self):
        self.labels = []

    def sleep(self, label=""):
        self.labels.append(label)
        return 0.0

    def summary(self):
        return {
            "enabled": True,
            "min_ms": 1,
            "max_ms": 2,
            "seed": None,
            "sleep_count": len(self.labels),
            "total_sleep_seconds": 0.0,
            "label_counts": {label: self.labels.count(label) for label in sorted(set(self.labels))},
        }


class RequestsProbeTests(unittest.TestCase):
    def test_probe_activity_requests_follows_kexam_record_links(self):
        result_url = "https://tms.vghks.gov.tw/ajax/sys.app.learningItem/kexam/?activityID=1&ajaxAuth=secret"
        record_url = "https://tms.vghks.gov.tw/kexam/8268/record?key=secret&recordID=940538"
        session = FakeProbeSession(
            {
                result_url: """
                <table>
                  <tr><th>測驗日期</th><th>分數</th><th>作答記錄</th></tr>
                  <tr><td>2026-06-15 01:00</td><td>100</td><td><a href="/kexam/8268/record?key=secret&recordID=940538"></a></td></tr>
                </table>
                """,
                record_url: """
                <form>
                  <div class="question">
                    <p>何者正確？</p>
                    <label class="correct"><input type="radio" name="q1" checked>以上皆是</label>
                  </div>
                </form>
                """,
            }
        )
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(
            title="Quiz",
            kind=ItemKind.QUIZ,
            metadata={"result_modal_url": result_url},
        )

        probe = probe_activity_requests(session, course, item)

        self.assertEqual(len(probe.attempt_probes), 1)
        self.assertEqual(probe.attempt_probes[0].availability, "available")
        self.assertEqual(probe.attempt_probes[0].question_records[0].selected_answers, ["以上皆是"])

    def test_probe_activity_requests_dedupes_record_links_across_sources(self):
        result_url = "https://tms.vghks.gov.tw/ajax/sys.app.learningItem/kexam/?activityID=1&ajaxAuth=secret"
        detail_url = "https://tms.vghks.gov.tw/course/1/exam/1"
        record_url = "https://tms.vghks.gov.tw/kexam/8268/record?key=secret&recordID=940538"
        modal_html = """
        <table>
          <tr><th>測驗日期</th><th>分數</th><th>作答記錄</th></tr>
          <tr><td>2026-06-15 01:00</td><td>100</td><td><a href="/kexam/8268/record?key=secret&recordID=940538"></a></td></tr>
        </table>
        """
        session = FakeProbeSession(
            {
                result_url: modal_html,
                detail_url: modal_html,
                record_url: """
                <form><div class="question"><p>何者正確？</p>
                <label class="correct"><input type="radio" name="q1" checked>以上皆是</label></div></form>
                """,
            }
        )
        pacer = RecordingPacer()
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(
            title="Quiz",
            kind=ItemKind.QUIZ,
            detail_url=detail_url,
            metadata={"result_modal_url": result_url},
        )

        probe = probe_activity_requests(session, course, item, pacer=pacer)

        self.assertEqual(len(probe.attempt_probes), 1)
        self.assertEqual(pacer.labels, ["requests:kexam_record"])
        self.assertEqual(sum(1 for url, _ in session.fetched if str(url) == record_url), 1)

    def test_probe_activity_requests_does_not_sleep_for_skipped_unsubmitted_record(self):
        result_url = "https://tms.vghks.gov.tw/ajax/sys.app.learningItem/kexam/?activityID=1&ajaxAuth=secret"
        record_url = "https://tms.vghks.gov.tw/kexam/8268/record?key=secret&recordID=940539"
        session = FakeProbeSession(
            {
                result_url: """
                <table>
                  <tr><th>測驗日期</th><th>分數</th><th>作答記錄</th></tr>
                  <tr><td>2026-06-15 01:30</td><td>-</td><td>未繳交 <a href="/kexam/8268/record?key=secret&recordID=940539"></a></td></tr>
                </table>
                """,
            }
        )
        pacer = RecordingPacer()
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(title="Quiz", kind=ItemKind.QUIZ, metadata={"result_modal_url": result_url})

        probe = probe_activity_requests(session, course, item, pacer=pacer)

        self.assertEqual(pacer.labels, [])
        self.assertFalse(any(str(url) == record_url for url, _ in session.fetched))
        self.assertEqual(probe.attempt_probes[0].availability, "unavailable")

    def test_classify_activity_form_requests_uses_direct_detail_url(self):
        detail_url = "https://tms.vghks.gov.tw/survey/1"
        session = FakeProbeSession(
            {
                detail_url: """
                <form>
                  <label><input type="radio" name="s1">普通</label>
                  <button type="submit">送出</button>
                </form>
                """
            }
        )
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(title="Survey", kind=ItemKind.SURVEY, detail_url=detail_url)

        result = classify_activity_form_requests(session, course, item)

        self.assertEqual(result.classification, "form_available")
        self.assertEqual(result.entry_method, "detail_url")
        self.assertEqual(result.fields.radio_groups, 1)


if __name__ == "__main__":
    unittest.main()
