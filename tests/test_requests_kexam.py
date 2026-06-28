import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.kexam_common import parse_kexam_exam_page_html
from tms_vghks.cli import build_parser
from tms_vghks.models import CourseDetail, CourseItem, ItemKind, ItemState
from tms_vghks.playwright_probe import KExamAttempt
from tms_vghks.quiz import QuestionBank, QuestionBankEntry
from tms_vghks.requests_kexam import (
    build_kexam_submit_payload,
    parse_kexam_take_page_html,
    read_kexam_exam_page_requests,
    run_requests_kexam_resubmit_diagnostic,
    try_read_kexam_requests_probe,
    try_run_kexam_requests_submit,
)
from tms_vghks.requests_probe import probe_kexam_attempt_requests


EXAM_URL = "https://tms.vghks.gov.tw/course/5416/exam/10613"
TAKE_URL = (
    "https://tms.vghks.gov.tw/kexam/8267/take?key=secret&title=x&backUrl=%2Fcourse%2F5416%2Fexam%2F10613"
    "&backAppUrl=%2Fcourse%2F5416&ownerID=5416&ownerType=course&userID=11078&examID=10613&redirKey=redir"
)
OPERATE_URL = (
    "/ajax/sys.pages.kexam_operate/operate/?id=8267&act=takeExam&redir=%2Fkexam%2F8267%2Ftake%3Fkey%3Dsecret"
    "%26title%3Dx%26redirKey%3Dredir&modalId=checkCodeModalId&_lock=id%2Cact%2Credir%2CmodalId&ajaxAuth=secret"
)
RECORD_MODAL_URL = (
    "/ajax/sys.modules.mod_kexamRecordList/list/?kexamID=8267&userId=11078&title=%E8%AA%B2%E5%89%8D%E6%B8%AC%E9%A9%97"
    "&key=secret&_lock=kexamID%2CuserId%2Ctitle%2Ckey&ajaxAuth=secret"
)


def exam_html(attempt_count=5, include_operate=True):
    operate_script = (
        f"""
        <script>
        $(".__button").click(function(event){{
          fs.post("{OPERATE_URL}", {{}}, function(resp) {{ fs.respHandler(resp); }});
        }});
        </script>
        """
        if include_operate
        else ""
    )
    return f"""
    <dl>
      <dt>次數限制</dt>
      <dd>無限制 ( 已測驗 {attempt_count} 次，<a href="####" data-url="{RECORD_MODAL_URL}">紀錄</a> )</dd>
      <dt>成績</dt>
      <dd>80 <a href="/kexam/8267/record?key=best-secret&recordID=938871">作答記錄</a></dd>
    </dl>
    <button class="__button">繼續測驗</button>
    {operate_script}
    """


def record_modal_html(record_ids):
    rows = []
    for index, record_id in enumerate(record_ids, start=1):
        rows.append(
            f"""
            <tr>
              <td>{index}</td><td>80</td><td>2026-06-23 07:{index:02d}:00</td><td>2026-06-23 07:{index:02d}:30</td>
              <td><a href="/kexam/8267/record?recordID={record_id}&key=record-secret&show=0&title=x&from=record"></a></td>
            </tr>
            """
        )
    return "<table><tbody>" + "\n".join(rows) + "</tbody></table>"


def record_html(record_id):
    return f"""
    <body class="body-layout-record">
      <div>分數: 80 / 100</div>
      <div class="question">
        <p>何者正確？</p>
        <label class="correct"><input type="radio" name="q1" checked disabled>A</label>
        <label><input type="radio" name="q1" disabled>B</label>
      </div>
      <div class="question">
        <p>我國發展之第一款自製之防衛戰機名稱為何?</p>
        <label><input type="radio" name="q2" disabled>A. F-16戰機</label>
        <label class="correct"><input type="radio" name="q2" checked disabled>B. IDF戰機</label>
        <label><input type="radio" name="q2" disabled>C. 幻象2000戰機</label>
      </div>
      <span>{record_id}</span>
    </body>
    """


def blank_record_html(record_id):
    return f"""
    <body class="body-layout-record">
      <div>分數: 0 / 100</div>
      <div class="question">
        <p>何者正確？</p>
        <label><input type="radio" name="q1" disabled>A</label>
        <label><input type="radio" name="q1" disabled>B</label>
      </div>
      <div class="question">
        <p>我國發展之第一款自製之防衛戰機名稱為何?</p>
        <label><input type="radio" name="q2" disabled>A. F-16戰機</label>
        <label><input type="radio" name="q2" disabled>B. IDF戰機</label>
        <label><input type="radio" name="q2" disabled>C. 幻象2000戰機</label>
      </div>
      <span>{record_id}</span>
    </body>
    """


def record_json_html(record_id="938871"):
    payload = {
        "record": {"id": record_id, "score": "80", "submitTime": "2026-06-23 07:01:30"},
        "questionData": {
            "47699": {
                "id": 47699,
                "type": 0,
                "questionTitle": "<div>「技術落後就要挨打」提醒我們要不斷更新武器系統。</div>",
                "option": ["是", "否"],
                "answer": 0,
                "score": "20",
                "record": {"userAnswer": "{\"answer\":0}", "isCorrect": 1, "getScore": 20},
            },
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
                "record": {"userAnswer": "{\"answer\":0}", "isCorrect": 0, "getScore": 0},
            },
        },
    }
    raw = json.dumps(payload, ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")
    return f"<script>fs.kexamRecord.setData('#kexam-record', JSON.parse('{raw}'));</script>"


def take_payload():
    return {
        "record": {"id": "950781", "timeUsed": 0},
        "url": {
            "confirmRecord": "/ajax/sys.app.kexam/confirmRecord/?recordID=950781&_lock=recordID&ajaxAuth=secret",
            "submitExam": "/ajax/sys.app.kexam/submitExam/?recordId=950781&redirKey=redir&_lock=recordId%2CredirKey&ajaxAuth=secret",
            "submittedRedir": "/kexam/8267/record?recordID=950781&from=take&key=secret&title=x",
        },
        "questionData": {
            "47699": {
                "id": 47699,
                "type": 0,
                "questionTitle": "<div>「技術落後就要挨打」提醒我們要不斷更新武器系統。</div>",
                "option": ["是", "否"],
                "answer": 0,
                "score": "20",
                "optOrder": None,
            },
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
                "optOrder": None,
            },
        },
    }


def take_html():
    raw = json.dumps(take_payload(), ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")
    return f"<script>fs.kexamTake.setData('#kexam-record', JSON.parse('{raw}'));</script>"


def kexam_question_bank(include_first=True, include_second=True):
    entries = []
    if include_first:
        entries.append(
            QuestionBankEntry(
                question="「技術落後就要挨打」提醒我們要不斷更新武器系統。",
                answers=["是"],
                options=["是", "否"],
                trusted_for_auto=True,
            )
        )
    if include_second:
        entries.append(
            QuestionBankEntry(
                question="我國發展之第一款自製之防衛戰機名稱為何?",
                answers=["B. IDF戰機"],
                options=["A. F-16戰機", "B. IDF戰機", "C. 幻象2000戰機"],
                trusted_for_auto=True,
            )
        )
    return QuestionBank(entries)


class FakeResponse:
    def __init__(self, url, status_code=200, payload=None, text=""):
        self.url = url
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeKExamSession:
    base_url = "https://tms.vghks.gov.tw"

    def __init__(self, include_operate=True, after_passed=True):
        self.include_operate = include_operate
        self.after_passed = after_passed
        self.submitted = False
        self.fetches = []
        self.requests = []

    def get_course_detail(self, url_or_id):
        state = ItemState.PASSED if self.after_passed or not self.submitted else ItemState.PENDING
        return CourseDetail(
            title="115年全民國防教育訓練",
            url="https://tms.vghks.gov.tw/course/5416",
            course_id="5416",
            items=[
                CourseItem(
                    title="課前測驗",
                    kind=ItemKind.QUIZ,
                    state=state,
                    detail_url=EXAM_URL,
                )
            ],
        )

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        url = str(path_or_url)
        self.fetches.append(url)
        if "/course/5416/exam/10613" in url:
            return exam_html(6 if self.submitted else 5, include_operate=self.include_operate)
        if "mod_kexamRecordList" in url:
            ids = ["938871", "942191", "950763", "950781", "950782"]
            if self.submitted:
                ids.append("950999")
            return record_modal_html(ids)
        if "/kexam/8267/take" in url:
            return take_html()
        if "/kexam/8267/record" in url:
            return record_html(url)
        return ""

    def _request_with_transient_retries(self, method, path_or_url, **kwargs):
        self.requests.append((method, str(path_or_url), kwargs.get("data")))
        if "submitExam" in str(path_or_url):
            self.submitted = True
        return FakeResponse(str(path_or_url), payload={"status": True, "ret": {"status": "true"}})


class FakeKExamSubmit500Session(FakeKExamSession):
    def _request_with_transient_retries(self, method, path_or_url, **kwargs):
        self.requests.append((method, str(path_or_url), kwargs.get("data")))
        if "submitExam" in str(path_or_url):
            self.submitted = True
            return FakeResponse(str(path_or_url), status_code=500, payload={"ret": {"status": False}})
        return FakeResponse(str(path_or_url), payload={"status": True, "ret": {"status": "true"}})


class FakeKExamSubmit500BlankRecordSession(FakeKExamSubmit500Session):
    def fetch_activity_html_requests(self, path_or_url, referer=None):
        url = str(path_or_url)
        self.fetches.append(url)
        if "/course/5416/exam/10613" in url:
            return exam_html(6 if self.submitted else 5, include_operate=self.include_operate)
        if "mod_kexamRecordList" in url:
            ids = ["938871", "942191", "950763", "950781", "950782"]
            if self.submitted:
                ids.append("950999")
            return record_modal_html(ids)
        if "/kexam/8267/take" in url:
            return take_html()
        if "/kexam/8267/record" in url and "950999" in url:
            return blank_record_html(url)
        if "/kexam/8267/record" in url:
            return record_html(url)
        return ""


class FakeKExamNoRecordChangeSession(FakeKExamSession):
    def fetch_activity_html_requests(self, path_or_url, referer=None):
        url = str(path_or_url)
        self.fetches.append(url)
        if "/course/5416/exam/10613" in url:
            return exam_html(5, include_operate=self.include_operate)
        if "mod_kexamRecordList" in url:
            return record_modal_html(["938871", "942191", "950763", "950781", "950782"])
        if "/kexam/8267/take" in url:
            return take_html()
        if "/kexam/8267/record" in url:
            return record_html(url)
        return ""


class FakeKExamRecordJsonSession:
    base_url = "https://tms.vghks.gov.tw"

    def fetch_activity_html_requests(self, path_or_url, referer=None):
        return record_json_html("938871")


class FakeGeminiClient:
    def answer_question_indexes(self, questions, course_title, item_title):
        return {"q1": [0] for _question in questions}


class RequestsKExamTests(unittest.TestCase):
    def test_exam_page_parser_extracts_record_modal_and_take_operate_url(self):
        parsed = parse_kexam_exam_page_html(exam_html(), EXAM_URL)

        self.assertEqual(parsed.attempt_count, 5)
        self.assertIn("mod_kexamRecordList", parsed.record_modal_url)
        self.assertIn("act=takeExam", parsed.take_operate_url)
        self.assertIn("/kexam/8267/take", parsed.take_url)
        self.assertIn("ajaxAuth=REDACTED", parsed.redacted_take_operate_url)
        self.assertNotIn("secret", parsed.redacted_take_operate_url)

    def test_take_page_parser_extracts_endpoints_and_questions(self):
        parsed = parse_kexam_take_page_html(take_html(), TAKE_URL, "https://tms.vghks.gov.tw")

        self.assertEqual(parsed.record_id, "950781")
        self.assertIn("confirmRecord", parsed.confirm_record_url)
        self.assertIn("submitExam", parsed.submit_exam_url)
        self.assertEqual(len(parsed.questions), 2)
        self.assertEqual(parsed.questions[1].display_options[1], "B. IDF戰機")

    def test_payload_builder_uses_complete_question_bank_for_auto_answers(self):
        parsed = parse_kexam_take_page_html(take_html(), TAKE_URL, "https://tms.vghks.gov.tw")
        bank = kexam_question_bank()

        built = build_kexam_submit_payload(parsed, "115年全民國防教育訓練", "課前測驗", bank, "auto")
        question_data = json.loads(built.payload["questionData"])

        self.assertEqual(built.missing_required, [])
        self.assertEqual(built.selected_answer_count, 2)
        self.assertEqual(question_data[0]["userAnswer"], '{"answer":0}')
        self.assertNotIn("optOrder", question_data[0])
        self.assertEqual(question_data[1]["userAnswer"], '{"answer":[1]}')
        self.assertEqual(question_data[1]["optOrder"], "[0,1,2]")
        self.assertEqual(question_data[1]["isCorrect"], 1)
        self.assertIn("questionData", built.payload_keys)
        self.assertIn("forceType", built.payload_keys)

    def test_payload_builder_auto_uses_heuristic_for_missing_question_bank_answer(self):
        parsed = parse_kexam_take_page_html(take_html(), TAKE_URL, "https://tms.vghks.gov.tw")
        bank = kexam_question_bank(include_first=False)

        built = build_kexam_submit_payload(parsed, "115年全民國防教育訓練", "課前測驗", bank, "auto")

        self.assertEqual(built.missing_required, [])
        self.assertEqual(built.selected_answer_count, 2)
        self.assertEqual(built.answer_sources, {"47699": "heuristic", "47702": "question_bank"})

    def test_payload_builder_auto_uses_gemini_before_heuristic_for_missing_question_bank_answer(self):
        parsed = parse_kexam_take_page_html(take_html(), TAKE_URL, "https://tms.vghks.gov.tw")
        bank = kexam_question_bank(include_first=False)

        built = build_kexam_submit_payload(
            parsed,
            "115年全民國防教育訓練",
            "課前測驗",
            bank,
            "auto",
            gemini_client=FakeGeminiClient(),
        )

        self.assertEqual(built.missing_required, [])
        self.assertEqual(built.selected_answer_count, 2)
        self.assertEqual(built.answer_sources, {"47699": "gemini", "47702": "question_bank"})

    def test_read_kexam_records_requests_follows_modal_and_record_pages(self):
        session = FakeKExamSession()

        result = read_kexam_exam_page_requests(session, EXAM_URL)

        self.assertTrue(result.success)
        self.assertEqual(result.attempt_count, 5)
        self.assertEqual(result.record_count, 5)
        self.assertIn("ajaxAuth=REDACTED", result.record_modal_url)
        self.assertGreaterEqual(result.record_question_count, 5)

    def test_try_read_kexam_probe_is_read_only(self):
        session = FakeKExamSession()
        item = CourseItem(title="課前測驗", kind=ItemKind.QUIZ, detail_url=EXAM_URL)

        result = try_read_kexam_requests_probe(session, item)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.success)
        self.assertEqual(result.status, "records_read")
        self.assertEqual(result.record_count, 5)
        self.assertEqual(session.requests, [])

    def test_requests_kexam_record_probe_parses_json_only_record_page(self):
        attempt = KExamAttempt(
            exam_id="8267",
            record_id="938871",
            record_url="https://tms.vghks.gov.tw/kexam/8267/record?recordID=938871&key=secret",
            redacted_record_url="https://tms.vghks.gov.tw/kexam/8267/record?recordID=938871&key=REDACTED",
            score="80",
            submitted_status="submitted",
        )

        probe = probe_kexam_attempt_requests(FakeKExamRecordJsonSession(), attempt)

        self.assertEqual(probe.availability, "available")
        self.assertEqual(probe.score, "80")
        self.assertEqual(len(probe.question_records), 2)
        self.assertEqual(probe.question_records[0].selected_answers, ["是"])
        self.assertEqual(probe.question_records[1].options[1], "B. IDF戰機")
        self.assertEqual(probe.question_records[1].correct_answers, ["B. IDF戰機"])
        self.assertEqual(probe.question_records[1].incorrect_answers, ["A. F-16戰機"])

    def test_requests_kexam_resubmit_verifies_new_record(self):
        session = FakeKExamSession()

        result = run_requests_kexam_resubmit_diagnostic(
            session,
            course="5416",
            exam_url=EXAM_URL,
            quiz_policy="auto",
            question_bank_path=None,
            question_bank=kexam_question_bank(),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "resubmit_verified")
        self.assertEqual(result.before_attempt_count, 5)
        self.assertEqual(result.after_attempt_count, 6)
        self.assertEqual(result.new_record_ids, ["950999"])
        self.assertTrue(any("confirmRecord" in request[1] for request in session.requests))
        self.assertTrue(any("submitExam" in request[1] for request in session.requests))

    def test_requests_kexam_resubmit_probe_only_builds_payload_without_confirm_or_submit(self):
        session = FakeKExamSession()

        result = run_requests_kexam_resubmit_diagnostic(
            session,
            course="5416",
            exam_url=EXAM_URL,
            quiz_policy="auto",
            question_bank_path=None,
            question_bank=kexam_question_bank(),
            probe_only=True,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "kexam_submit_preflight_ready")
        self.assertEqual(result.question_count, 2)
        self.assertEqual(result.selected_answer_count, 2)
        self.assertEqual(result.submit_result["answer_source_counts"], {"question_bank": 2})
        self.assertTrue(any("act=takeExam" in request[1] for request in session.requests))
        self.assertFalse(any("confirmRecord" in request[1] for request in session.requests))
        self.assertFalse(any("submitExam" in request[1] for request in session.requests))

    def test_requests_kexam_resubmit_accepts_verified_record_after_submit_500(self):
        session = FakeKExamSubmit500Session()

        result = run_requests_kexam_resubmit_diagnostic(
            session,
            course="5416",
            exam_url=EXAM_URL,
            quiz_policy="auto",
            question_bank_path=None,
            question_bank=kexam_question_bank(),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_submit_response_failed_record_verified")
        self.assertEqual(result.before_attempt_count, 5)
        self.assertEqual(result.after_attempt_count, 6)
        self.assertEqual(result.new_record_ids, ["950999"])
        self.assertEqual(result.submit_result["status"], "kexam_submit_failed")
        self.assertIn("kexam_submit_http_500", result.issues)
        self.assertIn("requests_submit_response_failed_record_verified", result.issues)

    def test_requests_kexam_resubmit_rejects_blank_record_after_submit_500(self):
        session = FakeKExamSubmit500BlankRecordSession()

        result = run_requests_kexam_resubmit_diagnostic(
            session,
            course="5416",
            exam_url=EXAM_URL,
            quiz_policy="auto",
            question_bank_path=None,
            question_bank=kexam_question_bank(),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.status, "requests_submit_failed_record_blank")
        self.assertEqual(result.before_attempt_count, 5)
        self.assertEqual(result.after_attempt_count, 6)
        self.assertEqual(result.new_record_ids, ["950999"])
        self.assertEqual(result.submit_result["status"], "kexam_submit_failed")
        self.assertIn("kexam_submit_http_500", result.issues)
        self.assertIn("record_probe_unverified:950999", result.issues)
        self.assertNotIn("requests_submit_response_failed_record_verified", result.issues)

    def test_try_run_kexam_submit_uses_strong_record_verification(self):
        session = FakeKExamSession()
        course = session.get_course_detail("5416")
        item = course.items[0]

        result = try_run_kexam_requests_submit(
            session,
            course,
            item,
            question_bank=kexam_question_bank(),
            quiz_policy="auto",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_kexam_submit_verified")
        self.assertEqual(result.selected_answer_count, 2)
        self.assertEqual(result.before_attempt_count, 5)
        self.assertEqual(result.after_attempt_count, 6)
        self.assertEqual(result.new_record_ids, ["950999"])
        self.assertEqual(result.verified_record_ids, ["950999"])
        self.assertEqual(result.verification_strength, "record")
        self.assertEqual(result.verification_method, "kexam_record_probe")
        self.assertEqual(result.verification_record_id, "950999")
        self.assertTrue(any("act=takeExam" in request[1] for request in session.requests))
        self.assertTrue(any("confirmRecord" in request[1] for request in session.requests))
        self.assertTrue(any("submitExam" in request[1] for request in session.requests))
        self.assertTrue(any("mod_kexamRecordList" in fetch for fetch in session.fetches))
        self.assertTrue(any("/record" in fetch for fetch in session.fetches))

    def test_try_run_kexam_submit_missing_answers_does_not_confirm_or_submit(self):
        session = FakeKExamSession()
        course = session.get_course_detail("5416")
        item = course.items[0]

        result = try_run_kexam_requests_submit(
            session,
            course,
            item,
            question_bank=None,
            quiz_policy="confirm",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.success)
        self.assertEqual(result.status, "kexam_missing_required_answers")
        self.assertTrue(any("act=takeExam" in request[1] for request in session.requests))
        self.assertFalse(any("confirmRecord" in request[1] for request in session.requests))
        self.assertFalse(any("submitExam" in request[1] for request in session.requests))

    def test_try_run_kexam_submit_requires_record_change_verification(self):
        session = FakeKExamNoRecordChangeSession()
        course = session.get_course_detail("5416")
        item = course.items[0]

        result = try_run_kexam_requests_submit(
            session,
            course,
            item,
            question_bank=kexam_question_bank(),
            quiz_policy="auto",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.success)
        self.assertEqual(result.status, "kexam_submit_not_verified")
        self.assertEqual(result.new_record_ids, [])
        self.assertEqual(result.verified_record_ids, [])
        self.assertTrue(any("submitExam" in request[1] for request in session.requests))

    def test_try_run_kexam_submit_accepts_verified_record_after_submit_500(self):
        session = FakeKExamSubmit500Session()
        course = session.get_course_detail("5416")
        item = course.items[0]

        result = try_run_kexam_requests_submit(
            session,
            course,
            item,
            question_bank=kexam_question_bank(),
            quiz_policy="auto",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.success)
        self.assertEqual(result.status, "requests_submit_response_failed_record_verified")
        self.assertEqual(result.new_record_ids, ["950999"])
        self.assertEqual(result.verified_record_ids, ["950999"])
        self.assertIn("kexam_submit_http_500", result.issues)

    def test_try_run_kexam_submit_rejects_blank_record_after_submit_500(self):
        session = FakeKExamSubmit500BlankRecordSession()
        course = session.get_course_detail("5416")
        item = course.items[0]

        result = try_run_kexam_requests_submit(
            session,
            course,
            item,
            question_bank=kexam_question_bank(),
            quiz_policy="auto",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.success)
        self.assertEqual(result.status, "requests_submit_failed_record_blank")
        self.assertEqual(result.new_record_ids, ["950999"])
        self.assertEqual(result.verified_record_ids, [])
        self.assertIn("record_probe_unverified:950999", result.issues)

    def test_requests_kexam_resubmit_with_empty_bank_uses_heuristic_answers(self):
        session = FakeKExamSession()

        result = run_requests_kexam_resubmit_diagnostic(
            session,
            course="5416",
            exam_url=EXAM_URL,
            quiz_policy="auto",
            question_bank_path=None,
            question_bank=QuestionBank([]),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, "resubmit_verified")
        self.assertEqual(result.selected_answer_count, 2)
        self.assertEqual(result.submit_result["answer_source_counts"], {"heuristic": 2})
        self.assertIn("gemini_api_key_missing", result.submit_result["answer_resolution_notes"])
        self.assertNotIn("answer_resolution:gemini_api_key_missing", result.submit_result["issues"])
        self.assertTrue(any("submitExam" in request[1] for request in session.requests))

    def test_requests_kexam_resubmit_reports_missing_entry(self):
        session = FakeKExamSession(include_operate=False)

        result = run_requests_kexam_resubmit_diagnostic(session, course="5416", exam_url=EXAM_URL, quiz_policy="auto")

        self.assertFalse(result.success)
        self.assertEqual(result.status, "kexam_entry_unavailable")

    def test_cli_parser_accepts_requests_kexam_commands(self):
        parser = build_parser()
        records = parser.parse_args(
            [
                "diag",
                "kexam-records",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
                "--exam-url",
                EXAM_URL,
            ]
        )
        self.assertEqual(records.command, "requests-kexam-records")
        self.assertEqual(records.accounts, ".tms_accounts.toml")
        self.assertEqual(records.exam_url, EXAM_URL)
        resubmit = parser.parse_args(
            [
                "diag",
                "quiz-resubmit",
                "--accounts",
                ".tms_accounts.toml",
                "--label",
                "account1",
                "--course",
                "5416",
                "--exam-url",
                EXAM_URL,
                "--quiz",
                "auto",
                "--probe-only",
            ]
        )
        self.assertEqual(resubmit.command, "requests-quiz-resubmit-diagnostics")
        self.assertEqual(resubmit.course, "5416")
        self.assertEqual(resubmit.quiz, "auto")
        self.assertTrue(resubmit.probe_only)


if __name__ == "__main__":
    unittest.main()
