import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.models import CourseDetail, CourseItem, CourseSummary, ItemKind, ItemState
from tms_vghks.reference_bank import build_reference_question_bank
from tms_vghks.requests_question_bank import export_historical_quiz_bank_requests


KEXAM_EXAM_HTML = """
<dl>
  <dt>次數限制</dt>
  <dd>無限制 ( 已測驗 1 次，<a href="####" data-url="/ajax/sys.modules.mod_kexamRecordList/list/?kexamID=8267&ajaxAuth=secret">紀錄</a> )</dd>
  <dt>成績</dt>
  <dd>100 <a href="/kexam/8267/record?key=best-secret&recordID=953644">作答記錄</a></dd>
</dl>
"""

KEXAM_RECORD_MODAL_HTML = """
<table><tbody>
  <tr>
    <td>1</td><td>100</td><td>2026-06-25 15:30:24</td><td>2026-06-25 15:34:01</td>
    <td><a href="/kexam/8267/record?recordID=953644&key=record-secret&show=0&title=x&from=record"></a></td>
  </tr>
</tbody></table>
"""


def kexam_record_json_html():
    payload = {
        "record": {"id": "953644", "score": "100", "submitTime": "2026-06-25 15:34:01"},
        "questionData": {
            "47702": {
                "id": 47702,
                "type": 1,
                "questionTitle": "<div>我國發展之第一款自製之防衛戰機名稱為何?</div>",
                "option": [
                    {"text": "F-16戰機", "img": ""},
                    {"text": "IDF戰機", "img": ""},
                    {"text": "幻象2000戰機", "img": ""},
                ],
                "answer": [1],
                "score": "20",
                "record": {"userAnswer": "{\"answer\":[1]}", "isCorrect": 1, "getScore": 20},
            }
        },
    }
    raw = json.dumps(payload, ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")
    return f"<script>fs.kexamRecord.setData('#kexam-record', JSON.parse('{raw}'));</script>"


class FakeRequestsHistorySession:
    base_url = "https://tms.vghks.gov.tw"

    def list_completed_courses(self):
        return [
            CourseSummary(
                title="115年全民國防教育訓練",
                detail_url="https://tms.vghks.gov.tw/course/5416",
                course_id="5416",
                progress="2026-06-25",
                completed=True,
            )
        ]

    def get_course_detail(self, url_or_id):
        return CourseDetail(
            title="115年全民國防教育訓練",
            url="https://tms.vghks.gov.tw/course/5416",
            course_id="5416",
            completed=True,
            items=[
                CourseItem(
                    title="課後測驗",
                    order=1,
                    kind=ItemKind.QUIZ,
                    state=ItemState.PASSED,
                    detail_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
                    pass_condition="80 分及格",
                    result="100",
                    passed_marker="通過",
                    metadata={"activity_id": "kexam-posttest"},
                ),
                CourseItem(
                    title="一般測驗",
                    order=2,
                    kind=ItemKind.QUIZ,
                    state=ItemState.PASSED,
                    detail_url="https://tms.vghks.gov.tw/entry/generic-quiz",
                    pass_condition="60 分及格",
                    result="100",
                    passed_marker="通過",
                ),
            ],
        )

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        url = str(path_or_url)
        if "/course/5416/exam/10613" in url:
            return KEXAM_EXAM_HTML
        if "mod_kexamRecordList" in url:
            return KEXAM_RECORD_MODAL_HTML
        if "/kexam/8267/record" in url:
            return kexam_record_json_html()
        if "generic-quiz" in url:
            return "<table><tr><th>測驗日期</th><th>分數</th></tr><tr><td>2026-06-25</td><td>100</td></tr></table>"
        return ""


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


class RequestsQuestionBankExportTests(unittest.TestCase):
    def test_requests_historical_export_writes_buildable_kexam_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.jsonl"
            markdown = Path(tmp) / "history.md"
            reference = Path(tmp) / "question-bank.jsonl"

            result = export_historical_quiz_bank_requests(
                FakeRequestsHistorySession(),
                output_path=history,
                markdown_path=markdown,
                source_account_label="account1",
                allow_private_export=True,
            )
            built = build_reference_question_bank(history, reference)
            rows = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result.record_count, 1)
        self.assertEqual(result.quiz_activity_count, 2)
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(rows[0]["provenance"]["method"], "requests-historical-kexam-record")
        self.assertEqual(rows[0]["answer"]["status"], "verified_correct")
        self.assertEqual(rows[0]["answer"]["selected_answers"], ["B. IDF戰機"])
        self.assertEqual(built.reference_record_count, 1)
        self.assertTrue(any(issue.code == "quiz_records_unavailable" for issue in result.issues))
        self.assertEqual(result.pacing["enabled"], False)

    def test_requests_historical_export_requires_private_export_flag(self):
        with self.assertRaises(ValueError):
            export_historical_quiz_bank_requests(FakeRequestsHistorySession(), allow_private_export=False)

    def test_requests_historical_export_uses_pacer_for_course_activity_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            pacer = RecordingPacer()
            result = export_historical_quiz_bank_requests(
                FakeRequestsHistorySession(),
                output_path=Path(tmp) / "history.jsonl",
                markdown_path=Path(tmp) / "history.md",
                allow_private_export=True,
                pacer=pacer,
            )

        self.assertIn("requests:course_detail", pacer.labels)
        self.assertGreaterEqual(pacer.labels.count("requests:activity_probe"), 2)
        self.assertIn("requests:kexam_record", pacer.labels)
        self.assertEqual(result.pacing["sleep_count"], len(pacer.labels))
        self.assertEqual(result.pacing["label_counts"]["requests:course_detail"], 1)
        self.assertEqual(result.pacing["label_counts"]["requests:kexam_record"], 1)


if __name__ == "__main__":
    unittest.main()
