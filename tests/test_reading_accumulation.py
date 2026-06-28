import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.models import CourseDetail, CourseItem, CourseSummary, ItemKind, ItemState
from tms_vghks.reading_accumulation import (
    item_is_accumulation_candidate,
    result_increased,
    run_reading_accumulation_diagnostic,
    select_reading_accumulation_target,
)


class FakeSelectionSession:
    def __init__(self, details):
        self.details = details

    def list_pending_courses(self):
        raise AssertionError("reading accumulation auto-selection must not use pending courses")

    def list_completed_courses(self):
        return [CourseSummary(title=key, detail_url=key) for key in self.details]

    def get_course_detail(self, url_or_id):
        return self.details[url_or_id]


class ReadingAccumulationTests(unittest.TestCase):
    def test_result_increased_handles_empty_and_timer_values(self):
        self.assertTrue(result_increased("--", "00:20"))
        self.assertTrue(result_increased("00:02", "01:05"))
        self.assertFalse(result_increased("01:05", "01:05"))
        self.assertFalse(result_increased("41:03", "41:03"))
        self.assertFalse(result_increased("01:05", "--"))

    def test_completed_item_is_auto_candidate(self):
        passed = CourseItem(
            title="Completed Read",
            kind=ItemKind.READING,
            state=ItemState.PASSED,
            pass_condition="閱讀達 40 分鐘",
            result="41:03",
        )
        pending = CourseItem(
            title="Pending Read",
            kind=ItemKind.READING,
            state=ItemState.IN_PROGRESS,
            pass_condition="閱讀達 1 分鐘",
            result="00:02",
        )

        self.assertTrue(item_is_accumulation_candidate(passed))
        self.assertFalse(item_is_accumulation_candidate(pending))

    def test_auto_selects_first_completed_reading_or_video(self):
        pending_reading = CourseDetail(
            title="Pending Course",
            url="pending",
            items=[
                CourseItem(
                    title="Pending Read",
                    order=1,
                    kind=ItemKind.READING,
                    state=ItemState.IN_PROGRESS,
                    pass_condition="閱讀達 1 分鐘",
                    result="00:30",
                )
            ],
        )
        target_course = CourseDetail(
            title="Target Course",
            url="target",
            items=[
                CourseItem(title="Survey", order=1, kind=ItemKind.SURVEY, state=ItemState.PENDING),
                CourseItem(
                    title="Completed Video",
                    order=2,
                    kind=ItemKind.VIDEO,
                    state=ItemState.PASSED,
                    pass_condition="觀看達 1 分鐘",
                    result="01:05",
                ),
            ],
        )
        session = FakeSelectionSession({"pending": pending_reading, "target": target_course})

        target = select_reading_accumulation_target(session)

        self.assertEqual(target.course.title, "Target Course")
        self.assertEqual(target.item.title, "Completed Video")

    def test_specified_course_item_allows_passed_reading(self):
        session = FakeSelectionSession(
            {
                "course": CourseDetail(
                    title="Course",
                    url="course",
                    items=[
                        CourseItem(
                            title="Completed Read",
                            order=1,
                            kind=ItemKind.READING,
                            state=ItemState.PASSED,
                            pass_condition="閱讀達 1 分鐘",
                            result="01:01",
                        )
                    ],
                )
            }
        )

        target = select_reading_accumulation_target(session, course="course", item_order=1)

        self.assertEqual(target.item.title, "Completed Read")

    def test_no_completed_reading_returns_structured_status_for_auto_mode(self):
        session = FakeSelectionSession(
            {
                "course": CourseDetail(
                    title="Course",
                    url="course",
                    items=[CourseItem(title="Survey", kind=ItemKind.SURVEY, state=ItemState.PENDING)],
                )
            }
        )

        result = run_reading_accumulation_diagnostic(session, wait_seconds=0)

        self.assertEqual(result.status, "no_completed_reading_item")
        self.assertIn("no completed", result.blocker)


if __name__ == "__main__":
    unittest.main()
