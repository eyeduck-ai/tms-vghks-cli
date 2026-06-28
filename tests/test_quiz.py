import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.quiz import QuestionBank, QuestionBankEntry, QuizQuestion, suggest_answer
from tms_vghks.handlers import RunOptions, TmsRunner, missing_required_quiz_answers
from tms_vghks.models import CourseDetail, CourseItem, ItemKind, ItemState, OperationBackend


class MinimalRunnerSession:
    def configure_transient_policy(self, *args, **kwargs):
        return None

    def use_backend(self, backend):
        self.backend = backend

    def sync_cookies_to_requests(self):
        return None


class FakeQuizPage:
    def locator(self, selector):
        return self

    def inner_text(self, timeout=0):
        return "通過"


class QuizTests(unittest.TestCase):
    def test_question_bank_match_prefers_verified(self):
        bank = QuestionBank.from_markdown(
            """
            Question: 洗手時機為何？
            Options: 飯前 | 飯後 | 以上皆是
            Answer: 以上皆是
            Verified: false
            Score: 60

            Question: 洗手時機為何？
            Options: 飯前 | 飯後 | 以上皆是
            Answer: 以上皆是
            Verified: true
            Score: 100
            """
        )
        entry = bank.match(QuizQuestion("洗手時機為何？", ["飯前", "飯後", "以上皆是"]))
        self.assertIsNotNone(entry)
        self.assertTrue(entry.verified)

    def test_question_bank_match_allows_course_code_and_page_title_variants(self):
        bank = QuestionBank.from_markdown(
            """
            Course: 115年全民國防教育訓練 (110_26_318_A005_001)
            Item: 課前測驗 (成績比重: 0%)
            Question: 我國發展之第一款自製之防衛戰機名稱為何?
            Options: A. F-16戰機 | B. IDF戰機 | C. 幻象2000戰機
            Answer: B. IDF戰機
            Verified: true
            """
        )

        entry = bank.match(
            QuizQuestion(
                "我國發展之第一款自製之防衛戰機名稱為何?",
                ["A. F-16戰機", "B. IDF戰機", "C. 幻象2000戰機"],
            ),
            course="115年全民國防教育訓練 | 高雄榮民總醫院學習認證平台 TMS+",
            item="課前測驗",
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.answers, ["B. IDF戰機"])

    def test_question_bank_match_prefers_verified_exact_question_across_items(self):
        bank = QuestionBank(
            [
                QuestionBankEntry(
                    question="洋務運動中所生產的第一把步槍名稱為何？",
                    answers=["A. M1871步槍"],
                    course="115年全民國防教育訓練 (110_26_318_A005_001)",
                    item="課前測驗 (成績比重: 0%)",
                    options=["A. M1871步槍", "B. M1898步槍", "C. 漢陽造88式步槍"],
                    source_status="pretest_historical_selected",
                    trusted_for_auto=True,
                    score=80,
                    confidence=0.85,
                ),
                QuestionBankEntry(
                    question="洋務運動中所生產的第一把步槍名稱為何？",
                    answers=["C. 漢陽造88式步槍"],
                    course="115年全民國防教育訓練 (110_26_318_A005_001)",
                    item="課後測驗 (成績比重: 100%)",
                    options=["A. M1871步槍", "B. M1898步槍", "C. 漢陽造88式步槍"],
                    verified=True,
                    source_status="verified_correct",
                    trusted_for_auto=True,
                    score=100,
                    confidence=0.9,
                ),
            ]
        )

        entry = bank.match(
            QuizQuestion(
                "洋務運動中所生產的第一把步槍名稱為何？",
                ["A. M1871步槍", "B. M1898步槍", "C. 漢陽造88式步槍"],
            ),
            course="115年全民國防教育訓練 | 高雄榮民總醫院學習認證平台 TMS+",
            item="課前測驗",
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.answers, ["C. 漢陽造88式步槍"])

    def test_suggest_answer(self):
        question = QuizQuestion("下列何者正確？", ["A", "B", "以上皆是"])
        self.assertEqual(suggest_answer(question), ["以上皆是"])

    def test_playwright_quiz_auto_uses_heuristic_without_question_bank_answer(self):
        runner = TmsRunner(
            MinimalRunnerSession(),
            RunOptions(quiz_policy="auto", backend=OperationBackend.PLAYWRIGHT),
        )
        runner.question_bank = None
        course = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1")
        item = CourseItem(title="Quiz Item", kind=ItemKind.QUIZ)
        question = QuizQuestion("未收錄題目", ["A", "B"], name="q1")

        refreshed = CourseDetail(title="Course", url="https://tms.vghks.gov.tw/course/1", items=[item])
        page = FakeQuizPage()

        with (
            patch.object(TmsRunner, "_new_item_page", return_value=page),
            patch("tms_vghks.handlers.collect_quiz_questions", return_value=[question]),
            patch("tms_vghks.handlers.apply_quiz_answers") as apply_answers,
            patch("tms_vghks.handlers.missing_required_quiz_answers", return_value=[]),
            patch.object(TmsRunner, "_click_first_visible", return_value=True),
            patch.object(TmsRunner, "_recover_known_transient_dialog", return_value=None),
            patch.object(TmsRunner, "_get_course_detail", return_value=refreshed),
            patch.object(TmsRunner, "_extract_latest_kexam_record_summary", return_value={}),
        ):
            result = runner.run_quiz_playwright(course, item)

        self.assertTrue(result.success)
        self.assertEqual(result.state, ItemState.PASSED)
        apply_answers.assert_called_once_with(page, [question], {"q1": ["A"]})
        self.assertEqual(result.data["answer_sources"], {"q1": "heuristic"})

    def test_missing_required_quiz_answers_reports_unchecked_groups(self):
        class FakePage:
            def evaluate(self, script, names):
                self.names = names
                return ["q2"]

        page = FakePage()
        missing = missing_required_quiz_answers(
            page,
            [
                QuizQuestion("題目一", ["A", "B"], name="q1"),
                QuizQuestion("題目二", ["A", "B"], name="q2"),
            ],
        )
        self.assertEqual(page.names, ["q1", "q2"])
        self.assertEqual(missing, ["q2"])


if __name__ == "__main__":
    unittest.main()
