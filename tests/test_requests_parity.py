import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks import cli as cli_module
from tms_vghks.capabilities import capability_contracts_by_feature, validate_capability_contracts
from tms_vghks.cli import build_parser
from tms_vghks.models import CourseDetail, CourseItem, CourseSummary, ItemKind, ItemState, LoginStatus, SiteState
from tms_vghks.requests_parity import (
    build_requests_parity_report,
    compare_backend_read_paths,
    format_backend_comparison_markdown,
    format_requests_parity_markdown,
)
from tms_vghks.requests_reproduction import analyze_requests_reproduction_observations


class RequestsParityTests(unittest.TestCase):
    def test_static_matrix_has_expected_statuses(self):
        report = build_requests_parity_report()
        rows = {row.feature: row for row in report.matrix}

        self.assertEqual(rows["login_session"].status, "equivalent")
        self.assertEqual(rows["course_list_and_detail"].status, "equivalent")
        self.assertEqual(rows["question_bank_history_export"].status, "partially_equivalent")
        self.assertEqual(rows["reading_video_completion"].status, "partially_equivalent")
        self.assertEqual(rows["survey_submission"].status, "partially_equivalent")
        self.assertEqual(rows["quiz_submission"].status, "partially_equivalent")
        self.assertEqual(rows["kexam_records_resubmit"].status, "equivalent")
        self.assertEqual(report.status_counts["equivalent"], 3)
        self.assertEqual(report.status_counts["partially_equivalent"], 6)
        self.assertEqual(report.status_counts["not_equivalent_yet"], 0)

    def test_live_mutation_rows_require_network_evidence(self):
        report = build_requests_parity_report()
        live_rows = [row for row in report.matrix if row.scope == "live_mutation"]

        self.assertEqual(
            {row.feature for row in live_rows},
            {"reading_video_completion", "survey_submission", "quiz_submission", "kexam_records_resubmit"},
        )
        for row in live_rows:
            self.assertTrue(any("network" in step.lower() or "capture" in step.lower() for step in row.verification))

    def test_capability_contracts_require_requests_counterparts(self):
        self.assertEqual(validate_capability_contracts(), [])
        contracts = capability_contracts_by_feature()

        for feature, contract in contracts.items():
            self.assertTrue(contract.playwright_entrypoints, feature)
            self.assertTrue(contract.requests_entrypoints, feature)
        self.assertIn("TmsRunner.run_scheduler with OperationBackend.REQUESTS", contracts["scheduler_run_backend"].requests_entrypoints)
        self.assertIn("requests_kexam_submit_verified", contracts["quiz_submission"].requests_success_statuses)

    def test_markdown_includes_verification_commands_and_protocol(self):
        markdown = format_requests_parity_markdown()

        self.assertIn("# Playwright vs Requests Parity Report", markdown)
        self.assertIn("| Feature | Playwright entrypoints | Requests entrypoints |", markdown)
        self.assertIn("uv run tms-vghks-cli courses list pending --accounts .tms_accounts.toml --label account1", markdown)
        self.assertIn("Wait the real required time or watch interval for reading/video items.", markdown)

    def test_parity_report_can_include_diagnostic_evidence_without_changing_static_implementation_status(self):
        reproduction = analyze_requests_reproduction_observations(
            [
                {
                    "action": "read-finish:verification",
                    "item_kind": "reading",
                    "method": "",
                    "url": "https://tms.vghks.gov.tw/course/1",
                    "response_json_summary": {"verified": True},
                },
                {
                    "action": "read-finish",
                    "item_kind": "reading",
                    "method": "POST",
                    "url": "https://tms.vghks.gov.tw/ajax/learningItem/finish",
                    "post_data_keys": ["anticsrf", "activityID"],
                    "response_json_summary": {"keys": ["success"], "success": True},
                },
            ]
        )
        report = build_requests_parity_report(reproduction_report=reproduction)
        rows = {row.feature: row for row in report.matrix}

        self.assertEqual(rows["reading_video_completion"].status, "partially_equivalent")
        self.assertTrue(report.diagnostic_evidence)
        self.assertIn("Diagnostic Evidence Summary", format_requests_parity_markdown(report))

    def test_cli_parser_accepts_requests_parity(self):
        args = build_parser().parse_args(
            ["diag", "parity", "--output", "REQUESTS_PARITY_REPORT.md", "--observations", "network.jsonl"]
        )

        self.assertEqual(args.command, "requests-parity")
        self.assertEqual(args.output, "REQUESTS_PARITY_REPORT.md")
        self.assertEqual(args.observations, "network.jsonl")
        self.assertFalse(hasattr(args, "json"))

    def test_cli_requests_parity_outputs_json_without_session(self):
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli_module.main(["diag", "parity"])

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["report"]["status_counts"]["equivalent"], 3)
        self.assertEqual(payload["report"]["status_counts"]["partially_equivalent"], 6)
        self.assertEqual(payload["report"]["matrix"][0]["feature"], "login_session")

    def test_cli_requests_parity_writes_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "parity.md"
            buffer = StringIO()
            with redirect_stdout(buffer):
                code = cli_module.main(["diag", "parity", "--output", str(output)])

            self.assertEqual(code, 0)
            self.assertTrue(output.exists())
            self.assertIn("Parity Matrix", output.read_text(encoding="utf-8"))
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["output_path"], str(output))

    def test_backend_comparison_equivalent_for_matching_read_paths(self):
        report = compare_backend_read_paths(FakeBackendSession(), detail_limit=1)

        self.assertEqual(report.status, "equivalent")
        self.assertEqual([row.status for row in report.rows], ["equivalent", "equivalent", "equivalent"])
        self.assertEqual(report.rows[0].requests_count, 1)
        self.assertEqual(report.rows[0].playwright_count, 1)
        self.assertIn("Live Requests vs Playwright Read Comparison", format_backend_comparison_markdown(report))

    def test_backend_comparison_reports_read_path_mismatches(self):
        report = compare_backend_read_paths(FakeBackendSession(mismatch=True), detail_limit=1)

        self.assertEqual(report.status, "mismatch")
        mismatch_text = "\n".join("\n".join(row.mismatches) for row in report.rows)
        self.assertIn("progress", mismatch_text)
        self.assertIn("result", mismatch_text)

    def test_cli_parser_accepts_compare_backends(self):
        args = build_parser().parse_args(
            ["diag", "compare", "--course", "123", "--detail-limit", "2", "--skip-completed"]
        )

        self.assertEqual(args.command, "compare-backends")
        self.assertEqual(args.course, "123")
        self.assertEqual(args.detail_limit, 2)
        self.assertTrue(args.skip_completed)
        self.assertFalse(hasattr(args, "json"))

    def test_cli_compare_backends_outputs_json(self):
        original_session = cli_module.TmsSession
        try:
            cli_module.TmsSession = FakeBackendSession
            buffer = StringIO()
            with redirect_stdout(buffer):
                code = cli_module.main(["diag", "compare"])
        finally:
            cli_module.TmsSession = original_session

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["report"]["status"], "equivalent")
        self.assertEqual(payload["report"]["rows"][0]["feature"], "pending_courses")

    def test_cli_accounts_compare_backends_returns_mismatch_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = Path(tmp) / "accounts.toml"
            accounts.write_text(
                """
                [[accounts]]
                label = "account1"
                account = "u"
                password = "p"
                """,
                encoding="utf-8",
            )
            original_session = cli_module.TmsSession
            try:
                FakeAccountCompareSession.mismatch = True
                cli_module.TmsSession = FakeAccountCompareSession
                buffer = StringIO()
                with redirect_stdout(buffer):
                    code = cli_module.main(
                        [
                            "diag",
                            "compare",
                            "--accounts",
                            str(accounts),
                            "--label",
                            "account1",
                            "--login-method",
                            "saved",
                        ]
                    )
            finally:
                cli_module.TmsSession = original_session
                FakeAccountCompareSession.mismatch = False

        self.assertEqual(code, 4)
        payload = json.loads(buffer.getvalue())
        self.assertFalse(payload["success"])
        self.assertEqual(payload["results"][0]["status"], "mismatch")
        self.assertEqual(payload["results"][0]["result"]["status"], "mismatch")


class FakeBackendSession:
    def __init__(self, mismatch: bool = False):
        self.mismatch = mismatch

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def use_backend(self, backend):
        self.active_backend = backend

    def configure_transient_policy(self, retries, delay_seconds):
        self.transient_policy = (retries, delay_seconds)

    def ensure_authenticated(self, options):
        return LoginStatus(SiteState.LOGGED_IN)

    def list_pending_courses(self, backend=None):
        suffix = "playwright" if _backend_value(backend) == "playwright" else "requests"
        progress = "2/3" if not self.mismatch or suffix == "requests" else "1/3"
        return [
            CourseSummary(
                title="Safety Training",
                detail_url="https://tms.example.test/course/123",
                course_id="123",
                progress=progress,
                completed=False,
            )
        ]

    def list_completed_courses(self, backend=None):
        return []

    def get_course_detail(self, course, backend=None):
        suffix = "playwright" if _backend_value(backend) == "playwright" else "requests"
        result = "10/30" if not self.mismatch or suffix == "requests" else "0/30"
        return CourseDetail(
            title="Safety Training",
            url="https://tms.example.test/course/123",
            course_id="123",
            items=[
                CourseItem(
                    title="Reading Material",
                    order=1,
                    kind=ItemKind.READING,
                    state=ItemState.IN_PROGRESS,
                    detail_url="https://tms.example.test/activity/abc",
                    pass_condition="30 minutes",
                    result=result,
                    metadata={"activity_id": "abc"},
                )
            ],
        )


def _backend_value(backend) -> str:
    return getattr(backend, "value", backend)


class FakeAccountCompareSession(FakeBackendSession):
    mismatch = False

    def __init__(self, base_url="https://tms.example.test"):
        super().__init__(mismatch=type(self).mismatch)
        self.base_url = base_url
        self.saved_browser_auth = []

    def ensure_saved_browser_authenticated(self, session_dir, headless=False):
        self.saved_browser_auth.append((session_dir, headless))
        return LoginStatus(SiteState.LOGGED_IN)


if __name__ == "__main__":
    unittest.main()
