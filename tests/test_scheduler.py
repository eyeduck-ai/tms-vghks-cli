import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.handlers import RunOptions, TmsRunner, first_incomplete_item, next_adaptive_limit
from tms_vghks.models import CourseDetail, CourseItem, CourseSummary, ItemKind, ItemState, LoginStatus, OperationBackend, SiteState
from tms_vghks.session import TmsSession


class FakeCourseAutomationSession(TmsSession):
    def __init__(self, course_items: dict[str, list[CourseItem]]):
        super().__init__()
        self.course_items = course_items
        self.completed: dict[str, set[int | None]] = {course_id: set() for course_id in course_items}

    def ensure_authenticated(self, options=None):
        return LoginStatus(SiteState.LOGGED_IN, message="fake")

    def list_pending_courses(self):
        pending = []
        for course_id, items in self.course_items.items():
            if any(item.order not in self.completed[course_id] for item in items):
                pending.append(
                    CourseSummary(
                        title=f"Course {course_id}",
                        detail_url=f"https://tms.vghks.gov.tw/course/{course_id}",
                        course_id=course_id,
                    )
                )
        return pending

    def get_course_detail(self, url_or_id):
        course_id = str(url_or_id).rstrip("/").split("/")[-1]
        items = []
        for item in self.course_items[course_id]:
            state = ItemState.PASSED if item.order in self.completed[course_id] else item.state
            items.append(
                CourseItem(
                    title=item.title,
                    order=item.order,
                    kind=item.kind,
                    state=state,
                    detail_url=item.detail_url,
                    pass_condition=item.pass_condition,
                    result=item.result,
                )
            )
        return CourseDetail(
            title=f"Course {course_id}",
            url=f"https://tms.vghks.gov.tw/course/{course_id}",
            course_id=course_id,
            items=items,
        )


class BackendAwareCourseAutomationSession(FakeCourseAutomationSession):
    def __init__(self, course_items: dict[str, list[CourseItem]]):
        super().__init__(course_items)
        self.list_backends: list[OperationBackend | None] = []
        self.detail_backends: list[OperationBackend | None] = []

    def list_pending_courses(self, backend=None):
        self.list_backends.append(backend)
        return super().list_pending_courses()

    def get_course_detail(self, url_or_id, backend=None):
        self.detail_backends.append(backend)
        return super().get_course_detail(url_or_id)


class MarkingRunner(TmsRunner):
    def run_item(self, course, item):
        self.session.completed[course.course_id].add(item.order)
        return super_result(course, item, True, ItemState.PASSED, "ok")


class FailingOneCourseRunner(MarkingRunner):
    def run_item(self, course, item):
        if course.course_id == "A":
            return super_result(course, item, False, "forced_failure", "failed")
        return super().run_item(course, item)


class ConcurrentMarkingRunner(MarkingRunner):
    def __init__(self, session, options):
        super().__init__(session, options)
        self.active = 0
        self.max_active = 0
        self.active_by_course: dict[str, int] = {}
        self.max_active_by_course: dict[str, int] = {}
        self.lock = threading.Lock()

    def run_item(self, course, item):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.active_by_course[course.course_id] = self.active_by_course.get(course.course_id, 0) + 1
            self.max_active_by_course[course.course_id] = max(
                self.max_active_by_course.get(course.course_id, 0),
                self.active_by_course[course.course_id],
            )
        try:
            time.sleep(0.05)
            return super().run_item(course, item)
        finally:
            with self.lock:
                self.active -= 1
                self.active_by_course[course.course_id] -= 1


class DispatchRecordingRunner(TmsRunner):
    def __init__(self, session, options):
        super().__init__(session, options)
        self.dispatches = []

    def run_reading_requests(self, course, item):
        return self._record_requests_dispatch(course, item, "run_reading_requests")

    def run_survey_requests(self, course, item):
        return self._record_requests_dispatch(course, item, "run_survey_requests")

    def run_quiz_requests(self, course, item):
        return self._record_requests_dispatch(course, item, "run_quiz_requests")

    def _record_requests_dispatch(self, course, item, method_name):
        self.dispatches.append((method_name, item.order, item.kind))
        self.session.completed[course.course_id].add(item.order)
        return super_result(course, item, True, ItemState.PASSED, method_name)


def super_result(course, item, success, state, message):
    from tms_vghks.models import RunResult

    return RunResult(success, state, message, course=course, item=item)


def reading_item(order: int) -> CourseItem:
    return CourseItem(title=f"Read {order}", order=order, kind=ItemKind.READING, state=ItemState.PENDING)


def result_complete_reading_item(order: int) -> CourseItem:
    return CourseItem(
        title=f"Read {order}",
        order=order,
        kind=ItemKind.READING,
        state=ItemState.IN_PROGRESS,
        pass_condition="閱讀達 40 分鐘",
        result="41:03",
        passed_marker="-",
    )


def survey_item(order: int) -> CourseItem:
    return CourseItem(title=f"Survey {order}", order=order, kind=ItemKind.SURVEY, state=ItemState.PENDING)


def quiz_item(order: int) -> CourseItem:
    return CourseItem(title=f"Quiz {order}", order=order, kind=ItemKind.QUIZ, state=ItemState.PENDING)


class SchedulerTests(unittest.TestCase):
    def test_adaptive_limit(self):
        self.assertEqual(next_adaptive_limit(4, True, 8), 6)
        self.assertEqual(next_adaptive_limit(8, True, 8), 8)
        self.assertEqual(next_adaptive_limit(8, False, 8), 4)
        self.assertEqual(next_adaptive_limit(6, False, 8), 4)
        self.assertEqual(next_adaptive_limit(6, True, 8, adaptive=False), 6)

    def test_requests_scheduler_runs_all_items_in_course(self):
        session = FakeCourseAutomationSession({"A": [reading_item(1), reading_item(2), reading_item(3)]})
        runner = MarkingRunner(session, RunOptions(backend=OperationBackend.REQUESTS, concurrency=1))

        result = runner.run_scheduler()

        self.assertTrue(result.success)
        self.assertEqual(result.data["pending_count_before"], 1)
        self.assertEqual(result.data["pending_count_after"], 0)
        self.assertEqual([row["item_order"] for row in result.data["item_results"]], [1, 2, 3])
        self.assertEqual(result.data["summary"]["succeeded_courses"], 1)
        self.assertEqual(result.data["summary"]["failed_items"], 0)

    def test_scheduler_skips_items_whose_result_satisfies_pass_condition(self):
        detail = CourseDetail(
            title="Course A",
            url="https://tms.vghks.gov.tw/course/A",
            course_id="A",
            items=[result_complete_reading_item(1), reading_item(2)],
        )
        self.assertEqual(first_incomplete_item(detail).order, 2)

        session = FakeCourseAutomationSession({"A": [result_complete_reading_item(1), reading_item(2)]})
        runner = MarkingRunner(session, RunOptions(backend=OperationBackend.REQUESTS, concurrency=1))

        result = runner.run_scheduler()

        self.assertTrue(result.success)
        self.assertEqual([row["item_order"] for row in result.data["item_results"]], [2])

    def test_requests_scheduler_dispatches_all_supported_item_kinds_to_requests_methods(self):
        session = FakeCourseAutomationSession({"A": [reading_item(1), survey_item(2), quiz_item(3)]})
        runner = DispatchRecordingRunner(session, RunOptions(backend=OperationBackend.REQUESTS, concurrency=1))

        result = runner.run_scheduler()

        self.assertTrue(result.success)
        self.assertEqual(
            runner.dispatches,
            [
                ("run_reading_requests", 1, ItemKind.READING),
                ("run_survey_requests", 2, ItemKind.SURVEY),
                ("run_quiz_requests", 3, ItemKind.QUIZ),
            ],
        )
        self.assertEqual([row["message"] for row in result.data["item_results"]], [row[0] for row in runner.dispatches])
        self.assertEqual(result.data["summary"]["succeeded_courses"], 1)

    def test_course_failure_does_not_stop_other_courses(self):
        session = FakeCourseAutomationSession({"A": [reading_item(1)], "B": [reading_item(1)]})
        runner = FailingOneCourseRunner(session, RunOptions(backend=OperationBackend.REQUESTS, concurrency=1))

        result = runner.run_scheduler()

        self.assertFalse(result.success)
        self.assertEqual(result.data["summary"]["failed_courses"], 1)
        self.assertEqual(result.data["summary"]["succeeded_courses"], 1)
        self.assertEqual({record["course_id"]: record["status"] for record in result.data["course_runs"]}, {"A": "failed", "B": "succeeded"})
        self.assertEqual(len(result.data["errors"]), 1)

    def test_course_level_concurrency_keeps_single_course_items_sequential(self):
        session = FakeCourseAutomationSession(
            {
                "A": [reading_item(1), reading_item(2)],
                "B": [reading_item(1), reading_item(2)],
            }
        )
        runner = ConcurrentMarkingRunner(session, RunOptions(backend=OperationBackend.REQUESTS, concurrency=2))

        result = runner.run_scheduler()

        self.assertTrue(result.success)
        self.assertGreaterEqual(runner.max_active, 2)
        self.assertEqual(runner.max_active_by_course["A"], 1)
        self.assertEqual(runner.max_active_by_course["B"], 1)
        self.assertEqual(result.data["summary"]["effective_concurrency"], 2)

    def test_scheduler_uses_selected_backend_for_list_and_detail_reads(self):
        session = BackendAwareCourseAutomationSession({"A": [reading_item(1)]})
        runner = MarkingRunner(session, RunOptions(backend=OperationBackend.PLAYWRIGHT, concurrency=1))

        result = runner.run_scheduler()

        self.assertTrue(result.success)
        self.assertEqual(session.list_backends, [OperationBackend.PLAYWRIGHT, OperationBackend.PLAYWRIGHT])
        self.assertEqual(session.detail_backends, [OperationBackend.PLAYWRIGHT, OperationBackend.PLAYWRIGHT])


if __name__ == "__main__":
    unittest.main()

