import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks import cli as cli_module
from tms_vghks.cli import build_parser
from tms_vghks.models import CourseDetail, CourseItem, CourseSummary, ItemKind, ItemState
from tms_vghks.quiz import QuestionBank, QuestionBankEntry
from tms_vghks.requests_form_submit import (
    build_quiz_payload,
    build_survey_payload,
    parse_activity_form_html,
    probe_form_submit_requests,
    resolve_activity_entry_url_requests,
    run_quiz_requests_submit,
    run_survey_requests_submit,
)
from tms_vghks.survey_text import DEFAULT_NEUTRAL_SURVEY_TEXT


COURSE_HTML = """
<ul id="activityTree">
  <li id="survey-node" class="xtree-node" data-id="survey-node">
    <span class="sn">1</span>
    <span class="fs-singleLineText">
      <a id="survey_btn" href="#" class="__button_survey"><span>Survey Item</span></a>
    </span>
  </li>
</ul>
<script>
$(".__button_survey").click(function(event) {
  fs.post('/ajax/sys.pages.course/checkPassPrevious/?itemID=1&ajaxAuth=secret', {}, function(o) {
    window.location.href = o.data.url;
  });
});
</script>
"""


SURVEY_HTML = """
<form action="/ajax/survey/submit?ajaxAuth=secret" method="post">
  <input type="hidden" name="anticsrf" value="token">
  <div class="question">
    <p>滿意度</p>
    <label><input type="radio" name="s1" value="bad">差</label>
    <label><input type="radio" name="s1" value="ok">普通</label>
    <label><input type="radio" name="s1" value="good">好</label>
  </div>
  <label>建議<textarea name="comment"></textarea></label>
  <button type="submit" name="submitBtn" value="1">送出</button>
</form>
"""


QUIZ_HTML = """
<form action="/ajax/quiz/submit?ajaxAuth=secret" method="post">
  <input type="hidden" name="anticsrf" value="token">
  <div class="question">
    <p>何者正確？</p>
    <label><input type="radio" name="q1" value="A">A 選項</label>
    <label><input type="radio" name="q1" value="B">B 選項</label>
  </div>
  <button type="submit">交卷</button>
</form>
"""


KEXAM_EXAM_HTML = """
<dl>
  <dt>次數限制</dt>
  <dd>無限制 ( 已測驗 1 次，<a href="####" data-url="/ajax/sys.modules.mod_kexamRecordList/list/?kexamID=8267&ajaxAuth=secret">紀錄</a> )</dd>
  <dt>成績</dt>
  <dd>80 <a href="/kexam/8267/record?key=best-secret&recordID=938871">作答記錄</a></dd>
</dl>
<script>
$(".__button").click(function(event){
  fs.post("/ajax/sys.pages.kexam_operate/operate/?id=8267&act=takeExam&redir=%2Fkexam%2F8267%2Ftake%3Fkey%3Dsecret&_lock=id%2Cact%2Credir&ajaxAuth=secret", {}, function(resp) {});
});
</script>
"""


KEXAM_RECORD_MODAL_HTML = """
<table><tbody>
  <tr>
    <td>1</td><td>80</td><td>2026-06-23 07:01:00</td><td>2026-06-23 07:01:30</td>
    <td><a href="/kexam/8267/record?recordID=938871&key=record-secret&show=0&title=x&from=record"></a></td>
  </tr>
</tbody></table>
"""


KEXAM_RECORD_HTML = """
<body class="body-layout-record">
  <div>分數: 80 / 100</div>
  <div class="question">
    <p>何者正確？</p>
    <label class="correct"><input type="radio" name="q1" checked disabled>A</label>
    <label><input type="radio" name="q1" disabled>B</label>
  </div>
</body>
"""


KEXAM_COURSE_TREE_HTML = """
<ul id="activityTree">
  <li id="quiz-node" class="xtree-node" data-id="quiz-node">
    <span class="sn">1</span>
    <span class="fs-singleLineText">
      <a id="quiz_btn" href="#" class="__button_quiz"><span>Quiz Item</span></a>
    </span>
  </li>
</ul>
<script>
$(".__button_quiz").click(function(event) {
  fs.post('/ajax/sys.pages.course/checkPassPrevious/?itemID=1&ajaxAuth=secret', {}, function(o) {
    window.location.href = o.data.url;
  });
});
</script>
"""


MISSING_ACTION_HTML = """
<form method="post">
  <input type="hidden" name="anticsrf" value="token">
  <p>review only</p>
</form>
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


class FakeFormSession:
    base_url = "https://tms.vghks.gov.tw"

    def __init__(self, item_kind=ItemKind.SURVEY, form_html=SURVEY_HTML, after_passed=True):
        self.item_kind = item_kind
        self.form_html = form_html
        self.after_passed = after_passed
        self.detail_calls = 0
        self.requests = []

    def get_course_detail(self, url_or_id):
        self.detail_calls += 1
        passed = self.after_passed and self.detail_calls > 1
        return CourseDetail(
            title="Course",
            url="https://tms.vghks.gov.tw/course/1",
            course_id="1",
            items=[
                CourseItem(
                    title="Survey Item" if self.item_kind == ItemKind.SURVEY else "Quiz Item",
                    order=1,
                    kind=self.item_kind,
                    state=ItemState.PASSED if passed else ItemState.PENDING,
                    pass_condition="須填寫" if self.item_kind == ItemKind.SURVEY else "60 分及格",
                    result="完成" if passed and self.item_kind == ItemKind.SURVEY else "100" if passed else "--",
                    passed_marker="通過" if passed else None,
                    metadata={"activity_tree_id": "survey-node", "activity_id": "survey-node"},
                )
            ],
        )

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        if "/entry" in path_or_url:
            return self.form_html
        return COURSE_HTML

    def _request_with_transient_retries(self, method, path_or_url, **kwargs):
        self.requests.append((method, path_or_url, kwargs.get("data")))
        if "checkPassPrevious" in path_or_url:
            return FakeResponse(
                "https://tms.vghks.gov.tw/ajax/sys.pages.course/checkPassPrevious/",
                payload={"status": True, "data": {"url": "/entry/form"}},
            )
        return FakeResponse(
            str(path_or_url),
            payload={"status": True, "ret": {"status": True}},
        )


class FakeAutoFormSession:
    base_url = "https://tms.vghks.gov.tw"

    def __init__(self, forms=None, include_quiz=True):
        self.forms = forms or {
            "survey": SURVEY_HTML,
            "quiz": QUIZ_HTML,
        }
        self.include_quiz = include_quiz
        self.requests = []

    def configure_transient_policy(self, *args, **kwargs):
        return None

    def list_completed_courses(self):
        return [CourseSummary(title="Course", detail_url="https://tms.vghks.gov.tw/course/1", completed=True)]

    def list_pending_courses(self):
        return []

    def get_course_detail(self, url_or_id):
        items = [
            CourseItem(
                title="Survey Review",
                order=1,
                kind=ItemKind.SURVEY,
                state=ItemState.PASSED,
                detail_url="https://tms.vghks.gov.tw/entry/survey-review",
                pass_condition="須填寫",
                result="完成",
                passed_marker="通過",
            ),
            CourseItem(
                title="Survey Item",
                order=2,
                kind=ItemKind.SURVEY,
                state=ItemState.PASSED,
                detail_url="https://tms.vghks.gov.tw/entry/survey",
                pass_condition="須填寫",
                result="完成",
                passed_marker="通過",
            ),
        ]
        if self.include_quiz:
            items.append(
                CourseItem(
                    title="Quiz Item",
                    order=3,
                    kind=ItemKind.QUIZ,
                    state=ItemState.PASSED,
                    detail_url="https://tms.vghks.gov.tw/entry/quiz",
                    pass_condition="60 分及格",
                    result="100",
                    passed_marker="通過",
                )
            )
        return CourseDetail(
            title="Course",
            url="https://tms.vghks.gov.tw/course/1",
            course_id="1",
            completed=True,
            items=items,
        )

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        if "survey-review" in path_or_url:
            return self.forms.get("survey-review", MISSING_ACTION_HTML)
        if "survey" in path_or_url:
            return self.forms.get("survey", SURVEY_HTML)
        if "quiz" in path_or_url:
            return self.forms.get("quiz", QUIZ_HTML)
        return ""

    def _request_with_transient_retries(self, method, path_or_url, **kwargs):
        self.requests.append((method, path_or_url, kwargs.get("data")))
        return FakeResponse(
            str(path_or_url),
            payload={"status": True, "ret": {"status": True}},
        )


class FakeReviewThenEntrySession(FakeFormSession):
    def get_course_detail(self, url_or_id):
        detail = super().get_course_detail(url_or_id)
        detail.items[0].detail_url = "https://tms.vghks.gov.tw/entry/review"
        return detail

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        if "review" in path_or_url:
            return MISSING_ACTION_HTML
        if "/entry/form" in path_or_url:
            return self.form_html
        return COURSE_HTML


class FakeKExamProbeSession(FakeFormSession):
    def get_course_detail(self, url_or_id):
        detail = super().get_course_detail(url_or_id)
        detail.items[0].detail_url = "https://tms.vghks.gov.tw/course/5416/exam/10613"
        return detail

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        if "/course/5416/exam/10613" in path_or_url:
            return KEXAM_EXAM_HTML
        if "mod_kexamRecordList" in path_or_url:
            return KEXAM_RECORD_MODAL_HTML
        if "/kexam/8267/record" in path_or_url:
            return KEXAM_RECORD_HTML
        return super().fetch_activity_html_requests(path_or_url, referer=referer)


class FakeKExamCourseTreeProbeSession(FakeKExamProbeSession):
    def get_course_detail(self, url_or_id):
        detail = super().get_course_detail(url_or_id)
        detail.items[0].detail_url = ""
        return detail

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        if str(path_or_url).rstrip("/") == "https://tms.vghks.gov.tw/course/1":
            return KEXAM_COURSE_TREE_HTML
        return super().fetch_activity_html_requests(path_or_url, referer=referer)

    def _request_with_transient_retries(self, method, path_or_url, **kwargs):
        self.requests.append((method, path_or_url, kwargs.get("data")))
        if "checkPassPrevious" in path_or_url:
            return FakeResponse(
                "https://tms.vghks.gov.tw/ajax/sys.pages.course/checkPassPrevious/",
                payload={"status": True, "data": {"url": "/course/5416/exam/10613"}},
            )
        return super()._request_with_transient_retries(method, path_or_url, **kwargs)


class RequestsFormSubmitTests(unittest.TestCase):
    def test_parse_form_and_build_neutral_survey_payload(self):
        form = parse_activity_form_html(
            SURVEY_HTML,
            "https://tms.vghks.gov.tw/entry/form",
            "https://tms.vghks.gov.tw",
            ItemKind.SURVEY,
        )

        self.assertEqual(form.method, "POST")
        self.assertIn("/ajax/survey/submit", form.action_url or "")
        self.assertEqual(form.field_summary["hidden_fields"], ["anticsrf"])
        self.assertEqual(len(form.questions), 1)

        built = build_survey_payload(form, DEFAULT_NEUTRAL_SURVEY_TEXT)

        self.assertIn(("anticsrf", "token"), built.payload)
        self.assertIn(("s1", "ok"), built.payload)
        self.assertIn(("comment", "無"), built.payload)
        self.assertFalse(any("管理員" in value or "管理者" in value or "測試" in value for _, value in built.payload))
        self.assertIn("anticsrf", built.payload_keys)
        self.assertIn("s1", built.payload_keys)

    def test_resolve_entry_url_from_course_tree_check_pass_previous(self):
        session = FakeFormSession()
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0

        url, issues = resolve_activity_entry_url_requests(session, course, item)

        self.assertEqual(issues, [])
        self.assertEqual(url, "https://tms.vghks.gov.tw/entry/form")
        self.assertTrue(any("checkPassPrevious" in request[1] for request in session.requests))

    def test_survey_submit_success_when_course_detail_verifies(self):
        session = FakeFormSession(item_kind=ItemKind.SURVEY, after_passed=True)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0

        result = run_survey_requests_submit(session, course, item)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_survey_submit_verified")
        self.assertIn("ajaxAuth=REDACTED", result.form_action_url)
        self.assertIn("anticsrf", result.payload_keys)
        self.assertIn("s1", result.payload_keys)
        submitted = [request[2] for request in session.requests if "/ajax/survey/submit" in request[1]][0]
        self.assertIn(("comment", "無"), submitted)
        self.assertTrue(any("/ajax/survey/submit" in request[1] for request in session.requests))

    def test_survey_submit_does_not_require_allow_submit(self):
        session = FakeFormSession(item_kind=ItemKind.SURVEY, after_passed=True)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0

        result = run_survey_requests_submit(session, course, item)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_survey_submit_verified")
        self.assertTrue(any("/ajax/survey/submit" in request[1] for request in session.requests))

    def test_review_detail_url_falls_back_to_course_tree_entry(self):
        session = FakeReviewThenEntrySession(item_kind=ItemKind.SURVEY, after_passed=True)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0

        result = run_survey_requests_submit(session, course, item)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_survey_submit_verified")
        self.assertIn("course_tree_entry_fallback", result.issues)
        self.assertTrue(any("checkPassPrevious" in request[1] for request in session.requests))
        self.assertTrue(any("/ajax/survey/submit" in request[1] for request in session.requests))

    def test_survey_submit_without_detail_verification_is_not_verified(self):
        session = FakeFormSession(item_kind=ItemKind.SURVEY, after_passed=False)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0

        result = run_survey_requests_submit(session, course, item)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "form_submit_not_verified")

    def test_survey_submit_uses_fixed_neutral_text_without_forbidden_gate(self):
        session = FakeFormSession(item_kind=ItemKind.SURVEY, after_passed=True)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0

        result = run_survey_requests_submit(session, course, item)

        self.assertTrue(result.success)
        submitted = [request[2] for request in session.requests if "/ajax/survey/submit" in request[1]][0]
        self.assertIn(("comment", "無"), submitted)
        self.assertNotIn("neutral_survey_text_forbidden", ",".join(result.issues))

    def test_missing_form_action_is_endpoint_unverified(self):
        html = "<form method='post'><input type='hidden' name='anticsrf' value='token'></form>"
        session = FakeFormSession(item_kind=ItemKind.SURVEY, form_html=html)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0

        result = run_survey_requests_submit(session, course, item)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "form_endpoint_unverified")
        self.assertIn("form_action_missing", result.issues)

    def test_quiz_payload_uses_question_bank_answer(self):
        form = parse_activity_form_html(
            QUIZ_HTML,
            "https://tms.vghks.gov.tw/entry/form",
            "https://tms.vghks.gov.tw",
            ItemKind.QUIZ,
        )
        bank = QuestionBank(
            [
                QuestionBankEntry(
                    question="何者正確？",
                    answers=["B 選項"],
                    options=["A 選項", "B 選項"],
                    trusted_for_auto=True,
                )
            ]
        )

        built = build_quiz_payload(form, "Course", "Quiz Item", bank, "confirm")

        self.assertEqual(built.missing_required, [])
        self.assertIn(("q1", "B"), built.payload)

    def test_quiz_submit_success_with_known_answer(self):
        session = FakeFormSession(item_kind=ItemKind.QUIZ, form_html=QUIZ_HTML, after_passed=True)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0
        bank = QuestionBank([QuestionBankEntry(question="何者正確？", answers=["B 選項"], trusted_for_auto=True)])

        result = run_quiz_requests_submit(session, course, item, bank, quiz_policy="confirm")

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_quiz_submit_course_detail_only")
        self.assertEqual(result.verification_strength, "course_detail")
        self.assertEqual(result.verification_method, "course_detail_item_passed")
        self.assertIn("q1", result.payload_keys)

    def test_quiz_missing_answer_is_required_field_failure(self):
        session = FakeFormSession(item_kind=ItemKind.QUIZ, form_html=QUIZ_HTML, after_passed=True)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0

        result = run_quiz_requests_submit(session, course, item, None, quiz_policy="confirm")

        self.assertFalse(result.success)
        self.assertEqual(result.status, "form_missing_required_fields")
        self.assertFalse(any("/ajax/quiz/submit" in request[1] for request in session.requests))

    def test_quiz_auto_without_question_bank_uses_heuristic_answer(self):
        session = FakeFormSession(item_kind=ItemKind.QUIZ, form_html=QUIZ_HTML, after_passed=True)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0

        result = run_quiz_requests_submit(session, course, item, None, quiz_policy="auto")

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_quiz_submit_course_detail_only")
        self.assertEqual(result.answer_sources, {"q1": "heuristic"})
        self.assertIn("gemini_api_key_missing", result.answer_resolution_notes)
        self.assertNotIn("answer_resolution:gemini_api_key_missing", result.issues)
        self.assertTrue(any("/ajax/quiz/submit" in request[1] for request in session.requests))

    def test_quiz_auto_unmatched_question_bank_uses_heuristic_answer(self):
        session = FakeFormSession(item_kind=ItemKind.QUIZ, form_html=QUIZ_HTML, after_passed=True)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]
        session.detail_calls = 0
        bank = QuestionBank([QuestionBankEntry(question="其他題目", answers=["B 選項"], trusted_for_auto=True)])

        result = run_quiz_requests_submit(session, course, item, bank, quiz_policy="auto")

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_quiz_submit_course_detail_only")
        self.assertEqual(result.answer_sources, {"q1": "heuristic"})
        self.assertTrue(any("/ajax/quiz/submit" in request[1] for request in session.requests))

    def test_probe_only_does_not_trigger_kexam_submit_fast_path(self):
        session = FakeKExamProbeSession(item_kind=ItemKind.QUIZ, form_html=QUIZ_HTML)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]

        result = probe_form_submit_requests(session, course, item)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "mutation_unsupported")
        self.assertFalse(any("submitExam" in request[1] or "/ajax/quiz/submit" in request[1] for request in session.requests))
        self.assertTrue(result.form_summary["kexam"])
        self.assertEqual(result.method, "GET")
        self.assertEqual(result.kexam_record_probe_summary["record_count"], 1)
        self.assertEqual(result.question_count, 1)
        self.assertIn("kexam_read_only_probe", result.issues)

    def test_probe_only_resolves_kexam_entry_from_course_tree_without_submit(self):
        session = FakeKExamCourseTreeProbeSession(item_kind=ItemKind.QUIZ, form_html=QUIZ_HTML)
        course = session.get_course_detail("https://tms.vghks.gov.tw/course/1")
        item = course.items[0]

        result = probe_form_submit_requests(session, course, item)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "mutation_unsupported")
        self.assertTrue(result.form_summary["kexam"])
        self.assertEqual(result.entry_attempts[0]["source"], "course_tree")
        self.assertTrue(any("checkPassPrevious" in request[1] for request in session.requests))
        self.assertFalse(any("submitExam" in request[1] for request in session.requests))

    def test_cli_parser_accepts_auto_requests_form_submit_diagnostics(self):
        args = build_parser().parse_args(
            [
                "diag",
                "forms",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
            ]
        )

        self.assertEqual(args.command, "requests-form-submit-diagnostics")
        self.assertIsNone(args.course)
        self.assertIsNone(args.item_order)
        self.assertEqual(args.kind, "both")
        self.assertEqual(args.scope, "completed")
        self.assertEqual(args.quiz, "auto")
        self.assertFalse(hasattr(args, "neutral_survey_text"))
        self.assertFalse(hasattr(args, "allow_submit"))
        self.assertEqual(args.candidate_limit, 1)

    def test_cli_parser_accepts_explicit_requests_form_submit_diagnostics(self):
        args = build_parser().parse_args(
            [
                "diag",
                "forms",
                "--course",
                "1",
                "--item-order",
                "2",
                "--kind",
                "survey",
                "--probe-only",
                "--candidate-limit",
                "3",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
            ]
        )

        self.assertEqual(args.command, "requests-form-submit-diagnostics")
        self.assertEqual(args.course, "1")
        self.assertEqual(args.item_order, 2)
        self.assertEqual(args.kind, "survey")
        self.assertFalse(hasattr(args, "allow_submit"))
        self.assertTrue(args.probe_only)
        self.assertEqual(args.candidate_limit, 3)

    def test_auto_diagnostic_selects_completed_survey_and_quiz_without_allow_submit(self):
        session = FakeAutoFormSession(forms={"survey-review": MISSING_ACTION_HTML})
        args = build_parser().parse_args(["diag", "forms"])

        result = cli_module._run_requests_form_submit_diagnostic(session, args)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_form_submit_verified")
        self.assertEqual(result.requested_kinds, ["survey", "quiz"])
        statuses = [row.status if hasattr(row, "status") else row["status"] for row in result.results]
        self.assertEqual(statuses, ["requests_survey_submit_verified", "requests_quiz_submit_course_detail_only"])
        self.assertEqual(result.skipped_candidates[0]["item_title"], "Survey Review")
        self.assertEqual(result.skipped_candidates[0]["status"], "form_endpoint_unverified")
        self.assertTrue(any("/ajax/survey/submit" in request[1] for request in session.requests))
        self.assertTrue(any("/ajax/quiz/submit" in request[1] for request in session.requests))

    def test_auto_probe_only_diagnostic_does_not_submit_completed_forms(self):
        session = FakeAutoFormSession(forms={"survey-review": MISSING_ACTION_HTML})
        args = build_parser().parse_args(["diag", "forms", "--probe-only"])

        result = cli_module._run_requests_form_submit_diagnostic(session, args)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_form_probe_completed")
        self.assertEqual([row.status for row in result.results], ["mutation_unsupported", "mutation_unsupported"])
        self.assertFalse(any("/ajax/survey/submit" in request[1] for request in session.requests))
        self.assertFalse(any("/ajax/quiz/submit" in request[1] for request in session.requests))
        self.assertIn("form_action_missing", result.results[0].issues)
        self.assertEqual(result.skipped_candidates, [])

    def test_auto_probe_only_candidate_limit_scans_multiple_completed_candidates(self):
        session = FakeAutoFormSession(forms={"survey-review": MISSING_ACTION_HTML, "survey": SURVEY_HTML})
        args = build_parser().parse_args(
            [
                "diag",
                "forms",
                "--probe-only",
                "--kind",
                "survey",
                "--candidate-limit",
                "2",
            ]
        )

        result = cli_module._run_requests_form_submit_diagnostic(session, args)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_form_probe_completed")
        self.assertEqual([row.item_order for row in result.results], [1, 2])
        self.assertEqual([row.status for row in result.results], ["mutation_unsupported", "mutation_unsupported"])
        self.assertFalse(any("/ajax/survey/submit" in request[1] for request in session.requests))
        self.assertIn("form_action_missing", result.results[0].issues)
        self.assertEqual(result.results[1].form_summary["method"], "POST")
        self.assertIn("anticsrf", result.results[1].form_summary["hidden_fields"])

    def test_auto_diagnostic_reports_no_candidate(self):
        session = FakeAutoFormSession(include_quiz=False)
        args = build_parser().parse_args(["diag", "forms", "--kind", "quiz"])

        result = cli_module._run_requests_form_submit_diagnostic(session, args)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "no_form_submit_candidate")
        self.assertEqual(result.results[0]["status"], "no_form_submit_candidate")


if __name__ == "__main__":
    unittest.main()
