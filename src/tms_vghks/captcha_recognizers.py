from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
import sys
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests


CAPTCHA_MODE_CHOICES = ("manual", "paddleocr-sdk")
CAPTCHA_MODES = set(CAPTCHA_MODE_CHOICES)

PADDLEOCR_API_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
PADDLEOCR_API_MODEL = "PP-OCRv6"
PADDLEOCR_API_SOURCE = "paddleocr-api"
PADDLEOCR_API_DEFAULT_TIMEOUT_SECONDS = 60.0
PADDLEOCR_API_DEFAULT_POLL_INTERVAL_SECONDS = 5.0
PADDLEOCR_API_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

PADDLEOCR_SDK_DEFAULT_PROFILE = "v6-small"
PADDLEOCR_SDK_DEFAULT_DEVICE = "cpu"
PADDLEOCR_SDK_DISABLE_MKLDNN_ENV = "PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"
PADDLEOCR_SDK_PROFILE_CHOICES = (
    "v6-medium",
    "v6-small",
    "v6-tiny",
    "v5-mobile",
    "v5-en-mobile",
    "v4-mobile",
    "v4-en-mobile",
)
PADDLEOCR_SDK_DEFAULT_DIAGNOSTIC_PROFILES = ("v6-small", "v6-tiny", "v5-en-mobile", "v4-en-mobile")
PADDLEOCR_SDK_PROFILE_MODEL_NAMES = {
    "v6-medium": ("PP-OCRv6", "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
    "v6-small": ("PP-OCRv6", "PP-OCRv6_small_det", "PP-OCRv6_small_rec"),
    "v6-tiny": ("PP-OCRv6", "PP-OCRv6_tiny_det", "PP-OCRv6_tiny_rec"),
    "v5-mobile": ("PP-OCRv5", "PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"),
    "v5-en-mobile": ("PP-OCRv5", "PP-OCRv5_mobile_det", "en_PP-OCRv5_mobile_rec"),
    "v4-mobile": ("PP-OCRv4", "PP-OCRv4_mobile_det", "PP-OCRv4_mobile_rec"),
    "v4-en-mobile": ("PP-OCRv4", "PP-OCRv4_mobile_det", "en_PP-OCRv4_mobile_rec"),
}
PADDLEOCR_SDK_LEGACY_TIER_MAP = {
    "medium": "v6-medium",
    "small": "v6-small",
    "tiny": "v6-tiny",
}
PADDLEOCR_SDK_TIERS = tuple(PADDLEOCR_SDK_LEGACY_TIER_MAP)

_PADDLEOCR_LOCK = threading.Lock()
_PADDLEOCR_INSTANCES: dict[tuple[str, str], Any] = {}


class NoOcrTextError(ValueError):
    """Raised when PaddleOCR returns successfully but no usable OCR text is present."""


@dataclass(slots=True)
class PaddleOcrSdkConfig:
    profile: str = PADDLEOCR_SDK_DEFAULT_PROFILE
    device: str = PADDLEOCR_SDK_DEFAULT_DEVICE


@dataclass(slots=True)
class PaddleOcrApiConfig:
    token: str = ""
    job_url: str = PADDLEOCR_API_JOB_URL
    model: str = PADDLEOCR_API_MODEL
    timeout_seconds: float = PADDLEOCR_API_DEFAULT_TIMEOUT_SECONDS
    poll_interval_seconds: float = PADDLEOCR_API_DEFAULT_POLL_INTERVAL_SECONDS
    request_timeout_seconds: float = PADDLEOCR_API_DEFAULT_REQUEST_TIMEOUT_SECONDS


@dataclass(slots=True)
class OcrConfig:
    sdk: PaddleOcrSdkConfig = field(default_factory=PaddleOcrSdkConfig)
    api: PaddleOcrApiConfig = field(default_factory=PaddleOcrApiConfig)


@dataclass(slots=True)
class OcrResult:
    text: str
    confidence: float | None = None
    source: str = ""


@dataclass(slots=True)
class OcrSdkDiagnosticResult:
    profile: str
    success: bool
    status: str
    text: str = ""
    confidence: float | None = None
    elapsed_seconds: float = 0.0
    error: str = ""
    source: str = "paddleocr-sdk"


class CaptchaRecognizer(Protocol):
    source: str

    def recognize(self, captcha_path: str | Path) -> OcrResult:
        ...


@dataclass(slots=True)
class ManualCaptchaRecognizer:
    source: str = "manual"

    def recognize(self, captcha_path: str | Path) -> OcrResult:
        raise ValueError("manual captcha mode does not produce OCR suggestions")


@dataclass(slots=True)
class PaddleOcrSdkRecognizer:
    config: OcrConfig
    source: str = "paddleocr-sdk"

    def recognize(self, captcha_path: str | Path) -> OcrResult:
        return recognize_captcha_sdk(captcha_path, self.config)


@dataclass(slots=True)
class PaddleOcrApiRecognizer:
    config: OcrConfig
    source: str = PADDLEOCR_API_SOURCE

    def recognize(self, captcha_path: str | Path) -> OcrResult:
        try:
            return recognize_captcha_sdk(captcha_path, self.config)
        except Exception as sdk_exc:
            try:
                return recognize_captcha_api(captcha_path, self.config)
            except Exception as api_exc:
                message = f"PaddleOCR SDK failed: {sdk_exc}; PaddleOCR API fallback failed: {api_exc}"
                raise ValueError(message) from api_exc


def validate_captcha_mode(value: str) -> None:
    if value not in CAPTCHA_MODES:
        allowed = ", ".join(sorted(CAPTCHA_MODES))
        raise ValueError(f"captcha_mode must be one of: {allowed}; use paddleocr-sdk for SDK/API OCR fallback")


def build_captcha_recognizer(captcha_mode: str, config: OcrConfig) -> CaptchaRecognizer:
    validate_captcha_mode(captcha_mode)
    if captcha_mode == "manual":
        return ManualCaptchaRecognizer()
    if captcha_mode == "paddleocr-sdk":
        if config.api.token:
            return PaddleOcrApiRecognizer(config)
        return PaddleOcrSdkRecognizer(config)
    raise ValueError(f"unsupported captcha mode: {captcha_mode}")


def recognize_captcha(captcha_path: str | Path, config: OcrConfig, captcha_mode: str) -> OcrResult:
    recognizer = build_captcha_recognizer(captcha_mode, config)
    return recognizer.recognize(captcha_path)


def recognize_captcha_api(captcha_path: str | Path, config: OcrConfig) -> OcrResult:
    image_path = Path(captcha_path)
    if not image_path.is_file():
        raise ValueError(f"captcha image not found: {image_path}")
    api = config.api
    token = api.token.strip()
    if not token:
        raise ValueError("paddleocr_api_token is required for PaddleOCR API recognition")

    headers = {"Authorization": f"bearer {token}"}
    optional_payload = {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useTextlineOrientation": False,
    }
    data = {
        "model": api.model,
        "optionalPayload": json.dumps(optional_payload),
    }
    with image_path.open("rb") as handle:
        response = requests.post(
            api.job_url,
            headers=headers,
            data=data,
            files={"file": handle},
            timeout=api.request_timeout_seconds,
        )
    payload = _paddleocr_api_json(response, "submit")
    _raise_for_paddleocr_api_code(payload, "submit")
    job_id = _paddleocr_api_job_id(payload)

    deadline = time.monotonic() + api.timeout_seconds
    while True:
        poll_response = requests.get(
            f"{api.job_url}/{job_id}",
            headers=headers,
            timeout=api.request_timeout_seconds,
        )
        poll_payload = _paddleocr_api_json(poll_response, "poll")
        _raise_for_paddleocr_api_code(poll_payload, "poll")
        data_payload = poll_payload.get("data")
        if not isinstance(data_payload, dict):
            raise ValueError("PaddleOCR API poll response missing data")
        state = str(data_payload.get("state") or "").strip().lower()
        if state == "done":
            result_url = data_payload.get("resultUrl")
            if not isinstance(result_url, dict) or not isinstance(result_url.get("jsonUrl"), str):
                raise ValueError("PaddleOCR API done response missing resultUrl.jsonUrl")
            jsonl_url = result_url["jsonUrl"]
            break
        if state == "failed":
            error = data_payload.get("errorMsg") or poll_payload.get("msg") or "unknown error"
            raise ValueError(f"PaddleOCR API job failed: {error}")
        if state not in {"pending", "running"}:
            raise ValueError(f"PaddleOCR API returned unknown job state: {state or '<empty>'}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"PaddleOCR API job timed out after {api.timeout_seconds:g}s")
        time.sleep(max(0.0, api.poll_interval_seconds))

    jsonl_response = requests.get(jsonl_url, timeout=api.request_timeout_seconds)
    jsonl_response.raise_for_status()
    result = _normalize_paddleocr_api_jsonl(jsonl_response.text)
    result.source = PADDLEOCR_API_SOURCE
    return result


def recognize_captcha_sdk(captcha_path: str | Path, config: OcrConfig) -> OcrResult:
    image_path = Path(captcha_path)
    if not image_path.is_file():
        raise ValueError(f"captcha image not found: {image_path}")
    with _PADDLEOCR_LOCK:
        engine = _get_paddleocr_instance(config.sdk)
        raw_result = _run_paddleocr_engine(engine, image_path, config.sdk)
    result = _normalize_paddleocr_result(raw_result)
    result.source = "paddleocr-sdk"
    return result


def compare_paddleocr_sdk_profiles(
    captcha_path: str | Path,
    profiles: list[str] | tuple[str, ...],
) -> list[OcrSdkDiagnosticResult]:
    image_path = Path(captcha_path)
    if not image_path.is_file():
        raise ValueError(f"captcha image not found: {image_path}")
    results: list[OcrSdkDiagnosticResult] = []
    for profile in profiles:
        profile = profile.strip().lower()
        start = time.perf_counter()
        try:
            config = OcrConfig(sdk=PaddleOcrSdkConfig(profile=profile))
            result = recognize_captcha_sdk(image_path, config)
            results.append(
                OcrSdkDiagnosticResult(
                    profile=profile,
                    success=True,
                    status="ok",
                    text=result.text,
                    confidence=result.confidence,
                    elapsed_seconds=time.perf_counter() - start,
                )
            )
        except NoOcrTextError as exc:
            results.append(
                OcrSdkDiagnosticResult(
                    profile=profile,
                    success=False,
                    status="no_ocr_text",
                    elapsed_seconds=time.perf_counter() - start,
                    error=str(exc),
                )
            )
        except Exception as exc:
            results.append(
                OcrSdkDiagnosticResult(
                    profile=profile,
                    success=False,
                    status="error",
                    elapsed_seconds=time.perf_counter() - start,
                    error=str(exc),
                )
            )
    return results


def compare_paddleocr_sdk_tiers(
    captcha_path: str | Path,
    tiers: list[str] | tuple[str, ...],
) -> list[OcrSdkDiagnosticResult]:
    profiles: list[str] = []
    for tier in tiers:
        tier = tier.strip().lower()
        if tier not in PADDLEOCR_SDK_LEGACY_TIER_MAP:
            allowed = ", ".join(PADDLEOCR_SDK_TIERS)
            raise ValueError(f"paddleocr sdk tier must be one of: {allowed}")
        profiles.append(PADDLEOCR_SDK_LEGACY_TIER_MAP[tier])
    return compare_paddleocr_sdk_profiles(captcha_path, profiles)


def parse_paddleocr_sdk_profiles(value: str) -> list[str]:
    profiles = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not profiles:
        raise ValueError("profiles must include at least one value")
    for profile in profiles:
        validate_paddleocr_sdk_profile(profile)
    return profiles


def parse_paddleocr_sdk_tiers(value: str) -> list[str]:
    tiers = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not tiers:
        raise ValueError("tiers must include at least one value")
    profiles: list[str] = []
    for tier in tiers:
        if tier not in PADDLEOCR_SDK_LEGACY_TIER_MAP:
            allowed = ", ".join(PADDLEOCR_SDK_TIERS)
            raise ValueError(f"paddleocr sdk tier must be one of: {allowed}")
        profiles.append(PADDLEOCR_SDK_LEGACY_TIER_MAP[tier])
    return profiles


def validate_paddleocr_sdk_profile(profile: str) -> None:
    if profile not in PADDLEOCR_SDK_PROFILE_MODEL_NAMES:
        allowed = ", ".join(PADDLEOCR_SDK_PROFILE_CHOICES)
        raise ValueError(f"paddleocr sdk profile must be one of: {allowed}")


def build_paddleocr_sdk_kwargs(config: PaddleOcrSdkConfig) -> dict[str, Any]:
    validate_paddleocr_sdk_profile(config.profile)
    ocr_version, text_detection_model_name, text_recognition_model_name = PADDLEOCR_SDK_PROFILE_MODEL_NAMES[config.profile]
    return {
        "ocr_version": ocr_version,
        "text_detection_model_name": text_detection_model_name,
        "text_recognition_model_name": text_recognition_model_name,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "device": config.device,
    }


def _get_paddleocr_instance(config: PaddleOcrSdkConfig):
    config_key = (config.profile, config.device)
    if config_key in _PADDLEOCR_INSTANCES:
        return _PADDLEOCR_INSTANCES[config_key]
    os.environ[PADDLEOCR_SDK_DISABLE_MKLDNN_ENV] = "0"
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise ValueError("paddleocr is not installed; run `uv sync --extra ocr-sdk` first") from exc
    kwargs = build_paddleocr_sdk_kwargs(config)
    kwargs = _filter_callable_kwargs(PaddleOCR, kwargs)
    try:
        with contextlib.redirect_stdout(sys.stderr), warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="`lang` and `ocr_version` will be ignored.*")
            _PADDLEOCR_INSTANCES[config_key] = PaddleOCR(**kwargs)
    except Exception as exc:
        raise ValueError(f"PaddleOCR SDK initialization failed with options {kwargs}: {exc}") from exc
    return _PADDLEOCR_INSTANCES[config_key]


def _run_paddleocr_engine(engine, image_path: Path, config: PaddleOcrSdkConfig):
    with contextlib.redirect_stdout(sys.stderr):
        if hasattr(engine, "predict"):
            try:
                return engine.predict(str(image_path))
            except TypeError:
                return engine.predict(input=str(image_path))
        if hasattr(engine, "ocr"):
            try:
                return engine.ocr(str(image_path))
            except TypeError:
                return engine.ocr(img=str(image_path))
    raise ValueError("PaddleOCR SDK object has no ocr() or predict() method")


def _normalize_paddleocr_result(raw_result: Any) -> OcrResult:
    texts: list[str] = []
    scores: list[float] = []
    _collect_paddleocr_text_scores(raw_result, texts, scores)
    text = re.sub(r"\s+", "", "".join(texts))
    if not text:
        raise NoOcrTextError("no_ocr_text: PaddleOCR SDK response missing text")
    confidence = sum(scores) / len(scores) if scores else None
    return OcrResult(text=text, confidence=confidence, source="paddleocr-sdk")


def _normalize_paddleocr_api_jsonl(value: str) -> OcrResult:
    texts: list[str] = []
    scores: list[float] = []
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"PaddleOCR API JSONL line {line_number} is invalid JSON: {exc}") from exc
        result = payload.get("result") if isinstance(payload, dict) else payload
        _collect_paddleocr_text_scores(result, texts, scores)
    text = re.sub(r"\s+", "", "".join(texts))
    if not text:
        raise NoOcrTextError("no_ocr_text: PaddleOCR API response missing text")
    confidence = sum(scores) / len(scores) if scores else None
    return OcrResult(text=text, confidence=confidence, source=PADDLEOCR_API_SOURCE)


def _collect_paddleocr_text_scores(value: Any, texts: list[str], scores: list[float]) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        before_count = len(texts)
        _collect_rec_texts_scores(value.get("rec_texts"), value.get("rec_scores"), texts, scores)
        if len(texts) > before_count:
            return
        for key in ("text", "label", "rec_text", "recText"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                texts.append(item.strip())
                _append_score(value.get("confidence") or value.get("score"), scores)
                return
        for key in ("data", "result", "results", "res", "ocrResults", "layoutParsingResults", "prunedResult"):
            if key in value:
                _collect_paddleocr_text_scores(value[key], texts, scores)
        return
    for attr in ("to_dict", "dict", "json"):
        item = getattr(value, attr, None)
        if callable(item):
            try:
                item = item()
            except Exception:
                continue
        if item is not None:
            _collect_paddleocr_text_scores(item, texts, scores)
            if texts:
                return
    res = getattr(value, "res", None)
    if res is not None:
        _collect_paddleocr_text_scores(res, texts, scores)
        if texts:
            return
    rec_texts = getattr(value, "rec_texts", None)
    rec_scores = getattr(value, "rec_scores", None)
    if rec_texts is not None:
        _collect_rec_texts_scores(rec_texts, rec_scores, texts, scores)
        return
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and isinstance(value[0], str):
            if value[0].strip():
                texts.append(value[0].strip())
                _append_score(value[1], scores)
            return
        if (
            len(value) >= 2
            and isinstance(value[1], (list, tuple))
            and len(value[1]) >= 2
            and isinstance(value[1][0], str)
        ):
            if value[1][0].strip():
                texts.append(value[1][0].strip())
                _append_score(value[1][1], scores)
            return
        for item in value:
            _collect_paddleocr_text_scores(item, texts, scores)


def _collect_rec_texts_scores(raw_texts: Any, raw_scores: Any, texts: list[str], scores: list[float]) -> None:
    if isinstance(raw_texts, str):
        raw_texts = [raw_texts]
    if not isinstance(raw_texts, (list, tuple)):
        return
    score_items = raw_scores if isinstance(raw_scores, (list, tuple)) else []
    for index, text in enumerate(raw_texts):
        if not isinstance(text, str) or not text.strip():
            continue
        texts.append(text.strip())
        if index < len(score_items):
            _append_score(score_items[index], scores)


def _append_score(value: Any, scores: list[float]) -> None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return
    scores.append(score)


def _paddleocr_api_json(response: requests.Response, context: str) -> dict[str, Any]:
    if response.status_code != 200:
        raise ValueError(f"PaddleOCR API {context} failed with HTTP {response.status_code}: {_response_excerpt(response)}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"PaddleOCR API {context} returned invalid JSON: {_response_excerpt(response)}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"PaddleOCR API {context} returned non-object JSON")
    return payload


def _raise_for_paddleocr_api_code(payload: dict[str, Any], context: str) -> None:
    code = payload.get("code")
    if code in (None, 0):
        return
    message = payload.get("msg") or payload.get("message") or "unknown error"
    raise ValueError(f"PaddleOCR API {context} failed with code {code}: {message}")


def _paddleocr_api_job_id(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("jobId"), str) or not data["jobId"].strip():
        raise ValueError("PaddleOCR API submit response missing data.jobId")
    return data["jobId"].strip()


def _response_excerpt(response: requests.Response, limit: int = 300) -> str:
    text = getattr(response, "text", "")
    return text[:limit]


def _filter_callable_kwargs(target: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return kwargs
    parameters = signature.parameters
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in parameters}
