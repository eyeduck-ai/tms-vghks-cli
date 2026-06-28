import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks import batch_login
from tms_vghks import captcha_recognizers
from tms_vghks.batch_login import (
    AccountLoginConfig,
    AccountsLoginConfig,
    DEFAULT_BATCH_CONCURRENCY,
    DEFAULT_SESSION_ROOT,
    load_accounts_config,
    run_batch_requests_login,
)
from tms_vghks.captcha_recognizers import (
    NoOcrTextError,
    OcrConfig,
    OcrResult,
    PaddleOcrApiConfig,
    PADDLEOCR_SDK_DEFAULT_PROFILE,
    PADDLEOCR_SDK_DISABLE_MKLDNN_ENV,
    PADDLEOCR_SDK_PROFILE_MODEL_NAMES,
    PaddleOcrSdkConfig,
    build_paddleocr_sdk_kwargs,
    compare_paddleocr_sdk_profiles,
    parse_paddleocr_sdk_profiles,
    parse_paddleocr_sdk_tiers,
    recognize_captcha,
    recognize_captcha_api,
    recognize_captcha_sdk,
)
from tms_vghks.cli import build_parser, to_jsonable
from tms_vghks.models import RequestsLoginChallenge, RequestsLoginResult


class FakeBatchSession:
    instances = []
    prepared_records = []
    submitted_records = []
    active_prepares = 0
    max_active_prepares = 0
    delay_prepare_seconds = 0.0
    fail_first_submit_for_session_dirs = set()
    submit_statuses_by_dir = {}
    submit_counts_by_dir = {}
    lock = threading.Lock()

    def __init__(self, base_url="https://example.test"):
        self.base_url = base_url
        self.prepared = []
        self.submitted = []
        self.show_captcha_values = []
        self.transient_policy = None
        FakeBatchSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def configure_transient_policy(self, retries=None, delay_seconds=None):
        self.transient_policy = (retries, delay_seconds)

    def prepare_requests_login(self, captcha_path, show_captcha=True, session_dir=".tms_session"):
        with FakeBatchSession.lock:
            FakeBatchSession.active_prepares += 1
            FakeBatchSession.max_active_prepares = max(
                FakeBatchSession.max_active_prepares,
                FakeBatchSession.active_prepares,
            )
        try:
            if FakeBatchSession.delay_prepare_seconds:
                time.sleep(FakeBatchSession.delay_prepare_seconds)
            self.prepared.append((str(captcha_path), show_captcha, session_dir))
            self.show_captcha_values.append(show_captcha)
            with FakeBatchSession.lock:
                FakeBatchSession.prepared_records.append((str(captcha_path), show_captcha, session_dir))
            return RequestsLoginChallenge(
                login_url=f"{self.base_url}/index/login",
                action_url=f"{self.base_url}/index/login",
                hidden_fields={"anticsrf": "token"},
                captcha_path=str(captcha_path),
            )
        finally:
            with FakeBatchSession.lock:
                FakeBatchSession.active_prepares -= 1

    def submit_requests_login(
        self,
        account="",
        password="",
        captcha="",
        challenge=None,
        save=True,
        session_dir=".tms_session",
        transient_retries=None,
        transient_delay_seconds=None,
    ):
        self.submitted.append(
            {
                "account": account,
                "password": password,
                "captcha": captcha,
                "session_dir": session_dir,
                "transient_retries": transient_retries,
                "transient_delay_seconds": transient_delay_seconds,
            }
        )
        with FakeBatchSession.lock:
            FakeBatchSession.submitted_records.append(self.submitted[-1])
            FakeBatchSession.submit_counts_by_dir[session_dir] = FakeBatchSession.submit_counts_by_dir.get(session_dir, 0) + 1
            submit_count = FakeBatchSession.submit_counts_by_dir[session_dir]
        configured_statuses = FakeBatchSession.submit_statuses_by_dir.get(session_dir, [])
        if submit_count <= len(configured_statuses):
            status = configured_statuses[submit_count - 1]
            return RequestsLoginResult(
                success=False,
                status=status,
                message=f"{status} message",
            )
        if session_dir in FakeBatchSession.fail_first_submit_for_session_dirs and submit_count == 1:
            return RequestsLoginResult(
                success=False,
                status="captcha_failed",
                message="驗證碼錯誤",
            )
        return RequestsLoginResult(
            success=True,
            status="logged_in",
            message="ok",
            requests_cookies_path=str(Path(session_dir) / "requests_cookies.json"),
            playwright_storage_state_path=str(Path(session_dir) / "playwright_storage_state.json"),
        )


class FakeHttpResponse:
    def __init__(self, status_code=200, payload=None, text="", json_error=None):
        self.status_code = status_code
        self.payload = payload
        self.text = text
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        if self.payload is not None:
            return self.payload
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError(f"HTTP {self.status_code}")


class BatchLoginTests(unittest.TestCase):
    def setUp(self):
        FakeBatchSession.instances = []
        FakeBatchSession.prepared_records = []
        FakeBatchSession.submitted_records = []
        FakeBatchSession.active_prepares = 0
        FakeBatchSession.max_active_prepares = 0
        FakeBatchSession.delay_prepare_seconds = 0.0
        FakeBatchSession.fail_first_submit_for_session_dirs = set()
        FakeBatchSession.submit_statuses_by_dir = {}
        FakeBatchSession.submit_counts_by_dir = {}
        captcha_recognizers._PADDLEOCR_INSTANCES.clear()

    def write_config(self, body: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "accounts.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_load_accounts_config_uses_tomllib_and_sdk_defaults(self):
        self.assertEqual(batch_login.tomllib.__name__, "tomllib")
        path = self.write_config(
            """
            [[accounts]]
            account = "a1"
            password = "p1"

            [[accounts]]
            label = "named user"
            account = "a2"
            password = "p2"
            """
        )
        config = load_accounts_config(path)
        self.assertEqual(config.captcha_mode, "paddleocr-sdk")
        self.assertEqual(config.concurrency, DEFAULT_BATCH_CONCURRENCY)
        self.assertEqual(config.ocr.sdk.profile, PADDLEOCR_SDK_DEFAULT_PROFILE)
        self.assertEqual(config.ocr.api.token, "")
        self.assertEqual(config.accounts[0].label, "account1")
        self.assertEqual(config.accounts[0].session_dir, str(Path(DEFAULT_SESSION_ROOT) / "account1"))
        self.assertEqual(config.accounts[1].label, "named user")
        self.assertEqual(config.accounts[1].session_dir, str(Path(DEFAULT_SESSION_ROOT) / "named_user"))

    def test_example_accounts_config_omits_captcha_mode(self):
        path = Path(__file__).resolve().parents[1] / ".tms_accounts.example.toml"
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("captcha_mode", text)

        config = load_accounts_config(path)

        self.assertEqual(config.captcha_mode, "paddleocr-sdk")

    def test_batch_login_does_not_depend_on_quiz_resolver(self):
        path = Path(__file__).resolve().parents[1] / "src" / "tms_vghks" / "batch_login.py"
        text = path.read_text(encoding="utf-8")

        self.assertNotIn("quiz_resolver", text)

    def test_load_accounts_config_accepts_top_level_gemini_settings(self):
        path = self.write_config(
            """
            [gemini]
            api_key = "gemini-key"
            model = "gemini-3.5-flash"

            [[accounts]]
            account = "a1"
            password = "p1"
            """
        )

        config = load_accounts_config(path)

        self.assertEqual(config.gemini.api_key, "gemini-key")
        self.assertEqual(config.gemini.model, "gemini-3.5-flash")

    def test_load_accounts_config_rejects_legacy_captcha_mode_key(self):
        for mode in ("manual", "paddleocr-sdk", "auto"):
            with self.subTest(mode=mode):
                path = self.write_config(
                    f"""
                    captcha_mode = "{mode}"

                    [[accounts]]
                    account = "a1"
                    password = "p1"
                    """
                )
                with self.assertRaisesRegex(ValueError, "captcha_mode was removed"):
                    load_accounts_config(path)

    def test_load_accounts_config_accepts_ocr_paddleocr_api_token(self):
        path = self.write_config(
            """
            [ocr]
            paddleocr_api_token = " token-123 "

            [[accounts]]
            account = "a1"
            password = "p1"
            """
        )

        config = load_accounts_config(path)

        self.assertEqual(config.ocr.api.token, "token-123")
        self.assertEqual(config.ocr.sdk.profile, PADDLEOCR_SDK_DEFAULT_PROFILE)

    def test_load_accounts_config_rejects_unknown_gemini_keys(self):
        path = self.write_config(
            """
            [gemini]
            api_key = "gemini-key"
            temperature = "0"

            [[accounts]]
            account = "a1"
            password = "p1"
            """
        )

        with self.assertRaisesRegex(ValueError, "unsupported TOML keys"):
            load_accounts_config(path)

    def test_load_accounts_config_rejects_invalid_paddleocr_api_token(self):
        cases = [
            '[ocr]\npaddleocr_api_token = ""',
            "[ocr]\npaddleocr_api_token = 123",
        ]
        for token_line in cases:
            with self.subTest(token_line=token_line):
                path = self.write_config(
                    f"""
                    {token_line}

                    [[accounts]]
                    account = "a1"
                    password = "p1"
                    """
                )
                with self.assertRaisesRegex(ValueError, "paddleocr_api_token"):
                    load_accounts_config(path)

    def test_load_accounts_config_rejects_duplicate_labels(self):
        path = self.write_config(
            """
            [[accounts]]
            label = "same"
            account = "a1"
            password = "p1"

            [[accounts]]
            label = "same"
            account = "a2"
            password = "p2"
            """
        )
        with self.assertRaisesRegex(ValueError, "duplicate account label"):
            load_accounts_config(path)

    def test_load_accounts_config_rejects_captcha_mode_key(self):
        for mode in ("manual", "paddleocr-sdk", "auto", "ocr-confirm", "paddleocr-api"):
            with self.subTest(mode=mode):
                path = self.write_config(
                    f"""
                    captcha_mode = "{mode}"

                    [[accounts]]
                    label = "a"
                    account = "a1"
                    password = "p1"
                    """
                )
                with self.assertRaisesRegex(ValueError, "captcha_mode was removed"):
                    load_accounts_config(path)

    def test_load_accounts_config_rejects_legacy_top_level_paddleocr_api_token(self):
        path = self.write_config(
            """
            paddleocr_api_token = "token-123"

            [[accounts]]
            account = "a1"
            password = "p1"
            """
        )

        with self.assertRaisesRegex(ValueError, "unsupported TOML keys.*paddleocr_api_token"):
            load_accounts_config(path)

    def test_load_accounts_config_rejects_expanded_ocr_schema_keys(self):
        cases = [
            """
            [ocr.api]
            endpoint_url = "https://ocr.test/ocr"

            [[accounts]]
            account = "a1"
            password = "p1"
            """,
            """
            endpoint_url = "https://ocr.test/ocr"

            [[accounts]]
            account = "a1"
            password = "p1"
            """,
            """
            model = "PP-OCRv5"

            [[accounts]]
            account = "a1"
            password = "p1"
            """,
        ]
        for body in cases:
            with self.subTest(body=body):
                path = self.write_config(body)
                with self.assertRaisesRegex(ValueError, "unsupported TOML keys"):
                    load_accounts_config(path)

    def test_load_accounts_config_rejects_session_dir_schema_key(self):
        path = self.write_config(
            """
            [[accounts]]
            account = "a1"
            password = "p1"
            session_dir = ".tms_session/custom"
            """
        )
        with self.assertRaisesRegex(ValueError, "unsupported TOML keys"):
            load_accounts_config(path)

    def test_paddleocr_sdk_config_maps_profiles(self):
        for profile, expected_models in PADDLEOCR_SDK_PROFILE_MODEL_NAMES.items():
            with self.subTest(profile=profile):
                kwargs = build_paddleocr_sdk_kwargs(PaddleOcrSdkConfig(profile=profile))
                self.assertEqual(kwargs["ocr_version"], expected_models[0])
                self.assertEqual(kwargs["text_detection_model_name"], expected_models[1])
                self.assertEqual(kwargs["text_recognition_model_name"], expected_models[2])
                self.assertEqual(kwargs["device"], "cpu")
                self.assertFalse(kwargs["use_doc_orientation_classify"])
                self.assertFalse(kwargs["use_doc_unwarping"])
                self.assertFalse(kwargs["use_textline_orientation"])

        with self.assertRaisesRegex(ValueError, "profile"):
            build_paddleocr_sdk_kwargs(PaddleOcrSdkConfig(profile="v7-large"))

    def test_paddleocr_sdk_parse_profiles_and_legacy_tiers(self):
        self.assertEqual(
            parse_paddleocr_sdk_profiles("v6-small, v6-tiny,v5-en-mobile"),
            ["v6-small", "v6-tiny", "v5-en-mobile"],
        )
        self.assertEqual(parse_paddleocr_sdk_tiers("medium, small,tiny"), ["v6-medium", "v6-small", "v6-tiny"])

    def test_paddleocr_sdk_predict_output_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "captcha.png"
            image.write_bytes(b"image")
            init_calls = []

            class FakePaddleOCR:
                def __init__(self, **kwargs):
                    init_calls.append(kwargs)

                def predict(self, image_path):
                    return [{"res": {"rec_texts": ["89", " 70"], "rec_scores": [0.9, 0.8]}}]

            fake_module = types.SimpleNamespace(PaddleOCR=FakePaddleOCR)
            with patch.dict(sys.modules, {"paddleocr": fake_module}):
                result = recognize_captcha_sdk(image, OcrConfig(sdk=PaddleOcrSdkConfig(profile="v6-small")))

        self.assertEqual(result.text, "8970")
        self.assertAlmostEqual(result.confidence, 0.85)
        self.assertEqual(init_calls[0]["text_detection_model_name"], "PP-OCRv6_small_det")
        self.assertEqual(init_calls[0]["text_recognition_model_name"], "PP-OCRv6_small_rec")
        self.assertEqual(init_calls[0]["device"], "cpu")

    def test_paddleocr_sdk_empty_text_is_no_ocr_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "captcha.png"
            image.write_bytes(b"image")

            class FakePaddleOCR:
                def __init__(self, **kwargs):
                    pass

                def predict(self, image_path):
                    return [{"res": {"rec_texts": [], "rec_scores": []}}]

            fake_module = types.SimpleNamespace(PaddleOCR=FakePaddleOCR)
            with patch.dict(sys.modules, {"paddleocr": fake_module}):
                with self.assertRaisesRegex(NoOcrTextError, "no_ocr_text"):
                    recognize_captcha_sdk(image, OcrConfig())
                diagnostic = compare_paddleocr_sdk_profiles(image, ["v6-small"])

        self.assertEqual(diagnostic[0].status, "no_ocr_text")
        self.assertFalse(diagnostic[0].success)

    def test_paddleocr_sdk_disables_paddlex_mkldnn_before_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "captcha.png"
            image.write_bytes(b"image")
            env_values = []

            class FakePaddleOCR:
                def __init__(self, **kwargs):
                    env_values.append(os.environ.get(PADDLEOCR_SDK_DISABLE_MKLDNN_ENV))

                def predict(self, image_path):
                    return [{"res": {"rec_texts": ["8970"], "rec_scores": [0.9]}}]

            fake_module = types.SimpleNamespace(PaddleOCR=FakePaddleOCR)
            with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {"paddleocr": fake_module}):
                recognize_captcha_sdk(image, OcrConfig(sdk=PaddleOcrSdkConfig(profile="v6-small")))

        self.assertEqual(env_values, ["0"])

    def test_paddleocr_sdk_singleton_cache_is_per_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "captcha.png"
            image.write_bytes(b"image")
            init_calls = []

            class FakePaddleOCR:
                def __init__(self, **kwargs):
                    init_calls.append(kwargs)

                def predict(self, image_path):
                    return [{"res": {"rec_texts": ["1234"], "rec_scores": [0.7]}}]

            fake_module = types.SimpleNamespace(PaddleOCR=FakePaddleOCR)
            with patch.dict(sys.modules, {"paddleocr": fake_module}):
                recognize_captcha_sdk(image, OcrConfig(sdk=PaddleOcrSdkConfig(profile="v6-small")))
                recognize_captcha_sdk(image, OcrConfig(sdk=PaddleOcrSdkConfig(profile="v6-tiny")))
                recognize_captcha_sdk(image, OcrConfig(sdk=PaddleOcrSdkConfig(profile="v5-en-mobile")))
                recognize_captcha_sdk(image, OcrConfig(sdk=PaddleOcrSdkConfig(profile="v6-small")))

        self.assertEqual(
            [call["text_detection_model_name"] for call in init_calls],
            ["PP-OCRv6_small_det", "PP-OCRv6_tiny_det", "PP-OCRv5_mobile_det"],
        )
        self.assertEqual(init_calls[2]["text_recognition_model_name"], "en_PP-OCRv5_mobile_rec")

    def test_paddleocr_api_submit_poll_and_jsonl_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "captcha.png"
            image.write_bytes(b"image")
            post_calls = []
            get_calls = []
            states = iter(["pending", "running", "done"])

            def fake_post(url, headers=None, data=None, files=None, timeout=None):
                post_calls.append((url, headers, data, files, timeout))
                self.assertIn("file", files)
                self.assertEqual(data["model"], "PP-OCRv6")
                optional_payload = json.loads(data["optionalPayload"])
                self.assertFalse(optional_payload["useDocOrientationClassify"])
                self.assertFalse(optional_payload["useDocUnwarping"])
                self.assertFalse(optional_payload["useTextlineOrientation"])
                return FakeHttpResponse(payload={"code": 0, "data": {"jobId": "job-1"}})

            def fake_get(url, headers=None, timeout=None):
                get_calls.append((url, headers, timeout))
                if url.endswith("/job-1"):
                    state = next(states)
                    payload = {"code": 0, "data": {"state": state}}
                    if state == "done":
                        payload["data"]["resultUrl"] = {"jsonUrl": "https://result.test/result.jsonl"}
                    return FakeHttpResponse(payload=payload)
                jsonl = json.dumps(
                    {"result": {"ocrResults": [{"rec_texts": ["12", " 34"], "rec_scores": [0.9, 0.8]}]}}
                )
                return FakeHttpResponse(text=jsonl)

            config = OcrConfig(
                api=PaddleOcrApiConfig(
                    token="token-123",
                    poll_interval_seconds=0,
                    timeout_seconds=1,
                    request_timeout_seconds=2,
                )
            )
            with patch("tms_vghks.captcha_recognizers.requests.post", side_effect=fake_post), patch(
                "tms_vghks.captcha_recognizers.requests.get",
                side_effect=fake_get,
            ):
                result = recognize_captcha_api(image, config)

        self.assertEqual(result.text, "1234")
        self.assertAlmostEqual(result.confidence, 0.85)
        self.assertEqual(result.source, "paddleocr-api")
        self.assertEqual(post_calls[0][1]["Authorization"], "bearer token-123")
        self.assertEqual([call[0] for call in get_calls[:3]], [f"{config.api.job_url}/job-1"] * 3)
        self.assertEqual(get_calls[-1][0], "https://result.test/result.jsonl")

    def test_paddleocr_sdk_success_skips_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "captcha.png"
            image.write_bytes(b"image")
            config = OcrConfig(api=PaddleOcrApiConfig(token="token-123"))
            with patch(
                "tms_vghks.captcha_recognizers.recognize_captcha_sdk",
                return_value=OcrResult("9999", 0.7, "paddleocr-sdk"),
            ), patch(
                "tms_vghks.captcha_recognizers.requests.post",
                side_effect=AssertionError("API should not be called when SDK succeeds"),
            ):
                result = recognize_captcha(image, config, "paddleocr-sdk")

        self.assertEqual(result.text, "9999")
        self.assertEqual(result.source, "paddleocr-sdk")

    def test_paddleocr_sdk_failure_falls_back_to_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "captcha.png"
            image.write_bytes(b"image")
            post_calls = []

            def fake_post(url, headers=None, data=None, files=None, timeout=None):
                post_calls.append(url)
                return FakeHttpResponse(payload={"code": 0, "data": {"jobId": "job-1"}})

            def fake_get(url, headers=None, timeout=None):
                if url.endswith("/job-1"):
                    return FakeHttpResponse(
                        payload={
                            "code": 0,
                            "data": {
                                "state": "done",
                                "resultUrl": {"jsonUrl": "https://result.test/result.jsonl"},
                            },
                        }
                    )
                jsonl = json.dumps({"result": {"ocrResults": [{"rec_texts": ["12", "34"], "rec_scores": [0.9, 0.8]}]}})
                return FakeHttpResponse(text=jsonl)

            config = OcrConfig(api=PaddleOcrApiConfig(token="token-123", poll_interval_seconds=0))
            with patch(
                "tms_vghks.captcha_recognizers.recognize_captcha_sdk",
                side_effect=NoOcrTextError("no_ocr_text: sdk failed"),
            ), patch("tms_vghks.captcha_recognizers.requests.post", side_effect=fake_post), patch(
                "tms_vghks.captcha_recognizers.requests.get",
                side_effect=fake_get,
            ):
                result = recognize_captcha(image, config, "paddleocr-sdk")

        self.assertEqual(result.text, "1234")
        self.assertEqual(result.source, "paddleocr-api")
        self.assertEqual(len(post_calls), 1)

    def test_paddleocr_api_and_sdk_failures_raise_combined_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "captcha.png"
            image.write_bytes(b"image")
            config = OcrConfig(api=PaddleOcrApiConfig(token="token-123"))
            with patch(
                "tms_vghks.captcha_recognizers.requests.post",
                return_value=FakeHttpResponse(status_code=401, text="invalid token"),
            ), patch(
                "tms_vghks.captcha_recognizers.recognize_captcha_sdk",
                side_effect=NoOcrTextError("no_ocr_text: sdk failed"),
            ):
                with self.assertRaisesRegex(ValueError, "PaddleOCR SDK failed.*PaddleOCR API fallback failed"):
                    recognize_captcha(image, config, "paddleocr-sdk")

    def test_run_batch_requests_login_manual_is_concurrent_and_isolated(self):
        config = AccountsLoginConfig(
            base_url="https://example.test",
            captcha_mode="manual",
            concurrency=2,
            accounts=[
                AccountLoginConfig("one", "a1", "p1", ".tms_session/accounts/one"),
                AccountLoginConfig("two", "a2", "p2", ".tms_session/accounts/two"),
                AccountLoginConfig("three", "a3", "p3", ".tms_session/accounts/three"),
            ],
        )
        FakeBatchSession.delay_prepare_seconds = 0.03
        answers = iter(["1111", "2222", "3333"])
        result = run_batch_requests_login(
            config,
            input_func=lambda prompt: next(answers),
            print_func=lambda message: None,
            session_factory=FakeBatchSession,
            show_captcha=False,
            transient_retries=1,
            transient_delay_seconds=0,
        )
        self.assertTrue(result.success)
        self.assertEqual(
            [row.session_dir for row in result.results],
            [".tms_session/accounts/one", ".tms_session/accounts/two", ".tms_session/accounts/three"],
        )
        self.assertGreaterEqual(FakeBatchSession.max_active_prepares, 2)
        submitted_by_dir = {row["session_dir"]: row for row in FakeBatchSession.submitted_records}
        self.assertEqual(submitted_by_dir[".tms_session/accounts/one"]["captcha"], "1111")
        self.assertEqual(submitted_by_dir[".tms_session/accounts/two"]["captcha"], "2222")
        self.assertEqual(submitted_by_dir[".tms_session/accounts/three"]["captcha"], "3333")
        self.assertEqual(FakeBatchSession.instances[0].transient_policy, (1, 0))

    def test_run_batch_requests_login_sdk_auto_retries_once_after_captcha_failure(self):
        config = AccountsLoginConfig(
            base_url="https://example.test",
            accounts=[
                AccountLoginConfig("accept", "a1", "p1", ".tms_session/accounts/accept"),
                AccountLoginConfig("override", "a2", "p2", ".tms_session/accounts/override"),
                AccountLoginConfig("skip", "a3", "p3", ".tms_session/accounts/skip"),
            ],
        )
        FakeBatchSession.fail_first_submit_for_session_dirs = {
            ".tms_session/accounts/accept",
            ".tms_session/accounts/override",
            ".tms_session/accounts/skip",
        }
        suggestions = []

        def ocr_func(path, ocr_config):
            suggestions.append((str(path), ocr_config.sdk.profile))
            return OcrResult("1234", 0.91)

        result = run_batch_requests_login(
            config,
            captcha_mode="paddleocr-sdk",
            input_func=lambda prompt: (_ for _ in ()).throw(AssertionError("manual fallback should not prompt")),
            print_func=lambda message: None,
            session_factory=FakeBatchSession,
            ocr_func=ocr_func,
            show_captcha=False,
        )
        self.assertTrue(result.success)
        submitted_by_dir = {}
        for row in FakeBatchSession.submitted_records:
            submitted_by_dir.setdefault(row["session_dir"], []).append(row)
        self.assertEqual(
            [row["captcha"] for row in submitted_by_dir[".tms_session/accounts/accept"]],
            ["1234", "1234"],
        )
        self.assertEqual(
            [row["captcha"] for row in submitted_by_dir[".tms_session/accounts/override"]],
            ["1234", "1234"],
        )
        self.assertEqual(
            [row["captcha"] for row in submitted_by_dir[".tms_session/accounts/skip"]],
            ["1234", "1234"],
        )
        prepared_paths = [row[0] for row in FakeBatchSession.prepared_records]
        self.assertTrue(any(path.endswith("captcha_retry.jpg") for path in prepared_paths))
        self.assertEqual(suggestions[0][1], "v6-small")
        self.assertEqual(len(suggestions), 6)

    def test_run_batch_requests_login_from_config_passes_api_token_to_ocr(self):
        path = self.write_config(
            """
            [ocr]
            paddleocr_api_token = "token-123"

            [[accounts]]
            label = "one"
            account = "a1"
            password = "p1"
            """
        )
        config = load_accounts_config(path)
        tokens = []

        def ocr_func(path, ocr_config):
            tokens.append(ocr_config.api.token)
            return OcrResult("1234", 0.91, "paddleocr-api")

        result = run_batch_requests_login(
            config,
            input_func=lambda prompt: (_ for _ in ()).throw(AssertionError("manual fallback should not prompt")),
            print_func=lambda message: None,
            session_factory=FakeBatchSession,
            ocr_func=ocr_func,
            show_captcha=False,
        )

        self.assertTrue(result.success)
        self.assertEqual(tokens, ["token-123"])
        self.assertEqual(FakeBatchSession.submitted_records[0]["captcha"], "1234")

    def test_run_batch_requests_login_uses_fresh_manual_challenge_after_three_ocr_failures(self):
        config = AccountsLoginConfig(
            base_url="https://example.test",
            accounts=[AccountLoginConfig("one", "a1", "p1", ".tms_session/accounts/one")],
        )
        FakeBatchSession.submit_statuses_by_dir = {
            ".tms_session/accounts/one": ["captcha_failed", "captcha_failed", "captcha_failed"],
        }
        answers = iter(["9999"])

        def ocr_func(path, ocr_config):
            if str(path).endswith("captcha.jpg"):
                return OcrResult("1111", 0.91)
            if str(path).endswith("captcha_retry.jpg"):
                return OcrResult("2222", 0.91)
            return OcrResult("3333", 0.91)

        result = run_batch_requests_login(
            config,
            captcha_mode="paddleocr-sdk",
            input_func=lambda prompt: next(answers),
            print_func=lambda message: None,
            session_factory=FakeBatchSession,
            ocr_func=ocr_func,
            show_captcha=False,
        )

        self.assertTrue(result.success)
        self.assertEqual(
            [row["captcha"] for row in FakeBatchSession.submitted_records],
            ["1111", "2222", "3333", "9999"],
        )
        prepared_paths = [row[0] for row in FakeBatchSession.prepared_records]
        self.assertTrue(any(path.endswith("captcha_retry.jpg") for path in prepared_paths))
        self.assertTrue(any(path.endswith("captcha_retry_3.jpg") for path in prepared_paths))
        self.assertTrue(any(path.endswith("captcha_manual.jpg") for path in prepared_paths))

    def test_run_batch_requests_login_stops_on_credential_failed_without_manual_retry(self):
        config = AccountsLoginConfig(
            base_url="https://example.test",
            accounts=[AccountLoginConfig("one", "a1", "p1", ".tms_session/accounts/one")],
        )
        FakeBatchSession.submit_statuses_by_dir = {
            ".tms_session/accounts/one": ["credential_failed"],
        }

        result = run_batch_requests_login(
            config,
            captcha_mode="paddleocr-sdk",
            input_func=lambda prompt: (_ for _ in ()).throw(AssertionError("credential failure must not prompt")),
            print_func=lambda message: None,
            session_factory=FakeBatchSession,
            ocr_func=lambda path, ocr_config: OcrResult("1111", 0.91),
            show_captcha=False,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.results[0].status, "credential_failed")
        self.assertEqual(len(FakeBatchSession.submitted_records), 1)
        self.assertFalse(any(row[0].endswith("captcha_retry.jpg") for row in FakeBatchSession.prepared_records))
        self.assertFalse(any(row[0].endswith("captcha_manual.jpg") for row in FakeBatchSession.prepared_records))

    def test_run_batch_requests_login_ocr_failure_reports_manual_required_without_submit(self):
        config = AccountsLoginConfig(
            base_url="https://tms.vghks.gov.tw",
            accounts=[
                AccountLoginConfig("one", "a1", "p1", ".tms_session/accounts/one"),
                AccountLoginConfig("two", "a2", "p2", ".tms_session/accounts/two"),
            ],
        )

        def ocr_func(path, ocr_config):
            if "one" in str(path):
                raise NoOcrTextError("no_ocr_text: missing text")
            return OcrResult("2222", 0.9)

        answers = iter([""])
        result = run_batch_requests_login(
            config,
            captcha_mode="paddleocr-sdk",
            input_func=lambda prompt: next(answers),
            print_func=lambda message: None,
            session_factory=FakeBatchSession,
            ocr_func=ocr_func,
            show_captcha=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.results[0].status, "manual_captcha_required")
        self.assertIn("no_ocr_text", result.results[0].message)
        submitted_by_dir = {row["session_dir"]: row for row in FakeBatchSession.submitted_records}
        self.assertNotIn(".tms_session/accounts/one", submitted_by_dir)
        self.assertEqual(submitted_by_dir[".tms_session/accounts/two"]["captcha"], "2222")
        prepared_paths = [row[0] for row in FakeBatchSession.prepared_records]
        self.assertTrue(any(path.endswith("captcha_manual.jpg") for path in prepared_paths))

    def test_run_batch_requests_login_ocr_failure_prompts_manual_fallback(self):
        config = AccountsLoginConfig(
            base_url="https://tms.vghks.gov.tw",
            accounts=[AccountLoginConfig("one", "a1", "p1", ".tms_session/accounts/one")],
        )
        answers = iter(["9999"])

        def ocr_func(path, ocr_config):
            raise NoOcrTextError("no_ocr_text: missing text")

        result = run_batch_requests_login(
            config,
            captcha_mode="paddleocr-sdk",
            input_func=lambda prompt: next(answers),
            print_func=lambda message: None,
            session_factory=FakeBatchSession,
            ocr_func=ocr_func,
            show_captcha=False,
        )

        self.assertTrue(result.success)
        self.assertEqual([row["captcha"] for row in FakeBatchSession.submitted_records], ["9999"])
        prepared_paths = [row[0] for row in FakeBatchSession.prepared_records]
        self.assertTrue(any(path.endswith("captcha_manual.jpg") for path in prepared_paths))

    def test_run_batch_requests_login_api_and_sdk_failure_reports_manual_required(self):
        config = AccountsLoginConfig(
            base_url="https://tms.vghks.gov.tw",
            ocr=OcrConfig(api=PaddleOcrApiConfig(token="token-123")),
            accounts=[AccountLoginConfig("one", "a1", "p1", ".tms_session/accounts/one")],
        )
        with patch(
            "tms_vghks.batch_login.recognize_captcha",
            side_effect=ValueError("PaddleOCR SDK failed: missing sdk; PaddleOCR API fallback failed: invalid token"),
        ):
            result = run_batch_requests_login(
                config,
                input_func=lambda prompt: "",
                print_func=lambda message: None,
                session_factory=FakeBatchSession,
                show_captcha=False,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.results[0].status, "manual_captcha_required")
        self.assertIn("PaddleOCR SDK failed", result.results[0].message)
        self.assertIn("PaddleOCR API fallback failed", result.results[0].message)
        self.assertEqual(FakeBatchSession.submitted_records, [])

    def test_cli_parser_rejects_removed_login_requests_batch(self):
        parser = build_parser()
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["login-requests", "batch", "--accounts", ".tms_accounts.toml"])

    def test_cli_parser_accepts_ocr_sdk_test_profiles_and_tiers(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "auth",
                "ocr-test",
                "--image",
                "captcha.png",
                "--profiles",
                "v6-small,v6-tiny,v5-en-mobile",
            ]
        )
        self.assertEqual(args.command, "ocr-sdk-test")
        self.assertEqual(args.image, "captcha.png")
        self.assertEqual(args.profiles, "v6-small,v6-tiny,v5-en-mobile")

        tier_args = parser.parse_args(["auth", "ocr-test", "--image", "captcha.png", "--tiers", "medium,small,tiny"])
        self.assertEqual(tier_args.tiers, "medium,small,tiny")

    def test_json_redaction_keeps_cookie_path_metadata(self):
        payload = to_jsonable(
            {
                "account": "a1",
                "password": "p1",
                "paddleocr_api_token": "token-123",
                "captcha": "1234",
                "cookies": [{"name": "PHPSESSID", "value": "secret"}],
                "requests_cookies_path": ".tms_session/accounts/a/requests_cookies.json",
                "url": "https://tms.vghks.gov.tw/kexam?key=secret",
            }
        )
        self.assertEqual(payload["account"], "REDACTED")
        self.assertEqual(payload["password"], "REDACTED")
        self.assertEqual(payload["paddleocr_api_token"], "REDACTED")
        self.assertEqual(payload["captcha"], "REDACTED")
        self.assertEqual(payload["cookies"], "REDACTED")
        self.assertEqual(payload["requests_cookies_path"], ".tms_session/accounts/a/requests_cookies.json")
        self.assertIn("key=REDACTED", payload["url"])


if __name__ == "__main__":
    unittest.main()
