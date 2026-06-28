import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.models import CourseItem, ItemKind, ItemState
from tms_vghks.playwright_form_validation import (
    FormValidationRecord,
    classify_form_html,
    entry_labels_for_item,
    parse_scope,
    render_validation_markdown,
    to_jsonable,
)
from tms_vghks.question_bank_export import ExportIssue


class PlaywrightFormValidationTests(unittest.TestCase):
    def test_quiz_form_available_with_submit_button(self):
        html = """
        <form>
          <div class="question">
            <p>何者正確？</p>
            <label><input type="radio" name="q1">A</label>
            <label><input type="radio" name="q1">B</label>
          </div>
          <button type="submit">送出</button>
        </form>
        """
        classification, fields, questions = classify_form_html(html, ItemKind.QUIZ)
        self.assertEqual(classification, "form_available")
        self.assertEqual(fields.radio_groups, 1)
        self.assertIn("送出", fields.submit_buttons)
        self.assertEqual(len(questions), 1)

    def test_survey_form_available_with_counter(self):
        html = """
        <form>
          <div>已填寫: 1 / 2 題</div>
          <label><input type="radio" name="s1">普通</label>
          <label for="t1">建議</label><textarea id="t1"></textarea>
          <button>提交</button>
        </form>
        """
        classification, fields, _ = classify_form_html(html, ItemKind.SURVEY)
        self.assertEqual(classification, "form_available")
        self.assertEqual(fields.radio_groups, 1)
        self.assertEqual(fields.text_fields, 1)
        self.assertEqual(fields.fill_counter, "已填寫: 1 / 2")

    def test_review_available_without_submit(self):
        html = """
        <div>
          <p>何者正確？</p>
          <label><input type="radio" name="q1" checked disabled>A</label>
          <label><input type="radio" name="q1" disabled>B</label>
        </div>
        """
        classification, fields, questions = classify_form_html(html, ItemKind.QUIZ)
        self.assertEqual(classification, "review_available")
        self.assertEqual(fields.submit_buttons, [])
        self.assertEqual(questions[0].selected_answers, ["A"])

    def test_result_only_modal_does_not_become_form(self):
        html = """
        <table><tr><th>測驗日期</th><th>分數</th></tr><tr><td>2026-06-15</td><td>100</td></tr></table>
        """
        classification, fields, questions = classify_form_html(html, ItemKind.QUIZ)
        self.assertEqual(classification, "result_only")
        self.assertFalse(questions)
        self.assertFalse(fields.submit_buttons)

    def test_sequence_guard_classification(self):
        classification, _, _ = classify_form_html("<div>請依序完成</div>", ItemKind.QUIZ)
        self.assertEqual(classification, "blocked_or_sequence_guard")

    def test_course_detail_sequence_text_is_not_blocked(self):
        html = """
        <div id="activityTree">
          <div>課程內容 ( 請依序完成 )</div>
          <div>課前測驗 學習成果 100 通過</div>
        </div>
        """
        classification, _, _ = classify_form_html(html, ItemKind.QUIZ)
        self.assertEqual(classification, "result_only")

    def test_validation_markdown_and_json_redact_tokens(self):
        record = FormValidationRecord(
            schema_version="v",
            exported_at="2026-06-16T00:00:00+00:00",
            scope="completed",
            source_system="tms.vghks.gov.tw",
            course={"title": "Course", "course_id": "1"},
            activity={
                "title": "Quiz",
                "kind": "quiz",
                "result_modal_url": "https://tms.vghks.gov.tw/ajax/kexam?activityID=1&ajaxAuth=REDACTED",
            },
            classification="result_only",
            attempts=[
                {
                    "record_id": "940538",
                    "record_url": "https://tms.vghks.gov.tw/kexam/8268/record?key=REDACTED&recordID=940538",
                    "submitted_status": "submitted",
                }
            ],
            record_count=1,
            record_question_count=5,
            record_selected_answer_count=5,
            issues=[ExportIssue("result_only_unavailable", "only score")],
        )
        payload = json.dumps(to_jsonable(record), ensure_ascii=False)
        markdown = render_validation_markdown([record])
        self.assertIn("ajaxAuth=REDACTED", payload)
        self.assertIn("key=REDACTED", payload)
        self.assertNotIn("secret", payload)
        self.assertIn("KExamRecords: records=1", markdown)
        self.assertIn("result_only", markdown)

    def test_parse_scope(self):
        self.assertEqual(parse_scope("completed,pending"), {"completed", "pending"})
        with self.assertRaisesRegex(ValueError, "unsupported validation scope"):
            parse_scope("all")

    def test_passed_survey_uses_review_labels_first(self):
        item = CourseItem("Survey", kind=ItemKind.SURVEY, state=ItemState.PASSED, passed_marker="通過")
        labels = entry_labels_for_item(item)
        self.assertIn("問卷回饋狀態", labels)
        self.assertNotIn("填寫問卷", labels)


if __name__ == "__main__":
    unittest.main()
