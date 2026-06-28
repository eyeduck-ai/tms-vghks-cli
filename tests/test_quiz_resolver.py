import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.quiz import QuestionBank, QuestionBankEntry, QuizQuestion
from tms_vghks.quiz_resolver import GeminiQuizClient, GeminiQuizConfig, resolve_quiz_answers


class FakeGeminiResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeGeminiClient:
    def __init__(self, answers=None, error=None):
        self.answers = answers or {}
        self.error = error
        self.calls = []

    def answer_question_indexes(self, questions, course_title, item_title):
        self.calls.append((questions, course_title, item_title))
        if self.error:
            raise self.error
        return self.answers


class QuizResolverTests(unittest.TestCase):
    def test_question_bank_match_does_not_call_gemini(self):
        bank = QuestionBank([QuestionBankEntry(question="何者正確？", answers=["B"], trusted_for_auto=True)])
        client = FakeGeminiClient(error=AssertionError("Gemini should not be called"))

        result = resolve_quiz_answers(
            questions=[QuizQuestion("何者正確？", ["A", "B"], name="q1")],
            course_title="Course",
            item_title="Quiz",
            question_bank=bank,
            quiz_policy="auto",
            gemini_config=GeminiQuizConfig(api_key="key"),
            gemini_client=client,
        )

        self.assertEqual(result.answers, {"q1": ["B"]})
        self.assertEqual(result.sources, {"q1": "question_bank"})
        self.assertEqual(client.calls, [])

    def test_gemini_batches_only_question_bank_misses(self):
        bank = QuestionBank([QuestionBankEntry(question="已收錄", answers=["B"], trusted_for_auto=True)])
        client = FakeGeminiClient(answers={"q1": [1]})

        result = resolve_quiz_answers(
            questions=[
                QuizQuestion("已收錄", ["A", "B"], name="known"),
                QuizQuestion("未收錄", ["C", "D"], name="unknown"),
            ],
            course_title="Course",
            item_title="Quiz",
            question_bank=bank,
            quiz_policy="auto",
            gemini_config=GeminiQuizConfig(api_key="key"),
            gemini_client=client,
        )

        self.assertEqual([question.name for question in client.calls[0][0]], ["unknown"])
        self.assertEqual(result.answers, {"known": ["B"], "unknown": ["D"]})
        self.assertEqual(result.sources, {"known": "question_bank", "unknown": "gemini"})

    def test_missing_gemini_key_falls_back_to_heuristic(self):
        result = resolve_quiz_answers(
            questions=[QuizQuestion("下列何者正確？", ["A", "以上皆是"], name="q1")],
            course_title="Course",
            item_title="Quiz",
            question_bank=None,
            quiz_policy="auto",
            gemini_config=GeminiQuizConfig(),
        )

        self.assertEqual(result.answers, {"q1": ["以上皆是"]})
        self.assertEqual(result.sources, {"q1": "heuristic"})
        self.assertEqual(result.issues, [])
        self.assertIn("gemini_api_key_missing", result.notes)

    def test_gemini_error_falls_back_to_heuristic(self):
        client = FakeGeminiClient(error=RuntimeError("boom"))

        result = resolve_quiz_answers(
            questions=[QuizQuestion("下列何者正確？", ["A", "以上皆是"], name="q1")],
            course_title="Course",
            item_title="Quiz",
            question_bank=None,
            quiz_policy="auto",
            gemini_config=GeminiQuizConfig(api_key="key"),
            gemini_client=client,
        )

        self.assertEqual(result.answers, {"q1": ["以上皆是"]})
        self.assertEqual(result.sources, {"q1": "heuristic"})
        self.assertTrue(any(issue.startswith("gemini_failed:") for issue in result.issues))

    def test_gemini_invalid_index_falls_back_per_question(self):
        client = FakeGeminiClient(answers={"q1": [99]})

        result = resolve_quiz_answers(
            questions=[QuizQuestion("下列何者正確？", ["A", "以上皆是"], name="q1")],
            course_title="Course",
            item_title="Quiz",
            question_bank=None,
            quiz_policy="auto",
            gemini_config=GeminiQuizConfig(api_key="key"),
            gemini_client=client,
        )

        self.assertEqual(result.answers, {"q1": ["以上皆是"]})
        self.assertEqual(result.sources, {"q1": "heuristic"})
        self.assertIn("gemini_invalid_answer:q1", result.issues)

    def test_gemini_client_posts_structured_generate_content_request(self):
        calls = []

        def fake_post(url, headers, json, timeout):
            calls.append((url, headers, json, timeout))
            text = json_module_dumps({"answers": [{"id": "q1", "option_indexes": [1]}]})
            return FakeGeminiResponse(
                payload={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": text}],
                            }
                        }
                    ]
                }
            )

        client = GeminiQuizClient(GeminiQuizConfig(api_key="secret", model="gemini-3.5-flash"), post_func=fake_post)

        result = client.answer_question_indexes([QuizQuestion("題目", ["A", "B"], name="q1")], "Course", "Quiz")

        self.assertEqual(result, {"q1": [1]})
        url, headers, payload, timeout = calls[0]
        self.assertIn("gemini-3.5-flash:generateContent", url)
        self.assertEqual(headers["X-goog-api-key"], "secret")
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("responseSchema", payload["generationConfig"])
        self.assertEqual(timeout, 30.0)

    def test_confirm_policy_does_not_use_gemini_or_heuristic(self):
        client = FakeGeminiClient(error=AssertionError("Gemini should not be called"))

        result = resolve_quiz_answers(
            questions=[QuizQuestion("未收錄", ["A", "以上皆是"], name="q1")],
            course_title="Course",
            item_title="Quiz",
            question_bank=None,
            quiz_policy="confirm",
            gemini_config=GeminiQuizConfig(api_key="key"),
            gemini_client=client,
        )

        self.assertEqual(result.answers, {})
        self.assertEqual(result.missing, ["q1"])
        self.assertEqual(client.calls, [])


def json_module_dumps(value):
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
