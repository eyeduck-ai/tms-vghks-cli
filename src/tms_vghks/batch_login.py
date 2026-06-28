from __future__ import annotations

import re
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .captcha_recognizers import (
    OcrConfig,
    OcrResult,
    PaddleOcrApiConfig,
    PaddleOcrSdkConfig,
    recognize_captcha,
    validate_captcha_mode as _validate_captcha_mode,
)
from .config import DEFAULT_GEMINI_MODEL, GeminiQuizConfig
from .models import RequestsLoginChallenge, RequestsLoginResult
from .parsers import BASE_URL
from .session import TmsSession


DEFAULT_ACCOUNTS_PATH = ".tms_accounts.toml"
DEFAULT_SESSION_ROOT = ".tms_session/accounts"
DEFAULT_CAPTCHA_MODE = "paddleocr-sdk"
DEFAULT_BATCH_CONCURRENCY = 4
MAX_OCR_LOGIN_ATTEMPTS = 3
AUTO_RETRY_STATUSES = {"captcha_failed"}
MANUAL_FALLBACK_STATUSES = {"captcha_failed", "login_failed"}
ACCOUNTS_CONFIG_KEYS = {"ocr", "gemini", "accounts"}
ACCOUNT_CONFIG_KEYS = {"label", "account", "password"}
OCR_CONFIG_KEYS = {"paddleocr_api_token"}
GEMINI_CONFIG_KEYS = {"api_key", "model"}


@dataclass(slots=True)
class AccountLoginConfig:
    label: str
    account: str
    password: str
    session_dir: str


@dataclass(slots=True)
class AccountsLoginConfig:
    base_url: str = BASE_URL
    session_root: str = DEFAULT_SESSION_ROOT
    captcha_mode: str = DEFAULT_CAPTCHA_MODE
    concurrency: int = DEFAULT_BATCH_CONCURRENCY
    ocr: OcrConfig = field(default_factory=OcrConfig)
    accounts: list[AccountLoginConfig] = field(default_factory=list)
    gemini: GeminiQuizConfig = field(default_factory=GeminiQuizConfig)


@dataclass(slots=True)
class BatchAccountLoginResult:
    label: str
    success: bool
    status: str
    message: str = ""
    session_dir: str = ""
    requests_cookies_path: str | None = None
    playwright_storage_state_path: str | None = None
    handled_multi_login: bool = False
    multi_login_action: str = ""
    multi_login_status: str = ""


@dataclass(slots=True)
class BatchLoginResult:
    success: bool
    results: list[BatchAccountLoginResult]


@dataclass(slots=True)
class PreparedAccountLogin:
    account: AccountLoginConfig
    success: bool
    status: str
    message: str = ""
    captcha_path: str = ""
    challenge: RequestsLoginChallenge | None = None
    ocr_result: OcrResult | None = None


@dataclass(slots=True)
class ConfirmedAccountLogin:
    index: int
    account: AccountLoginConfig
    challenge: RequestsLoginChallenge
    captcha: str


InputFunc = Callable[[str], str]
PrintFunc = Callable[[str], None]
SessionFactory = Callable[..., TmsSession]
OcrFunc = Callable[[str | Path, OcrConfig], OcrResult]


def load_accounts_config(path: str | Path = DEFAULT_ACCOUNTS_PATH) -> AccountsLoginConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise ValueError("accounts config must be a TOML table")

    if "captcha_mode" in data:
        raise ValueError(
            "captcha_mode was removed from accounts config; remove the key. "
            "PaddleOCR SDK is the default and manual captcha fallback is automatic."
        )
    _reject_unsupported_config_keys(data, ACCOUNTS_CONFIG_KEYS, "accounts config")

    ocr = _load_ocr_config(data.get("ocr"))
    gemini = _load_gemini_config(data.get("gemini"))

    rows = data.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise ValueError("accounts config requires at least one [[accounts]] entry")

    accounts: list[AccountLoginConfig] = []
    labels: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"accounts entry #{index} must be a TOML table")
        _reject_unsupported_config_keys(row, ACCOUNT_CONFIG_KEYS, f"accounts entry #{index}")
        label = _optional_str(row, "label", f"account{index}")
        if label in labels:
            raise ValueError(f"duplicate account label: {label}")
        labels.add(label)
        account = _required_str(row, "account", f"accounts entry {label}")
        password = _required_str(row, "password", f"accounts entry {label}", strip=False)
        session_dir = str(Path(DEFAULT_SESSION_ROOT) / _safe_path_segment(label))
        accounts.append(
            AccountLoginConfig(
                label=label,
                account=account,
                password=password,
                session_dir=session_dir,
            )
        )

    return AccountsLoginConfig(
        base_url=BASE_URL,
        session_root=DEFAULT_SESSION_ROOT,
        captcha_mode=DEFAULT_CAPTCHA_MODE,
        concurrency=DEFAULT_BATCH_CONCURRENCY,
        ocr=ocr,
        gemini=gemini,
        accounts=accounts,
    )


def run_batch_requests_login_from_file(
    path: str | Path = DEFAULT_ACCOUNTS_PATH,
    captcha_mode: str | None = None,
    concurrency: int | None = None,
    show_captcha: bool = False,
    transient_retries: int | None = None,
    transient_delay_seconds: float | None = None,
    input_func: InputFunc = input,
    print_func: PrintFunc = print,
    session_factory: SessionFactory = TmsSession,
    ocr_func: OcrFunc | None = None,
) -> BatchLoginResult:
    config = load_accounts_config(path)
    return run_batch_requests_login(
        config,
        captcha_mode=captcha_mode,
        concurrency=concurrency,
        show_captcha=show_captcha,
        transient_retries=transient_retries,
        transient_delay_seconds=transient_delay_seconds,
        input_func=input_func,
        print_func=print_func,
        session_factory=session_factory,
        ocr_func=ocr_func,
    )


def run_batch_requests_login(
    config: AccountsLoginConfig,
    captcha_mode: str | None = None,
    concurrency: int | None = None,
    show_captcha: bool = False,
    transient_retries: int | None = None,
    transient_delay_seconds: float | None = None,
    input_func: InputFunc = input,
    print_func: PrintFunc = print,
    session_factory: SessionFactory = TmsSession,
    ocr_func: OcrFunc | None = None,
) -> BatchLoginResult:
    mode = captcha_mode or config.captcha_mode
    _validate_captcha_mode(mode)
    worker_count = _resolve_concurrency(concurrency if concurrency is not None else config.concurrency, len(config.accounts))
    prepared = _prepare_accounts_concurrently(
        config,
        captcha_mode=mode,
        show_captcha=show_captcha,
        transient_retries=transient_retries,
        transient_delay_seconds=transient_delay_seconds,
        session_factory=session_factory,
        ocr_func=ocr_func,
        worker_count=worker_count,
    )
    result_slots: list[BatchAccountLoginResult | None] = [None] * len(prepared)
    auto_confirmed: list[ConfirmedAccountLogin] = []
    manual_rows: list[PreparedAccountLogin] = []
    manual_prepare_indices: list[int] = []
    for index, row in enumerate(prepared):
        if not row.success or row.challenge is None:
            result_slots[index] = _account_result_from_prepared(row)
            continue
        if mode == "manual":
            manual_rows.append(row)
            continue
        if row.ocr_result is None:
            result_slots[index] = _account_result_from_prepared(row)
            manual_prepare_indices.append(index)
            continue
        auto_confirmed.append(
            ConfirmedAccountLogin(index=index, account=row.account, challenge=row.challenge, captcha=row.ocr_result.text)
        )

    prepared_by_index = {index: row for index, row in enumerate(prepared)}
    retry_indices: list[int] = []
    for index, result in _submit_accounts_concurrently(
        auto_confirmed,
        config=config,
        transient_retries=transient_retries,
        transient_delay_seconds=transient_delay_seconds,
        session_factory=session_factory,
        worker_count=worker_count,
    ):
        if result.success:
            result_slots[index] = result
            continue
        result_slots[index] = result
        if _should_retry_ocr_after_auto_failure(result.status):
            retry_indices.append(index)
        elif _should_prompt_manual_after_auto_failure(result.status):
            manual_prepare_indices.append(index)

    for ocr_attempt_number in range(2, MAX_OCR_LOGIN_ATTEMPTS + 1):
        if not retry_indices:
            break
        retry_prepared = _prepare_indexed_accounts_concurrently(
            retry_indices,
            prepared_by_index=prepared_by_index,
            config=config,
            captcha_mode=mode,
            show_captcha=show_captcha,
            transient_retries=transient_retries,
            transient_delay_seconds=transient_delay_seconds,
            session_factory=session_factory,
            ocr_func=ocr_func,
            worker_count=worker_count,
            captcha_filename=_ocr_retry_captcha_filename(ocr_attempt_number),
            allow_ocr_failure=False,
        )
        retry_confirmed: list[ConfirmedAccountLogin] = []
        next_retry_indices: list[int] = []
        for index, row in retry_prepared.items():
            if not row.success or row.challenge is None or row.ocr_result is None:
                result_slots[index] = _account_result_from_prepared(row)
                if row.challenge is not None:
                    manual_prepare_indices.append(index)
                continue
            retry_confirmed.append(
                ConfirmedAccountLogin(index=index, account=row.account, challenge=row.challenge, captcha=row.ocr_result.text)
            )

        for index, result in _submit_accounts_concurrently(
            retry_confirmed,
            config=config,
            transient_retries=transient_retries,
            transient_delay_seconds=transient_delay_seconds,
            session_factory=session_factory,
            worker_count=worker_count,
        ):
            result_slots[index] = result
            if result.success:
                continue
            if _should_retry_ocr_after_auto_failure(result.status) and ocr_attempt_number < MAX_OCR_LOGIN_ATTEMPTS:
                next_retry_indices.append(index)
            elif _should_prompt_manual_after_auto_failure(result.status):
                manual_prepare_indices.append(index)
        retry_indices = next_retry_indices

    manual_prepared = _prepare_indexed_accounts_concurrently(
        _unique_indices(manual_prepare_indices),
        prepared_by_index=prepared_by_index,
        config=config,
        captcha_mode=mode,
        show_captcha=show_captcha,
        transient_retries=transient_retries,
        transient_delay_seconds=transient_delay_seconds,
        session_factory=session_factory,
        ocr_func=ocr_func,
        worker_count=worker_count,
        captcha_filename="captcha_manual.jpg",
        allow_ocr_failure=True,
    )
    for index, row in manual_prepared.items():
        if not row.success or row.challenge is None:
            result_slots[index] = _account_result_from_prepared(row)
            continue
        manual_rows.append(row)

    manual_confirmed = _confirm_manual_rows(
        manual_rows,
        prepared_by_index=prepared_by_index,
        result_slots=result_slots,
        captcha_mode=mode,
        input_func=input_func,
        print_func=print_func,
    )
    for index, result in _submit_accounts_concurrently(
        manual_confirmed,
        config=config,
        transient_retries=transient_retries,
        transient_delay_seconds=transient_delay_seconds,
        session_factory=session_factory,
        worker_count=worker_count,
    ):
        result_slots[index] = result
    results = [result for result in result_slots if result is not None]
    return BatchLoginResult(success=all(result.success for result in results), results=results)


def _prepare_accounts_concurrently(
    config: AccountsLoginConfig,
    captcha_mode: str,
    show_captcha: bool,
    transient_retries: int | None,
    transient_delay_seconds: float | None,
    session_factory: SessionFactory,
    ocr_func: OcrFunc | None,
    worker_count: int,
) -> list[PreparedAccountLogin]:
    results: list[PreparedAccountLogin | None] = [None] * len(config.accounts)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _prepare_one_account,
                account,
                config,
                captcha_mode,
                show_captcha,
                transient_retries,
                transient_delay_seconds,
                session_factory,
                ocr_func,
                "captcha.jpg",
                True,
            ): index
            for index, account in enumerate(config.accounts)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [result for result in results if result is not None]


def _prepare_one_account(
    account: AccountLoginConfig,
    config: AccountsLoginConfig,
    captcha_mode: str,
    show_captcha: bool,
    transient_retries: int | None,
    transient_delay_seconds: float | None,
    session_factory: SessionFactory,
    ocr_func: OcrFunc | None,
    captcha_filename: str = "captcha.jpg",
    allow_ocr_failure: bool = False,
) -> PreparedAccountLogin:
    session_dir = account.session_dir
    captcha_path = Path(session_dir) / captcha_filename
    try:
        with session_factory(base_url=config.base_url) as session:
            session.configure_transient_policy(transient_retries, transient_delay_seconds)
            challenge = session.prepare_requests_login(
                captcha_path=captcha_path,
                show_captcha=show_captcha,
                session_dir=session_dir,
            )
            prepared = PreparedAccountLogin(
                account=account,
                success=True,
                status="prepared",
                captcha_path=str(challenge.captcha_path or captcha_path),
                challenge=challenge,
            )
    except Exception as exc:
        return PreparedAccountLogin(
            account=account,
            success=False,
            status="error",
            message=str(exc),
            captcha_path=str(captcha_path),
        )
    if captcha_mode == "manual":
        return prepared
    try:
        prepared.ocr_result = _recognize_captcha_for_mode(prepared.captcha_path, config.ocr, captcha_mode, ocr_func)
        if not prepared.ocr_result.source:
            prepared.ocr_result.source = captcha_mode
        prepared.status = "ocr_ready"
        return prepared
    except Exception as exc:
        if allow_ocr_failure:
            prepared.status = "ocr_unavailable"
            prepared.message = str(exc)
            return prepared
        return PreparedAccountLogin(
            account=account,
            success=False,
            status="ocr_failed",
            message=str(exc),
            captcha_path=prepared.captcha_path,
            challenge=challenge,
        )


def _confirm_captcha(
    prepared: PreparedAccountLogin,
    captcha_mode: str,
    input_func: InputFunc,
    print_func: PrintFunc,
    previous_result: BatchAccountLoginResult | None = None,
) -> str:
    prompt_mode = captcha_mode if prepared.ocr_result is not None else "manual"
    return confirm_captcha_cli(
        label=prepared.account.label,
        captcha_path=prepared.captcha_path,
        captcha_mode=prompt_mode,
        ocr_result=prepared.ocr_result,
        previous_status=None if previous_result is None else previous_result.status,
        previous_message=None if previous_result is None else previous_result.message,
        input_func=input_func,
        print_func=print_func,
    )


def confirm_captcha_cli(
    label: str,
    captcha_path: str | Path,
    captcha_mode: str,
    ocr_result: OcrResult | None,
    previous_status: str | None = None,
    previous_message: str | None = None,
    input_func: InputFunc = input,
    print_func: PrintFunc = print,
) -> str:
    if previous_status:
        suffix = f": {previous_message}" if previous_message else ""
        print_func(f"[{label}] Previous login attempt failed ({previous_status}){suffix}")
    if captcha_mode == "manual":
        print_func(f"[{label}] Captcha image: {captcha_path}")
        return input_func(f"[{label}] Captcha: ").strip()

    if ocr_result is None:
        raise ValueError("OCR result is required for captcha confirmation")
    source = ocr_result.source or captcha_mode
    confidence = "" if ocr_result.confidence is None else f" confidence={ocr_result.confidence:.3f}"
    print_func(f"[{label}] Captcha image: {captcha_path}")
    print_func(f"[{label}] OCR source: {source}")
    print_func(f"[{label}] OCR suggestion:{confidence} {ocr_result.text}")
    answer = input_func(f"[{label}] Press Enter to accept, type override, or type skip: ").strip()
    if not answer:
        return ocr_result.text
    if answer.lower() == "skip":
        return ""
    return answer


def _confirm_manual_rows(
    rows: list[PreparedAccountLogin],
    prepared_by_index: dict[int, PreparedAccountLogin],
    result_slots: list[BatchAccountLoginResult | None],
    captcha_mode: str,
    input_func: InputFunc,
    print_func: PrintFunc,
) -> list[ConfirmedAccountLogin]:
    confirmed: list[ConfirmedAccountLogin] = []
    index_by_label = {row.account.label: index for index, row in prepared_by_index.items()}
    for row in rows:
        index = index_by_label[row.account.label]
        captcha = _confirm_captcha(
            row,
            captcha_mode=captcha_mode,
            previous_result=result_slots[index],
            input_func=input_func,
            print_func=print_func,
        )
        if not captcha:
            if captcha_mode != "manual":
                previous = result_slots[index]
                detail = f": {previous.message}" if previous is not None and previous.message else ""
                result_slots[index] = BatchAccountLoginResult(
                    label=row.account.label,
                    success=False,
                    status="manual_captcha_required",
                    message=f"OCR captcha failed; manual captcha is required{detail}",
                    session_dir=row.account.session_dir,
                )
                continue
            result_slots[index] = BatchAccountLoginResult(
                label=row.account.label,
                success=False,
                status="skipped",
                message="captcha was not confirmed",
                session_dir=row.account.session_dir,
            )
            continue
        confirmed.append(ConfirmedAccountLogin(index=index, account=row.account, challenge=row.challenge, captcha=captcha))
    return confirmed


def _should_prompt_manual_after_auto_failure(status: str) -> bool:
    return status in MANUAL_FALLBACK_STATUSES


def _should_retry_ocr_after_auto_failure(status: str) -> bool:
    return status in AUTO_RETRY_STATUSES


def _prepare_indexed_accounts_concurrently(
    indices: list[int],
    prepared_by_index: dict[int, PreparedAccountLogin],
    config: AccountsLoginConfig,
    captcha_mode: str,
    show_captcha: bool,
    transient_retries: int | None,
    transient_delay_seconds: float | None,
    session_factory: SessionFactory,
    ocr_func: OcrFunc | None,
    worker_count: int,
    captcha_filename: str,
    allow_ocr_failure: bool,
) -> dict[int, PreparedAccountLogin]:
    if not indices:
        return {}
    results: dict[int, PreparedAccountLogin] = {}
    with ThreadPoolExecutor(max_workers=min(worker_count, len(indices))) as executor:
        futures = {
            executor.submit(
                _prepare_one_account,
                prepared_by_index[index].account,
                config,
                captcha_mode,
                show_captcha,
                transient_retries,
                transient_delay_seconds,
                session_factory,
                ocr_func,
                captcha_filename,
                allow_ocr_failure,
            ): index
            for index in indices
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


def _unique_indices(indices: list[int]) -> list[int]:
    seen: set[int] = set()
    unique: list[int] = []
    for index in indices:
        if index in seen:
            continue
        seen.add(index)
        unique.append(index)
    return unique


def _ocr_retry_captcha_filename(ocr_attempt_number: int) -> str:
    if ocr_attempt_number <= 2:
        return "captcha_retry.jpg"
    return f"captcha_retry_{ocr_attempt_number}.jpg"


def _submit_accounts_concurrently(
    confirmed: list[ConfirmedAccountLogin],
    config: AccountsLoginConfig,
    transient_retries: int | None,
    transient_delay_seconds: float | None,
    session_factory: SessionFactory,
    worker_count: int,
) -> list[tuple[int, BatchAccountLoginResult]]:
    if not confirmed:
        return []
    results: list[tuple[int, BatchAccountLoginResult] | None] = [None] * len(confirmed)
    with ThreadPoolExecutor(max_workers=min(worker_count, len(confirmed))) as executor:
        futures = {
            executor.submit(
                _submit_one_account,
                row,
                config,
                transient_retries,
                transient_delay_seconds,
                session_factory,
            ): index
            for index, row in enumerate(confirmed)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [result for result in results if result is not None]


def _submit_one_account(
    row: ConfirmedAccountLogin,
    config: AccountsLoginConfig,
    transient_retries: int | None,
    transient_delay_seconds: float | None,
    session_factory: SessionFactory,
) -> tuple[int, BatchAccountLoginResult]:
    try:
        with session_factory(base_url=config.base_url) as session:
            session.configure_transient_policy(transient_retries, transient_delay_seconds)
            result = session.submit_requests_login(
                account=row.account.account,
                password=row.account.password,
                captcha=row.captcha,
                challenge=row.challenge,
                save=True,
                session_dir=row.account.session_dir,
                transient_retries=transient_retries,
                transient_delay_seconds=transient_delay_seconds,
            )
    except Exception as exc:
        return (
            row.index,
            BatchAccountLoginResult(
                label=row.account.label,
                success=False,
                status="error",
                message=str(exc),
                session_dir=row.account.session_dir,
            ),
        )
    return row.index, _account_result_from_login_result(row.account.label, row.account.session_dir, result)


def _account_result_from_prepared(row: PreparedAccountLogin) -> BatchAccountLoginResult:
    return BatchAccountLoginResult(
        label=row.account.label,
        success=False,
        status=row.status,
        message=row.message,
        session_dir=row.account.session_dir,
    )


def _account_result_from_login_result(
    label: str,
    session_dir: str,
    result: RequestsLoginResult,
) -> BatchAccountLoginResult:
    return BatchAccountLoginResult(
        label=label,
        success=result.success,
        status=result.status,
        message=result.message,
        session_dir=session_dir,
        requests_cookies_path=result.requests_cookies_path,
        playwright_storage_state_path=result.playwright_storage_state_path,
        handled_multi_login=result.handled_multi_login,
        multi_login_action=result.multi_login_action,
        multi_login_status=result.multi_login_status,
    )


def _recognize_captcha_for_mode(
    captcha_path: str | Path,
    config: OcrConfig,
    captcha_mode: str,
    ocr_func: OcrFunc | None,
) -> OcrResult:
    if ocr_func is not None:
        return ocr_func(captcha_path, config)
    return recognize_captcha(captcha_path, config, captcha_mode)


def _reject_unsupported_config_keys(row: dict, allowed_keys: set[str], context: str) -> None:
    unsupported = sorted(str(key) for key in row if key not in allowed_keys)
    if unsupported:
        allowed = ", ".join(sorted(allowed_keys))
        found = ", ".join(unsupported)
        raise ValueError(
            f"{context} contains unsupported TOML keys after config simplification: {found}; "
            f"keep only: {allowed}"
        )


def _load_ocr_config(row: object) -> OcrConfig:
    if row is None:
        return OcrConfig(sdk=PaddleOcrSdkConfig())
    if not isinstance(row, dict):
        raise ValueError("ocr config must be a TOML table")
    _reject_unsupported_config_keys(row, OCR_CONFIG_KEYS, "ocr config")
    return OcrConfig(
        sdk=PaddleOcrSdkConfig(),
        api=PaddleOcrApiConfig(token=_optional_token(row, "paddleocr_api_token")),
    )


def _load_gemini_config(row: object) -> GeminiQuizConfig:
    if row is None:
        return GeminiQuizConfig()
    if not isinstance(row, dict):
        raise ValueError("gemini config must be a TOML table")
    _reject_unsupported_config_keys(row, GEMINI_CONFIG_KEYS, "gemini config")
    return GeminiQuizConfig(
        api_key=_optional_str(row, "api_key", ""),
        model=_optional_str(row, "model", DEFAULT_GEMINI_MODEL),
    )


def _required_str(row: dict, key: str, context: str, strip: bool = True) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} requires {key}")
    return value.strip() if strip else value


def _optional_str(row: dict, key: str, default: str) -> str:
    value = row.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip() or default


def _optional_token(row: dict, key: str) -> str:
    if key not in row:
        return ""
    value = row.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    token = value.strip()
    if not token:
        raise ValueError(f"{key} must not be empty")
    return token


def _resolve_concurrency(value: int, account_count: int) -> int:
    if account_count <= 0:
        return 1
    return min(max(1, int(value)), account_count)


def _safe_path_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    segment = segment.strip("._")
    return segment or "account"
