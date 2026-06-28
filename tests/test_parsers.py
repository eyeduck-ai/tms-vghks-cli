import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.models import ItemKind, ItemState, SiteState
from tms_vghks.parsers import classify_response, parse_course_detail_html, parse_course_list_html, result_satisfies_condition
from tms_vghks.timeutils import parse_required_seconds, parse_timer_to_seconds, remaining_seconds


class ParserTests(unittest.TestCase):
    def test_timer_parsing(self):
        self.assertEqual(parse_timer_to_seconds("00:02"), 2)
        self.assertEqual(parse_timer_to_seconds("41:03"), 2463)
        self.assertEqual(parse_timer_to_seconds("1:02:03"), 3723)
        self.assertEqual(parse_timer_to_seconds("1 小時 2 分鐘 3 秒"), 3723)

    def test_required_seconds(self):
        self.assertEqual(parse_required_seconds("閱讀達 40 分鐘"), 2400)
        self.assertEqual(remaining_seconds(2400, "00:02"), 2398)

    def test_condition_satisfaction(self):
        self.assertTrue(result_satisfies_condition("閱讀達 40 分鐘", "41:03", "-"))
        self.assertTrue(result_satisfies_condition("60 分及格", "80", "-"))
        self.assertFalse(result_satisfies_condition("60 分及格", "50", "-"))
        self.assertTrue(result_satisfies_condition("須填寫", "已完成", "-"))

    def test_login_redirect_detection(self):
        status = classify_response(
            302,
            "https://tms.vghks.gov.tw/course/notCompleteList",
            {"location": "/index/login?next=%2Fcourse%2FnotCompleteList"},
            "",
        )
        self.assertEqual(status.state, SiteState.LOGIN_REQUIRED)

    def test_transient_marker_detection(self):
        status = classify_response(
            200,
            "https://tms.vghks.gov.tw/course/123",
            {},
            "儲存失敗，請檢查伺服器狀態",
        )
        self.assertEqual(status.state, SiteState.TRANSIENT_ERROR)

    def test_http_5xx_is_transient(self):
        status = classify_response(503, "https://tms.vghks.gov.tw/course/123", {}, "")
        self.assertEqual(status.state, SiteState.TRANSIENT_ERROR)

    def test_parse_course_list_table(self):
        html = """
        <table>
          <tr><th>課程名稱</th><th>完成度</th><th>操作</th></tr>
          <tr><td>感染管制</td><td>50%</td><td><a href="/course/123">進入</a></td></tr>
        </table>
        """
        courses = parse_course_list_html(html, completed=False)
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].title, "感染管制")
        self.assertEqual(courses[0].progress, "50%")
        self.assertEqual(courses[0].course_id, "123")

    def test_course_list_ignores_navigation_course_links(self):
        html = """
        <nav>
          <a href="/course/mine">我的學習</a>
          <a href="/course/latest?inSign=ing&state=progressing">最新課程</a>
          <a href="/course/bulletin">公告區</a>
          <a href="/course/notCompleteList">待修課程</a>
        </nav>
        <main>目前沒有待修課程</main>
        """
        self.assertEqual(parse_course_list_html(html, completed=False), [])

    def test_parse_course_detail_rows(self):
        html = """
        <h1>感染管制</h1>
        <table>
          <tr><th>項次</th><th>項目名稱</th><th>通過條件</th><th>學習成果</th><th>通過</th><th>操作</th></tr>
          <tr><td>1</td><td><a href="/reading/1">閱讀教材</a></td><td>閱讀達 40 分鐘</td><td>41:03</td><td>-</td><td>開始閱讀</td></tr>
          <tr><td>2</td><td><a href="/quiz/2">課後測驗</a></td><td>60 分及格</td><td>--</td><td>-</td><td>進入測驗</td></tr>
        </table>
        """
        detail = parse_course_detail_html(html, "https://tms.vghks.gov.tw/course/123")
        self.assertEqual(detail.title, "感染管制")
        self.assertEqual(len(detail.items), 2)
        self.assertEqual(detail.items[0].kind, ItemKind.READING)
        self.assertEqual(detail.items[0].state, ItemState.PASSED)
        self.assertEqual(detail.items[1].kind, ItemKind.QUIZ)
        self.assertEqual(detail.items[1].state, ItemState.PENDING)

    def test_parse_course_detail_activity_tree(self):
        html = """
        <title>115年全民國防教育訓練 | TMS+</title>
        <nav>個人資訊 測試使用者 登出</nav>
        <div>您已於 2026-06-15 完成課程</div>
        <div id="activityTree">
          <ol class="xtree-list">
            <li id="24002" class="xtree-node" data-id="24002" data-type="">
              <div class="header">
                <div class="ext-col col-date">12-31</div>
                <div class="ext-col col-char7">10 分及格</div>
                <div class="ext-col col-char4">
                  <a data-url="/ajax/sys.app.learningItem/kexam/?activityID=2619002">80</a>
                </div>
                <span class="item-pass"><span class="fa-check-circle"></span></span>
                <span class="xtree-node-label">
                  <div class="sn">1.</div>
                  <div class="fs-singleLineText"><a href="#">課前測驗 <span>(成績比重: 0%)</span></a></div>
                </span>
              </div>
            </li>
            <li id="23977" class="xtree-node" data-id="23977">
              <div class="ext-col col-date">12-31</div>
              <div class="ext-col col-char7">閱讀達 40 分鐘</div>
              <div class="ext-col col-char4">41:03</div>
              <span class="item-pass"></span>
              <div class="sn">2.</div>
              <div class="fs-singleLineText">國防科技與國防自主</div>
            </li>
            <li id="24005" class="xtree-node" data-id="24005">
              <div class="ext-col col-date">12-31</div>
              <div class="ext-col col-char7">須填寫</div>
              <div class="ext-col col-char4">已完成</div>
              <span class="item-pass"></span>
              <div class="sn">3.</div>
              <div class="fs-singleLineText">訓練課程滿意度問卷</div>
            </li>
            <li id="24006" class="xtree-node" data-id="24006">
              <div class="ext-col col-date">-</div>
              <div class="ext-col col-char7">-</div>
              <div class="ext-col col-char4">-</div>
              <span class="item-pass"></span>
              <div class="sn">4.</div>
              <div class="fs-singleLineText">參考講義</div>
            </li>
          </ol>
        </div>
        """
        detail = parse_course_detail_html(html, "https://tms.vghks.gov.tw/course/5416")
        self.assertEqual(len(detail.items), 4)
        self.assertEqual(detail.items[0].kind, ItemKind.QUIZ)
        self.assertEqual(detail.items[0].metadata["activity_id"], "24002")
        self.assertIn("/ajax/sys.app.learningItem/kexam/", detail.items[0].metadata["result_modal_url"])
        self.assertEqual(detail.items[1].kind, ItemKind.READING)
        self.assertEqual(detail.items[2].kind, ItemKind.SURVEY)
        self.assertEqual(detail.items[3].kind, ItemKind.UNKNOWN)
        self.assertTrue(detail.completed)
        self.assertIn("課前測驗", detail.raw_text)
        self.assertNotIn("測試使用者", detail.raw_text)


if __name__ == "__main__":
    unittest.main()
