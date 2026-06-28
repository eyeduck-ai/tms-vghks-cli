import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.cli import to_jsonable
from tms_vghks.playwright_kexam import (
    KExamExamPageReadResult,
    _collect_kexam_take_questions,
    _select_quiz_answers,
    build_kexam_resubmit_verification,
    parse_kexam_exam_page_html,
)
from tms_vghks.quiz import QuestionBank, QuestionBankEntry, QuizQuestion


def verified_record_probe(record_id: str, question_count: int = 2, selected_answer_count: int = 2) -> dict:
    return {
        "score": "80",
        "raw_summary": "80 2026-06-20 01:30 2026-06-20 01:31",
        "attempt": {"record_id": record_id, "score": "80", "submitted_status": "submitted"},
        "question_count": question_count,
        "selected_answer_count": selected_answer_count,
    }


class PlaywrightKExamTests(unittest.TestCase):
    def test_exam_page_parser_extracts_attempts_best_record_and_continue(self):
        html = """
        <section>
          <div>次數限制</div>
          <div>無限制 ( 已測驗 3 次，<a href="####">紀錄</a> )</div>
          <div>成績</div>
          <div>
            80
            <a href="/kexam/8267/record?key=secret-token&title=115%E5%B9%B4&recordID=938871">作答記錄</a>
          </div>
          <button>繼續測驗</button>
        </section>
        """
        parsed = parse_kexam_exam_page_html(html, "https://tms.vghks.gov.tw/course/5416/exam/10613")
        self.assertEqual(parsed.attempt_count, 3)
        self.assertIn("無限制", parsed.attempt_limit_text)
        self.assertTrue(parsed.continue_available)
        self.assertEqual(parsed.best_record_id, "938871")
        self.assertIn("/kexam/8267/record", parsed.best_record_url)
        self.assertIn("key=REDACTED", parsed.redacted_best_record_url)
        self.assertNotIn("secret-token", parsed.redacted_best_record_url)

    def test_exam_page_parser_keeps_continue_unavailable_when_button_missing(self):
        html = """
        <div>次數限制 無限制 ( 已測驗 0 次，[紀錄] )</div>
        <div>成績 --</div>
        """
        parsed = parse_kexam_exam_page_html(html, "https://tms.vghks.gov.tw/course/5416/exam/10613")
        self.assertEqual(parsed.attempt_count, 0)
        self.assertFalse(parsed.continue_available)
        self.assertEqual(parsed.best_record_url, "")

    def test_resubmit_verification_uses_new_record_ids_and_attempt_count(self):
        before = KExamExamPageReadResult(
            success=True,
            status="records_read",
            exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            redacted_exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            attempt_count=3,
            attempts=[
                {"record_id": "938869", "submitted_status": "submitted", "attempt_at": "2026-06-20 01:00"},
                {"record_id": "938870", "submitted_status": "submitted", "attempt_at": "2026-06-20 01:10"},
                {"record_id": "938871", "submitted_status": "submitted", "attempt_at": "2026-06-20 01:20"},
            ],
        )
        after = KExamExamPageReadResult(
            success=True,
            status="records_read",
            exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            redacted_exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            attempt_count=4,
            attempts=[
                {"record_id": "938869", "submitted_status": "submitted", "attempt_at": "2026-06-20 01:00"},
                {"record_id": "938870", "submitted_status": "submitted", "attempt_at": "2026-06-20 01:10"},
                {"record_id": "938871", "submitted_status": "submitted", "attempt_at": "2026-06-20 01:20"},
                {"record_id": "938999", "submitted_status": "submitted", "attempt_at": "2026-06-20 01:30"},
            ],
            record_probes=[verified_record_probe("938999")],
        )
        verification = build_kexam_resubmit_verification(before, after)
        self.assertEqual(verification["status"], "resubmit_verified")
        self.assertEqual(verification["before_attempt_count"], 3)
        self.assertEqual(verification["after_attempt_count"], 4)
        self.assertEqual(verification["new_record_ids"], ["938999"])
        self.assertEqual(verification["latest_submitted_attempt"]["record_id"], "938999")

    def test_resubmit_verification_reports_not_verified_without_record_change(self):
        before = KExamExamPageReadResult(
            success=True,
            status="records_read",
            exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            redacted_exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            attempt_count=3,
            best_record_id="938871",
            attempts=[{"record_id": "938871", "submitted_status": "submitted"}],
        )
        after = KExamExamPageReadResult(
            success=True,
            status="records_read",
            exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            redacted_exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            attempt_count=3,
            best_record_id="938871",
            attempts=[{"record_id": "938871", "submitted_status": "submitted"}],
        )
        verification = build_kexam_resubmit_verification(before, after)
        self.assertEqual(verification["status"], "submit_not_verified_by_record")
        self.assertEqual(verification["new_record_ids"], [])

    def test_resubmit_verification_rejects_best_record_change_without_probe(self):
        before = KExamExamPageReadResult(
            success=True,
            status="records_read",
            exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            redacted_exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            attempt_count=3,
            best_record_id="938871",
            attempts=[{"record_id": "938871", "submitted_status": "submitted"}],
        )
        after = KExamExamPageReadResult(
            success=True,
            status="records_read",
            exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            redacted_exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            attempt_count=3,
            best_record_id="938999",
            attempts=[{"record_id": "938999", "submitted_status": "submitted"}],
        )

        verification = build_kexam_resubmit_verification(before, after)

        self.assertEqual(verification["status"], "kexam_submit_not_verified")
        self.assertEqual(verification["verified_record_ids"], [])
        self.assertEqual(verification["unverified_record_ids"], ["938999"])
        self.assertIn("record_probe_missing:938999", verification["verification_issues"])

    def test_resubmit_verification_accepts_updated_existing_record(self):
        before = KExamExamPageReadResult(
            success=True,
            status="records_read",
            exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            redacted_exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            attempt_count=6,
            attempts=[
                {
                    "record_id": "950841",
                    "submitted_status": "unsubmitted",
                    "attempt_at": "2026-06-23 08:40:40",
                    "score": None,
                    "raw_summary": "6 - 2026-06-23 08:40:40 -",
                }
            ],
        )
        after = KExamExamPageReadResult(
            success=True,
            status="records_read",
            exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            redacted_exam_url="https://tms.vghks.gov.tw/course/5416/exam/10613",
            attempt_count=6,
            attempts=[
                {
                    "record_id": "950841",
                    "submitted_status": "submitted",
                    "attempt_at": "2026-06-23 08:40:40",
                    "score": "60",
                    "raw_summary": "6 60 2026-06-23 08:40:40 2026-06-23 08:42:32",
                }
            ],
            record_probes=[verified_record_probe("950841")],
        )

        verification = build_kexam_resubmit_verification(before, after)

        self.assertEqual(verification["status"], "resubmit_verified")
        self.assertEqual(verification["new_record_ids"], [])
        self.assertEqual(verification["updated_record_ids"], ["950841"])

    def test_jsonable_redacts_kexam_key_in_parse_payload(self):
        parsed = parse_kexam_exam_page_html(
            """
            <a href="/kexam/8267/record?key=secret-token&title=x&recordID=938871">作答記錄</a>
            <button>繼續測驗</button>
            """,
            "https://tms.vghks.gov.tw/course/5416/exam/10613",
        )
        payload = json.dumps(to_jsonable(parsed), ensure_ascii=False)
        self.assertIn("key=REDACTED", payload)
        self.assertNotIn("secret-token", payload)

    def test_select_quiz_answers_auto_uses_heuristic_for_unmatched_question(self):
        bank = QuestionBank(
            [
                QuestionBankEntry(
                    question="已收錄題目",
                    answers=["B"],
                    options=["A", "B"],
                    trusted_for_auto=True,
                )
            ]
        )
        questions = [
            QuizQuestion("已收錄題目", ["A", "B"], name="q1"),
            QuizQuestion("未收錄題目", ["A", "B"], name="q2"),
        ]

        result = _select_quiz_answers(bank, questions, "Course", "Quiz Item", "auto")

        self.assertEqual(result["selected"], {"q1": ["B"], "q2": ["A"]})
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["selected_answer_count"], 2)
        self.assertEqual(result["answer_sources"], {"q1": "question_bank", "q2": "heuristic"})

    def test_collect_kexam_take_questions_uses_payload_text_and_dom_name(self):
        class FakePage:
            url = "https://tms.vghks.gov.tw/kexam/8267/take?key=secret"

            def content(self):
                payload = {
                    "record": {"id": "950781", "timeUsed": 0},
                    "url": {},
                    "questionData": {
                        "47703": {
                            "id": 47703,
                            "type": 1,
                            "questionTitle": "<div>洋務運動中所生產的第一把步槍名稱為何？</div>",
                            "option": [
                                {"text": "M1871步槍"},
                                {"text": "M1898步槍"},
                                {"text": "漢陽造88式步槍"},
                            ],
                            "answer": [2],
                            "score": "20",
                        }
                    },
                }
                raw = json.dumps(payload, ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")
                return f"<script>fs.kexamTake.setData('#kexam-record', JSON.parse('{raw}'));</script>"

            def evaluate(self, script, kques_id):
                return f"kques_option_{kques_id}"

        questions = _collect_kexam_take_questions(FakePage(), "https://tms.vghks.gov.tw")

        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].name, "kques_option_47703")
        self.assertEqual(questions[0].text, "洋務運動中所生產的第一把步槍名稱為何？")
        self.assertEqual(questions[0].options[2], "C. 漢陽造88式步槍")


if __name__ == "__main__":
    unittest.main()
