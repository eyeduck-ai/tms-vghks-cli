from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl

from .models import CourseDetail, CourseItem, ItemKind, ItemState
from .parsers import result_satisfies_condition
from .privacy import redact_sensitive_url, redact_sensitive_value
from .session import TmsSession
from .timeutils import parse_required_seconds, remaining_seconds


DEFAULT_NETWORK_OBSERVATIONS_PATH = ".tms_session/network_observations.jsonl"
NetworkDiagnosticAction = Literal["open-only", "read-wait", "read-finish", "form-open", "form-submit"]
NETWORK_DIAGNOSTIC_ACTIONS: tuple[str, ...] = ("open-only", "read-wait", "read-finish", "form-open", "form-submit")


@dataclass(slots=True)
class NetworkObservation:
    observed_at: str
    action: str
    item_title: str
    item_kind: str
    method: str
    url: str
    status: int | None = None
    content_type: str | None = None
    redirect_url: str | None = None
    request_header_keys: list[str] = field(default_factory=list)
    response_header_keys: list[str] = field(default_factory=list)
    post_data_keys: list[str] = field(default_factory=list)
    response_json_summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(slots=True)
class NetworkDiagnosticResult:
    output_path: str
    observation_count: int
    item_title: str
    item_kind: str
    action: str = "open-only"
    observations: list[NetworkObservation] = field(default_factory=list)
    form_summary: dict[str, Any] = field(default_factory=dict)
    mutation_attempted: bool = False
    verification: dict[str, Any] = field(default_factory=dict)


def parse_network_diagnostic_action(value: str | None) -> NetworkDiagnosticAction:
    action = (value or "open-only").strip().lower()
    if action not in NETWORK_DIAGNOSTIC_ACTIONS:
        raise ValueError(f"unsupported network diagnostic action: {value}")
    return action  # type: ignore[return-value]


def run_activity_network_diagnostic(
    session: TmsSession,
    course: CourseDetail,
    item: CourseItem,
    output_path: str | Path = DEFAULT_NETWORK_OBSERVATIONS_PATH,
    headless: bool = False,
    wait_ms: int = 2000,
    action: NetworkDiagnosticAction | str = "open-only",
) -> NetworkDiagnosticResult:
    from .handlers import TmsRunner, find_matching_item

    action = parse_network_diagnostic_action(str(action))
    _validate_action_for_item(action, item)

    session.start_browser(headless=headless)
    assert session.context is not None
    session.sync_cookies_to_browser()
    page = session.context.new_page()
    observations: list[NetworkObservation] = []
    form_summary: dict[str, Any] = {}
    verification: dict[str, Any] = {}
    page.on("response", _capture_response(observations, action, item))
    try:
        runner = TmsRunner(session)
        runner._open_item_page(page, course, item)
        if action == "form-open":
            _wait_for_quiet_page(page)
            form_summary = _rendered_form_summary(page)
            _append_summary_observation(observations, f"{action}:rendered-form-metadata", item, page_url(page), form_summary)
        elif action == "read-wait":
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
        elif action == "read-finish":
            remaining = remaining_seconds(parse_required_seconds(item.pass_condition), item.result)
            if remaining is None:
                remaining = runner._remaining_from_live_page(page)
            if remaining is None:
                remaining = 0
            _wait_real_seconds(page, remaining)
            clicked = runner._click_first_visible(page, ("結束閱讀", "完成", "結束", "確定", "送出"), timeout=3000, required=False)
            runner._recover_known_transient_dialog(page, course, item)
            session.sync_cookies_to_requests()
            refreshed = session.get_course_detail(course.url)
            verified_item = find_matching_item(refreshed, item)
            verified = _item_verified_from_detail(verified_item)

            verification = {
                "verified": verified,
                "clicked_finish_control": clicked,
                "waited_seconds": remaining,
                "refreshed_item_state": str(verified_item.state) if verified_item else "",
            }
            _append_summary_observation(observations, f"{action}:verification", item, course.url, verification)
        elif action == "form-submit":
            form_summary = _rendered_form_summary(page)
            _append_summary_observation(observations, f"{action}:rendered-form-metadata", item, page_url(page), form_summary)
            submit_summary = _submit_form_for_diagnostics(runner, page, course, item)
            session.sync_cookies_to_requests()
            refreshed = session.get_course_detail(course.url)
            verified_item = find_matching_item(refreshed, item)
            verified = _item_verified_from_detail(verified_item)
            verification = {
                "verified": verified,
                "submit": submit_summary,
                "refreshed_item_state": str(verified_item.state) if verified_item else "",
            }
            _append_summary_observation(observations, f"{action}:verification", item, course.url, verification)
        elif wait_ms > 0:
            page.wait_for_timeout(wait_ms)
    except Exception as exc:
        observations.append(
            NetworkObservation(
                observed_at=_utc_now(),
                action=action,
                item_title=item.title,
                item_kind=str(item.kind),
                method="",
                url="",
                error=str(exc),
            )
        )
    finally:
        try:
            page.close()
        except Exception:
            pass
        session.sync_cookies_to_requests()

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8", newline="\n") as handle:
        for observation in observations:
            handle.write(json.dumps(to_jsonable(observation), ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    return NetworkDiagnosticResult(
        output_path=str(output),
        observation_count=len(observations),
        item_title=item.title,
        item_kind=str(item.kind),
        action=action,
        observations=observations,
        form_summary=form_summary,
        mutation_attempted=action in MUTATING_NETWORK_DIAGNOSTIC_ACTIONS,
        verification=verification,
    )


def _validate_action_for_item(action: str, item: CourseItem) -> None:
    kind = ItemKind(item.kind)
    if action in {"read-wait", "read-finish"} and kind not in {ItemKind.READING, ItemKind.VIDEO}:
        raise ValueError(f"{action} requires a reading or video item, got {item.kind}")
    if action in {"form-open", "form-submit"} and kind not in {ItemKind.QUIZ, ItemKind.SURVEY}:
        raise ValueError(f"{action} requires a quiz or survey item, got {item.kind}")


def _item_verified_from_detail(item: CourseItem | None) -> bool:
    return bool(
        item
        and (
            item.state == ItemState.PASSED
            or result_satisfies_condition(
                item.pass_condition,
                item.result,
                item.passed_marker,
            )
        )
    )


def _wait_for_quiet_page(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def _wait_real_seconds(page, seconds: int) -> None:
    deadline = time.monotonic() + max(0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        page.wait_for_timeout(int(min(remaining, 1.0) * 1000))


def page_url(page) -> str:
    try:
        return str(page.url)
    except Exception:
        return ""


def _rendered_form_summary(page) -> dict[str, Any]:
    try:
        summary = page.evaluate(
            """
            () => {
              const text = (node) => (node.innerText || node.value || node.title || "").trim().slice(0, 80);
              const names = (selector) => Array.from(document.querySelectorAll(selector))
                .map((node) => node.name || node.id || node.getAttribute("data-name") || "")
                .filter(Boolean)
                .sort()
                .slice(0, 80);
              const unique = (values) => Array.from(new Set(values));
              const submitButtons = Array.from(document.querySelectorAll("button, input, a"))
                .filter((node) => {
                  const kind = (node.type || "").toLowerCase();
                  const label = text(node);
                  return kind === "submit" || /送出|提交|確定|交卷/.test(label);
                })
                .map(text)
                .filter(Boolean)
                .slice(0, 20);
              return {
                forms: document.querySelectorAll("form").length,
                radio_groups: unique(names('input[type="radio"]')).length,
                checkbox_groups: unique(names('input[type="checkbox"]')).length,
                text_fields: document.querySelectorAll('textarea, input[type="text"]').length,
                contenteditable_fields: document.querySelectorAll('[contenteditable="true"]').length,
                hidden_fields: names('input[type="hidden"]'),
                submit_buttons: submitButtons,
              };
            }
            """
        )
    except Exception as exc:
        summary = {"error": str(exc)}
    return redact_sensitive_value(summary if isinstance(summary, dict) else {"value": summary})


def _submit_form_for_diagnostics(runner, page, course: CourseDetail, item: CourseItem) -> dict[str, Any]:
    kind = ItemKind(item.kind)
    if kind == ItemKind.SURVEY:
        filled = _fill_neutral_survey_for_diagnostics(page)
        clicked = runner._click_first_visible(page, ("送出", "提交", "確定"), timeout=5000, required=True)
        _wait_for_quiet_page(page)
        return {"kind": "survey", "filled": filled, "clicked_submit": clicked}
    if kind == ItemKind.QUIZ:
        from .handlers import apply_quiz_answers, collect_quiz_questions, missing_required_quiz_answers
        from .quiz_resolver import resolve_quiz_answers

        questions = collect_quiz_questions(page)
        resolution = resolve_quiz_answers(
            questions=questions,
            course_title=course.title,
            item_title=item.title,
            question_bank=runner.question_bank,
            quiz_policy=runner.options.quiz_policy,
            gemini_config=runner.options.gemini_config,
        )
        if resolution.missing:
            return {
                "kind": "quiz",
                "question_count": len(questions),
                "missing_required_groups": resolution.missing,
                "clicked_submit": False,
                "answer_sources": resolution.sources,
                "answer_source_counts": resolution.source_counts,
                "issues": resolution.issues,
            }
        apply_quiz_answers(page, questions, resolution.answers)
        missing = missing_required_quiz_answers(page, questions)
        if missing:
            return {"kind": "quiz", "question_count": len(questions), "missing_required_groups": missing, "clicked_submit": False}
        clicked = runner._click_first_visible(page, ("送出", "提交", "確定", "交卷"), timeout=5000, required=True)
        _wait_for_quiet_page(page)
        return {
            "kind": "quiz",
            "question_count": len(questions),
            "clicked_submit": clicked,
            "answer_sources": resolution.sources,
            "answer_source_counts": resolution.source_counts,
            "issues": resolution.issues,
        }
    raise ValueError(f"unsupported form item kind: {item.kind}")


def _fill_neutral_survey_for_diagnostics(page) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const radioGroups = new Map();
          document.querySelectorAll('input[type="radio"]').forEach((el) => {
            const name = el.name || el.getAttribute('data-name') || el.id;
            if (!name) return;
            if (!radioGroups.has(name)) radioGroups.set(name, []);
            radioGroups.get(name).push(el);
          });
          for (const group of radioGroups.values()) {
            const target = group[Math.floor(group.length / 2)];
            if (target && !target.checked) target.click();
          }
          document.querySelectorAll('textarea, input[type="text"]').forEach((el) => {
            if (!el.value) {
              el.focus();
              el.value = "diagnostic";
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              el.blur();
            }
          });
          document.querySelectorAll('[contenteditable="true"]').forEach((el) => {
            if (!el.textContent.trim()) {
              el.focus();
              el.textContent = "diagnostic";
              el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: 'diagnostic' }));
              el.blur();
            }
          });
          return {
            radio_groups: radioGroups.size,
            text_fields: document.querySelectorAll('textarea, input[type="text"]').length,
            contenteditable_fields: document.querySelectorAll('[contenteditable="true"]').length,
          };
        }
        """
    )


def _append_summary_observation(
    observations: list[NetworkObservation],
    action: str,
    item: CourseItem,
    url: str,
    summary: dict[str, Any],
) -> None:
    observations.append(
        NetworkObservation(
            observed_at=_utc_now(),
            action=action,
            item_title=item.title,
            item_kind=str(item.kind),
            method="",
            url=redact_sensitive_url(url),
            response_json_summary=redact_sensitive_value(summary),
        )
    )


def _capture_response(observations: list[NetworkObservation], action: str, item: CourseItem):
    def on_response(response) -> None:
        try:
            request = response.request
            observations.append(
                NetworkObservation(
                    observed_at=_utc_now(),
                    action=action,
                    item_title=item.title,
                    item_kind=str(item.kind),
                    method=str(request.method),
                    url=redact_sensitive_url(str(response.url)),
                    status=int(response.status),
                    content_type=response.headers.get("content-type"),
                    redirect_url=redact_sensitive_url(str(response.headers.get("location") or "")) or None,
                    request_header_keys=sorted(str(key).lower() for key in request.headers),
                    response_header_keys=sorted(str(key).lower() for key in response.headers),
                    post_data_keys=_post_data_keys(_request_post_data(request)),
                    response_json_summary=_response_json_summary(response),
                )
            )
        except Exception as exc:
            observations.append(
                NetworkObservation(
                    observed_at=_utc_now(),
                    action=action,
                    item_title=item.title,
                    item_kind=str(item.kind),
                    method="",
                    url="",
                    error=str(exc),
                )
            )

    return on_response


def _request_post_data(request) -> str:
    value = getattr(request, "post_data", "")
    if callable(value):
        value = value()
    return value or ""


def _post_data_keys(post_data: str) -> list[str]:
    text = (post_data or "").strip()
    if not text:
        return []
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return ["<json>"]
        if isinstance(data, dict):
            return sorted(str(key) for key in data)[:80]
        return [f"<json:{type(data).__name__}>"]
    try:
        return sorted({key for key, _ in parse_qsl(text, keep_blank_values=True) if len(key) <= 80})[:80]
    except Exception:
        return []


def _response_json_summary(response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return {}
    try:
        payload = response.json()
    except Exception:
        return {"json": False}
    if isinstance(payload, dict):
        summary: dict[str, Any] = {"json": True, "keys": sorted(str(key) for key in payload)[:80]}
        for key in ("status", "success", "message", "msg", "error"):
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool, type(None))):
                summary[key] = value
        return redact_sensitive_value(summary)
    return {"json": True, "type": type(payload).__name__}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return redact_sensitive_value({key: to_jsonable(item) for key, item in asdict(value).items()})
    if isinstance(value, list):
        return redact_sensitive_value([to_jsonable(item) for item in value])
    if isinstance(value, dict):
        return redact_sensitive_value({str(key): to_jsonable(item) for key, item in value.items()})
    return redact_sensitive_value(value)


__all__ = [
    "DEFAULT_NETWORK_OBSERVATIONS_PATH",
    "MUTATING_NETWORK_DIAGNOSTIC_ACTIONS",
    "NETWORK_DIAGNOSTIC_ACTIONS",
    "NetworkDiagnosticAction",
    "NetworkDiagnosticResult",
    "NetworkObservation",
    "parse_network_diagnostic_action",
    "run_activity_network_diagnostic",
]
