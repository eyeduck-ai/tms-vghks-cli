import json
import sys
import tempfile
import unittest
from pathlib import Path

from requests import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.models import CourseDetail, CourseItem, CourseSummary, ItemKind, ItemState
from tms_vghks.question_bank_export import export_question_bank


def make_response(body: str, content_type: str = "application/json; charset=utf-8") -> Response:
    response = Response()
    response.status_code = 200
    response.url = "https://tms.vghks.gov.tw/ajax"
    response.headers["content-type"] = content_type
    response._content = body.encode("utf-8")
    return response


class FakeExportSession:
    def list_completed_courses(self):
        return [
            CourseSummary(
                title="Course A",
                detail_url="https://tms.vghks.gov.tw/course/1",
                course_id="1",
                progress="2026-06-15",
                completed=True,
            )
        ]

    def get_course_detail(self, url):
        return CourseDetail(
            title="Course A",
            url="https://tms.vghks.gov.tw/course/1",
            course_id="1",
            completed=True,
            items=[
                CourseItem(
                    title="Post Quiz",
                    order=1,
                    kind=ItemKind.QUIZ,
                    state=ItemState.PASSED,
                    pass_condition="60 分及格",
                    result="100",
                    passed_marker="通過",
                    metadata={
                        "activity_id": "a1",
                        "deadline": "12-31",
                        "result_modal_url": "https://tms.vghks.gov.tw/ajax/sys.app.learningItem/kexam/?activityID=1&ajaxAuth=secret",
                    },
                ),
                CourseItem(
                    title="Survey",
                    order=2,
                    kind=ItemKind.SURVEY,
                    state=ItemState.PASSED,
                    pass_condition="須填寫",
                    result="已完成",
                    passed_marker="通過",
                    metadata={"activity_id": "a2", "deadline": "12-31"},
                ),
            ],
        )

    def get(self, url, **kwargs):
        return make_response(
            json.dumps(
                {
                    "status": True,
                    "data": {
                        "html": """
                        <table id="examTable">
                          <tr><th>測驗日期</th><th>分數</th></tr>
                          <tr><td>2026-06-15 01:37:00</td><td>100</td></tr>
                        </table>
                        """
                    },
                }
            )
        )


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


class QuestionBankExportTests(unittest.TestCase):
    def test_probe_counts_and_unavailable_issues(self):
        result = export_question_bank(FakeExportSession(), probe_only=True)
        self.assertEqual(result.course_count, 1)
        self.assertEqual(result.activity_count, 2)
        self.assertEqual(result.record_count, 2)
        self.assertEqual(result.quiz_result_endpoint_count, 1)
        self.assertGreaterEqual(result.issue_count, 2)

    def test_requires_private_export_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "question-bank.jsonl"
            with self.assertRaisesRegex(ValueError, "private export"):
                export_question_bank(FakeExportSession(), output_path=path)

    def test_writes_jsonl_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "question-bank.jsonl"
            result = export_question_bank(
                FakeExportSession(),
                output_path=path,
                allow_private_export=True,
                source_account_label="tester",
            )
            self.assertEqual(result.record_count, 2)
            exported_text = path.read_text(encoding="utf-8")
            rows = [json.loads(line) for line in exported_text.splitlines()]
            self.assertNotIn("secret", exported_text)
        self.assertEqual(rows[0]["schema_version"], "tms-vghks-question-bank.v1")
        self.assertEqual(rows[0]["source_account_label"], "tester")
        self.assertEqual(rows[0]["course"]["course_id"], "1")
        self.assertEqual(rows[0]["activity"]["activity_id"], "a1")
        self.assertIn("ajaxAuth=REDACTED", rows[0]["activity"]["result_modal_url"])
        self.assertNotIn("secret", rows[0]["activity"]["result_modal_url"])
        self.assertEqual(rows[0]["answer"]["status"], "unavailable")
        self.assertEqual(rows[0]["answer"]["score"], "100")

    def test_export_question_bank_returns_pacing_metadata(self):
        pacer = RecordingPacer()

        result = export_question_bank(FakeExportSession(), probe_only=True, pacer=pacer)

        self.assertEqual(pacer.labels, ["requests:course_detail", "requests:result_modal"])
        self.assertEqual(result.pacing["sleep_count"], 2)
        self.assertEqual(
            result.pacing["label_counts"],
            {
                "requests:course_detail": 1,
                "requests:result_modal": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
