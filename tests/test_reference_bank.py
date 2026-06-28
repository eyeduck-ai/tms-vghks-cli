import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.quiz import QuestionBank, QuizQuestion, dated_question_bank_filename, find_latest_question_bank_path
from tms_vghks.reference_bank import (
    build_reference_question_bank,
    classify_activity_stage,
    render_reference_markdown,
    shared_bank_privacy_issues,
)


def history_row(stage_title, status, question="何者正確？", options=None, answers=None, score="100", merge_key="mk1"):
    options = options or ["A", "B"]
    answers = answers or ["A"]
    return {
        "source_system": "tms.vghks.gov.tw",
        "exported_at": "2026-06-16T00:00:00+00:00",
        "source_account_label": "account1",
        "course": {
            "title": "Course",
            "course_id": "1",
            "detail_url": "https://tms.vghks.gov.tw/course/1",
            "completed_at": "2026-06-16",
            "raw_text": "private raw course row",
        },
        "activity": {"title": stage_title, "activity_id": "a1", "kind": "quiz", "result_modal_url": "https://tms.example/ajax?ajaxAuth=REDACTED"},
        "question": {"text": question, "options": options, "merge_key": merge_key, "type": "single_choice"},
        "answer": {"status": status, "selected_answers": answers, "score": score, "attempt_at": "2026-06-16"},
        "assessment": {
            "type": "quiz",
            "score": score,
            "passing_condition": "80",
            "merge_key": merge_key,
            "answer_status": status,
            "verification_method": "kexam_record_probe",
            "confidence": 0.8,
            "is_canonical": True,
        },
        "attempt": {
            "exam_id": "e1",
            "record_id": "r1",
            "record_url": "https://tms.vghks.gov.tw/kexam/1/record?key=REDACTED&recordID=r1",
            "attempt_at": "2026-06-16",
            "score": score,
            "submitted_status": "submitted",
        },
        "provenance": {"collector": "tms-vghks", "method": "playwright-historical-kexam-record"},
    }


class ReferenceBankTests(unittest.TestCase):
    def test_classifies_activity_stage(self):
        self.assertEqual(classify_activity_stage("課前測驗"), "pretest")
        self.assertEqual(classify_activity_stage("課後測驗"), "posttest")
        self.assertEqual(classify_activity_stage("滿意度問卷"), "survey")
        self.assertEqual(classify_activity_stage("測驗"), "unknown")

    def test_build_reference_bank_uses_suggestion_for_posttest_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.jsonl"
            suggestions = root / "suggestions.jsonl"
            output = root / "reference.jsonl"
            markdown = root / "reference.md"
            history.write_text(
                json.dumps(history_row("課後測驗", "unverified_selected", answers=["A"]), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            suggestions.write_text(
                json.dumps({"merge_key": "mk1", "answers": ["B"], "reason": "best", "confidence": 0.9}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            result = build_reference_question_bank(history, output, markdown, suggestions)
            self.assertEqual(result.reference_record_count, 1)
            self.assertEqual(result.posttest_ai_suggestion_count, 1)
            row = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(row["answer"]["answers"], ["B"])
            self.assertEqual(row["answer"]["status"], "ai_suggested_trusted")
            self.assertTrue(row["answer"]["trusted_for_auto"])

    def test_verified_posttest_not_overwritten_by_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.jsonl"
            suggestions = root / "suggestions.jsonl"
            output = root / "reference.jsonl"
            history.write_text(
                json.dumps(history_row("課後測驗", "verified_correct", answers=["A"]), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            suggestions.write_text(
                json.dumps({"merge_key": "mk1", "answers": ["B"], "reason": "guess", "confidence": 0.5}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            build_reference_question_bank(history, output, None, suggestions)
            row = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(row["answer"]["answers"], ["A"])
            self.assertEqual(row["answer"]["status"], "verified_correct")

    def test_pretest_can_use_unverified_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.jsonl"
            output = root / "reference.jsonl"
            history.write_text(
                json.dumps(history_row("課前測驗", "unverified_selected", answers=["A"]), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            build_reference_question_bank(history, output, None)
            row = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(row["quiz_stage"], "pretest")
            self.assertEqual(row["answer"]["status"], "pretest_historical_selected")
            self.assertTrue(row["answer"]["trusted_for_auto"])

    def test_reference_bank_output_keeps_shared_evidence_without_raw_private_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.jsonl"
            output = root / "reference.jsonl"
            history.write_text(
                json.dumps(history_row("課後測驗", "verified_correct", answers=["A"]), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            build_reference_question_bank(history, output, None)
            row = json.loads(output.read_text(encoding="utf-8"))
            payload = json.dumps(row, ensure_ascii=False)

        self.assertEqual(row["course"], {"completed_at": "2026-06-16", "course_id": "1", "title": "Course"})
        self.assertEqual(row["activity"]["activity_id"], "a1")
        self.assertEqual(row["activity"]["title"], "課後測驗")
        self.assertEqual(row["activity"]["kind"], "quiz")
        self.assertEqual(row["answer"]["attempt_at"], "2026-06-16")
        self.assertEqual(row["assessment"]["verification_method"], "kexam_record_probe")
        self.assertTrue(row["assessment"]["is_canonical"])
        self.assertEqual(row["attempt"]["exam_id"], "e1")
        self.assertEqual(row["attempt"]["record_id"], "r1")
        self.assertEqual(row["attempt"]["submitted_status"], "submitted")
        self.assertIn("key=REDACTED", row["attempt"]["record_url"])
        self.assertNotIn("source_account_label", row)
        self.assertNotIn("detail_url", payload)
        self.assertNotIn("raw_text", payload)
        self.assertNotIn("result_modal_url", payload)

    def test_reference_bank_redacts_record_url_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.jsonl"
            output = root / "reference.jsonl"
            row = history_row("課後測驗", "verified_correct", answers=["A"])
            row["attempt"]["record_url"] = "https://tms.vghks.gov.tw/kexam/1/record?key=secret-token&token=other-secret&recordID=r1"
            history.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

            build_reference_question_bank(history, output, None)
            payload = output.read_text(encoding="utf-8")

        self.assertIn("key=REDACTED", payload)
        self.assertIn("token=REDACTED", payload)
        self.assertNotIn("secret-token", payload)
        self.assertNotIn("other-secret", payload)

    def test_shared_bank_privacy_guard_blocks_severe_secret_shapes(self):
        self.assertEqual(
            shared_bank_privacy_issues({"attempt": {"record_url": "https://tms/kexam/1/record?key=REDACTED&recordID=r1"}}),
            [],
        )
        issues = shared_bank_privacy_issues({"attempt": {"record_url": "https://tms/kexam/1/record?key=secret&recordID=r1"}})
        self.assertIn("secret_pattern:unredacted_query_secret", issues)
        issues = shared_bank_privacy_issues({"provenance": {"headers": {"Authorization": "Bearer secret"}}})
        self.assertIn("forbidden_key:provenance.headers", issues)

    def test_default_output_is_root_jsonl_without_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.jsonl"
            history.write_text(
                json.dumps(history_row("課後測驗", "verified_correct", answers=["A"]), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                result = build_reference_question_bank(history)
            finally:
                os.chdir(old_cwd)
            expected = dated_question_bank_filename()
            self.assertEqual(result.output_path, expected)
            self.assertIsNone(result.markdown_path)
            self.assertTrue((root / expected).exists())
            self.assertFalse((root / ".tms_private_exports" / "reference-question-bank.md").exists())

    def test_question_bank_loads_reference_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = root / "reference.jsonl"
            record = {
                "course": {"title": "Course"},
                "activity": {"title": "課後測驗"},
                "quiz_stage": "posttest",
                "question": {"text": "何者正確？", "options": ["A", "B"], "merge_key": "mk1"},
                "answer": {
                    "answers": ["B"],
                    "status": "ai_suggested_trusted",
                    "trusted_for_auto": True,
                    "confidence": 0.9,
                },
                "created_at": "2026-06-16T00:00:00+00:00",
            }
            reference.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            bank = QuestionBank.from_path(str(reference))
            entry = bank.match(QuizQuestion("何者正確？", ["A", "B"]), "Course", "課後測驗")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.answers, ["B"])
            self.assertTrue(entry.verified)

    def test_question_bank_latest_alias_loads_newest_dated_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "question-bank-20260101.jsonl"
            newer = root / "question-bank-20260201.jsonl"
            older.write_text(
                json.dumps(
                    {
                        "course": {"title": "Course"},
                        "activity": {"title": "課後測驗"},
                        "quiz_stage": "posttest",
                        "question": {"text": "何者正確？", "options": ["A", "B"], "merge_key": "old"},
                        "answer": {"answers": ["A"], "status": "verified_correct", "trusted_for_auto": True},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            newer.write_text(
                json.dumps(
                    {
                        "course": {"title": "Course"},
                        "activity": {"title": "課後測驗"},
                        "quiz_stage": "posttest",
                        "question": {"text": "何者正確？", "options": ["A", "B"], "merge_key": "new"},
                        "answer": {"answers": ["B"], "status": "ai_suggested_trusted", "trusted_for_auto": True},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.assertEqual(find_latest_question_bank_path(), Path("question-bank-20260201.jsonl"))
                bank = QuestionBank.from_path("latest")
            finally:
                os.chdir(old_cwd)
            entry = bank.match(QuizQuestion("何者正確？", ["A", "B"]), "Course", "課後測驗")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.answers, ["B"])

    def test_reference_markdown_contains_existing_question_bank_fields(self):
        text = render_reference_markdown(
            [
                {
                    "course": {"title": "Course"},
                    "activity": {"title": "課後測驗"},
                    "quiz_stage": "posttest",
                    "question": {"text": "何者正確？", "options": ["A", "B"]},
                    "answer": {"answers": ["B"], "status": "ai_suggested_trusted", "trusted_for_auto": True},
                }
            ]
        )
        self.assertIn("Question: 何者正確？", text)
        self.assertIn("Answer: B", text)


if __name__ == "__main__":
    unittest.main()
