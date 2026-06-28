import json
import sys
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tms_vghks.playwright_probe as playwright_probe_module
from tms_vghks.models import CourseDetail, CourseItem, CourseSummary, ItemKind, ItemState
from tms_vghks.playwright_probe import (
    ActivityProbe,
    ExtractedQuestion,
    KExamAttempt,
    annotate_canonical_answers,
    build_historical_quiz_records,
    build_probe_records,
    classify_historical_answer,
    dedupe_kexam_attempts,
    extract_activity_probe_from_html,
    extract_attempt_row_metadata,
    extract_kexam_attempts_from_modal_html,
    historical_merge_key,
    probe_kexam_attempt_with_page,
    render_historical_quiz_bank_markdown,
    render_probe_markdown,
    to_jsonable,
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


class PlaywrightProbeTests(unittest.TestCase):
    def test_extracts_quiz_questions_selected_and_correct_answers(self):
        html = """
        <form>
          <div class="question">
            <p>下列何者正確？</p>
            <label class="correct"><input type="radio" name="q1" value="A" checked>以上皆是</label>
            <label><input type="radio" name="q1" value="B">以上皆非</label>
          </div>
          <table><tr><th>測驗日期</th><th>分數</th></tr><tr><td>2026-06-15 01:00</td><td>100</td></tr></table>
        </form>
        """
        probe = extract_activity_probe_from_html(html, ItemKind.QUIZ)
        self.assertEqual(probe.availability, "available")
        self.assertEqual(probe.score, "100")
        self.assertEqual(probe.question_records[0].text, "下列何者正確？")
        self.assertEqual(probe.question_records[0].options, ["以上皆是", "以上皆非"])
        self.assertEqual(probe.question_records[0].selected_answers, ["以上皆是"])
        self.assertEqual(probe.question_records[0].correct_answers, ["以上皆是"])

    def test_extracts_survey_free_text_answer(self):
        html = """
        <form>
          <label for="s1">課程建議</label>
          <textarea id="s1" name="s1">內容清楚</textarea>
        </form>
        """
        probe = extract_activity_probe_from_html(html, ItemKind.SURVEY)
        self.assertEqual(probe.availability, "available")
        self.assertEqual(probe.question_records[0].question_type, "free_text")
        self.assertEqual(probe.question_records[0].selected_answers, ["內容清楚"])

    def test_score_only_modal_is_unavailable(self):
        html = """
        <table><tr><th>測驗日期</th><th>分數</th></tr><tr><td>2026-06-15 01:00</td><td>80</td></tr></table>
        """
        probe = extract_activity_probe_from_html(html, ItemKind.QUIZ)
        self.assertEqual(probe.availability, "unavailable")
        self.assertEqual(probe.score, "80")
        self.assertIn("score metadata", probe.reason)

    def test_kexam_modal_extracts_blank_record_links_and_attempt_metadata(self):
        html = """
        <table>
          <tr><th>測驗日期</th><th>分數</th><th>作答記錄</th></tr>
          <tr>
            <td>2026-06-15 01:00</td><td>80</td>
            <td><a href="/kexam/8268/record?key=secret&recordID=940538"></a></td>
          </tr>
          <tr>
            <td>2026-06-15 01:30</td><td>0</td>
            <td>未繳交 <a href="/kexam/8268/record?key=secret2&recordID=940539"></a></td>
          </tr>
        </table>
        """
        attempts = extract_kexam_attempts_from_modal_html(html)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].exam_id, "8268")
        self.assertEqual(attempts[0].record_id, "940538")
        self.assertEqual(attempts[0].attempt_at, "2026-06-15 01:00")
        self.assertEqual(attempts[0].score, "80")
        self.assertEqual(attempts[0].submitted_status, "submitted")
        self.assertEqual(attempts[1].submitted_status, "unsubmitted")
        self.assertIn("key=REDACTED", attempts[0].redacted_record_url)
        self.assertNotIn("secret", attempts[0].redacted_record_url)

    def test_dedupes_kexam_attempts_by_record_id_or_redacted_url(self):
        attempts = [
            KExamAttempt(
                exam_id="8268",
                record_id="940538",
                record_url="https://tms.vghks.gov.tw/kexam/8268/record?key=a&recordID=940538",
                redacted_record_url="",
            ),
            KExamAttempt(
                exam_id="8268",
                record_id="940538",
                record_url="https://tms.vghks.gov.tw/kexam/8268/record?key=b&recordID=940538",
                redacted_record_url="",
            ),
            KExamAttempt(
                exam_id="8268",
                record_id=None,
                record_url="https://tms.vghks.gov.tw/kexam/8268/record?key=a",
                redacted_record_url="",
            ),
            KExamAttempt(
                exam_id="8268",
                record_id=None,
                record_url="https://tms.vghks.gov.tw/kexam/8268/record?key=b",
                redacted_record_url="",
            ),
        ]

        deduped = dedupe_kexam_attempts(attempts)

        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0].record_id, "940538")
        self.assertIsNone(deduped[1].record_id)

    def test_kexam_modal_does_not_treat_attempt_number_as_score(self):
        html = """
        <table>
          <tr>
            <td>10</td><td>-</td><td>2026-06-25 12:10:47</td><td>-</td><td>10.119.4.113</td>
          </tr>
        </table>
        """
        row = BeautifulSoup(html, "html.parser").find("tr")

        attempt_at, score = extract_attempt_row_metadata(row)

        self.assertEqual(attempt_at, "2026-06-25 12:10:47")
        self.assertIsNone(score)

    def test_playwright_kexam_probe_prefers_complete_json_payload(self):
        class FakePage:
            def goto(self, *args, **kwargs):
                return None

            def wait_for_load_state(self, *args, **kwargs):
                return None

            def wait_for_selector(self, *args, **kwargs):
                return None

            def evaluate(self, *args, **kwargs):
                return None

            def content(self):
                payload = {
                    "record": {"score": "100", "submitTime": "2026-06-25 12:20:00"},
                    "questionData": {
                        str(index): {
                            "id": index,
                            "type": 0,
                            "questionTitle": f"<div>題目 {index}</div>",
                            "option": ["是", "否"],
                            "answer": 0,
                            "record": {"userAnswer": "{\"answer\":0}", "isCorrect": 1},
                        }
                        for index in range(1, 6)
                    },
                }
                raw = json.dumps(payload, ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")
                return (
                    "<body>"
                    "<div class='question'><p>題目 1</p><label><input type='radio' name='q1' checked>是</label></div>"
                    "<div class='question'><p>題目 2</p><label><input type='radio' name='q2' checked>是</label></div>"
                    "<div class='question'><p>題目 3</p><label><input type='radio' name='q3' checked>是</label></div>"
                    f"<script>fs.kexamRecord.setData('#kexam-record', JSON.parse('{raw}'));</script>"
                    "</body>"
                )

        attempt = KExamAttempt(
            exam_id="8267",
            record_id="953999",
            record_url="https://tms.vghks.gov.tw/kexam/8267/record?recordID=953999&key=secret",
            redacted_record_url="https://tms.vghks.gov.tw/kexam/8267/record?recordID=953999&key=REDACTED",
            submitted_status="submitted",
        )

        probe = probe_kexam_attempt_with_page(FakePage(), attempt)

        self.assertEqual(len(probe.question_records), 5)
        self.assertEqual(sum(len(question.selected_answers) for question in probe.question_records), 5)
        self.assertEqual(probe.score, "100")

    def test_record_page_extracts_disabled_checked_answers(self):
        html = """
        <body class="body-layout-record">
          <div class="question">
            <p>全民國防教育的目的為何？</p>
            <label class="correct"><input type="radio" name="q1" value="A" checked disabled>提升國防意識</label>
            <label><input type="radio" name="q1" value="B" disabled>降低安全觀念</label>
          </div>
        </body>
        """
        probe = extract_activity_probe_from_html(html, ItemKind.QUIZ)
        self.assertEqual(probe.availability, "available")
        self.assertEqual(probe.question_records[0].selected_answers, ["提升國防意識"])
        self.assertEqual(probe.question_records[0].correct_answers, ["提升國防意識"])

    def test_record_page_extracts_incorrect_marker(self):
        html = """
        <div class="question">
          <p>何者正確？</p>
          <label class="incorrect"><input type="radio" name="q1" checked disabled>A</label>
          <label class="correct"><input type="radio" name="q1" disabled>B</label>
        </div>
        """
        probe = extract_activity_probe_from_html(html, ItemKind.QUIZ)
        question = probe.question_records[0]
        self.assertEqual(question.selected_answers, ["A"])
        self.assertEqual(question.correct_answers, ["B"])
        self.assertEqual(question.incorrect_answers, ["A"])
        status, method, _ = classify_historical_answer(question, probe)
        self.assertEqual(status, "verified_wrong")
        self.assertEqual(method, "per_question_marker")

    def test_record_table_group_uses_shared_question_container(self):
        html = """
        <div class="question">
          <div class="question-title">下列敘述是否正確？</div>
          <table>
            <tr><td><label><input type="radio" name="q1" value="yes" checked disabled>是</label></td></tr>
            <tr><td><label><input type="radio" name="q1" value="no" disabled>否</label></td></tr>
          </table>
        </div>
        """
        probe = extract_activity_probe_from_html(html, ItemKind.QUIZ)
        self.assertEqual(probe.question_records[0].text, "下列敘述是否正確？")
        self.assertEqual(probe.question_records[0].options, ["是", "否"])

    def test_probe_records_redact_tokens_and_markdown(self):
        probe = extract_activity_probe_from_html(
            """
            <div><p>題目一</p><label><input type="radio" name="q1" checked>答案一</label></div>
            """,
            ItemKind.QUIZ,
        )
        course = CourseSummary("Course", "https://tms.vghks.gov.tw/course/1", "1", "2026-06-15", True)
        detail = CourseDetail("Course", "https://tms.vghks.gov.tw/course/1", "1")
        item = CourseItem(
            "Quiz",
            order=1,
            kind=ItemKind.QUIZ,
            state=ItemState.PASSED,
            metadata={
                "activity_id": "a1",
                "result_modal_url": "https://tms.vghks.gov.tw/ajax/kexam?activityID=1&ajaxAuth=secret",
            },
        )
        records = build_probe_records(course, detail, item, probe, "2026-06-16T00:00:00+00:00")
        payload = json.dumps(to_jsonable(records[0]), ensure_ascii=False)
        markdown = render_probe_markdown(records)
        self.assertIn("ajaxAuth=REDACTED", payload)
        self.assertNotIn("secret", payload)
        self.assertIn("AnswerStatus: available", markdown)

    def test_markdown_summaries_include_pacing_label_counts(self):
        pacing = {
            "enabled": True,
            "min_ms": 400,
            "max_ms": 1400,
            "sleep_count": 3,
            "total_sleep_seconds": 2.1,
            "label_counts": {
                "requests:activity_probe": 1,
                "requests:kexam_record": 2,
            },
        }

        probe_markdown = render_probe_markdown([], pacing=pacing)
        historical_markdown = render_historical_quiz_bank_markdown([], pacing=pacing)

        self.assertIn("PacingLabelCounts: requests:activity_probe=1, requests:kexam_record=2", probe_markdown)
        self.assertIn("PacingLabelCounts: requests:activity_probe=1, requests:kexam_record=2", historical_markdown)

    def test_probe_record_attempt_metadata_redacts_record_key(self):
        probe = extract_activity_probe_from_html(
            """
            <div><p>題目一</p><label><input type="radio" name="q1" checked>答案一</label></div>
            """,
            ItemKind.QUIZ,
        )
        attempts = extract_kexam_attempts_from_modal_html(
            """
            <table><tr><td>2026-06-15</td><td>100</td>
            <td><a href="/kexam/8268/record?key=secret&recordID=940538"></a></td></tr></table>
            """
        )
        probe.attempt = attempts[0]
        course = CourseSummary("Course", "https://tms.vghks.gov.tw/course/1", "1", "2026-06-15", True)
        detail = CourseDetail("Course", "https://tms.vghks.gov.tw/course/1", "1")
        item = CourseItem("Quiz", order=1, kind=ItemKind.QUIZ, state=ItemState.PASSED)
        records = build_probe_records(course, detail, item, probe, "2026-06-16T00:00:00+00:00")
        payload = json.dumps(to_jsonable(records[0]), ensure_ascii=False)
        self.assertIn('"attempt"', payload)
        self.assertIn("key=REDACTED", payload)
        self.assertNotIn("secret", payload)

    def test_historical_records_classify_full_score_and_partial_score(self):
        course = CourseSummary("Course", "https://tms.vghks.gov.tw/course/1", "1", "2026-06-15", True)
        detail = CourseDetail("Course", "https://tms.vghks.gov.tw/course/1", "1")
        item = CourseItem("Quiz", order=1, kind=ItemKind.QUIZ, state=ItemState.PASSED)
        question = extract_activity_probe_from_html(
            """
            <div><p>題目一</p><label><input type="radio" name="q1" checked>A</label><label><input type="radio" name="q1">B</label></div>
            """,
            ItemKind.QUIZ,
        ).question_records[0]
        full_probe = ActivityProbe(
            question_records=[question],
            score="100",
            attempt=KExamAttempt("1", "r1", "https://tms.vghks.gov.tw/kexam/1/record?key=secret&recordID=r1", "https://tms.vghks.gov.tw/kexam/1/record?key=REDACTED&recordID=r1", score="100"),
        )
        partial_probe = ActivityProbe(
            question_records=[question],
            score="80",
            attempt=KExamAttempt("1", "r2", "https://tms.vghks.gov.tw/kexam/1/record?key=secret&recordID=r2", "https://tms.vghks.gov.tw/kexam/1/record?key=REDACTED&recordID=r2", score="80"),
        )
        probe = ActivityProbe(attempt_probes=[partial_probe, full_probe])
        records, issues = build_historical_quiz_records(course, detail, item, probe, "2026-06-16T00:00:00+00:00")
        self.assertFalse(issues)
        self.assertEqual([record.answer.status for record in records], ["unverified_selected", "verified_correct"])
        self.assertEqual(records[1].assessment["verification_method"], "full_score_inferred")
        self.assertEqual(records[1].question["merge_key"], historical_merge_key(question))

    def test_historical_records_mark_canonical_without_wrong_overwriting_correct(self):
        course = CourseSummary("Course", "https://tms.vghks.gov.tw/course/1", "1", "2026-06-15", True)
        detail = CourseDetail("Course", "https://tms.vghks.gov.tw/course/1", "1")
        item = CourseItem("Quiz", order=1, kind=ItemKind.QUIZ, state=ItemState.PASSED)
        wrong_question = extract_activity_probe_from_html(
            """
            <div><p>題目一</p><label class="incorrect"><input type="radio" name="q1" checked>A</label><label class="correct"><input type="radio" name="q1">B</label></div>
            """,
            ItemKind.QUIZ,
        ).question_records[0]
        correct_question = extract_activity_probe_from_html(
            """
            <div><p>題目一</p><label><input type="radio" name="q1">A</label><label><input type="radio" name="q1" checked>B</label></div>
            """,
            ItemKind.QUIZ,
        ).question_records[0]
        probe = ActivityProbe(
            attempt_probes=[
                ActivityProbe(question_records=[wrong_question], score="50", attempt=KExamAttempt("1", "r1", "u", "u", attempt_at="2026-01-01", score="50")),
                ActivityProbe(question_records=[correct_question], score="100", attempt=KExamAttempt("1", "r2", "u", "u", attempt_at="2026-01-02", score="100")),
            ]
        )
        records, _ = build_historical_quiz_records(course, detail, item, probe, "2026-06-16T00:00:00+00:00")
        annotate_canonical_answers(records)
        self.assertEqual(records[0].answer.status, "verified_wrong")
        self.assertEqual(records[1].answer.status, "verified_correct")
        self.assertFalse(records[0].assessment["is_canonical"])
        self.assertTrue(records[1].assessment["is_canonical"])

    def test_historical_export_uses_pacer_for_course_activity_and_records(self):
        class FakeHistorySession:
            def list_completed_courses_playwright(self):
                return [CourseSummary("Course", "https://tms.vghks.gov.tw/course/1", "1", "2026-06-15", True)]

            def get_course_detail_playwright(self, url_or_id):
                return CourseDetail(
                    "Course",
                    "https://tms.vghks.gov.tw/course/1",
                    "1",
                    items=[CourseItem("課後測驗", order=1, kind=ItemKind.QUIZ, state=ItemState.PASSED)],
                )

        def fake_probe_activity(session, detail, item, include_unsubmitted_records=False, pacer=None):
            if pacer:
                pacer.sleep("playwright:kexam_record")
            question = ExtractedQuestion(
                text="題目一",
                options=["A", "B"],
                selected_answers=["A"],
                correct_answers=["A"],
            )
            attempt = KExamAttempt(
                exam_id="1",
                record_id="r1",
                record_url="https://tms.vghks.gov.tw/kexam/1/record?recordID=r1",
                redacted_record_url="https://tms.vghks.gov.tw/kexam/1/record?recordID=r1",
                score="100",
                submitted_status="submitted",
            )
            return ActivityProbe(
                attempt_probes=[
                    ActivityProbe(
                        question_records=[question],
                        score="100",
                        attempt=attempt,
                    )
                ]
            )

        old_probe = playwright_probe_module.probe_activity_playwright
        playwright_probe_module.probe_activity_playwright = fake_probe_activity
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pacer = RecordingPacer()
                result = playwright_probe_module.export_historical_quiz_bank_playwright(
                    FakeHistorySession(),
                    output_path=Path(tmp) / "history.jsonl",
                    markdown_path=Path(tmp) / "history.md",
                    allow_private_export=True,
                    pacer=pacer,
                )
        finally:
            playwright_probe_module.probe_activity_playwright = old_probe

        self.assertIn("playwright:course_detail", pacer.labels)
        self.assertIn("playwright:activity_probe", pacer.labels)
        self.assertIn("playwright:kexam_record", pacer.labels)
        self.assertEqual(result.pacing["sleep_count"], len(pacer.labels))


if __name__ == "__main__":
    unittest.main()
