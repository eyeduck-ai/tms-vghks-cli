import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks import cli as cli_module
from tms_vghks.network_diagnostics import parse_network_diagnostic_action
from tms_vghks.requests_reproduction import (
    analyze_requests_reproduction_file,
    analyze_requests_reproduction_observations,
    feature_map,
)


class RequestsReproductionTests(unittest.TestCase):
    def test_parse_network_diagnostic_action(self):
        self.assertEqual(parse_network_diagnostic_action(None), "open-only")
        self.assertEqual(parse_network_diagnostic_action("read-wait"), "read-wait")
        with self.assertRaisesRegex(ValueError, "unsupported network diagnostic action"):
            parse_network_diagnostic_action("finish-now")

    def test_reading_open_only_is_partial(self):
        report = analyze_requests_reproduction_observations(
            [
                {
                    "action": "open-only",
                    "item_kind": "reading",
                    "method": "GET",
                    "url": "https://tms.vghks.gov.tw/learning/reading/123",
                    "post_data_keys": [],
                    "response_json_summary": {},
                }
            ]
        )
        reading = feature_map(report)["reading_video_completion"]

        self.assertEqual(reading.status, "requests_partial")
        self.assertIn("activity_get", reading.evidence)
        self.assertIn("finish_endpoint", reading.missing_evidence)
        self.assertIn("course_detail_verification", reading.missing_evidence)

    def test_reading_row_progress_increase_is_partial_evidence(self):
        report = analyze_requests_reproduction_observations(
            [
                {
                    "action": "reading-accumulation:sample",
                    "item_kind": "reading",
                    "method": "",
                    "url": "https://tms.vghks.gov.tw/course/1",
                    "post_data_keys": [],
                    "response_json_summary": {"result": "00:30", "progress_increased": True},
                }
            ]
        )
        reading = feature_map(report)["reading_video_completion"]

        self.assertEqual(reading.status, "requests_partial")
        self.assertIn("course_detail_progress_increased", reading.evidence)

    def test_check_pass_previous_is_not_progress_or_finish_endpoint(self):
        report = analyze_requests_reproduction_observations(
            [
                {
                    "action": "reading-accumulation",
                    "item_kind": "reading",
                    "method": "POST",
                    "url": "https://tms.vghks.gov.tw/ajax/sys.pages.course/checkPassPrevious/",
                    "post_data_keys": ["item"],
                    "response_json_summary": {"keys": ["status"], "status": True},
                }
            ]
        )
        reading = feature_map(report)["reading_video_completion"]

        self.assertEqual(reading.status, "requests_blocked")
        self.assertNotIn("start_or_heartbeat_endpoint", reading.evidence)
        self.assertNotIn("finish_endpoint", reading.evidence)

    def test_watch_time_endpoint_counts_as_progress_evidence(self):
        report = analyze_requests_reproduction_observations(
            [
                {
                    "action": "reading-accumulation",
                    "item_kind": "reading",
                    "method": "POST",
                    "url": "https://tms.vghks.gov.tw/ajax/sys.pages.media/watchTime/",
                    "post_data_keys": [],
                    "response_json_summary": {"keys": ["ret", "status"], "status": True},
                }
            ]
        )
        reading = feature_map(report)["reading_video_completion"]

        self.assertEqual(reading.status, "requests_partial")
        self.assertIn("start_or_heartbeat_endpoint", reading.evidence)
        self.assertNotIn("finish_endpoint", reading.evidence)

    def test_reading_finish_with_verification_is_reproducible(self):
        report = analyze_requests_reproduction_observations(
            [
                {
                    "action": "read-finish",
                    "item_kind": "reading",
                    "method": "GET",
                    "url": "https://tms.vghks.gov.tw/learning/reading/123",
                    "post_data_keys": [],
                    "response_json_summary": {},
                },
                {
                    "action": "read-finish",
                    "item_kind": "reading",
                    "method": "POST",
                    "url": "https://tms.vghks.gov.tw/ajax/learningItem/finish",
                    "post_data_keys": ["activityID", "anticsrf"],
                    "response_json_summary": {"keys": ["success"], "success": True},
                },
                {
                    "action": "read-finish:verification",
                    "item_kind": "reading",
                    "method": "",
                    "url": "https://tms.vghks.gov.tw/course/1",
                    "post_data_keys": [],
                    "response_json_summary": {"verified": True},
                },
            ]
        )
        reading = feature_map(report)["reading_video_completion"]

        self.assertEqual(reading.status, "requests_reproducible")
        self.assertIn("finish_endpoint", reading.evidence)
        self.assertIn("course_detail_verification", reading.evidence)

    def test_form_submit_without_token_is_partial(self):
        report = analyze_requests_reproduction_observations(
            [
                {
                    "action": "form-open:rendered-form-metadata",
                    "item_kind": "survey",
                    "method": "",
                    "url": "https://tms.vghks.gov.tw/survey/1",
                    "response_json_summary": {"radio_groups": 1, "submit_buttons": ["送出"]},
                },
                {
                    "action": "form-submit",
                    "item_kind": "survey",
                    "method": "POST",
                    "url": "https://tms.vghks.gov.tw/ajax/survey/submit",
                    "post_data_keys": [],
                    "response_json_summary": {"keys": ["success"]},
                },
            ]
        )
        survey = feature_map(report)["survey_submission"]

        self.assertEqual(survey.status, "requests_partial")
        self.assertIn("submit_endpoint", survey.evidence)
        self.assertIn("token_or_hidden_fields", survey.missing_evidence)

    def test_form_submit_with_verification_is_reproducible(self):
        report = analyze_requests_reproduction_observations(
            [
                {
                    "action": "form-submit:rendered-form-metadata",
                    "item_kind": "quiz",
                    "method": "",
                    "url": "https://tms.vghks.gov.tw/kexam/1",
                    "response_json_summary": {"hidden_fields": ["anticsrf"], "submit_buttons": ["交卷"]},
                },
                {
                    "action": "form-submit",
                    "item_kind": "quiz",
                    "method": "POST",
                    "url": "https://tms.vghks.gov.tw/ajax/kexam/submit",
                    "post_data_keys": ["anticsrf", "activityID", "q1"],
                    "response_json_summary": {"keys": ["success"], "success": True},
                },
                {
                    "action": "form-submit:verification",
                    "item_kind": "quiz",
                    "method": "",
                    "url": "https://tms.vghks.gov.tw/course/1",
                    "response_json_summary": {"verified": True},
                },
            ]
        )
        quiz = feature_map(report)["quiz_submission"]

        self.assertEqual(quiz.status, "requests_reproducible")
        self.assertIn("submit_endpoint", quiz.evidence)
        self.assertIn("token_or_hidden_fields", quiz.evidence)

    def test_cli_analyze_requests_reproduction_outputs_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "network.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "action": "open-only",
                        "item_kind": "reading",
                        "method": "GET",
                        "url": "https://tms.vghks.gov.tw/learning/reading/123",
                        "post_data_keys": [],
                        "response_json_summary": {},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            buffer = StringIO()
            with redirect_stdout(buffer):
                code = cli_module.main(["diag", "reproduction", "--input", str(path)])

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["report"]["observation_count"], 1)
        self.assertEqual(payload["report"]["features"][0]["feature"], "reading_video_completion")

    def test_analyze_file_reads_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "network.jsonl"
            path.write_text(
                '{"action":"form-open:rendered-form-metadata","item_kind":"survey","method":"","url":"","response_json_summary":{"radio_groups":1}}\n',
                encoding="utf-8",
            )
            report = analyze_requests_reproduction_file(path)

        self.assertEqual(report.observation_count, 1)
        self.assertEqual(feature_map(report)["survey_submission"].status, "requests_partial")


if __name__ == "__main__":
    unittest.main()
