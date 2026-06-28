import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.models import CourseItem, ItemKind, ItemState
from tms_vghks.network_diagnostics import _item_verified_from_detail


class NetworkDiagnosticsTests(unittest.TestCase):
    def test_item_verified_from_detail_uses_pass_condition_result(self):
        item = CourseItem(
            title="Read",
            kind=ItemKind.READING,
            state=ItemState.IN_PROGRESS,
            pass_condition="閱讀達 40 分鐘",
            result="41:03",
            passed_marker="-",
        )

        self.assertTrue(_item_verified_from_detail(item))
        self.assertFalse(_item_verified_from_detail(None))


if __name__ == "__main__":
    unittest.main()
