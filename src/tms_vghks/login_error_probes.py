from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .batch_login import AccountLoginConfig, AccountsLoginConfig
from .captcha_recognizers import OcrConfig, OcrResult, recognize_captcha
from .privacy import redact_sensitive_value
from .requests_login import (
    classify_login_response,
    extract_login_failure_message,
    login_response_text_excerpt,
)
from .session import LOGIN_URL, TmsError, TmsSession


LOGIN_ERROR_OBSERVATIONS_FILE = "login_error_observations.jsonl"
LOGIN_ERROR_PROBE_BACKENDS = ("requests", "playwright", "both")
LOGIN_ERROR_PROBE_SCENARIOS = ("wrong-captcha", "wrong-credentials", "all")
LOGIN_ERROR_EXPECTED_FAILURE_STATUSES = {"captcha_failed", "credential_failed", "login_failed", "multi_login_failed"}
DEFAULT_WRONG_CAPTCHA = "INVALID-CAPTCHA"
DEFAULT_FAKE_PROBE_ACCOUNT = "TMS_PROBE_INVALID_ACCOUNT"
DEFAULT_FAKE_PROBE_PASSWORD = "TMS_PROBE_INVALID_PASSWORD"

SessionFactory = Callable[..., TmsSession]
OcrFunc = Callable[[str | Path, OcrConfig, str], OcrResult]


@dataclass(slots=True)
class LoginErrorProbeOptions:
    backend: str = "both"
    scenarios: str = "all"
    captcha_mode: str = "paddleocr-sdk"
    wrong_captcha: str = DEFAULT_WRONG_CAPTCHA
    fake_account: str = DEFAULT_FAKE_PROBE_ACCOUNT
    fake_password: str = DEFAULT_FAKE_PROBE_PASSWORD
    headless: bool = False
    transient_retries: int | None = 3
    transient_delay_seconds: float | None = 2.0


def run_login_error_probes(
    config: AccountsLoginConfig,
    options: LoginErrorProbeOptions,
    session_factory: SessionFactory = TmsSession,
    ocr_func: OcrFunc = recognize_captcha,
) -> list[dict[str, Any]]:
    backends = parse_login_error_probe_backends(options.backend)
    scenarios = parse_login_error_probe_scenarios(options.scenarios)
    results: list[dict[str, Any]] = []
    for account in config.accounts:
        account_results: list[dict[str, Any]] = []
        for backend in backends:
            for scenario in scenarios:
                if backend == "requests":
                    observation = run_requests_error_probe(config, account, options, scenario, session_factory, ocr_func)
                else:
                    observation = run_playwright_error_probe(config, account, options, scenario, session_factory, ocr_func)
                observation = sanitize_probe_observation(
                    observation,
                    secrets=(
                        account.account,
                        account.password,
                        options.wrong_captcha,
                        options.fake_account,
                        options.fake_password,
                    ),
                )
                observation_path = append_login_error_observation(account.session_dir, observation)
                account_results.append({**observation, "observation_path": observation_path})
        success = all(row.get("success", False) for row in account_results)
        results.append(
            {
                "label": account.label,
                "session_dir": account.session_dir,
                "success": success,
                "status": "ok" if success else "probe_failed",
                "observations": account_results,
            }
        )
    return results


def parse_login_error_probe_backends(value: str) -> list[str]:
    return ["requests", "playwright"] if value == "both" else [value]


def parse_login_error_probe_scenarios(value: str) -> list[str]:
    items = [item.strip() for item in (value or "all").split(",") if item.strip()]
    if not items or "all" in items:
        return ["wrong-captcha", "wrong-credentials"]
    allowed = {"wrong-captcha", "wrong-credentials"}
    invalid = sorted(set(items) - allowed)
    if invalid:
        raise ValueError(f"unsupported login error probe scenario(s): {', '.join(invalid)}")
    return items


def run_requests_error_probe(
    config: AccountsLoginConfig,
    account: AccountLoginConfig,
    options: LoginErrorProbeOptions,
    scenario: str,
    session_factory: SessionFactory = TmsSession,
    ocr_func: OcrFunc = recognize_captcha,
) -> dict[str, Any]:
    probe_dir = login_error_probe_dir(account.session_dir, "requests", scenario)
    captcha_path = probe_dir / "captcha.jpg"
    login_account = account.account
    login_password = account.password
    captcha_text = options.wrong_captcha
    ocr_source = ""
    ocr_confidence = None
    try:
        with session_factory(base_url=config.base_url) as requests_session:
            requests_session.configure_transient_policy(options.transient_retries, options.transient_delay_seconds)
            result = None
            for attempt in range(2 if scenario == "wrong-credentials" else 1):
                attempt_captcha_path = captcha_path if attempt == 0 else probe_dir / "captcha_retry.jpg"
                challenge = requests_session.prepare_requests_login(
                    captcha_path=attempt_captcha_path,
                    show_captcha=False,
                    session_dir=probe_dir,
                )
                if scenario == "wrong-credentials":
                    ocr_result = ocr_func(challenge.captcha_path or attempt_captcha_path, config.ocr, options.captcha_mode)
                    captcha_text = ocr_result.text
                    ocr_source = ocr_result.source
                    ocr_confidence = ocr_result.confidence
                    login_account = options.fake_account
                    login_password = options.fake_password
                result = requests_session.submit_requests_login(
                    account=login_account,
                    password=login_password,
                    captcha=captcha_text,
                    challenge=challenge,
                    save=False,
                    session_dir=probe_dir,
                    transient_retries=options.transient_retries,
                    transient_delay_seconds=options.transient_delay_seconds,
                )
                captcha_path = attempt_captcha_path
                if not (scenario == "wrong-credentials" and not result.success and result.status == "captcha_failed" and attempt == 0):
                    break
    except Exception as exc:
        return login_error_observation(
            backend="requests",
            scenario=scenario,
            classified_status="probe_error",
            failure_message=str(exc),
            captcha_image_path=str(captcha_path),
            success=False,
        )
    assert result is not None
    classified_status = "unexpected_logged_in" if result.success else result.status
    return login_error_observation(
        backend="requests",
        scenario=scenario,
        classified_status=classified_status,
        failure_message=result.failure_message or result.message,
        response_status_code=result.response_status_code,
        final_url=result.redirect_url,
        login_state_after_post=result.login_state_after_post,
        response_json_summary=login_json_summary(result.response_json),
        response_text_excerpt=result.response_text_excerpt,
        captcha_image_path=str(captcha_path),
        ocr_source=ocr_source,
        ocr_confidence=ocr_confidence,
        handled_multi_login=result.handled_multi_login,
        multi_login_action=result.multi_login_action,
        multi_login_status=result.multi_login_status,
        success=classified_status in LOGIN_ERROR_EXPECTED_FAILURE_STATUSES,
    )


def run_playwright_error_probe(
    config: AccountsLoginConfig,
    account: AccountLoginConfig,
    options: LoginErrorProbeOptions,
    scenario: str,
    session_factory: SessionFactory = TmsSession,
    ocr_func: OcrFunc = recognize_captcha,
) -> dict[str, Any]:
    probe_dir = login_error_probe_dir(account.session_dir, "playwright", scenario)
    captcha_path = probe_dir / "captcha.jpg"
    probe_dir.mkdir(parents=True, exist_ok=True)
    login_account = account.account
    login_password = account.password
    captcha_text = options.wrong_captcha
    ocr_source = ""
    ocr_confidence = None
    try:
        with session_factory(base_url=config.base_url) as playwright_session:
            playwright_session.configure_transient_policy(options.transient_retries, options.transient_delay_seconds)
            playwright_session.start_browser(headless=options.headless)
            assert playwright_session.page is not None
            page = playwright_session.page
            observation: dict[str, Any] | None = None
            for attempt in range(2 if scenario == "wrong-credentials" else 1):
                captcha_path = probe_dir / ("captcha.jpg" if attempt == 0 else "captcha_retry.jpg")
                login_account = account.account
                login_password = account.password
                captcha_text = options.wrong_captcha
                goto_probe_login_page(playwright_session, page)
                playwright_session.recover_transient_page(page, LOGIN_URL)
                captcha_locator = first_visible_login_locator(
                    page,
                    (
                        "img.js-captcha",
                        "form#login_form img[src*='captcha']",
                        "form#login_form img[src*='capcha']",
                        "img[src*='captcha']",
                        "img[src*='capcha']",
                    ),
                    "captcha image",
                )
                captcha_locator.screenshot(path=str(captcha_path))
                if scenario == "wrong-credentials":
                    ocr_result = ocr_func(captcha_path, config.ocr, options.captcha_mode)
                    captcha_text = ocr_result.text
                    ocr_source = ocr_result.source
                    ocr_confidence = ocr_result.confidence
                    login_account = options.fake_account
                    login_password = options.fake_password
                first_visible_login_locator(page, ("input[name='account']", "#account"), "account input").fill(login_account)
                first_visible_login_locator(page, ("input[name='password']", "#password"), "password input").fill(login_password)
                first_visible_login_locator(page, ("input[name='captcha']", "#captcha"), "captcha input").fill(captcha_text)
                response = submit_playwright_login_probe(page)
                response_json = playwright_response_json(response)
                response_text = playwright_response_text(response)
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                status = playwright_session.browser_status()
                body_text = playwright_body_text(page)
                classified_status = (
                    "unexpected_logged_in"
                    if status.logged_in
                    else classify_login_response(response_json, body_text or response_text, False)
                )
                failure_message = "" if status.logged_in else extract_login_failure_message(
                    response_json,
                    body_text or response_text,
                )
                observation = login_error_observation(
                    backend="playwright",
                    scenario=scenario,
                    classified_status=classified_status,
                    failure_message=failure_message or status.message,
                    response_status_code=getattr(response, "status", None),
                    final_url=status.url or page.url,
                    login_state_after_post=str(status.state),
                    response_json_summary=login_json_summary(response_json),
                    response_text_excerpt=login_response_text_excerpt(
                        body_text or response_text,
                        account=login_account,
                        password=login_password,
                        captcha=captcha_text,
                    ),
                    captcha_image_path=str(captcha_path),
                    ocr_source=ocr_source,
                    ocr_confidence=ocr_confidence,
                    success=classified_status in LOGIN_ERROR_EXPECTED_FAILURE_STATUSES,
                )
                if not (scenario == "wrong-credentials" and classified_status == "captcha_failed" and attempt == 0):
                    return observation
            assert observation is not None
            return observation
    except Exception as exc:
        return login_error_observation(
            backend="playwright",
            scenario=scenario,
            classified_status="probe_error",
            failure_message=str(exc),
            captcha_image_path=str(captcha_path),
            success=False,
        )


def login_error_observation(
    backend: str,
    scenario: str,
    classified_status: str,
    failure_message: str = "",
    response_status_code: int | None = None,
    final_url: str | None = None,
    login_state_after_post: str = "",
    response_json_summary: dict[str, Any] | None = None,
    response_text_excerpt: str = "",
    captcha_image_path: str = "",
    ocr_source: str = "",
    ocr_confidence: float | None = None,
    handled_multi_login: bool = False,
    multi_login_action: str = "",
    multi_login_status: str = "",
    success: bool = True,
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "scenario": scenario,
        "success": success,
        "classified_status": classified_status,
        "recommended_action": login_error_recommended_action(classified_status),
        "failure_message": failure_message,
        "response_status_code": response_status_code,
        "final_url": final_url,
        "login_state_after_post": login_state_after_post,
        "response_json_summary": response_json_summary or {},
        "response_text_excerpt": response_text_excerpt,
        "captcha_image_path": captcha_image_path,
        "ocr_source": ocr_source,
        "ocr_confidence": ocr_confidence,
        "handled_multi_login": handled_multi_login,
        "multi_login_action": multi_login_action,
        "multi_login_status": multi_login_status,
    }


def login_error_recommended_action(status: str) -> str:
    return {
        "captcha_failed": "refresh_captcha_retry_ocr_then_manual",
        "credential_failed": "stop_account_no_retry",
        "multi_login_failed": "run_keep_login_flow_then_diagnostics",
        "transient_error": "retry_with_backoff",
        "login_failed": "save_observation_and_run_login_diagnostics",
        "unexpected_logged_in": "do_not_save_probe_session_mark_anomaly",
        "probe_error": "inspect_probe_error",
    }.get(status, "save_observation_and_run_login_diagnostics")


def login_error_probe_dir(session_dir: str, backend: str, scenario: str) -> Path:
    return Path(session_dir) / "error_probes" / backend / scenario


def append_login_error_observation(session_dir: str, observation: dict[str, Any]) -> str:
    path = Path(session_dir) / LOGIN_ERROR_OBSERVATIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = redact_sensitive_value(observation)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
    return str(path)


def sanitize_probe_observation(observation: dict[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    return redact_sensitive_value(redact_known_strings(observation, secrets))


def redact_known_strings(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        text = redact_diagnostic_string(value)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "REDACTED")
        return text
    if isinstance(value, list):
        return [redact_known_strings(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_known_strings(item, secrets) for item in value)
    if isinstance(value, dict):
        return {key: redact_known_strings(item, secrets) for key, item in value.items()}
    return value


def submit_playwright_login_probe(page):
    with page.expect_response(
        lambda response: response.request.method.upper() == "POST" and "/index/login" in response.url,
        timeout=15000,
    ) as response_info:
        page.evaluate(
            """
            () => {
              const button = document.querySelector("#login_form [data-role='form-submit']");
              if (button) {
                if (window.jQuery) {
                  window.jQuery(button).trigger("click");
                } else {
                  button.click();
                }
                return;
              }
              const form = document.querySelector("form#login_form");
              if (form) form.submit();
            }
            """
        )
    return response_info.value


def goto_probe_login_page(playwright_session: TmsSession, page) -> None:
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            playwright_session._goto_page(page, LOGIN_URL)
            return
        except Exception as exc:
            last_error = exc
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
    if last_error is not None:
        raise last_error


def first_visible_login_locator(page, selectors: tuple[str, ...], description: str):
    last_error = ""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=3000):
                return locator
        except Exception as exc:
            last_error = str(exc)
    suffix = f": {last_error}" if last_error else ""
    raise TmsError(f"{description} was not found on the login page{suffix}")


def playwright_response_json(response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def playwright_response_text(response) -> str:
    try:
        return response.text()
    except Exception:
        return ""


def playwright_body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def login_json_summary(data: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"json": True}
    if isinstance(data, dict):
        summary["keys"] = sorted(str(key) for key in data)[:50]
        ret = data.get("ret")
        if isinstance(ret, dict):
            summary["ret"] = {
                "status": ret.get("status"),
                "message": ret.get("message") or ret.get("msg"),
                "action": sorted(str(key) for key in ret.get("action", {})) if isinstance(ret.get("action"), dict) else None,
            }
            if isinstance(ret.get("action"), (dict, list, str)):
                summary["ret"]["action_summary"] = summarize_login_action(ret["action"])
        elif isinstance(ret, (str, int, float, bool, type(None))):
            summary["ret"] = ret
    elif data is None:
        summary = {}
    else:
        summary["type"] = type(data).__name__
    return redact_sensitive_value(summary)


def summarize_login_action(value: Any) -> Any:
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "items": [summarize_login_action(item) for item in value[:5]],
        }
    if isinstance(value, dict):
        selected: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if key_lower in {"account", "password", "captcha", "token", "anticsrf", "cookie", "cookies"}:
                selected[key_text] = "REDACTED"
            elif isinstance(item, (str, int, float, bool, type(None))) and key_lower in {
                "type",
                "kind",
                "name",
                "method",
                "target",
                "url",
                "href",
                "path",
                "location",
                "msg",
                "message",
                "text",
                "title",
                "customjs",
            }:
                selected[key_text] = redact_diagnostic_string(item[:300]) if isinstance(item, str) else redact_sensitive_value(item)
        return {
            "type": "dict",
            "keys": sorted(str(key) for key in value)[:50],
            "selected": selected,
        }
    if isinstance(value, str):
        return {"type": "str", "excerpt": redact_diagnostic_string(value[:300])}
    return {"type": type(value).__name__}


def redact_diagnostic_string(value: str) -> str:
    text = re.sub(
        r"([?&](?:ajaxAuth|key|token|authorization|auth|signature|sig)=)[^'\"&\s)]+",
        r"\1REDACTED",
        value,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b((?:ajaxAuth|key|token|authorization|auth|signature|sig)=)[^'\"&\s)]+",
        r"\1REDACTED",
        text,
        flags=re.IGNORECASE,
    )
    return redact_sensitive_value(text)
